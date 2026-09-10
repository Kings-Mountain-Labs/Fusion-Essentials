"""Unit tests for ``mesh_repair.py`` - the MeshRepair feature over a MeshBody.

No live Fusion. The fakes model the slice of the API the tool touches: MeshRepairFeatures
(createInput(mesh) -> input -> add(input) -> feature or None), a MeshRepairFeatureInput whose
density/offset accept ONLY a ValueInput (their declared type), a MeshBody whose triangle/vertex
counts and isClosed flag the repair mutates in place, and the base-feature plumbing
run_in_base_feature drives.

Pinned (the DoD):
  - each repair_type / rebuild_method key resolves the right enum member onto the input.
  - rebuild_method / density / offset are refused on a non-rebuild repair_type, and offset is
    refused unless rebuild_method='accurate' - before any mutation.
  - density is bounded 8..256; density and offset cross the wire as ValueInput, and offset is
    scaled from the call's units to internal cm.
  - PARAMETRIC opens a base-feature scope and sets targetBaseFeature to it; DIRECT opens none.
  - a repair that moves nothing (triangles, vertices, is_closed, mesh body count, volume) is an
    ERROR - except close_holes on an already-watertight mesh, which has nothing to close.
  - close_holes that changes the mesh but leaves it open reports PARTIAL, not plain success.
  - a mesh that cannot be read back at all is reported as unverified, not as success.
"""

import types

import adsk.fusion
import pytest

from conftest import (BRepBody, FakeBaseFeature, FakeBaseFeatures, FakeFeatures,
                      FakeValueInput as _ValueInput, MakeComp, MakeDesign, MeshBody, install,
                      load_tool, payload, error_message)

mr = load_tool("mesh_repair")

_TYPES = adsk.fusion.MeshRepairTypes
_REBUILDS = adsk.fusion.MeshRepairRebuildTypes


# ── fakes ────────────────────────────────────────────────────────────────────────────────────────

def _mesh(name="Scan1", tri=1000, nodes=502, is_closed=False, volume=0.0, **kw):
    """conftest's shared MeshBody with this file's scan defaults: an OPEN 1000-triangle scan, whose
    volume therefore reads 0.0 at both ends of a repair that leaves it open and turns positive the
    moment one closes it."""
    return MeshBody(name=name, tri=tri, nodes=nodes, is_closed=is_closed, volume=volume, **kw)


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


class _RepairInput:
    """MeshRepairFeatureInput. density and offset are declared core.ValueInput, so a raw number is
    refused here - that is the regression the live API raises on."""
    # True models the live SWIG trap: an assignment is ACCEPTED and lands nowhere, so the input
    # keeps its API default and only a read-back can tell.
    swallow_enum_sets = False

    def __init__(self, mesh):
        self.mesh = mesh
        self._repair_type = None
        self.meshRepairRebuildType = None
        self.targetBaseFeature = None
        self._density = None
        self._offset = None

    @property
    def meshRepairType(self):
        return self._repair_type

    @meshRepairType.setter
    def meshRepairType(self, value):
        if not self.swallow_enum_sets:
            self._repair_type = value

    @property
    def density(self):
        return self._density

    @density.setter
    def density(self, value):
        if not isinstance(value, _ValueInput):
            raise TypeError("density expects a ValueInput, got " + type(value).__name__)
        self._density = value

    @property
    def offset(self):
        return self._offset

    @offset.setter
    def offset(self, value):
        if not isinstance(value, _ValueInput):
            raise TypeError("offset expects a ValueInput, got " + type(value).__name__)
        self._offset = value


