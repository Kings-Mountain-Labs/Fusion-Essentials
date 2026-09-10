# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""MCP building block: drill HOLES with the real HoleFeatures command (not a sketch + extrude-cut).

Companion to model_extrude - use this for actual holes (bolt circles, tapped holes, counterbores)
so the feature reads as a Hole in the timeline and carries hole/thread metadata.
"""

import math
import re

import adsk.core
import adsk.fusion

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import ok, error, safe, target_component
from . import _common
from . import _geom
from . import _inputs
from . import _assert
from . import _joints
from . import _threads

app = adsk.core.Application.get()

_TYPES = ("simple", "counterbore", "countersink")
_EXTENTS = ("blind", "through")

# the face the holes are drilled into (a find_geometry planar-face handle) - defines orientation.
_FACE = _inputs.GeometryHandle("face", require="planar_face", required=True,
    description="Drilled into, normal to it.")
_TARGET_BODIES = _inputs.BodyRefList("target_bodies", required=False)

_PLACEMENTS = ("sketch_points", "center", "on_edge", "plane_offsets")

# Which frame 'points' are read in. The placement sketch this tool creates carries its OWN frame,
# which differs from the face plane's in origin AND in axis sign and is not derivable from the face.
# 'world' lets a caller hand over coordinates it already read; _sketch_space_point converts them.
_POINTS_SPACES = ("sketch", "world")
_POINTS_SPACE = _inputs.Choice("points_space", options=list(_POINTS_SPACES), default="sketch")

# How far off the face's plane a WORLD point may sit and still be drilled. It absorbs the rounding a
# world read publishes (find_geometry rounds to 3 decimals in the caller's units); past it the point
# is not on the face, and projecting it would drill somewhere the caller never asked for.
_OFF_PLANE_TOL_CM = 0.005

_EDGE = _inputs.GeometryHandle("edge", require="edge",
    description="For placement=center or on_edge.")
_OFFSET_EDGE_ONE = _inputs.GeometryHandle("offset_edge_one", require="edge",
    description="'offset_one' is measured from it.")
_OFFSET_EDGE_TWO = _inputs.GeometryHandle("offset_edge_two", require="edge",
    description="Paired with 'offset_two'.")

_EDGE_POSITIONS = ("start", "middle", "end")
_EDGE_POSITION_ATTRS = {"start": "EdgeStartPointPosition", "middle": "EdgeMidPointPosition",
                        "end": "EdgeEndPointPosition"}


# ── seams (real implementations; patched in tests) ──────────────────────────

def _target_component(design):
    return target_component(design)

def _resolve_face(design, handle):
    return _FACE.resolve(handle)

def _resolve_edge(design, handle):
    return _EDGE.resolve(handle)

def _resolve_offset_edge_one(design, handle):
    return _OFFSET_EDGE_ONE.resolve(handle)

def _resolve_offset_edge_two(design, handle):
    return _OFFSET_EDGE_TWO.resolve(handle)

def _object_collection():
    return adsk.core.ObjectCollection.create()

def _value(s):
    return adsk.core.ValueInput.createByString(str(s))

_extent_dirs = adsk.fusion.ExtentDirections


# ── clearance holes (fastener-aware) ────────────────────────────────────────
# setToClearanceHole tags the hole semantically but does not resize it, so the diameter comes from
# this ISO 273 table: nominal metric clearance-hole diameters in mm, (close, normal, loose).
_CLEARANCE_MM = {
    "M2":  (2.2, 2.4, 2.6),
    "M2.5":(2.7, 2.9, 3.1),
    "M3":  (3.2, 3.4, 3.6),
    "M4":  (4.3, 4.5, 4.8),
    "M5":  (5.3, 5.5, 5.8),
    "M6":  (6.4, 6.6, 7.0),
    "M8":  (8.4, 9.0, 10.0),
    "M10": (10.5, 11.0, 12.0),
    "M12": (13.0, 13.5, 14.5),
    "M16": (17.0, 17.5, 18.5),
    "M20": (21.0, 22.0, 24.0),
}
_FITS = ("close", "normal", "loose")
_FIT_INDEX = {"close": 0, "normal": 1, "loose": 2}


def _resolve_clearance(comp, fastener, fit):
    """Build a ClearanceHoleInfo for a fastener spec like 'M6 Socket Head Cap Screw', validating the
    fastener type + size against the LIVE catalog (ClearanceHoleDataQuery). Returns (info, None) or
    (None, error). The diameter comes separately from _CLEARANCE_MM. Patched in tests."""
    parts = fastener.strip().split(" ", 1)
    size = parts[0]
    ftype = parts[1].strip() if len(parts) > 1 else ""
    if not ftype:
        return None, (f"Fastener '{fastener}' needs a type, e.g. 'M6 Socket Head Cap Screw'.")
    try:
        q = adsk.fusion.ClearanceHoleDataQuery.create()
    except Exception as e:
        return None, f"Clearance hole data unavailable: {e}"
    standards = safe(lambda: list(q.allStandards), []) or []
    std = next((s for s in standards if "Metric" in s), standards[0] if standards else None)
    if not std:
        return None, "No clearance-hole standards available."
    ftypes = safe(lambda: list(q.allFastenerTypes(std)), []) or []
    if ftype not in ftypes:
        return None, (f"Unknown fastener type '{ftype}'. Available: {', '.join(ftypes)}.")
    sizes = safe(lambda: list(q.allSizes(std, ftype)), []) or []
    if size not in sizes:
        return None, (f"Size '{size}' isn't valid for '{ftype}'. Available: {', '.join(sizes)}.")
    fit_enum = {
        "close": adsk.fusion.ClearanceHoleFits.CloseClearanceHoleFit,
        "normal": adsk.fusion.ClearanceHoleFits.NormalClearanceHoleFit,
        "loose": adsk.fusion.ClearanceHoleFits.LooseClearanceHoleFit,
    }[fit]
    info = safe(lambda: adsk.fusion.ClearanceHoleInfo.create(std, ftype, size, fit_enum))
    if not info:
        return None, f"Could not build clearance info for '{fastener}'."
    return info, None


def _clearance_diameter(fastener, fit):
    """The mm clearance-hole diameter for this fastener size + fit, from _CLEARANCE_MM, or (None, err)."""
    size = fastener.strip().split(" ", 1)[0]
    row = _CLEARANCE_MM.get(size)
    if not row:
        return None, (f"No clearance diameter known for size '{size}'. Sized fastener clearances cover: "
                      f"{', '.join(_CLEARANCE_MM.keys())}.")
    return row[_FIT_INDEX[fit]], None


def _build_input(holes, hole_type, diameter, cbore_diameter, cbore_depth, csink_diameter, csink_angle):
    """Create the HoleFeatureInput for the chosen type, or (None, error)."""
    if hole_type == "simple":
        return holes.createSimpleInput(_value(diameter)), None
    if hole_type == "counterbore":
        if not cbore_diameter or not cbore_depth:
            return None, "A counterbore hole needs 'cbore_diameter' and 'cbore_depth'."
        return holes.createCounterboreInput(_value(diameter), _value(cbore_diameter),
                                            _value(cbore_depth)), None
    if hole_type == "countersink":
        if not csink_diameter or not csink_angle:
            return None, "A countersink hole needs 'csink_diameter' and 'csink_angle' (e.g. '90 deg')."
        return holes.createCountersinkInput(_value(diameter), _value(csink_diameter),
                                            _value(csink_angle)), None
    return None, f"Unknown hole_type '{hole_type}'."


_CENTER_CURVE_TYPES = (adsk.core.Curve3DTypes.Circle3DCurveType, adsk.core.Curve3DTypes.Ellipse3DCurveType)


def _require_center_edge(edge_ent):
    """None if edge_ent is circular/elliptical, else the guard message naming what it got."""
    ct = safe(lambda: edge_ent.geometry.curveType)
    if ct in _CENTER_CURVE_TYPES:
        return None
    label = {adsk.core.Curve3DTypes.Line3DCurveType: "a straight",
             adsk.core.Curve3DTypes.Arc3DCurveType: "an arc"}.get(ct, "a non-circular")
    return f"placement='center' needs a CIRCULAR or ELLIPTICAL 'edge' (got {label} edge)."


def _require_linear_edge(edge_ent, label):
    """None if edge_ent is straight, else the guard message. setPositionByPlaneAndOffsets takes a
    LINEAR BRepEdge for each offset reference."""
    ct = safe(lambda: edge_ent.geometry.curveType)
    if ct == adsk.core.Curve3DTypes.Line3DCurveType:
        return None
    got = {adsk.core.Curve3DTypes.Circle3DCurveType: "a circular",
           adsk.core.Curve3DTypes.Ellipse3DCurveType: "an elliptical",
           adsk.core.Curve3DTypes.Arc3DCurveType: "an arc"}.get(ct, "a non-linear")
    return (f"placement='plane_offsets' measures from STRAIGHT edges; '{label}' is {got} edge. "
            "Pass a linear edge handle from find_geometry(kind='line_edge').")


# The route out when a world point names no single frame: it is performable start to finish, and
# each step names a tool that exists (sketch_create publishes the new sketch's own frame, so the
# circle can be placed in the coordinates the cut actually reads).
_PROFILE_CUT_REMEDY = (
    "Cut it as a profile instead: sketch_create on the same face reports where that sketch's origin "
    "sits and where its +X/+Y point, place a circle at those coordinates with "
    "sketch_add_geometry(geometry=[{kind: 'circle', ...}]), then model_extrude(operation='cut').")


def _world_lift(design, sketch, context_occ):
    """(matrix, error) for carrying a WORLD point into the space `modelToSketchSpace` reads from -
    the sketch's own parent component's frame. A None matrix with no error means no lift is needed:
    the sketch belongs to the design ROOT, whose model space IS world. Anywhere else the lift is the
    INVERSE of that component's placement, and no SINGLE placement means a refusal."""
    owner = safe(lambda: sketch.parentComponent)
    # `is True`: "no lift needed" is the claim that this sketch's model space IS world. An unproven
    # owner goes down the ladder, which answers a real matrix or None - and None is already refused
    # below, naming the component and the way to narrow to one instance.
    if _common.same_component(owner, safe(lambda: design.rootComponent)) is True:
        return None, ""
    to_world = _joints.component_world_matrix(design, owner, context_occ)
    name = safe(lambda: owner.name)
    whose = f" ('{name}')" if name else ""
    if to_world is None:
        # The instance remedy first: a 'face' reached THROUGH an occurrence is what this function
        # resolves the frame from, and find_geometry's 'target' is where a caller narrows to one.
        return None, (f"The placement sketch landed in a component{whose} that no single placement "
                      "answers for, so a points_space='world' point names no one frame to convert "
                      "from. Take 'face' from find_geometry with 'target' narrowed to the ONE "
                      "occurrence you mean - a face reached through an occurrence names that "
                      "placement. " + _PROFILE_CUT_REMEDY)
    inverse = safe(lambda: to_world.copy())
    if inverse is None or not safe(lambda: inverse.invert()):
        return None, (f"The placement of the component{whose} the placement sketch landed in did "
                      "not invert, so a points_space='world' point could not be carried into its "
                      "frame. " + _PROFILE_CUT_REMEDY)
    return inverse, ""


