"""Unit tests for sketch_add_geometry.py - every draw kind, its landed-curve gate, and the batch.

One call draws a LIST: the sketch/component/units scope stays top-level and every per-entity input
rides in a 'geometry' entry, so a single-entity test reads its payload off results[0].
"""

from types import SimpleNamespace
import pytest
from conftest import load_tool
from _sketch_fakes import FakeSketch, _Curve, _payload, draw_installer, scoped_installer

sk = load_tool("sketch_add_geometry")
_install_draw = draw_installer(sk)
_install_scoped = scoped_installer(sk)


class _DeferStuck(FakeSketch):
    """A sketch that refuses to leave deferred compute: setting isComputeDeferred back to False
    raises, so the draw finishes with the sketch still deferred."""

    _deferred = False

    @property
    def isComputeDeferred(self):
        return self._deferred

    @isComputeDeferred.setter
    def isComputeDeferred(self, value):
        if self._deferred and not value:
            raise RuntimeError("compute cannot be resumed on this sketch")
        self._deferred = bool(value)


@pytest.fixture
def shared_name(monkeypatch):
    """'Sketch1' in BOTH components. The two sketches start with DIFFERENT curve counts (Alpha has
    one line already, Beta none), so a call that reached the wrong one is visible in the counts and
    not merely in a name that both sketches share."""
    alpha, beta = FakeSketch("Sketch1"), FakeSketch("Sketch1")
    alpha.sketchLines._land(1)
    design = _install_scoped(monkeypatch, [("Alpha", [alpha]), ("Beta", [beta])])
    return design, alpha, beta


def _first(res):
    """The ONE row a single-entry batch landed: results[0], with the batch counts checked."""
    out = _payload(res)
    assert (out["drawn"], out["requested"]) == (1, 1), out
    return out["results"][0]


class TestKindDescription:
    def test_the_kind_description_names_every_kinds_inputs_from_the_guard_table(self):
        desc = sk._ENTRY_SCHEMA["properties"]["kind"]["description"]
        for kind, keys in sk._REQUIRED.items():
            clause = f"{kind} {','.join(keys) or 'points'}"
            assert clause in desc, clause
        assert desc.count(";") == len(sk._REQUIRED) - 1


class TestScaleWiring:

    def test_sketches_uses_the_shared_scale(self):
        import importlib
        common = importlib.import_module(sk.scale.__module__)
        assert sk.scale is common.scale            # same single-source callable, not a local copy
        assert sk.scale("mm") == 0.1 and sk.scale("furlongs") is None


class TestNewKinds:

    def test_ellipse(self, monkeypatch):
        s = FakeSketch(); _install_draw(monkeypatch, s)
        _payload(sk.handler(geometry=[{"kind": "ellipse", "cx": 0, "cy": 0, "radius": 10,
                                       "minor": 4}]))
        assert s.sketchEllipses.count == 1

    def test_slot(self, monkeypatch):
        # slot must call the SKETCH method addCenterToCenterSlot with a ValueInput width
        # (curves.sketchLines is the wrong object; a bare float width is rejected), with
        # width = radius*2 (full slot width; radius is the documented half-width). 3mm radius,
        # default units mm -> full width 6mm = 0.6cm.
        s = FakeSketch(); _install_draw(monkeypatch, s)
        _payload(sk.handler(geometry=[{"kind": "slot", "x1": 0, "y1": 0, "x2": 20, "y2": 0,
                                       "radius": 3}]))
        assert s.slot_call is not None, "addCenterToCenterSlot not called on the sketch"
        tag, val = s.slot_call["width"]
        assert tag == "real" and abs(val - 0.6) < 1e-9    # ValueInput, full width 6mm -> 0.6cm
        # and it must NOT have gone through sketchLines
        assert s.sketchLines.last is None
        # the measured landing: 2 solid lines + the construction centre-to-centre line, 2 arc caps
        assert s.sketchLines.count == 3 and s.sketchArcs.count == 2
        assert [c.isConstruction for c in s.sketchLines._items] == [False, False, True]

    def test_point(self, monkeypatch):
        s = FakeSketch(); _install_draw(monkeypatch, s)
        _payload(sk.handler(geometry=[{"kind": "point", "cx": 5, "cy": 5}]))
        assert s.sketchPoints.count == 1

    def test_a_circle_publishes_the_index_its_centre_point_landed_at(self, monkeypatch):
        # The centre joins sketchPoints, so 'point:<index>' counted by hand runs one short per
        # circle already drawn. The payload names the index the sketch itself gave it.
        s = FakeSketch(); _install_draw(monkeypatch, s)
        s.sketchPoints._land_point()                       # a point drawn earlier holds index 0
        out = _first(sk.handler(geometry=[{"kind": "circle", "cx": 0, "cy": 0, "radius": 5}]))
        assert out["center_point"] == "point:1"
        assert "center_point=point:1" in out["note"]

    def test_a_second_circle_publishes_its_own_centre_not_the_first_ones(self, monkeypatch):
        # The read must follow the NEWEST circle: two circles in one sketch land two centres, and
        # naming the first one's index for the second is exactly the off-by-N this key exists for.
        s = FakeSketch(); _install_draw(monkeypatch, s)
        first = _first(sk.handler(geometry=[{"kind": "circle", "cx": 0, "cy": 0, "radius": 5}]))
        second = _first(sk.handler(geometry=[{"kind": "circle", "cx": 20, "cy": 0, "radius": 5}]))
        assert first["center_point"] == "point:0"
        assert second["center_point"] == "point:1"

    def test_an_arc_publishes_its_centre_point_too(self, monkeypatch):
        s = FakeSketch(); _install_draw(monkeypatch, s)
        out = _first(sk.handler(geometry=[{"kind": "arc", "cx": 0, "cy": 0, "x1": 5, "y1": 0,
                                           "sweep_deg": 90}]))
        assert out["center_point"] == "point:0"

    def test_a_kind_with_no_centre_publishes_no_centre_point(self, monkeypatch):
        s = FakeSketch(); _install_draw(monkeypatch, s)
        out = _first(sk.handler(geometry=[{"kind": "line", "x1": 0, "y1": 0, "x2": 10, "y2": 0}]))
        assert "center_point" not in out

    def test_spline(self, monkeypatch):
        s = FakeSketch(); _install_draw(monkeypatch, s)
        _payload(sk.handler(geometry=[{"kind": "spline", "points": [[0, 0], [5, 8], [10, 0]]}]))
        assert s.sketchFittedSplines.count == 1

    def test_center_rectangle(self, monkeypatch):
        s = FakeSketch(); _install_draw(monkeypatch, s)
        _payload(sk.handler(geometry=[{"kind": "center_rectangle", "cx": 0, "cy": 0, "x2": 10,
                                       "y2": 5}]))
        assert s.sketchLines.last[0] == "crect"

    def test_is_construction_marks_curve(self, monkeypatch):
        s = FakeSketch(); _install_draw(monkeypatch, s)
        _payload(sk.handler(geometry=[{"kind": "circle", "cx": 0, "cy": 0, "radius": 5,
                                       "is_construction": True}]))
        assert s.sketchCircles.item(0).isConstruction is True

    def test_non_construction_default(self, monkeypatch):
        s = FakeSketch(); _install_draw(monkeypatch, s)
        _payload(sk.handler(geometry=[{"kind": "circle", "cx": 0, "cy": 0, "radius": 5}]))
        assert s.sketchCircles.item(0).isConstruction is False

    def test_ellipse_label_reports_the_minor_radius_it_drew(self, monkeypatch):
        # 'minor' omitted -> major/2 is DRAWN, so the label must state 10, not the raw None
        s = FakeSketch(); _install_draw(monkeypatch, s)
        out = _first(sk.handler(geometry=[{"kind": "ellipse", "cx": 0, "cy": 0, "radius": 20}]))
        assert "minor=10" in out["label"]
        _tag, _c, _major, minor_pt = s.sketchEllipses.last
        assert minor_pt.y == 1.0                       # (20/2)mm -> 1cm actually drawn

    def test_ellipse_label_reports_an_explicit_minor(self, monkeypatch):
        s = FakeSketch(); _install_draw(monkeypatch, s)
        out = _first(sk.handler(geometry=[{"kind": "ellipse", "cx": 0, "cy": 0, "radius": 20,
                                           "minor": 4}]))
        assert "minor=4" in out["label"]

    def test_ellipse_needs_positive_radius(self, monkeypatch):
        s = FakeSketch(); _install_draw(monkeypatch, s)
        res = sk.handler(geometry=[{"kind": "ellipse", "cx": 0, "cy": 0, "radius": 0}])
        assert res["isError"] is True
        assert "radius must be > 0" in res["message"]


