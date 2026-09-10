"""Unit tests for ``sketch_constrain.py`` — apply geometric constraints to sketch entities.

Adds the Sketch Constrain menu - every GeometricConstraints add* kind, from perpendicular through
the point-align, surface, polygon, offset and pattern kinds - to sketch entities referenced by
'<type>:<index>' within a named sketch (no human selection). One call carries a LIST of
constraints against one resolved sketch; ``_constrain`` drives the single-entry form.

Pinned here (no live Fusion): the entity resolver ('line:0' -> sketch.sketchCurves.sketchLines
.item(0); 'point:2' -> sketch.sketchPoints.item(2)), the constraint DISPATCH by arity (which add*
method each constraint routes to + what it needs), and the read-back gates on the kinds that CREATE
geometry. The actual constraint creation is captured on a fake GeometricConstraints whose add*
methods refuse the operand kinds the live bindings refuse.
"""

import ast
import json
import re
import types
from pathlib import Path

import adsk.core
import adsk.fusion
import pytest

from conftest import (MakeComp, Sketch, SketchCurves, _NamedCollection, load_tool, make_design,
                      install as install_design)

sc = load_tool("sketch_constrain")


# ── fakes ───────────────────────────────────────────────────────────────────


class FakeCurve:
    def __init__(self, name, kind=None):
        self.name = name
        self.kind = kind
        self.isFixed = False


class FakePlanarEntity:
    """A BRepFace/ConstructionPlane - what the *_to_surface constraints take as their second
    argument, and what a PlaneRef resolves to. Not a sketch entity, and not adsk.core.Plane."""
    def __init__(self, name):
        self.name = name
        self.kind = "plane"


# the SketchCurve kinds ('point' is a SketchPoint, not a curve) and the three spline collections
_CURVE_KINDS = ("line", "arc", "circle", "ellipse", "spline", "cv_spline", "fixed_spline")
_SPLINE_KINDS = ("spline", "cv_spline", "fixed_spline")


class FakeParam:
    """A pattern constraint's count ModelParameter - the value read back off the created pattern."""
    def __init__(self, value):
        self.value = float(value)


class FakeOffsetConstraint:
    def __init__(self, n):
        self.childCurves = [FakeCurve(f"OFF{i}", "line") for i in range(n)]


class FakeConstraintVector:
    """addTwoSidesOffset returns an OffsetConstraintVector - a SWIG sequence answering len() and
    indexing, but NOT a list or tuple. Code that isinstance-checks for a list treats the whole
    vector as one constraint and reads nothing off it."""
    def __init__(self, items):
        self._items = list(items)
    def __len__(self):
        return len(self._items)
    def __getitem__(self, i):
        return self._items[i]


class FakePatternConstraint:
    def __init__(self, created, **counts):
        self.createdEntities = [FakeCurve(f"PAT{i}", "line") for i in range(created)]
        for name, value in counts.items():
            setattr(self, name, FakeParam(value))


class FakeOffsetInput:
    def __init__(self, curves, offset):
        self.curves = list(curves)
        self.offset = offset


class _SuppressionInput:
    """The half of a pattern input that carries per-instance suppression. The echo is a TUPLE on
    both live input classes, whatever sequence was assigned - a fake echoing a list would hide the
    tuple != list comparison that decides whether the set is seen as taking. `order` records when
    the flags were assigned relative to the quantities: the binding requires the quantities to hold
    valid values BEFORE isSuppressed means anything."""
    def __init__(self):
        self.order = []
        self._suppressed = None
    @property
    def isSuppressed(self):
        return self._suppressed
    @isSuppressed.setter
    def isSuppressed(self, value):
        self.order.append("suppressed")
        self._suppressed = tuple(value)


class FakeRectPatternInput(_SuppressionInput):
    def __init__(self, entities, distance_type):
        super().__init__()
        self.entities = list(entities)
        self.distanceType = distance_type
        self.one = None
        self.two = None
    def setDirectionOne(self, entity, quantity, distance):
        self.order.append("direction_one")
        self.one = (entity, quantity, distance)
        return True
    def setDirectionTwo(self, entity, quantity, distance):
        self.order.append("direction_two")
        self.two = (entity, quantity, distance)
        return True


class FakeCircPatternInput(_SuppressionInput):
    def __init__(self, entities, center):
        super().__init__()
        self.entities = list(entities)
        self.centerPoint = center
        self.quantity = None
        self.totalAngle = None


class FakeConstraints:
    """Mirrors live GeometricConstraints: an add* handed the wrong SketchEntity subclass raises at
    the API boundary (e.g. addHorizontal with a SketchCircle), it does not return a constraint."""
    def __init__(self):
        self.calls = []
        # what the API HANDS BACK, independent of what was asked for: the platform can create a
        # different count than requested, which the tool has to catch instead of echoing the request.
        self.pattern_quantity_is = None
        self.offset_curves_created = None
        self.count = 0                 # constraints the sketch holds, as the live collection reports
        # what the CREATED pattern reports for the two settings the input asked for - the platform
        # can decline a symmetry or land a different distance type than the one it was handed.
        self.pattern_symmetric_is = None
        self.pattern_distance_type_is = None
        # the per-instance suppression the CREATED pattern reports, and whether it reports any at
        # all - a constraint that carries no readable flags leaves the tool with only the request.
        self.pattern_suppressed_is = None
        self.pattern_suppression_readable = True
    def _rec(self, name, *args):
        self.calls.append((name, args))
        return (name, args)
    def _require(self, entity, *kinds):
        k = getattr(entity, "kind", None)
        if k not in kinds:
            raise TypeError(f"invalid argument: a {k or 'unknown'} entity where "
                            f"{'/'.join(kinds)} is required")
    def _require_same(self, a, b, *kinds):
        self._require(a, *kinds)
        self._require(b, *kinds)
        ka, kb = getattr(a, "kind", None), getattr(b, "kind", None)
        if ka != kb:
            raise TypeError(f"invalid argument value: {ka} and {kb} are different kinds")
    # live-verified type gates: perpendicular/parallel/collinear/horizontal/vertical and
    # symmetry's axis take a typed SketchLine; coincident/midpoint take a typed SketchPoint.
    def addPerpendicular(self, a, b):
        self._require(a, "line")
        self._require(b, "line")
        return self._rec("perpendicular", a, b)
    def addParallel(self, a, b):
        self._require(a, "line")
        self._require(b, "line")
        return self._rec("parallel", a, b)
    def addTangent(self, a, b):
        # live-verified: a point arg raises at the SWIG boundary (SketchCurve required), while an
        # ellipse and a fitted spline both pass the type gate
        self._require(a, "line", "arc", "circle", "ellipse", "spline")
        self._require(b, "line", "arc", "circle", "ellipse", "spline")
        return self._rec("tangent", a, b)
    def addEqual(self, a, b):
        # live-verified: matching kinds AND only these three - arc+circle, ellipse+ellipse and
        # cv_spline+cv_spline all raise "3 : invalid argument value"
        self._require_same(a, b, "line", "arc", "circle")
        return self._rec("equal", a, b)
    def addConcentric(self, a, b):
        # live-verified: a line arg raises "3 : invalid argument entityOne"; ellipse+circle passes
        self._require(a, "arc", "circle", "ellipse")
        self._require(b, "arc", "circle", "ellipse")
        return self._rec("concentric", a, b)
    def addCollinear(self, a, b):
        self._require(a, "line")
        self._require(b, "line")
        return self._rec("collinear", a, b)
    def addMidPoint(self, p, c):
        self._require(p, "point")
        return self._rec("midpoint", p, c)
    def addCoincident(self, p, e):
        self._require(p, "point")
        return self._rec("coincident", p, e)
    def addHorizontal(self, l):
        self._require(l, "line")
        return self._rec("horizontal", l)
    def addVertical(self, l):
        self._require(l, "line")
        return self._rec("vertical", l)
    def addSymmetry(self, a, b, line):
        self._require(line, "line")
        return self._rec("symmetry", a, b, line)

    # ── the kinds whose gates come from the installed bindings' argument types ──
    def _require_surface(self, s, curved_ok=False):
        # every *_to_surface method takes a BRepFace or ConstructionPlane, never a sketch entity.
        # addCoincidentToSurface and addPerpendicularToSurface take a `surface: Base` and accept a
        # CURVED face - both applied to a cylinder live; the two PlanarSurface-named methods do not.
        allowed = ("plane", "curved_face") if curved_ok else ("plane",)
        if getattr(s, "kind", None) not in allowed:
            raise TypeError(f"invalid argument: a {getattr(s, 'kind', None) or 'unknown'} where a "
                            + ("BRepFace or ConstructionPlane" if curved_ok
                               else "PLANAR BRepFace or ConstructionPlane") + " is required")
    def addSmooth(self, a, b):
        # addSmooth(curveOne: SketchCurve, curveTwo: SketchCurve) - "One of the curves must be a
        # spline. The other curve can be a spline or any other type of curve."
        self._require(a, *_CURVE_KINDS)
        self._require(b, *_CURVE_KINDS)
        if a.kind not in _SPLINE_KINDS and b.kind not in _SPLINE_KINDS:
            raise TypeError("invalid argument value: one of the curves must be a spline")
        return self._rec("smooth", a, b)
    def addHorizontalPoints(self, p1, p2):
        self._require(p1, "point")
        self._require(p2, "point")
        return self._rec("horizontal_points", p1, p2)
    def addVerticalPoints(self, p1, p2):
        self._require(p1, "point")
        self._require(p2, "point")
        return self._rec("vertical_points", p1, p2)
    def addCoincidentToSurface(self, point, surface):
        self._require(point, "point")
        self._require_surface(surface, curved_ok=True)
        return self._rec("coincident_to_surface", point, surface)
    def addLineOnPlanarSurface(self, line, surface):
        self._require(line, "line")
        self._require_surface(surface)
        return self._rec("line_on_surface", line, surface)
    def addLineParallelToPlanarSurface(self, line, surface):
        self._require(line, "line")
        self._require_surface(surface)
        return self._rec("line_parallel_to_surface", line, surface)
    def addPerpendicularToSurface(self, curve, surface):
        # addPerpendicularToSurface(curve: SketchCurve, ...) - "Line and spline curves are supported."
        self._require(curve, "line", *_SPLINE_KINDS)
        self._require_surface(surface, curved_ok=True)
        return self._rec("perpendicular_to_surface", curve, surface)
    def addPolygon(self, lines):
        for line in lines:
            self._require(line, "line")
        return self._rec("polygon", list(lines))

    def createOffsetInput(self, curves, offset):
        for c in curves:
            self._require(c, *_CURVE_KINDS)
        return FakeOffsetInput(curves, offset)
    def addOffset2(self, oin):
        self._rec("offset", oin)
        return FakeOffsetConstraint(self.offset_curves_created
                                    if self.offset_curves_created is not None else len(oin.curves))
    def addTwoSidesOffset(self, oin, link_offsets):
        self._rec("offset_two_sides", oin, link_offsets)
        n = (self.offset_curves_created if self.offset_curves_created is not None
             else len(oin.curves))
        return FakeConstraintVector([FakeOffsetConstraint(n), FakeOffsetConstraint(n)])

    def createRectangularPatternInput(self, entities, distance_type):
        for e in entities:
            self._require(e, "point", *_CURVE_KINDS)
        return FakeRectPatternInput(entities, distance_type)
    def addRectangularPattern(self, pin):
        if pin.one is None:
            raise TypeError("invalid argument value: direction one was never set")
        q1, q2 = int(pin.one[1]), int(pin.two[1]) if pin.two else 1
        self._rec("rectangular_pattern", pin)
        # measured: a suppressed instance produces NO curve - a 3x2 with one flag creates 4, not 5
        instances = (q1 * q2 - 1) - sum(pin.isSuppressed or ())
        c = FakePatternConstraint(len(pin.entities) * instances,
                                  quantityOne=self.pattern_quantity_is or q1, quantityTwo=q2)
        c.distanceType = (pin.distanceType if self.pattern_distance_type_is is None
                          else self.pattern_distance_type_is)
        sym = (bool(getattr(pin, "isSymmetricInDirectionOne", False))
               if self.pattern_symmetric_is is None else self.pattern_symmetric_is)
        c.isSymmetricInDirectionOne = sym
        c.isSymmetricInDirectionTwo = sym
        self._carry_suppression(c, pin)
        return c

    def _carry_suppression(self, constraint, pin):
        """What the created pattern reports for suppression - by default the flags the input was
        handed, or the platform's own answer when the test wires one. A TUPLE, as the created
        constraint reports it."""
        if not self.pattern_suppression_readable:
            return
        constraint.isSuppressed = tuple(pin.isSuppressed or () if self.pattern_suppressed_is is None
                                        else self.pattern_suppressed_is)
    def createCircularPatternInput(self, entities, center):
        for e in entities:
            self._require(e, "point", *_CURVE_KINDS)
        self._require(center, "point")
        return FakeCircPatternInput(entities, center)
    def addCircularPattern(self, pin):
        q = int(pin.quantity)
        self._rec("circular_pattern", pin)
        c = FakePatternConstraint(len(pin.entities) * ((q - 1) - sum(pin.isSuppressed or ())),
                                  quantity=self.pattern_quantity_is or q)
        c.isSymmetric = (bool(getattr(pin, "isSymmetric", False))
                         if self.pattern_symmetric_is is None else self.pattern_symmetric_is)
        self._carry_suppression(c, pin)
        return c


class FakeSketchCurves(SketchCurves):
    """The shared SketchCurves, every per-kind sub-collection filled from its own argument."""
    def __init__(self, lines, arcs, circles, ellipses=(), splines=(), cv_splines=(), fixed_splines=()):
        super().__init__()
        self.sketchLines = _NamedCollection(lines)
        self.sketchArcs = _NamedCollection(arcs)
        self.sketchCircles = _NamedCollection(circles)
        self.sketchEllipses = _NamedCollection(ellipses)
        self.sketchFittedSplines = _NamedCollection(splines)
        self.sketchControlPointSplines = _NamedCollection(cv_splines)
        self.sketchFixedSplines = _NamedCollection(fixed_splines)


