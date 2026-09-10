# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""MCP building block: apply geometric constraints (the Sketch Constrain menu) between sketch
entities referenced by '<type>:<index>' (e.g. 'line:0', 'arc:1', 'point:2') within a named sketch.
WRITES.
"""

import json

import adsk.core
import adsk.fusion

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import error, safe, all_sketch_names
from . import _common
from . import _inputs
from . import _sketch_batch
from . import _sketch_detail

app = adsk.core.Application.get()


_REQUIRES = {
    # live-verified: perpendicular/parallel/collinear/horizontal/vertical are LINE-ONLY - their
    # curve argument is typed SketchLine.
    "perpendicular": "two lines",
    "parallel": "two lines",
    "tangent": "two curves",
    # measured: a smooth refused with both operands satisfying the curve/spline rule, because an
    # earlier smooth had moved the spline's end point off the line it had been coincident with.
    "smooth": ("two curves, at least one of them a spline, meeting at a COINCIDENT point - when "
               "both operands already satisfy that, check the shared endpoint is still coincident "
               "(an earlier smooth can move it apart)"),
    # live-verified: equal refuses mismatched kinds, and refuses two ellipses, two fitted splines
    # or two control-point splines even though those kinds match ("3 : invalid argument value").
    "equal": "two lines, two arcs, or two circles",
    "concentric": "two curves with a center point",
    "collinear": "two lines",
    "midpoint": "a POINT as entity_one and a line/curve as entity_two",
    "coincident": ("a POINT as entity_one - coincident onto a CURVE puts that point ON it, so to "
                   "CENTRE a circle pass its CENTRE point, not the circle"),
    "horizontal": "one line",
    "vertical": "one line",
    "horizontal_points": "two points",
    "vertical_points": "two points",
    "symmetry": "two entities plus an axis line (symmetry_line)",
    "coincident_to_surface": "a POINT as entity_one plus a 'surface' (curved faces allowed)",
    "line_on_surface": "a LINE as entity_one plus a planar 'surface'",
    "line_parallel_to_surface": "a LINE as entity_one plus a planar 'surface'",
    "perpendicular_to_surface": ("a LINE or SPLINE as entity_one plus a 'surface' "
                                 "(curved faces allowed)"),
    "polygon": ("lines in 'entities' that already close the shape - equal lengths, equal angles, "
                "joined end to end"),
    "offset": "end-connected curves in 'entities' plus a 'distance'",
    "offset_two_sides": "end-connected curves in 'entities' plus a 'distance'",
    "rectangular_pattern": "sketch points/curves in 'entities' plus BOTH direction lines (entity_one and entity_two)",
    "circular_pattern": "sketch points/curves in 'entities' plus a POINT as entity_one (the center)",
    "auto": "only 'sketch_name' - it constrains the whole sketch, taking no entity refs",
    "fix": "one sketch entity",
    "unfix": "one sketch entity",
}


# constraint -> ("kind", method-or-None). kinds: two_curve | point_curve | two_point | one_line |
# symmetry | entity_surface | entity_list | offset | rect_pattern | circ_pattern | auto | fix
_CONSTRAINTS = {
    "perpendicular": ("two_curve", "addPerpendicular"),
    "parallel": ("two_curve", "addParallel"),
    "tangent": ("two_curve", "addTangent"),
    "smooth": ("two_curve", "addSmooth"),
    "equal": ("two_curve", "addEqual"),
    "concentric": ("two_curve", "addConcentric"),
    "collinear": ("two_curve", "addCollinear"),
    "midpoint": ("point_curve", "addMidPoint"),
    "coincident": ("point_curve", "addCoincident"),
    "horizontal": ("one_line", "addHorizontal"),
    "vertical": ("one_line", "addVertical"),
    "horizontal_points": ("two_point", "addHorizontalPoints"),
    "vertical_points": ("two_point", "addVerticalPoints"),
    "symmetry": ("symmetry", "addSymmetry"),
    "coincident_to_surface": ("entity_surface", "addCoincidentToSurface"),
    "line_on_surface": ("entity_surface", "addLineOnPlanarSurface"),
    "line_parallel_to_surface": ("entity_surface", "addLineParallelToPlanarSurface"),
    "perpendicular_to_surface": ("entity_surface", "addPerpendicularToSurface"),
    "polygon": ("entity_list", "addPolygon"),
    "offset": ("offset", "addOffset2"),
    "offset_two_sides": ("offset", "addTwoSidesOffset"),
    "rectangular_pattern": ("rect_pattern", "addRectangularPattern"),
    "circular_pattern": ("circ_pattern", "addCircularPattern"),
    "auto": ("auto", None),
    "fix": ("fix", None),
    "unfix": ("fix", None),
}

# Kinds that take their operands from the 'entities' ref list.
_LIST_OPERAND_KINDS = ("entity_list", "offset", "rect_pattern", "circ_pattern")

# Of those, the ones for which entity_one is not an operand (rectangular_pattern reads it as an
# OPTIONAL direction line; circular_pattern needs it as the center point).
_OPTIONAL_ENTITY_ONE = ("entity_list", "offset", "rect_pattern")

# Kinds that ADD sketch geometry rather than only relating existing entities.
_CREATOR_KINDS = ("offset", "rect_pattern", "circ_pattern")

# Kinds whose add* takes entity_one AND entity_two as its two operands.
_TWO_ENTITY_KINDS = ("two_curve", "point_curve", "two_point")

# constraint -> (entity_one takes a point anchor, entity_two takes one). An anchored ref
# ('circle:0:center' - _common.parse_anchor_ref) resolves to that entity's OWN SketchPoint; every
# other constraint takes whole entities, so an anchor there is refused.
_ANCHOR_SLOTS = {
    "coincident": (True, True),
    "midpoint": (True, False),              # entity_two is the curve the point rides
    "horizontal_points": (True, True),
    "vertical_points": (True, True),
    "coincident_to_surface": (True, False),  # entity_two is a surface, not a sketch entity
    "circular_pattern": (True, False),       # entity_one is the pattern centre point
}
_ANCHORED_CONSTRAINTS = ", ".join(sorted(_ANCHOR_SLOTS))
# 'midpoint' is the long spelling of 'mid' - one form per anchor in agent-facing text.
_ANCHOR_FORMS = "/".join(f"':{a}'" for a in _common.SKETCH_ANCHORS if a != "midpoint")


# ── 'text:<i>' - a SketchText, an operand for fix/unfix ONLY ────────────────

# No geometric constraint takes a SketchText as an operand; its anchor degree of freedom lives on
# the four rectangle lines of its definition, and fixing all four flips isFullyConstrained true.
# definition.rectangleLines carries no .count/.item, so it is list()ed, never indexed.


def _is_text_ref(ref):
    """Whether this ref uses the 'text:<i>' grammar (_inputs owns that parse)."""
    return _inputs._split_text_ref(ref) is not None


def _text_at_ref(sketch, ref):
    """(SketchText, error) for a 'text:<index>' ref, indexed in the sketch this call resolved; a
    sketch-qualified address is refused."""
    sname, idx = _inputs._split_text_ref(ref)
    if sname:
        return None, (f"'{ref}': name the sketch in 'sketch_name', not in the ref - this tool "
                      "constrains one named sketch.")
    n = safe(lambda: sketch.sketchTexts.count, 0) or 0
    if not n:
        return None, (f"'{ref}' addresses a sketch text, but this sketch holds none. Create one "
                      "with sketch_set_text.")
    if idx is None or idx < 0 or idx >= n:
        return None, (f"'{ref}' does not address one of this sketch's {n} sketch text(s) "
                      f"(text:0..text:{n - 1}).")
    text = safe(lambda: sketch.sketchTexts.item(idx))
    if text is None:
        return None, f"'{ref}': the sketch would not hand that text back."
    return text, None


def _text_anchor_lines(text):
    """The definition rectangle lines carrying a SketchText's anchor DOF, as a list. The vector is a
    plain iterable, so it is list()ed - a .count/.item walk over it reads nothing."""
    return safe(lambda: list(text.definition.rectangleLines)) or []


# The curve kinds whose keypoint is a CENTRE; every other curve kind carries endpoints instead, so
# the remedy the coincident note offers is read off the operand's own kind.
_CENTRE_BEARING = ("circle", "arc", "ellipse")


def _coincident_curve_note(ref):
    """The success-path note for coincident onto a CURVE, or None when entity_two names a point (a
    point operand is the ordinary point-to-point case, with no keypoint to miss)."""
    kind = ref.strip().lower().rpartition(":")[0]
    if kind in ("", "point"):
        return None
    forms = (f"'{ref.strip()}:center'" if kind in _CENTRE_BEARING
             else f"'{ref.strip()}:start' (or ':end'/':mid')")
    return (" Coincident onto a CURVE puts entity_one's point ON that curve, at no particular place "
            f"along it - to land on a KEYPOINT of {ref.strip()} instead, anchor entity_two: {forms}.")


def _names_a_point(base_ref, anchor):
    """Whether a ref resolves to a SketchPoint, decided on the ref FORM: every ':start/:end/:mid/
    :center' anchor yields one, and 'point:<n>' names one."""
    # The FORM, not isinstance: two distinct sketch CURVES share one entityToken (a split's pieces),
    # so an identity comparison over curve operands would refuse a legitimate pair.
    return bool(anchor) or (base_ref or "").strip().lower().rpartition(":")[0] == "point"


