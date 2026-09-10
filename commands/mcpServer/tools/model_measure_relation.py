# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""MCP building block: ASSERT a named geometric relation between two entities, with the evidence.

  model_measure_relation -> pass/fail for one predicate (coaxial / concentric / parallel /
      perpendicular / flush / clearance / touching) over two targets, with the measured numbers and
      the tolerance. Two axes at angle 0 are PARALLEL, not COAXIAL: coaxial needs a zero offset too.
"""

import math

import adsk.core
import adsk.fusion

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item
from ..mcp_primitives.registry import register
from ._common import ok, error, safe
from . import _common
from . import _geom
from . import _inputs
from . import _outputs

app = adsk.core.Application.get()

_RELATIONS = ("coaxial", "parallel", "perpendicular", "flush", "clearance", "touching", "concentric")

# What this tool RETURNS: the verdict contract - relation/passed/measured/tolerance_used, enforced.
RETURNS = [_outputs.ReturnsVerdict(relations=_RELATIONS)]

# Per-relation default for the LINEAR tolerance, in cm (the API unit) so the default is a FIXED
# physical size regardless of the caller's 'units' (0.01 cm = 0.1 mm). A caller-supplied tolerance is
# scaled from 'units' to cm instead. Angular relations (parallel/perpendicular) carry no linear part.
_DEFAULT_TOL_CM = {"coaxial": 0.01, "flush": 0.01, "clearance": 0.01, "touching": 0.01, "concentric": 0.01}
_DEFAULT_TOL_DEG = 0.5

# TargetRef references: a find_geometry handle or a name. 'edge' is allowed so 'concentric' can take
# circular edges; the face/axis relations (coaxial/parallel/perpendicular/flush) still need cylindrical
# or planar faces.
_A = _inputs.TargetRef("entity_a", required=True, allow=("body", "face", "edge", "occurrence", "component"))
_B = _inputs.TargetRef("entity_b", required=True, allow=("body", "face", "edge", "occurrence", "component"))
_REL = _inputs.Choice("relation", list(_RELATIONS), required=True)
_TOL = _inputs.Distance("tolerance", allow_zero=True, allow_negative=False)


# ── pure vector math (plain tuples in cm; no adsk objects, so it is unit-testable) ────────────────

def _v(p):
    """A Point3D/Vector3D as an (x, y, z) tuple - None when the object is absent OR any one of its
    three components will not read. A point whose components do not all read is no position at all:
    a 0.0 stand-in publishes the world origin as a measured coordinate, and every consumer below
    compares this tuple against a tolerance, so one fabricated component decides a verdict."""
    if p is None:
        return None
    xyz = (safe(lambda: p.x), safe(lambda: p.y), safe(lambda: p.z))
    if not all(isinstance(c, (int, float)) and not isinstance(c, bool) for c in xyz):
        return None
    return xyz


def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _mag(a):
    return math.sqrt(_geom.dot(a, a))


def _unit(a):
    m = _mag(a)
    return None if m < 1e-12 else (a[0] / m, a[1] / m, a[2] / m)


def _line_angle_deg(u, v):
    """Angle between two DIRECTION LINES (undirected), folded to [0, 90] - a line and its reverse are
    the same line, so 179 deg reads as 1 deg. None if either vector is degenerate."""
    uu, vv = _unit(u), _unit(v)
    if uu is None or vv is None:
        return None
    d = max(-1.0, min(1.0, abs(_geom.dot(uu, vv))))
    return math.degrees(math.acos(d))


def _line_offset(p1, d1, p2, d2):
    """The minimum (perpendicular) distance between two infinite lines p+t*d. For coincident/parallel
    lines it is the point-to-line distance; for skew lines the common-perpendicular length. Same unit
    as the input points (cm here)."""
    u1, u2 = _unit(d1), _unit(d2)
    if u1 is None or u2 is None:
        return None
    w = _sub(p2, p1)
    cr = _geom.cross(u1, u2)
    m = _mag(cr)
    if m < 1e-9: # parallel: drop the component of w along the shared direction
        proj = _geom.dot(w, u1)
        perp = _sub(w, (u1[0] * proj, u1[1] * proj, u1[2] * proj))
        return _mag(perp)
    return abs(_geom.dot(w, cr)) / m


# ── geometry extraction (what each resolved entity offers; guard-friendly) ────────────────────────

def _face_geom(ent, kind):
    """(surfaceType, geometry) when the entity is a face, else (None, None)."""
    if kind != "face":
        return None, None
    g = safe(lambda: ent.geometry)
    if g is None:
        return None, None
    return safe(lambda: g.surfaceType), g


def _describe(ent, kind):
    """A short 'what this entity is' label for a guard message."""
    if kind == "face":
        st, _ = _face_geom(ent, kind)
        return {
            adsk.core.SurfaceTypes.CylinderSurfaceType: "a cylindrical face",
            adsk.core.SurfaceTypes.PlaneSurfaceType: "a planar face",
            adsk.core.SurfaceTypes.ConeSurfaceType: "a conical face",
            adsk.core.SurfaceTypes.SphereSurfaceType: "a spherical face",
            adsk.core.SurfaceTypes.TorusSurfaceType: "a toroidal face",
        }.get(st, "a face (not planar or cylindrical)")
    if kind == "edge":
        ct = safe(lambda: ent.geometry.curveType)
        CT = adsk.core.Curve3DTypes
        return {
            CT.Line3DCurveType: "a straight edge",
            CT.Circle3DCurveType: "a circular edge",
            CT.Arc3DCurveType: "an arc edge",
        }.get(ct, "an edge (not circular)")
    nm = safe(lambda: ent.name)
    return f"a {kind} '{nm}'" if nm else f"a {kind}"


def _axis(ent, kind):
    """(origin_cm, direction, label) for an entity that has an AXIS, else (None, None, None).
    Available from a cylindrical face. (A circular edge feeds 'concentric' via its center in
    _circle_center, not an axis here.)"""
    st, g = _face_geom(ent, kind)
    if st == adsk.core.SurfaceTypes.CylinderSurfaceType:
        return _v(safe(lambda: g.origin)), _v(safe(lambda: g.axis)), "cylindrical-face axis"
    return None, None, None


def _circle_center(ent, kind):
    """(center_cm, label) for a circular/arc edge or a cylindrical/conical face, else (None, None)."""
    if kind == "edge":
        g = safe(lambda: ent.geometry)
        ct = safe(lambda: g.curveType)
        CT = adsk.core.Curve3DTypes
        if ct == CT.Circle3DCurveType:
            return _v(safe(lambda: g.center)), "circular edge"
        if ct == CT.Arc3DCurveType:
            return _v(safe(lambda: g.center)), "arc edge"
        return None, None
    if kind == "face":
        st, g = _face_geom(ent, kind)
        if st == adsk.core.SurfaceTypes.CylinderSurfaceType:
            return _v(safe(lambda: g.origin)), "cylindrical-face center"
        if st == adsk.core.SurfaceTypes.ConeSurfaceType:
            return _v(safe(lambda: g.origin)), "conical-face center"
        return None, None
    return None, None


def _ptc(c, inv):
    """{x,y,z} for a cm-tuple center `c`, scaled to display units and rounded; None if `c` is None."""
    return None if c is None else {"x": _fmt(c[0] * inv), "y": _fmt(c[1] * inv), "z": _fmt(c[2] * inv)}


def _direction(ent, kind):
    """(direction, label) for an entity with a defining direction, else (None, None): a cylinder's
    axis, or a planar face's normal."""
    st, g = _face_geom(ent, kind)
    if st == adsk.core.SurfaceTypes.CylinderSurfaceType:
        return _v(safe(lambda: g.axis)), "cylinder axis"
    if st == adsk.core.SurfaceTypes.PlaneSurfaceType:
        return _v(safe(lambda: g.normal)), "planar-face normal"
    return None, None


