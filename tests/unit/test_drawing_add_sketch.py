"""Unit tests for ``drawing_add_sketch.py`` - a sketch plus 2D geometry on a drawing sheet.

The shared drawing-sketch fake carries the measured contract, and each clause of it is a way this
tool can report a draw that did not happen:
  - ``Lines.add`` takes a LIST of Point2D and draws a connected CHAIN, so N points yield N-1 Line
    entities while the call returns a single Line - the landed count can only come from the
    collection diff;
  - coordinates are the drawing's OWN length units, never the centimetres the modelling tools scale
    to, so a factory that receives a scaled number silently draws the wrong size;
  - a created entity exposes nothing but cast/classType/isValid/objectType, so the per-collection
    ``count`` is the only read-back there is - a factory can hand back an object while the
    collection never grows.
"""

import types

import pytest

import adsk  # the mock package conftest installed at import time
from conftest import (FakeDrawingSketch, FakeDrawingSketches, FakeSheet, error_message, load_tool,
                      make_drawing, make_drawing_session, payload)

dw = load_tool("drawing_add_sketch")


def _point(x, y):
    """Stands in for Point2D.create: the pair the factory was handed, in the units it was handed."""
    return (round(float(x), 6), round(float(y), 6))


@pytest.fixture
def wire(monkeypatch):
    def _install(sheets=("Sheet1",), active=0, units="mm", is_drawing=True, landed_name=None,
                 no_sketches=False, standard="iso"):
        sketch = FakeDrawingSketch()
        objs = [FakeSheet(n, sketches=FakeDrawingSketches(sketch, landed_name=landed_name))
                for n in sheets]
        if no_sketches:
            for sheet in objs:
                sheet.sketches = None
        # units and standard are SEPARATE settings and a drawing can be minted with them split
        # (drawing_create takes each from its own Choice), which is exactly the case the coordinate
        # unit has to survive.
        document = make_drawing(sheets=objs, active=active, units=units, standard=standard,
                                name="Drw")
        make_drawing_session(monkeypatch, document if is_drawing else object())
        monkeypatch.setattr(adsk.core.Point2D, "create", _point)
        return types.SimpleNamespace(sketch=sketch, sheets=objs, drawing=document.drawing)
    return _install


_LINE = {"kind": "line", "points": [[10, 10], [40, 10], [40, 30]]}
_CIRCLE = {"kind": "circle", "points": [[60, 60]], "radius": 5}
_ARC = {"kind": "arc", "points": [[0, 0], [5, 5], [10, 0]]}


# ── the draw, read back off the sketch's own collections ─────────────────────

class TestDraw:
    def test_every_kind_lands_and_the_counts_are_read_back(self, wire):
        state = wire()
        out = payload(dw.handler(geometry=[
            _LINE,
            {"kind": "rectangle", "points": [[0, 0], [20, 15]]},
            _ARC,
            {"kind": "ellipse", "points": [[50, 50], [70, 50], [50, 60]]},
            _CIRCLE,
        ]))
        assert out["entities_drawn"] == 5
        assert out["curves_requested"] == 6          # the 3-point line chain counts as TWO
        assert out["curves_landed"] == 6
        assert out["landed"] == {"lines": 2, "rectangles": 1, "arcs": 1, "circles": 1, "ellipses": 1}
        assert [k for k, _ in state.sketch._drawn] == [
            "lines", "rectangles", "arcs", "ellipses", "circles"]

    def test_a_line_is_one_chain_of_all_its_points_not_a_call_per_segment(self, wire):
        # measured: Lines.add takes a LIST - N points make N-1 Line entities in ONE call, and the
        # call returns a single Line, so a per-segment loop would report the wrong entity count.
        state = wire()
        out = payload(dw.handler(geometry=[{"kind": "line",
                                            "points": [[0, 0], [10, 0], [10, 10], [0, 10]]}]))
        assert len(state.sketch._drawn) == 1
        key, args = state.sketch._drawn[0]
        assert key == "lines"
        assert args == ([(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)],)
        assert out["curves_requested"] == 3 and out["curves_landed"] == 3

    def test_coordinates_reach_the_factory_in_drawing_units_unscaled(self, wire):
        # drawing-sketch coordinates are sheet units, NOT the centimetres the modelling tools scale
        # to - a 0.1x conversion here would draw a tenth-size detail on the sheet.
        state = wire()
        payload(dw.handler(geometry=[{"kind": "circle", "points": [[120, 80]], "radius": 5}]))
        assert state.sketch._drawn == [("circles", ((120.0, 80.0), 5.0))]

    def test_the_sketch_name_published_is_the_one_the_sketch_reports(self, wire):
        state = wire(landed_name="Notes (1)")
        out = payload(dw.handler(geometry=[_CIRCLE], name="Notes"))
        assert state.sheets[0].sketches._requested == ["Notes"]
        assert out["sketch_name"] == "Notes (1)"          # read off the sketch, not echoed back

    def test_no_name_adds_the_sketch_with_no_argument(self, wire):
        # measured: the name argument is optional and a no-arg add auto-names the sketch; passing an
        # empty string instead is not the same call.
        state = wire()
        payload(dw.handler(geometry=[_CIRCLE]))
        assert state.sheets[0].sketches._requested == [None]

    def test_declared_outputs_present(self, wire):
        wire()
        out = payload(dw.handler(geometry=[_CIRCLE]))
        for r in dw.RETURNS:
            assert r.assert_present(out) == "", r.key


