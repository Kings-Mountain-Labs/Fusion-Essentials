# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""MCP building block: trim an OPEN surface body against a tool that intersects and divides it.
TrimFeatures.createInput opens a partial-compute transaction that must be committed via add() or
aborted via TrimFeatureInput.cancel() - never let an exception leak it open. WRITES.
"""

import adsk.core

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import error, ok, safe, target_component
from . import _common
from . import _inputs
from . import _assert
from ._surface_common import _body_names_and_solid

app = adsk.core.Application.get()

_SURFACE = _inputs.SurfaceBodyRef("surface", required=True)
_TRIM_TOOL = _inputs.GeometryHandle("trim_tool", require="face", required=True)


def _abort(trim_input):
    """Cancel this tool's open TrimFeatureInput transaction - the shared abort, named for the trim."""
    return _common.cancel_input(trim_input, "trim")


_KEEP_FORMS = "'keep' takes 'larger', 'smaller', or cell index number(s)"


def _parse_keep_indices(keep, total):
    """(set of cell indices, error) for an explicit 'keep'. A value that is not a cell index, or an
    index outside 0..total-1, is REFUSED naming the value - never swapped for the largest cell,
    which would trim away a different piece of the surface than the caller asked to keep."""
    if isinstance(keep, (list, tuple)):
        items = list(keep)
    elif isinstance(keep, str):
        items = [s.strip() for s in keep.split(",") if s.strip()]
    else:
        items = [keep]
    out = set()
    for v in items:
        if isinstance(v, bool):
            return None, f"{_KEEP_FORMS} - {v} is not one."
        try:
            i = int(v)
        except (TypeError, ValueError):
            return None, f"{_KEEP_FORMS} - '{v}' is not one."
        if not 0 <= i < total:
            return None, (f"'keep' index {i} does not exist - this trim computed {total} cell(s), "
                          f"so the valid indices are 0..{total - 1}.")
        out.add(i)
    if not out:
        return None, f"{_KEEP_FORMS} - '{keep}' names no cell."
    return out, ""


def _select_cells(trim_input, keep):
    """(kept_indices, kept_area, total, err) for 'keep': omitted / "larger" keeps the single largest
    cell by cellBody.area, "smaller" the smallest, an int/str index or list of indices keeps those,
    and anything else is REFUSED naming the value."""
    # For a Trim feature a SELECTED cell is REMOVED, so a kept cell keeps isSelected=False.
    # createInput does a partial compute to populate bRepCells, and with zero cells selected add()
    # raises "No cells are selected".
    cells = trim_input.bRepCells
    total = int(safe(lambda: cells.count, 0) or 0)
    if total == 0:
        return None, None, 0, ("Trim failed: the trim tool does not divide the surface (no cells). "
                               "(The trim tool must INTERSECT the surface and divide it.)")

    areas = [float(safe(lambda i=i: cells.item(i).cellBody.area, 0.0) or 0.0) for i in range(total)]
    # A cell's INDEX is its address ('keep' takes an int index, 'areas' is indexed by it, and the
    # kept indices are published), so this walk and the selection walk below stay positional:
    # iter_collection drops an unreadable cell, sliding every later cell onto the wrong index.

    named = keep.strip().lower() if isinstance(keep, str) else keep
    if named in (None, "", [], "larger"):
        # DEFAULT: keep the single largest cell by area
        keep_set = {max(range(total), key=lambda i: areas[i])}
    elif named == "smaller":
        keep_set = {min(range(total), key=lambda i: areas[i])}
    else:
        keep_set, kerr = _parse_keep_indices(named, total)
        if kerr:
            return None, None, total, kerr

    kept_area = 0.0
    for i in range(total):
        cell = safe(lambda i=i: cells.item(i))
        if cell is None:
            continue
        if i in keep_set:
            cell.isSelected = False        # KEEP this cell
            kept_area += areas[i]
        else:
            cell.isSelected = True         # REMOVE (select) this cell
    return sorted(keep_set), round(kept_area, 6), total, None


def _visible_surface_scope(design, surface):
    """Refuse a trim when another visible surface can enter its global cell computation."""
    from ._view_common import same_body

    def complete(collection):
        count = collection.count
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError("invalid collection count")
        values = list(_common.iter_collection(collection))
        if len(values) != count:
            raise ValueError("incomplete collection")
        return values

    try:
        if surface.isVisible is not True:
            return "The target surface must be visible; use view_set action='show' first."
        found = False
        for component in complete(design.allComponents):
            for body in complete(component.bRepBodies):
                if same_body(body, surface):
                    found = True
                    continue
                visible = body.isVisible
                if visible is False:
                    continue
                solid = body.isSolid
                if visible is not True or (solid is not True and solid is not False):
                    raise ValueError("unreadable body visibility or solid state")
                if solid is False:
                    name = _inputs.qualified_body_name(body)
                    return (f"Other visible surface '{name}' can enter this trim. "
                            "Use view_set action='hide' for other surface bodies, then retry.")
        if not found:
            raise ValueError("target missing from body census")
    except Exception as exc:
        return f"Cannot establish trim surface scope: {exc}. Refresh the design and retry."
    return None