def _feature_host(face_ent, active):
    """(the component this hole is built in, whether the FACE named it). The component owning the
    drilled body hosts both the HoleFeature and its placement sketch; `active` stands in only when
    the face's owner does not read at all."""
    owner = _inputs.entity_component(face_ent)
    if owner is None:
        return active, False
    return owner, True


def _active_host_clause(host_named):
    """The disclosure for a hole hosted on the ACTIVE component: which read did not answer, and
    where the hole was built instead."""
    return (" The component that owns 'face' did not read, so the hole was built in the active "
            f"component{host_named}.")


def _no_placement_sketch_error(comp, active, detail, host_from_face):
    """The refusal for a build host that will not take 'face' as a placement sketch's plane. It
    names the host and - where same_component PROVES the two different - the active component the
    hole was not built in. The activate step needs a name to quote AND a host the FACE named, since
    on the fallback the host IS the active component and activating it moves the caller nowhere."""
    host = safe(lambda: comp.name)
    named = f" '{host}'" if host else ""
    msg = f"Could not create a placement sketch on the face in the host component{named}{detail}."
    if _common.same_component(comp, active) is False:
        other = safe(lambda: active.name)
        msg += (" The host is the component that owns 'face', not the active component"
                + (f" '{other}'" if other else "") + ".")
    if host and host_from_face:
        msg += (f" Activate '{host}' with design_activate_component and take 'face' from "
                "find_geometry there.")
    return msg + " " + _PROFILE_CUT_REMEDY


