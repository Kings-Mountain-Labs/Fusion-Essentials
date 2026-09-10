# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""MCP building block: duplicate component occurrences or bodies evenly around an axis over a total
angle. WRITES; the instance count is read back off the created feature, never echoed.
"""

import adsk.core

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import error, ok, safe
from . import _common
from . import _inputs
from . import _assert
from ._pattern_common import (_BODIES, _OCCURRENCES, _direction_entity, _direction_label,
                              _owning_component, _resolve_input_entities)

app = adsk.core.Application.get()

# face_entity hands a cylindrical/conical FACE through: createInput's axis takes the entity that
# DEFINES the axis, and a face resolved to a direction vector would drop the axis POSITION.
_CIRC_AXIS = _inputs.AxisRef("axis", face_entity=True, default="z")


def handler(occurrences: str = "", bodies=None, quantity: int = 4, total_angle_deg: float = 360.0,
            axis: str = "z", symmetric: bool = False) -> dict:
    """Pattern component occurrences OR bodies evenly around an axis."""
    if int(quantity) < 2:
        return error("quantity must be >= 2 for a circular pattern.")
    design = _common.design()
    if not design:
        return error("No active design. Open or create a document with components first.")

    coll, resolved, rerr = _resolve_input_entities(design, occurrences, bodies)
    if rerr:
        return error(rerr)

    # The axis and the feature must share a component or Fusion raises getObjectPath.
    owner = _owning_component(design, coll, bodies)
    ax, axerr = _direction_entity(_CIRC_AXIS, axis, owner, design)
    if axerr:
        return error(axerr)

    try:
        pin = owner.features.circularPatternFeatures.createInput(coll, ax)
        pin.quantity = adsk.core.ValueInput.createByReal(int(quantity))
        pin.totalAngle = adsk.core.ValueInput.createByString(f"{float(total_angle_deg)} deg")
        # isSymmetric through the read-back helper: a bare assignment to a SWIG proxy can be
        # silently ignored, and the instance count reads the same either way.
        symerr = _common.set_verified(pin, "isSymmetric", bool(symmetric), "symmetric",
                                      "CircularPatternFeatureInput")
        if symerr:
            return error(f"{symerr} No pattern was created.")
        feature = owner.features.circularPatternFeatures.add(pin)
    except Exception as e:
        return error(f"Circular pattern failed: {e}")
    if not feature:
        return error(_common.no_feature_error(design, "Circular pattern"))

    # Verify the effect: the REAL instance count read off the created feature, never the request.
    real_total = safe(lambda: feature.patternElements.count)
    if real_total is not None and int(real_total) != int(quantity):
        return error(
            f"Pattern '{safe(lambda: feature.name)}' created {int(real_total)} instances but "
            f"{int(quantity)} were requested. The feature is left in the timeline for inspection - "
            "design_delete_feature removes it.")
    return ok({
        "patterned": True,
        "type": "circular",
        "feature": safe(lambda: feature.name),
        "entities": resolved,
        "entity_kind": "bodies" if bodies not in (None, "", []) else "occurrences",
        "axis": _direction_label(_CIRC_AXIS, axis, ax),
        "quantity": int(real_total) if real_total is not None else int(quantity),
        "total_angle_deg": float(total_angle_deg),
        "symmetric": bool(symmetric),
        "note": ("Bodies" if bodies not in (None, "", []) else "Occurrences")
                + " patterned around the axis. Pair with view_screenshot to view.",
    })


TOOL_DESCRIPTION = (
"Pattern occurrences or bodies evenly around an axis."
)
tool = (
    Tool.create_simple(name="model_pattern_circular", description=TOOL_DESCRIPTION)
    .add_input_property(*_OCCURRENCES.as_property())
    .add_input_property("bodies", _BODIES.schema())
    .add_input_property("quantity", {"type": "integer"})
    .add_input_property("total_angle_deg", {"type": "number", "description": "360 = a full ring."})
    .add_input_property(*_CIRC_AXIS.as_property())
    .add_input_property("symmetric", {"type": "boolean"})
    .strict_schema()
)
item = Item.create_tool_item(tool=tool, write="write", handler=handler,
                             run_on_main_thread=True,
                             postconditions=[_assert.FeatureHealthy(),
                                             _assert.PatternElementsPlaced(severity="soft")],
                             verification=Verification(
                                 kind="inline", rung="value",
                                 evidence_test="tests/unit/test_model_pattern_circular.py"
                                 "::TestInstanceCountReadBack"
                                 "::test_a_feature_reporting_fewer_instances_is_an_error_naming_both_counts"))


def register_tool():
    register(item)
