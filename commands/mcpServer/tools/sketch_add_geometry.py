# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""MCP building block: draw a LIST of geometry entities on one sketch - line, rectangle, circle,
arc, polygon, slot, spline, polyline. Coords/sizes accept mm | cm | in and convert to the API's
internal cm.
"""

import math

import adsk.core
import adsk.fusion

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import error, safe, scale
from ._sketch_detail import COMPONENT_SCOPE, _detail_engine, _sketch_summary
from . import _assert
from . import _common
from . import _inputs
from . import _sketch_batch

app = adsk.core.Application.get()

_KINDS = ("line", "rectangle", "center_rectangle", "circle", "ellipse", "elliptical_arc", "arc",
    "conic", "polygon", "slot", "overall_slot", "center_point_slot", "center_point_arc_slot",
    "three_point_arc_slot", "point", "spline", "cv_spline", "polyline", "closed_path")

# The slot kinds that follow an ARC instead of a straight centre line. Both constructors build the
# whole slot out of SketchArcs (two end caps plus the inner/centre/outer arcs), so 'arc' is the
# collection whose before/after count verifies the draw.
_ARC_SLOT_KINDS = ("center_point_arc_slot", "three_point_arc_slot")

# The STRAIGHT slot kinds carrying an optional length/angle tail. Each lands three SketchLines (two
# sides + centreline), four with the length/angle tail, plus two SketchArc end caps - so 'line' is
# the collection counted.
_LINEAR_SLOT_KINDS = ("overall_slot", "center_point_slot")

# Every slot kind, tailed or not. All of them route through the one pre-call guard: the tailed ones
# because their arity must be built exactly, plain 'slot' because it is called in its three-argument
# form and so has nowhere to put a length, angle or dimension flag.
_SLOT_KINDS = ("slot",) + _ARC_SLOT_KINDS + _LINEAR_SLOT_KINDS


# Control-point spline degree -> the SplineDegrees member. SketchControlPointSplines.add takes the
# degree as an enum member, and its binding states only degree 3 and degree 5 can be specified at
# creation - so this map IS the legal set the handler guards on.
_SPLINE_DEGREES = {3: "SplineDegreeThree", 5: "SplineDegreeFive"}

# The wire note for the two conic-family kinds: the ref token that names one, and the closure each
# needs to bound a profile that extrudes (measured live on both).
_CONIC_FAMILY_NOTES = {
    "conic": ("Conic drawn. Address it as 'conic:<index>' - "
              "sketch_get(include_entities=true) lists the index. Closed by a chord between its "
              "endpoints it forms a profile that extrudes to a solid."),
    "elliptical_arc": ("Elliptical arc drawn. Address it as 'elliptical_arc:<index>' - "
                       "sketch_get(include_entities=true) lists the index. A 180 deg arc closed by "
                       "a line across its diameter forms a profile that extrudes to a solid."),
}


# kind -> the '<type>:<index>' REF TOKEN whose collection its curves land in, where the kind's own
# name is not that token. The composite kinds are built by a SketchLines factory, so they land in
# 'line'; addCenterToCenterSlot lands 2 solid lines + 1 construction line + 2 arcs, a line delta 3.
_KIND_REF_TOKEN = {"cv_spline": "cv_spline",
                   "center_point_arc_slot": "arc",
                   "three_point_arc_slot": "arc",
                   "overall_slot": "line",
                   "center_point_slot": "line",
                   "slot": "line",
                   "rectangle": "line",
                   "center_rectangle": "line",
                   "polygon": "line",
                   "polyline": "line",
                   "closed_path": "line"}

# How many landed segments a broken-chain error names before it summarizes the rest - a chain can
# hold hundreds of points, and the error crosses the wire.
_MAX_NAMED_SEGMENTS = 6

# Which params each kind requires (in user units / degrees / counts).
_REQUIRED = {
    "line": ["x1", "y1", "x2", "y2"],
    "rectangle": ["x1", "y1", "x2", "y2"],
    "center_rectangle": ["cx", "cy", "x2", "y2"],   # center + corner half-extents (x2,y2)
    "circle": ["cx", "cy", "radius"],
    "ellipse": ["cx", "cy", "radius"],              # radius = major; 'minor' optional
    "elliptical_arc": ["cx", "cy", "radius", "sweep_deg"],   # + optional 'minor' / 'start_deg'
    "arc": ["cx", "cy", "x1", "y1", "sweep_deg"],
    "conic": ["x1", "y1", "x2", "y2", "cx", "cy", "rho"],    # start, end, apex (cx,cy), rho
    "polygon": ["cx", "cy", "radius", "sides"],
    "slot": ["x1", "y1", "x2", "y2", "radius"],     # two centers + radius (half-width)
    # arc slots: (cx,cy) is the arc CENTRE for center_point_arc_slot and a point ON the arc for
    # three_point_arc_slot; (x1,y1)/(x2,y2) are the two slot-end centres; radius is the half-width.
    "center_point_arc_slot": ["cx", "cy", "x1", "y1", "x2", "y2", "radius"],
    "three_point_arc_slot": ["x1", "y1", "x2", "y2", "cx", "cy", "radius"],
    # straight slots, whose two point roles are NOT symmetric: overall_slot's (x1,y1)/(x2,y2) are
    # the overall TIPS, while center_point_slot's (x1,y1) is the slot centre and (x2,y2) a CAP
    # CENTRE - so tip-to-tip there is 2*half-length + width.
    "overall_slot": ["x1", "y1", "x2", "y2", "radius"],
    "center_point_slot": ["x1", "y1", "x2", "y2", "radius"],
    "point": ["cx", "cy"],
    # spline / cv_spline / polyline / closed_path take a 'points' list instead of flat scalars
    # (handled specially).
    "spline": [],
    "cv_spline": [],
    "polyline": [],
    "closed_path": [],
}

_POINT_LIST_KINDS = ("polyline", "closed_path", "spline", "cv_spline")


def _inputs_by_kind(required):
    """One clause per kind naming the inputs it needs, built from the table the guard reads, so the
    wire cannot disagree with the refusal."""
    return "; ".join(f"{kind} {','.join(keys) or 'points'}" for kind, keys in required.items())


# Point ROLES the input names cannot carry: the kinds whose x1,y1 / x2,y2 / cx,cy / radius mean
# something other than "point 1, point 2, centre, radius".
_ROLES = (" Roles: center_rectangle x2,y2 = half-extents. conic cx,cy = apex. overall_slot "
          "x1,y1/x2,y2 = tips, center_point_slot = centre and cap. slot radius = half-width.")

_KIND = _inputs.Choice("kind", list(_KINDS), required=True,
                       description="Inputs by kind: " + _inputs_by_kind(_REQUIRED) + "." + _ROLES)


def _pt(x, y, k):
    """Point3D at (x*k, y*k, 0) - sketch-plane coordinates in cm."""
    return adsk.core.Point3D.create(x * k, y * k, 0.0)


def _kind_curve_collection(sketch, kind):
    """The sketch sub-collection this kind's factory adds to - the one the before/after count that
    VERIFIES the draw is read from. None when no collection answers for the kind."""
    # The token table answers BEFORE the fall-through: a kind whose curves land in another kind's
    # collection is counted there, and every ref token resolves through _common.
    return _common.entity_collection(sketch, _KIND_REF_TOKEN.get(kind, kind))


def _kind_curve_count(sketch, kind):
    """How many curves the kind's OWN sub-collection holds - None when the kind has no dedicated
    collection to count, or when the collection cannot be read."""
    coll = _kind_curve_collection(sketch, kind)
    return safe(lambda: coll.count) if coll is not None else None


def _center_point_ref(sketch, kind):
    """The 'point:<index>' ref of the centre point the newest circle/arc owns, or None when the
    centre or its index does not read."""
    # A circle/arc's centre lands in sketchPoints, which is the collection a 'point:<index>' ref
    # indexes - so drawing one shifts every later point's index. Proxy equality, not `is`.
    coll = _kind_curve_collection(sketch, kind)
    n = safe(lambda: coll.count, 0) if coll is not None else 0
    center = safe(lambda: coll.item(n - 1).centerSketchPoint) if n else None
    pts = _common.entity_collection(sketch, "point")
    if center is None or pts is None:
        return None
    for i in range(safe(lambda: pts.count, 0)):
        if safe(lambda i=i: pts.item(i) == center):
            return f"point:{i}"
    return None


def _effective_spline_degree(sketch):
    """The degree the newest control-point spline was actually BUILT at, or None."""
    # add() accepts a degree it cannot honor and SILENTLY CLAMPS it to controlPointCount - 1. The
    # `.degree` property echoes the REQUESTED degree back; `.geometry.degree` is the built one.
    coll = _kind_curve_collection(sketch, "cv_spline")
    n = safe(lambda: coll.count, 0) if coll is not None else 0
    if not n:
        return None
    return safe(lambda: coll.item(n - 1).geometry.degree)


def _segment_text(a, b) -> str:
    """One segment as '(x1,y1)->(x2,y2)', in the call's own units."""
    return f"({a[0]:g},{a[1]:g})->({b[0]:g},{b[1]:g})"