class FakeSketch(Sketch):
    """The shared Sketch carrying the entity collections a '<type>:<index>' ref indexes, the
    recording geometricConstraints, and the autoConstrain input/result the tool round-trips."""
    def __init__(self, name, lines=(), arcs=(), circles=(), points=(), ellipses=(), splines=(),
                cv_splines=(), fixed_splines=(), dimensions=()):
        super().__init__(name=name,
                         curves=FakeSketchCurves(list(lines), list(arcs), list(circles),
                                                 list(ellipses), list(splines), list(cv_splines),
                                                 list(fixed_splines)))
        self.sketchPoints = _NamedCollection(list(points))
        self.sketchDimensions = _NamedCollection(list(dimensions))
        self.geometricConstraints = FakeConstraints()
        self.isFullyConstrained = False
        # autoConstrain: the input the tool sets resultOption on, and the result it reads back.
        self.autoConstrainInput = None
        self.autoConstrainResult = None
        self.autoConstrainCalls = []

    def createAutoConstrainInput(self):
        self.autoConstrainInput = types.SimpleNamespace(resultOption=None, datumPoint=None)
        return self.autoConstrainInput

    def autoConstrain(self, aci):
        self.autoConstrainCalls.append(aci)
        return self.autoConstrainResult


def _component(sketches, name="Root"):
    """A component holding `sketches` plus the origin plane a PlaneRef('xy') resolves to - the
    'surface' operand's simplest form."""
    return MakeComp(name=name, sketches=list(sketches),
                    origin_planes=(FakePlanarEntity("XY"), FakePlanarEntity("XZ"),
                                   FakePlanarEntity("YZ")))


@pytest.fixture
def install(monkeypatch):
    """Wire a sketch into the tool's design seams for one test."""
    def _do(sketch):
        design = install_design(sc, make_design(comp=_component([sketch])))
        # ValueInput passes the raw number/string through here, so the fake pattern/offset inputs
        # read the counts and distances the handler actually asked for.
        monkeypatch.setattr(adsk.core.ValueInput, "createByReal", lambda v: v)
        monkeypatch.setattr(adsk.core.ValueInput, "createByString", lambda v: v)
        return design
    return _do


def _payload(result):
    assert result["isError"] is False, result
    return json.loads(result["content"][0]["text"])


def _constrain(sketch_name="S", component="", units="mm", **entry):
    """One constraint through the LIST wire: handler(constraints=[entry], sketch_name=...)."""
    return sc.handler(constraints=[entry], sketch_name=sketch_name, component=component,
                      units=units)


def _one_row(result):
    """The one landed entry's result row - where a single-entry call's per-constraint keys live."""
    out = _payload(result)
    assert out["constrained"] == 1, out
    return out["results"][0]


class _CurvedFace:
    """A face whose surfaceType is NOT a plane - what PlaneRef refuses and a face handle accepts."""
    def __init__(self):
        self.kind = "curved_face"
        self.geometry = type("G", (), {"surfaceType": adsk.core.SurfaceTypes.CylinderSurfaceType})()


_CURVED_FACE = _CurvedFace()


def _wire_curved_face(monkeypatch, design, handle="CYL"):
    """Make `handle` resolve to the curved face through the design's token lookup. BRepFace is a
    Mock in the harness, so the isinstance checks in _inputs need a real class to test against."""
    monkeypatch.setattr(adsk.fusion, "BRepFace", _CurvedFace)
    monkeypatch.setattr(design, "findEntityByToken",
                        lambda h, _f=_CURVED_FACE: [_f] if h == handle else [], raising=False)
    monkeypatch.setattr(sc._inputs._common, "design", lambda: design)


def _two_line_sketch():
    return FakeSketch("S", lines=[FakeCurve("L0", "line"), FakeCurve("L1", "line")],
                      arcs=[FakeCurve("A0", "arc")], circles=[FakeCurve("C0", "circle")],
                      points=[FakeCurve("P0", "point"), FakeCurve("P1", "point"),
                              FakeCurve("P2", "point")])


def _full_sketch(circles=(), ellipses=(FakeCurve("E0", "ellipse"),)):
    """A sketch also holding an ellipse and one of each spline kind, for resolver round-trip
    coverage over the full ENTITY_REF_KINDS set."""
    return FakeSketch("S", lines=[FakeCurve("L0", "line")], circles=list(circles),
                      ellipses=list(ellipses),
                      splines=[FakeCurve("SP0", "spline"), FakeCurve("SP1", "spline")],
                      cv_splines=[FakeCurve("CV0", "cv_spline")],
                      fixed_splines=[FakeCurve("FX0", "fixed_spline")])


# ── entity resolver ──────────────────────────────────────────────────────────

class TestResolveEntity:
    def test_line_index(self):
        s = _two_line_sketch()
        assert sc._common.resolve_entity_ref(s, "line:1").name == "L1"

    def test_arc_circle_point(self):
        s = _two_line_sketch()
        assert sc._common.resolve_entity_ref(s, "arc:0").name == "A0"
        assert sc._common.resolve_entity_ref(s, "circle:0").name == "C0"
        assert sc._common.resolve_entity_ref(s, "point:2").name == "P2"

    def test_bad_type(self):
        # a token that is not one of _common.ENTITY_REF_KINDS at all
        s = _two_line_sketch()
        assert sc._common.resolve_entity_ref(s, "helix:0") is None

    def test_recognized_kind_missing_from_this_sketch_still_misses_cleanly(self):
        # 'ellipse'/'spline' ARE valid ENTITY_REF_KINDS; a sketch holding none of either resolves
        # to None rather than raising.
        s = _two_line_sketch()
        assert sc._common.resolve_entity_ref(s, "ellipse:0") is None
        assert sc._common.resolve_entity_ref(s, "spline:0") is None

    def test_out_of_range(self):
        s = _two_line_sketch()
        assert sc._common.resolve_entity_ref(s, "line:9") is None

    def test_malformed(self):
        s = _two_line_sketch()
        assert sc._common.resolve_entity_ref(s, "line") is None

    def test_noninteger_index(self):
        s = _two_line_sketch()
        assert sc._common.resolve_entity_ref(s, "line:abc") is None

    def test_negative_index(self):
        s = _two_line_sketch()
        assert sc._common.resolve_entity_ref(s, "line:-1") is None

    def test_empty_ref(self):
        s = _two_line_sketch()
        assert sc._common.resolve_entity_ref(s, "") is None


class TestResolveNewKinds:
    """Ellipse and the three spline collections are addressable ENTITY_REF_KINDS alongside
    ENTITY_REF_KINDS - a spline sketch_add_geometry(kind='spline') just created must be reachable by
    sketch_constrain/sketch_dimension/sketch_delete_entity, not just by sketch_get."""

    def test_ellipse_index(self):
        s = _full_sketch()
        assert sc._common.resolve_entity_ref(s, "ellipse:0").name == "E0"

    def test_fitted_spline_index(self):
        s = _full_sketch()
        assert sc._common.resolve_entity_ref(s, "spline:0").name == "SP0"
        assert sc._common.resolve_entity_ref(s, "spline:1").name == "SP1"

    def test_control_point_spline_index(self):
        s = _full_sketch()
        assert sc._common.resolve_entity_ref(s, "cv_spline:0").name == "CV0"

    def test_fixed_spline_index(self):
        s = _full_sketch()
        assert sc._common.resolve_entity_ref(s, "fixed_spline:0").name == "FX0"

    def test_each_spline_collection_has_its_own_index_space(self):
        # a fitted spline at index 0 and a control-point spline at index 0 are DIFFERENT entities -
        # the three spline collections don't share one index space.
        s = _full_sketch()
        fitted = sc._common.resolve_entity_ref(s, "spline:0")
        cv = sc._common.resolve_entity_ref(s, "cv_spline:0")
        assert fitted.name != cv.name

    def test_new_kind_out_of_range(self):
        s = _full_sketch()
        assert sc._common.resolve_entity_ref(s, "cv_spline:9") is None

    def test_entity_collection_helper_matches_resolver(self):
        # _common.entity_collection is the same collection resolve_entity_ref indexes - a caller
        # needing a before/after count (sketch_delete_entity) must see the identical collection.
        s = _full_sketch()
        assert sc._common.entity_collection(s, "spline") is s.sketchCurves.sketchFittedSplines
        assert sc._common.entity_collection(s, "cv_spline") is s.sketchCurves.sketchControlPointSplines
        assert sc._common.entity_collection(s, "fixed_spline") is s.sketchCurves.sketchFixedSplines
        assert sc._common.entity_collection(s, "ellipse") is s.sketchCurves.sketchEllipses


# ── dispatch: two-curve constraints ─────────────────────────────────────────

class TestTwoCurve:
    def test_perpendicular(self, install):
        s = _two_line_sketch(); install(s)
        out = _one_row(_constrain(constraint="perpendicular", sketch_name="S",
                                  entity_one="line:0", entity_two="line:1"))
        assert s.geometricConstraints.calls[0][0] == "perpendicular"
        assert out["applied"] == "perpendicular"

    def test_parallel_equal_tangent_concentric_collinear(self, install):
        # tangent needs curves (line+circle is the canonical pair).
        cases = (("parallel", "line:0", "line:1"), ("equal", "line:0", "line:1"),
                 ("tangent", "line:0", "circle:0"), ("concentric", "circle:0", "arc:0"),
                 ("collinear", "line:0", "line:1"))
        for cname, e1, e2 in cases:
            s = _two_line_sketch(); install(s)
            _payload(_constrain(constraint=cname, sketch_name="S",
                                entity_one=e1, entity_two=e2))
            assert s.geometricConstraints.calls[0][0] == cname

    def test_two_curve_needs_entity_two(self, install):
        s = _two_line_sketch(); install(s)
        res = _constrain(constraint="parallel", sketch_name="S", entity_one="line:0")
        assert res["isError"] is True and "entity_two" in res["message"]


# ── point + curve ────────────────────────────────────────────────────────────

class TestPointCurve:
    def test_midpoint(self, install):
        s = _two_line_sketch(); install(s)
        _payload(_constrain(constraint="midpoint", sketch_name="S",
                            entity_one="point:0", entity_two="line:0"))
        name, args = s.geometricConstraints.calls[0]
        assert name == "midpoint"
        assert args[0].name == "P0" and args[1].name == "L0"

    def test_coincident(self, install):
        s = _two_line_sketch(); install(s)
        out = _one_row(_constrain(constraint="coincident", sketch_name="S",
                                  entity_one="point:1", entity_two="line:0"))
        name, args = s.geometricConstraints.calls[0]
        assert name == "coincident"
        assert args[0].name == "P1" and args[1].name == "L0"
        assert out["entity_two"] == "line:0"

    def test_point_curve_needs_entity_two(self, install):
        s = _two_line_sketch(); install(s)
        res = _constrain(constraint="coincident", sketch_name="S", entity_one="point:0")
        assert res["isError"] is True and "entity_two" in res["message"]

    def test_coincident_onto_a_curve_says_the_point_lands_on_it(self, install):
        # the API takes (point, circle) happily and puts the point on the RIM; a caller who meant
        # "centre it" gets a clean ok and wrong geometry, so the success note has to say so - and
        # name the ref form that expresses the other intent
        s = _two_line_sketch(); install(s)
        out = _one_row(_constrain(constraint="coincident", sketch_name="S",
                                  entity_one="point:1", entity_two="circle:0"))
        assert "ON that curve" in out["note"]
        assert "'circle:0:center'" in out["note"]

    def test_the_remedy_the_note_offers_matches_the_operand_kind(self, install):
        # a LINE has no centre - offering ':center' for one sends the caller at a refusal
        s = _two_line_sketch(); install(s)
        out = _one_row(_constrain(constraint="coincident", sketch_name="S",
                                  entity_one="point:1", entity_two="line:0"))
        assert "'line:0:start'" in out["note"] and "':end'" in out["note"]
        assert ":center" not in out["note"]

    def test_coincident_between_two_points_carries_no_curve_warning(self, install):
        s = _two_line_sketch(); install(s)
        out = _one_row(_constrain(constraint="coincident", sketch_name="S",
                                  entity_one="point:0", entity_two="point:1"))
        assert "ON that curve" not in out["note"]


# ── entity-anchored refs (':start/:end/:mid/:center', the sketch_dimension grammar) ──────────

class _AnchoredCircle(FakeCurve):
    """A circle carrying its own centre SketchPoint - what 'circle:0:center' resolves to."""
    def __init__(self, name, center):
        super().__init__(name, "circle")
        self.centerSketchPoint = center


class _AnchoredLine(FakeCurve):
    def __init__(self, name, start, end):
        super().__init__(name, "line")
        self.startSketchPoint = start
        self.endSketchPoint = end


class _AddablePoints(_NamedCollection):
    """sketchPoints as the 'mid' anchor uses it - that anchor CREATES a point in the sketch, so what
    it added (and whether a refused call added anything) is readable here."""
    def __init__(self, items):
        super().__init__(items)
        self.added = []

    def add(self, geometry):
        self.added.append(geometry)
        pt = FakeCurve(f"MID{len(self.added)}", "point")
        self._items.append(pt)
        return pt


def _endpoint(name, x, y):
    pt = FakeCurve(name, "point")
    pt.geometry = types.SimpleNamespace(x=x, y=y, z=0.0)
    return pt


def _anchored_sketch():
    """A sketch whose curves carry the endpoint/centre SketchPoints an anchored ref names, and whose
    point collection records what a 'mid' anchor creates."""
    s = FakeSketch(
        "S",
        lines=[_AnchoredLine("L0", _endpoint("L0S", 0.0, 0.0), _endpoint("L0E", 4.0, 0.0)),
               FakeCurve("L1", "line")],
        circles=[_AnchoredCircle("C0", FakeCurve("C0C", "point"))],
        points=[FakeCurve("P0", "point"), FakeCurve("P1", "point")])
    s.sketchPoints = _AddablePoints([FakeCurve("P0", "point"), FakeCurve("P1", "point")])
    return s


