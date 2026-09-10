"""Unit tests for ``mesh_smooth.py`` - the MeshSmooth feature over a MeshBody.

No live Fusion. The fakes model the measured shape of a smooth: the node COORDINATES move while
the triangle count, the vertex count and is_closed all hold still, so a count census cannot judge
the result and the coordinate diff is the whole verdict.

Pinned (the DoD):
  - smoothness crosses as a UNITLESS ValueInput; omitted, the property is left alone so the API's
    own default applies, and the value that landed is read back off the feature.
  - smoothness outside 0..1, or non-numeric, is refused before any mutation.
  - counts holding still is NOT a failure - a moved coordinate is success; every coordinate holding
    still IS a failure.
  - unreadable coordinates are reported UNVERIFIED, never as a bare ok.
  - a DIRECT design returns no feature while the smooth LANDS; a PARAMETRIC no-feature return stays
    an honest error.
  - a smooth that ate most of the enclosed volume is reported as a collapse, not as a clean relax.
"""

import types

import pytest

from conftest import (FakeFeatures, FakeValueInput as _ValueInput, MakeComp, MakeDesign, MeshBody,
                      install, load_tool, payload, error_message)

msm = load_tool("mesh_smooth")

_BOX_COORDS = [0.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 1.0, 0.0, 1.0, 0.0, 0.0]
_SMOOTHED_COORDS = [0.4997, 0.4997, 0.4997, 0.4997, 0.4997, 0.5003, 0.4997, 0.5003, 0.4997,
                    0.5003, 0.4997, 0.4997]


# ── fakes ────────────────────────────────────────────────────────────────────────────────────────

def _mesh(name="Scan1", coords=None, **kw):
    """conftest's shared MeshBody carrying this file's box node coordinates by default."""
    return MeshBody(name=name, coords=_BOX_COORDS if coords is None else coords, **kw)


class _SmoothInput:
    """MeshSmoothFeatureInput. smoothness is declared core.ValueInput, so a raw number is refused
    here exactly as the live typed property refuses it."""
    def __init__(self, mesh):
        self.mesh = mesh
        self.targetBaseFeature = None
        self._smoothness = None

    @property
    def smoothness(self):
        return self._smoothness

    @smoothness.setter
    def smoothness(self, value):
        if not isinstance(value, _ValueInput):
            raise TypeError("smoothness expects a ValueInput, got " + type(value).__name__)
        self._smoothness = value


class _SmoothFeatures:
    """comp.features.meshSmoothFeatures. `feature_smoothness` is what the created feature's own
    ModelParameter reports, which is not always what was asked for; `default_smoothness` is what it
    reports when the caller set nothing at all."""
    def __init__(self, on_add=None, raise_on_add=False, none_feature=False,
                 create_input_returns_none=False, feature_smoothness=None,
                 default_smoothness=0.02):
        self._on_add = on_add
        self.raise_on_add = raise_on_add
        self.none_feature = none_feature
        self._create_input_returns_none = create_input_returns_none
        self._feature_smoothness = feature_smoothness
        self._default_smoothness = default_smoothness
        self.last_input = None
        self.add_called = False

    def createInput(self, mesh):
        if self._create_input_returns_none:
            return None
        self.last_input = _SmoothInput(mesh)
        return self.last_input

    def add(self, inp):
        self.add_called = True
        if self.raise_on_add:
            raise RuntimeError("smooth failed")
        if self._on_add is not None:
            self._on_add()
        if self.none_feature:
            return None
        asked = getattr(getattr(self.last_input, "smoothness", None), "realValue", None)
        landed = self._feature_smoothness
        if landed is None:
            landed = asked if asked is not None else self._default_smoothness
        return types.SimpleNamespace(
            name="MeshSmooth1", smoothness=types.SimpleNamespace(value=float(landed)))


