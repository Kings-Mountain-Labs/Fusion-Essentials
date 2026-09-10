"""Unit tests for ``sketch_edit_curve.py`` - trim/extend/break/split/fillet/chamfer/offset on an
existing sketch curve.

Pinned: the action + units + reference guards, the positional pick-point contract (display units ->
centimetres), the per-action API arguments, the empty-result refusals and their named causes, and
the SketchCurvesChanged postcondition.
"""

import math

import adsk.core
import pytest

from conftest import (FakeBoundingBox3D, FakePoint, MakeComp, assert_no_active_design,
                      assert_unknown_units, error_message,
                      install, load_tool, make_design, make_sketch, make_sketch_curve, payload,
                      sketch_curves_edit)


def _result(*curves):
    """An ObjectCollection carrying the curves an edit returned."""
    coll = adsk.core.ObjectCollection.create()
    for curve in curves:
        coll.add(curve)
    return coll


def _extends(line, new_length):
    """A faithful SketchLine.extend: it lengthens the curve IN PLACE and returns an EMPTY
    collection whether or not anything moved, so the length is the only evidence it worked."""
    def _extend(point, create_constraints=True):
        line.length = new_length
        return _result()
    return _extend


def _recorder(store, returns):
    """A stub that records its positional arguments and returns ``returns``."""
    def _call(*args):
        store.append(args)
        return returns
    return _call


@pytest.fixture
def mod():
    return load_tool("sketch_edit_curve")


@pytest.fixture
def sketch(mod, monkeypatch):
    """A 'Plate' sketch holding line:0 (10 cm) and line:1 (5 cm), wired into the tool module."""
    line0 = make_sketch_curve("L0", length=10.0)
    line1 = make_sketch_curve("L1", length=5.0)
    sk = make_sketch("Plate", lines=[line0, line1])
    install(mod, make_design(sketches=[sk]))
    monkeypatch.setattr(adsk.core.Point3D, "create", lambda x, y, z: FakePoint(x, y, z))
    return sk


def _lines(sketch):
    return sketch.sketchCurves.sketchLines