def _ownership_readback(host, feature, sketch):
    """(the sides that read back as a DIFFERENT component, the sides whose owner would not read) for
    the hole feature and its placement sketch. Every comparison is TRI-STATE: only a PROVEN
    difference is a mismatch, and an identity that did not read is carried out as unreadable."""
    mismatched, unreadable = [], []
    for label, entity in (("the hole feature", feature), ("its placement sketch", sketch)):
        if entity is None:
            continue
        got = safe(lambda e=entity: e.parentComponent)
        verdict = _common.same_component(got, host)
        if verdict is False:
            # the OTHER component is named only when its name reads: a difference was proven, so an
            # unreadable name is stated as one rather than interpolated as the literal None.
            got_name = safe(lambda g=got: g.name)
            mismatched.append(f"{label} sits in '{got_name}'" if got_name
                              else f"{label} sits in another component")
        elif verdict is not True:
            unreadable.append(label)
    return mismatched, unreadable


def _sketch_space_point(sketch, x, y, z, to_model=None):
    """A WORLD point (cm) in the sketch's own space: (u, v, off_plane) in cm, or (None, None, None).
    `to_model` is _world_lift's matrix, applied first when the sketch's model space is not world.
    The space a placement sketch reads follows the sketch's OWNER, not the face, so no fixed
    occurrence transform serves both cases - modelToSketchSpace is the converter that does."""
    p = safe(lambda: adsk.core.Point3D.create(x, y, z))
    if p is not None and to_model is not None and not safe(lambda: p.transformBy(to_model)):
        return None, None, None
    q = safe(lambda: sketch.modelToSketchSpace(p)) if p is not None else None
    c = ((safe(lambda: q.x), safe(lambda: q.y), safe(lambda: q.z))
         if q is not None else (None, None, None))
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in c):
        return None, None, None
    return c


def _feature_warning(feature):
    """The feature's error/warning text, stripped of the platform's markup (Fusion concatenates
    fragments like 'No target body!<b>1 Reference Failures</b><br/>...' - live-verified)."""
    msg = safe(lambda: feature.errorOrWarningMessage) or ""
    msg = re.sub(r"<[^>]+>", " ", msg)
    return re.sub(r"\s+", " ", msg).strip()[:200]


# Axis-coincidence tolerance (cm) for the drill-axis read-back: dedupe + point matching.
_AXIS_TOL_CM = 1e-3


def _point_on_axis(px, py, pz, axis):
    """Perpendicular distance of (px,py,pz) to the axis LINE <= _AXIS_TOL_CM."""
    ox, oy, oz, ux, uy, uz = axis
    wx, wy, wz = px - ox, py - oy, pz - oz
    cx = wy * uz - wz * uy
    cy = wz * ux - wx * uz
    cz = wx * uy - wy * ux
    return (cx * cx + cy * cy + cz * cz) ** 0.5 <= _AXIS_TOL_CM