class TestArcSlotKinds:

    """The two arc-slot kinds. Both take 'width' as a ValueInput holding the FULL width (radius*2),
    and addCenterPointArcSlot's optional tail is positional - radius, angle, then three dimension
    flags - with no overload accepting a bool in the radius or angle slot, so the ARITY the wrapper
    builds and the refusals that keep a flag off a short form are the contract under test."""

    def test_three_point_arc_slot_orders_start_end_point_on_arc_then_full_width(self, monkeypatch):
        s = FakeSketch(); _install_draw(monkeypatch, s)
        _payload(sk.handler(geometry=[{"kind": "three_point_arc_slot", "x1": 0, "y1": 0, "x2": 20,
                                       "y2": 0, "cx": 10, "cy": 6, "radius": 3}]))
        start, end, on_arc, width, flag = s.three_point_arc_slot_args
        # cx,cy is the point ON the arc and rides in the THIRD slot, not the first
        assert (start.x, end.x, on_arc.x) == (0.0, 2.0, 1.0)   # mm -> cm
        assert abs(on_arc.y - 0.6) < 1e-9
        tag, val = width                   # ValueInput, full width = radius*2 = 6mm -> 0.6cm
        assert tag == "real" and abs(val - 0.6) < 1e-9
        assert flag is False
        assert s.sketchLines.last is None  # a Sketch method, not a sketchLines one

    def test_three_point_arc_slot_forwards_the_width_dimension_flag(self, monkeypatch):
        s = FakeSketch(); _install_draw(monkeypatch, s)
        _payload(sk.handler(geometry=[{"kind": "three_point_arc_slot", "x1": 0, "y1": 0, "x2": 20,
                                       "y2": 0, "cx": 10, "cy": 6, "radius": 3,
                                       "create_width_dimension": True}]))
        assert s.three_point_arc_slot_args[4] is True

    def test_three_point_arc_slot_refuses_the_radius_and_angle_dimension_flags(self, monkeypatch):
        # its only trailing argument is createWidthDimension, so silently dropping the other two
        # would misreport what was drawn
        s = FakeSketch(); _install_draw(monkeypatch, s)
        res = sk.handler(geometry=[{"kind": "three_point_arc_slot", "x1": 0, "y1": 0, "x2": 20,
                                    "y2": 0, "cx": 10, "cy": 6, "radius": 3,
                                    "create_angle_dimension": True}])
        assert res["isError"] is True
        assert "create_angle_dimension" in res["message"]
        assert s.three_point_arc_slot_args is None      # refused BEFORE reaching the API

    def test_center_point_arc_slot_sends_exactly_four_args_when_no_tail_is_given(self, monkeypatch):
        s = FakeSketch(); _install_draw(monkeypatch, s)
        _payload(sk.handler(geometry=[{"kind": "center_point_arc_slot", "cx": 0, "cy": 0, "x1": 50,
                                       "y1": 0, "x2": 0, "y2": 50, "radius": 5}]))
        args = s.center_point_arc_slot_args
        assert len(args) == 4                          # a trailing default bool is not an overload
        center, start, end, width = args
        assert (center.x, start.x, end.y) == (0.0, 5.0, 5.0)
        assert width == ("real", 1.0)                  # full width = 5mm*2 -> 1.0cm

    def test_center_point_arc_slot_orders_radius_then_angle_then_three_bools(self, monkeypatch):
        s = FakeSketch(); _install_draw(monkeypatch, s)
        _payload(sk.handler(geometry=[{"kind": "center_point_arc_slot", "cx": 0, "cy": 0, "x1": 50,
                                       "y1": 0, "x2": 0, "y2": 50, "radius": 5, "arc_radius": 30,
                                       "angle_deg": 45, "create_width_dimension": True,
                                       "create_radius_dimension": False,
                                       "create_angle_dimension": True}]))
        args = s.center_point_arc_slot_args
        assert len(args) == 9
        assert args[4] == ("real", 3.0)                # 30mm arc radius -> 3cm
        assert args[5] == ("string", "45.0 deg")       # a unit-bearing expression, not radians
        assert list(args[6:]) == [True, False, True]   # width, radius, angle - in that order

    def test_center_point_arc_slot_radius_alone_makes_the_five_arg_form(self, monkeypatch):
        s = FakeSketch(); _install_draw(monkeypatch, s)
        _payload(sk.handler(geometry=[{"kind": "center_point_arc_slot", "cx": 0, "cy": 0, "x1": 50,
                                       "y1": 0, "x2": 0, "y2": 50, "radius": 5,
                                       "arc_radius": 30}]))
        assert len(s.center_point_arc_slot_args) == 5

    def test_center_point_arc_slot_refuses_an_angle_without_a_radius(self, monkeypatch):
        s = FakeSketch(); _install_draw(monkeypatch, s)
        res = sk.handler(geometry=[{"kind": "center_point_arc_slot", "cx": 0, "cy": 0, "x1": 50,
                                    "y1": 0, "x2": 0, "y2": 50, "radius": 5, "angle_deg": 45}])
        assert res["isError"] is True
        assert "45" in res["message"] and "arc_radius" in res["message"]
        assert s.center_point_arc_slot_args is None

    def test_center_point_arc_slot_refuses_a_dimension_flag_without_radius_and_angle(self, monkeypatch):
        s = FakeSketch(); _install_draw(monkeypatch, s)
        res = sk.handler(geometry=[{"kind": "center_point_arc_slot", "cx": 0, "cy": 0, "x1": 50,
                                    "y1": 0, "x2": 0, "y2": 50, "radius": 5, "arc_radius": 30,
                                    "create_angle_dimension": True}])
        assert res["isError"] is True
        assert "create_angle_dimension" in res["message"] and "angle_deg" in res["message"]
        assert s.center_point_arc_slot_args is None

    def test_arc_slot_refuses_a_non_positive_half_width(self, monkeypatch):
        s = FakeSketch(); _install_draw(monkeypatch, s)
        res = sk.handler(geometry=[{"kind": "center_point_arc_slot", "cx": 0, "cy": 0, "x1": 50,
                                    "y1": 0, "x2": 0, "y2": 50, "radius": 0}])
        assert res["isError"] is True
        assert "radius=0" in res["message"]
        assert s.center_point_arc_slot_args is None

    def test_arc_slot_counts_the_arcs_it_added(self, monkeypatch):
        s = FakeSketch(); _install_draw(monkeypatch, s)
        out = _first(sk.handler(geometry=[{"kind": "center_point_arc_slot", "cx": 0, "cy": 0,
                                           "x1": 50, "y1": 0, "x2": 0, "y2": 50, "radius": 5}]))
        assert out["curves_added"] == 5
        assert "arc:<index>" in out["note"]

    def test_arc_slot_that_lands_no_arc_is_an_error(self, monkeypatch):
        # the constructor handing back an object is not proof the arcs reached the sketch
        s = FakeSketch(); _install_draw(monkeypatch, s)
        monkeypatch.setattr(s, "addCenterPointArcSlot", lambda *a: _Curve())
        res = sk.handler(geometry=[{"kind": "center_point_arc_slot", "cx": 0, "cy": 0, "x1": 50,
                                    "y1": 0, "x2": 0, "y2": 50, "radius": 5}])
        assert res["isError"] is True
        assert "did not change" in res["message"]


class TestPlainSlotRejectsTailInputs:

    """kind='slot' is drawn from two centres and radius alone, so every tail input the other slot
    kinds carry has nowhere to go here - each one must be refused BY NAME rather than dropped into
    a clean ok, which is what makes the wrong kind look like it worked."""

    @pytest.mark.parametrize("field, value", [
        ("slot_length", 40), ("angle_deg", 30), ("arc_radius", 30),
        ("create_width_dimension", True), ("create_radius_dimension", True),
        ("create_angle_dimension", True)])
    def test_tail_input_is_refused_by_name(self, monkeypatch, field, value):
        s = FakeSketch(); _install_draw(monkeypatch, s)
        res = sk.handler(geometry=[{"kind": "slot", "x1": 0, "y1": 0, "x2": 20, "y2": 0,
                                    "radius": 3, field: value}])
        assert res["isError"] is True
        assert field in res["message"]
        assert s.slot_call is None                  # refused BEFORE the API call

    def test_a_plain_slot_without_a_tail_still_draws(self, monkeypatch):
        s = FakeSketch(); _install_draw(monkeypatch, s)
        _payload(sk.handler(geometry=[{"kind": "slot", "x1": 0, "y1": 0, "x2": 20, "y2": 0,
                                       "radius": 3}]))
        assert s.slot_call is not None

    def test_plain_slot_refusal_names_every_stray_input_at_once(self, monkeypatch):
        s = FakeSketch(); _install_draw(monkeypatch, s)
        res = sk.handler(geometry=[{"kind": "slot", "x1": 0, "y1": 0, "x2": 20, "y2": 0,
                                    "radius": 3, "slot_length": 40,
                                    "create_angle_dimension": True}])
        assert res["isError"] is True
        assert "slot_length" in res["message"] and "create_angle_dimension" in res["message"]


