# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""The joint WRITE substrate: how one joint input is resolved (a find_geometry handle, an
autonomous '<occurrence>:<snap>', or a Joint Origin by name), the motion vocabulary the wire offers,
and the limit writer that reads every value back off the joint. It sits above _joints (which
_inputs itself imports) because a joint input resolves through the typed kinds."""

import math

import adsk.core

from ._common import safe
from . import _common
from . import _inputs
from ._joints import (AXES as _AXES, all_joint_origins, build_joint_geometry as _jg_from_entity,
                      is_joint_origin)

MAP_BLURB = (
    "the joint WRITE substrate above _joints: _resolve_input - the ONE joint-input resolve (a "
    "find_geometry handle, an autonomous '<occurrence>:<snap>', then a Joint Origin by name), with "
    "_parse_snap/_pick_face behind it; _JOINT_TYPES/_MOTIONS - the motion vocabulary every joint "
    "write surface offers; _apply_limits - the limit writer reading each value BACK off the joint")

# A joint input may be a find_geometry handle (resolved via the shared GeometryHandle kind, require=any
# since a joint can land on a face/edge/vertex/point). Not required at the kind level - a non-token spec
# (a JO name or a '<occ>:<snap>') just fails to resolve as a handle and falls through in _resolve_input.
_HANDLE = _inputs.GeometryHandle("input", require="any")

# A joint input may also be a JOINT ORIGIN, by the handle assembly_get(include=['joint_origins']) mints
# OR by name (bare when unique, else '<occurrence>:<JO name>'; ambiguity refused). This ONE kind is the
# resolve-one leaf over the shared _joints.all_joint_origins walk (the JO-name path).
_JO_REF = _inputs.JointOriginRef("input")

# joint_type -> (label, needs_axis). The setter is dispatched in _joints.apply_motion. pin_slot
# needs_axis is True for its ROTATION axis; its perpendicular SLIDE direction comes from
# 'slide_axis' (defaulting to the next frame axis).
_JOINT_TYPES = {
"rigid": ("rigid", False),
"revolute": ("revolute", True),
"slider": ("slider", True),
"cylindrical": ("cylindrical", True),
"planar": ("planar", True),
"ball": ("ball", False),
"pin_slot": ("pin_slot", True),
}

# The motion Choice for a joint WRITE surface: the shared six + pin_slot. A consumer imports THIS
# list rather than re-spelling it, so the wire vocabulary cannot drift per tool.
_MOTIONS = list(_inputs.JOINT_MOTIONS) + ["pin_slot"]


# The one honest note appended whenever a rest limit is set: a rest value is the joint LIMIT's
# equilibrium setpoint and does NOT move the static model, which stays at the joint's home value.
_REST_LIMIT_NOTE = (
    " NOTE: rest_mm/rest_deg set the joint LIMIT'S rest value (a motion-study equilibrium), which does "
    "NOT reposition the static model - it stays at the joint's home value. To POSE the mechanism use "
    "joint_drive (a driven pose does not survive recompute).")


def _available_joint_origins(design, limit=8):
    """The design's joint-origin names with where each lives, as (listed, overflow) - the
    self-correction data a resolve failure reports."""
    root_name = safe(lambda: design.rootComponent.name)
    found = []
    for jo, comp in all_joint_origins(design):
        nm = safe(lambda jo=jo: jo.name)
        if not nm:
            continue
        cname = safe(lambda comp=comp: comp.name)
        where = "root" if cname == root_name else f"in component '{cname}'"
        found.append(f"'{nm}' ({where})")
    return found[:limit], max(len(found) - limit, 0)


def _resolve_snap_entity(design, occ_name, snap):
    """Resolve an occurrence's geometry to a single PROXIED BRep entity in the occurrence's
    assembly context. Returns (entity, kind, error), kind being 'point' | 'planar' | 'cylinder'."""
    # The shared OccurrenceRef logic: an entityToken handle or a fullPathName/name, REFUSING an
    # ambiguous one instead of grabbing the first instance.
    occ, occ_err = _inputs._resolve_occurrence(occ_name, occ_name)
    if not occ:
        return None, None, occ_err

    if snap == "origin":
        op = safe(lambda: occ.component.originConstructionPoint)
        if not op:
            return None, None, f"'{occ_name}' has no origin construction point."
        pt = safe(lambda: op.createForAssemblyContext(occ)) or op
        return pt, "point", None

    body = safe(lambda: occ.component.bRepBodies.item(0))
    if not body:
        return None, None, f"'{occ_name}' has no body to snap to."
    faces = safe(lambda: body.faces)
    if not faces or safe(lambda: faces.count, 0) == 0:
        return None, None, f"'{occ_name}' body has no faces."

    if snap == "cylinder":
        # The enum MEMBER, never its integer: CylinderSurfaceType is 1 while 3 is
        # SphereSurfaceType, so a hand-typed 3 matches spheres. Cones count too.
        want = (adsk.core.SurfaceTypes.CylinderSurfaceType, adsk.core.SurfaceTypes.ConeSurfaceType)
        cyl = None
        for f in faces:
            if safe(lambda f=f: f.geometry.surfaceType, None) in want:
                cyl = f
                break
        if not cyl:
            return None, None, f"'{occ_name}' has no cylindrical face to snap to."
        proxy = safe(lambda: cyl.createForAssemblyContext(occ)) or cyl
        return proxy, "cylinder", None

    # top / bottom / center -> a planar face
    face = _pick_face(faces, snap)
    if not face:
        return None, None, f"Could not pick a '{snap}' face on '{occ_name}'."
    proxy = safe(lambda: face.createForAssemblyContext(occ)) or face
    return proxy, "planar", None


def _resolve_snap_input(design, occ_name, snap):
    """Build a JointGeometry from an occurrence's geometry, proxied into its assembly context.
    Returns (jointGeometry, error)."""
    entity, _kind, err = _resolve_snap_entity(design, occ_name, snap)
    if not entity:
        return None, err
    g, _label, gerr = _jg_from_entity(entity)
    return (g, None) if g else (None, gerr)


def _resolve_input(design, spec):
    """Resolve one joint input as (input_object, label, error), in order: a find_geometry handle
    (a JOINT ORIGIN handle is used directly, any other becomes a JointGeometry AT that geometry),
    a geometry snap '<occ>:<snap>', then a Joint Origin by name (bare or '<occ>:<JO name>')."""
    # A JO NAME is never a valid token, so it falls through to the name path below.
    ent, herr = _HANDLE.resolve(spec)
    if ent is not None:
        if is_joint_origin(ent):
            return ent, "handle:joint_origin", None
        g, label, gerr = _jg_from_entity(ent)
        return (g, f"handle:{label}", None) if g else (None, spec, gerr)
    # (2) geometry snap
    occ_name, snap = _parse_snap(spec)
    if snap:
        g, err = _resolve_snap_input(design, occ_name, snap)
        return g, f"{occ_name}:{snap}", err
    # (3) a Joint Origin by name - the ONE resolver (bare/qualified/ambiguity), shared with cam WCS bind.
    jo, jerr = _JO_REF.resolve(spec)
    if jo is not None:
        return jo, spec, None
    if jerr and "ambiguous" in jerr.lower():
        return None, spec, jerr        # surface the disambiguation candidates verbatim
    # (4) nothing matched - the comprehensive form-guide + the available JOs (self-correction data)
    msg = (f"'{spec}' is not a find_geometry handle, a Joint Origin (handle or name), or a recognized "
    "'<occurrence>:<snap>' spec (snap = origin/center/top/bottom/left/right/front/back/cylinder). "
    "A Joint Origin may be passed bare ('Center of Model') or scoped through the occurrence that "
    "carries it ('<occurrence>:<JO name>').")
    listed, more = _available_joint_origins(design)
    if listed:
        msg += " Joint Origins in this design: " + ", ".join(listed)
        msg += f" (+{more} more)." if more else "."
    return None, spec, msg


# Autonomous geometry "snaps": resolve a joint input from an occurrence's geometry - no human
# selection. An input string may be '<occurrence>:<snap>' where snap is one of these keywords.
_SNAP_KEYWORDS = ("origin", "center", "top", "bottom", "left", "right", "front", "back", "cylinder")


def _parse_snap(spec):
    """Split '<occurrence>:<snap>' into (occurrence_name, snap) when the trailing token is a known
    snap keyword, else (None, None). An occurrence name itself carries a ':<instance>', so only a
    FINAL snap keyword counts: 'Boom:1' is a plain name, 'Boom:1:top' is a snap."""
    s = (spec or "").strip()
    if ":" not in s:
        return None, None
    head, _, tail = s.rpartition(":")
    if head and tail.lower() in _SNAP_KEYWORDS:
        return head, tail.lower()
    return None, None


def _face_extent(face, axis):
    """Return (min, max) of a face's bounding box along axis 0/1/2 (x/y/z)."""
    bb = safe(lambda: face.boundingBox)
    if not bb:
        return 0.0, 0.0
    coord = ("x", "y", "z")[axis]
    return (safe(lambda: getattr(bb.minPoint, coord), 0.0),
            safe(lambda: getattr(bb.maxPoint, coord), 0.0))


