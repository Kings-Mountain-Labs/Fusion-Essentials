"""Tests for `model_measure_between` — distance / angle between two targets.

Pins the handler's own job: mode dispatch (distance|angle), unit scaling of the distance + points,
the closest-point payload, degree conversion, and the guards. Target RESOLUTION is TargetRef's job
(tested in test_inputs.TestTargetRef); here the two resolve seams + the measureManager are stubbed.
The measureMinimumDistance/measureAngle signatures + return shape are confirmed by live validation.
"""

import json
import math

import pytest

from conftest import (BRepBody, BRepEdge, BRepFace, FakeBoundingBox3D, FakePoint, FakeVector3D,
                      Line3D, MakeComp, Plane, error_message, install, load_tool, make_design,
                      make_occurrence)

mb = load_tool("model_measure_between")

_PLATE = MakeComp(name="Plate", entity_token="CTOK::Plate")

_REAL_A, _REAL_B = mb._A.resolve, mb._B.resolve


@pytest.fixture(autouse=True)
def _restore(monkeypatch):
    """Each test stubs _A/_B.resolve + the design + measureManager; restore the resolvers after."""
    monkeypatch.setattr(mb._common, "design", lambda: object())
    yield
    mb._A.resolve, mb._B.resolve = _REAL_A, _REAL_B


def _payload(result):
    assert result["isError"] is False, result
    return json.loads(result["content"][0]["text"])


class _Res:
    def __init__(self, value, p1=None, p2=None):
        self.value = value
        self.positionOne, self.positionTwo, self.positionThree = p1, p2, None


def _resolve_both(kind="body"):
    mb._A.resolve = lambda raw: ((type("E", (), {"name": "A"})(), kind), None)
    mb._B.resolve = lambda raw: ((type("E", (), {"name": "B"})(), kind), None)


def _install_mgr(monkeypatch, result):
    """Stub app.measureManager to return `result` from both measure methods."""
    class _Mgr:
        measureMinimumDistance = staticmethod(lambda x, y: result)
        measureAngle = staticmethod(lambda x, y: result)
    monkeypatch.setattr(mb.app, "measureManager", _Mgr())


class TestDistance:
    def test_distance_default_mode_scales_to_mm(self, monkeypatch):
        _resolve_both()
        # value is in cm; mm scale = x10. closest points scale too.
        _install_mgr(monkeypatch, _Res(8.0, FakePoint(1, 0, 0), FakePoint(9, 0, 0)))
        out = _payload(mb.handler(a="A", b="B"))
        assert out["mode"] == "distance"
        assert out["distance"] == 80.0                     # 8cm -> 80mm
        assert out["closest_point_on_a"]["x"] == 10.0      # 1cm -> 10mm
        assert out["closest_point_on_b"]["x"] == 90.0

    def test_distance_in_cm(self, monkeypatch):
        _resolve_both()
        _install_mgr(monkeypatch, _Res(8.0, FakePoint(1, 0, 0), FakePoint(9, 0, 0)))
        out = _payload(mb.handler(a="A", b="B", units="cm"))
        assert out["distance"] == 8.0


class _GapMgr:
    """A measureManager answering BOTH call shapes the distance path makes: the entity pair, and the
    (returned point, target a) read-back that decides which point carries which label. `gaps` maps a
    point to the cm distance the read-back reports for it; a point missing from it reads as
    unmeasurable, the way a raising read-back does."""

    def __init__(self, pair, gaps):
        self._pair, self._gaps = pair, gaps
        self.readbacks = []

    def measureMinimumDistance(self, x, y):
        if isinstance(x, FakePoint):
            self.readbacks.append((x, y))
            if x not in self._gaps:
                raise RuntimeError("read-back unavailable")
            return _Res(self._gaps[x])
        return self._pair

    measureAngle = staticmethod(lambda x, y: None)


def _resolve_named(entities):
    """Resolve a/b to the two given entity objects, so a test can assert what was measured against."""
    ea, eb = entities
    mb._A.resolve = lambda raw: ((ea, "face"), None)
    mb._B.resolve = lambda raw: ((eb, "face"), None)