# ── honesty: a draw that did not land is never a false ok ────────────────────

class TestHonesty:
    def test_an_entity_handed_back_while_the_count_stands_still_is_an_error(self, wire):
        state = wire()
        state.sketch._silent.add("circles")
        msg = error_message(dw.handler(geometry=[_LINE, _CIRCLE]))
        assert "circles 0 of 1" in msg and "came up short" in msg
        assert "keeps the 2 curves" in msg               # the line segments that DID land stay

    def test_a_factory_returning_nothing_is_an_error(self, wire):
        state = wire()
        state.sketch._nothing.add("arcs")
        msg = error_message(dw.handler(geometry=[_ARC]))
        assert "geometry[0] ('arc') returned no entity." in msg
        assert ".." not in msg                           # the detail's own period is not doubled

    def test_a_raise_after_the_curve_landed_is_still_an_error(self, wire):
        # the failure gate stands on its own: the curve DID land, so no collection is short, and a
        # call that only checked the counts would return a clean ok for a factory that raised.
        state = wire()
        state.sketch._raises_after.add("arcs")
        msg = error_message(dw.handler(geometry=[_ARC]))
        assert state.sketch.arcs.count == 1               # nothing is short
        assert "geometry[0] ('arc') was refused" in msg and "locked" in msg
        assert "keeps the 1 curve already drawn" in msg   # singular, not "1 curves"

    def test_a_failure_on_the_first_entity_reports_the_sketch_as_empty(self, wire):
        state = wire()
        state.sketch._refuses.add("arcs")
        msg = error_message(dw.handler(geometry=[_ARC, _CIRCLE]))
        assert "Drew 0 of 2" in msg
        assert "That empty sketch stays behind" in msg    # never "the 0 curves already drawn"

    def test_a_refused_entity_names_it_and_reports_what_already_landed(self, wire):
        state = wire()
        state.sketch._refuses.add("arcs")
        msg = error_message(dw.handler(geometry=[_LINE, _ARC, _CIRCLE]))
        assert "Drew 1 of 3" in msg and "geometry[1] ('arc')" in msg and "locked" in msg
        assert "keeps the 2 curves" in msg               # what the sketch is left holding
        assert state.sketch.circles.count == 0           # the run stopped, it did not carry on

    def test_the_failure_names_the_measured_delete_route_not_an_absence(self, wire):
        # Drawing.deleteEntities EXISTS and raises 'not yet implemented' on a drawn curve; the wire
        # states that measured fact rather than claiming no delete call exists.
        state = wire()
        state.sketch._nothing.add("circles")
        msg = error_message(dw.handler(geometry=[_CIRCLE]))
        assert "Drawing.deleteEntities raises 'API Function not yet implemented'" in msg

    def test_a_sketch_that_could_not_be_added_is_an_error(self, wire):
        state = wire()
        state.sheets[0].sketches._returns_nothing = True
        assert "returned nothing" in error_message(dw.handler(geometry=[_CIRCLE]))


# ── the request is validated before anything is drawn ────────────────────────

