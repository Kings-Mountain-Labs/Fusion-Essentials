"""Unit tests for mesh_generate_face_groups.py - the planar face-group segmentation."""

import types

import adsk.fusion
import pytest

from conftest import (BRepBody, FakeBaseFeature, FakeBaseFeatures, FakeFeatures, MakeComp,
                      MakeDesign, MeshBody, _NamedCollection, install, load_tool,
                      make_face_groups, payload)

me = load_tool("mesh_generate_face_groups")
mesh_to_brep = load_tool("mesh_to_brep")


_FG = adsk.fusion.MeshGenerateFaceGroupsMethodTypes


class _FeatureResult:
    """A mesh feature: its name and the bodies it produced."""
    def __init__(self, name, bodies):
        self.name = name
        self.bodies = _NamedCollection(bodies)


class _FaceGroupsFeatures:
    """`groups_after` is the face-group set the mesh publishes once add() lands (a count, or the
    tempIds outright) - left None, the mesh keeps the groups it had, ids included."""
    def __init__(self, feat_name="FaceGroups1", raise_on_add=False, none_feature=False,
                 groups_after=None):
        self._feat_name = feat_name
        self.raise_on_add = raise_on_add
        self.none_feature = none_feature
        self.groups_after = groups_after
        self.last_input = None
        self.add_called = False

    def createInput(self, mesh):
        self.last_input = type("Inp", (), {"mesh": mesh})()
        return self.last_input

    def add(self, inp):
        self.add_called = True
        if self.raise_on_add:
            raise RuntimeError("face groups failed")
        if self.groups_after is not None:
            self.last_input.mesh.faceGroups = make_face_groups(self.groups_after)
        if self.none_feature:
            return None
        return _FeatureResult(self._feat_name, [])


class _Features(FakeFeatures):
    """comp.features plus the two mesh-feature collections this tool reaches through."""
    def __init__(self, face_groups=None, plane_cut=None, base_features=None):
        super().__init__(base_features=base_features)
        self.meshGenerateFaceGroupsFeatures = face_groups
        self.meshPlaneCutFeatures = plane_cut


@pytest.fixture(autouse=True)
def _fusion_types(monkeypatch):
    """The adsk.fusion type identities the tool and the input kinds branch on."""
    monkeypatch.setattr(adsk.fusion, "MeshBody", MeshBody, raising=False)
    monkeypatch.setattr(adsk.fusion, "BRepBody", BRepBody, raising=False)
    monkeypatch.setattr(adsk.fusion, "BaseFeature", FakeBaseFeature, raising=False)


def _component(features):
    """A component carrying the mesh-feature collections this tool reaches through."""
    comp = MakeComp("Comp", mesh_bodies=[])
    comp.features = features
    return comp


