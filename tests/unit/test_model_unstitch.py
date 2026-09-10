"""Unit tests for model_unstitch.py - the explode and its identity-operation gate."""

import json

import adsk.core
import adsk.fusion
import pytest

from conftest import (load_tool, make_design, install, MakeComp, BRepBody, BRepFace, FakeFeature,
                      FakeFeatures, Profile, _NamedCollection)

so = load_tool("model_unstitch")


class _FakeFeature(FakeFeature):
    """The shared feature plus isSolid - the flag unstitch reads off the FEATURE, not the body."""
    def __init__(self, name, result_bodies, is_solid=None):
        super().__init__(name=name, bodies=result_bodies)
        if is_solid is not None:
            self.isSolid = is_solid


class _FakeUnstitchFeatures:
    def __init__(self, result_bodies):
        self.last_call = None        # (faces_collection, chain)
        self._result_bodies = result_bodies
    def add(self, faces, chain):
        self.last_call = (faces, chain)
        return _FakeFeature("Unstitch1", self._result_bodies)


class _FakeFeatures(FakeFeatures):
    """comp.features: a per-kind collection is present only when the test wires one."""
    def __init__(self, loft=None, stitch=None, unstitch=None):
        super().__init__()
        if loft is not None:
            self.loftFeatures = loft
        if stitch is not None:
            self.stitchFeatures = stitch
        if unstitch is not None:
            self.unstitchFeatures = unstitch


@pytest.fixture(autouse=True)
def _types(monkeypatch):
    monkeypatch.setattr(adsk.fusion, "BRepBody", BRepBody, raising=False)
    monkeypatch.setattr(adsk.fusion, "BRepFace", BRepFace, raising=False)
    monkeypatch.setattr(adsk.fusion, "Profile", Profile, raising=False)
    monkeypatch.setattr(adsk.core.ValueInput, "createByReal",
                        staticmethod(lambda v: ("real", v)), raising=False)


def _install(features, bodies_by_name=None, handle_map=None):
    comp = MakeComp(name="Comp", bodies=list((bodies_by_name or {}).values()), mesh_bodies=())
    comp.features = features
    return install(so, make_design(comp=comp, tokens=handle_map))


def _payload(result):
    assert result["isError"] is False, result
    return json.loads(result["content"][0]["text"])


class TestUnstitch:

    def test_explode_body_uses_add_not_createInput(self):
        body = BRepBody("Solid1", is_solid=True)
        result = [BRepBody("Srf1", is_solid=False), BRepBody("Srf2", is_solid=False),
                  BRepBody("Srf3", is_solid=False)]
        uf = _FakeUnstitchFeatures(result)
        _install(_FakeFeatures(unstitch=uf), bodies_by_name={"Solid1": body})
        out = _payload(so.handler(target="Solid1"))
        assert out["unstitched"] is True
        assert out["surface_body_count"] == 3
        # add() was called (the createInput-less shape) with (collection, chain)
        assert uf.last_call is not None
        faces_coll, chain = uf.last_call
        assert chain is True
        assert faces_coll.count == 1          # the one body collected

    def test_peel_faces(self):
        f1 = BRepFace(None)
        f2 = BRepFace(None)
        result = [BRepBody("Srf1", is_solid=False), BRepBody("Srf2", is_solid=False)]
        uf = _FakeUnstitchFeatures(result)
        _install(_FakeFeatures(unstitch=uf), handle_map={"H1": f1, "H2": f2})
        out = _payload(so.handler(faces=["H1", "H2"], chain=False))
        assert out["surface_body_count"] == 2
        faces_coll, chain = uf.last_call
        assert chain is False
        assert faces_coll.count == 2          # two faces collected

    def test_needs_target_or_faces(self):
        _install(_FakeFeatures(unstitch=_FakeUnstitchFeatures([])))
        res = so.handler()
        assert res["isError"] is True
        assert "target" in res["message"] and "faces" in res["message"]

    def test_target_and_faces_both_rejected(self):
        body = BRepBody("Solid1", is_solid=True)
        f1 = BRepFace(None)
        _install(_FakeFeatures(unstitch=_FakeUnstitchFeatures([])),
                 bodies_by_name={"Solid1": body}, handle_map={"H1": f1})
        res = so.handler(target="Solid1", faces=["H1"])
        assert res["isError"] is True
        assert "not both" in res["message"]

    def test_null_feature_is_error(self):
        # add() returns None (not unstitchable) -> honest error, never reported as success
        class _NullUnstitch:
            def add(self, faces, chain):
                return None
        body = BRepBody("Solid1", is_solid=True)
        _install(_FakeFeatures(unstitch=_NullUnstitch()), bodies_by_name={"Solid1": body})
        res = so.handler(target="Solid1")
        assert res["isError"] is True
        assert "loose surfaces" in res["message"] or "unstitchable" in res["message"]


class TestUnstitchIdentityGate:

    def test_identity_unstitch_on_a_loose_surface_is_refused(self):
        # An unstitch of an ALREADY-LOOSE surface is an identity op the API reports as success
        # (measured: same census, the body re-serialized under a new name) - the count gate refuses.
        body = BRepBody("Loose1", is_solid=False)
        body.parentComponent = MakeComp(name="Host", bodies=["A", "B", "C"])
        uf = _FakeUnstitchFeatures([BRepBody("Loose1 (1)", is_solid=False)])
        _install(_FakeFeatures(unstitch=uf), bodies_by_name={"Loose1": body})
        res = so.handler(target="Loose1")
        assert res["isError"] is True and "identity operation" in res["message"]

    def test_a_real_explode_reports_the_census(self):
        # A genuine unstitch grows the host's body count; the payload carries both sides.
        class _GrowingBodies(_NamedCollection):
            """A host collection whose count MOVES across the mutation: 1 before, 3 after."""
            def __init__(self, counts):
                super().__init__()
                self._counts = list(counts)

            @property
            def count(self):
                return self._counts.pop(0)

        host = MakeComp(name="Host")
        host.bRepBodies = _GrowingBodies([1, 3])
        body = BRepBody("Solid1", is_solid=True)
        body.parentComponent = host
        uf = _FakeUnstitchFeatures([BRepBody("S1", is_solid=False), BRepBody("S2", is_solid=False),
                                    BRepBody("S3", is_solid=False)])
        _install(_FakeFeatures(unstitch=uf), bodies_by_name={"Solid1": body})
        out = _payload(so.handler(target="Solid1"))
        assert out["bodies_before"] == 1 and out["bodies_after"] == 3
