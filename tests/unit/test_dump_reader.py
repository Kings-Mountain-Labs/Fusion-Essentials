# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""The dump-post reader: what it parses out of a .dmp, and what its four verdicts refuse."""

import math
import os
import sys

import pytest

TESTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(TESTS_DIR, "live"))
import _dump_reader  # noqa: E402
import verify_acts_dump  # noqa: E402
import verify_acts_hub  # noqa: E402

# A synthetic dump with the shape the real one has: an unnumbered header, the parameter block, an
# unnumbered currentSection line, then the motion events each test appends.
_HEAD = "\n".join([
    "  Post Engine Version = 5.413.5",
    "-1: onOpen()",
    "0: onParameter('product-id', 'fusion360')",
    "16: onParameter('job-description', 'MillTop, second op')",
    "19: onParameter('stock', '((-10, -10, -20), (10, 10, 0))')",
    "21: onParameter('stock-lower-x', -10)",
    "23: onParameter('stock-lower-y', -10)",
    "25: onParameter('stock-lower-z', -20)",
    "27: onParameter('stock-upper-x', 10)",
    "29: onParameter('stock-upper-y', 10)",
    "31: onParameter('stock-upper-z', 0)",
    "33: onParameter('part-lower-x', -9)",
    "35: onParameter('part-lower-y', -9)",
    "37: onParameter('part-lower-z', -19)",
    "39: onParameter('part-upper-x', 9)",
    "41: onParameter('part-upper-y', 9)",
    "43: onParameter('part-upper-z', -1)",
    "50: onParameter('operation-strategy', 'moduleworks_multiaxis_finishing')",
    "80: onParameter('operation:metric', 1)",
    "97: onParameter('operation:tool_unit', 'millimeters')",
    "56: onSection()",
    "  currentSection.unit=1",
])
_TAIL = "\n".join(["2130: onSectionEnd()", "2130: onClose()"])

_RAPID = "559: onRapid5D(0, 0, 5, 0, 0, 1)"
_CUT = "563: onLinear5D(1, 2, -3, 0, 0.6, 0.8, 750, 2)"
_CORNER = "565: onLinear5D(10, 10, -20, 0, 0, 1, 750, 2)"
_PAST_X = "567: onLinear5D(11, 0, -3, 0, 0, 1, 750, 2)"
_BELOW_Z = "569: onLinear5D(0, 0, -21, 0, 0, 1, 750, 2)"
_TILT_DEG = math.degrees(math.acos(0.8))

# The 3-axis events in the shape a posted 2D contour states them: position then feed, and no tool
# axis at all. The arc states its direction flag and centre FIRST and its endpoint fourth, so its
# position sits where the others state their feed.
_CUT3 = "571: onLinear(1, 2, -3, 500)"
_RAPID3 = "573: onRapid(0, 0, 5)"
_THREE_AXIS = (_CUT3, _RAPID3)
_ARC3 = "575: onCircular(0, 0, 0, -3, 1, 2, -3, 500)"

# The rows a rotary wrap states: a tool axis square to +Z, and one leaning 3 degrees off square.
_SQUARE = "587: onLinear5D(1, 2, -3, 1, 0, 0, 750, 2)"
_SQUARE_RAPID = "589: onRapid5D(0, 0, 5, 0, 1, 0)"
_OFF_SQUARE = "591: onLinear5D(0, 0, -1, 0.99862953, 0, 0.05233596, 750, 2)"
_ZERO_AXIS = "593: onRapid5D(0, 0, 5, 0, 0, 0)"

# The sample the counts below were read from - the hub's Multi-Axis Finishing1 posted through
# dump.cps. It carries the poster's own account and document ids, so no copy lives in the repo;
# point FE_DUMP_SAMPLE at one to run that test.
_SAMPLE_ENV = "FE_DUMP_SAMPLE"


def _dump(*motion):
    """A parsed synthetic dump carrying the given motion event lines."""
    return _dump_reader.parse_dump("\n".join((_HEAD,) + motion + (_TAIL,)))


def _posted_dump(tmp_path, *motion):
    """A successful cam_post payload naming a synthetic dump file."""
    path = tmp_path / "posted.dmp"
    path.write_text("\n".join((_HEAD,) + motion + (_TAIL,)), encoding="utf-8")
    return {
        "posted": True,
        "program_name": "4002",
        "file_count": 1,
        "files": [{"file_path": str(path), "size_bytes": path.stat().st_size}],
    }


