# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""MCP building block: draw a line in 3D on a sketch, where the END point may sit OFF the sketch
plane (z != 0, measured along the sketch's LOCAL normal). WRITES.
"""

import adsk.core

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import error, ok, safe, scale
from ._sketch_detail import COMPONENT_SCOPE, _detail_engine, _sketch_summary
from . import _common
from . import _inputs

app = adsk.core.Application.get()


def _pt3(x, y, z, k):
    """Point3D at (x,y,z)*k in cm - a TRUE 3D point (z may be non-zero, i.e. off the sketch plane)."""
    return adsk.core.Point3D.create(x * k, y * k, z * k)


def _xyz(sketch_point, k):
    """Read a SketchPoint's geometry as user-unit (x, y, z), rounded for readability."""
    g = safe(lambda: sketch_point.geometry)
    if g is None:
        return None
    return {
    "x": round(safe(lambda: g.x, 0.0) / k, 6),
    "y": round(safe(lambda: g.y, 0.0) / k, 6),
    "z": round(safe(lambda: g.z, 0.0) / k, 6),
    }


def handler(sketch_name: str = "", units: str = "mm",
            x1: float = 0.0, y1: float = 0.0, z1: float = 0.0,
            x2: float = None, y2: float = None, z2: float = None,
            coincident_start_to_origin: bool = False,
            is_construction: bool = False, component: str = "") -> dict:
    """Draw a line in 3D on a sketch - the end point may be off the sketch plane (z != 0)."""
    k = scale(units)
    if k is None:
        return error(f"Unknown units '{units}'. Valid: mm, cm, in.")
    for key, val in (("x2", x2), ("y2", y2), ("z2", z2)):
        if val is None:
            return error("Provide the end point: x2, y2, z2 (the start defaults to the origin, "
    "0,0,0; set coincident_start_to_origin=true to lock it there).")

    design = _common.design()
    if not design:
        return error("No active design. Create or open a document first (see doc_new).")

    sketch, requested, refusal = _detail_engine().scoped_or_recent_sketch(
        design, sketch_name, component)
    if refusal:
        return error(refusal)
    if not sketch:
        if requested:
            return error(f"No sketch named '{requested}'. Use sketch_get or sketch_create.")
        return error("No sketch to draw on. Create one first with sketch_create.")

    try:
        line = sketch.sketchCurves.sketchLines.addByTwoPoints(
            _pt3(x1, y1, z1, k), _pt3(x2, y2, z2, k))
    except Exception as e:
        return error(f"Failed to draw 3D line: {e}")
    if not line:
        return error("3D line creation returned no entity.")

    if is_construction:
        # MUTATION - a rejected set must surface (with the partial state named), not silently no-op
        try:
            line.isConstruction = True
        except Exception as e:
            return error(f"Line was drawn but could not be marked construction: {e}")

    constraint_added = False
    constraint_error = None
    if coincident_start_to_origin:
        try:
            origin_pt = sketch.originPoint
            start_pt = line.startSketchPoint
            con = sketch.geometricConstraints.addCoincident(start_pt, origin_pt)
            constraint_added = con is not None
        except Exception as e:
            constraint_error = str(e)

    start_xyz = _xyz(safe(lambda: line.startSketchPoint), k)
    end_xyz = _xyz(safe(lambda: line.endSketchPoint), k)
    off_plane = bool(end_xyz and abs(end_xyz.get("z", 0.0)) > 1e-9)

    result = {
    "drawn": "3d_line",
    "sketch_name": safe(lambda: sketch.name),
    "units": units,
    "start": start_xyz,
    "end": end_xyz,
    "end_is_off_plane": off_plane,
    "is_construction": bool(safe(lambda: line.isConstruction, False)),
    "coincident_start_to_origin": constraint_added,
    "sketch": _sketch_summary(sketch),
    "note": ("Line drawn in 3D. The end point's non-zero z places it off the sketch's x-y "
        "plane. View it from an iso angle with view_screenshot (a top view hides the "
        "out-of-plane component)."),
    }
    if constraint_error:
        result["coincident_constraint_error"] = constraint_error
    return ok(result)


TOOL_DESCRIPTION = (
    "Draw a sketch line whose end may sit OFF the sketch plane: x/y/z are in 'units', z along the "
    "sketch's own normal. The start defaults to the origin."
)
tool = (
    Tool.create_simple(name="sketch_add_3d_line", description=TOOL_DESCRIPTION)
    .add_input_property("sketch_name", {"type": "string", "description": "Default: most recent sketch."})
    .add_input_property(*COMPONENT_SCOPE)
    .add_input_property(*_inputs.UNITS.as_property())
    .add_input_property("x1", {"type": "number"})
    .add_input_property("y1", {"type": "number"})
    .add_input_property("z1", {"type": "number"})
    .add_input_property("x2", {"type": "number"})
    .add_input_property("y2", {"type": "number"})
    .add_input_property("z2", {"type": "number"})
    .add_input_property("coincident_start_to_origin", {"type": "boolean"})
    .add_input_property("is_construction", {"type": "boolean",
            "description": "Reference geometry, not a profile edge."})
    .strict_schema()
)
item = Item.create_tool_item(
    tool=tool, write="write", handler=handler, run_on_main_thread=True,
    verification=Verification(
        kind="effect", rung="value",
        evidence_test="tests/unit/test_sketch_add_3d_line.py::TestDraw3dLine"
                      "::test_a_stuck_construction_flag_is_published_as_the_line_reads_it"))


def register_tool():
    register(item)