class TestLinearSlotKinds:

    """overall_slot / center_point_slot. Their tail is createWidthDimension FIRST, then the length
    ValueInput, then the angle ValueInput - the reverse nesting of the arc slots - and the linear and
    angular dimensions are created by passing the values, with no flag of their own. So the arity the
    wrapper builds, and the refusals for a value or flag with nowhere to sit, are the contract."""

    def test_overall_slot_sends_exactly_three_args_when_no_tail_is_given(self, monkeypatch):
        s = FakeSketch(); _install_draw(monkeypatch, s)
        _payload(sk.handler(geometry=[{"kind": "overall_slot", "x1": 0, "y1": 0, "x2": 60,
                                       "y2": 0, "radius": 4}]))
        args = s.overall_slot_args
        assert len(args) == 3                          # no trailing default bool
        a, b, width = args
        assert (a.x, b.x) == (0.0, 6.0)                # mm -> cm
        assert width == ("real", 0.8)                  # ValueInput, full width = radius*2

    def test_overall_slot_full_tail_orders_bool_then_length_then_angle(self, monkeypatch):
        s = FakeSketch(); _install_draw(monkeypatch, s)
        _payload(sk.handler(geometry=[{"kind": "overall_slot", "x1": 0, "y1": 0, "x2": 60, "y2": 0,
                                       "radius": 4, "slot_length": 40, "angle_deg": 30,
                                       "create_width_dimension": True}]))
        args = s.overall_slot_args
        assert len(args) == 6
        assert args[3] is True                         # the bool sits BEFORE the two values
        assert args[4] == ("real", 4.0)                # 40mm length -> 4cm
        assert args[5] == ("string", "30.0 deg")       # a unit-bearing expression, not radians

    def test_overall_slot_length_alone_still_sends_the_bool_ahead_of_it(self, monkeypatch):
        # the length has no overload it can reach without createWidthDimension in front of it
        s = FakeSketch(); _install_draw(monkeypatch, s)
        _payload(sk.handler(geometry=[{"kind": "overall_slot", "x1": 0, "y1": 0, "x2": 60, "y2": 0,
                                       "radius": 4, "slot_length": 40}]))
        args = s.overall_slot_args
        assert len(args) == 5
        assert args[3] is False and args[4] == ("real", 4.0)

    def test_width_dimension_flag_alone_makes_the_four_arg_form(self, monkeypatch):
        s = FakeSketch(); _install_draw(monkeypatch, s)
        _payload(sk.handler(geometry=[{"kind": "center_point_slot", "x1": 0, "y1": 0, "x2": 25,
                                       "y2": 0, "radius": 3, "create_width_dimension": True}]))
        assert len(s.center_point_slot_args) == 4
        assert s.center_point_slot_args[3] is True

    def test_center_point_slot_length_is_the_half_length_as_passed(self, monkeypatch):
        s = FakeSketch(); _install_draw(monkeypatch, s)
        out = _first(sk.handler(geometry=[{"kind": "center_point_slot", "x1": 0, "y1": 0, "x2": 25,
                                           "y2": 0, "radius": 3, "slot_length": 25}]))
        assert s.center_point_slot_args[4] == ("real", 2.5)   # passed through unhalved, in cm
        assert "half_len=25" in out["label"]                  # named for what the API takes

    def test_center_point_slot_routes_to_its_own_constructor(self, monkeypatch):
        s = FakeSketch(); _install_draw(monkeypatch, s)
        _payload(sk.handler(geometry=[{"kind": "center_point_slot", "x1": 0, "y1": 0, "x2": 25,
                                       "y2": 0, "radius": 3}]))
        assert s.center_point_slot_args is not None and s.overall_slot_args is None

    def test_linear_slot_refuses_the_radius_and_angle_dimension_flags(self, monkeypatch):
        # its linear/angular dimensions come from passing slot_length/angle_deg, not from a flag
        s = FakeSketch(); _install_draw(monkeypatch, s)
        res = sk.handler(geometry=[{"kind": "overall_slot", "x1": 0, "y1": 0, "x2": 60, "y2": 0,
                                    "radius": 4, "slot_length": 40,
                                    "create_angle_dimension": True}])
        assert res["isError"] is True
        assert "create_angle_dimension" in res["message"]
        assert s.overall_slot_args is None

    def test_linear_slot_refuses_an_angle_without_a_length(self, monkeypatch):
        s = FakeSketch(); _install_draw(monkeypatch, s)
        res = sk.handler(geometry=[{"kind": "overall_slot", "x1": 0, "y1": 0, "x2": 60, "y2": 0,
                                    "radius": 4, "angle_deg": 30}])
        assert res["isError"] is True
        assert "30" in res["message"] and "slot_length" in res["message"]
        assert s.overall_slot_args is None

    def test_linear_slot_refuses_arc_radius(self, monkeypatch):
        s = FakeSketch(); _install_draw(monkeypatch, s)
        res = sk.handler(geometry=[{"kind": "center_point_slot", "x1": 0, "y1": 0, "x2": 25,
                                    "y2": 0, "radius": 3, "arc_radius": 30}])
        assert res["isError"] is True
        assert "arc_radius" in res["message"] and "center_point_arc_slot" in res["message"]
        assert s.center_point_slot_args is None

    def test_linear_slot_refuses_a_non_positive_length(self, monkeypatch):
        s = FakeSketch(); _install_draw(monkeypatch, s)
        res = sk.handler(geometry=[{"kind": "overall_slot", "x1": 0, "y1": 0, "x2": 60, "y2": 0,
                                    "radius": 4, "slot_length": 0}])
        assert res["isError"] is True
        assert "slot_length=0" in res["message"]
        assert s.overall_slot_args is None

    def test_arc_slot_refuses_slot_length(self, monkeypatch):
        s = FakeSketch(); _install_draw(monkeypatch, s)
        res = sk.handler(geometry=[{"kind": "center_point_arc_slot", "cx": 0, "cy": 0, "x1": 50,
                                    "y1": 0, "x2": 0, "y2": 50, "radius": 5, "slot_length": 40}])
        assert res["isError"] is True
        assert "slot_length" in res["message"] and "arc_radius" in res["message"]
        assert s.center_point_arc_slot_args is None

    def test_three_point_arc_slot_refuses_arc_radius(self, monkeypatch):
        # its arc is fixed by the three points, so a radius would be silently dropped
        s = FakeSketch(); _install_draw(monkeypatch, s)
        res = sk.handler(geometry=[{"kind": "three_point_arc_slot", "x1": 0, "y1": 0, "x2": 20,
                                    "y2": 0, "cx": 10, "cy": 6, "radius": 3, "arc_radius": 30}])
        assert res["isError"] is True
        assert "arc_radius" in res["message"]
        assert s.three_point_arc_slot_args is None

    def test_linear_slot_counts_the_lines_it_added(self, monkeypatch):
        s = FakeSketch(); _install_draw(monkeypatch, s)
        out = _first(sk.handler(geometry=[{"kind": "overall_slot", "x1": 0, "y1": 0, "x2": 60,
                                           "y2": 0, "radius": 4}]))
        assert out["curves_added"] == 3               # sides + centreline; the caps are arcs
        assert "line:<index>" in out["note"]

    def test_width_dimension_flag_alone_lands_no_fourth_line(self, monkeypatch):
        # the fourth line arrives with the length/angle tail, not with the bool
        s = FakeSketch(); _install_draw(monkeypatch, s)
        out = _first(sk.handler(geometry=[{"kind": "overall_slot", "x1": 0, "y1": 0, "x2": 60,
                                           "y2": 0, "radius": 4,
                                           "create_width_dimension": True}]))
        assert len(s.overall_slot_args) == 4 and out["curves_added"] == 3

    def test_tailed_linear_slot_reports_the_fourth_line(self, monkeypatch):
        s = FakeSketch(); _install_draw(monkeypatch, s)
        out = _first(sk.handler(geometry=[{"kind": "overall_slot", "x1": 0, "y1": 0, "x2": 60,
                                           "y2": 0, "radius": 4, "slot_length": 40,
                                           "angle_deg": 30, "create_width_dimension": True}]))
        assert out["curves_added"] == 4
        assert "three, four when a length or angle is passed" in out["note"]

    def test_three_point_slot_length_refusal_points_at_the_kind_that_takes_one(self, monkeypatch):
        # three_point_arc_slot REFUSES arc_radius, so this remedy must not send the caller there
        s = FakeSketch(); _install_draw(monkeypatch, s)
        res = sk.handler(geometry=[{"kind": "three_point_arc_slot", "x1": 0, "y1": 0, "x2": 20,
                                    "y2": 0, "cx": 10, "cy": 6, "radius": 3, "slot_length": 40}])
        assert res["isError"] is True
        assert "kind='center_point_arc_slot'" in res["message"]
        assert "arc_radius" not in res["message"]
        assert s.three_point_arc_slot_args is None

    def test_linear_slot_that_lands_no_line_is_an_error(self, monkeypatch):
        s = FakeSketch(); _install_draw(monkeypatch, s)
        monkeypatch.setattr(s, "addOverallSlot", lambda *a: ["entity"])
        res = sk.handler(geometry=[{"kind": "overall_slot", "x1": 0, "y1": 0, "x2": 60, "y2": 0,
                                    "radius": 4}])
        assert res["isError"] is True
        assert "did not change" in res["message"]


class TestCoreKinds:

    def test_circle_radius_scaled_to_cm(self, monkeypatch):
        s = FakeSketch(); _install_draw(monkeypatch, s)
        _payload(sk.handler(geometry=[{"kind": "circle", "cx": 0, "cy": 0, "radius": 10}],
                            units="mm"))
        # addByCenterRadius(center, radius_cm): 10mm -> 1.0cm
        tag, center, r = s.sketchCircles.last
        assert tag == "circle" and abs(r - 1.0) < 1e-9

    def test_line_points_scaled(self, monkeypatch):
        s = FakeSketch(); _install_draw(monkeypatch, s)
        _payload(sk.handler(geometry=[{"kind": "line", "x1": 10, "y1": 0, "x2": 20, "y2": 0}],
                            units="mm"))
        tag, p1, p2 = s.sketchLines.last
        assert (round(p1.x, 6), round(p2.x, 6)) == (1.0, 2.0)   # cm

    def test_arc_sweep_converted_to_radians(self, monkeypatch):
        import math
        s = FakeSketch(); _install_draw(monkeypatch, s)
        _payload(sk.handler(geometry=[{"kind": "arc", "cx": 0, "cy": 0, "x1": 10, "y1": 0,
                                       "sweep_deg": 90}]))
        tag, center, start, sweep = s.sketchArcs.last
        assert abs(sweep - math.pi / 2) < 1e-9   # 90deg -> pi/2 rad

    def test_polygon_radius_scaled(self, monkeypatch):
        s = FakeSketch(); _install_draw(monkeypatch, s)
        _payload(sk.handler(geometry=[{"kind": "polygon", "cx": 0, "cy": 0, "radius": 10,
                                       "sides": 6}], units="mm"))
        tag, center, n, r = s.sketchLines.last
        assert n == 6 and abs(r - 1.0) < 1e-9

    def test_unknown_kind_errors(self, monkeypatch):
        s = FakeSketch(); _install_draw(monkeypatch, s)
        res = sk.handler(geometry=[{"kind": "blob", "cx": 0, "cy": 0}])
        assert res["isError"] is True and "Unknown kind" in res["message"]

    def test_unknown_units_errors(self, monkeypatch):
        s = FakeSketch(); _install_draw(monkeypatch, s)
        res = sk.handler(geometry=[{"kind": "circle", "cx": 0, "cy": 0, "radius": 5}],
                         units="furlong")
        assert res["isError"] is True and "Unknown units" in res["message"]

    def test_missing_required_params_listed(self, monkeypatch):
        s = FakeSketch(); _install_draw(monkeypatch, s)
        res = sk.handler(geometry=[{"kind": "line", "x1": 0, "y1": 0}])   # x2,y2 missing
        assert res["isError"] is True
        assert "x2" in res["message"] and "y2" in res["message"]

    def test_polygon_needs_three_sides(self, monkeypatch):
        s = FakeSketch(); _install_draw(monkeypatch, s)
        res = sk.handler(geometry=[{"kind": "polygon", "cx": 0, "cy": 0, "radius": 5, "sides": 2}])
        assert res["isError"] is True and "sides >= 3" in res["message"]

    def test_summary_reports_counts(self, monkeypatch):
        s = FakeSketch(); _install_draw(monkeypatch, s)
        out = _first(sk.handler(geometry=[{"kind": "circle", "cx": 0, "cy": 0, "radius": 5}]))
        assert out["sketch"]["circle_count"] == 1
        assert out["kind"] == "circle"