class _RepairFeatures:
    """comp.features.meshRepairFeatures. add() returns a feature, or None for the documented
    non-parametric case; `on_add` is the in-place change the repair makes to the mesh."""
    def __init__(self, on_add=None, raise_on_add=False, none_feature=False,
                 create_input_returns_none=False, feat_name="MeshRepair1",
                 feature_density=None, feature_offset=None):
        self._on_add = on_add
        self.raise_on_add = raise_on_add
        self.none_feature = none_feature
        self._create_input_returns_none = create_input_returns_none
        self._feat_name = feat_name
        # What the CREATED FEATURE reports, which is not always what was asked for: a live
        # MeshRepairFeature exposes density/offset as ModelParameters, so the request is read back
        # rather than echoed. None means "report whatever was set".
        self._feature_density = feature_density
        self._feature_offset = feature_offset
        self.last_input = None
        self.create_arg = None
        self.add_called = False

    def createInput(self, mesh):
        self.create_arg = mesh
        if self._create_input_returns_none:
            return None
        self.last_input = _RepairInput(mesh)
        return self.last_input

    def add(self, inp):
        self.add_called = True
        if self.raise_on_add:
            raise RuntimeError("repair failed")
        if self._on_add is not None:
            self._on_add()
        if self.none_feature:
            return None
        feat = types.SimpleNamespace(name=self._feat_name)
        inp = self.last_input
        dens = (self._feature_density if self._feature_density is not None
                else getattr(getattr(inp, "density", None), "realValue", None))
        off = (self._feature_offset if self._feature_offset is not None
               else getattr(getattr(inp, "offset", None), "realValue", None))
        if dens is not None:
            feat.density = types.SimpleNamespace(value=float(dens))
        if off is not None:
            feat.offset = types.SimpleNamespace(value=float(off))
        return feat


class _Features(FakeFeatures):
    """comp.features plus the mesh-repair collection this tool reaches through."""
    def __init__(self, repair=None, base_features=None):
        super().__init__(base_features=base_features)
        self.meshRepairFeatures = repair


# ── rig ──────────────────────────────────────────────────────────────────────────────────────────

def _rig(monkeypatch, mesh=None, on_add=None, design_type=1, **feat_kw):
    """Wire one mesh + a meshRepairFeatures collection into the tool. Returns (mesh, comp, feats,
    base_feature)."""
    mesh = mesh if mesh is not None else _mesh()
    comp = _MeshComp("Comp", mesh_bodies=[mesh])
    mesh.parentComponent = comp
    feats = _RepairFeatures(on_add=on_add, **feat_kw)
    base_feature = FakeBaseFeature()
    comp.features = _Features(repair=feats, base_features=FakeBaseFeatures(made=base_feature))
    install(mr, MakeDesign(comp=comp, design_type=design_type))
    monkeypatch.setattr(mr._MESH, "resolve", lambda raw: (mesh, None))
    monkeypatch.setattr(mr.adsk.core.ValueInput, "createByReal", _ValueInput)
    return mesh, comp, feats, base_feature


def _grow(mesh, tri=0, nodes=0, close=None):
    """The in-place change a repair makes to a mesh - the fake feature's add() side effect."""
    def _apply():
        mesh.displayMesh.triangleCount += tri
        mesh.displayMesh.nodeCount += nodes
        if close is not None:
            mesh.isClosed = close
    return _apply


@pytest.fixture
def rig(monkeypatch):
    """A repair that adds 120 triangles / 60 vertices and closes the mesh - the happy path. The
    body reads 0.0 while it is open and 8 cm3 once the holes are closed, which is what a real
    close-holes does to the enclosed volume."""
    mesh = _mesh(tri=1000, nodes=502, is_closed=False, volume=8.0)
    mesh_, comp, feats, bf = _rig(monkeypatch, mesh=mesh,
                                  on_add=_grow(mesh, tri=120, nodes=60, close=True))
    return types.SimpleNamespace(mesh=mesh, comp=comp, feats=feats, base_feature=bf,
                                 monkeypatch=monkeypatch)


# ── enum mapping ─────────────────────────────────────────────────────────────────────────────────

