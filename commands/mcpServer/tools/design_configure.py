# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""BUILDS and switches a Configured Design: converts the active design and defines configuration rows
plus the columns that vary across them (a parameter, a feature suppress, a body visibility, a
per-config appearance or material theme, or a nested part insert). One action-dispatched verb for the whole
configuration-table subsystem; design_get(include=['configurations']) reads the table; this WRITES it.
"""

import json

import adsk.core
import adsk.fusion

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import ok, error, iter_collection, safe
from . import _common
from . import _data_common
from . import _inputs

app = adsk.core.Application.get()

_BODY = _inputs.BodyRef("body")


def _find_row(table, target):
    """Find a configuration row by exact name or id (name first, then id), or None."""
    target = target.strip()
    rows = safe(lambda: table.rows)
    if not rows:
        return None
    for attr in ("name", "id"):
        try:
            for r in rows:
                if (safe(lambda r=r, a=attr: getattr(r, a)) or "") == target:
                    return r
        except Exception:
            pass
    return None

_ACTIONS = ("create", "activate", "add_configuration", "rename_configuration", "add_parameter",
            "add_suppress", "add_visibility", "set_appearance", "add_material", "add_insert")


# ── resolvers (patched in tests; real lookups here) ─────────────────────────

# Timeline entity names are NOT design-wide unique (two components can each hold an 'Extrude1'), so
# the suppress column's target goes through FeatureRef, which refuses the ambiguity.
_FEATURE = _inputs.FeatureRef("feature")


def _resolve_appearance(design, name):
    """An appearance by name already present in the design's appearances."""
    apps = safe(lambda: design.appearances)
    return safe(lambda: apps.itemByName(name)) if apps else None


def _resolve_material(design, name):
    """A physical material by EXACT (case-insensitive) name among the design's OWN materials.
    Returns (material, error_or_None). A material cell only accepts a material that already exists
    in the design, so a library material has to be copied in before it can be configured. A name two
    document materials share is refused rather than resolved to whichever comes first."""
    want = (name or "").strip()
    if not want:
        return None, "Provide a material name for every configuration in 'materials'."
    hits, names = [], []
    for m in iter_collection(safe(lambda: design.materials)):
        nm = safe(lambda m=m: m.name)
        if not nm:
            continue
        names.append(nm)
        if nm.lower() == want.lower():
            hits.append(m)
    listed = ", ".join(f"'{n}'" for n in sorted(set(names))[:12]) or "none"
    if not hits:
        return None, (f"No material named '{want}' in the design. Copy it into the design first "
                      "(design.materials.addByCopy from a loaded material library) - a configuration "
                      f"material cell only accepts a material the design already holds. In the "
                      f"design now: {listed}.")
    if len(hits) > 1:
        return None, (f"'{want}' names {len(hits)} of the design's materials - refusing to pick one. "
                      "Rename or remove the duplicate, then retry.")
    return hits[0], None


def _resolve_datafile(design, name_or_id):
    """(DataFile, problem): a configured-design DataFile by lineage id (a urn, or a Fusion web/share
    URL), else by name in the ACTIVE DOCUMENT's own project - the project a referenced insert
    requires the part to share. Patched in tests."""
    # A urn or a pasted Fusion web URL: route through the shared candidate decoder, which also
    # base64url-decodes the lineage segment a share link carries (a raw startswith('urn:') check
    # silently failed on a pasted URL).
    df, _resolved, _tried = _data_common._resolve_data_file(name_or_id)
    if df:
        return df, None
    proj, problem = _data_common.active_project()
    if not proj:
        return None, problem
    folder = safe(lambda: proj.rootFolder)
    if folder is None:
        return None, f"the root folder of project '{safe(lambda: proj.name)}' does not read."
    # A folder can hold several files of one name (distinct lineages), so the shared resolver
    # refuses that rather than handing back whichever comes first.
    hit, shared = _data_common._file_in_folder_by_name(folder, name_or_id)
    if shared:
        return None, shared
    return hit, None


def _part_config_rows(datafile):
    """{name: ConfigurationRow} from a configured part's DataFile table."""
    ct = safe(lambda: datafile.configurationTable)
    out = {}
    if not ct:
        return out
    rows = safe(lambda: ct.rows)
    for i in range(safe(lambda: rows.count, 0) or 0):
        r = safe(lambda i=i: rows.item(i))
        nm = safe(lambda r=r: r.name)
        if r is not None and nm:
            out[nm] = r
    return out


def _top_table(design):
    return safe(lambda: design.configurationTopTable)


def _doc_is_saved():
    """True if the active document has ever been saved (has a cloud file) - the configured-design
    conversion only materializes for the user once saved. Patched in tests."""
    return bool(safe(lambda: app.activeDocument.isSaved, False))