def handler(surface=None, trim_tool=None, keep=None) -> dict:
    """Trim a surface against a tool that intersects it - remove the unwanted cell(s)."""
    design = _common.design()
    if not design:
        return error("No active design. Create or open a document first (see doc_new).")
    comp = target_component(design)

    surf, serr = _SURFACE.resolve(surface)     # validates isSolid == false, redirecting error otherwise
    if serr:
        return error(serr)
    tool, terr = _TRIM_TOOL.resolve(trim_tool)
    if terr:
        return error(terr)
    scope_error = _visible_surface_scope(design, surf)
    if scope_error:
        return error(scope_error)
    area_before = safe(lambda: surf.area)

    # createInput opens a transaction: commit via add or abort via cancel, explicitly and NOT under
    # safe, including on an exception or a null feature.
    trim_input = None
    cell_info = None
    try:
        trim_input = comp.features.trimFeatures.createInput(tool)
        kept, kept_area, total, cerr = _select_cells(trim_input, keep)
        if cerr:
            # no intersection, or a 'keep' naming no real cell - either way the open transaction
            # must be aborted before returning, and nothing is trimmed on a guessed cell.
            return error(cerr + _abort(trim_input))
        cell_info = {"cells_total": total, "cells_kept": kept,
    "cells_removed": [i for i in range(total) if i not in set(kept)],
    "kept_area": kept_area}
        # PHANTOM-CELL GATE, before the add. createInput takes only the tool, so its cells span
        # every VISIBLE surface the tool crosses; a kept area LARGER than the target's own proves a
        # cell from another surface got in. One-sided: a smaller foreign cell sails through.
        if kept_area is not None and area_before and kept_area > area_before * (1 + 1e-6):
            aborted = _abort(trim_input)
            return error(
                f"Trim aborted: kept area {round(kept_area * 100.0, 1)} mm2 is larger than "
                f"target area {round(area_before * 100.0, 1)} mm2. HIDE other surfaces with "
                "view_set, re-read the target and retry. The transaction was cancelled." + aborted)
        feature = comp.features.trimFeatures.add(trim_input)
    except Exception as e:
        # abort the open partial-compute transaction so Fusion isn't left in a bad state
        return error(f"Trim failed: {e}. (The trim tool must INTERSECT the surface and divide "
                     f"it.){_abort(trim_input)}")
    if not feature:
        # add returned nothing but didn't raise - still must abort the transaction we opened
        return error(_common.no_feature_error(design, "Trim",
                                              "(The tool may not intersect the surface.)")
                     + (_abort(trim_input) or " The open transaction was cancelled."))

    names, any_solid = _body_names_and_solid(feature)
    # Commit proof: removing cells must shrink the surface's area; unchanged area = no cell removed.
    area_after = None
    vals = [safe(lambda b=b: b.area) for b in _common.result_bodies(feature)]
    vals = [v for v in vals if v]
    if vals:
        area_after = sum(vals)
    if (cell_info and cell_info["cells_removed"] and area_before and area_after is not None
            and area_after >= area_before * (1 - 1e-6)):
        return error(f"Trim committed but the surface area did not decrease "
                     f"({round(area_before, 4)} cm2 before and after) - no cell was actually removed.")
    payload = {
    "trimmed": True,
    "feature": safe(lambda: feature.name),
    "surface": safe(lambda: surf.name),
    "result_body": names[0] if names else None,
    "result_bodies": names,
    "is_solid": any_solid,
    "note": "Surface trimmed. Selected cells removed; the open transaction was committed via add().",
    }
    if cell_info is not None:
        payload.update(cell_info)
    payload["surface_scope"] = "no_other_visible_surfaces"
    if any_solid is None:
        payload["unverified"] = ["is_solid"]
        payload["note"] += " Not read back off the feature: is_solid."
    return ok(payload)


TOOL_DESCRIPTION = (
"Trim one visible open surface body; hide other surface bodies with view_set first."
)
tool = (
    Tool.create_simple(name="surface_trim", description=TOOL_DESCRIPTION)
    .add_input_property("surface", _SURFACE.schema())
    .add_input_property("trim_tool", _TRIM_TOOL.schema())
    .add_input_property("keep", {"type": ["string", "array"],
            "description": "'larger' (default), 'smaller', or cell index number(s)."})
    .add_required_input("surface")
    .add_required_input("trim_tool")
    .strict_schema()
)
item = Item.create_tool_item(tool=tool, write="write", handler=handler,
                             run_on_main_thread=True,
                             postconditions=[_assert.FeatureHealthy()],
                             verification=Verification(
                                 kind="inline", rung="geometry",
                                 evidence_test="tests/unit/test_surface_trim.py::TestSurfaceTrim"
                                               "::test_trim_that_removes_no_area_bites"))


def register_tool():
    register(item)