class TestClosestPointLabelBinding:
    """positionOne is DOCUMENTED as the point on the first entity - and for two PARALLEL planar faces
    Fusion puts it on the SECOND, swapping with the arguments, while a NON-PARALLEL pair that is
    APART holds the documented order in both argument orders. Both legs are the
    'min-distance-position-order-parallel-faces' row in measure_api.py. So each point is bound to
    its label by MEASURING it against 'a', never by the documented order."""

    def test_a_position_one_that_sits_on_b_is_relabelled(self, monkeypatch):
        # a is the z=12mm face, b the z=20mm face, and positionOne came back on b.
        ea, eb = object(), object()
        _resolve_named((ea, eb))
        p1, p2 = FakePoint(5, 3, 2.0), FakePoint(5, 3, 1.2)          # cm: z = 20mm and z = 12mm
        monkeypatch.setattr(mb.app, "measureManager",
                            _GapMgr(_Res(0.8, p1, p2), {p1: 0.8, p2: 0.0}))
        out = _payload(mb.handler(a="A", b="B"))
        assert out["distance"] == 8.0
        assert out["closest_point_on_a"]["z"] == 12.0     # the point ON a, whatever its position slot
        assert out["closest_point_on_b"]["z"] == 20.0

    def test_the_documented_order_is_kept_when_position_one_is_on_a(self, monkeypatch):
        # the non-parallel case, measured holding the documented order in both argument orders - the
        # relabelling must not fire here or it would invent the swap it exists to undo.
        ea, eb = object(), object()
        _resolve_named((ea, eb))
        p1, p2 = FakePoint(5, 3, 1.2), FakePoint(5, 3, 2.0)
        monkeypatch.setattr(mb.app, "measureManager",
                            _GapMgr(_Res(0.8, p1, p2), {p1: 0.0, p2: 0.8}))
        out = _payload(mb.handler(a="A", b="B"))
        assert out["closest_point_on_a"]["z"] == 12.0
        assert out["closest_point_on_b"]["z"] == 20.0

    def test_an_equal_read_back_keeps_the_documented_order(self, monkeypatch):
        # the boundary: the swap fires only when b's point measures STRICTLY closer to a. Two
        # coplanar targets put both points the same distance from a, and there the documented order
        # is the only thing left to go on.
        ea, eb = object(), object()
        _resolve_named((ea, eb))
        p1, p2 = FakePoint(5, 3, 2.0), FakePoint(5, 3, 1.2)
        monkeypatch.setattr(mb.app, "measureManager",
                            _GapMgr(_Res(0.8, p1, p2), {p1: 0.4, p2: 0.4}))
        out = _payload(mb.handler(a="A", b="B"))
        assert out["closest_point_on_a"]["z"] == 20.0     # positionOne, unmoved
        assert out["closest_point_on_b"]["z"] == 12.0

    def test_an_unreadable_read_back_keeps_the_documented_order(self, monkeypatch):
        # a read-back that raises proves nothing about the binding, so it must not reorder the pair.
        ea, eb = object(), object()
        _resolve_named((ea, eb))
        p1, p2 = FakePoint(5, 3, 2.0), FakePoint(5, 3, 1.2)
        monkeypatch.setattr(mb.app, "measureManager", _GapMgr(_Res(0.8, p1, p2), {}))
        out = _payload(mb.handler(a="A", b="B"))
        assert out["closest_point_on_a"]["z"] == 20.0
        assert out["closest_point_on_b"]["z"] == 12.0

    def test_the_read_back_measures_both_points_against_target_a(self, monkeypatch):
        # against a, not b: one target is enough to place both points, and measuring against b too
        # would double the cost for nothing.
        ea, eb = object(), object()
        _resolve_named((ea, eb))
        p1, p2 = FakePoint(5, 3, 2.0), FakePoint(5, 3, 1.2)
        mgr = _GapMgr(_Res(0.8, p1, p2), {p1: 0.8, p2: 0.0})
        monkeypatch.setattr(mb.app, "measureManager", mgr)
        mb.handler(a="A", b="B")
        assert [pt for pt, _ in mgr.readbacks] == [p1, p2]
        assert [ent for _, ent in mgr.readbacks] == [ea, ea]

    def test_a_bool_read_back_is_not_read_as_a_zero_gap(self, monkeypatch):
        # bool is an int subclass: False would sail through a bare number test as a 0 cm gap and
        # relabel the pair on a value that is not a measurement.
        ea, eb = object(), object()
        _resolve_named((ea, eb))
        p1, p2 = FakePoint(5, 3, 2.0), FakePoint(5, 3, 1.2)
        monkeypatch.setattr(mb.app, "measureManager",
                            _GapMgr(_Res(0.8, p1, p2), {p1: 0.8, p2: False}))
        out = _payload(mb.handler(a="A", b="B"))
        assert out["closest_point_on_a"]["z"] == 20.0     # order kept, not swapped on a False