class TestEntrySchemaDefaults:
    """An omitted entry field's value is STRUCTURE on the wire: the entry schema's `default` is the
    value the draw substitutes for it, so the prose never has to spell one."""

    def test_an_omitted_degree_draws_the_schema_default(self, monkeypatch):
        import adsk.fusion
        s = FakeSketch(); _install_draw(monkeypatch, s)
        _first(sk.handler(geometry=[{"kind": "cv_spline",
                                     "points": [[0, 0], [5, 8], [10, 0], [15, 8]]}]))
        promised = sk._ENTRY_SCHEMA["properties"]["degree"]["default"]
        member = getattr(adsk.fusion.SplineDegrees, sk._SPLINE_DEGREES[promised])
        assert s.sketchControlPointSplines.last[-1] == member


class TestKindCollectionFallback:

    """Every kind's collection resolves through _common, so the factory-returned-but-nothing-landed
    gate runs for all of them and each publishes 'curves_added'. _KIND_REF_TOKEN answers first, for
    a kind whose curves land in ANOTHER kind's collection (a rectangle's four lines)."""

    def test_a_line_that_never_lands_is_an_error(self, monkeypatch):
        # addByTwoPoints hands back a curve without it reaching sketchLines: a false success
        s = FakeSketch(); _install_draw(monkeypatch, s)
        monkeypatch.setattr(s.sketchLines, "addByTwoPoints", lambda a, b: _Curve())
        res = sk.handler(geometry=[{"kind": "line", "x1": 0, "y1": 0, "x2": 20, "y2": 0}])
        assert res["isError"] is True and "did not change" in res["message"]
        # a kind neither table names still has its collection NAMED in the refusal
        assert "own line collection" in res["message"]

    def test_a_line_reports_the_one_curve_it_added(self, monkeypatch):
        s = FakeSketch(); _install_draw(monkeypatch, s)
        out = _first(sk.handler(geometry=[{"kind": "line", "x1": 0, "y1": 0, "x2": 20, "y2": 0}]))
        assert out["curves_added"] == 1

    def test_a_rectangle_counts_every_line_the_one_call_landed(self, monkeypatch):
        # a rectangle is built BY the SketchLines factory, so 'line' is its collection and
        # curves_added counts the pieces - not one per factory call
        s = FakeSketch(); _install_draw(monkeypatch, s)
        out = _first(sk.handler(geometry=[{"kind": "rectangle", "x1": 0, "y1": 0, "x2": 20,
                                           "y2": 10}]))
        assert out["curves_added"] == 4

    def test_a_rectangle_that_never_lands_is_an_error(self, monkeypatch):
        s = FakeSketch(); _install_draw(monkeypatch, s)
        monkeypatch.setattr(s.sketchLines, "addTwoPointRectangle", lambda a, b: _Curve())
        res = sk.handler(geometry=[{"kind": "rectangle", "x1": 0, "y1": 0, "x2": 20, "y2": 10}])
        assert res["isError"] is True and "did not change" in res["message"]
        # the refusal names the collection that was COUNTED - there is no 'rectangle' collection
        assert "own line collection" in res["message"]
        assert "rectangle collection" not in res["message"]

    def test_every_kind_resolves_a_collection_to_count(self, monkeypatch):
        # the gate is only real for a kind whose collection answers: a kind resolving to None draws
        # unverified and silently omits curves_added, which is exactly the defect this closes
        s = FakeSketch(); _install_draw(monkeypatch, s)
        unresolved = [k for k in sk._KINDS if sk._kind_curve_collection(s, k) is None]
        assert unresolved == [], f"kinds with no collection to count: {unresolved}"

    def test_a_polygon_counts_one_line_per_side(self, monkeypatch):
        s = FakeSketch(); _install_draw(monkeypatch, s)
        out = _first(sk.handler(geometry=[{"kind": "polygon", "cx": 0, "cy": 0, "radius": 10,
                                           "sides": 6}]))
        assert out["curves_added"] == 6

    def test_a_closed_path_counts_its_segments_including_the_closing_one(self, monkeypatch):
        s = FakeSketch(); _install_draw(monkeypatch, s)
        out = _first(sk.handler(geometry=[{"kind": "closed_path",
                                           "points": [[0, 0], [10, 0], [10, 10]]}]))
        assert out["curves_added"] == 3      # 3 points + the repeated first = 3 segments

    def test_the_plain_slot_gates_on_the_lines_it_lands(self, monkeypatch):
        # addCenterToCenterSlot is a Sketch method, but its curves land in the sketch's own
        # collections: 3 SketchLines (2 solid + the construction centre-to-centre line) and 2 arcs
        s = FakeSketch(); _install_draw(monkeypatch, s)
        assert sk._kind_curve_collection(s, "slot") is s.sketchLines
        out = _first(sk.handler(geometry=[{"kind": "slot", "x1": 0, "y1": 0, "x2": 20, "y2": 0,
                                           "radius": 3}]))
        assert out["curves_added"] == 3
        assert "2 solid SketchLines" in out["note"] and "CONSTRUCTION" in out["note"]

    def test_a_plain_slot_that_never_lands_is_an_error(self, monkeypatch):
        # the BaseVector return is not proof the curves reached the sketch - the delta is
        s = FakeSketch(); _install_draw(monkeypatch, s)
        monkeypatch.setattr(s, "addCenterToCenterSlot", lambda p1, p2, width: ["slot-entity"])
        res = sk.handler(geometry=[{"kind": "slot", "x1": 0, "y1": 0, "x2": 20, "y2": 0,
                                    "radius": 3}])
        assert res["isError"] is True and "did not change" in res["message"]

    def test_a_point_counts_against_the_sketch_points_collection(self, monkeypatch):
        # 'point' is the ref kind that lives on the sketch itself, not under sketchCurves
        s = FakeSketch(); _install_draw(monkeypatch, s)
        out = _first(sk.handler(geometry=[{"kind": "point", "cx": 5, "cy": 5}]))
        assert out["curves_added"] == 1
        assert sk._kind_curve_collection(s, "point") is s.sketchPoints

    def test_the_conic_family_kinds_resolve_their_own_collection(self, monkeypatch):
        # conic/elliptical_arc are ref tokens _common resolves, so they count in their own
        # collection rather than falling through to None
        s = FakeSketch(); _install_draw(monkeypatch, s)
        assert sk._kind_curve_collection(s, "conic") is s.sketchConicCurves
        assert sk._kind_curve_collection(s, "elliptical_arc") is s.sketchEllipticalArcs


class TestConic:

    """SketchConicCurves.add(startPoint, endPoint, apexPoint, rhoValue) - the apex rides on cx,cy,
    and the binding states rhoValue must be greater than zero and less than one."""

    def test_points_in_binding_order_and_scaled_to_cm(self, monkeypatch):
        s = FakeSketch(); _install_draw(monkeypatch, s)
        _payload(sk.handler(geometry=[{"kind": "conic", "x1": 0, "y1": 0, "x2": 20, "y2": 0,
                                       "cx": 10, "cy": 10, "rho": 0.5}], units="mm"))
        tag, start, end, apex, rho = s.sketchConicCurves.last
        assert tag == "add"
        assert (start.x, end.x, apex.x, apex.y) == (0.0, 2.0, 1.0, 1.0)   # mm -> cm
        assert rho == 0.5

    def test_rho_of_one_is_refused(self, monkeypatch):
        s = FakeSketch(); _install_draw(monkeypatch, s)
        res = sk.handler(geometry=[{"kind": "conic", "x1": 0, "y1": 0, "x2": 20, "y2": 0,
                                    "cx": 10, "cy": 10, "rho": 1.0}])
        assert res["isError"] is True and "less than 1" in res["message"]
        assert s.sketchConicCurves.count == 0

    def test_rho_of_zero_is_refused(self, monkeypatch):
        s = FakeSketch(); _install_draw(monkeypatch, s)
        res = sk.handler(geometry=[{"kind": "conic", "x1": 0, "y1": 0, "x2": 20, "y2": 0,
                                    "cx": 10, "cy": 10, "rho": 0.0}])
        assert res["isError"] is True and "greater than 0" in res["message"]

    def test_missing_rho_is_named(self, monkeypatch):
        s = FakeSketch(); _install_draw(monkeypatch, s)
        res = sk.handler(geometry=[{"kind": "conic", "x1": 0, "y1": 0, "x2": 20, "y2": 0,
                                    "cx": 10, "cy": 10}])
        assert res["isError"] is True and "rho" in res["message"]

    def test_curve_count_delta_is_reported(self, monkeypatch):
        s = FakeSketch(); _install_draw(monkeypatch, s)
        out = _first(sk.handler(geometry=[{"kind": "conic", "x1": 0, "y1": 0, "x2": 20, "y2": 0,
                                           "cx": 10, "cy": 10, "rho": 0.4}]))
        assert out["curves_added"] == 1

    def test_note_states_the_ref_and_the_profile_that_works(self, monkeypatch):
        s = FakeSketch(); _install_draw(monkeypatch, s)
        out = _first(sk.handler(geometry=[{"kind": "conic", "x1": 0, "y1": 0, "x2": 20, "y2": 0,
                                           "cx": 10, "cy": 10, "rho": 0.4}]))
        assert "'conic:<index>'" in out["note"]
        assert "chord" in out["note"] and "extrudes" in out["note"]

    def test_a_curve_that_never_lands_is_an_error(self, monkeypatch):
        # the factory hands back an object but the sketch's own collection does not grow: a false
        # success, so the tool must report failure
        s = FakeSketch(); _install_draw(monkeypatch, s)
        s.sketchConicCurves.add = lambda *a: object()
        res = sk.handler(geometry=[{"kind": "conic", "x1": 0, "y1": 0, "x2": 20, "y2": 0,
                                    "cx": 10, "cy": 10, "rho": 0.4}])
        assert res["isError"] is True and "did not change" in res["message"]