def _one_operand(a, b):
    """Whether two resolved operands are ONE entity - by object identity, else by native_identity;
    an identity that will not read is no evidence and answers False."""
    if a is None or b is None:
        return False
    if a is b:
        return True
    ia = _common.native_identity(a)
    return ia is not None and ia == _common.native_identity(b)


def _anchor_refusal(cname, label, anchor):
    """The refusal for an anchor on a slot that takes a whole entity - it names the constraints that
    DO take one, so the caller can see whether the anchor or the constraint is the wrong half."""
    return (f"'{cname}' takes a whole entity as {label}, not a point anchor - drop the ':{anchor}'. "
            f"The {_ANCHOR_FORMS} anchors apply to: {_ANCHORED_CONSTRAINTS}.")

# addCoincidentToSurface and addPerpendicularToSurface take a `surface: Base` and accept a CURVED
# face - both were applied to a cylinder live. The other two carry PlanarSurface in the API name and
# in the parameter, so they stay on PlaneRef; only these two resolve through the wider kind.
_CURVED_SURFACE_OK = ("coincident_to_surface", "perpendicular_to_surface")
_CONSTRAINT = _inputs.Choice("constraint", list(_CONSTRAINTS))
_SURFACE = _inputs.SurfaceRef("surface", curved_ops=_CURVED_SURFACE_OK)
_DISTANCE = _inputs.Distance("distance", allow_zero=False,
                             description="Offset, or spacing one.")
# 'distance' when omitted - a cross-field fallback, so it has no schema `default` to live in.
_DISTANCE_TWO = _inputs.Distance("distance_two", allow_zero=False,
                                 description="Spacing two; else 'distance'.")

# rectangular_pattern spacing meaning, measured: with 3 instances and a 9 mm distance, 'spacing'
# puts the centres 9 mm apart, while 'extent' spreads 9 instances across a 9 mm TOTAL span.
_DISTANCE_TYPES = {"spacing": "SpacingPatternDistanceType", "extent": "ExtentPatternDistanceType"}
_DISTANCE_TYPE = _inputs.Choice("distance_type", list(_DISTANCE_TYPES), default="spacing",
                                description="spacing = gap per instance, extent = whole span.")

# autoConstrain's result option. Option 3 may ADJUST the sketch geometry within tolerance to reach a
# fully constrained solve, and returns null when the sketch is not eligible for that adjustment.
_RESULT_OPTIONS = {"option1": "Option1AutoConstrainResultType",
                   "option2": "Option2AutoConstrainResultType",
                   "option3": "Option3AutoConstrainResultType"}
_RESULT_OPTION = _inputs.Choice("result_option", list(_RESULT_OPTIONS), default="option1",
                                description="option3 may MOVE geometry within tolerance.")