class TestFaceGroups:

    def _setup(self, parametric=False, base_feature=None, raise_on_add=False, none_feature=False,
               face_groups=(0,), groups_after=None):
        # (0,) is the one group, tempId 0, a mesh publishes before anything segments it (measured);
        # groups_after is what the landed generation leaves behind - a count or the tempIds.
        fg = _FaceGroupsFeatures(raise_on_add=raise_on_add, none_feature=none_feature,
                                 groups_after=groups_after)
        bf = base_feature
        feats = _Features(face_groups=fg, base_features=FakeBaseFeatures(made=bf))
        src = MeshBody("Scan", face_groups=face_groups)
        comp = _component(feats)
        src.parentComponent = comp
        des = MakeDesign(comp=comp, tokens={"H": src},
                         design_type=1 if parametric else 0,
                         active_edit_object=bf if parametric else None)
        install(me, des)
        return src, fg, bf

    def test_direct_generates_without_scope(self):
        # DIRECT design -> run_in_base_feature runs inner_op(None) directly, NO base-feature touched.
        src, fg, _ = self._setup(parametric=False)
        out = payload(me.handler(mesh="H", method="accurate"))
        assert out["generated"] is True
        assert out["method"] == "accurate"
        assert out["feature"] == "FaceGroups1"
        assert fg.add_called is True
        # the accurate enum was set on the input
        assert getattr(fg.last_input, "meshGenerateFaceGroupsMethodType", None) == _FG.AccurateGenerateFaceGroupsType
        # the convert-now-works note is present
        assert "prismatic" in out["note"].lower()

    def test_fast_method_resolves_enum(self):
        src, fg, _ = self._setup(parametric=False)
        out = payload(me.handler(mesh="H", method="fast"))
        assert out["method"] == "fast"
        assert getattr(fg.last_input, "meshGenerateFaceGroupsMethodType", None) == _FG.FastGenerateFaceGroupsType

    def test_parametric_routes_through_base_feature_scope(self):
        # PARAMETRIC -> run_in_base_feature opens the scope: the captured BaseFeature is started AND
        # finished (atomic), and the add lands inside it.
        bf = FakeBaseFeature()
        src, fg, _ = self._setup(parametric=True, base_feature=bf)
        out = payload(me.handler(mesh="H"))
        assert out["generated"] is True
        assert bf._starts == 1 and bf._finishes == 1        # scope opened AND closed (leak-proof)
        assert fg.add_called is True

    def test_direct_does_not_open_a_scope(self):
        # Even though a baseFeatures collection exists, DIRECT mode must NOT open/touch it.
        bf = FakeBaseFeature()
        src, fg, _ = self._setup(parametric=False, base_feature=bf)
        out = payload(me.handler(mesh="H"))
        assert out["generated"] is True
        assert bf._starts == 0 and bf._finishes == 0         # no scope used in direct

    def test_add_failure_surfaces_not_swallowed(self):
        self._setup(parametric=False, raise_on_add=True)
        res = me.handler(mesh="H")
        assert res["isError"] is True and "face groups failed" in res["message"]

    def test_none_feature_with_face_groups_is_success(self):
        # add() returns None in a DIRECT design but the count MOVED off the one group the mesh
        # already published: success is judged by the side effect, not the feature return.
        self._setup(parametric=False, none_feature=True, groups_after=7)
        out = payload(me.handler(mesh="H"))
        assert out["generated"] is True
        assert out["design_mode"] == "direct"
        assert out["base_feature"] is None          # direct opens no scope
        assert out["feature"] is None
        assert out["face_group_count_before"] == 1 and out["face_group_count"] == 7
        assert out["changed"] is True
        assert me._common.DIRECT_FEATURE_NOTE in out["note"]

    def test_none_feature_in_parametric_scope_is_success(self):
        # PARAMETRIC: the scoped add returns None (the base-feature scope suppresses the feature) and
        # the side effect is present -> SUCCESS, with the scope opened/closed around it. The design's
        # mode is reported as PARAMETRIC - the null feature says nothing about the mode.
        bf = FakeBaseFeature()
        self._setup(parametric=True, base_feature=bf, none_feature=True, groups_after=3)
        out = payload(me.handler(mesh="H"))
        assert out["generated"] is True and out["feature"] is None
        assert out["design_mode"] == "parametric"
        assert out["base_feature"] == "BaseFeature1"    # where the generation actually landed
        assert "BaseFeature1" in out["note"]
        assert "direct" not in out["note"].lower()
        assert out["face_group_count"] == 3
        assert bf._starts == 1 and bf._finishes == 1

    def test_mode_is_read_before_the_scope_opens(self):
        # designType reads DIRECT while a base-feature edit scope is open, so a mode read taken after
        # the generation would report 'direct' for a parametric design.
        bf = FakeBaseFeature()
        src, fg, _ = self._setup(parametric=True, base_feature=bf, none_feature=True, groups_after=3)
        des = me.app.activeProduct
        real_start = bf.startEdit

        def start_and_flip():
            des.designType = 0        # what the platform reports while the scope is open
            return real_start()

        bf.startEdit = start_and_flip
        out = payload(me.handler(mesh="H"))
        assert out["design_mode"] == "parametric"

    def test_brep_handle_rejected_with_redirect(self):
        brep = BRepBody("SolidBody", is_solid=True)
        comp = _component(_Features())
        install(me, MakeDesign(comp=comp, tokens={"H": brep}, design_type=0))
        res = me.handler(mesh="H")
        assert res["isError"] is True
        assert "must be a MESH body" in res["message"]
        assert "SOLID body" in res["message"]

    def test_missing_features_collection_errors(self):
        # comp.features has no meshGenerateFaceGroupsFeatures -> honest error
        comp = _component(_Features(face_groups=None))
        src = MeshBody("Scan", parent=comp)
        install(me, MakeDesign(comp=comp, tokens={"H": src}, design_type=0))
        res = me.handler(mesh="H")
        assert res["isError"] is True
        assert "meshGenerateFaceGroupsFeatures collection" in res["message"]

    def test_create_input_none_errors(self):
        src, fg, _ = self._setup(parametric=False)
        fg.createInput = lambda mesh: None
        res = me.handler(mesh="H")
        assert res["isError"] is True and "returned nothing" in res["message"]

    def test_create_input_raise_surfaces(self):
        src, fg, _ = self._setup(parametric=False)
        def _boom(mesh):
            raise RuntimeError("createInput blew up")
        fg.createInput = _boom
        res = me.handler(mesh="H")
        assert res["isError"] is True and "Could not create the face-groups input" in res["message"]

    def test_a_landed_generation_that_leaves_the_count_at_one_is_not_refused(self):
        # MEASURED: a coplanar 2-triangle mesh segments INTO one group, so the count reads 1 before
        # and after a generation that landed - the ids are what moved. Refusing this is the bug.
        self._setup(parametric=False, none_feature=True, groups_after=[1])
        out = payload(me.handler(mesh="H"))
        assert out["generated"] is True
        assert out["face_group_count_before"] == 1 and out["face_group_count"] == 1
        assert out["changed"] is False and out["face_group_ids_changed"] is True
        assert "the group ids moved" in out["note"]

    def test_a_repeat_pass_that_reproduces_the_groups_is_not_refused(self):
        # MEASURED: a fast re-generation of an already-segmented tetrahedron leaves the count AND
        # the tempIds identical - indistinguishable from a no-op, so neither read may refuse.
        self._setup(parametric=False, none_feature=True, face_groups=4, groups_after=4)
        out = payload(me.handler(mesh="H"))
        assert out["generated"] is True
        assert out["changed"] is False and out["face_group_ids_changed"] is False
        assert out["face_group_count"] == 4

    def test_a_count_that_moved_is_reported_as_changed(self):
        self._setup(parametric=False, none_feature=True, groups_after=2)
        out = payload(me.handler(mesh="H"))
        assert out["generated"] is True and out["changed"] is True
        assert out["face_group_count_before"] == 1 and out["face_group_count"] == 2
        assert out["face_group_ids_changed"] is True

    def test_a_null_feature_with_no_readable_readback_is_a_failure(self):
        # add() returned no feature AND neither read answers - nothing observed the generation, so
        # 'generated: true' would rest on nothing.
        self._setup(parametric=False, none_feature=True, face_groups=None, groups_after=None)
        res = me.handler(mesh="H")
        assert res["isError"] is True
        assert "nothing observed what this generation did" in res["message"]