def _row_names(table):
    out = []
    for i in range(safe(lambda: table.rows.count, 0) or 0):
        out.append(safe(lambda i=i: table.rows.item(i).name))
    return out


# The unit a TEXT parameter reads (measured: a text parameter reads 'Text', a length one 'mm').
_TEXT_UNIT = "Text"


def _column_ids(table):
    """Every column id the table lists, or None when the collection would not enumerate - an
    unreadable list is not an empty one, and a rollback may not be claimed on it."""
    cols = safe(lambda: table.columns)
    n = safe(lambda: cols.count) if cols is not None else None
    if not isinstance(n, int) or isinstance(n, bool):
        return None
    return [safe(lambda i=i: cols.item(i).id) for i in range(n)]


def _column_rolled_back(table, col):
    """Remove a column whose cells could not be filled and PROVE it is gone - the table must stop
    listing its id. deleteMe answering true is the request, not the proof."""
    col_id = safe(lambda: col.id)
    if not safe(lambda: col.deleteMe()):
        return False
    after = _column_ids(table)
    return after is not None and col_id is not None and col_id not in after


# ── action handlers ──────────────────────────────────────────────────────────

def _do_create(design):
    if _top_table(design):
        return ok({"configured": True, "created": False,
                   "note": "Design is already a configured design; reusing its configuration table. "
                           "Add configurations with action='add_configuration' and columns with "
                           "add_parameter / add_suppress / add_visibility / set_appearance."})
    if not _doc_is_saved():
        return error("Save the document first (doc_save_as), THEN run create. Converting an unsaved "
                     "document builds the table only in memory - it won't materialize as a configured "
                     "design (no DataFile to carry it, and the UI won't show the Configurations panel). "
                     "Save is the commit step for the conversion.")
    table = design.createConfiguredDesign()      # MUTATION - let it raise on failure
    if not table:
        return error("createConfiguredDesign() returned no table.")
    return ok({"configured": True, "created": True,
               "configurations": _row_names(table),
               "note": "Design converted to a configured design (one configuration so far). Add "
                       "configurations and columns with the other actions. To see it in the UI: SAVE, "
                       "then REOPEN by the URN that save reports - the first save moves this document "
                       "to a NEW lineage, and the pre-conversion one is relocated to a Fusion-managed "
                       "project ('System Project - CONFIG') that deleting this design leaves behind."})


def _do_activate(design, table, name):
    """Switch the live design to a configuration (by name or id) and rebuild, so its geometry shows."""
    target = (name or "").strip()
    if not target:
        return error("Provide 'name' - the configuration to activate (a configuration name or id).")
    row = _find_row(table, target)
    if not row:
        avail = ", ".join(_row_names(table)) or "(none)"
        return error(f"No configuration matched '{target}'. Available: {avail}.")
    before = safe(lambda: table.activeRow.name)
    err_before, _, _ = _common.timeline_health(design)
    if not safe(lambda: row.activate(), False):
        return error(f"Activating configuration '{target}' failed (activate() returned false).")
    safe(lambda: design.computeAll()) # rebuild so the switched geometry shows
    now = safe(lambda: table.activeRow.name)
    want_row = safe(lambda: row.name)
    if now is not None and want_row is not None and now != want_row:
        return error(f"activate() returned true but the active configuration still reads '{now}' "
                     f"(expected '{want_row}') - the switch did not take.")
    # A different configuration can drive parameters that over/under-constrain the model, so the
    # rebuilt geometry may carry NEW timeline errors even though the switch itself took. Surface them
    # (the switch stands) rather than reporting a clean success over a broken model.
    err_after, warn_after, _ = _common.timeline_health(design)
    out = {"activated": True, "requested": target, "previous": before,
           "now_active": now,
           "note": "Configuration switched + rebuilt. Pair with view_screenshot to view it, or "
                   "design_get(include=['timeline']) / param_get to see what changed."}
    if len(err_after) > len(err_before):
        out["timeline_warning"] = (
            f"Switching to '{target}' left the timeline with a new error ({err_after}). This "
            "configuration's values may over/under-constrain the model - the switch stands; inspect "
            "with design_get(include=['timeline']).")
    elif warn_after:
        out["timeline_warnings"] = warn_after
    return ok(out)