class TestPlanFirst:
    def test_an_unknown_kind_is_refused_without_adding_a_sketch(self, wire):
        state = wire()
        msg = error_message(dw.handler(geometry=[_CIRCLE, {"kind": "spline", "points": [[0, 0]]}]))
        assert "geometry[1] has kind 'spline'" in msg and "ellipse" in msg
        assert state.sheets[0].sketches._requested == []
        assert state.sketch._drawn == []

    def test_a_wrong_point_count_names_the_entry_and_the_arity(self, wire):
        wire()
        msg = error_message(dw.handler(geometry=[{"kind": "arc", "points": [[0, 0], [1, 1]]}]))
        assert "geometry[0] ('arc') needs exactly 3 points" in msg and "Got 2" in msg

    def test_a_one_point_line_is_refused_as_too_short_a_chain(self, wire):
        wire()
        msg = error_message(dw.handler(geometry=[{"kind": "line", "points": [[0, 0]]}]))
        assert "at least 2 points" in msg and "Got 1" in msg

    def test_a_malformed_point_names_its_position(self, wire):
        wire()
        msg = error_message(dw.handler(geometry=[{"kind": "line", "points": [[0, 0], "x"]}]))
        assert "geometry[0].points[1] is not an [x, y] pair" in msg

    def test_a_point_object_is_refused_the_schema_form_is_the_pair(self, wire):
        # the declared contract is an [x, y] array; an {x, y} object is not silently accepted.
        state = wire()
        msg = error_message(dw.handler(geometry=[{"kind": "circle", "points": [{"x": 1, "y": 2}],
                                                  "radius": 3}]))
        assert "geometry[0].points[0] is not an [x, y] pair" in msg
        assert state.sketch._drawn == []

    def test_a_circle_needs_a_positive_numeric_radius(self, wire):
        wire()
        assert "needs a finite numeric 'radius'" in error_message(
            dw.handler(geometry=[{"kind": "circle", "points": [[0, 0]]}]))
        assert "greater than 0. Got -2.0" in error_message(
            dw.handler(geometry=[{"kind": "circle", "points": [[0, 0]], "radius": -2}]))

    def test_empty_geometry_is_refused(self, wire):
        wire()
        assert "non-empty list" in error_message(dw.handler(geometry=[]))


# ── which sheet ──────────────────────────────────────────────────────────────

class TestSheetTargeting:
    def test_no_name_draws_on_the_active_sheet(self, wire):
        state = wire(sheets=("Sheet1", "Sheet2"), active=1)
        out = payload(dw.handler(geometry=[_CIRCLE]))
        assert out["sheet_name"] == "Sheet2"
        assert state.sheets[1].sketches._requested == [None]
        assert state.sheets[0].sketches._requested == []

    def test_a_named_sheet_is_resolved_exactly(self, wire):
        state = wire(sheets=("Sheet1", "Sheet2"), active=0)
        out = payload(dw.handler(geometry=[_CIRCLE], sheet_name="Sheet2"))
        assert out["sheet_name"] == "Sheet2"
        assert state.sheets[1].sketches._requested == [None]

    def test_a_name_in_another_case_resolves_to_the_same_sheet(self, wire):
        # sheet names are measured case-INsensitively unique, so a case variant is the same sheet.
        state = wire(sheets=("Sheet1", "Detail A"), active=0)
        out = payload(dw.handler(geometry=[_CIRCLE], sheet_name="detail a"))
        assert out["sheet_name"] == "Detail A"
        assert state.sheets[1].sketches._requested == [None]

    def test_an_unknown_sheet_lists_the_sheets_instead_of_drawing(self, wire):
        state = wire(sheets=("Sheet1", "Sheet2"))
        msg = error_message(dw.handler(geometry=[_CIRCLE], sheet_name="Sheet9"))
        assert "No sheet named 'Sheet9'" in msg and "Sheet1, Sheet2" in msg
        assert state.sketch._drawn == []

    def test_a_sheet_with_no_sketches_collection_is_reported(self, wire):
        wire(no_sketches=True)
        assert "no sketches collection" in error_message(dw.handler(geometry=[_CIRCLE]))


# ── the drawing document and its units ───────────────────────────────────────

class TestDrawingDocument:
    def test_a_non_drawing_active_document_is_refused(self, wire):
        state = wire(is_drawing=False)
        msg = error_message(dw.handler(geometry=[_CIRCLE]))
        assert "the active document is not a drawing" in msg
        assert state.sketch._drawn == []

    def test_millimetre_and_inch_settings_are_reported_as_read(self, wire):
        wire(units="mm")
        assert payload(dw.handler(geometry=[_CIRCLE]))["sheet_units"] == "mm"
        wire(units="in")
        assert payload(dw.handler(geometry=[_CIRCLE]))["sheet_units"] == "in"

    def test_an_unreadable_unit_setting_is_null_not_guessed(self, wire):
        wire(units="???")
        out = payload(dw.handler(geometry=[_CIRCLE]))
        assert out["sheet_units"] is None
        assert out["coordinate_unit"] == "mm"          # the standard still reads


# ── the coordinate unit: keyed to the STANDARD, not to the dimension display unit ─────────────

class TestCoordinateUnit:
    def test_a_split_drawing_keys_coordinates_to_the_standard_not_the_dimension_unit(self, wire):
        # drawing_create's standard and units are separate Choices, so standard='iso' with
        # units='inch' is mintable. Coordinates are in drawing length units - millimetres when the
        # standard includes ISO - so the two fields disagree and the payload must say so.
        wire(standard="iso", units="in")
        out = payload(dw.handler(geometry=[_CIRCLE]))
        assert out["coordinate_unit"] == "mm"
        assert out["sheet_units"] == "in"
        assert "taken as mm" in out["note"]

    def test_an_asme_drawing_takes_coordinates_in_inches(self, wire):
        wire(standard="asme", units="mm")
        out = payload(dw.handler(geometry=[_CIRCLE]))
        assert out["coordinate_unit"] == "in"
        assert out["sheet_units"] == "mm"
        assert "taken as in" in out["note"]

    def test_an_unreadable_standard_is_null_and_the_note_states_the_rule(self, wire):
        wire(standard="???")
        out = payload(dw.handler(geometry=[_CIRCLE]))
        assert out["coordinate_unit"] is None
        assert "mm under ISO, in under ASME" in out["note"]

    def test_the_note_never_ties_the_coordinates_to_sheet_units(self, wire):
        wire(standard="iso", units="in")
        note = payload(dw.handler(geometry=[_CIRCLE]))["note"]
        assert "STANDARD fixes" in note
        assert "taken as in" not in note


