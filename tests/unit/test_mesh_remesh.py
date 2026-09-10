"""Unit tests for mesh_remesh.py - the retriangulation and its density read-back."""

import adsk.fusion
import pytest

from conftest import (BRepBody, FakeBaseFeature, FakeBaseFeatures, FakeFeatures,
                      FakeValueInput as _FakeValueInput, MakeComp, MeshBody, _NamedCollection,
                      install, load_tool, make_design, payload)

mo = load_tool("mesh_remesh")


class _Features(FakeFeatures):
    """comp.features plus the mesh-remesh collection this tool reaches through."""
    def __init__(self, remesh=None, base_features=None):
        super().__init__(base_features=base_features)
        self.meshRemeshFeatures = remesh


class _FeatureResult:
    """The MeshRemeshFeature add() returns: its name and the result bodies."""
    def __init__(self, name, bodies):
        self.name = name
        self.bodies = _NamedCollection(bodies)


class _MeshFeatures:
    """comp.features.meshRemeshFeatures: createInput -> input -> add() -> feature or None.

    raise_on_add forces a mutation failure (it must surface, not be swallowed); none_feature is the
    None a non-parametric add returns; on_add is the in-place edit the remesh makes to the mesh."""
    def __init__(self, result_bodies, feat_name="MeshFeat1", raise_on_add=False, none_feature=False,
                 on_add=None, input_factory=None):
        self._result_bodies = result_bodies
        self._feat_name = feat_name
        self.raise_on_add = raise_on_add
        self.none_feature = none_feature
        self._on_add = on_add
        self._input_factory = input_factory or (lambda: type("Inp", (), {})())
        self.last_input = None

    def createInput(self, *a):
        self.last_input = self._input_factory()
        return self.last_input

    def add(self, inp):
        if self.raise_on_add:
            raise RuntimeError("conversion failed")
        if self._on_add is not None:
            self._on_add()
        if self.none_feature:
            return None
        return _FeatureResult(self._feat_name, self._result_bodies)


@pytest.fixture(autouse=True)
def _types(monkeypatch):
    """The adsk.fusion type identities the body kind and the base-feature scope check branch on."""
    monkeypatch.setattr(adsk.fusion, "MeshBody", MeshBody, raising=False)
    monkeypatch.setattr(adsk.fusion, "BRepBody", BRepBody, raising=False)
    monkeypatch.setattr(adsk.fusion, "BaseFeature", FakeBaseFeature, raising=False)


@pytest.fixture(autouse=True)
def _value_input(monkeypatch):
    """density crosses as a ValueInput - a raw float is what the live SWIG setter refuses."""
    monkeypatch.setattr(mo.adsk.core.ValueInput, "createByReal", _FakeValueInput)


def _wire(src, feats, design_type=0, base_feature=None):
    """One mesh + a meshRemeshFeatures collection on its component, wired into the tool."""
    comp = MakeComp("Comp", mesh_bodies=[src])
    comp.features = _Features(remesh=feats,
                              base_features=FakeBaseFeatures(made=base_feature))
    src.parentComponent = comp
    install(mo, make_design(comp=comp, tokens={"H": src}, design_type=design_type,
                            active_edit_object=base_feature))
    return comp