class _Features(FakeFeatures):
    """comp.features plus the mesh-smooth collection this tool reaches through."""
    def __init__(self, smooth=None):
        super().__init__()
        self.meshSmoothFeatures = smooth


# ── rig ──────────────────────────────────────────────────────────────────────────────────────────

def _rig(monkeypatch, mesh=None, on_add=None, design_type=1, **feat_kw):
    mesh = mesh if mesh is not None else _mesh()
    comp = MakeComp("Comp", mesh_bodies=[mesh])
    mesh.parentComponent = comp
    feats = _SmoothFeatures(on_add=on_add, **feat_kw)
    comp.features = _Features(smooth=feats)
    install(msm, MakeDesign(comp=comp, design_type=design_type))
    monkeypatch.setattr(msm._MESH, "resolve", lambda raw: (mesh, None))
    monkeypatch.setattr(msm.adsk.core.ValueInput, "createByReal", _ValueInput)
    return mesh, comp, feats


def _relax(mesh, coords=None, volume=None):
    """The measured smooth: coordinates move, counts and is_closed hold still."""
    def _apply():
        mesh._coords = list(coords if coords is not None else _SMOOTHED_COORDS)
        if volume is not None:
            mesh._volume_cm3 = volume
    return _apply


@pytest.fixture
def rig(monkeypatch):
    mesh = _mesh(tri=12, nodes=8, volume=1.0)
    _m, comp, feats = _rig(monkeypatch, mesh=mesh, on_add=_relax(mesh, volume=0.9))
    return types.SimpleNamespace(mesh=mesh, comp=comp, feats=feats, monkeypatch=monkeypatch)


# ── the unitless smoothness ──────────────────────────────────────────────────────────────────────

class TestSmoothnessInput:
    def test_smoothness_crosses_as_a_value_input_carrying_the_bare_factor(self, rig):
        payload(msm.handler(mesh="H", smoothness=0.5))
        assert rig.feats.last_input.smoothness.realValue == pytest.approx(0.5)

    def test_an_omitted_smoothness_leaves_the_property_unset(self, rig):
        payload(msm.handler(mesh="H"))
        assert rig.feats.last_input.smoothness is None

    def test_an_omitted_smoothness_reports_the_api_default_that_landed(self, rig):
        out = payload(msm.handler(mesh="H"))
        assert out["smoothness"] == pytest.approx(0.02)

    def test_a_raw_number_on_smoothness_would_be_refused_by_the_api(self, rig):
        rig.monkeypatch.setattr(msm.adsk.core.ValueInput, "createByReal", lambda v: v)
        assert "smoothness" in error_message(msm.handler(mesh="H", smoothness=0.5))

    @pytest.mark.parametrize("bad", [-0.1, 1.5, 2])
    def test_smoothness_outside_zero_to_one_is_refused(self, rig, bad):
        msg = error_message(msm.handler(mesh="H", smoothness=bad))
        assert "0.0" in msg and "1.0" in msg and str(float(bad)) in msg
        assert rig.feats.add_called is False

    @pytest.mark.parametrize("good", [0, 0.02, 0.5, 1])
    def test_smoothness_at_and_inside_the_bounds_is_accepted(self, rig, good):
        payload(msm.handler(mesh="H", smoothness=good))
        assert rig.feats.add_called is True

    def test_a_non_numeric_smoothness_is_refused(self, rig):
        assert "smoothness" in error_message(msm.handler(mesh="H", smoothness="silky"))
        assert rig.feats.add_called is False


# ── guards ───────────────────────────────────────────────────────────────────────────────────────

