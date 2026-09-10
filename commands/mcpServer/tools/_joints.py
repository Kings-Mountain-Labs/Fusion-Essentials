# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Joint substrate: the JointGeometry keypoint factory, the motion-type dispatcher, and the
Joint/AsBuiltJoint-by-name lookup every joint tool resolves an existing joint through."""

import math
import re

import adsk.core
import adsk.fusion

from . import _assert
from . import _common
from . import _geom
from ._common import safe


def is_joint_origin(x):
    """isinstance(x, adsk.fusion.JointOrigin), degrading to False when the type is not a real
    class instead of raising."""
    try:
        return isinstance(x, adsk.fusion.JointOrigin)
    except TypeError:
        return False


def is_as_built_joint(x):
    """isinstance(x, adsk.fusion.AsBuiltJoint), degrading to False when the type is not a real
    class. apply_motion routes an as-built joint through a DIFFERENT setter arity."""
    try:
        return isinstance(x, adsk.fusion.AsBuiltJoint)
    except TypeError:
        return False

# The "what to reuse from here" catalog line for the generated CLAUDE.md helper map.
MAP_BLURB = (
    "build_joint_geometry/apply_motion - keypoint factory + motion dispatch; all_joints/"
    "find_joint - walks joints+asBuiltJoints, REFUSING a shared name; DRIVES_ANY - drivable "
    "gate; motion_link_record/link_ratio_values - link record + ratio codec; "
    "all_joint_origins/jo_assembly_proxy - JointOrigin walk + proxy; component_world_matrix - "
    "world matrix ladder; pair_used_clause/PAIR_USED - used-pair refusal")


# The words Fusion refuses a SECOND joint on an already-jointed PAIR with - the ONE gate both create
# tools ride, so a different add() failure never gets this diagnosis.
PAIR_USED = re.compile(r"joint in system exists|over ?constrain", re.IGNORECASE)

# Measured on BOTH create tools: one pair refused a second joint through ':origin' snaps and again
# through ':top' snaps with the identical text, so the offending input is the pair, not the anchor.
PAIR_USED_ANCHOR_NOTE = " The same refusal comes back whichever anchor is passed."


def pair_used_clause(platform_text, id1, id2, anchor_note=""):
    """The clause naming the PAIR as what a joint create was refused on, or '' when Fusion's text
    does not name an existing joint."""
    if not PAIR_USED.search(platform_text or ""):
        return ""
    return (f" Fusion names an existing joint, not the geometry: '{id1}' and '{id2}' are already "
            f"jointed to each other.{anchor_note} Change the existing joint with joint_edit, or "
            "joint a different pair.")


def planar_outward_normal(entity):
    """Outward unit normal of a PLANAR face (the shared evaluator sample), else None - the input to
    the flush face-to-face detection: two planar faces whose outward normals OPPOSE."""
    try:
        if not isinstance(entity, adsk.fusion.BRepFace):
            return None
    except TypeError:               # the type is unavailable - nothing can be a face then
        return None
    if safe(lambda: entity.geometry.surfaceType) != adsk.core.SurfaceTypes.PlaneSurfaceType:
        return None
    return _geom.evaluator_normal_at(entity, safe(lambda: entity.pointOnFace))


def normals_oppose(n1, n2):
    """True when two unit normals point at each other (dot below -0.9) - the flush face-to-face
    pick. False when either is absent."""
    return (n1 is not None and n2 is not None
            and (n1[0] * n2[0] + n1[1] * n2[1] + n1[2] * n2[2]) < -0.9)


# The ONE flip-hint sentence every joint create publishes for the flush face-to-face pick without
# flip. A joint aligns the two geometry frames Z-onto-Z and a planar face's frame Z is its OUTWARD
# normal, so opposing normals rotate the free part 180 deg.
FLIP_HINT = ("The two planar faces' outward normals OPPOSE (the flush face-to-face pick). A joint "
             "aligns the two geometry frames Z-onto-Z, so the free part was ROTATED 180 deg to "
             "satisfy that - typically embedding it. For the seated flush mate, re-run with "
             "flip=true (or joint_edit flip).")


def motion_param_names(joint):
    """The joint's OWN ModelParameter names: {'offset': dNN, 'angle': dNN}, absent ones omitted."""
    # Joint.offset moves the anchor along the joint frame's tertiary (Z) axis and is the ONLY
    # parametric position drive a joint has; a slider's slide VALUE carries no ModelParameter at
    # all, even after joint_drive poses it. OFFSET_PARAM_NOTE carries both to the caller.
    out = {}
    for key in ("offset", "angle"):
        nm = safe(lambda k=key: getattr(joint, k).name)
        if nm:
            out[key] = nm
    return out


