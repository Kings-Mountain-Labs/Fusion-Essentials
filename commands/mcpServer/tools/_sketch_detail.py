# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Detail engine behind sketch_get: X-rays ONE sketch - entities, construction geometry,
constraints, dimensions - and owns the sketch's world FRAME. Entity ids ('<type>:<index>') are the
references sketch_constrain / model_extrude / sketch_add_geometry take."""

import importlib
import math
import types

import adsk.core
import adsk.fusion

from ._common import ok, error, safe, find_sketch, all_sketch_names
from . import _common
from . import _geom
from . import _inputs

app = adsk.core.Application.get()

MAP_BLURB = (
    "sketch_get's X-ray; sketch_world_frame/frame_space_note - a sketch's frame + sentence; "
    "curve_id - a curve's '<type>:<index>'; scope_component/scope_components/COMPONENT_SCOPE/"
    "component_scope/scoped_sketch/scoped_or_recent_sketch/scope_remedy - the 'component' scope "
    "+ wire form; _sketch_summary - a sketch's row; _prepare/_transform - move/copy matrix; "
    "unquote_text/font_read_back - SketchText.")


def unquote_text(expr):
    """The textParameter expression is a quoted string ('foo'); return the inner text."""
    if expr is None:
        return None
    s = str(expr)
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        return s[1:-1]
    return s


def font_read_back(obj):
    """The font the landed/edited SketchText reports, or None if it will not read as a name."""
    value = safe(lambda: obj.fontName)
    return value if isinstance(value, str) and value else None


def _plane_normal(x_world, y_world):
    """The unit normal of the plane two in-plane [x, y, z] axis vectors span, or None if they are
    parallel and span none."""
    cross = types.SimpleNamespace(
        x=x_world[1] * y_world[2] - x_world[2] * y_world[1],
        y=x_world[2] * y_world[0] - x_world[0] * y_world[2],
        z=x_world[0] * y_world[1] - x_world[1] * y_world[0])
    n = _geom.unit_vector(cross)
    # + 0.0 turns IEEE -0.0 back into 0.0; a lifted frame's axes carry signed zeros.
    return None if n is None else [c + 0.0 for c in n]


WORLD_SPACE = "world"
COMPONENT_LOCAL_SPACE = "component_local"

# The one sentence each space owes the caller, keyed by frame['space'].
FRAME_SPACE_NOTE = {
    WORLD_SPACE: (
        "'frame' maps sketch coords to WORLD (frame.space='world'): sketch (0,0) sits at "
        "frame.origin_mm, +X runs along frame.x_world, +Y along frame.y_world, and frame.normal is "
        "the plane's world normal - place geometry from those, not by eye."),
    COMPONENT_LOCAL_SPACE: (
        "'frame' is COMPONENT-LOCAL, not world (frame.space='component_local'): no single "
        "placement of this sketch resolved, so the in-plane axes are published as frame.x_local / "
        "frame.y_local, not frame.x_world. The numbers map this sketch's own entity coordinates. "
        "For a world frame, name the instance: sketch_get(component='<occurrence "
        "fullPathName>'); design_get(include=['tree']) lists the paths."),
}


def frame_space_note(frame) -> str:
    """The one sentence naming the space `frame`'s numbers are in; an unreadable frame (None) gets
    the component-local wording."""
    space = (frame or {}).get("space") if isinstance(frame, dict) else None
    return FRAME_SPACE_NOTE.get(space, FRAME_SPACE_NOTE[COMPONENT_LOCAL_SPACE])


def _placement_count(root, comp):
    """How many times `root`'s assembly places `comp` - 0 for a component nothing places there, and
    0 for `root` itself, which its own design places nowhere."""
    occs = safe(lambda: root.allOccurrencesByComponent(comp)) if comp is not None else None
    return (safe(lambda: occs.count, 0) or 0) if occs is not None else 0


def _frame_context(sketch, design, occurrence=None):
    """(the sketch the frame is read off, the space those numbers are in), resolved against
    `design`'s root and through `occurrence` when the caller named one."""
    # Sketch.origin/xDirection/yDirection read in the sketch's PARENT COMPONENT space, not the
    # assembly's, so a sub-component's sketch is lifted through its placement first; a component
    # placed several times, or none, has no one world frame and stays component_local.
    def _lift(through):
        # A proxy that will not read leaves the NATIVE numbers, which are component-local.
        proxy = safe(lambda: sketch.createForAssemblyContext(through))
        return (proxy, WORLD_SPACE) if proxy is not None else (sketch, COMPONENT_LOCAL_SPACE)

    if occurrence is not None:
        return _lift(occurrence)
    root = safe(lambda: design.rootComponent) if design is not None else None
    if root is None:
        return sketch, COMPONENT_LOCAL_SPACE
    occ, err = _inputs.single_placement("the sketch", sketch, root, design)
    if err:
        return sketch, COMPONENT_LOCAL_SPACE
    if occ is not None:
        return _lift(occ)
    # Root components in DIFFERENT documents read ONE entityToken, so comparing this root against
    # the sketch's owner answers SAME for an x-ref'd sketch. The placement census settles ownership
    # without that comparison: a design places its own root nowhere.
    if safe(lambda: sketch.assemblyContext) is not None:
        return sketch, WORLD_SPACE          # already in the assembly's space; nothing to census
    owner = safe(lambda: sketch.parentComponent)
    if owner is None:
        return sketch, COMPONENT_LOCAL_SPACE
    if _placement_count(root, owner) == 0:
        return sketch, WORLD_SPACE
    # Placed here, so ask the walk again with no component to compare against - its
    # `comp is not None` guard makes None mean "decide on placement alone".
    occ, _err = _inputs.single_placement("the sketch", sketch, None, design)
    return _lift(occ) if occ is not None else (sketch, COMPONENT_LOCAL_SPACE)