def _plane(ent, kind):
    """(origin_cm, normal, label) for a PLANAR face, else (None, None, None)."""
    st, g = _face_geom(ent, kind)
    if st == adsk.core.SurfaceTypes.PlaneSurfaceType:
        return _v(safe(lambda: g.origin)), _v(safe(lambda: g.normal)), "planar face"
    return None, None, None


def _needs(rel, need, ea, ka, eb, kb, bad_a, bad_b):
    """A guard error naming what EACH entity resolved to and what the relation required."""
    got = []
    if bad_a:
        got.append(f"entity_a is {_describe(ea, ka)}")
    if bad_b:
        got.append(f"entity_b is {_describe(eb, kb)}")
    return error(f"'{rel}' needs {need}, but {' and '.join(got)}. Pass find_geometry handles at the "
                 "required geometry (a cylindrical face for an axis, a planar face for a plane).")


def _fmt(x):
    return round(x, 4)


# ── relation evaluators (each returns an ok/error result) ─────────────────────────────────────────

def _rel_coaxial(ea, ka, eb, kb, tol_cm, tol_deg, inv, units):
    oa, da, la = _axis(ea, ka)
    ob, db, lb = _axis(eb, kb)
    # The LABEL says the entity was the right KIND; the numbers say whether it could be read. Split
    # so a cylindrical face whose axis will not read is reported as unreadable, not as "not a
    # cylinder" - and never scored against a tolerance on fabricated coordinates.
    if la is None or lb is None:
        return _needs("coaxial", "an axis on EACH entity (a cylindrical face)",
                      ea, ka, eb, kb, la is None, lb is None)
    if oa is None or da is None or ob is None or db is None:
        return error("coaxial: an axis origin or direction did not read as three numbers, so the "
                     "axis lines cannot be compared - the relation is UNKNOWN, not a pass. Re-run "
                     "find_geometry for fresh handles and retry.")
    ang = _line_angle_deg(da, db)
    offs = _line_offset(oa, da, ob, db)
    if ang is None or offs is None:
        return error("coaxial: an axis was degenerate (zero-length direction); cannot compare.")
    is_parallel = ang <= tol_deg
    is_aligned = offs <= tol_cm
    passed = bool(is_parallel and is_aligned)
    if passed:
        note = (f"PASS: axes are parallel ({_fmt(ang)} deg <= {tol_deg}) and their axis lines coincide "
                f"({_fmt(offs * inv)} <= {_fmt(tol_cm * inv)} {units} offset) - coaxial.")
    elif is_parallel:
        note = (f"FAIL: axes are parallel but OFFSET by {_fmt(offs * inv)} {units} "
                f"(> {_fmt(tol_cm * inv)} tol) - parallel is NOT coaxial.")
    else:
        note = (f"FAIL: axes are {_fmt(ang)} deg apart (> {tol_deg} tol) - not parallel, so not coaxial.")
    return ok({
        "relation": "coaxial",
        "passed": passed,
        "measured": {"angle_deg": _fmt(ang), "axis_offset": _fmt(offs * inv), "units": units},
        "tolerance_used": {"tolerance_deg": tol_deg, "offset_tolerance": _fmt(tol_cm * inv), "units": units},
        "sources": {"a": la, "b": lb},
        "note": note,
    })


