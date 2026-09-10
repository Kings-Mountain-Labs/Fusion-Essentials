"""Unit tests for mesh_reduce.py - the decimation targets and the unreduced-count gate."""

import adsk.fusion
import pytest

from conftest import (BRepBody, FakeBaseFeature, FakeBaseFeatures, FakeFeatures,
                      FakeValueInput as _FakeValueInput, MakeComp, MeshBody, _NamedCollection,
                      install, load_tool, make_design, payload)

mo = load_tool("mesh_reduce")


class _ReduceInput:
    """A MeshReduceFeatureInput whose proportion/facecount/maximumDeviation setters REQUIRE a
    ValueInput-like object and raise TypeError on a bare float/int, as the live ones do. The
    meshReduceTargetType / meshReduceMethodType attributes are free-form."""
    def __init__(self):
        object.__setattr__(self, "_vi_fields", {"proportion", "facecount", "maximumDeviation"})

    def __setattr__(self, name, value):
        if name in object.__getattribute__(self, "_vi_fields"):
            if not isinstance(value, _FakeValueInput):
                raise TypeError(
                    f"MeshReduceFeatureInput.{name} requires an adsk.core.ValueInput "
                    f"(Ptr<ValueInput>), got {type(value).__name__} {value!r}")
        object.__setattr__(self, name, value)


class _Features(FakeFeatures):
    """comp.features plus the mesh-reduce collection this tool reaches through."""
    def __init__(self, reduce=None, base_features=None):
        super().__init__(base_features=base_features)
        self.meshReduceFeatures = reduce


class _FeatureResult:
    """The MeshReduceFeature add() returns: its name and the result bodies."""
    def __init__(self, name, bodies):
        self.name = name
        self.bodies = _NamedCollection(bodies)


class _MeshFeatures:
    """comp.features.meshReduceFeatures: createInput -> input -> add() -> feature or None.

    raise_on_add forces a mutation failure (it must surface, not be swallowed); none_feature is the
    None a non-parametric add returns; _on_add is the in-place decimation the add performs."""
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
    """The reduce setters take a ValueInput; a bare float is what _ReduceInput refuses, as live."""
    monkeypatch.setattr(mo.adsk.core.ValueInput, "createByReal", _FakeValueInput)


def _wire(src, feats, design_type=0, base_feature=None):
    """One mesh + a meshReduceFeatures collection on its component, wired into the tool."""
    comp = MakeComp("Comp", mesh_bodies=[src])
    comp.features = _Features(reduce=feats, base_features=FakeBaseFeatures(made=base_feature))
    src.parentComponent = comp
    install(mo, make_design(comp=comp, tokens={"H": src}, design_type=design_type,
                            active_edit_object=base_feature))
    return comp