def sketch_world_frame(sketch, design, occurrence=None) -> dict:
    """Where sketch (0,0) lands, where +X/+Y point (unit axes, keyed 'x_world' or 'x_local' by the
    published 'space'), and the plane's normal - origin_mm in mm. None when the plane cannot be
    read. `occurrence`, when the caller reached the sketch through a placement, is lifted through."""
    def _vec(g):
        # The lift is a matrix multiply, so an axis component that should be zero arrives as a tiny
        # signed residue that round() collapses to -0.0; + 0.0 turns that back into 0.0.
        return [round(safe(lambda: g.x, 0.0) or 0.0, 6) + 0.0,
                round(safe(lambda: g.y, 0.0) or 0.0, 6) + 0.0,
                round(safe(lambda: g.z, 0.0) or 0.0, 6) + 0.0]

    lifted, space = _frame_context(sketch, design, occurrence)
    op = safe(lambda: lifted.origin)            # Point3D of sketch (0,0) in `space`
    xd = safe(lambda: lifted.xDirection)        # Vector3D of sketch +X in `space`
    yd = safe(lambda: lifted.yDirection)        # Vector3D of sketch +Y in `space`
    if op is None or xd is None or yd is None:
        return None
    x_axis, y_axis = _vec(xd), _vec(yd)
    frame = {
        "origin_mm": [round((safe(lambda: op.x, 0.0) or 0.0) * 10, 4) + 0.0,
                      round((safe(lambda: op.y, 0.0) or 0.0) * 10, 4) + 0.0,
                      round((safe(lambda: op.z, 0.0) or 0.0) * 10, 4) + 0.0],
        "normal": _plane_normal(x_axis, y_axis),
        "space": space,
    }
    # Assigned by name in each branch, not through a lookup: the disclosure lint pins the literals.
    if space == WORLD_SPACE:
        frame["x_world"], frame["y_world"] = x_axis, y_axis
    else:
        frame["x_local"], frame["y_local"] = x_axis, y_axis
    return frame


# local aliases (this module's own record builders read them under the short names)
_unquote = unquote_text
_font_read_back = font_read_back

# Constraint class -> the spelling sketch_constrain's 'constraint' input takes + the attributes
# holding its referenced SKETCH entities. A surface-side attribute is a model face or construction
# plane, outside this sketch's id space, so listing it would only add a '?'.
_CONSTRAINT_REFS = {
    "PerpendicularConstraint": ("perpendicular", ("lineOne", "lineTwo")),
    "ParallelConstraint": ("parallel", ("lineOne", "lineTwo")),
    "CollinearConstraint": ("collinear", ("lineOne", "lineTwo")),
    "TangentConstraint": ("tangent", ("curveOne", "curveTwo")),
    "EqualConstraint": ("equal", ("curveOne", "curveTwo")),
    "ConcentricConstraint": ("concentric", ("entityOne", "entityTwo")),
    "SymmetryConstraint": ("symmetry", ("entityOne", "entityTwo", "symmetryLine")),
    "HorizontalConstraint": ("horizontal", ("line",)),
    "VerticalConstraint": ("vertical", ("line",)),
    "HorizontalPointsConstraint": ("horizontal_points", ("pointOne", "pointTwo")),
    "VerticalPointsConstraint": ("vertical_points", ("pointOne", "pointTwo")),
    "CoincidentConstraint": ("coincident", ("point", "entity")),
    "CoincidentToSurfaceConstraint": ("coincident_to_surface", ("point",)),
    "LineOnPlanarSurfaceConstraint": ("line_on_surface", ("line",)),
    "LineParallelToPlanarSurfaceConstraint": ("line_parallel_to_surface", ("line",)),
    "PerpendicularToSurfaceConstraint": ("perpendicular_to_surface", ("curve",)),
    "MidPointConstraint": ("midpoint", ("point", "midPointCurve")),
    "SmoothConstraint": ("smooth", ("curveOne", "curveTwo")),
    "OffsetConstraint": ("offset", ("parentCurves", "childCurves")),
    "PolygonConstraint": ("polygon", ("lines",)),          # 'lines' is a vector (many)
    "CircularPatternConstraint": ("circular_pattern", ("centerPoint", "entities")),
    "RectangularPatternConstraint": ("rectangular_pattern",
                                     ("entities", "directionOneEntity", "directionTwoEntity")),
}


def _round(v, f):
    """v scaled by the cm -> display-unit factor f and rounded to 4dp, or None if v is None."""
    return round(float(v) * f, 4) if v is not None else None


def _curve_collections(sketch):
    """(kind, collection) for every curve kind resolve_entity_ref addresses, in id order."""
    curves = safe(lambda: sketch.sketchCurves)
    for kind, coll_get in (("line", lambda: curves.sketchLines),
                           ("arc", lambda: curves.sketchArcs),
                           ("circle", lambda: curves.sketchCircles),
                           ("ellipse", lambda: curves.sketchEllipses),
                           ("elliptical_arc", lambda: curves.sketchEllipticalArcs),
                           ("conic", lambda: curves.sketchConicCurves),
                           ("spline", lambda: curves.sketchFittedSplines),
                           ("cv_spline", lambda: curves.sketchControlPointSplines),
                           ("fixed_spline", lambda: curves.sketchFixedSplines)):
        yield kind, safe(coll_get)


def curve_id(sketch, curve):
    """'<type>:<index>' for one curve, matched by IDENTITY against the sketch's own collections, or
    None when it is not among them - the two pieces a split returns share ONE entityToken."""
    for kind, coll in _curve_collections(sketch):
        for i in range(safe(lambda coll=coll: coll.count, 0) if coll else 0):
            if safe(lambda coll=coll, i=i: coll.item(i) == curve) is True:
                return f"{kind}:{i}"
    return None


def _build_token_map(sketch):
    """Map entityToken -> '<type>:<index>' for every entity kind resolve_entity_ref addresses -
    _curve_collections' kinds plus 'point'."""
    tok2id = {}
    for kind, coll in _curve_collections(sketch):
        for i in range(safe(lambda coll=coll: coll.count, 0) if coll else 0):
            tok = safe(lambda coll=coll, i=i: coll.item(i).entityToken)
            if tok:
                tok2id[tok] = f"{kind}:{i}"
    pts = safe(lambda: sketch.sketchPoints)
    for i in range(safe(lambda: pts.count, 0) if pts else 0):
        tok = safe(lambda i=i: pts.item(i).entityToken)
        if tok:
            tok2id[tok] = f"point:{i}"
    return tok2id


_Z_EPS = 1e-6   # cm; a sketch point within this of the plane is on-plane and its z is omitted


