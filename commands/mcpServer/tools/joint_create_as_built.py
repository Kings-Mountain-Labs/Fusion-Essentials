# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Joint two occurrences WHERE THEY ALREADY ARE - an as-built joint moves neither part. WRITES."""

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import error, ok, safe
from . import _common
from . import _inputs
from . import _assert
from ._joints import (AXES as _AXES, DRIVES_ANY as _DRIVES_ANY,
                      PAIR_USED_ANCHOR_NOTE as _PAIR_USED_ANCHOR_NOTE, apply_motion as _apply_motion,
                      current_joint_type as _current_joint_type, is_joint_origin as _is_joint_origin,
                      pair_used_clause as _pair_used_clause,
                      pending_move_guard as _pending_move_guard)
from ._joint_inputs import _JOINT_TYPES, _MOTIONS, _resolve_input, _slide_index, _slide_name

# What 'axis' actually does per motion type - the role the setter gives that argument, so the result
# note states the DOF that was set rather than a generic "motion axis". Keyed by the types
# _JOINT_TYPES marks as needing an axis; ball is deliberately absent (see _BALL_AXIS_NOTE).
_AXIS_ROLE = {
"revolute": "rotating about",
"slider": "sliding along",
"cylindrical": "rotating about and sliding along",
"planar": "sliding in the plane normal to",
"pin_slot": "rotating about",
}

# setAsBallJointMotion takes no selectable axis: pitch MUST be Z and yaw MUST be X (live-verified on
# an as-built input - the API rejects any other pair), so 'axis' is ignored for ball and the note must
# not claim one.
_BALL_AXIS_NOTE = "ball motion (pitch Z / yaw X - the API accepts no other pair)"

# joint_drive drives a single-value DOF and REFUSES every other motion, on the SAME shared set this
# pointer splits on (_joints.DRIVES_ANY), so the two can never disagree about which result is posable.
_POSE_HINT_OTHER = ("joint_drive does not drive this motion type (only revolute/slider/cylindrical "
                    "take a value) - pose the part with assembly_move.")