class TestEntityAnchors:
    """sketch_constrain reads the SAME ':start/:end/:mid/:center' anchor forms sketch_dimension
    does (_common.parse_anchor_ref / anchor_point): a point slot handed 'circle:0:center' gets that
    circle's own centre point, so centring a circle takes no hunt for the right 'point:N'."""

    def test_a_centre_anchored_circle_reaches_coincident_as_the_centre_point(self, install):
        s = _anchored_sketch(); install(s)
        _payload(_constrain(constraint="coincident", sketch_name="S",
                            entity_one="point:0", entity_two="circle:0:center"))
        name, args = s.geometricConstraints.calls[0]
        assert name == "coincident"
        assert args[1].name == "C0C"          # the CENTRE point, not the circle

    def test_an_anchored_entity_two_carries_no_on_the_curve_note(self, install):
        # the anchored ref already resolved to a point, so the on-the-rim trap does not apply
        s = _anchored_sketch(); install(s)
        out = _one_row(_constrain(constraint="coincident", sketch_name="S",
                                  entity_one="point:0", entity_two="circle:0:center"))
        assert "ON that curve" not in out["note"]
        assert out["entity_two"] == "circle:0:center"      # echoed as given, anchor included

    def test_a_line_endpoint_anchor_reaches_the_api(self, install):
        s = _anchored_sketch(); install(s)
        _payload(_constrain(constraint="coincident", sketch_name="S",
                            entity_one="line:0:end", entity_two="point:1"))
        _name, args = s.geometricConstraints.calls[0]
        assert args[0].name == "L0E"

    def test_a_coincident_between_two_refs_that_are_ONE_point_is_refused_before_the_write(self, install):
        # A closed_path welds its seam, so the two ends meeting there are a SINGLE SketchPoint; the
        # solver answers a coincident onto itself with VCS_SKETCH_SOLVING_FAILED, which names
        # nothing the caller can act on.
        seam = _endpoint("SEAM", 4.0, 0.0)
        s = FakeSketch("S",
                       lines=[_AnchoredLine("L0", _endpoint("L0S", 0.0, 0.0), seam),
                              _AnchoredLine("L1", seam, _endpoint("L1E", 4.0, 4.0))],
                       points=[FakeCurve("P0", "point")])
        install(s)
        res = _constrain(constraint="coincident", sketch_name="S",
                         entity_one="line:0:end", entity_two="line:1:start")
        assert res["isError"] is True
        assert "'line:0:end' and 'line:1:start'" in res["message"]
        assert "ONE sketch point" in res["message"]
        assert s.geometricConstraints.calls == []       # nothing was written

    def test_two_DISTINCT_wrappers_of_one_point_are_refused_too(self, install):
        # The live shape: every read mints a fresh wrapper, so the seam's two ends come back as two
        # objects `is` never matches - one entityToken is what says they are one point.
        left, right = _endpoint("SEAM", 4.0, 0.0), _endpoint("SEAM", 4.0, 0.0)
        left.entityToken = right.entityToken = "TOK-SEAM"
        s = FakeSketch("S",
                       lines=[_AnchoredLine("L0", _endpoint("L0S", 0.0, 0.0), left),
                              _AnchoredLine("L1", right, _endpoint("L1E", 4.0, 4.0))],
                       points=[FakeCurve("P0", "point")])
        install(s)
        res = _constrain(constraint="coincident", sketch_name="S",
                         entity_one="line:0:end", entity_two="line:1:start")
        assert left is not right                        # two objects, one point
        assert res["isError"] is True and "ONE sketch point" in res["message"]
        assert s.geometricConstraints.calls == []

    def test_two_CURVES_sharing_one_token_are_not_refused_as_one_point(self, install):
        # MEASURED in this repo: two distinct sketch CURVES (a split's pieces) share one
        # entityToken, so an identity check over curve operands would refuse a legitimate pair
        # with a message about points. The guard reads the ref FORM, so a curve pair never meets it.
        a, b = FakeCurve("L0", "line"), FakeCurve("L1", "line")
        a.entityToken = b.entityToken = "TOK-SPLIT"
        s = FakeSketch("S", lines=[a, b], points=[FakeCurve("P0", "point")])
        install(s)
        res = _constrain(constraint="coincident", sketch_name="S",
                         entity_one="line:0", entity_two="line:1")
        assert "ONE sketch point" not in (res.get("message") or "")

    def test_both_point_slots_of_horizontal_points_take_anchors(self, install):
        s = _anchored_sketch(); install(s)
        _payload(_constrain(constraint="horizontal_points", sketch_name="S",
                            entity_one="line:0:start", entity_two="circle:0:center"))
        name, args = s.geometricConstraints.calls[0]
        assert name == "horizontal_points"
        assert [a.name for a in args] == ["L0S", "C0C"]

    def test_a_circular_pattern_centres_on_an_anchored_point(self, install):
        s = _anchored_sketch(); install(s)
        _payload(_constrain(constraint="circular_pattern", sketch_name="S",
                            entities="line:1", entity_one="circle:0:center", quantity=3))
        _name, args = s.geometricConstraints.calls[0]
        assert args[0].centerPoint.name == "C0C"

    def test_an_anchor_on_a_whole_entity_constraint_is_refused_naming_the_ones_that_take_it(self, install):
        # a point where addParallel wants a SketchLine raises live; refuse it before the call
        s = _anchored_sketch(); install(s)
        res = _constrain(constraint="parallel", sketch_name="S",
                         entity_one="line:0:end", entity_two="line:1")
        assert res["isError"] is True
        assert "':end'" in res["message"] and "coincident" in res["message"]
        assert s.geometricConstraints.calls == []

    def test_an_anchor_on_the_curve_slot_of_midpoint_is_refused(self, install):
        # midpoint's entity_two is the CURVE the point rides - a point there is the wrong operand
        s = _anchored_sketch(); install(s)
        res = _constrain(constraint="midpoint", sketch_name="S",
                         entity_one="point:0", entity_two="line:0:end")
        assert res["isError"] is True and "entity_two" in res["message"]

    def test_an_unknown_anchor_is_refused_naming_the_valid_ones(self, install):
        s = _anchored_sketch(); install(s)
        res = _constrain(constraint="coincident", sketch_name="S",
                         entity_one="point:0", entity_two="circle:0:middle")
        assert res["isError"] is True and "unknown anchor" in res["message"]

    def test_a_centre_anchor_on_a_line_is_refused_naming_what_it_needs(self, install):
        s = _anchored_sketch(); install(s)
        res = _constrain(constraint="coincident", sketch_name="S",
                         entity_one="point:0", entity_two="line:0:center")
        assert res["isError"] is True and "circle or arc" in res["message"]

    def test_a_mid_anchor_creates_the_welded_point_and_passes_it(self, install, monkeypatch):
        monkeypatch.setattr(adsk.core.Point3D, "create", lambda x, y, z: ("pt", x, y, z))
        s = _anchored_sketch(); install(s)
        _payload(_constrain(constraint="coincident", sketch_name="S",
                            entity_one="line:0:mid", entity_two="point:1"))
        assert s.sketchPoints.added == [("pt", 2.0, 0.0, 0.0)]
        name, args = s.geometricConstraints.calls[-1]
        assert name == "coincident" and args[0].name == "MID1"

    def test_a_refusal_after_a_mid_anchor_would_orphan_it_so_nothing_is_built_one_row(self, install, monkeypatch):
        # the 'mid' anchor CREATES a point + midpoint constraint; entity_two resolves BEFORE it, so
        # an unresolvable entity_two leaves the sketch exactly as it was
        monkeypatch.setattr(adsk.core.Point3D, "create", lambda x, y, z: ("pt", x, y, z))
        s = _anchored_sketch(); install(s)
        res = _constrain(constraint="coincident", sketch_name="S",
                         entity_one="line:0:mid", entity_two="point:99")
        assert res["isError"] is True and "entity_two" in res["message"]
        assert s.sketchPoints.added == []                    # no orphan point
        assert s.geometricConstraints.calls == []            # and no orphan constraint

    def test_the_anchor_forms_are_on_the_wire(self):
        # the anchor form is only reachable if the schema an agent reads names it
        props = sc.tool.to_dict()["inputSchema"]["properties"]["constraints"]["items"]["properties"]
        assert "'circle:0:center'" in props["entity_one"]["description"]

    def test_an_upper_case_ref_keeps_its_anchor(self, install):
        # entity refs resolve case-insensitively; an anchor that did not would make 'CIRCLE:0:CENTER'
        # a silent miss on a form the tool accepts in lower case
        s = _anchored_sketch(); install(s)
        _payload(_constrain(constraint="coincident", sketch_name="S",
                            entity_one="point:0", entity_two="CIRCLE:0:CENTER"))
        _name, args = s.geometricConstraints.calls[0]
        assert args[1].name == "C0C"


# ── the 'component' SCOPE ────────────────────────────────────────────────────
# Fusion numbers sketches per component from 1, so two components each holding a "Sketch1" is the
# norm rather than a corner. The design-wide walk is right to REFUSE that name, and the refusal
# names 'component' as the way through: a component that arrived inside a referenced document is
# not renameable from here, so a rename is no remedy. These drive the REAL walk and scope filter.

@pytest.fixture
def install_multi():
    """Several named components, each with its OWN sketches collection - the shape the scope filter
    is asked about. `allComponents` is a DESIGN property, which is what all_components walks."""
    def _do(pairs):
        comps = [_component(s, name=n) for n, s in pairs]
        return install_design(sc, make_design(comp=comps[0], all_components=comps))
    return _do


@pytest.fixture
def shared_name(install_multi):
    """One name, TWO components. The two sketches carry different geometry (Alpha two lines, Beta
    one line and one circle) so which one a call reached is readable from the constraint that
    landed - a fixture with two DIFFERENT names would never run the filter at all."""
    alpha = FakeSketch("Sketch1", lines=[FakeCurve("A0", "line"), FakeCurve("A1", "line")])
    beta = FakeSketch("Sketch1", lines=[FakeCurve("B0", "line")],
                      circles=[FakeCurve("BC", "circle")])
    install_multi([("Alpha", [alpha]), ("Beta", [beta])])
    return alpha, beta


class TestComponentScope:
    def test_the_unscoped_shared_name_refuses_and_names_the_scope_input(self, shared_name):
        alpha, beta = shared_name
        res = _constrain(constraint="horizontal", sketch_name="Sketch1", entity_one="line:0")
        assert res["isError"] is True
        assert "2 sketches are named 'Sketch1'" in res["message"]
        assert "'component'" in res["message"] and "Rename one" not in res["message"]
        assert alpha.geometricConstraints.calls == [] and beta.geometricConstraints.calls == []

    def test_the_scope_constrains_THAT_components_sketch(self, shared_name):
        alpha, beta = shared_name
        _payload(_constrain(constraint="horizontal", sketch_name="Sketch1", component="Beta",
                            entity_one="line:0"))
        assert beta.geometricConstraints.calls[0][1][0] is beta.sketchCurves.sketchLines.item(0)
        assert alpha.geometricConstraints.calls == []

    def test_the_sibling_component_is_reachable_by_the_same_call(self, shared_name):
        alpha, beta = shared_name
        _payload(_constrain(constraint="horizontal", sketch_name="Sketch1", component="Alpha",
                            entity_one="line:0"))
        assert alpha.geometricConstraints.calls[0][1][0] is alpha.sketchCurves.sketchLines.item(0)
        assert beta.geometricConstraints.calls == []

    def test_an_unknown_component_is_refused(self, shared_name):
        alpha, beta = shared_name
        res = _constrain(constraint="horizontal", sketch_name="Sketch1", component="Gamma",
                         entity_one="line:0")
        assert res["isError"] is True and "No component named 'Gamma'" in res["message"]
        assert alpha.geometricConstraints.calls == [] and beta.geometricConstraints.calls == []

    def test_a_wrong_component_is_refused_even_when_the_name_is_UNIQUE(self, install_multi):
        # A scope that was passed is VALIDATED, never dropped because the walk would have resolved
        # anyway: otherwise the constraint lands in Alpha while the call named Beta.
        alpha = FakeSketch("OnlyOne", lines=[FakeCurve("A0", "line")])
        install_multi([("Alpha", [alpha]), ("Beta", [])])
        res = _constrain(constraint="horizontal", sketch_name="OnlyOne", component="Beta",
                         entity_one="line:0")
        assert res["isError"] is True and "'Beta'" in res["message"]
        assert alpha.geometricConstraints.calls == []


# ── single line ──────────────────────────────────────────────────────────────

class TestSingleLine:
    def test_horizontal(self, install):
        s = _two_line_sketch(); install(s)
        _payload(_constrain(constraint="horizontal", sketch_name="S", entity_one="line:0"))
        assert s.geometricConstraints.calls[0][0] == "horizontal"

    def test_vertical(self, install):
        s = _two_line_sketch(); install(s)
        out = _one_row(_constrain(constraint="vertical", sketch_name="S", entity_one="line:1"))
        assert s.geometricConstraints.calls[0][0] == "vertical"
        # a one-line constraint reports entity_two / symmetry_line as None
        assert out["entity_two"] is None and out["symmetry_line"] is None

    def test_constraint_returning_nothing_is_error(self, install):
        s = _two_line_sketch(); install(s)
        s.geometricConstraints.addHorizontal = lambda l: None
        res = _constrain(constraint="horizontal", sketch_name="S", entity_one="line:0")
        assert res["isError"] is True and "returned no constraint object" in res["message"]

    def test_fix_sets_isfixed(self, install):
        s = _two_line_sketch(); install(s)
        out = _one_row(_constrain(constraint="fix", sketch_name="S", entity_one="line:1"))
        assert s.sketchCurves.sketchLines.item(1).isFixed is True
        assert out["applied"] == "fix"

    def test_unfix(self, install):
        s = _two_line_sketch(); install(s)
        s.sketchCurves.sketchLines.item(0).isFixed = True
        _payload(_constrain(constraint="unfix", sketch_name="S", entity_one="line:0"))
        assert s.sketchCurves.sketchLines.item(0).isFixed is False

    def test_fix_failure_is_reported_not_a_false_success(self, install):
        s = _two_line_sketch(); install(s)

        class _RejectsFix:
            name = "L1"
            @property
            def isFixed(self):
                return False
            @isFixed.setter
            def isFixed(self, v):
                raise RuntimeError("cannot fix this entity")

        s.sketchCurves.sketchLines._items[1] = _RejectsFix()
        res = _constrain(constraint="fix", sketch_name="S", entity_one="line:1")
        assert res["isError"] is True
        assert "fix" in res["message"].lower()

    def test_a_silently_declined_fix_is_an_error_not_a_false_success(self, install):
        # the platform ACCEPTS the isFixed assignment and leaves the curve free: no exception is
        # raised, so only the read-back can convict. An ok here would report a locked curve that
        # the solver is still free to move.
        s = _two_line_sketch(); install(s)

        class _SwallowsFix:
            name = "L1"

            @property
            def isFixed(self):
                return False

            @isFixed.setter
            def isFixed(self, v):
                pass                              # accepted and ignored

        s.sketchCurves.sketchLines._items[1] = _SwallowsFix()
        res = _constrain(constraint="fix", sketch_name="S", entity_one="line:1")
        assert res["isError"] is True
        assert "fix" in res["message"].lower()


