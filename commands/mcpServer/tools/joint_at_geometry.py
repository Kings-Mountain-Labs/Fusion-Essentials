# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Creates a joint at two GEOMETRY HANDLES (the consume half of geometry-as-values): joint two parts
at specific geometry - a crank pin's cylindrical face to a rod's bore, a hole edge to a pin - given the
two handles find_geometry returned. The joint lands AT the real geometry, NOT collapsed to the part
origins. Motion: rigid/revolute/slider/cylindrical/ball, with an axis. WRITES.
"""

import adsk.core
import adsk.fusion

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item
from ..mcp_primitives.registry import register
from ._common import apply_rename, ok, error, safe
from . import _common
from . import _inputs
from . import _outputs
from . import _assert
from ._joints import (AXES as _AXES, FLIP_HINT, OFFSET_PARAM_NOTE, apply_motion,
                      build_joint_geometry as _joint_geometry_for,
                      is_joint_origin as _is_joint_origin, motion_param_names,
                      normals_oppose as _normals_oppose, pending_move_guard,
                      planar_outward_normal as _planar_outward_normal)

app = adsk.core.Application.get()

# What this tool RETURNS: the joint name (a consumer key) + the AUTHORITATIVE health verdict (read from
# the joint's own state - no separate assembly_get needed to know if it computed).
RETURNS = [
    _outputs.ReturnsName("joint_name", of="joint", consumers=["joint_edit", "joint_motion_link"]),
    _outputs.ReturnsValue("healthy", "whether the joint actually COMPUTES (added != working); null "
                                     "only when neither the joint nor its timeline item answered a "
                                     "state"),
]

_MOTIONS = {"rigid", "revolute", "slider", "cylindrical", "ball"}

# The motions that consume NO axis input. Rigid has no motion to aim; a ball joint's
# setAsBallJointMotion takes no selectable axis at all - pitch is Z and yaw is X - so it reads
# neither the axis keyword nor a custom entity. Reporting an axis for either would be a false claim.
_NO_AXIS_MOTIONS = {"rigid", "ball"}

_AXIS = _inputs.Choice("axis", ["auto", "x", "y", "z"], default="auto",
                       description="ball uses none.")


# require='any': a joint can land on a face, edge, vertex, or construction/sketch point, and
# _joint_geometry_for does the per-kind validation.
_HANDLE_ONE = _inputs.GeometryHandle("handle_one", require="any", required=True)
_HANDLE_TWO = _inputs.GeometryHandle("handle_two", require="any", required=True)


def _joint_input_for(entity):
    """A joint input from a resolved handle: a JOINT ORIGIN (assembly_get(include=['joint_origins'])
    mints those handles) is used DIRECTLY - it IS a joint input; any other entity (face/edge/vertex/
    point) becomes a JointGeometry AT that geometry. Returns (input, label, error)."""
    if _is_joint_origin(entity):
        return entity, "joint_origin", None
    return _joint_geometry_for(entity)


def _occ_origin(occ):
    """The moving occurrence's origin as (x,y,z) cm in WORLD space, None if unreadable."""
    # transform2, not transform: .transform is the occurrence's LOCAL matrix with no parent
    # composed in, so the two agree only while every ancestor is identity. .transform is the
    # fallback for a build that does not carry transform2.
    m = safe(lambda: occ.transform2) or safe(lambda: occ.transform)
    t = safe(lambda: m.translation) if m is not None else None
    if t is None:
        return None
    return (safe(lambda: t.x, 0.0) or 0.0, safe(lambda: t.y, 0.0) or 0.0, safe(lambda: t.z, 0.0) or 0.0)


# Above this the reposition is reported as moved_by. 0.005 cm = 0.05 mm - below it the move is joint
# solver noise, not a teleport worth flagging.
_MOVE_TOL_CM = 0.005