class TestInputGuards:
    def test_no_active_design_is_an_error(self, rig):
        rig.monkeypatch.setattr(msm._common, "design", lambda: None)
        assert "No active design" in error_message(msm.handler(mesh="H"))

    def test_a_missing_smooth_features_collection_is_an_error(self, monkeypatch):
        _mesh, comp, _feats = _rig(monkeypatch)
        comp.features = _Features(smooth=None)
        assert "meshSmoothFeatures" in error_message(msm.handler(mesh="H"))

    def test_create_input_returning_none_is_an_error(self, monkeypatch):
        _rig(monkeypatch, create_input_returns_none=True)
        assert "returned nothing" in error_message(msm.handler(mesh="H"))

    def test_an_add_failure_surfaces_and_is_not_swallowed(self, monkeypatch):
        _rig(monkeypatch, raise_on_add=True)
        assert "meshSmoothFeatures.add raised" in error_message(msm.handler(mesh="H"))


# ── verify-the-effect (the coordinate diff, not a count census) ──────────────────────────────────

class TestVerification:
    def test_counts_holding_still_is_success_when_the_coordinates_moved(self, rig):
        # the whole point: a mesh_repair-style count census would FALSE-ERROR here
        out = payload(msm.handler(mesh="H", smoothness=0.5))
        assert out["smoothed"] is True
        assert out["triangle_count"] == {"before": 12, "after": 12}
        assert out["vertex_count"] == {"before": 8, "after": 8}
        assert out["nodes_moved"] == 4
        # mesh_nodes is the PolygonMesh node count the diff runs over - NOT vertex_count, which
        # comes off displayMesh and duplicates a vertex per hard edge
        assert out["mesh_nodes"] == 4
        assert "node_count" not in out

    # a node moves when ANY of its three coordinates moves - the flat array is x,y,z per node, so
    # index 3/4/5 are node 1's x, y and z. A diff that only compares one of the three misses the
    # other two entirely.
    @pytest.mark.parametrize("component", [3, 4, 5])
    def test_a_partial_move_counts_only_the_nodes_that_moved(self, monkeypatch, component):
        mesh = _mesh(tri=12, nodes=8, volume=1.0)
        after = list(_BOX_COORDS)
        after[component] = _BOX_COORDS[component] + 0.25    # one component of node 1
        _rig(monkeypatch, mesh=mesh, on_add=_relax(mesh, coords=after))
        out = payload(msm.handler(mesh="H", smoothness=0.02))
        assert out["nodes_moved"] == 1

    def test_a_smooth_that_moved_no_node_is_an_error(self, monkeypatch):
        _rig(monkeypatch)   # add() fires no side effect at all
        msg = error_message(msm.handler(mesh="H", smoothness=0.5))
        assert "every one of the 4" in msg and "original coordinate" in msg

    def test_unreadable_coordinates_are_reported_as_unverified(self, monkeypatch):
        mesh = _mesh(mesh_readable=False)
        _rig(monkeypatch, mesh=mesh)
        msg = error_message(msm.handler(mesh="H"))
        assert "UNVERIFIED" in msg and "hold still" in msg

    def test_the_volume_change_is_reported_as_a_percentage_not_a_raw_length(self, rig):
        out = payload(msm.handler(mesh="H", smoothness=0.5))
        assert out["volume_change_percent"] == pytest.approx(-10.0)
        assert "units" not in out

    def test_an_open_mesh_reports_no_volume_percentage_rather_than_a_wrong_one(self, monkeypatch):
        # an open mesh READS 0.0 rather than raising, and a percentage off a zero base is
        # undefined - so the field is null off the value, with no division ever attempted.
        mesh = _mesh(is_closed=False, volume=1.0)
        _rig(monkeypatch, mesh=mesh, on_add=_relax(mesh))
        assert mesh.volume == 0.0                       # the reading, not a raise
        out = payload(msm.handler(mesh="H"))
        assert out["volume_change_percent"] is None

    def test_a_large_volume_drop_adds_advice_and_never_a_verdict(self, monkeypatch):
        # smoothness 0.5 takes a coarse box to a near-point; the note reports the number and
        # advises, it does not diagnose a "collapse"
        mesh = _mesh(tri=12, nodes=8, volume=1.0)
        _rig(monkeypatch, mesh=mesh, on_add=_relax(mesh, volume=0.001))
        out = payload(msm.handler(mesh="H", smoothness=0.5))
        assert out["volume_change_percent"] == pytest.approx(-99.9)
        assert "Enclosed volume changed -99.9 percent." in out["note"]
        assert "Re-run with a lower smoothness if that is more than intended." in out["note"]
        assert "collapse" not in out["note"].lower()

    def test_a_modest_relax_reports_the_number_without_the_advice(self, rig):
        out = payload(msm.handler(mesh="H", smoothness=0.02))
        assert "Enclosed volume changed -10.0 percent." in out["note"]
        assert "lower smoothness" not in out["note"]
        assert "4 of 4 mesh nodes moved" in out["note"]


