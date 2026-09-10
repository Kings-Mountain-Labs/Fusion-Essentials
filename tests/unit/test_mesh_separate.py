"""Unit tests for ``mesh_separate.py`` - the MeshSeparate feature over a MeshBody.

No live Fusion. The fakes model the measured shape of a shell separation: the input body is
CONSUMED (gone from the component while its held wrapper still answers isValid), and the shells are
minted as AUTO-NAMED new mesh bodies in the same component - so the only way to name the pieces is
a before/after census of that component's mesh body names.

Pinned (the DoD):
  - the pieces are the NEW names read back from the component, never echoed from the request.
  - the input body's name is captured BEFORE the add, since the separate consumes it.
  - a separate that minted fewer than two bodies did not divide the mesh and is an ERROR.
  - an unreadable component census is UNVERIFIED, never a bare ok.
  - the ShellMeshSeparateType assignment is read back - a set that lands nowhere is refused.
  - a DIRECT design returns no feature while the split LANDS; a PARAMETRIC no-feature return stays
    an honest error.
"""

import types

import adsk.fusion
import pytest

from conftest import (FakeFeatures, MakeComp, MakeDesign, MeshBody, go_stale, install, load_tool,
                      payload, error_message)

msp = load_tool("mesh_separate")


# ── fakes ────────────────────────────────────────────────────────────────────────────────────────

class _MeshComp(MakeComp):
    """A component whose meshBodies read RAISES once `dead` is set - the census host that stops
    answering mid-flight."""
    def __init__(self, *args, **kwargs):
        self.dead = False
        super().__init__(*args, **kwargs)

    @property
    def meshBodies(self):
        if self.dead:
            raise RuntimeError("3 : object is no longer valid")
        return self._mesh_bodies

    @meshBodies.setter
    def meshBodies(self, value):
        self._mesh_bodies = value


class _SeparateInput:
    """MeshSeparateFeatureInput. swallow_enum_sets True models the live SWIG trap: the assignment
    is ACCEPTED and lands nowhere, so the input keeps its API default and only a read-back tells."""
    swallow_enum_sets = False

    def __init__(self, mesh):
        self.mesh = mesh
        self.targetBaseFeature = None
        self.isKeepBody = False
        self.isMultipleBodies = False
        self._separate_type = None

    @property
    def meshSeparateType(self):
        return self._separate_type

    @meshSeparateType.setter
    def meshSeparateType(self, value):
        if not self.swallow_enum_sets:
            self._separate_type = value


class _SeparateFeatures:
    """comp.features.meshSeparateFeatures. `on_add` is the census change the split makes."""
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
        self.last_input = _SeparateInput(mesh)
        return self.last_input

    def add(self, inp):
        self.add_called = True
        if self.raise_on_add:
            raise RuntimeError("separate failed")
        if self._on_add is not None:
            self._on_add()
        if self.none_feature:
            return None
        return types.SimpleNamespace(name="MeshSeparate1")


class _Features(FakeFeatures):
    """comp.features plus the mesh-separate collection this tool reaches through."""
    def __init__(self, separate=None):
        super().__init__()
        self.meshSeparateFeatures = separate


# ── rig ──────────────────────────────────────────────────────────────────────────────────────────

def _rig(monkeypatch, meshes=None, on_add=None, design_type=1, **feat_kw):
    """A component holding 4 mesh bodies, the first of which is the separate target."""
    meshes = meshes if meshes is not None else [MeshBody("MeshSepTarget"), MeshBody("MeshShellTarget"),
                                                MeshBody("MeshSmoothTarget"), MeshBody("MeshRevTarget")]
    comp = _MeshComp("Comp", mesh_bodies=meshes)
    for m in meshes:
        m.parentComponent = comp
    feats = _SeparateFeatures(on_add=on_add, **feat_kw)
    comp.features = _Features(separate=feats)
    install(msp, MakeDesign(comp=comp, design_type=design_type))
    monkeypatch.setattr(msp._MESH, "resolve", lambda raw: (meshes[0], None))
    return meshes[0], comp, feats