def _drill_axes(feature):
    """The DISTINCT drill-axis lines among the faces the feature CREATED - one per drilled point (a
    counterbore's two cylinders dedupe to one line), in the parent component's space (cm). Returns
    (axes, readable); readable=False means feature.faces could not be read at all."""
    faces = safe(lambda: feature.faces)
    if faces is None:
        return [], False
    axes = []
    for f in _common.iter_collection(faces):
        geo = safe(lambda f=f: f.geometry)
        o = safe(lambda: geo.origin) if geo is not None else None
        unit = _geom.unit_vector(safe(lambda: geo.axis) if geo is not None else None)
        if o is None or unit is None:
            continue                       # planar/spherical face - carries no drill axis
        origin = [safe(lambda c=c: float(getattr(o, c))) for c in ("x", "y", "z")]
        if not all(isinstance(c, float) for c in origin):
            continue
        cand = tuple(origin) + tuple(unit)
        # same LINE as an already-seen axis (parallel + origin on it, either direction) -> one hole
        dup = False
        for ex in axes:
            dot = cand[3] * ex[3] + cand[4] * ex[4] + cand[5] * ex[5]
            if abs(dot) >= 1.0 - 1e-6 and _point_on_axis(cand[0], cand[1], cand[2], ex):
                dup = True
                break
        if not dup:
            axes.append(cand)
    # The EMPTY verdict is gated on the collection's own count, never on how many items the walk
    # yielded: iter_collection skips an unreadable face, so nothing yielded over a count of 3 means
    # "nothing could be read", not "this feature drilled no holes".
    if axes or int(safe(lambda: faces.count, 0) or 0) == 0:
        return axes, True
    # faces exist but none exposes a readable axis - inconclusive, never a guessed shortfall
    return [], False