class TestEnumMapping:
    def test_every_repair_type_sets_its_own_enum_member(self, rig):
        for key, member in mr._REPAIR_TYPES.items():
            out = payload(mr.handler(mesh="H", repair_type=key))
            assert out["repair_type"] == key
            assert rig.feats.last_input.meshRepairType is getattr(_TYPES, member)

    def test_every_rebuild_method_sets_its_own_enum_member(self, rig):
        for key, member in mr._REBUILD_METHODS.items():
            offset = 1.0 if key == "accurate" else None
            payload(mr.handler(mesh="H", repair_type="rebuild", rebuild_method=key, offset=offset))
            assert rig.feats.last_input.meshRepairRebuildType is getattr(_REBUILDS, member)

    def test_each_key_maps_to_its_own_named_member(self):
        # pins the PAIRING, not just the member set - a swapped mapping (close_holes -> Wrap...)
        # sets a real enum member and would otherwise pass the round-trip test above.
        assert mr._REPAIR_TYPES == {
            "close_holes": "CloseHolesMeshRepairType",
            "stitch_and_remove": "StitchAndRemoveMeshRepairType",
            "wrap": "WrapMeshRepairType",
            "rebuild": "RebuildMeshRepairType",
            "one_touch_fix": "OneTouchFixMeshRepairType"}
        assert mr._REBUILD_METHODS == {
            "fast": "FastMeshRepairRebuildType",
            "preserve_sharp_edges": "PreserveSharpEdgesMeshRepairRebuildType",
            "accurate": "AccurateMeshRepairRebuildType",
            "blocky": "BlockyMeshRepairRebuildType",
            "adaptive": "AdaptiveMeshRepairRebuildType",
            "adaptive_preserve_sharp_edges": "AdaptivePreserveSharpEdgesMeshRepairRebuildType"}

    def test_a_non_rebuild_repair_leaves_the_rebuild_knobs_untouched(self, rig):
        payload(mr.handler(mesh="H", repair_type="one_touch_fix"))
        assert rig.feats.last_input.meshRepairRebuildType is None
        assert rig.feats.last_input.density is None
        assert rig.feats.last_input.offset is None


# ── input guards (all BEFORE any mutation) ───────────────────────────────────────────────────────

