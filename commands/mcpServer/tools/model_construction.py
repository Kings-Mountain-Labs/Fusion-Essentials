# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""MCP building block: add construction geometry (points / axes / planes) in the active component,
across the API's real datum-creation modes (_MODES_BY_KIND is the mode vocabulary per kind).

A bare coordinate point or a world-axis-through-a-point needs DIRECT modeling; every geometry-based
mode (an edge/face/plane/vertex reference) is parametric-legal.
"""

import math

import adsk.core
import adsk.fusion

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import error, ok, safe, scale, target_component
from . import _common
from . import _geom
from . import _inputs

app = adsk.core.Application.get()

# ── the mode vocabulary, grouped by kind ──────────────────────────────────────────────────────────
_PLANE_MODES = ("offset", "at_angle", "at_angle_on_face", "three_points", "midplane",
                "tangent_at_point", "offset_through_point", "on_path", "two_edges")
_AXIS_MODES = ("edge", "world", "circular_face", "two_points", "two_planes", "perpendicular_at_point")
_POINT_MODES = ("coordinate", "circle_center", "two_edges", "three_planes", "edge_plane", "on_path")
_MODES_BY_KIND = {"plane": _PLANE_MODES, "axis": _AXIS_MODES, "point": _POINT_MODES}
_LEGACY_MODE = {"plane": "offset", "axis": "edge", "point": "coordinate"}
# de-duplicated, order-preserving (two_edges/on_path are legal for two kinds - listed once).
_MODE_OPTIONS = list(dict.fromkeys(_PLANE_MODES + _AXIS_MODES + _POINT_MODES))

# _geom.unit_vector's default 6-decimal rounding is DISPLAY precision: a 5e-7 error per component
# times the lever arm manufactures a "miss" on geometry that is exactly on the plane. The payload
# keeps the rounded vector; only the gates read at this precision.
_GATE_DECIMALS = 15

# A gate compares a projected distance against zero. Fusion lands a datum on its defining point
# exactly, so the only slack needed is the float noise the two coordinate reads accumulate - which
# grows with the distance between them, hence RELATIVE to that lever arm, not a flat cm figure.
_COINCIDENT_REL_TOL = 1e-8

# ── shared typed inputs (reused across modes; each mode uses the subset it needs) ────────────────
_AXIS = _inputs.AxisRef("axis", default="z")
_PLANE = _inputs.PlaneRef("plane", default="xy", description="Base/1st plane.")
_PLANE2 = _inputs.PlaneRef("plane2")
_PLANE3 = _inputs.PlaneRef("plane3")
_EDGES = _inputs.GeometryHandleList("edges", require="edge")
_POINTS = _inputs.GeometryHandleList("points", require="vertex")
_FACE = _inputs.GeometryHandle("face", require="face")
_MODE = _inputs.Choice("mode", _MODE_OPTIONS, default="",
    description="Build method within 'kind'.")
# mode='on_path' reads 'at' through this: a unitless 0-1 ratio, or a length from the path start.
_DISTANCE_TYPE = _inputs.Choice("distance_type", ["proportional", "absolute"])
_TO_OBJECT = _inputs.GeometryHandle("to_object", require="vertex")

# MODE GUARD: setByPoint(Point3D) / setByLine(InfiniteLine3D) are DIRECT-edit-only and fail in
# parametric, the default. Every other mode resolves real geometry and is parametric-valid, so it
# gets no guard. Declaring the guard derives the error from MODE_DIRECT, so the remedy cannot invert.
_DIRECT_GUARD = _inputs.ModeGuard(
    _inputs.MODE_DIRECT,
    why="setByPoint(Point3D)/setByLine(InfiniteLine3D) are direct-edit-only.",
    fix_hint="Switch to direct mode (Design settings / design_set_mode), or build the datum parametrically.")

_PARAMETRIC_COORD_MSG = (
"kind={k} at a raw coordinate needs DIRECT-modeling mode - the parametric construction API has "
"no way to place a {k} at a bare x/y/z (setByPoint/setByLine are direct-edit-only and fail in "
"parametric). This design is PARAMETRIC. Options: (1) sketch_create a sketch and add a sketch "
"point at the location, then build the datum from THAT geometry; (2) for an axis, pass an edge "
"handle from find_geometry (axis='<handle>') - that IS parametric-legal; or (3) switch the "
"design to Direct modeling (Design settings) if you truly want a coordinate datum."
)


def _direct_only_block(design, k):
    """If a coordinate point/world-axis (direct-edit-only) is not allowed in the current mode,
    return the ready error MESSAGE (a string); else None."""
    ok_mode, _ = _DIRECT_GUARD.check(design)
    if ok_mode:
        return None
    return _PARAMETRIC_COORD_MSG.format(k=k)


def _env_error(e):
    """Backstop for an environment/mode rejection that slips past the ModeGuard. States which datum
    kinds need Direct vs Parametric rather than prescribing a fix direction."""
    msg = str(e)
    if "Environment is not supported" in msg or "parametric" in msg.lower():
        return error("Could not add construction geometry: this datum mode isn't supported in the "
            "current modeling mode. Only a bare coordinate point or a world-axis-through-a-point "
            "needs DIRECT-modeling; every geometry-based mode (edge axis, offset plane, and all "
            "edge/face/plane/vertex-based modes) works in Parametric.")
    return error(f"Could not add construction geometry: {e}")


def _resolve_mode(knd, raw_mode):
    """The effective mode for `knd` - the legacy default when unset, else `raw_mode` validated
    against the modes legal for `knd`. Returns (mode, error)."""
    m = (raw_mode or "").strip().lower()
    legal = _MODES_BY_KIND[knd]
    if not m:
        return _LEGACY_MODE[knd], None
    if m not in legal:
        return None, (f"mode '{m}' is not valid for kind='{knd}'. Valid modes for kind='{knd}': "
                      f"{', '.join(legal)}.")
    return m, None


def _need(items, n, label, m):
    """None if `items` has exactly n entries, else a guard error NAMING the mode/count."""
    if len(items) != n:
        return f"mode='{m}' needs exactly {n} '{label}' handle(s) (got {len(items)})."
    return None


def _need_val(val, label, m):
    """None if `val` is present, else a guard error NAMING the missing input for this mode."""
    if val is None:
        return f"mode='{m}' needs '{label}'."
    return None


def _surface_label(face):
    st = safe(lambda: face.geometry.surfaceType)
    ST = adsk.core.SurfaceTypes
    return {ST.PlaneSurfaceType: "planar", ST.CylinderSurfaceType: "cylindrical",
            ST.ConeSurfaceType: "conical", ST.SphereSurfaceType: "spherical",
            ST.TorusSurfaceType: "toroidal"}.get(st, "a non-standard-surface")


def _require_curved_face(face_ent, m):
    st = safe(lambda: face_ent.geometry.surfaceType)
    ST = adsk.core.SurfaceTypes
    if st not in (ST.CylinderSurfaceType, ST.ConeSurfaceType):
        return f"mode='{m}' needs a CYLINDRICAL or CONICAL 'face' (got a {_surface_label(face_ent)} face)."
    return None


def _curve_label(edge):
    ct = safe(lambda: edge.geometry.curveType)
    CT = adsk.core.Curve3DTypes
    return {CT.Circle3DCurveType: "circular", CT.Line3DCurveType: "straight",
            CT.Arc3DCurveType: "arc"}.get(ct, "a non-circular")


def _require_circular_edge(edge_ent, m):
    if safe(lambda: edge_ent.geometry.curveType) != adsk.core.Curve3DTypes.Circle3DCurveType:
        return f"mode='{m}' needs a CIRCULAR 'edges' entry (got a {_curve_label(edge_ent)} edge)."
    return None


def _datum_geometry(design, obj):
    """An entity's geometry in the SAME space the resolved input geometry reads in, or None."""
    # A NATIVE datum reads component-LOCAL off .geometry while a proxy-resolved vertex/face reads
    # WORLD; createForAssemblyContext(activeOccurrence) restores world space. An entity that ALREADY
    # carries an assemblyContext reads world and RAISES '3 : object is not a native object' if lifted.
    occ = safe(lambda: design.activeOccurrence)
    if occ is None or safe(lambda: obj.assemblyContext) is not None:
        return safe(lambda: obj.geometry)
    proxy = safe(lambda: obj.createForAssemblyContext(occ))
    return safe(lambda: proxy.geometry) if proxy is not None else None