def _do_add_configuration(table, name):
    name = (name or "").strip()
    if not name:
        return error("Provide 'name' for the new configuration (e.g. 'Large').")
    if name in _row_names(table):
        return error(f"A configuration named '{name}' already exists.")
    row = table.rows.add(name)                   # MUTATION
    if not row:
        return error(f"Adding configuration '{name}' failed.")
    # Adding a row to the TOP table ACTIVATES the new configuration - the design now shows it, not
    # the one that was active before the call. Publish the read-back so the caller can see the switch.
    return ok({"configuration": name, "id": safe(lambda: row.id),
               "configurations": _row_names(table),
               "active_configuration": safe(lambda: table.activeRow.name),
               "note": "Configuration row added, and adding it ACTIVATED it - 'active_configuration' "
                       "is what the design shows now; switch back with action='activate'. Set its "
                       "values via add_parameter/add_suppress/add_visibility/set_appearance/"
                       "add_material (address by this name)."})


def _do_rename_configuration(table, name, new_name):
    """Rename an existing configuration row (the default row is 'Configuration 1' - usually worth
    renaming to something meaningful like 'Medium')."""
    name = (name or "").strip()
    new_name = (new_name or "").strip()
    if not name or not new_name:
        return error("Provide 'name' (the existing configuration) and 'new_name' (what to call it).")
    row = _find_row(table, name)
    if not row:
        return error(f"No configuration named '{name}'. Existing: "
                     f"{', '.join(str(n) for n in _row_names(table))}.")
    if new_name in _row_names(table) and new_name != name:
        return error(f"A configuration named '{new_name}' already exists.")
    row.name = new_name                          # MUTATION
    if safe(lambda: row.name) != new_name:
        return error(f"Renaming '{name}' to '{new_name}' did not take (the API may still be persisting "
                     "a recent save - retry shortly).")
    return ok({"renamed": True, "from": name, "to": new_name,
               "configurations": _row_names(table),
               "note": "Configuration renamed. Address it by the new name from now on."})


def _validate_rows(table, value_map):
    """Every key in value_map must be an existing configuration row name."""
    names = set(_row_names(table))
    unknown = [k for k in value_map if k not in names]
    return unknown


def _do_add_parameter(design, table, parameter, values):
    if not parameter:
        return error("Provide 'parameter' - the name of a model parameter to vary across configurations.")
    values = values or {}
    p = safe(lambda: design.allParameters.itemByName(parameter))
    if not p:
        return error(f"No parameter named '{parameter}'. (Add/expose it first; a parameter column "
                     "only matters if the parameter drives geometry.)")
    unknown = _validate_rows(table, values)
    if unknown:
        return error(f"Values reference configurations that don't exist: {', '.join(unknown)}. "
                     f"Existing: {', '.join(str(n) for n in _row_names(table))}.")
    # A TEXT parameter's column is refused BEFORE it lands rather than rolled back after. Measured
    # on the owner's open Configured Dumbbell: a text parameter reads unit 'Text', and its per-row
    # cells read the expression quoted ('10'), with the value only on textValue.
    if safe(lambda: p.unit) == _TEXT_UNIT:
        return error(f"'{parameter}' is a {_TEXT_UNIT} parameter: its cells read the expression "
                     "quoted ('10'), with the value only on textValue. This tool sets and verifies "
                     "plain expressions, so it does not configure a Text column. Vary a "
                     "length/number parameter in the table, and relabel per configuration with "
                     "param_set after activating the row.")
    col = table.columns.addParameterColumn(p)    # MUTATION
    if not col:
        return error(f"addParameterColumn for '{parameter}' returned null.")
    n = 0
    for rname, expr in values.items():
        cell = safe(lambda rname=rname: col.getCellByRowName(rname))
        if cell is None:
            return error(f"No cell for configuration '{rname}' in the '{parameter}' column.")
        cell.expression = str(expr) # MUTATION
        # An unreadable read-back is a FAILURE, not a pass - the same rule the material path
        # states; a silently dropped write reads back None here.
        got = safe(lambda cell=cell: cell.expression)
        if got is None or got != str(expr):
            # The COLUMN is a mutation that already landed, so a bare refusal would leave an
            # orphan behind and every retry would add another. Roll it back and say which happened.
            removed = _column_rolled_back(table, col)
            return error(f"Cell '{rname}' of the '{parameter}' column reads "
                         f"{'nothing' if got is None else repr(got)} after the set - the "
                         f"expression '{expr}' did not verifiably take"
                         + (f" ({n} earlier cell(s) did)" if n else "") + ". "
                         + ("The column has been rolled back." if removed else
                            "The column could NOT be auto-removed and is still on the table - "
                            "delete it before retrying.")
                         + " Relabel per configuration with param_set after activating the row.")
        n += 1
    return ok({"parameter": parameter, "column_id": safe(lambda: col.id), "set": n,
               "note": "Parameter column added and per-configuration expressions set. Switch with "
                       "design_configure(action='activate', name=...) - the geometry rebuilds only if this "
                       "parameter drives a dimension."})


