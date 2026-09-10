# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""MCP building block: push/pull one or more faces of a solid by a signed distance (the Offset Face
direct edit).

  model_offset_face -> nudge a face along its normal without redrawing the sketch that created it (a
                        very common "make this wall 2mm thicker" edit). WRITES.
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


RETURNS = [
    _outputs.ReturnsName("feature", of="feature", consumers=["design_delete_feature"],
                         absent_when="no_timeline_feature"),
]

_FACES = _inputs.GeometryHandleList("faces", require="face", required=True)
_DISTANCE = _inputs.Distance("distance", allow_zero=False, required=True)

app = adsk.core.Application.get()


def handler(faces=None, distance: float = 0.0, units: str = "mm") -> dict:
    """See TOOL_DESCRIPTION."""
    scale_factor, uerr = _inputs.UNITS.resolve(units)
    if uerr:
        return error(uerr)
    dist_cm, derr = _DISTANCE.resolve_scaled(distance, scale_factor)
    if derr:
        return error(derr)

    design = _common.design()
    if not design:
        return error("No active design. Create or open a document first (see doc_new).")
    comp = target_component(design)

    face_ents, ferr = _FACES.resolve(faces)
    if ferr:
        return error(ferr)

    bodies = _geom.owning_bodies(face_ents)
    if not bodies:
        return error("'faces' resolved to face(s) with no readable owning body - cannot offset.")
    # Read before the mutation: a post-mutation proxy can stop answering .name, and the offset's
    # only evidence is the volume delta over these bodies.
    vol_before = _geom.volumes(bodies)
    body_names = [safe(lambda b=b: b.name) for b in bodies]

    # createInput wants a Python list of BRepFace (a SWIG vector): an ObjectCollection raises a
    # vector-type argument error.
    face_list = list(face_ents)
    dist_val = adsk.core.ValueInput.createByReal(dist_cm)

    try:
        off_input = comp.features.offsetFacesFeatures.createInput(face_list, dist_val)
        feature = comp.features.offsetFacesFeatures.add(off_input)
    except Exception as e:
        return error(f"Offset face failed: {e}. (The distance may be too large for the geometry, or "
                     "the faces may not support a uniform offset together - try a smaller distance or "
                     "fewer faces.)")
    # offsetFacesFeatures.add returns None in a DIRECT design while the offset lands; the volume
    # delta below needs no feature object. In parametric a None feature stays an error.
    direct_no_feature = _common.direct_feature_absence(design, feature)
    if not feature and not direct_no_feature:
        return error(_common.no_feature_error(design, "Offset face"))

    # A feature can be ADDED yet fail to compute.
    if safe(lambda: feature.healthState) == adsk.fusion.FeatureHealthStates.ErrorFeatureHealthState:
        msg = safe(lambda: feature.errorOrWarningMessage) or "no detail"
        return error(f"Offset face was created but failed to compute: {msg}. Try a smaller distance "
                     "or a different face selection. "
                     + _common.failed_effect_remedy(design, feature))

    delta_total, any_readable = _geom.volume_delta(bodies, vol_before)
    if direct_no_feature and not any_readable:
        return error("Offset face ran in a DIRECT design, which returns no feature object, and no "
                     "affected body's volume could be read back - so whether the faces moved is "
                     "UNVERIFIED. Re-read the body with model_inspect.")
    if any_readable and abs(delta_total) < _common.NO_VOLUME_CHANGE_CM3:
        return error("Offset face reported success but the affected body's volume is unchanged - "
                     "nothing was actually pushed or pulled. "
                     + _common.failed_effect_remedy(design, feature))

    payload = {
        "offset": True,
        "faces_requested": len(face_ents),
        "bodies": body_names,
        "distance": round(float(distance), 6),
        "units": units,
        "note": "Face(s) pushed/pulled along their normal. Positive extends outward (adds "
                "material); negative pushes inward (removes material).",
    }
    if direct_no_feature:
        payload["no_timeline_feature"] = True
        payload["note"] += " " + _common.DIRECT_FEATURE_NOTE
    else:
        payload["feature"] = safe(lambda: feature.name)
    if any_readable:
        payload["volume_delta_cm3"] = round(delta_total, 6)
    return ok(payload)


TOOL_DESCRIPTION = (
    "Push faces along the normal; positive adds material.\n"
    + _outputs.produces_block(RETURNS)
)

offset_face_tool = (
    Tool.create_simple(name="model_offset_face", description=TOOL_DESCRIPTION)
    .add_input_property(_FACES.name, _FACES.schema())
    .add_input_property(_DISTANCE.name, _DISTANCE.schema())
    .add_input_property(*_inputs.UNITS.as_property())
    .strict_schema()
)
offset_face_item = Item.create_tool_item(tool=offset_face_tool, write="write", handler=handler,
                                         run_on_main_thread=True,
                                         postconditions=[_assert.FeatureHealthy()],
                                         verification=Verification(
                                             kind="inline", rung="geometry",
                                             evidence_test="tests/unit/test_model_offset_face.py"
                                             "::TestHonesty"
                                             "::test_unchanged_volume_reports_error_not_ok"))


def register_tool():
    register(offset_face_item)