def _space_unread(design, obj):
    """True when an occurrence is ACTIVE and `obj`'s geometry could not be read in its space - the
    one condition under which a claim is withheld for a SPACE reason rather than an unreadable
    property. Callers publish the claim as null and disclose the reason (see _UNREAD_SPACE_NOTE)."""
    return (safe(lambda: design.activeOccurrence) is not None
            and _datum_geometry(design, obj) is None)


# What the published 'handle' can be spent on, per kind - each named tool resolves the datum through
# a typed kind that accepts it (AxisRef for an axis, PlaneRef for a plane), so the claim fails loudly
# if it stops being true. No typed kind resolves a construction POINT, so that kind claims none.
_HANDLE_NOTE = {
    "axis": ("'handle' is this axis's entityToken - pass it (or the datum's name in this component) "
             "as 'axis' to model_pattern_circular, model_revolve, or model_move."),
    "plane": ("'handle' is this plane's entityToken - pass it as 'plane' to model_mirror, "
              "view_section, or model_split; sketch_create takes this datum's NAME instead."),
    "point": "'handle' is this point's entityToken.",
}


# A plane's own origin and the origin a sketch built on it takes are DIFFERENT points, and only the
# plane's is published here, so the sketch's is pointed at rather than left to be assumed.
_SKETCH_ORIGIN_NOTE = (" A sketch on this plane takes its origin at the world origin's projection "
                       "onto it, not at geometry.origin - sketch_create's frame.origin_mm reads it.")


# Appended when a claim came back null because the entity could not be read in the ACTIVE
# occurrence's space: a missing key reads as "not applicable", a null with this sentence reads as
# "not checked, and here is why".
_UNREAD_SPACE_NOTE = (" A null field above was NOT measured: an occurrence is active and the entity "
                      "could not be read in its space, so the value is left unclaimed rather than "
                      "reported in the component-local space the resolved inputs are not in.")


# distance_type='absolute' measures from the path START and is not clamped at EITHER end: a negative
# distance lands the datum before the start and one past the length beyond the end, both along the
# tangent and both healthy. The second note is the fallback when the path's length cannot be read.
_OFF_PATH_NOTE = (" This datum landed OFF the path: 'along_path' vs 'path_length', in 'units'. An "
                  "absolute distance is not clamped at either end - Fusion extrapolates along the "
                  "tangent and reports the feature healthy.")
_PATH_EXTRAPOLATE_NOTE = (" An absolute distance is measured from the path start and is not clamped "
                          "at either end - a value outside the path places the datum off the curve "
                          "instead of failing, so check 'geometry' and 'landed'.")


def _plane_offset(normal_src, origin, pt):
    """(signed distance in cm from `pt` to the plane through `origin`, lever arm |origin -> pt| in
    cm), or (None, None) if any read is unavailable. `normal_src` is the RAW Vector3D - normalized
    here at _GATE_DECIMALS, never handed in pre-rounded."""
    n = _geom.unit_vector(normal_src, decimals=_GATE_DECIMALS)
    if n is None or origin is None or pt is None:
        return None, None
    v = safe(lambda: origin.vectorTo(pt))
    if v is None:
        return None, None
    dist = safe(lambda: n[0] * v.x + n[1] * v.y + n[2] * v.z)
    lever = safe(lambda: (v.x * v.x + v.y * v.y + v.z * v.z) ** 0.5)
    if dist is None or lever is None:
        return None, None
    return dist, lever