def _rel_parallel(ea, ka, eb, kb, tol_cm, tol_deg, inv, units):
    da, la = _direction(ea, ka)
    db, lb = _direction(eb, kb)
    if la is None or lb is None:
        return _needs("parallel", "a direction on EACH entity (a cylinder axis or a planar-face normal)",
                      ea, ka, eb, kb, la is None, lb is None)
    if da is None or db is None:
        return error("parallel: a direction did not read as three numbers, so the directions "
                     "cannot be compared - the relation is UNKNOWN, not a pass.")
    ang = _line_angle_deg(da, db)
    if ang is None:
        return error("parallel: a direction was degenerate (zero-length); cannot compare.")
    passed = bool(ang <= tol_deg)
    verdict = "PASS" if passed else "FAIL"
    return ok({
        "relation": "parallel",
        "passed": passed,
        "measured": {"angle_deg": _fmt(ang)},
        "tolerance_used": {"tolerance_deg": tol_deg},
        "sources": {"a": la, "b": lb},
        "note": f"{verdict}: {la} and {lb} are {_fmt(ang)} deg apart (tol {tol_deg} deg for parallel).",
    })


def _rel_perpendicular(ea, ka, eb, kb, tol_cm, tol_deg, inv, units):
    da, la = _direction(ea, ka)
    db, lb = _direction(eb, kb)
    if la is None or lb is None:
        return _needs("perpendicular", "a direction on EACH entity (a cylinder axis or a planar-face normal)",
                      ea, ka, eb, kb, la is None, lb is None)
    if da is None or db is None:
        return error("perpendicular: a direction did not read as three numbers, so the directions "
                     "cannot be compared - the relation is UNKNOWN, not a pass.")
    ang = _line_angle_deg(da, db)
    if ang is None:
        return error("perpendicular: a direction was degenerate (zero-length); cannot compare.")
    dev = abs(ang - 90.0)
    passed = bool(dev <= tol_deg)
    verdict = "PASS" if passed else "FAIL"
    return ok({
        "relation": "perpendicular",
        "passed": passed,
        "measured": {"angle_deg": _fmt(ang), "deviation_from_90_deg": _fmt(dev)},
        "tolerance_used": {"tolerance_deg": tol_deg},
        "sources": {"a": la, "b": lb},
        "note": f"{verdict}: {la} and {lb} meet at {_fmt(ang)} deg, {_fmt(dev)} deg off 90 (tol {tol_deg} deg).",
    })