def handler(occurrence_one: str = "", occurrence_two: str = "", geometry: str = "",
            joint_type: str = "rigid", axis: str = "z",
            slide_axis: str = "", name: str = "") -> dict:
    """See TOOL_DESCRIPTION."""
    jtype = (joint_type or "rigid").strip().lower()
    if jtype not in _JOINT_TYPES:
        return error(f"Unknown joint_type '{joint_type}'. Valid: {', '.join(_JOINT_TYPES)}.")
    ax_name = (axis or "z").strip().lower()
    if ax_name not in _AXES:
        return error(f"Unknown axis '{axis}'. Valid: x, y, z.")
    slide_idx = None
    if jtype == "pin_slot":
        slide_idx, slide_err = _slide_index(slide_axis, ax_name)
        if slide_err:
            return error(slide_err)

    design = _common.design()
    if not design:
        return error("No active design with components.")

    pending = _pending_move_guard(design)
    if pending:
        return pending

    # The shared OccurrenceRef logic: an entityToken handle or a fullPathName/name, REFUSING an
    # ambiguous one instead of grabbing the first instance.
    o1, e1 = _inputs._resolve_occurrence(occurrence_one, occurrence_one)
    if not o1:
        return error(e1)
    o2, e2 = _inputs._resolve_occurrence(occurrence_two, occurrence_two)
    if not o2:
        return error(e2)
    # Distinctness by fullPathName, not .name: two DISTINCT instances of one component share a
    # .name. Two siblings CAN wear one path (Fusion enforces no name uniqueness), so this can still
    # refuse a legitimate pair - it errors rather than jointing the wrong instance.
    id1 = safe(lambda: o1.fullPathName) or safe(lambda: o1.name)
    id2 = safe(lambda: o2.fullPathName) or safe(lambda: o2.name)
    if (id1 is not None and id1 == id2) or o1 is o2:
        return error("As-built joint needs two distinct occurrences.")

    spec = (geometry or "").strip()
    # Live-verified: asBuiltJoints.add() RAISES "Geometry should not be null if joint motion is not
    # rigid" when createInput was handed None - a null geometry is rigid-ONLY, so every other motion
    # type must be given an anchor here rather than discovering the raise at add().
    if jtype != "rigid" and not spec:
        return error(f"joint_type '{jtype}' needs 'geometry' - the anchor its motion runs on (a "
                     "find_geometry handle, or '<occurrence>:<snap>' with snap = origin/center/top/"
                     "bottom/left/right/front/back/cylinder). Fusion refuses a non-rigid as-built "
                     "joint with no geometry; only 'rigid' is creatable without one.")
    if jtype == "rigid" and spec:
        return error("joint_type 'rigid' takes no 'geometry' - a rigid as-built joint locks the two "
                     f"occurrences with no anchor to move along, so '{spec}' would be ignored. Drop "
                     "'geometry', or set joint_type to the motion you want at that geometry.")

    geom, geom_label = None, None
    if spec:
        anchor, geom_label, gerr = _resolve_input(design, spec)
        if anchor is None:
            return error(f"'geometry': {gerr or f'could not resolve {spec}.'}")
        if _is_joint_origin(anchor):
            return error(f"'geometry' resolved to the Joint Origin '{spec}'. An as-built joint "
                         "anchors on a JointGeometry - real geometry (a face/edge/vertex handle, or "
                         "an '<occurrence>:<snap>'). To joint AT a Joint Origin use joint_create.")
        geom = anchor

    try:
        abj_input = design.rootComponent.asBuiltJoints.createInput(o1, o2, geom)
    except Exception as e:
        return error(f"As-built joint input failed: {e}")
    if not abj_input:
        return error("asBuiltJoints.createInput returned nothing for these two occurrences.")

    if jtype != "rigid":
        # An AsBuiltJointInput takes the JointInput setter arity - the axis enum alone, with no
        # geometry argument - and is NOT an AsBuiltJoint (both live-verified), so apply_motion's
        # existing-as-built branch and its extra geometry argument do not claim it.
        did, merr = _apply_motion(abj_input, jtype, _AXES[ax_name], slide_axis_idx=slide_idx)
        if not did:
            return error(f"Could not set {jtype} motion on the as-built joint input: "
                         f"{merr or 'setter returned false'}.")

    try:
        joint = design.rootComponent.asBuiltJoints.add(abj_input)
    except Exception as e:
        return error(f"As-built joint failed: {e}."
                     + _pair_used_clause(str(e), id1, id2, _PAIR_USED_ANCHOR_NOTE))
    if not joint:
        return error("As-built joint creation returned nothing.")

    # A motion that silently comes back rigid is a wrong result with a healthy feature, so read the
    # motion class back. On the rigid path the null geometry IS the proof - any other motion raises
    # at add() with a null geometry - so only a requested MOTION must confirm itself.
    got = _current_joint_type(joint)
    if got and got != jtype:
        return error(f"The as-built joint was created as '{got}', not the requested '{jtype}'. It "
                     "remains in the design - remove it with design_delete_feature and retry.")
    if jtype != "rigid" and not got:
        return error(f"The as-built joint was created but its motion could not be read back, so "
                     f"'{jtype}' is unconfirmed. Check it with assembly_get before relying on the "
                     "degree of freedom.")

    # AsBuiltJoints.createInput/add take no name, so the name is applied AFTER the joint exists and
    # read back. set_verified is not used: its message tail describes a pre-add input object, and
    # this is a POST-creation rename with the joint already in the design.
    want_name = (name or "").strip()
    if want_name:
        try:
            joint.name = want_name
        except Exception as e:
            return error(f"The as-built joint WAS created (Fusion named it "
                         f"'{safe(lambda: joint.name)}') but renaming it to '{want_name}' raised: "
                         f"{e}. Rename it in the browser, or remove it with design_delete_feature "
                         "and retry with a different name.")
        landed_name = safe(lambda: joint.name)
        if landed_name != want_name:
            return error(f"The as-built joint WAS created but renaming it to '{want_name}' did not "
                         f"take - AsBuiltJoint.name still reads '{landed_name}'. Rename it in the "
                         "browser, or remove it with design_delete_feature and retry with a "
                         "different name.")

    # Publish the fullPathName (id1/id2 above), not the leaf .name: a nested child reads 'Inner:1'
    # while the caller addressed it as 'Outer:1+Inner:1', and only the full path names it uniquely.
    out = {"created": True, "joint": safe(lambda: joint.name),
           "occurrence_one": id1, "occurrence_two": id2,
           "joint_type": got or jtype,
           "type": f"{got or jtype} (as-built)",
           "axis": ax_name if _JOINT_TYPES[jtype][1] else None,
           "slide_axis": _slide_name(slide_idx, ax_name) if jtype == "pin_slot" else None,
           "geometry": geom_label}
    if jtype == "rigid":
        out["note"] = "Occurrences rigidly joined where they already are."
        return ok(out)

    if _JOINT_TYPES[jtype][1]:
        moved = f"{jtype} motion {_AXIS_ROLE.get(jtype, 'on')} the frame {ax_name} axis"
        if jtype == "pin_slot":
            moved += f" and sliding along the frame {out['slide_axis']} axis"
    else:
        moved = _BALL_AXIS_NOTE if jtype == "ball" else f"{jtype} motion"
    pose_hint = "Pose it with joint_drive." if jtype in _DRIVES_ANY else _POSE_HINT_OTHER
    out["note"] = (f"Occurrences joined where they already are with {moved} - an as-built joint "
                   f"moves neither part. {pose_hint}")
    # AsBuiltJoint.geometry reads null when (and only when) the motion is rigid (live-verified across
    # rigid plus the five motion types), so a non-rigid joint with no geometry to read is a signal worth reporting
    # rather than swallowing.
    if safe(lambda: joint.geometry) is None:
        out["anchor_warning"] = (f"The joint reports '{got}' motion but no anchor geometry reads back "
                                 f"off it - check the pose before relying on the degree of freedom. "
                                 f"{pose_hint}")
    return ok(out)