# autoConstrain's four dimensioning knobs. The bindings say a preference "may be ignored if not
# applicable to the geometry", so each is published as REQUESTED, never as applied. Each family's
# Default* member is what a fresh AutoConstrainInput holds, so no option maps to it.
_LAYOUT_STRATEGIES = {
    "chain": "ChainDimensionStrategyType",
    "baseline": "BaselineDimensionStrategyType",
    "edge_aligned": "EdgeAndAlignedDimensionStrategyType",
    "symmetric_chain": "SymmetricAndChainDimensionStrategyType",
    "symmetric_baseline": "SymmetricAndBaselineDimensionStrategyType",
    "edge_aligned_angle_first": "EdgeAndAlignedHigherAngleDimPriorityDimensionStrategyType",
    "edge_aligned_baseline": "EdgeAndAlignedWithBaselineDimensionStrategyType"}
_INTER_LOOP_STRATEGIES = {"chain": "ChainInterLoopDimensionStrategyType",
                          "baseline": "BaselineInterLoopDimensionStrategyType"}
_SYMMETRIC_STRATEGIES = {
    "end_to_end": "EndToEndSymmetricDimensionStrategyType",
    "end_to_center": "EndToCenterSymmetricDimensionStrategyType",
    "center_to_end": "CenterToEndSymmetricDimensionStrategyType",
    "center_to_end_symmetric": "CenterToEndWithSymmetryConstraintSymmetricDimensionStrategyType"}
_LINEAR_DIAMETER = {"prefer": "PreferLinearDiameterDimensionPreferenceType",
                    "avoid": "AvoidLinearDiameterDimensionPreferenceType"}

# input name -> (AutoConstrainInput property, member table).
_STRATEGY_KNOBS = (
    ("dimension_strategy", "dimensionStrategy", _LAYOUT_STRATEGIES),
    ("inter_loop_strategy", "interLoopDimensionStrategy", _INTER_LOOP_STRATEGIES),
    ("symmetric_strategy", "symmetricDimensionStrategy", _SYMMETRIC_STRATEGIES),
    ("linear_diameter_dims", "linearDiameterDimensionPreference", _LINEAR_DIAMETER),
)
_STRATEGY_CHOICES = (
    _inputs.Choice("dimension_strategy", list(_LAYOUT_STRATEGIES)),
    _inputs.Choice("inter_loop_strategy", list(_INTER_LOOP_STRATEGIES)),
    _inputs.Choice("symmetric_strategy", list(_SYMMETRIC_STRATEGIES)),
    _inputs.Choice("linear_diameter_dims", list(_LINEAR_DIAMETER),
                   description="Diameter dims on circles."),
)

# Knobs that exist only on ONE constraint's input object - the property is simply absent on the
# others, so passing one elsewhere is a caller error rather than something to drop silently.
_KNOB_KINDS = {"suppressed": ("rectangular_pattern", "circular_pattern"),
               "dimension_strategy": ("auto",), "inter_loop_strategy": ("auto",),
               "symmetric_strategy": ("auto",), "linear_diameter_dims": ("auto",)}


def _cm_value(value_cm):
    """A ValueInput for a length already in internal centimetres, as an expression carrying 'cm'."""
    # GeometricConstraints' pattern input reads a ValueInput.createByReal as a number in the
    # DOCUMENT'S DEFAULT LENGTH UNIT, not centimetres, so every length this file hands it needs an
    # explicit unit - which means the same length under either reading.
    return adsk.core.ValueInput.createByString(f"{float(value_cm)} cm")


def _apply_offset(gc, cname, curves, dist_cm):
    """addOffset2 (one side) or addTwoSidesOffset (both sides, linked so one parameter drives both)."""
    oin = gc.createOffsetInput(curves, _cm_value(dist_cm))
    if cname == "offset_two_sides":
        return gc.addTwoSidesOffset(oin, True)
    return gc.addOffset2(oin)


def _enum_member(family, key, table):
    """The adsk enum member `table[key]` names, or None when this Fusion build does not carry it
    (set_verified and the callers report a None rather than running on a default)."""
    return getattr(family, table[key], None)


def _knob_guard(cname, passed):
    """The error for a knob passed to a constraint whose input object carries no such setting, or
    None. Dropping it silently would leave the caller believing a suppression or a strategy applied
    that the call never had anywhere to put."""
    for key in sorted(passed):
        kinds = _KNOB_KINDS[key]
        if cname not in kinds:
            return (f"'{key}' applies to constraint={' / '.join(kinds)} only, not "
                    f"'{cname}'. Drop it, or change 'constraint'.")
    return None


def _suppressed_flags(raw, instances, cname):
    """(flags, error) - the per-instance suppression list for a pattern of `instances` instances,
    or (None, None) when none was asked for: one flag per instance with the ORIGINAL not counting,
    in row-column order for a rectangular pattern. Another length is refused."""
    if raw is None or raw == "" or raw == []:
        return None, None
    items = raw
    if isinstance(raw, str):
        try:
            items = json.loads(raw)
        except Exception:
            return None, (f"'suppressed' must be a JSON list of true/false. Got '{raw}'.")
    if not isinstance(items, (list, tuple)) or any(not isinstance(v, bool) for v in items):
        return None, ("'suppressed' must be a list of true/false values, one per pattern instance. "
                      f"Got {items!r}.")
    want = instances - 1
    if len(items) != want:
        order = " in row-column order" if cname == "rectangular_pattern" else ""
        return None, (f"'suppressed' needs {want} flag(s) for this {cname} - one per instance"
                      f"{order}, with the original geometry not counting. Got {len(items)}.")
    return list(items), None


def _suppression_applied(constraint):
    """The per-instance suppression the CREATED pattern reports, as a list of bools - the platform
    can carry different flags than the input was handed, so the payload publishes what it reads.
    None when the constraint reports none that can be read."""
    raw = safe(lambda: constraint.isSuppressed)
    if raw is None:
        return None
    return safe(lambda: [bool(v) for v in raw])