class TestDegenerateOverlap:
    """Interpenetrating solids: measureMinimumDistance returns 0 with BOTH closest points collapsed
    to (0,0,0) - degenerate, not a contact location (live-verified). The handler must flag it."""

    def test_zero_distance_with_both_points_at_origin_flagged(self, monkeypatch):
        _resolve_both()
        _install_mgr(monkeypatch, _Res(0.0, FakePoint(0, 0, 0), FakePoint(0, 0, 0)))
        out = _payload(mb.handler(a="A", b="B"))
        assert out["closest_points_degenerate"] is True
        assert "OVERLAP" in out["note"]
        assert "assembly_inspect_interference" in out["note"]

    def test_zero_distance_at_a_real_contact_point_not_flagged(self, monkeypatch):
        # touching at a NON-origin point is a real contact location - keep the normal payload
        _resolve_both()
        _install_mgr(monkeypatch, _Res(0.0, FakePoint(2, 0, 0), FakePoint(2, 0, 0)))
        out = _payload(mb.handler(a="A", b="B"))
        assert "closest_points_degenerate" not in out

    def test_positive_distance_not_flagged(self, monkeypatch):
        _resolve_both()
        _install_mgr(monkeypatch, _Res(1.0, FakePoint(0, 0, 0), FakePoint(1, 0, 0)))
        out = _payload(mb.handler(a="A", b="B"))
        assert "closest_points_degenerate" not in out


