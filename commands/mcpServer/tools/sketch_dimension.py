# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""MCP building block: add a dimensional constraint (distance/radius/diameter/angle) to a sketch and
drive its value - the sizing half of parametric sketching (sketch_constrain is the geometric half).
Entity references are '<type>:<index>', the same scheme as sketch_constrain. WRITES.
"""

import adsk.core
import adsk.fusion

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import error, safe
from . import _common
from . import _inputs
from . import _sketch_batch
from . import _sketch_detail

app = adsk.core.Application.get()

_DIM_TYPES = ("distance", "horizontal_distance", "vertical_distance", "radius", "diameter", "angle",
              "offset", "linear_diameter", "concentric_circle", "tangent_distance",
              "ellipse_major_radius", "ellipse_minor_radius", "point_to_surface", "line_to_surface")
_DISTANCE_TYPES = ("distance", "horizontal_distance", "vertical_distance")  # sign is a signed placement
_DIM_TYPE = _inputs.Choice("dim_type", list(_DIM_TYPES), default="distance")

# dim_type -> ('<type>:<index>' ref kinds legal as entity_one, same for entity_two or None), read
# off the ARGUMENT TYPES the installed SketchDimensions bindings declare. Surfaced in the refusal,
# so the wire description carries no per-type operand table.
_OPERANDS = {
    "offset": (("line",), ("line", "point")),
    "linear_diameter": (("line",), ("line", "point")),
    "concentric_circle": (("circle", "arc"), ("circle", "arc")),
    "tangent_distance": (("point", "line", "circle", "arc"), ("circle", "arc")),
    "ellipse_major_radius": (("ellipse",), None),
    "ellipse_minor_radius": (("ellipse",), None),
    "point_to_surface": (("point",), None),
    "line_to_surface": (("line",), None),
}

# Types taking a second sketch entity (the lone-LINE shortcut stays a _DISTANCE_TYPES-only rule).
_TWO_ENTITY_TYPES = _DISTANCE_TYPES + ("angle", "offset", "linear_diameter", "concentric_circle",
                                       "tangent_distance")

# Types anchoring to ONE point of an entity ('line:0:end'): the distance family, plus
# point_to_surface - its binding argument is a SketchPoint, which is exactly what an anchor yields.
_ANCHOR_TYPES = _DISTANCE_TYPES + ("point_to_surface",)

# The two dims whose second operand is a model face / construction plane, not a sketch entity.
_SURFACE_TYPES = ("point_to_surface", "line_to_surface")
# addDistanceBetweenPointAndSurfaceDimension takes planar, cylindrical, spherical and conical
# faces; the line dim's argument is planarSurface, planar-only. So only the point dim is a
# curved_op, and the kind carries that split into both the schema and the resolution.
_CURVED_SURFACE_OK = ("point_to_surface",)
_SURFACE = _inputs.SurfaceRef("surface", curved_ops=_CURVED_SURFACE_OK,
                              description="The *_to_surface operand.")

# Dims whose API failure message NAMES its own cause ("Both sketch lines should be parallel",
# "line is not parallel to the planar surface"), so it is surfaced alone with no operand-kind hint.
_SELF_NAMING_FAILURE = ("linear_diameter", "line_to_surface")


def _kinds_text(kinds):
    return " or ".join(f"'{k}'" for k in kinds)


def _operand_error(dt, label, ref, base_ref, kinds):
    """None when the ref names one of the kinds this dim_type's binding accepts, else the refusal -
    which names the kind actually given, since a '<type>:<index>' ref declares its own kind."""
    kind = (base_ref or "").rpartition(":")[0].strip().lower()
    if kind in kinds:
        return None
    return (f"'{dt}' takes {_kinds_text(kinds)} as {label}; '{ref}' names "
            + (f"a '{kind}'." if kind else "no entity kind."))


def _point_of(entity):
    """A representative SketchPoint for an entity: a point itself, a line's start point, or a
    circle's CENTER point - addDistanceDimension accepts only SketchPoints."""
    sp = safe(lambda: entity.startSketchPoint)   # lines/arcs have start/end sketch points
    if sp is not None:
        return sp
    cp = safe(lambda: entity.centerSketchPoint)  # circles anchor at their center
    if cp is not None:
        return cp
    return entity   # a sketch point itself