# ── 'text:<i>' - the SketchText anchor, fix/unfix only ───────────────────────

class _RectangleLine:
    """One of the four definition rectangle lines carrying a SketchText's anchor DOF."""
    def __init__(self, name, lockable=True):
        self.name = name
        self.kind = "line"
        self._lockable = lockable
        self.isFixed = False

    def __setattr__(self, attr, value):
        # a line the platform declines to lock reports back what it still is, never what was asked
        if attr == "isFixed" and not getattr(self, "_lockable", True):
            return object.__setattr__(self, "isFixed", False)
        object.__setattr__(self, attr, value)


class _LineVector:
    """SketchText.definition.rectangleLines: a SketchLineVector, which iterates PLAINLY - it carries
    no .count/.item, so code that walks it that way reads nothing at all."""
    def __init__(self, lines):
        self._lines = list(lines)

    def __iter__(self):
        return iter(self._lines)


class _SketchText:
    def __init__(self, lines):
        self.definition = types.SimpleNamespace(rectangleLines=_LineVector(lines))


class _TextSketch(FakeSketch):
    """A sketch holding one SketchText whose anchor lines decide, between them, whether it reads
    fully constrained - the live relationship this route exists to reach. `always_loose` models the
    ordinary case where the sketch holds other freedom too."""
    def __init__(self, n_lines=4, lockable=(True, True, True, True), always_loose=False):
        super().__init__("S", lines=[FakeCurve("L0", "line")])
        self.anchor_lines = [_RectangleLine(f"R{i}", lockable[i]) for i in range(n_lines)]
        self.sketchTexts = _NamedCollection([_SketchText(self.anchor_lines)])
        self.always_loose = always_loose

    @property
    def isFullyConstrained(self):
        if self.always_loose or not self.anchor_lines:
            return False
        return all(ln.isFixed for ln in self.anchor_lines)

    @isFullyConstrained.setter
    def isFullyConstrained(self, value):
        pass          # FakeSketch seeds a plain False; here the anchor lines are what decide


def _text_sketch(n_lines=4, lockable=(True, True, True, True), always_loose=False):
    return _TextSketch(n_lines=n_lines, lockable=lockable, always_loose=always_loose)


class TestSketchTextAnchor:
    """A SketchText's anchor DOF lives on the four rectangle lines of its definition, which no
    geometric constraint takes as an operand - fixing them is the only route to a text-bearing
    sketch reading fully constrained."""

    def test_fixing_a_text_locks_every_anchor_line_and_reads_the_sketch_back(self, install):
        s = _text_sketch(); install(s)
        out = _one_row(_constrain(constraint="fix", sketch_name="S", entity_one="text:0"))
        assert [ln.isFixed for ln in s.anchor_lines] == [True] * 4
        assert out["anchor_lines_fixed"] == 4
        assert out["is_fully_constrained"] is True
        assert "FULLY CONSTRAINED" in out["note"]

    def test_unfix_releases_them_again(self, install):
        s = _text_sketch(); install(s)
        _payload(_constrain(constraint="fix", sketch_name="S", entity_one="text:0"))
        out = _one_row(_constrain(constraint="unfix", sketch_name="S", entity_one="text:0"))
        assert [ln.isFixed for ln in s.anchor_lines] == [False] * 4
        assert out["is_fully_constrained"] is False
        assert "RELEASED" in out["note"]

    def test_a_lock_that_leaves_the_sketch_loose_says_so(self, install):
        # the anchor is one DOF among however many the sketch holds - a text lock is not a promise
        # that the sketch is now constrained, and the payload must not imply it
        s = _text_sketch(always_loose=True); install(s)
        out = _one_row(_constrain(constraint="fix", sketch_name="S", entity_one="text:0"))
        assert [ln.isFixed for ln in s.anchor_lines] == [True] * 4    # the lock still landed
        assert out["is_fully_constrained"] is False
        assert "still NOT fully constrained" in out["note"]

    def test_a_line_that_declines_the_lock_is_reported_not_papered_over(self, install):
        # 3 of 4 taking is a partly-locked anchor - a clean ok here would be a false success
        s = _text_sketch(lockable=(True, True, True, False)); install(s)
        res = _constrain(constraint="fix", sketch_name="S", entity_one="text:0")
        assert res["isError"] is True
        assert "3 of 4" in res["message"]

    def test_a_definition_handing_back_no_lines_is_refused(self, install):
        s = _text_sketch(n_lines=0, lockable=()); install(s)
        res = _constrain(constraint="fix", sketch_name="S", entity_one="text:0")
        assert res["isError"] is True and "no rectangle lines" in res["message"]

    def test_a_text_ref_on_any_other_constraint_names_the_two_that_take_it(self, install):
        s = _text_sketch(); install(s)
        res = _constrain(constraint="horizontal", sketch_name="S", entity_one="text:0")
        assert res["isError"] is True
        assert "fix / unfix" in res["message"] and "horizontal" in res["message"]

    def test_an_out_of_range_text_index_names_what_the_sketch_holds(self, install):
        s = _text_sketch(); install(s)
        res = _constrain(constraint="fix", sketch_name="S", entity_one="text:3")
        assert res["isError"] is True and "text:0..text:0" in res["message"]

    def test_a_sketch_qualified_text_ref_is_refused(self, install):
        # this tool constrains the ONE sketch 'sketch_name' names; honoring half an address silently
        # would fix a text in a sketch the caller never named here
        s = _text_sketch(); install(s)
        res = _constrain(constraint="fix", sketch_name="S", entity_one="Other/text:0")
        assert res["isError"] is True and "sketch_name" in res["message"]


# ── symmetry (3 entities) ────────────────────────────────────────────────────

class TestSymmetry:
    def test_symmetry_uses_symmetry_line(self, install):
        s = _two_line_sketch(); install(s)
        _payload(_constrain(constraint="symmetry", sketch_name="S",
                            entity_one="line:0", entity_two="line:1", symmetry_line="line:0"))
        name, args = s.geometricConstraints.calls[0]
        assert name == "symmetry" and len(args) == 3

    def test_symmetry_needs_symmetry_line(self, install):
        s = _two_line_sketch(); install(s)
        res = _constrain(constraint="symmetry", sketch_name="S",
                         entity_one="line:0", entity_two="line:1")
        assert res["isError"] is True and "symmetry_line" in res["message"]


# ── wrong-kind refusals (the live API raises; the tool must return a clean error) ──

class TestWrongKindRefusals:
    def test_horizontal_on_a_circle_is_a_clean_error(self, install):
        s = _two_line_sketch(); install(s)
        res = _constrain(constraint="horizontal", sketch_name="S", entity_one="circle:0")
        assert res["isError"] is True
        assert "horizontal" in res["message"] and "circle" in res["message"]
        assert s.geometricConstraints.calls == []          # nothing was applied

    def test_tangent_with_a_point_is_a_clean_error(self, install):
        s = _two_line_sketch(); install(s)
        res = _constrain(constraint="tangent", sketch_name="S",
                         entity_one="point:0", entity_two="circle:0")
        assert res["isError"] is True
        assert "tangent" in res["message"]
        assert s.geometricConstraints.calls == []

    def test_concentric_with_a_line_is_a_clean_error(self, install):
        s = _two_line_sketch(); install(s)
        res = _constrain(constraint="concentric", sketch_name="S",
                         entity_one="line:0", entity_two="circle:0")
        assert res["isError"] is True
        assert "concentric" in res["message"] and "line" in res["message"]
        assert s.geometricConstraints.calls == []

    def test_vertical_on_an_arc_is_a_clean_error(self, install):
        s = _two_line_sketch(); install(s)
        res = _constrain(constraint="vertical", sketch_name="S", entity_one="arc:0")
        assert res["isError"] is True
        assert "vertical" in res["message"] and "arc" in res["message"]
        assert s.geometricConstraints.calls == []

    def test_coincident_with_a_curve_first_is_a_clean_error(self, install):
        # coincident/midpoint take a POINT as entity_one; a curve there raises live
        s = _two_line_sketch(); install(s)
        res = _constrain(constraint="coincident", sketch_name="S",
                         entity_one="circle:0", entity_two="line:0")
        assert res["isError"] is True
        assert "coincident" in res["message"] and "circle" in res["message"]
        assert s.geometricConstraints.calls == []

    def test_midpoint_with_a_line_first_is_a_clean_error(self, install):
        s = _two_line_sketch(); install(s)
        res = _constrain(constraint="midpoint", sketch_name="S",
                         entity_one="line:0", entity_two="line:1")
        assert res["isError"] is True
        assert "midpoint" in res["message"] and "line" in res["message"]
        assert s.geometricConstraints.calls == []

    def test_the_raw_api_reason_leads_and_the_operand_rule_follows(self, install):
        s = _two_line_sketch(); install(s)
        r_tan = _constrain(constraint="tangent", sketch_name="S",
                           entity_one="point:0", entity_two="circle:0")
        msg = r_tan["message"]
        assert msg.index("invalid argument") < msg.index("two curves")
        r_con = _constrain(constraint="concentric", sketch_name="S",
                           entity_one="line:0", entity_two="circle:0")
        assert "center point" in r_con["message"]
        r_hor = _constrain(constraint="horizontal", sketch_name="S", entity_one="circle:0")
        assert "one line" in r_hor["message"]


# ── the handler accepts ellipse/spline refs end-to-end (no per-tool kind list to update) ────────

class TestHandlerAcceptsNewKinds:
    def test_a_spline_ref_reaches_the_api_unchanged(self, install):
        s = _full_sketch(); install(s)
        out = _one_row(_constrain(constraint="tangent", sketch_name="S",
                                  entity_one="spline:0", entity_two="line:0"))
        name, args = s.geometricConstraints.calls[0]
        assert name == "tangent"
        assert args[0].name == "SP0" and args[1].name == "L0"
        assert out["applied"] == "tangent"

    def test_an_ellipse_ref_reaches_the_api_unchanged(self, install):
        s = _full_sketch(circles=[FakeCurve("C0", "circle")]); install(s)
        out = _one_row(_constrain(constraint="concentric", sketch_name="S",
                                  entity_one="ellipse:0", entity_two="circle:0"))
        name, args = s.geometricConstraints.calls[0]
        assert name == "concentric"
        assert args[0].name == "E0" and args[1].name == "C0"
        assert out["applied"] == "concentric"

    def test_control_point_and_fixed_spline_refs_are_fixable(self, install):
        s = _full_sketch(); install(s)
        for ref in ("cv_spline:0", "fixed_spline:0"):
            _payload(_constrain(constraint="fix", sketch_name="S", entity_one=ref))
        assert [c.name for c in (s.sketchCurves.sketchControlPointSplines.item(0),
                                 s.sketchCurves.sketchFixedSplines.item(0))] == ["CV0", "FX0"]
        assert s.sketchCurves.sketchControlPointSplines.item(0).isFixed is True
        assert s.sketchCurves.sketchFixedSplines.item(0).isFixed is True

    def test_unresolvable_new_kind_ref_is_a_clean_error(self, install):
        s = _full_sketch(); install(s)
        res = _constrain(constraint="equal", sketch_name="S",
                         entity_one="fixed_spline:9", entity_two="line:0")
        assert res["isError"] is True and "fixed_spline:9" in res["message"]


# ── guards ───────────────────────────────────────────────────────────────────

class TestGuards:
    def test_unknown_constraint(self, install):
        s = _two_line_sketch(); install(s)
        res = _constrain(constraint="weld", sketch_name="S", entity_one="line:0")
        assert res["isError"] is True and "Unknown constraint" in res["message"]

    def test_missing_sketch(self, install):
        install(_two_line_sketch())
        res = _constrain(constraint="horizontal", sketch_name="Nope", entity_one="line:0")
        assert res["isError"] is True and "Nope" in res["message"]

    def test_a_sketch_name_two_components_share_is_refused_naming_both(self, install):
        # Constraining an arbitrary one of two same-named sketches edits geometry in a component the
        # caller never named; the refusal names both owners so one can be renamed.
        mine, theirs = _two_line_sketch(), _two_line_sketch()
        design = install(mine)
        design.rootComponent.name = "Root"
        other = _component([theirs], name="Frame")
        design._all_components = [design.rootComponent, other]
        res = _constrain(constraint="horizontal", sketch_name="S", entity_one="line:0")
        assert res["isError"] is True
        assert "2 sketches" in res["message"] and "Root" in res["message"] and "Frame" in res["message"]
        assert mine.geometricConstraints.calls == [] and theirs.geometricConstraints.calls == []

    def test_unresolvable_entity(self, install):
        s = _two_line_sketch(); install(s)
        res = _constrain(constraint="horizontal", sketch_name="S", entity_one="line:9")
        assert res["isError"] is True and "line:9" in res["message"]