# The one wire sentence appended wherever a payload carries model_parameters.
OFFSET_PARAM_NOTE = (
    " model_parameters are the joint's own dNN params: param_set 'offset' to an expression for a "
    "PARAMETRIC position - it ALWAYS moves along the joint FRAME'S Z axis, and neither 'flip' nor "
    "'world_axis' redirects it. A slider's slide VALUE has no parameter, so parametric TRAVEL "
    "comes from co-driving the geometry the joint anchors on.")


# The design-wide moved-but-uncaptured position flag, and the refusal a joint CREATE returns while
# it is set - the one home every joint-creation tool and assembly_capture_position read.

def pending_position(design):
    """Design.snapshots.hasPendingSnapshot as True / False / None - None when the flag cannot be
    read at all, which is NOT evidence either way. A free move and a joint_drive pose both set it;
    a design_add_instance placement does not."""
    return _common.read_flag(lambda: design.snapshots.hasPendingSnapshot)


PENDING_MOVE_REFUSAL = (
    "Uncaptured occurrence moves exist and this joint creation would silently revert them - "
    "assembly_capture_position(action='capture') records the current pose into the timeline, and "
    "action='discard_pending' throws the move away. The flag is design-wide, so it names no "
    "occurrence; action='status' reports it and lists the captured markers.")


def pending_move_guard(design):
    """The refusal a joint CREATE returns while an uncaptured move is pending, else None. Only a
    flag that reads True refuses - an unreadable flag is not evidence a move is pending."""
    # Creating a joint recomputes the assembly, and a recompute REVERTS an uncaptured position: the
    # parts snap back to their last captured pose and the new joint freezes THAT one. Driving a
    # joint back to 0 clears the flag, so a sequence that restores its drives never meets this.
    return _common.error(PENDING_MOVE_REFUSAL) if pending_position(design) is True else None

# axis keyword -> JointDirections axis index (Custom=3 is not indexed here - it is selected by
# passing a custom_entity to apply_motion instead).
AXES = {"x": 0, "y": 1, "z": 2}


def _non_planar_face_geometry(entity, keypoint):
    """createByNonPlanarFace(entity, keypoint) as (geometry, error) - the API's OWN raise text is
    carried into the error, since it names the keypoint a face type demands."""
    try:
        g = adsk.fusion.JointGeometry.createByNonPlanarFace(entity, keypoint)
    except Exception as e:
        return None, f"createByNonPlanarFace failed: {e}"
    return g, None if g else "createByNonPlanarFace failed"


# Two keypoints agreeing to this in cm are the same point - the trap below misses by whole
# centimetres, so the band only absorbs float noise.
_KEYPOINT_TOL_CM = 1e-4


def _xyz(pt):
    """(x, y, z) off a Point3D, or None when any component is unreadable."""
    if pt is None:
        return None
    vals = (safe(lambda: pt.x), safe(lambda: pt.y), safe(lambda: pt.z))
    return None if any(not isinstance(v, (int, float)) or isinstance(v, bool) for v in vals) else vals


def _fmt_point(xyz):
    """A published coordinate, rounded to 4dp (a micron in cm); the caller labels its FRAME."""
    return "(%.4f, %.4f, %.4f)" % xyz


def _occurrence_chain(occ):
    """`occ` and each of its assembly ancestors, innermost first. A top-level occurrence's
    assemblyContext reads None, ending the walk; the depth cap is a cycle guard."""
    out = []
    while occ is not None and len(out) < 64:
        out.append(occ)
        occ = safe(lambda o=occ: o.assemblyContext)
    return out


def component_world_matrix(design, comp, context_occ=None):
    """The Matrix3D taking `comp`'s OWN frame into WORLD, or None when no single placement answers
    for it: the ROOT's frame IS world, a component placed once takes that occurrence's transform2,
    and `context_occ` (or one of its ancestors) picks the instance for a multi-placed one."""
    # transform2 is the ONLY matrix read: `transform` is LOCAL and composes no parent, so on a
    # nested occurrence it names a different frame.
    root = safe(lambda: design.rootComponent)
    if comp is None or root is None:
        return None
    # `is True` on both: an identity comparison that did not read cannot mint a frame. An unproven
    # match falls through the placement ladder and, failing that, to None.
    if _common.same_component(comp, root) is True:
        return safe(lambda: adsk.core.Matrix3D.create())
    for o in _occurrence_chain(context_occ):
        if _common.same_component(safe(lambda o=o: o.component), comp) is True:
            return safe(lambda o=o: o.transform2)
    occs = list(safe(lambda: root.allOccurrencesByComponent(comp)) or [])
    if len(occs) == 1:
        return safe(lambda: occs[0].transform2)
    return None


