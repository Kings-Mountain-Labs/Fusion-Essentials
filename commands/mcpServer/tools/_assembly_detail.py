# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Row serializers assembly_get's payload is built from: occurrence, joint, joint-origin, relation
and contact rows. Read-only, and not a separately-registered tool."""

import math

from ._common import safe
from . import _assert
from . import _common
from . import _contacts
from . import _geom
from . import _inputs
from . import _joints
from . import _relations

# The "what to reuse from here" catalog line for the generated CLAUDE.md helper map.
MAP_BLURB = (
    "the row SERIALIZERS behind assembly_get, one per array in its payload - reach for one only "
    "from there. _occ_record/_all_occurrence_rows - occurrence identity, placement behind "
    "with_pose; "
    "_health_fields - the compute-state verdict; _joint_frame/_limit_facts/_value_now/_motion_axes "
    "- a joint's world frame, limits, value and heading; _joint_origin_rows/_relation_rows/"
    "_contact_rows - the rest")


def _occ_record(occ, inv_k, occ_joints, include_joints, full_path=False, with_pose=False):
    """ONE occurrence row: identity + ground flags + body count + the joints it takes part in.
    with_pose adds its world placement (_geom.occ_world_frame - origin, basis axes, bbox), the
    ~40-line half of the row; full_path adds the fullPathName two same-named instances differ by."""
    name = safe(lambda: occ.name)
    rec = {
    "name": name,
    "component": safe(lambda: occ.component.name),
    "grounded": bool(safe(lambda: occ.isGrounded, False)),
    "ground_to_parent": bool(safe(lambda: occ.isGroundToParent, False)),
    "body_count": safe(lambda: occ.bRepBodies.count, 0),
    }
    if full_path:
        rec["full_path"] = safe(lambda: occ.fullPathName)
    if with_pose:
        rec.update(_geom.occ_world_frame(occ, inv_k))
    if include_joints:
        rec["joints"] = occ_joints.get(name, [])
    return rec


def _unresolved_row(broken):
    """One unresolved-reference row in the all_occurrences slice - no placement, bodies or joints,
    since every one of those reads RAISES on such an occurrence."""
    return {"name": broken["name"], "unresolved": True, "parent_path": broken["parent_path"],
            "detail": broken["detail"]}


def _all_occurrence_rows(walk, inv_k, cap, occ_joints, include_joints, with_pose=False):
    """The all_occurrences slice: rows for the usable occurrences, then one flagged row per
    unresolved reference, bounded by cap. Returns (rows, walk)."""
    rows = [_occ_record(o, inv_k, occ_joints, include_joints, full_path=True, with_pose=with_pose)
            for o in walk.occurrences[:cap]]
    for b in walk.broken[:max(0, cap - len(rows))]:
        rows.append(_unresolved_row(b))
    return rows, walk


def _health(obj):
    """(healthy: True / False / None, message) for one entity's compute state, over the shared
    _assert.compute_state. None is the verdict only when NEITHER the entity nor its timeline item
    answers a state, so a caller branches on `is False` / `is True`, never on truthiness."""
    # An AsBuiltJoint carries neither healthState nor errorOrWarningMessage (AttributeError on
    # both) while its TimelineObject answers both - hence the paired read.
    state, failure = _assert.compute_state(obj)
    if state == "broken":
        _label, msg = failure
        return False, (msg or "compute failed / warning")
    return (True if state == "healthy" else None), None


def _health_fields(obj):
    """The row fields stating one entity's compute health: {'healthy': True}, {'healthy': False,
    'error': msg}, or {'health_unknown': True} - the last WITHOUT a healthy key, so a row whose
    state never read cannot be mistaken for one that read fine."""
    healthy, msg = _health(obj)
    if healthy is None:
        return {"health_unknown": True}
    if healthy:
        return {"healthy": True}
    return {"healthy": False, "error": msg}


def _limit_facts(lims, to_out):
    """The ENABLED bounds of one JointLimits as {min/max/rest}, converted by to_out; {} when none
    are enabled or the limits object is absent."""
    if lims is None:
        return {}
    out = {}
    for flag, member, key in (("isMinimumValueEnabled", "minimumValue", "min"),
                              ("isMaximumValueEnabled", "maximumValue", "max"),
                              ("isRestValueEnabled", "restValue", "rest")):
        if _common.read_flag(lambda m=flag: getattr(lims, m)):
            v = _common.measured(lambda m=member: getattr(lims, m))
            if v is not None:
                out[key] = round(to_out(v), 4)
    return out


# JointMotion attribute -> wire key + the conversion out of Fusion's internal units (radians for a
# rotation, cm for a slide). A motion class exposes only the values ITS degrees of freedom have, so
# an absent attribute is that joint kind not carrying that DOF, not a failed read.
_VALUE_NOW = (("rotationValue", "angle_deg", math.degrees),
              ("slideValue", "slide_mm", lambda cm: cm * 10.0),
              ("primarySlideValue", "slide_primary_mm", lambda cm: cm * 10.0),
              ("secondarySlideValue", "slide_secondary_mm", lambda cm: cm * 10.0))


def _value_now(j):
    """The joint's CURRENT driven value(s) in the same units as its limits ({angle_deg} revolute,
    {slide_mm} slider, all three planar), or None when the motion carries no driven value."""
    jm = safe(lambda: j.jointMotion)
    if jm is None:
        return None
    out = {}
    for attr, key, conv in _VALUE_NOW:
        v = _common.measured(lambda a=attr: getattr(jm, a))
        if v is not None:
            out[key] = round(conv(v), 4)
    return out or None


# Wire key -> (the JointMotion member holding that heading, the joint kinds whose DOF has it). The
# kinds in NEITHER set - rigid, ball, planar, pin_slot - go unread: which heading members those
# motion classes expose is unmeasured, so their rows carry no direction.
_MOTION_AXES = (("rotation_axis", "rotationAxisVector", _joints.DRIVES_ANGLE),
                ("slide_direction", "slideDirectionVector", _joints.DRIVES_SLIDE))


def _motion_axes(j, kind):
    """The joint motion's own heading(s) for the DOF `kind` has: rotation_axis on a revolute or
    cylindrical, slide_direction on a slider, {} otherwise - the measured CylindricalJointMotion
    exposes no slideDirectionVector. A heading that answers nothing leaves its key OFF the row."""
    # Published exactly as the member answers, with NO placement lift: rotationAxisVector read the
    # WORLD axis on an as-built joint whose component was turned 90 deg, and the space
    # slideDirectionVector answers in is unmeasured - lifting either could turn it a second time.
    jm = safe(lambda: j.jointMotion)
    out = {}
    for key, attr, kinds in _MOTION_AXES:
        if kind not in kinds:
            continue
        v = _geom.axis_vec(safe(lambda a=attr: getattr(jm, a)))
        if v is not None:
            out[key] = v
    return out


def _jo_in_context(jo, occ):
    """A joint's JointOrigin reference as THAT HALF of the joint sees it, or None when the instance
    cannot be re-established."""
    # A joint's STORED reference reads assemblyContext None even when built from a
    # createForAssemblyContext proxy, and the context-stripped native answers the FIRST placement's
    # world point; the occurrence the joint names on this half puts the right instance back.
    if safe(lambda: jo.assemblyContext) is not None or occ is None:
        return jo
    return safe(lambda: jo.createForAssemblyContext(occ))


def _geometry_component(design, occ):
    """The component a JointGeometry's axis vectors are expressed in: the one `occ` places, or the
    ROOT when that side names no occurrence. None when the occurrence reads but its component does
    not, which leaves the axes unpublished rather than in an unknown frame."""
    if occ is None:
        return safe(lambda: design.rootComponent)
    return safe(lambda: occ.component)


def _geometry_frame(design, g, comp, occ, inv_k):
    """One JointGeometry's frame - {origin (display units), z_axis, x_axis, y_axis} - read through
    `occ`'s placement of `comp`, or None when the reference answers neither an origin nor an axis."""
    # A JointGeometry carries neither parentComponent nor assemblyContext, so the caller supplies
    # both. Its AXES are component-LOCAL and are lifted; its ORIGIN needs no lift, since the STORED
    # reference already holds the world point of the instance that reference names.
    z, x, y = _world_axes(design, g, comp, occ)
    o = safe(lambda: g.origin)
    origin = None
    if o is not None:
        c = [_common.measured(lambda ax=ax: getattr(o, ax), inv_k, 3) for ax in ("x", "y", "z")]
        origin = None if None in c else c
    if origin is None and not (z or x or y):
        return None
    return {"origin": origin, "z_axis": z, "x_axis": x, "y_axis": y}