def _split(comp, target, pieces=("MeshBody5", "MeshBody6"), consume=True):
    """The measured split: the target is consumed and auto-named pieces appear in its component."""
    def _apply():
        if consume:
            comp.meshBodies._items.remove(target)
            # The consumed wrapper keeps answering isValid=True, so nothing raises - but its
            # identity reads are gone, which is what forces the name to be captured BEFORE the add.
            go_stale(target)
        for name in pieces:
            comp.meshBodies._items.append(MeshBody(name, parent=comp))
    return _apply


@pytest.fixture
def rig(monkeypatch):
    """4 mesh bodies in, one 2-shell target split into MeshBody5 + MeshBody6 - the measured 4 -> 5."""
    target = MeshBody("MeshSepTarget")
    meshes = [target, MeshBody("MeshShellTarget"), MeshBody("MeshSmoothTarget"),
              MeshBody("MeshRevTarget")]
    comp = _MeshComp("Comp", mesh_bodies=meshes)
    for m in meshes:
        m.parentComponent = comp
    feats = _SeparateFeatures(on_add=_split(comp, target))
    comp.features = _Features(separate=feats)
    install(msp, MakeDesign(comp=comp, design_type=1))
    monkeypatch.setattr(msp._MESH, "resolve", lambda raw: (target, None))
    return types.SimpleNamespace(mesh=target, comp=comp, feats=feats, monkeypatch=monkeypatch)


# ── the enum assignment ──────────────────────────────────────────────────────────────────────────

class TestSeparateType:
    def test_the_shell_type_is_set_on_the_input(self, rig):
        payload(msp.handler(mesh="H"))
        assert rig.feats.last_input.meshSeparateType is not None

    def test_a_type_the_input_never_took_is_an_error(self, rig):
        # a SWIG proxy accepts an assignment to a name it does not define, so a set that lands
        # nowhere reads back unchanged and only the read-back catches it
        rig.monkeypatch.setattr(_SeparateInput, "swallow_enum_sets", True)
        msg = error_message(msp.handler(mesh="H"))
        assert "did not take" in msg and "shell separation" in msg
        assert rig.feats.add_called is False

    def test_a_missing_enum_family_is_refused_before_the_mutation(self, rig):
        class _NoMembers:
            def __getattr__(self, name):
                raise AttributeError(name)
        rig.monkeypatch.setattr(adsk.fusion, "MeshSeparateTypes", _NoMembers())
        assert "not available" in error_message(msp.handler(mesh="H"))
        assert rig.feats.add_called is False


# ── guards ───────────────────────────────────────────────────────────────────────────────────────

class TestInputGuards:
    def test_no_active_design_is_an_error(self, rig):
        rig.monkeypatch.setattr(msp._common, "design", lambda: None)
        assert "No active design" in error_message(msp.handler(mesh="H"))

    def test_a_missing_separate_features_collection_is_an_error(self, monkeypatch):
        _mesh, comp, _feats = _rig(monkeypatch)
        comp.features = _Features(separate=None)
        assert "meshSeparateFeatures" in error_message(msp.handler(mesh="H"))

    def test_create_input_returning_none_is_an_error(self, monkeypatch):
        _rig(monkeypatch, create_input_returns_none=True)
        assert "returned nothing" in error_message(msp.handler(mesh="H"))

    def test_an_add_failure_surfaces_and_is_not_swallowed(self, monkeypatch):
        _rig(monkeypatch, raise_on_add=True)
        assert "meshSeparateFeatures.add raised" in error_message(msp.handler(mesh="H"))


# ── verify-the-effect (the component census) ─────────────────────────────────────────────────────