class _ChainBroken(Exception):
    """A polyline/closed_path chain that stopped part-way, carrying the segments that DID land -
    they stay in the sketch, so the refusal names them."""

    def __init__(self, kind, failed, total, points, cause=None):
        self.kind = kind
        self.failed = failed
        self.total = total
        self.landed = [(points[j - 1], points[j]) for j in range(1, failed)]
        super().__init__(self._message(points, cause))

    def _message(self, points, cause):
        head = (f"{self.kind} segment {self.failed} of {self.total} "
                f"{_segment_text(points[self.failed - 1], points[self.failed])} did not draw")
        head += f": {cause}." if cause is not None else "."
        if not self.landed:
            return head + " No segment landed, so nothing was added to the sketch."
        named = ", ".join(_segment_text(a, b) for a, b in self.landed[:_MAX_NAMED_SEGMENTS])
        if len(self.landed) > _MAX_NAMED_SEGMENTS:
            named += f", ... (+{len(self.landed) - _MAX_NAMED_SEGMENTS} more)"
        return (f"{head} The first {len(self.landed)} segment(s) DID land and are still in the "
                f"sketch: {named}. Delete them as 'line:<index>' with sketch_delete_entity "
                "(sketch_get lists the indexes) before retrying - a retry of the whole chain draws "
                "them a second time.")