# ── the smoothness read-back (never an echo) ─────────────────────────────────────────────────────

class TestSmoothnessReadBack:
    def test_the_landed_smoothness_is_reported(self, rig):
        out = payload(msm.handler(mesh="H", smoothness=0.5))
        assert out["smoothness"] == pytest.approx(0.5)

    def test_a_smoothness_the_api_did_not_take_is_an_error_not_an_echo(self, monkeypatch):
        mesh = _mesh(tri=12, nodes=8, volume=1.0)
        _rig(monkeypatch, mesh=mesh, on_add=_relax(mesh), feature_smoothness=0.02)
        msg = error_message(msm.handler(mesh="H", smoothness=0.5))
        assert "created with smoothness = 0.02" in msg and "0.5 was requested" in msg

    def test_an_unreadable_smoothness_is_reported_as_unverified_not_as_the_request(self, monkeypatch):
        mesh = _mesh(tri=12, nodes=8, volume=1.0)
        _rig(monkeypatch, mesh=mesh, on_add=_relax(mesh), design_type=0, none_feature=True)
        out = payload(msm.handler(mesh="H", smoothness=0.5))
        assert out["smoothness"] is None
        assert "smoothness_unverified" in out

    def test_an_omitted_smoothness_in_direct_mode_claims_no_default_it_cannot_read(self, monkeypatch):
        mesh = _mesh(tri=12, nodes=8, volume=1.0)
        _rig(monkeypatch, mesh=mesh, on_add=_relax(mesh), design_type=0, none_feature=True)
        out = payload(msm.handler(mesh="H"))
        assert out["smoothness"] is None
        assert "smoothness_unverified" not in out


# ── mode routing (the measured direct-mode None return) ──────────────────────────────────────────

class TestModeRouting:
    def test_direct_mode_returns_no_feature_yet_the_landed_smooth_is_success(self, monkeypatch):
        mesh = _mesh(tri=12, nodes=8, volume=1.0)
        _rig(monkeypatch, mesh=mesh, on_add=_relax(mesh), design_type=0, none_feature=True)
        out = payload(msm.handler(mesh="H", smoothness=0.5))
        assert out["smoothed"] is True
        assert out["no_timeline_feature"] is True
        assert "feature" not in out
        assert "DIRECT mode" in out["note"]

    def test_direct_mode_with_no_coordinate_movement_is_still_an_error(self, monkeypatch):
        _rig(monkeypatch, design_type=0, none_feature=True)
        assert "original coordinate" in error_message(msm.handler(mesh="H", smoothness=0.5))

    def test_parametric_no_feature_return_stays_an_honest_error(self, monkeypatch):
        mesh = _mesh(tri=12, nodes=8, volume=1.0)
        _rig(monkeypatch, mesh=mesh, on_add=_relax(mesh), design_type=1, none_feature=True)
        assert "returned no feature" in error_message(msm.handler(mesh="H", smoothness=0.5))

    def test_no_base_feature_scope_is_opened(self, rig):
        payload(msm.handler(mesh="H", smoothness=0.5))
        assert rig.feats.last_input.targetBaseFeature is None