def _as_built_source(j):
    """(the ONE JointGeometry an as-built joint holds, the occurrence it is read through, that
    occurrence's component), or None for a joint carrying no such geometry."""
    # An AsBuiltJoint exposes `geometry` and neither geometryOrOriginOne nor Two, a Joint the
    # reverse. The instance its axes belong to is named by the geometry's own entity's
    # assemblyContext, not by occurrenceOne, which reports the axes unlifted.
    g = safe(lambda: j.geometry)
    if g is None:
        return None
    ent = safe(lambda: g.entityOne)
    occ = safe(lambda: ent.assemblyContext)
    comp = safe(lambda: occ.component) if occ is not None else _inputs.entity_component(ent)
    return g, occ, comp


def _joint_frame(design, j, inv_k):
    """The joint's own frame - {origin (display units), z_axis, x_axis, y_axis} - from
    geometryOrOriginOne, falling back to geometryOrOriginTwo, and None when neither reads; an
    as-built joint answers off its single `geometry`. Z is primaryAxisVector, the direction the
    joint's OFFSET drives along, and every axis is lifted to WORLD by _world_axes."""
    as_built = _as_built_source(j)
    if as_built is not None:
        g, occ, comp = as_built
        return _geometry_frame(design, g, comp, occ, inv_k)
    for attr, occ_attr in (("geometryOrOriginOne", "occurrenceOne"),
                           ("geometryOrOriginTwo", "occurrenceTwo")):
        g = safe(lambda a=attr: getattr(j, a))
        if g is None:
            continue
        # The occurrence THIS half is anchored to - the joint's own statement of which instance the
        # half stands for, and the placement both frame kinds are read through.
        occ = safe(lambda a=occ_attr: getattr(j, a))
        if not _joints.is_joint_origin(g):
            frame = _geometry_frame(design, g, _geometry_component(design, occ), occ, inv_k)
            if frame is None:
                continue
            return frame
        g = _jo_in_context(g, occ)
        if g is None:
            continue
        z, x, y = _world_axes(design, g, safe(lambda g=g: g.parentComponent), occ)
        # A JointOrigin carries the three axis vectors but NO origin of its own - its position is
        # the base anchor plus offsetX/Y/Z, which _jo_world_origin assembles. That position is an
        # INSTANCE read, so it stands or falls with the axes.
        if not (z or x or y):
            continue
        return {"origin": _jo_world_origin(g, x, y, z, inv_k),
                "z_axis": z, "x_axis": x, "y_axis": y}
    return None


