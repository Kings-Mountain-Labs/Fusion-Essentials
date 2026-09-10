# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""MCP building block: pattern occurrences or bodies ALONG A PATH.

  model_pattern_path -> duplicate occurrences/bodies along a curve (model edges or a path sketch):
                        a count plus either the spacing between instances or the total extent to
                        spread them over. WRITES.
"""

import adsk.core
import adsk.fusion

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import error, ok, safe, set_verified, build_path
from . import _common
from . import _inputs
from . import _assert
from . import _outputs
from ._pattern_common import _BODIES, _OCCURRENCES, _owning_component, _resolve_input_entities

app = adsk.core.Application.get()

# 'spacing' is the distance BETWEEN instances; 'extent' spreads the quantity over a total distance.
_DISTANCE_TYPES = {
    "spacing": "SpacingPatternDistanceType",
    "extent": "ExtentPatternDistanceType",
}

RETURNS = [
    _outputs.ReturnsName("feature", of="feature", consumers=["design_delete_feature"]),
]

_DISTANCE_TYPE = _inputs.Choice("distance_type", tuple(_DISTANCE_TYPES), default="spacing")
_DISTANCE = _inputs.Distance("distance", allow_zero=False, allow_negative=False, required=True)


def _start_point(raw):
    """(fraction along the path, error) - 0 is the path's start point, 1 its end point."""
    if raw is None or raw == "":
        return 0.0, None
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None, f"'start_point' must be a number between 0 and 1, got {raw!r}."
    if not 0.0 <= v <= 1.0:
        return None, (f"'start_point' must be between 0 and 1 (0 = the path's start, 1 = its end), "
                      f"got {v}.")
    return v, None


def handler(occurrences: str = "", bodies=None, path=None, quantity: int = 2, distance=None,
            distance_type: str = "spacing", start_point=None, symmetric: bool = False,
            units: str = "mm") -> dict:
    """See TOOL_DESCRIPTION."""
    if int(quantity) < 2:
        return error("quantity must be >= 2 for a path pattern (the original plus at least one copy).")
    dt_key, dterr = _DISTANCE_TYPE.resolve(distance_type)
    if dterr:
        return error(dterr)
    scale_factor, uerr = _inputs.UNITS.resolve(units)
    if uerr:
        return error(uerr)
    dist_cm, derr = _DISTANCE.resolve_scaled(distance, scale_factor)
    if derr:
        return error(derr)
    start, serr = _start_point(start_point)
    if serr:
        return error(serr)

    design = _common.design()
    if not design:
        return error("No active design. Open or create a document with components first.")

    coll, resolved, rerr = _resolve_input_entities(design, occurrences, bodies)
    if rerr:
        return error(rerr)

    # The path and the feature must share the component that owns the patterned entities.
    owner = _owning_component(design, coll, bodies)
    pattern_path, path_label, patherr = build_path(owner, path)
    if patherr:
        return error(patherr)

    dist_type = safe(lambda: getattr(adsk.fusion.PatternDistanceType, _DISTANCE_TYPES[dt_key]))
    if dist_type is None:
        return error(f"PatternDistanceType.{_DISTANCE_TYPES[dt_key]} is not available on this "
                     "Fusion version.")
    try:
        q = adsk.core.ValueInput.createByReal(int(quantity))
        d = adsk.core.ValueInput.createByReal(float(dist_cm))
        pin = owner.features.pathPatternFeatures.createInput(coll, pattern_path, q, d, dist_type)
    except Exception as e:
        return error(f"Could not start the path pattern: {e}")
    if not pin:
        return error("Path pattern createInput returned nothing, so no pattern was created.")

    # Each setter answers whether it took; a SWIG proxy accepts an assignment it then ignores, so
    # both are set through the read-back helper rather than assigned bare.
    for prop, value, label in (("startPoint", float(start), "start_point"),
                               ("isSymmetric", bool(symmetric), "symmetric")):
        seterr = set_verified(pin, prop, value, label, "PathPatternFeatureInput")
        if seterr:
            return error(f"{seterr} No pattern was created.")

    try:
        feature = owner.features.pathPatternFeatures.add(pin)
    except Exception as e:
        return error(f"Path pattern failed: {e}. Check that the path is one connected chain and "
                     "that the entities sit on or near it.")
    if not feature:
        return error(_common.no_feature_error(design, "Path pattern"))

    # Verify the effect: the REAL instance count read off the created feature, never the request.
    real_total = safe(lambda: feature.patternElements.count)
    note = ("Instances placed along the path. Copies keep the seed's orientation; they do not "
            "rotate to follow the path. Pair with view_screenshot to view.")
    if real_total is None:
        note += (" 'quantity' could NOT be read back off the created feature, so it is reported as "
                 "null rather than as the count requested - confirm in Fusion, or with design_get.")
    if real_total is not None and int(real_total) != int(quantity):
        return error(
            f"Pattern '{safe(lambda: feature.name)}' created {int(real_total)} instances but "
            f"{int(quantity)} were requested - the path may be too short for the spacing asked "
            "for. The feature is left in the timeline for inspection - design_delete_feature "
            "removes it.")
    return ok({
        "patterned": True,
        "type": "path",
        "feature": safe(lambda: feature.name),
        "entities": resolved,
        "entity_kind": "bodies" if bodies not in (None, "", []) else "occurrences",
        "path": path_label,
        "quantity": int(real_total) if real_total is not None else None,
        "distance_type": dt_key,
        "distance": round(float(distance), 6),
        "units": units,
        "start_point": float(start),
        "symmetric": bool(symmetric),
        # isOrientationAlongPath is False by default, so the copies do not turn with the path.
        "note": note,
    })


TOOL_DESCRIPTION = (
"Pattern occurrences or bodies along a path.\n"
+ _outputs.produces_block(RETURNS)
)

pattern_path_tool = (
    Tool.create_simple(name="model_pattern_path", description=TOOL_DESCRIPTION)
    .add_input_property(*_OCCURRENCES.as_property())
    .add_input_property("bodies", _BODIES.schema())
    .add_input_property("path", {"type": ["string", "array"], "items": {"type": "string"},
            "description": "An edge 'handle' (chains across TANGENT connections only; the 'path' "
                           "count is the truth), or 'sketch:<name>'."})
    .add_input_property("quantity", {"type": "integer"})
    .add_input_property(*_DISTANCE.as_property())
    .add_input_property(*_DISTANCE_TYPE.as_property())
    .add_input_property("start_point", {"type": "number"})
    .add_input_property("symmetric", {"type": "boolean"})
    .add_input_property(*_inputs.UNITS.as_property())
    .strict_schema()
)
pattern_path_item = Item.create_tool_item(tool=pattern_path_tool, write="write", handler=handler,
                                          run_on_main_thread=True,
                                          postconditions=[_assert.FeatureHealthy(),
                                                          _assert.PatternElementsPlaced(severity="soft")],
                                          verification=Verification(
                                              kind="inline", rung="value",
                                              evidence_test="tests/unit/test_model_pattern_path.py"
                                              "::test_wrong_instance_count_fails_the_call"))


def register_tool():
    register(pattern_path_item)