def _apply_rect_pattern(gc, ents, dir_one, dir_two, qty_one, qty_two, dist_one_cm, dist_two_cm,
                        dist_type, symmetric, suppressed):
    """A sketch rectangular pattern, as (constraint, error); BOTH direction entities are required
    and 'ents' is a PLAIN LIST."""
    # A null direction entity raises "3 : invalid argument directionOneEntity" despite the binding
    # documenting it as the sketch X axis; the handler's guard refuses the call before it gets here.
    # An ObjectCollection raises TypeError "argument 2 of type 'std::vector<...SketchEntity...>'".
    pin = gc.createRectangularPatternInput(ents, dist_type)
    if pin.setDirectionOne(dir_one, adsk.core.ValueInput.createByReal(qty_one),
                           _cm_value(dist_one_cm)) is False:
        return None, ("Fusion refused direction one of the rectangular pattern (setDirectionOne "
                      "returned false), so no pattern was created.")
    if pin.setDirectionTwo(dir_two, adsk.core.ValueInput.createByReal(qty_two),
                           _cm_value(dist_two_cm)) is False:
        return None, ("Fusion refused direction two of the rectangular pattern (setDirectionTwo "
                      "returned false), so no pattern was created.")
    if symmetric:
        for prop in ("isSymmetricInDirectionOne", "isSymmetricInDirectionTwo"):
            serr = _common.set_verified(pin, prop, True, "symmetric", "rectangular_pattern")
            if serr:
                return None, serr
    if suppressed is not None:
        # After both directions: quantityOne and quantityTwo must hold valid values before
        # isSuppressed means anything. The getter echoes a TUPLE, so the flags are set in that form.
        serr = _common.set_verified(pin, "isSuppressed", tuple(suppressed), "suppressed",
                                    "rectangular_pattern")
        if serr:
            return None, serr
    return gc.addRectangularPattern(pin), None


def _apply_circ_pattern(gc, ents, center, qty, angle_deg, symmetric, suppressed):
    """A sketch circular pattern of 'qty' instances (the original included) spread over angle_deg,
    as (constraint, error). 'entities' is a PLAIN LIST - measured: an ObjectCollection here raises
    TypeError "argument 2 of type 'std::vector<...SketchEntity...>'"."""
    pin = gc.createCircularPatternInput(ents, center)
    pin.quantity = adsk.core.ValueInput.createByReal(int(qty))
    pin.totalAngle = adsk.core.ValueInput.createByString(f"{float(angle_deg)} deg")
    if symmetric:
        serr = _common.set_verified(pin, "isSymmetric", True, "symmetric", "circular_pattern")
        if serr:
            return None, serr
    if suppressed is not None:
        # a TUPLE, for the same measured echo as the rectangular input
        serr = _common.set_verified(pin, "isSuppressed", tuple(suppressed), "suppressed",
                                    "circular_pattern")
        if serr:
            return None, serr
    return gc.addCircularPattern(pin), None


def _symmetry_applied(constraint, props):
    """What the CREATED pattern reports for its symmetry flag(s) - the platform can decline a
    symmetry the input accepted, so the payload publishes what it reads, never what was asked for.
    None when no flag can be read."""
    got = [safe(lambda p=p: bool(getattr(constraint, p))) for p in props]
    readable = [g for g in got if g is not None]
    return all(readable) if readable else None


def _constraints_returned(result_obj):
    """The constraint(s) a creator call returned, as a Python list. addTwoSidesOffset hands back an
    OffsetConstraintVector - a SWIG sequence that answers len() and indexing but is NOT a list or
    tuple, so an isinstance check treats the whole vector as one constraint and every read off it
    fails. Anything that is not a sequence is wrapped as a single constraint."""
    if isinstance(result_obj, (list, tuple)):
        return list(result_obj)
    n = safe(lambda: len(result_obj))
    if n is None:
        return [result_obj]
    return [c for c in (safe(lambda i=i: result_obj[i]) for i in range(n)) if c is not None]


def _created_count(result_obj):
    """How many sketch entities the creator constraint made - an offset's childCurves or a pattern's
    createdEntities, summed over the constraint(s) returned (addTwoSidesOffset returns two). None
    when no count can be read."""
    items = _constraints_returned(result_obj)
    total = 0
    for c in items:
        n = safe(lambda c=c: len(c.childCurves))
        if n is None:
            n = safe(lambda c=c: len(c.createdEntities))
        if n is None:
            return None
        total += n
    return total


_MOVED_CAP = 20     # a large auto-constrain can nudge many entities; publish a bounded sample


def _strategy_families():
    """The adsk enum family behind each strategy knob, read through safe() so a build that does not
    carry one reports THAT knob unavailable instead of sinking the call."""
    return {"dimension_strategy": safe(lambda: adsk.fusion.DimensionStrategyTypes),
            "inter_loop_strategy": safe(lambda: adsk.fusion.InterLoopDimensionStrategyTypes),
            "symmetric_strategy": safe(lambda: adsk.fusion.SymmetricDimensionStrategyTypes),
            "linear_diameter_dims": safe(lambda: adsk.fusion.LinearDiameterDimensionPreferenceTypes)}


def _already_full_noop(sketch, option_key):
    """The clean no-op for an ALREADY fully constrained sketch. The platform RAISES
    "AutoConstrain cannot be applied to a fully constrained sketch" (measured live on 2705.0.87),
    so the call is refused there by Fusion itself - a sketch with nothing left to constrain
    answers with the truth instead of surfacing that raise."""
    return {"applied": "auto",
            "result_option_requested": option_key,
            "added_dimensions": 0, "added_constraints": 0,
            "dimension_count": safe(lambda: sketch.sketchDimensions.count, 0) or 0,
            "constraint_count": safe(lambda: sketch.geometricConstraints.count, 0) or 0,
            "is_fully_constrained": True,
            "note": "The sketch is already fully constrained - nothing to add. (Fusion refuses "
                    "AutoConstrain on a fully constrained sketch, so no call was made.)"}, None


