"""Unit tests for save_as_mesh.py - the tessellation, the vertex weld and the landed body."""

import types

import adsk.fusion
import pytest

import live_api_facts
from conftest import (BRepBody, FakeBaseFeature, FakeBaseFeatures, FakeFeatures, FakeMeshManager,
                      MakeComp, MakeDesign, MeshBody, _FakeTriangleMesh, _MeshBodies, install,
                      load_tool, make_mesh_manager, payload)

mx = load_tool("save_as_mesh")


class _AddingMeshBodies(_MeshBodies):
    """comp.meshBodies where addByTriangleMeshData records its arguments and LANDS the new body in
    the collection, so the count read back afterwards grows."""

    def __init__(self, result=None, raise_on_add=False, existing=()):
        super().__init__(existing)
        self._result = result if result is not None else MeshBody("SavedMesh")
        self._raise_on_add = raise_on_add
        self.add_args = None

    def addByTriangleMeshData(self, coords, coord_idx, normals, normal_idx):
        if self._raise_on_add:
            raise RuntimeError("add failed")
        self.add_args = (coords, coord_idx, normals, normal_idx)
        self._items.append(self._result)
        return self._result


@pytest.fixture(autouse=True)
def _types(monkeypatch):
    """The adsk types the input kinds isinstance-check - TriangleMeshQualityOptions arrives seeded."""
    monkeypatch.setattr(adsk.fusion, "BRepBody", BRepBody, raising=False)
    monkeypatch.setattr(adsk.fusion, "MeshBody", MeshBody, raising=False)
    monkeypatch.setattr(adsk.fusion, "BaseFeature", FakeBaseFeature, raising=False)


def _comp(name="Comp", mesh_bodies=None, features=None):
    """A component carrying the two collections save_as_mesh writes through."""
    comp = MakeComp(name)
    comp.meshBodies = mesh_bodies if mesh_bodies is not None else _AddingMeshBodies()
    comp.features = features if features is not None else FakeFeatures()
    return comp


def _wire(comp, design_type=0, handles=None):
    """save_as_mesh wired onto a design rooted at `comp` - both design seams patched."""
    return install(mx, MakeDesign(comp=comp, tokens=handles, design_type=design_type))


def _mesh_source(name="SolidA", tri=12, nodes=8, parent_comp=None, calculate_raises=None,
                 quality_ok=True):
    """A BRep body wired with a meshManager that yields a TriangleMesh of the given counts.

    A live TriangleMesh exposes no normal index list, so the one the handler passes on is empty."""
    tm = _FakeTriangleMesh(tri, nodes, coords=[0.0] * (nodes * 3),
                           node_indices=list(range(tri * 3)), normals=[0.0] * (nodes * 3))
    return BRepBody(name, parent_component=parent_comp,
                    mesh_manager=make_mesh_manager(tm, quality_ok=quality_ok,
                                                   calculate_raises=calculate_raises))


def _calculator(body):
    """The ONE calculator the body's mesh manager hands out - what the tessellation ran on."""
    return body.meshManager.createMeshCalculator()


def _quality_family_without(member):
    """The measured TriangleMeshQualityOptions family with `member` absent - the build whose enum
    does not carry the requested quality. Reading the missing name raises, as the seeded family
    does, so the handler's safe(getattr(...)) degrades exactly the way it does live."""
    measured = live_api_facts.ENUMS["fusion.TriangleMeshQualityOptions"]
    return types.SimpleNamespace(**{k: v for k, v in measured.items() if k != member})


