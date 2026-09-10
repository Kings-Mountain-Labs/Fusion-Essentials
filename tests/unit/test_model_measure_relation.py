"""Tests for `model_measure_relation` - named geometric predicates over two entities.

This tool is mostly PURE MATH (angle between directions, offset between axis lines, min-distance
thresholding), so it is tested hard: per relation a passing case, a failing case, and the boundary
exactly at tolerance. The reason-to-exist case - parallel-but-OFFSET axes must FAIL 'coaxial' - is
pinned explicitly. Entity RESOLUTION is TargetRef's job (tested elsewhere); here the two resolve seams
+ the measureManager are stubbed, and fake cylinder/planar-face geometry drives the extraction+math.
"""

import json
import math
import types

import pytest

from conftest import (BRepBody, BRepEdge, BRepFace, Circle3D, Cylinder, FakeBoundingBox3D,
                      FakePoint, FakeVector3D, Line3D, MakeComp, Plane, error_message, load_tool,
                      make_occurrence)

mr = load_tool("model_measure_relation")

_PLATE = MakeComp(name="Plate", entity_token="CTOK::Plate")

_REAL_A, _REAL_B = mr._A.resolve, mr._B.resolve


@pytest.fixture(autouse=True)
def _restore(monkeypatch):
    """Stub the design seam; restore the entity resolvers after each test."""
    monkeypatch.setattr(mr._common, "design", lambda: object())
    yield
    mr._A.resolve, mr._B.resolve = _REAL_A, _REAL_B


# ── fake geometry ─────────────────────────────────────────────────────────────

def _cyl(origin, axis):
    """A cylindrical surface: the axis line an axis relation reads, plus its radius."""
    surface = Cylinder(FakeVector3D(*axis), FakePoint(*origin))
    surface.radius = 1.0
    return surface


def _plane(origin, normal):
    return Plane(FakeVector3D(*normal), FakePoint(*origin))


def _face(geom):
    return BRepFace(geom)


def _resolve_ab(ent_a, kind_a, ent_b, kind_b):
    mr._A.resolve = lambda raw: ((ent_a, kind_a), None)
    mr._B.resolve = lambda raw: ((ent_b, kind_b), None)


def _faces(ga, gb):
    _resolve_ab(_face(ga), "face", _face(gb), "face")


def _dir_at(theta_deg):
    """A unit direction at `theta_deg` from world +Z, in the X-Z plane."""
    t = math.radians(theta_deg)
    return (math.sin(t), 0.0, math.cos(t))


def _payload(result):
    assert result["isError"] is False, result
    return json.loads(result["content"][0]["text"])


class _MR:
    def __init__(self, value, p1=None, p2=None):
        self.value = value
        self.positionOne, self.positionTwo = p1, p2


def _install_mgr(monkeypatch, result, raises=False):
    class _Mgr:
        def measureMinimumDistance(self, a, b):
            if raises:
                raise RuntimeError("measurement failed")
            return result
    monkeypatch.setattr(mr.app, "measureManager", _Mgr())


# ── coaxial: the tool's reason to exist ───────────────────────────────────────