class TestParallelPlanarPair:
    """Two PARALLEL PLANAR faces are the pair whose measured distance can be the separation between
    their PLANES rather than the gap between the bounded faces. The plane separation is a true lower
    bound - every point of one face is exactly that far from the other's plane - but two faces
    offset ACROSS their planes are further apart than the planes are, and publishing the separation
    as the gap reads them as closer (or, at separation 0, as touching) than they are."""

    def _face(self, origin, normal, box, comp=None):
        # Both faces are NATIVE faces of ONE component unless a test says otherwise - the space
        # their boxes are only comparable in.
        return BRepFace(Plane(FakeVector3D(*normal), FakePoint(*origin)),
                        body=BRepBody(name="Plate1", parent_component=comp or _PLATE),
                        bounding_box=FakeBoundingBox3D(FakePoint(*box[0]), FakePoint(*box[1])))

    def _pair(self, monkeypatch, a, b, kind="face"):
        monkeypatch.setattr(mb._A, "resolve", lambda raw: ((a, kind), None))
        monkeypatch.setattr(mb._B, "resolve", lambda raw: ((b, kind), None))

    def test_a_lateral_offset_replaces_the_separation_with_the_proven_bound(self, monkeypatch):
        # planes 2 cm apart, faces 3 cm apart along x -> the nearest points of the two bounded
        # faces are sqrt(13) cm apart, not the 2 cm the measurement came back with.
        a = self._face((0, 0, 0), (0, 0, 1), ((0, 0, 0), (1, 1, 0)))
        b = self._face((0, 0, 2), (0, 0, 1), ((4, 0, 2), (5, 1, 2)))
        self._pair(monkeypatch, a, b)
        _install_mgr(monkeypatch, _Res(2.0, FakePoint(0, 0, 0), FakePoint(0, 0, 2)))
        out = _payload(mb.handler(a="A", b="B"))
        assert out["distance"] == round(math.sqrt(13.0) * 10, 6)
        assert out["distance_is_lower_bound"] is True
        assert out["plane_separation"] == 20.0
        assert "LOWER BOUND" in out["note"]

    def test_the_bounded_pair_drops_the_point_pair_it_no_longer_describes(self, monkeypatch):
        # The points belong to the API's own number; publishing them beside a different distance
        # would offer a pair whose separation is not the distance above them. The note has to
        # describe what the payload DOES - the keys are present and null, so it says "null".
        a = self._face((0, 0, 0), (0, 0, 1), ((0, 0, 0), (1, 1, 0)))
        b = self._face((0, 0, 2), (0, 0, 1), ((4, 0, 2), (5, 1, 2)))
        self._pair(monkeypatch, a, b)
        _install_mgr(monkeypatch, _Res(2.0, FakePoint(0, 0, 0), FakePoint(0, 0, 2)))
        out = _payload(mb.handler(a="A", b="B"))
        assert "closest_point_on_a" in out and out["closest_point_on_a"] is None
        assert "closest_point_on_b" in out and out["closest_point_on_b"] is None
        assert "are null here" in out["note"] and "omitted" not in out["note"]

    def test_coplanar_faces_that_do_not_overlap_are_not_reported_as_touching(self, monkeypatch):
        # 0 is this payload's "touching/overlapping" - and these two faces are 4.4 cm apart.
        a = self._face((0, 0, 0), (0, 0, 1), ((0, 0, 0), (1, 1, 0)))
        b = self._face((0, 0, 0), (0, 0, 1), ((5.4, 0, 0), (6.4, 1, 0)))
        self._pair(monkeypatch, a, b)
        _install_mgr(monkeypatch, _Res(0.0, FakePoint(0, 0, 0), FakePoint(0, 0, 0)))
        out = _payload(mb.handler(a="A", b="B"))
        assert out["distance"] == 44.0
        assert out["distance_is_lower_bound"] is True
        # the degenerate-overlap reading is what a 0 with both points at the origin means; a proven
        # bound says these faces are apart, so that reading must not also be published.
        assert "closest_points_degenerate" not in out

    def test_an_overlapping_pair_keeps_its_number_and_says_what_it_is(self, monkeypatch):
        # Boxes overlapping in x and y: the boxes prove exactly the separation and nothing more, so
        # the measurement stands - flagged, because nothing here proves the faces meet across it.
        a = self._face((0, 0, 0), (0, 0, 1), ((0, 0, 0), (1, 1, 0)))
        b = self._face((0, 0, 2), (0, 0, 1), ((0, 0, 2), (1, 1, 2)))
        self._pair(monkeypatch, a, b)
        _install_mgr(monkeypatch, _Res(2.0, FakePoint(0, 0, 0), FakePoint(0, 0, 2)))
        out = _payload(mb.handler(a="A", b="B"))
        assert out["distance"] == 20.0
        assert out["plane_separation_only"] is True
        assert "distance_is_lower_bound" not in out
        assert out["closest_point_on_b"]["z"] == 20.0       # the API's own pair is still published

    def test_a_measurement_that_is_not_the_plane_separation_is_published_untouched(self, monkeypatch):
        # The read-back gate: without it, any parallel pair would be relabelled on arithmetic that
        # never agreed with the number it is describing.
        a = self._face((0, 0, 0), (0, 0, 1), ((0, 0, 0), (1, 1, 0)))
        b = self._face((0, 0, 2), (0, 0, 1), ((4, 0, 2), (5, 1, 2)))
        self._pair(monkeypatch, a, b)
        _install_mgr(monkeypatch, _Res(math.sqrt(13.0), FakePoint(1, 0, 0), FakePoint(4, 0, 2)))
        out = _payload(mb.handler(a="A", b="B"))
        assert out["distance"] == round(math.sqrt(13.0) * 10, 6)
        assert "distance_is_lower_bound" not in out and "plane_separation_only" not in out

    def test_a_body_pair_is_left_alone(self, monkeypatch):
        # The correction is scoped to two FACES: a body's own distance is not a plane separation.
        a = self._face((0, 0, 0), (0, 0, 1), ((0, 0, 0), (1, 1, 0)))
        b = self._face((0, 0, 2), (0, 0, 1), ((4, 0, 2), (5, 1, 2)))
        self._pair(monkeypatch, a, b, kind="body")
        _install_mgr(monkeypatch, _Res(2.0, FakePoint(0, 0, 0), FakePoint(0, 0, 2)))
        out = _payload(mb.handler(a="A", b="B"))
        assert out["distance"] == 20.0
        assert "plane_separation_only" not in out and "distance_is_lower_bound" not in out

    def test_the_degenerate_overlap_reading_survives_where_nothing_is_proven(self, monkeypatch):
        # Regression: two overlapping parallel faces at distance 0 with both points collapsed to the
        # origin is still the interpenetration reading, not a bound.
        a = self._face((0, 0, 0), (0, 0, 1), ((0, 0, 0), (1, 1, 0)))
        b = self._face((0, 0, 0), (0, 0, 1), ((0, 0, 0), (1, 1, 0)))
        self._pair(monkeypatch, a, b)
        _install_mgr(monkeypatch, _Res(0.0, FakePoint(0, 0, 0), FakePoint(0, 0, 0)))
        out = _payload(mb.handler(a="A", b="B"))
        assert out["closest_points_degenerate"] is True
        assert "OVERLAP" in out["note"]


def _occ(name, children=()):
    """An occurrence as the payload reads one: a fullPathName, its direct children, and the
    component-local collection the unresolved-child count is read through."""
    kids = list(children)
    return make_occurrence(path=name, children=kids,
                           component=MakeComp(name=name.split(":")[0], occurrences=kids))


class _ChildMgr:
    """A measureManager answering the pair with one result and each named child with its own cm gap
    ({fullPathName: cm}); a child absent from the table does not measure."""

    def __init__(self, pair_result, gaps):
        self._pair, self._gaps = pair_result, gaps

    def measureMinimumDistance(self, x, y):
        key = getattr(x, "fullPathName", None)
        if key in self._gaps:
            return _Res(self._gaps[key])
        return self._pair

    measureAngle = staticmethod(lambda x, y: None)