def _on_plane(dist, lever):
    """True when a projected distance is zero to the precision a lever arm of `lever` cm carries.
    None when the distance could not be read."""
    if dist is None:
        return None
    return abs(dist) <= _COINCIDENT_REL_TOL * max(1.0, lever or 0.0)


def _at_fraction(raw, m):
    """(proportional position along 'path', error). 0 = the path's start, 1 = its end. Outside that
    range setByPath RAISES ('3 : proportional distance must be in [0, 1]'), and the raise rolls back
    the whole call's transaction - so the range is refused HERE, before the call."""
    if raw is None or raw == "":
        return None, f"mode='{m}' needs 'at' - the position along 'path', 0 (start) to 1 (end)."
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None, f"mode='{m}': 'at' must be a number between 0 and 1, got {raw!r}."
    if not 0.0 <= v <= 1.0:
        return None, (f"mode='{m}': 'at' must be between 0 and 1 when distance_type='proportional' "
                      f"(0 = the path's start, 1 = its end), got {v} - Fusion raises on a value "
                      f"outside that range. For a length along the path use distance_type="
                      f"'absolute', which reads 'at' in 'units'.")
    return v, None


def _on_path_distance(comp, design, k, path_raw, at_raw, dtype_raw, m):
    """(path, PathDistanceTypes member, distance ValueInput, payload extra, error) for setByPath -
    'absolute' reads 'at' as a length in 'units', 'proportional' as a unitless ratio."""
    # 'proportional' when omitted, which is NOT a schema default: 'to_object' refuses a supplied
    # distance_type, so sending 'proportional' is not the same call as omitting it.
    dtype, derr = _DISTANCE_TYPE.resolve(dtype_raw or "proportional")
    if derr:
        return None, None, None, None, derr
    if dtype == "absolute":
        if at_raw is None or at_raw == "":
            return None, None, None, None, (f"mode='{m}' needs 'at' - the distance along 'path' from "
                                            f"its start, in 'units' (distance_type='absolute').")
        val, _at_cm, verr = _inputs.length_value_input(at_raw, k, design, "at")
        if verr:
            return None, None, None, None, verr
        member = adsk.fusion.PathDistanceTypes.PhysicalPathDistanceType
        extra = {"at_distance": _inputs.expression_report(at_raw), "path_extrapolates": True}
    else:
        v, aerr = _at_fraction(at_raw, m)
        if aerr:
            return None, None, None, None, aerr
        member = adsk.fusion.PathDistanceTypes.ProportionalPathDistanceType
        val, extra = adsk.core.ValueInput.createByReal(v), {"at_ratio": v}
    path, label, perr = _common.build_path(comp, path_raw)
    if perr:
        return None, None, None, None, perr
    extra["path"] = label
    extra["distance_type"] = dtype
    return path, member, val, extra, None


def _path_length_cm(path):
    """A Path's total length in cm, or None if any part of it cannot be read. adsk.fusion.Path
    carries no length member, so each entity's curve is measured through its evaluator."""
    n = safe(lambda: path.count, 0) or 0
    if not n:
        return None
    total = 0.0
    # EVERY segment must measure or the length is withdrawn (None), so this stays a positional walk:
    # iter_collection drops an unreadable entity, which would publish a SHORT total as the path length.
    for i in range(n):
        ev = safe(lambda i=i: path.item(i).curve.evaluator)
        if ev is None:
            return None
        ext = safe(lambda: ev.getParameterExtents())
        if not (isinstance(ext, (list, tuple)) and len(ext) == 3 and ext[0]):
            return None
        res = safe(lambda: ev.getLengthAtParameter(ext[1], ext[2]))
        if not (isinstance(res, (list, tuple)) and len(res) == 2 and res[0] and res[1] is not None):
            return None
        total += res[1]
    return total


def _path_extent_report(obj, path, inv_k, with_offset=False):
    """Where a PHYSICAL placement landed along 'path' against the path's own length, both in display
    units - {} when any part is unreadable. A to-object plane's position is the target's along-path
    distance PLUS its signed offset, which the definition carries as two separate parameters."""
    defn = safe(lambda: obj.definition)
    length = _common.measured(lambda: _path_length_cm(path), inv_k)
    along = _common.measured(lambda: defn.distance.value, inv_k)
    if with_offset:
        shift = _common.measured(lambda: defn.offset.value, inv_k)
        along = None if along is None or shift is None else round(along + shift, 6)
    if length is None or along is None:
        return {}
    return {"path_length": length, "along_path": along,
            "beyond_path": bool(along < 0.0 or along > length)}


def _landed_path_report(obj):
    """The on-path placement read back off the CREATED datum: the distance (and a to-object plane's
    separate offset) as ModelParameter expressions, plus those parameters' names for param_set. A
    key is absent when the property does not read - a point's path definition carries no offset."""
    defn = safe(lambda: obj.definition)
    if defn is None:
        return {}
    landed, params = {}, {}
    for key in ("distance", "offset"):
        p = safe(lambda key=key: getattr(defn, key))
        if p is None:
            continue
        expr = safe(lambda p=p: p.expression)
        if expr is not None:
            landed[key] = expr
        nm = safe(lambda p=p: p.name)
        if nm:
            params[key] = nm
    out = {"landed": landed} if landed else {}
    if params:
        out["model_parameters"] = params
    return out


def _offset_parameter(obj):
    """The model parameter (dNN) backing an offset construction plane, so the offset is retargetable
    with param_set WITHOUT fishing through param_get to guess which dNN it is. Read live off the
    ConstructionPlaneOffsetDefinition's offset ModelParameter; None if unavailable."""
    defn = safe(lambda: obj.definition)
    if defn is None:
        return None
    p = safe(lambda: defn.offset)
    return safe(lambda: p.name) if p is not None else None