# --- joint_origins slice: each Joint Origin (a reusable WCS frame) as a referenceable, handle-bearing row ---

# Per-rigid-group / per-contact-set member preview; the row's own count carries the rest.
_MEMBER_CAP = 12


def _jo_consumers(design):
    """{JointOrigin name: [joint names]} - which joints CONSUME each JO as an input, matched by
    name over the shared joint walk. Best-effort: never raises."""
    out = {}
    for j in _joints.all_joints(design):
        jname = safe(lambda j=j: j.name)
        if not jname:
            continue
        for attr in ("geometryOrOriginOne", "geometryOrOriginTwo"):
            ref = safe(lambda j=j, a=attr: getattr(j, a))
            if ref is not None and _joints.is_joint_origin(ref):
                rn = safe(lambda ref=ref: ref.name)
                if rn:
                    out.setdefault(rn, []).append(jname)
    return out


def _jo_instances(design, jo, comp):
    """Yield (reference_name, jo_in_context) per INSTANCE of a JointOrigin: the native JO under its
    bare name for a root JO, else the assembly-context proxy per occurrence under
    '<occurrence>:<name>'."""
    root = safe(lambda: design.rootComponent)
    nm = safe(lambda: jo.name) or "?"
    # same_component, not `is` or a name compare: component wrappers are never identity-stable, and
    # a name test calls a sub-component sharing the root's name the root. `is True` only - an
    # unproven owner takes the occurrence walk below.
    if _common.same_component(comp, root) is True:
        yield nm, jo
        return
    occs = list(safe(lambda: root.allOccurrencesByComponent(comp)) or []) if root else []
    if not occs:
        yield nm, jo
        return
    for o in occs:
        fp = safe(lambda o=o: o.fullPathName)
        proxy = safe(lambda o=o: jo.createForAssemblyContext(o)) or jo
        yield (f"{fp}:{nm}" if fp else nm), proxy