# Per-type note text, appended to the payload's note.
_TYPE_NOTES = {
    "angle": (" The wedge dimensioned is the one FACING THE SKETCH ORIGIN (the dimension's text "
              "point sits at the origin, and the dimensioned wedge is the one containing it) - a "
              "value near 180 minus the angle wanted means the supplement was measured; re-read "
              "'value' before driving it."),
    "offset": (" A second line that is NOT parallel to the first is ROTATED parallel by this "
               "dimension: the constraint MOVES geometry rather than refusing, so re-read "
               "sketch_get to confirm the shape is still what was drawn."),
}


def _dim_point(sketch, entity, anchor):
    """(point, None) or (None, error): the SketchPoint for a distance dimension - _point_of with no
    anchor, else the explicit ':start/:end/:mid/:center' anchor point."""
    if anchor is None:
        return _point_of(entity), None
    return _common.anchor_point(sketch, entity, anchor)


def _radial_text_point(curve):
    """A text-point for a radial/diameter dimension: a Point3D offset from the arc/circle CENTER
    along +X by one radius (sketch-local, z=0)."""
    # addRadialDimension/addDiameterDimension take the dimension's radial DIRECTION from
    # (textPoint - center), so a text-point AT the centre raises "Some input argument is invalid".
    P = adsk.core.Point3D.create
    geo = safe(lambda: curve.geometry)            # SketchCircle/SketchArc geometry (Circle3D/Arc3D)
    c = safe(lambda: geo.center)
    r = safe(lambda: geo.radius, 0.0) or 0.0
    if c is None:
        return P(1, 0, 0)                          # last-resort non-degenerate point
    off = r if r > 1e-9 else 1.0                   # a sane non-zero offset even for a tiny/odd curve
    return P(c.x + off, c.y, getattr(c, "z", 0.0))


# ── the post-solve read-back ────────────────────────────────────────────────
# A distance dimension is UNSIGNED, so the solver may satisfy it by moving EITHER referenced entity
# and its value says nothing about which one moved: the payload reads both back after the solve.

_MM = 10.0            # cm -> mm; the solved read-back publishes millimetres
# How much further than the change it DEMANDED (|gap_before - value|) a solve may move an entity
# before the move is called out; the floor keeps a rounding-level nudge from flagging.
_MOVE_SLACK = 2.0
_MOVE_FLOOR_CM = 0.01   # 0.1 mm


def _xyz(geo):
    """(x, y, z) in cm off a point geometry, or None when it does not read."""
    x = safe(lambda: float(geo.x))
    y = safe(lambda: float(geo.y))
    if x is None or y is None:
        return None
    return (x, y, safe(lambda: float(geo.z), 0.0) or 0.0)


def _mm3(p):
    return [round(v * _MM, 4) for v in p]


def _distance_cm(a, b):
    return sum((p - q) ** 2 for p, q in zip(a, b)) ** 0.5


def _entity_solved(entity):
    """(facts, positions) for ONE sketch entity: `facts` is the mm geometry the payload publishes -
    a circle/arc's center + radius, a line's span and midpoint, a point's position - and `positions`
    are the cm points a MOVE is measured on. ({}, []) for an entity whose geometry does not read."""
    center = safe(lambda: entity.centerSketchPoint)
    if center is not None:
        c = _xyz(safe(lambda: center.geometry))
        r = safe(lambda: float(entity.geometry.radius))
        facts = {}
        if c is not None:
            facts["center_mm"] = _mm3(c)
        if r is not None:
            facts["radius_mm"] = round(r * _MM, 4)
        return facts, ([c] if c is not None else [])
    start = safe(lambda: entity.startSketchPoint)
    end = safe(lambda: entity.endSketchPoint)
    if start is not None and end is not None:
        a = _xyz(safe(lambda: start.geometry))
        b = _xyz(safe(lambda: end.geometry))
        if a is None or b is None:
            return {}, [p for p in (a, b) if p is not None]
        mid = tuple((p + q) / 2.0 for p, q in zip(a, b))
        return ({"start_mm": _mm3(a), "end_mm": _mm3(b), "mid_mm": _mm3(mid),
                 "length_mm": round(_distance_cm(a, b) * _MM, 4)}, [a, b])
    p = _xyz(safe(lambda: entity.geometry))
    if p is not None:
        return {"position_mm": _mm3(p)}, [p]
    return {}, []


def _referenced_pairs(*refs_and_entities):
    """The (ref, entity) pairs to read back, ONE per referenced entity. Two refs into the same
    entity ('line:0:start' + 'line:0:end', which both name line:0) collapse to a single pair, so the
    payload cannot carry two rows describing the same geometry under one key."""
    pairs, seen = [], set()
    for ref, ent in refs_and_entities:
        if ent is None or not ref or ref in seen:
            continue
        seen.add(ref)
        pairs.append((ref, ent))
    return pairs