def _apply_auto(sketch, option_key, strategies):
    """Auto-constrain the WHOLE sketch, as (AutoConstrainResult, error). A NULL result is this
    method's FAILURE channel ("Returns null in the case of a failure or if the input is invalid"),
    and under option3 it means the sketch is not eligible for the geometry adjustment that option
    asks for - so the two nulls carry different advice and neither is passed off as success."""
    member = _enum_member(adsk.fusion.AutoConstrainResultTypes, option_key, _RESULT_OPTIONS)
    aci = sketch.createAutoConstrainInput()
    if aci is None:
        return None, "createAutoConstrainInput returned nothing for this sketch."
    serr = _common.set_verified(aci, "resultOption", member, "result_option", "auto")
    if serr:
        return None, serr
    families = _strategy_families()
    for key, prop, table in _STRATEGY_KNOBS:
        if not strategies[key]:
            continue
        serr = _common.set_verified(aci, prop,
                                    _enum_member(families[key], strategies[key], table), key, "auto")
        if serr:
            return None, serr
    res = sketch.autoConstrain(aci)
    if res is None:
        if option_key == "option3":
            return None, ("autoConstrain returned null with result_option='option3': this sketch is "
                          "not eligible for geometry adjustment. Retry with result_option='option1' "
                          "or 'option2'. Nothing was constrained.")
        return None, (f"autoConstrain returned null with result_option='{option_key}' - null is how "
                      "it reports a failure or an invalid input. Nothing was constrained.")
    return res, None


def _auto_payload(sketch, res, option_key, before, strategies=None):
    """The autoConstrain read-back: what it ADDED (counted off the result AND off the sketch), the
    sketch's resulting constrained state, and the entities option3 MOVED. A result that added
    nothing to a sketch that was not already fully constrained is a success-shaped no-op, so it is
    an error here."""
    dims_before, cons_before, was_full = before
    n_dims = len(_constraints_returned(safe(lambda: res.addedDimensions) or []))
    n_cons = len(_constraints_returned(safe(lambda: res.addedConstraints) or []))
    moved = _constraints_returned(safe(lambda: res.movedGeometry) or [])
    dims_after = safe(lambda: sketch.sketchDimensions.count, 0) or 0
    cons_after = safe(lambda: sketch.geometricConstraints.count, 0) or 0
    fully = safe(lambda: bool(sketch.isFullyConstrained))

    # Option 3 can MOVE geometry within tolerance on its way to a solve, and those moves stay even
    # when the constrain half is a failure - so every refusal below has to say so, or the caller
    # reads "nothing was constrained" as "nothing happened" and never checks its coordinates.
    moved_note = (f" result_option='{option_key}' MOVED {len(moved)} entity(ies) within tolerance "
                  "before this - that geometry change REMAINS; re-read "
                  "sketch_get(include_entities=true)." if moved else "")
    if n_dims + n_cons == 0 and not was_full:
        return None, ("autoConstrain returned a result but added no dimensions or constraints, and "
                      f"the sketch is still not fully constrained ({cons_after} constraint(s), "
                      f"{dims_after} dimension(s)). Try result_option='option3', which may adjust "
                      "geometry within tolerance, or constrain it explicitly." + moved_note)
    if n_dims + n_cons > 0 and (dims_after - dims_before) + (cons_after - cons_before) <= 0:
        return None, (f"autoConstrain reported {n_dims} dimension(s) and {n_cons} constraint(s) "
                      f"added but the sketch still holds {dims_after} dimension(s) and "
                      f"{cons_after} constraint(s) - nothing landed in it." + moved_note)

    note = ("The sketch is now fully constrained." if fully else
            "The sketch is NOT fully constrained yet - dimension the remaining freedom with "
            "sketch_dimension, or retry with result_option='option3', which may adjust geometry "
            "within tolerance to close the solve.")
    out = {"applied": "auto",
           "result_option_requested": option_key,
           "added_dimensions": n_dims, "added_constraints": n_cons,
           "dimension_count": dims_after, "constraint_count": cons_after,
           "is_fully_constrained": fully}
    if moved:
        refs = [r for r in (_sketch_detail.curve_id(sketch, m) for m in moved[:_MOVED_CAP]) if r]
        out["moved_geometry_count"] = len(moved)
        if refs:
            out["moved_geometry"] = refs
        note = (f"result_option='{option_key}' MOVED {len(moved)} entity(ies) within tolerance to "
                "reach the solve - their coordinates changed. " + note)
    asked = {k: v for k, v in (strategies or {}).items() if v}
    if asked:
        # every strategy is documented as a preference the operation may ignore where it does not
        # apply, and the result reports no strategy back - so these are published as REQUESTED.
        out["strategies_requested"] = asked
        note += (" The strategy settings were REQUESTED - Fusion ignores one that does not apply "
                 "to this geometry, and reports no strategy back, so read the dimensions it added "
                 "to see which layout landed.")
    out["note"] = note + (" Read what it added with sketch_get(include_entities=true).")
    return out, None


def _count_param(obj, attr):
    """The integer value of a pattern constraint's count ModelParameter, or None if unreadable."""
    return safe(lambda: int(round(getattr(obj, attr).value)))


def _length(kind, raw, k, cname, label):
    """(cm value, error) for a creator kind's length input; a missing one is named, not defaulted."""
    v, err = kind.resolve_scaled(raw, k)
    if err:
        return None, err
    if v is None:
        return None, f"'{cname}' needs '{label}' - a length in the call's 'units'."
    return v, None


# The fields one 'constraints' entry takes - the wire schema and the entry guard read this table.
_ENTRY_FIELDS = ("constraint", "entity_one", "entity_two", "symmetry_line", "entities", "surface",
                 "distance", "distance_two", "quantity", "quantity_two", "angle", "distance_type",
                 "symmetric", "suppressed", "result_option", "dimension_strategy",
                 "inter_loop_strategy", "symmetric_strategy", "linear_diameter_dims")


def handler(constraints=None, sketch_name: str = "", component: str = "",
            units: str = "mm") -> dict:
    """See TOOL_DESCRIPTION."""
    entries, eerr = _sketch_batch.entries_or_error(constraints, "constraints", _ENTRY_FIELDS)
    if eerr:
        return error(eerr)
    design = _common.design()
    if not design:
        return error("No active design.")
    wanted = (sketch_name or "").strip()
    sketch, refusal = _sketch_detail.scoped_sketch(design, wanted, component)
    if refusal:
        return error(refusal)
    if not sketch:
        names = all_sketch_names(design)
        return error(f"No sketch named '{wanted}'. Available: "
                     + (", ".join(n for n in names if n) or "(none)") + ". Use sketch_get.")
    k, uerr = _inputs.UNITS.resolve(units)
    if uerr:
        return error(uerr)
    return _sketch_batch.run_batch(entries, lambda _i, e: _one(sketch, k, e), "constraints",
                                   "constrained", safe(lambda: sketch.name))


