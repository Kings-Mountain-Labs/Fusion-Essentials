# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""The framing pass reading the camera rows an act module wrote by hand, and the frame a placement
reads a step's coordinates in.

An act module builds its rows as it imports, before any chunk is placed and before any sketch plane
is known. The framing pass is the first place that knows both, so it is where a hand row's view is
settled and where the standing frame it leaves behind is recorded - a hand row walked past unread
leaves the pass deciding against a view the camera left several steps ago.
"""

import os
import sys

import pytest

TESTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(TESTS_DIR, "live"))
import tool_verify  # noqa: E402
import verify_layout  # noqa: E402


@pytest.fixture
def field(monkeypatch):
    """Two adjacent chunks that share a frame, and one a long way off."""
    for chunk, box in (("Alpha", [0.0, 20.0, 0.0, 20.0]), ("Beta", [10.0, 30.0, 0.0, 20.0]),
                       ("Far", [900.0, 920.0, 900.0, 920.0])):
        monkeypatch.setitem(verify_layout._PLACED_BOX, chunk, box)


def _made(name):
    return [("model_create_component", {"name": name, "activate": True}, "ok", None),
            ("model_extrude", {"distance": 5}, "ok", None)]


def _frames(rows):
    return [s[1]["focus"] for s in rows if s[0] == "view_set"]


class TestStandingFrame:
    def test_a_hand_frame_sends_the_next_subject_back_on_camera(self, field):
        rows = verify_layout._framed(
            _made("Alpha") + [tool_verify._watch("Far:1")] + _made("Beta"))
        # Beta sits inside the frame Alpha was given, but the camera is on Far by then.
        assert "Beta:1" in _frames(rows)[-1]

    def test_a_row_that_only_isolates_leaves_the_frame_where_it_was(self, field):
        rows = verify_layout._framed(
            _made("Alpha")
            + [("view_set", {"action": "isolate", "focus": "Far:1"}, "ok", None)]
            + _made("Beta"))
        # An isolate aims nothing, so Beta is still on screen and costs no second frame.
        assert _frames(rows) == [["Alpha:1"], "Far:1"]


class TestMeasuredExtents:
    """The authored box counts only the coordinates a step carries, so a pattern or a mirror reaches
    past it. A measured row widens the frame; it never shrinks one."""

    def test_a_measured_overrun_widens_the_frame_and_never_narrows_it(self, field, monkeypatch):
        assert verify_layout._chunk_box("Alpha") == [0.0, 20.0, 0.0, 20.0]
        monkeypatch.setitem(verify_layout._MEASURED_BOX, "Alpha", [-240.0, 390.0, 5.0, 15.0])
        # x grows both ways; y stays the authored span, which the narrower measurement cannot cut
        assert verify_layout._chunk_box("Alpha") == [-240.0, 390.0, 0.0, 20.0]
        assert verify_layout._frame_box(["Alpha:1"]) == [-240.0, 390.0, 0.0, 20.0]

    def test_measured_boxes_unions_the_instances_and_drops_an_unread_corner(self):
        rows = verify_layout.measured_boxes({
            "Alpha:1": {"min_point": {"x": 0.0, "y": 1.0, "z": 0.0},
                        "max_point": {"x": 10.0, "y": 4.0, "z": 2.0}},
            "Alpha:2": {"min_point": {"x": -5.0, "y": 2.0, "z": 0.0},
                        "max_point": {"x": 6.0, "y": 9.0, "z": 2.0}},
            "Beta:1": {"min_point": {"x": None, "y": 0.0, "z": 0.0},
                       "max_point": {"x": 3.0, "y": 3.0, "z": 1.0}},
        })
        assert rows == {"Alpha": [-5.0, 10.0, 1.0, 9.0]}


class TestLayoutDriftGate:
    """ACT 9's receipt row: _MEASURED_BOX is only true while the field still stands where it was
    read, so a layout move has to fail rather than age the table silently."""

    def _points(self, box):
        return ({"x": box[0], "y": box[2], "z": 0.0}, {"x": box[1], "y": box[3], "z": 0.0})

    def test_every_gated_chunk_passes_where_it_was_measured_and_fails_when_moved(self):
        for chunk in verify_layout._DRIFT_CHUNKS:
            recorded = verify_layout._MEASURED_BOX[chunk]
            lo, hi = self._points(recorded)
            assert verify_layout.layout_placed_as_measured(chunk, lo, hi) is True, chunk
            # one gutter of travel is the smallest move that matters - never agreement
            lo, hi = self._points([v + 60.0 for v in recorded])
            assert verify_layout.layout_placed_as_measured(chunk, lo, hi) is False, chunk

    def test_a_corner_that_did_not_read_is_not_agreement(self):
        chunk = verify_layout._DRIFT_CHUNKS[0]
        lo, hi = self._points(verify_layout._MEASURED_BOX[chunk])
        assert verify_layout.layout_placed_as_measured(
            chunk, {"x": None, "y": lo["y"], "z": 0.0}, hi) is False

    def test_the_four_rows_are_built_from_one_shared_predicate(self):
        # the gate widens by a NAME, not by another copy of the check: each row targets its own
        # occurrence and every row is judged by the same layout_placed_as_measured.
        rows = [verify_layout.drift_row(c) for c in verify_layout._DRIFT_CHUNKS]
        assert [r[1]["target"] for r in rows] == [c + ":1" for c in verify_layout._DRIFT_CHUNKS]
        for chunk, row in zip(verify_layout._DRIFT_CHUNKS, rows):
            box = verify_layout._MEASURED_BOX[chunk]
            lo, hi = self._points(box)
            assert row[2]({"min_point": lo, "max_point": hi}) is True, chunk
            lo, hi = self._points([v + 60.0 for v in box])
            assert row[2]({"min_point": lo, "max_point": hi}) is False, chunk


class TestPlacementFrame:
    """A coordinate LIST is read in the sketch's own frame, as the pair keys already are."""

    _POINTS = {"sketch_name": "XZOnly", "kind": "polyline",
               "points": [[400.0, 5.0], [460.0, 40.0]]}

    def test_an_xz_points_list_pins_only_the_axis_its_plane_spans(self):
        # read as world (x, y) the depths land as world Y, so the chunk measures 5..40 mm deep in an
        # axis the XZ plane does not span - and the shift then carries it along that axis.
        assert verify_layout._place_points(self._POINTS, "xz", "sketch_add_geometry") == [
            (400.0, None), (460.0, None)]
        moved = verify_layout._place_shift(self._POINTS, 100.0, 200.0, "xz", "sketch_add_geometry")
        assert moved["points"] == [[500.0, 5.0], [560.0, 40.0]]

    def test_a_points_only_xz_chunk_stays_where_it_was_authored(self):
        program = [("act", None, [
            ("sketch_create", {"name": "XZOnly", "plane": "xz"}, "ok", None),
            ("sketch_add_geometry", self._POINTS, "ok", None)], None)]
        assert verify_layout._place_slots(program) == {}


