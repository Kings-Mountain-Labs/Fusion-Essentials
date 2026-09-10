# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Edit an existing joint's DEFINITION in place - inputs, motion type, axis, flip, offset, angle and
limits - reading every value back off the joint. WRITES."""

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import error, ok, safe
from . import _common
from . import _inputs
from ._joints import (
    AXES as _AXES,
    OFFSET_PARAM_NOTE as _OFFSET_PARAM_NOTE,
    apply_motion as _apply_motion,
    current_joint_type as _current_joint_type,
    find_joint as _find_joint,
    is_as_built_joint as _is_as_built_joint,
    is_joint_origin as _is_joint_origin,
    motion_param_names as _motion_param_names,
)
from ._joint_inputs import (
    _DEG_PER_RAD, _JOINT_TYPES, _LIMIT_BAND, _MOTIONS, _REST_LIMIT_NOTE, _apply_limits,
    _resolve_input, _slide_index, _slide_name, _unverified_limits_note,
)


def _fmt_num(v):
    """Format a number for a parameter expression: drop a trailing '.0' (e.g. -200, not -200.0)."""
    f = float(v)
    return str(int(f)) if f == int(f) else str(f)


def _world_axis_entity(design, axis_idx):
    """The root component's world construction axis (x/y/z), for use as a CUSTOM joint direction."""
    # The XAxis/YAxis/ZAxisJointDirection enums are relative to the JOINT GEOMETRY's local frame,
    # not world: a snap whose local Z points along world Y pivots about world Y when asked for 'Z'.
    root = design.rootComponent
    return _inputs.world_construction_axis(root, "xyz"[axis_idx])


def _unverified_edits_note(keys):
    """The same sentence for the non-limit sets (flip / offset / angle) whose read-back could not be
    taken - published null in 'changes' rather than as the request echoed back."""
    return (f" Published null ({', '.join(keys)}) - each was assigned but could not be read back "
            "off the joint, so whether it TOOK is UNKNOWN here (it is not a 'yes').")


def _applied_so_far(changed):
    """The edits recorded before a failing one, for the partial-success disclosure a bare error
    would hide."""
    return ", ".join(f"{k}={v}" for k, v in changed.items()) if changed else "none"


def _set_flip(joint, wanted):
    """Set Joint.isFlipped, then read the flag BACK off the live joint. Returns (the flag as the
    JOINT reads it or None when the re-read could not be taken, error)."""
    before = _common.read_flag(lambda: joint.isFlipped)
    # NOT safe()-wrapped: this is the mutation the tool was ASKED to do, so a failure raises into
    # the handler's try/except rather than being swallowed into a false success.
    joint.isFlipped = bool(wanted)
    after = _common.read_flag(lambda: joint.isFlipped)
    if after is None:
        return None, None
    if after != bool(wanted):
        return None, (f"flip did not take - {bool(wanted)} was requested and the joint reads "
                      f"isFlipped back as {after} (it read {before} before the set).")
    return after, None


def _set_one_parameter(param, key, wanted, expression, unit_scale):
    """Assign ONE of the joint's own ModelParameters by EXPRESSION, then read its VALUE back.
    A Parameter's `value` reads in Fusion's DATABASE units, never the parameter's own, so
    `unit_scale` converts it into the caller's. Returns (read-back or None, error)."""
    before = _common.measured(lambda: param.value, scale=unit_scale, places=9)
    # NOT safe()-wrapped: the mutation the tool was ASKED to do (see _set_flip).
    param.expression = expression
    landed = _common.measured(lambda: param.value, scale=unit_scale, places=9)
    if landed is None:
        return None, None
    if abs(landed - float(wanted)) > _LIMIT_BAND:
        return None, (f"{key} did not take - {wanted} was requested and the joint's parameter reads "
                      f"back {landed} in the same units (it read {before} before the set).")
    return landed, None