def _offset_value_cm(obj):
    """The offset an OFFSET construction plane REPORTS in internal cm, or None. The offset
    ModelParameter reads SIGNED, so the sign is part of the answer and a magnitude-only compare
    would pass a plane built on the wrong side of its base."""
    defn = safe(lambda: obj.definition)
    if defn is None:
        return None
    got = safe(lambda: defn.offset.value)
    if isinstance(got, (int, float)) and not isinstance(got, bool):
        return float(got)
    return None


def _geometry_readback(knd, design, obj, inv_k):
    """The CREATED datum's real geometry (not an echo of the input), read through _datum_geometry so
    it sits in the same space as the caller's handles. Construction geometry carries no healthState,
    so this read is the effect check. {} when the geometry cannot be read."""
    g = _datum_geometry(design, obj)
    if g is None:
        return {}
    if knd == "plane":
        return {"normal": _geom.unit_vector(safe(lambda: g.normal)),
                "origin": _common.ptxyz(safe(lambda: g.origin), inv_k)}
    if knd == "axis":
        return {"direction": _geom.unit_vector(safe(lambda: g.direction)),
                "origin": _common.ptxyz(safe(lambda: g.origin), inv_k)}
    return {"at": _common.ptxyz(g, inv_k)}   # point: .geometry IS the Point3D itself


# ── per-kind builders: (mode, ...) -> (obj, extra_payload, error) ────────────────────────────────