def _is_planar(face):
    """Whether a face's surface is a plane - the only kind createByPlanarFace takes."""
    return (safe(lambda: face.geometry.surfaceType, None)
            == adsk.core.SurfaceTypes.PlaneSurfaceType)


# Directional snap -> (axis index, want_max). 'right/left' = +X/-X, 'back/front' = +Y/-Y,
# 'top/bottom' = +Z/-Z. The extreme PLANAR face along that axis is chosen.
_FACE_DIRECTIONS = {
"right": (0, True), "left": (0, False),
"back": (1, True), "front": (1, False),
"top": (2, True), "bottom": (2, False),
}


def _pick_face(faces, snap):
    """Choose a PLANAR face from a body by snap: a directional snap takes the extreme planar face
    along that world axis, 'center' the largest-area one. Only PLANAR faces are considered, since
    createByPlanarFace rejects the rest. Returns the face or None."""
    if snap in _FACE_DIRECTIONS:
        axis, want_max = _FACE_DIRECTIONS[snap]
        # Rank by the face's NEAR coordinate: the extreme face LIES IN the extreme plane, while a
        # side wall merely REACHES it and spans inward. For 'max' the face whose MIN is greatest;
        # for 'min' the face whose MAX is least - the cap, not a wall touching the extreme.
        best, best_v = None, None
        for f in faces:
            if not _is_planar(f):
                continue
            mn, mx = _face_extent(f, axis)
            v = mn if want_max else mx
            if best_v is None or (v > best_v if want_max else v < best_v):
                best_v, best = v, f
        return best
    # center -> largest planar face
    best, best_area = None, -1.0
    for f in faces:
        if not _is_planar(f):
            continue
        a = safe(lambda f=f: f.area, 0.0) or 0.0
        if a > best_area:
            best_area, best = a, f
    return best