def handler(hole_type: str = "simple", diameter: str = "", face: str = "", points: list = None,
            extent: str = "blind", depth: str = "",
            cbore_diameter: str = "", cbore_depth: str = "",
            csink_diameter: str = "", csink_angle: str = "",
            tap: str = "", fastener: str = "", fit: str = "normal", units: str = "mm",
            placement: str = "sketch_points", points_space: str = "sketch", edge: str = "",
            edge_position: str = "",
            point: list = None, offset_edge_one: str = "", offset_one: str = "",
            offset_edge_two: str = "", offset_two: str = "",
            modeled: bool = False, tip_angle: str = "", thread_type: str = "",
            target_bodies=None) -> dict:
    """See TOOL_DESCRIPTION."""
    hole_type = (hole_type or "simple").strip().lower()
    if hole_type not in _TYPES:
        return error(f"Unknown hole_type '{hole_type}'. Use one of: {', '.join(_TYPES)}.")
    fastener = (fastener or "").strip()
    fit = (fit or "normal").strip().lower()
    if fastener:
        if fit not in _FITS:
            return error(f"Unknown fit '{fit}'. Use one of: {', '.join(_FITS)}.")
        # the fastener sizes the through-diameter from the clearance table (overrides 'diameter')
        cd, cerr = _clearance_diameter(fastener, fit)
        if cerr:
            return error(cerr)
        # format without a trailing '.0' (9.0 -> '9 mm', 6.6 -> '6.6 mm')
        diameter = f"{cd:g} mm"
    if not diameter:
        return error("Provide 'diameter' (e.g. '8 mm') or a 'fastener' (e.g. 'M6 Socket Head Cap "
                     "Screw') to size the hole.")

    placement = (placement or "sketch_points").strip().lower()
    if placement not in _PLACEMENTS:
        return error(f"Unknown placement '{placement}'. Use one of: {', '.join(_PLACEMENTS)}.")
    points_space, pserr = _POINTS_SPACE.resolve(points_space)
    if pserr:
        return error(pserr)
    if points_space == "world" and placement != "sketch_points":
        # it would otherwise be accepted and do nothing - the other placements take no 'points'
        return error(f"points_space='world' applies to placement='sketch_points' (got "
                     f"'{placement}', which positions the hole off 'edge'/offsets instead).")
    pts = points or []
    edge_position = (edge_position or "").strip().lower()
    pt = point or []
    if placement == "sketch_points":
        if not pts:
            return error("Provide 'points' - a list of [x, y, z] positions on the face to drill at.")
    elif placement == "center":
        if not edge:
            return error("placement='center' needs 'edge' - a find_geometry handle at the circular/"
                         "elliptical edge to center the hole on.")
    elif placement == "on_edge":
        if not edge:
            return error("placement='on_edge' needs 'edge' - a find_geometry handle at the edge to "
                         "position the hole along.")
        if edge_position not in _EDGE_POSITIONS:
            return error(f"placement='on_edge' needs 'edge_position' - one of: "
                         f"{', '.join(_EDGE_POSITIONS)}.")
    else:   # plane_offsets
        if not pt:
            return error("placement='plane_offsets' needs 'point' - an approximate [x, y, z] hole "
                         "location (picks the solution when several are possible).")
        if not offset_edge_one or not offset_one:
            return error("placement='plane_offsets' needs 'offset_edge_one' and 'offset_one'.")
        if bool(offset_edge_two) != bool(offset_two):
            return error("placement='plane_offsets': 'offset_edge_two' and 'offset_two' must be "
                         "given together.")

    # type-specific dimensions (before extent details, so the most fundamental gap is reported first)
    if hole_type == "counterbore" and (not cbore_diameter or not cbore_depth):
        return error("A counterbore hole needs 'cbore_diameter' and 'cbore_depth'.")
    if hole_type == "countersink" and (not csink_diameter or not csink_angle):
        return error("A countersink hole needs 'csink_diameter' and 'csink_angle' (e.g. '90 deg').")

    if modeled and not tap:
        return error("'modeled' (a real helical thread) only applies to a tapped hole; pass 'tap' too.")

    extent = (extent or "blind").strip().lower()
    if extent not in _EXTENTS:
        return error(f"Unknown extent '{extent}'. Use 'blind' (with 'depth') or 'through'.")
    if extent == "blind" and not depth:
        return error("A blind hole needs 'depth' (e.g. '10 mm'). For a hole through the body use "
                     "extent='through'.")
    if extent == "blind":
        # A zero/negative literal reached the API bare and came back as a non-diagnostic
        # passthrough (measured with depth='0 mm') - refuse it with the cause named. An
        # expression falls through for the API to evaluate.
        try:
            num = float(str(depth).strip().split()[0])
        except (ValueError, IndexError):
            num = None
        if num is not None and num <= 0:
            return error(f"depth='{depth}' is not a drillable hole - a blind hole needs a "
                         "POSITIVE depth (e.g. '10 mm'), or use extent='through'.")

    design = _common.design()
    if not design:
        return error("No active design.")
    active = _target_component(design)
    if not active:
        return error("No target component.")

    # every placement mode below takes this as its planarEntity.
    face_ent, ferr = _resolve_face(design, face)   # _FACE.resolve returns (entity, error)
    if ferr:
        return error(ferr)
    if not face_ent:
        return error("Could not resolve 'face' to a planar face. Pass a find_geometry face handle.")

    # 'face' is resolved first because the face names the component this hole is built in.
    comp, host_from_face = _feature_host(face_ent, active)
    host_name = safe(lambda: comp.name)
    host_named = f" '{host_name}'" if host_name else ""

    edge_ent = None
    offset_edge_one_ent = None
    offset_edge_two_ent = None
    if placement in ("center", "on_edge"):
        edge_ent, eerr = _resolve_edge(design, edge)
        if eerr:
            return error(eerr)
        if not edge_ent:
            return error("Could not resolve 'edge' to an edge. Pass a find_geometry edge handle.")
        if placement == "center":
            center_err = _require_center_edge(edge_ent)
            if center_err:
                return error(center_err)
    elif placement == "plane_offsets":
        offset_edge_one_ent, oe1err = _resolve_offset_edge_one(design, offset_edge_one)
        if oe1err:
            return error(oe1err)
        if not offset_edge_one_ent:
            return error("Could not resolve 'offset_edge_one' to an edge.")
        lin_err = _require_linear_edge(offset_edge_one_ent, "offset_edge_one")
        if lin_err:
            return error(lin_err)
        if offset_edge_two:
            offset_edge_two_ent, oe2err = _resolve_offset_edge_two(design, offset_edge_two)
            if oe2err:
                return error(oe2err)
            if not offset_edge_two_ent:
                return error("Could not resolve 'offset_edge_two' to an edge.")
            lin_err = _require_linear_edge(offset_edge_two_ent, "offset_edge_two")
            if lin_err:
                return error(lin_err)

    scoped_bodies = None
    if target_bodies not in (None, "", []):
        scoped_bodies, berr = _TARGET_BODIES.resolve(target_bodies)
        if berr:
            return error(berr)

    # Resolve tap thread + clearance fastener BEFORE building geometry (a later raise aborts the script).
    thread_info = None
    if tap:
        thread_info, carried_by, terr = _threads.resolve_thread_info(
            comp, tap.strip(), internal=True, thread_type=thread_type)
        if terr:
            return error(terr)
    clearance_info = None
    if fastener:
        clearance_info, cerr2 = _resolve_clearance(comp, fastener, fit)
        if cerr2:
            return error(cerr2)

    # NB: a valid-but-EMPTY Fusion collection evaluates falsy (count==0). Test `is None`, never `not`.
    holes = safe(lambda: comp.features.holeFeatures)
    if holes is None:
        return error("This component does not support hole features.")

    hin, berr = _build_input(holes, hole_type, diameter, cbore_diameter, cbore_depth,
                             csink_diameter, csink_angle)
    if berr:
        return error(berr)

    factor = _common.scale(units)
    if factor is None:
        return error(f"Unknown units '{units}'. Use mm, cm, or in.")

    sketch = None
    sketch_pts = []
    scaled_pts = []            # the raw scaled (cm) coords, for best-effort naming of failed points
    world_lift = None          # the component whose placement carried the world points, if any

    def _rollback_sketch():
        """Roll the placement sketch back; the clause disclosing a rollback that did NOT report
        success, or '' when there is no sketch or deleteMe answered true."""
        if sketch is None:
            return ""
        name = safe(lambda: sketch.name)
        if bool(safe(lambda: sketch.deleteMe(), False)):
            return ""
        named = f" '{name}'" if name else ""
        return (f" Rolling the placement sketch{named} back did not report success, so it may "
                "still sit on the face - remove it from the timeline with design_delete_feature.")

    def _abandon(msg):
        """Error exit AFTER the placement sketch exists: roll the sketch back first, so a refused
        or failed hole never leaves an orphaned placement sketch on the face."""
        return error(msg + _rollback_sketch())

    if placement == "sketch_points":
        # The sketch is created THROUGH the host, so a host the platform will not take the face in
        # refuses here rather than being silently swapped for one that would.
        try:
            sketch = comp.sketches.add(face_ent)
        except Exception as e:
            return error(_no_placement_sketch_error(comp, active, f": {e}", host_from_face))
        if not sketch:
            return error(_no_placement_sketch_error(comp, active,
                                                    " (sketches.add returned nothing)",
                                                    host_from_face))
        # The sketch's OWNER decides the space modelToSketchSpace converts from, so outside the root
        # a 'world' point is carried into that component's frame first - the leg that lets a hole in
        # a NESTED component be placed by world coordinates at all.
        to_model = None
        if points_space == "world":
            to_model, lerr = _world_lift(design, sketch, safe(lambda: face_ent.assemblyContext))
            if lerr:
                return _abandon(lerr)
            if to_model is not None:
                world_lift = safe(lambda: sketch.parentComponent.name)
        for xyz in pts:
            try:
                sx, sy, sz = (float(xyz[0]) * factor, float(xyz[1]) * factor,
                              float(xyz[2]) * factor)
            except Exception:
                return _abandon(f"Bad point {xyz!r}; expected [x, y, z] in '{units}'.")
            local = (sx, sy, sz)
            if points_space == "world":
                # sketchPoints.add takes SKETCH coordinates, and the sketch's own converter is the
                # only thing that knows the mapping - see _sketch_space_point.
                u, v, off = _sketch_space_point(sketch, sx, sy, sz, to_model)
                if u is None:
                    return _abandon(f"Point {xyz!r} could not be carried into the placement "
                                    "sketch's space. Retry with points_space='sketch'.")
                if abs(off) > _OFF_PLANE_TOL_CM:
                    return _abandon(f"Point {xyz!r} lies {round(off / factor, 4):g} '{units}' off "
                                    "the plane of 'face', and a points_space='world' point must lie "
                                    "ON that face - projecting it would drill somewhere else. Take "
                                    "the position from find_geometry on this face.")
                local = (u, v, 0.0)
            p = safe(lambda local=local: adsk.core.Point3D.create(*local))
            if p is None:
                return _abandon(f"Could not build a point from {xyz!r} (in '{units}').")
            sp = safe(lambda p=p: sketch.sketchPoints.add(p))
            if not sp:
                return _abandon(f"Could not add a sketch point at {xyz!r}.")
            sketch_pts.append(sp)
            scaled_pts.append((sx, sy, sz))
        # These setters run with the placement sketch already on the face, so a raise leaves through
        # _abandon - an escaping one orphans the sketch.
        try:
            if len(sketch_pts) == 1:
                setter = "setPositionBySketchPoint"
                placed = hin.setPositionBySketchPoint(sketch_pts[0])
            else:
                coll = _object_collection()
                for sp in sketch_pts:
                    coll.add(sp)
                setter = "setPositionBySketchPoints"
                placed = hin.setPositionBySketchPoints(coll)
        except Exception as e:
            return _abandon(f"Could not collect or position the placement sketch point(s): {e}")
        if placed is False:
            return _abandon(f"Fusion refused the sketch-point placement ({setter} returned false), "
                            "so nothing was drilled.")
        n_expected = len(sketch_pts)
    elif placement == "center":
        try:
            placed = hin.setPositionAtCenter(face_ent, edge_ent)
        except Exception as e:
            return error(f"Could not position the hole at the edge's center: {e}")
        if placed is False:
            return error("Fusion refused to centre the hole on that edge, so nothing was placed. "
                         "Check that 'edge' is a circular/elliptical edge ON 'face'.")
        n_expected = 1
    elif placement == "on_edge":
        pos_attr = _EDGE_POSITION_ATTRS[edge_position]
        pos_enum = getattr(adsk.fusion.HoleEdgePositions, pos_attr, None)
        if pos_enum is None:
            return error(f"HoleEdgePositions.{pos_attr} is not available on this Fusion version.")
        try:
            placed = hin.setPositionOnEdge(face_ent, edge_ent, pos_enum)
        except Exception as e:
            return error(f"Could not position the hole on the edge: {e}")
        if placed is False:
            return error(f"Fusion refused to place the hole at the '{edge_position}' of that edge, "
                         "so nothing was placed. Check that 'edge' borders 'face'.")
        n_expected = 1
    else:   # plane_offsets
        try:
            pt3d = adsk.core.Point3D.create(float(pt[0]) * factor, float(pt[1]) * factor,
                                            float(pt[2]) * factor)
        except Exception:
            return error(f"Bad point {pt!r}; expected [x, y, z] in '{units}'.")
        args = [face_ent, pt3d, offset_edge_one_ent, _value(offset_one)]
        if offset_edge_two_ent is not None:
            args += [offset_edge_two_ent, _value(offset_two)]
        try:
            placed = hin.setPositionByPlaneAndOffsets(*args)
        except Exception as e:
            return error(f"Could not position the hole by plane and offsets: {e}")
        if placed is False:
            return error("Fusion refused the plane-and-offsets placement, so nothing was placed. "
                         "Check that both offset edges border 'face' and the offsets reach a point "
                         "on it.")
        n_expected = 1

    # Extent (THROUGH must be Positive - verified live).
    if extent == "blind":
        if hin.setDistanceExtent(_value(depth)) is False:
            return _abandon(f"Fusion refused the blind-hole depth '{depth}' (setDistanceExtent "
                            "returned false), so nothing was drilled.")
    else:
        # The HOLE setAllExtent is NOT the retired extrude sibling: it is measured honest on this
        # build (an 8 mm through hole in a 15 mm plate removed exactly the bore volume), so a false
        # answer here is a real refusal and must not be run past.
        if not hin.setAllExtent(_extent_dirs.PositiveExtentDirection):
            return _abandon("Fusion rejected the through-all hole extent (setAllExtent returned "
                            "false), so nothing was drilled. Retry with extent='blind' and a depth "
                            "that clears the body.")

    if tip_angle:
        try:
            hin.tipAngle = _value(tip_angle)
        except Exception as e:
            return _abandon(f"Could not set tip_angle '{tip_angle}': {e}")

    # Tap (after placement/extent; size comes from the designation).
    if thread_info is not None:
        if hin.setToTappedHole(thread_info) is False:
            return _abandon(f"Fusion refused to tap the hole to '{tap}' (setToTappedHole returned "
                            "false), so nothing was drilled.")
        try:
            # isModeled only takes effect after setToTappedHole.
            hin.isModeled = bool(modeled)
        except Exception as e:
            if modeled:
                return _abandon(f"Could not set the tapped hole to a MODELED (helical) thread: {e}")

    # Clearance fastener TAG: records the fastener spec on the feature (the diameter was already set from
    # the table into the base input). setToClearanceHole does NOT resize the geometry on this version.
    if clearance_info is not None:
        if hin.setToClearanceHole(clearance_info) is False:
            return _abandon(f"Fusion refused the clearance-hole spec for '{fastener}' "
                            "(setToClearanceHole returned false), so nothing was drilled.")

    if scoped_bodies is not None:
        try:
            hin.participantBodies = list(scoped_bodies)
        except Exception as e:
            return _abandon(f"Could not set target_bodies before hole creation: {e}")

    try:
        feature = holes.add(hin)         # MUTATION - raises (and aborts) if anything is inconsistent
    except Exception as e:
        # the aborted add leaves no feature - but the placement sketch is real and would orphan
        return _abandon(f"Hole creation failed: {e}")
    if not feature:
        return _abandon(_common.no_feature_error(design, "Hole"))

    # READ THE EFFECT BACK: a point that misses the body cuts nothing while add() still succeeds,
    # leaving only a warning on the feature. The DISTINCT drill axes are counted, one per expected
    # hole; a position-based check is not reliable here. A shortfall rolls the partial feature back.
    axes, verified = _drill_axes(feature)
    n_pts = n_expected
    if verified and len(axes) < n_pts:
        n_missing = n_pts - len(axes)
        named = ""
        where_msg = "Points must lie ON the drilled face."
        if placement == "sketch_points":
            # best-effort naming: when the input coords read as component-space, the points on NO
            # drilled axis are the failures - name them only if they account exactly for the
            # shortfall, never guess.
            unmatched = [pts[i] for i, sc in enumerate(scaled_pts)
                         if not any(_point_on_axis(sc[0], sc[1], sc[2], ax) for ax in axes)]
            named = f" No hole exists at {unmatched} (in '{units}')." if len(unmatched) == n_missing else ""
        else:
            where_msg = ("The position the 'face' and the edge/offset references resolve to "
                         "must lie on the target body.")
        warn = _feature_warning(feature)
        removed = bool(safe(lambda: feature.deleteMe(), False))
        if removed:
            # the sketch goes only once the feature it placed is gone, and a rollback that did not
            # report success is disclosed here too
            tail = ("The partial feature was rolled back; nothing was drilled."
                    + _rollback_sketch())
        else:
            tail = (f"Rollback FAILED - the {len(axes)} drilled hole(s) remain "
                    f"(feature '{safe(lambda: feature.name)}').")
        return error(
            f"{n_missing} of {n_pts} hole point(s) cut NOTHING - the feature created {len(axes)} "
            f"hole(s).{named} {where_msg} {tail}"
            + ("" if host_from_face else _active_host_clause(host_named))
            + (f" Fusion reported: {warn}" if warn else ""))

    # Read the ownership back off the feature and the placement sketch and compare it with the
    # component they were built through; only a PROVEN difference is an error.
    mismatched, unreadable = _ownership_readback(comp, feature, sketch)
    if mismatched:
        feature_name = safe(lambda: feature.name)
        sketch_name = safe(lambda: sketch.name) if sketch is not None else None
        # the placement sketch positioned the feature, so removing one strands the other
        removable = f"'{feature_name}'" if feature_name else "the hole feature"
        if sketch_name:
            removable += f" and the placement sketch '{sketch_name}' it was positioned by"
        return error(f"The hole was created through the host component{host_named}, but "
                     f"{' and '.join(mismatched)}. Remove {removable} with design_delete_feature, "
                     "then retry with 'face' taken from find_geometry on the instance you mean.")

    result = {
        "holes": min(len(axes), n_pts) if verified else n_pts,
        "holes_verified": verified,
        "points": n_pts,
        "hole_type": hole_type,
        "extent": extent,
        "placement": placement,
        "points_space": points_space,
        "feature": safe(lambda: feature.name),
        "host_from_face": host_from_face,
        "host_verified": not unreadable,
        "note": "Hole feature added (a real Hole, with hole/thread metadata - not an extrude-cut). "
                "For a bolt circle, pass every position in 'points' in ONE call - the pattern tools "
                "take bodies/occurrences, not hole features.",
    }
    if host_name:
        # withheld rather than published as a null - a name that did not read is not a component name
        result["host_component"] = host_name
    if sketch is not None:
        result["placement_sketch"] = safe(lambda: sketch.name)
    if scoped_bodies is not None:
        result["scoped_to_bodies"] = [_inputs.qualified_body_name(b) for b in scoped_bodies]
        result["note"] += (" 'scoped_to_bodies' lists the configured participant bodies; Fusion "
                           "does not expose a readback for this input. The face only locates the hole.")
    else:
        result["note"] += " With no target_bodies, Fusion considers every intersected body; the face only locates the hole."
    if not host_from_face:
        result["note"] += _active_host_clause(host_named)
    if unreadable:
        result["note"] += (f" The owning component of {' and '.join(unreadable)} did not read back, "
                           f"so nothing here proves it landed in the host component{host_named}.")
    if world_lift:
        # Disclose the extra step the points took: nothing else in the payload shows that they were
        # carried through a placement rather than handed straight to the sketch's converter.
        result["world_lift_component"] = world_lift
        result["note"] += (" The world points were carried into the frame of component "
                           f"'{world_lift}' through its placement before the sketch converted them.")
    if tap:
        # A tapped hole also creates a ThreadFeature, and the helix flag lives THERE: the
        # HoleFeature has no isModeled and its tappedHoleInfo (a ThreadInfo) has none either.
        name = safe(lambda: feature.name)
        got_tap = safe(lambda: feature.tappedHoleInfo.threadDesignation)
        if not got_tap:
            return error(f"The hole was drilled but carries no tap, so '{tap.strip()}' did not "
                         f"take. Remove '{name}' with design_delete_feature.")
        if got_tap != tap.strip():
            return error(f"The hole was tapped '{got_tap}', not the requested '{tap.strip()}'. "
                         f"Remove '{name}' with design_delete_feature.")
        result["tapped"] = got_tap
        result["thread_type"] = safe(lambda: feature.tappedHoleInfo.threadType)
        if len(carried_by) > 1:
            result["thread_type_alternatives"] = carried_by
        got_modeled = safe(lambda: feature.thread.isModeled)
        if not isinstance(got_modeled, bool):
            if modeled:
                return error("A modeled thread was requested, but the hole's thread feature could "
                             f"not be read back, so there is no proof the helix was cut. Remove "
                             f"'{name}' with design_delete_feature.")
        elif got_modeled != bool(modeled):
            return error(f"The tap was requested {'modeled' if modeled else 'cosmetic'} but reads "
                         f"back {'modeled' if got_modeled else 'cosmetic'}. Remove '{name}' with "
                         "design_delete_feature.")
        else:
            result["modeled"] = got_modeled
    if tip_angle:
        got_tip = safe(lambda: feature.tipAngle.value)
        if isinstance(got_tip, float):
            result["tip_angle"] = f"{round(math.degrees(got_tip), 4):g} deg"
    if fastener:
        result["fastener"] = fastener
        result["fit"] = fit
        result["clearance_diameter"] = diameter
        result["note"] = ("Clearance hole drilled + TAGGED for " + fastener + " (" + fit + " fit). "
                          "Diameter set from the standard clearance table (the API tags the fastener but "
                          "doesn't auto-size on this version).")
    return ok(result)