def _world_placement(entity):
    """The Matrix3D taking `entity`'s owning component's frame into WORLD, over
    component_world_matrix and the occurrence the entity was reached through; None when no single
    placement answers, which means the caller makes NO judgement rather than a wrong one."""
    return component_world_matrix(_common.design(),
                                  safe(lambda: entity.body.parentComponent),
                                  safe(lambda: entity.assemblyContext))


def _world_torus_centre(entity):
    """The torus face's own centre in WORLD coordinates, or None when it cannot be established."""
    # Which frame geometry.origin answers in follows assemblyContext: a NATIVE face reads
    # component-local and needs the lift, while a face reached through the assembly PROXY is
    # already world and a second lift would land it off the model.
    origin = safe(lambda: entity.geometry.origin)
    if origin is None:
        return None
    if safe(lambda: entity.assemblyContext) is not None:
        return _xyz(origin)
    m = _world_placement(entity)
    if m is None:
        return None
    moved = safe(lambda: origin.copy())
    if moved is None or not safe(lambda: moved.transformBy(m)):
        return None
    return _xyz(moved)


def _torus_keypoint_error(g, entity):
    """Error text when a TORUS CenterKeyPoint does not describe the torus face, else None -
    either side unestablished means no judgement."""
    # createByNonPlanarFace on a torus inside a BASE FEATURE returns the OWNING COMPONENT'S ORIGIN,
    # world-framed, and raises nothing - so the keypoint is compared against the torus's own centre
    # read in the SAME world frame, the only comparison that separates the two.
    kp = _xyz(safe(lambda: g.origin))
    centre = _world_torus_centre(entity)
    if kp is None or centre is None:
        return None
    if max(abs(a - b) for a, b in zip(kp, centre)) <= _KEYPOINT_TOL_CM:
        return None
    return (f"This torus face's joint keypoint came back as {_fmt_point(kp)} cm in WORLD space, but "
            f"the torus face is centred at {_fmt_point(centre)} cm in WORLD space - the keypoint "
            "does not describe the face. A torus face inside a BASE FEATURE returns its owning "
            "COMPONENT'S ORIGIN from this call with no error, so the joint would be anchored there "
            "instead. Pick a circular EDGE or a planar face on this body, or rebuild the torus "
            "parametrically (model_revolve).")


def build_joint_geometry(entity, edge_keypoint=None):
    """Build a JointGeometry for a face/edge/vertex/point entity, picking the keypoint the API
    accepts for that kind; edge_keypoint overrides the automatic edge pick with an explicit
    JointKeyPointTypes value. Returns (geometry, label, error)."""
    JG = adsk.fusion.JointGeometry
    KP = adsk.fusion.JointKeyPointTypes
    if isinstance(entity, adsk.fusion.BRepFace):
        st = safe(lambda: entity.geometry.surfaceType)
        if st == adsk.core.SurfaceTypes.PlaneSurfaceType:
            g = safe(lambda: JG.createByPlanarFace(entity, None, KP.CenterKeyPoint))
            return g, "planar_face@center", None if g else "createByPlanarFace failed"
        if st in (adsk.core.SurfaceTypes.CylinderSurfaceType, adsk.core.SurfaceTypes.ConeSurfaceType):
            g, err = _non_planar_face_geometry(entity, KP.MiddleKeyPoint)
            return g, "cylinder_face@middle", err
        # A sphere or torus face takes ONLY CenterKeyPoint: MiddleKeyPoint raises "Key point type
        # should be CenterKeyPoint, if the face is sphere and torus face".
        centre_only = {adsk.core.SurfaceTypes.SphereSurfaceType: "sphere_face@center",
                       adsk.core.SurfaceTypes.TorusSurfaceType: "torus_face@center"}
        if st in centre_only:
            g, err = _non_planar_face_geometry(entity, KP.CenterKeyPoint)
            if err is None and st == adsk.core.SurfaceTypes.TorusSurfaceType:
                err = _torus_keypoint_error(g, entity)
                if err:
                    g = None
            return g, centre_only[st], err
        g, err = _non_planar_face_geometry(entity, KP.MiddleKeyPoint)
        return g, "nonplanar_face@middle", err
    if isinstance(entity, adsk.fusion.BRepEdge):
        if edge_keypoint is not None:
            kp = edge_keypoint
        else:
            ct = safe(lambda: entity.geometry.curveType)
            kp = KP.CenterKeyPoint if ct == adsk.core.Curve3DTypes.Circle3DCurveType else KP.MiddleKeyPoint
        g = safe(lambda: JG.createByCurve(entity, kp))
        return g, "edge", None if g else "createByCurve failed for this edge"
    if isinstance(entity, adsk.fusion.SketchPoint):
        g = safe(lambda: JG.createByPoint(entity))
        return g, "sketch_point", None if g else "createByPoint failed"
    if isinstance(entity, (adsk.fusion.BRepVertex, adsk.fusion.ConstructionPoint)):
        g = safe(lambda: JG.createByPoint(entity))
        return g, "point", None if g else "createByPoint failed"
    return None, None, f"entity kind {type(entity).__name__} is not a supported joint geometry"