TOOL_DESCRIPTION = (
    "Joint two occurrences WHERE THEY ALREADY ARE - neither part moves; joint_create moves the "
    "free part instead. An as-built joint exposes NO offset/angle ModelParameter, so use "
    "joint_create when it must be parametric."
)
tool = (
    Tool.create_simple(name="joint_create_as_built", description=TOOL_DESCRIPTION)
    .add_input_property("occurrence_one", {"type": "string"})
    .add_input_property("occurrence_two", {"type": "string"})
    .add_input_property("geometry", {"type": "string",
            "description": "A find_geometry handle, or '<occurrence>:<snap>'."})
    .add_input_property(*_inputs.joint_motion(default="rigid", options=_MOTIONS,
            description="").as_property())
    .add_input_property(*_inputs.frame_axis("axis", default="z",
            description="For pin_slot, the rotation axis.").as_property())
    .add_input_property(*_inputs.frame_axis("slide_axis", default="",
            description="pin_slot only.").as_property())
    .add_input_property("name", {"type": "string"})
    .strict_schema()
)
item = Item.create_tool_item(
    tool=tool, write="write", handler=handler, run_on_main_thread=True,
    postconditions=[_assert.FeatureHealthy()],
    # Beside the kernel's health gate: the handler reads the created joint's motion class back and
    # refuses one that came back as a different type, and reads an applied name back the same way.
    verification=Verification(
        kind="inline", rung="value",
        evidence_test="tests/unit/test_joint_create_as_built.py::TestAsBuiltMotion"
                      "::test_motion_readback_mismatch_is_an_error"))


def register_tool():
    register(item)