class TestOperandRulesReachTheWire:
    """Each constraint's operand rule is readable off its own error message."""

    def test_perpendicular_names_two_lines(self, install):
        s = _full_sketch(); install(s)
        res = _constrain(constraint="perpendicular", sketch_name="S",
                         entity_one="line:0", entity_two="spline:0")
        assert "'perpendicular' takes two lines." in res["message"]

    def test_parallel_names_two_lines(self, install):
        s = _full_sketch(); install(s)
        res = _constrain(constraint="parallel", sketch_name="S",
                         entity_one="line:0", entity_two="ellipse:0")
        assert "'parallel' takes two lines." in res["message"]

    def test_equal_names_only_lines_arcs_and_circles(self, install):
        s = _full_sketch(); install(s)
        res = _constrain(constraint="equal", sketch_name="S",
                         entity_one="spline:0", entity_two="spline:1")
        assert "'equal' takes two lines, two arcs, or two circles." in res["message"]

    def test_equal_refuses_two_ellipses_despite_matching_kinds(self, install):
        s = _full_sketch(ellipses=[FakeCurve("E0", "ellipse"), FakeCurve("E1", "ellipse")])
        install(s)
        res = _constrain(constraint="equal", sketch_name="S",
                         entity_one="ellipse:0", entity_two="ellipse:1")
        assert res["isError"] is True
        assert s.geometricConstraints.calls == []

    def test_equal_refuses_two_curves_of_different_kinds(self, install):
        # the matching-kind half of the rule: arc+circle is refused live even though both are
        # legal 'equal' kinds on their own
        s = _two_line_sketch(); install(s)
        res = _constrain(constraint="equal", sketch_name="S",
                         entity_one="arc:0", entity_two="circle:0")
        assert res["isError"] is True
        assert s.geometricConstraints.calls == []

    def test_symmetry_refuses_a_non_line_axis(self, install):
        s = _two_line_sketch(); install(s)
        res = _constrain(constraint="symmetry", sketch_name="S", entity_one="line:0",
                         entity_two="line:1", symmetry_line="circle:0")
        assert res["isError"] is True and "axis line" in res["message"]

    def test_tangent_accepts_an_ellipse(self, install):
        s = _full_sketch(); install(s)
        out = _one_row(_constrain(constraint="tangent", sketch_name="S",
                                  entity_one="ellipse:0", entity_two="line:0"))
        assert out["applied"] == "tangent"


# ── smooth (curvature-continuous; one operand must be a spline) ──────────────

class TestSmooth:
    def test_two_lines_are_refused_with_the_spline_rule(self, install):
        s = _two_line_sketch(); install(s)
        res = _constrain(constraint="smooth", sketch_name="S",
                         entity_one="line:0", entity_two="line:1")
        assert res["isError"] is True
        assert "'smooth' takes two curves, at least one of them a spline" in res["message"]
        assert s.geometricConstraints.calls == []

    def test_the_refusal_names_the_coincidence_the_constraint_needs(self, install):
        # the operand-kind rule alone asserts a precondition that HELD in the measured failure:
        # both were curves and one was a spline, and what was missing was the shared coincident
        # endpoint an earlier smooth had moved apart. The remedy has to name it.
        s = _full_sketch(); install(s)

        def _raise(_a, _b):
            raise RuntimeError("3 : Invalid argument for constraint")
        s.geometricConstraints.addSmooth = _raise
        res = _constrain(constraint="smooth", sketch_name="S",
                         entity_one="spline:0", entity_two="line:0")
        assert res["isError"] is True
        assert "COINCIDENT" in res["message"]
        assert "still coincident" in res["message"]

    def test_a_spline_and_a_line_constrain(self, install):
        s = _full_sketch(); install(s)
        out = _one_row(_constrain(constraint="smooth", sketch_name="S",
                                  entity_one="spline:0", entity_two="line:0"))
        name, args = s.geometricConstraints.calls[0]
        assert name == "smooth"
        assert args[0].name == "SP0" and args[1].name == "L0"
        assert out["applied"] == "smooth"

    def test_a_point_operand_is_refused(self, install):
        s = _two_line_sketch(); install(s)
        res = _constrain(constraint="smooth", sketch_name="S",
                         entity_one="point:0", entity_two="line:0")
        assert res["isError"] is True
        assert s.geometricConstraints.calls == []


# ── point alignment (horizontal_points / vertical_points) ────────────────────

class TestPointAlignment:
    def test_horizontal_points_passes_both_points(self, install):
        s = _two_line_sketch(); install(s)
        out = _one_row(_constrain(constraint="horizontal_points", sketch_name="S",
                                  entity_one="point:0", entity_two="point:2"))
        name, args = s.geometricConstraints.calls[0]
        assert name == "horizontal_points"
        assert args[0].name == "P0" and args[1].name == "P2"
        assert out["applied"] == "horizontal_points"

    def test_vertical_points_passes_both_points(self, install):
        s = _two_line_sketch(); install(s)
        _payload(_constrain(constraint="vertical_points", sketch_name="S",
                            entity_one="point:1", entity_two="point:2"))
        assert s.geometricConstraints.calls[0][0] == "vertical_points"

    def test_a_line_operand_is_refused_with_the_two_points_rule(self, install):
        s = _two_line_sketch(); install(s)
        res = _constrain(constraint="vertical_points", sketch_name="S",
                         entity_one="line:0", entity_two="point:0")
        assert res["isError"] is True
        assert "'vertical_points' takes two points." in res["message"]
        assert s.geometricConstraints.calls == []

    def test_second_point_is_required(self, install):
        s = _two_line_sketch(); install(s)
        res = _constrain(constraint="horizontal_points", sketch_name="S", entity_one="point:0")
        assert res["isError"] is True and "entity_two" in res["message"]


# ── the surface kinds (a sketch entity constrained to a model face/plane) ────

class TestSurfaceConstraints:
    def test_coincident_to_surface_passes_the_resolved_plane(self, install):
        s = _two_line_sketch(); install(s)
        out = _one_row(_constrain(constraint="coincident_to_surface", sketch_name="S",
                                  entity_one="point:0", surface="xy"))
        name, args = s.geometricConstraints.calls[0]
        assert name == "coincident_to_surface"
        assert args[0].name == "P0" and args[1].name == "XY"
        # the payload names the plane the constraint ATTACHED to, not the 'xy' token it was asked with
        assert out["surface"] == "XY"

    def test_line_on_surface_and_parallel_take_a_line(self, install):
        for cname in ("line_on_surface", "line_parallel_to_surface"):
            s = _two_line_sketch(); install(s)
            _payload(_constrain(constraint=cname, sketch_name="S",
                                entity_one="line:0", surface="xy"))
            assert s.geometricConstraints.calls[0][0] == cname

    def test_line_on_surface_refuses_a_circle(self, install):
        s = _two_line_sketch(); install(s)
        res = _constrain(constraint="line_on_surface", sketch_name="S",
                         entity_one="circle:0", surface="xy")
        assert res["isError"] is True
        assert "'line_on_surface' takes a LINE as entity_one plus a planar 'surface'." in res["message"]
        assert s.geometricConstraints.calls == []

    def test_perpendicular_to_surface_accepts_a_spline(self, install):
        s = _full_sketch(); install(s)
        out = _one_row(_constrain(constraint="perpendicular_to_surface", sketch_name="S",
                                  entity_one="cv_spline:0", surface="xy"))
        assert out["applied"] == "perpendicular_to_surface"

    def test_perpendicular_to_surface_refuses_an_ellipse(self, install):
        # the binding supports line and spline curves only
        s = _full_sketch(); install(s)
        res = _constrain(constraint="perpendicular_to_surface", sketch_name="S",
                         entity_one="ellipse:0", surface="xy")
        assert res["isError"] is True
        assert "LINE or SPLINE" in res["message"]
        assert s.geometricConstraints.calls == []

    def test_a_curved_face_reaches_the_two_constraints_that_accept_one(self, install, monkeypatch):
        # addCoincidentToSurface / addPerpendicularToSurface take a `surface: Base` and were both
        # applied to a cylinder live. PlaneRef alone would refuse the handle as non-planar.
        for cname, ref in (("coincident_to_surface", "point:0"),
                           ("perpendicular_to_surface", "line:0")):
            s = _two_line_sketch()
            design = install(s)
            _wire_curved_face(monkeypatch, design)
            out = _one_row(_constrain(constraint=cname, sketch_name="S", entity_one=ref,
                                      surface="CYL"))
            assert out["applied"] == cname
            assert s.geometricConstraints.calls[0][1][1] is _CURVED_FACE
            # a face carries no name, so the payload reports the ENTITY TYPE it attached to
            assert out["surface"] == type(_CURVED_FACE).__name__

    def test_the_surface_schema_names_the_constraints_that_accept_a_curved_face(self):
        # the only string an agent reads about this input must match what resolve() actually does:
        # blanket 'planar-face' prose would falsify the two constraints that take a cylinder
        props = sc.tool.to_dict()["inputSchema"]["properties"]["constraints"]["items"]["properties"]
        desc = props["surface"]["description"]
        assert "coincident_to_surface" in desc and "perpendicular_to_surface" in desc
        assert "curved face too for" in desc and "planar-face" in desc
        assert "line_on_surface" not in desc          # planar-only ops are not listed as curved-OK

    def test_a_curved_face_is_still_refused_by_the_planar_only_constraints(self, install,
                                                                           monkeypatch):
        for cname in ("line_on_surface", "line_parallel_to_surface"):
            s = _two_line_sketch()
            design = install(s)
            _wire_curved_face(monkeypatch, design)
            res = _constrain(constraint=cname, sketch_name="S", entity_one="line:0",
                             surface="CYL")
            assert res["isError"] is True and "not PLANAR" in res["message"]
            assert s.geometricConstraints.calls == []

    @pytest.mark.parametrize("cname,ref,tail", [
        ("coincident_to_surface", "point:0", "(curved faces allowed)."),
        ("line_on_surface", "line:0", "(this constraint takes a PLANAR face only)."),
    ])
    def test_a_missing_surface_names_the_faces_that_constraint_takes(self, install, cname, ref,
                                                                     tail):
        # An omitted 'surface' resolves to nothing with no error of its own (the operand is optional
        # on the kind, required by these constraints), so this refusal is the handler's own - and it
        # is where the caller learns which face kinds THIS constraint accepts.
        s = _two_line_sketch(); install(s)
        res = _constrain(constraint=cname, sketch_name="S", entity_one=ref)
        assert res["isError"] is True
        assert f"'{cname}' needs 'surface'" in res["message"]
        assert tail in res["message"]
        assert s.geometricConstraints.calls == []

    def test_an_unresolvable_surface_is_a_clean_error(self, install):
        s = _two_line_sketch(); install(s)
        res = _constrain(constraint="coincident_to_surface", sketch_name="S",
                         entity_one="point:0", surface="NoSuchPlane")
        assert res["isError"] is True and "NoSuchPlane" in res["message"]
        assert s.geometricConstraints.calls == []


# ── polygon (an existing closed run of lines) ────────────────────────────────

def _polygon_sketch():
    return FakeSketch("S", lines=[FakeCurve(f"L{i}", "line") for i in range(4)],
                      circles=[FakeCurve("C0", "circle")],
                      points=[FakeCurve("P0", "point")])


class TestPolygon:
    def test_every_listed_line_reaches_the_api(self, install):
        s = _polygon_sketch(); install(s)
        out = _one_row(_constrain(constraint="polygon", sketch_name="S",
                                  entities="line:0, line:1, line:2, line:3"))
        name, args = s.geometricConstraints.calls[0]
        assert name == "polygon"
        assert [line.name for line in args[0]] == ["L0", "L1", "L2", "L3"]
        assert out["entities"] == "line:0, line:1, line:2, line:3"

    def test_fewer_than_three_lines_cannot_close_a_shape(self, install):
        s = _polygon_sketch(); install(s)
        res = _constrain(constraint="polygon", sketch_name="S", entities="line:0,line:1")
        assert res["isError"] is True and "at least 3" in res["message"]
        assert s.geometricConstraints.calls == []

    def test_a_non_line_member_is_refused(self, install):
        s = _polygon_sketch(); install(s)
        res = _constrain(constraint="polygon", sketch_name="S",
                         entities="line:0,line:1,circle:0")
        assert res["isError"] is True and "equal lengths" in res["message"]
        assert s.geometricConstraints.calls == []

    def test_an_unresolvable_member_is_named(self, install):
        s = _polygon_sketch(); install(s)
        res = _constrain(constraint="polygon", sketch_name="S",
                         entities="line:0,line:9,line:2")
        assert res["isError"] is True and "line:9" in res["message"]
        assert s.geometricConstraints.calls == []

    def test_entities_is_required(self, install):
        s = _polygon_sketch(); install(s)
        res = _constrain(constraint="polygon", sketch_name="S")
        assert res["isError"] is True and "'entities'" in res["message"]


# ── offset (creates curves) ──────────────────────────────────────────────────

