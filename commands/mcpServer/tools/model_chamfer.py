# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""MCP building block: bevel the edges of a body - equal distance, two distances, or a distance and
an angle. WRITES; what the feature BUILT is read back off it, never the request echoed.
"""

import adsk.core

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import error
from . import _inputs
from . import _assert
from ._edge_common import _BODY, _CORNER_TYPES, _EDGES, _EDGE_FILTER_DESC, _FACES, _apply

app = adsk.core.Application.get()

_CORNER_TYPE = _inputs.Choice("corner_type", list(_CORNER_TYPES))


def _angle_spec(angle_deg, distance_two):
    """(angle in degrees, error) for the distance-and-angle chamfer. A None angle with no error
    means the definition was not requested. 'distance_two' and 'angle_deg' are two DIFFERENT
    definitions of the same chamfer, so asking for both is refused rather than silently dropping
    one of them."""
    if angle_deg in (None, ""):
        return None, None
    try:
        d2 = float(distance_two or 0.0)
    except (TypeError, ValueError):
        return None, f"'distance_two' must be a number, got {distance_two!r}."
    if d2 > 0:
        return None, (f"'angle_deg' ({angle_deg}) and 'distance_two' ({distance_two}) are two "
                      "different chamfer definitions and cannot be combined. Pass 'distance_two' "
                      "for an asymmetric two-distance chamfer, or 'angle_deg' for a "
                      "distance-and-angle chamfer.")
    try:
        a = float(angle_deg)
    except (TypeError, ValueError):
        return None, f"'angle_deg' must be a number, got {angle_deg!r}."
    if a <= 0:
        return None, f"'angle_deg' must be positive, got {a}."
    return a, None


def handler(body_name: str = "", distance: float = 1.0, units: str = "mm",
            edge_filter: str = "", edges=None, faces=None, distance_two: float = 0.0,
            angle_deg=None, corner_type: str = "") -> dict:
    """Bevel edges with a Chamfer - equal-distance, two-distance (asymmetric) via 'distance_two',
    or distance-and-angle via 'angle_deg'. Edge handles, every edge of named faces, or a filter."""
    angle, aerr = _angle_spec(angle_deg, distance_two)
    if aerr:
        return error(aerr)
    corner_key, cerr = _CORNER_TYPE.resolve(corner_type)
    if cerr:
        return error(cerr)
    return _apply("chamfer", body_name, distance, units, edge_filter, edges, distance_two,
                  angle=angle, corner_key=corner_key, face_handles=faces)


TOOL_DESCRIPTION = (
"Bevel (chamfer) edges; model_fillet rounds instead."
)

tool = (
    Tool.create_simple(name="model_chamfer", description=TOOL_DESCRIPTION)
    .add_input_property("edges", _EDGES.schema())
    .add_input_property(*_FACES.as_property())
    .add_input_property("body_name", _BODY.schema())
    .add_input_property("distance", {"type": ["number", "string"],
        "description": "In 'units', or a parameter expression ('WallT/2')."})
    .add_input_property("distance_two", {"type": "number"})
    .add_input_property("angle_deg", {"type": "number"})
    .add_input_property(*_CORNER_TYPE.as_property())
    .add_input_property(*_inputs.UNITS.as_property())
    .add_input_property("edge_filter", {"type": "string", "enum": ["all", "convex", "concave"],
        "description": _EDGE_FILTER_DESC})
    .strict_schema()
)
item = Item.create_tool_item(tool=tool, write="write", handler=handler, run_on_main_thread=True,
                             postconditions=[_assert.FeatureHealthy()],
                             verification=Verification(
                                 kind="inline", rung="geometry",
                                 evidence_test="tests/unit/test_model_chamfer.py::TestVolumeReadBack"
                                 "::test_chamfer_with_unchanged_volume_errors_and_rolls_back"))


def register_tool():
    register(item)
