# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""The measurement math the geometry reads share: direction vectors, the bodies-only AABB, the
before/after effect reads a write is verified with, and the nested-child disclosure a measure
publishes."""

import math

import adsk.core
import adsk.fusion

from ._common import counted, safe
from . import _common

MAP_BLURB = (
    "the geometry reads' measurement math: unit_vector/unit_vector_between/"
    "evaluator_normal_at/dot/cross (vector math); body_aabb/occ_world_frame/axis_vec "
    "(bodies-only AABB, placement); volumes/volume_delta/signed_volume/face_counts/"
    "face_count_delta/lump_count/aabb_gap/parallel_plane_facts (the effect reads a write is "
    "judged by); address/subtree_facts (a measure row's name, nested children)")

# boundingBox2's body types: the plain .boundingBox also counts the VISIBLE sketch and construction
# datums, so an orphaned shown plane inflates it (measured: an occurrence's Z read 4x).
_BODY_BBOX_TYPES = (adsk.fusion.BoundingBoxEntityTypes.SolidBRepBodyBoundingBoxEntityType
                    | adsk.fusion.BoundingBoxEntityTypes.SurfaceBodyBoundingBoxEntityType
                    | adsk.fusion.BoundingBoxEntityTypes.MeshBodyBoundingBoxEntityType)


def body_aabb(entity):
    """The world AABB of an entity counting only its BODIES (solid+surface+mesh), or None."""
    # An Occurrence/Component exposes boundingBox2(entityTypes); a BRepBody has none, and its own
    # .boundingBox is already body-only.
    bb2 = safe(lambda: entity.boundingBox2)   # a bound method on Occurrence/Component; absent on a body
    if callable(bb2):
        return safe(lambda: bb2(_BODY_BBOX_TYPES))
    return safe(lambda: entity.boundingBox)


def _coords(p):
    """A Point3D/Vector3D's (x, y, z) as read, or None when ANY component is not a number."""
    # bool is excluded ahead of the number test: it is an int subclass, so True would pass as 1.
    if p is None:
        return None
    xyz = (safe(lambda: p.x), safe(lambda: p.y), safe(lambda: p.z))
    if not all(isinstance(c, (int, float)) and not isinstance(c, bool) for c in xyz):
        return None
    return xyz


def axis_vec(v):
    """A basis axis Vector3D as [x,y,z] rounded to 4dp - dimensionless, so never unit-scaled."""
    c = _coords(v)
    return None if c is None else [round(c[0], 4), round(c[1], 4), round(c[2], 4)]


def occ_world_frame(occ, inv_k):
    """One occurrence's world placement: origin, the transform's three basis axes, and the
    bodies-only bbox center/size - lengths scaled by inv_k, a key omitted where a read failed.
    Directions are dimensionless, so - unlike origin - they are NOT unit-scaled."""
    out = {}
    m = safe(lambda: occ.transform2)
    t = _coords(safe(lambda: m.translation)) if m is not None else None
    if t is not None:
        out["origin"] = [round(t[0] * inv_k, 3), round(t[1] * inv_k, 3), round(t[2] * inv_k, 3)]
    if m is not None:
        # getAsCoordinateSystem returns (origin, xAxis, yAxis, zAxis) in Python.
        cs = safe(lambda: m.getAsCoordinateSystem())
        if isinstance(cs, (list, tuple)) and len(cs) == 4:
            for key, vec in (("x_axis", cs[1]), ("y_axis", cs[2]), ("z_axis", cs[3])):
                av = axis_vec(vec)
                if av is not None:
                    out[key] = av
    extents = _aabb_extents(occ)
    if extents is not None:
        out["bbox_center"] = [round((lo + hi) / 2 * inv_k, 3) for lo, hi in extents]
        out["bbox_size"] = [round((hi - lo) * inv_k, 3) for lo, hi in extents]
    return out