def apply_motion(ji, jtype, axis_idx, custom_entity=None, slide_axis_idx=None):
    """Set rigid/revolute/slider/cylindrical/planar/ball/pin_slot motion on a JointInput or an
    existing Joint. axis_idx (0/1/2) selects the FRAME-relative axis unless custom_entity is given,
    which pairs CustomJointDirection with that entity for a TRUE direction. pin_slot takes two:
    axis_idx rotates and slide_axis_idx slides, and they must differ. Returns (did, error)."""
    JD = adsk.fusion.JointDirections
    dirs = [JD.XAxisJointDirection, JD.YAxisJointDirection, JD.ZAxisJointDirection]
    if custom_entity is not None:
        ax = JD.CustomJointDirection
    else:
        ax = dirs[axis_idx]
    # An EXISTING as-built joint takes a DIFFERENT setter arity than a JointInput: its motion
    # setters carry an extra JointGeometry arg, and a rigid as-built joint carries NO geometry
    # ("Geometry should not be null if joint motion is not rigid"), so it converts to no motion.
    if jtype != "rigid" and is_as_built_joint(ji):
        geom = safe(lambda: ji.geometry)
        if geom is None:
            return False, (f"this is a rigid AS-BUILT joint with no joint geometry, so the API "
                           f"cannot redefine it as a '{jtype}' joint (it has no anchor to move "
                           "along). Delete it and build the motion joint with joint_create_as_built "
                           "(same two occurrences, plus the 'geometry' the motion anchors on), "
                           "joint_create (a ':origin' snap) or joint_at_geometry (a real face/edge).")
        setter = {"revolute": "setAsRevoluteJointMotion", "slider": "setAsSliderJointMotion",
                  "cylindrical": "setAsCylindricalJointMotion",
                  "planar": "setAsPlanarJointMotion"}.get(jtype)
        if setter is None:
            return False, (f"redefining an as-built joint as '{jtype}' is not supported here - "
                           "delete it and use joint_create.")
        try:
            fn = getattr(ji, setter)
            if custom_entity is not None:
                return bool(fn(ax, geom, custom_entity)), None
            return bool(fn(ax, geom)), None
        except Exception as e:
            return False, str(e)
    try:
        if jtype == "rigid":
            return bool(ji.setAsRigidJointMotion()), None
        if jtype == "pin_slot":
            # setAsPinSlotJointMotion(rotationAxis, slideDirection[, customRotationAxisEntity,
            # customSlideDirectionEntity]) - a positional custom_entity fills the ROTATION entity,
            # so the slide direction stays frame-relative.
            s_idx = slide_axis_idx if slide_axis_idx is not None else (axis_idx + 1) % 3
            if s_idx == axis_idx:
                return False, "pin_slot rotation axis and slide direction must differ."
            slide_dir = dirs[s_idx]
            if custom_entity is not None:
                return bool(ji.setAsPinSlotJointMotion(ax, slide_dir, custom_entity)), None
            return bool(ji.setAsPinSlotJointMotion(ax, slide_dir)), None
        if jtype == "revolute":
            if custom_entity is not None:
                return bool(ji.setAsRevoluteJointMotion(ax, custom_entity)), None
            return bool(ji.setAsRevoluteJointMotion(ax)), None
        if jtype == "slider":
            if custom_entity is not None:
                return bool(ji.setAsSliderJointMotion(ax, custom_entity)), None
            return bool(ji.setAsSliderJointMotion(ax)), None
        if jtype == "cylindrical":
            if custom_entity is not None:
                return bool(ji.setAsCylindricalJointMotion(ax, custom_entity)), None
            return bool(ji.setAsCylindricalJointMotion(ax)), None
        if jtype == "planar":
            if custom_entity is not None:
                return bool(ji.setAsPlanarJointMotion(ax, custom_entity)), None
            return bool(ji.setAsPlanarJointMotion(ax)), None
        if jtype == "ball":
            # pitch MUST be Z, yaw MUST be X (not the intuitive X/Y) - the API rejects any other pair
            # with "Invalid parameter pitchDirection".
            return bool(ji.setAsBallJointMotion(JD.ZAxisJointDirection, JD.XAxisJointDirection)), None
    except Exception as e:
        return False, str(e)
    return False, f"unsupported joint_type '{jtype}'"