class TestNestedTargetDisclosure:
    """MEASURED: an occurrence is measured on its OWN bodies - what is nested inside it is NOT in
    the number. A carrier that seats on a pedestal held by the frame reads 6 mm against the FRAME
    while the pedestal inside it is at 0, so the gap the caller gets is silently OPTIMISTIC about
    the assembly. The number is right; the payload has to say what it does not cover, and measure
    each child so the caller sees the smaller one."""

    def _pair(self, monkeypatch, a, kind_a, b, kind_b):
        monkeypatch.setattr(mb._A, "resolve", lambda raw: ((a, kind_a), None))
        monkeypatch.setattr(mb._B, "resolve", lambda raw: ((b, kind_b), None))

    def test_a_flat_occurrence_pair_carries_no_caveat(self, monkeypatch):
        # The quiet case: a caveat on every measurement is a caveat nobody reads.
        self._pair(monkeypatch, _occ("Carrier:1"), "occurrence", _occ("Frame:1"), "occurrence")
        _install_mgr(monkeypatch, _Res(0.6, FakePoint(0, 0, 0), FakePoint(0.6, 0, 0)))
        out = _payload(mb.handler(a="Carrier:1", b="Frame:1"))
        assert "targets_with_children" not in out
        assert "NESTED" not in out["note"]

    def test_a_parent_target_names_and_measures_the_child_its_number_excludes(self, monkeypatch):
        # The frame reads 6 mm; the pedestal inside it is touching. Both numbers reach the caller.
        self._pair(monkeypatch, _occ("Carrier:1"), "occurrence",
                   _occ("Frame:1", [_occ("Frame:1+Pedestal:1")]), "occurrence")
        monkeypatch.setattr(mb.app, "measureManager",
                            _ChildMgr(_Res(0.6, FakePoint(1, 0, 0), FakePoint(1.6, 0, 0)),
                                      {"Frame:1+Pedestal:1": 0.0}))
        out = _payload(mb.handler(a="Carrier:1", b="Frame:1"))
        assert out["distance"] == 6.0
        assert [r["target"] for r in out["targets_with_children"]] == ["b"]
        rec = out["targets_with_children"][0]
        assert rec["name"] == "Frame:1" and rec["measured_against"] == "a"
        assert rec["children"] == [{"name": "Frame:1+Pedestal:1", "distance": 0.0}]
        assert "the nearest is Frame:1+Pedestal:1 at 0.0 mm" in out["note"]
        assert "an occurrence contributes its OWN bodies" in out["note"]
        assert "SMALLER" in out["note"]

    def test_the_child_gaps_are_scaled_to_the_callers_units(self, monkeypatch):
        # The seam carries the SCALE FACTOR, not raw cm. mm (x10) on a non-zero child is the only
        # pairing that can tell the two apart: at units='cm' the factor is 1.0, and a 0.0 child
        # scales to 0.0 either way - so a 10x-wrong gap would ship labelled 'mm'.
        self._pair(monkeypatch, _occ("Carrier:1"), "occurrence",
                   _occ("Frame:1", [_occ("Frame:1+Pedestal:1")]), "occurrence")
        monkeypatch.setattr(mb.app, "measureManager",
                            _ChildMgr(_Res(0.6, FakePoint(1, 0, 0), FakePoint(1.6, 0, 0)),
                                      {"Frame:1+Pedestal:1": 2.5}))
        out = _payload(mb.handler(a="Carrier:1", b="Frame:1", units="mm"))
        assert out["distance"] == 6.0
        assert out["targets_with_children"][0]["children"][0]["distance"] == 25.0
        assert "at 25.0 mm" in out["note"]

    def test_the_child_gaps_follow_a_change_of_units(self, monkeypatch):
        # The same child through the other unit: 2.5 cm is 25.0 mm and 2.5 cm.
        self._pair(monkeypatch, _occ("Carrier:1"), "occurrence",
                   _occ("Frame:1", [_occ("Frame:1+Pedestal:1")]), "occurrence")
        monkeypatch.setattr(mb.app, "measureManager",
                            _ChildMgr(_Res(0.6, FakePoint(1, 0, 0), FakePoint(1.6, 0, 0)),
                                      {"Frame:1+Pedestal:1": 2.5}))
        out = _payload(mb.handler(a="Carrier:1", b="Frame:1", units="cm"))
        assert out["distance"] == 0.6
        assert out["targets_with_children"][0]["children"][0]["distance"] == 2.5
        assert "at 2.5 cm" in out["note"]

    def test_the_measured_number_and_points_are_untouched_by_the_caveat(self, monkeypatch):
        # The disclosure changes what the payload SAYS, never what it measured: the same measurement
        # reads the same distance and the same closest points with and without a nested child.
        for b, caveat in ((_occ("Frame:1"), False),
                          (_occ("Frame:1", [_occ("Frame:1+Pedestal:1")]), True)):
            self._pair(monkeypatch, _occ("Carrier:1"), "occurrence", b, "occurrence")
            monkeypatch.setattr(mb.app, "measureManager",
                                _ChildMgr(_Res(0.6, FakePoint(1, 0, 0), FakePoint(1.6, 0, 0)),
                                          {"Frame:1+Pedestal:1": 0.0}))
            out = _payload(mb.handler(a="Carrier:1", b="Frame:1"))
            assert out["distance"] == 6.0
            assert out["closest_point_on_a"]["x"] == 10.0
            assert out["closest_point_on_b"]["x"] == 16.0
            assert ("targets_with_children" in out) is caveat

    def test_a_body_handle_target_is_unaffected(self, monkeypatch):
        # Measuring the frame BAND directly is the way out of the trap - and a body holds no
        # occurrences, so that payload must stay clean.
        self._pair(monkeypatch, _occ("Carrier:1"), "occurrence",
                   _occ("Band", [_occ("Pedestal:1")]), "body")
        _install_mgr(monkeypatch, _Res(0.6, FakePoint(0, 0, 0), FakePoint(0.6, 0, 0)))
        out = _payload(mb.handler(a="Carrier:1", b="h1"))
        assert "targets_with_children" not in out and "NESTED" not in out["note"]

    def test_the_angle_payload_carries_no_caveat(self, monkeypatch):
        # measureAngle refuses an occurrence (and a component, and a body), so only face/edge
        # targets reach an angle payload - and neither holds occurrences.
        self._pair(monkeypatch, _occ("F1"), "face", _occ("F2"), "face")
        _install_mgr(monkeypatch, _Res(math.pi / 2))
        out = _payload(mb.handler(a="h1", b="h2", mode="angle"))
        assert out["angle_deg"] == 90.0
        assert "targets_with_children" not in out and "NESTED" not in out["note"]