def _one(sketch, k, entry):
    """(result, error) for ONE constraint entry applied to `sketch`, at unit scale `k`."""
    constraint = entry.get("constraint") or ""
    entity_one = entry.get("entity_one") or ""
    entity_two = entry.get("entity_two") or ""
    symmetry_line = entry.get("symmetry_line") or ""
    entities = entry.get("entities") or ""
    surface = entry.get("surface") or ""
    distance = entry.get("distance")
    distance_two = entry.get("distance_two")
    quantity = entry.get("quantity", 2)
    quantity_two = entry.get("quantity_two", 1)
    angle = entry.get("angle", 360.0)
    symmetric = entry.get("symmetric", False)
    suppressed = entry.get("suppressed")
    cname = (constraint or "").strip().lower()
    if cname not in _CONSTRAINTS:
        return None, f"Unknown constraint '{constraint}'. Valid: {', '.join(_CONSTRAINTS)}."
    kind, method = _CONSTRAINTS[cname]
    choices, cerr = _inputs.resolve_inputs(
        [_DISTANCE_TYPE, _RESULT_OPTION] + list(_STRATEGY_CHOICES),
        {name: entry.get(name) for name in ("distance_type", "result_option",
                                            "dimension_strategy", "inter_loop_strategy",
                                            "symmetric_strategy", "linear_diameter_dims")})
    if cerr:
        return None, cerr["message"]
    strategies = {key: choices[key] for key, _prop, _table in _STRATEGY_KNOBS}
    passed = {key: v for key, v in strategies.items() if v}
    if suppressed not in (None, "", []):
        passed["suppressed"] = suppressed
    kerr = _knob_guard(cname, passed)
    if kerr:
        return None, kerr

    # A ref's optional third segment names WHICH point of the entity is meant ('circle:0:center') -
    # the same grammar sketch_dimension reads, parsed here before the bare ref is resolved.
    base_one, anchor_one, aerr = _common.parse_anchor_ref(entity_one)
    if aerr:
        return None, aerr
    base_two, anchor_two, aerr2 = _common.parse_anchor_ref(entity_two)
    if aerr2:
        return None, aerr2
    takes_one, takes_two = _ANCHOR_SLOTS.get(cname, (False, False))
    if anchor_one and not takes_one:
        return None, _anchor_refusal(cname, "entity_one", anchor_one)
    if anchor_two and not takes_two:
        return None, _anchor_refusal(cname, "entity_two", anchor_two)

    # A SketchText is an operand for fix/unfix alone - every other constraint's add* takes sketch
    # curves or points, which a text is not.
    text_obj, anchor_lines = None, []
    if _is_text_ref(base_one):
        if kind != "fix":
            return None, (f"a 'text:<index>' ref applies to constraint=fix / unfix only - no other "
                          f"constraint takes a sketch TEXT as an operand. '{cname}' takes "
                          f"{_REQUIRES.get(cname, 'sketch curves or points')}.")
        text_obj, terr = _text_at_ref(sketch, base_one)
        if terr:
            return None, terr

    e1 = None
    if text_obj is None and kind != "auto" and (kind not in _OPTIONAL_ENTITY_ONE or base_one.strip()):
        e1 = _common.resolve_entity_ref(sketch, base_one)
        if not e1:
            return None, (f"Could not resolve entity_one '{entity_one}' "
                          f"(use '<type>:<index>', type = {'/'.join(_common.ENTITY_REF_KINDS)}).")
    # BOTH refs resolve before EITHER anchor is built: a 'mid' anchor CREATES a point and a midpoint
    # constraint, so a refusal after that leaves an orphan behind in the sketch. Nothing is added
    # until every operand this call needs is in hand.
    e2 = None
    if kind in _TWO_ENTITY_KINDS:
        e2 = _common.resolve_entity_ref(sketch, base_two)
        if not e2:
            return None, (f"'{cname}' needs 'entity_two' (a second '<type>:<index>'). "
                          f"Got '{entity_two}'.")
    if anchor_one:
        e1, perr = _common.anchor_point(sketch, e1, anchor_one)
        if perr:
            return None, f"entity_one '{entity_one}': {perr}"
    if anchor_two:
        e2, perr = _common.anchor_point(sketch, e2, anchor_two)
        if perr:
            return None, f"entity_two '{entity_two}': {perr}"
    if (cname == "coincident" and _names_a_point(base_one, anchor_one)
            and _names_a_point(base_two, anchor_two) and _one_operand(e1, e2)):
        return None, (f"'{entity_one}' and '{entity_two}' resolve to ONE sketch point, so there is "
                      "nothing to constrain. Name two different points, or drop the call; "
                      "sketch_get(include_entities=true) lists them.")

    ents = None
    if kind in _LIST_OPERAND_KINDS:
        ents, _refs, lerr = _common.resolve_entity_refs(sketch, entities)
        if lerr:
            return None, lerr
        if not ents:
            return None, (f"'{cname}' needs 'entities' - comma-separated '<type>:<index>' refs. "
                          f"Got '{entities}'.")

    gc = safe(lambda: sketch.geometricConstraints)
    auto_before = None
    flags = None

    try:
        if kind == "auto":
            auto_before = (safe(lambda: sketch.sketchDimensions.count, 0) or 0,
                           safe(lambda: sketch.geometricConstraints.count, 0) or 0,
                           bool(safe(lambda: sketch.isFullyConstrained)))
            if auto_before[2]:
                return _already_full_noop(sketch, choices["result_option"])
            result_obj, autoerr = _apply_auto(sketch, choices["result_option"], strategies)
            if autoerr:
                return None, autoerr
        elif kind == "fix":
            want = (cname == "fix")
            # The requested mutation - set it directly (inside this try) so a failure is reported, not
            # swallowed by safe() into the unconditional result_obj=True below.
            if text_obj is not None:
                anchor_lines = _text_anchor_lines(text_obj)
                if not anchor_lines:
                    return None, (f"'{entity_one}' resolved to a sketch text whose definition hands "
                                  "back no rectangle lines - there is no anchor to lock.")
                for ln in anchor_lines:
                    ln.isFixed = want
                landed = sum(1 for ln in anchor_lines
                             if _common.read_flag(lambda ln=ln: ln.isFixed) is want)
                if landed != len(anchor_lines):
                    return None, (f"{landed} of {len(anchor_lines)} anchor lines took the {cname} - "
                                  "the text's anchor is left partly locked. Re-read the sketch with "
                                  "sketch_get before relying on its constrained state.")
                result_obj = True
            else:
                e1.isFixed = want
                result_obj = (safe(lambda: e1.isFixed) == want)
        elif kind == "one_line":
            result_obj = getattr(gc, method)(e1)
        elif kind in _TWO_ENTITY_KINDS:
            result_obj = getattr(gc, method)(e1, e2)
        elif kind == "symmetry":
            e2 = _common.resolve_entity_ref(sketch, entity_two)
            if not e2:
                return None, f"'symmetry' needs 'entity_two'. Got '{entity_two}'."
            sline = _common.resolve_entity_ref(sketch, symmetry_line)
            if not sline:
                return None, ("'symmetry' needs 'symmetry_line' - the axis line ref "
                              "(e.g. 'line:0').")
            result_obj = getattr(gc, method)(e1, e2, sline)
        elif kind == "entity_surface":
            surf, serr = _SURFACE.resolve(surface, cname)
            if serr:
                return None, serr
            if surf is None:
                return None, (f"'{cname}' needs 'surface' - a plane alias (xy/xz/yz), a "
                              "construction-plane name, or a face handle from find_geometry"
                              + (" (curved faces allowed)." if cname in _CURVED_SURFACE_OK
                                 else " (this constraint takes a PLANAR face only)."))
            result_obj = getattr(gc, method)(e1, surf)
        elif kind == "entity_list":
            if len(ents) < 3:
                return None, (f"'{cname}' needs at least 3 lines in 'entities' to close a shape. "
                              f"Got {len(ents)}.")
            result_obj = getattr(gc, method)(ents)
        elif kind == "offset":
            d1, derr = _length(_DISTANCE, distance, k, cname, "distance")
            if derr:
                return None, derr
            result_obj = _apply_offset(gc, cname, ents, d1)
        elif kind == "circ_pattern":
            if int(quantity) < 2:
                return None, f"'{cname}' needs quantity >= 2. Got {quantity}."
            flags, ferr = _suppressed_flags(suppressed, int(quantity), cname)
            if ferr:
                return None, ferr
            result_obj, perr = _apply_circ_pattern(gc, ents, e1, int(quantity), float(angle),
                                                   bool(symmetric), flags)
            if perr:
                return None, perr
        elif kind == "rect_pattern":
            e2_dir = _common.resolve_entity_ref(sketch, entity_two)
            if e1 is None or e2_dir is None:
                missing = [n for n, v in (("entity_one", e1), ("entity_two", e2_dir)) if v is None]
                return None, (f"'{cname}' needs BOTH direction lines - {' and '.join(missing)} did "
                              "not resolve. A null direction is documented as the sketch X axis but "
                              "the API refuses it ('invalid argument directionOneEntity').")
            if int(quantity) < 1 or int(quantity_two) < 1:
                return None, (f"'{cname}' needs quantity >= 1 and quantity_two >= 1. "
                              f"Got {quantity} and {quantity_two}.")
            d1, derr = _length(_DISTANCE, distance, k, cname, "distance")
            if derr:
                return None, derr
            d2, d2err = _length(_DISTANCE_TWO,
                                distance_two if distance_two is not None else distance, k,
                                cname, "distance_two")
            if d2err:
                return None, d2err
            dtype = _enum_member(adsk.fusion.PatternDistanceType, choices["distance_type"],
                                 _DISTANCE_TYPES)
            if dtype is None:
                return None, (f"PatternDistanceType.{_DISTANCE_TYPES[choices['distance_type']]} is "
                              "not available on this Fusion version.")
            flags, ferr = _suppressed_flags(suppressed, int(quantity) * int(quantity_two), cname)
            if ferr:
                return None, ferr
            result_obj, perr = _apply_rect_pattern(gc, ents, e1, e2_dir, int(quantity),
                                                   int(quantity_two), d1, d2, dtype,
                                                   bool(symmetric), flags)
            if perr:
                return None, perr
        else:
            return None, f"unsupported constraint kind '{kind}'."
    except Exception as e:
        # The API raises the same way for a wrong operand type and for an unsolvable sketch.
        req = _REQUIRES.get(cname)
        if req:
            return None, f"Could not apply {cname}: {e} | '{cname}' takes {req}."
        return None, f"Could not apply {cname}: {e}"
    if not result_obj:
        return None, f"Applying {cname} returned no constraint object."
    if kind == "auto":
        return _auto_payload(sketch, result_obj, choices["result_option"], auto_before, strategies)

    created = _created_count(result_obj) if kind in _CREATOR_KINDS else None
    # MEASURED: a suppressed instance produces no curve (a 3x2 pattern with one flag set creates 4,
    # not 5), so a pattern whose every instance was suppressed legitimately creates none - that is
    # the request, not the silent no-op the gate below exists to catch.
    all_suppressed = bool(flags) and all(flags)
    if created == 0 and not all_suppressed:
        return None, (f"'{cname}' returned a constraint but added no sketch geometry - nothing was "
                      "created. Delete it with sketch_delete_entity(target='constraint:<index>').")
    if kind in ("rect_pattern", "circ_pattern"):
        wanted = {"quantity": int(quantity)} if kind == "circ_pattern" else {
            "quantityOne": int(quantity), "quantityTwo": int(quantity_two)}
        for attr, want in wanted.items():
            got = _count_param(result_obj, attr)
            if got is not None and got != want:
                return None, (f"'{cname}' was created with {attr} = {got} but {want} was requested. "
                              "The constraint is left in the sketch for inspection - "
                              "sketch_delete_entity(target='constraint:<index>') removes it.")
        if flags is not None:
            landed_flags = _suppression_applied(result_obj)
            if landed_flags is not None and landed_flags != flags:
                return None, (f"'{cname}' was created with {sum(landed_flags)} instance(s) "
                              f"suppressed, not the {sum(flags)} requested, so the pattern is not "
                              "what was asked for. The constraint is left in the sketch for "
                              "inspection - sketch_delete_entity(target='constraint:<index>') "
                              "removes it.")
        if kind == "rect_pattern":
            landed = safe(lambda: result_obj.distanceType)
            if landed is not None and landed != dtype:
                return None, (f"'{cname}' was created with a different distance_type than the "
                              f"'{choices['distance_type']}' requested, so its spacing is not what "
                              "was asked for. The constraint is left in the sketch for inspection - "
                              "sketch_delete_entity(target='constraint:<index>') removes it.")

    payload = {
        "applied": cname,
        "entity_one": entity_one or None,
        "entity_two": entity_two or None,
        "symmetry_line": symmetry_line or None,
        "note": "Geometric constraint applied - the sketch is now parametric for this relationship.",
    }
    # addCoincident(point, curve) SUCCEEDS on the wrong geometry: it lands the point ON the curve,
    # so a caller who meant "centre this circle here" gets a clean ok and a circle on its own rim.
    # The note on the success path carries the anchor that expresses the other intent.
    if text_obj is not None:
        fully = _common.read_flag(lambda: sketch.isFullyConstrained)
        payload["anchor_lines_fixed"] = len(anchor_lines)
        payload["is_fully_constrained"] = fully
        payload["note"] = (
            f"The sketch text's anchor is {'LOCKED' if cname == 'fix' else 'RELEASED'} - "
            f"{cname} applied to the {len(anchor_lines)} rectangle lines of its definition, the "
            "degree of freedom no geometric constraint can address. "
            + ("The sketch now reads FULLY CONSTRAINED." if fully is True else
               "The sketch is still NOT fully constrained - other geometry holds the remaining "
               "freedom (sketch_get(include_entities=true) shows what)." if fully is False else
               "The sketch's constrained state did not read back."))
    if cname == "coincident" and not anchor_two:
        curve_note = _coincident_curve_note(base_two)
        if curve_note:
            payload["note"] += curve_note
    if kind in _CREATOR_KINDS or kind == "entity_list":
        payload["entities"] = entities
    if kind in _CREATOR_KINDS:
        payload["created_count"] = created
        payload["note"] = (f"{cname} applied, adding {created if created is not None else 'new'} "
                           "sketch entities - read their '<type>:<index>' refs with sketch_get.")
        if all_suppressed:
            payload["note"] = (f"{cname} applied with EVERY instance suppressed, so it created no "
                               "curves - the pattern constraint itself is in the sketch. Re-run "
                               "with fewer 'suppressed' flags set for a pattern that draws.")
    if kind in ("rect_pattern", "circ_pattern"):
        props = (("isSymmetricInDirectionOne", "isSymmetricInDirectionTwo")
                 if kind == "rect_pattern" else ("isSymmetric",))
        if kind == "rect_pattern":
            payload["distance_type"] = choices["distance_type"]
        applied = _symmetry_applied(result_obj, props)
        payload["symmetric_requested"] = bool(symmetric)
        payload["symmetric_applied"] = applied
        if bool(symmetric) and applied is False:
            payload["note"] += (" The pattern was created but reads back NOT symmetric - Fusion "
                                "declined the symmetry for this geometry.")
        if flags is not None:
            payload["suppressed_requested"] = flags
            payload["suppressed_applied"] = _suppression_applied(result_obj)
            if payload["suppressed_applied"] is None:
                payload["note"] += (" The suppression was accepted by the pattern input but the "
                                    "created constraint reports no flags to read back, so only "
                                    "what was REQUESTED is published.")
    if kind == "entity_surface":
        # what the operand RESOLVED to (a construction plane's name, a face's type), not the token
        payload["surface"] = _inputs.surface_ref_label(surf)
    return payload, None