def _do_add_suppress(design, table, feature, suppressed_in):
    if not feature:
        return error("Provide 'feature' - the timeline feature name to suppress per configuration.")
    resolved, ferr = _FEATURE.resolve(feature)
    if ferr:
        return error(ferr)
    feat, feature = resolved            # publish the TIMELINE name that resolved, not the raw input
    suppressed_in = suppressed_in or []
    unknown = [r for r in suppressed_in if r not in set(_row_names(table))]
    if unknown:
        return error(f"suppressed_in names unknown configurations: {', '.join(unknown)}.")
    col = table.columns.addSuppressColumn(feat)  # MUTATION
    if not col:
        return error(f"addSuppressColumn for '{feature}' returned null.")
    for rname in suppressed_in:
        cell = safe(lambda rname=rname: col.getCellByRowName(rname))
        if cell is None:
            return error(f"No suppress cell for configuration '{rname}'.")
        cell.isSuppressed = True # MUTATION
        # read_flag semantics by hand: True is the only pass; False AND unreadable both fail.
        if safe(lambda cell=cell: cell.isSuppressed) is not True:
            return error(f"Suppress cell '{rname}' does not read suppressed after the set - the "
                         "suppression did not verifiably take.")
    return ok({"feature": feature, "suppressed_in": suppressed_in,
               "note": "Suppress column added; the feature is suppressed in the listed configurations "
                       "(present in the others)."})


def _do_add_visibility(design, table, body, hidden_in):
    if not body:
        return error("Provide 'body' - the body name whose visibility varies per configuration.")
    ent, body_err = _BODY.resolve(body)
    if body_err:
        return error(body_err)
    hidden_in = hidden_in or []
    unknown = [r for r in hidden_in if r not in set(_row_names(table))]
    if unknown:
        return error(f"hidden_in names unknown configurations: {', '.join(unknown)}.")
    col = table.columns.addVisibilityColumn(ent)  # MUTATION
    if not col:
        return error(f"addVisibilityColumn for '{body}' returned null.")
    for rname in hidden_in:
        cell = safe(lambda rname=rname: col.getCellByRowName(rname))
        if cell is None:
            return error(f"No visibility cell for configuration '{rname}'.")
        cell.isVisible = False # MUTATION
        # False is the only pass; still-visible AND unreadable both fail (unreadable != hidden).
        if safe(lambda cell=cell: cell.isVisible) is not False:
            return error(f"Visibility cell '{rname}' does not read hidden after the set - the hide "
                         "did not verifiably take.")
    return ok({"body": body, "hidden_in": hidden_in,
               "note": "Visibility column added; the body is hidden in the listed configurations."})


