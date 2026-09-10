"""Tests for `mesh_delete` - the mode branch (MeshRemoveFeatures parametric / deleteMe direct), the
gated delete, and the component-scoped, count-based survivor re-check. Modeled on pmi_delete's test
file; `_MESH.resolve` is monkeypatched for most tests so they exercise mesh_delete's own logic, and
`TestDesignWideMeshResolution` drives the real resolver against a multi-component design.

Behavior these tests pin: dir(occurrence) LISTS meshBodies but READING occ.meshBodies raises
AttributeError while occ.bRepBodies reads fine;
after a successful delete a held wrapper's isValid stays True and an entityToken lookup still
resolves the pre-remove body; and a same-named mesh in another component is not a survivor of this
delete, so the verdict is n_after == n_before - 1 within the owning component."""

import json
from types import SimpleNamespace

import pytest

from conftest import (FakeFeatures, MakeComp, MakeDesign, MeshBody, install, load_tool,
                      error_message)

md = load_tool("mesh_delete")


def _payload(result):
    assert result["isError"] is False, result
    return json.loads(result["content"][0]["text"])


# ── fakes ──────────────────────────────────────────────────────────────────────────────────────

class _MeshBody(MeshBody):
    """Records what deleteMe() answered, so a test can tell a delete that was DECLINED from one that
    was never attempted. The removal itself is the shared fake's; `deletes=False` is the decline."""
    deleted = False

    def deleteMe(self):
        self.deleted = super().deleteMe()
        return self.deleted


class _FakeMeshRemoveFeatures:
    """meshRemoveFeatures - createInput(coll) -> input; add(input) -> feature or None (non-parametric
    contract, matching mesh_reduce/mesh_remesh). on_add is the in-place removal side effect."""
    def __init__(self, raise_on_add=False, none_feature=False, feat_name="MeshRemove1", on_add=None,
                 create_input_returns_none=False):
        self.raise_on_add = raise_on_add
        self.none_feature = none_feature
        self._feat_name = feat_name
        self._on_add = on_add
        self._create_input_returns_none = create_input_returns_none
        self.last_input = None
        self.last_coll = None

    def createInput(self, bodies):
        # LIVE-VERIFIED: MeshRemoveFeatures.createInput wants a plain Python LIST of MeshBody (the
        # std::vector<Ptr<MeshBody>> SWIG binding) - an ObjectCollection raises live with "argument 2
        # of type 'std::vector< adsk::core::Ptr< adsk::fusion::MeshBody > >'". Reject anything else so
        # a regression back to ObjectCollection.create() surfaces here, not just in a downstream assert.
        if not isinstance(bodies, list):
            raise TypeError(
                f"MeshRemoveFeatures.createInput requires a plain list of MeshBody, got "
                f"{type(bodies).__name__}")
        self.last_coll = bodies
        if self._create_input_returns_none:
            return None
        self.last_input = SimpleNamespace(inputBodies=bodies)
        return self.last_input

    def add(self, inp):
        if self.raise_on_add:
            raise RuntimeError("remove failed")
        if self._on_add is not None:
            self._on_add()
        if self.none_feature:
            return None
        return SimpleNamespace(name=self._feat_name)


class _FakeFeatures(FakeFeatures):
    """comp.features plus the mesh-remove collection this tool reaches through, with baseFeatures
    counting its own reads - the seam a reintroduced base-feature scope would show up on."""
    def __init__(self, mesh_remove=None):
        self._base_feature_reads = 0
        super().__init__()
        self.meshRemoveFeatures = mesh_remove

    @property
    def baseFeatures(self):
        self._base_feature_reads += 1
        return self._base_features

    @baseFeatures.setter
    def baseFeatures(self, value):
        self._base_features = value


# ── DIRECT mode: MeshBody.deleteMe() ─────────────────────────────────────────────────────────────