class TestTargetEcho:
    """The payload's 'a'/'b' name what was MEASURED, and a nested occurrence's `name` is the LEAF
    only: a target given as 'Frame:1+Pedestal:1' answers 'Pedestal:1' to name. Echoing the leaf
    names a different address than the one measured - and not a unique one, since two
    sub-assemblies can each hold a 'Pedestal:1'. The fullPathName is the address this tool's own
    target input resolves, and the address the nested-children rows already use."""

    def _pair(self, monkeypatch, a, kind_a, b, kind_b):
        monkeypatch.setattr(mb._A, "resolve", lambda raw: ((a, kind_a), None))
        monkeypatch.setattr(mb._B, "resolve", lambda raw: ((b, kind_b), None))

    def _nested(self, path):
        """A nested occurrence as Fusion answers one: fullPathName is the assembly path, name the
        leaf. No children, so the subtree disclosure stays out of this payload."""
        return make_occurrence(path=path, component=MakeComp(name=path.split(":")[0]))

    def test_a_nested_occurrence_echoes_its_full_path_not_its_leaf(self, monkeypatch):
        self._pair(monkeypatch, self._nested("Frame:1+Pedestal:1"), "occurrence",
                   self._nested("Carrier:1"), "occurrence")
        _install_mgr(monkeypatch, _Res(0.6, FakePoint(0, 0, 0), FakePoint(0.6, 0, 0)))
        out = _payload(mb.handler(a="Frame:1+Pedestal:1", b="Carrier:1"))
        assert out["a"] == "occurrence 'Frame:1+Pedestal:1'"
        assert out["b"] == "occurrence 'Carrier:1'"

    def test_the_angle_payload_echoes_the_full_path_too(self, monkeypatch):
        # the same echo is built twice, once per mode - a fix applied to one leg only leaves the
        # other naming the leaf.
        self._pair(monkeypatch, self._nested("Frame:1+Pedestal:1"), "face",
                   self._nested("Carrier:1"), "face")
        _install_mgr(monkeypatch, _Res(math.pi / 2))
        out = _payload(mb.handler(a="Frame:1+Pedestal:1", b="Carrier:1", mode="angle"))
        assert out["a"] == "face 'Frame:1+Pedestal:1'"
        assert out["b"] == "face 'Carrier:1'"

    def test_a_target_with_no_path_still_echoes_its_name(self, monkeypatch):
        # a body carries a name and no fullPathName - the fallback must not drop to the raw input
        # and hide which body the handle resolved to.
        self._pair(monkeypatch, BRepBody(name="Band"), "body", BRepBody(name="Plate"), "body")
        _install_mgr(monkeypatch, _Res(0.6, FakePoint(0, 0, 0), FakePoint(0.6, 0, 0)))
        out = _payload(mb.handler(a="h1", b="h2"))
        assert out["a"] == "body 'Band'" and out["b"] == "body 'Plate'"

    def test_a_target_answering_neither_echoes_the_value_the_caller_passed(self, monkeypatch):
        # a face has neither property, so the find_geometry handle IS its address here.
        self._pair(monkeypatch, object(), "face", object(), "face")
        _install_mgr(monkeypatch, _Res(0.6, FakePoint(0, 0, 0), FakePoint(0.6, 0, 0)))
        out = _payload(mb.handler(a="h7", b="h8"))
        assert out["a"] == "face 'h7'" and out["b"] == "face 'h8'"


