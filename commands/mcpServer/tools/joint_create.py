# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Create a Joint between two joint inputs with a chosen motion type (rigid/revolute/slider/
cylindrical/planar/ball/pin-slot) and optional offset/angle/flip. Each input resolves by JOINT-ORIGIN
NAME, a find_geometry handle, or an autonomous '<occurrence>:<snap>' geometry snap. WRITES."""

import math

import adsk.core

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item
from ..mcp_primitives.registry import register
from ._common import apply_rename, error, ok, safe
from . import _common
from . import _inputs
from . import _assert
from ._joints import (
    AXES as _AXES,
    OFFSET_PARAM_NOTE as _OFFSET_PARAM_NOTE,
    apply_motion as _apply_motion,
    motion_param_names as _motion_param_names,
    pending_move_guard as _pending_move_guard,
)
from . import _joints
from ._joint_inputs import (
    _JOINT_TYPES, _MOTIONS, _REST_LIMIT_NOTE, _apply_limits, _resolve_input, _slide_index,
    _slide_name, _unverified_limits_note,
)


def handler(occurrence_one: str = "", occurrence_two: str = "", joint_type: str = "rigid",
            axis: str = "z", slide_axis: str = "", offset: float = 0.0, angle: float = 0.0,
            units: str = "mm", flip: bool = False, name: str = "", min_deg=None, max_deg=None,
            rest_deg=None, min_mm=None, max_mm=None, rest_mm=None) -> dict:
    """See TOOL_DESCRIPTION."""
    design = _common.design()
    if not design:
        return error("No active design (open a document with assembly geometry).")

    pending = _pending_move_guard(design)
    if pending:
        return pending

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

    scale = _common.scale(units)
    if scale is None:
        return error(f"Unknown units '{units}'. Valid: mm, cm, in.")

    n1, n2 = (occurrence_one or "").strip(), (occurrence_two or "").strip()
    if not n1 or not n2:
        return error("Provide 'occurrence_one' and 'occurrence_two' - each a Joint Origin name OR "
    "an autonomous geometry snap '<occurrence>:<snap>' "
    "(snap = origin/center/top/bottom/left/right/front/back/cylinder).")

    jo1, label1, err1 = _resolve_input(design, n1)
    jo2, label2, err2 = _resolve_input(design, n2)
    if not jo1:
        return error(err1 or f"Could not resolve joint input '{n1}'.")
    if not jo2:
        return error(err2 or f"Could not resolve joint input '{n2}'.")

    # Flush face-to-face detection, sampled PRE-ADD off the resolved JointGeometry entities (the
    # add moves the free part) - the same shared hint joint_at_geometry publishes, so a snap-based
    # create that seats two opposing planar faces gets the same 180-deg warning.
    opposing = _joints.normals_oppose(
        _joints.planar_outward_normal(safe(lambda: jo1.entityOne)),
        _joints.planar_outward_normal(safe(lambda: jo2.entityOne)))

    # Joints live on the root component (a joint between two components is owned there).
    joints = design.rootComponent.joints
    try:
        ji = joints.createInput(jo1, jo2)
    except Exception as e:
        return error(f"Could not create joint input: {e}")
    if not ji:
        return error("createInput returned nothing for these inputs.")

    did, err = _apply_motion(ji, jtype, _AXES[ax_name], slide_axis_idx=slide_idx)
    if not did:
        return error(f"Could not set {jtype} motion: {err or 'setter returned false'}.")

    # Optional offset / angle / flip.
    try:
        if offset:
            ji.offset = adsk.core.ValueInput.createByReal(offset * scale)
        if angle:
            ji.angle = adsk.core.ValueInput.createByReal(math.radians(angle))
        if flip:
            ji.isFlipped = True
    except Exception as e:
        return error(f"Could not apply offset/angle/flip: {e}")

    try:
        joint = joints.add(ji)
    except Exception as e:
        msg = f"Joint creation failed: {e}"
        if "input paths" in str(e).lower():
            # Fusion's "Provided input paths for joint are not valid": an input is not in assembly
            # context (typical when scripting a joint to a native sub-component JO). This tool's
            # by-name resolution proxies JOs for exactly that reason - so steer to it.
            msg += (" - an input is likely not in assembly context. Pass Joint Origins by NAME "
    "(bare or '<occurrence>:<JO name>') so this tool proxies them into the assembly; "
    "for geometry, use a fresh find_geometry handle.")
        pair = _joints.pair_used_clause(str(e), n1, n2, _joints.PAIR_USED_ANCHOR_NOTE)
        if pair:
            # The platform text ends mid-sentence, so the clause is terminated onto it the way the
            # as-built sibling terminates its own.
            msg = msg.rstrip(" .") + "." + pair
        return error(msg)
    if not joint:
        return error("joints.add returned nothing.")

    joint_name_final, rename_warning = apply_rename(joint, name)

    # Optional limits (rotation and/or linear) - applied after the joint exists so its jointMotion
    # is established. Same routing as joint_edit.
    limits_out, limits_unverified = {}, []
    if any(v is not None for v in (min_deg, max_deg, rest_deg, min_mm, max_mm, rest_mm)):
        jm = safe(lambda: joint.jointMotion)
        if jm is None:
            return error("Limits requested but this joint type has no motion to limit "
            "(rigid/inferred). Use revolute/slider/cylindrical.")
        lim_changed, limits_unverified, lim_err = _apply_limits(
            jm, min_deg=min_deg, max_deg=max_deg, rest_deg=rest_deg,
            min_mm=min_mm, max_mm=max_mm, rest_mm=rest_mm, cm_scale=scale)
        if lim_err:
            # PARTIAL SUCCESS disclosed: the joint EXISTS in the timeline and any limits applied
            # before the failing one HAVE been written, so a bare error invites a duplicate create.
            applied_txt = (", ".join(f"{k}={v}" for k, v in lim_changed.items())
                           if lim_changed else "none")
            return error(
                f"Joint '{joint_name_final}' WAS CREATED, but a limit failed: {lim_err} "
                f"Limits already applied before the failure: {applied_txt}. Fix the limits with "
                f"joint_edit(joint_name='{joint_name_final}', ...) or remove the joint with "
                "design_delete_feature - do NOT re-create it.")
        limits_out = lim_changed

    # A joint can be ADDED yet fail to COMPUTE: joints.add() hands back a truthy Joint while Fusion
    # marks it 'Compute Failed'. So the state is read back here through _assert.compute_state, and
    # a create that did not solve is a refusal rather than a plain success.
    state, failure = _assert.compute_state(joint)
    if failure:
        state_label, detail = failure
        return error(
            f"Joint '{joint_name_final}' WAS CREATED but FAILED to compute (health state: "
            f"{state_label})" + (f": {detail}" if detail else " (it reports no message)")
            + f" - it does not position the parts. It REMAINS in the timeline: remove it with "
              f"design_delete_feature(name='{joint_name_final}'), or fix its inputs with "
              "joint_edit. A part locked by assembly_ground(ground_to_parent=true), itself or an "
              "ancestor, conflicts with a joint that would move it - read the state back with "
              "assembly_get.")

    # No failure found - but that verdict rests on a state that must actually have been READ. When
    # neither the joint nor its timeline item answers healthState, 'healthy' is null (unknown), never
    # a coerced true: an unread state is not a clean bill of health.
    healthy = True if state == "healthy" else None

    payload = {
        "created": True,
        "healthy": healthy,
        "joint_name": joint_name_final,
        "joint_type": jtype,
        "input_one": label1,
        "input_two": label2,
        "axis": (ax_name if _JOINT_TYPES[jtype][1] else None),
        "slide_axis": (_slide_name(slide_idx, ax_name) if jtype == "pin_slot" else None),
        "offset": offset if offset else None,
        "angle_deg": angle if angle else None,
        "flipped": bool(flip),
        **limits_out,
        "note": "Joint created as a timeline feature. View it with view_screenshot.",
    }
    if rename_warning:
        payload["rename_warning"] = rename_warning
    if healthy is None:
        payload["note"] += (" 'healthy' is null - the joint's compute state could not be read off "
                            "either the joint or its timeline item, so whether it SOLVED is UNKNOWN "
                            "here (it is not a 'yes'). Check it with assembly_get (is_healthy, "
                            "broken_joints) before relying on the parts' positions.")
    # The shared flush face-to-face hint (see _joints.FLIP_HINT) - parity with joint_at_geometry:
    # a snap create that seats two opposing planar faces without flip lands the part rotated
    # 180 deg, and the payload says so instead of leaving a coincident-looking embed unexplained.
    if opposing and not flip:
        payload["flip_hint"] = _joints.FLIP_HINT
    # A limit whose read-back could not be taken is published NULL above (its key is in limits_out
    # with a None value, never the request echoed back) and named here, so a caller cannot mistake
    # the absence of a mismatch for a confirmed write.
    if limits_unverified:
        payload["limits_unverified"] = limits_unverified
        payload["note"] += _unverified_limits_note(limits_unverified)
    if any(k in limits_out for k in ("rest_mm", "rest_deg")):
        payload["note"] += _REST_LIMIT_NOTE
    mp = _motion_param_names(joint)
    if mp:
        payload["model_parameters"] = mp
        payload["note"] += _OFFSET_PARAM_NOTE
    return ok(payload)


TOOL_DESCRIPTION = (
    "Create a Joint between two inputs: the FREE part MOVES so the two inputs coincide - do not "
    "pre-place it."
)

tool = (
    Tool.create_with_string_input(
        name="joint_create",
        description=TOOL_DESCRIPTION,
        input_param_name="occurrence_one",
        input_param_description="A find_geometry handle, a Joint Origin name, or a snap "
                                "'<occurrence>:<snap>'.",
    )
    .add_input_property("occurrence_two", {"type": "string",
            "description": "Same forms as occurrence_one."})
    .add_input_property(*_inputs.joint_motion(default="rigid", options=_MOTIONS,
            description="").as_property())
    .add_input_property(*_inputs.frame_axis("axis", default="z",
            description="FRAME-relative, not world; joint_edit(world_axis=...) re-points one that "
                        "pivots wrong. For pin_slot, the rotation axis.").as_property())
    .add_input_property(*_inputs.frame_axis("slide_axis", default="",
            description="pin_slot only.").as_property())
    .add_input_property("offset", {"type": "number", "description": "In 'units'."})
    .add_input_property("angle", {"type": "number", "description": "In degrees."})
    .add_input_property(*_inputs.UNITS.as_property())
    .add_input_property("flip", {"type": "boolean"})
    .add_input_property("name", {"type": "string"})
    .add_input_property("min_deg", {"type": "number"})
    .add_input_property("max_deg", {"type": "number"})
    .add_input_property("rest_deg", {"type": "number"})
    .add_input_property("min_mm", {"type": "number", "description": "In 'units'."})
    .add_input_property("max_mm", {"type": "number", "description": "In 'units'."})
    .add_input_property("rest_mm", {"type": "number", "description": "In 'units'."})
    .strict_schema()
)

item = Item.create_tool_item(tool=tool, write="write", handler=handler, run_on_main_thread=True,
                             postconditions=[_assert.FeatureHealthy(), _assert.ChildGeometryMoved()])


def register_tool():
    register(item)