class TestMeshToBrepHint:

    def _setup_convert(self, raise_on_add=True):
        """A mesh_to_brep design whose convert add() RAISES, so the prismatic error path fires."""
        class _ConvFeatures:
            def __init__(self):
                self.last_input = None

            def createInput(self, meshes):
                self.last_input = type("Inp", (), {})()
                return self.last_input

            def add(self, inp):
                if raise_on_add:
                    raise RuntimeError("MESH_FAILED_BREP")
                return None

        comp = _component(types.SimpleNamespace(meshConvertFeatures=_ConvFeatures()))
        src = MeshBody("Scan", is_closed=True, parent=comp)
        return install(mesh_to_brep, MakeDesign(comp=comp, tokens={"H": src}, design_type=0))

    def test_prismatic_convert_failure_mentions_face_groups_tool(self):
        self._setup_convert(raise_on_add=True)
        res = mesh_to_brep.handler(mesh="H", method="prismatic")
        assert res["isError"] is True
        assert "mesh_generate_face_groups" in res["message"]

    def test_faceted_convert_failure_omits_the_hint(self):
        # the hint is prismatic-specific (face groups are a prismatic requirement)
        self._setup_convert(raise_on_add=True)
        res = mesh_to_brep.handler(mesh="H", method="faceted")
        assert res["isError"] is True
        assert "mesh_generate_face_groups" not in res["message"]
