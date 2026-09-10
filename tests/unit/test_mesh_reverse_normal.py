"""Unit tests for ``mesh_reverse_normal.py`` - the MeshReverseNormal feature over a MeshBody.

No live Fusion. The fakes model the measured shape of a flip: the body's SIGNED volume changes sign
and its per-node normal vectors negate componentwise, while is_closed and is_oriented hold still.
The call shape is the mesh one - createInput(mesh) -> add(input) - not surface_reverse_normal's
add(objectCollection).

Pinned (the DoD):
  - either the signed-volume sign flip OR the negated normals confirms the reverse; neither moving
    is an ERROR, and neither being readable is UNVERIFIED.
  - is_closed / is_oriented are never cited as evidence.
  - an all-zero normal list cannot pass the negation test trivially.
  - a DIRECT design returns no feature while the flip LANDS; a PARAMETRIC no-feature return stays
    an honest error.
"""

import types

import pytest

from conftest import (FakeFeatures, MakeComp, MakeDesign, MeshBody, install, load_tool, payload,
                      error_message)

mrn = load_tool("mesh_reverse_normal")

_NORMALS = [-6.666666766007739, -3.3333333830038696, -6.666666766007739,
            -4.08248299237366, -8.16496598474732, 4.08248299237366]
_FLIPPED = [-v for v in _NORMALS]


# ── fakes ────────────────────────────────────────────────────────────────────────────────────────

def _mesh(name="Scan1", normals=None, **kw):
    """conftest's shared MeshBody carrying this file's measured normal array by default."""
    return MeshBody(name=name, normals=_NORMALS if normals is None else normals, **kw)


class _ReverseInput:
    """MeshReverseNormalFeatureInput - `mesh` and `targetBaseFeature`, and nothing else."""
    def __init__(self, mesh):
        self.mesh = mesh
        self.targetBaseFeature = None


class _ReverseFeatures:
    def __init__(self, on_add=None, raise_on_add=False, none_feature=False,
                 create_input_returns_none=False):
        self._on_add = on_add
        self.raise_on_add = raise_on_add
        self.none_feature = none_feature
        self._create_input_returns_none = create_input_returns_none
        self.last_input = None
        self.create_arg = None
        self.add_called = False

    def createInput(self, mesh):
        self.create_arg = mesh
        if self._create_input_returns_none:
            return None
        self.last_input = _ReverseInput(mesh)
        return self.last_input

    def add(self, inp):
        self.add_called = True
        if self.raise_on_add:
            raise RuntimeError("reverse failed")
        if self._on_add is not None:
            self._on_add()
        if self.none_feature:
            return None
        return types.SimpleNamespace(name="MeshReverseNormal1")


class _Features(FakeFeatures):
    """comp.features plus the mesh-reverse-normal collection this tool reaches through."""
    def __init__(self, reverse=None):
        super().__init__()
        self.meshReverseNormalFeatures = reverse


# ── rig ──────────────────────────────────────────────────────────────────────────────────────────

def _rig(monkeypatch, mesh=None, on_add=None, design_type=1, **feat_kw):
    mesh = mesh if mesh is not None else _mesh()
    comp = MakeComp("Comp", mesh_bodies=[mesh])
    mesh.parentComponent = comp
    feats = _ReverseFeatures(on_add=on_add, **feat_kw)
    comp.features = _Features(reverse=feats)
    install(mrn, MakeDesign(comp=comp, design_type=design_type))
    monkeypatch.setattr(mrn._MESH, "resolve", lambda raw: (mesh, None))
    return mesh, comp, feats


def _flip(mesh, volume=True, normals=True):
    """The measured flip: signed volume changes sign, normal vectors negate componentwise."""
    def _apply():
        if volume:
            mesh._volume_cm3 = -mesh._volume_cm3
        if normals:
            mesh._normals = [-v for v in mesh._normals]
    return _apply


@pytest.fixture
def rig(monkeypatch):
    mesh = _mesh(volume=1.0)
    _m, comp, feats = _rig(monkeypatch, mesh=mesh, on_add=_flip(mesh))
    return types.SimpleNamespace(mesh=mesh, comp=comp, feats=feats, monkeypatch=monkeypatch)


# ── the negation test in isolation ───────────────────────────────────────────────────────────────

class TestNegated:
    def test_a_componentwise_negation_is_recognised(self):
        assert mrn._negated(_NORMALS, _FLIPPED) is True

    def test_an_unchanged_list_is_not_a_negation(self):
        assert mrn._negated(_NORMALS, list(_NORMALS)) is False

    def test_an_all_zero_list_cannot_pass_trivially(self):
        # 0 + 0 == 0 satisfies the sum test, so a degenerate list must be refused outright
        assert mrn._negated([0.0, 0.0, 0.0], [0.0, 0.0, 0.0]) is False

    def test_a_length_change_is_not_a_negation(self):
        assert mrn._negated(_NORMALS, _FLIPPED[:3]) is False

    def test_a_partial_negation_is_not_a_negation(self):
        half = [-_NORMALS[0]] + _NORMALS[1:]
        assert mrn._negated(_NORMALS, half) is False


# ── guards ───────────────────────────────────────────────────────────────────────────────────────