def _draw_polyline(sketch, points, k, kind="polyline", weld_seam=False):
    """Draw a connected chain of lines through 'points' ((x,y) in user units * k = cm), returning a
    label or None for < 2 points; a segment that does not draw raises _ChainBroken."""
    # Each segment starts at the previous one's endSketchPoint, so consecutive segments SHARE a
    # point. weld_seam ends the LAST segment ON the first segment's start point: measured, that
    # leaves 4 distinct endpoints round a 4-point loop where a repeated coordinate leaves 5.
    pts = [(float(x), float(y)) for x, y in (points or [])]
    if len(pts) < 2:
        return None
    lines = sketch.sketchCurves.sketchLines
    prev_end = None
    first_start = None
    total = len(pts) - 1
    for i in range(1, len(pts)):
        start = prev_end if prev_end is not None else _pt(pts[i - 1][0], pts[i - 1][1], k)
        # addByTwoPoints takes a SketchPoint in EITHER slot (measured), so the closing segment
        # ends on the first point itself rather than on a new one at the same coordinates.
        if weld_seam and i == len(pts) - 1 and first_start is not None:
            end = first_start
        else:
            end = _pt(pts[i][0], pts[i][1], k)
        try:
            ln = lines.addByTwoPoints(start, end)
        except Exception as e:
            raise _ChainBroken(kind, i, total, pts, cause=e) from e
        if ln is None:
            raise _ChainBroken(kind, i, total, pts)
        if first_start is None:
            first_start = safe(lambda ln=ln: ln.startSketchPoint)
        prev_end = safe(lambda ln=ln: ln.endSketchPoint)
    return f"polyline {len(pts)} pts, {total} segments"


def _restore_clause(restore_error, sketch) -> str:
    """The sentence a result carries when the sketch could not be taken back OUT of deferred
    compute, or ''."""
    if not restore_error:
        return ""
    return (f" The sketch '{safe(lambda: sketch.name)}' was left with compute DEFERRED - restoring "
            f"it raised: {restore_error}. Until compute resumes, its profiles and geometry can read "
            "stale.")


def _all_sketch_curves_count(sketch):
    """Total count of sketch curves (across all curve collections) - a cheap 'how many before' marker."""
    return safe(lambda: sketch.sketchCurves.count, 0) or 0


def _mark_recent_construction(sketch, before_count):
    """Mark every sketch curve added since 'before_count' as construction geometry."""
    # 'before_count' is an INDEX into sketchCurves, so this stays a positional walk: iter_collection
    # drops an unreadable curve, which would slide the window onto curves that were already there.
    curves = safe(lambda: sketch.sketchCurves)
    n = safe(lambda: curves.count, 0) if curves else 0
    for i in range(before_count, n):
        setattr(curves.item(i), "isConstruction", True)


_RECT_KINDS = ("rectangle", "center_rectangle")
_AXIS_EPS = 1e-9   # cm; a rectangle side this close to an axis is treated as lying on it


def _rect_constrain(sketch, lines_before):
    """Apply horizontal/vertical to each rectangle line the draw just landed, the way the UI does,
    and return how many the sketch's own constraint count GAINED. MEASURED: both rectangle
    constructors land ZERO constraints through the API, deferred compute or not."""
    coll = _common.entity_collection(sketch, "line")
    gc = safe(lambda: sketch.geometricConstraints)
    n = safe(lambda: coll.count) if coll is not None else None
    before = _common.counted(lambda: gc.count) if gc is not None else None
    if n is None or before is None or lines_before is None:
        return None
    for i in range(lines_before, n):
        ln = safe(lambda i=i: coll.item(i))
        a = safe(lambda ln=ln: ln.startSketchPoint.geometry)
        b = safe(lambda ln=ln: ln.endSketchPoint.geometry)
        if a is None or b is None:
            continue
        dx, dy = abs(b.x - a.x), abs(b.y - a.y)
        # Each line is classified by its OWN geometry: the constructors' line order is not a
        # contract, and a rotated rectangle has no axis-aligned side to constrain.
        add = (safe(lambda: gc.addHorizontal) if (dy <= _AXIS_EPS < dx)
               else safe(lambda: gc.addVertical) if (dx <= _AXIS_EPS < dy) else None)
        if add is not None:
            safe(lambda add=add, ln=ln: add(ln))
    after = _common.counted(lambda: gc.count)
    return None if after is None else after - before


def _minor_radius(p):
    """The minor radius an ellipse/elliptical arc is DRAWN with, in the call's units: the given
    'minor', else half the major. The one place the default is applied, so the drawn label reports
    the radius that was built instead of the raw (possibly omitted) input."""
    return float(p["minor"] if p.get("minor") is not None else p["radius"] / 2.0)