class TestDirectDelete:
    @pytest.fixture
    def rig(self, monkeypatch):
        comp = MakeComp("Comp", mesh_bodies=[])
        mesh = _MeshBody()
        mesh.parentComponent = comp
        comp.meshBodies._items.append(mesh)
        design = install(md, MakeDesign(comp=comp, design_type=0))
        monkeypatch.setattr(md._MESH, "resolve", lambda raw: (mesh, None))
        return SimpleNamespace(mesh=mesh, comp=comp, design=design, monkeypatch=monkeypatch)

    def test_deletes_and_confirms_gone(self, rig):
        out = _payload(md.handler(mesh="H"))
        assert out["deleted"] == "Scan1"
        assert out["deleted_via"] == "MeshBody.deleteMe"
        assert out["component"] == "Comp"
        assert out["remaining_meshes"] == 0
        assert rig.mesh.deleted is True

    def test_a_declined_delete_is_an_error(self, rig):
        rig.mesh._deletes = False
        msg = error_message(md.handler(mesh="H"))
        # the survivor count is READ back, not asserted from the false answer
        assert "answered false" in msg and "1 mesh(es) named 'Scan1'" in msg
        # nothing changed - the mesh is still in the collection
        assert rig.mesh in rig.comp.meshBodies._items

    def test_deleteMe_raising_surfaces_not_swallowed(self, rig):
        def boom():
            raise RuntimeError("boom")
        rig.mesh.deleteMe = boom
        assert "deleteMe() failed" in error_message(md.handler(mesh="H"))

    def test_a_survivor_after_success_is_an_error(self, rig):
        # deleteMe() LIES (returns True) but the mesh still resolves afterwards - never trust the bool
        # alone; the survivor re-read must catch it.
        rig.mesh.deleteMe = lambda: True
        assert "still resolves" in error_message(md.handler(mesh="H"))

    def test_stale_isValid_true_is_not_a_false_alarm(self, rig):
        # A held wrapper's isValid stays True after a genuine delete, so it is never consulted.
        out = _payload(md.handler(mesh="H"))
        assert out["deleted"] == "Scan1"
        assert rig.mesh.isValid is True   # still true - and that's fine, it isn't consulted

    def test_stale_entity_token_lookup_is_not_a_false_alarm(self, rig):
        # LIVE FACT: findEntityByToken(token) still resolves the PRE-REMOVE body after a genuine
        # delete (a historical-resolution artifact, the same trap pmi_delete documents for a
        # suppressed PMI). The collection is genuinely empty (a fresh walk would see that) - the
        # survivor verdict must come from that walk alone, never from a token re-resolution.
        rig.design._tokens[rig.mesh.entityToken] = rig.mesh
        out = _payload(md.handler(mesh="H"))
        assert out["deleted"] == "Scan1"
        assert out["remaining_meshes"] == 0
        # the stale lookup still "finds" it - proving the handler never consulted this path
        assert rig.design.findEntityByToken(rig.mesh.entityToken) == [rig.mesh]

    def test_two_same_named_meshes_in_one_component_deleting_one_still_succeeds(self, rig):
        # two meshes named 'Scan1' sit in the SAME component. Deleting the resolved one is a genuine
        # success even though its same-named sibling remains: the count-based check only requires
        # exactly ONE FEWER 'Scan1' in this component (n_after == n_before - 1 == 1), not zero - a
        # bare "does this name still appear" check would wrongly fail this.
        duplicate = _MeshBody(name="Scan1", token="MTOK::Other")
        duplicate.parentComponent = rig.comp
        rig.comp.meshBodies._items.append(duplicate)   # a second "Scan1" already present pre-delete
        out = _payload(md.handler(mesh="H"))
        assert out["deleted"] == "Scan1"
        assert rig.mesh not in rig.comp.meshBodies._items   # the resolved one really is gone
        assert duplicate in rig.comp.meshBodies._items       # its same-named sibling is untouched
        assert out["remaining_meshes"] == 1

    def test_same_named_mesh_in_a_different_component_is_not_a_false_alarm(self, monkeypatch):
        # A handle resolves one of two same-named meshes across components; the untouched copy in
        # the other component is not a survivor of this delete.
        comp_a = MakeComp("CompA", mesh_bodies=[])
        comp_b = MakeComp("CompB", mesh_bodies=[])
        mesh_a = _MeshBody(name="Twin", token="MTOK::A")
        mesh_a.parentComponent = comp_a
        comp_a.meshBodies._items.append(mesh_a)
        mesh_b = _MeshBody(name="Twin", token="MTOK::B")
        mesh_b.parentComponent = comp_b
        comp_b.meshBodies._items.append(mesh_b)

        install(md, MakeDesign(comp=comp_a, design_type=0, all_components=[comp_a, comp_b]))
        monkeypatch.setattr(md._MESH, "resolve", lambda raw: (mesh_a, None))   # a HANDLE, not a name

        out = _payload(md.handler(mesh="<handle for CompA's Twin>"))
        assert out["deleted"] == "Twin"
        assert out["component"] == "CompA"
        assert mesh_a not in comp_a.meshBodies._items   # CompA's copy is genuinely gone
        assert mesh_b in comp_b.meshBodies._items        # CompB's copy is untouched and irrelevant
        assert out["remaining_meshes"] == 1              # only CompB's 'Twin' remains, design-wide

    def test_no_active_design_is_an_error(self, rig):
        rig.monkeypatch.setattr(md._common, "design", lambda: None)
        assert "No active design" in error_message(md.handler(mesh="H"))

    def test_resolver_error_surfaces(self, rig):
        rig.monkeypatch.setattr(md._MESH, "resolve", lambda raw: (None, "No mesh named 'X'."))
        assert "No mesh named" in error_message(md.handler(mesh="X"))

    def test_ambiguous_name_is_refused_not_guessed(self, rig):
        # the resolver refuses an ambiguous name with candidates rather than grabbing the first -
        # mesh_delete must surface that refusal as-is and attempt no deletion.
        rig.monkeypatch.setattr(
            md._MESH, "resolve",
            lambda raw: (None, "'mesh': 'Scan' is ambiguous - it names 2 bodies ('Scan' in Root, "
                                "'Scan' in Sub). Pass a find_geometry 'handle' to pick the exact one."))
        msg = error_message(md.handler(mesh="Scan"))
        assert "ambiguous" in msg
        assert rig.mesh.deleted is False   # no deletion was attempted