class TestInputGuards:
    def test_missing_repair_type_lists_the_options(self, rig):
        msg = error_message(mr.handler(mesh="H"))
        assert "repair_type" in msg and "one_touch_fix" in msg
        assert rig.feats.add_called is False

    def test_unknown_repair_type_is_refused(self, rig):
        assert "must be one of" in error_message(mr.handler(mesh="H", repair_type="fix_it"))
        assert rig.feats.add_called is False

    def test_rebuild_method_on_a_non_rebuild_type_is_refused(self, rig):
        msg = error_message(mr.handler(mesh="H", repair_type="close_holes",
                                       rebuild_method="accurate"))
        assert "rebuild_method" in msg and "close_holes" in msg
        assert rig.feats.add_called is False

    def test_density_on_a_non_rebuild_type_is_refused(self, rig):
        msg = error_message(mr.handler(mesh="H", repair_type="wrap", density=128))
        assert "density" in msg
        assert rig.feats.add_called is False

    def test_offset_on_a_non_rebuild_type_is_refused(self, rig):
        msg = error_message(mr.handler(mesh="H", repair_type="stitch_and_remove", offset=1.0))
        assert "offset" in msg
        assert rig.feats.add_called is False

    def test_offset_without_the_accurate_rebuild_method_is_refused(self, rig):
        msg = error_message(mr.handler(mesh="H", repair_type="rebuild", offset=1.0))
        assert "offset" in msg and "accurate" in msg and "fast" in msg
        assert rig.feats.add_called is False

    def test_offset_is_accepted_with_the_accurate_rebuild_method(self, rig):
        payload(mr.handler(mesh="H", repair_type="rebuild", rebuild_method="accurate", offset=1.0))
        assert rig.feats.add_called is True

    @pytest.mark.parametrize("bad", [7, 257, 0, -5])
    def test_density_outside_8_to_256_is_refused(self, rig, bad):
        msg = error_message(mr.handler(mesh="H", repair_type="rebuild", density=bad))
        assert "8" in msg and "256" in msg and str(bad) in msg
        assert rig.feats.add_called is False

    @pytest.mark.parametrize("good", [8, 128, 256])
    def test_density_at_and_inside_the_bounds_is_accepted(self, rig, good):
        out = payload(mr.handler(mesh="H", repair_type="rebuild", density=good))
        assert out["density"] == float(good)

    def test_non_numeric_density_is_refused(self, rig):
        assert "density" in error_message(mr.handler(mesh="H", repair_type="rebuild",
                                                     density="dense"))
        assert rig.feats.add_called is False

    def test_no_active_design_is_an_error(self, rig):
        rig.monkeypatch.setattr(mr._common, "design", lambda: None)
        assert "No active design" in error_message(mr.handler(mesh="H", repair_type="rebuild"))

    def test_missing_repair_features_collection_is_an_error(self, monkeypatch):
        mesh, comp, feats, _bf = _rig(monkeypatch)
        comp.features = _Features(repair=None)
        assert "meshRepairFeatures" in error_message(mr.handler(mesh="H",
                                                                repair_type="one_touch_fix"))

    def test_create_input_returning_none_is_an_error(self, monkeypatch):
        _rig(monkeypatch, create_input_returns_none=True)
        assert "returned nothing" in error_message(mr.handler(mesh="H", repair_type="wrap"))

    def test_a_brep_body_is_redirected_to_the_mesh_tools(self, monkeypatch):
        mesh, comp, feats, _bf = _rig(monkeypatch)
        monkeypatch.setattr(adsk.fusion, "MeshBody", MeshBody)
        monkeypatch.setattr(adsk.fusion, "BRepBody", BRepBody)
        brep = BRepBody(name="Body1", entity_token="BTOK::Body1")
        monkeypatch.setattr(mr._MESH, "resolve", mr._inputs.MeshBodyRef("mesh").resolve)
        install(mr, MakeDesign(comp=comp, design_type=1, tokens={"BTOK::Body1": brep}))
        msg = error_message(mr.handler(mesh="BTOK::Body1", repair_type="one_touch_fix"))
        assert "MESH body" in msg and "SOLID" in msg
        assert feats.add_called is False

    def test_an_ambiguous_mesh_name_is_refused_with_candidates(self, monkeypatch):
        # two meshes named 'Scan' in two components: the resolver must refuse, never pick the first.
        mesh_a = _mesh(name="Scan", token="MTOK::A")
        mesh_b = _mesh(name="Scan", token="MTOK::B")
        comp_a = _MeshComp("CompA", mesh_bodies=[mesh_a])
        comp_b = _MeshComp("CompB", mesh_bodies=[mesh_b])
        mesh_a.parentComponent, mesh_b.parentComponent = comp_a, comp_b
        feats = _RepairFeatures()
        comp_a.features = _Features(repair=feats, base_features=FakeBaseFeatures())
        monkeypatch.setattr(adsk.fusion, "MeshBody", MeshBody)
        install(mr, MakeDesign(comp=comp_a, design_type=1, all_components=[comp_a, comp_b]))
        msg = error_message(mr.handler(mesh="Scan", repair_type="one_touch_fix"))
        assert "ambiguous" in msg and "CompA" in msg and "CompB" in msg
        assert feats.add_called is False


# ── the ValueInput-typed knobs ───────────────────────────────────────────────────────────────────

class TestValueInputs:
    def test_density_crosses_as_a_value_input_carrying_the_number(self, rig):
        payload(mr.handler(mesh="H", repair_type="rebuild", density=64))
        assert rig.feats.last_input.density.realValue == 64.0

    def test_offset_is_scaled_from_the_call_units_to_internal_cm(self, rig):
        payload(mr.handler(mesh="H", repair_type="rebuild", rebuild_method="accurate",
                           offset=2.5, units="mm"))
        assert rig.feats.last_input.offset.realValue == pytest.approx(0.25)

    def test_offset_in_inches_is_scaled_by_2_54(self, rig):
        payload(mr.handler(mesh="H", repair_type="rebuild", rebuild_method="accurate",
                           offset=1.0, units="in"))
        assert rig.feats.last_input.offset.realValue == pytest.approx(2.54)

    def test_a_zero_offset_is_still_sent(self, rig):
        payload(mr.handler(mesh="H", repair_type="rebuild", rebuild_method="accurate", offset=0))
        assert rig.feats.last_input.offset.realValue == 0.0

    def test_unknown_units_are_refused(self, rig):
        assert "units" in error_message(mr.handler(mesh="H", repair_type="rebuild",
                                                   rebuild_method="accurate", offset=1.0,
                                                   units="furlong")).lower()


