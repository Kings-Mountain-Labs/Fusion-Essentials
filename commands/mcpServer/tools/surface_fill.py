# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""MCP building block: seal the enclosed volume(s) bounded by a set of surface/solid bodies into a
solid - Fusion's Boundary Fill. WRITES. BoundaryFillFeatures.createInput opens a partial-compute
transaction that must be committed via add() or aborted via BoundaryFillFeatureInput.cancel() -
never let an exception (or a refusal) leak it open.
"""

import adsk.core
import adsk.fusion

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import error, ok, safe, target_component
from . import _common
from . import _assert
from . import _geom
from . import _inputs
from . import _outputs

# What this tool RETURNS (declared once; drives the PRODUCES: prose + the assert-present contract test).
RETURNS = [
    _outputs.ReturnsName("feature", of="feature", consumers=["design_delete_feature"]),
]

# kind="brep" accepts a SOLID or an OPEN SURFACE body and REDIRECTS a mesh - boundaryFillFeatures
# takes BRep bodies, so a mesh is refused before the transaction opens rather than inside it.
_TOOLS = _inputs.BodyRefList("tools", kind="brep", required=True)
_OPERATION = _inputs.boolean_op(options=("new", "join", "cut", "intersect"), default="new")
_UNITS = _inputs.UnitField()

_SPEC = [_TOOLS, _OPERATION, _UNITS]

# How many cells the refusal lists before it truncates (the listing is the agent's whole basis for
# picking, but a tool set can in principle divide space into many cells).
_LISTING_CAP = 20

# What each operation DID to the model, for the result note - so a cut/intersect never reports the
# same "sealed into a body" sentence a new-body fill does.
_OP_EFFECT = {
    "new": "sealed into a new body",
    "join": "sealed and joined into the target body",
    "cut": "cut away from the target body",
    "intersect": "intersected with the target body",
}

# How far the produced volume may sit from the volume the input's cells predicted before it is
# called out. Relative, because the two are the same quantity measured at different moments.
_VOLUME_TOLERANCE = 0.01

# Sentinel for "this read FAILED", distinct from a read that legitimately answered None.
_UNREADABLE = object()


def _abort(fill_input) -> str:
    """Cancel this tool's open BoundaryFillFeatureInput transaction - the shared abort, named for it."""
    return _common.cancel_input(fill_input, "boundary-fill")


def _solid_snapshot(design):
    """Every SOLID body in the WHOLE design - the pre-image the volume read-back compares against.
    Design-wide, since a join/cut can move material in a body another component holds and
    feature.bodies reports only the feature's own-component result body."""
    out = []
    for comp in _common.all_components(design):
        for b in _common.iter_collection(safe(lambda c=comp: c.bRepBodies)):
            if safe(lambda b=b: b.isSolid):
                out.append(b)
    return out


def _cell_volumes(fill_input, factor):
    """(cells collection, [volume-or-None per cell]) - each cell's transient cellBody volume, scaled
    by `factor` (cm3 -> the caller's units cubed). A None collection means bRepCells itself could
    not be read, which is NOT finding zero cells; an unreadable volume is None, never a zero."""
    cells = safe(lambda: fill_input.bRepCells)
    if cells is None:
        return None, []
    total = int(safe(lambda: cells.count, 0) or 0)
    # A cell's INDEX is its address (_cell_listing publishes '[0] 12.5 mm3, ...' and 'keep' takes
    # those indices back), so this stays a positional walk: iter_collection drops an unreadable
    # cell, which would slide every later volume onto the wrong index.
    return cells, [_common.measured(lambda i=i: cells.item(i).cellBody.volume, factor)
                   for i in range(total)]


def _cell_listing(volumes, units_key) -> str:
    """'[0] 12.5 mm3, [1] 3.2 mm3, ...' - the per-cell disclosure a caller picks indices from."""
    shown = volumes[:_LISTING_CAP]
    parts = [(f"[{i}] {v} {units_key}3" if v is not None else f"[{i}] volume unreadable")
             for i, v in enumerate(shown)]
    if len(volumes) > len(shown):
        parts.append(f"... {len(volumes) - len(shown)} more")
    return ", ".join(parts)