class TestCoaxial:
    def test_same_axis_line_passes(self):
        _faces(_cyl((0, 0, 0), (0, 0, 1)), _cyl((0, 0, 3), (0, 0, 1)))
        out = _payload(mr.handler(relation="coaxial"))
        assert out["passed"] is True
        assert out["measured"]["angle_deg"] == 0.0
        assert out["measured"]["axis_offset"] == 0.0

    def test_parallel_but_offset_axes_FAIL(self):
        # The classic trap: angle 0 (parallel) but the axis lines are 5 cm apart -> NOT coaxial.
        _faces(_cyl((0, 0, 0), (0, 0, 1)), _cyl((5, 0, 0), (0, 0, 1)))
        out = _payload(mr.handler(relation="coaxial"))
        assert out["passed"] is False
        assert out["measured"]["angle_deg"] == 0.0          # parallel...
        assert out["measured"]["axis_offset"] == 50.0       # ...but 50 mm offset
        assert "parallel" in out["note"].lower() and "offset" in out["note"].lower()

    def test_non_parallel_axes_fail(self):
        _faces(_cyl((0, 0, 0), (0, 0, 1)), _cyl((0, 0, 0), (0, 1, 0)))
        out = _payload(mr.handler(relation="coaxial"))
        assert out["passed"] is False
        assert out["measured"]["angle_deg"] == 90.0
        assert "not parallel" in out["note"].lower()

    def test_offset_exactly_at_tolerance_passes(self):
        # offset = 0.5 cm = 5 mm; tolerance 5 mm -> boundary is inclusive (<=).
        _faces(_cyl((0, 0, 0), (0, 0, 1)), _cyl((0.5, 0, 0), (0, 0, 1)))
        out = _payload(mr.handler(relation="coaxial", tolerance=5, units="mm"))
        assert out["passed"] is True

    def test_offset_just_over_tolerance_fails(self):
        _faces(_cyl((0, 0, 0), (0, 0, 1)), _cyl((0.5001, 0, 0), (0, 0, 1)))
        out = _payload(mr.handler(relation="coaxial", tolerance=5, units="mm"))
        assert out["passed"] is False

    def test_wrong_kind_planar_faces_refused(self):
        _faces(_plane((0, 0, 0), (0, 0, 1)), _plane((0, 0, 1), (0, 0, 1)))
        res = mr.handler(relation="coaxial")
        assert res["isError"] is True
        msg = error_message(res).lower()
        assert "coaxial" in msg and "planar face" in msg

    def test_units_invariant_verdict_mm_vs_in(self):
        # Same physical geometry (0.5 cm axis offset). A tolerance of 6 mm and of 0.25 in are both
        # larger than 5 mm, so BOTH must PASS - while the reported offset is in the caller's units.
        # (If 'units' were ignored, 0.25 would read as 0.25 cm < 0.5 cm and the in-case would FAIL.)
        _faces(_cyl((0, 0, 0), (0, 0, 1)), _cyl((0.5, 0, 0), (0, 0, 1)))
        mm = _payload(mr.handler(relation="coaxial", tolerance=6, units="mm"))
        _faces(_cyl((0, 0, 0), (0, 0, 1)), _cyl((0.5, 0, 0), (0, 0, 1)))
        inch = _payload(mr.handler(relation="coaxial", tolerance=0.25, units="in"))
        assert mm["passed"] is True and inch["passed"] is True
        assert mm["measured"]["axis_offset"] == 5.0                 # 0.5 cm -> 5 mm
        assert inch["measured"]["axis_offset"] == round(0.5 / 2.54, 4)   # 0.5 cm -> 0.1969 in


# ── parallel ──────────────────────────────────────────────────────────────────

class TestParallel:
    def test_parallel_axes_pass(self):
        _faces(_cyl((0, 0, 0), (0, 0, 1)), _cyl((3, 0, 0), (0, 0, 1)))
        out = _payload(mr.handler(relation="parallel"))
        assert out["passed"] is True and out["measured"]["angle_deg"] == 0.0

    def test_anti_parallel_still_parallel(self):
        # A reversed direction (0,0,-1) is the SAME line -> folded angle 0, so parallel PASSES.
        _faces(_cyl((0, 0, 0), (0, 0, 1)), _cyl((0, 0, 0), (0, 0, -1)))
        out = _payload(mr.handler(relation="parallel"))
        assert out["passed"] is True and out["measured"]["angle_deg"] == 0.0

    def test_perpendicular_axes_not_parallel(self):
        _faces(_cyl((0, 0, 0), (0, 0, 1)), _cyl((0, 0, 0), (0, 1, 0)))
        out = _payload(mr.handler(relation="parallel"))
        assert out["passed"] is False

    def test_boundary_at_one_degree(self):
        _faces(_cyl((0, 0, 0), (0, 0, 1)), _cyl((0, 0, 0), _dir_at(1.0)))
        assert _payload(mr.handler(relation="parallel", tolerance_deg=1.0))["passed"] is True
        _faces(_cyl((0, 0, 0), (0, 0, 1)), _cyl((0, 0, 0), _dir_at(1.0)))
        assert _payload(mr.handler(relation="parallel", tolerance_deg=0.9))["passed"] is False

    def test_two_planar_faces_parallel(self):
        _faces(_plane((0, 0, 0), (0, 0, 1)), _plane((9, 9, 9), (0, 0, 1)))
        assert _payload(mr.handler(relation="parallel"))["passed"] is True


# ── perpendicular ───────────────────────────────────────────────────────────────

class TestPerpendicular:
    def test_ninety_degrees_passes(self):
        _faces(_cyl((0, 0, 0), (0, 0, 1)), _cyl((0, 0, 0), (0, 1, 0)))
        out = _payload(mr.handler(relation="perpendicular"))
        assert out["passed"] is True and out["measured"]["deviation_from_90_deg"] == 0.0

    def test_parallel_is_not_perpendicular(self):
        _faces(_cyl((0, 0, 0), (0, 0, 1)), _cyl((0, 0, 0), (0, 0, 1)))
        assert _payload(mr.handler(relation="perpendicular"))["passed"] is False

    def test_boundary_half_degree_off(self):
        _faces(_cyl((0, 0, 0), (0, 0, 1)), _cyl((0, 0, 0), _dir_at(89.5)))
        assert _payload(mr.handler(relation="perpendicular", tolerance_deg=0.5))["passed"] is True
        _faces(_cyl((0, 0, 0), (0, 0, 1)), _cyl((0, 0, 0), _dir_at(89.4)))
        assert _payload(mr.handler(relation="perpendicular", tolerance_deg=0.5))["passed"] is False