def _plane_datum(m, comp, design, k, units, plane_raw, plane2_raw, offset, edges_raw, angle,
                 face_raw, points_raw, path_raw, at_raw, dtype_raw, to_object_raw):
    if m == "offset":
        base, err = _PLANE.resolve(plane_raw)
        if err:
            return None, None, err
        val, offset_cm, verr = _inputs.length_value_input(offset, k, design, "offset")
        if verr:
            return None, None, verr
        cpi = comp.constructionPlanes.createInput()
        if not cpi.setByOffset(base, val):
            return None, None, ("mode='offset': Fusion rejected these inputs (setByOffset returned "
                                "false).")
        obj = comp.constructionPlanes.add(cpi)
        # OFFSET read-back: the definition's own offset parameter is the only thing that can
        # contradict a plane the API reported as created, and it is withheld when either number is
        # unreadable. The on_path forms store a separate 'distance' parameter and are not judged here.
        landed_cm = _offset_value_cm(obj)
        if (offset_cm is not None and landed_cm is not None
                and abs(landed_cm - offset_cm) > _common.EXTENT_MATCH_TOL_CM):
            asked = f"{round(offset_cm / k, 6)} {units}"
            if _inputs.looks_like_expression(offset):
                asked += f" (what the expression '{str(offset).strip()}' evaluates to)"
            return None, None, (f"mode='offset': Fusion reported success but the plane it created "
                                f"reads back an offset of {round(landed_cm / k, 6)} {units}, not "
                                f"the requested {asked}. The datum "
                                f"'{safe(lambda: obj.name)}' was added and is still in the design - "
                                f"remove it with design_delete_feature.")
        extra = {"offset_from": _inputs.surface_ref_label(base), "offset": _inputs.expression_report(offset)}
        dparam = _offset_parameter(obj)
        if dparam:
            extra["model_parameters"] = {"offset": dparam}
        return obj, extra, None

    if m == "at_angle":
        base, err = _PLANE.resolve(plane_raw)
        if err:
            return None, None, err
        eds, err = _EDGES.resolve(edges_raw)
        if err:
            return None, None, err
        cerr = _need(eds, 1, "edges", m)
        if cerr:
            return None, None, cerr
        cpi = comp.constructionPlanes.createInput()
        # setByAngle(linearEntity, angle radians, planarEntity) rotates planarEntity about
        # linearEntity by angle (live API doc).
        if not cpi.setByAngle(eds[0], adsk.core.ValueInput.createByReal(math.radians(float(angle))), base):
            return None, None, (f"mode='at_angle': Fusion rejected these inputs (setByAngle returned "
                                "false).")
        obj = comp.constructionPlanes.add(cpi)
        extra = {"angle_deg": float(angle)}
        # Both normals through _datum_geometry: 'plane' resolves either to a NATIVE construction
        # plane (component-local, lifted here) or to a proxy-resolved face (already world), so the
        # dot compares one space against itself.
        base_normal = _geom.unit_vector(safe(lambda: _datum_geometry(design, base).normal))
        new_normal = _geom.unit_vector(safe(lambda: _datum_geometry(design, obj).normal)) if obj else None
        dot = _geom.dot(base_normal, new_normal)
        if dot is not None:
            extra["normal_changed"] = bool(dot < 0.999999)
        elif _space_unread(design, base) or _space_unread(design, obj):
            extra["normal_changed"] = None
            extra["space_unread"] = True
        return obj, extra, None

    if m == "at_angle_on_face":
        base, err = _PLANE.resolve(plane_raw)
        if err:
            return None, None, err
        face, err = _FACE.resolve(face_raw)
        if err:
            return None, None, err
        cerr = _need_val(face, "face", m)
        if cerr:
            return None, None, cerr
        cerr = _require_curved_face(face, m)
        if cerr:
            return None, None, cerr
        cpi = comp.constructionPlanes.createInput()
        # setByAngleOnCurvedFace(curvedFace, angle, planarEntity) rotates about the axis INFERRED
        # from the curved face - there is no axis argument. createByReal takes RADIANS, and angle=0
        # lands the plane parallel to planarEntity (both measured).
        if not cpi.setByAngleOnCurvedFace(
                face, adsk.core.ValueInput.createByReal(math.radians(float(angle))), base):
            return None, None, ("mode='at_angle_on_face': Fusion rejected these inputs "
                                "(setByAngleOnCurvedFace returned false).")
        # A 'plane' whose NORMAL is parallel to the inferred axis leaves the angle undefined:
        # setByAngleOnCurvedFace returns true and add() then raises '3 : reference planarEntity must
        # not be perpendicular with axis input', which names its own offender, so no pre-guard.
        obj = comp.constructionPlanes.add(cpi)
        extra = {"angle_deg": float(angle), "angle_from": _inputs.surface_ref_label(base)}
        g = _datum_geometry(design, obj)
        dot = _geom.dot(_geom.unit_vector(safe(lambda: g.normal), decimals=_GATE_DECIMALS),
                    _geom.unit_vector(safe(lambda: face.geometry.axis), decimals=_GATE_DECIMALS))
        # Cylinder/Cone .origin is the centre of the base, i.e. a point ON the inferred axis - so
        # the plane contains the whole axis when the axis direction lies in it and that point does.
        on_axis = _on_plane(*_plane_offset(safe(lambda: g.normal), safe(lambda: g.origin),
                                           safe(lambda: face.geometry.origin)))
        if dot is not None and on_axis is not None:
            extra["contains_face_axis"] = bool(abs(dot) <= _COINCIDENT_REL_TOL and on_axis)
        elif _space_unread(design, obj):
            extra["contains_face_axis"] = None
            extra["space_unread"] = True
        return obj, extra, None

    if m == "offset_through_point":
        base, err = _PLANE.resolve(plane_raw)
        if err:
            return None, None, err
        pts, err = _POINTS.resolve(points_raw)
        if err:
            return None, None, err
        cerr = _need(pts, 1, "points", m)
        if cerr:
            return None, None, cerr
        cpi = comp.constructionPlanes.createInput()
        if not cpi.setByOffsetThroughPoint(base, pts[0]):
            return None, None, ("mode='offset_through_point': Fusion rejected these inputs "
                                "(setByOffsetThroughPoint returned false).")
        obj = comp.constructionPlanes.add(cpi)
        # The point DEFINES the offset, so the created plane passes through it exactly (measured) -
        # a non-zero distance means the plane that landed is not the one that was asked for.
        g = _datum_geometry(design, obj)
        dist, lever = _plane_offset(safe(lambda: g.normal), safe(lambda: g.origin),
                                    safe(lambda: pts[0].geometry))
        on = _on_plane(dist, lever)
        if on is False:
            return None, None, (f"mode='offset_through_point': Fusion reported success but the "
                                f"plane it created misses the point by {abs(dist):.6f} cm - an "
                                f"offset-through-point plane passes through it exactly. The datum "
                                f"'{safe(lambda: obj.name)}' was added and is still in the design.")
        if on:
            return obj, {"passes_through_point": True}, None
        if _space_unread(design, obj):
            return obj, {"passes_through_point": None, "space_unread": True}, None
        return obj, {}, None

    if m == "on_path":
        to_obj, err = _TO_OBJECT.resolve(to_object_raw)
        if err:
            return None, None, err
        if to_obj is not None:
            if at_raw not in (None, "") or (dtype_raw or "").strip():
                return None, None, ("mode='on_path': 'to_object' places the plane AT that point "
                                    "(shifted by 'offset'), so it cannot be combined with 'at' or "
                                    "'distance_type' - pass one or the other.")
            val, _offset_cm, verr = _inputs.length_value_input(offset, k, design, "offset")
            if verr:
                return None, None, verr
            path, label, perr = _common.build_path(comp, path_raw)
            if perr:
                return None, None, perr
            cpi = comp.constructionPlanes.createInput()
            # setByPathToObject lands the plane at toObject's own along-path position plus a SIGNED
            # along-path offset; the two arrive as SEPARATE ModelParameters on the definition.
            if not cpi.setByPathToObject(path, to_obj, val):
                return None, None, ("mode='on_path': Fusion rejected these inputs "
                                    "(setByPathToObject returned false).")
            obj = comp.constructionPlanes.add(cpi)
            # A to-object placement is physical too, and its offset can carry the plane past the
            # path end as readily as a raw distance can (measured) - so it gets the same extent
            # reading and the same off-path disclosure.
            extra = {"path": label, "to_object": True, "offset": _inputs.expression_report(offset),
                     "path_extrapolates": True}
            extra.update(_landed_path_report(obj))
            extra.update(_path_extent_report(obj, path, 1.0 / k, with_offset=True))
            return obj, extra, None
        path, dtype, val, extra, perr = _on_path_distance(comp, design, k, path_raw, at_raw,
                                                          dtype_raw, m)
        if perr:
            return None, None, perr
        cpi = comp.constructionPlanes.createInput()
        # setByPath orients the plane normal to the path at the placement point (measured).
        if not cpi.setByPath(path, dtype, val):
            return None, None, ("mode='on_path': Fusion rejected these inputs (setByPath returned "
                                "false).")
        obj = comp.constructionPlanes.add(cpi)
        extra.update(_landed_path_report(obj))
        if extra["distance_type"] == "absolute":
            extra.update(_path_extent_report(obj, path, 1.0 / k))
        return obj, extra, None

    if m == "three_points":
        pts, err = _POINTS.resolve(points_raw)
        if err:
            return None, None, err
        cerr = _need(pts, 3, "points", m)
        if cerr:
            return None, None, cerr
        cpi = comp.constructionPlanes.createInput()
        # Fails if the points do not form a triangle (two coincident, or all three collinear) - live API doc.
        if not cpi.setByThreePoints(pts[0], pts[1], pts[2]):
            return None, None, ("mode='three_points': Fusion rejected these points (setByThreePoints "
                                "returned false) - they must form a triangle (no two coincident, not "
                                "all three collinear).")
        return comp.constructionPlanes.add(cpi), {"point_count": 3}, None

    if m == "midplane":
        p1, err = _PLANE.resolve(plane_raw)
        if err:
            return None, None, err
        p2, err = _PLANE2.resolve(plane2_raw)
        if err:
            return None, None, err
        cerr = _need_val(p2, "plane2", m)
        if cerr:
            return None, None, cerr
        cpi = comp.constructionPlanes.createInput()
        # setByTwoPlanes on a PLANE input = the MIDPLANE between them (live API doc); fails if co-planar.
        if not cpi.setByTwoPlanes(p1, p2):
            return None, None, ("mode='midplane': Fusion rejected these planes (setByTwoPlanes "
                                "returned false) - this fails when the two planes are co-planar "
                                "(identical).")
        return comp.constructionPlanes.add(cpi), {}, None

    if m == "tangent_at_point":
        face, err = _FACE.resolve(face_raw)
        if err:
            return None, None, err
        cerr = _need_val(face, "face", m)
        if cerr:
            return None, None, cerr
        cerr = _require_curved_face(face, m)
        if cerr:
            return None, None, cerr
        pts, err = _POINTS.resolve(points_raw)
        if err:
            return None, None, err
        cerr = _need(pts, 1, "points", m)
        if cerr:
            return None, None, cerr
        cpi = comp.constructionPlanes.createInput()
        if not cpi.setByTangentAtPoint(face, pts[0]):
            return None, None, ("mode='tangent_at_point': Fusion rejected these inputs "
                                "(setByTangentAtPoint returned false).")
        return comp.constructionPlanes.add(cpi), {}, None

    if m == "two_edges":
        eds, err = _EDGES.resolve(edges_raw)
        if err:
            return None, None, err
        cerr = _need(eds, 2, "edges", m)
        if cerr:
            return None, None, cerr
        cpi = comp.constructionPlanes.createInput()
        # Fails if the two linear entities are not coplanar - live API doc.
        if not cpi.setByTwoEdges(eds[0], eds[1]):
            return None, None, ("mode='two_edges': Fusion rejected these edges (setByTwoEdges "
                                "returned false) - the two entities must be coplanar and linear.")
        return comp.constructionPlanes.add(cpi), {}, None

    return None, None, f"Unhandled plane mode '{m}'."