def _slot_error(kind, p):
    """The refusal a slot call needs BEFORE it reaches the API, or None."""
    # Every tailed slot constructor's tail is POSITIONAL and the ladders differ, so a value or flag
    # with nowhere to sit would otherwise raise a bare overload TypeError. Passing slot_length /
    # angle_deg to a linear slot IMPLIES its linear and angular dimensions - no flag of their own.
    if float(p["radius"]) <= 0:
        return (f"'{kind}' radius is the slot's HALF-width (full width = radius*2) and must be > 0. "
                f"Got radius={p['radius']}.")
    flags = (("create_width_dimension", p.get("create_width_dimension")),
             ("create_radius_dimension", p.get("create_radius_dimension")),
             ("create_angle_dimension", p.get("create_angle_dimension")))
    extra = [n for n, on in flags if on and n != "create_width_dimension"]
    if kind == "slot":
        stray = [n for n in ("slot_length", "arc_radius", "angle_deg") if p.get(n) is not None]
        stray += [n for n, on in flags if on]
        if stray:
            return (f"kind='slot' draws from two centres and radius alone, so {', '.join(stray)} "
                    "would be dropped. Pass a length or angle with kind='overall_slot' or "
                    "'center_point_slot'; size an arc with kind='center_point_arc_slot'.")
        return None
    if kind in _LINEAR_SLOT_KINDS:
        if p.get("arc_radius") is not None:
            return (f"'{kind}' has no arc to size - 'arc_radius' belongs to center_point_arc_slot. "
                    "Override this slot's second point with 'slot_length' instead.")
        if extra:
            return (f"'{kind}' takes only create_width_dimension - its linear and angular dimensions "
                    f"are created by passing slot_length / angle_deg, with no flag of their own. "
                    f"Drop {', '.join(extra)}.")
        if p.get("slot_length") is not None and float(p["slot_length"]) <= 0:
            return f"'{kind}' slot_length must be > 0. Got slot_length={p['slot_length']}."
        if p.get("angle_deg") is not None and p.get("slot_length") is None:
            return (f"'{kind}' angle_deg ({p['angle_deg']}) needs 'slot_length' too - the angle sits "
                    "AFTER the length in the API's argument list, so there is no form that takes an "
                    "angle on its own.")
        return None
    if p.get("slot_length") is not None:
        # the remedy is per-kind: only center_point_arc_slot has a radius argument to redirect to
        remedy = ("Size this slot's arc with 'arc_radius'." if kind == "center_point_arc_slot"
                  else "Its arc is fixed by its three points - draw one you can size with "
                       "kind='center_point_arc_slot'.")
        return (f"'{kind}' takes no 'slot_length' - that belongs to overall_slot / "
                f"center_point_slot. {remedy}")
    if kind == "three_point_arc_slot":
        stray = [n for n in ("arc_radius", "angle_deg") if p.get(n) is not None]
        if stray:
            return (f"three_point_arc_slot's arc is fixed by its three points, so it takes no "
                    f"{', '.join(stray)}. Drop them, or draw the slot with "
                    "kind='center_point_arc_slot'.")
        if extra:
            return (f"three_point_arc_slot takes only create_width_dimension - it has no radius or "
                    f"angle argument to dimension. Drop {', '.join(extra)}, or draw the slot with "
                    "kind='center_point_arc_slot'.")
        return None
    if p.get("arc_radius") is not None and float(p["arc_radius"]) <= 0:
        return (f"center_point_arc_slot 'arc_radius' must be > 0. Got arc_radius={p['arc_radius']}; "
                "omit it to take the arc radius from the start point's distance to the centre.")
    if p.get("angle_deg") is not None and p.get("arc_radius") is None:
        return (f"center_point_arc_slot 'angle_deg' ({p['angle_deg']}) needs 'arc_radius' too - the "
                "angle sits AFTER the radius in the API's argument list, so there is no form that "
                "takes an angle on its own.")
    wanted = [n for n, on in flags if on]
    missing = [n for n in ("arc_radius", "angle_deg") if p.get(n) is None]
    if wanted and missing:
        return (f"center_point_arc_slot {', '.join(wanted)} needs both 'arc_radius' and 'angle_deg' "
                f"- the dimension flags sit after them in the API's argument list. "
                f"Missing: {', '.join(missing)}.")
    return None


def _draw_arc_slot(sketch, kind, p, k):
    """Draw an arc slot; 'width' is the slot's FULL width (radius*2, radius being the half-width)."""
    # addCenterPointArcSlot's optional positional tail is radius, angle, then the three dimension
    # flags. A supplied radius OVERRIDES the centre-to-start distance, leaving the start point to
    # set direction only; the angle argument takes a unit-bearing expression, not radians.
    width = adsk.core.ValueInput.createByReal(p["radius"] * 2 * k)
    if kind == "three_point_arc_slot":
        slot = sketch.addThreePointArcSlot(_pt(p["x1"], p["y1"], k), _pt(p["x2"], p["y2"], k),
                                           _pt(p["cx"], p["cy"], k), width,
                                           bool(p.get("create_width_dimension")))
        if slot is None:
            return None
        return (f"three_point_arc_slot ({p['x1']},{p['y1']})->({p['x2']},{p['y2']}) through "
                f"({p['cx']},{p['cy']}) w={p['radius'] * 2}")
    args = [_pt(p["cx"], p["cy"], k), _pt(p["x1"], p["y1"], k), _pt(p["x2"], p["y2"], k), width]
    if p.get("arc_radius") is not None:
        args.append(adsk.core.ValueInput.createByReal(float(p["arc_radius"]) * k))
    if p.get("angle_deg") is not None:
        args.append(adsk.core.ValueInput.createByString(f"{float(p['angle_deg'])} deg"))
    flags = [bool(p.get("create_width_dimension")), bool(p.get("create_radius_dimension")),
             bool(p.get("create_angle_dimension"))]
    if any(flags):
        args.extend(flags)
    slot = sketch.addCenterPointArcSlot(*args)
    if slot is None:
        return None
    label = (f"center_point_arc_slot c=({p['cx']},{p['cy']}) start=({p['x1']},{p['y1']}) "
             f"end=({p['x2']},{p['y2']}) w={p['radius'] * 2}")
    if p.get("arc_radius") is not None:
        label += f" arc_r={p['arc_radius']}"
    if p.get("angle_deg") is not None:
        label += f" angle={p['angle_deg']}deg"
    return label