def _world_axes(design, frame, comp, context_occ):
    """A joint frame's (Z, X, Y) axis vectors in WORLD, each an [x,y,z] unit list or None -
    `context_occ` is the instance whose placement is applied."""
    # A JointOrigin AND a JointGeometry both report their axis vectors in the OWNING COMPONENT's
    # frame, so a world heading holds only once that placement is applied. Only DIRECTIONS are
    # transformed, so the placement's translation never enters.
    m = _joints.component_world_matrix(design, comp, context_occ)
    out = []
    for attr in ("primaryAxisVector", "secondaryAxisVector", "thirdAxisVector"):
        moved = safe(lambda a=attr: getattr(frame, a).copy()) if m is not None else None
        if moved is None or not safe(lambda mv=moved: mv.transformBy(m)):
            out.append(None)
            continue
        out.append(_geom.axis_vec(moved))
    return out[0], out[1], out[2]


def _jo_world_origin(jo, xa, ya, za, inv_k):
    """The JO frame's world origin (units-scaled): the base geometry origin PLUS its offsetX/Y/Z
    parameters projected along the frame's X/Y/Z axes; None when a NONZERO offset has no world axis
    to run along."""
    # A coordinate-anchored JO carries its position in those offsets, geometry.origin staying at
    # the base anchor, so geometry.origin alone under-reports. That origin reads in WORLD, so the
    # axes handed in must be world too or the sum mixes two frames.
    o = safe(lambda: jo.geometry.origin)
    if o is None:
        return None
    ox, oy, oz = safe(lambda: o.x, 0.0), safe(lambda: o.y, 0.0), safe(lambda: o.z, 0.0)
    dx = safe(lambda: jo.offsetX.value, 0.0) or 0.0     # cm along the frame X (secondary axis)
    dy = safe(lambda: jo.offsetY.value, 0.0) or 0.0     # cm along the frame Y (third axis)
    dz = safe(lambda: jo.offsetZ.value, 0.0) or 0.0     # cm along the frame Z (primary axis)
    if any(d and axis is None for d, axis in ((dx, xa), (dy, ya), (dz, za))):
        return None
    # Every offset with no axis is ZERO by the guard above, so a zero vector contributes nothing
    # and no basis is invented for a direction nobody read.
    xa = xa or [0.0, 0.0, 0.0]
    ya = ya or [0.0, 0.0, 0.0]
    za = za or [0.0, 0.0, 0.0]
    wx = ox + dx * xa[0] + dy * ya[0] + dz * za[0]
    wy = oy + dx * xa[1] + dy * ya[1] + dz * za[1]
    wz = oz + dx * xa[2] + dy * ya[2] + dz * za[2]
    return [round(wx * inv_k, 3), round(wy * inv_k, 3), round(wz * inv_k, 3)]


def _jo_row(design, jo, ref, comp, inv_k, consumers):
    """One joint_origins row: name, the qualified reference (feed to joint_create /
    joint_at_geometry / cam_edit_setup wcs), owning component, world position and frame axes (both
    absent when no placement answers), the joints that consume it, and an entityToken handle."""
    nm = safe(lambda: jo.name)
    row = {"name": nm, "qualified_name": ref, "component": safe(lambda: comp.name)}
    z, x, y = _world_axes(design, jo, comp, safe(lambda: jo.assemblyContext))
    wp = _jo_world_origin(jo, x, y, z, inv_k)
    if wp is not None:
        row["world_position"] = wp
    if z or x or y:
        row["frame"] = {"z_axis": z, "x_axis": x, "y_axis": y}
    row["consumed_by"] = consumers.get(nm, [])
    tok = safe(lambda: jo.entityToken)
    if tok:
        row["handle"] = tok
    return row


def _joint_origin_rows(design, inv_k, cap):
    """The joint_origins slice: a row per JointOrigin instance over the ONE JO walk
    (_joints.all_joint_origins). Bounded by cap; returns (rows, total)."""
    consumers = _jo_consumers(design)
    rows, total = [], 0
    for jo, comp in _joints.all_joint_origins(design):
        for ref, ctx_jo in _jo_instances(design, jo, comp):
            total += 1
            if len(rows) < cap:
                rows.append(_jo_row(design, ctx_jo, ref, comp, inv_k, consumers))
    return rows, total