class TestParse:
    def test_a_parameter_value_holding_commas_survives_the_split(self):
        dump = _dump(_CUT)
        assert dump.parameters["stock"] == "((-10, -10, -20), (10, 10, 0))"
        assert dump.parameters["job-description"] == "MillTop, second op"
        assert dump.parameters["product-id"] == "fusion360"

    def test_the_boxes_come_off_the_per_axis_parameters(self):
        dump = _dump(_CUT)
        assert dump.box("stock") == ((-10.0, -10.0, -20.0), (10.0, 10.0, 0.0))
        assert dump.box("part") == ((-9.0, -9.0, -19.0), (9.0, 9.0, -1.0))
        assert dump.box("fixture") is None

    def test_the_unit_rows_come_back_uninterpreted(self):
        assert _dump(_CUT).units() == {"operation:metric": 1, "operation:tool_unit": "millimeters"}
        assert _dump_reader.parse_dump("-1: onOpen()").units() == {
            "operation:metric": None, "operation:tool_unit": None}

    def test_each_motion_kind_lands_with_its_position_axis_and_feed(self):
        rows = _dump(_RAPID, _CUT).rows
        assert [r["kind"] for r in rows] == ["rapid5d", "linear5d"]
        assert (rows[0]["z"], rows[0]["k"], rows[0]["feed"]) == (5.0, 1.0, None)
        assert (rows[1]["x"], rows[1]["y"], rows[1]["z"]) == (1.0, 2.0, -3.0)
        assert (rows[1]["i"], rows[1]["j"], rows[1]["k"], rows[1]["feed"]) == (0.0, 0.6, 0.8, 750.0)

    def test_each_three_axis_kind_lands_with_its_position_and_no_tool_axis(self):
        rows = _dump(_RAPID3, _CUT3).rows
        assert [r["kind"] for r in rows] == ["rapid", "linear"]
        assert (rows[0]["x"], rows[0]["y"], rows[0]["z"], rows[0]["feed"]) == (0.0, 0.0, 5.0, None)
        assert (rows[1]["x"], rows[1]["y"], rows[1]["z"], rows[1]["feed"]) == (1.0, 2.0, -3.0, 500.0)
        assert not [k for r in rows for k in "ijk" if k in r]

    def test_the_strategy_reads_off_the_parameter_block(self):
        assert _dump(_CUT).strategy == "moduleworks_multiaxis_finishing"
        assert _dump_reader.parse_dump("-1: onOpen()").strategy is None

    def test_every_unread_event_kind_is_named_with_its_count(self):
        dump = _dump(_RAPID, _CUT, _ARC3, *_THREE_AXIS)
        assert [r["kind"] for r in dump.rows] == ["rapid5d", "linear5d", "circular", "linear",
                                                  "rapid"]
        assert dump.skipped == {"onOpen": 1, "onSection": 1, "onSectionEnd": 1, "onClose": 1}

    def test_an_arc_lands_with_its_endpoint_its_centre_and_its_feed(self):
        row = _dump(_ARC3).rows[0]
        assert (row["kind"], row["x"], row["y"], row["z"]) == ("circular", 1.0, 2.0, -3.0)
        assert (row["cx"], row["cy"], row["cz"], row["feed"]) == (0.0, 0.0, -3.0, 500.0)
        assert not [k for k in "ijk" if k in row]

    def test_an_arc_missing_its_endpoint_is_skipped_not_zeroed(self):
        dump = _dump("581: onCircular(0, 0, 0, -3, 1, 2, undefined, 500)", "583: onCircular(0, 0)")
        assert dump.rows == []
        assert dump.skipped["onCircular"] == 2

    def test_an_arc_with_an_unreadable_centre_is_skipped_not_zeroed(self):
        dump = _dump("585: onCircular(0, undefined, 0, -3, 1, 2, -3, 500)")
        assert dump.rows == []
        assert dump.skipped["onCircular"] == 1

    def test_a_three_axis_line_missing_its_position_is_skipped_not_zeroed(self):
        dump = _dump("577: onLinear(1, 2, undefined, 500)", "579: onRapid(0, 0)")
        assert dump.rows == []
        assert (dump.skipped["onLinear"], dump.skipped["onRapid"]) == (1, 1)

    def test_a_motion_line_with_an_unreadable_coordinate_is_skipped_not_zeroed(self):
        dump = _dump("575: onLinear5D(1, 2, undefined, 0, 0, 1, 750, 2)")
        assert dump.rows == []
        assert dump.skipped["onLinear5D"] == 1