def _axis_datum(m, comp, design, k, x, y, z, axis_raw, plane_raw, plane2_raw, face_raw, points_raw):
    if m in ("", "edge", "world"):
        ax, aerr = _AXIS.resolve(axis_raw)
        if aerr:
            return None, None, aerr
        if ax[0] == "edge":
            # Edge-defined axis: setByEdge is parametric-LEGAL (setByLine is not) - confirmed live.
            cai = comp.constructionAxes.createInput()
            if not cai.setByEdge(ax[1]):
                return None, None, ("mode='edge': Fusion rejected this edge (setByEdge returned "
                                    "false).")
            obj = comp.constructionAxes.add(cai)
        else:
            blocked = _direct_only_block(design, "axis")
            if blocked:
                return None, None, blocked
            vx, vy, vz = ax[1]
            origin = adsk.core.Point3D.create(float(x) * k, float(y) * k, float(z) * k)
            line = adsk.core.InfiniteLine3D.create(origin, adsk.core.Vector3D.create(vx, vy, vz))
            cai = comp.constructionAxes.createInput()
            if not cai.setByLine(line):
                return None, None, ("mode='world': Fusion rejected this world axis (setByLine "
                                    "returned false).")
            obj = comp.constructionAxes.add(cai)
        return obj, {"through": {"x": float(x), "y": float(y), "z": float(z)},
                    "axis": (axis_raw or "z").strip().lower()}, None

    if m == "circular_face":
        face, err = _FACE.resolve(face_raw)
        if err:
            return None, None, err
        cerr = _need_val(face, "face", m)
        if cerr:
            return None, None, cerr
        cerr = _require_curved_face(face, m)
        if cerr:
            return None, None, cerr
        face_axis = _geom.unit_vector(safe(lambda: face.geometry.axis))
        cai = comp.constructionAxes.createInput()
        if not cai.setByCircularFace(face):
            return None, None, ("mode='circular_face': Fusion rejected this face "
                                "(setByCircularFace returned false).")
        obj = comp.constructionAxes.add(cai)
        extra = {}
        # The face is proxy-resolved (WORLD); the created axis reads component-LOCAL while an
        # occurrence is active, so it is lifted into the same space before the directions are dotted.
        new_dir = _geom.unit_vector(safe(lambda: _datum_geometry(design, obj).direction)) if obj else None
        dot = _geom.dot(face_axis, new_dir)
        if dot is not None:
            extra["aligned_to_face_axis"] = bool(abs(dot) > 0.999999)
        elif _space_unread(design, obj):
            extra["aligned_to_face_axis"] = None
            extra["space_unread"] = True
        return obj, extra, None

    if m == "two_points":
        pts, err = _POINTS.resolve(points_raw)
        if err:
            return None, None, err
        cerr = _need(pts, 2, "points", m)
        if cerr:
            return None, None, cerr
        cai = comp.constructionAxes.createInput()
        # Fails if the two points are coincident - live API doc.
        if not cai.setByTwoPoints(pts[0], pts[1]):
            return None, None, ("mode='two_points': Fusion rejected these points (setByTwoPoints "
                                "returned false) - they must not be coincident.")
        return comp.constructionAxes.add(cai), {}, None

    if m == "two_planes":
        p1, err = _PLANE.resolve(plane_raw)
        if err:
            return None, None, err
        p2, err = _PLANE2.resolve(plane2_raw)
        if err:
            return None, None, err
        cerr = _need_val(p2, "plane2", m)
        if cerr:
            return None, None, cerr
        cai = comp.constructionAxes.createInput()
        # setByTwoPlanes on an AXIS input = their INTERSECTION line (live API doc); fails if parallel.
        if not cai.setByTwoPlanes(p1, p2):
            return None, None, ("mode='two_planes': Fusion rejected these planes (setByTwoPlanes "
                                "returned false) - this fails when the two planes are parallel.")
        return comp.constructionAxes.add(cai), {}, None

    if m == "perpendicular_at_point":
        face, err = _FACE.resolve(face_raw)
        if err:
            return None, None, err
        cerr = _need_val(face, "face", m)
        if cerr:
            return None, None, cerr
        pts, err = _POINTS.resolve(points_raw)
        if err:
            return None, None, err
        cerr = _need(pts, 1, "points", m)
        if cerr:
            return None, None, cerr
        face_normal = _geom.unit_vector(safe(lambda: face.geometry.normal))
        cai = comp.constructionAxes.createInput()
        if not cai.setByPerpendicularAtPoint(face, pts[0]):
            return None, None, ("mode='perpendicular_at_point': Fusion rejected these inputs "
                                "(setByPerpendicularAtPoint returned false).")
        obj = comp.constructionAxes.add(cai)
        extra = {}
        # Same space split as circular_face: the face reads WORLD, the fresh axis reads LOCAL.
        new_dir = _geom.unit_vector(safe(lambda: _datum_geometry(design, obj).direction)) if obj else None
        dot = _geom.dot(face_normal, new_dir)
        if dot is not None:
            extra["aligned_to_face_normal"] = bool(abs(dot) > 0.999999)
        elif _space_unread(design, obj):
            extra["aligned_to_face_normal"] = None
            extra["space_unread"] = True
        return obj, extra, None

    return None, None, f"Unhandled axis mode '{m}'."