def _slide_index(slide_axis, ax_name):
    """Resolve the pin_slot SLIDE direction index. Blank -> the next frame axis after the rotation
    axis (guaranteed distinct). Returns (slide_idx_or_None, error_or_None); slide_idx None means 'use
    the default in apply_motion'."""
    s = (slide_axis or "").strip().lower()
    if not s:
        return None, None
    if s not in _AXES:
        return None, f"Unknown slide_axis '{slide_axis}'. Valid: x, y, z."
    if _AXES[s] == _AXES[ax_name]:
        return None, "For pin_slot, 'slide_axis' must differ from 'axis' (the rotation axis)."
    return _AXES[s], None


def _slide_name(slide_idx, ax_name):
    """Human name of the effective pin_slot slide axis for the response."""
    idx = slide_idx if slide_idx is not None else (_AXES[ax_name] + 1) % 3
    return ("x", "y", "z")[idx]


# The band a read-back may differ from the request by, in the REQUEST'S OWN units. A limit and a
# joint offset/angle each make a units round trip in each direction, so the read-back is converted
# back and compared there.
_LIMIT_BAND = 1e-3

# native -> degrees, for the radians a rotation limit stores.
_DEG_PER_RAD = 180.0 / math.pi

# (payload key, enabled-flag property, value property) per limit, in the order they are applied.
_ROT_LIMITS = (("min_deg", "isMinimumValueEnabled", "minimumValue"),
               ("max_deg", "isMaximumValueEnabled", "maximumValue"),
               ("rest_deg", "isRestValueEnabled", "restValue"))
_LIN_LIMITS = (("min_mm", "isMinimumValueEnabled", "minimumValue"),
               ("max_mm", "isMaximumValueEnabled", "maximumValue"),
               ("rest_mm", "isRestValueEnabled", "restValue"))