# ── PARAMETRIC mode: MeshRemoveFeatures (at NORMAL parametric scope, NO base-feature edit scope) ───
#
# LIVE-VERIFIED: wrapping this add() in a base-feature edit scope raises "Mesh remove only available
# in parametric mode" - inside an open scope the design PRESENTS as direct, so the feature refuses.
# These tests assert the handler calls createInput/add DIRECTLY (no scope-open/close machinery).

class TestParametricDelete:
    def _setup(self, monkeypatch, raise_on_add=False, none_feature=False,
               create_input_returns_none=False, remove_on_delete=True):
        comp = MakeComp("Comp", mesh_bodies=[])
        mesh = _MeshBody()
        mesh.parentComponent = comp
        comp.meshBodies._items.append(mesh)

        def _on_add():
            if remove_on_delete and mesh in comp.meshBodies._items:
                comp.meshBodies._items.remove(mesh)   # isValid intentionally left True - see _MeshBody

        mrf = _FakeMeshRemoveFeatures(raise_on_add=raise_on_add, none_feature=none_feature,
                                      create_input_returns_none=create_input_returns_none,
                                      on_add=_on_add)
        comp.features = _FakeFeatures(mesh_remove=mrf)
        design = install(md, MakeDesign(comp=comp, design_type=1))
        monkeypatch.setattr(md._MESH, "resolve", lambda raw: (mesh, None))
        return mesh, comp, design, mrf

    def test_deletes_via_mesh_remove_features_at_normal_scope(self, monkeypatch):
        mesh, comp, design, mrf = self._setup(monkeypatch)
        out = _payload(md.handler(mesh="H"))
        assert out["deleted"] == "Scan1"
        assert out["deleted_via"] == "meshRemoveFeatures"
        assert out["feature"] == "MeshRemove1"
        assert out["remaining_meshes"] == 0
        # the mesh was handed to createInput as a PLAIN LIST (live-verified call shape) - an
        # ObjectCollection regression makes the fake raise TypeError, which mesh_delete's try/except
        # turns into an error, and this success assertion goes red.
        assert mrf.last_coll == [mesh]
        assert isinstance(mrf.last_coll, list)

    def test_none_feature_return_is_still_success_when_mesh_is_gone(self, monkeypatch):
        # add() returning None is the documented non-parametric-feature contract - success is judged
        # by the mesh actually being gone, not the return.
        mesh, comp, design, mrf = self._setup(monkeypatch, none_feature=True)
        out = _payload(md.handler(mesh="H"))
        assert out["deleted"] == "Scan1"
        assert out["feature"] is None
        assert out["remaining_meshes"] == 0

    def test_add_failure_surfaces_not_swallowed(self, monkeypatch):
        mesh, comp, design, mrf = self._setup(monkeypatch, raise_on_add=True)
        assert "failed" in error_message(md.handler(mesh="H")).lower()
        assert mesh in comp.meshBodies._items   # nothing was removed

    def test_create_input_none_errors(self, monkeypatch):
        mesh, comp, design, mrf = self._setup(monkeypatch, create_input_returns_none=True)
        assert "returned nothing" in error_message(md.handler(mesh="H"))

    def test_missing_remove_features_collection_errors(self, monkeypatch):
        comp = MakeComp("Comp", mesh_bodies=[])
        mesh = _MeshBody()
        mesh.parentComponent = comp
        comp.meshBodies._items.append(mesh)
        comp.features = _FakeFeatures(mesh_remove=None)
        install(md, MakeDesign(comp=comp, design_type=1))
        monkeypatch.setattr(md._MESH, "resolve", lambda raw: (mesh, None))
        res = md.handler(mesh="H")
        assert res["isError"] is True
        assert "meshRemoveFeatures collection" in res["message"]

    def test_survivor_after_reported_success_is_an_error(self, monkeypatch):
        # add() succeeds but the mesh was NOT actually removed from the collection - the survivor
        # re-read must catch the lie, mirroring the direct-mode case.
        mesh, comp, design, mrf = self._setup(monkeypatch, remove_on_delete=False)
        assert "still resolves" in error_message(md.handler(mesh="H"))

    def test_add_does_not_touch_base_features_collection(self, monkeypatch):
        # mesh_delete calls createInput/add directly, opening no scope. run_in_base_feature reads
        # comp.features.baseFeatures, so a reintroduced wrapper lands on this rig as a read.
        mesh, comp, design, mrf = self._setup(monkeypatch)
        out = _payload(md.handler(mesh="H"))
        assert out["deleted"] == "Scan1"
        assert comp.features._base_feature_reads == 0