class TestGuards:
    def test_unknown_action_names_the_valid_actions(self, mod, sketch):
        res = mod.handler(action="fillit", entity_one="line:0", x1=0, y1=0)
        msg = error_message(res)
        assert "'action' must be one of" in msg and "fillet" in msg and "offset" in msg

    def test_unknown_units_is_refused(self, mod, sketch):
        assert_unknown_units(mod.handler, action="trim", entity_one="line:0", x1=1, y1=1)

    def test_no_active_design_is_a_clean_error(self, mod, sketch):
        assert_no_active_design(mod, mod.handler, action="trim", entity_one="line:0", x1=1, y1=1)

    def test_named_sketch_not_found_lists_the_available_names(self, mod, sketch):
        res = mod.handler(action="trim", sketch_name="Nope", entity_one="line:0", x1=1, y1=1)
        msg = error_message(res)
        assert "No sketch named 'Nope'" in msg and "Plate" in msg

    def test_a_shared_sketch_name_is_refused_with_its_owners(self, mod, sketch, monkeypatch):
        # Two components can each hold a "Plate". The refusal names them and nothing is trimmed;
        # calling it "No sketch named 'Plate'" states the opposite of what the walk read.
        refusal = "2 sketches are named 'Plate' ('Plate' in Root, 'Plate' in Frame)"
        monkeypatch.setattr(mod._common, "find_or_recent_sketch",
                            lambda d, n, remedy=None: (None, n, refusal))
        trimmed = []
        monkeypatch.setattr(_lines(sketch).item(0), "trim", lambda p: trimmed.append(p),
                            raising=False)
        msg = error_message(mod.handler(action="trim", sketch_name="Plate",
                                        entity_one="line:0", x1=1, y1=1))
        assert msg == refusal and "No sketch named" not in msg
        assert trimmed == []          # no curve was touched

    def test_the_available_list_qualifies_a_name_two_components_share(self, mod, monkeypatch):
        # Bare, the list read "Available: Plate, Plate" - two sketches in two components printed as
        # one name twice, which reads as a duplicated entry rather than as two different sketches.
        alpha = MakeComp(name="Alpha", sketches=[make_sketch("Plate")])
        beta = MakeComp(name="Beta", sketches=[make_sketch("Plate")])
        install(mod, make_design(comp=alpha, all_components=[alpha, beta]))
        monkeypatch.setattr(adsk.core.Point3D, "create", lambda x, y, z: FakePoint(x, y, z))
        msg = error_message(mod.handler(action="trim", sketch_name="Nope", entity_one="line:0",
                                        x1=1, y1=1))
        assert "Available: Plate (Alpha), Plate (Beta)" in msg

    def test_a_design_with_no_sketches_lists_none(self, mod, monkeypatch):
        install(mod, make_design(sketches=[]))
        monkeypatch.setattr(adsk.core.Point3D, "create", lambda x, y, z: FakePoint(x, y, z))
        msg = error_message(mod.handler(action="trim", sketch_name="Nope", entity_one="line:0",
                                        x1=1, y1=1))
        assert "Available: (none)" in msg

    def test_a_padded_name_reports_the_stripped_name_it_searched_for(self, mod, sketch):
        # The resolver strips before searching, so the miss must name what was searched for -
        # echoing the raw input tells the caller a name that was never looked up.
        msg = error_message(mod.handler(action="trim", sketch_name="  Ghost  ",
                                        entity_one="line:0", x1=1, y1=1))
        assert "No sketch named 'Ghost'" in msg and "'  Ghost  '" not in msg

    def test_a_blank_name_with_no_sketch_says_so_instead_of_naming_none(self, mod, monkeypatch):
        # A blank name means "the most recent sketch", so the requested name is None - a caller
        # that formats it into the named-miss branch puts the literal 'None' on the wire.
        install(mod, make_design(sketches=[]))
        monkeypatch.setattr(adsk.core.Point3D, "create", lambda x, y, z: FakePoint(x, y, z))
        msg = error_message(mod.handler(action="trim", sketch_name="", entity_one="line:0",
                                        x1=1, y1=1))
        assert msg == "No sketch to edit. Draw one first with sketch_create + sketch_add_geometry."
        assert "'None'" not in msg

    def test_a_whitespace_only_name_takes_the_most_recent_sketch(self, mod, monkeypatch):
        # " " strips to blank, so it is the most-recent-sketch request - not a search for a sketch
        # literally named with a space.
        line = make_sketch_curve("L0", length=10.0)
        last = make_sketch("Last", lines=[line])
        install(mod, make_design(sketches=[make_sketch("First"), last]))
        monkeypatch.setattr(adsk.core.Point3D, "create", lambda x, y, z: FakePoint(x, y, z))
        short = make_sketch_curve("L2", length=4.0)

        def _trim(point, create_constraints=True):
            sketch_curves_edit(last.sketchCurves.sketchLines, remove=[line], add=[short])
            return _result(short)

        line.trim = _trim
        out = payload(mod.handler(action="trim", sketch_name=" ", entity_one="line:0", x1=1, y1=1))
        assert out["sketch"] == "Last"

    def test_missing_entity_one_is_refused(self, mod, sketch):
        msg = error_message(mod.handler(action="trim", x1=1, y1=1))
        assert "'entity_one' is required" in msg and "<type>:<index>" in msg

    def test_out_of_range_reference_names_the_ref_kinds(self, mod, sketch):
        msg = error_message(mod.handler(action="trim", entity_one="line:7", x1=1, y1=1))
        assert "did not resolve 'line:7'" in msg and "fixed_spline" in msg

    def test_missing_pick_point_is_refused(self, mod, sketch):
        msg = error_message(mod.handler(action="trim", entity_one="line:0", x1=1))
        assert "pick point x1,y1" in msg

    def test_fillet_without_entity_two_is_refused(self, mod, sketch):
        msg = error_message(mod.handler(action="fillet", entity_one="line:0", x1=1, y1=1,
                                        radius=2))
        assert "'entity_two' is required" in msg

    def test_two_curve_action_needs_the_second_pick_point(self, mod, sketch):
        msg = error_message(mod.handler(action="fillet", entity_one="line:0", entity_two="line:1",
                                        x1=1, y1=1, radius=2))
        assert "x2,y2" in msg and "quadrant" in msg

    def test_fillet_needs_a_positive_radius(self, mod, sketch):
        msg = error_message(mod.handler(action="fillet", entity_one="line:0", entity_two="line:1",
                                        x1=1, y1=1, x2=2, y2=2, radius=0))
        assert "fillet needs 'radius' > 0" in msg and "got 0" in msg

    def test_fillet_refuses_a_circle(self, mod, monkeypatch):
        circle = make_sketch_curve("C0", length=6.28)
        line = make_sketch_curve("L0", length=10.0)
        sk = make_sketch("Plate", lines=[line], circles=[circle])
        install(mod, make_design(sketches=[sk]))
        monkeypatch.setattr(adsk.core.Point3D, "create", lambda x, y, z: FakePoint(x, y, z))
        msg = error_message(mod.handler(action="fillet", entity_one="circle:0",
                                        entity_two="line:0", x1=1, y1=1, x2=2, y2=2, radius=2))
        assert "fillet needs OPEN curves" in msg and "'circle:0' is closed" in msg

    def test_fillet_refuses_a_closed_spline(self, mod, monkeypatch):
        spline = make_sketch_curve("S0", length=20.0, is_closed=True)
        line = make_sketch_curve("L0", length=10.0)
        sk = make_sketch("Plate", lines=[line], splines=[spline])
        install(mod, make_design(sketches=[sk]))
        monkeypatch.setattr(adsk.core.Point3D, "create", lambda x, y, z: FakePoint(x, y, z))
        msg = error_message(mod.handler(action="fillet", entity_one="line:0",
                                        entity_two="spline:0", x1=1, y1=1, x2=2, y2=2, radius=2))
        assert "'entity_two' = 'spline:0' is closed" in msg

    def test_fillet_accepts_an_open_spline(self, mod, monkeypatch):
        spline = make_sketch_curve("S0", length=20.0, is_closed=False)
        line = make_sketch_curve("L0", length=10.0)
        sk = make_sketch("Plate", lines=[line], splines=[spline])
        install(mod, make_design(sketches=[sk]))
        monkeypatch.setattr(adsk.core.Point3D, "create", lambda x, y, z: FakePoint(x, y, z))
        arc = make_sketch_curve("A0", length=1.5)

        def _add_fillet(*args):
            sketch_curves_edit(sk.sketchCurves.sketchArcs, add=[arc])
            return arc

        sk.sketchCurves.sketchArcs.addFillet = _add_fillet
        out = payload(mod.handler(action="fillet", entity_one="line:0", entity_two="spline:0",
                                  x1=1, y1=1, x2=2, y2=2, radius=2))
        assert out["resulting"] == [{"id": "arc:0", "length": 15.0}]

    def test_chamfer_refuses_a_non_line(self, mod, monkeypatch):
        arc = make_sketch_curve("A0", length=3.0)
        line = make_sketch_curve("L0", length=10.0)
        sk = make_sketch("Plate", lines=[line], arcs=[arc])
        install(mod, make_design(sketches=[sk]))
        monkeypatch.setattr(adsk.core.Point3D, "create", lambda x, y, z: FakePoint(x, y, z))
        msg = error_message(mod.handler(action="chamfer", entity_one="line:0", entity_two="arc:0",
                                        x1=1, y1=1, x2=2, y2=2, distance=1))
        assert "chamfer joins two straight LINES" in msg and "'entity_two' = 'arc:0' is a arc" in msg

    def test_chamfer_refuses_both_distance_two_and_angle(self, mod, sketch):
        msg = error_message(mod.handler(action="chamfer", entity_one="line:0", entity_two="line:1",
                                        x1=1, y1=1, x2=2, y2=2, distance=1, distance_two=2,
                                        angle_deg=30))
        assert "EITHER 'distance_two'" in msg and "not both" in msg

    def test_chamfer_refuses_an_out_of_range_angle(self, mod, sketch):
        msg = error_message(mod.handler(action="chamfer", entity_one="line:0", entity_two="line:1",
                                        x1=1, y1=1, x2=2, y2=2, distance=1, angle_deg=180))
        assert "'angle_deg' must be between 0 and 180" in msg

    def test_offset_needs_a_positive_distance(self, mod, sketch):
        msg = error_message(mod.handler(action="offset", entity_one="line:0", x1=1, y1=1,
                                        distance=-3))
        assert "offset needs 'distance' > 0" in msg and "Got -3" in msg