class TestSketchView:
    def test_a_hand_frame_written_before_the_planes_were_known_is_rewritten(self, monkeypatch):
        monkeypatch.setitem(tool_verify._SKETCH_PLANE, "FarS", "xz")
        stale = ("view_set", {"action": "orient", "orientation": "iso-top-right",
                              "focus": "FarS"}, "ok", None)
        assert verify_layout._framed([stale])[0][1]["orientation"] == "front"


def _component(name, low, high):
    """One component with a line whose endpoints define its authored layout box."""
    sketch = name + "Sketch"
    return [
        ("model_create_component", {"name": name, "activate": True}, "ok", None),
        ("sketch_create", {"name": sketch, "plane": "xy"}, "ok", None),
        ("sketch_add_geometry", {"sketch_name": sketch, "kind": "line",
                                 "x1": low[0], "y1": low[1],
                                 "x2": high[0], "y2": high[1]}, "ok", None),
    ]


class TestFocusCap:
    def test_a_whole_known_neighbour_group_cannot_overrun_the_focus_cap(self):
        focus, _box = verify_layout._frame_neighbourhood(
            ["Subject"], [0.0, 1.0, 0.0, 1.0],
            [(["NearA", "NearB", "NearC"], [10.0, 11.0, 0.0, 1.0])],
            target=100.0, cap=2)
        assert focus == ["Subject"]

    def test_an_unknown_subject_still_enforces_the_focus_cap_by_whole_group(self):
        focus, _box = verify_layout._frame_neighbourhood(
            ["Unknown"], None,
            [(["NearA", "NearB", "NearC"], [10.0, 11.0, 0.0, 1.0])],
            target=100.0, cap=2)
        assert focus == ["Unknown"]


class TestPatternSuffix:
    def test_a_plain_name_ending_in_one_is_not_treated_as_a_colon_one_occurrence(
            self, monkeypatch):
        monkeypatch.setattr(verify_layout, "_PATTERNED", {"Part"})
        monkeypatch.setitem(verify_layout._PLACED_BOX, "Neighbor", [600.0, 700.0, 0.0, 100.0])
        monkeypatch.setitem(verify_layout._PLACED_BOX, "Part11", [0.0, 100.0, 0.0, 100.0])
        rows = verify_layout._framed(_made("Neighbor") + _made("Part11"))
        assert _frames(rows)[-1] == ["Part11:1"]


class TestCollisionRefusal:
    def test_a_second_row_still_blocked_is_refused(self, monkeypatch):
        monkeypatch.setattr(verify_layout, "_PLACE_ANCHORED",
                            tuple(verify_layout._PLACE_ANCHORED) + ("Blocker",))
        program = [("blocked", None,
                    _component("Blocker", (200.0, 220.0), (1100.0, 500.0))
                    + _component("Target", (1300.0, 1300.0), (1320.0, 1320.0)), None)]
        with pytest.raises(ValueError, match="Target"):
            verify_layout._place_slots(program)

    def test_a_cell_wider_than_the_field_is_refused(self):
        program = [("wide", None,
                    _component("TooWide", (1200.0, 500.0), (2200.0, 520.0)), None)]
        with pytest.raises(ValueError, match="TooWide"):
            verify_layout._place_slots(program)