class TestOffset:
    def test_created_curves_are_counted_off_the_constraint(self, install):
        s = _two_line_sketch(); install(s)
        out = _one_row(_constrain(constraint="offset", sketch_name="S",
                                  entities="line:0,line:1", distance=5))
        assert s.geometricConstraints.calls[0][0] == "offset"
        assert out["created_count"] == 2
        assert "sketch_get" in out["note"]

    def test_the_distance_reaches_the_api_as_a_cm_expression(self, install):
        # the length carries its UNIT: this factory family was measured reading a bare ValueInput
        # real in the document's default length unit, which turns 0.5 into 0.5 mm on an mm document
        s = _two_line_sketch(); install(s)
        _payload(_constrain(constraint="offset", sketch_name="S", entities="line:0",
                            distance=5, units="mm"))
        _, args = s.geometricConstraints.calls[0]
        assert args[0].offset == "0.5 cm"

    def test_inch_units_scale_the_distance(self, install):
        s = _two_line_sketch(); install(s)
        _payload(_constrain(constraint="offset", sketch_name="S", entities="line:0",
                            distance=1, units="in"))
        _, args = s.geometricConstraints.calls[0]
        assert args[0].offset == "2.54 cm"

    def test_two_sides_links_the_offsets_and_counts_both(self, install):
        # The two constraints come back in an OffsetConstraintVector, not a list - counting it as a
        # single constraint reads no childCurves at all and the created-nothing gate goes blind.
        s = _two_line_sketch(); install(s)
        out = _one_row(_constrain(constraint="offset_two_sides", sketch_name="S",
                                  entities="line:0,line:1", distance=5))
        name, args = s.geometricConstraints.calls[0]
        assert name == "offset_two_sides" and args[1] is True
        assert out["created_count"] == 4

    def test_two_sides_creating_nothing_is_an_error(self, install):
        # The gate must reach INTO the vector: two constraints that made no curves is a false ok.
        s = _two_line_sketch(); install(s)
        s.geometricConstraints.offset_curves_created = 0
        res = _constrain(constraint="offset_two_sides", sketch_name="S",
                         entities="line:0,line:1", distance=5)
        assert res["isError"] is True and "added no sketch geometry" in res["message"]

    def test_a_missing_distance_is_named(self, install):
        s = _two_line_sketch(); install(s)
        res = _constrain(constraint="offset", sketch_name="S", entities="line:0")
        assert res["isError"] is True and "'distance'" in res["message"]
        assert s.geometricConstraints.calls == []

    def test_missing_entities_is_named_before_anything_is_created(self, install):
        s = _two_line_sketch(); install(s)
        res = _constrain(constraint="offset", sketch_name="S", distance=5)
        assert res["isError"] is True and "'entities'" in res["message"]
        assert s.geometricConstraints.calls == []

    def test_a_zero_distance_is_refused(self, install):
        s = _two_line_sketch(); install(s)
        res = _constrain(constraint="offset", sketch_name="S", entities="line:0", distance=0)
        assert res["isError"] is True and "non-zero" in res["message"]
        assert s.geometricConstraints.calls == []

    def test_a_point_cannot_be_offset(self, install):
        s = _two_line_sketch(); install(s)
        res = _constrain(constraint="offset", sketch_name="S", entities="point:0", distance=5)
        assert res["isError"] is True and "end-connected curves" in res["message"]
        assert s.geometricConstraints.calls == []

    def test_a_constraint_that_created_no_curves_is_an_error(self, install):
        s = _two_line_sketch(); install(s)
        s.geometricConstraints.offset_curves_created = 0
        res = _constrain(constraint="offset", sketch_name="S", entities="line:0", distance=5)
        assert res["isError"] is True and "added no sketch geometry" in res["message"]

    def test_bad_units_are_named(self, install):
        s = _two_line_sketch(); install(s)
        res = _constrain(constraint="offset", sketch_name="S", entities="line:0",
                         distance=5, units="furlong")
        assert res["isError"] is True and "furlong" in res["message"]


# ── sketch patterns (create copies of the listed entities) ───────────────────

class TestSketchPatterns:
    def test_rectangular_sets_both_directions(self, install):
        s = _two_line_sketch(); install(s)
        out = _one_row(_constrain(constraint="rectangular_pattern", sketch_name="S",
                                  entities="circle:0", entity_one="line:0",
                                  entity_two="line:1", quantity=3, distance=10,
                                  quantity_two=2, distance_two=5))
        _, args = s.geometricConstraints.calls[0]
        pin = args[0]
        assert pin.one[1] == 3 and pin.one[2] == "1.0 cm"
        assert pin.two[1] == 2 and pin.two[2] == "0.5 cm"
        assert out["created_count"] == 5

    def test_the_counts_stay_bare_reals_while_the_distances_carry_cm(self, install):
        # MEASURED: setDirectionOne/Two read a bare ValueInput real as a number in the DOCUMENT'S
        # default length unit - createByReal(9.0) laid out 9 mm of spacing on an mm document, a 10x
        # error - so a DISTANCE must carry its unit. A quantity is a count and is unaffected.
        s = _two_line_sketch(); install(s)
        _payload(_constrain(constraint="rectangular_pattern", sketch_name="S",
                            entities="circle:0", entity_one="line:0", entity_two="line:1",
                            quantity=3, distance=90, quantity_two=2, distance_two=90))
        pin = s.geometricConstraints.calls[0][1][0]
        assert pin.one[2] == "9.0 cm" and pin.two[2] == "9.0 cm"
        assert pin.one[1] == 3 and pin.two[1] == 2

    def _rect_with(self, s, cls):
        """Drive a rectangular pattern whose input is `cls` - the seam a refusing setter arrives on."""
        s.geometricConstraints.createRectangularPatternInput = cls
        return _constrain(constraint="rectangular_pattern", sketch_name="S", entities="circle:0",
                          entity_one="line:0", entity_two="line:1", quantity=3, distance=10,
                          quantity_two=2, distance_two=5)

    def test_a_refused_first_direction_is_an_honest_error(self, install):
        # setDirectionOne/Two are declared bool. A False that is not read runs straight on to
        # addRectangularPattern, which builds the pattern off a direction that never landed.
        s = _two_line_sketch(); install(s)

        class _RefusesOne(FakeRectPatternInput):
            def setDirectionOne(self, entity, quantity, distance):
                return False

        res = self._rect_with(s, _RefusesOne)
        assert res["isError"] is True and "setDirectionOne returned false" in res["message"]
        assert s.geometricConstraints.calls == []

    def test_a_refused_second_direction_is_an_honest_error(self, install):
        # the two directions are separate calls, so each needs its own read and its own sentence
        s = _two_line_sketch(); install(s)

        class _RefusesTwo(FakeRectPatternInput):
            def setDirectionTwo(self, entity, quantity, distance):
                return False

        res = self._rect_with(s, _RefusesTwo)
        assert res["isError"] is True and "setDirectionTwo returned false" in res["message"]
        assert s.geometricConstraints.calls == []

    def test_a_direction_answer_that_read_as_nothing_is_not_a_refusal(self, install):
        # the exact boundary of `is False`. The bindings declare setDirectionOne/Two -> bool, and
        # gen_api_surface subtracts any name that returns non-bool ANYWHERE, so a live call answers
        # True or False and nothing else; the calls are direct rather than wrapped in safe(), so a
        # platform failure raises instead of answering None. A non-bool is therefore unreachable in
        # production and is pinned here only as the boundary - against the real bindings `is False`
        # and `not` cannot differ.
        s = _two_line_sketch(); install(s)

        class _Mute(FakeRectPatternInput):
            def setDirectionOne(self, *args):
                super().setDirectionOne(*args); return None
            def setDirectionTwo(self, *args):
                super().setDirectionTwo(*args); return None

        out = _one_row(self._rect_with(s, _Mute))
        assert out["created_count"] == 5

    def test_rectangular_direction_entities_default_to_none(self, install):
        # a null direction entity means the sketch X axis / 90 degrees to direction one
        s = _two_line_sketch(); install(s)
        _payload(_constrain(constraint="rectangular_pattern", sketch_name="S",
                            entities="circle:0", entity_one="line:0", entity_two="line:1",
                            quantity=2, distance=10))
        pin = s.geometricConstraints.calls[0][1][0]
        assert pin.one[0] is not None and pin.two[0] is not None

    def test_rectangular_uses_the_given_direction_lines(self, install):
        s = _two_line_sketch(); install(s)
        _payload(_constrain(constraint="rectangular_pattern", sketch_name="S",
                            entities="circle:0", entity_one="line:0", entity_two="line:1",
                            quantity=2, distance=10, quantity_two=2, distance_two=10))
        pin = s.geometricConstraints.calls[0][1][0]
        assert pin.one[0].name == "L0" and pin.two[0].name == "L1"

    def test_distance_two_falls_back_to_distance(self, install):
        s = _two_line_sketch(); install(s)
        _payload(_constrain(constraint="rectangular_pattern", sketch_name="S",
                            entities="circle:0", entity_one="line:0", entity_two="line:1",
                            quantity=2, distance=10, quantity_two=2))
        pin = s.geometricConstraints.calls[0][1][0]
        assert pin.two[2] == "1.0 cm"

    def test_a_count_the_api_changed_is_reported_not_echoed(self, install):
        s = _two_line_sketch(); install(s)
        s.geometricConstraints.pattern_quantity_is = 7
        res = _constrain(constraint="rectangular_pattern", sketch_name="S",
                         entities="circle:0", entity_one="line:0", entity_two="line:1",
                         quantity=3, distance=10)
        assert res["isError"] is True
        assert "quantityOne = 7" in res["message"] and "3 was requested" in res["message"]

    def test_a_pattern_that_created_nothing_is_an_error(self, install):
        s = _two_line_sketch(); install(s)
        res = _constrain(constraint="rectangular_pattern", sketch_name="S",
                         entities="circle:0", entity_one="line:0", entity_two="line:1",
                         quantity=1, distance=10)
        assert res["isError"] is True and "added no sketch geometry" in res["message"]

    def test_circular_takes_the_center_point_and_the_angle(self, install):
        s = _two_line_sketch(); install(s)
        out = _one_row(_constrain(constraint="circular_pattern", sketch_name="S",
                                  entities="circle:0,arc:0", entity_one="point:0",
                                  quantity=4, angle=180))
        pin = s.geometricConstraints.calls[0][1][0]
        assert pin.centerPoint.name == "P0"
        assert pin.totalAngle == "180.0 deg"
        assert out["created_count"] == 6

    def test_circular_needs_a_point_as_the_center(self, install):
        s = _two_line_sketch(); install(s)
        res = _constrain(constraint="circular_pattern", sketch_name="S",
                         entities="circle:0", entity_one="line:0", quantity=4)
        assert res["isError"] is True
        assert "a POINT as entity_one (the center)" in res["message"]
        assert s.geometricConstraints.calls == []

    def test_circular_refuses_a_quantity_below_two(self, install):
        s = _two_line_sketch(); install(s)
        res = _constrain(constraint="circular_pattern", sketch_name="S",
                         entities="circle:0", entity_one="point:0", quantity=1)
        assert res["isError"] is True and "quantity >= 2" in res["message"]
        assert s.geometricConstraints.calls == []

    def test_a_quantity_the_api_changed_is_reported_not_echoed(self, install):
        s = _two_line_sketch(); install(s)
        s.geometricConstraints.pattern_quantity_is = 2
        res = _constrain(constraint="circular_pattern", sketch_name="S",
                         entities="circle:0", entity_one="point:0", quantity=4)
        assert res["isError"] is True and "quantity = 2" in res["message"]


def _auto_result(sketch, dims=0, cons=0, moved=(), fully=False):
    """Wire the sketch's autoConstrain to hand back a result whose additions also land in the
    sketch's OWN counters - the second read-back the tool gates the result against."""
    result = types.SimpleNamespace(addedDimensions=[object()] * dims,
                                   addedConstraints=[object()] * cons,
                                   movedGeometry=list(moved))

    def _run(aci):
        sketch.autoConstrainCalls.append(aci)
        sketch.sketchDimensions._items.extend([object()] * dims)
        sketch.geometricConstraints.count += cons
        sketch.isFullyConstrained = fully
        return result
    sketch.autoConstrain = _run
    return result