def owning_bodies(entities):
    """The distinct BRepBodies owning `entities` (faces OR edges), in first-seen order, deduped on
    ``_common.native_identity``; an entity whose body will not read is skipped."""
    # Neither other key works: face.body/edge.body mint a FRESH PROXY per read (measured), so id()
    # splits one body into N, while the wrapper's own entityToken is document-local (measured) and
    # merges two distinct bodies reached through two x-refs. `or id(b)` over-counts rather than merges.
    bodies, seen = [], set()
    for f in entities:
        b = safe(lambda f=f: f.body)
        if b is None:
            continue
        key = _common.native_identity(b) or id(b)
        if key not in seen:
            seen.add(key)
            bodies.append(b)
    return bodies


def volumes(bodies):
    """{id(body): volume-or-None} - the pre/post sample a material-changing feature compares."""
    # PRECONDITION: the caller holds the SAME body objects across the before/after pair. Re-reading
    # the bodies off the API between the two calls mints fresh wrappers with new ids, and every
    # entry then reads as unreadable.
    return {id(b): safe(lambda b=b: b.volume) for b in bodies}


def volume_delta(bodies, before):
    """(total cm3 moved, readable) between `before` (from volumes()) and the bodies' volumes NOW -
    readable is False when no body's volume read at both ends, so a real zero is not an
    unmeasurable one."""
    after = volumes(bodies)
    delta, readable = 0.0, False
    for b in bodies:
        vb, va = before.get(id(b)), after.get(id(b))
        if isinstance(vb, (int, float)) and isinstance(va, (int, float)):
            readable = True
            delta += (va - vb)
    return delta, readable


def signed_volume(body):
    """ONE body's signed volume in internal cm3, or None (never 0.0) when it will not read - for a
    feature judged on the SIGN: a mesh whose normals were reversed reports the opposite sign."""
    v = volumes([body]).get(id(body))
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def face_counts(bodies):
    """{id(body): face-count-or-None} - the pre/post sample a TOPOLOGY-changing feature compares,
    where volume does not move (a healed face delete measured 7 -> 6 faces). Same id()
    precondition as volumes()."""
    return {id(b): safe(lambda b=b: b.faces.count) for b in bodies}


def face_count_delta(bodies, before):
    """(total faces gained/lost, readable) between `before` (from face_counts()) and the bodies NOW
    - readable False when no body's count read at both ends. Mirrors volume_delta."""
    after = face_counts(bodies)
    delta, readable = 0, False
    for b in bodies:
        cb, ca = before.get(id(b)), after.get(id(b))
        if isinstance(cb, int) and isinstance(ca, int):
            readable = True
            delta += (ca - cb)
    return delta, readable


def lump_count(body):
    """ONE body's LUMP count - its DISCONNECTED solid pieces - or None; a MeshBody carries no
    ``lumps``. The read a JOIN is verified with: a result still holding its inputs' lumps between
    them fused NOTHING, which a body count and a volume both report as a clean combine."""
    return counted(lambda: body.lumps.count)


def _aabb_extents(entity):
    """[(min, max)] per axis of an entity's body AABB, or None when any coordinate is unreadable."""
    box = body_aabb(entity)
    lo, hi = safe(lambda: box.minPoint), safe(lambda: box.maxPoint)
    out = []
    for axis in ("x", "y", "z"):
        a = safe(lambda ax=axis: getattr(lo, ax))
        b = safe(lambda ax=axis: getattr(hi, ax))
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (a, b)):
            return None
        out.append((a, b))
    return out