# ── mode routing (base-feature scope) ────────────────────────────────────────────────────────────

class TestModeRouting:
    def test_parametric_opens_no_scope_and_leaves_target_base_feature_unset(self, rig):
        # live-measured: meshRepairFeatures.add() returns a real feature at plain parametric
        # scope, unlike MeshRemoveFeatures whose add is parametric-only
        payload(mr.handler(mesh="H", repair_type="one_touch_fix"))
        assert rig.base_feature._starts == 0
        assert getattr(rig.feats.last_input, "targetBaseFeature", None) is None

    def test_direct_opens_no_scope_and_leaves_target_base_feature_unset(self, monkeypatch):
        mesh = _mesh(tri=1000, nodes=502)
        _m, _c, feats, bf = _rig(monkeypatch, mesh=mesh, design_type=0,
                                 on_add=_grow(mesh, tri=50, nodes=25))
        payload(mr.handler(mesh="H", repair_type="one_touch_fix"))
        assert bf._starts == 0
        assert feats.last_input.targetBaseFeature is None

    def test_a_none_feature_return_in_a_parametric_design_still_reports_parametric(self, monkeypatch):
        # the repair verifies itself on the MESH, so a null feature is still success - but it is a
        # fact about the RETURN, never evidence the design is non-parametric (this rig is parametric).
        mesh = _mesh(tri=1000, nodes=502)
        _rig(monkeypatch, mesh=mesh, none_feature=True, on_add=_grow(mesh, tri=50, nodes=25))
        out = payload(mr.handler(mesh="H", repair_type="stitch_and_remove"))
        assert out["feature"] is None
        assert out["design_mode"] == "parametric"
        assert "no base-feature scope was opened" in out["note"]
        assert out["repaired"] is True

    def test_a_none_feature_return_in_a_direct_design_reports_direct(self, monkeypatch):
        mesh = _mesh(tri=1000, nodes=502)
        _rig(monkeypatch, mesh=mesh, design_type=0, none_feature=True,
             on_add=_grow(mesh, tri=50, nodes=25))
        out = payload(mr.handler(mesh="H", repair_type="stitch_and_remove"))
        assert out["design_mode"] == "direct"
        assert mr._common.DIRECT_FEATURE_NOTE in out["note"]

    def test_a_returned_feature_is_named_beside_the_parametric_mode(self, rig):
        out = payload(mr.handler(mesh="H", repair_type="one_touch_fix"))
        assert out["feature"] == "MeshRepair1"
        assert out["design_mode"] == "parametric"
        assert "'feature' is null" not in out["note"]


# ── verify-the-effect ────────────────────────────────────────────────────────────────────────────