class TestMeshReduce:

    def _setup(self, before_tri=1000, after_tri=300, raise_on_add=False, none_feature=False,
               parametric=False, base_feature=None):
        src = MeshBody("Scan", tri=before_tri)
        feats = _MeshFeatures([MeshBody("Scan", tri=after_tri)], raise_on_add=raise_on_add,
                              none_feature=none_feature, input_factory=_ReduceInput)
        _wire(src, feats, design_type=1 if parametric else 0,
              base_feature=base_feature if parametric else None)
        return src, feats

    def test_proportion_reduces_and_reports_pct(self):
        src, feats = self._setup(before_tri=1000, after_tri=300)
        out = payload(mo.handler(mesh="H", target="proportion", value=30))
        assert out["reduced"] is True
        assert out["before"]["triangle_count"] == 1000
        assert out["after"]["triangle_count"] == 300
        assert abs(out["reduced_pct"] - 70.0) < 1e-6
        # proportion must be set as a ValueInput (NOT a raw float) - the live API requirement, and the
        # value is the PERCENT as-is (30 = 30%), not 0.30.
        vi = getattr(feats.last_input, "proportion", None)
        assert isinstance(vi, _FakeValueInput)
        assert abs(vi.realValue - 30.0) < 1e-9

    def test_proportion_out_of_range_rejected(self):
        self._setup()
        res = mo.handler(mesh="H", target="proportion", value=150)
        assert res["isError"] is True and "percent" in res["message"]

    def test_facecount_sets_lowercase_field_as_valueinput(self):
        src, feats = self._setup()
        out = payload(mo.handler(mesh="H", target="face_count", value=500))
        assert out["reduced"] is True
        # 'facecount' is set as a ValueInput, not a raw int (the API rejects a bare number); the count
        # is carried as a real (500.0).
        vi = getattr(feats.last_input, "facecount", None)
        assert isinstance(vi, _FakeValueInput)
        assert abs(vi.realValue - 500.0) < 1e-9

    def test_facecount_below_one_rejected(self):
        self._setup()
        res = mo.handler(mesh="H", target="face_count", value=0)
        assert res["isError"] is True and "positive" in res["message"].lower()

    def test_facecount_negative_rejected(self):
        self._setup()
        res = mo.handler(mesh="H", target="face_count", value=-5)
        assert res["isError"] is True and "positive" in res["message"].lower()

    def test_a_fractional_facecount_under_one_is_refused_naming_it(self):
        # 0.5 truncated to int is a ZERO-face target - a request the after<before gate reads as a
        # successful reduce. It is refused instead, and the refusal names the offending value.
        src, feats = self._setup()
        res = mo.handler(mesh="H", target="face_count", value=0.5)
        assert res["isError"] is True
        assert "0.5" in res["message"] and "WHOLE face count" in res["message"]
        assert feats.last_input is None            # nothing was configured, nothing ran

    def test_a_fractional_facecount_is_refused_rather_than_silently_truncated(self):
        # 10.9 -> 10 would decimate to a target the caller never asked for, with no echo of the shift.
        src, feats = self._setup()
        res = mo.handler(mesh="H", target="face_count", value=10.9)
        assert res["isError"] is True
        assert "10.9" in res["message"]
        assert feats.last_input is None

    def test_the_smallest_whole_facecount_is_accepted(self):
        # 1 is the boundary the > 0 guard admits - a whole count, so it runs.
        src, feats = self._setup()
        out = payload(mo.handler(mesh="H", target="face_count", value=1))
        assert out["reduced"] is True
        assert out["face_count_target"] == 1
        assert abs(feats.last_input.facecount.realValue - 1.0) < 1e-9

    def test_an_integral_float_facecount_is_accepted_and_echoed(self):
        # 10.0 IS a whole count (the wire carries numbers, not ints) - accepted, and the integer that
        # reached the feature input is published so the caller can see what was targeted.
        src, feats = self._setup()
        out = payload(mo.handler(mesh="H", target="face_count", value=10.0))
        assert out["reduced"] is True
        assert out["face_count_target"] == 10
        assert abs(feats.last_input.facecount.realValue - 10.0) < 1e-9

    def test_the_applied_facecount_target_is_only_published_for_face_count(self):
        # a proportion reduce has no face-count target to report
        src, feats = self._setup()
        out = payload(mo.handler(mesh="H", target="proportion", value=30))
        assert "face_count_target" not in out

    def test_max_deviation_sets_valueinput_scaled_to_cm(self):
        src, feats = self._setup()
        # max_deviation is a LENGTH: 1 mm input -> 0.1 cm handed to the ValueInput.
        out = payload(mo.handler(mesh="H", target="max_deviation", value=1, units="mm"))
        assert out["reduced"] is True
        vi = getattr(feats.last_input, "maximumDeviation", None)
        assert isinstance(vi, _FakeValueInput)
        assert abs(vi.realValue - 0.1) < 1e-9

    def test_add_failure_surfaces(self):
        self._setup(raise_on_add=True)
        res = mo.handler(mesh="H", target="proportion", value=50)
        assert res["isError"] is True and "failed" in res["message"].lower()

    def test_non_numeric_value_rejected(self):
        self._setup()
        res = mo.handler(mesh="H", target="proportion", value="lots")
        assert res["isError"] is True and "number" in res["message"]

    def test_max_deviation_below_zero_rejected(self):
        self._setup()
        res = mo.handler(mesh="H", target="max_deviation", value=-1)
        assert res["isError"] is True and "positive length" in res["message"]

    def test_missing_reduce_features_collection_errors(self):
        _wire(MeshBody("Scan", tri=1000), None)
        res = mo.handler(mesh="H", target="proportion", value=50)
        assert res["isError"] is True
        assert "meshReduceFeatures collection" in res["message"]

    def test_create_input_none_errors(self):
        src, feats = self._setup()
        feats.createInput = lambda *a: None
        res = mo.handler(mesh="H", target="proportion", value=50)
        assert res["isError"] is True and "returned nothing" in res["message"]

    def test_slow_note_for_large_source_mesh(self):
        # a SOURCE mesh above the slow threshold carries the fire-and-poll advisory note
        src, feats = self._setup(before_tri=300_000, after_tri=100_000)
        out = payload(mo.handler(mesh="H", target="proportion", value=33))
        assert "30s" in out["note"] and "300000" in out["note"]

    def test_no_slow_note_for_small_mesh(self):
        src, feats = self._setup(before_tri=1000, after_tri=300)
        out = payload(mo.handler(mesh="H", target="proportion", value=30))
        assert "note" not in out

    def test_none_feature_is_success_in_place(self):
        # add() returns None in a DIRECT design; mesh_reduce edits the mesh in place, so success is
        # the mesh's updated triangle count, not the None feature return.
        src, feats = self._setup(before_tri=1000, none_feature=True)
        # the in-place reduction lands DURING add(): before_tri (read first) stays 1000, after = 250
        feats._on_add = lambda: setattr(src.displayMesh, "triangleCount", 250)
        out = payload(mo.handler(mesh="H", target="proportion", value=25))
        assert out["reduced"] is True
        assert out["design_mode"] == "direct"
        assert out["base_feature"] is None            # direct opens no scope
        assert out["feature"] is None
        assert out["before"]["triangle_count"] == 1000
        assert out["after"]["triangle_count"] == 250
        assert mo._common.DIRECT_FEATURE_NOTE in out["note"]

    def test_null_feature_in_a_parametric_scope_reports_parametric(self):
        # the scope - not the design's mode - is why the feature is null, so the payload reports the
        # design as PARAMETRIC and names the base feature the reduce actually landed in.
        bf = FakeBaseFeature()
        src, feats = self._setup(before_tri=1000, parametric=True, base_feature=bf,
                                 none_feature=True)
        feats._on_add = lambda: setattr(src.displayMesh, "triangleCount", 400)
        out = payload(mo.handler(mesh="H", target="proportion", value=40))
        assert out["feature"] is None
        assert out["design_mode"] == "parametric"
        assert out["base_feature"] == "BaseFeature1"
        assert "BaseFeature1" in out["note"]
        assert "direct" not in out["note"].lower()

    def test_mode_is_read_before_the_scope_opens(self):
        # designType reads DIRECT while a base-feature edit scope is open, so a mode read taken after
        # the reduce would report 'direct' for a parametric design.
        bf = FakeBaseFeature()
        src, feats = self._setup(before_tri=1000, parametric=True, base_feature=bf,
                                 none_feature=True)
        feats._on_add = lambda: setattr(src.displayMesh, "triangleCount", 400)
        des = mo.app.activeProduct
        real_start = bf.startEdit

        def start_and_flip():
            des.designType = 0        # what the platform reports while the scope is open
            return real_start()

        bf.startEdit = start_and_flip
        out = payload(mo.handler(mesh="H", target="proportion", value=40))
        assert out["design_mode"] == "parametric"

    def test_unreduced_count_is_an_error_not_success(self):
        # the honesty gate: add() succeeded but the triangle count did not decrease -> error, not ok
        src, feats = self._setup(before_tri=1000, after_tri=1000)
        res = mo.handler(mesh="H", target="proportion", value=30)
        assert res["isError"] is True
        assert "did not decrease" in res["message"]

    def test_proportion_100_keep_everything_is_not_gated(self):
        # proportion=100 asks to keep every triangle - an unchanged count is the requested outcome
        src, feats = self._setup(before_tri=1000, after_tri=1000)
        out = payload(mo.handler(mesh="H", target="proportion", value=100))
        assert out["reduced"] is True

    def test_parametric_routes_through_base_feature_scope(self):
        # REGRESSION: in PARAMETRIC the createInput->set->add runs INSIDE the helper's base-feature
        # scope (opened AND finished). The feature add must not be defeated by an undetectable-scope
        # guard - it succeeds.
        bf = FakeBaseFeature()
        src, feats = self._setup(before_tri=1000, after_tri=400, parametric=True, base_feature=bf,
                                 none_feature=True)
        feats._on_add = lambda: setattr(src.displayMesh, "triangleCount", 400)
        out = payload(mo.handler(mesh="H", target="proportion", value=40))
        assert out["reduced"] is True
        assert out["after"]["triangle_count"] == 400
        # the reduce ran inside the helper-opened base-feature scope (leak-proof open/close)
        assert bf._starts == 1 and bf._finishes == 1