def _rel_flush(ea, ka, eb, kb, tol_cm, tol_deg, inv, units):
    oa, na, la = _plane(ea, ka)
    ob, nb, lb = _plane(eb, kb)
    if la is None or lb is None:
        return _needs("flush", "two PLANAR faces", ea, ka, eb, kb, la is None, lb is None)
    if oa is None or na is None or ob is None or nb is None:
        return error("flush: a face plane's origin or normal did not read as three numbers, so the "
                     "planes cannot be compared - the relation is UNKNOWN, not a pass.")
    ang = _line_angle_deg(na, nb)
    un = _unit(na)
    if ang is None or un is None:
        return error("flush: a face normal was degenerate; cannot compare.")
    offs = abs(_geom.dot(_sub(ob, oa), un)) # distance from plane B's origin to plane A, along A's normal
    is_parallel = ang <= tol_deg
    is_coincident = offs <= tol_cm
    passed = bool(is_parallel and is_coincident)
    if passed:
        note = (f"PASS: faces are coplanar - normals parallel ({_fmt(ang)} deg) and offset "
                f"{_fmt(offs * inv)} {units} (<= {_fmt(tol_cm * inv)}) - flush.")
    elif is_parallel:
        note = (f"FAIL: faces are parallel but STEPPED by {_fmt(offs * inv)} {units} "
                f"(> {_fmt(tol_cm * inv)} tol) - not flush.")
    else:
        note = (f"FAIL: face normals are {_fmt(ang)} deg apart (> {tol_deg} tol) - the faces are "
                "tilted, not flush.")
    return ok({
        "relation": "flush",
        "passed": passed,
        "measured": {"normal_angle_deg": _fmt(ang), "plane_offset": _fmt(offs * inv), "units": units},
        "tolerance_used": {"tolerance_deg": tol_deg, "offset_tolerance": _fmt(tol_cm * inv), "units": units},
        "note": note,
    })


def _min_distance_cm(ea, eb):
    """(distance_cm, MeasureResults, error_result). Delegates to _common.min_distance (the shared
    measureMinimumDistance core model_measure_between also uses); a failure is surfaced, never swallowed."""
    mr, err = _common.min_distance(ea, eb)
    if err:
        return None, None, err
    # None, never 0.0: every caller compares this against a tolerance, and a fabricated 0 would
    # score an unreadable gap as CONTACT - the one answer a relation check must never invent.
    value = safe(lambda: mr.value)
    # bool is excluded ahead of the number test - it is an int subclass, so False would be scored
    # against the tolerance as a 0 cm gap and pass every caller's CONTACT test. The same deliberate
    # exclusion _common.measured/counted make; the refusal names what was read instead.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None, None, error(
            f"The minimum distance read as {value!r}, not a number, so the relation is UNKNOWN - it "
            "is not reported as touching. Re-run find_geometry for fresh handles and retry.")
    return value, mr, None


def _distance_evidence(mr, inv, units):
    """The evidence block the two distance relations share: the units and the measurement API's own
    closest-point pair. The distance itself lands after _plane_bound has had its say on it."""
    return {"units": units,
            "closest_point_on_a": _common.ptxyz(safe(lambda: mr.positionOne), inv),
            "closest_point_on_b": _common.ptxyz(safe(lambda: mr.positionTwo), inv)}