def _do_set_appearance(design, table, body, appearances):
    if not body:
        return error("Provide 'body' - the body to color per configuration.")
    ent, body_err = _BODY.resolve(body)
    if body_err:
        return error(body_err)
    appearances = appearances or {}
    unknown = _validate_rows(table, appearances)
    if unknown:
        return error(f"Appearance map references unknown configurations: {', '.join(unknown)}.")
    # Resolve every named appearance up front (fail before mutating).
    resolved = {}
    for rname, aname in appearances.items():
        a = _resolve_appearance(design, aname)
        if not a:
            return error(f"No appearance named '{aname}' in the design. Copy it in first "
                         "(design.appearances.addByCopy) - appearance_set copies the Fusion "
                         "Appearance Library's 'Paint - Enamel Glossy (White)' and keeps it in the "
                         "document as 'MCP Neutral Base'.")
        resolved[rname] = a

    appt = safe(lambda: table.appearanceTable)
    if not appt:
        return error("This design has no appearance table.")
    # ORDERING GOTCHA: add the body column FIRST (auto-creates the first theme row), then add extra
    # theme rows so there is one theme per configuration we want to color.
    col = appt.columns.add(ent)                   # MUTATION (creates first theme row)
    if not col:
        return error(f"appearanceTable.columns.add for '{body}' returned null.")
    needed = len(resolved)
    # The adds go through the shared minting rule, which owns the fact that rows.add of a taken name
    # adds NOTHING and hands that row back. Bounded by the number wanted - a census that will not
    # read counts as no rows - and the count REACHED is asserted below rather than trusted.
    for _ in range(needed):
        rows = _theme_rows(appt)
        if len(rows) >= needed:
            break
        _mint_theme_row(appt, rows, prefix="Theme")      # MUTATION
    reached = _common.counted(lambda: appt.rows.count)
    if reached is None or reached < needed:
        return error(f"The appearance table holds {reached} theme rows after adding, not the "
                     f"{needed} this call needs - one per configuration named in 'appearances'. "
                     "Name fewer configurations, or add the theme rows in the UI first.")

    # Assign each named appearance to a distinct theme row, and link config row -> theme row.
    theme_col = safe(lambda: appt.parentTableColumn)
    if theme_col is None:
        return error("The appearance table has no theme column (parentTableColumn) to link configurations.")
    theme_rows = _theme_rows(appt)
    set_count = 0
    for theme_idx, (rname, appearance) in enumerate(resolved.items()):
        theme_row, theme_name = (theme_rows[theme_idx] if theme_idx < len(theme_rows)
                                 else (None, None))
        cell = safe(lambda theme_name=theme_name: col.getCellByRowName(theme_name))
        if cell is None or theme_row is None:
            return error(f"No appearance cell/row on theme row {theme_name!r} (for configuration "
                         f"'{rname}').")
        cell.appearance = appearance # MUTATION (assign appearance to this theme row)
        # An unreadable read-back is a FAILURE, not a pass (the material path's stated rule).
        got = safe(lambda cell=cell: cell.appearance.name)
        if got is None or got != safe(lambda: appearance.name):
            return error(f"Appearance cell on theme row '{theme_name}' reads "
                         f"{'nothing' if got is None else repr(got)} after the set - the "
                         "assignment did not verifiably take.")
        # Link the configuration row to this theme row. CRITICAL: the theme column's getCell(index)
        # does NOT share top.rows ordering - addressing by positional index links the WRONG config
        # (live-caught: Small got Large's theme). Address the theme cell by the CONFIG ROW NAME.
        tcell = safe(lambda rname=rname: theme_col.getCellByRowName(rname))
        if tcell is None:
            return error(f"No theme cell for configuration '{rname}'.")
        tcell.referencedTableRow = theme_row # MUTATION
        got_row = safe(lambda tcell=tcell: tcell.referencedTableRow.name)
        if got_row is None or got_row != safe(lambda theme_row=theme_row: theme_row.name):
            return error(f"Configuration '{rname}' links theme "
                         f"{'nothing readable' if got_row is None else repr(got_row)} after the "
                         "set - the theme link did not verifiably take.")
        set_count += 1
    return ok({"body": body, "themes": set_count,
               "note": "Appearance theme column added and configurations linked to theme rows. Switch "
                       "configurations to see the color change (design_configure(action='activate', name=...))."})


def _theme_rows(mtbl):
    """[(row, name)] for every theme row of a theme table. The NAME is the argument a column's
    getCellByRowName takes - ConfigurationMaterialColumn and ConfigurationAppearanceColumn both
    expose it; a column's getCell index runs over the column's own rowCount, which is not the
    table's row order."""
    out = []
    for i in range(safe(lambda: mtbl.rows.count, 0) or 0):
        r = safe(lambda i=i: mtbl.rows.item(i))
        if r is not None:
            out.append((r, safe(lambda r=r: r.name)))
    return out


def _theme_links(table, theme_col):
    """{configuration name: the name of the theme row it references} for every configuration row."""
    links = {}
    for cname in _row_names(table):
        cell = safe(lambda cname=cname: theme_col.getCellByRowName(cname))
        links[cname] = safe(lambda cell=cell: cell.referencedTableRow.name) if cell is not None else None
    return links


def _carry_theme_materials(mtbl, from_name, to_name):
    """Copy every column's material from one theme row to another. A minted row copies the row ABOVE
    it, which is not the row the configuration being moved was referencing - without this carry, the
    OTHER bodies in that configuration silently take some other configuration's materials. Returns
    the title of a column whose copy did not read back, or None."""
    for c in iter_collection(safe(lambda: mtbl.columns)):
        src = safe(lambda c=c: c.getCellByRowName(from_name))
        dst = safe(lambda c=c: c.getCellByRowName(to_name))
        mat = safe(lambda src=src: src.material) if src is not None else None
        if dst is None or mat is None:
            continue
        dst.material = mat # MUTATION (carry the outgoing row's material onto the new one)
        want = safe(lambda mat=mat: mat.name)
        if safe(lambda dst=dst: dst.material.name) != want:
            return safe(lambda c=c: c.title) or "a column"
    return None


def _mint_theme_row(mtbl, rows, prefix="Material"):
    """Add a theme row named '<prefix> N' under a name no existing row carries: (row, name).
    The name search is load-bearing: rows.add(<a name an existing row carries>) adds NOTHING and
    returns THAT row, so a colliding name would hand this configuration a row another configuration
    already references."""
    taken = {n for (_r, n) in rows}
    n = len(rows) + 1
    while ("%s %d" % (prefix, n)) in taken:
        n += 1
    name = "%s %d" % (prefix, n)
    return mtbl.rows.add(name), name                  # MUTATION