def _draw_linear_slot(sketch, kind, p, k):
    """Draw a straight slot through addOverallSlot / addCenterPointSlot, 'width' being the FULL
    width (radius*2)."""
    # The optional positional tail is createWidthDimension, then the length, then the angle - the
    # flag sits BEFORE the values. A length overrides the second point's distance and creates the
    # linear dimension on its own; addCenterPointSlot's is the HALF length, forwarded unhalved.
    factory = sketch.addOverallSlot if kind == "overall_slot" else sketch.addCenterPointSlot
    args = [_pt(p["x1"], p["y1"], k), _pt(p["x2"], p["y2"], k),
            adsk.core.ValueInput.createByReal(p["radius"] * 2 * k)]
    length, angle = p.get("slot_length"), p.get("angle_deg")
    if length is not None or angle is not None or p.get("create_width_dimension"):
        args.append(bool(p.get("create_width_dimension")))
    if length is not None:
        args.append(adsk.core.ValueInput.createByReal(float(length) * k))
    if angle is not None:
        args.append(adsk.core.ValueInput.createByString(f"{float(angle)} deg"))
    slot = factory(*args)
    if slot is None:
        return None
    label = f"{kind} ({p['x1']},{p['y1']})->({p['x2']},{p['y2']}) w={p['radius'] * 2}"
    if length is not None:
        label += f" {'half_len' if kind == 'center_point_slot' else 'len'}={length}"
    if angle is not None:
        label += f" angle={angle}deg"
    return label


def _draw(sketch, kind, p, k):
    """Dispatch a draw operation. p = params dict (raw user numbers). k = cm scale. Returns a label."""
    curves = sketch.sketchCurves
    if kind in ("polyline", "closed_path"):
        pts = list(p.get("points") or [])
        # closed_path DELEGATES to the polyline shape with the first point appended, and WELDS the
        # seam: the closing segment ends on the first point itself, so the loop carries no duplicate
        # point and needs no closing constraint. Scales like polyline (no ~48 ceiling).
        if kind == "closed_path" and len(pts) >= 2:
            pts = pts + [pts[0]]
        label = _draw_polyline(sketch, pts, k, kind, weld_seam=(kind == "closed_path"))
        if label and kind == "closed_path":
            label += " (closed)"
        return label
    if kind == "line":
        ln = curves.sketchLines.addByTwoPoints(_pt(p["x1"], p["y1"], k), _pt(p["x2"], p["y2"], k))
        return f"line ({p['x1']},{p['y1']})->({p['x2']},{p['y2']})" if ln else None
    if kind == "rectangle":
        rect = curves.sketchLines.addTwoPointRectangle(_pt(p["x1"], p["y1"], k), _pt(p["x2"], p["y2"], k))
        return f"rectangle ({p['x1']},{p['y1']})-({p['x2']},{p['y2']})" if rect else None
    if kind == "circle":
        c = curves.sketchCircles.addByCenterRadius(_pt(p["cx"], p["cy"], k), p["radius"] * k)
        return f"circle c=({p['cx']},{p['cy']}) r={p['radius']}" if c else None
    if kind == "arc":
        center = _pt(p["cx"], p["cy"], k)
        start = _pt(p["x1"], p["y1"], k)
        a = curves.sketchArcs.addByCenterStartSweep(center, start, math.radians(p["sweep_deg"]))
        return f"arc c=({p['cx']},{p['cy']}) start=({p['x1']},{p['y1']}) sweep={p['sweep_deg']}deg" if a else None
    if kind == "polygon":
        poly = curves.sketchLines.addScribedPolygon(
            _pt(p["cx"], p["cy"], k), int(p["sides"]), 0.0, p["radius"] * k, True)
        return f"polygon c=({p['cx']},{p['cy']}) sides={int(p['sides'])} r={p['radius']}" if poly else None
    if kind == "center_rectangle":
        # center at (cx,cy), half-extents from (x2,y2) treated as a corner offset -> width/height.
        hw, hh = abs(p["x2"]) * k, abs(p["y2"]) * k
        c = _pt(p["cx"], p["cy"], k)
        corner = adsk.core.Point3D.create(c.x + hw, c.y + hh, 0)
        rect = curves.sketchLines.addCenterPointRectangle(c, corner)
        return f"center_rectangle c=({p['cx']},{p['cy']}) half=({p['x2']},{p['y2']})" if rect else None
    if kind == "ellipse":
        center = _pt(p["cx"], p["cy"], k)
        major = adsk.core.Point3D.create(center.x + p["radius"] * k, center.y, 0)   # major endpoint
        minor_u = _minor_radius(p)
        e = curves.sketchEllipses.add(center, major,
                                      adsk.core.Point3D.create(center.x, center.y + minor_u * k, 0))
        return f"ellipse c=({p['cx']},{p['cy']}) major={p['radius']} minor={minor_u:g}" if e else None
    if kind == "slot":
        # addCenterToCenterSlot is a method on the SKETCH, not on sketchLines, and 'width' must be
        # a ValueInput (real -> cm), not a bare float.
        p1, p2 = _pt(p["x1"], p["y1"], k), _pt(p["x2"], p["y2"], k)
        width = adsk.core.ValueInput.createByReal(p["radius"] * 2 * k)   # full width = radius*2
        slot = sketch.addCenterToCenterSlot(p1, p2, width)
        if slot is None:
            return None
        return f"slot ({p['x1']},{p['y1']})-({p['x2']},{p['y2']}) w={p['radius']*2}"
    if kind in _ARC_SLOT_KINDS:
        return _draw_arc_slot(sketch, kind, p, k)
    if kind in _LINEAR_SLOT_KINDS:
        return _draw_linear_slot(sketch, kind, p, k)
    if kind == "point":
        pt = sketch.sketchPoints.add(_pt(p["cx"], p["cy"], k))
        return f"point ({p['cx']},{p['cy']})" if pt else None
    if kind == "spline":
        pts = adsk.core.ObjectCollection.create()
        for (px, py) in (p.get("_points") or []):
            pts.add(_pt(px, py, k))
        sp = curves.sketchFittedSplines.add(pts)
        return f"spline through {pts.count} pts" if sp else None
    if kind == "cv_spline":
        # SketchControlPointSplines.add takes controlPoints as a plain Python list[Base], NOT the
        # ObjectCollection the FITTED spline's add() declares.
        pts = [_pt(px, py, k) for (px, py) in (p.get("_points") or [])]
        deg = int(p["degree"])
        sp = curves.sketchControlPointSplines.add(
            pts, getattr(adsk.fusion.SplineDegrees, _SPLINE_DEGREES[deg]))
        return f"cv_spline over {len(pts)} control points" if sp else None
    if kind == "conic":
        # add(startPoint, endPoint, apexPoint, rhoValue) - the apex rides on cx,cy.
        c = curves.sketchConicCurves.add(_pt(p["x1"], p["y1"], k), _pt(p["x2"], p["y2"], k),
                                         _pt(p["cx"], p["cy"], k), float(p["rho"]))
        return (f"conic ({p['x1']},{p['y1']})->({p['x2']},{p['y2']}) "
                f"apex=({p['cx']},{p['cy']}) rho={p['rho']}") if c else None
    if kind == "elliptical_arc":
        # addByAngle(centerPoint, majorAxis, minorAxis, startAngle, sweepAngle): each axis vector's
        # MAGNITUDE is that radius, and the minor axis must be perpendicular to the major. Angles
        # are radians from the major axis, positive counterclockwise.
        center = _pt(p["cx"], p["cy"], k)
        minor_u = _minor_radius(p)
        start = float(p.get("start_deg") or 0.0)
        a = curves.sketchEllipticalArcs.addByAngle(
            center, adsk.core.Vector3D.create(p["radius"] * k, 0, 0),
            adsk.core.Vector3D.create(0, minor_u * k, 0),
            math.radians(start), math.radians(p["sweep_deg"]))
        return (f"elliptical_arc c=({p['cx']},{p['cy']}) major={p['radius']} "
                f"minor={minor_u:g} start={start}deg sweep={p['sweep_deg']}deg") if a else None
    return None