class TestControlPointSpline:

    """SketchControlPointSplines.add(controlPoints: list[Base], degree) - a plain LIST (the fitted
    spline is the one taking an ObjectCollection), and only degree 3 or 5 at creation."""

    def _clamping_add(self, coll):
        """A created spline carrying BOTH degree surfaces the live one has: `.degree` answers the
        REQUESTED degree, `.geometry.degree` the degree the NURBS curve was built at (clamped to
        the control-point count minus one). A payload reading the property echoes the request."""
        import adsk.fusion
        real = coll.add

        def _add(pts, degree):
            sp = real(pts, degree)
            sp.degree = 5 if degree is adsk.fusion.SplineDegrees.SplineDegreeFive else 3
            sp.geometry = SimpleNamespace(degree=min(sp.degree, len(pts) - 1))
            return sp
        coll.add = _add

    def test_control_points_are_a_plain_list_not_an_object_collection(self, monkeypatch):
        s = FakeSketch(); _install_draw(monkeypatch, s)
        _payload(sk.handler(geometry=[{"kind": "cv_spline",
                                       "points": [[0, 0], [10, 20], [20, 0]]}], units="mm"))
        tag, pts, _degree = s.sketchControlPointSplines.last
        assert tag == "add" and isinstance(pts, list) and len(pts) == 3
        assert (pts[1].x, pts[1].y) == (1.0, 2.0)          # mm -> cm

    def test_degree_defaults_to_three(self, monkeypatch):
        import adsk.fusion
        s = FakeSketch(); _install_draw(monkeypatch, s)
        _payload(sk.handler(geometry=[{"kind": "cv_spline",
                                       "points": [[0, 0], [1, 1], [2, 0]]}]))
        _tag, _pts, degree = s.sketchControlPointSplines.last
        assert degree is adsk.fusion.SplineDegrees.SplineDegreeThree

    def test_degree_five_maps_to_its_own_member(self, monkeypatch):
        import adsk.fusion
        s = FakeSketch(); _install_draw(monkeypatch, s)
        _payload(sk.handler(geometry=[{"kind": "cv_spline",
                                       "points": [[0, 0], [1, 1], [2, 0]], "degree": 5}]))
        _tag, _pts, degree = s.sketchControlPointSplines.last
        assert degree is adsk.fusion.SplineDegrees.SplineDegreeFive

    def test_degree_four_is_refused(self, monkeypatch):
        s = FakeSketch(); _install_draw(monkeypatch, s)
        res = sk.handler(geometry=[{"kind": "cv_spline", "points": [[0, 0], [1, 1]],
                                    "degree": 4}])
        assert res["isError"] is True and "3 or 5" in res["message"]
        assert s.sketchControlPointSplines.count == 0

    def test_note_names_the_ref_kind_it_is_addressed_by(self, monkeypatch):
        s = FakeSketch(); _install_draw(monkeypatch, s)
        out = _first(sk.handler(geometry=[{"kind": "cv_spline",
                                           "points": [[0, 0], [1, 1], [2, 0]]}]))
        assert "cv_spline" in out["note"]

    def test_too_few_points_is_refused(self, monkeypatch):
        s = FakeSketch(); _install_draw(monkeypatch, s)
        res = sk.handler(geometry=[{"kind": "cv_spline", "points": [[0, 0]]}])
        assert res["isError"] is True and "at least 2" in res["message"]

    def test_a_clamped_degree_is_published_and_named_in_the_note(self, monkeypatch):
        s = FakeSketch(); _install_draw(monkeypatch, s)
        self._clamping_add(s.sketchControlPointSplines)
        out = _first(sk.handler(geometry=[{"kind": "cv_spline",
                                           "points": [[0, 0], [1, 1], [2, 0]], "degree": 5}]))
        sp = s.sketchControlPointSplines.item(0)
        assert (sp.degree, sp.geometry.degree) == (5, 2)   # the request vs the built curve
        assert out["degree"] == 2                          # the BUILT curve, never the property
        assert "5" in out["note"] and "2" in out["note"]

    def test_an_honored_degree_is_published_without_a_clamp_warning(self, monkeypatch):
        s = FakeSketch(); _install_draw(monkeypatch, s)
        self._clamping_add(s.sketchControlPointSplines)
        out = _first(sk.handler(geometry=[{"kind": "cv_spline",
                                           "points": [[0, 0], [1, 1], [2, 2], [3, 0]],
                                           "degree": 3}]))
        assert out["degree"] == 3
        assert "clamp" not in out["note"]

    def test_an_unreadable_degree_is_omitted_rather_than_echoed(self, monkeypatch):
        # the fake spline carries no 'degree' - the payload must not report the REQUEST as if it
        # had been read back off the curve
        s = FakeSketch(); _install_draw(monkeypatch, s)
        out = _first(sk.handler(geometry=[{"kind": "cv_spline",
                                           "points": [[0, 0], [1, 1], [2, 0]], "degree": 5}]))
        assert "degree" not in out


class TestEllipticalArc:

    """SketchEllipticalArcs.addByAngle(center, majorAxis, minorAxis, startAngle, sweepAngle) - each
    axis vector's MAGNITUDE is that radius, the minor axis is perpendicular to the major, and both
    angles are radians measured from the major axis (positive counterclockwise)."""

    def test_axis_vectors_carry_the_radii_and_are_perpendicular(self, monkeypatch):
        s = FakeSketch(); _install_draw(monkeypatch, s)
        _payload(sk.handler(geometry=[{"kind": "elliptical_arc", "cx": 0, "cy": 0, "radius": 20,
                                       "minor": 5, "sweep_deg": 90}], units="mm"))
        tag, center, major, minor, _start, _sweep = s.sketchEllipticalArcs.last
        assert tag == "elliptical_arc"
        assert (major.x, major.y) == (2.0, 0.0)     # 20mm major -> 2cm along +X
        assert (minor.x, minor.y) == (0.0, 0.5)     # 5mm minor -> 0.5cm along +Y, perpendicular
        assert (center.x, center.y) == (0.0, 0.0)

    def test_angles_converted_to_radians(self, monkeypatch):
        import math
        s = FakeSketch(); _install_draw(monkeypatch, s)
        _payload(sk.handler(geometry=[{"kind": "elliptical_arc", "cx": 0, "cy": 0, "radius": 10,
                                       "sweep_deg": 90, "start_deg": 45}]))
        _tag, _c, _maj, _min, start, sweep = s.sketchEllipticalArcs.last
        assert abs(start - math.pi / 4) < 1e-9
        assert abs(sweep - math.pi / 2) < 1e-9

    def test_start_angle_defaults_to_zero(self, monkeypatch):
        s = FakeSketch(); _install_draw(monkeypatch, s)
        _payload(sk.handler(geometry=[{"kind": "elliptical_arc", "cx": 0, "cy": 0, "radius": 10,
                                       "sweep_deg": 180}]))
        _tag, _c, _maj, _min, start, _sweep = s.sketchEllipticalArcs.last
        assert start == 0.0

    def test_minor_defaults_to_half_the_major(self, monkeypatch):
        s = FakeSketch(); _install_draw(monkeypatch, s)
        _payload(sk.handler(geometry=[{"kind": "elliptical_arc", "cx": 0, "cy": 0, "radius": 20,
                                       "sweep_deg": 90}], units="mm"))
        _tag, _c, _maj, minor, _start, _sweep = s.sketchEllipticalArcs.last
        assert minor.y == 1.0                       # (20/2)mm -> 1cm

    def test_label_reports_the_minor_radius_it_drew(self, monkeypatch):
        # 'minor' omitted -> major/2 is DRAWN, so the label must state 10, not the raw None
        s = FakeSketch(); _install_draw(monkeypatch, s)
        out = _first(sk.handler(geometry=[{"kind": "elliptical_arc", "cx": 0, "cy": 0,
                                           "radius": 20, "sweep_deg": 90}]))
        assert "minor=10" in out["label"]

    def test_label_reports_an_explicit_minor(self, monkeypatch):
        s = FakeSketch(); _install_draw(monkeypatch, s)
        out = _first(sk.handler(geometry=[{"kind": "elliptical_arc", "cx": 0, "cy": 0,
                                           "radius": 20, "minor": 5, "sweep_deg": 90}]))
        assert "minor=5" in out["label"]

    def test_note_states_the_ref_and_the_profile_that_works(self, monkeypatch):
        s = FakeSketch(); _install_draw(monkeypatch, s)
        out = _first(sk.handler(geometry=[{"kind": "elliptical_arc", "cx": 0, "cy": 0,
                                           "radius": 20, "sweep_deg": 180}]))
        assert "'elliptical_arc:<index>'" in out["note"]
        assert "profile" in out["note"] and "extrudes" in out["note"]

    def test_zero_radius_is_refused(self, monkeypatch):
        s = FakeSketch(); _install_draw(monkeypatch, s)
        res = sk.handler(geometry=[{"kind": "elliptical_arc", "cx": 0, "cy": 0, "radius": 0,
                                    "sweep_deg": 90}])
        assert res["isError"] is True and "radius must be > 0" in res["message"]

    def test_negative_minor_is_refused_naming_the_value(self, monkeypatch):
        s = FakeSketch(); _install_draw(monkeypatch, s)
        res = sk.handler(geometry=[{"kind": "elliptical_arc", "cx": 0, "cy": 0, "radius": 10,
                                    "minor": -2, "sweep_deg": 90}])
        assert res["isError"] is True and "-2" in res["message"]

    def test_missing_sweep_is_named(self, monkeypatch):
        s = FakeSketch(); _install_draw(monkeypatch, s)
        res = sk.handler(geometry=[{"kind": "elliptical_arc", "cx": 0, "cy": 0, "radius": 10}])
        assert res["isError"] is True and "sweep_deg" in res["message"]