class TestJointGroupLocking:
    def test_locking_one_joint_member_keeps_every_member_on_authored_ground(self):
        program = [("joint", None,
                    _component("BallSphere", (-10.0, -10.0), (10.0, 10.0))
                    + _component("BallPost", (500.0, 500.0), (520.0, 520.0)), None)]
        assert verify_layout._place_slots(program) == {}


class TestSketchHoistOwner:
    def test_hoist_uses_recorded_active_owner_through_reading_order(self):
        narrative = [
            ("model_create_component", {"name": "OwnerA", "activate": True}, "ok", None),
            ("model_create_component", {"name": "NearB", "activate": True}, "ok", None),
            ("design_activate_component", {"occurrence": "OwnerA:1"}, "ok", None),
            ("sketch_create", {"name": "OwnedSketch", "plane": "xy"}, "ok", None),
            ("sketch_add_geometry", {"sketch_name": "OwnedSketch", "kind": "line",
                                     "x1": 0.0, "y1": 0.0, "x2": 20.0, "y2": 0.0},
             "ok", None),
        ]
        hoisted, _kept = verify_layout._sketches_first(
            [("owners", None, narrative, None)], set())
        created = [s[1]["name"] for s in hoisted if s[0] == "model_create_component"]
        assert created == ["OwnerA"]
        reading = verify_layout._sketch_reading_order(hoisted, {"OwnerA": (0.0, 0.0)})
        reading_created = [s[1]["name"] for s in reading
                           if s[0] == "model_create_component"]
        assert reading_created == ["OwnerA"]

class TestMappedPatternIdentity:
    def test_an_exact_mapped_pattern_key_still_widens_the_frame(self, monkeypatch):
        monkeypatch.setattr(verify_layout, "_PATTERNED", {"Pattern:1"})
        monkeypatch.setitem(verify_layout._CHUNK_OF, "Alias", "Pattern:1")
        monkeypatch.setitem(verify_layout._CHUNK_OF, "Alias:1", "Pattern:1")
        monkeypatch.setitem(verify_layout._PLACED_BOX, "Pattern:1",
                            [0.0, 100.0, 0.0, 100.0])
        monkeypatch.setitem(verify_layout._PLACED_BOX, "Neighbor",
                            [600.0, 700.0, 0.0, 100.0])
        rows = verify_layout._framed(_made("Neighbor") + _made("Alias"))
        assert _frames(rows)[-1] == ["Alias:1", "Neighbor:1"]


class TestNonactivatingSketchOwner:
    def test_hoist_creates_then_activates_a_recorded_owner(self):
        narrative = [
            ("model_create_component", {"name": "OwnerA", "activate": False}, "ok", None),
            ("design_activate_component", {"occurrence": "OwnerA:1"}, "ok", None),
            ("sketch_create", {"name": "OwnedSketch", "plane": "xy"}, "ok", None),
            ("sketch_add_geometry", {"sketch_name": "OwnedSketch", "kind": "line",
                                     "x1": 0.0, "y1": 0.0, "x2": 20.0, "y2": 0.0},
             "ok", None),
        ]
        hoisted, _kept = verify_layout._sketches_first(
            [("owner", None, narrative, None)], set())
        reading = verify_layout._sketch_reading_order(hoisted, {"OwnerA": (0.0, 0.0)})
        tools = [step[0] for step in reading]
        create_at = tools.index("model_create_component")
        activate_at = tools.index("design_activate_component")
        sketch_at = tools.index("sketch_create")
        assert create_at < activate_at < sketch_at
        assert reading[create_at][1]["activate"] is False
        assert reading[activate_at][1]["occurrence"] == "OwnerA:1"

    @pytest.mark.parametrize("second_activates", [False, True])
    def test_multiple_hoisted_creates_precede_their_sketch_activation(self, second_activates):
        a = _component("A", (400.0, 400.0), (420.0, 420.0))
        b = _component("B", (500.0, 500.0), (520.0, 520.0))
        narrative = [
            ("model_create_component", {"name": "A", "activate": False}, "ok", None),
            ("model_create_component", {"name": "B", "activate": second_activates}, "ok", None),
            ("design_activate_component", {"occurrence": "B:1"}, "ok", None),
        ] + b[1:] + [
            ("design_activate_component", {"occurrence": "A:1"}, "ok", None),
        ] + a[1:]
        hoisted, _kept = verify_layout._sketches_first(
            [("owners", None, narrative, None)], set())
        reading = verify_layout._sketch_reading_order(hoisted, {"A": (0, 0), "B": (100, 0)})
        for rows in (hoisted, reading):
            created, owners, active = set(), {}, None
            for tool, args, _expected, _capture in rows:
                if tool == "model_create_component":
                    created.add(args["name"])
                    if args.get("activate"):
                        active = args["name"]
                elif tool == "design_activate_component":
                    active = args["occurrence"].removesuffix(":1")
                    assert active == "root" or active in created
                elif tool == "sketch_create":
                    owners[args["name"]] = active
            assert owners == {"ASketch": "A", "BSketch": "B"}