def _move_delta(before, after):
    """{distance_mm, direction} if the moving occurrence shifted more than _MOVE_TOL_CM, else
    None."""
    # joint_at aligns the two picked KEYPOINTS (a planar face's centroid, an edge's midpoint), so
    # pairing a small feature with a large one repositions the part by the keypoint gap.
    if before is None or after is None:
        return None
    dx, dy, dz = after[0] - before[0], after[1] - before[1], after[2] - before[2]
    dist = (dx * dx + dy * dy + dz * dz) ** 0.5
    if dist <= _MOVE_TOL_CM:
        return None
    inv = 1.0 / dist
    return {"distance_mm": round(dist * 10.0, 3),
            "direction": [round(dx * inv, 4), round(dy * inv, 4), round(dz * inv, 4)]}


def _axis_entity(entity):
    """A cylinder/cone face or circular edge, which can define the joint's rotation/slide axis
    from its own axis; else None."""
    if isinstance(entity, adsk.fusion.BRepFace):
        st = safe(lambda: entity.geometry.surfaceType)
        if st in (adsk.core.SurfaceTypes.CylinderSurfaceType, adsk.core.SurfaceTypes.ConeSurfaceType):
            return entity
    if isinstance(entity, adsk.fusion.BRepEdge):
        if safe(lambda: entity.geometry.curveType) == adsk.core.Curve3DTypes.Circle3DCurveType:
            return entity
    return None


# healthState value -> the state's own name, for the states that are NOT a compute failure. The
# members are read off adsk.fusion.FeatureHealthStates by NAME, never a hand-typed int.
_NON_FAILURE_STATES = (("healthy", "HealthyFeatureHealthState"),
                       ("suppressed", "SuppressedFeatureHealthState"),
                       ("rolled_back", "RolledBackFeatureHealthState"))


def _non_failure_state(joint):
    """The NAME of the non-failure health state the joint reports, read off the joint first and
    then its timeline item; None when neither answers a state this build carries a member for."""
    states = safe(lambda: adsk.fusion.FeatureHealthStates)
    if states is None:
        return None
    for src in (joint, safe(lambda: joint.timelineObject)):
        hs = safe(lambda src=src: src.healthState) if src is not None else None
        if hs is None:
            continue
        for name, member in _NON_FAILURE_STATES:
            if hs == safe(lambda m=member: getattr(states, m)):
                return name
    return None


def _health_verdict(joint):
    """(healthy, state_name, message) for the created joint, over _assert.compute_state: False on
    an ERROR or WARNING state with the classifier's condensed text, True on a state that is no
    failure, None when NEITHER the joint nor its timeline item answered a state at all."""
    state, failure = _assert.compute_state(joint)
    if state == "broken":
        label, msg = failure
        return False, label, msg
    if state == "unknown":
        return None, None, None
    return True, _non_failure_state(joint), None


def _axis_note(mot, ax_name, use_custom):
    """The note sentence for what the motion axis ACTUALLY is: '' for a motion taking no axis, the
    geometry's own axis when the custom-direction path ran, else the joint FRAME's axis."""
    if mot in _NO_AXIS_MOTIONS:
        return ""
    if use_custom:
        return " axis='auto' derived the motion axis from the geometry itself."
    frame_ax = ax_name if ax_name in _AXES else "z"
    return (f" The motion axis is the joint geometry FRAME's {frame_ax} axis, NOT world {frame_ax} "
            "(they coincide only when that frame is world-aligned) - for a true world axis follow "
            "with joint_edit(world_axis=x/y/z).")


