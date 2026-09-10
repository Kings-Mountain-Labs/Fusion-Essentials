# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""MCP building block: duplicate component occurrences or bodies in a rectangular grid, along one
axis or two. WRITES; the instance count is read back off the created feature, never echoed.
"""

import adsk.core
import adsk.fusion

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import error, ok, safe, scale
from . import _common
from . import _inputs
from . import _assert
from ._pattern_common import (_BODIES, _OCCURRENCES, _direction_entity, _direction_label,
                              _owning_component, _resolve_input_entities)

app = adsk.core.Application.get()

# entity_only refuses a FACE handle: RectangularPatternFeatureInput's direction takes a linear
# ENTITY, and a face resolves to a direction VECTOR that input cannot consume.
_DIR_ONE = _inputs.AxisRef("direction_one", entity_only=True, default="x")
_DIR_TWO = _inputs.AxisRef("direction_two", entity_only=True, default="y")


def handler(occurrences: str = "", bodies=None, quantity_one: int = 2, spacing_one: float = 10.0,
            direction_one: str = "x", quantity_two: int = 1, spacing_two: float = 10.0,
            direction_two: str = "y", units: str = "mm") -> dict:
    """Pattern component occurrences OR bodies in a rectangular grid."""
    k = scale(units)
    if k is None:
        return error(f"Unknown units '{units}'. Use mm, cm, or in.")
    if int(quantity_one) < 1:
        return error("quantity_one must be >= 1.")
    # spacing_one=0 stacks every instance on the seed and the platform reports a clean pattern.
    if int(quantity_one) > 1 and float(spacing_one) == 0:
        return error("spacing_one=0 would stack every instance exactly on the seed (coincident "
                     "duplicates). Provide a non-zero spacing_one.")
    if int(quantity_two) > 1 and float(spacing_two) == 0:
        return error("spacing_two=0 would stack the second-direction instances exactly on the "
                     "first row (coincident duplicates). Provide a non-zero spacing_two.")
    design = _common.design()
    if not design:
        return error("No active design. Open or create a document with components first.")

    coll, resolved, rerr = _resolve_input_entities(design, occurrences, bodies)
    if rerr:
        return error(rerr)

    # The axis and the feature must share a component or Fusion raises getObjectPath.
    owner = _owning_component(design, coll, bodies)
    d1, d1err = _direction_entity(_DIR_ONE, direction_one, owner, design)
    if d1err:
        return error(d1err)
    # Direction two is resolved even for a single row - it is always set below.
    d2, d2err = _direction_entity(_DIR_TWO, direction_two, owner, design)
    if d2err:
        return error(d2err)

    try:
        dist_type = adsk.fusion.PatternDistanceType.SpacingPatternDistanceType
        q1 = adsk.core.ValueInput.createByReal(int(quantity_one))
        s1 = adsk.core.ValueInput.createByReal(float(spacing_one) * k)
        pin = owner.features.rectangularPatternFeatures.createInput(coll, d1, q1, s1, dist_type)

        # Direction two is ALWAYS set: a fresh createInput carries quantityTwo=3, so a single-row
        # request that leaves it alone builds three coincident rows.
        q2 = adsk.core.ValueInput.createByReal(max(1, int(quantity_two)))
        s2 = adsk.core.ValueInput.createByReal(float(spacing_two) * k)
        if not pin.setDirectionTwo(d2, q2, s2):
            return error("Fusion refused the second pattern direction (setDirectionTwo returned "
                         "false), so no pattern was created.")

        feature = owner.features.rectangularPatternFeatures.add(pin)
    except Exception as e:
        return error(f"Rectangular pattern failed: {e}")
    if not feature:
        return error(_common.no_feature_error(design, "Rectangular pattern"))

    # Verify the effect: the REAL instance count read off the created feature, never the request.
    requested_total = int(quantity_one) * max(1, int(quantity_two))
    real_total = safe(lambda: feature.patternElements.count)
    if real_total is not None and int(real_total) != requested_total:
        return error(
            f"Pattern '{safe(lambda: feature.name)}' created {int(real_total)} instances but "
            f"{requested_total} were requested ({quantity_one} x {max(1, int(quantity_two))}). The "
            "feature is left in the timeline for inspection - design_delete_feature removes it.")
    return ok({
        "patterned": True,
        "type": "rectangular",
        "feature": safe(lambda: feature.name),
        "entities": resolved,
        "entity_kind": "bodies" if bodies not in (None, "", []) else "occurrences",
        "direction_one": _direction_label(_DIR_ONE, direction_one, d1),
        "quantity_one": int(quantity_one),
        "spacing_one": round(float(spacing_one), 6),
        "direction_two": (_direction_label(_DIR_TWO, direction_two, d2)
                          if int(quantity_two) > 1 else None),
        "quantity_two": int(quantity_two), "spacing_two": round(float(spacing_two), 6),
        "units": units,
        # the read-back count when available (the verified value); the computed request otherwise
        "total_instances": int(real_total) if real_total is not None else requested_total,
        "note": ("Bodies" if bodies not in (None, "", []) else "Occurrences")
                + " patterned in a grid. Pair with view_screenshot to view.",
    })


TOOL_DESCRIPTION = (
"Pattern occurrences or bodies in a rectangular grid."
)
tool = (
    Tool.create_simple(name="model_pattern_rectangular", description=TOOL_DESCRIPTION)
    .add_input_property(*_OCCURRENCES.as_property())
    .add_input_property("bodies", _BODIES.schema())
    .add_input_property("quantity_one", {"type": "integer"})
    .add_input_property("spacing_one", {"type": "number",
            "description": "Distance between instances."})
    .add_input_property(*_DIR_ONE.as_property())
    .add_input_property("quantity_two", {"type": "integer"})
    .add_input_property("spacing_two", {"type": "number"})
    .add_input_property(*_DIR_TWO.as_property())
    .add_input_property(*_inputs.UNITS.as_property())
    .strict_schema()
)
item = Item.create_tool_item(tool=tool, write="write", handler=handler,
                             run_on_main_thread=True,
                             postconditions=[_assert.FeatureHealthy(),
                                             _assert.PatternElementsPlaced(spacings=(
                                                 ("quantity_one", "spacing_one", 2, 10.0),
                                                 ("quantity_two", "spacing_two", 1, 10.0)))],
                             verification=Verification(
                                 kind="inline", rung="value",
                                 evidence_test="tests/unit/test_model_pattern_rectangular.py"
                                 "::TestInstanceCountReadBack"
                                 "::test_a_feature_reporting_fewer_instances_is_an_error_naming_both_counts"))


def register_tool():
    register(item)