class TestEnvelope:
    def test_a_cut_outside_the_stated_stock_box_fails(self):
        ok, facts = _dump_reader.envelope(_dump(_CUT, _PAST_X))
        assert ok is False
        assert (facts["outside_count"], facts["first_outside"]["x"]) == (1, 11.0)
        assert facts["stock_box"] == ((-10.0, -10.0, -20.0), (10.0, 10.0, 0.0))

    def test_a_cut_exactly_on_the_box_corner_is_inside(self):
        ok, facts = _dump_reader.envelope(_dump(_CORNER))
        assert (ok, facts["outside_count"], facts["cutting_rows"]) == (True, 0, 1)

    def test_the_tolerance_admits_a_cut_exactly_that_far_past_an_upper_face(self):
        assert _dump_reader.envelope(_dump(_PAST_X), tol=1.0)[0] is True
        assert _dump_reader.envelope(_dump(_PAST_X), tol=0.999)[0] is False

    def test_the_tolerance_admits_a_cut_exactly_that_far_below_a_lower_face(self):
        ok, facts = _dump_reader.envelope(_dump(_BELOW_Z))
        assert (ok, facts["outside_count"], facts["first_outside"]["z"]) == (False, 1, -21.0)
        assert _dump_reader.envelope(_dump(_BELOW_Z), tol=1.0)[0] is True
        assert _dump_reader.envelope(_dump(_BELOW_Z), tol=0.999)[0] is False

    def test_rapids_above_the_box_are_not_judged(self):
        ok, facts = _dump_reader.envelope(_dump(_RAPID, _CUT))
        assert (ok, facts["cutting_rows"]) == (True, 1)

    def test_a_dump_with_no_cut_at_all_fails_rather_than_passing_vacuously(self):
        ok, facts = _dump_reader.envelope(_dump(_RAPID))
        assert (ok, facts["cutting_rows"], facts["outside_count"]) == (False, 0, 0)

    def test_the_upper_slack_admits_a_cut_exactly_that_far_over_the_top_face(self):
        over = _dump("583: onLinear(0, 0, 1, 500)")
        assert _dump_reader.envelope(over, up_tol=1.0)[0] is True
        ok, facts = _dump_reader.envelope(over, up_tol=0.999)
        assert (ok, facts["outside_count"], facts["up_tol"]) == (False, 1, 0.999)

    def test_the_upper_slack_loosens_no_other_face(self):
        assert _dump_reader.envelope(_dump(_PAST_X), up_tol=5.0)[0] is False
        assert _dump_reader.envelope(_dump(_BELOW_Z), up_tol=5.0)[0] is False
        assert _dump_reader.envelope(_dump("585: onLinear(0, 0, 1, 500)"), tol=1.0)[0] is True

    def test_a_three_axis_cut_is_judged_against_the_box_like_a_five_axis_one(self):
        assert _dump_reader.envelope(_dump(_RAPID3, _CUT3))[0] is True
        ok, facts = _dump_reader.envelope(_dump(_CUT3, "581: onLinear(11, 0, -3, 500)"))
        assert (ok, facts["cutting_rows"], facts["outside_count"]) == (False, 2, 1)

    def test_an_arc_endpoint_outside_the_box_fails(self):
        dump = _dump("580: onRapid(9, -2, -3)",
                     "581: onCircular(false, 9, 0, -3, 11, 0, -3, 500)\n"
                     "  sweep: 90deg\n  normal: X=0 Y=0 Z=1 (XY)")
        ok, facts = _dump_reader.envelope(dump)
        assert (ok, facts["cutting_rows"], facts["outside_count"]) == (False, 1, 1)
        assert facts["first_outside"]["x"] == 11.0

    def test_a_minor_arc_with_its_centre_outside_the_box_can_stay_inside(self):
        dump = _dump("582: onRapid(-5, 0, -3)",
                     "583: onCircular(false, 0, 12, -3, 5, 0, -3, 500)\n"
                     "  sweep: 45.239730deg\n  normal: X=0 Y=0 Z=1 (XY)")
        ok, facts = _dump_reader.envelope(dump)
        assert (ok, facts["cutting_rows"], facts["outside_count"]) == (True, 1, 0)