# ── flush ────────────────────────────────────────────────────────────────────

class TestFlush:
    def test_coplanar_faces_pass(self):
        # Same normal, origins differ only WITHIN the plane -> zero offset along the normal.
        _faces(_plane((0, 0, 2), (0, 0, 1)), _plane((5, 7, 2), (0, 0, 1)))
        out = _payload(mr.handler(relation="flush"))
        assert out["passed"] is True and out["measured"]["plane_offset"] == 0.0

    def test_parallel_but_stepped_fails(self):
        _faces(_plane((0, 0, 0), (0, 0, 1)), _plane((0, 0, 3), (0, 0, 1)))
        out = _payload(mr.handler(relation="flush"))
        assert out["passed"] is False and "stepped" in out["note"].lower()

    def test_tilted_faces_fail(self):
        _faces(_plane((0, 0, 0), (0, 0, 1)), _plane((0, 0, 0), (0, 1, 1)))
        assert _payload(mr.handler(relation="flush"))["passed"] is False

    def test_offset_boundary(self):
        _faces(_plane((0, 0, 0), (0, 0, 1)), _plane((0, 0, 0.5), (0, 0, 1)))   # 0.5 cm = 5 mm step
        assert _payload(mr.handler(relation="flush", tolerance=5, units="mm"))["passed"] is True
        _faces(_plane((0, 0, 0), (0, 0, 1)), _plane((0, 0, 0.5001), (0, 0, 1)))
        assert _payload(mr.handler(relation="flush", tolerance=5, units="mm"))["passed"] is False

    def test_cylinder_face_refused(self):
        _faces(_cyl((0, 0, 0), (0, 0, 1)), _plane((0, 0, 0), (0, 0, 1)))
        res = mr.handler(relation="flush")
        assert res["isError"] is True
        assert "planar" in error_message(res).lower()


# ── clearance / touching (measureManager stubbed) ─────────────────────────────

class TestClearance:
    def test_clears_passes(self, monkeypatch):
        _faces(_plane((0, 0, 0), (0, 0, 1)), _plane((0, 0, 1), (0, 0, 1)))
        _install_mgr(monkeypatch, _MR(8.0, FakePoint(0, 0, 0), FakePoint(0, 0, 8)))
        out = _payload(mr.handler(relation="clearance", tolerance=5, units="mm"))
        assert out["passed"] is True                    # 80 mm >= 5 mm
        assert out["measured"]["min_distance"] == 80.0
        assert out["measured"]["closest_point_on_b"]["z"] == 80.0

    def test_too_close_fails(self, monkeypatch):
        _faces(_plane((0, 0, 0), (0, 0, 1)), _plane((0, 0, 1), (0, 0, 1)))
        _install_mgr(monkeypatch, _MR(0.02))
        assert _payload(mr.handler(relation="clearance", tolerance=5, units="mm"))["passed"] is False

    def test_boundary_at_required_gap(self, monkeypatch):
        _faces(_plane((0, 0, 0), (0, 0, 1)), _plane((0, 0, 1), (0, 0, 1)))
        _install_mgr(monkeypatch, _MR(0.5))             # 0.5 cm == 5 mm required -> PASS (>=)
        assert _payload(mr.handler(relation="clearance", tolerance=5, units="mm"))["passed"] is True

    def test_measure_failure_surfaced(self, monkeypatch):
        _faces(_plane((0, 0, 0), (0, 0, 1)), _plane((0, 0, 1), (0, 0, 1)))
        _install_mgr(monkeypatch, None, raises=True)
        res = mr.handler(relation="clearance")
        assert res["isError"] is True and "failed" in error_message(res).lower()