# ── the 'component' SCOPE ────────────────────────────────────────────────────
# Fusion numbers sketches per component from 1, so two components each holding a "Plate" is the
# norm. The design-wide refusal is right to refuse - but until this scope existed its only way
# forward was renaming a sketch, which for a component that arrived inside a referenced document
# means editing a DIFFERENT document. These drive the REAL walk, not a stubbed resolver.

@pytest.fixture
def shared_name(mod, monkeypatch):
    """One name, two components, DIFFERENT contents: Alpha's 'Plate' holds two lines, Beta's holds
    one. Equal-sized collections would hide a swapped source, and two different NAMES would resolve
    design-wide without the filter ever running."""
    a0, a1 = make_sketch_curve("A0", length=10.0), make_sketch_curve("A1", length=4.0)
    b0 = make_sketch_curve("B0", length=7.0)
    alpha_sk, beta_sk = make_sketch("Plate", lines=[a0, a1]), make_sketch("Plate", lines=[b0])
    alpha = MakeComp(name="Alpha", sketches=[alpha_sk])
    beta = MakeComp(name="Beta", sketches=[beta_sk])
    install(mod, make_design(comp=alpha, all_components=[alpha, beta]))
    monkeypatch.setattr(adsk.core.Point3D, "create", lambda x, y, z: FakePoint(x, y, z))
    return alpha_sk, beta_sk