# --- relations slice: the maintained assembly relationships, each editable by name ---
# Rigid groups, motion links and assembly constraints are three SEPARATE collections - they are
# not joints, so the joint walk above never sees them.

def _rigid_group_row(rg, comp):
    """One rigid group. The member list is PREVIEWED to _MEMBER_CAP; occurrence_count is the true
    total and occurrences_truncated marks the row, so a capped preview is never silent."""
    members, total = _relations.rigid_group_members(rg, _MEMBER_CAP)
    return {"name": safe(lambda: rg.name), "component": safe(lambda: comp.name),
            "occurrences": members, "occurrence_count": total,
            "occurrences_truncated": total > len(members),
            "suppressed": bool(safe(lambda: rg.isSuppressed, False))}


def _motion_link_row(ml, comp):
    """One motion link: the two joints it couples and the coupling itself. joint_two is null for a
    link between two DOF of the SAME joint (the API returns null there - it is not a read failure).
    value_one/value_two are the link's own ModelParameters in Fusion's internal units (cm / radians);
    their RATIO is what the coupling means."""
    row = {"name": safe(lambda: ml.name), "component": safe(lambda: comp.name),
           "joint_one": safe(lambda: ml.jointOne.name),
           "joint_two": safe(lambda: ml.jointTwo.name),
           "value_one": safe(lambda: ml.valueOne.value),
           "value_two": safe(lambda: ml.valueTwo.value),
           "reversed": bool(safe(lambda: ml.isReversed, False)),
           "suppressed": bool(safe(lambda: ml.isSuppressed, False))}
    row.update(_health_fields(ml))
    return row


def _constraint_row(con, comp):
    row = {"name": safe(lambda: con.name), "component": safe(lambda: comp.name),
           "relationship_count": safe(lambda: con.geometricRelationships.count, 0),
           "suppressed": bool(safe(lambda: con.isSuppressed, False))}
    row.update(_health_fields(con))
    return row


_RELATION_ROWS = (("rigid_groups", "rigid_group", _rigid_group_row),
                  ("motion_links", "motion_link", _motion_link_row),
                  ("constraints", "constraint", _constraint_row))


def _relation_rows(design, cap):
    """The relations slice over the ONE relations walk (_relations.all_relations): ({key: rows},
    {key: total}) for the three kinds, each list bounded by cap."""
    rows, totals = {}, {}
    for key, kind, build in _RELATION_ROWS:
        pairs = _relations.all_relations(design, kind)
        totals[key] = len(pairs)
        rows[key] = [build(obj, comp) for obj, comp in pairs[:cap]]
    return rows, totals


# --- contacts slice: the design's contact sets + the two flags that decide whether they do anything ---
# Contact sets hang off the DESIGN, not a component, so the relations walk never sees them. A
# ContactSet has no entityToken and no healthState, so a row carries neither.

def _contact_row(cs):
    """One contact set: members PREVIEWED to _MEMBER_CAP, member_count the true total, and
    members_unreadable the count carrying no readable name - or true when the member list did not
    read at all, where member_count is null rather than a zero that would read as EMPTY."""
    names, total, unnamed = _contacts.membership(cs, _MEMBER_CAP)
    row = {"name": safe(lambda: cs.name),
           "suppressed": bool(safe(lambda: cs.isSuppressed, False))}
    if total is None:
        row["member_count"] = None
        row["members_unreadable"] = True
        return row
    row["members"] = names
    row["member_count"] = total
    row["members_truncated"] = total > len(names) + unnamed
    if unnamed:
        row["members_unreadable"] = unnamed
    return row


def _contact_rows(design, cap):
    """The contacts slice over the ONE design-scoped contact-set walk: (rows, total), bounded by cap."""
    sets = _contacts.all_contact_sets(design)
    return [_contact_row(cs) for cs in sets[:cap]], len(sets)


def _contact_analysis(design):
    """Both design-level flags - a contact set takes part only when analysis is ENABLED and the
    scope is the contact sets. Either key is null when its flag cannot be read."""
    enabled = safe(lambda: design.isContactAnalysisEnabled)
    use_sets = safe(lambda: design.isContactSetAnalysis)
    return {"enabled": (None if enabled is None else bool(enabled)),
            "scope": (None if use_sets is None else
                      ("contact_sets" if use_sets else "all_bodies"))}