def _xy(geo, f):
    """{x, y} for a sketch point's geometry in display units, plus 'z' (the sketch-local height
    along the plane normal) when the point sits OFF the sketch plane."""
    if geo is None:
        return None
    z = safe(lambda: geo.z, 0.0) or 0.0
    rec = {"x": _round(geo.x, f), "y": _round(geo.y, f)}
    if abs(z) > _Z_EPS:
        rec["z"] = _round(z, f)
    return rec


def _line_geo(ln, f):
    s = safe(lambda: ln.startSketchPoint.geometry)
    e = safe(lambda: ln.endSketchPoint.geometry)
    return {"start": _xy(s, f), "end": _xy(e, f)}


def _texts(sketch, f):
    """Every SketchText as a 'text:<index>' entity record, in sketchTexts creation order."""
    # A positional walk: a slot that will not read holds its index with its fields None, since
    # dropping it would slide every later text onto a different id. SketchText.text/.height are
    # retired, so the string and height come off textParameter/heightParameter.
    out = []
    texts = safe(lambda: sketch.sketchTexts)
    for i in range(safe(lambda: texts.count, 0) if texts else 0):
        st = safe(lambda i=i: texts.item(i))
        # textParameter.expression is the string QUOTED for a literal and the BINDING for a bound
        # text ('d7'); textValue is what Fusion renders either way (measured). A bound text is the
        # pair disagreeing, so the expression is published only where it says something else.
        expr = safe(lambda st=st: st.textParameter.expression)
        rendered = safe(lambda st=st: st.textParameter.textValue)
        rec = {"id": f"text:{i}", "type": "text", "construction": False,
               "text": rendered if rendered is not None else _unquote(expr),
               "height": _round(safe(lambda st=st: st.heightParameter.value), f),
               "font": _font_read_back(st)}
        if expr is not None and rendered is not None and _unquote(expr) != rendered:
            rec["text_expression"] = expr
        bb = safe(lambda st=st: st.boundingBox)
        if bb is not None:
            rec["bounding_box"] = {"min": _xy(safe(lambda: bb.minPoint), f),
                                   "max": _xy(safe(lambda: bb.maxPoint), f)}
        out.append(rec)
    return out


# Control points ride INSIDE a row of the _XRAY_CAP-capped entity list, so a traced spline would
# multiply the payload past that bound; control_point_count carries the true total whatever is cut.
_CV_POINT_CAP = 64


def _projected(curve) -> dict:
    """{'reference': True} for a curve Fusion projected in from model geometry, else {} - MEASURED:
    isReference reads True on a projected copy and False on a drawn one."""
    return {"reference": True} if _common.read_flag(lambda: curve.isReference) else {}


def _entities(sketch, f):
    """List every entity with id, type, isConstruction, and key geometry, in display units (f = cm ->
    display-unit factor)."""
    out = []
    curves = safe(lambda: sketch.sketchCurves)
    construction = 0

    lines = safe(lambda: curves.sketchLines)
    for i in range(safe(lambda: lines.count, 0) if lines else 0):
        ln = lines.item(i)
        con = bool(safe(lambda ln=ln: ln.isConstruction, False))
        construction += 1 if con else 0
        rec = {"id": f"line:{i}", "type": "line", "construction": con, **_projected(ln)}
        rec.update(_line_geo(ln, f))
        out.append(rec)

    arcs = safe(lambda: curves.sketchArcs)
    for i in range(safe(lambda: arcs.count, 0) if arcs else 0):
        a = arcs.item(i)
        con = bool(safe(lambda a=a: a.isConstruction, False))
        construction += 1 if con else 0
        c = safe(lambda: a.centerSketchPoint.geometry)
        out.append({"id": f"arc:{i}", "type": "arc", "construction": con, **_projected(a),
        "center": _xy(c, f),
        "radius": _round(safe(lambda: a.radius), f)})

    circles = safe(lambda: curves.sketchCircles)
    for i in range(safe(lambda: circles.count, 0) if circles else 0):
        cc = circles.item(i)
        con = bool(safe(lambda cc=cc: cc.isConstruction, False))
        construction += 1 if con else 0
        c = safe(lambda: cc.centerSketchPoint.geometry)
        out.append({"id": f"circle:{i}", "type": "circle", "construction": con, **_projected(cc),
        "center": _xy(c, f),
        "radius": _round(safe(lambda: cc.radius), f)})

    ellipses = safe(lambda: curves.sketchEllipses)
    for i in range(safe(lambda: ellipses.count, 0) if ellipses else 0):
        el = ellipses.item(i)
        con = bool(safe(lambda el=el: el.isConstruction, False))
        construction += 1 if con else 0
        c = safe(lambda: el.centerSketchPoint.geometry)
        out.append({"id": f"ellipse:{i}", "type": "ellipse", "construction": con, **_projected(el),
        "center": _xy(c, f),
        "major_radius": _round(safe(lambda: el.majorAxisRadius), f),
        "minor_radius": _round(safe(lambda: el.minorAxisRadius), f)})

    ell_arcs = safe(lambda: curves.sketchEllipticalArcs)
    for i in range(safe(lambda: ell_arcs.count, 0) if ell_arcs else 0):
        ea = ell_arcs.item(i)
        con = bool(safe(lambda ea=ea: ea.isConstruction, False))
        construction += 1 if con else 0
        c = safe(lambda: ea.centerSketchPoint.geometry)
        out.append({"id": f"elliptical_arc:{i}", "type": "elliptical_arc", "construction": con,
        **_projected(ea), "center": _xy(c, f),
        "major_radius": _round(safe(lambda: ea.majorAxisRadius), f),
        "minor_radius": _round(safe(lambda: ea.minorAxisRadius), f)})

    conics = safe(lambda: curves.sketchConicCurves)
    for i in range(safe(lambda: conics.count, 0) if conics else 0):
        cn = conics.item(i)
        con = bool(safe(lambda cn=cn: cn.isConstruction, False))
        construction += 1 if con else 0
        # A conic is shaped by an APEX and a rho, not by a centre and a radius.
        apex = safe(lambda: cn.apexSketchPoint.geometry)
        out.append({"id": f"conic:{i}", "type": "conic", "construction": con, **_projected(cn),
        "apex": _xy(apex, f), "rho": _round(safe(lambda: cn.rhoValue), 1.0)})

    splines = safe(lambda: curves.sketchFittedSplines)
    for i in range(safe(lambda: splines.count, 0) if splines else 0):
        sp = splines.item(i)
        con = bool(safe(lambda sp=sp: sp.isConstruction, False))
        construction += 1 if con else 0
        fit_pts = safe(lambda sp=sp: sp.fitPoints)
        out.append({"id": f"spline:{i}", "type": "spline", "construction": con, **_projected(sp),
        "is_closed": safe(lambda sp=sp: bool(sp.isClosed)),
        "fit_point_count": safe(lambda fit_pts=fit_pts: fit_pts.count) if fit_pts is not None else None})

    cv_splines = safe(lambda: curves.sketchControlPointSplines)
    for i in range(safe(lambda: cv_splines.count, 0) if cv_splines else 0):
        cv = cv_splines.item(i)
        con = bool(safe(lambda cv=cv: cv.isConstruction, False))
        construction += 1 if con else 0
        # MEASURED: controlPoints is a SketchPointVector - a plain SEQUENCE whose .count and
        # .item(i) BOTH raise, so a collection-style read answers null on every spline. list() is
        # the protocol it does carry, and its members are ordinary SketchPoints.
        ctrl_pts = safe(lambda cv=cv: list(cv.controlPoints))
        # MEASURED on an SVG-traced spline: controlPoints reads EMPTY and .degree RAISES "Spline
        # has invalid intention degree". No spline is describable by zero control points, so an
        # empty read publishes unknown rather than a 0 a caller would compare against.
        described = bool(ctrl_pts)
        # SketchControlPointSpline has no isClosed (live-verified).
        rec = {"id": f"cv_spline:{i}", "type": "cv_spline", "construction": con, **_projected(cv),
               "degree": safe(lambda cv=cv: cv.degree),
               "control_point_count": len(ctrl_pts) if described else None,
               "control_points": ([_xy(safe(lambda p=p: p.geometry), f)
                                   for p in ctrl_pts[:_CV_POINT_CAP]] if described else None)}
        if described and len(ctrl_pts) > _CV_POINT_CAP:
            rec["control_points_truncated"] = True
        out.append(rec)

    fixed_splines = safe(lambda: curves.sketchFixedSplines)
    for i in range(safe(lambda: fixed_splines.count, 0) if fixed_splines else 0):
        fx = fixed_splines.item(i)
        con = bool(safe(lambda fx=fx: fx.isConstruction, False))
        construction += 1 if con else 0
        # SketchFixedSpline exposes no isClosed/fitPoints/degree (live-verified).
        out.append({"id": f"fixed_spline:{i}", "type": "fixed_spline", "construction": con,
                    **_projected(fx)})

    pts = safe(lambda: sketch.sketchPoints)
    origin = safe(lambda: sketch.originPoint)
    for i in range(safe(lambda: pts.count, 0) if pts else 0):
        g = safe(lambda i=i: pts.item(i).geometry)
        rec = {"id": f"point:{i}", "type": "point", "construction": False,
        "position": _xy(g, f)}
        # the sketch ORIGIN is a real, addressable point entity - flag it so an agent anchoring a
        # constraint to the origin does not have to infer which (0,0) point it is. Proxy equality
        # (not `is`) is the sanctioned entity comparison.
        if origin is not None and safe(lambda i=i: pts.item(i) == origin):
            rec["origin"] = True
        out.append(rec)

    out.extend(_texts(sketch, f))
    return out, construction