class TestSaveAsMesh:

    def _tessellate_without_the_quality_member(self, monkeypatch, quality="high"):
        """save_as_mesh on a build carrying no TriangleMeshQualityOptions member for the request -
        setQuality is never called and the calculator runs at its own default level of detail."""
        # The FAMILY is swapped for one that is a member short, never a member deleted off the seeded
        # family: that object outlives one test, so the deletion would leak into the next.
        monkeypatch.setattr(adsk.fusion, "TriangleMeshQualityOptions",
                            _quality_family_without("HighQualityTriangleMesh"), raising=False)
        comp = _comp()
        src = _mesh_source("SolidA", parent_comp=comp)
        _wire(comp, design_type=0, handles={"H": src})
        return payload(mx.handler(body="H", quality=quality)), src

    def test_direct_tessellates_adds_mesh_no_scope(self):
        mb_coll = _AddingMeshBodies(result=MeshBody("SavedMesh"))
        bf = FakeBaseFeature()
        comp = _comp("Comp", mesh_bodies=mb_coll,
                     features=FakeFeatures(base_features=FakeBaseFeatures(made=bf)))
        src = _mesh_source("SolidA", tri=12, nodes=8, parent_comp=comp)
        _wire(comp, design_type=0, handles={"H": src})               # DIRECT
        out = payload(mx.handler(body="H", quality="normal"))
        assert out["saved_as_mesh"] is True
        assert out["name"] == "SavedMesh"
        assert out["triangle_count"] == 12 and out["node_count"] == 8
        assert mb_coll.add_args is not None            # the mesh was actually added
        # A TriangleMesh exposes no normal index list, so the fourth argument is always empty.
        assert mb_coll.add_args[3] == []
        # DIRECT: run_in_base_feature ran the op with NO scope (the base feature was never started)
        assert bf._starts == 0 and bf._finishes == 0

    def test_parametric_routes_through_base_feature_scope(self):
        mb_coll = _AddingMeshBodies(result=MeshBody("SavedMesh"))
        bf = FakeBaseFeature()
        base_features = FakeBaseFeatures(made=bf)
        comp = _comp("Comp", mesh_bodies=mb_coll, features=FakeFeatures(base_features=base_features))
        src = _mesh_source("SolidA", parent_comp=comp)
        _wire(comp, design_type=1, handles={"H": src})               # PARAMETRIC
        out = payload(mx.handler(body="H"))
        assert out["saved_as_mesh"] is True
        # PARAMETRIC: the write was wrapped in an OPEN/CLOSED base-feature scope - and a scope left
        # open would hide its own base feature from the collection it was added to.
        assert bf._starts == 1 and bf._finishes == 1
        assert base_features.count == 1 and base_features.item(0) is bf
        assert mb_coll.add_args is not None

    def test_phantom_body_that_never_lands_bites(self):
        # a returned body object is not proof it joined the component - the count is

        class PhantomMeshBodies(_AddingMeshBodies):
            count = 3                                    # static: the add never actually lands

        mb_coll = PhantomMeshBodies(result=MeshBody("Phantom"))
        comp = _comp("Comp", mesh_bodies=mb_coll)
        src = _mesh_source("SolidA", parent_comp=comp)
        _wire(comp, design_type=0, handles={"H": src})
        res = mx.handler(body="H")
        assert res["isError"] is True
        assert "did not increase" in res["message"]

    def test_landed_body_grows_the_count_and_passes(self):
        mb_coll = _AddingMeshBodies(result=MeshBody("SavedMesh"),
                                    existing=[MeshBody("Old1"), MeshBody("Old2"), MeshBody("Old3")])
        comp = _comp("Comp", mesh_bodies=mb_coll)
        src = _mesh_source("SolidA", parent_comp=comp)
        _wire(comp, design_type=0, handles={"H": src})
        out = payload(mx.handler(body="H"))
        assert out["saved_as_mesh"] is True
        assert mb_coll.count == 4                        # 3 before, 4 after

    def test_quality_passed_to_calculator(self):
        comp = _comp()
        src = _mesh_source("SolidA", parent_comp=comp)
        _wire(comp, design_type=0, handles={"H": src})
        out = payload(mx.handler(body="H", quality="very_high"))
        assert out["quality"] == "very_high"
        assert out["quality_requested"] == "very_high"
        # the calculator received the VeryHigh quality enum value...
        assert _calculator(src)._quality == 15
        # ...and setQuality moved surfaceTolerance off the zero a fresh calculator reads
        assert _calculator(src).surfaceTolerance != 0.0

    def test_a_quality_the_platform_declines_is_an_error_not_a_default_tessellation(self):
        # setQuality answers a bool. A false leaves the calculator at its own level of detail, so
        # reporting the requested quality would name a level of detail the mesh does not have.
        comp = _comp()
        src = _mesh_source("SolidA", parent_comp=comp, quality_ok=False)
        _wire(comp, design_type=0, handles={"H": src})
        res = mx.handler(body="H", quality="high")
        assert res["isError"] is True and "setQuality returned false" in res["message"]

    def test_a_mesh_manager_that_hands_out_no_calculator_is_refused(self):
        comp = _comp()
        src = BRepBody("SolidA", parent_component=comp,
                       mesh_manager=FakeMeshManager(calculator=None))
        _wire(comp, design_type=0, handles={"H": src})
        res = mx.handler(body="H")
        assert res["isError"] is True and "createMeshCalculator" in res["message"]

    def test_quality_that_never_reached_setquality_is_published_null(self, monkeypatch):
        # the tessellation ran at the calculator's DEFAULT LOD, so publishing the requested key as
        # 'quality' would report a level of detail the mesh does not have.
        out, src = self._tessellate_without_the_quality_member(monkeypatch)
        assert _calculator(src)._quality is None           # setQuality was never called
        assert out["quality"] is None
        assert out["quality_requested"] == "high"

    def test_an_unlanded_quality_says_so_on_the_wire(self, monkeypatch):
        out, _src = self._tessellate_without_the_quality_member(monkeypatch)
        assert "did NOT land" in out["note"] and "default level of detail" in out["note"]

    def test_a_landed_quality_adds_no_did_not_land_note(self):
        comp = _comp()
        src = _mesh_source("SolidA", parent_comp=comp)
        _wire(comp, design_type=0, handles={"H": src})
        out = payload(mx.handler(body="H", quality="low"))
        assert out["quality"] == "low"
        assert "did NOT land" not in out["note"]

    def test_optional_name_renames_the_mesh(self):
        comp = _comp()
        src = _mesh_source("SolidA", parent_comp=comp)
        _wire(comp, design_type=0, handles={"H": src})
        out = payload(mx.handler(body="H", name="MyMesh"))
        assert out["name"] == "MyMesh"

    def test_bad_quality_rejected(self):
        comp = _comp()
        src = _mesh_source("SolidA", parent_comp=comp)
        _wire(comp, design_type=0, handles={"H": src})
        res = mx.handler(body="H", quality="ultra")
        assert res["isError"] is True and "quality" in res["message"]

    def test_mesh_source_rejected(self):
        # passing an existing MESH body to save_as_mesh (it wants a BRep) -> honest refusal
        comp = _comp()
        m = MeshBody("AlreadyMesh")
        _wire(comp, design_type=0, handles={"H": m})
        res = mx.handler(body="H")
        assert res["isError"] is True and "already a MESH" in res["message"]

    def test_calculate_failure_surfaces(self):
        comp = _comp()
        src = _mesh_source("SolidA", parent_comp=comp, calculate_raises="calculate blew up")
        _wire(comp, design_type=0, handles={"H": src})
        res = mx.handler(body="H")
        assert res["isError"] is True and "tessellation" in res["message"].lower()

    def test_add_failure_surfaces_not_swallowed(self):
        mb_coll = _AddingMeshBodies(raise_on_add=True)
        comp = _comp("Comp", mesh_bodies=mb_coll)
        src = _mesh_source("SolidA", parent_comp=comp)
        _wire(comp, design_type=0, handles={"H": src})
        # in DIRECT mode the add runs directly inside run_in_base_feature; the raise must propagate
        try:
            res = mx.handler(body="H")
        except RuntimeError as e:
            assert "add failed" in str(e)
        else:
            assert res["isError"] is True