def _do_add_material(design, table, body, materials):
    if not body:
        return error("Provide 'body' - the body whose physical material varies per configuration.")
    ent, body_err = _BODY.resolve(body)
    if body_err:
        return error(body_err)
    materials = materials or {}
    if not materials:
        return error("Provide 'materials' - {configuration_name: material_name} for at least one "
                     "configuration.")
    unknown = _validate_rows(table, materials)
    if unknown:
        return error(f"Material map references unknown configurations: {', '.join(unknown)}. "
                     f"Existing: {', '.join(str(n) for n in _row_names(table))}.")
    # Resolve every named material up front (fail before mutating).
    resolved = {}
    for rname, mname in materials.items():
        m, merr = _resolve_material(design, mname)
        if merr:
            return error(merr)
        resolved[rname] = m

    mtbl = safe(lambda: table.materialTable)
    if not mtbl:
        return error("This design has no material table.")
    # Add the body column FIRST: on an empty table it also mints the first theme row, and the first
    # non-root column auto-creates a root-component column ahead of it, so the column COUNT jumps by
    # two - the returned column object is the signal, never the count.
    col = mtbl.columns.add(ent)                   # MUTATION (creates first theme row)
    if not col:
        return error(f"materialTable.columns.add for '{body}' returned null.")
    theme_col = safe(lambda: mtbl.parentTableColumn)
    if theme_col is None:
        return error("The material table has no theme column (parentTableColumn) to link configurations.")

    # Theme rows and the configuration -> theme link are table-GLOBAL, and a fresh column leaves
    # every configuration on the one auto-created row - so a row shared with another configuration
    # is replaced by a minted one rather than written through.
    links = _theme_links(table, theme_col)
    applied = {}
    for rname, material in resolved.items():
        rows = _theme_rows(mtbl)
        cur = links.get(rname)
        held = next(((r, n) for (r, n) in rows if n == cur), None) if cur else None
        shared = [c for c, t in links.items() if t == cur and c != rname]
        if held is None or shared:
            new_row, new_name = _mint_theme_row(mtbl, rows)
            if not new_row:
                return error(f"Adding a theme row for configuration '{rname}' returned null.")
            rows = _theme_rows(mtbl)
            entry = next(((r, n) for (r, n) in rows if r is new_row or n == new_name), None)
            if entry is None:
                return error(f"Theme row '{new_name}' is not in the material table after adding it.")
            if held is not None:
                bad = _carry_theme_materials(mtbl, held[1], entry[1])
                if bad:
                    return error(f"Configuration '{rname}' moved to a new theme row, but column "
                                 f"'{bad}' did not carry its material over - the material table is "
                                 "partially built; inspect it before retrying.")
        else:
            entry = held
        theme_row, want_row = entry

        cell = safe(lambda want_row=want_row: col.getCellByRowName(want_row))
        if cell is None:
            return error(f"No material cell on theme row '{want_row}' for configuration '{rname}'.")
        cell.material = material # MUTATION (assign material to this configuration's theme row)
        want = safe(lambda material=material: material.name)
        got = safe(lambda cell=cell: cell.material.name)
        # An unreadable read-back is a failure, not a pass: an unset cell leaves that configuration
        # carrying whatever material it had, which looks correct in a payload that echoes the request.
        if got is None:
            return error(f"Material cell for configuration '{rname}' reads back no material after "
                         f"setting '{want}' - the assignment could not be confirmed.")
        if got != want:
            return error(f"Material cell for configuration '{rname}' still reads '{got}' after "
                         f"the set - the material '{want}' did not take.")
        # Link the configuration row to its theme row, addressed by the CONFIG ROW NAME: the theme
        # column's getCell(index) does NOT share top.rows ordering, so a positional index can hand
        # one configuration another configuration's theme.
        tcell = safe(lambda rname=rname: theme_col.getCellByRowName(rname))
        if tcell is None:
            return error(f"No theme cell for configuration '{rname}'.")
        tcell.referencedTableRow = theme_row # MUTATION
        got_row = safe(lambda tcell=tcell: tcell.referencedTableRow.name)
        if got_row is None or got_row != want_row:
            return error(f"Configuration '{rname}' links theme '{got_row}' after the set (expected "
                         f"'{want_row}') - the theme link did not take.")
        links[rname] = want_row
        applied[rname] = got

    unset = [c for c in _row_names(table) if c not in applied]
    note = ("Material column added for this body and each listed configuration linked to its own "
            "theme row (calling this again for the same body updates that column rather than adding "
            "a second one). The names above are read back off the table cells. Fusion applies a "
            "configuration's material to the geometry after that configuration is activated, and the "
            "application trails the switch - a body material read right after "
            "design_configure(action='activate', name=...) can still report the other "
            "configuration's material, so read the table, not the body, to confirm what is configured.")
    out = {"body": body, "themes": len(applied), "materials": applied,
           "column_id": safe(lambda: col.id), "column_title": safe(lambda: col.title)}
    if unset:
        out["configurations_unset"] = unset
        note += (" Configurations you did not name (" + ", ".join(unset) + ") keep the theme row they "
                 "already reference, so this body's material in them is whatever that row holds - "
                 "name them in 'materials' to set them.")
    out["note"] = note
    return ok(out)