def _ent_id(ent, tok2id):
    tok = safe(lambda: ent.entityToken)
    return tok2id.get(tok, "?") if tok else "?"


def _describe_constraint(c, tok2id):
    """Map one geometric constraint to {type, entities:[ids]}. An attribute may be a single entity
    or a VECTOR of entities (e.g. PolygonConstraint.lines) - both are expanded to ids."""
    cls = type(c).__name__
    friendly, attrs = _CONSTRAINT_REFS.get(cls, (cls.replace("Constraint", "").lower(), ()))
    ids = []
    for attr in attrs:
        ent = safe(lambda attr=attr: getattr(c, attr))
        if ent is None:
            continue
        items = _vector_items(ent)
        if items is not None:        # a vector of entities (e.g. PolygonConstraint.lines)
            for sub in items:
                ids.append(_ent_id(sub, tok2id))
        else:
            ids.append(_ent_id(ent, tok2id))
    return {"type": friendly, "entities": ids}


def _vector_items(ent):
    """If ent is a vector/collection of entities, return a list of them; else None. Handles both the
    .count/.item collection idiom AND the SketchLineVector len()/[i] idiom (used by PolygonConstraint
    .lines). A single BRep/sketch entity is NOT a vector - so a plain SketchLine returns None."""
    # A single sketch entity exposes entityToken; treat that as NOT a vector even if it has len.
    if safe(lambda: ent.entityToken) is not None:
        return None
    n = safe(lambda: ent.count, None)
    if n is not None and safe(lambda: ent.item) is not None:
        return [ent.item(i) for i in range(n)]
    n = safe(lambda: len(ent), None)
    if n is not None:
        return [ent[i] for i in range(n)]
    return None


def compute_deferred(sketch):
    """Whether this sketch's compute is DEFERRED - True, False, or None when the flag will not read
    (never coerced: an unreadable flag is not a confident 'no')."""
    return _common.read_flag(lambda: sketch.isComputeDeferred)


# The one sentence a stale-profile read owes. Deferring compute is what sketch_add_geometry does
# around a draw, so the tool that set the flag is also the one that clears it.
DEFERRED_NOTE = (
    "Compute is DEFERRED (isComputeDeferred reads true): profile_count can be stale. A read does "
    "not resume it - draw with sketch_add_geometry, or set isComputeDeferred=false with "
    "sys_execute_script.")