class TestParsePoints:

    def test_list_pairs(self):
        pts, err = sk._parse_points([[0, 0], [1, 2]])
        assert err is None and pts == [(0.0, 0.0), (1.0, 2.0)]

    def test_dict_pairs(self):
        pts, err = sk._parse_points([{"x": 1, "y": 2}, {"x": 3, "y": 4}])
        assert err is None and pts == [(1.0, 2.0), (3.0, 4.0)]

    def test_too_few_points(self):
        pts, err = sk._parse_points([[0, 0]])
        assert pts is None and "at least 2" in err

    def test_malformed_pair(self):
        pts, err = sk._parse_points([[0, 0], ["bad"]])
        assert pts is None and "points[1]" in err

    def test_not_a_list(self):
        pts, err = sk._parse_points(None)
        assert pts is None and "points" in err


class TestClosedPathDelegation:

    """closed_path DELEGATES to the repeated-first-point polyline shape and WELDS the seam: the
    closing segment ends ON the first segment's start point, so the loop shares that point and
    needs no closing constraint. MEASURED live: a welded 4-point loop carries 4 distinct endpoints
    where a repeated coordinate carries 5, and a profile forms either way."""

    def test_closed_path_adds_no_closing_coincident(self, monkeypatch):
        s = FakeSketch(); _install_draw(monkeypatch, s)
        out = _first(sk.handler(geometry=[{"kind": "closed_path",
                                           "points": [[0, 0], [1, 0], [1, 1]]}]))
        assert "(closed)" in out["label"]
        assert len(s.geometricConstraints.added) == 0     # no explicit closing constraint

    def test_closed_path_survives_a_solver_rejecting_constraint(self, monkeypatch):
        # Even with the geometric-constraint API set to reject every addCoincident, closed_path
        # SUCCEEDS: it does not route through that call, so the solver failure can't fire and leave
        # a partial chain (the non-atomicity defect this delegation guards against).
        s = FakeSketch(); _install_draw(monkeypatch, s)
        s.geometricConstraints.raise_on_add = True
        out = _first(sk.handler(geometry=[{"kind": "closed_path",
                                           "points": [[0, 0], [1, 0], [1, 1]]}]))
        assert "(closed)" in out["label"]

    def test_the_closing_segment_ends_ON_the_first_segments_start_point(self, monkeypatch):
        # THE WELD: the last addByTwoPoints must be handed the first line's OWN start point, not a
        # fresh coordinate at the same place - that duplicate point is the seam defect.
        s = FakeSketch(); _install_draw(monkeypatch, s)
        _payload(sk.handler(geometry=[{"kind": "closed_path",
                                       "points": [[0, 0], [1, 0], [1, 1]]}]))
        drawn = s.sketchLines._items
        assert len(drawn) == 3
        assert drawn[-1].endSketchPoint is drawn[0].startSketchPoint

    def test_a_plain_polyline_leaves_its_ends_apart(self, monkeypatch):
        # the boundary the weld must not cross: polyline is an OPEN chain, so its last segment
        # ends on its own point, never on the first segment's.
        s = FakeSketch(); _install_draw(monkeypatch, s)
        _payload(sk.handler(geometry=[{"kind": "polyline",
                                       "points": [[0, 0], [1, 0], [1, 1], [0, 0]]}]))
        drawn = s.sketchLines._items
        assert drawn[-1].endSketchPoint is not drawn[0].startSketchPoint

    def test_the_note_says_the_seam_is_welded(self, monkeypatch):
        s = FakeSketch(); _install_draw(monkeypatch, s)
        out = _first(sk.handler(geometry=[{"kind": "closed_path",
                                           "points": [[0, 0], [1, 0], [1, 1]]}]))
        assert "WELDED" in out["note"] and "no closing coincident" in out["note"]

    def test_closed_path_repeats_first_point_for_the_closing_segment(self, monkeypatch):
        # N points -> N segments (the appended first point closes the loop), matching the proven
        # polyline-with-repeated-point shape.
        s = FakeSketch(); _install_draw(monkeypatch, s)
        _payload(sk.handler(geometry=[{"kind": "closed_path",
                                       "points": [[0, 0], [2, 0], [2, 2], [0, 2]]}]))
        assert s.sketchLines.count == 4      # 4 points + repeated first = 5 pts -> 4 segments


class TestRectangleConstraints:

    """MEASURED live: addTwoPointRectangle and addCenterPointRectangle each land four lines and
    ZERO constraints, with or without deferred compute, while the UI's rectangle carries
    horizontal/vertical on its sides. The tool applies them and publishes the count that took."""

    def test_a_two_point_rectangle_gets_four_axis_constraints(self, monkeypatch):
        s = FakeSketch(); _install_draw(monkeypatch, s)
        out = _first(sk.handler(geometry=[{"kind": "rectangle", "x1": 0, "y1": 0, "x2": 40,
                                           "y2": 30}]))
        assert out["constraints_added"] == 4
        kinds = sorted(k for k, _ln in s.geometricConstraints.axis)
        assert kinds == ["horizontal", "horizontal", "vertical", "vertical"]

    def test_a_center_rectangle_gets_them_too(self, monkeypatch):
        s = FakeSketch(); _install_draw(monkeypatch, s)
        out = _first(sk.handler(geometry=[{"kind": "center_rectangle", "cx": 0, "cy": 0, "x2": 20,
                                           "y2": 15}]))
        assert out["constraints_added"] == 4
        assert len(s.geometricConstraints.axis) == 4

    def test_the_note_names_the_count_that_took(self, monkeypatch):
        s = FakeSketch(); _install_draw(monkeypatch, s)
        out = _first(sk.handler(geometry=[{"kind": "rectangle", "x1": 0, "y1": 0, "x2": 40,
                                           "y2": 30}]))
        assert "4 horizontal/vertical constraint(s)" in out["note"]

    def test_a_sketch_refusing_them_says_so_instead_of_claiming_them(self, monkeypatch):
        # the constraints are a best-effort addition on top of a SUCCESSFUL draw: when none takes,
        # the rectangle still lands and the note must not claim a constraint that is not there.
        s = FakeSketch(); _install_draw(monkeypatch, s)

        def _refuse(_line):
            raise RuntimeError("3 : constraint refused")
        s.geometricConstraints.addHorizontal = _refuse
        s.geometricConstraints.addVertical = _refuse
        out = _first(sk.handler(geometry=[{"kind": "rectangle", "x1": 0, "y1": 0, "x2": 40,
                                           "y2": 30}]))
        assert out["constraints_added"] == 0
        assert "NO horizontal/vertical constraint took" in out["note"]
        assert out["curves_added"] == 4          # the draw itself still succeeded

    def test_a_non_rectangle_kind_gets_no_axis_constraints(self, monkeypatch):
        s = FakeSketch(); _install_draw(monkeypatch, s)
        out = _first(sk.handler(geometry=[{"kind": "line", "x1": 0, "y1": 0, "x2": 10, "y2": 0}]))
        assert "constraints_added" not in out
        assert s.geometricConstraints.axis == []


class TestMarkConstructionHonesty:

    """_mark_recent_construction must raise (into the handler's try/except -> error()) on a failed
    isConstruction set, rather than swallow it in safe() and silently no-op while the caller reports
    success (is_construction implied applied) even though the flag never took."""

    def test_setattr_failure_propagates(self):
        class _BadCurve:
            @property
            def isConstruction(self):
                return False
            @isConstruction.setter
            def isConstruction(self, v):
                raise RuntimeError("isConstruction is locked on this curve")

        class _Curves:
            def __init__(self):
                self._items = [_BadCurve()]
            @property
            def count(self):
                return len(self._items)
            def item(self, i):
                return self._items[i]

        sketch = SimpleNamespace(sketchCurves=_Curves())
        import pytest
        with pytest.raises(RuntimeError, match="isConstruction is locked"):
            sk._mark_recent_construction(sketch, 0)


class TestMarkConstructionWindow:

    """before_count is an INDEX into sketchCurves, so the marking window is [before_count, count).
    A walk that renumbers the collection by POSITION slides that window onto curves that were
    already in the sketch - marking somebody else's geometry construction and missing a new curve."""

    def _curves(self, items):
        coll = SimpleNamespace(_items=list(items))
        coll.count = len(coll._items)
        coll.item = lambda i: coll._items[i]
        return SimpleNamespace(sketchCurves=coll)

    def test_only_the_curves_added_since_the_marker_are_marked(self):
        pre = [SimpleNamespace(isConstruction=False) for _ in range(3)]
        new = [SimpleNamespace(isConstruction=False) for _ in range(2)]
        sketch = self._curves(pre + new)
        sk._mark_recent_construction(sketch, 3)
        assert [c.isConstruction for c in pre] == [False, False, False]
        assert [c.isConstruction for c in new] == [True, True]

    def test_an_unreadable_earlier_curve_does_not_slide_the_window(self):
        # the pre-existing curve at index 1 reads back as nothing. Its slot still belongs to it, so
        # the window opening at 3 must still land on exactly the two curves that were just drawn.
        pre = [SimpleNamespace(isConstruction=False), None, SimpleNamespace(isConstruction=False)]
        new = [SimpleNamespace(isConstruction=False) for _ in range(2)]
        sketch = self._curves(pre + new)
        sk._mark_recent_construction(sketch, 3)
        assert [c.isConstruction for c in new] == [True, True]
        assert pre[0].isConstruction is False and pre[2].isConstruction is False