class TestWeld:

    """_weld merges coincident vertices so a watertight solid tessellates to a watertight mesh. The
    calculator emits one vertex per triangle corner; without welding the mesh is topologically open
    (isClosed=false) and mesh_to_brep refuses it."""

    def test_box_corners_merge_24_to_8(self):
        # 8 distinct corners, each repeated 3x (one per adjacent face) = 24 emitted vertices.
        corners = [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0),
                   (0, 0, 1), (1, 0, 1), (1, 1, 1), (0, 1, 1)]
        coords, idx = [], []
        for i, c in enumerate(corners):
            for _ in range(3):                       # emit each corner 3 times (unwelded)
                idx.append(len(coords) // 3)
                coords.extend(c)
        wc, wi = mx._weld(coords, idx)
        assert len(wc) // 3 == 8                      # 24 -> 8 unique vertices
        # every welded index points at the right merged coordinate
        for emitted, new in zip(idx, wi):
            ex = coords[3 * emitted: 3 * emitted + 3]
            got = wc[3 * new: 3 * new + 3]
            assert ex == got

    def test_distinct_vertices_are_preserved(self):
        coords = [0, 0, 0, 1, 0, 0, 0, 1, 0]         # 3 distinct vertices, no duplicates
        idx = [0, 1, 2]
        wc, wi = mx._weld(coords, idx)
        assert len(wc) // 3 == 3 and wi == [0, 1, 2]

    def test_vertices_agreeing_to_the_quantization_merge(self):
        # the merge keys on round(x, 6), not on equality: 1e-9 apart is ONE vertex.
        wc, wi = mx._weld([0.0, 0.0, 0.0, 1e-9, 0.0, 0.0], [0, 1])
        assert len(wc) // 3 == 1 and wi == [0, 0]

    def test_vertices_beyond_the_quantization_stay_distinct(self):
        # 1e-3 apart is two vertices - the quantization is a floor, not a modelling tolerance.
        wc, wi = mx._weld([0.0, 0.0, 0.0, 1e-3, 0.0, 0.0], [0, 1])
        assert len(wc) // 3 == 2 and wi == [0, 1]

    def test_malformed_input_returned_unchanged(self):
        # ragged coordinate list (not a multiple of 3) is passed through untouched, never raises
        bad = [0.0, 1.0]
        assert mx._weld(bad, [0]) == (bad, [0])
        assert mx._weld([], []) == ([], [])