class TestTouching:
    def test_within_gap_passes(self, monkeypatch):
        _faces(_plane((0, 0, 0), (0, 0, 1)), _plane((0, 0, 1), (0, 0, 1)))
        _install_mgr(monkeypatch, _MR(0.005))           # 0.05 mm <= 0.1 mm default
        assert _payload(mr.handler(relation="touching"))["passed"] is True

    def test_far_apart_fails(self, monkeypatch):
        _faces(_plane((0, 0, 0), (0, 0, 1)), _plane((0, 0, 1), (0, 0, 1)))
        _install_mgr(monkeypatch, _MR(5.0))
        assert _payload(mr.handler(relation="touching"))["passed"] is False

    def test_zero_distance_flags_overlap(self, monkeypatch):
        _faces(_plane((0, 0, 0), (0, 0, 1)), _plane((0, 0, 1), (0, 0, 1)))
        _install_mgr(monkeypatch, _MR(0.0))
        out = _payload(mr.handler(relation="touching"))
        assert out["passed"] is True
        assert "assembly_inspect_interference" in out["note"]   # does not silently call an overlap "touching"


class TestParallelPlanarPairVerdicts:
    """Two PARALLEL PLANAR faces are the pair whose minimum distance can be the separation between
    their PLANES rather than the gap between the bounded faces. Judging a verdict on that number
    reads a laterally offset pair as closer than it is - a clearance that fails on parts that clear,
    and, at separation 0, a 'touching' PASS on faces that never meet."""

    def _face(self, origin, normal, box, comp=None):
        # Both faces are NATIVE faces of ONE component unless a test says otherwise - the space
        # their boxes are only comparable in.
        return BRepFace(Plane(FakeVector3D(*normal), FakePoint(*origin)),
                        body=BRepBody(name="Plate1", parent_component=comp or _PLATE),
                        bounding_box=FakeBoundingBox3D(FakePoint(*box[0]), FakePoint(*box[1])))

    def _pair(self, monkeypatch, a, b, kind="face"):
        monkeypatch.setattr(mr._A, "resolve", lambda raw: ((a, kind), None))
        monkeypatch.setattr(mr._B, "resolve", lambda raw: ((b, kind), None))

    def _offset_pair(self, monkeypatch, kind="face"):
        """Planes 2 cm apart, faces 3 cm apart along x - so the bounded gap is sqrt(13) cm."""
        self._pair(monkeypatch,
                   self._face((0, 0, 0), (0, 0, 1), ((0, 0, 0), (1, 1, 0))),
                   self._face((0, 0, 2), (0, 0, 1), ((4, 0, 2), (5, 1, 2))), kind=kind)

    def test_clearance_is_judged_on_the_proven_gap_not_the_plane_separation(self, monkeypatch):
        # 20 mm of plane separation FAILS a 25 mm requirement; the faces are actually 36.06 mm
        # apart, so the parts clear. A clearance check that answers on the separation refuses an
        # assembly that fits.
        self._offset_pair(monkeypatch)
        _install_mgr(monkeypatch, _MR(2.0, FakePoint(0, 0, 0), FakePoint(0, 0, 2)))
        out = _payload(mr.handler(relation="clearance", tolerance=25, units="mm"))
        assert out["passed"] is True
        assert out["measured"]["min_distance"] == round(math.sqrt(13.0) * 10, 4)
        assert out["measured"]["min_distance_is_lower_bound"] is True
        assert out["measured"]["plane_separation"] == 20.0

    def test_touching_does_not_pass_on_coplanar_faces_that_never_meet(self, monkeypatch):
        # separation 0 reads as CONTACT, and these two faces are 44 mm apart in their shared plane.
        self._pair(monkeypatch,
                   self._face((0, 0, 0), (0, 0, 1), ((0, 0, 0), (1, 1, 0))),
                   self._face((0, 0, 0), (0, 0, 1), ((5.4, 0, 0), (6.4, 1, 0))))
        _install_mgr(monkeypatch, _MR(0.0, FakePoint(0, 0, 0), FakePoint(0, 0, 0)))
        out = _payload(mr.handler(relation="touching"))
        assert out["passed"] is False
        assert out["measured"]["min_distance"] == 44.0
        assert "assembly_inspect_interference" not in out["note"]   # not a 0-distance overlap

    def test_a_bounded_verdict_drops_the_point_pair_it_no_longer_describes(self, monkeypatch):
        self._offset_pair(monkeypatch)
        _install_mgr(monkeypatch, _MR(2.0, FakePoint(0, 0, 0), FakePoint(0, 0, 2)))
        measured = _payload(mr.handler(relation="clearance", tolerance=25, units="mm"))["measured"]
        assert measured["closest_point_on_a"] is None and measured["closest_point_on_b"] is None

    def test_an_overlapping_pair_keeps_its_number_and_says_what_it_is(self, monkeypatch):
        # The boxes prove exactly the separation and no more, so the verdict stands on the measured
        # number - flagged, because nothing here proves the two faces meet across it.
        self._pair(monkeypatch,
                   self._face((0, 0, 0), (0, 0, 1), ((0, 0, 0), (1, 1, 0))),
                   self._face((0, 0, 2), (0, 0, 1), ((0, 0, 2), (1, 1, 2))))
        _install_mgr(monkeypatch, _MR(2.0, FakePoint(0, 0, 0), FakePoint(0, 0, 2)))
        out = _payload(mr.handler(relation="clearance", tolerance=25, units="mm"))
        assert out["passed"] is False
        assert out["measured"]["min_distance"] == 20.0
        assert out["measured"]["plane_separation_only"] is True
        assert out["measured"]["closest_point_on_b"]["z"] == 20.0
        assert "IS that plane separation" in out["note"]

    def test_a_body_pair_is_judged_exactly_as_before(self, monkeypatch):
        self._offset_pair(monkeypatch, kind="body")
        _install_mgr(monkeypatch, _MR(2.0, FakePoint(0, 0, 0), FakePoint(0, 0, 2)))
        out = _payload(mr.handler(relation="clearance", tolerance=25, units="mm"))
        assert out["passed"] is False and out["measured"]["min_distance"] == 20.0
        assert "plane_separation" not in out["measured"]

    def test_touching_still_passes_where_the_two_faces_really_do_meet(self, monkeypatch):
        # The correction must not cost the true positive: coplanar faces whose boxes overlap.
        self._pair(monkeypatch,
                   self._face((0, 0, 0), (0, 0, 1), ((0, 0, 0), (1, 1, 0))),
                   self._face((0, 0, 0), (0, 0, 1), ((0.5, 0.5, 0), (1.5, 1.5, 0))))
        _install_mgr(monkeypatch, _MR(0.0, FakePoint(0, 0, 0), FakePoint(0, 0, 0)))
        out = _payload(mr.handler(relation="touching"))
        assert out["passed"] is True
        assert out["measured"]["plane_separation_only"] is True