# ── design-wide mesh NAME resolution (exercises the REAL _inputs.MeshBodyRef, not monkeypatched) ───
#
# dir(occurrence) LISTS meshBodies, but READING occ.meshBodies raises AttributeError while
# occ.bRepBodies reads fine - so with the mesh's owner component NOT active, an occurrence-based
# walk finds nothing (root.allOccurrences is deliberately [] here - no occurrence proxies are
# modeled at all, so this can only pass via the design-wide component sweep, never the occurrence
# path).

class TestDesignWideMeshResolution:
    def test_child_component_mesh_resolves_with_root_active(self, monkeypatch):
        root = MakeComp("Root", mesh_bodies=[])
        child = MakeComp("MeshChild", mesh_bodies=[])
        child_mesh = _MeshBody(name="ChildMesh", token="MTOK::ChildMesh")
        child_mesh.parentComponent = child
        child.meshBodies._items.append(child_mesh)

        design = MakeDesign(comp=root, design_type=0, all_components=[root, child])
        assert design.activeComponent is root   # ROOT is active, not the mesh's owner

        import adsk.fusion
        monkeypatch.setattr(adsk.fusion, "MeshBody", MeshBody)
        install(md, design)          # both design seams - the dual-seam trap

        out = _payload(md.handler(mesh="ChildMesh"))
        assert out["deleted"] == "ChildMesh"
        assert out["component"] == "MeshChild"
        assert out["remaining_meshes"] == 0
        assert child_mesh not in child.meshBodies._items