class TestFloor:
    def test_a_cut_exactly_at_the_floor_passes_and_one_step_below_it_fails(self):
        assert _dump_reader.floor(_dump(_CUT, _CORNER), -20.0)[0] is True
        ok, facts = _dump_reader.floor(_dump(_CUT, _CORNER), -19.99)
        assert (ok, facts["lowest_z"], facts["cutting_rows"]) == (False, -20.0, 2)

    def test_the_tolerance_admits_a_cut_exactly_that_far_below(self):
        assert _dump_reader.floor(_dump(_CORNER), -19.99, tol=0.01)[0] is True
        assert _dump_reader.floor(_dump(_CORNER), -19.99, tol=0.009)[0] is False

    def test_rapids_below_the_floor_are_not_judged(self):
        ok, facts = _dump_reader.floor(_dump("577: onRapid5D(0, 0, -50, 0, 0, 1)", _CUT), -10.0)
        assert (ok, facts["lowest_z"]) == (True, -3.0)

    def test_a_dump_with_no_cut_at_all_fails_rather_than_passing_vacuously(self):
        ok, facts = _dump_reader.floor(_dump(_RAPID), -20.0)
        assert (ok, facts["lowest_z"]) == (False, None)

    def test_a_three_axis_cut_sets_the_lowest_z_and_its_rapid_does_not(self):
        ok, facts = _dump_reader.floor(_dump(_RAPID3, _CUT3), -3.0)
        assert (ok, facts["lowest_z"], facts["cutting_rows"]) == (True, -3.0, 1)
        assert _dump_reader.floor(_dump(_RAPID3, _CUT3), -2.99)[0] is False

    def test_an_arc_below_the_linear_moves_sets_the_lowest_z(self):
        dump = _dump(_CUT3, "580: onRapid(2, 0, -20)",
                     "581: onCircular(false, 0, 0, -20, 0, 2, -20, 500)\n"
                     "  sweep: 90deg\n  normal: X=0 Y=0 Z=1 (XY)")
        ok, facts = _dump_reader.floor(dump, -20.0)
        assert (ok, facts["lowest_z"], facts["cutting_rows"]) == (True, -20.0, 2)
        assert _dump_reader.floor(dump, -19.99)[0] is False


class TestTilt:
    def test_an_axis_exactly_at_the_limit_passes_and_one_step_over_it_fails(self):
        assert _dump_reader.tilt(_dump(_CUT), _TILT_DEG)[0] is True
        ok, facts = _dump_reader.tilt(_dump(_CUT), _TILT_DEG - 1e-9)
        assert (ok, facts["axis_rows"]) == (False, 1)
        assert facts["max_angle_deg"] == pytest.approx(36.8698976, abs=1e-6)

    def test_the_default_limit_admits_any_axis_that_is_not_below_the_table(self):
        assert _dump_reader.tilt(_dump(_CUT, _RAPID))[0] is True
        assert _dump_reader.tilt(_dump("579: onLinear5D(0, 0, 0, 0, 0.6, -0.8, 750, 2)"))[0] is False

    def test_a_zero_length_axis_is_unreadable_rather_than_upright(self):
        ok, facts = _dump_reader.tilt(_dump("581: onRapid5D(0, 0, 5, 0, 0, 0)"))
        assert (ok, facts["unreadable_axes"], facts["max_angle_deg"]) == (False, 1, None)

    def test_a_dump_carrying_no_five_axis_event_reports_no_axis_rows_at_all(self):
        ok, facts = _dump_reader.tilt(_dump(*_THREE_AXIS))
        assert (ok, facts["axis_rows"], facts["max_angle_deg"]) == (True, 0, None)

    def test_a_three_axis_row_beside_a_five_axis_one_is_not_counted_as_an_axis(self):
        ok, facts = _dump_reader.tilt(_dump(_CUT, *_THREE_AXIS), _TILT_DEG)
        assert (ok, facts["axis_rows"], facts["unreadable_axes"]) == (True, 1, 0)