def _parse_cells(raw, total):
    """(indices to keep, error). None indices means the caller gave nothing - the handler decides.
    An index outside 0..total-1 is REFUSED naming the value, never clamped to a neighbouring cell."""
    if raw in (None, "", []):
        return None, None
    items = raw if isinstance(raw, (list, tuple)) else \
        [s.strip() for s in str(raw).split(",") if s.strip()]
    out = []
    for v in items:
        if isinstance(v, bool):
            return None, f"'cells' takes cell index numbers - {v} is not one."
        try:
            i = int(v)
        except (TypeError, ValueError):
            return None, f"'cells' takes cell index numbers - '{v}' is not one."
        if not 0 <= i < total:
            return None, (f"'cells' index {i} does not exist - this boundary fill computed {total} "
                          f"cell(s), so the valid indices are 0..{total - 1}.")
        if i not in out:
            out.append(i)
    if not out:
        return None, "'cells' resolved to no cell index."
    return sorted(out), None


def _select_kept_cells(cells, total, keep):
    """Mark every cell, kept or not, returning an error string or ''. A boundary fill KEEPS a
    selected cell - the opposite of a trim, which removes it. Each write goes through set_verified,
    since a property assignment that does not take cannot raise."""
    keep_set = set(keep)
    # A cell's INDEX is its address, so this stays a positional walk: dropping an unreadable cell
    # would select a different set of cells than the one requested instead of refusing.
    for i in range(total):
        cell = safe(lambda i=i: cells.item(i))
        if cell is None:
            return f"Cell {i} could not be read back off the boundary-fill input."
        want = i in keep_set
        serr = _common.set_verified(cell, "isSelected", want, f"cell {i} isSelected={want}",
                                    "BRepCell")
        if serr:
            return serr + " The fill would run on a different set of cells than the one requested."
    return ""


def _tool_identity(bodies):
    """[(name, owning component)] per tool body, read BEFORE the feature runs - the basis for
    judging what remove_tools consumed."""
    # feature.isRemoveTools and feature.tools RAISE "3 : Didn't roll editing feature back." after
    # add(), and a CONSUMED body's pre-add proxy still reads isValid True with a stale volume. The
    # component's BODY CENSUS holds: after an isRemoveTools=True fill the body is gone from it.
    return [(safe(lambda b=b: b.name), safe(lambda b=b: b.parentComponent)) for b in bodies]


def _tool_fate(identities, produced_names):
    """(consumed, kept, unreadable count, inherited names) for the tool bodies, by re-scanning each
    tool's owning component after the fill. Matched by NAME within that component - body names are
    unique there, while an entityToken can be re-minted by the rebuild."""
    # Two answers the census cannot give a verdict on: a failed READ is not evidence a body is
    # gone, and a found name that a PRODUCED body also carries (the result body can inherit the
    # consumed tool's name) is the fill's own product, so it goes to `inherited` unjudged.
    consumed, kept, unreadable, inherited = [], [], 0, []
    for name, comp in identities:
        coll = safe(lambda c=comp: c.bRepBodies) if (name is not None and comp is not None) else None
        if coll is None:
            unreadable += 1
            continue
        found = safe(lambda c=coll, n=name: c.itemByName(n), _UNREADABLE)
        if found is _UNREADABLE:          # the lookup itself failed - not an answer either way
            unreadable += 1
            continue
        if found is None:
            consumed.append(name)
        elif name in produced_names:      # the fill's own product wearing the tool's name
            inherited.append(name)
        else:
            kept.append(name)
    return consumed, kept, unreadable, inherited


def _total_volume(bodies, factor):
    """The summed volume of `bodies` scaled by `factor`, or None when not one of them can be read -
    the MEASURED counterpart to the volume the input's cells predicted."""
    vals = [_common.measured(lambda b=b: b.volume, factor) for b in bodies]
    vals = [v for v in vals if v is not None]
    return round(sum(vals), 6) if vals else None