_MOTION_CLASS_TO_TYPE = {
"RigidJointMotion": "rigid", "RevoluteJointMotion": "revolute",
"SliderJointMotion": "slider", "CylindricalJointMotion": "cylindrical",
"PlanarJointMotion": "planar", "BallJointMotion": "ball",
"PinSlotJointMotion": "pin_slot",
}


def current_joint_type(joint):
    """Map a joint's current JointMotion subclass to the create/edit joint_type keyword (or '' if the
    motion is absent or unrecognized)."""
    jm = safe(lambda: joint.jointMotion)
    return _MOTION_CLASS_TO_TYPE.get(type(jm).__name__, "") if jm else ""


# Joint KIND -> the drivable degree of freedom it carries, the ONE pairing every consumer of a
# rotate-or-slide member selects through. The kinds in NEITHER set - rigid, ball, planar, pin_slot -
# are not addressed here, and a consumer reading one states its own basis.
DRIVES_ANGLE = frozenset(("revolute", "cylindrical"))
DRIVES_SLIDE = frozenset(("slider", "cylindrical"))

# Their union - the kinds that carry a drivable value AT ALL, which is the set joint_drive's refusal
# and the as-built create's pose pointer each gate on before either half is selected.
DRIVES_ANY = frozenset(DRIVES_ANGLE | DRIVES_SLIDE)


# JointMotion subclass -> the single JointMotionTypes DOF a MotionLink.setMotionData couples. That
# is a DIFFERENT enum from JointTypes: jointMotion.jointType is a JointTypes value, which
# setMotionData REJECTS as "BAD_JOINT_DOF - Motion Link joint DOF is wrong type".
def motion_link_dof(joint):
    """The JointMotionTypes DOF MotionLink.setMotionData couples for `joint` as (value, None), or
    (None, reason) when it has no single linkable rotate/slide DOF."""
    JMT = adsk.fusion.JointMotionTypes
    table = {
        "RevoluteJointMotion": JMT.RevoluteJointRotateMotionType,
        "SliderJointMotion": JMT.SliderJointSlideMotionType,
        "CylindricalJointMotion": JMT.CylindricalJointRotateMotionType,
    }
    jm = safe(lambda: joint.jointMotion)
    cls = type(jm).__name__ if jm else ""
    if cls in table:
        return table[cls], None
    kw = _MOTION_CLASS_TO_TYPE.get(cls, "") or "unknown"
    if kw == "rigid":
        return None, "is a rigid joint (no motion to link)"
    return None, (f"is a '{kw}' joint - motion links couple single-DOF joints "
                  "(revolute, slider, or cylindrical)")


# The DISPLAY unit a motion-link ratio is stated in, per DOF kind, beside Fusion's NATIVE unit for
# the same DOF and the native-per-display factor between them.
_DOF_UNITS = {
    "rotation": ("deg", "rad", math.pi / 180.0),      # rad-per-deg; no repo constant carries it
    "slide": ("mm", "cm", _common.scale("mm")),       # cm-per-mm, from the one unit table
}

# JointMotionTypes members per kind, addressed BY NAME: a member a build does not carry leaves the
# table rather than raising, and its DOF then classifies as unknown.
_ROTATION_DOF_NAMES = ("RevoluteJointRotateMotionType", "CylindricalJointRotateMotionType",
                       "PinSlotJointRotateMotionType", "PlanarJointRotateMotionType",
                       "BallJointPitchMotionType", "BallJointRollMotionType",
                       "BallJointYawMotionType")