def _profiles(sketch, f):
    """One record (index / area / centroid / loop_count / handle) per closed region, largest area
    first, the handle a ProfileRef for model_extrude / model_revolve / model_loft; f is the
    cm -> display-unit factor. None when compute is DEFERRED - the walk reads a stale profile set."""
    if compute_deferred(sketch) is True:
        return None
    profs = safe(lambda: sketch.profiles)
    n = safe(lambda: profs.count, 0) if profs else 0
    out = []
    sk_name = safe(lambda: sketch.name) or ""
    for i in range(n):
        p = profs.item(i)
        ap = safe(lambda p=p: p.areaProperties())
        area = safe(lambda: ap.area) if ap else None
        c = safe(lambda: ap.centroid) if ap else None
        # The centroid is COMPONENT-LOCAL (cm), not world, and an assembly-context proxy reads the
        # same point; it doubles as the handle's locator.
        pos = (c.x, c.y, c.z) if c else None
        loops = safe(lambda p=p: p.profileLoops.count)
        # findEntityByToken resolves NOTHING for a sub-component sketch profile's token, so the
        # locator is the handle's real resolution path. It carries the RAW cm area, which tells
        # same-centroid profiles apart; a ':' or ',' in the name would garble its parse.
        safe_name = sk_name if (":" not in sk_name and "," not in sk_name) else ""
        kind = f"profile[{safe_name}~{area:.4f}]" if area is not None else "profile"
        out.append({
            "index": i,
            "area": _round(area, f * f),
            "centroid": [_round(c.x, f), _round(c.y, f), _round(c.z, f)] if c else None,
            "loop_count": loops,
            "handle": _inputs.make_handle(p, kind, pos) if pos else safe(lambda: p.entityToken),
        })
    # A positive scale factor preserves order, so sorting on the scaled 'area' still agrees.
    out.sort(key=lambda r: (r["area"] is None, -(r["area"] or 0)))
    return out


_XRAY_CAP = 200   # a dense sketch can carry hundreds of entities/constraints/dimensions; bound each


def _dimension_value(raw, is_angle, f):
    """A dimension's value in the unit its row publishes: DEGREES for an angular dimension, whose
    parameter reads DATABASE units (radians), else the caller's display unit."""
    if raw is None:
        return None
    return _round(math.degrees(raw), 1.0) if is_angle else _round(raw, f)


def _entity_xray(sketch, f, unit, max_results=_XRAY_CAP):
    """(entities, constraints, dimensions, construction_count, driving_dim_count, truncated), each
    array independently capped at max_results and the two counts taken over the UNCAPPED walk.
    f is the cm -> display-unit factor applied to every length value, `unit` its name."""
    tok2id = _build_token_map(sketch)
    entities, construction_count = _entities(sketch, f)

    constraints = []
    gc = safe(lambda: sketch.geometricConstraints)
    for i in range(safe(lambda: gc.count, 0) if gc else 0):
        constraints.append(_describe_constraint(gc.item(i), tok2id))

    dimensions = []
    sd = safe(lambda: sketch.sketchDimensions)
    for i in range(safe(lambda: sd.count, 0) if sd else 0):
        d = sd.item(i)
        par = safe(lambda d=d: d.parameter)
        raw_value = safe(lambda: par.value) if par else None
        # An ANGULAR dimension's value is radians, not a length, so it takes its own conversion.
        is_angle = type(d).__name__ == "SketchAngularDimension"
        dimensions.append({
            "name": safe(lambda: par.name) if par else None,
            "value": _dimension_value(raw_value, is_angle, f),
            # The frame 'value' is in - never the caller's length unit for an ANGLE.
            "value_units": "deg" if is_angle else unit,
            "expression": safe(lambda: par.expression) if par else None,
            # driving = constrains geometry; a driven/reference dim just MEASURES (doesn't lock).
            "driving": bool(safe(lambda d=d: d.isDriving, True)),
            "type": type(d).__name__.replace("SketchDimension", "").replace("Dimension", "").lower(),
        })
    driving_dims = sum(1 for d in dimensions if d.get("driving"))

    cap = max(1, int(max_results))
    entities_out = entities[:cap]
    constraints_out = constraints[:cap]
    dimensions_out = dimensions[:cap]
    truncated = (len(entities_out) < len(entities) or len(constraints_out) < len(constraints)
                 or len(dimensions_out) < len(dimensions))
    return (entities_out, constraints_out, dimensions_out, construction_count, driving_dims, truncated)


# The occurrence half of the 'component' scope's vocabulary, resolved through the SHARED
# ambiguity-refusing resolver (the same kind doc_insert_occurrence's 'into_component' takes) rather
# than a local path matcher. Its .name is the input's name, so its refusals quote 'component' back.
_SCOPE_OCCURRENCE = _inputs.OccurrenceRef(
    "component", description="Occurrence whose component to read.")


def _scope_hits(design, raw):
    """(the components a 'component' scope selects, the OCCURRENCE it named or None, error_or_None):
    blank selects every component, a NAME every component wearing it, an occurrence fullPathName or
    handle the one component that occurrence places - the only vocabulary naming a placement."""
    # Fusion dedupes a component RENAME but not an INSERT, so two referenced documents each bring
    # their own 'Frame' and only the occurrence path tells those two components apart.
    want = (raw or "").strip()
    if not want:
        return _common.all_components(design), None, None
    comps, name_error = _common.components_in_scope(design, want)
    if not name_error:
        return comps, None, None
    occ, occ_error = _SCOPE_OCCURRENCE.resolve(want)
    if occ is not None:
        comp = safe(lambda: occ.component)
        if comp is not None:
            return [comp], occ, None
        # The occurrence RESOLVED and reading its component failed, so ``occ_error`` is None here.
        # ``broken_reference`` is the ONE unresolved-external-reference detector; a component that
        # reads None without raising has no detail to quote.
        _is_broken, detail = _common.broken_reference(occ)
        where = safe(lambda: occ.fullPathName) or want
        because = f": {detail}" if detail else " (the read returned nothing)"
        return None, None, (f"'{want}' resolved to occurrence '{where}', but its component could "
                            f"not be read{because}. design_get(include=['tree']) reports "
                            "occurrences whose referenced component will not load.")
    return None, None, (f"{name_error} As an occurrence path or handle it did not resolve either: "
                        f"{occ_error}")


def scope_components(design, raw):
    """The component(s) a 'component' scope selects, as (components, error_or_None)."""
    comps, _occ, err = _scope_hits(design, raw)
    return comps, err