def _occ(name, children=()):
    """An occurrence as the verdict reads one: a fullPathName, its direct children, and the
    component-local collection the unresolved-child count is read through."""
    kids = list(children)
    return make_occurrence(path=name, children=kids,
                           component=MakeComp(name=name.split(":")[0], occurrences=kids))


def _install_child_mgr(monkeypatch, pair_result, gaps):
    """A measureManager answering the pair with one result and each named child with its own cm gap
    ({fullPathName: cm}); a child absent from the table does not measure."""
    class _Mgr:
        def measureMinimumDistance(self, x, y):
            key = getattr(x, "fullPathName", None)
            if key in gaps:
                return _MR(gaps[key])
            return pair_result
    monkeypatch.setattr(mr.app, "measureManager", _Mgr())


class TestNestedTargetDisclosure:
    """MEASURED: an occurrence is measured on its OWN bodies, so a verdict read off a parent judges
    nothing nested inside it. A carrier that seats on a pedestal held by the frame clears the FRAME
    by 6 mm while the pedestal inside it is touching - so 'clearance' PASSES on an assembly that
    does not clear. The verdict stands for what it measured; the evidence has to carry each child's
    own gap, or the pass is read as the whole assembly clearing."""

    def test_a_flat_pair_carries_no_caveat(self, monkeypatch):
        _resolve_ab(_occ("Carrier:1"), "occurrence", _occ("Frame:1"), "occurrence")
        _install_mgr(monkeypatch, _MR(0.6, FakePoint(0, 0, 0), FakePoint(0.6, 0, 0)))
        out = _payload(mr.handler(relation="clearance", entity_a="Carrier:1", entity_b="Frame:1"))
        assert "targets_with_children" not in out["measured"]
        assert "NESTED" not in out["note"]

    def test_a_clearance_pass_carries_the_child_that_does_not_clear(self, monkeypatch):
        # The verdict PASSES at 6 mm and the pedestal inside the frame is at 0 - the number the
        # caller has to see before designing around that clearance.
        _resolve_ab(_occ("Carrier:1"), "occurrence",
                    _occ("Frame:1", [_occ("Frame:1+Pedestal:1")]), "occurrence")
        _install_child_mgr(monkeypatch, _MR(0.6, FakePoint(0, 0, 0), FakePoint(0.6, 0, 0)),
                           {"Frame:1+Pedestal:1": 0.0})
        out = _payload(mr.handler(relation="clearance", tolerance=5, units="mm",
                                  entity_a="Carrier:1", entity_b="Frame:1"))
        assert out["passed"] is True and out["measured"]["min_distance"] == 6.0
        rec = out["measured"]["targets_with_children"][0]
        assert rec["target"] == "entity_b" and rec["measured_against"] == "entity_a"
        assert rec["children"] == [{"name": "Frame:1+Pedestal:1", "distance": 0.0}]
        assert "an occurrence contributes its OWN bodies" in out["note"]
        assert "SMALLER" in out["note"]

    def test_a_touching_verdict_carries_it_too(self, monkeypatch):
        _resolve_ab(_occ("Carrier:1"), "occurrence",
                    _occ("Frame:1", [_occ("Frame:1+Pedestal:1")]), "occurrence")
        _install_child_mgr(monkeypatch, _MR(0.6, FakePoint(0, 0, 0), FakePoint(0.6, 0, 0)),
                           {"Frame:1+Pedestal:1": 0.0})
        out = _payload(mr.handler(relation="touching", entity_a="Carrier:1", entity_b="Frame:1"))
        assert out["passed"] is False                      # the frame's own bodies do not touch
        assert out["measured"]["targets_with_children"][0]["children"][0]["distance"] == 0.0
        assert "the nearest is Frame:1+Pedestal:1 at 0.0 mm" in out["note"]

    def test_the_verdict_and_its_number_are_untouched_by_the_caveat(self, monkeypatch):
        # The disclosure changes what the evidence SAYS, never what was measured or judged.
        for b, caveat in ((_occ("Frame:1"), False),
                          (_occ("Frame:1", [_occ("Frame:1+Pedestal:1")]), True)):
            _resolve_ab(_occ("Carrier:1"), "occurrence", b, "occurrence")
            _install_child_mgr(monkeypatch, _MR(0.6, FakePoint(0, 0, 0), FakePoint(0.6, 0, 0)),
                               {"Frame:1+Pedestal:1": 0.0})
            out = _payload(mr.handler(relation="clearance", tolerance=5, units="mm",
                                      entity_a="Carrier:1", entity_b="Frame:1"))
            assert out["passed"] is True and out["measured"]["min_distance"] == 6.0
            assert ("targets_with_children" in out["measured"]) is caveat

    def test_a_body_target_is_unaffected(self, monkeypatch):
        # Measuring the frame BAND directly is the way out of the trap, so that verdict stays clean.
        _resolve_ab(_occ("Carrier:1"), "occurrence", _occ("Band", [_occ("Pedestal:1")]), "body")
        _install_mgr(monkeypatch, _MR(0.6, FakePoint(0, 0, 0), FakePoint(0.6, 0, 0)))
        out = _payload(mr.handler(relation="clearance", entity_a="Carrier:1", entity_b="h1"))
        assert "targets_with_children" not in out["measured"] and "NESTED" not in out["note"]