class TestAutoConstrain:
    def test_auto_needs_no_entity_refs_at_all(self, install):
        s = _two_line_sketch(); install(s)
        _auto_result(s, dims=2, cons=3, fully=True)
        out = _one_row(_constrain(constraint="auto", sketch_name="S"))
        assert out["applied"] == "auto"
        assert out["added_dimensions"] == 2 and out["added_constraints"] == 3
        assert out["is_fully_constrained"] is True and "now fully constrained" in out["note"]

    def test_auto_on_an_already_fully_constrained_sketch_is_a_no_call_no_op(self, install):
        # Fusion RAISES "AutoConstrain cannot be applied to a fully constrained sketch" (measured
        # live) - so a sketch with nothing left to constrain answers the no-op truth WITHOUT the
        # call, and the platform raise can never surface.
        s = _two_line_sketch(); install(s)
        s.isFullyConstrained = True
        out = _one_row(_constrain(constraint="auto", sketch_name="S"))
        assert out["added_dimensions"] == 0 and out["added_constraints"] == 0
        assert out["is_fully_constrained"] is True
        assert "already fully constrained" in out["note"]
        assert s.autoConstrainCalls == []          # no call reached the platform

    def test_the_requested_option_is_set_on_the_input_and_labeled_requested(self, install):
        s = _two_line_sketch(); install(s)
        _auto_result(s, cons=1)
        out = _one_row(_constrain(constraint="auto", sketch_name="S", result_option="option2"))
        assert s.autoConstrainInput.resultOption is \
            adsk.fusion.AutoConstrainResultTypes.Option2AutoConstrainResultType
        assert out["result_option_requested"] == "option2"

    def test_a_null_result_is_an_error_never_a_silent_ok(self, install):
        # null is autoConstrain's whole failure channel - it does not raise
        s = _two_line_sketch(); install(s)
        s.autoConstrainResult = None
        res = _constrain(constraint="auto", sketch_name="S")
        assert res["isError"] is True
        assert "returned null" in res["message"] and "Nothing was constrained" in res["message"]

    def test_a_null_under_option3_advises_retrying_with_option1_or_2(self, install):
        s = _two_line_sketch(); install(s)
        s.autoConstrainResult = None
        res = _constrain(constraint="auto", sketch_name="S", result_option="option3")
        assert res["isError"] is True
        assert "not eligible for geometry adjustment" in res["message"]
        assert "option1" in res["message"] and "option2" in res["message"]

    def test_option3_discloses_the_geometry_it_moved(self, install):
        s = _two_line_sketch(); install(s)
        _auto_result(s, cons=2, moved=[s.sketchCurves.sketchLines.item(0)], fully=True)
        out = _one_row(_constrain(constraint="auto", sketch_name="S", result_option="option3"))
        assert out["moved_geometry_count"] == 1 and out["moved_geometry"] == ["line:0"]
        assert "MOVED 1 entity" in out["note"]

    def test_a_result_that_added_nothing_to_a_free_sketch_is_an_error(self, install):
        s = _two_line_sketch(); install(s)
        _auto_result(s)
        res = _constrain(constraint="auto", sketch_name="S")
        assert res["isError"] is True
        assert "added no dimensions or constraints" in res["message"]
        assert "option3" in res["message"]

    def test_an_already_fully_constrained_sketch_is_a_clean_no_op(self, install):
        s = _two_line_sketch(); install(s)
        s.isFullyConstrained = True
        _auto_result(s, fully=True)
        out = _one_row(_constrain(constraint="auto", sketch_name="S"))
        assert out["added_dimensions"] == 0 and out["added_constraints"] == 0
        assert out["is_fully_constrained"] is True

    def test_additions_the_sketch_does_not_show_are_an_error(self, install):
        s = _two_line_sketch(); install(s)
        s.autoConstrain = lambda aci: types.SimpleNamespace(
            addedDimensions=[object(), object()], addedConstraints=[], movedGeometry=[])
        res = _constrain(constraint="auto", sketch_name="S")
        assert res["isError"] is True
        assert "reported 2 dimension(s)" in res["message"] and "nothing landed" in res["message"]

    def test_an_option3_failure_still_discloses_the_geometry_it_moved(self, install):
        # option3 adjusts geometry on its way to a solve and those adjustments STAY even when the
        # constrain half fails - a refusal that hides them reads as "nothing happened"
        s = _two_line_sketch(); install(s)
        _auto_result(s, moved=[s.sketchCurves.sketchLines.item(0)])
        res = _constrain(constraint="auto", sketch_name="S", result_option="option3")
        assert res["isError"] is True
        assert "added no dimensions or constraints" in res["message"]
        assert "MOVED 1 entity(ies)" in res["message"] and "REMAINS" in res["message"]

    def test_additions_that_never_landed_also_disclose_the_moved_geometry(self, install):
        s = _two_line_sketch(); install(s)
        line0 = s.sketchCurves.sketchLines.item(0)
        s.autoConstrain = lambda aci: types.SimpleNamespace(
            addedDimensions=[object()], addedConstraints=[], movedGeometry=[line0])
        res = _constrain(constraint="auto", sketch_name="S", result_option="option3")
        assert res["isError"] is True
        assert "nothing landed in it." in res["message"] and "MOVED 1 entity(ies)" in res["message"]

    def test_a_swig_vector_of_added_constraints_is_counted_not_wrapped(self, install):
        # addedConstraints can arrive as a SWIG sequence - it answers len() and indexing but is not
        # a list, so an isinstance check counts the whole vector as a single constraint.
        s = _two_line_sketch(); install(s)
        vector = FakeConstraintVector([object(), object(), object()])

        def _run(aci):
            s.geometricConstraints.count += 3
            return types.SimpleNamespace(addedDimensions=[], addedConstraints=vector,
                                         movedGeometry=[])
        s.autoConstrain = _run
        out = _one_row(_constrain(constraint="auto", sketch_name="S"))
        assert out["added_constraints"] == 3

    def test_a_sketch_left_free_points_at_the_next_step(self, install):
        s = _two_line_sketch(); install(s)
        _auto_result(s, cons=1, fully=False)
        out = _one_row(_constrain(constraint="auto", sketch_name="S"))
        assert out["is_fully_constrained"] is False
        assert "NOT fully constrained" in out["note"] and "option3" in out["note"]


class TestPatternExtensions:
    def _rect(self, **kw):
        base = dict(constraint="rectangular_pattern", sketch_name="S", entities="circle:0",
                    entity_one="line:0", entity_two="line:1", quantity=3, distance=10)
        base.update(kw)
        return _constrain(**base)

    def test_distance_type_defaults_to_spacing(self, install):
        s = _two_line_sketch(); install(s)
        out = _one_row(self._rect())
        pin = s.geometricConstraints.calls[0][1][0]
        assert pin.distanceType is adsk.fusion.PatternDistanceType.SpacingPatternDistanceType
        assert out["distance_type"] == "spacing"

    def test_distance_type_extent_reaches_the_pattern_input(self, install):
        s = _two_line_sketch(); install(s)
        out = _one_row(self._rect(distance_type="extent"))
        pin = s.geometricConstraints.calls[0][1][0]
        assert pin.distanceType is adsk.fusion.PatternDistanceType.ExtentPatternDistanceType
        assert out["distance_type"] == "extent"

    def test_a_distance_type_the_pattern_did_not_take_is_an_error(self, install):
        s = _two_line_sketch(); install(s)
        s.geometricConstraints.pattern_distance_type_is = \
            adsk.fusion.PatternDistanceType.ExtentPatternDistanceType
        res = self._rect(distance_type="spacing")
        assert res["isError"] is True
        assert "different distance_type" in res["message"] and "'spacing'" in res["message"]

    def test_an_unknown_distance_type_is_refused_by_the_enum(self, install):
        s = _two_line_sketch(); install(s)
        res = self._rect(distance_type="middling")
        assert res["isError"] is True and "'distance_type' must be one of" in res["message"]
        assert s.geometricConstraints.calls == []

    def test_symmetric_sets_both_rectangular_directions(self, install):
        s = _two_line_sketch(); install(s)
        out = _one_row(self._rect(symmetric=True))
        pin = s.geometricConstraints.calls[0][1][0]
        assert pin.isSymmetricInDirectionOne is True and pin.isSymmetricInDirectionTwo is True
        assert out["symmetric_requested"] is True and out["symmetric_applied"] is True

    def test_symmetry_is_left_alone_when_it_was_not_asked_for(self, install):
        s = _two_line_sketch(); install(s)
        out = _one_row(self._rect())
        pin = s.geometricConstraints.calls[0][1][0]
        assert not hasattr(pin, "isSymmetricInDirectionOne")
        assert out["symmetric_requested"] is False and out["symmetric_applied"] is False

    def test_a_symmetry_the_platform_declined_is_surfaced_not_claimed(self, install):
        s = _two_line_sketch(); install(s)
        s.geometricConstraints.pattern_symmetric_is = False
        out = _one_row(self._rect(symmetric=True))
        assert out["symmetric_requested"] is True and out["symmetric_applied"] is False
        assert "reads back NOT symmetric" in out["note"]

    def test_symmetric_sets_the_circular_flag(self, install):
        s = _two_line_sketch(); install(s)
        out = _one_row(_constrain(constraint="circular_pattern", sketch_name="S",
                                  entities="circle:0", entity_one="point:0", quantity=4,
                                  symmetric=True))
        pin = s.geometricConstraints.calls[0][1][0]
        assert pin.isSymmetric is True
        assert out["symmetric_applied"] is True and "distance_type" not in out


class TestPatternSuppression:
    """isSuppressed carries one flag per pattern INSTANCE with the original not counting - a 3x2
    pattern takes 5, not 6 - and the flags are only meaningful once both quantities are set."""

    def _rect(self, **kw):
        base = dict(constraint="rectangular_pattern", sketch_name="S", entities="circle:0",
                    entity_one="line:0", entity_two="line:1", quantity=3, quantity_two=2,
                    distance=10)
        base.update(kw)
        return _constrain(**base)

    def test_the_flags_reach_the_pattern_input_as_the_tuple_it_echoes(self, install):
        # the echo is a TUPLE on both input classes: flags handed over as a list read back unequal,
        # and the pattern is refused as a set that did not take before it is ever created
        s = _two_line_sketch(); install(s)
        flags = [False, True, False, False, True]
        out = _one_row(self._rect(suppressed=flags))
        pin = s.geometricConstraints.calls[0][1][0]
        assert pin.isSuppressed == (False, True, False, False, True)
        assert out["suppressed_requested"] == flags and out["suppressed_applied"] == flags

    def test_the_flags_are_set_after_both_quantities(self, install):
        # the binding: quantityOne and quantityTwo must hold valid values before isSuppressed is
        # meaningful, so setting the flags first would apply them to an unsized pattern
        s = _two_line_sketch(); install(s)
        self._rect(suppressed=[False] * 5)
        pin = s.geometricConstraints.calls[0][1][0]
        assert pin.order == ["direction_one", "direction_two", "suppressed"]

    def test_a_short_list_is_refused_naming_expected_and_got(self, install):
        s = _two_line_sketch(); install(s)
        res = self._rect(suppressed=[False, True])
        assert res["isError"] is True
        assert "needs 5 flag(s)" in res["message"] and "Got 2" in res["message"]
        assert s.geometricConstraints.calls == []

    def test_a_list_as_long_as_the_instance_count_is_refused(self, install):
        # 6 flags for a 3x2 is the off-by-one the row-column rule exists to catch: the original
        # geometry cannot be suppressed as part of the pattern
        s = _two_line_sketch(); install(s)
        res = self._rect(suppressed=[False] * 6)
        assert res["isError"] is True and "needs 5 flag(s)" in res["message"]
        assert s.geometricConstraints.calls == []

    def test_a_json_string_of_flags_is_accepted(self, install):
        s = _two_line_sketch(); install(s)
        out = _one_row(self._rect(suppressed="[false, true, false, false, false]"))
        pin = s.geometricConstraints.calls[0][1][0]
        assert pin.isSuppressed == (False, True, False, False, False)
        assert out["suppressed_requested"][1] is True

    def test_a_non_boolean_member_is_refused(self, install):
        s = _two_line_sketch(); install(s)
        res = self._rect(suppressed=[0, 1, 0, 0, 1])
        assert res["isError"] is True and "true/false" in res["message"]
        assert s.geometricConstraints.calls == []

    def test_unparseable_text_is_refused(self, install):
        s = _two_line_sketch(); install(s)
        res = self._rect(suppressed="first and third")
        assert res["isError"] is True and "JSON list" in res["message"]
        assert s.geometricConstraints.calls == []

    def test_a_pattern_carrying_other_flags_is_reported_not_echoed(self, install):
        # the platform can create the pattern with different suppression than it was handed;
        # echoing the request would report a pattern that is not in the sketch
        s = _two_line_sketch(); install(s)
        s.geometricConstraints.pattern_suppressed_is = [False] * 5
        res = self._rect(suppressed=[True, True, False, False, False])
        assert res["isError"] is True
        assert "0 instance(s) suppressed" in res["message"] and "2 requested" in res["message"]

    def test_an_unreadable_pattern_publishes_the_request_only(self, install):
        s = _two_line_sketch(); install(s)
        s.geometricConstraints.pattern_suppression_readable = False
        out = _one_row(self._rect(suppressed=[True, False, False, False, False]))
        assert out["suppressed_applied"] is None
        assert "only" in out["note"] and "REQUESTED" in out["note"]

    def test_an_input_that_drops_the_flags_is_refused_before_the_pattern_is_created(self, install):
        # a SWIG proxy accepts an assignment it does not keep; the pattern must not be created on
        # settings that never landed
        s = _two_line_sketch(); install(s)
        gc = s.geometricConstraints

        class _DeafInput(FakeRectPatternInput):
            @property
            def isSuppressed(self):
                return None
            @isSuppressed.setter
            def isSuppressed(self, value):
                pass
        gc.createRectangularPatternInput = _DeafInput
        res = self._rect(suppressed=[True] + [False] * 4)
        assert res["isError"] is True and "did not take" in res["message"]
        assert gc.calls == []

    def test_circular_takes_one_flag_per_instance_less_the_original(self, install):
        # the N-1 rule holds for circular too (measured at quantity 4 with 3 flags), though its
        # binding states no ORDER for the indices, so no order is claimed for it
        s = _two_line_sketch(); install(s)
        out = _one_row(_constrain(constraint="circular_pattern", sketch_name="S",
                                  entities="circle:0", entity_one="point:0", quantity=4,
                                  suppressed=[False, True, False]))
        pin = s.geometricConstraints.calls[0][1][0]
        assert pin.isSuppressed == (False, True, False)
        assert out["suppressed_applied"] == [False, True, False]
        assert out["created_count"] == 2          # 3 instances less the suppressed one

    def test_circular_refuses_a_wrong_length_list(self, install):
        s = _two_line_sketch(); install(s)
        res = _constrain(constraint="circular_pattern", sketch_name="S", entities="circle:0",
                         entity_one="point:0", quantity=4, suppressed=[False, True])
        assert res["isError"] is True and "needs 3 flag(s)" in res["message"]
        assert s.geometricConstraints.calls == []

    def test_a_suppressed_instance_draws_no_curve(self, install):
        # measured: a 3x2 pattern of one circle with a single flag set creates 4, not 5
        s = _two_line_sketch(); install(s)
        out = _one_row(self._rect(suppressed=[False, True, False, False, False]))
        assert out["created_count"] == 4

    def test_suppressing_every_instance_is_a_pattern_with_no_curves_not_a_failure(self, install):
        # the created-nothing gate catches a silent no-op; a request to suppress everything is not
        # one, and refusing it would call the caller's own instruction a failure
        s = _two_line_sketch(); install(s)
        out = _one_row(self._rect(suppressed=[True] * 5))
        assert out["created_count"] == 0
        assert out["suppressed_applied"] == [True] * 5
        assert "EVERY instance suppressed" in out["note"] and "no curves" in out["note"]

    def test_a_partial_suppression_that_drew_nothing_is_still_an_error(self, install):
        # the gate only stands down for an ALL-suppressed request: two of five suppressed and no
        # curve created is the silent no-op it was written to catch
        s = _two_line_sketch(); install(s)
        gc = s.geometricConstraints
        real = gc.addRectangularPattern

        def _barren(pin):
            c = real(pin)
            c.createdEntities = []
            return c
        gc.addRectangularPattern = _barren
        res = self._rect(suppressed=[True, True, False, False, False])
        assert res["isError"] is True and "added no sketch geometry" in res["message"]

    def test_the_row_column_rule_is_claimed_for_the_rectangular_kind_only(self, install):
        # the rectangular binding fixes the order as row-column; the circular one states no order
        s = _two_line_sketch(); install(s)
        rect = self._rect(suppressed=[False])
        circ = _constrain(constraint="circular_pattern", sketch_name="S", entities="circle:0",
                          entity_one="point:0", quantity=4, suppressed=[False])
        assert "row-column order" in rect["message"]
        assert "row-column" not in circ["message"] and "needs 3 flag(s)" in circ["message"]

    def test_a_pattern_without_the_input_is_untouched(self, install):
        s = _two_line_sketch(); install(s)
        out = _one_row(self._rect())
        pin = s.geometricConstraints.calls[0][1][0]
        assert pin.isSuppressed is None and "suppressed" not in pin.order
        assert "suppressed_requested" not in out

    def test_suppression_is_refused_on_a_non_pattern_constraint(self, install):
        s = _two_line_sketch(); install(s)
        res = _constrain(constraint="polygon", sketch_name="S",
                         entities="line:0,line:1,circle:0", suppressed=[True])
        assert res["isError"] is True
        assert "'suppressed' applies to constraint=" in res["message"]
        assert "rectangular_pattern" in res["message"] and "circular_pattern" in res["message"]
        assert s.geometricConstraints.calls == []


