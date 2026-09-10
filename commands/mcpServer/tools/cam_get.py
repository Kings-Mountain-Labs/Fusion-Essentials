# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""The rich CAM read (CLAUDE.md "Reads are RICH"): the active document's CAM state, zoomed via
include=[...]. The handler is a thin router over _slice_*() helpers, one per slice. Operation
validity is only trustworthy once the Manufacture workspace has been entered."""

import json

import adsk.core
import adsk.cam

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item
from ..mcp_primitives.registry import register
from ._common import (CM_TO_UNIT, iter_collection, measured, named_with_remainder, ok, error,
                      read_flag, safe, terse)
from ._cam_common import (STRATEGY_PAIR_NOTE, choice_expressions, get_cam, find_setup,
                          resolve_cam_node, resolve_operation, strategy_pair)
from ._cam_presets import _preset_names, _presets_named
from ..guidance.loader import STRATEGY_RECIPE_ID
from . import _inputs

app = adsk.core.Application.get()

_SLICES = ("operations", "strategies", "parameters", "tool", "references", "nc_programs", "time",
           "tools", "library", "library_types", "machine", "machines", "templates", "inspection")

# The orientation slice's own names in include=. Any deep include omits that slice unless one of
# these rides beside it, so a deep read carries what was asked for and not the default again.
_DEFAULT_NAMES = ("default", "setups")

# The quiet answers _common.terse drops from an operation row: a healthy op collapses to
# {name, tool, strategy, state}, and any flag that is not its default here survives and pops.
_OP_NOISE = {"is_generating": False, "is_suppressed": False, "is_optional": False,
             "has_warning": False, "has_error": False, "is_out_of_date": False,
             "has_toolpath": True, "toolpath_valid": True,
             "preset": None, "spindle_over_machine_max": False}


# ── slice helpers - each calls a read core in _cam_read, then shapes and bounds its payload ───────

def _unwrap(result):
    """(payload, None) on ok; (None, error_result) on error (so a slice's own guard can surface)."""
    if result.get("isError"):
        return None, result
    try:
        return json.loads(result["content"][0]["text"]), None
    except Exception:
        return None, result


def _slice_setups(cam, setup):
    """The setups orientation default: machine + model/fixture/stock + per-setup operation_count (the
    REAL total, incl. ops nested in folders) + folder_count (the depth breadcrumb)."""
    from . import _cam_read as _cr
    return _unwrap(_cr.get_cam_setups_handler())


def _dedupe_orientation(out, inc):
    """Content-aware de-dup for a call that asked for BOTH ('default' beside a deep slice): a fact the
    included slice RESTATES at finer grain is dropped from the orientation copy. When 'operations' is
    included, each op carries its own invalidation_reasons, so the SETUP-level rollup is the duplicate."""
    if "operations" in inc:
        for s in out.get("setups", []):
            s.pop("invalidation_reasons", None)


def _cam_pointers(setups):
    """The tool that resolves each actionable state the orientation slice found - present-only, so a
    pointer appears when that state is actually there."""
    ptrs = {}
    stale = 0
    machine_stale = False
    for s in setups or []:
        st = s.get("op_states") or {}
        stale += (st.get("out_of_date", 0) or 0) + (st.get("no_toolpath", 0) or 0)
        if s.get("machine_out_of_date"):
            machine_stale = True
    if stale:
        ptrs["toolpaths"] = (f"cam_generate to regenerate the {stale} out-of-date / ungenerated "
                             "operation(s); cam_get(include=['operations']) for the per-op detail.")
    if machine_stale:
        ptrs["machine"] = "cam_edit_setup to refresh the out-of-date machine definition."
    return ptrs


_OPERATIONS_CAP = 250   # a large CAM doc can hold hundreds of ops; cap the per-turn dump + flag it.


def _slice_operations(cam, setup):
    """Per-operation state grouped by setup (+ tools_used rollup), with healthy-row noise dropped (a
    normal op is {name,tool,strategy,state}; a suppressed/errored op keeps its flags and stands out).
    Bounded: across all setups the operation rows are capped (the counts in the default setups slice
    are unbounded, so the agent always sees the true total; 'setup' scopes to one setup)."""
    from . import _cam_read as _cr
    payload, err = _unwrap(_cr.get_cam_operations_handler(setup=setup))
    if payload:
        emitted = 0
        for su in payload.get("setups", []):
            rows = []
            for op in su.get("operations", []):
                if emitted >= _OPERATIONS_CAP:
                    payload["truncated"] = True
                    break
                rows.append(terse(op, _OP_NOISE))
                emitted += 1
            su["operations"] = rows
        if payload.get("truncated"):
            payload["note"] = (f"Operation rows capped at {_OPERATIONS_CAP}. Pass 'setup' to scope to "
                               "one setup, or add 'default' to include for the per-setup "
                               "operation_count. " + (payload.get("note") or ""))
    return payload, err


_STRATEGY_CAP = 250     # a job holds many setups and each offers a full strategy vocabulary.

# A strategy row's classification flags collapse at their MEASURED usual value, so a row shows what
# the strategy IS: false for most, true for is_suppressible (all 54 strategies of a milling setup
# read it true). 'allowed' is never in here, and a flag that did not read is None, so it survives.
_STRATEGY_NOISE = {"is_2d": False, "is_3d": False, "is_drilling": False, "is_milling": False,
                   "is_rotary": False, "is_turning": False, "is_finishing": False,
                   "is_additive": False, "is_cutting": False, "is_support": False,
                   "is_suppressible": True}

_STRATEGY_NOTE = (
    "'allowed' is isGenerationAllowed; cam_create_operation REFUSES a false one. null means it "
    "did not read, and no refusal follows. A classification flag at its usual value is dropped "
    "(false; is_suppressible true). "
    f"Which fits: sys_get_guidance(recipe='{STRATEGY_RECIPE_ID}').")

# The empty 'strategies' list of a setup that never reported one reads exactly like a setup that
# offers nothing, so the note says which whenever such a row is in the payload.
_UNREADABLE_SETUPS = " strategies_read false: that list did not read."


def _slice_strategies(cam, setup):
    """Each setup's compatible strategy vocabulary: name, title, the isGenerationAllowed flag
    cam_create_operation pre-flights against, and the classification flags; 'setup' scopes it."""
    from . import cam_create_operation
    payload, err = _unwrap(cam_create_operation.read_strategies(setup))
    if payload:
        emitted = 0
        for su in payload.get("setups", []):
            rows = []
            for row in su.get("strategies", []):
                if emitted >= _STRATEGY_CAP:
                    payload["truncated"] = True
                    break
                rows.append(terse(row, _STRATEGY_NOISE))
                emitted += 1
            su["strategies"] = rows
        payload["note"] = _STRATEGY_NOTE
        if any(su.get("strategies_read") is False for su in payload.get("setups", [])):
            payload["note"] += _UNREADABLE_SETUPS
        if payload.get("truncated"):
            payload["note"] = (f"Capped at {_STRATEGY_CAP} rows; 'setup' scopes it, "
                               "strategy_count is the true total. " + payload["note"])
    return payload, err


# The walk tests each of a setup's model/fixture/stock entries with Occurrence.isReferencedComponent
# and does not descend, so the census covers the SELECTED entries alone. The number is stated every
# time: a 0 that does not say what it counted reads as a verdict on the document.
_REFERENCE_CENSUS = (
    "Counted {found} referenced component(s) among the model/fixture/stock entries the {setups} "
    "setup(s) in scope SELECT DIRECTLY - the top-level setups[] slice names those entries in "
    "selected_models / fixtures / stock_solids - and only an entry that is ITSELF a referenced "
    "component counts. A reference nested INSIDE a selected entry is not examined, so 0 here is not "
    "'this document has no external references'. doc_get(include=['xref_tree']) walks every depth.")


def _slice_references(cam, setup):
    """Each setup's external X-ref models/fixtures/stock -> source document, plus the census sentence
    saying which entries that count covers (see _REFERENCE_CENSUS)."""
    from . import _cam_read as _cr
    payload, err = _unwrap(_cr.get_setup_references_handler(setup=setup))
    if payload:
        rows = payload.get("setups") or []
        payload["counted"] = ("the model/fixture/stock entries each setup selects directly, and of "
                              "those only the ones that are themselves referenced components")
        note = _REFERENCE_CENSUS.format(
            found=sum(r.get("reference_count") or 0 for r in rows), setups=len(rows))
        if any(r.get("references_truncated") for r in rows):
            note += (" references_truncated is set on at least one setup, so even that selected-entry "
                     "census is incomplete - a selection list would not read, or its cap was hit.")
        payload["note"] = note
    return payload, err


def _slice_nc_programs(cam):
    """The NC/post programs - SUMMARY only (name, machine, post, op count + post_parameter_count). The
    full post_parameters are the post's static schema (often 60+ rows, identical across programs), a
    deeper level not dumped here - point at it rather than flooding (CLAUDE.md 'point, don't inline')."""
    from . import _cam_read as _cr
    payload, err = _unwrap(_cr.get_nc_programs_handler())
    if payload:
        for p in payload.get("nc_programs", []):
            params = p.pop("post_parameters", None)
            if params is not None:
                p["post_parameter_count"] = len(params)
    return payload, err


def _slice_time(cam, setup, units):
    """Machining cycle-time estimate (per setup + per operation), suppressed ops excluded."""
    from . import _cam_read as _cr
    return _unwrap(_cr.get_machining_time_handler(setup=setup, units=units))


def _slice_machine(cam, setup, units):
    """The machine's own LIMITS per setup: spindle speed range + per-axis travels, off the machine's
    kinematics. Distinct from 'machines' (the catalog of machines you can assign)."""
    from . import _cam_read as _cr
    return _unwrap(_cr.get_machine_limits_handler(setup=setup, units=units))


def _slice_tools(cam):
    """The distinct cutting tools used across operations (the tool sheet)."""
    from . import _cam_read as _cr
    return _unwrap(_cr.get_tool_list_handler())


def _slice_library(cam, scope, library, tool_type):
    """A tool LIBRARY's catalog (the tools you can ADD), by scope: document/local/cloud/hub/fusion.
    A shared scope with no 'library' lists the libraries there. Distinct from 'tools' (what ops USE);
    the write actions stay on cam_edit_tools, which refuses every write at the fusion scope."""
    from . import cam_edit_tools
    return _unwrap(cam_edit_tools.read_library(scope or "document", library, tool_type))


def _slice_library_types(cam):
    """The tool-type vocabulary (the geometry families a sample can be cloned from) that
    cam_edit_tools resolves add_tools[].from_type against. Distinct from 'library' (a catalog's
    tools); the add/remove/edit writes stay on cam_edit_tools."""
    from . import cam_edit_tools
    return _unwrap(cam_edit_tools._do_list_types())


def _slice_machines(cam, vendor, machine_type):
    """The machine catalog cam_edit_setup's 'machine' input can resolve from (Local + Fusion360
    locations), each row name/vendor/model/location/kind/simulation_ready. 'vendor' and
    'machine_type' (milling/turning/cutting/additive) filter."""
    from . import cam_edit_setup
    return _unwrap(cam_edit_setup.read_machines(vendor or "", machine_type or ""))


def _slice_inspection(cam, measure, max_results, units):
    """The recorded surface-inspection (probing) results: a per-measure state rollup + its worst
    out-of-tolerance point by default; 'measure'=<index> (or '<index>/<path>') drills that scope's
    out-of-tolerance points, capped by 'max_results'."""
    from . import _cam_read as _cr
    return _unwrap(_cr.get_inspection_results_handler(
        measure=measure, max_results=max_results, units=units))


def _slice_templates(cam, template_location, template_url, template_depth):
    """The CAM toolpath TEMPLATE library tree (folders + templates by URL) for a location
    (cloud/local/fusion/...) or a specific folder 'template_url'. Apply/save stay on cam_apply_template
    / cam_save_template."""
    from . import _cam_templates
    return _unwrap(_cam_templates.list_cam_templates_handler(
        location=template_location or "cloud", url=template_url, max_depth=template_depth or 4))


# ── ONE operation's detail: its parameters / tool / a tool preset (the deepest level) ──────────────
# An operation holds 400+ CAMParameters, so only the visible+enabled ones are kept, grouped into the
# panel's own sections by the `group_*` marker params in declaration order - there is no group API.

def _op_miss_error(operation, names, refusal):
    """The refusal for an unscoped operation resolve that came back empty: duplicates a SETUP can
    separate are offered as setup= values, and everything else returns `refusal`, the shared
    resolver's own text."""
    want = (operation or "").strip().lower()
    paths = [n for n in names if n and n.split(" / ")[-1].strip().lower() == want]
    if paths:
        # The setup is the FIRST segment of each breadcrumb. A path with no separator is a bare
        # name, and a setup holding SEVERAL duplicates refuses again when scoped to, so only a
        # setup appearing EXACTLY ONCE is offered.
        heads = [p.split(" / ")[0].strip() for p in paths if " / " in p]
        scopes = [s for s in dict.fromkeys(heads) if s and heads.count(s) == 1]
        if scopes:
            return error(f"'{operation}' is ambiguous - {len(paths)} operations share that name: "
                         f"{named_with_remainder(paths)}. Retry with the setup that holds the one "
                         "you mean: " + ", ".join(f"setup='{s}'" for s in scopes) + ".")
    return error(refusal)


def _resolve_op_in_scope(cam, operation, setup):
    """(operation, error_result_or_None) - the ONE operation resolve behind the deep per-operation
    slices. 'setup' SCOPES the resolve to that setup's subtree, since an operation name is unique
    only within a setup; unscoped, the same resolver runs over the whole tree."""
    want_setup = (setup or "").strip()
    if not want_setup:
        node, rerr, names = resolve_operation(cam, operation)
        if node is not None:
            return node.obj, None
        return None, _op_miss_error(operation, names, rerr)
    s, _names, serr = find_setup(cam, want_setup)
    if not s:
        return None, error(serr)
    node, rerr = resolve_cam_node(cam, operation, kinds=("operation",), setup=s,
                                  label=f"operation in setup '{want_setup}'")
    if rerr:
        return None, error(rerr)
    return node.obj, None


_EDITABLE_NOTE = (
    "Only a row carrying editable false refuses a write - cam_edit_operation and cam_edit_setup "
    "reject one by name before applying anything; a row with no editable key read isEditable True, "
    "and null means the flag did not read.")

# Said only where a row actually carries the key: a parameters read with no choice row in it would
# otherwise advertise a key nothing in the payload has.
_CHOICES_NOTE = (
    " A row's 'choices' are the values that parameter's own getChoices() answers - the only "
    "expressions it takes; pass one of them verbatim.")


# Said only where rows were dropped; parameter_count is the LISTED rows. MEASURED on a contour2d:
# of 384 hidden, 205 read isVisible true with isEnabled FALSE and 179 read isVisible false. A gated
# row flips to enabled+editable when its switch lands, and the listing grows by it.
_HIDDEN_NOTE = (
    " {n} more parameter(s) did not read visible+enabled and are NOT listed (hidden_count). A row "
    "behind a switch reads isEnabled FALSE until that switch is on, and cam_edit_operation writes "
    "such a row after the switch in the SAME call - its refusal names the pair. Others read "
    "isVisible false, licence and mode flags among them. parameter_count counts the LISTED rows.")

# The same disclosure for a SETUP. MEASURED on a fresh milling setup: 304 parameters, 42 listed,
# 262 hidden - of which the 12 computed extents stock_extents publishes are a small part.
_SETUP_HIDDEN_NOTE = (
    " {n} more parameter(s) did not read visible+enabled and are NOT listed (hidden_count); the "
    "computed extents among them are what stock_extents publishes. cam_edit_setup REFUSES a row "
    "reading editable false outright, so a hidden row here is NOT reachable the way "
    "cam_edit_operation reaches one behind its switch. parameter_count counts the LISTED rows.")


def _any_choices(groups) -> bool:
    """Whether any grouped row published a 'choices' set - what gates the note above."""
    return any("choices" in row for rows in groups.values() for row in rows)


def _grouped_visible_params(param_coll):
    """({section_title: [{name, title, expression}]} for the VISIBLE + ENABLED parameters, grouped
    by the `group_*` sentinels, plus 'editable' on a row whose isEditable did not read True; how
    many rows that filter DROPPED)."""
    groups = {}
    hidden = 0
    current = "General"
    for p in iter_collection(param_coll):
        if not (safe(lambda p=p: p.isVisible, False) and safe(lambda p=p: p.isEnabled, False)):
            hidden += 1
            continue
        nm = safe(lambda p=p: p.name) or ""
        title = safe(lambda p=p: p.title) or nm
        # a group sentinel ('group_feedspeed', 'stockDefinition', 'useShaftAndHolder', ...) opens a
        # section and is NOT itself a value row.
        if nm.startswith("group_") or (nm[:1].islower() and safe(lambda p=p: p.value, None) is True
                                       and not safe(lambda p=p: p.expression, "").strip("truefalse ")):
            current = title
            groups.setdefault(current, [])
            continue
        row = {"name": nm, "title": title, "expression": safe(lambda p=p: p.expression)}
        # Quiet default: the key rides only when the flag is NOT True - false (the write is refused)
        # or null (unread). read_flag, so an unreadable flag never publishes as a confident false.
        editable = read_flag(lambda p=p: p.isEditable)
        if editable is not True:
            row["editable"] = editable
        # A CHOICE row carries the set it accepts; every other parameter carries none, and the key
        # is absent there rather than an empty list.
        choices = choice_expressions(p)
        if choices:
            row["choices"] = choices
        groups.setdefault(current, []).append(row)
    # drop empty sections (a sentinel with no following values)
    return {g: rows for g, rows in groups.items() if rows}, hidden


# The setup's stock/model extents: COMPUTED parameters reading isVisible False, so the grouping
# above drops them. A CAMParameter carries the same length twice in DIFFERENT units - .value.value
# in internal CM, .expression as authored text that may not even be a number.
_STOCK_EXTENTS = ("stockXLow", "stockXHigh", "stockYLow", "stockYHigh", "stockZLow", "stockZHigh",
                  "surfaceXLow", "surfaceXHigh", "surfaceYLow", "surfaceYHigh",
                  "surfaceZLow", "surfaceZHigh")

_SETUP_PARAM_NOTE = (
    "The setup's own parameters, filtered to the visible+enabled rows and grouped like the Fusion "
    "panel (job_stockMode is the stock mode). 'stock_extents' adds the computed low/high extents "
    "the rows refer to. Each extent carries the same length twice in DIFFERENT units: 'value' is "
    "scaled into 'units', 'expression' is the authored text in the document's display unit. "
    "Subtract within ONE of them.")


def _stock_extents(param_coll, factor, unit) -> dict:
    """{units, <name>: {value, expression}} for the computed stock/model extents that ARE present. A
    name the setup does not carry is simply absent - nothing is reported for a parameter that did
    not read. 'value' is scaled out of Fusion's internal cm; 'expression' is left as authored."""
    out = {}
    for name in _STOCK_EXTENTS:
        p = safe(lambda name=name: param_coll.itemByName(name))
        if p is None:
            continue
        row = {"value": measured(lambda p=p: p.value.value, factor),
               "expression": safe(lambda p=p: p.expression)}
        if row["value"] is not None or row["expression"]:
            out[name] = row
    if out:
        out["units"] = unit          # names the unit 'value' is in; 'expression' is not in it
    return out


def _slice_setup_parameters(cam, setup, units):
    """ONE SETUP's own parameters (the read-back side of cam_edit_setup's writes): the visible rows
    grouped by section, plus the computed stock extents."""
    factor = CM_TO_UNIT.get((units or "mm").strip().lower())
    if factor is None:
        return None, error(f"Unknown units '{units}'. Valid: mm, cm, in.")
    s, _names, err = find_setup(cam, setup)
    if not s:
        return None, error(err)
    params = safe(lambda: s.parameters)
    if params is None:
        return None, error(f"Setup '{setup}' exposes no readable parameters - nothing about its "
                           "stock or job settings can be read.")
    groups, hidden = _grouped_visible_params(params)
    out = {"setup": safe(lambda: s.name), "sections": groups,
           "parameter_count": sum(len(v) for v in groups.values()),
           "note": _SETUP_PARAM_NOTE + " " + _EDITABLE_NOTE
                   + (_CHOICES_NOTE if _any_choices(groups) else "")
                   + (_SETUP_HIDDEN_NOTE.format(n=hidden) if hidden else "")}
    if hidden:
        out["hidden_count"] = hidden   # absent = every parameter the setup carries is listed
    extents = _stock_extents(params, factor, (units or "mm").strip().lower())
    if extents:
        out["stock_extents"] = extents
    return out, None


def _slice_parameters(cam, operation, setup, units="mm"):
    """ONE operation's machining parameters, or ONE setup's own (pass 'setup' with no 'operation'),
    visible-only and grouped by section. With BOTH named, 'setup' scopes which operation of that
    name is read."""
    if not (operation or "").strip():
        if (setup or "").strip():
            return _slice_setup_parameters(cam, setup, units)
        return None, error("include=['parameters'] needs 'operation' - the operation whose settings to "
                           "read (scope first with cam_get(setup=..., include=['operations'])); or "
                           "'setup' alone for that SETUP's own parameters (stock mode + extents).")
    op, oerr = _resolve_op_in_scope(cam, operation, setup)
    if oerr:
        return None, oerr
    groups, hidden = _grouped_visible_params(safe(lambda: op.parameters))
    out = {"operation": safe(lambda: op.name),
           "sections": groups,
           "parameter_count": sum(len(v) for v in groups.values()),
           "note": _EDITABLE_NOTE + (_CHOICES_NOTE if _any_choices(groups) else "")
                   + (_HIDDEN_NOTE.format(n=hidden) if hidden else "")
                   + " " + STRATEGY_PAIR_NOTE}
    if hidden:
        out["hidden_count"] = hidden   # absent = every parameter the operation carries is listed
    out.update(strategy_pair(op))
    return out, None


_TOOL_COPY_NOTE = (
    "'dimensions' are THIS operation's own copy of the tool - a document-tool edit does not reach "
    "an operation created before it.")


_SHARED_PRESET_NAME_NOTE = (
    "'{name}' names {n} presets on this tool, so every match is returned under "
    "presets_sharing_name keyed by its index on the tool - the name alone does not pick one. Omit "
    "'preset' for the preset_names / preset_count / active_preset summary.")


def _preset_expressions(preset):
    """{parameter name: expression} for ONE tool preset - its feeds/speeds recipe as authored."""
    return {safe(lambda p=p: p.name): safe(lambda p=p: p.expression)
            for p in iter_collection(safe(lambda: preset.parameters))}


def _slice_tool(cam, operation, preset, setup="", units="mm"):
    """ONE operation's tool: spec + cutting geometry + its preset NAMES (a tool can hold 20+);
    'preset' drills one preset's expressions (the feeds/speeds recipe). Requires 'operation'; 'setup'
    scopes which operation of that name is read, through the parameters slice's resolve."""
    if not (operation or "").strip():
        return None, error("include=['tool'] needs 'operation' - the operation whose tool to read.")
    unit = (units or "mm").strip().lower()
    factor = CM_TO_UNIT.get(unit)
    if factor is None:
        return None, error(f"Unknown units '{units}'. Valid: mm, cm, in.")
    op, oerr = _resolve_op_in_scope(cam, operation, setup)
    if oerr:
        return None, oerr
    t = safe(lambda: op.tool)
    if not t:
        return {"operation": safe(lambda: op.name), "tool": None}, None
    # The preset index and the by-name lookup are _cam_presets' own, the resolver the WRITE path
    # runs: an exact case-insensitive match, so the spelling cam_edit_tools accepted reads back here.
    presets = safe(lambda: t.presets)
    pnames = _preset_names(presets) if presets is not None else []
    from . import _cam_common as _cc
    # WHICH preset this operation runs: two operations can share one tool and run different presets,
    # so the tool description alone cannot say what feeds an op cuts at.
    active = safe(lambda: op.toolPreset)
    out = {"operation": safe(lambda: op.name),
           "tool": safe(lambda: t.description),
           "holder": _cc.tool_holder(t),          # assigned holder identity (None if the tool has none)
           "dimensions": _cc.tool_dimensions(t, factor, unit),
           "active_preset": ({"name": safe(lambda: active.name), "id": safe(lambda: active.id)}
                             if active is not None else None),
           "preset_names": pnames, "preset_count": len(pnames),
           "note": _TOOL_COPY_NOTE}
    want_preset = (preset or "").strip()
    if want_preset:
        matches = _presets_named(presets, want_preset) if presets is not None else []
        if not matches:
            # pnames holds a SLOT per preset (null where the name did not read), so an empty
            # tool and a tool whose names are unreadable are different answers.
            avail = [n for n in pnames if n]
            if not pnames:
                return None, error(f"This tool carries no presets at all, so '{want_preset}' names "
                                   "none. cam_edit_tools(action='add_preset') authors one on a "
                                   "library tool.")
            if not avail:
                return None, error(f"This tool carries {len(pnames)} preset(s) but none of their "
                                   f"names read, so '{want_preset}' cannot be matched against them.")
            return None, error(f"No preset named '{want_preset}' on this tool. Presets on this "
                               f"tool: {named_with_remainder(avail)}.")
        # the preset's OWN name, never the spelling asked for - the match is case-insensitive
        if len(matches) == 1:
            chosen = matches[0][1]
            out["preset"] = {"name": safe(lambda: chosen.name),
                             "expressions": _preset_expressions(chosen)}
        else:
            out["presets_sharing_name"] = [
                {"index": i, "name": safe(lambda p=p: p.name), "expressions": _preset_expressions(p)}
                for i, p in matches]
            out["note"] += " " + _SHARED_PRESET_NAME_NOTE.format(name=want_preset, n=len(matches))
    return out, None


# ── the router ─────────────────────────────────────────────────────────────────────────────────────

_VALIDITY_NOTE = ("Readable from any workspace; operation validity is trustworthy only in the "
                  "Manufacture workspace.")


def handler(include=None, setup: str = "", operation: str = "", preset: str = "",
            scope: str = "", library: str = "", tool_type: str = "", vendor: str = "",
            machine_type: str = "",
            template_location: str = "", template_url: str = "", template_depth: int = 0,
            measure: str = "", max_results: int = 0, units: str = "mm") -> dict:
    """See TOOL_DESCRIPTION."""
    cam, cerr = get_cam()
    if not cam:
        return error(cerr)

    inc = _normalize_include(include)
    bad = [s for s in inc if s not in _SLICES and s not in _DEFAULT_NAMES]
    if bad:
        return error(f"Unknown include {bad}. Valid: {', '.join(_SLICES + _DEFAULT_NAMES)}.")

    deep = [s for s in inc if s in _SLICES]
    want_default = not deep or any(s in _DEFAULT_NAMES for s in inc)
    out = {}
    if want_default:
        out, serr = _slice_setups(cam, setup)
        if serr:
            return serr

    if "operations" in inc:
        out["operations"], e = _slice_operations(cam, setup)
        if e:
            return e
    if "strategies" in inc:                     # the createable strategy vocabulary + entitlement
        out["strategies"], e = _slice_strategies(cam, setup)
        if e:
            return e
    if "parameters" in inc:                     # deep: ONE operation's (or setup's) settings, grouped
        out["parameters"], e = _slice_parameters(cam, operation, setup, units)
        if e:
            return e
    if "tool" in inc:                           # deep: ONE operation's tool + presets (preset= drills)
        out["tool"], e = _slice_tool(cam, operation, preset, setup, units)
        if e:
            return e
    if "references" in inc:
        out["references"], e = _slice_references(cam, setup)
        if e:
            return e
    if "nc_programs" in inc:
        out["nc_programs"], e = _slice_nc_programs(cam)
        if e:
            return e
    if "time" in inc:
        out["time"], e = _slice_time(cam, setup, units)
        if e:
            return e
    if "machine" in inc:                        # the assigned machine's spindle/axis limits
        out["machine"], e = _slice_machine(cam, setup, units)
        if e:
            return e
    if "tools" in inc:
        out["tools"], e = _slice_tools(cam)
        if e:
            return e
    if "library" in inc:                        # the tool-library catalog (tools you can ADD)
        out["library"], e = _slice_library(cam, scope, library, tool_type)
        if e:
            return e
    if "library_types" in inc:                  # the from_type vocabulary add_tools clones from
        out["library_types"], e = _slice_library_types(cam)
        if e:
            return e
    if "machines" in inc:                       # the machine catalog (names cam_edit_setup accepts)
        out["machines"], e = _slice_machines(cam, vendor, machine_type)
        if e:
            return e
    if "templates" in inc:                      # the CAM toolpath template library tree
        out["templates"], e = _slice_templates(cam, template_location, template_url, template_depth)
        if e:
            return e
    if "inspection" in inc:                     # recorded probing results ('measure' drills one)
        out["inspection"], e = _slice_inspection(cam, measure, max_results, units)
        if e:
            return e

    _dedupe_orientation(out, inc)

    # name the tool that resolves each present, actionable state (stale toolpaths -> cam_generate, etc.)
    # so an agent reading the orientation knows the next action, not just that something is out of date.
    ptrs = _cam_pointers(out.get("setups"))
    if ptrs:
        out["pointers"] = ptrs

    remaining = [s for s in _SLICES if s not in inc]
    lines = []
    if want_default and remaining:
        lines.append("Setups orientation slice. Pull deeper with include=" + str(remaining) +
                     ". Scope then deepen: include=['operations'] ('setup' filters) -> "
                     "include=['parameters'] or ['tool'] with 'operation'=<name> for one op's "
                     "settings/tool -> 'preset'=<name> for a preset's feeds/speeds.")
    if want_default or "operations" in inc:
        lines.append(_VALIDITY_NOTE)
    if lines:
        # The setups slice's own note describes a state IN this payload, so it is kept ahead of the
        # router's pointers rather than replaced by them.
        out["note"] = " ".join(([out["note"]] if out.get("note") else []) + lines)
    return ok(out)


def _normalize_include(include):
    if include in (None, "", []):
        return []
    if isinstance(include, str):
        return [s.strip().lower() for s in include.split(",") if s.strip()]
    return [str(s).strip().lower() for s in include]


TOOL_DESCRIPTION = (
    "Read the active document's CAM (Manufacture) state: one row per setup by default, deeper "
    "slices via 'include'. Edits go through cam_edit_setup / cam_edit_operation / cam_edit_tools."
)

tool = (
    Tool.create_simple(name="cam_get", description=TOOL_DESCRIPTION)
    .add_input_property("include", {"type": "array",
            "items": {"type": "string", "enum": list(_SLICES + _DEFAULT_NAMES)},
            "description": "'default'/'setups' keeps the setups slice beside them."})
    .add_input_property("setup", {"type": "string",
            "description": "Scopes the operations/strategies/references/time/machine slices."})
    .add_input_property("operation", {"type": "string"})
    .add_input_property("preset", {"type": "string",
            "description": "With include=['tool']: this preset's feeds/speeds."})
    .add_input_property("scope", {"type": "string",
            "enum": ["document", "local", "cloud", "hub", "fusion"],
            "description": "'library': which location. Default document."})
    .add_input_property("library", {"type": "string",
            "description": "'library': a name/url; omit to list the libraries there."})
    .add_input_property("tool_type", {"type": "string"})
    .add_input_property("vendor", {"type": "string"})
    .add_input_property("machine_type", {"type": "string",
            "enum": ["milling", "turning", "cutting", "additive"]})
    .add_input_property("template_location", {"type": "string",
            "description": "Default cloud."})
    .add_input_property("template_url", {"type": "string"})
    .add_input_property("template_depth", {"type": "integer",
            "description": "Default 4."})
    .add_input_property("measure", {"type": "string",
            "description": "One measure by INDEX ('0'), or one of its paths ('0/1')."})
    .add_input_property("max_results", {"type": "integer",
            "description": "Default 50, max 200."})
    .add_input_property(*_inputs.UNITS.as_property())
    .strict_schema()
)
item = Item.create_tool_item(tool=tool, write="read", handler=handler, run_on_main_thread=True)


def register_tool():
    register(item)