class TestMeshRemesh:

    def test_remesh_reports_before_after(self):
        src = MeshBody("Scan", tri=2000)
        feats = _MeshFeatures([MeshBody("Scan", tri=1500)])
        _wire(src, feats)
        out = payload(mo.handler(mesh="H"))
        assert out["remeshed"] is True
        assert out["changed"] is True
        assert out["before"]["triangle_count"] == 2000
        assert out["after"]["triangle_count"] == 1500

    def test_unchanged_count_is_flagged_not_asserted(self):
        # count identical after the remesh -> the payload says so instead of implying a fresh mesh
        src = MeshBody("Scan", tri=2000)
        _wire(src, _MeshFeatures([MeshBody("Scan", tri=2000)]))
        out = payload(mo.handler(mesh="H"))
        assert out["changed"] is False
        assert "unchanged" in out["note"]

    def test_a_flat_count_is_judged_on_the_meshs_own_area_and_volume(self):
        # a retriangulation can land with the triangle count flat, so the mesh's own area/volume is
        # read back as the second signal and the note reports what it observed
        src = MeshBody("Scan", tri=2000, area=50.0, volume=10.0)
        _wire(src, _MeshFeatures([MeshBody("Scan", tri=2000, area=51.5, volume=10.0)]))
        out = payload(mo.handler(mesh="H"))
        assert out["changed"] is False
        assert out["geometry_moved"] is True
        assert out["before"]["area_cm2"] == 50.0 and out["after"]["area_cm2"] == 51.5
        assert "MOVED" in out["note"]

    def test_missing_remesh_features_collection_errors(self):
        src = MeshBody("Scan", tri=2000)
        _wire(src, None)
        res = mo.handler(mesh="H")
        assert res["isError"] is True
        assert "meshRemeshFeatures collection" in res["message"]

    def test_none_feature_is_success_in_place(self):
        # add() returns None in a DIRECT design; remesh edits in place, so success is the mesh's
        # updated counts, not the None feature return.
        src = MeshBody("Scan", tri=2000)
        feats = _MeshFeatures([], none_feature=True,
                              on_add=lambda: setattr(src.displayMesh, "triangleCount", 1800))
        _wire(src, feats)
        out = payload(mo.handler(mesh="H"))
        assert out["remeshed"] is True
        assert out["design_mode"] == "direct"
        assert out["base_feature"] is None            # direct opens no scope
        assert out["feature"] is None
        assert out["before"]["triangle_count"] == 2000
        assert out["after"]["triangle_count"] == 1800
        assert mo._common.DIRECT_FEATURE_NOTE in out["note"]

    def test_null_feature_in_a_parametric_scope_reports_parametric(self):
        # the scope suppresses the feature; the payload still reports the DESIGN's own mode and names
        # the base feature the remesh landed in.
        bf = FakeBaseFeature()
        src = MeshBody("Scan", tri=2000)
        feats = _MeshFeatures([], none_feature=True,
                              on_add=lambda: setattr(src.displayMesh, "triangleCount", 1800))
        _wire(src, feats, design_type=1, base_feature=bf)
        out = payload(mo.handler(mesh="H"))
        assert out["feature"] is None
        assert out["design_mode"] == "parametric"
        assert out["base_feature"] == "BaseFeature1"
        assert "BaseFeature1" in out["note"]
        assert "direct" not in out["note"].lower()

    def test_parametric_routes_through_base_feature_scope(self):
        # REGRESSION: in PARAMETRIC the remesh createInput->add runs INSIDE the helper's base-feature
        # scope (opened AND finished) and succeeds.
        bf = FakeBaseFeature()
        src = MeshBody("Scan", tri=2000)
        _wire(src, _MeshFeatures([MeshBody("Scan", tri=1500)]), design_type=1, base_feature=bf)
        out = payload(mo.handler(mesh="H"))
        assert out["remeshed"] is True
        assert out["after"]["triangle_count"] == 1500
        assert bf._starts == 1 and bf._finishes == 1


class TestRemeshDensityReadBack:

    def test_density_that_lands_is_echoed(self):
        # density takes a ValueInput (live-verified; a raw float raises in the SWIG layer) and the
        # set is read back off realValue; a landed density is published, never silently assumed.
        src = MeshBody("Scan", tri=2000)
        feats = _MeshFeatures([MeshBody("Scan", tri=900)])
        _wire(src, feats)
        out = payload(mo.handler(mesh="H", density=12))
        assert out["density_applied"] == 12.0
        assert feats.last_input.density.realValue == 12.0

    def test_density_the_build_drops_is_refused(self):
        # Measured: a silent setattr drop ran the default remesh while implying the density took -
        # a set whose read-back does not echo refuses the input instead.
        class _DropsDensity:
            def __setattr__(self, name, value):
                if name == "density":
                    return                      # the SWIG-proxy silent drop
                object.__setattr__(self, name, value)

        src = MeshBody("Scan", tri=2000)
        _wire(src, _MeshFeatures([MeshBody("Scan", tri=900)],
                                 input_factory=lambda: _DropsDensity()))
        res = mo.handler(mesh="H", density=12)
        assert res["isError"] is True and "did not land" in res["message"]

    def test_no_density_asks_for_no_read_back(self):
        src = MeshBody("Scan", tri=2000)
        _wire(src, _MeshFeatures([MeshBody("Scan", tri=900)]))
        out = payload(mo.handler(mesh="H"))
        assert "density_applied" not in out