_SLIDE_DOF_NAMES = ("SliderJointSlideMotionType", "CylindricalJointSlideMotionType",
                    "PinSlotJointSlideMotionType", "PlanarJointSlideOneMotionType",
                    "PlanarJointSlideTwoMotionType")


def dof_motion_kind(dof):
    """'rotation' or 'slide' for a JointMotionTypes DOF value, else None - which keeps a ratio
    across it UNCONVERTED rather than scaled by a guessed unit."""
    if dof is None:
        return None
    JMT = adsk.fusion.JointMotionTypes
    for kind, names in (("rotation", _ROTATION_DOF_NAMES), ("slide", _SLIDE_DOF_NAMES)):
        for nm in names:
            member = safe(lambda nm=nm: getattr(JMT, nm))
            if member is not None and member == dof:
                return kind
    return None


def _ratio_text(value):
    """A ratio number as a wire sentence states it, rounded to 12 significant digits."""
    return "%.12g" % value


# setMotionData's two values are in Fusion's NATIVE units, not the display units a caller states a
# ratio in, so a ratio across two DIFFERENT DOF kinds converts by both factors.
def link_ratio_values(dof_one, dof_two, ratio):
    """The (value_one, value_two, facts) MotionLink.setMotionData is given for a caller's `ratio` -
    joint_two's motion per ONE unit of joint_one in DISPLAY units. value_one is always 1 and both
    are MAGNITUDES, the sign travelling as isReversed; `facts` carries the three wire keys both
    writers publish, and states what was SENT rather than what will move."""
    r = float(ratio)
    mag = abs(r)
    k1, k2 = dof_motion_kind(dof_one), dof_motion_kind(dof_two)
    if k1 is None or k2 is None:
        return 1.0, mag, {
            "ratio_units": None, "value_units": None,
            "interpreted": (
                f"Sent to setMotionData as 1 for joint_one and {_ratio_text(mag)} for joint_two, "
                "with NO unit conversion: one of the two coupled degrees of freedom does not answer "
                "as a rotation or a slide here, so no display unit is established for it.")}
    d1, n1, f1 = _DOF_UNITS[k1]
    d2, n2, f2 = _DOF_UNITS[k2]
    value_two = mag if k1 == k2 else mag * (f2 / f1)
    sign = "" if r >= 0 else " The sign is carried by isReversed, not by these two values."
    return 1.0, value_two, {
        "ratio_units": f"{d2} of joint_two per {d1} of joint_one",
        "value_units": f"value_one in {n1}, value_two in {n2}",
        "interpreted": (
            f"Ratio read in DISPLAY units: {_ratio_text(mag)} {d2} of joint_two per 1 {d1} of "
            f"joint_one. Sent to setMotionData as 1 {n1} for joint_one and "
            f"{_ratio_text(value_two)} {n2} for joint_two - Fusion's native units." + sign)}


# Two link parameters whose RATIO agrees with the sent value_two to this relative band express the
# same coupling: a converted value carries a float tail a platform storing the clean number does not.
RATIO_READ_TOLERANCE = 1e-6


def link_ratio_mismatch(value_two, read_one, read_two):
    """Why a link's own valueOne/valueTwo do NOT express `value_two` as a clause naming the
    numbers, or None when they do. The comparison is the RATIO read_two / read_one, since a
    platform storing the coupling scaled still expresses the sent ratio; a pair that did not READ
    as two numbers is no evidence either way and answers None."""
    for v in (read_one, read_two):
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            return None
    if not read_one:
        return f"its valueOne parameter reads {read_one} - a zero first value is no coupling at all"
    got = read_two / read_one
    if abs(got - value_two) > max(RATIO_READ_TOLERANCE, RATIO_READ_TOLERANCE * value_two):
        return (f"its parameters read {read_one}:{read_two} (a ratio of {got}), not the {value_two} "
                "this ratio converts to - the ratio did not take")
    return None