def scope_component(design, raw, input_name="component"):
    """The ONE component a by-name sketch read scopes to, as (component, the occurrence the scope
    named or None, error_or_None); a name SEVERAL components wear is refused. ``input_name`` is the
    scope input the refusals quote back, which is not always spelled 'component'."""
    want = (raw or "").strip()
    if not want:
        return None, None, (f"Provide a component name in '{input_name}', or omit it to search the "
                            "whole design.")
    comps, occ, err = _scope_hits(design, want)
    if err:
        return None, None, err
    if len(comps) == 1:
        return comps[0], occ, None
    # The match is case-insensitive and strip-tolerant, so 'beta' hits 'Beta' and 'BETA'; the
    # spellings as read are what tells the hits apart.
    spelled = _common.spelled_as_read(comps, want)
    # The paths come from the occurrence WALK filtered by the occurrence's own component name.
    # Two distinct components can read ONE entityToken, so pairing matched components against the
    # walk instead hands every 'Frame' every other 'Frame''s path.
    paths = _common.placement_paths_named(design, want)
    if paths:
        return None, None, (f"{len(comps)} components match '{want}'{spelled}, so the name does not "
                            "identify one of them. Scope by one of their occurrence paths instead: "
                            f"{_common.named_with_remainder(paths)} - '{input_name}' also takes an "
                            "occurrence fullPathName or handle (design_get(include=['tree']) emits "
                            "both).")
    return None, None, (f"{len(comps)} components match '{want}'{spelled}, and no occurrence places "
                        "any of them, so nothing tells them apart. Read them with sketch_get("
                        "component='" + want + "') and no 'sketch_name'.")


# ── the 'component' scope as a WRITE tool's input - the read's scope, wired for by-name EDITS ────

def scope_remedy(input_name="component"):
    """The closing sentence a shared-name refusal carries, naming ``input_name`` as the input that
    narrows THIS sketch reference (see _common.find_sketch's ``remedy``)."""
    return (f"Name the one you mean by passing its owning component as '{input_name}' (sketch_get "
            "lists each sketch's owning component).")

# The vocabulary half of the scope's description: what it accepts. Shared by every spelling of the
# input, so a second scope on one tool cannot drift from the first. WHY it exists, and the
# design_get pointer that mints an occurrence path, are in scope_component's refusals.
_SCOPE_VOCABULARY = "a component name or an occurrence path/handle."

# The ONE wire declaration of that input: tool.add_input_property(*_sketch_detail.COMPONENT_SCOPE),
# resolved through scoped_sketch / scoped_or_recent_sketch below.
COMPONENT_SCOPE = ("component", {"type": "string", "description":
                   "The sketch's component: " + _SCOPE_VOCABULARY})


def component_scope(input_name, narrows=""):
    """(name, schema) for that same scope under a DIFFERENT input name - for a tool whose own
    'component' already names something else, or that scopes a SECOND sketch reference.
    ``narrows`` names the sketch input this scope applies to."""
    lead = (f"The component of '{narrows}': " if narrows
            else "The sketch's component: ")
    return input_name, {"type": "string", "description": lead + _SCOPE_VOCABULARY}


def _scoped_component(design, raw, input_name="component"):
    """(component, error_or_None) for a scope a WRITE was given - scope_component without the
    occurrence, which only a frame read needs."""
    comp, _occ, err = scope_component(design, raw, input_name)
    return comp, err


def scoped_sketch(design, name, component, input_name="component"):
    """(sketch, error_or_None): ONE sketch by name for an EDIT, design-wide with no ``component``
    and out of that component's own collection with one. A scope that WAS passed is always resolved
    and validated, so a component this design does not hold refuses rather than being dropped.
    ``input_name`` names the scope input in all three refusals this can produce."""
    scope = (component or "").strip()
    if not scope:
        return _common.find_sketch(design, name, remedy=scope_remedy(input_name))
    comp, scope_error = _scoped_component(design, scope, input_name)
    if scope_error:
        return None, scope_error
    return _common.find_sketch_in(design, name, comp, scope, input_name)


def scoped_or_recent_sketch(design, name, component, input_name="component"):
    """(sketch, the stripped requested name or None, error_or_None): scoped_sketch's contract with
    a blank NAME meaning "the most recent sketch" - of the scoped component when one was passed, of
    the active component when none was."""
    scope = (component or "").strip()
    nm = (name or "").strip()
    if not scope:
        return _common.find_or_recent_sketch(design, name, remedy=scope_remedy(input_name))
    comp, scope_error = _scoped_component(design, scope, input_name)
    if scope_error:
        return None, (nm or None), scope_error
    if nm:
        sketch, refusal = _common.find_sketch_in(design, nm, comp, scope, input_name)
        return sketch, nm, refusal
    coll = safe(lambda: comp.sketches)
    n = safe(lambda: coll.count, 0) if coll is not None else 0
    if not n:
        read_name = safe(lambda: comp.name)
        where = f"'{read_name}'" if read_name else f"the one '{scope}' resolved to"
        return None, None, (f"Component {where} holds no sketches, so it has no most recent one to "
                            f"act on (scope '{scope}'). Name a sketch in 'sketch_name', or create "
                            "one with sketch_create.")
    return safe(lambda i=n - 1: coll.item(i)), None, None


def _detail_engine():
    """This module, looked up in the module table by NAME at call time - the package attribute is
    bound once, so swapping the engine in the module table would not reach it."""
    return importlib.import_module("._sketch_detail", __package__)


def _plane_name(sketch) -> str:
    rp = safe(lambda: sketch.referencePlane)
    return safe(lambda: rp.name) if rp is not None else None


def _sketch_summary(sketch) -> dict:
    """The per-sketch row (counts + visibility) every sketch payload carries."""
    curves = safe(lambda: sketch.sketchCurves)
    row = {
    "name": safe(lambda: sketch.name),
    "plane": _plane_name(sketch),
    "line_count": safe(lambda: curves.sketchLines.count, 0) if curves else 0,
    "circle_count": safe(lambda: curves.sketchCircles.count, 0) if curves else 0,
    "arc_count": safe(lambda: curves.sketchArcs.count, 0) if curves else 0,
    "point_count": safe(lambda: sketch.sketchPoints.count, 0),
    "profile_count": safe(lambda: sketch.profiles.count, 0),
    "is_visible": safe(lambda: sketch.isVisible),
    }
    # While compute is deferred the profile_count above is the pre-deferral one, and no read of
    # this sketch resumes compute.
    if compute_deferred(sketch) is True:
        row["compute_deferred"] = True
        row["profiles_stale"] = True
    return row


# ── the sketch-space transform both sketch_move and sketch_copy place geometry through ──────────

_ENTITIES = {"type": "string", "description": "Refs to transform, comma-separated."}