def _trim_recorder(monkeypatch, sketch, store):
    """Record WHICH sketch's line:0 was trimmed - the discriminating read, since both sketches
    answer to one name and only the object identity says which one the call reached."""
    monkeypatch.setattr(_lines(sketch).item(0), "trim",
                        lambda p, s=sketch: (store.append(s), _result())[1], raising=False)


class TestComponentScope:
    def test_the_unscoped_shared_name_refuses_and_names_the_scope_input(self, mod, shared_name,
                                                                        monkeypatch):
        alpha_sk, beta_sk = shared_name
        touched = []
        _trim_recorder(monkeypatch, alpha_sk, touched)
        _trim_recorder(monkeypatch, beta_sk, touched)
        msg = error_message(mod.handler(action="trim", sketch_name="Plate",
                                        entity_one="line:0", x1=1, y1=1))
        assert "2 sketches are named 'Plate'" in msg
        assert "'component'" in msg and "Rename one" not in msg
        assert touched == []

    def test_the_scope_edits_THAT_components_curve(self, mod, shared_name, monkeypatch):
        alpha_sk, beta_sk = shared_name
        touched = []
        _trim_recorder(monkeypatch, alpha_sk, touched)
        _trim_recorder(monkeypatch, beta_sk, touched)
        mod.handler(action="trim", sketch_name="Plate", component="Beta",
                    entity_one="line:0", x1=1, y1=1)
        assert touched == [beta_sk]

    def test_the_sibling_component_is_reachable_by_the_same_call(self, mod, shared_name,
                                                                 monkeypatch):
        alpha_sk, beta_sk = shared_name
        touched = []
        _trim_recorder(monkeypatch, alpha_sk, touched)
        _trim_recorder(monkeypatch, beta_sk, touched)
        mod.handler(action="trim", sketch_name="Plate", component="Alpha",
                    entity_one="line:0", x1=1, y1=1)
        assert touched == [alpha_sk]

    def test_an_unknown_component_is_refused(self, mod, shared_name, monkeypatch):
        alpha_sk, _beta_sk = shared_name
        touched = []
        _trim_recorder(monkeypatch, alpha_sk, touched)
        msg = error_message(mod.handler(action="trim", sketch_name="Plate", component="Gamma",
                                        entity_one="line:0", x1=1, y1=1))
        assert "No component named 'Gamma'" in msg and touched == []

    def test_a_wrong_component_is_refused_even_when_the_name_is_UNIQUE(self, mod, monkeypatch):
        # The scope is VALIDATED rather than dropped: otherwise the trim lands in Alpha on a call
        # that named Beta, and nothing tells the caller.
        a0 = make_sketch_curve("A0", length=10.0)
        alpha_sk = make_sketch("OnlyOne", lines=[a0])
        alpha = MakeComp(name="Alpha", sketches=[alpha_sk])
        beta = MakeComp(name="Beta", sketches=[])
        install(mod, make_design(comp=alpha, all_components=[alpha, beta]))
        monkeypatch.setattr(adsk.core.Point3D, "create", lambda x, y, z: FakePoint(x, y, z))
        touched = []
        _trim_recorder(monkeypatch, alpha_sk, touched)
        msg = error_message(mod.handler(action="trim", sketch_name="OnlyOne", component="Beta",
                                        entity_one="line:0", x1=1, y1=1))
        assert "'Beta'" in msg and touched == []


