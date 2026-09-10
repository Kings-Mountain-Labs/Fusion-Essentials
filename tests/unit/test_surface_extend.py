"""Unit tests for surface_extend.py - the extend types and the landed distance."""

import types

import adsk.core
import adsk.fusion
import pytest

from conftest import (BRepBody, BRepEdge, BRepFace, FakeFeature as _SharedFeature, MakeComp,
                      install, load_tool, make_design, payload)

se = load_tool("surface_extend")


@pytest.fixture(autouse=True)
def _adsk_seams(monkeypatch):
    """The adsk types the input kinds isinstance-check, and a ValueInput.createByReal returning the
    ('real', cm) pair the tests read the scaled length off. FeatureOperations arrives seeded."""
    monkeypatch.setattr(adsk.fusion, "BRepBody", BRepBody, raising=False)
    monkeypatch.setattr(adsk.fusion, "BRepFace", BRepFace, raising=False)
    monkeypatch.setattr(adsk.fusion, "BRepEdge", BRepEdge, raising=False)
    monkeypatch.setattr(adsk.core.ValueInput, "createByReal",
                        staticmethod(lambda v: ("real", v)), raising=False)


def _edge(body=None):
    """One BRep edge owned by `body` - the read the single-body chain check walks."""
    return BRepEdge(None, body=body)


def _wire(extend_features, handle_map=None):
    """Install a design whose active component carries `extend_features`, with `handle_map` behind
    the geometry handles."""
    comp = MakeComp()
    comp.features = types.SimpleNamespace(extendFeatures=extend_features)
    install(se, make_design(comp=comp, tokens=dict(handle_map or {})))
    return comp


class FakeFeature(_SharedFeature):
    """An ExtendFeature: the shared feature plus the distance ModelParameter (CM) a read-back
    reads; distance_cm None gives a feature whose length parameter cannot be read at all."""
    def __init__(self, name="Feat1", bodies=None, distance_cm=None):
        super().__init__(name=name, bodies=bodies or ())
        if distance_cm is not None:
            self.distance = types.SimpleNamespace(value=distance_cm)


class FakeExtendInput:
    def __init__(self, edges, dist, et, chaining):
        self.edges = edges
        self.dist = dist
        self.et = et
        self.chaining = chaining


class _SwallowingExtendInput(FakeExtendInput):
    """An ExtendFeatureInput that ACCEPTS the extendAlignment write and keeps its default anyway -
    nothing raises and the extend would just run free-edged. The shape set_verified catches."""
    def __setattr__(self, name, value):
        object.__setattr__(self, name, "FreeEdges" if name == "extendAlignment" else value)


class FakeExtendFeatures:
    def __init__(self, result_bodies=None, input_cls=FakeExtendInput, landed_cm=None,
                 distance_readable=True):
        # landed_cm: the distance the created feature reads back, when it differs from the one the
        # input was given (live, the two agree). distance_readable=False models a feature whose
        # distance parameter cannot be read at all.
        self.last_input = None
        self._result = result_bodies
        self._input_cls = input_cls
        self._landed_cm = landed_cm
        self._distance_readable = distance_readable
    def createInput(self, edges, dist, et, chaining):
        self.last_input = self._input_cls(edges, dist, et, chaining)
        return self.last_input
    def add(self, inp):
        landed = None
        if self._distance_readable:
            landed = self._landed_cm if self._landed_cm is not None else inp.dist[1]
        return FakeFeature(name="Extend1", bodies=self._result, distance_cm=landed)