class TestToolDescription:
    def test_the_geometry_input_keys_coordinates_to_the_standard(self):
        # the wire is all an agent has before the first call, so the input carrying the numbers
        # states the rule that decides whether a 100 lands as 100 mm or as 2540 mm.
        desc = dw.tool.to_dict()["inputSchema"]["properties"]["geometry"]["description"]
        assert "mm under ISO, in under ASME" in desc
        assert "coordinate_unit" in desc

    def test_an_arity_refusal_names_what_the_points_mean(self):
        # the per-kind point FORM is the claim the description no longer carries: an arc's three
        # points are not interchangeable, so the refusal an undercount meets spells them out
        message = dw._plan([{"kind": "arc", "points": [[0, 0], [1, 1]]}])[2]
        assert "start, a point on it, end" in message and "Got 2" in message


class TestStrictGeometryGuards:
    @pytest.mark.parametrize("bad,needle", [
        ({"kind": "circle", "points": [[float("nan"), 0]], "radius": 5}, "finite"),
        ({"kind": "circle", "points": [[float("inf"), 0]], "radius": 5}, "finite"),
        ({"kind": "circle", "points": [[0, 0]], "radius": float("nan")}, "finite"),
        ({"kind": "circle", "points": [["2.5", 0]], "radius": 5}, "finite"),
        ({"kind": "circle", "points": [[True, 0]], "radius": 5}, "finite"),
        ({"kind": "circle", "points": [[0, 0, 1]], "radius": 5}, "[x, y] pair"),
        ({"kind": "circle", "points": [[0, 0]], "radius": "5"}, "finite numeric"),
        ({"kind": "circle", "points": [[0, 0]], "radius": True}, "finite numeric"),
        ({"kind": "line", "points": [[0, 0], [0, 0], [10, 0]]}, "duplicate"),
        ({"kind": "rectangle", "points": [[0, 0], [0, 10]]}, "different x and y"),
        ({"kind": "rectangle", "points": [[0, 0], [10, 0]]}, "different x and y"),
        ({"kind": "arc", "points": [[0, 0], [0, 0], [10, 0]]}, "distinct"),
        ({"kind": "arc", "points": [[0, 0], [5, 0], [10, 0]]}, "collinear"),
        ({"kind": "ellipse", "points": [[0, 0], [0, 0], [0, 10]]}, "center distinct"),
        ({"kind": "ellipse", "points": [[0, 0], [10, 0], [0, 0]]}, "center distinct"),
    ])
    def test_invalid_row_after_valid_predecessor_is_rejected_before_mutation(
            self, wire, bad, needle):
        state = wire()
        msg = error_message(dw.handler(geometry=[_CIRCLE, bad]))
        assert "geometry[1]" in msg and needle in msg
        assert state.sheets[0].sketches._requested == []
        assert state.sketch._drawn == []

    def test_tiny_circle_and_arc_keep_unrounded_coordinates(self, wire, monkeypatch):
        state = wire()
        monkeypatch.setattr(adsk.core.Point2D, "create", lambda x, y: (x, y))
        out = payload(dw.handler(geometry=[
            {"kind": "circle", "points": [[1e-200, -1e-200]], "radius": 1e-200},
            {"kind": "arc", "points": [[0.0, 0.0], [1e-200, 1e-200], [2e-200, 0.0]]},
        ]))
        assert out["curves_landed"] == 2
        assert state.sketch._drawn[0][1] == ((1e-200, -1e-200), 1e-200)
        assert state.sketch._drawn[1][1] == ((0.0, 0.0), (1e-200, 1e-200), (2e-200, 0.0))

    def test_reversed_rectangle_and_collinear_nonzero_ellipse_are_valid(self, wire):
        state = wire()
        out = payload(dw.handler(geometry=[
            {"kind": "rectangle", "points": [[20, 15], [0, 0]]},
            {"kind": "ellipse", "points": [[0, 0], [10, 0], [5, 0]]},
        ]))
        assert out["curves_landed"] == 2
        assert [kind for kind, _ in state.sketch._drawn] == ["rectangles", "ellipses"]