class TestSingleCurveEdits:
    def test_trim_scales_the_pick_point_to_centimetres(self, mod, sketch):
        picks = []
        short = make_sketch_curve("L2", length=4.0)

        def _trim(point, create_constraints=True):
            picks.append((point.x, point.y, point.z))
            sketch_curves_edit(_lines(sketch), remove=[_lines(sketch).item(0)],
                               add=[short])
            return _result(short)

        _lines(sketch).item(0).trim = _trim
        payload(mod.handler(action="trim", entity_one="line:0", x1=25.0, y1=-40.0))
        assert picks == [(2.5, -4.0, 0.0)]

    def test_trim_reports_the_replacement_curve_id(self, mod, sketch):
        short = make_sketch_curve("L2", length=4.0)

        def _trim(point, create_constraints=True):
            sketch_curves_edit(_lines(sketch), remove=[_lines(sketch).item(0)],
                               add=[short])
            return _result(short)

        _lines(sketch).item(0).trim = _trim
        out = payload(mod.handler(action="trim", entity_one="line:0", x1=1, y1=1))
        assert out["curve_count_before"] == 2 and out["curve_count_after"] == 2
        assert out["resulting"] == [{"id": "line:1", "length": 40.0}]
        assert out["action"] == "trim" and out["sketch"] == "Plate"

    def test_trim_that_changes_nothing_is_an_error(self, mod, sketch):
        _lines(sketch).item(0).trim = lambda point, create_constraints=True: _result()
        msg = error_message(mod.handler(action="trim", entity_one="line:0", x1=1, y1=1))
        assert "trim returned no curves" in msg and "a pick point on a segment" in msg
        assert "still holds 2 curve(s)" in msg

    def test_trim_that_consumed_the_whole_curve_is_reported(self, mod, sketch):
        def _trim(point, create_constraints=True):
            sketch_curves_edit(_lines(sketch), remove=[_lines(sketch).item(0)])
            return _result()

        _lines(sketch).item(0).trim = _trim
        out = payload(mod.handler(action="trim", entity_one="line:0", x1=1, y1=1))
        assert out["curve_count_before"] == 2 and out["curve_count_after"] == 1
        assert out["resulting"] == []
        assert "The whole curve was consumed" in out["note"]

    def test_break_with_no_crossings_names_the_cause(self, mod, sketch):
        _lines(sketch).item(0).breakCurve = lambda point, create_constraints=True: _result()
        msg = error_message(mod.handler(action="break", entity_one="line:0", x1=1, y1=1))
        assert "returned no curves" in msg and "crosses another curve" in msg

    def test_split_of_a_closed_curve_names_the_cause(self, mod, sketch):
        _lines(sketch).item(0).split = lambda point, create_constraints=True: _result()
        msg = error_message(mod.handler(action="split", entity_one="line:0", x1=1, y1=1))
        assert "returned no curves" in msg and "an OPEN curve" in msg

    def test_extend_reports_the_lengthened_original(self, mod, sketch):
        line0 = _lines(sketch).item(0)

        def _extend(point, create_constraints=True):
            line0.length = 13.0
            return _result(line0)

        line0.extend = _extend
        out = payload(mod.handler(action="extend", entity_one="line:0", x1=1, y1=1))
        assert out["resulting"] == [{"id": "line:0", "length": 130.0}]
        assert out["curve_count_before"] == 2 and out["curve_count_after"] == 2

    def test_lengths_are_reported_in_the_requested_units(self, mod, sketch):
        line0 = _lines(sketch).item(0)
        line0.extend = _extends(line0, 13.0)
        out = payload(mod.handler(action="extend", entity_one="line:0", x1=1, y1=1, units="in"))
        assert out["units"] == "in"
        assert out["resulting"] == [{"id": "line:0", "length": round(13.0 / 2.54, 4)}]

    def test_an_api_failure_surfaces_as_an_error(self, mod, sketch):
        def _boom(point, create_constraints=True):
            raise RuntimeError("3 : invalid argument segmentPoint")

        _lines(sketch).item(0).trim = _boom
        msg = error_message(mod.handler(action="trim", entity_one="line:0", x1=1, y1=1))
        assert "Could not trim 'line:0' in sketch 'Plate'" in msg
        assert "invalid argument segmentPoint" in msg

    def test_a_break_that_raises_still_states_what_break_needs(self, mod, sketch):
        # breakCurve RAISES "Break is not available for this segment point" on a curve nothing
        # crosses - it does not return an empty collection - so the requirement has to ride the
        # raised-error path or the caller never learns what break wants.
        def _boom(point, create_constraints=True):
            raise RuntimeError("3 : Break is not available for this segment point")

        _lines(sketch).item(0).breakCurve = _boom
        msg = error_message(mod.handler(action="break", entity_one="line:0", x1=1, y1=1))
        assert "Break is not available for this segment point" in msg
        assert "'break' needs a curve that crosses another curve in the sketch." in msg

    def test_an_action_with_no_requirement_entry_adds_no_tail(self, mod, sketch):
        def _boom(*a, **k):
            raise RuntimeError("3 : invalid argument")

        sketch.sketchCurves.sketchArcs.addFillet = _boom
        msg = error_message(mod.handler(action="fillet", entity_one="line:0", entity_two="line:1",
                                        x1=1, y1=1, x2=2, y2=2, radius=1))
        assert "needs" not in msg