def _positions_of(pairs):
    """{ref: positions} for the (ref, entity) pairs a call referenced - the before half of the move
    measurement, read the same way the after half is."""
    return {ref: _entity_solved(ent)[1] for ref, ent in pairs}


def _gap_cm(dt, p1, p2):
    """The distance this dimension MEASURES between its two points, before it solves, in cm - the
    euclidean span for an aligned distance, the axis projection for a horizontal/vertical one (those
    two measure one component, so their demanded change is computed on that component). None when
    either point does not read."""
    a = _xyz(safe(lambda: p1.geometry))
    b = _xyz(safe(lambda: p2.geometry))
    if a is None or b is None:
        return None
    if dt == "horizontal_distance":
        return abs(a[0] - b[0])
    if dt == "vertical_distance":
        return abs(a[1] - b[1])
    return _distance_cm(a, b)


def _moved_cm(before, after):
    """The furthest one entity's sampled points moved, in cm, or None when the two reads do not
    describe the same points (nothing can be said about a move that was not measured twice)."""
    if not before or not after or len(before) != len(after):
        return None
    return max(_distance_cm(a, b) for a, b in zip(before, after))


def _solved_block(pairs, before):
    """(solved rows, [(ref, moved_cm), ...]) - each referenced entity's post-solve geometry, and how
    far it moved while the dimension was applied. Rows whose geometry does not read are dropped."""
    rows, moves = [], []
    for ref, ent in pairs:
        facts, positions = _entity_solved(ent)
        moved = _moved_cm(before.get(ref), positions)
        if moved is not None:
            moves.append((ref, moved))
            facts["moved_mm"] = round(moved * _MM, 4)
        if facts:
            rows.append(dict(ref=ref, **facts))
    return rows, moves


def _moved_warning(moves, value_cm, gap_before_cm):
    """The teleport warning, or None - for the DISTANCE family, whose value and gap are both lengths
    in cm. The discriminator is the CHANGE the dimension demanded, |gap_before - value|, not its
    value; a legitimate outsized solve exists, so this warns and never refuses."""
    if value_cm is None or gap_before_cm is None:
        return None
    demanded = abs(gap_before_cm - abs(value_cm))
    limit = max(demanded * _MOVE_SLACK, _MOVE_FLOOR_CM)
    jumps = [(ref, moved) for ref, moved in moves if moved > limit]
    if not jumps:
        return None
    named = ", ".join(f"{ref} by {round(moved * _MM, 4)} mm" for ref, moved in jumps)
    return (f"The solver moved {named}, but satisfying this dimension demanded only "
            f"{round(demanded * _MM, 4)} mm of change ({round(gap_before_cm * _MM, 4)} mm measured "
            f"before it, driven to {round(abs(value_cm) * _MM, 4)} mm) - a move far beyond that is "
            "what a dimension attached to the UNINTENDED entity looks like (a distance is unsigned, "
            "so the solver may move either side). Verify the geometry that moved is the one meant; "
            "'solved' holds each referenced entity's post-solve position.")


# The fields one 'dimensions' entry takes - the wire schema and the entry guard read this one table.
_ENTRY_FIELDS = ("dim_type", "entity_one", "entity_two", "value", "surface", "is_driving",
                 "tangent_side_one", "tangent_side_two")


def handler(dimensions=None, sketch_name: str = "", component: str = "") -> dict:
    """See TOOL_DESCRIPTION."""
    entries, eerr = _sketch_batch.entries_or_error(dimensions, "dimensions", _ENTRY_FIELDS)
    if eerr:
        return error(eerr)
    design = _common.design()
    if not design:
        return error("No active design. Create or open a document first (see doc_new).")
    sketch, requested, refusal = _sketch_detail.scoped_or_recent_sketch(
        design, sketch_name, component)
    if refusal:
        return error(refusal)
    if not sketch:
        if requested:
            return error(f"No sketch named '{requested}'. Available: "
                        + (", ".join(n for n in _common.all_sketch_names(design) if n)
                           or "(none)") + ". Use sketch_get.")
        return error("No sketch to dimension. Create one first with sketch_create.")
    return _sketch_batch.run_batch(entries, lambda i, e: _one(sketch, e), "dimensions",
                                   "dimensioned", safe(lambda: sketch.name))