class TestAutoConstrainStrategies:
    """The four AutoConstrainInput dimensioning preferences. Each is documented as ignorable where
    it does not apply to the geometry, and the result reports none of them back, so the payload
    publishes what was REQUESTED."""

    # (input, AutoConstrainInput property, an option, its adsk family, the member that option names)
    _KNOBS = (("dimension_strategy", "dimensionStrategy", "chain", "DimensionStrategyTypes",
               "ChainDimensionStrategyType"),
              ("inter_loop_strategy", "interLoopDimensionStrategy", "baseline",
               "InterLoopDimensionStrategyTypes", "BaselineInterLoopDimensionStrategyType"),
              ("symmetric_strategy", "symmetricDimensionStrategy", "end_to_end",
               "SymmetricDimensionStrategyTypes", "EndToEndSymmetricDimensionStrategyType"),
              ("linear_diameter_dims", "linearDiameterDimensionPreference", "avoid",
               "LinearDiameterDimensionPreferenceTypes",
               "AvoidLinearDiameterDimensionPreferenceType"))

    def test_each_knob_sets_its_own_property_to_the_named_enum_member(self, install):
        # each knob has its OWN family: crossing two of them would set a member from the wrong enum
        for input_name, prop, option, family_name, member in self._KNOBS:
            s = _two_line_sketch(); install(s)
            _auto_result(s, cons=1)
            _payload(_constrain(constraint="auto", sketch_name="S", **{input_name: option}))
            family = getattr(adsk.fusion, family_name)
            assert getattr(s.autoConstrainInput, prop) is getattr(family, member)

    def test_an_unset_knob_assigns_nothing(self, install):
        # the family's own Default* member is what a fresh input already holds - assigning it would
        # be a needless failure point on a build that renamed the member
        s = _two_line_sketch(); install(s)
        _auto_result(s, cons=1)
        _payload(_constrain(constraint="auto", sketch_name="S"))
        for _input_name, prop, _option, _family, _member in self._KNOBS:
            assert not hasattr(s.autoConstrainInput, prop)

    def test_the_requested_strategies_are_published_as_requested(self, install):
        s = _two_line_sketch(); install(s)
        _auto_result(s, dims=2, cons=1, fully=True)
        out = _one_row(_constrain(constraint="auto", sketch_name="S", dimension_strategy="baseline",
                                  linear_diameter_dims="prefer"))
        assert out["strategies_requested"] == {"dimension_strategy": "baseline",
                                               "linear_diameter_dims": "prefer"}
        assert "REQUESTED" in out["note"] and "ignores" in out["note"]

    def test_a_run_without_strategies_publishes_none(self, install):
        s = _two_line_sketch(); install(s)
        _auto_result(s, cons=1)
        out = _one_row(_constrain(constraint="auto", sketch_name="S"))
        assert "strategies_requested" not in out

    def test_an_unknown_option_is_refused_by_the_enum(self, install):
        s = _two_line_sketch(); install(s)
        _auto_result(s, cons=1)
        res = _constrain(constraint="auto", sketch_name="S", dimension_strategy="freestyle")
        assert res["isError"] is True
        assert "'dimension_strategy' must be one of" in res["message"]
        assert s.autoConstrainCalls == []

    def test_a_member_this_build_does_not_carry_is_named_not_defaulted(self, install,
                                                                       monkeypatch):
        # running the operation on its default while reporting the requested strategy is the lie
        # this refusal exists to prevent
        s = _two_line_sketch(); install(s)
        _auto_result(s, cons=1)
        monkeypatch.setattr(adsk.fusion, "SymmetricDimensionStrategyTypes", object())
        res = _constrain(constraint="auto", sketch_name="S", symmetric_strategy="end_to_center")
        assert res["isError"] is True
        assert "'symmetric_strategy' is not available on this Fusion version." in res["message"]
        assert s.autoConstrainCalls == []

    def test_a_strategy_the_input_drops_is_refused_before_the_sketch_is_touched(self, install):
        # a SWIG proxy accepts an assignment to a property it does not define; the sketch must not
        # be auto-constrained on settings that never landed
        s = _two_line_sketch(); install(s)
        _auto_result(s, cons=1)

        class _Deaf:
            resultOption = None
            def __setattr__(self, name, value):
                if name != "dimensionStrategy":
                    object.__setattr__(self, name, value)
        s.createAutoConstrainInput = lambda: _Deaf()
        res = _constrain(constraint="auto", sketch_name="S", dimension_strategy="chain")
        assert res["isError"] is True and "did not take" in res["message"]
        assert s.autoConstrainCalls == []

    def test_a_strategy_is_refused_on_a_constraint_that_has_no_such_setting(self, install):
        s = _two_line_sketch(); install(s)
        res = _constrain(constraint="horizontal", sketch_name="S", entity_one="line:0",
                         inter_loop_strategy="chain")
        assert res["isError"] is True
        assert "'inter_loop_strategy' applies to constraint=auto only" in res["message"]
        assert s.geometricConstraints.calls == []

    def test_the_strategy_tables_map_every_offered_option(self, install):
        # an option on the wire with no member behind it would set nothing and report success
        for input_name, _prop, table in sc._STRATEGY_KNOBS:
            choice = next(c for c in sc._STRATEGY_CHOICES if c.name == input_name)
            assert sorted(choice.options) == sorted(table)
            assert all(m.endswith("Type") for m in table.values())


class TestEveryConstraintCarriesItsOperandRule:
    def test_no_constraint_reaches_the_wire_without_a_requires_entry(self):
        # the operand rule is what a failed add* explains itself with - a constraint without one
        # returns the bare API text and the caller cannot tell a type error from a solver failure
        missing = [name for name in sc._CONSTRAINTS if name not in sc._REQUIRES]
        assert missing == []

    def test_a_missing_direction_line_is_refused(self, install):
        # live-verified: a null direction raises "3 : invalid argument directionOneEntity" even
        # though the binding documents it as the sketch X axis
        s = _two_line_sketch(); install(s)
        res = _constrain(constraint="rectangular_pattern", sketch_name="S",
                         entities="circle:0", entity_one="line:0", quantity=2, distance=10)
        assert res["isError"] is True and "BOTH direction lines" in res["message"]
        assert s.geometricConstraints.calls == []

    def test_an_unknown_constraint_lists_the_valid_ones(self, install):
        s = _two_line_sketch(); install(s)
        res = _constrain(constraint="weldish", sketch_name="S", entity_one="line:0")
        assert res["isError"] is True and "smooth" in res["message"]

    def test_an_unresolvable_entity_one_names_the_ref(self, install):
        s = _two_line_sketch(); install(s)
        res = _constrain(constraint="horizontal", sketch_name="S", entity_one="line:99")
        assert res["isError"] is True and "line:99" in res["message"]

    def test_the_entities_selector_is_parsed_by_the_shared_resolver(self, install):
        # sketch_constrain's list kinds and sketch_move/sketch_copy take the SAME selector; a
        # per-tool copy of the parser is how the two start disagreeing about a bad ref
        s = _two_line_sketch(); install(s)
        res = _constrain(constraint="polygon", sketch_name="S", entities="line:0,arc:7,line:1")
        assert res["isError"] is True
        assert sc._common.resolve_entity_refs(s, "line:0,arc:7,line:1")[2] in res["message"]

    def test_a_list_kind_without_entities_is_refused(self, install):
        s = _two_line_sketch(); install(s)
        res = _constrain(constraint="polygon", sketch_name="S")
        assert res["isError"] is True and "entities" in res["message"]

    def test_symmetry_without_entity_two_is_refused(self, install):
        s = _two_line_sketch(); install(s)
        res = _constrain(constraint="symmetry", sketch_name="S", entity_one="line:0",
                         symmetry_line="line:1")
        assert res["isError"] is True and "entity_two" in res["message"]


class TestNamedSketchMiss:
    def test_reports_the_name_the_walk_searched_for_not_the_raw_input(self, install):
        # the name is STRIPPED before the walk, so echoing the raw input quotes a name nothing
        # ever looked for - and the caller retries against a sketch that was never missing.
        install(_two_line_sketch())
        res = _constrain(constraint="parallel", sketch_name="  Ghost  ",
                         entity_one="line:0", entity_two="line:1")
        assert res["isError"] is True
        assert "No sketch named 'Ghost'" in res["message"]
        assert "'  Ghost  '" not in res["message"]

    def test_the_miss_still_lists_what_is_there(self, install):
        install(_two_line_sketch())
        res = _constrain(constraint="parallel", sketch_name="Ghost",
                         entity_one="line:0", entity_two="line:1")
        assert "Available: S" in res["message"]


class TestBatch:
    """One call carries a LIST of constraints against ONE resolved sketch: they run in order, the
    first failure stops the run, and the payload says what landed and what was not attempted."""

    def test_two_entries_land_in_order_carrying_their_indexes(self, install):
        s = _two_line_sketch(); install(s)
        out = _payload(sc.handler(constraints=[{"constraint": "horizontal",
                                                "entity_one": "line:0"},
                                               {"constraint": "vertical",
                                                "entity_one": "line:1"}], sketch_name="S"))
        assert out["constrained"] == 2 and out["requested"] == 2 and out["sketch"] == "S"
        assert [r["index"] for r in out["results"]] == [0, 1]
        assert [r["applied"] for r in out["results"]] == ["horizontal", "vertical"]
        assert [c[0] for c in s.geometricConstraints.calls] == ["horizontal", "vertical"]

    def test_a_second_entry_that_fails_leaves_the_first_in_the_sketch(self, install):
        # the run stops, it does not roll back - what landed before the failure stays, so the
        # payload has to name the entry that stopped it and how many never ran
        s = _two_line_sketch(); install(s)
        out = _payload(sc.handler(constraints=[{"constraint": "horizontal",
                                                "entity_one": "line:0"},
                                               {"constraint": "horizontal",
                                                "entity_one": "circle:0"},
                                               {"constraint": "vertical",
                                                "entity_one": "line:1"}], sketch_name="S"))
        assert out["constrained"] == 1 and out["requested"] == 3
        assert out["results"][0]["applied"] == "horizontal"
        assert out["failed"]["index"] == 1
        assert "'horizontal' takes one line." in out["failed"]["error"]
        assert out["not_attempted"] == 1
        assert [c[0] for c in s.geometricConstraints.calls] == ["horizontal"]

    def test_a_first_entry_that_fails_is_an_error_naming_its_index(self, install):
        s = _two_line_sketch(); install(s)
        res = sc.handler(constraints=[{"constraint": "horizontal", "entity_one": "circle:0"},
                                      {"constraint": "vertical", "entity_one": "line:1"}],
                         sketch_name="S")
        assert res["isError"] is True
        assert "constraints[0]:" in res["message"] and "Nothing landed." in res["message"]
        assert s.geometricConstraints.calls == []

    def test_an_unknown_field_in_an_entry_is_refused_naming_it(self, install):
        s = _two_line_sketch(); install(s)
        res = sc.handler(constraints=[{"constraint": "horizontal", "entity_one": "line:0",
                                       "entity_three": "line:1"}], sketch_name="S")
        assert res["isError"] is True and "entity_three" in res["message"]
        assert s.geometricConstraints.calls == []

    def test_an_empty_list_is_refused(self, install):
        s = _two_line_sketch(); install(s)
        res = sc.handler(constraints=[], sketch_name="S")
        assert res["isError"] is True and "non-empty list" in res["message"]
        assert s.geometricConstraints.calls == []


def _defaults_one_applies():
    """{field: value} for every entry.get(field, value) _one substitutes, read off the module."""
    src = Path(sc.__file__).read_text(encoding="utf-8")
    return {f: ast.literal_eval(v)
            for f, v in re.findall(r'entry\.get\("(\w+)", ([^)]+)\)', src)}


class TestEntrySchemaDefaults:
    """An omitted entry field's value is STRUCTURE on the wire: the entry schema's `default` is the
    value _one substitutes for it, so the prose never has to spell one."""

    def test_the_entry_schema_promises_what_one_substitutes(self):
        applied = _defaults_one_applies()
        assert set(applied) >= {"quantity", "quantity_two", "angle", "symmetric"}, applied
        props = sc._ENTRY_SCHEMA["properties"]
        for field, value in applied.items():
            # a false / '' / 0 default is what an absent field already says (_one reads each of
            # them through bool()/or), so it stays OFF the wire and off this entry's byte budget
            want = value if value else None
            assert props[field].get("default") == want, field

    def test_no_entry_default_is_promised_that_nothing_applies(self):
        applied = _defaults_one_applies()
        kinds = {k.name: k.default for v in vars(sc).values()
                 for k in (v if isinstance(v, tuple) else (v,))
                 if isinstance(k, sc._inputs.InputKind)}
        for field, prop in sc._ENTRY_SCHEMA["properties"].items():
            if "default" in prop:
                assert prop["default"] in (applied.get(field), kinds.get(field)), field