def all_joints(design):
    """Every Joint AND AsBuiltJoint in the design as a flat list, over every component and both of
    the SEPARATE collections one carries them in - a joint internal to a sub-component lives on
    THAT component, so a root-only walk under-reports."""
    out, seen = [], set()
    # _common.all_components already carries the root, so prepending design.rootComponent would
    # read every root joint twice as two wrappers, which the id() fallback key never collapses.
    for c in _common.all_components(design):
        for coll_name in ("joints", "asBuiltJoints"):
            jc = safe(lambda c=c, cn=coll_name: getattr(c, cn))
            for i in range(safe(lambda: jc.count, 0) or 0 if jc else 0):
                j = safe(lambda i=i: jc.item(i))
                if j is None:
                    continue
                # A SUPPRESSED joint's token can read None, so the fallback key is (name,
                # objectType, owning component), which two readings of one joint still share; id()
                # remains only for a joint with no readable name.
                token = safe(lambda j=j: j.entityToken)
                if token is not None:
                    key = ("tok", token)
                else:
                    nm = safe(lambda j=j: j.name)
                    key = (("nm", nm, safe(lambda j=j: j.objectType),
                            safe(lambda j=j: j.parentComponent.name))
                           if nm else ("id", id(j)))
                if key in seen:
                    continue
                seen.add(key)
                out.append(j)
    return out


def find_joints_by_name(design, name):
    """Every Joint or AsBuiltJoint whose name EXACTLY matches `name`, over all_joints - a LIST,
    since a joint name is only component-locally unique."""
    want = (name or "").strip()
    if not want:
        return []
    return [j for j in all_joints(design) if (safe(lambda j=j: j.name) or "") == want]


def find_joint(design, name):
    """Resolve ONE Joint or AsBuiltJoint by name over all_joints; returns (joint, error). Several
    hits are REFUSED naming each hit's owning component, and a name no joint carries is
    (None, None) - the caller words its own not-found error."""
    hits = find_joints_by_name(design, name)
    if len(hits) == 1:
        return hits[0], None
    if not hits:
        return None, None
    want = (name or "").strip()
    where = ", ".join(
        f"'{want}' in {safe(lambda j=j: j.parentComponent.name) or '(unreadable component)'}"
        for j in hits[:8])
    return None, (f"'{want}' names {len(hits)} joints ({where}) - joint names are only unique within "
                  "a component. Rename one in Fusion so the name resolves to a single joint "
                  "(assembly_get lists every joint in the design).")


def _link_partner_name(ml, my_name):
    """The name of the joint in `ml` that is NOT `my_name`, or None when the link names no partner -
    jointTwo is null for a link between two DOF of the SAME joint."""
    one = safe(lambda: ml.jointOne.name)
    two = safe(lambda: ml.jointTwo.name)
    if one == my_name and two:
        return two
    if two == my_name and one:
        return one
    return None


def _suppression_state(entity):
    """`entity`'s suppression as a TRI-STATE over its own isSuppressed and its timelineObject's:
    True when EITHER reads True, False when at least one read and neither is True, None when
    NEITHER read - a Joint's own flag keeps reading False for a suppression set on the timeline."""
    own = _common.read_flag(lambda: entity.isSuppressed)
    timeline = _common.read_flag(lambda: entity.timelineObject.isSuppressed)
    if own is True or timeline is True:
        return True
    return None if own is None and timeline is None else False


def _blank_link(linked):
    """The record shape for a joint carrying no partner link: `linked` False when the membership
    READ and named none, None when the membership itself could not be read."""
    return {"linked": linked, "partner": None, "link": None, "suppressed": None, "broken": None,
            "value_self": None, "value_partner": None, "reversed": None}


def motion_link_record(joint):
    """`joint`'s own MotionLink membership as ONE record: linked / partner / link / suppressed /
    broken / value_self / value_partner / reversed, each a TRI-STATE a consumer branches on with
    ``is True`` / ``is False`` / ``is None``. The two values are in Fusion's internal units."""
    # Joint.motionLinks is a MotionLinkVector - a plain SEQUENCE with no .count/.item, so a
    # collection-style read finds nothing. jointTwo is null for a same-joint two-DOF link.
    my_name = safe(lambda: joint.name)
    if not my_name:
        return _blank_link(None)
    links = safe(lambda: list(joint.motionLinks))
    if links is None:                       # the membership read RAISED - not an empty membership
        return _blank_link(None)
    for ml in links:
        if ml is None:
            continue
        partner = _link_partner_name(ml, my_name)
        if not partner:
            continue
        one_is_self = safe(lambda: ml.jointOne.name) == my_name
        state, _failure = _assert.compute_state(ml)
        return {
            "linked": True,
            "partner": partner,
            "link": safe(lambda: ml.name),
            "suppressed": _suppression_state(ml),
            "broken": None if state == "unknown" else (state == "broken"),
            "value_self": _common.measured(
                lambda: (ml.valueOne if one_is_self else ml.valueTwo).value),
            "value_partner": _common.measured(
                lambda: (ml.valueTwo if one_is_self else ml.valueOne).value),
            "reversed": _common.read_flag(lambda: ml.isReversed),
        }
    return _blank_link(False)