def _plane_bound(ea, ka, eb, kb, d_cm, inv, units, measured):
    """(the distance in cm to judge the relation on, the sentence to append to the note) for a pair
    that may be two PARALLEL PLANAR faces, folding that pair's evidence into `measured`. Their
    measured distance can be the separation of the PLANES rather than the gap between the bounded
    faces, which _geom.parallel_plane_facts proves and words; anything else is left as measured."""
    if ka != "face" or kb != "face":
        return d_cm, ""
    facts = _geom.parallel_plane_facts(ea, eb, d_cm, inv, units)
    if facts is None:
        return d_cm, ""
    measured["plane_separation"] = _fmt(facts["separation_cm"] * inv)
    if not facts["bounded"]:
        measured["plane_separation_only"] = True
        if facts["lateral_offset_untested"]:
            measured["lateral_offset_untested"] = True
        return d_cm, " " + facts["note"]
    measured["min_distance_is_lower_bound"] = True
    measured["closest_point_on_a"] = None
    measured["closest_point_on_b"] = None
    return facts["distance_cm"], " " + facts["note"]


def _subtree_tail(ea, ka, eb, kb, measured, inv, units):
    """The sentence to append to the note for a pair where either target holds child occurrences,
    folding what was read about them into `measured` - and "" for a pair with nothing nested. An
    occurrence is measured on its OWN bodies, so a clearance verdict on a parent PASSES on a subtree
    that does not clear; _geom.subtree_facts measures each child on its own to say so."""
    facts = _geom.subtree_facts((("entity_a", ea, ka), ("entity_b", eb, kb)), inv, units)
    if facts is None:
        return ""
    measured["targets_with_children"] = facts["targets"]
    return " " + facts["note"]


def _rel_concentric(ea, ka, eb, kb, tol_cm, tol_deg, inv, units):
    ca, la = _circle_center(ea, ka)
    cb, lb = _circle_center(eb, kb)
    if la is None or lb is None:
        return _needs("concentric", "a CIRCULAR entity on EACH side (a circular/arc edge or a "
                      "cylindrical face)", ea, ka, eb, kb, la is None, lb is None)
    if ca is None or cb is None:
        return error("concentric: a center point did not read as three numbers, so the centers "
                     "cannot be compared - the relation is UNKNOWN, not a pass.")
    d = _mag(_sub(cb, ca))
    passed = bool(d <= tol_cm)
    verdict = "PASS" if passed else "FAIL"
    note = f"{verdict}: centers are {_fmt(d * inv)} {units} apart (tol {_fmt(tol_cm * inv)} for concentric)."
    if passed:
        note += " Their center points coincide - concentric."
    else:
        note += (" The centers do not coincide. NOTE: two circles offset ALONG a shared axis are "
                 "coaxial, not concentric - try relation='coaxial' for a shared axis LINE.")
        if "face center" in la and "face center" in lb:
            note += (" Both centers came off a FACE, whose center is where its profile plane "
                     "crosses the axis - faces built on different sketch planes read offset "
                     "centers. Compare circular EDGES instead.")
    return ok({
        "relation": "concentric",
        "passed": passed,
        "measured": {"center_distance": _fmt(d * inv), "units": units,
                     "center_a": _ptc(ca, inv), "center_b": _ptc(cb, inv)},
        "tolerance_used": {"max_center_offset": _fmt(tol_cm * inv), "units": units},
        "sources": {"a": la, "b": lb},
        "note": note,
    })


def _rel_clearance(ea, ka, eb, kb, tol_cm, tol_deg, inv, units):
    d_cm, mr, err = _min_distance_cm(ea, eb)
    if err:
        return err
    measured = _distance_evidence(mr, inv, units)
    d_cm, tail = _plane_bound(ea, ka, eb, kb, d_cm, inv, units, measured)
    tail += _subtree_tail(ea, ka, eb, kb, measured, inv, units)
    measured["min_distance"] = _fmt(d_cm * inv)
    passed = bool(d_cm >= tol_cm)
    verdict = "PASS" if passed else "FAIL"
    body = (f"parts clear by {_fmt(d_cm * inv)} {units}" if passed
            else f"only {_fmt(d_cm * inv)} {units} apart")
    return ok({
        "relation": "clearance",
        "passed": passed,
        "measured": measured,
        "tolerance_used": {"min_clearance": _fmt(tol_cm * inv), "units": units},
        "note": f"{verdict}: {body} (required clearance {_fmt(tol_cm * inv)} {units})." + tail,
    })