# ── concentric: two circular entities whose CENTER POINTS coincide (distinct from coaxial) ────────

def _circ_edge(center, arc=False):
    """A circular edge. `arc` swaps in an Arc3D curve, which has no measured shape of its own."""
    if arc:
        return BRepEdge(types.SimpleNamespace(curveType=mr.adsk.core.Curve3DTypes.Arc3DCurveType,
                                              center=FakePoint(*center)))
    return BRepEdge(Circle3D(FakeVector3D(0, 0, 1), FakePoint(*center), 1.0))


def _edges(ca, cb, arc_b=False):
    _resolve_ab(_circ_edge(ca), "edge", _circ_edge(cb, arc=arc_b), "edge")


class TestConcentric:
    def test_coincident_centers_pass(self):
        _edges((1, 2, 3), (1, 2, 3))
        out = _payload(mr.handler(relation="concentric"))
        assert out["passed"] is True
        assert out["measured"]["center_distance"] == 0.0
        assert "concentric" in out["note"].lower()

    def test_offset_centers_fail_and_point_at_coaxial(self):
        # 0.5 cm apart, default tol 0.1 mm -> FAIL. The note must distinguish concentric from coaxial.
        _edges((0, 0, 0), (0.5, 0, 0))
        out = _payload(mr.handler(relation="concentric"))
        assert out["passed"] is False
        assert out["measured"]["center_distance"] == 5.0          # 0.5 cm -> 5 mm
        assert "coaxial" in out["note"].lower()

    def test_boundary_exactly_at_tolerance(self):
        _edges((0, 0, 0), (0.5, 0, 0))                            # 5 mm apart
        assert _payload(mr.handler(relation="concentric", tolerance=5, units="mm"))["passed"] is True
        _edges((0, 0, 0), (0.5001, 0, 0))
        assert _payload(mr.handler(relation="concentric", tolerance=5, units="mm"))["passed"] is False

    def test_an_arc_edge_also_supplies_a_center(self):
        _edges((2, 0, 0), (2, 0, 0), arc_b=True)
        assert _payload(mr.handler(relation="concentric"))["passed"] is True

    def test_cylindrical_faces_use_their_axis_base_point(self):
        _resolve_ab(_face(_cyl((0, 0, 0), (0, 0, 1))), "face",
                    _face(_cyl((0, 0, 0), (0, 0, 1))), "face")
        assert _payload(mr.handler(relation="concentric"))["passed"] is True

    def test_a_failed_face_pair_is_told_where_a_face_center_comes_from(self):
        # The profile-plane caveat is read off BOTH sources being face centers - two circular EDGES
        # have real centers, so the same failure must not carry it.
        _resolve_ab(_face(_cyl((0, 0, 0), (0, 0, 1))), "face",
                    _face(_cyl((0, 0, 0.5), (0, 0, 1))), "face")
        faces = _payload(mr.handler(relation="concentric"))
        assert faces["passed"] is False
        assert "profile plane" in faces["note"]
        _edges((0, 0, 0), (0.5, 0, 0))
        assert "profile plane" not in _payload(mr.handler(relation="concentric"))["note"]

    def test_straight_edge_is_refused_as_non_circular(self):
        straight = BRepEdge(Line3D(FakePoint(0, 0, 0), FakePoint(1, 0, 0)))
        _resolve_ab(straight, "edge", _circ_edge((0, 0, 0)), "edge")
        res = mr.handler(relation="concentric")
        assert res["isError"] is True
        assert "circular" in error_message(res).lower()