def _parse_points(points):
    """Normalize a 'points' argument into a list of (x, y) floats. Accepts a list of [x,y] pairs or
    {x,y} dicts. Returns (list, error_or_None)."""
    if not points or not isinstance(points, (list, tuple)):
        return None, "Provide 'points' - a list of [x, y] pairs for the polyline/closed_path."
    out = []
    for i, pt in enumerate(points):
        try:
            if isinstance(pt, dict):
                out.append((float(pt["x"]), float(pt["y"])))
            else:
                out.append((float(pt[0]), float(pt[1])))
        except Exception:
            return None, f"points[{i}] is not a valid [x, y] pair."
    if len(out) < 2:
        return None, "A polyline needs at least 2 points."
    return out, None


# The fields one 'geometry' entry takes - the wire schema and the entry guard read this one table.
_ENTRY_FIELDS = ("kind", "x1", "y1", "x2", "y2", "cx", "cy", "radius", "minor", "sweep_deg",
                 "start_deg", "rho", "degree", "sides", "arc_radius", "slot_length", "angle_deg",
                 "points", "create_width_dimension", "create_radius_dimension",
                 "create_angle_dimension", "is_construction")


def handler(geometry=None, sketch_name: str = "", component: str = "", units: str = "mm") -> dict:
    """See TOOL_DESCRIPTION."""
    entries, eerr = _sketch_batch.entries_or_error(geometry, "geometry", _ENTRY_FIELDS)
    if eerr:
        return error(eerr)
    k = scale(units)
    if k is None:
        return error(f"Unknown units '{units}'. Valid: mm, cm, in.")
    design = _common.design()
    if not design:
        return error("No active design. Create or open a document first (see doc_new).")

    # 'component' narrows the by-name walk to one component's own sketches - the way through a name
    # two components carry, which Fusion's per-component numbering makes the norm. Unscoped, the
    # walk still REFUSES that name, now naming this input as the way to say which one was meant.
    sketch, requested, refusal = _detail_engine().scoped_or_recent_sketch(
        design, sketch_name, component)
    if refusal:
        return error(refusal)
    if not sketch:
        if requested:
            return error(f"No sketch named '{requested}'. Use sketch_get to list them, "
    "or sketch_create first.")
        return error("No sketch to draw on. Create one first with sketch_create.")
    return _sketch_batch.run_batch(entries, lambda i, e: _one(sketch, e, k, units), "geometry",
                                   "drawn", safe(lambda: sketch.name))


