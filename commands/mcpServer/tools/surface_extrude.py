# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""MCP building block: extrude an OPEN profile into a sheet (surface) body - isSolid == False.
WRITES; never wrap a feature .add() in safe() - assert the returned feature/body and read isSolid back.
"""

import adsk.core
import adsk.fusion

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import error, ok, safe, scale, target_component
from . import _common
from . import _inputs
from . import _sketch_detail
from . import _assert
from ._surface_common import (_CURVES, _SURFACE_OPS, _body_names_and_solid, _curve_host_component,
                              _open_profile_from_curves)

app = adsk.core.Application.get()


def _landed_depth(feature, want_cm, k):
    """(depth in display units, error) over _common.landed_extent_cm - the feature's OWN depth, not
    the echoed input; a depth that cannot be read comes back None for the caller's `unverified`."""
    got = _common.landed_extent_cm(feature)
    if got is None:
        return None, ""
    if abs(got - want_cm) > _common.EXTENT_MATCH_TOL_CM:
        return None, (f"The surface was extruded but its depth reads back {round(got / k, 6)}, not "
                      f"the requested {round(want_cm / k, 6)}.")
    return round(got / k, 6), ""


def handler(sketch_name: str = "", curves=None, distance: float = 0.0,
            units: str = "mm", symmetric: bool = False, operation: str = "new",
            component: str = "") -> dict:
    """Extrude an OPEN profile into a sheet (surface) body - isSolid == False."""
    k = scale(units)
    if k is None:
        return error(f"Unknown units '{units}'. Use mm, cm, or in.")
    if distance == 0:
        return error("Provide a non-zero 'distance' to extrude.")
    op_key = (operation or "new").strip().lower()
    if op_key not in _SURFACE_OPS:
        return error(f"Unknown operation '{operation}'. Surface extrude supports: new, join.")

    design = _common.design()
    if not design:
        return error("No active design. Create or open a document first (see doc_new).")
    comp = target_component(design)

    # profile: from explicit curve handles, else from a sketch's open chain. Build the profile AND the
    # feature on the component that OWNS the source (the curves' or sketch's owner) - a profile-consuming
    # feature created on the active component raises bSet when the source is owned elsewhere.
    if curves not in (None, "", []):
        resolved, cerr = _CURVES.resolve(curves)
        if cerr:
            return error(cerr)
        coll, meta = resolved
        ents = meta["entities"]
        if not ents:
            return error("'curves' resolved to no edges/curves.")
        host = _curve_host_component(ents, comp)
        profile, perr = _open_profile_from_curves(host, ents)
        source = "curves"
    else:
        sketch, requested, ambiguous = _sketch_detail.scoped_or_recent_sketch(
            design, sketch_name, component)
        if ambiguous:
            return error(ambiguous)
        if not sketch:
            if requested:
                return error(f"No sketch named '{requested}'. Use sketch_get or sketch_create.")
            return error("No sketch or 'curves' to extrude. Draw an OPEN chain first, or pass curves.")
        host = safe(lambda: sketch.parentComponent) or comp
        profile, perr = _common.open_profile_from_sketch(host, sketch, "from the sketch")
        source = safe(lambda: sketch.name)
    if perr:
        return error(perr)

    op = getattr(adsk.fusion.FeatureOperations, _common.OPERATIONS[op_key])
    try:
        ext_input = host.features.extrudeFeatures.createInput(profile, op)
        ext_input.isSolid = False        # THE surface switch: no end caps, an open sheet body
        dist_val = adsk.core.ValueInput.createByReal(float(distance) * k)
        if not ext_input.setDistanceExtent(bool(symmetric), dist_val):
            return error(f"Fusion refused a {'symmetric ' if symmetric else ''}distance extent of "
                         f"{distance} {units}, so no surface was extruded.")
        feature = host.features.extrudeFeatures.add(ext_input)
    except Exception as e:
        return error(f"Surface extrude failed: {e}.")
    if not feature:
        return error(_common.no_feature_error(design, "Surface extrude"))

    names, any_solid = _body_names_and_solid(feature)
    # An empty result set is a failure: 'created: true' beside result_bodies [] claims a sheet the
    # payload cannot show, and there is no body to read isSolid off.
    if not names:
        return error("Surface extrude reported success but the feature owns no result body - no "
                     "sheet was created. " + _common.failed_effect_remedy(design, feature))
    landed, rerr = _landed_depth(feature, float(distance) * k, k)
    if rerr:
        return error(rerr + " " + _common.failed_effect_remedy(design, feature))

    unverified = []
    if any_solid is False:
        note = ("Open surface body created (isSolid=false). Feed it to "
                "surface_trim/extend/patch/thicken.")
    elif any_solid is True:
        note = ("The result reads back SOLID (isSolid=true) - the profile closed into a solid, not "
                "a sheet.")
    else:
        note = ("Surface body created, but no result body's isSolid flag could be read back - "
                "whether it is an open sheet is UNVERIFIED.")
        unverified.append("is_solid")
    payload = {
        "created": True,
        "feature": safe(lambda: feature.name),
        "operation": op_key,
        "source": source,
        "result_bodies": names,
        "is_solid": any_solid,       # read back from the body, not assumed (expected False for a sheet)
        "open_edge_count": len(meta["entities"]) if curves not in (None, "", []) else None,
        "distance": round(float(distance), 6),
        "units": units,
        "symmetric": bool(symmetric),
        "note": note,
    }
    if landed is None:
        unverified.append("distance")
        payload["note"] += " Not read back off the feature: distance."
    else:
        payload["distance"] = landed
    if unverified:
        payload["unverified"] = unverified
    return ok(payload)


TOOL_DESCRIPTION = (
"Extrude an open profile into a sheet body; model_extrude makes a capped solid."
)

tool = (
    Tool.create_simple(name="surface_extrude", description=TOOL_DESCRIPTION)
    .add_input_property("sketch_name", {"type": "string",
            "description": "Omit for the most recent sketch."})
    .add_input_property("curves", _CURVES.schema())
    .add_input_property("distance", {"type": "number", "description": "Depth in 'units' (negative reverses)."})
    .add_input_property(*_inputs.UNITS.as_property())
    .add_input_property("symmetric", {"type": "boolean"})
    .add_input_property(*_inputs.boolean_op(options=("new", "join"), default="new").as_property())
    .add_input_property(*_sketch_detail.COMPONENT_SCOPE)
    .strict_schema()
)
item = Item.create_tool_item(tool=tool, write="write", handler=handler,
                             run_on_main_thread=True,
                             postconditions=[_assert.FeatureHealthy(), _assert.SurfaceAreaAdded()],
                             verification=Verification(
                                 kind="inline", rung="exists",
                                 evidence_test="tests/unit/test_surface_extrude.py"
                                               "::TestEmptyResultSetIsAnError"
                                               "::test_extrude_with_no_result_body_is_an_error"))


def register_tool():
    register(item)