def _same_space(first, second):
    """True when two body references are expressed in the SAME coordinate space, so their AABBs can
    be subtracted."""
    # An occurrence PROXY's box is in root space and its native's is component-LOCAL, so the pair
    # must agree on both the assembly context and the owning component. same_component is TRI-STATE:
    # only a proven True licenses the measurement.
    ctx_a = safe(lambda: first.assemblyContext)
    ctx_b = safe(lambda: second.assemblyContext)
    if (ctx_a is None) != (ctx_b is None):
        return False
    if ctx_a is not None and _common.same_component(
            safe(lambda: ctx_a.component), safe(lambda: ctx_b.component)) is not True:
        return False
    own_a, own_b = safe(lambda: first.parentComponent), safe(lambda: second.parentComponent)
    if own_a is None or own_b is None:
        return ctx_a is None and ctx_b is None and own_a is None and own_b is None
    return _common.same_component(own_a, own_b) is True


def _box_axis_gaps(first, second):
    """The per-axis signed gap in cm between two entities' body AABBs as [x, y, z], or None:
    positive is apart on that axis, zero or negative overlaps. Each caller owns the one-coordinate-
    space precondition."""
    a, b = _aabb_extents(first), _aabb_extents(second)
    if a is None or b is None:
        return None
    return [max(a[i][0] - b[i][1], b[i][0] - a[i][1]) for i in range(3)]


def aabb_gap(first, second):
    """The largest per-axis GAP in cm between two entities' body AABBs - a LOWER BOUND on the
    clearance that PROVES two bodies cannot touch when positive, and proves nothing when zero or
    negative. None for an unreadable box, or two references in different coordinate spaces."""
    if not _same_space(first, second):
        return None
    gaps = _box_axis_gaps(first, second)
    return None if gaps is None else max(gaps)


# ── the bounded gap between two PARALLEL PLANAR faces ────────────────────────────────────────────

# Degrees off 0 or 180 the two normals may be and still count as parallel: at 0.01 deg two faces
# 100 mm across vary by under 0.02 mm along their span.
PARALLEL_PLANE_TOL_DEG = 0.01

# The band two cm lengths count as one number in - 1e-6 cm is 10 nanometres, under any length a
# design expresses and over double-precision drift.
_SAME_LENGTH_CM = 1e-6


def _face_plane(face):
    """(origin cm, unit normal) of a PLANAR face's own plane, else (None, None)."""
    # MEASURED: the normal is NOT reliably outward - on a plate extruded +Z from an XY sketch both
    # faces parallel to the sketch plane read (0,0,+1), the bottom one included. Every comparison
    # against it is undirected (the abs() in parallel_plane_facts).
    g = safe(lambda: face.geometry)
    if g is None or safe(lambda: g.surfaceType) != adsk.core.SurfaceTypes.PlaneSurfaceType:
        return None, None
    origin = _coords(safe(lambda: g.origin))
    normal = unit_vector(safe(lambda: g.normal), decimals=12)
    if origin is None or normal is None:
        return None, None
    return origin, tuple(normal)


def _comparable_boxes(face_a, face_b):
    """True when two faces' bounding boxes can be subtracted - both in ONE coordinate space."""
    # MEASURED: a proxy face's plane AND its box both read ROOT space, so two proxies compare even
    # across different occurrences. A native reads its owning component's space, so two natives
    # compare only under one component; a mixed native/proxy pair is unmeasured and refused.
    ctx_a = safe(lambda: face_a.assemblyContext)
    ctx_b = safe(lambda: face_b.assemblyContext)
    if (ctx_a is None) != (ctx_b is None):
        return False
    if ctx_a is not None:
        return True
    own_a = safe(lambda: face_a.body.parentComponent)
    own_b = safe(lambda: face_b.body.parentComponent)
    if own_a is None or own_b is None:
        return False
    # `is True`: same_component answers None where an owner's identity did not read, and this gate
    # licenses a SUBTRACTION of two boxes - an unproven pair is refused like a mixed one.
    return _common.same_component(own_a, own_b) is True