# ── guards + contract ─────────────────────────────────────────────────────────

class TestUnreadableGeometry:
    """A coordinate that will not read makes the relation UNKNOWN, never a verdict. _v is
    whole-point tri-state: one unreadable component and the point is None, because a 0.0 stand-in
    puts the world origin into a comparison the caller reads as pass/fail."""

    class _Blind(FakePoint):
        """A point whose z refuses to read - the shape a per-component 0.0 default hides."""

        def __init__(self, x, y):
            self.x, self.y = x, y

        @property
        def z(self):
            raise RuntimeError("this coordinate is unavailable")

    def _cyl_with(self, origin, axis):
        surface = Cylinder(axis, origin)
        surface.radius = 1.0
        return surface

    def test_a_whole_point_is_none_when_any_one_component_will_not_read(self):
        assert mr._v(self._Blind(1.0, 2.0)) is None
        assert mr._v(FakePoint(1.0, 2.0, 3.0)) == (1.0, 2.0, 3.0)     # all three read -> the tuple

    def test_a_non_numeric_component_is_unreadable_too(self):
        # an adsk mock hands back a truthy child object for anything unmodeled; letting one
        # through would put it into the vector math as if it were a coordinate
        assert mr._v(FakePoint(1.0, 2.0, object())) is None

    def test_a_bool_component_is_not_a_coordinate(self):
        assert mr._v(FakePoint(1.0, 2.0, True)) is None

    def test_coaxial_on_an_unreadable_axis_origin_is_unknown_not_coincident(self):
        # both axes point +z; with z coerced to 0.0 the two origins would read as the SAME line
        # and the call would return passed=true on a measurement that was never made.
        _resolve_ab(_face(self._cyl_with(self._Blind(0.0, 0.0), FakeVector3D(0, 0, 1))), "face",
                    _face(_cyl((0, 0, 0), (0, 0, 1))), "face")
        res = mr.handler(relation="coaxial")
        assert res["isError"] is True
        msg = error_message(res)
        assert "UNKNOWN" in msg and "not a pass" in msg

    def test_flush_on_an_unreadable_plane_origin_is_unknown_not_coplanar(self):
        blind = Plane(FakeVector3D(0, 0, 1), self._Blind(0.0, 0.0))
        _resolve_ab(_face(blind), "face", _face(_plane((0, 0, 0), (0, 0, 1))), "face")
        res = mr.handler(relation="flush")
        assert res["isError"] is True and "UNKNOWN" in error_message(res)

    def test_concentric_on_an_unreadable_center_is_unknown_not_coincident(self):
        blind = BRepEdge(Circle3D(FakeVector3D(0, 0, 1), self._Blind(0.0, 0.0), 1.0))
        _resolve_ab(blind, "edge", _circ_edge((0, 0, 0)), "edge")
        res = mr.handler(relation="concentric")
        assert res["isError"] is True and "UNKNOWN" in error_message(res)

    def test_parallel_on_an_unreadable_direction_is_unknown_not_parallel(self):
        _resolve_ab(_face(self._cyl_with(FakePoint(0, 0, 0), self._Blind(0.0, 0.0))), "face",
                    _face(_cyl((0, 0, 0), (0, 0, 1))), "face")
        res = mr.handler(relation="parallel")
        assert res["isError"] is True and "UNKNOWN" in error_message(res)

    def test_a_wrong_kind_still_gets_the_kind_refusal_not_the_unreadable_one(self):
        # the two are told apart: a planar face asked for 'coaxial' is the WRONG KIND, and its
        # message must keep naming what each entity actually is
        _faces(_plane((0, 0, 0), (0, 0, 1)), _cyl((0, 0, 0), (0, 0, 1)))
        msg = error_message(mr.handler(relation="coaxial"))
        assert "planar face" in msg and "UNKNOWN" not in msg