def _one(sketch, entry, k, units):
    """(result, error) for ONE geometry entry drawn on `sketch`; required params per 'kind' are in
    _REQUIRED."""
    kind = (entry.get("kind") or "").strip().lower()
    if kind not in _KINDS:
        return None, f"Unknown kind '{kind}'. Valid: {', '.join(_KINDS)}."
    is_construction = bool(entry.get("is_construction"))

    # polyline / closed_path / spline / cv_spline: a chain/curve from a 'points' list.
    if kind in _POINT_LIST_KINDS:
        pts, perr = _parse_points(entry.get("points"))
        if perr:
            return None, perr
        p = {"points": pts, "_points": pts}
        if kind == "cv_spline":
            degree = entry.get("degree")
            d = 3 if degree is None else int(degree)
            if d not in _SPLINE_DEGREES:
                return None, (f"cv_spline 'degree' must be "
                              f"{' or '.join(str(n) for n in sorted(_SPLINE_DEGREES))} - the only "
                              f"degrees the API accepts when creating a spline. Got {degree}.")
            p["degree"] = d
        # fall through to the shared draw + result below

    else:
        # Gather + validate the scalar params this kind needs.
        supplied = {key: entry.get(key) for key in
                    ("x1", "y1", "x2", "y2", "cx", "cy", "radius", "sweep_deg", "sides", "minor",
                     "rho")}
        p = {}
        missing = []
        for key in _REQUIRED[kind]:
            if supplied.get(key) is None:
                missing.append(key)
            else:
                p[key] = supplied[key]
        if missing:
            return None, (f"'{kind}' needs: {', '.join(_REQUIRED[kind])}. "
                          f"Missing: {', '.join(missing)}.")
        p["minor"] = entry.get("minor")   # optional, passed through for ellipse / elliptical_arc
        p["start_deg"] = entry.get("start_deg")  # optional, elliptical_arc (default 0 = major axis)
        p["arc_radius"] = entry.get("arc_radius")     # optional, center_point_arc_slot only
        p["slot_length"] = entry.get("slot_length")   # optional, overall_slot / center_point_slot
        p["angle_deg"] = entry.get("angle_deg")       # optional, the tailed slot kinds
        p["create_width_dimension"] = bool(entry.get("create_width_dimension"))
        p["create_radius_dimension"] = bool(entry.get("create_radius_dimension"))
        p["create_angle_dimension"] = bool(entry.get("create_angle_dimension"))
        if kind in _SLOT_KINDS:
            slot_err = _slot_error(kind, p)
            if slot_err:
                return None, slot_err
        if kind in ("circle", "ellipse", "elliptical_arc") and p["radius"] <= 0:
            return None, "radius must be > 0."
        if kind == "elliptical_arc" and p["minor"] is not None and p["minor"] <= 0:
            return None, f"minor must be > 0 (got {p['minor']}); omit it for major/2."
        # the conic binding states rhoValue must be greater than zero and less than one.
        if kind == "conic" and not 0.0 < float(p["rho"]) < 1.0:
            return None, f"conic 'rho' must be greater than 0 and less than 1. Got {p['rho']}."
        if kind == "polygon" and int(p["sides"]) < 3:
            return None, "polygon needs sides >= 3."

    # Draw (defer compute so the single add is efficient and consistent).
    before_kind = _kind_curve_count(sketch, kind)
    deferred_set = False
    restore_error = None
    draw_error = None
    label = None
    try:
        sketch.isComputeDeferred = True
        deferred_set = True
        before = safe(lambda: _all_sketch_curves_count(sketch), 0)
        label = _draw(sketch, kind, p, k)
        if is_construction and label:
            _mark_recent_construction(sketch, before)
    except _ChainBroken as broken:
        draw_error = str(broken)
        if is_construction and broken.landed:
            draw_error += (" They are plain geometry: is_construction is applied once the chain "
                           "completes, so it never reached them.")
    except Exception as e:
        draw_error = f"Failed to draw {kind}: {e}"
    finally:
        if deferred_set:
            try:
                sketch.isComputeDeferred = False
            except Exception as e:
                # The restore is a MUTATION of its own: a refused one leaves the sketch deferred,
                # which every result below discloses rather than swallows.
                restore_error = str(e)

    if draw_error:
        return None, draw_error + _restore_clause(restore_error, sketch)
    if not label:
        return None, (f"Drawing {kind} returned no entity (check the parameters)."
                      + _restore_clause(restore_error, sketch))

    # VERIFY the draw against the sketch's own collection for this kind: a factory can hand back an
    # object without the curve landing in the sketch, and that is a failure, not a success.
    after_kind = _kind_curve_count(sketch, kind)
    delta = None
    if before_kind is not None and after_kind is not None:
        delta = after_kind - before_kind
        if delta < 1:
            # Name the collection that was COUNTED, not the kind: a rectangle/polygon/slot/polyline
            # is built out of lines, so 'rectangle collection' would send the caller looking for a
            # collection the sketch does not have.
            counted = _KIND_REF_TOKEN.get(kind, kind)
            return None, (f"Drawing {kind} returned an entity but the sketch's own {counted} "
                          f"collection count did not change ({before_kind} -> {after_kind}) - "
                          "nothing was added. Re-read sketch_get."
                          + _restore_clause(restore_error, sketch))

    out = {
    "label": label,
    "kind": kind,
    "units": units,
    "sketch": _sketch_summary(sketch),
    "note": "Draw more with sketch_add_geometry, or view_screenshot to view the sketch.",
    }
    if delta is not None:
        out["curves_added"] = delta
    if kind in _RECT_KINDS:
        gained = _rect_constrain(sketch, before_kind)
        out["constraints_added"] = gained
        out["note"] = (
            f"Rectangle drawn with {gained} horizontal/vertical constraint(s) applied to its "
            "sides, as the UI does - the constructor itself lands none. Its corners already share "
            "points; what remains free is position and size, so dimension those with "
            "sketch_dimension." if gained else
            "Rectangle drawn. NO horizontal/vertical constraint took, so its sides are held only "
            "by their coordinates - a later edit can skew it. Add them with sketch_constrain "
            "(horizontal / vertical) before dimensioning.")
    if kind == "cv_spline":
        out["note"] = ("Control-point spline drawn - constrain or dimension it as "
                       "'cv_spline:<index>' (sketch_get lists the index).")
        # The degree is READ BACK off the created spline: the API accepts a degree it cannot honor
        # and clamps it to (control points - 1) without saying so, so what it BUILT is published.
        effective = _effective_spline_degree(sketch)
        if effective is not None:
            out["degree"] = effective
            if effective != p["degree"]:
                out["note"] += (f" Degree {p['degree']} was requested but the spline was built at "
                                f"degree {effective}: the API silently clamps the degree to the "
                                "control-point count minus one. Pass more 'points' to get the "
                                "degree asked for.")
    if kind in ("circle", "arc"):
        center_ref = _center_point_ref(sketch, kind)
        if center_ref:
            out["center_point"] = center_ref
            out["note"] = (f"{kind.capitalize()} drawn. Its centre is a sketch point of its own: "
                           f"center_point={center_ref} - address points by that ref rather than by "
                           "counting the ones you drew.")
    if kind in _ARC_SLOT_KINDS:
        out["note"] = ("Arc slot drawn out of SketchArcs - 'curves_added' counts them and each is "
                       "addressable as 'arc:<index>' for sketch_dimension / sketch_constrain "
                       "(sketch_get(include_entities=true) lists the indexes).")
    if kind == "slot":
        out["note"] = ("Slot drawn from 2 solid SketchLines, 1 CONSTRUCTION SketchLine (the "
                       "centre-to-centre line) and 2 SketchArc end caps - 5 curves, of which "
                       "'curves_added' counts the 3 lines. Address any of them as 'line:<index>' "
                       "or 'arc:<index>' for sketch_dimension / sketch_constrain "
                       "(sketch_get(include_entities=true) lists the indexes).")
    if kind in _LINEAR_SLOT_KINDS:
        out["note"] = ("Slot drawn - 'curves_added' counts its SketchLines: three, four when a "
                       "length or angle is passed. Its two end caps are SketchArcs. Address either "
                       "as 'line:<index>' / 'arc:<index>' for sketch_dimension / sketch_constrain "
                       "(sketch_get(include_entities=true) lists the indexes).")
    if kind == "closed_path":
        out["note"] = ("Closed path drawn and a profile forms. The seam is WELDED - the closing "
                       "segment ends on the first segment's start point, so the loop shares that "
                       "point instead of carrying two at the same coordinates, and needs no "
                       "closing coincident. Size it with sketch_dimension; the loop still carries "
                       "its position and shape freedom.")
    if kind in _CONIC_FAMILY_NOTES:
        out["note"] = _CONIC_FAMILY_NOTES[kind]
    if restore_error:
        out["compute_deferred"] = True
        out["note"] += _restore_clause(restore_error, sketch)
    return out, None