def parallel_plane_facts(face_a, face_b, measured_cm, inv, units):
    """{separation_cm, distance_cm, bounded, plane_separation_only, lateral_offset_untested, note}
    for two PARALLEL PLANAR faces, beside the `measured_cm` a measurement API answered - or None
    unless both are planar, their normals parallel within PARALLEL_PLANE_TOL_DEG, and the plane
    separation computed here reproduces `measured_cm`."""
    if isinstance(measured_cm, bool) or not isinstance(measured_cm, (int, float)):
        return None
    origin_a, normal_a = _face_plane(face_a)
    origin_b, normal_b = _face_plane(face_b)
    if origin_a is None or origin_b is None:
        return None
    cosine = min(1.0, abs(sum(normal_a[i] * normal_b[i] for i in range(3))))
    if math.degrees(math.acos(cosine)) > PARALLEL_PLANE_TOL_DEG:
        return None
    separation = abs(sum((origin_b[i] - origin_a[i]) * normal_a[i] for i in range(3)))
    if abs(separation - measured_cm) > _SAME_LENGTH_CM:
        return None
    # _comparable_boxes, not _same_space: that guard refuses two proxies under DIFFERENT
    # occurrences, which is exactly the part-to-part pair a clearance read is made of.
    gaps = _box_axis_gaps(face_a, face_b) if _comparable_boxes(face_a, face_b) else None
    # Only the axes the boxes are actually APART on: a negative gap is an overlap along that axis
    # and contributes nothing to the distance between the boxes. No boxes to compare leaves `boxed`
    # None - an untested offset, which is not a tested offset of zero.
    boxed = math.sqrt(sum(g * g for g in gaps if g > 0.0)) if gaps is not None else None
    bounded = boxed is not None and boxed > measured_cm + _SAME_LENGTH_CM
    apart = f"planar faces on parallel planes {round(separation * inv, 6)} {units} apart"
    if bounded:
        note = (f"Both targets are {apart}. The measurement API answered with that plane "
                f"separation, and two bounded faces on those planes cannot be closer than "
                f"{round(boxed * inv, 6)} {units}, so the distance reported here is that LOWER "
                "BOUND - the gap is at least this and may be larger. closest_point_on_a/b are "
                "null here: the point pair the measurement API returned goes with its own number, "
                "not with this bound.")
    elif boxed is None:
        note = (f"Both targets are {apart}, and the distance reported here IS that plane "
                "separation. Their bounding boxes were NOT compared - one would not read, or the "
                "two are not expressed in one coordinate space - so nothing here was tested for a "
                "lateral offset between the faces. If they do not overlap where they face each "
                "other, the real gap is larger.")
    else:
        note = (f"Both targets are {apart}, and the distance reported here IS that plane "
                "separation. Their bounding boxes were compared and prove no larger gap - but "
                "boxes that overlap are not faces that meet, so if the two faces do not overlap "
                "where they face each other, the real gap is larger.")
    return {
        "separation_cm": separation,
        "distance_cm": boxed if bounded else measured_cm,
        "bounded": bounded,
        "plane_separation_only": not bounded,
        "lateral_offset_untested": boxed is None,
        "note": note,
    }


# ── the child occurrences nested inside a measurement target ─────────────────────────────────────

# How many child occurrences one disclosure names AND measures. The count published beside them is
# read off the collection itself, so a capped list still says how many there are.
SUBTREE_NAMES_MAX = 12


def address(entity, fallback="(unreadable name)"):
    """How a disclosure addresses an occurrence: its fullPathName (MEASURED to round-trip through a
    measure tool's target input), else its name, else `fallback`."""
    # An occurrence whose external reference is unresolved RAISES on fullPathName and still answers
    # name (measured, _common.broken_reference), so the row survives either read failing.
    return safe(lambda: entity.fullPathName) or safe(lambda: entity.name) or fallback


def _child_occurrences(entity, kind):
    """The collection of a measurement target's DIRECT child occurrences, or None - only an
    OCCURRENCE has any (measureMinimumDistance refuses a Component outright, and a body/face/edge
    holds none)."""
    return safe(lambda: entity.childOccurrences) if kind == "occurrence" else None