class TestBrokenChain:

    """A chain draw is NOT atomic: the segments before a failing one are already in the sketch. A
    refusal naming none of them reads as "nothing happened", so the caller retries the whole chain
    and draws those segments a second time."""

    @staticmethod
    def _fail_at(sketch, n, exc=None):
        """The nth (1-based) addByTwoPoints call hands back nothing - or raises `exc` - while the
        earlier calls land normally."""
        real = sketch.sketchLines.addByTwoPoints
        seen = {"n": 0}

        def _add(a, b):
            seen["n"] += 1
            if seen["n"] == n:
                if exc is not None:
                    raise exc
                return None
            return real(a, b)
        sketch.sketchLines.addByTwoPoints = _add

    _SQUARE = [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]      # 5 points -> 4 segments

    def test_a_mid_chain_failure_names_every_segment_that_landed(self, monkeypatch):
        s = FakeSketch(); _install_draw(monkeypatch, s)
        self._fail_at(s, 3)
        res = sk.handler(geometry=[{"kind": "polyline", "points": self._SQUARE}])
        assert res["isError"] is True
        assert "polyline segment 3 of 4 (10,10)->(0,10) did not draw" in res["message"]
        assert "The first 2 segment(s) DID land" in res["message"]
        assert "(0,0)->(10,0), (10,0)->(10,10)" in res["message"]
        assert "sketch_delete_entity" in res["message"]
        assert s.sketchLines.count == 2          # and they really are still in the sketch

    def test_the_second_segment_failing_names_exactly_the_one_that_landed(self, monkeypatch):
        # the boundary between "nothing landed" and the listing: one landed segment
        s = FakeSketch(); _install_draw(monkeypatch, s)
        self._fail_at(s, 2)
        res = sk.handler(geometry=[{"kind": "polyline", "points": self._SQUARE}])
        assert "The first 1 segment(s) DID land" in res["message"]
        assert "(0,0)->(10,0)." in res["message"]
        assert "(10,0)->(10,10)," not in res["message"]
        assert s.sketchLines.count == 1

    def test_the_first_segment_failing_says_nothing_landed(self, monkeypatch):
        # the other side of that boundary: no partial state to clean up, and no listing
        s = FakeSketch(); _install_draw(monkeypatch, s)
        self._fail_at(s, 1)
        res = sk.handler(geometry=[{"kind": "polyline", "points": self._SQUARE}])
        assert res["isError"] is True
        assert "No segment landed" in res["message"]
        assert "DID land" not in res["message"]
        assert s.sketchLines.count == 0

    def test_a_raising_segment_carries_fusions_message_beside_the_landed_ones(self, monkeypatch):
        s = FakeSketch(); _install_draw(monkeypatch, s)
        self._fail_at(s, 3, exc=RuntimeError("3 : VCS_SKETCH_SOLVING_FAILED"))
        res = sk.handler(geometry=[{"kind": "polyline", "points": self._SQUARE}])
        assert res["isError"] is True
        assert "VCS_SKETCH_SOLVING_FAILED" in res["message"]
        assert "The first 2 segment(s) DID land" in res["message"]

    def test_a_closed_path_failure_is_named_for_the_kind_that_was_asked_for(self, monkeypatch):
        # closed_path draws through the same chain, with the repeated first point as the last
        # segment - so its own kind and segment total are what the caller is told
        s = FakeSketch(); _install_draw(monkeypatch, s)
        self._fail_at(s, 3)
        res = sk.handler(geometry=[{"kind": "closed_path", "points": [[0, 0], [10, 0], [10, 10]]}])
        assert "closed_path segment 3 of 3 (10,10)->(0,0) did not draw" in res["message"]
        assert "The first 2 segment(s) DID land" in res["message"]

    def test_a_long_chain_caps_the_named_segments_and_counts_the_rest(self, monkeypatch):
        s = FakeSketch(); _install_draw(monkeypatch, s)
        pts = [[i, 0] for i in range(12)]        # 12 points -> 11 segments
        self._fail_at(s, 11)
        res = sk.handler(geometry=[{"kind": "polyline", "points": pts}])
        assert "The first 10 segment(s) DID land" in res["message"]
        assert res["message"].count("->") == 1 + sk._MAX_NAMED_SEGMENTS   # the failed one + the cap
        assert "(+4 more)" in res["message"]

    def test_a_broken_construction_chain_says_the_landed_segments_are_not_marked(self, monkeypatch):
        # is_construction runs after the chain completes, so the segments left behind are plain
        # geometry - claiming otherwise would send the caller looking for construction lines
        s = FakeSketch(); _install_draw(monkeypatch, s)
        self._fail_at(s, 3)
        res = sk.handler(geometry=[{"kind": "polyline", "points": self._SQUARE,
                                    "is_construction": True}])
        assert "They are plain geometry" in res["message"]
        assert [c.isConstruction for c in s.sketchLines._items] == [False, False]

    def test_a_complete_chain_reports_no_partial_state(self, monkeypatch):
        s = FakeSketch(); _install_draw(monkeypatch, s)
        out = _first(sk.handler(geometry=[{"kind": "polyline", "points": self._SQUARE}]))
        assert out["curves_added"] == 4


class TestComputeDeferredRestore:

    """The draw defers compute and restores it in a finally. A restore that raises leaves the sketch
    DEFERRED - swallowed, the call reports a clean success over a sketch whose profiles can read
    stale."""

    def test_a_refused_restore_is_disclosed_on_the_success(self, monkeypatch):
        s = _DeferStuck(); _install_draw(monkeypatch, s)
        out = _first(sk.handler(geometry=[{"kind": "circle", "cx": 0, "cy": 0, "radius": 5}]))
        assert out["compute_deferred"] is True
        assert "compute DEFERRED" in out["note"]
        assert "cannot be resumed" in out["note"]
        assert s.isComputeDeferred is True          # the state the disclosure describes

    def test_a_clean_restore_carries_no_disclosure(self, monkeypatch):
        s = FakeSketch(); _install_draw(monkeypatch, s)
        out = _first(sk.handler(geometry=[{"kind": "circle", "cx": 0, "cy": 0, "radius": 5}]))
        assert "compute_deferred" not in out
        assert "DEFERRED" not in out["note"]
        assert s.isComputeDeferred is False

    def test_a_refused_restore_is_appended_to_a_draw_failure(self, monkeypatch):
        # the draw error is the headline, but the deferred sketch is state the caller must know
        s = _DeferStuck(); _install_draw(monkeypatch, s)
        monkeypatch.setattr(s.sketchCircles, "addByCenterRadius", lambda c, r: None)
        res = sk.handler(geometry=[{"kind": "circle", "cx": 0, "cy": 0, "radius": 5}])
        assert res["isError"] is True
        assert "returned no entity" in res["message"]
        assert "compute DEFERRED" in res["message"]

    def test_a_refused_restore_is_appended_to_a_broken_chain(self, monkeypatch):
        s = _DeferStuck(); _install_draw(monkeypatch, s)
        TestBrokenChain._fail_at(s, 2)
        res = sk.handler(geometry=[{"kind": "polyline", "points": [[0, 0], [1, 0], [2, 0]]}])
        assert "The first 1 segment(s) DID land" in res["message"]
        assert "compute DEFERRED" in res["message"]


class TestPolyline:

    def test_open_polyline_segment_count(self, monkeypatch):
        s = FakeSketch(); _install_draw(monkeypatch, s)
        out = _first(sk.handler(geometry=[{"kind": "polyline",
                                           "points": [[0, 0], [1, 0], [1, 1]]}]))
        # 3 points -> 2 segments, no closing segment
        assert "3 pts, 2 segments" in out["label"]
        assert "(closed)" not in out["label"]
        assert s.sketchLines.count == 2

    def test_closed_path_adds_closing_segment(self, monkeypatch):
        s = FakeSketch(); _install_draw(monkeypatch, s)
        out = _first(sk.handler(geometry=[{"kind": "closed_path",
                                           "points": [[0, 0], [1, 0], [1, 1]]}]))
        # 3 points + closing -> 3 segments, labelled closed
        assert "3 segments (closed)" in out["label"]
        assert s.sketchLines.count == 3


class TestTargetSketch:

    def test_named_sketch_resolved(self, monkeypatch):
        s = FakeSketch("Named"); _install_draw(monkeypatch, s)
        got, requested = sk._common.resolve_or_recent_sketch(sk._common.design(), "Named")
        assert got is s and requested == "Named"

    def test_default_is_most_recent(self, monkeypatch):
        s = FakeSketch("Only"); _install_draw(monkeypatch, s)
        got, requested = sk._common.resolve_or_recent_sketch(sk._common.design(), "")
        assert got is s and requested is None

    def test_missing_named_sketch_errors(self, monkeypatch):
        s = FakeSketch("Real"); _install_draw(monkeypatch, s)
        res = sk.handler(geometry=[{"kind": "circle", "cx": 0, "cy": 0, "radius": 5}],
                         sketch_name="Ghost")
        assert res["isError"] is True and "Ghost" in res["message"]