TOOL_DESCRIPTION = (
    "Apply geometric constraints to one sketch, entities '<type>:<index>'."
)

# One 'constraints' entry on the wire: the same fields the entry guard admits (_ENTRY_FIELDS).
_ENTRY_SCHEMA = {
    "type": "object",
    "properties": {
        "constraint": _CONSTRAINT.schema(),
        "entity_one": {"type": "string",
                       "description": "Point slots take an anchor: 'circle:0:center'."},
        "entity_two": {"type": "string"},
        "symmetry_line": {"type": "string"},
        "entities": {"type": "string"},
        "surface": _SURFACE.schema(),
        "distance": _DISTANCE.schema(),
        "distance_two": _DISTANCE_TWO.schema(brief=True),
        "quantity": {"type": "integer", "description": "Includes the original.", "default": 2},
        "quantity_two": {"type": "integer", "default": 1},
        "angle": {"type": "number", "description": "circular_pattern total angle, degrees.",
                  "default": 360.0},
        "distance_type": _DISTANCE_TYPE.schema(),
        "symmetric": {"type": "boolean"},
        "suppressed": {"type": "array", "items": {"type": "boolean"}},
        "result_option": _RESULT_OPTION.schema(),
        "dimension_strategy": _STRATEGY_CHOICES[0].schema(),
        "inter_loop_strategy": _STRATEGY_CHOICES[1].schema(),
        "symmetric_strategy": _STRATEGY_CHOICES[2].schema(),
        "linear_diameter_dims": _STRATEGY_CHOICES[3].schema(),
    },
    "required": ["constraint"],
    "additionalProperties": False,
}

tool = (
    Tool.create_simple(name="sketch_constrain", description=TOOL_DESCRIPTION)
    .add_input_property("constraints", {"type": "array", "items": _ENTRY_SCHEMA,
            "description": "Run in order; the first failure stops the run."})
    .add_required_input("constraints")
    .add_input_property("sketch_name", {"type": "string"})
    .add_input_property(*_sketch_detail.COMPONENT_SCOPE)
    .add_input_property(*_inputs.UNITS.as_property())
    .strict_schema()
)
item = Item.create_tool_item(
    tool=tool, write="write", handler=handler, run_on_main_thread=True,
    verification=Verification(
        kind="inline", rung="value",
        evidence_test="tests/unit/test_sketch_constrain.py::TestSingleLine"
                      "::test_a_silently_declined_fix_is_an_error_not_a_false_success"))


def register_tool():
    register(item)