class TestAxisBand:
    def test_a_file_stating_no_tool_axis_at_all_fails_rather_than_passing_vacuously(self):
        ok, facts = _dump_reader.axis_band(_dump_reader.parse_dump("-1: onOpen()"), 90.0, 0.5)
        assert (ok, facts["axis_rows"], facts["angle_span_deg"]) == (False, 0, None)

    def test_a_zero_length_axis_beside_a_square_one_fails_rather_than_going_uncounted(self):
        ok, facts = _dump_reader.axis_band(_dump(_SQUARE, _ZERO_AXIS), 90.0, 0.5)
        assert (ok, facts["axis_rows"], facts["unreadable_axes"]) == (False, 2, 1)
        assert facts["angle_span_deg"] == (90.0, 90.0)

    def test_a_row_stating_no_tool_axis_fails(self):
        ok, facts = _dump_reader.axis_band(_dump(_SQUARE, _CUT3), 90.0, 0.5)
        assert (ok, facts["rows_without_an_axis"], facts["axis_rows"]) == (False, 1, 1)

    def test_every_axis_square_to_the_target_passes(self):
        ok, facts = _dump_reader.axis_band(_dump(_SQUARE, _SQUARE_RAPID), 90.0, 0.5)
        assert (ok, facts["axis_rows"], facts["unreadable_axes"]) == (True, 2, 0)
        assert (facts["rows_without_an_axis"], facts["angle_span_deg"]) == (0, (90.0, 90.0))

    def test_an_axis_three_degrees_off_square_fails_a_half_degree_band(self):
        ok, facts = _dump_reader.axis_band(_dump(_SQUARE, _OFF_SQUARE), 90.0, 0.5)
        assert (ok, facts["axis_rows"]) == (False, 2)
        assert facts["angle_span_deg"][0] == pytest.approx(87.0, abs=1e-4)

    def test_the_band_admits_an_axis_exactly_that_far_off_and_refuses_one_step_more(self):
        ok, facts = _dump_reader.axis_band(_dump(_RAPID), 90.0, 90.0)
        assert ok is True
        assert facts["angle_span_deg"] == (0.0, 0.0)
        assert _dump_reader.axis_band(_dump(_RAPID), 90.0, 89.999)[0] is False


class TestFiniteMotion:
    @pytest.mark.parametrize("value", ["nan", "inf", "-inf"])
    @pytest.mark.parametrize("axis", ["i", "j", "k"])
    def test_nonfinite_axes_fail_before_angle_clamping(self, value, axis):
        row = {"i": 0.0, "j": 0.0, "k": 1.0, axis: float(value)}
        assert _dump_reader._axis_angle(row) is None
        motion = "600: onRapid5D(0, 0, 5, {i}, {j}, {k})".format(**row)
        dump = _dump(_CUT, motion)
        assert _dump_reader.tilt(dump)[0] is False
        assert _dump_reader.axis_band(dump, 45.0, 45.0)[0] is False

    @pytest.mark.parametrize("scale", [1e-300, 1e300])
    def test_finite_axis_magnitude_does_not_change_its_direction(self, scale):
        row = {"i": scale, "j": 0.0, "k": scale}
        assert _dump_reader._axis_angle(row) == pytest.approx(45.0)

    @pytest.mark.parametrize("value", ["nan", "inf", "-inf"])
    def test_nonfinite_depth_cannot_disappear_behind_an_earlier_finite_minimum(self, value):
        dump = _dump(_CUT, f"600: onLinear(0, 0, {value}, 500)")
        assert _dump_reader.floor(dump, -20.0)[0] is False
        assert _dump_reader.envelope(dump)[0] is False

    @pytest.mark.parametrize("motion", [
        "600: onLinear(0, 0, -3)",
        "600: onLinear(0, 0, -3, nan)",
        "600: onCircular(undefined, 0, 0, -3, 1, 2, -3, 500)",
    ])
    def test_incomplete_known_motion_invalidates_every_verdict(self, motion):
        dump = _dump(_CUT, motion)
        verdicts = (_dump_reader.envelope(dump), _dump_reader.floor(dump, -20.0),
                    _dump_reader.tilt(dump), _dump_reader.axis_band(dump, 45.0, 45.0))
        for ok, facts in verdicts:
            assert ok is False
            assert facts["unread_motion"]

    @pytest.mark.parametrize("value", ["nan", "inf", "-inf"])
    def test_nonfinite_stock_is_not_an_unbounded_envelope(self, value):
        dump = _dump(_CUT, f"600: onParameter('stock-upper-x', {value})")
        assert dump.box("stock") is None
        assert _dump_reader.envelope(dump)[0] is False