class TestFilletChamferOffset:
    def test_fillet_passes_both_pick_points_and_a_centimetre_radius(self, mod, sketch):
        calls = []
        arc = make_sketch_curve("A0", length=1.5)
        sketch.sketchCurves.sketchArcs.addFillet = _recorder(calls, arc)
        out = payload(mod.handler(action="fillet", entity_one="line:0", entity_two="line:1",
                                  x1=10, y1=0, x2=0, y2=20, radius=3))
        first, p1, second, p2, radius_cm = calls[0]
        assert (p1.x, p1.y) == (1.0, 0.0) and (p2.x, p2.y) == (0.0, 2.0)
        assert radius_cm == pytest.approx(0.3)
        assert first is _lines(sketch).item(0) and second is _lines(sketch).item(1)
        assert out["entity_two"] == "line:1"

    def test_a_fillet_that_returns_nothing_is_an_error(self, mod, sketch):
        sketch.sketchCurves.sketchArcs.addFillet = _recorder([], None)
        msg = error_message(mod.handler(action="fillet", entity_one="line:0", entity_two="line:1",
                                        x1=1, y1=0, x2=0, y2=1, radius=3))
        assert "fillet returned no curves" in msg and "nothing changed" in msg

    def test_chamfer_defaults_the_second_setback_to_the_first(self, mod, sketch):
        calls = []
        chamfer = make_sketch_curve("L2", length=1.0)
        _lines(sketch).addDistanceChamfer = _recorder(calls, chamfer)
        payload(mod.handler(action="chamfer", entity_one="line:0", entity_two="line:1",
                            x1=1, y1=0, x2=0, y2=1, distance=4))
        assert calls[0][4] == pytest.approx(0.4) and calls[0][5] == pytest.approx(0.4)

    def test_chamfer_takes_an_explicit_second_setback(self, mod, sketch):
        calls = []
        chamfer = make_sketch_curve("L2", length=1.0)
        _lines(sketch).addDistanceChamfer = _recorder(calls, chamfer)
        payload(mod.handler(action="chamfer", entity_one="line:0", entity_two="line:1",
                            x1=1, y1=0, x2=0, y2=1, distance=4, distance_two=6))
        assert calls[0][4] == pytest.approx(0.4) and calls[0][5] == pytest.approx(0.6)

    def test_chamfer_angle_form_passes_radians(self, mod, sketch):
        calls = []
        chamfer = make_sketch_curve("L2", length=1.0)
        _lines(sketch).addAngleChamfer = _recorder(calls, chamfer)
        payload(mod.handler(action="chamfer", entity_one="line:0", entity_two="line:1",
                            x1=1, y1=0, x2=0, y2=1, distance=4, angle_deg=30))
        assert calls[0][4] == pytest.approx(0.4)
        assert calls[0][5] == pytest.approx(math.radians(30))

    def test_offset_feeds_the_connected_chain(self, mod, sketch):
        calls = []
        new_line = make_sketch_curve("L2", length=10.0)
        chain = _result(_lines(sketch).item(0), _lines(sketch).item(1))
        sketch.findConnectedCurves = lambda curve: chain
        sketch.offset = _recorder(calls, _result(new_line))
        out = payload(mod.handler(action="offset", entity_one="line:0", x1=5, y1=5, distance=2))
        curves, direction, distance_cm = calls[0]
        assert curves is chain and distance_cm == pytest.approx(0.2)
        assert (direction.x, direction.y) == (0.5, 0.5)
        assert out["source_curve_count"] == 2

    def test_offset_falls_back_to_the_single_curve_when_no_chain_is_found(self, mod, sketch):
        calls = []
        new_line = make_sketch_curve("L2", length=10.0)
        sketch.findConnectedCurves = lambda curve: _result()
        sketch.offset = _recorder(calls, _result(new_line))
        out = payload(mod.handler(action="offset", entity_one="line:0", x1=5, y1=5, distance=2))
        assert calls[0][0].count == 1 and calls[0][0].item(0) is _lines(sketch).item(0)
        assert out["source_curve_count"] == 1