def _unverified_limits_note(keys):
    """The one sentence a payload appends for limits whose read-back could not be taken."""
    return (f" Limits published null ({', '.join(keys)}) - each was assigned but could not be read "
            "back off the joint, so whether it TOOK is UNKNOWN here (it is not a 'yes'). Read the "
            "joint's limits with assembly_get before relying on them.")


def _set_one_limit(limits, flag_prop, value_prop, key, wanted, native, unit_scale):
    """Enable and assign ONE joint limit, then read BOTH back off the live JointLimits. `native` is
    the value in the API's own units and `unit_scale` converts a read back into the caller's, where
    the compare happens within _LIMIT_BAND. Returns (published read-back or None, error)."""
    try:
        setattr(limits, flag_prop, True)
        setattr(limits, value_prop, native)
    except Exception as e:
        return None, f"Setting {key} raised: {e}."
    enabled = _common.read_flag(lambda: getattr(limits, flag_prop))
    if enabled is False:
        return None, (f"{key} did not take - the joint reads {flag_prop} back as false, so that "
                      "limit is not in force.")
    landed = _common.measured(lambda: getattr(limits, value_prop), scale=unit_scale, places=9)
    if landed is None or enabled is None:
        return None, None
    if abs(landed - float(wanted)) > _LIMIT_BAND:
        return None, (f"{key} did not take - {wanted} was requested and the joint reads back "
                      f"{landed} in the same units.")
    return landed, None


def _apply_limits(motion, *, min_deg=None, max_deg=None, rest_deg=None,
                  min_mm=None, max_mm=None, rest_mm=None, cm_scale=0.1):
    """Apply rotation and/or slide limits to a JointMotion, reading each one BACK off the joint.
    Returns (changed as the JOINT reads it, the limits whose read-back could not be taken, error);
    an error leaves `changed` holding the limits that landed before it."""
    changed, unverified = {}, []

    # Inverted-pair guard: a min above its max creates an EMPTY feasible range that makes the joint
    # undrivable while every health field keeps reading healthy.
    if min_deg is not None and max_deg is not None and float(min_deg) > float(max_deg):
        return changed, unverified, (
            f"Rotation limits are INVERTED: min_deg ({min_deg}) > max_deg ({max_deg}) "
            "- an empty feasible range silently makes the joint undrivable. Swap them.")
    if min_mm is not None and max_mm is not None and float(min_mm) > float(max_mm):
        return changed, unverified, (
            f"Slide limits are INVERTED: min_mm ({min_mm}) > max_mm ({max_mm}) - an "
            "empty feasible range silently makes the joint undrivable. Swap them.")

    want_rot = any(v is not None for v in (min_deg, max_deg, rest_deg))
    want_lin = any(v is not None for v in (min_mm, max_mm, rest_mm))

    if want_rot:
        rl = safe(lambda: motion.rotationLimits)
        if rl is None:
            return changed, unverified, ("This joint's motion has no ROTATION limits "
    "(min_deg/max_deg/rest_deg need a revolute or cylindrical joint).")
        for (key, flag_prop, value_prop), wanted in zip(_ROT_LIMITS,
                                                        (min_deg, max_deg, rest_deg)):
            if wanted is None:
                continue
            published, lerr = _set_one_limit(rl, flag_prop, value_prop, key, wanted,
                                             math.radians(float(wanted)), _DEG_PER_RAD)
            if lerr:
                return changed, unverified, lerr
            changed[key] = published
            if published is None:
                unverified.append(key)

    if want_lin:
        sl = safe(lambda: motion.slideLimits)
        if sl is None:
            return changed, unverified, ("This joint's motion has no LINEAR/slide limits "
    "(min_mm/max_mm/rest_mm need a slider or cylindrical joint).")
        for (key, flag_prop, value_prop), wanted in zip(_LIN_LIMITS, (min_mm, max_mm, rest_mm)):
            if wanted is None:
                continue
            published, lerr = _set_one_limit(sl, flag_prop, value_prop, key, wanted,
                                             float(wanted) * cm_scale, 1.0 / cm_scale)
            if lerr:
                return changed, unverified, lerr
            changed[key] = published
            if published is None:
                unverified.append(key)

    return changed, unverified, None