def _point_datum(m, comp, design, k, x, y, z, plane_raw, plane2_raw, plane3_raw, edges_raw,
                 path_raw, at_raw, dtype_raw, to_object_raw):
    if m == "coordinate":
        # setByPoint(Point3D) is direct-edit-only - the ModeGuard refuses cleanly BEFORE the doomed
        # mutation (and gives the correct-direction remedy).
        blocked = _direct_only_block(design, "point")
        if blocked:
            return None, None, blocked
        cpi = comp.constructionPoints.createInput()
        if not cpi.setByPoint(adsk.core.Point3D.create(float(x) * k, float(y) * k, float(z) * k)):
            return None, None, ("mode='coordinate': Fusion rejected this coordinate (setByPoint "
                                "returned false).")
        obj = comp.constructionPoints.add(cpi)
        return obj, {"at": {"x": float(x), "y": float(y), "z": float(z)}}, None

    if m == "circle_center":
        eds, err = _EDGES.resolve(edges_raw)
        if err:
            return None, None, err
        cerr = _need(eds, 1, "edges", m)
        if cerr:
            return None, None, cerr
        cerr = _require_circular_edge(eds[0], m)
        if cerr:
            return None, None, cerr
        cpi = comp.constructionPoints.createInput()
        if not cpi.setByCenter(eds[0]):
            return None, None, "mode='circle_center': Fusion rejected this edge (setByCenter returned false)."
        return comp.constructionPoints.add(cpi), {}, None

    if m == "two_edges":
        eds, err = _EDGES.resolve(edges_raw)
        if err:
            return None, None, err
        cerr = _need(eds, 2, "edges", m)
        if cerr:
            return None, None, cerr
        cpi = comp.constructionPoints.createInput()
        if not cpi.setByTwoEdges(eds[0], eds[1]):
            return None, None, ("mode='two_edges': Fusion rejected these edges (setByTwoEdges "
                                "returned false) - the two linear edges/lines must actually "
                                "intersect.")
        return comp.constructionPoints.add(cpi), {}, None

    if m == "three_planes":
        p1, err = _PLANE.resolve(plane_raw)
        if err:
            return None, None, err
        p2, err = _PLANE2.resolve(plane2_raw)
        if err:
            return None, None, err
        cerr = _need_val(p2, "plane2", m)
        if cerr:
            return None, None, cerr
        p3, err = _PLANE3.resolve(plane3_raw)
        if err:
            return None, None, err
        cerr = _need_val(p3, "plane3", m)
        if cerr:
            return None, None, cerr
        cpi = comp.constructionPoints.createInput()
        if not cpi.setByThreePlanes(p1, p2, p3):
            return None, None, ("mode='three_planes': Fusion rejected these planes "
                                "(setByThreePlanes returned false) - they must intersect at a "
                                "single point.")
        return comp.constructionPoints.add(cpi), {}, None

    if m == "edge_plane":
        eds, err = _EDGES.resolve(edges_raw)
        if err:
            return None, None, err
        cerr = _need(eds, 1, "edges", m)
        if cerr:
            return None, None, cerr
        base, err = _PLANE.resolve(plane_raw)
        if err:
            return None, None, err
        cpi = comp.constructionPoints.createInput()
        if not cpi.setByEdgePlane(eds[0], base):
            return None, None, ("mode='edge_plane': Fusion rejected these inputs (setByEdgePlane "
                                "returned false) - the edge (extended if needed) must meet the "
                                "plane.")
        return comp.constructionPoints.add(cpi), {}, None

    if m == "on_path":
        # ConstructionPointInput carries setByPath but NOT setByPathToObject (measured) - to-object
        # placement exists for the plane kind only, so the point kind refuses it instead of
        # silently dropping the input.
        if (to_object_raw or "").strip():
            return None, None, ("mode='on_path' with kind='point': 'to_object' is plane-only - "
                                "ConstructionPointInput has no setByPathToObject. Use kind='plane', "
                                "or place the point with 'at' and distance_type='absolute'.")
        path, dtype, val, extra, perr = _on_path_distance(comp, design, k, path_raw, at_raw,
                                                          dtype_raw, m)
        if perr:
            return None, None, perr
        cpi = comp.constructionPoints.createInput()
        # Same (path, distanceType, distance) triple as the plane kind; the created point's
        # coordinates come back in the payload's 'geometry'.
        if not cpi.setByPath(path, dtype, val):
            return None, None, ("mode='on_path': Fusion rejected these inputs (setByPath returned "
                                "false).")
        obj = comp.constructionPoints.add(cpi)
        extra.update(_landed_path_report(obj))
        if extra["distance_type"] == "absolute":
            extra.update(_path_extent_report(obj, path, 1.0 / k))
        return obj, extra, None

    return None, None, f"Unhandled point mode '{m}'."