def handler(joint_name: str = "", input_one: str = "", input_two: str = "",
            joint_type: str = "", axis: str = "", slide_axis: str = "", world_axis: str = "",
            flip=None, offset=None, angle=None, units: str = "mm",
            rotation_deg=None, min_deg=None, max_deg=None, rest_deg=None,
            min_mm=None, max_mm=None, rest_mm=None) -> dict:
    """See TOOL_DESCRIPTION."""
    design = _common.design()
    if not design:
        return error("No active design.")
    joint, ambiguous = _find_joint(design, joint_name)
    if ambiguous:
        return error(ambiguous)
    if not joint:
        return error(f"No joint named '{joint_name}'. Use design_get(include=['timeline']) or check the name.")

    # Posing a joint to a drive value is joint_drive's job (the Drive Joints command); joint_edit
    # changes the joint DEFINITION (type/axis/snaps/flip/limits), not its pose. Redirect.
    if rotation_deg is not None:
        return error("Posing a joint to a rotation value is joint_drive's job. Use "
    "joint_drive(joint_name=..., angle_deg=...) to drive it; joint_edit changes the joint "
    "definition (type/axis/snaps/limits), not its pose.")

    # world_axis (re-point the motion to a TRUE WORLD axis) forces a motion re-set even if the
    # joint_type isn't changing - that's the whole point (fixing a frame-relative axis).
    wa_name = (world_axis or "").strip().lower()
    if wa_name and wa_name not in _AXES:
        return error(f"Unknown world_axis '{world_axis}'. Valid: x, y, z.")

    # Validate units (used by offset).
    if (offset is not None) and (_common.scale(units) is None):
        return error(f"Unknown units '{units}'. Valid: mm, cm, in.")

    # Decide what's being changed; refuse a no-op so we never roll the timeline for nothing.
    want_inputs = bool((input_one or "").strip() or (input_two or "").strip())
    want_motion = bool((joint_type or "").strip()) or bool(wa_name)
    want_flip = flip is not None
    want_offset = offset is not None
    want_angle = angle is not None
    want_limits = any(v is not None for v in
                      (min_deg, max_deg, rest_deg, min_mm, max_mm, rest_mm))
    if not (want_inputs or want_motion or want_flip or want_offset or want_angle or want_limits):
        return error("Nothing to change. Provide at least one of: input_one/input_two, joint_type "
                      "(+axis), world_axis, flip, offset (+units), angle, "
                      "min_deg/max_deg/rest_deg (rotation), min_mm/max_mm/rest_mm (linear).")

    # Validate motion type up front (before touching the timeline). If only world_axis is given,
    # re-apply the joint's CURRENT motion type with the world axis.
    jtype = (joint_type or "").strip().lower()
    if (joint_type or "").strip() and jtype not in _JOINT_TYPES:
        return error(f"Unknown joint_type '{joint_type}'. Valid: {', '.join(_JOINT_TYPES)}.")
    if want_motion and not jtype:
        jtype = _current_joint_type(joint)
        if jtype not in _JOINT_TYPES:
            return error("world_axis given but the joint's current motion type is not "
    "axis-based (rigid/ball have no single axis to re-point).")
    ax_name = (axis or "z").strip().lower()
    if want_motion and not wa_name and _JOINT_TYPES[jtype][1] and ax_name not in _AXES:
        return error(f"Unknown axis '{axis}'. Valid: x, y, z.")

    # pin_slot slide direction (validated up front, before touching the timeline).
    slide_idx = None
    if jtype == "pin_slot":
        slide_idx, slide_err = _slide_index(slide_axis, ax_name if ax_name in _AXES else "z")
        if slide_err:
            return error(slide_err)

    # Resolve new snap inputs (before rolling, so a bad input fails cleanly).
    new1 = new2 = None
    label1 = label2 = None
    if (input_one or "").strip():
        new1, label1, err1 = _resolve_input(design, input_one.strip())
        if not new1:
            return error(err1 or f"Could not resolve input_one '{input_one}'.")
    if (input_two or "").strip():
        new2, label2, err2 = _resolve_input(design, input_two.strip())
        if not new2:
            return error(err2 or f"Could not resolve input_two '{input_two}'.")

    # A joint can only reference geometry/origins that exist BEFORE it in the timeline: an edit
    # rolls the marker to just before the joint, and a later feature raises a bare
    # 'InternalValidationError: findObjectPath'. Refuse here, before rolling the timeline.
    joint_tl = safe(lambda: joint.timelineObject.index)
    for lbl, newx in (("input_one", new1), ("input_two", new2)):
        if newx is not None and _is_joint_origin(newx) and joint_tl is not None:
            jo_tl = safe(lambda nx=newx: nx.timelineObject.index)
            if jo_tl is not None and jo_tl >= joint_tl:
                return error(
                    f"Cannot rewire '{joint_name}' {lbl} to that Joint Origin: the Joint Origin is "
                    f"LATER in the timeline (position {jo_tl}) than the joint (position {joint_tl}). "
                    "Editing a joint rolls the timeline to just before it, where a later feature does "
                    "not exist yet. Create the Joint Origin before the joint, or delete the joint and "
                    "recreate it after the Joint Origin with joint_create.")

    changed = {}
    limits_unverified = []
    edits_unverified = []
    rolled = False
    try:
        # The marker MUST be before the joint to edit geometry/flip/motion.
        safe(lambda: joint.timelineObject.rollTo(True))
        rolled = True

        if new1 is not None:
            joint.geometryOrOriginOne = new1
            changed["input_one"] = label1
        if new2 is not None:
            joint.geometryOrOriginTwo = new2
            changed["input_two"] = label2

        if want_motion:
            wa_entity = _world_axis_entity(design, _AXES[wa_name]) if wa_name else None
            did, err = _apply_motion(joint, jtype, _AXES.get(ax_name, 2), wa_entity,
                                     slide_axis_idx=slide_idx)
            if not did:
                return error(f"Could not set {jtype} motion: {err or 'setter returned false'}.")
            changed["joint_type"] = jtype
            if wa_name:
                changed["world_axis"] = wa_name
            elif _JOINT_TYPES[jtype][1]:
                changed["axis"] = ax_name
            if jtype == "pin_slot":
                changed["slide_axis"] = _slide_name(slide_idx, ax_name if ax_name in _AXES else "z")

        if want_flip:
            published, ferr = _set_flip(joint, flip)
            if ferr:
                return error(f"{ferr} Edits already applied before the failure: "
                             f"{_applied_so_far(changed)}.")
            changed["flipped"] = published
            if published is None:
                edits_unverified.append("flipped")

        # offset / angle are ModelParameters on the Joint - set via an explicit-units expression
        # (robust regardless of document units), matching the create-joint tool's behaviour.
        if want_offset:
            op = safe(lambda: joint.offset)
            if op is None:
                if _is_as_built_joint(joint):
                    # An AsBuiltJoint carries no offset/angle ModelParameter of any kind, whatever
                    # its motion - so no expression can position it and rest_mm only sets a motion
                    # -study equilibrium (see the note below). The parametric path is a real Joint.
                    return error(
                        f"'{joint_name}' is an AS-BUILT joint, which exposes no offset parameter "
                        "for ANY motion type - its position cannot be driven by a parameter or an "
                        "expression. Delete it (design_delete_feature) and build the pair with "
                        "joint_create instead: that joint's offset is a ModelParameter, moving "
                        "along the joint frame's Z axis.")
                return error("This joint has no offset parameter (rigid/inferred or already 0-DOF).")
            u = (units or "mm").strip().lower()
            u = "in" if u == "inch" else u
            # `u` passed the units guard above, so it is a key of the shared cm-to-unit table -
            # the factor that turns the parameter's internal cm back into the unit asked for.
            published, oerr = _set_one_parameter(op, "offset", float(offset),
                                                 f"{_fmt_num(offset)} {u}",
                                                 _common.CM_TO_UNIT[u])
            if oerr:
                return error(f"{oerr} Edits already applied before the failure: "
                             f"{_applied_so_far(changed)}.")
            changed["offset"] = published
            changed["units"] = u
            if published is None:
                edits_unverified.append("offset")

        if want_angle:
            ap = safe(lambda: joint.angle)
            if ap is None:
                if _is_as_built_joint(joint):
                    return error(
                        f"'{joint_name}' is an AS-BUILT joint, which exposes no offset/angle "
                        "ModelParameter for ANY motion type - no expression can drive it. Delete "
                        "it (design_delete_feature) and build the pair with joint_create instead.")
                return error("This joint has no angle parameter.")
            published, aerr = _set_one_parameter(ap, "angle", float(angle),
                                                 f"{_fmt_num(angle)} deg", _DEG_PER_RAD)
            if aerr:
                return error(f"{aerr} Edits already applied before the failure: "
                             f"{_applied_so_far(changed)}.")
            changed["angle"] = published
            if published is None:
                edits_unverified.append("angle")

        if want_limits:
            jm = safe(lambda: joint.jointMotion)
            if jm is None:
                return error("This joint has no editable motion (rigid/inferred has no limits).")
            lim_scale = _common.scale(units) or 0.1
            lim_changed, limits_unverified, lim_err = _apply_limits(
                jm, min_deg=min_deg, max_deg=max_deg, rest_deg=rest_deg,
                min_mm=min_mm, max_mm=max_mm, rest_mm=rest_mm, cm_scale=lim_scale)
            changed.update(lim_changed)
            if lim_err:
                # PARTIAL SUCCESS disclosed: every edit recorded in `changed` so far HAS landed
                # (earlier fields and any limit applied before the failing one) - a bare error
                # would hide the writes that took.
                return error(f"{lim_err} Edits already applied before the failure: "
                             f"{_applied_so_far(changed)}.")
    except Exception as e:
        msg = f"Edit failed: {e}"
        if "findObjectPath" in str(e) or "InternalValidationError" in str(e):
            # A referenced input (geometry or origin) is later in the timeline than the joint, so it
            # does not exist at the rolled-back marker. Name the cause rather than ship the raw error.
            msg += (" - a re-selected input likely appears LATER in the timeline than the joint; a "
                    "joint can only reference geometry/origins created before it. Recreate the joint "
                    "after that input with joint_create.")
        return error(msg)
    finally:
        if rolled:
            # Roll the marker to the TRUE END of the timeline: rollTo(False) stops immediately after
            # the edited joint, leaving downstream features rolled OUT, where they silently revert
            # to home while still reading healthy.
            tl = safe(lambda: design.timeline)
            n = safe(lambda: tl.count, 0) or 0
            if tl is not None and n:
                safe(lambda: setattr(tl, "markerPosition", n))
            else:
                safe(lambda: joint.timelineObject.rollTo(False))

    # Editing a joint rolls the timeline marker, which can leave DOWNSTREAM features in a stale
    # compute-failed state until a full recompute. A computeAll that RAISES is not a failure of the
    # edit, which already landed, so 'recomputed' publishes what actually ran.
    recompute_errors = None
    recomputed = False
    try:
        design.computeAll()
        recomputed = True
        recompute_errors, _, _ = _common.timeline_health(design)
    except Exception:
        pass

    out = {"edited": True, "joint_name": safe(lambda: joint.name), "changes": changed}
    # surface the most-asked fields at top level for convenience
    for key in ("input_one", "input_two", "joint_type", "axis", "slide_axis", "world_axis", "flipped",
                       "offset", "angle", "min_deg", "max_deg", "rest_deg", "min_mm", "max_mm", "rest_mm"):
        if key in changed:
            out[key] = changed[key]
    out["recomputed"] = recomputed
    if recompute_errors:
        out["timeline_errors_after"] = recompute_errors
        out["note"] = ("Joint edited + recomputed, but the timeline still has errored feature(s) "
                       f"({', '.join(recompute_errors)}) - the edit may over-constrain something.")
    elif recomputed:
        out["note"] = ("Joint edited in place + full recompute (downstream features settled). "
                       "view_screenshot to view.")
    else:
        out["note"] = ("Joint edited in place, but the full recompute RAISED - downstream features "
                       "may be unsettled and their health unread. Run design_recompute and check "
                       "workspace_orient before trusting the model state.")
    # A limit whose read-back could not be taken is published NULL in 'changes' (and at top level),
    # never the request echoed back, and named here so the null is not read as a confirmed write.
    if limits_unverified:
        out["limits_unverified"] = limits_unverified
        out["note"] += _unverified_limits_note(limits_unverified)
    # Same rule for the flip/offset/angle arms: each publishes the joint's own read-back, and a
    # read-back that could not be taken is null plus its name here, never the request.
    if edits_unverified:
        out["edits_unverified"] = edits_unverified
        out["note"] += _unverified_edits_note(edits_unverified)
    if any(k in changed for k in ("rest_mm", "rest_deg")):
        out["note"] += _REST_LIMIT_NOTE
    mp = _motion_param_names(joint)
    if mp:
        out["model_parameters"] = mp
        out["note"] += _OFFSET_PARAM_NOTE
    # Suppressed-joint disclosure: the edit is REAL but the joint is INERT while suppressed. BOTH
    # flags are OR'd - Joint.isSuppressed keeps reading False when the suppression was set on the
    # TIMELINE item. read_flag, so two unreadable flags stay undisclosed.
    sup = (_common.read_flag(lambda: joint.isSuppressed) or
           _common.read_flag(lambda: joint.timelineObject.isSuppressed))
    if sup:
        out["suppressed"] = True
        out["note"] += (" WARNING: this joint is SUPPRESSED - the edit landed on the definition but "
                        "the joint is INERT and positions nothing until it is unsuppressed "
                        "(design_edit_timeline action='suppress', suppressed=false).")
    return ok(out)