class TestVerification:
    def test_the_pieces_are_the_new_names_read_back_from_the_component(self, rig):
        out = payload(msp.handler(mesh="H"))
        assert out["separated"] is True
        assert out["pieces"] == ["MeshBody5", "MeshBody6"]
        assert out["piece_count"] == 2
        assert out["mesh_body_count"] == {"before": 4, "after": 5}
        assert out["feature"] == "MeshSeparate1"

    def test_the_consumed_input_is_named_from_the_pre_mutation_read(self, rig):
        # the split consumes the body: its identity reads are gone afterwards while isValid still
        # answers True, so a name captured after the add would come back null
        out = payload(msp.handler(mesh="H"))
        assert out["mesh"] == "MeshSepTarget"
        assert rig.mesh.isValid is True
        assert out["input_consumed"] is True
        assert "was consumed" in out["note"]

    def test_the_census_is_taken_on_the_pre_mutation_component(self, monkeypatch):
        # census_host resolves the owning component ONCE, before the mutation. A wrapper that
        # starts answering a DIFFERENT component afterwards must not swing the census onto it -
        # the difference of two unrelated counts is a fabricated verdict, not a measured one.
        target = MeshBody("MeshSepTarget")
        home = _MeshComp("Home", mesh_bodies=[target])
        elsewhere = _MeshComp("Elsewhere",
                              mesh_bodies=[MeshBody("X"), MeshBody("Y"), MeshBody("Z")])
        target.parentComponent = home

        def _apply():
            home.meshBodies._items.remove(target)
            for name in ("MeshBody5", "MeshBody6"):
                home.meshBodies._items.append(MeshBody(name, parent=home))
            target.parentComponent = elsewhere

        feats = _SeparateFeatures(on_add=_apply)
        home.features = _Features(separate=feats)
        elsewhere.features = _Features(separate=feats)
        install(msp, MakeDesign(comp=home, design_type=1))
        monkeypatch.setattr(msp._MESH, "resolve", lambda raw: (target, None))
        out = payload(msp.handler(mesh="H"))
        assert out["component"] == "Home"
        assert out["pieces"] == ["MeshBody5", "MeshBody6"]
        assert out["mesh_body_count"] == {"before": 1, "after": 2}

    def test_a_surviving_input_is_not_claimed_as_consumed(self, monkeypatch):
        target = MeshBody("MeshSepTarget")
        meshes = [target, MeshBody("Other")]
        comp = _MeshComp("Comp", mesh_bodies=meshes)
        for m in meshes:
            m.parentComponent = comp
        feats = _SeparateFeatures(on_add=_split(comp, target, consume=False))
        comp.features = _Features(separate=feats)
        install(msp, MakeDesign(comp=comp, design_type=1))
        monkeypatch.setattr(msp._MESH, "resolve", lambda raw: (target, None))
        out = payload(msp.handler(mesh="H"))
        assert out["input_consumed"] is False
        assert "was consumed" not in out["note"]

    def test_a_split_into_three_shells_reports_all_three(self, monkeypatch):
        target = MeshBody("MeshSepTarget")
        comp = _MeshComp("Comp", mesh_bodies=[target])
        target.parentComponent = comp
        feats = _SeparateFeatures(on_add=_split(comp, target, pieces=("A", "B", "C")))
        comp.features = _Features(separate=feats)
        install(msp, MakeDesign(comp=comp, design_type=1))
        monkeypatch.setattr(msp._MESH, "resolve", lambda raw: (target, None))
        out = payload(msp.handler(mesh="H"))
        assert out["pieces"] == ["A", "B", "C"]
        assert out["piece_count"] == 3

    def test_no_new_bodies_is_an_error_naming_the_census(self, monkeypatch):
        _rig(monkeypatch)   # add() fires no side effect at all
        msg = error_message(msp.handler(mesh="H"))
        assert "did not divide" in msg and "4 mesh bodies before and 4 after" in msg

    def test_a_single_shell_mesh_is_refused_and_the_rename_is_named(self, monkeypatch):
        # the measured single-shell signature: the input is CONSUMED, ONE body appears and the
        # count is unchanged (2 -> 2) - the same shell under a new auto-name
        target = MeshBody("MeshBody4")
        other = MeshBody("MeshBody3")
        comp = _MeshComp("Comp", mesh_bodies=[other, target])
        target.parentComponent = other.parentComponent = comp
        feats = _SeparateFeatures(on_add=_split(comp, target, pieces=("MeshBody5",)))
        comp.features = _Features(separate=feats)
        install(msp, MakeDesign(comp=comp, design_type=1))
        monkeypatch.setattr(msp._MESH, "resolve", lambda raw: (target, None))
        msg = error_message(msp.handler(mesh="H"))
        assert "2 mesh bodies before and 2 after" in msg
        assert "1 new body name appeared" in msg and "name(s)" not in msg
        assert "'MeshBody4' is no longer in the component" in msg
        assert "'MeshBody5' is that same shell renamed" in msg

    def test_a_surviving_input_with_one_new_body_claims_no_rename(self, monkeypatch):
        # the rename clause is only true when the input is GONE - a surviving input means the new
        # body is something else, and the error must not say otherwise
        target = MeshBody("MeshSepTarget")
        comp = _MeshComp("Comp", mesh_bodies=[target])
        target.parentComponent = comp
        feats = _SeparateFeatures(on_add=_split(comp, target, pieces=("OnlyOne",), consume=False))
        comp.features = _Features(separate=feats)
        install(msp, MakeDesign(comp=comp, design_type=1))
        monkeypatch.setattr(msp._MESH, "resolve", lambda raw: (target, None))
        msg = error_message(msp.handler(mesh="H"))
        assert "1 new body name appeared" in msg and "at least 2" in msg
        assert "renamed" not in msg

    def test_no_new_bodies_uses_the_plural_form(self, monkeypatch):
        _rig(monkeypatch)
        assert "0 new body names appeared" in error_message(msp.handler(mesh="H"))

    def test_an_unreadable_census_is_reported_as_unverified(self, monkeypatch):
        _mesh, comp, feats = _rig(monkeypatch)
        feats._on_add = lambda: setattr(comp, "dead", True)
        assert "UNVERIFIED" in error_message(msp.handler(mesh="H"))