class TestInputGuards:
    def test_no_active_design_is_an_error(self, rig):
        rig.monkeypatch.setattr(mrn._common, "design", lambda: None)
        assert "No active design" in error_message(mrn.handler(mesh="H"))

    def test_a_missing_reverse_features_collection_is_an_error(self, monkeypatch):
        _mesh, comp, _feats = _rig(monkeypatch)
        comp.features = _Features(reverse=None)
        assert "meshReverseNormalFeatures" in error_message(mrn.handler(mesh="H"))

    def test_create_input_returning_none_is_an_error(self, monkeypatch):
        _rig(monkeypatch, create_input_returns_none=True)
        assert "returned nothing" in error_message(mrn.handler(mesh="H"))

    def test_an_add_failure_surfaces_and_is_not_swallowed(self, monkeypatch):
        _rig(monkeypatch, raise_on_add=True)
        assert "meshReverseNormalFeatures.add raised" in error_message(mrn.handler(mesh="H"))

    def test_the_input_is_the_mesh_itself_not_an_object_collection(self, rig):
        # the mesh path is createInput(mesh) -> add(input); its BRep namesake takes a collection
        payload(mrn.handler(mesh="H"))
        assert rig.feats.create_arg is rig.mesh
        assert rig.feats.last_input.targetBaseFeature is None


# ── verify-the-effect ────────────────────────────────────────────────────────────────────────────

class TestVerification:
    def test_a_flip_reports_both_the_sign_change_and_the_negated_normals(self, rig):
        out = payload(mrn.handler(mesh="H"))
        assert out["reversed"] is True
        assert out["volume_sign_flipped"] is True
        assert out["normals_negated"] is True
        assert out["mesh"] == "Scan1"
        assert out["feature"] == "MeshReverseNormal1"

    def test_the_note_never_cites_is_closed_or_is_oriented(self, rig):
        out = payload(mrn.handler(mesh="H"))
        assert "do not move" in out["note"]
        assert "is_closed" not in out and "is_oriented" not in out

    def test_an_open_mesh_is_confirmed_by_the_normals_alone(self, monkeypatch):
        # an open mesh READS 0.0 rather than raising, and a zero has no sign to flip - so the gate
        # still comes back null off the value itself, without ever needing an exception.
        mesh = _mesh(is_closed=False, volume=1.0)
        _rig(monkeypatch, mesh=mesh, on_add=_flip(mesh, volume=False))
        assert mesh.volume == 0.0                       # the reading, not a raise
        out = payload(mrn.handler(mesh="H"))
        assert out["volume_sign_flipped"] is None
        assert out["normals_negated"] is True

    def test_unreadable_normals_are_confirmed_by_the_sign_flip_alone(self, monkeypatch):
        mesh = _mesh(volume=1.0, mesh_readable=False)
        _rig(monkeypatch, mesh=mesh, on_add=_flip(mesh, normals=False))
        out = payload(mrn.handler(mesh="H"))
        assert out["volume_sign_flipped"] is True
        assert out["normals_negated"] is None

    def test_a_zero_volume_does_not_count_as_a_sign_flip(self, monkeypatch):
        mesh = _mesh(volume=0.0)
        _rig(monkeypatch, mesh=mesh, on_add=_flip(mesh, volume=False))
        out = payload(mrn.handler(mesh="H"))
        assert out["volume_sign_flipped"] is None
        assert out["normals_negated"] is True

    def test_nothing_moving_is_an_error(self, monkeypatch):
        _rig(monkeypatch)   # add() fires no side effect at all
        msg = error_message(mrn.handler(mesh="H"))
        assert "still points the same way" in msg and "Scan1" in msg

    def test_neither_signal_readable_is_reported_as_unverified(self, monkeypatch):
        mesh = _mesh(is_closed=False, mesh_readable=False)
        _rig(monkeypatch, mesh=mesh, on_add=_flip(mesh))
        msg = error_message(mrn.handler(mesh="H"))
        assert "UNVERIFIED" in msg and "is_oriented" in msg


# ── mode routing (the measured direct-mode None return) ──────────────────────────────────────────

class TestModeRouting:
    def test_direct_mode_returns_no_feature_yet_the_landed_flip_is_success(self, monkeypatch):
        mesh = _mesh(volume=1.0)
        _rig(monkeypatch, mesh=mesh, on_add=_flip(mesh), design_type=0, none_feature=True)
        out = payload(mrn.handler(mesh="H"))
        assert out["reversed"] is True
        assert out["no_timeline_feature"] is True
        assert "feature" not in out
        assert "DIRECT mode" in out["note"]

    def test_direct_mode_with_no_flip_is_still_an_error(self, monkeypatch):
        _rig(monkeypatch, design_type=0, none_feature=True)
        assert "still points the same way" in error_message(mrn.handler(mesh="H"))

    def test_parametric_no_feature_return_stays_an_honest_error(self, monkeypatch):
        mesh = _mesh(volume=1.0)
        _rig(monkeypatch, mesh=mesh, on_add=_flip(mesh), design_type=1, none_feature=True)
        assert "returned no feature" in error_message(mrn.handler(mesh="H"))