class TestSharedSketchNameRefused:

    _REFUSAL = "2 sketches are named 'S' ('S' in Root, 'S' in Frame)"

    def test_add_geometry_refuses_with_its_owners(self, monkeypatch):
        _install_draw(monkeypatch, FakeSketch("S"))
        monkeypatch.setattr(sk._common, "find_or_recent_sketch",
                            lambda d, n, remedy=None: (None, n, self._REFUSAL))
        res = sk.handler(geometry=[{"kind": "circle", "cx": 0, "cy": 0, "radius": 5}],
                         sketch_name="S")
        assert res["isError"] is True
        assert res["message"] == self._REFUSAL and "No sketch named" not in res["message"]

    def test_a_name_no_sketch_carries_still_says_not_found(self, monkeypatch):
        # The refusal must not swallow the ordinary miss - they are different readings.
        _install_draw(monkeypatch, FakeSketch("S"))
        monkeypatch.setattr(sk._common, "find_or_recent_sketch",
                            lambda d, n, remedy=None: (None, n, None))
        res = sk.handler(geometry=[{"kind": "circle", "cx": 0, "cy": 0, "radius": 5}],
                         sketch_name="Nope")
        assert res["isError"] is True and "No sketch named 'Nope'" in res["message"]


class TestSketchNameReporting:

    def test_a_padded_name_is_reported_as_the_name_that_was_SEARCHED(self, monkeypatch):
        # The resolver strips before it walks, so reporting the raw argument would name a string
        # nothing was ever looked up under.
        _install_draw(monkeypatch, FakeSketch("Real"))
        res = sk.handler(geometry=[{"kind": "circle", "cx": 0, "cy": 0, "radius": 5}],
                         sketch_name="  Ghost  ")
        assert res["isError"] is True
        assert "No sketch named 'Ghost'" in res["message"]

    def test_add_geometry_blank_name_with_no_sketch_says_nothing_to_draw_on(self, monkeypatch):
        # 'requested' is None for a blank name, and that is the ONLY thing separating this message
        # from the named one - without it the wire reads "No sketch named 'None'".
        _install_draw(monkeypatch, None)
        res = sk.handler(geometry=[{"kind": "circle", "cx": 0, "cy": 0, "radius": 5}],
                         sketch_name="")
        assert res["isError"] is True
        assert "No sketch to draw on" in res["message"] and "No sketch named" not in res["message"]

    def test_a_whitespace_only_name_falls_back_to_the_most_recent_sketch(self, monkeypatch):
        # " " strips to empty, so it means "the most recent sketch", NOT a search for " ".
        _install_draw(monkeypatch, FakeSketch("Only"))
        out = _payload(sk.handler(geometry=[{"kind": "circle", "cx": 0, "cy": 0, "radius": 5}],
                                  sketch_name=" "))
        assert out["sketch"] == "Only"          # the ONE sketch the batch resolved, top-level


class TestDrawComponentScope:

    def test_unscoped_shared_name_still_refuses_and_names_the_scope_input(self, shared_name):
        # The refusal must not weaken to a first-match now a scope exists, and its way forward must
        # be an input this tool ACCEPTS - "rename one" is a change to another document.
        _d, alpha, beta = shared_name
        res = sk.handler(geometry=[{"kind": "circle", "cx": 0, "cy": 0, "radius": 5}],
                         sketch_name="Sketch1")
        assert res["isError"] is True
        assert "2 sketches are named 'Sketch1'" in res["message"]
        assert "'component'" in res["message"] and "Rename one" not in res["message"]
        assert alpha.sketchCircles.count == 0 and beta.sketchCircles.count == 0

    def test_the_scope_draws_into_THAT_components_sketch_and_no_other(self, shared_name):
        _d, alpha, beta = shared_name
        out = _first(sk.handler(geometry=[{"kind": "circle", "cx": 1, "cy": 2, "radius": 5}],
                                sketch_name="Sketch1", component="Beta"))
        assert out["label"] == "circle c=(1,2) r=5"
        assert beta.sketchCircles.count == 1 and alpha.sketchCircles.count == 0

    def test_the_other_component_is_reachable_by_the_same_call(self, shared_name):
        # The pair proves the scope SELECTS rather than always answering the first hit.
        _d, alpha, beta = shared_name
        _payload(sk.handler(geometry=[{"kind": "circle", "cx": 1, "cy": 2, "radius": 5}],
                            sketch_name="Sketch1", component="Alpha"))
        assert alpha.sketchCircles.count == 1 and beta.sketchCircles.count == 0

    def test_a_component_the_design_does_not_hold_is_refused(self, shared_name):
        _d, alpha, beta = shared_name
        res = sk.handler(geometry=[{"kind": "circle", "cx": 0, "cy": 0, "radius": 5}],
                         sketch_name="Sketch1", component="Gamma")
        assert res["isError"] is True and "No component named 'Gamma'" in res["message"]
        assert alpha.sketchCircles.count == 0 and beta.sketchCircles.count == 0

    def test_a_scope_that_does_not_hold_the_sketch_is_refused_naming_who_does(self, monkeypatch):
        alpha = FakeSketch("Plate")
        design = _install_scoped(monkeypatch, [("Alpha", [alpha]), ("Beta", [])])
        assert design.allComponents.itemByName("Beta") is not None
        res = sk.handler(geometry=[{"kind": "circle", "cx": 0, "cy": 0, "radius": 5}],
                         sketch_name="Plate", component="Beta")
        assert res["isError"] is True
        assert "'Beta'" in res["message"] and "'Alpha'" in res["message"]
        assert alpha.sketchCircles.count == 0

    def test_a_wrong_component_is_refused_even_when_the_sketch_name_is_UNIQUE(self, monkeypatch):
        # The decision this pins: a scope that was passed is VALIDATED, never dropped because the
        # name happened to identify one sketch on its own. An input a caller can get wrong without
        # being told is a trap - the draw would land in Alpha while the call said Beta.
        alpha = FakeSketch("OnlyOne")
        _install_scoped(monkeypatch, [("Alpha", [alpha]), ("Beta", [])])
        res = sk.handler(geometry=[{"kind": "circle", "cx": 0, "cy": 0, "radius": 5}],
                         sketch_name="OnlyOne", component="Beta")
        assert res["isError"] is True
        assert alpha.sketchCircles.count == 0

    def test_a_blank_name_with_a_scope_takes_THAT_components_most_recent_sketch(self, monkeypatch):
        # A scope silently ignored here would draw into the ACTIVE component instead of the named
        # one - the same wrong-sketch write the scope exists to prevent.
        first, second = FakeSketch("A1"), FakeSketch("B2")
        _install_scoped(monkeypatch, [("Alpha", [first]), ("Beta", [second])])
        out = _payload(sk.handler(geometry=[{"kind": "circle", "cx": 0, "cy": 0, "radius": 5}],
                                  sketch_name="", component="Beta"))
        assert out["sketch"] == "B2"
        assert second.sketchCircles.count == 1 and first.sketchCircles.count == 0

    def test_a_blank_name_scoped_to_a_component_with_no_sketches_is_refused(self, monkeypatch):
        _install_scoped(monkeypatch, [("Alpha", [FakeSketch("A1")]), ("Beta", [])])
        res = sk.handler(geometry=[{"kind": "circle", "cx": 0, "cy": 0, "radius": 5}],
                         sketch_name="", component="Beta")
        assert res["isError"] is True and "holds no sketches" in res["message"]


class TestBatch:

    """The entries of one call draw in order against the ONE resolved sketch and the first failure
    stops the run: what landed stays in the sketch, so the payload names the entry that failed and
    counts the ones never attempted rather than reading as "nothing happened"."""

    _BAD_POLYGON = {"kind": "polygon", "cx": 0, "cy": 0, "radius": 5, "sides": 2}

    def test_two_entries_land_each_carrying_its_index(self, monkeypatch):
        s = FakeSketch(); _install_draw(monkeypatch, s)
        out = _payload(sk.handler(geometry=[
            {"kind": "circle", "cx": 0, "cy": 0, "radius": 5},
            {"kind": "line", "x1": 0, "y1": 0, "x2": 10, "y2": 0}]))
        assert (out["drawn"], out["requested"]) == (2, 2)
        assert [r["index"] for r in out["results"]] == [0, 1]
        assert [r["kind"] for r in out["results"]] == ["circle", "line"]
        assert s.sketchCircles.count == 1 and s.sketchLines.count == 1

    def test_a_second_entry_that_fails_leaves_the_first_one_drawn(self, monkeypatch):
        s = FakeSketch(); _install_draw(monkeypatch, s)
        out = _payload(sk.handler(geometry=[
            {"kind": "circle", "cx": 0, "cy": 0, "radius": 5},
            self._BAD_POLYGON,
            {"kind": "line", "x1": 0, "y1": 0, "x2": 10, "y2": 0}]))
        assert (out["drawn"], out["requested"]) == (1, 3)
        assert out["failed"] == {"index": 1, "error": "polygon needs sides >= 3."}
        assert out["not_attempted"] == 1
        assert "polygon needs sides >= 3." in out["note"]
        # the circle really is in the sketch, and the third entry never ran
        assert s.sketchCircles.count == 1 and s.sketchLines.count == 0

    def test_a_first_entry_that_fails_is_an_error_naming_it(self, monkeypatch):
        s = FakeSketch(); _install_draw(monkeypatch, s)
        res = sk.handler(geometry=[self._BAD_POLYGON,
                                   {"kind": "circle", "cx": 0, "cy": 0, "radius": 5}])
        assert res["isError"] is True
        assert "geometry[0]: polygon needs sides >= 3." in res["message"]
        assert "Nothing landed." in res["message"]
        assert s.sketchCircles.count == 0        # the run stopped before the later entry

    def test_an_unknown_field_in_an_entry_is_refused_by_name(self, monkeypatch):
        # a top-level input written into an entry: the guard names it instead of dropping it
        s = FakeSketch(); _install_draw(monkeypatch, s)
        res = sk.handler(geometry=[{"kind": "circle", "cx": 0, "cy": 0, "radius": 5,
                                    "sketch_name": "S"}])
        assert res["isError"] is True
        assert "geometry[0]" in res["message"] and "sketch_name" in res["message"]
        assert s.sketchCircles.count == 0

    def test_an_empty_list_is_refused(self, monkeypatch):
        s = FakeSketch(); _install_draw(monkeypatch, s)
        res = sk.handler(geometry=[])
        assert res["isError"] is True and "non-empty list" in res["message"]