def handler(tools=None, cells=None, operation: str = "new", remove_tools: bool = False,
            units: str = "mm") -> dict:
    """See TOOL_DESCRIPTION."""
    design = _common.design()
    if not design:
        return error("No active design. Create or open a document first (see doc_new).")

    vals, verr = _inputs.resolve_inputs(_SPEC, {"tools": tools, "operation": operation,
                                                "units": units})
    if verr:
        return verr
    tool_bodies, op_key = vals["tools"], vals["operation"]
    units_key = (vals["units"] or "mm").strip().lower()
    factor = _common.CM_TO_UNIT[units_key] ** 3          # cm3 -> the caller's units cubed
    comp = target_component(design)

    # operation='join' with remove_tools=True and the target body among 'tools' (which join
    # REQUIRES) leaves a design with ZERO bodies: the merge lands INTO the target tool body and
    # remove_tools then consumes the merged result with the rest, so the pair is refused.
    if remove_tools and op_key != "new":
        return error(
            f"remove_tools=true is not supported with operation='{op_key}'. For join/cut/intersect "
            "the target body must itself be among 'tools', and remove_tools consumes the tools "
            "AFTER the boolean - measured on join: the merged result is deleted along with them, leaving a "
            "healthy feature and an EMPTY model. Use remove_tools with operation='new', or re-run "
            "with remove_tools=false and delete the tool bodies afterwards.")

    # Identify the tools NOW: remove_tools deletes them, and their proxies lie about it afterwards.
    tool_ids = _tool_identity(tool_bodies)

    # createInput wants an ObjectCollection of tools (not a plain list) plus the operation.
    coll = adsk.core.ObjectCollection.create()
    for b in tool_bodies:
        coll.add(b)
    op = getattr(adsk.fusion.FeatureOperations, _common.OPERATIONS[op_key])

    pre_solids = _solid_snapshot(design)
    before = _geom.volumes(pre_solids)

    # CRITICAL: createInput opens a transaction. Commit via add or abort via cancel - explicitly,
    # NOT under safe. Every path out from here carries _abort().
    fill_input = None
    try:
        fill_input = comp.features.boundaryFillFeatures.createInput(coll, op)
    except Exception as e:
        return error(f"Boundary fill failed: {e}. (The tools must enclose a volume between them.)"
                     + _abort(fill_input))
    if fill_input is None:
        return error("boundaryFillFeatures.createInput returned nothing - no boundary could be "
                     "calculated from the tools given.")

    try:
        cell_coll, volumes = _cell_volumes(fill_input, factor)
        if cell_coll is None:
            return error("The boundary-fill input's bRepCells could not be read, so which volumes "
                         "the tools enclose is unknown - nothing was created." + _abort(fill_input))
        total = len(volumes)
        if total == 0:
            return error("Boundary fill found no cell: the tools given do not enclose a volume "
                         "between them. Extend or add bodies until the region is closed."
                         + _abort(fill_input))
        keep, cerr = _parse_cells(cells, total)
        if cerr:
            return error(cerr + _abort(fill_input))
        if keep is None:
            if total > 1:
                return error(
                    f"Boundary fill computed {total} cells and 'cells' was not given - name the "
                    f"one(s) to keep by index: {_cell_listing(volumes, units_key)}. Re-run with "
                    "cells=[index] (several indices seal several cells in one feature). These "
                    "indices are valid for the NEXT call only - measured: the same tools enumerated "
                    "their cells in a different order on a later compute, so pick by the volume "
                    "listed here and re-read this listing after any model change."
                    + _abort(fill_input))
            keep = [0]
        serr = _select_kept_cells(cell_coll, total, keep)
        if serr:
            return error(serr + _abort(fill_input))
        serr = _common.set_verified(fill_input, "isRemoveTools", bool(remove_tools),
                                    f"remove_tools={bool(remove_tools)}",
                                    "BoundaryFillFeatureInput")
        if serr:
            return error(serr + _abort(fill_input))
        feature = comp.features.boundaryFillFeatures.add(fill_input)
    except Exception as e:
        # Live-measured: a CUT whose 'tools' hold only the enclosing surface raises at add() - the
        # cells can only be partitioned against a body that is itself among the tools, so a
        # cut/join/intersect failure points at the missing target body.
        hint = ("For cut/join/intersect, include the target body among 'tools' - the cells are "
                "partitioned against it." if op_key in ("cut", "join", "intersect") else
                "The cells are computed from the tools given - check that they enclose the volume "
                "you meant.")
        return error(f"Boundary fill failed: {e}. ({hint})" + _abort(fill_input))
    if not feature:
        return error(_common.no_feature_error(design, "Boundary fill",
                                              "The selected cell(s) produced nothing.")
                     + (_abort(fill_input) or " The open transaction was cancelled."))

    # Read the effect back: the feature's own bodies (and what they MEASURE now), what the fill did
    # to the solids that already existed, and the tool census.
    produced = _common.result_bodies(feature)
    facts = _common.body_facts(produced)
    result_volume = _total_volume(produced, factor)
    delta_cm3, delta_readable = _geom.volume_delta(pre_solids, before)
    # The census runs BEFORE the honesty gate: remove_tools=True blinds the other two signals at
    # once - feature.bodies comes back EMPTY, and a consumed solid's pre-add proxy keeps reporting
    # its pre-fill volume, so its contribution to the delta is stale-minus-stale = 0.
    consumed, kept_tools, unreadable, inherited = _tool_fate(tool_ids, {f["name"] for f in facts})
    if not facts and not consumed and not (delta_readable and abs(delta_cm3) > 1e-9):
        rolled = bool(safe(lambda: feature.deleteMe()))
        return error(
            "Boundary fill reported success but produced no body, consumed no tool, and left every "
            "solid in the design at its old volume - nothing was sealed. "
            + ("The empty feature was rolled back." if rolled else
               "The empty feature could NOT be deleted - remove it with design_delete_feature."))

    picked = [volumes[i] for i in keep]
    predicted = (round(sum(v for v in picked if v is not None), 6)
                 if any(v is not None for v in picked) else None)
    unclassified = unreadable + len(inherited)

    note = [f"Cell(s) {_OP_EFFECT[op_key]}. cells_volume_picked is the volume the input predicted "
            "for the kept cell(s); result_volume is what the feature's bodies measure now."]
    not_solid = [f["name"] for f in facts if not f["is_solid"]]
    if not_solid:
        note.append("PARTIAL: " + ", ".join(str(n) for n in not_solid) + " reads isSolid=false - "
                    "inspect it with model_inspect.")
    # A new-body fill's product IS the kept cells, so the numbers should agree; a divergence is
    # reported rather than errored, since feature.bodies also lists a pre-existing source body for
    # some feature types, which would inflate result_volume for a correct fill.
    if (op_key == "new" and predicted and result_volume is not None
            and abs(result_volume - predicted) > abs(predicted) * _VOLUME_TOLERANCE):
        note.append(f"The bodies produced measure {result_volume} {units_key}3, not the "
                    f"{predicted} {units_key}3 the kept cell(s) predicted - check what landed with "
                    "model_inspect.")
    if consumed:
        note.append("Tool bodies consumed (gone from their component): "
                    + ", ".join(str(n) for n in consumed) + ".")
    if remove_tools and kept_tools:
        note.append("remove_tools was requested but these tool bodies are still in their component: "
                    + ", ".join(str(n) for n in kept_tools) + ".")
    elif consumed and not remove_tools:
        note.append("remove_tools was NOT requested, yet those bodies are gone.")
    if unreadable:
        note.append(f"{unreadable} tool body(ies) could not be judged either way - their name, "
                    "component, or its body list could not be read.")
    if inherited:
        note.append("The fill's own result body carries the name of tool body "
                    + ", ".join(str(n) for n in inherited) + ", so the census cannot tell them "
                    "apart - reported as unjudged, NOT as a surviving tool. Do not delete it by "
                    "that name without checking with model_inspect first.")
    return ok({
        "filled": True,
        "feature": safe(lambda: feature.name),
        "operation": op_key,
        "cells_total": total,
        "cells_kept": keep,
        "cell_volumes": volumes,
        "cells_volume_picked": predicted,
        "result_bodies": facts,
        "result_volume": result_volume,
        "all_solid": bool(facts) and not not_solid,
        "existing_volume_change": round(delta_cm3 * factor, 6) if delta_readable else None,
        # the REQUEST, named as one: the feature's own isRemoveTools raises after add (see
        # _tool_identity), so tools_consumed/tools_kept - the body census - are the measured effect.
        "remove_tools_requested": bool(remove_tools),
        "tools_consumed": consumed,
        "tools_kept": kept_tools,
        "tools_unclassified": unclassified,
        "units": units_key,
        "note": " ".join(note),
    })


TOOL_DESCRIPTION = (
    "Seal the volume enclosed by several surface and/or solid bodies into a solid.\n"
    + _outputs.produces_block(RETURNS)
)

fill_tool = (
    _inputs.apply_to_tool(
        Tool.create_simple(name="surface_fill", description=TOOL_DESCRIPTION), _SPEC)
    .add_input_property("cells", {"type": "array", "items": {"type": "integer"},
                                  "description": "Indices of the cells to KEEP."})
    .add_input_property("remove_tools", {"type": "boolean",
                                         "description": "Consume the bounding bodies."})
    .strict_schema()
)
fill_item = Item.create_tool_item(tool=fill_tool, write="write", handler=handler,
                                  run_on_main_thread=True,
                                  postconditions=[_assert.FeatureHealthy()],
                                  verification=Verification(
                                      kind="inline", rung="geometry",
                                      evidence_test="tests/unit/test_surface_fill.py::TestHonesty"
                                      "::test_landing_nothing_is_an_error_with_a_rollback"))


def register_tool():
    register(fill_item)