def _do_add_insert(design, table, insert_part, insert_config, insert_map):
    """Insert a configured PART into this (assembly) configured design and map each assembly
    configuration to one of the part's configurations - a NESTED configuration. The part must be in
    the same project as the assembly (referenced insert requires it)."""
    if not insert_part:
        return error("Provide 'insert_part' - the configured part to insert (lineage urn or its name "
                     "in the active project).")
    insert_map = insert_map or {}
    df, problem = _resolve_datafile(design, insert_part)
    if not df:
        why = problem or ("it resolves as no lineage urn or Fusion URL, and the project's root "
                          "folder holds no file of that name.")
        return error(f"Could not find a configured part '{insert_part}': {why} It must be saved in "
                     "the SAME project as this assembly.")
    if not safe(lambda: df.isConfiguredDesign, False):
        return error(f"'{insert_part}' is not a configured design - use a normal insert for a "
                     "non-configured part. (Only configured parts get an insert column.)")
    part_rows = _part_config_rows(df)
    if not part_rows:
        return error(f"'{insert_part}' exposes no configuration rows.")

    # validate the map BEFORE inserting (fail clean, no half-built state)
    asm_names = set(_row_names(table))
    bad_asm = [a for a in insert_map if a not in asm_names]
    if bad_asm:
        return error(f"insert_map references assembly configurations that don't exist: "
                     f"{', '.join(bad_asm)}. Existing: {', '.join(str(n) for n in _row_names(table))}.")
    bad_part = [p for p in insert_map.values() if p not in part_rows]
    if bad_part:
        return error(f"insert_map references part configurations that don't exist: "
                     f"{', '.join(bad_part)}. The part '{insert_part}' has: "
                     f"{', '.join(part_rows.keys())}.")

    # choose which config to physically insert (default: the part's first row)
    init_name = (insert_config or "").strip() or next(iter(part_rows))
    if init_name not in part_rows:
        return error(f"insert_config '{init_name}' is not a configuration of '{insert_part}'. "
                     f"Available: {', '.join(part_rows.keys())}.")

    import adsk.core as _ac
    transform = _ac.Matrix3D.create()
    occ = design.rootComponent.occurrences.addFromConfiguration(part_rows[init_name], transform)  # MUTATION
    if not occ:
        return error(f"Inserting '{insert_part}' ({init_name}) returned no occurrence (same-project "
                     "requirement, or the part isn't accessible).")

    col = table.columns.addInsertColumn(occ)     # MUTATION
    if not col:
        return error("addInsertColumn returned null.")
    mapped = 0
    for acfg, pcfg in insert_map.items():
        cell = safe(lambda acfg=acfg: col.getCellByRowName(acfg))
        if cell is None:
            return error(f"No insert cell for assembly configuration '{acfg}'.")
        cell.row = part_rows[pcfg] # MUTATION (by-name part row - see appearance lesson)
        got_cfg = safe(lambda cell=cell: cell.row.name)
        if got_cfg is not None and got_cfg != safe(lambda pcfg=pcfg: part_rows[pcfg].name):
            return error(f"Insert cell '{acfg}' still selects part configuration '{got_cfg}' after "
                         "the set - the mapping did not take.")
        mapped += 1
    return ok({"inserted_part": insert_part, "inserted_config": init_name, "mapped": mapped,
               "occurrence": safe(lambda: occ.name),
               "note": "Configured part inserted and an insert column added: each listed assembly "
                       "configuration now selects the mapped part configuration (nested config). Switch "
                       "with design_configure(action='activate', name=...) + computeAll to see it follow. "
                       "This insert can run for minutes; the read that proves it landed without this "
                       "payload is design_get(include=['configurations']) showing the insert column."})


def _as_map(val, field):
    """(dict-or-None, error). A map input that arrived as JSON text is parsed; anything else
    non-dict is refused naming the field, never iterated as characters."""
    if val is None or isinstance(val, dict):
        return val, None
    if isinstance(val, str):
        if not val.strip():
            return None, None
        try:
            parsed = json.loads(val)
        except ValueError:
            return None, (f"'{field}' must be a JSON object ({{name: value}}), got unparseable "
                          f"text starting {val[:40]!r}.")
        if not isinstance(parsed, dict):
            return None, f"'{field}' must be a JSON object ({{name: value}}), got {type(parsed).__name__}."
        return parsed, None
    return None, f"'{field}' must be a JSON object ({{name: value}}), got {type(val).__name__}."