_DX = _inputs.Distance("dx", allow_zero=True, default=0.0,
                       description="Translation along sketch X.")
_DY = _inputs.Distance("dy", allow_zero=True, default=0.0,
                       description="Translation along sketch Y.")
_CENTER_X = _inputs.Distance("center_x", allow_zero=True, default=0.0,
                             description="Rotate/scale anchor X.")
_CENTER_Y = _inputs.Distance("center_y", allow_zero=True, default=0.0,
                             description="Rotate/scale anchor Y.")

_TRANSFORM_INPUTS = (
    ("rotation_deg", {"type": "number", "description": "Rotation about the anchor, degrees CCW."}),
    ("scale_factor", {"type": "number", "description": "Uniform scale about the anchor; > 0."}),
)


def _object_collection(ents, refs):
    """(ObjectCollection, error) holding the entities to transform."""
    # Sketch.move/copy take an ObjectCollection and raise TypeError on a plain list; the
    # neighbouring GeometricConstraints.createCircularPatternInput takes the OPPOSITE container.
    coll = adsk.core.ObjectCollection.create()
    for ent, ref in zip(ents, refs):
        if not coll.add(ent):
            return None, f"The sketch entity '{ref}' was refused by the collection to transform."
    return coll, None


def _transform(dx_cm, dy_cm, angle_deg, factor, cx_cm, cy_cm):
    """(Matrix3D, error) for a sketch-space uniform SCALE by `factor` and ROTATION by `angle_deg`
    about (cx, cy), followed by a TRANSLATION of (dx, dy). Assembled as ONE matrix - linear part
    factor*R, translation column c + d - factor*R*c - so the result never depends on which way round
    Matrix3D.transformBy composes."""
    theta = math.radians(float(angle_deg or 0.0))
    cos_t, sin_t = math.cos(theta), math.sin(theta)
    m = adsk.core.Matrix3D.create()
    applied = [m.setToRotation(theta, adsk.core.Vector3D.create(0.0, 0.0, 1.0),
                               adsk.core.Point3D.create(0.0, 0.0, 0.0))]
    if factor != 1.0:
        # scaling the whole 3x3 linear block is symmetric in row/column, so it needs no assumption
        # about which index of the 4x4 holds the translation.
        applied += [m.setCell(r, c, m.getCell(r, c) * factor)
                    for r in range(3) for c in range(3)]
    m.translation = adsk.core.Vector3D.create(
        cx_cm + dx_cm - factor * (cos_t * cx_cm - sin_t * cy_cm),
        cy_cm + dy_cm - factor * (sin_t * cx_cm + cos_t * cy_cm), 0.0)
    if not all(applied):
        return None, "Fusion refused a cell of the transform matrix, so nothing was transformed."
    return m, None


def _prepare(sketch_name, entities, units, dx, dy, rotation_deg, center_x, center_y, scale_factor,
             component=""):
    """Everything both tools need before the mutation: (design, sketch, ents, refs, coll, matrix,
    unit, error_result)."""
    blank = (None,) * 7
    k, uerr = _inputs.UNITS.resolve(units)
    if uerr:
        return blank + (error(uerr),)
    unit = (units or "mm").strip().lower()

    design = _common.design()
    if not design:
        return blank + (error("No active design. Create or open a document first (see doc_new)."),)
    sketch, requested, refusal = scoped_or_recent_sketch(design, sketch_name, component)
    if refusal:
        return blank + (error(refusal),)
    if not sketch:
        if requested:
            return blank + (error(f"No sketch named '{requested}'. Available: " + (
                ", ".join(n for n in _common.all_sketch_names(design) if n) or "(none)")),)
        return blank + (error("No sketch to transform. Draw one first with sketch_create + "
                              "sketch_add_geometry."),)

    ents, refs, rerr = _common.resolve_entity_refs(sketch, entities)
    if rerr:
        return blank + (error(rerr),)
    if not refs:
        return blank + (error("'entities' is required - comma-separated '<type>:<index>' refs (e.g. "
                              "'line:0,arc:1') from sketch_get(include_entities=true)."),)

    try:
        factor = float(scale_factor if scale_factor is not None else 1.0)
    except (TypeError, ValueError):
        return blank + (error(f"'scale_factor' must be a number, got {scale_factor!r}."),)
    if factor <= 0:
        return blank + (error(f"'scale_factor' must be greater than 0, got {factor}. A uniform "
                              "scale cannot mirror geometry - draw the mirrored curves instead."),)
    try:
        angle = float(rotation_deg if rotation_deg is not None else 0.0)
    except (TypeError, ValueError):
        return blank + (error(f"'rotation_deg' must be a number, got {rotation_deg!r}."),)

    lengths = {}
    for kind, raw in ((_DX, dx), (_DY, dy), (_CENTER_X, center_x), (_CENTER_Y, center_y)):
        value, lerr = kind.resolve_scaled(raw, k)
        if lerr:
            return blank + (error(lerr),)
        lengths[kind.name] = float(value or 0.0)
    if not (lengths["dx"] or lengths["dy"] or angle or factor != 1.0):
        return blank + (error("Nothing to apply: give a 'dx'/'dy' translation, a 'rotation_deg', "
                              "or a 'scale_factor' other than 1."),)

    coll, cerr = _object_collection(ents, refs)
    if cerr:
        return blank + (error(cerr),)
    matrix, merr = _transform(lengths["dx"], lengths["dy"], angle, factor,
                              lengths["center_x"], lengths["center_y"])
    if merr:
        return blank + (error(merr),)
    return design, sketch, ents, refs, coll, matrix, unit, None


def _requested(unit, dx, dy, rotation_deg, scale_factor):
    """The transform the caller asked for, echoed in the caller's own units."""
    return {"units": unit, "dx": float(dx or 0.0), "dy": float(dy or 0.0),
            "rotation_deg": float(rotation_deg or 0.0),
            "scale_factor": float(scale_factor if scale_factor is not None else 1.0)}


def _transform_wire(tool):
    """The transform inputs both tools share, in one order."""
    tool = (tool.add_input_property("entities", dict(_ENTITIES))
                .add_required_input("entities")
                .add_input_property(*_DX.as_property())
                .add_input_property(*_DY.as_property())
                .add_input_property(*_CENTER_X.as_property())
                .add_input_property(*_CENTER_Y.as_property()))
    for name, schema in _TRANSFORM_INPUTS:
        tool = tool.add_input_property(name, dict(schema))
    return tool.add_input_property(*_inputs.UNITS.as_property()).strict_schema()