TOOL_DESCRIPTION = (
    "Drill holes with the Hole feature, so it carries hole and thread metadata; several 'points' "
    "make ONE patterned feature."
)

tool = (
    Tool.create_simple(name="model_hole", description=TOOL_DESCRIPTION)
    .add_input_property("hole_type", {"type": "string", "enum": list(_TYPES)})
    .add_input_property("diameter", {"type": "string", "description": "e.g. '8 mm'."})
    .add_input_property("face", _FACE.schema())
    .add_input_property("points", {"type": "array", "items": {"type": "array", "items": {"type": "number"}},
            "description": "Positions in 'units', in the frame 'points_space' names."})
    .add_input_property(*_POINTS_SPACE.as_property())
    .add_input_property(*_inputs.UNITS.as_property())
    .add_input_property("extent", {"type": "string", "enum": list(_EXTENTS),
            "description": "'blind' needs 'depth'."})
    .add_input_property("depth", {"type": "string", "description": "e.g. '10 mm'."})
    .add_input_property("cbore_diameter", {"type": "string"})
    .add_input_property("cbore_depth", {"type": "string"})
    .add_input_property("csink_diameter", {"type": "string"})
    .add_input_property("csink_angle", {"type": "string", "description": "e.g. '90 deg'."})
    .add_input_property("tap", {"type": "string", "description": "e.g. 'M5x0.8'."})
    .add_input_property("thread_type", {"type": "string",
            "description": "When several standards carry 'tap'."})
    .add_input_property("modeled", {"type": "boolean",
            "description": "Default cosmetic; true cuts the helix."})
    .add_input_property("tip_angle", {"type": "string", "description": "e.g. '118 deg'."})
    .add_input_property("fastener", {"type": "string",
            "description": "e.g. 'M6 Socket Head Cap Screw'; overrides 'diameter'."})
    .add_input_property("fit", {"type": "string", "enum": list(_FITS)})
    .add_input_property("target_bodies", _TARGET_BODIES.schema())
    .add_input_property("placement", {"type": "string", "enum": list(_PLACEMENTS)})
    .add_input_property(*_EDGE.as_property())
    .add_input_property("edge_position", {"type": "string", "enum": list(_EDGE_POSITIONS),
            "description": "For placement=on_edge."})
    .add_input_property("point", {"type": "array", "items": {"type": "number"},
            "description": "Approximate [x,y,z] in the frame of the component owning 'face'."})
    .add_input_property(*_OFFSET_EDGE_ONE.as_property())
    .add_input_property("offset_one", {"type": "string", "description": "e.g. '10 mm'."})
    .add_input_property(*_OFFSET_EDGE_TWO.as_property(brief=True))
    .add_input_property("offset_two", {"type": "string", "description": "e.g. '10 mm'."})
    .strict_schema()
)
item = Item.create_tool_item(tool=tool, write="write", handler=handler, run_on_main_thread=True,
                             postconditions=[_assert.FeatureHealthy()],
                             verification=Verification(
                                 kind="inline", rung="geometry",
                                 evidence_test="tests/unit/test_model_hole.py"
                                               "::TestPerPointVerification"
                                               "::test_partial_cut_errors_and_rolls_back"))


def register_tool():
    register(item)