class TestExpandedMotion:
    def test_an_expanded_cut_cannot_hide_outside_stock(self):
        dump = _dump(_CUT, "600: EXPANDED onLinear(11, 0, -21, 500)")
        assert _dump_reader.envelope(dump)[0] is False
        assert _dump_reader.floor(dump, -20)[0] is False
        inside = _dump("600: EXPANDED onLinear(1, 0, -3, 500)")
        assert _dump_reader.envelope(inside)[0] is True
        assert _dump_reader.floor(inside, -20)[0] is True

    def test_unreadable_expanded_motion_invalidates_every_verdict(self):
        dump = _dump(_CUT, "600: EXPANDED onLinear5D(0, 0, undefined, 0, 0, 1, 500)")
        verdicts = (_dump_reader.envelope(dump), _dump_reader.floor(dump, -20),
                    _dump_reader.tilt(dump), _dump_reader.axis_band(dump, 45, 45))
        for ok, facts in verdicts:
            assert ok is False
            assert facts["unread_motion"] == {"onLinear5D": 1}

    def test_an_expanded_arc_uses_its_own_metadata_and_extrema(self):
        dump = _dump("600: EXPANDED onRapid(7, 0, -3)",
                     "601: EXPANDED onCircular(false, 9, 0, -3, 9, 2, -3, 500)\n"
                     "  sweep: 270deg\n  normal: X=0 Y=0 Z=1 (XY)")
        ok, facts = _dump_reader.envelope(dump)
        assert ok is False
        assert facts["unverified_paths"] == []
        assert facts["first_outside_point"] == {"x": 11, "y": 0, "z": -3}


class TestArcPaths:
    @pytest.mark.parametrize("start, centre, end, plane, major_clockwise, witness", [
        ("7, 0, -3", "9, 0, -3", "9, 2, -3", "XY", False, {"x": 11, "y": 0, "z": -3}),
        ("9, 2, -3", "9, 0, -3", "7, 0, -3", "XY", True, {"x": 11, "y": 0, "z": -3}),
        ("0, 7, -3", "0, 9, -3", "0, 9, -1", "YZ", False, {"x": 0, "y": 11, "z": -3}),
        ("-2, 0, -19", "0, 0, -19", "0, 0, -17", "ZX", True, {"x": 0, "y": 0, "z": -21}),
    ])
    def test_a_major_arc_exits_stock_where_the_reverse_minor_arc_stays_inside(
            self, start, centre, end, plane, major_clockwise, witness):
        for clockwise, sweep, expected in ((major_clockwise, 270, False),
                                           (not major_clockwise, 90, True)):
            normal = {"XY": (0, 0, 1), "YZ": (1, 0, 0), "ZX": (0, 1, 0)}[plane]
            nx, ny, nz = (-v if clockwise else v for v in normal)
            dump = _dump(f"600: onRapid({start})",
                         f"601: onCircular({str(clockwise).lower()}, {centre}, {end}, 500)\n"
                         f"  sweep: {sweep}deg\n  normal: X={nx} Y={ny} Z={nz} ({plane})")
            ok, facts = _dump_reader.envelope(dump)
            assert ok is expected
            assert facts["unverified_paths"] == []
            if not expected:
                assert facts["outside_count"] == 1
                assert facts["first_outside_point"] == witness

    def test_a_full_circle_checks_extrema_even_when_its_endpoints_coincide(self):
        dump = _dump("600: onRapid(7, 0, -3)",
                     "601: onCircular(false, 9, 0, -3, 7, 0, -3, 500)\n"
                     "  sweep: 360deg\n  normal: X=0 Y=0 Z=1 (XY)")
        assert _dump_reader.envelope(dump)[0] is False
        assert _dump_reader.envelope(dump, tol=1.0)[0] is True

    def test_a_vertical_major_arc_dips_below_both_endpoints(self):
        dump = _dump("600: onRapid(-2, 0, -19)",
                     "601: onCircular(true, 0, 0, -19, 0, 0, -17, 500)\n"
                     "  sweep: 270deg\n  normal: X=0 Y=-1 Z=0 (ZX)")
        ok, facts = _dump_reader.floor(dump, -20)
        assert ok is False
        assert facts["lowest_z"] == -21.0
        assert _dump_reader.floor(dump, -21)[0] is True

    @pytest.mark.parametrize("prefix, suffix", [
        ([], "  sweep: 90deg\n  normal: X=0 Y=0 Z=1 (XY)"),
        (["600: onRapid(7, 0, -3)"], ""),
        (["600: onRapid(7, 0, -3)", "601: onSection()"],
         "  sweep: 90deg\n  normal: X=0 Y=0 Z=1 (XY)"),
        (["600: onRapid(7, 0, -3)"], "  sweep: 90deg\n  normal: X=0.1 Y=0 Z=0.9"),
        (["600: onRapid(7, 0, -3)"], "  sweep: nandeg\n  normal: X=0 Y=0 Z=1 (XY)"),
        (["600: onRapid(7, 0, -3)"], "  sweep: 90deg\n  normal: X=0 Y=0 Z=1 (XY)\n  spiral"),
        (["600: onRapid(7, 0, -3)"],
         "  sweep: 90deg\n  normal: X=0 Y=0 Z=1 (XY)\n  helical pitch: 1"),
    ])
    def test_unverifiable_arcs_do_not_inherit_endpoint_success(self, prefix, suffix):
        dump = _dump(*prefix, "603: onCircular(true, 9, 0, -3, 9, 2, -3, 500)\n" + suffix)
        for ok, facts in (_dump_reader.envelope(dump), _dump_reader.floor(dump, -20)):
            assert ok is False
            assert facts["unverified_paths"][0]["reason"]