def _one(sketch, entry):
    """(result, error) for ONE dimension entry added to `sketch`."""
    dim_type = entry.get("dim_type", "distance")
    entity_one = entry.get("entity_one") or ""
    entity_two = entry.get("entity_two") or ""
    value = entry.get("value") or ""
    surface = entry.get("surface") or ""
    is_driving = entry.get("is_driving", True)
    tangent_side_one = entry.get("tangent_side_one", True)
    tangent_side_two = entry.get("tangent_side_two", True)
    dt = (dim_type or "distance").strip().lower()
    if dt not in _DIM_TYPES:
        return None, (f"Unknown dim_type '{dim_type}'. Valid: {', '.join(_DIM_TYPES)}.")
    # isDriving=False is the API's DRIVEN (reference) dimension: the geometry controls the
    # dimension, so an expression cannot drive it. Refuse the contradiction naming both inputs.
    if not is_driving and (value or "").strip():
        return None, (f"is_driving=false creates a DRIVEN (reference) dimension - the geometry "
                     f"controls it, so value '{value}' cannot drive it. Drop 'value', or leave "
                     "is_driving true.")

    base1, anchor1, aerr1 = _common.parse_anchor_ref(entity_one)
    if aerr1:
        return None, (aerr1)
    e1 = _common.resolve_entity_ref(sketch, base1)
    if e1 is None:
        return None, (f"entity_one '{entity_one}' did not resolve. Use '<type>:<index>' "
    f"({'/'.join(_common.ENTITY_REF_KINDS)}), optionally with an anchor "
    "':start'/':end'/':mid'/':center', e.g. 'line:0:end'.")
    need_two = dt in _TWO_ENTITY_TYPES
    e2 = None
    base2 = None
    anchor2 = None
    lone_line = False
    if need_two and dt in _DISTANCE_TYPES and not (entity_two or "").strip():
        # A lone LINE dimensions its OWN length (endpoint to endpoint) - the native behavior.
        # Only a line qualifies: it has two endpoints and no center (an arc's endpoint span is
        # not its length, so an arc/circle still needs an explicit entity_two).
        if anchor1:
            return None, (f"A single-entity '{dt}' dimensions the whole line's length - drop the "
                         f"':{anchor1}' anchor, or give entity_two to pin two points.")
        has_ends = (safe(lambda: e1.startSketchPoint) is not None
                    and safe(lambda: e1.endSketchPoint) is not None)
        if not has_ends or safe(lambda: e1.centerSketchPoint) is not None:
            return None, (f"'{dt}' with no entity_two dimensions a LINE's own length; "
                         f"'{entity_one}' is not a line. Give entity_two ('<type>:<index>').")
        lone_line = True
    elif need_two:
        base2, anchor2, aerr2 = _common.parse_anchor_ref(entity_two)
        if aerr2:
            return None, (aerr2)
        e2 = _common.resolve_entity_ref(sketch, base2)
        if e2 is None:
            return None, (f"'{dt}' needs entity_two ('<type>:<index>'). '{entity_two}' did not resolve.")

    dims = sketch.sketchDimensions
    P = adsk.core.Point3D.create
    # Text position is cosmetic for a LINEAR dim only. RADIAL/DIAMETER dims take their radial
    # direction from (textPoint - center), so they use the offset _radial_text_point; for an ANGULAR
    # dim the dimensioned wedge is the one CONTAINING the text point.
    tp = P(0, 0, 0)
    # Anchors pin one point of an entity for a DISTANCE dim (and for point_to_surface, whose binding
    # argument IS a SketchPoint); every other type takes whole entities.
    if anchor1 and dt not in _ANCHOR_TYPES:
        return None, (f"'{dt}' takes a whole entity, not a point anchor - drop the ':{anchor1}' from entity_one.")
    if anchor2 and dt not in _DISTANCE_TYPES:
        return None, (f"'{dt}' takes a whole entity as entity_two, not a point anchor - drop the "
                     f"':{anchor2}'.")

    kinds1, kinds2 = _OPERANDS.get(dt, (None, None))
    # an anchored ref for point_to_surface resolves to a SketchPoint whatever entity it names, so the
    # entity_one kind gate applies only to a bare ref.
    if kinds1 and not anchor1:
        oerr = _operand_error(dt, "entity_one", entity_one, base1, kinds1)
        if oerr:
            return None, (oerr)
    if kinds2:
        oerr = _operand_error(dt, "entity_two", entity_two, base2, kinds2)
        if oerr:
            return None, (oerr)

    surf = None
    if dt in _SURFACE_TYPES:
        surf, serr = _SURFACE.resolve(surface, dt)
        if serr:
            return None, (serr)
        if surf is None:
            return None, (f"'{dt}' needs 'surface' - a plane alias (xy/xz/yz), a construction-plane "
                         "name, or a face handle from find_geometry"
                         + (" (curved faces allowed)." if dt in _CURVED_SURFACE_OK
                            else " (this dimension takes a PLANAR face only)."))

    if dt in _DISTANCE_TYPES:
        if lone_line:
            p1 = safe(lambda: e1.startSketchPoint)
            p2 = safe(lambda: e1.endSketchPoint)
        else:
            p1, perr1 = _dim_point(sketch, e1, anchor1)
            if perr1:
                return None, (f"entity_one: {perr1}")
            p2, perr2 = _dim_point(sketch, e2, anchor2)
            if perr2:
                return None, (f"entity_two: {perr2}")
    elif dt == "point_to_surface":
        p1, perr1 = _dim_point(sketch, e1, anchor1)
        if perr1:
            return None, (f"entity_one: {perr1}")
    # Where the referenced entities sit BEFORE the solve - the baseline the post-solve read-back
    # measures movement against; gap_before is what the dimension measures between them right now.
    pairs = _referenced_pairs((base1, e1), (base2, e2))
    before = _positions_of(pairs)
    gap_before = _gap_cm(dt, p1, p2) if dt in _DISTANCE_TYPES else None
    try:
        if dt in _DISTANCE_TYPES:
            orient = {
            "distance": adsk.fusion.DimensionOrientations.AlignedDimensionOrientation,
            "horizontal_distance": adsk.fusion.DimensionOrientations.HorizontalDimensionOrientation,
            "vertical_distance": adsk.fusion.DimensionOrientations.VerticalDimensionOrientation,
            }[dt]
            dim = dims.addDistanceDimension(p1, p2, orient, tp, is_driving)
        elif dt == "radius":
            dim = dims.addRadialDimension(e1, _radial_text_point(e1), is_driving)
        elif dt == "diameter":
            dim = dims.addDiameterDimension(e1, _radial_text_point(e1), is_driving)
        elif dt == "angle":
            dim = dims.addAngularDimension(e1, e2, tp, is_driving)
        elif dt == "offset":
            dim = dims.addOffsetDimension(e1, e2, tp, is_driving)
        elif dt == "linear_diameter":
            dim = dims.addLinearDiameterDimension(e1, e2, tp, is_driving)
        elif dt == "concentric_circle":
            dim = dims.addConcentricCircleDimension(e1, e2, tp, is_driving)
        elif dt == "tangent_distance":
            # The binding interleaves the two tangent-side selectors between the entities:
            # (entityOne, isCloseToEnityTwo, entityTwo, isCloseToEnityOne, textPoint, isDriving) -
            # 'Enity' is the API's own spelling, so these are passed POSITIONALLY.
            dim = dims.addTangentDistanceDimension(e1, bool(tangent_side_one), e2,
                                                   bool(tangent_side_two), tp, is_driving)
        elif dt in ("ellipse_major_radius", "ellipse_minor_radius"):
            # an offset-from-center text point, never AT the center (degenerate for a radial-family
            # dimension on an origin-centered curve).
            etp = _radial_text_point(e1)
            dim = (dims.addEllipseMajorRadiusDimension(e1, etp, is_driving)
                   if dt == "ellipse_major_radius"
                   else dims.addEllipseMinorRadiusDimension(e1, etp, is_driving))
        elif dt == "point_to_surface":
            # no textPoint argument - this dim places its own text (per the binding).
            dim = dims.addDistanceBetweenPointAndSurfaceDimension(p1, surf, is_driving)
        else:  # line_to_surface
            dim = dims.addDistanceBetweenLineAndPlanarSurfaceDimension(e1, surf, is_driving)
    except Exception as e:
        if dt in _SELF_NAMING_FAILURE:
            return None, (f"Could not add the {dt} dimension: {e}")
        if kinds1:
            hint = (f"'{dt}' takes {_kinds_text(kinds1)} as entity_one"
                    + (f" and {_kinds_text(kinds2)} as entity_two" if kinds2 else "")
                    + (" plus a 'surface'" if dt in _SURFACE_TYPES else "") + ".")
        else:
            hint = ("Check the entity types match the dimension - radius/diameter need an "
                    "arc/circle, angle needs two lines.")
        return None, (f"Could not add the {dt} dimension: {e}. ({hint})")
    if not dim:
        return None, (f"Adding the {dt} dimension returned nothing.")

    set_value = None
    if (value or "").strip():
        try:
            dim.parameter.expression = value.strip()
            set_value = value.strip()
        except Exception as e:
            return None, (f"Dimension added but could not set value '{value}': {e}.")

    out = {
    "dim_type": dt,
    "parameter": safe(lambda: dim.parameter.name),
    # the value is READ BACK off the parameter - what Fusion holds, not an echo of the request
    "value": safe(lambda: dim.parameter.expression),
    "value_driven": set_value is not None,
    # READ BACK off the dimension: a driving dimension controls the geometry, a driven one only
    # reports it - which of the two the API actually made is not assumed from the request.
    "is_driving": safe(lambda: dim.isDriving),
    "note": "Drive it later by name via param_set." + _TYPE_NOTES.get(dt, ""),
    }
    if dt in _SURFACE_TYPES:
        out["surface"] = _inputs.surface_ref_label(surf)
    # A negative DISTANCE does not mirror: the solver places the point at the SIGNED offset, and a
    # flipped spot landing on another point merges geometry silently. The read-back EVALUATED
    # value's sign detects it, where a profile count does not (overlaps ADD intersection regions).
    eval_cm = safe(lambda: dim.parameter.value)
    if dt in _DISTANCE_TYPES and eval_cm is not None and eval_cm < -1e-9:
        out["negative_distance_warning"] = (
            "This distance evaluated NEGATIVE - the solver does not mirror it. It placed the point at "
            "the signed offset, flipping it across its reference; if that spot coincides with another "
            "point the two merge silently. For a reflection use sketch_constrain symmetry; for a "
            "magnitude use a positive value. Re-read sketch_get to confirm the geometry.")
    # The dimensioned entities as they sit AFTER the solve, each with how far it travelled.
    solved, moves = _solved_block(pairs, before)
    if solved:
        out["solved"] = solved
        out["note"] += (" 'solved' is each REFERENCED entity's geometry read back after the solve, "
                        "with the distance it moved (moved_mm) getting there; geometry the solve "
                        "moved elsewhere in the sketch is not covered - read that back with "
                        "sketch_get(include_entities=true).")
    # DISTANCE family only: a parameter's value is in DATABASE units, so an angular dimension's is
    # radians and no length threshold applies to it.
    jump = _moved_warning(moves, eval_cm, gap_before) if dt in _DISTANCE_TYPES else None
    if jump:
        out["solver_moved_warning"] = jump
    return out, None