def handler(handle_one: str = "", handle_two: str = "", motion: str = "revolute",
            axis: str = "auto", name: str = "", flip: bool = False) -> dict:
    """Joint two parts at two geometry handles from find_geometry. 'auto' axis derives the motion
    axis from the geometry itself; x/y/z are FRAME-relative, and joint_edit(world_axis=) re-points
    a joint to a true world axis. WRITES."""
    mot = (motion or "revolute").strip().lower()
    if mot not in _MOTIONS:
        return error(f"Unknown motion '{motion}'. Use: {', '.join(sorted(_MOTIONS))}.")

    ax_name, ax_err = _AXIS.resolve(axis)
    if ax_err:
        return error(ax_err)

    design = _common.design()
    if not design:
        return error("No active design.")

    pending = pending_move_guard(design)
    if pending:
        return pending

    # Resolve each handle via the shared GeometryHandle kind (require='any' - joints accept faces, edges,
    # vertices, construction/sketch points; the per-kind validation happens in _joint_geometry_for). This
    # is the same typed path every other handle input uses (staleness note + 'live entity' error baked in).
    e1, err1 = _HANDLE_ONE.resolve(handle_one)   # the kind's error already names 'handle_one'
    if err1:
        return error(err1)
    e2, err2 = _HANDLE_TWO.resolve(handle_two)
    if err2:
        return error(err2)

    g1, l1, err1 = _joint_input_for(e1)
    if err1:
        return error(f"handle_one: {err1}")
    g2, l2, err2 = _joint_input_for(e2)
    if err2:
        return error(f"handle_two: {err2}")

    # Sample the two outward normals BEFORE the joint moves anything - the flush face-to-face pick
    # (normals opposing) is detected from the pre-joint pose.
    normals_oppose = _normals_oppose(_planar_outward_normal(e1), _planar_outward_normal(e2))

    root = design.rootComponent
    try:
        ji = root.joints.createInput(g1, g2)
    except Exception as e:
        return error(f"Could not create joint input from the two geometries: {e}")
    if flip:
        try:
            ji.isFlipped = True
        except Exception as e:
            return error(f"Could not apply flip: {e}")

    # axis='auto' (default): derive the motion axis from the geometry itself (a cylinder face / round
    # edge), so a pin rotates about the PIN's axis - not a guessed world axis that would over-constrain
    # the assembly. Prefer whichever input carries a usable axis.
    axis_ent = _axis_entity(e1) or _axis_entity(e2)
    use_custom = (ax_name == "auto") and (axis_ent is not None)
    did, merr = apply_motion(ji, mot, _AXES.get(ax_name, 2), axis_ent if use_custom else None)
    if merr or not did:
        # The axis advice only applies to a motion that HAS an axis - offering it on a ball/rigid
        # failure would contradict the input's own "ball uses none" and send the caller chasing a
        # knob this motion never read.
        hint = ("" if mot in _NO_AXIS_MOTIONS else
                " (For a frame-relative axis pass axis=x/y/z; 'auto' needs a cylinder face / round "
                "edge to derive the axis from.)")
        return error(f"Could not set {mot} motion: {merr or 'rejected'}.{hint}")

    # capture BOTH occurrences' origins BEFORE add() - the joint repositions parts to align the picked
    # keypoints, and that move must be reported, not silent (see _move_delta). Either side can be the
    # one that moves (grounding / an existing joint on one part decides), so watch both.
    occ_one = safe(lambda: e1.assemblyContext)
    occ_two = safe(lambda: e2.assemblyContext)
    before_one, before_two = _occ_origin(occ_one), _occ_origin(occ_two)

    try:
        joint = root.joints.add(ji)
    except Exception as e:
        return error(f"Joint creation failed: {e}. (The two geometries may be incompatible, or one "
    "part may be over-constrained.)")
    if not joint:
        return error("Joint creation returned nothing.")

    joint_name_final, rename_warning = apply_rename(joint, name)

    # report the joint's resulting occurrences so the caller can verify the wiring
    o1 = safe(lambda: joint.occurrenceOne.name)
    o2 = safe(lambda: joint.occurrenceTwo.name)
    # CHECK HEALTH at the source: a joint can be ADDED yet fail to COMPUTE (over-constrained) - the
    # 'Compute Failed' the user sees first. Surface it here so the caller doesn't trust a broken joint.
    healthy, health_state, failure_message = _health_verdict(joint)
    out = {
    "jointed": True,
    "joint_name": joint_name_final,
    "motion": mot,
    "axis": None if mot in _NO_AXIS_MOTIONS else ("auto(geometry)" if use_custom else ax_name),
    "healthy": healthy,
    "health_state": health_state,
    "flipped": bool(flip),
    "geometry_one": l1,
    "geometry_two": l2,
    "occurrence_one": o1,
    "occurrence_two": o2,
    "note": "Joint created AT the geometry." + _axis_note(mot, ax_name, use_custom)
    + " Verify with assembly_get (is_healthy + positions).",
    }
    if rename_warning:
        out["rename_warning"] = rename_warning
    mp = motion_param_names(joint)
    if mp:
        out["model_parameters"] = mp
        out["note"] += OFFSET_PARAM_NOTE
    # The flush face-to-face pick without flip: the joint aligns the two geometry frames Z-onto-Z
    # (each planar face's frame Z = its OUTWARD normal), so opposing normals force a 180-deg rotation
    # of the free part - live-verified, typically embedding it in the other part. Say so.
    if normals_oppose and not flip:
        out["flip_hint"] = FLIP_HINT
    if healthy is False:
        # _assert's condensation: Fusion's errorOrWarningMessage repeats its sentence joined by
        # 'Compute Failed' plus the joint's name, and a raw slice of that blob lands mid-word.
        msg = _assert.compute_failure_message(failure_message, 200)
        out["health_warning"] = ("This joint FAILED TO COMPUTE (likely over-constrained): "
                                 + (msg or "conflicts with assembly relationships"))
    elif healthy is None:
        # NEITHER the joint nor its timeline item answered a state - not a failure and not a clean
        # compute; claiming either would be a verdict this read never took.
        out["health_warning"] = ("Neither this joint nor its timeline item answered a health state, "
                                 "so whether it computes is UNVERIFIED here - 'healthy' is null. "
                                 "Re-read it with assembly_get (is_healthy / broken_joints).")
    elif health_state in ("suppressed", "rolled_back"):
        # A state that is not a compute failure but is not an active compute either: published by
        # NAME, with no claim about what the joint does or does not position.
        out["note"] += (f" This joint's health state reads '{health_state}' - not a compute failure. "
                        "Read what it positions with assembly_get.")
    elif health_state is None:
        # A state ANSWERED that no name in this tool's table matches. 'healthy' reports what was
        # read - not a compute failure - and no state name is claimed for it.
        out["note"] += (" This joint's compute state answered a value this tool has no name for - "
                        "it is not a compute failure, so 'healthy' is true and 'health_state' is "
                        "null. Read what it positions with assembly_get.")

    # Report how far a part was repositioned to align the keypoints. The move is legitimate joint
    # behavior (keypoints align at a face CENTROID / edge MIDPOINT) - flagging it just ends the silence
    # that let a mismatched-size pair teleport a part unnoticed. Whichever occurrence moved more wins.
    candidates = [(o1, _move_delta(before_one, _occ_origin(occ_one))),
                  (o2, _move_delta(before_two, _occ_origin(occ_two)))]
    moved_name, moved = max(((n, m) for n, m in candidates if m),
                            key=lambda nm: nm[1]["distance_mm"], default=(None, None))
    if moved:
        out["moved_by"] = moved
        out["move_warning"] = (
            f"'{moved_name}' moved {moved['distance_mm']} mm to align the picked keypoints (a planar "
            "face aligns at its CENTROID, an edge at its MIDPOINT) - so pairing differently sized "
            "features repositions the part. Expected joint behavior; restore an intended offset with "
            "joint_edit(offset) if this was not wanted.")
    return ok(out)


TOOL_DESCRIPTION = (
    "Joint two parts at two find_geometry handles - the FREE occurrence is the one moved. "
    "'healthy' is null when no compute state answered. joint_create takes names or snap-strings "
    "instead.\n"
    + _outputs.produces_block(RETURNS)
)

joint_at_tool = (
    Tool.create_simple(name="joint_at_geometry", description=TOOL_DESCRIPTION)
    .add_input_property(*_HANDLE_ONE.as_property())
    .add_input_property(*_HANDLE_TWO.as_property())
    .add_input_property(*_inputs.joint_motion(
        "motion", options=("rigid", "revolute", "slider", "cylindrical", "ball"),
        default="revolute", description="").as_property())
    .add_input_property(*_AXIS.as_property())
    .add_input_property("flip", {"type": "boolean"})
    .add_input_property("name", {"type": "string"})
    .strict_schema()
)
joint_at_item = Item.create_tool_item(tool=joint_at_tool, write="write", handler=handler,
                                      run_on_main_thread=True,
                                      postconditions=[_assert.ChildGeometryMoved()])


def register_tool():
    register(joint_at_item)