class TestVerification:
    def test_a_repair_that_changes_nothing_is_an_error(self, monkeypatch):
        _rig(monkeypatch)   # add() fires no side effect at all
        msg = error_message(mr.handler(mesh="H", repair_type="one_touch_fix"))
        assert "unchanged" in msg and "1000 triangles" in msg

    def test_close_holes_that_leaves_an_open_mesh_untouched_is_an_error(self, monkeypatch):
        _rig(monkeypatch, mesh=_mesh(is_closed=False))
        assert "unchanged" in error_message(mr.handler(mesh="H", repair_type="close_holes"))

    def test_close_holes_on_an_already_watertight_mesh_is_success_with_nothing_to_close(
            self, monkeypatch):
        _rig(monkeypatch, mesh=_mesh(is_closed=True, volume=12.0))
        out = payload(mr.handler(mesh="H", repair_type="close_holes"))
        assert out["changed"] == []
        assert out["watertight"] is True
        assert "found nothing of its kind to fix" in out["note"]

    def test_a_no_op_on_a_healthy_mesh_is_reported_not_failed(self, monkeypatch):
        # live-measured: one_touch_fix on a clean closed box moves nothing and is right to. A
        # MeshBody exposes no defect count beyond is_closed, so a repair with nothing of its kind
        # to do is reported as measured rather than convicted.
        _rig(monkeypatch, mesh=_mesh(is_closed=True, volume=12.0))
        out = payload(mr.handler(mesh="H", repair_type="one_touch_fix"))
        assert out["changed"] == [] and "found nothing of its kind to fix" in out["note"]

    def test_a_hole_closer_that_left_an_open_mesh_untouched_is_an_error(self, monkeypatch):
        # the one defect state a MeshBody DOES expose: an open mesh handed to a hole-closing
        # repair must change something
        _rig(monkeypatch, mesh=_mesh(is_closed=False, volume=12.0))
        msg = error_message(mr.handler(mesh="H", repair_type="one_touch_fix"))
        assert "STILL not watertight" in msg

    def test_a_stitch_that_moves_nothing_on_an_open_mesh_is_reported_not_failed(self, monkeypatch):
        # only a HOLE-CLOSING repair can be convicted by an open mesh. stitch_and_remove was never
        # asked to close anything, so its no-op is reported as measured in the result note.
        _rig(monkeypatch, mesh=_mesh(is_closed=False, volume=12.0))
        out = payload(mr.handler(mesh="H", repair_type="stitch_and_remove"))
        assert out["changed"] == []
        assert out["watertight"] is False
        assert "found nothing of its kind to fix" in out["note"]

    def test_close_holes_that_moves_the_mesh_but_leaves_it_open_is_partial(self, monkeypatch):
        mesh = _mesh(is_closed=False)
        _rig(monkeypatch, mesh=mesh, on_add=_grow(mesh, tri=40, nodes=20, close=False))
        out = payload(mr.handler(mesh="H", repair_type="close_holes"))
        assert out["watertight"] is False
        assert out["changed"] == ["triangle_count", "vertex_count"]
        assert "PARTIAL" in out["note"]

    def test_a_successful_close_holes_reports_the_counts_and_the_watertight_flip(self, rig):
        out = payload(mr.handler(mesh="H", repair_type="close_holes"))
        assert out["before"] == {"triangle_count": 1000, "vertex_count": 502, "is_closed": False,
                                 "mesh_body_count": 1}
        assert out["after"] == {"triangle_count": 1120, "vertex_count": 562, "is_closed": True,
                                "mesh_body_count": 1}
        # the enclosed volume joins the signal: 0.0 while open is a READING, not an unreadable
        # field, so closing the mesh shows up as a volume change and not merely as a flag flip.
        assert out["changed"] == ["triangle_count", "vertex_count", "is_closed", "volume"]
        assert out["volume_change"] == pytest.approx(8000.0)   # 8 cm3 -> mm3
        assert out["watertight"] is True
        assert "PARTIAL" not in out["note"]

    def test_a_vertex_only_change_still_counts_as_repaired(self, monkeypatch):
        mesh = _mesh(tri=1000, nodes=502)
        _rig(monkeypatch, mesh=mesh, on_add=_grow(mesh, nodes=-2))
        out = payload(mr.handler(mesh="H", repair_type="stitch_and_remove"))
        assert out["changed"] == ["vertex_count"]

    def test_a_new_mesh_body_alone_counts_as_repaired(self, monkeypatch):
        mesh = _mesh(tri=1000, nodes=502)
        mesh_, comp, feats, _bf = _rig(monkeypatch, mesh=mesh)
        feats._on_add = lambda: comp.meshBodies._items.append(_mesh(name="Wrap1"))
        out = payload(mr.handler(mesh="H", repair_type="wrap"))
        assert out["changed"] == ["mesh_body_count"]
        assert out["after"]["mesh_body_count"] == 2

    def test_a_volume_only_change_is_detected_and_reported_in_the_call_units(self, monkeypatch):
        # counts and flags identical, geometry moved: volume is the only signal left.
        mesh = _mesh(tri=1000, nodes=502, is_closed=True, volume=8.0)
        _rig(monkeypatch, mesh=mesh)
        mesh.parentComponent.features.meshRepairFeatures._on_add = (
            lambda: setattr(mesh, "_volume_cm3", 9.0))
        out = payload(mr.handler(mesh="H", repair_type="one_touch_fix", units="mm"))
        assert out["changed"] == ["volume"]
        assert out["volume_change"] == pytest.approx(1000.0)   # 1 cm3 = 1000 mm3
        assert out["units"] == "mm"

    def test_a_mesh_that_stays_open_reports_a_real_zero_volume_change(self, monkeypatch):
        # MeshBody.volume RETURNS 0.0 on a mesh that is not closed, so both ends read a number: the
        # delta is a measured zero (the geometry moved, the enclosed volume did not appear), and
        # 'volume' must NOT be listed as changed.
        mesh = _mesh(is_closed=False, volume=12.0)
        _rig(monkeypatch, mesh=mesh, on_add=_grow(mesh, tri=40, nodes=20, close=False))
        out = payload(mr.handler(mesh="H", repair_type="close_holes", units="mm"))
        assert out["watertight"] is False
        assert out["volume_change"] == 0.0
        assert "volume" not in out["changed"]

    def test_volume_change_is_null_only_when_the_field_cannot_be_read(self, monkeypatch):
        # the OTHER meaning of the field: a volume read that RAISES is unreadable and publishes
        # null - never the 0.0 an open mesh legitimately reports. The counts still read, so the
        # repair itself is still verified.
        mesh = _mesh(is_closed=False, volume_readable=False)
        _rig(monkeypatch, mesh=mesh, on_add=_grow(mesh, tri=40, nodes=20, close=True))
        out = payload(mr.handler(mesh="H", repair_type="close_holes"))
        assert out["volume_change"] is None
        assert out["changed"] == ["triangle_count", "vertex_count", "is_closed"]

    def test_an_unreadable_mesh_is_reported_as_unverified(self, monkeypatch):
        mesh = _mesh()
        mesh_, comp, feats, _bf = _rig(monkeypatch, mesh=mesh)

        def _wipe():
            mesh._dead = True
            comp.dead = True
        feats._on_add = _wipe
        msg = error_message(mr.handler(mesh="H", repair_type="one_touch_fix"))
        assert "UNVERIFIED" in msg

    def test_an_add_failure_surfaces_and_is_not_swallowed(self, monkeypatch):
        mesh, comp, feats, bf = _rig(monkeypatch, raise_on_add=True)
        msg = error_message(mr.handler(mesh="H", repair_type="wrap"))
        assert "meshRepairFeatures.add raised" in msg
        assert bf._starts == 0   # no scope is opened, so none can be left open

    def test_a_raw_number_on_density_would_be_refused_by_the_api(self, rig):
        # the fake input rejects a non-ValueInput exactly as the live typed property does; the tool
        # must wrap, so this stays green - a regression to a raw float turns it red.
        rig.monkeypatch.setattr(mr.adsk.core.ValueInput, "createByReal", lambda v: v)
        assert "configure" in error_message(mr.handler(mesh="H", repair_type="rebuild",
                                                       density=128))