class TestAngle:
    def test_angle_returns_degrees(self, monkeypatch):
        _resolve_both("face")
        _install_mgr(monkeypatch, _Res(math.pi / 2))       # 90 degrees
        out = _payload(mb.handler(a="A", b="B", mode="angle"))
        assert out["mode"] == "angle"
        assert abs(out["angle_deg"] - 90.0) < 1e-6
        assert abs(out["angle_rad"] - math.pi / 2) < 1e-6
        assert "distance" not in out


class TestEdgeTargets:
    """An edge handle reaches the measurement AS THE EDGE. Without 'edge' in the kind's allow=,
    TargetRef walks a BRepEdge to its owning BODY - the tool would measure something the caller
    never named while its own angle guidance points at edge handles."""

    def test_both_target_kinds_accept_an_edge(self):
        assert "edge" in mb._A.allow and "edge" in mb._B.allow

    def test_an_edge_is_not_widened_to_its_owning_body(self, monkeypatch):
        # the kind resolves an edge to (entity, 'edge'); _owning_body would hand back the body it
        # belongs to, and the payload would then label and measure that body instead.
        edge = BRepEdge(Line3D(FakePoint(0, 0, 0), FakePoint(1, 0, 0)))
        mb._A.resolve = lambda raw: ((edge, "edge"), None)
        mb._B.resolve = lambda raw: ((BRepFace(Plane(FakeVector3D(0, 0, 1))), "face"), None)
        seen = []

        class _Mgr:
            def measureMinimumDistance(self, x, y):
                seen.append((x, y))
                return _Res(2.0, FakePoint(0, 0, 0), FakePoint(2, 0, 0))
        monkeypatch.setattr(mb.app, "measureManager", _Mgr())
        out = _payload(mb.handler(a="h1", b="h2"))
        # call 0 is the entity pair; the label read-backs that follow measure POINTS against the
        # same 'a', so the edge must be what call 0 measured from.
        assert seen[0][0] is edge
        # a BRepEdge answers neither fullPathName nor name, so the echo falls back to the handle
        assert out["a"] == "edge 'h1'"

    def test_the_wire_names_the_edge_handle_it_accepts(self):
        # the claim and the kind agree: the target inputs offer an edge handle, and the allow= above
        # is what refuses every kind it does not list
        props = mb.tool.to_dict()["inputSchema"]["properties"]
        assert "edge" in props["a"]["description"] and "edge" in props["b"]["description"]

    def test_a_kind_outside_the_allow_set_is_still_refused(self):
        # the allow= widening is exactly one kind wide - a mesh handle stays refused
        assert "mesh" not in mb._A.allow and "design" not in mb._A.allow


class TestValueMustBeARealNumber:
    """A MeasureResults.value that is not a number is UNKNOWN, never a measurement. bool is the
    dangerous case: it is an int subclass, so `isinstance(v, (int, float))` alone lets True through
    as 1 cm and False through as 0 - and 0 is this payload's "touching" / "parallel"."""

    @pytest.mark.parametrize("value", [False, True])
    def test_a_bool_distance_is_refused_naming_the_value(self, monkeypatch, value):
        _resolve_both()
        _install_mgr(monkeypatch, _Res(value, FakePoint(0, 0, 0), FakePoint(0, 0, 0)))
        res = mb.handler(a="A", b="B")
        assert res["isError"] is True
        msg = error_message(res)
        assert repr(value) in msg and "UNKNOWN" in msg and "touching" in msg

    def test_a_real_zero_distance_is_still_a_measurement(self, monkeypatch):
        # the boundary the bool guard must not swallow: 0.0 IS an answer (touching), and 0 is the
        # int form of the same answer.
        _resolve_both()
        _install_mgr(monkeypatch, _Res(0, FakePoint(2, 0, 0), FakePoint(2, 0, 0)))
        assert _payload(mb.handler(a="A", b="B"))["distance"] == 0.0

    def test_an_unreadable_distance_is_refused_too(self, monkeypatch):
        _resolve_both()
        _install_mgr(monkeypatch, _Res(None))
        assert "UNKNOWN" in error_message(mb.handler(a="A", b="B"))

    @pytest.mark.parametrize("value", [False, True])
    def test_a_bool_angle_is_refused_naming_the_value(self, monkeypatch, value):
        _resolve_both("face")
        _install_mgr(monkeypatch, _Res(value))
        res = mb.handler(a="A", b="B", mode="angle")
        assert res["isError"] is True
        msg = error_message(res)
        assert repr(value) in msg and "parallel" in msg

    def test_a_real_zero_angle_is_still_a_measurement(self, monkeypatch):
        _resolve_both("face")
        _install_mgr(monkeypatch, _Res(0.0))
        assert _payload(mb.handler(a="A", b="B", mode="angle"))["angle_deg"] == 0.0