class TestDownstreamHealth:
    def test_a_feature_broken_by_the_edit_is_named(self, mod, sketch, monkeypatch):
        health = iter([([], [], 3), (["Extrude1"], [], 3)])
        monkeypatch.setattr(mod._common, "timeline_health", lambda design: next(health))
        line0 = _lines(sketch).item(0)
        line0.extend = _extends(line0, 13.0)
        out = payload(mod.handler(action="extend", entity_one="line:0", x1=1, y1=1))
        assert out["downstream_broken"] == ["Extrude1"]
        assert "no longer compute" in out["note"]

    def test_a_feature_already_broken_before_the_edit_is_not_blamed(self, mod, sketch,
                                                                   monkeypatch):
        health = iter([(["Extrude1"], [], 3), (["Extrude1"], [], 3)])
        monkeypatch.setattr(mod._common, "timeline_health", lambda design: next(health))
        line0 = _lines(sketch).item(0)
        line0.extend = _extends(line0, 13.0)
        out = payload(mod.handler(action="extend", entity_one="line:0", x1=1, y1=1))
        assert "downstream_broken" not in out
        assert "no longer compute" not in out["note"]


class TestSketchCurvesChangedPostcondition:
    def test_the_postcondition_reads_the_same_component_scope_the_handler_edits_in(self, mod):
        # the handler narrows its by-name resolve with 'component'; a fingerprint that did not
        # would read a different sketch whenever that name is shared, and disclose an unconfirmed
        # change over an edit that landed.
        post, = mod.item.handler.__wrapped__.__assert_postconditions__
        assert post.keys == ("sketch_name",) and post.scope_keys == ("component",)

    def test_an_unchanged_curve_set_is_reported_as_a_no_op(self, mod, sketch):
        kind = load_tool("_assert").SketchCurvesChanged()
        before = kind.capture({"sketch_name": "Plate"})
        reason, evidence = kind.verify({"sketch_name": "Plate"}, {}, before)
        assert "the sketch's entities are unchanged" in reason and evidence == {}

    def test_an_in_place_length_change_is_detected(self, mod, sketch):
        kind = load_tool("_assert").SketchCurvesChanged()
        before = kind.capture({"sketch_name": "Plate"})
        _lines(sketch).item(0).length = 12.0
        reason, evidence = kind.verify({"sketch_name": "Plate"}, {}, before)
        assert reason == "" and evidence == {"curve_count_after": 2}

    def test_a_same_count_identity_swap_is_detected(self, mod, sketch):
        kind = load_tool("_assert").SketchCurvesChanged()
        before = kind.capture({"sketch_name": "Plate"})
        line0 = _lines(sketch).item(0)
        sketch_curves_edit(_lines(sketch), remove=[line0],
                           add=[make_sketch_curve("L9", length=10.0)])
        reason, evidence = kind.verify({"sketch_name": "Plate"}, {}, before)
        assert reason == "" and evidence == {"curve_count_after": 2}

    def test_a_pure_translation_registers_even_though_every_length_matches(self, mod, sketch):
        # a translated curve keeps its entityToken AND its length - only its position moves - so a
        # fingerprint of tokens and lengths alone reads a successful move as a no-op.
        line0 = _lines(sketch).item(0)
        line0.boundingBox = FakeBoundingBox3D(FakePoint(0.0, 0.0, 0.0), FakePoint(10.0, 0.0, 0.0))
        kind = load_tool("_assert").SketchCurvesChanged()
        before = kind.capture({"sketch_name": "Plate"})
        line0.boundingBox.minPoint.x += 2.0
        line0.boundingBox.maxPoint.x += 2.0
        reason, evidence = kind.verify({"sketch_name": "Plate"}, {}, before)
        assert reason == "" and evidence == {"curve_count_after": 2}

    def test_a_missing_sketch_leaves_the_verdict_open_but_says_so(self, mod, sketch):
        # a sketch that cannot be read on one side of the call is not a no-op verdict - the call is
        # not failed, but the payload carries the marker instead of passing in silence
        kind = load_tool("_assert").SketchCurvesChanged()
        reason, evidence = kind.verify({"sketch_name": "Gone"}, {}, None)
        assert reason == ""
        assert evidence == {"sketch_curves_confirmed": False}


class TestExtendIsJudgedByLength:
    def test_an_empty_return_is_success_when_the_curve_grew(self, mod, sketch):
        # extend returns an empty collection either way; only the length says whether it worked
        line0 = _lines(sketch).item(0)
        line0.extend = _extends(line0, 13.0)
        out = payload(mod.handler(action="extend", entity_one="line:0", x1=1, y1=1))
        assert out["resulting"] == [{"id": "line:0", "length": 130.0}]

    def test_an_unchanged_length_is_refused(self, mod, sketch):
        line0 = _lines(sketch).item(0)
        line0.extend = _extends(line0, 10.0)
        msg = error_message(mod.handler(action="extend", entity_one="line:0", x1=1, y1=1))
        assert "extend changed nothing" in msg

    def test_a_shortened_curve_is_refused(self, mod, sketch):
        line0 = _lines(sketch).item(0)
        line0.extend = _extends(line0, 4.0)
        assert "extend changed nothing" in error_message(
            mod.handler(action="extend", entity_one="line:0", x1=1, y1=1))