class TestUnreadableDistance:
    """The min-distance core hands its callers a RESULT, never a bare string: clearance/touching
    return it straight to the wire, where a string would cross as a malformed payload."""

    def _blind_measure(self, monkeypatch, value):
        _faces(_plane((0, 0, 0), (0, 0, 1)), _plane((0, 0, 1), (0, 0, 1)))
        _install_mgr(monkeypatch, _MR(value))

    def test_the_core_returns_an_error_result_not_a_string(self, monkeypatch):
        self._blind_measure(monkeypatch, None)
        _d, _mr_res, err = mr._min_distance_cm(object(), object())
        assert isinstance(err, dict) and err["isError"] is True

    def test_clearance_surfaces_it_as_a_normal_error(self, monkeypatch):
        self._blind_measure(monkeypatch, None)
        res = mr.handler(relation="clearance")
        assert res["isError"] is True and "UNKNOWN" in error_message(res)

    def test_a_bool_distance_is_refused_naming_the_value(self, monkeypatch):
        # bool is an int subclass, so `isinstance(v, (int, float))` alone lets False through as a
        # 0 cm gap - which every caller here scores against the tolerance as CONTACT.
        self._blind_measure(monkeypatch, False)
        res = mr.handler(relation="touching")
        assert res["isError"] is True
        msg = error_message(res)
        assert "False" in msg and "not reported as touching" in msg

    def test_a_real_zero_distance_is_still_a_measurement(self, monkeypatch):
        # the boundary the bool guard must not swallow: 0.0 IS the touching answer.
        self._blind_measure(monkeypatch, 0.0)
        out = _payload(mr.handler(relation="touching"))
        assert out["passed"] is True and out["measured"]["min_distance"] == 0.0

    def test_touching_surfaces_it_as_a_normal_error_rather_than_claiming_contact(self, monkeypatch):
        # the dangerous direction: an unreadable gap must never be scored against the tolerance,
        # which would report the parts as touching
        self._blind_measure(monkeypatch, None)
        res = mr.handler(relation="touching")
        assert res["isError"] is True and "not reported as touching" in error_message(res)

    def test_a_readable_zero_still_measures(self, monkeypatch):
        # the boundary the unreadable case is confused with: 0.0 IS a measurement
        self._blind_measure(monkeypatch, 0.0)
        assert _payload(mr.handler(relation="touching"))["passed"] is True


class TestGuards:
    def test_unknown_relation_errors(self):
        _faces(_cyl((0, 0, 0), (0, 0, 1)), _cyl((0, 0, 0), (0, 0, 1)))
        res = mr.handler(relation="bogus")
        assert res["isError"] is True and "relation" in error_message(res).lower()

    def test_bad_units_errors(self):
        res = mr.handler(relation="clearance", units="furlong")
        assert res["isError"] is True and "furlong" in error_message(res).lower()

    def test_negative_tolerance_deg_errors(self):
        _faces(_cyl((0, 0, 0), (0, 0, 1)), _cyl((0, 0, 0), (0, 0, 1)))
        res = mr.handler(relation="parallel", tolerance_deg=-1)
        assert res["isError"] is True and "tolerance_deg" in error_message(res).lower()

    def test_no_active_design_errors(self, monkeypatch):
        monkeypatch.setattr(mr._common, "design", lambda: None)
        res = mr.handler(relation="coaxial")
        assert res["isError"] is True and "design" in error_message(res).lower()

    def test_unresolvable_entity_surfaced(self):
        mr._A.resolve = lambda raw: (None, "no such target 'Ghost'")
        mr._B.resolve = lambda raw: ((object(), "body"), None)
        res = mr.handler(relation="clearance", entity_a="Ghost")
        assert res["isError"] is True and "ghost" in error_message(res).lower()

    def test_passed_is_a_declared_output(self):
        _faces(_cyl((0, 0, 0), (0, 0, 1)), _cyl((0, 0, 3), (0, 0, 1)))
        out = _payload(mr.handler(relation="coaxial"))
        assert mr.RETURNS[0].assert_present(out) == ""      # the RETURNS contract mints 'passed'