TOOL_DESCRIPTION = (
"Edit an existing joint's DEFINITION in place; joint_drive poses it to a value instead."
)
tool = (
    Tool.create_simple(name="joint_edit", description=TOOL_DESCRIPTION)
    .add_input_property("joint_name", {"type": "string"})
    .add_input_property("input_one", {"type": "string",
            "description": "A Joint Origin name or '<occurrence>:<snap>'."})
    .add_input_property("input_two", {"type": "string"})
    # No schema default on either: an omitted joint_type leaves the motion alone, and 'axis' is read
    # only when a joint_type/world_axis re-sets it.
    .add_input_property(*_inputs.joint_motion(default="", options=_MOTIONS,
            description="").as_property())
    .add_input_property(*_inputs.frame_axis("axis", default="",
            description="FRAME-relative, not world; for pin_slot the rotation axis.").as_property())
    .add_input_property(*_inputs.frame_axis("slide_axis", default="",
            description="pin_slot only.").as_property())
    .add_input_property(*_inputs.frame_axis("world_axis", default="",
            description="Re-point the motion to a TRUE WORLD axis.").as_property())
    .add_input_property("flip", {"type": "boolean",
            "description": "Sets the flag - not a toggle."})
    .add_input_property("offset", {"type": "number",
            "description": "In 'units'; the anchor offset along the joint frame's Z, not a slide "
                           "value (joint_drive poses that)."})
    .add_input_property("angle", {"type": "number", "description": "In degrees."})
    .add_input_property(*_inputs.UNITS.as_property())
    # rotation_deg is intentionally NOT exposed: the handler still accepts the kwarg and returns a
    # helpful redirect if passed, but advertising a parameter whose only behavior is to error wastes
    # context. To pose a joint, use joint_drive.
    .add_input_property("min_deg", {"type": "number"})
    .add_input_property("max_deg", {"type": "number"})
    .add_input_property("rest_deg", {"type": "number"})
    .add_input_property("min_mm", {"type": "number", "description": "In 'units'."})
    .add_input_property("max_mm", {"type": "number", "description": "In 'units'."})
    .add_input_property("rest_mm", {"type": "number", "description": "In 'units'."})
    .strict_schema()
)
item = Item.create_tool_item(
    tool=tool, write="write", handler=handler, run_on_main_thread=True,
    # Every VALUE the tool sets is re-read off the joint and published as that read-back, erroring
    # when the joint keeps its own value; the motion arm gates on the platform's own setter bool.
    verification=Verification(
        kind="inline", rung="value",
        evidence_test="tests/unit/test_joint_edit.py::TestSwallowedSets::"
                      "test_a_flip_that_did_not_take_errors_naming_it"))


def register_tool():
    register(item)