TOOL_DESCRIPTION = (
    "Draw entities on a sketch; coords in 'units', angles in degrees."
)

# One 'geometry' entry on the wire: the same fields the entry guard admits (_ENTRY_FIELDS).
_ENTRY_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": _KIND.schema(),
        "points": {"type": "array", "items": {"type": "array"},
                   "description": "[x,y] pairs in 'units'. A cv_spline's are control points."},
        "x1": {"type": "number"},
        "y1": {"type": "number"},
        "x2": {"type": "number"},
        "y2": {"type": "number"},
        "cx": {"type": "number"},
        "cy": {"type": "number"},
        "radius": {"type": "number"},
        "minor": {"type": "number"},
        "sweep_deg": {"type": "number"},
        "start_deg": {"type": "number"},
        "rho": {"type": "number"},
        "degree": {"type": "integer", "default": 3},
        "sides": {"type": "integer"},
        "arc_radius": {"type": "number", "description": "Overrides the start-to-centre distance."},
        "slot_length": {"type": "number", "description": "overall_slot tip-to-tip, center_point_slot HALF length. Overrides x2,y2."},
        "angle_deg": {"type": "number"},
        "create_width_dimension": {"type": "boolean"},
        "create_radius_dimension": {"type": "boolean"},
        "create_angle_dimension": {"type": "boolean"},
        "is_construction": {"type": "boolean"},
    },
    "required": ["kind"],
    "additionalProperties": False,
}

tool = (
    Tool.create_simple(name="sketch_add_geometry", description=TOOL_DESCRIPTION)
    .add_input_property("geometry", {"type": "array", "items": _ENTRY_SCHEMA,
            "description": "Run in order; the first failure stops the run."})
    .add_required_input("geometry")
    .add_input_property("sketch_name", {"type": "string", "description": "Default: most recent."})
    .add_input_property(*COMPONENT_SCOPE)
    .add_input_property(*_inputs.UNITS.as_property())
    .strict_schema()
)
item = Item.create_tool_item(
    tool=tool, write="write", handler=handler,
    run_on_main_thread=True,
    postconditions=[_assert.SketchCurvesChanged(scope_keys=("component",))],
    verification=Verification(
        kind="inline", rung="value",
        evidence_test="tests/unit/test_sketch_add_geometry.py::TestKindCollectionFallback"
                      "::test_a_line_that_never_lands_is_an_error"))


def register_tool():
    register(item)