def motion_link_partner(joint):
    """The name of the joint motion-linked to `joint`, else None - the NAME projection of
    motion_link_record. A caller that branches on whether the link COUPLES reads the record: this
    cannot tell a suppressed or broken link from a working one, nor an unread membership."""
    return motion_link_record(joint)["partner"]


def all_joint_origins(design):
    """Every JointOrigin in the design as a flat list of (jo, owning_component), over every
    component - a JO internal to a sub-component lives on THAT component."""
    out, seen = [], set()
    # _common.all_components already carries the root, so prepending design.rootComponent would
    # read every root JO twice as two wrappers, which the id() fallback key never collapses.
    for c in _common.all_components(design):
        jos = safe(lambda c=c: c.jointOrigins)
        for i in range(safe(lambda: jos.count, 0) or 0 if jos else 0):
            jo = safe(lambda i=i: jos.item(i))
            if jo is None:
                continue
            token = safe(lambda jo=jo: jo.entityToken)
            key = token if token is not None else id(jo)
            if key in seen:
                continue
            seen.add(key)
            out.append((jo, c))
    return out


def find_joint_origins_by_name(design, name):
    """Every (jo, owning_component) whose JointOrigin name EXACTLY matches `name`, over
    all_joint_origins - a LIST, since a JO name is only component-locally unique."""
    want = (name or "").strip()
    if not want:
        return []
    return [(jo, c) for jo, c in all_joint_origins(design)
            if (safe(lambda jo=jo: jo.name) or "") == want]


def jo_assembly_proxy(design, jo, comp):
    """Return `jo` usable in ASSEMBLY CONTEXT as (obj, error): the native JO on the root component,
    else its proxy in the SINGLE occurrence of its owning component - a native sub-component JO
    yields 'Provided input paths for joint are not valid'. An owner instanced MORE THAN ONCE is
    refused, naming the '<occurrence>:<JO name>' form that picks one."""
    root = safe(lambda: design.rootComponent)
    # `is True`: only a PROVEN root JO is handed back native (the form Fusion refuses anywhere else).
    # An unproven owner takes the placement walk below, which ends on the same native when nothing
    # places the component - so the unknown costs one lookup and claims nothing.
    if _common.same_component(comp, root) is True:
        return jo, None
    occs = list(safe(lambda: root.allOccurrencesByComponent(comp)) or []) if root else []
    if len(occs) == 1:
        proxy = safe(lambda: jo.createForAssemblyContext(occs[0]))
        if proxy is None:
            # The native is the object the line above says Fusion refuses, so falling back to it
            # hands the joint the input that yields 'Provided input paths for joint are not valid'.
            nm = safe(lambda: jo.name) or "?"
            path = safe(lambda: occs[0].fullPathName) or safe(lambda: occs[0].name) or "its one occurrence"
            return None, (f"Joint Origin '{nm}' could not be read in the assembly's space ({path}), "
                          "so where that frame sits in the model is unknown. Pass its handle from "
                          "assembly_get(include=['joint_origins']).")
        return proxy, None
    if not occs:
        return jo, None                       # not instanced in the assembly; native is the only form
    nm = safe(lambda: jo.name) or "?"
    return None, (f"Joint Origin '{nm}' is instanced {len(occs)} times - address it as "
                  f"'<occurrence>:{nm}' to pick which instance (assembly_get(include=['joint_origins']) "
                  "lists the qualified names).")


def jo_reference_names(design, jo, comp):
    """The resolvable reference string(s) for a JointOrigin: its BARE name on the root component,
    else '<occurrence fullPathName>:<name>' for EACH occurrence of its owning component - the
    qualified form disambiguates a name shared across components or instanced several times."""
    nm = safe(lambda: jo.name) or "?"
    root = safe(lambda: design.rootComponent)
    # `is True`: an owner proven to be the root is reachable by the bare name. An unproven one takes
    # the occurrence walk, which prints the qualified spellings that exist and falls back to the bare
    # name when none do - so no reference string is offered on an identity that did not read.
    if _common.same_component(comp, root) is True:
        return [nm]
    occs = list(safe(lambda: root.allOccurrencesByComponent(comp)) or []) if root else []
    out = [f"{safe(lambda o=o: o.fullPathName)}:{nm}" for o in occs if safe(lambda o=o: o.fullPathName)]
    return out or [nm]