def handler(kind: str = "point", mode: str = "", x: float = 0.0, y: float = 0.0, z: float = 0.0,
            axis: str = "z", plane: str = "xy", plane2: str = "", plane3: str = "",
            edges=None, points=None, face: str = "", angle: float = 0.0,
            path=None, at=None, distance_type: str = "", to_object: str = "",
            offset: float = 0.0, units: str = "mm", name: str = "") -> dict:
    """See TOOL_DESCRIPTION."""
    k = scale(units)
    if k is None:
        return error(f"Unknown units '{units}'. Use mm, cm, or in.")
    knd = (kind or "point").strip().lower()
    if knd not in ("point", "axis", "plane"):
        return error(f"Unknown kind '{kind}'. Use: point, axis, plane.")
    m, merr = _resolve_mode(knd, mode)
    if merr:
        return error(merr)

    design = _common.design()
    if not design:
        return error("No active design. Create or open a document first (see doc_new).")
    comp = target_component(design)

    try:
        if knd == "point":
            obj, extra, berr = _point_datum(m, comp, design, k, x, y, z, plane, plane2, plane3,
                                            edges, path, at, distance_type, to_object)
        elif knd == "axis":
            obj, extra, berr = _axis_datum(m, comp, design, k, x, y, z, axis, plane, plane2, face, points)
        else:
            obj, extra, berr = _plane_datum(m, comp, design, k, units, plane, plane2, offset, edges,
                                            angle, face, points, path, at, distance_type, to_object)
        if berr:
            return error(berr)
    except Exception as e:
        return _env_error(e)

    if not obj:
        return error(f"Construction {knd} creation returned nothing.")
    # apply_rename, not a swallowed setattr: a declined/deduped datum rename is disclosed.
    datum_name, rename_warning = _common.apply_rename(obj, name)

    # The datum's own entityToken, so the next call can point AT what was just created - it is what
    # AxisRef/PlaneRef resolve a datum handle through. Published null, never omitted, when unread.
    handle = safe(lambda: obj.entityToken) or None
    out = {
    "created": True,
    "kind": knd,
    "mode": m,
    "name": datum_name,
    "component": safe(lambda: comp.name),
    "units": units,
    "handle": handle,
    "geometry": _geometry_readback(knd, design, obj, 1.0 / k),
    "note": "Construction datum created - snap joints/sketches to it (e.g. joint_create_origin).",
    }
    if rename_warning:
        out["rename_warning"] = rename_warning
    if handle:
        out["note"] += " " + _HANDLE_NOTE[knd]
    if knd == "plane":
        out["note"] += _SKETCH_ORIGIN_NOTE
    out.update(extra or {})
    # One disclosure for every null the active occurrence's space cost us - the mode's own claim
    # (flagged by the builder) and/or the geometry read-back, which comes back empty for the same
    # reason.
    if out.pop("space_unread", False) or _space_unread(design, obj):
        out["note"] += _UNREAD_SPACE_NOTE
    # An absolute placement measured to be INSIDE the path needs no warning at all - 'path_length'
    # and 'along_path' already say where it sits. The warning is for the one that landed outside,
    # and the generic form for the path whose length could not be measured.
    if out.pop("path_extrapolates", False):
        if out.get("beyond_path"):
            out["note"] += _OFF_PATH_NOTE
        elif "beyond_path" not in out:
            out["note"] += _PATH_EXTRAPOLATE_NOTE
    if out.get("model_parameters"):
        out["note"] += (" The names in 'model_parameters' are this datum's own model parameters - "
                        "param_set one to an expression to drive the datum parametrically.")
    return ok(out)


TOOL_DESCRIPTION = (
    "Add a construction point, axis, or plane; each 'mode' reads its own subset of the inputs."
)

construction_tool = (
    Tool.create_simple(name="model_construction", description=TOOL_DESCRIPTION)
    .add_input_property(*_inputs.Choice("kind", ["point", "axis", "plane"],
        default="point").as_property())
    .add_input_property(*_MODE.as_property())
    .add_input_property("x", {"type": "number"})
    .add_input_property("y", {"type": "number"})
    .add_input_property("z", {"type": "number"})
    .add_input_property(*_AXIS.as_property())
    .add_input_property(*_PLANE.as_property())
    .add_input_property(*_PLANE2.as_property(brief=True))
    .add_input_property(*_PLANE3.as_property(brief=True))
    .add_input_property(*_EDGES.as_property())
    .add_input_property(*_POINTS.as_property())
    .add_input_property(*_FACE.as_property())
    .add_input_property("angle", {"type": "number", "description": "In degrees."})
    .add_input_property("path", {"type": ["string", "array"], "items": {"type": "string"},
            "description": "An edge 'handle' (chains across TANGENT connections only; the 'path' "
                           "count is the truth), or 'sketch:<name>'."})
    .add_input_property("at", {"type": ["number", "string"]})
    .add_input_property(*_DISTANCE_TYPE.as_property())
    .add_input_property(*_TO_OBJECT.as_property())
    .add_input_property("offset", {"type": ["number", "string"],
            "description": "The plane offset, or the shift from 'to_object'."})
    .add_input_property(*_inputs.UNITS.as_property())
    .add_input_property("name", {"type": "string"})
    .strict_schema()
)
construction_item = Item.create_tool_item(
    tool=construction_tool, write="write", handler=handler, run_on_main_thread=True,
    verification=Verification(
        kind="effect",
        rung="value",
        evidence_test="tests/unit/test_model_construction.py::TestOffsetReadBack"
                      "::test_a_mismatched_read_back_errors_naming_both_values"))


def register_tool():
    register(construction_item)