TOOL_DESCRIPTION = (
"Add dimensional constraints to one sketch, each optionally driven to a value."
)

# One 'dimensions' entry on the wire: the same fields the entry guard admits (_ENTRY_FIELDS).
_ENTRY_SCHEMA = {
    "type": "object",
    "properties": {
        "dim_type": _DIM_TYPE.schema(),
        "entity_one": {"type": "string", "description": "Bare ref = the entity's START point (circle/ellipse: CENTRE); anchor ':start'/':end'/':mid'/':center'."},
        "entity_two": {"type": "string"},
        "value": {"type": "string", "description": "Expression like '25 mm' or 'StockX/2'; omit to keep measured."},
        "surface": _SURFACE.schema(),
        "is_driving": {"type": "boolean", "default": True},
        "tangent_side_one": {"type": "boolean", "default": True},
        "tangent_side_two": {"type": "boolean", "default": True},
    },
    "required": ["dim_type"],
    "additionalProperties": False,
}

tool = (
    Tool.create_simple(name="sketch_dimension", description=TOOL_DESCRIPTION)
    .add_input_property("dimensions", {"type": "array", "items": _ENTRY_SCHEMA,
            "description": "Run in order; the first failure stops the run."})
    .add_required_input("dimensions")
    .add_input_property("sketch_name", {"type": "string"})
    .add_input_property(*_sketch_detail.COMPONENT_SCOPE)
    .strict_schema()
)

item = Item.create_tool_item(
    tool=tool, write="write", handler=handler, run_on_main_thread=True,
    verification=Verification(
        kind="effect", rung="geometry",
        evidence_test="tests/unit/test_sketch_dimension.py::TestSolvedReadBack"
                      "::test_a_line_reports_its_span_midpoint_and_length"))


def register_tool():
    register(item)