def _unresolved_children(entity):
    """How many of one occurrence's children hold an UNRESOLVED external reference."""
    # ``childOccurrences`` - where the named children come from - silently DROPS such a child while
    # the component-local collection still holds it, so the named list runs short by this many. An
    # unenumerable collection contributes nothing: it is not evidence of an unresolved child.
    comp = safe(lambda: entity.component)
    return sum(1 for child in _common.iter_collection(safe(lambda: comp.occurrences))
               if _common.broken_reference(child)[0])


def _child_gap(child, other, inv):
    """The measured gap from ONE child occurrence to the other target, scaled by `inv`, or None -
    never 0.0, which reads as TOUCHING (an occurrence with no bodies of its OWN raises here)."""
    mr, err = _common.min_distance(child, other)
    if err:
        return None
    value = safe(lambda: mr.value)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return round(value * inv, 6)


def _subtree_record(label, entity, kind, other_label, other, inv):
    """One target's {target, name, child_count, children, measured_against, children_truncated?,
    nested_deeper?, unresolved_children?} record - or None where there is nothing READ to disclose
    (no occurrences, none held, or a count that would not read)."""
    coll = _child_occurrences(entity, kind)
    if coll is None:
        return None
    total = counted(lambda: coll.count)
    if not total:
        return None
    children, deeper = [], False
    for child in _common.iter_collection(coll):
        children.append({"name": address(child), "distance": _child_gap(child, other, inv)})
        deeper = deeper or bool(counted(lambda c=child: c.childOccurrences.count))
        if len(children) >= SUBTREE_NAMES_MAX:
            break
    rec = {"target": label, "name": address(entity), "child_count": total,
           "children": children, "measured_against": other_label}
    if len(children) < total:
        rec["children_truncated"] = True
    if deeper:
        rec["nested_deeper"] = True
    unresolved = _unresolved_children(entity)
    if unresolved:
        rec["unresolved_children"] = unresolved
    return rec


def _subtree_clause(rec, units):
    """The half-sentence one record contributes to the disclosure note: the NEAREST child, and a
    pointer to ``targets_with_children`` for the rest."""
    n, kids = rec["child_count"], rec["children"]
    measured = [c for c in kids if c["distance"] is not None]
    listed, got = len(kids), len(measured)
    against = f"measured individually against target {rec['measured_against']} as named"
    # Counted over the children the TARGET holds, not the ones this list reached: measured live, a
    # capped list whose 12 measured children were far called 290 mm "the nearest" while a 5 mm child
    # sat outside the cap.
    unknown = n - got
    if got == 0:
        scope = (f"of which the first {listed} are listed, none of which measured"
                 if rec.get("children_truncated") else "none of which measured")
    elif got == listed:
        scope = (f"of which the first {listed} were {against}"
                 if rec.get("children_truncated") else against)
    else:
        scope = (f"of which the first {listed} are listed and {got} of those {against}"
                 if rec.get("children_truncated") else f"{got} of the {listed} listed {against}")
    if unknown and got:
        scope += (f" ({unknown} {'was' if unknown == 1 else 'were'} not measured, so a nearer "
                  "child is possible)")
    if measured:
        # min() keeps the FIRST of equal gaps - a tie has no nearer answer to pick.
        near = min(measured, key=lambda c: c["distance"])
        # The bare claim needs every child the TARGET holds measured, not every child listed.
        found = (f"; the nearest{'' if got == n else ' of those'} is {near['name']} at "
                 f"{near['distance']} {units}")
        if got > 1:
            found += " - every child gap is in targets_with_children"
    else:
        found = ""
    extra = ""
    if rec.get("unresolved_children"):
        extra += (f" - {rec['unresolved_children']} more hold an unresolved reference and are not "
                  "in that list")
    if rec.get("nested_deeper"):
        extra += " - and a named child holds children of its own, which were not measured"
    return (f"target {rec['target']} is occurrence '{rec['name']}' and holds {n} child "
            f"occurrence{'' if n == 1 else 's'}, {scope}{found}{extra}")