def _rel_touching(ea, ka, eb, kb, tol_cm, tol_deg, inv, units):
    d_cm, mr, err = _min_distance_cm(ea, eb)
    if err:
        return err
    measured = _distance_evidence(mr, inv, units)
    d_cm, tail = _plane_bound(ea, ka, eb, kb, d_cm, inv, units, measured)
    tail += _subtree_tail(ea, ka, eb, kb, measured, inv, units)
    measured["min_distance"] = _fmt(d_cm * inv)
    passed = bool(d_cm <= tol_cm)
    verdict = "PASS" if passed else "FAIL"
    note = (f"{verdict}: gap is {_fmt(d_cm * inv)} {units} (tol {_fmt(tol_cm * inv)} for touching).")
    if passed and d_cm <= 1e-6:
        # measureMinimumDistance reports 0 for touching AND for interpenetration - do not claim a
        # clean touch when it could be an overlap. Point at the tool that actually measures overlap.
        note += (" NOTE: a 0 distance means the parts touch OR overlap - use assembly_inspect_interference to "
                 "confirm there is no interpenetration.")
    return ok({
        "relation": "touching",
        "passed": passed,
        "measured": measured,
        "tolerance_used": {"max_gap": _fmt(tol_cm * inv), "units": units},
        "note": note + tail,
    })


_DISPATCH = {
    "coaxial": _rel_coaxial,
    "parallel": _rel_parallel,
    "perpendicular": _rel_perpendicular,
    "flush": _rel_flush,
    "clearance": _rel_clearance,
    "touching": _rel_touching,
    "concentric": _rel_concentric,
}


def handler(entity_a: str = "", entity_b: str = "", relation: str = "",
            tolerance=None, tolerance_deg=None, units: str = "mm") -> dict:
    """See TOOL_DESCRIPTION."""
    design = _common.design()
    if not design:
        return error("No active design. Open or create a document first (see doc_new).")

    rel, rerr = _REL.resolve(relation)
    if rerr:
        return error(rerr)

    f = _common.scale(units)
    if f is None:
        return error(f"Unknown units '{units}'. Valid: mm, cm, in.")
    inv = 1.0 / f

    tol_val, terr = _TOL.resolve_scaled(tolerance, f)
    if terr:
        return error(terr)
    tol_cm = tol_val if tol_val is not None else _DEFAULT_TOL_CM.get(rel, 0.01)

    if tolerance_deg is None:
        tol_deg = _DEFAULT_TOL_DEG
    else:
        try:
            tol_deg = float(tolerance_deg)
        except Exception:
            return error("tolerance_deg must be a number (degrees).")
        if tol_deg < 0:
            return error("tolerance_deg must be >= 0 (degrees).")

    res_a, ea = _A.resolve(entity_a)
    if ea:
        return ea if isinstance(ea, dict) else error(ea)
    res_b, eb = _B.resolve(entity_b)
    if eb:
        return eb if isinstance(eb, dict) else error(eb)
    ent_a, kind_a = res_a
    ent_b, kind_b = res_b

    return _DISPATCH[rel](ent_a, kind_a, ent_b, kind_b, tol_cm, tol_deg, inv, units)


TOOL_DESCRIPTION = (
    "Judge a geometric relation between two entities.\n"
    + _outputs.produces_block(RETURNS)
)

tool = (
    Tool.create_simple(name="model_measure_relation", description=TOOL_DESCRIPTION)
    .add_input_property(*_A.as_property())
    .add_input_property(*_B.as_property())
    .add_input_property(*_REL.as_property())
    .add_input_property(*_TOL.as_property())
    .add_input_property("tolerance_deg", {"type": "number"})
    .add_input_property(*_inputs.UNITS.as_property())
    .add_required_input("entity_a")
    .add_required_input("entity_b")
    .add_required_input("relation")
    .strict_schema()
)
item = Item.create_tool_item(tool=tool, write="read", handler=handler, run_on_main_thread=True)


def register_tool():
    register(item)
