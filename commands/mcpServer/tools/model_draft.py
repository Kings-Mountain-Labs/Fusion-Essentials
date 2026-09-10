# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""MCP building block: taper faces to a pull direction (the Draft feature).

  model_draft -> add draft (taper) to faces so a molded/cast part releases from its tooling. The
                 pull direction is a planar face or construction plane. WRITES.
                 The angle ValueInput is in RADIANS - Fusion's internal angle unit.
"""

import math

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
    _outputs.ReturnsValue("faces_drafted", "how many faces the draft actually applied to (read from the feature)"),
]

# faces to taper (any BRep face); the pull direction is a PlaneRef - exactly the planar-face/plane
# the DraftFeatureInput.plane accepts. AxisRef is the WRONG kind here: a draft's direction is a plane
# normal, not an edge/world axis.
_FACES = _inputs.GeometryHandleList("faces", require="face", required=True)
_PULL = _inputs.PlaneRef("pull_direction", required=True)

app = adsk.core.Application.get()


def handler(faces=None, pull_direction: str = "", angle_deg: float = 0.0,
            symmetric: bool = False, tangent_chain: bool = True, flip: bool = False) -> dict:
    """See TOOL_DESCRIPTION."""
    try:
        angle = float(angle_deg)
    except Exception:
        return error("'angle_deg' must be a number (draft angle in degrees).")
    if angle == 0:
        return error("'angle_deg' must be non-zero - a 0 deg draft tapers nothing.")
    if abs(angle) >= 90:
        return error(f"'angle_deg' must be between -90 and 90 degrees (got {angle}).")

    design = _common.design()
    if not design:
        return error("No active design. Create or open a document first (see doc_new).")
    comp = target_component(design)

    face_ents, ferr = _FACES.resolve(faces)
    if ferr:
        return error(ferr)
    plane, perr = _PULL.resolve(pull_direction)
    if perr:
        return error(perr)

    # DraftFeatures.createInput wants a Python list of BRepFace (per the live signature), not an
    # ObjectCollection.
    face_list = list(face_ents)
    # The bodies the taper must move, sampled BEFORE the add: a draft that computes cleanly while
    # tapering nothing is the silent no-op this gate catches, and a feature object cannot show it.
    draft_bodies = _geom.owning_bodies(face_list)
    vol_before = _geom.volumes(draft_bodies)
    angle_val = adsk.core.ValueInput.createByReal(math.radians(angle))
    try:
        di = comp.features.draftFeatures.createInput(face_list, plane, bool(tangent_chain))
        di.isDirectionFlipped = bool(flip)
        # A single angle for every drafted face; isSymmetric splits at the pull plane and tapers both
        # sides by that angle. This is the setSingleAngle path (not setTwoAngles, which is per-side).
        if not di.setSingleAngle(bool(symmetric), angle_val):
            return error(f"Fusion refused a {'symmetric ' if symmetric else ''}draft angle of "
                         f"{angle_deg} deg (setSingleAngle returned false), so nothing was drafted.")
        feature = comp.features.draftFeatures.add(di)
    except Exception as e:
        return error(f"Draft failed: {e}. (The pull direction may not suit these faces, or the angle "
                     "undercuts the geometry - try a smaller angle or 'flip'.)")
    if not feature:
        return error(_common.no_feature_error(design, "Draft"))

    # A feature can be ADDED yet fail to compute; report that as failure, not a false ok.
    if safe(lambda: feature.healthState) == adsk.fusion.FeatureHealthStates.ErrorFeatureHealthState:
        msg = safe(lambda: feature.errorOrWarningMessage) or "no detail"
        return error(f"Draft feature was created but failed to compute: {msg}. Try a smaller angle, "
                     "'flip', or a different pull direction. "
                     + _common.failed_effect_remedy(design, feature))

    # MATERIAL evidence: a one-sided taper always cuts or adds a wedge, so an unmoved volume means
    # the draft tapered nothing. A SYMMETRIC draft tapers both sides in OPPOSITE directions, where
    # the wedges can cancel on a real taper - so its delta is published but carries no verdict.
    volume_delta_cm3 = None
    if draft_bodies:
        delta, readable = _geom.volume_delta(draft_bodies, vol_before)
        if readable:
            volume_delta_cm3 = round(delta, 6)
        if readable and not symmetric and abs(delta) < _common.NO_VOLUME_CHANGE_CM3:
            named = ", ".join(str(safe(lambda b=b: b.name)) for b in draft_bodies)
            return error(f"Draft computed but tapered nothing - {named} measures the volume it had "
                         f"before, so the {angle} deg taper moved no material. Check 'pull_direction' "
                         "is the plane the faces taper relative to, and try 'flip' or a face that is "
                         "not already parallel to it. " + _common.failed_effect_remedy(design, feature))

    requested = len(face_list)
    # The count the FEATURE reports, never the request echoed back. NOT `inputFaces`: that property
    # raises RuntimeError "Didn't roll editing feature back" here, while `faces` reads on the newest
    # feature and on an earlier one alike.
    drafted = _common.counted(lambda: feature.faces.count)
    note = "Faces tapered to the pull direction. Pair with view_screenshot to view."
    if drafted is None:
        note += (f" 'faces_drafted' is null - the count could not be read off the feature, so how "
                 f"many faces the draft took is UNKNOWN here; {requested} face(s) were requested.")
    payload = {
        "drafted": True,
        "feature": safe(lambda: feature.name),
        "faces_requested": requested,
        "faces_drafted": drafted,
        "angle_deg": round(angle, 6),
        "symmetric": bool(symmetric),
        "tangent_chain": bool(tangent_chain),
        "flipped": bool(flip),
        "pull_direction": pull_direction,
        "note": note,
    }
    # Published only where the before/after pair was READABLE: a null here would read as "no material
    # moved" rather than "the measurement could not be taken", so the key is simply absent instead.
    if volume_delta_cm3 is not None:
        payload["volume_delta_cm3"] = volume_delta_cm3
    return ok(payload)


TOOL_DESCRIPTION = (
    "Taper (draft) faces relative to a pull plane."
)

FULL_DESCRIPTION = TOOL_DESCRIPTION + "\n" + _outputs.produces_block(RETURNS)

draft_tool = (
    Tool.create_simple(name="model_draft", description=FULL_DESCRIPTION)
    .add_input_property(_FACES.name, _FACES.schema())
    .add_input_property(_PULL.name, _PULL.schema())
    .add_input_property("angle_deg", {"type": "number"})
    .add_input_property("symmetric", {"type": "boolean"})
    .add_input_property("tangent_chain", {"type": "boolean"})
    .add_input_property("flip", {"type": "boolean"})
    .strict_schema()
)
draft_item = Item.create_tool_item(tool=draft_tool, write="write", handler=handler, run_on_main_thread=True,
                                   postconditions=[_assert.FeatureHealthy()],
                                   verification=Verification(
                                       kind="inline", rung="geometry",
                                       evidence_test="tests/unit/test_model_draft.py"
                                       "::TestTaperMovesMaterial"
                                       "::test_draft_that_moves_no_volume_is_an_error"))


def register_tool():
    register(draft_item)