def subtree_facts(pair, inv, units):
    """{targets, note} about the child occurrences nested inside a measurement's two (label,
    entity, kind) targets, or None when neither holds any."""
    # MEASURED: measureMinimumDistance against an occurrence measures that occurrence's OWN bodies
    # and NOT what is nested inside it (a parent 90 mm away holding a child 40 mm away answers 90),
    # so each child is measured here on its own to say how optimistic the parent's number is.
    (label_a, ent_a, kind_a), (label_b, ent_b, kind_b) = pair
    records = [rec for rec in (_subtree_record(label_a, ent_a, kind_a, label_b, ent_b, inv),
                               _subtree_record(label_b, ent_b, kind_b, label_a, ent_a, inv))
               if rec is not None]
    if not records:
        return None
    head = "NESTED TARGET: " if len(records) == 1 else "NESTED TARGETS: "
    return {
        "targets": records,
        "note": (head + "; ".join(_subtree_clause(r, units) for r in records)
                 + ". The distance above is between the two targets as named, and an occurrence "
                   "contributes its OWN bodies - what is nested inside it is NOT in that number, "
                   "so the true gap to that assembly can be SMALLER. The per-child gaps in "
                   "targets_with_children are that measurement, and they cover only the children "
                   "listed."),
    }


def dot(a, b):
    """Dot product of two [x, y, z] sequences, or None if either is None."""
    return None if a is None or b is None else a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def cross(a, b):
    """Cross product of two [x, y, z] sequences as a tuple, or None if either is None."""
    if a is None or b is None:
        return None
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def unit_vector(v, decimals: int = 6):
    """A normalized [x,y,z] (rounded to `decimals`) for anything exposing .x/.y/.z, or None if
    zero-length/unavailable. Renormalizing an already-unit vector is a no-op within rounding."""
    if v is None:
        return None
    x, y, z = safe(lambda: v.x), safe(lambda: v.y), safe(lambda: v.z)
    if x is None or y is None or z is None:
        return None
    mag = (x * x + y * y + z * z) ** 0.5
    if mag < 1e-12:
        return None
    return [round(x / mag, decimals), round(y / mag, decimals), round(z / mag, decimals)]


def unit_vector_between(p1, p2, decimals: int = 6):
    """Unit direction from point `p1` to `p2` (each exposing .x/.y/.z), or None if degenerate - the
    shared subtract-normalize-round core, whatever the two endpoints were read off."""
    if p1 is None or p2 is None:
        return None
    x1, y1, z1 = safe(lambda: p1.x), safe(lambda: p1.y), safe(lambda: p1.z)
    x2, y2, z2 = safe(lambda: p2.x), safe(lambda: p2.y), safe(lambda: p2.z)
    if None in (x1, y1, z1, x2, y2, z2):
        return None
    dx, dy, dz = x2 - x1, y2 - y1, z2 - z1
    mag = (dx * dx + dy * dy + dz * dz) ** 0.5
    if mag < 1e-12:
        return None
    return [round(dx / mag, decimals), round(dy / mag, decimals), round(dz / mag, decimals)]


def evaluator_normal_at(face, point, decimals: int = 6):
    """Outward unit normal of `face` AT `point` via the surface evaluator (getNormalAtPoint answers
    a (bool, Vector3D) tuple in Python), or None - never a fabricated normal."""
    if point is None:
        return None
    ev = safe(lambda: face.evaluator)
    if ev is None:
        return None
    res = safe(lambda: ev.getNormalAtPoint(point))
    if not (isinstance(res, (list, tuple)) and len(res) == 2):
        return None
    okflag, nrm = res
    if not okflag or nrm is None:
        return None
    return unit_vector(nrm, decimals=decimals)