# ── mode routing (the measured direct-mode None return) ──────────────────────────────────────────

class TestModeRouting:
    def test_direct_mode_returns_no_feature_yet_the_landed_split_is_success(self, monkeypatch):
        target = MeshBody("MeshSepTarget")
        comp = _MeshComp("Comp", mesh_bodies=[target])
        target.parentComponent = comp
        feats = _SeparateFeatures(on_add=_split(comp, target), none_feature=True)
        comp.features = _Features(separate=feats)
        install(msp, MakeDesign(comp=comp, design_type=0))
        monkeypatch.setattr(msp._MESH, "resolve", lambda raw: (target, None))
        out = payload(msp.handler(mesh="H"))
        assert out["pieces"] == ["MeshBody5", "MeshBody6"]
        assert out["no_timeline_feature"] is True
        assert "feature" not in out
        assert "DIRECT mode" in out["note"]

    def test_direct_mode_with_no_new_bodies_is_still_an_error(self, monkeypatch):
        _rig(monkeypatch, design_type=0, none_feature=True)
        assert "did not divide" in error_message(msp.handler(mesh="H"))

    def test_parametric_no_feature_return_stays_an_honest_error(self, monkeypatch):
        target = MeshBody("MeshSepTarget")
        comp = _MeshComp("Comp", mesh_bodies=[target])
        target.parentComponent = comp
        feats = _SeparateFeatures(on_add=_split(comp, target), none_feature=True)
        comp.features = _Features(separate=feats)
        install(msp, MakeDesign(comp=comp, design_type=1))
        monkeypatch.setattr(msp._MESH, "resolve", lambda raw: (target, None))
        assert "returned no feature" in error_message(msp.handler(mesh="H"))

    def test_no_base_feature_scope_is_opened(self, rig):
        payload(msp.handler(mesh="H"))
        assert rig.feats.last_input.targetBaseFeature is None