class TestLiveDumpConsumers:
    def test_the_five_axis_consumer_accepts_a_valid_tilted_dump(self, tmp_path):
        payload = _posted_dump(tmp_path, _CUT)
        assert verify_acts_dump._dumped_5d("Tilted", "4002", 80.0)(payload) is True

    def test_the_five_axis_consumer_refuses_an_unreadable_known_motion(self, tmp_path):
        payload = _posted_dump(
            tmp_path,
            _CUT,
            "565: onLinear5D(1, 2, undefined, 0, 0, 1, 750, 2)",
        )
        with pytest.raises(AssertionError):
            verify_acts_dump._dumped_5d("Tilted", "4002", 80.0)(payload)

    def test_the_rotary_consumer_accepts_a_valid_square_dump(self, tmp_path):
        payload = _posted_dump(tmp_path, _SQUARE)
        assert verify_acts_hub._dumped_rotary("Rotary", "4002")(payload) is True

    def test_the_rotary_consumer_refuses_an_unreadable_known_motion(self, tmp_path):
        payload = _posted_dump(
            tmp_path,
            _SQUARE,
            "589: onRapid5D(0, 0, undefined, 0, 1, 0)",
        )
        with pytest.raises(AssertionError):
            verify_acts_hub._dumped_rotary("Rotary", "4002")(payload)


class TestPostedSample:
    def _sample(self):
        path = os.environ.get(_SAMPLE_ENV, "")
        if not path or not os.path.exists(path):
            pytest.skip(f"set {_SAMPLE_ENV} to a .dmp posted with dump.cps")
        return _dump_reader.read_dump(path)

    def test_the_posted_multi_axis_dump_parses_786_five_axis_records(self):
        dump = self._sample()
        kinds = {k: sum(1 for r in dump.rows if r["kind"] == k) for k in ("linear5d", "rapid5d")}
        assert kinds == {"linear5d": 781, "rapid5d": 5}
        assert dump.strategy == "moduleworks_multiaxis_finishing"
        assert dump.box("stock") == ((-41.0, -41.0, -91.0), (41.0, 41.0, 0.0))
        assert dump.units() == {"operation:metric": 1, "operation:tool_unit": "millimeters"}
        assert sorted(dump.skipped) == ["onClose", "onFeedMode", "onMovement", "onOpen",
                                        "onSection", "onSectionEnd"]

    def test_the_posted_dump_cuts_inside_its_own_stock_and_tilts_off_z(self):
        dump = self._sample()
        inside, envelope_facts = _dump_reader.envelope(dump)
        assert (inside, envelope_facts["outside_count"]) == (True, 0)
        assert _dump_reader.floor(dump, -91.0)[0] is True
        assert _dump_reader.floor(dump, 0.0)[0] is False
        ok, facts = _dump_reader.tilt(dump, 60.0)
        assert (ok, facts["axis_rows"]) == (True, 786)
        assert facts["max_angle_deg"] == pytest.approx(51.8428, abs=0.001)