# ── _moved_facts (the change verdict, in isolation) ──────────────────────────────────────────────

class TestMovedFacts:
    def _facts(self, tri=10, vert=5, closed=True, bodies=1):
        return {"triangle_count": tri, "vertex_count": vert, "is_closed": closed,
                "mesh_body_count": bodies}

    def test_identical_facts_move_nothing(self):
        moved, comparable = mr._moved_facts(self._facts(), self._facts())
        assert moved == []
        assert comparable == list(mr._FACT_KEYS)

    def test_an_unreadable_field_is_neither_moved_nor_comparable(self):
        before = self._facts()
        after = dict(self._facts(), triangle_count=None)
        moved, comparable = mr._moved_facts(before, after)
        assert moved == []
        assert "triangle_count" not in comparable

    def test_a_flipped_flag_is_reported_as_moved(self):
        moved, _ = mr._moved_facts(self._facts(closed=False), self._facts(closed=True))
        assert moved == ["is_closed"]


class TestGuardsAndFailurePathsBite:
    """Input guards and the two API-failure paths - none had an asserting test."""

    def test_offset_outside_accurate_is_refused_naming_the_method(self, monkeypatch):
        _rig(monkeypatch)
        msg = error_message(mr.handler(mesh="H", repair_type="rebuild",
                                       rebuild_method="fast", offset=1.0))
        assert "rebuild_method='accurate' only" in msg and "fast" in msg

    def test_density_below_the_floor_is_refused_naming_the_range(self, monkeypatch):
        _rig(monkeypatch)
        msg = error_message(mr.handler(mesh="H", repair_type="rebuild", density=2))
        assert str(mr._DENSITY_MIN) in msg and str(mr._DENSITY_MAX) in msg

    def test_density_above_the_ceiling_is_refused(self, monkeypatch):
        _rig(monkeypatch)
        msg = error_message(mr.handler(mesh="H", repair_type="rebuild", density=9999))
        assert str(mr._DENSITY_MAX) in msg

    def test_a_non_numeric_density_is_refused(self, monkeypatch):
        _rig(monkeypatch)
        msg = error_message(mr.handler(mesh="H", repair_type="rebuild", density="thick"))
        assert "must be a number" in msg

    def test_density_the_api_did_not_take_is_an_error_not_an_echo(self, monkeypatch):
        # The feature's own ModelParameter is the authority. Publishing the REQUEST would report a
        # rebuild at 128 that Fusion actually ran at 64.
        mesh = _mesh(tri=1000, nodes=502, is_closed=False)
        _rig(monkeypatch, mesh=mesh, on_add=_grow(mesh, tri=120, nodes=60, close=True),
             feature_density=64)
        msg = error_message(mr.handler(mesh="H", repair_type="rebuild", density=128))
        assert "created with density = 64.0" in msg and "128.0 was requested" in msg

    def test_offset_the_api_did_not_take_is_an_error(self, monkeypatch):
        mesh = _mesh(tri=1000, nodes=502, is_closed=False)
        _rig(monkeypatch, mesh=mesh, on_add=_grow(mesh, tri=120, nodes=60, close=True),
             feature_offset=0.9)
        msg = error_message(mr.handler(mesh="H", repair_type="rebuild",
                                       rebuild_method="accurate", offset=10, units="mm"))
        assert "created with offset = 0.9" in msg and "requested" in msg

    def test_an_unreadable_density_is_reported_as_unverified_not_as_the_request(self, monkeypatch):
        mesh = _mesh(tri=1000, nodes=502, is_closed=False)
        _rig(monkeypatch, mesh=mesh, on_add=_grow(mesh, tri=120, nodes=60, close=True),
             none_feature=True)
        out = payload(mr.handler(mesh="H", repair_type="rebuild", density=128))
        assert out["density"] is None and "density_unverified" in out

    def test_a_repair_type_the_input_never_took_is_an_error(self, monkeypatch):
        # A SWIG proxy accepts an assignment to a name it does not define, so a set that lands
        # nowhere reads back unchanged, and that read-back is the only thing that catches it.
        _rig(monkeypatch)
        monkeypatch.setattr(_RepairInput, "swallow_enum_sets", True)
        msg = error_message(mr.handler(mesh="H", repair_type="close_holes"))
        assert "did not take" in msg and "close_holes" in msg

    def test_a_createinput_returning_nothing_is_an_error(self, monkeypatch):
        _rig(monkeypatch, create_input_returns_none=True)
        msg = error_message(mr.handler(mesh="H", repair_type="close_holes"))
        assert "createInput returned nothing" in msg

    def test_an_add_that_raises_surfaces_the_api_text(self, monkeypatch):
        _rig(monkeypatch, raise_on_add=True)
        msg = error_message(mr.handler(mesh="H", repair_type="wrap"))
        assert "meshRepairFeatures.add raised" in msg