class TestSurfaceExtend:

    def test_extends_from_open_edges(self):
        body = BRepBody("Surf1", is_solid=False)
        e1, e2 = _edge(body=body), _edge(body=body)
        xf = FakeExtendFeatures(result_bodies=[BRepBody("Surf1", is_solid=False)])
        _wire(xf, handle_map={"E1": e1, "E2": e2})
        out = payload(se.handler(edges=["E1", "E2"], distance=4, units="mm"))
        assert out["extended"] is True and out["is_solid"] is False
        assert xf.last_input.dist == ("real", 0.4)
        assert xf.last_input.et is adsk.fusion.SurfaceExtendTypes.NaturalSurfaceExtendType

    def test_rejects_edges_from_more_than_one_body(self):
        e1 = _edge(body=BRepBody("SurfA", is_solid=False))
        e2 = _edge(body=BRepBody("SurfB", is_solid=False))
        _wire(FakeExtendFeatures(), handle_map={"E1": e1, "E2": e2})
        res = se.handler(edges=["E1", "E2"], distance=4)
        assert res["isError"] is True and "ONE surface body" in res["message"]

    def test_zero_distance_guard(self):
        _wire(FakeExtendFeatures(), handle_map={"E1": _edge()})
        res = se.handler(edges=["E1"], distance=0)
        assert res["isError"] is True and "non-zero" in res["message"]

    def test_unknown_units_rejected(self):
        _wire(FakeExtendFeatures(), handle_map={"E1": _edge()})
        res = se.handler(edges=["E1"], distance=4, units="cubits")
        assert res["isError"] is True and "mm, cm, or in" in res["message"]

    def test_unknown_extend_type_rejected(self):
        _wire(FakeExtendFeatures(), handle_map={"E1": _edge()})
        res = se.handler(edges=["E1"], distance=4, extend_type="warp")
        assert res["isError"] is True
        assert "natural, tangent, perpendicular" in res["message"]

    def test_tangent_extend_type_resolves_enum(self):
        e1 = _edge(body=BRepBody("Surf1", is_solid=False))
        xf = FakeExtendFeatures(result_bodies=[BRepBody("Surf1", is_solid=False)])
        _wire(xf, handle_map={"E1": e1})
        out = payload(se.handler(edges=["E1"], distance=4, extend_type="tangent"))
        assert out["extend_type"] == "tangent"
        assert xf.last_input.et is adsk.fusion.SurfaceExtendTypes.TangentSurfaceExtendType

    def test_extend_alignment_set_on_input_and_reported(self):
        e1 = _edge(body=BRepBody("Surf1", is_solid=False))
        xf = FakeExtendFeatures(result_bodies=[BRepBody("Surf1", is_solid=False)])
        _wire(xf, handle_map={"E1": e1})
        out = payload(se.handler(edges=["E1"], distance=4, extend_alignment="align_edges"))
        # the member name is BARE (AlignEdges), not suffixed like the neighbouring extend types
        assert xf.last_input.extendAlignment is adsk.fusion.SurfaceExtendAlignment.AlignEdges
        assert out["extend_alignment"] == "align_edges"

    def test_free_edges_alignment_resolves_its_own_member(self):
        # free_edges must land on FreeEdges, not on the other member of the pair: both names
        # resolve, so set_verified's read-back cannot tell them apart - only this can.
        e1 = _edge(body=BRepBody("Surf1", is_solid=False))
        xf = FakeExtendFeatures(result_bodies=[BRepBody("Surf1", is_solid=False)])
        _wire(xf, handle_map={"E1": e1})
        out = payload(se.handler(edges=["E1"], distance=4, extend_alignment="free_edges"))
        assert xf.last_input.extendAlignment is adsk.fusion.SurfaceExtendAlignment.FreeEdges
        assert xf.last_input.extendAlignment is not adsk.fusion.SurfaceExtendAlignment.AlignEdges
        assert out["extend_alignment"] == "free_edges"

    def test_extend_alignment_omitted_writes_nothing_and_reports_nothing(self):
        # a fresh input's extendAlignment reads 0 (measured), so an unwritten one keeps that;
        # the payload must not claim a value nobody set
        e1 = _edge(body=BRepBody("Surf1", is_solid=False))
        xf = FakeExtendFeatures(result_bodies=[BRepBody("Surf1", is_solid=False)])
        _wire(xf, handle_map={"E1": e1})
        out = payload(se.handler(edges=["E1"], distance=4))
        assert not hasattr(xf.last_input, "extendAlignment")
        assert "extend_alignment" not in out

    def test_distance_that_reads_back_wrong_is_an_error(self):
        # the extend landed a distance Fusion took, not the one asked for -> error, never an ok
        # payload echoing the request as if it were the surface's growth
        xf = FakeExtendFeatures(result_bodies=[BRepBody("Surf1", is_solid=False)], landed_cm=0.25)
        _wire(xf, handle_map={"E1": _edge(body=BRepBody("Surf1", is_solid=False))})
        res = se.handler(edges=["E1"], distance=4, units="mm")
        assert res["isError"] is True
        assert "reads back 2.5" in res["message"] and "requested 4.0" in res["message"]
        assert "design_delete_feature" in res["message"]

    def test_distance_read_off_the_feature_is_published(self):
        xf = FakeExtendFeatures(result_bodies=[BRepBody("Surf1", is_solid=False)])
        _wire(xf, handle_map={"E1": _edge(body=BRepBody("Surf1", is_solid=False))})
        out = payload(se.handler(edges=["E1"], distance=4, units="mm"))
        assert out["distance"] == 4.0            # the feature's own parameter, in the caller's units
        assert "unverified" not in out

    def test_unreadable_distance_is_flagged_unverified_not_silently_echoed(self):
        xf = FakeExtendFeatures(result_bodies=[BRepBody("Surf1", is_solid=False)],
                                distance_readable=False)
        _wire(xf, handle_map={"E1": _edge(body=BRepBody("Surf1", is_solid=False))})
        out = payload(se.handler(edges=["E1"], distance=4, units="mm"))
        assert out["unverified"] == ["distance"]
        assert "Not read back off the feature: distance." in out["note"]
        assert out["distance"] == 4.0            # the request, published only because it is flagged

    def test_unknown_extend_alignment_rejected(self):
        _wire(FakeExtendFeatures(), handle_map={"E1": _edge()})
        res = se.handler(edges=["E1"], distance=4, extend_alignment="snap_to_grid")
        assert res["isError"] is True
        assert "free_edges, align_edges" in res["message"]

    def test_extend_alignment_that_does_not_take_is_refused(self):
        e1 = _edge(body=BRepBody("Surf1", is_solid=False))
        xf = FakeExtendFeatures(result_bodies=[BRepBody("Surf1", is_solid=False)],
                                input_cls=_SwallowingExtendInput)
        _wire(xf, handle_map={"E1": e1})
        res = se.handler(edges=["E1"], distance=4, extend_alignment="align_edges")
        assert res["isError"] is True
        assert "extend_alignment=align_edges" in res["message"]
        assert "reads back unchanged" in res["message"]

    def test_extend_alignment_unavailable_member_is_refused_not_silently_defaulted(self, monkeypatch):
        # a missing enum class/member must REFUSE - running the extend on its default while the
        # payload echoes the request is the failure mode this path exists to prevent
        e1 = _edge(body=BRepBody("Surf1", is_solid=False))
        monkeypatch.setattr(adsk.fusion, "SurfaceExtendAlignment", object())
        xf = FakeExtendFeatures(result_bodies=[BRepBody("Surf1", is_solid=False)])
        _wire(xf, handle_map={"E1": e1})
        res = se.handler(edges=["E1"], distance=4, extend_alignment="align_edges")
        assert res["isError"] is True
        assert "not available on this Fusion version" in res["message"]


class TestSolidVerdictOverBodyFacts:

    """trim/extend collapse the shared per-body {name, is_solid} projection into one verdict. That
    projection publishes True/False/None, so the collapse keeps the three apart - any() would fold
    an unreadable flag into a confident 'a surface'."""

    def test_extend_publishes_a_null_is_solid_beside_its_landed_distance(self):
        xf = FakeExtendFeatures(result_bodies=[BRepBody("Surf1", solid_readable=False)],
                                landed_cm=0.5)
        _wire(xf, handle_map={"E1": _edge(body=BRepBody("Surf1", is_solid=False))})
        out = payload(se.handler(edges=["E1"], distance=5))
        assert out["is_solid"] is None
        assert out["unverified"] == ["is_solid"]
        assert out["distance"] == 5.0                 # the length still read back off the feature