def handler(sketch_name: str = "", include_entities: bool = False, units: str = "mm",
            component: str = "") -> dict:
    """Read one sketch: light overview by default, the full entity/constraint/dimension X-ray with
    include_entities=true. 'component' scopes the name to one component. Lengths/areas are reported
    in 'units' (mm default; area = units^2)."""
    unit = (units or "mm").strip().lower()
    f = _common.CM_TO_UNIT.get(unit)
    if f is None:
        return error(f"Unknown units '{units}'. Valid: mm, cm, in.")

    design = _common.design()
    if not design:
        return error("No active design.")

    name = (sketch_name or "").strip()
    if not name:
        names = all_sketch_names(design)
        return error("Provide 'sketch_name'. Available: " + (", ".join(n for n in names if n) or "(none)"))
    scope = (component or "").strip()
    # The placement this read reached the sketch through, when the scope named one; the frame's
    # lift into THIS document's world runs on it.
    host_occurrence = None
    if scope:
        scoped, host_occurrence, scope_error = scope_component(design, scope)
        if scope_error:
            return error(scope_error)
        sketch, refusal = _common.find_sketch_in(design, name, scoped, scope)
        if refusal:
            return error(refusal)
    else:
        sketch, ambiguous = find_sketch(design, name, remedy=scope_remedy())
        if ambiguous:
            return error(ambiguous)
    if not sketch:
        # 'Available' renders a shared name as "<sketch> (<component>)", which is NOT a spelling
        # this tool resolves - the sentence converts it into the call that does.
        names = all_sketch_names(design)
        return error(f"No sketch named '{name}'. Available: "
                     + (", ".join(n for n in names if n) or "(none)")
                     + ". A name listed as \"<sketch> (<component>)\" is one several components "
                       "carry: pass the sketch name alone with component='<component>'.")

    counts = {
    "lines": safe(lambda: sketch.sketchCurves.sketchLines.count, 0),
    "arcs": safe(lambda: sketch.sketchCurves.sketchArcs.count, 0),
    "circles": safe(lambda: sketch.sketchCurves.sketchCircles.count, 0),
    "ellipses": safe(lambda: sketch.sketchCurves.sketchEllipses.count, 0),
    "elliptical_arcs": safe(lambda: sketch.sketchCurves.sketchEllipticalArcs.count, 0),
    "conics": safe(lambda: sketch.sketchCurves.sketchConicCurves.count, 0),
    "points": safe(lambda: sketch.sketchPoints.count, 0),
    "splines": safe(lambda: sketch.sketchCurves.sketchFittedSplines.count, 0),
    "cv_splines": safe(lambda: sketch.sketchCurves.sketchControlPointSplines.count, 0),
    "fixed_splines": safe(lambda: sketch.sketchCurves.sketchFixedSplines.count, 0),
    # sketchTexts is not a sketchCurves sub-collection and needs its own count.
    "texts": safe(lambda: sketch.sketchTexts.count, 0),
    }
    fully = safe(lambda: sketch.isFullyConstrained)
    constraint_count = safe(lambda: sketch.geometricConstraints.count, 0)
    dim_count = safe(lambda: sketch.sketchDimensions.count, 0)

    profiles = _profiles(sketch, f)
    out = {
        "sketch": safe(lambda: sketch.name),
        "plane": safe(lambda: sketch.referencePlane.name),
        # The only DOF signal the API exposes - no DOF count, no over-constrained flag.
        "is_fully_constrained": bool(fully) if fully is not None else None,
        "counts": counts,
        "constraint_count": constraint_count,
        "dimension_count": dim_count,
        "profile_count": safe(lambda: sketch.profiles.count, 0),
        "units": unit,
        # Every x/y below is sketch-LOCAL; the frame is what places them. safe(): a plane read that
        # raises reports frame null rather than sinking the whole read.
        "frame": safe(lambda: sketch_world_frame(sketch, design, host_occurrence)),
    }
    if profiles is None:
        # The flag stands in for the list, beside the profile_count it leaves stale.
        out["compute_deferred"] = True
        out["profiles_stale"] = True
    else:
        out["profiles"] = profiles
    lead = DEFERRED_NOTE + " " if profiles is None else ""

    if not include_entities:
        out["note"] = (lead + "Overview only, lengths in 'units' (area=units^2). "
                       + frame_space_note(out.get("frame"))
                       + " For the full entity/constraint/dimension X-ray, call again with "
                       "include_entities=true.")
        return ok(out)

    entities, constraints, dimensions, construction_count, driving_dims, truncated = _entity_xray(
        sketch, f, unit)
    note = (lead + "Full X-ray, lengths in 'units'. Entity coordinates are sketch-LOCAL; "
                 + frame_space_note(out.get("frame"))
                 + " Entity ids ('line:0', 'arc:1', ...) match sketch_constrain "
                 "/ extrude refs. A point OFF the sketch plane (a 3D line's endpoint) carries a 'z' (local "
                 "height along the plane normal); on-plane 2D points omit it. The point flagged origin:true "
                 "is the sketch ORIGIN (anchor origin-pinned constraints to it). is_fully_constrained=false "
                 "means free DOF remain; a dimension driving=true locks geometry, driving=false only measures. "
                 "A 'text:<i>' entity carries the sketch text's string, height, font and sketch-space "
                 "bounding_box; that same id is what sketch_set_text(index=<i>) edits and "
                 "sketch_delete_entity(target='text:<i>') removes.")
    if any(e.get("reference") for e in entities):
        note += (" reference:true marks a curve PROJECTED in from model geometry, not drawn here.")
    if any("?" in c.get("entities", []) for c in constraints):
        note += (" A constraint entity of '?' has no id in this payload - nothing listed in "
                 "'entities' matches it.")
    if truncated:
        note += (f" entities/constraints/dimensions each capped at {_XRAY_CAP}; counts above "
                 "(constraint_count/dimension_count/counts) are the full, uncapped totals.")
    out.update({
        "driving_dimension_count": driving_dims,
        "construction_count": construction_count,
        "entities": entities,
        "constraints": constraints,
        "dimensions": dimensions,
        "truncated": truncated,
        "note": note,
    })
    return ok(out)