class TestGuards:
    def test_unknown_mode_errors(self):
        _resolve_both()
        res = mb.handler(a="A", b="B", mode="bogus")
        assert "bogus" in error_message(res).lower() or "mode" in error_message(res).lower()

    def test_bad_units_errors(self):
        _resolve_both()
        res = mb.handler(a="A", b="B", units="furlong")
        assert "furlong" in error_message(res).lower() or "units" in error_message(res).lower()

    def test_unresolvable_a_errors(self):
        mb._A.resolve = lambda raw: (None, "no such target 'Ghost'")
        mb._B.resolve = lambda raw: ((object(), "body"), None)
        res = mb.handler(a="Ghost", b="B")
        assert "ghost" in error_message(res).lower()

    def test_measure_failure_surfaced(self, monkeypatch):
        _resolve_both()

        class _Mgr:
            def measureMinimumDistance(self, x, y): raise RuntimeError("measurement failed")
            measureAngle = staticmethod(lambda x, y: None)
        monkeypatch.setattr(mb.app, "measureManager", _Mgr())
        res = mb.handler(a="A", b="B")
        assert "failed" in error_message(res).lower()


class TestWhichRefusalReachesTheWire:
    """A COLLIDING target and a MISSING one are different answers, and the tool publishes whichever
    TargetRef hands it - so both are driven through the REAL kind against a fake design here, not
    through the stubbed resolve seams the rest of this file uses.

    A local component named like an x-ref'd one numbers in its own :N sequence, so a local and an
    x-ref occurrence both answer to one fullPathName. Nothing in the string separates them; the
    entityToken handles do, and they only help if the refusal that carries them arrives."""

    def _occ(self, name, path, token, referenced):
        occ = make_occurrence(path=path, component=MakeComp(name=name.split(":")[0]))
        occ.entityToken, occ.isReferencedComponent = token, referenced
        return occ

    def test_two_occurrences_wearing_one_path_are_refused_naming_their_handles(self):
        install(mb, make_design(occurrences=[
            self._occ("ProbeFrame:1", "ProbeFrame:1", "tok-local", False),
            self._occ("ProbeFrame:1", "ProbeFrame:1", "tok-xref", True)]))
        res = mb.handler(a="ProbeFrame:1", b="")
        assert res["isError"] is True
        msg = error_message(res)
        assert "worn by 2 occurrences" in msg
        assert "tok-local" in msg and "tok-xref" in msg
        assert "did not resolve" not in msg      # the miss text would deny both of them exist

    def test_a_scope_that_holds_no_such_body_names_the_bodies_it_does_hold(self, monkeypatch):
        # The BODY half of the same seam. 'Frame:Pinn' is a qualified reference whose SCOPE resolves
        # and whose body name does not, so the resolver answers with what Frame actually holds - the
        # spelling that would resolve. That refusal carries no word this step could match on either,
        # and reporting the miss instead would deny the component and its body both.
        import adsk.fusion
        # The body walk discriminates kind by isinstance, so the shared fake has to BE the type it
        # stands for (the autouse snapshot puts the mock's own attribute back).
        monkeypatch.setattr(adsk.fusion, "BRepBody", BRepBody)
        root = MakeComp(name="Root")
        frame = MakeComp(name="Frame", bodies=["Pin"])
        install(mb, make_design(comp=root, all_components=[root, frame]))
        res = mb.handler(a="Frame:Pinn", b="")
        assert res["isError"] is True
        msg = error_message(res)
        assert "'Frame' holds no body named 'Pinn'" in msg
        assert "'Pin'" in msg
        assert "did not resolve" not in msg

    def test_a_name_the_design_does_not_carry_still_reports_the_miss(self):
        install(mb, make_design(occurrences=[self._occ("Wheel:1", "Wheel:1", "tok-wheel", False)]))
        res = mb.handler(a="Ghost", b="")
        assert res["isError"] is True
        msg = error_message(res)
        assert "'Ghost' did not resolve to a body handle, an occurrence/component/body name" in msg
