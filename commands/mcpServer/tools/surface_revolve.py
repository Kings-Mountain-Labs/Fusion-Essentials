# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""MCP building block: revolve an OPEN profile about an x/y/z axis into a sheet (surface) body -
isSolid == False. WRITES; the result body's isSolid is read back, never assumed.
"""

import math

import adsk.core
import adsk.fusion

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import error, ok, safe, target_component
from . import _common
from . import _inputs
from . import _sketch_detail
from . import _assert
from ._surface_common import (_CURVES, _SURFACE_OPS, _body_names_and_solid, _curve_host_component,
                              _open_profile_from_curves)

app = adsk.core.Application.get()


def handler(sketch_name: str = "", curves=None, axis: str = "z",
            angle_deg: float = 360.0, symmetric: bool = False, operation: str = "new",
            component: str = "") -> dict:
    """Revolve an OPEN profile about an axis into a sheet (surface) body - isSolid == False."""
    try:
        ang = float(angle_deg)
    except Exception:
        return error("angle_deg must be a number (degrees).")
    if ang == 0:
        return error("Provide a non-zero 'angle_deg' to revolve (e.g. 360 for a full revolve).")
    op_key = (operation or "new").strip().lower()
    if op_key not in _SURFACE_OPS:
        return error(f"Unknown operation '{operation}'. Surface revolve supports: new, join.")
    a = (axis or "z").strip().lower()
    if a not in _inputs.WORLD_AXIS_ATTRS:
        return error(f"Unknown axis '{axis}'. Use x, y, or z.")

    design = _common.design()
    if not design:
        return error("No active design. Create or open a document first (see doc_new).")
    comp = target_component(design)

    # Build the profile, take the axis and create the feature on the SOURCE's owning component: a
    # profile-consuming feature on the active component raises bSet when the source is owned
    # elsewhere, and a revolve input mixes contexts if the axis is a different component's.
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
            return error("No sketch or 'curves' to revolve. Draw an OPEN chain first, or pass curves.")
        host = safe(lambda: sketch.parentComponent) or comp
        profile, perr = _common.open_profile_from_sketch(host, sketch, "from the sketch")
        source = safe(lambda: sketch.name)
    if perr:
        return error(perr)

    axis_entity = _inputs.world_construction_axis(host, a)
    if not axis_entity:
        return error(f"Could not resolve the {a}-axis of the active component.")

    op = getattr(adsk.fusion.FeatureOperations, _common.OPERATIONS[op_key])
    try:
        rev_input = host.features.revolveFeatures.createInput(profile, axis_entity, op)
        rev_input.isSolid = False
        angle_val = adsk.core.ValueInput.createByReal(math.radians(ang))
        if not rev_input.setAngleExtent(bool(symmetric), angle_val):
            return error(f"Fusion refused a {'symmetric ' if symmetric else ''}revolve extent of "
                         f"{angle_deg} deg, so no surface was revolved.")
        feature = host.features.revolveFeatures.add(rev_input)
    except Exception as e:
        return error(f"Surface revolve failed: {e}. (The profile must be coplanar with the axis.)")
    if not feature:
        return error(_common.no_feature_error(design, "Surface revolve"))

    names, any_solid = _body_names_and_solid(feature)
    if not names:
        return error("Surface revolve reported success but the feature owns no result body - no "
                     "sheet was created. " + _common.failed_effect_remedy(design, feature))

    if any_solid is False:
        note = "Open surface body created (isSolid=false)."
    elif any_solid is True:
        note = ("The result reads back SOLID (isSolid=true) - the profile closed into a solid, not "
                "a sheet.")
    else:
        note = ("Surface body created, but no result body's isSolid flag could be read back - "
                "whether it is an open sheet is UNVERIFIED.")
    payload = {
        "created": True,
        "feature": safe(lambda: feature.name),
        "operation": op_key,
        "source": source,
        "axis": f"{a}-axis",
        "angle_deg": round(ang, 6),
        "result_bodies": names,
        "is_solid": any_solid,       # read back from the body, not assumed (expected False for a shell)
        "symmetric": bool(symmetric),
        "note": note,
    }
    if any_solid is None:
        payload["unverified"] = ["is_solid"]
    return ok(payload)


TOOL_DESCRIPTION = (
"Revolve an open profile into a sheet body; model_revolve makes a solid."
)

tool = (
    Tool.create_simple(name="surface_revolve", description=TOOL_DESCRIPTION)
    .add_input_property("sketch_name", {"type": "string",
            "description": "Omit for the most recent sketch."})
    .add_input_property("curves", _CURVES.schema())
    .add_input_property(*_inputs.frame_axis("axis", default="z", description="Component origin axis.").as_property())
    .add_input_property("angle_deg", {"type": "number"})
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
                                 evidence_test="tests/unit/test_surface_revolve.py"
                                               "::TestEmptyResultSetIsAnError"
                                               "::test_revolve_with_no_result_body_is_an_error"))


def register_tool():
    register(item)