def handler(action: str = "", name: str = "", new_name: str = "", parameter: str = "",
            feature: str = "", body: str = "", values: dict = None, suppressed_in: list = None,
            hidden_in: list = None, appearances: dict = None, materials: dict = None,
            insert_part: str = "", insert_config: str = "", insert_map: dict = None) -> dict:
    """Build/extend a configured design - 'action' selects the verb, dispatched below. WRITES."""
    action = (action or "").strip()
    if action not in _ACTIONS:
        return error(f"Unknown action '{action}'. Use one of: {', '.join(_ACTIONS)}.")

    # Some MCP clients deliver an object-typed input as its JSON TEXT; iterating that string as a
    # map sprays per-character "unknown configuration" refusals, so parse-or-refuse each map input.
    values, verr = _as_map(values, "values")
    appearances, aerr = _as_map(appearances, "appearances")
    materials, merr = _as_map(materials, "materials")
    insert_map, ierr = _as_map(insert_map, "insert_map")
    for e in (verr, aerr, merr, ierr):
        if e:
            return error(e)

    design = _common.design()
    if not design:
        return error("No active design. Open or create a document first.")

    if action == "create":
        return _do_create(design)

    # all other actions need an existing configuration table
    table = _top_table(design)
    if not table:
        return error("The active design is not yet a configured design. Run action='create' first.")

    if action == "activate":
        return _do_activate(design, table, name)
    if action == "add_configuration":
        return _do_add_configuration(table, name)
    if action == "rename_configuration":
        return _do_rename_configuration(table, name, new_name)
    if action == "add_parameter":
        return _do_add_parameter(design, table, parameter, values)
    if action == "add_suppress":
        return _do_add_suppress(design, table, feature, suppressed_in)
    if action == "add_visibility":
        return _do_add_visibility(design, table, body, hidden_in)
    if action == "set_appearance":
        return _do_set_appearance(design, table, body, appearances)
    if action == "add_material":
        return _do_add_material(design, table, body, materials)
    if action == "add_insert":
        return _do_add_insert(design, table, insert_part, insert_config, insert_map)
    return error(f"Unhandled action '{action}'.")   # unreachable (guarded above)


TOOL_DESCRIPTION = (
    "Build or switch a Configured Design; 'action' picks the verb. Read the table back with "
    "design_get(include=['configurations'])."
)

tool = (
    Tool.create_simple(name="design_configure", description=TOOL_DESCRIPTION)
    .add_input_property("action", {"type": "string", "enum": list(_ACTIONS)})
    .add_input_property("name", {"type": "string",
            "description": "The configuration (rename_configuration: the existing one)."})
    .add_input_property("new_name", {"type": "string"})
    .add_input_property("parameter", {"type": "string"})
    .add_input_property("feature", {"type": "string"})
    .add_input_property(*_BODY.as_property())
    .add_input_property("values", {"type": "object", "description": "{config: expression} (add_parameter)."})
    .add_input_property("suppressed_in", {"type": "array", "items": {"type": "string"},
            "description": "Which configurations (add_suppress)."})
    .add_input_property("hidden_in", {"type": "array", "items": {"type": "string"},
            "description": "Which configurations (add_visibility)."})
    .add_input_property("appearances", {"type": "object",
            "description": "{config: appearance} (set_appearance)."})
    .add_input_property("materials", {"type": "object",
            "description": "{config: material} (add_material)."})
    .add_input_property("insert_part", {"type": "string",
            "description": "Lineage urn or project name (add_insert)."})
    .add_input_property("insert_config", {"type": "string",
            "description": "add_insert; default the part's first."})
    .add_input_property("insert_map", {"type": "object",
            "description": "{assembly_config: part_config} (add_insert)."})
    .strict_schema()
)
# enforce_timeout=False: add_insert's addFromConfiguration is a blocking, uninterruptible
# main-thread call that COMMITS - one ran past the server's cap with the occurrence, the column and
# both cell mappings landed. The flag covers all ten actions, so each one's loops are bounded.
item = Item.create_tool_item(
    tool=tool, write="write", handler=handler, run_on_main_thread=True,
    enforce_timeout=False,
    verification=Verification(
        kind="inline", rung="value",
        evidence_test="tests/unit/test_design_configure.py::TestAddParameterRefusals"
                      "::test_an_expression_that_does_not_take_is_an_error"))


def register_tool():
    register(item)
