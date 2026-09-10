"""Unit tests for mesh_get.py - the MESH body listing, and the shared mesh reads it publishes."""

import adsk.fusion
import pytest

from conftest import (BRepBody, FakeBaseFeature, MakeComp, MeshBody, install, load_tool, make_bbox,
                      make_design, make_occurrence, payload)

mo = load_tool("mesh_get")
mesh_common_mod = load_tool("_mesh_common")


@pytest.fixture(autouse=True)
def _types(monkeypatch):
    """The adsk.fusion type identities the body kind and the base-feature scope check branch on."""
    monkeypatch.setattr(adsk.fusion, "MeshBody", MeshBody, raising=False)
    monkeypatch.setattr(adsk.fusion, "BRepBody", BRepBody, raising=False)
    monkeypatch.setattr(adsk.fusion, "BaseFeature", FakeBaseFeature, raising=False)


def _wire(comp, all_components=None):
    """Wire a design rooted at `comp` into the tool through both design seams."""
    return install(mo, make_design(comp=comp, all_components=all_components))


class TestMeshGet:

    def test_lists_meshes_with_counts(self):
        m1 = MeshBody("ScanA", tri=1200, nodes=602)
        m2 = MeshBody("ScanB", tri=80, nodes=42)
        _wire(MakeComp("Comp", mesh_bodies=[m1, m2]))
        out = payload(mo.handler(target=""))
        assert out["count"] == 2
        by_name = {m["name"]: m for m in out["meshes"]}
        assert by_name["ScanA"]["triangle_count"] == 1200
        assert by_name["ScanA"]["node_count"] == 602
        assert by_name["ScanB"]["triangle_count"] == 80
        # the handle (entityToken) is surfaced for the geometry-as-values bridge
        assert by_name["ScanA"]["handle"] == "MTOK::ScanA"

    def test_empty_when_no_meshes(self):
        _wire(MakeComp("Comp", mesh_bodies=[]))
        out = payload(mo.handler(target=""))
        assert out["count"] == 0 and out["meshes"] == []

    def test_no_design_errors(self, monkeypatch):
        _wire(MakeComp("Comp", mesh_bodies=[]))
        monkeypatch.setattr(mo._common, "design", lambda: None)
        res = mo.handler(target="")
        assert res["isError"] is True and "No active design" in res["message"]

    def test_named_component_scopes_to_that_component(self):
        # target=<component name> lists only that component's meshes (not the whole design)
        root = MakeComp("Root", mesh_bodies=[MeshBody("RootScan", tri=10)])
        sub = MakeComp("SubPart", mesh_bodies=[MeshBody("SubScan", tri=20)])
        _wire(root, all_components=[root, sub])
        out = payload(mo.handler(target="SubPart"))
        assert out["count"] == 1
        assert out["meshes"][0]["name"] == "SubScan"
        assert out["scope"] == "SubPart"

    def test_named_occurrence_scopes_to_its_component(self):
        # target=<occurrence name> lists that INSTANCE's component - the vocabulary a tree read emits.
        sub = MakeComp("SubPart", mesh_bodies=[MeshBody("SubScan", tri=20)])
        root = MakeComp("Root", mesh_bodies=[MeshBody("RootScan", tri=10)])
        root.allOccurrences = [make_occurrence("SubPart:1", sub)]
        _wire(root, all_components=[root, sub])
        out = payload(mo.handler(target="SubPart:1"))
        assert out["count"] == 1 and out["meshes"][0]["name"] == "SubScan"

    def test_an_occurrence_name_two_instances_share_is_refused(self):
        # Occurrence names are NOT unique (two sub-assemblies each hold a 'Bolt:1'), so listing the
        # meshes of whichever instance the walk reached first would answer about the wrong part.
        one = MakeComp("BoltA", mesh_bodies=[MeshBody("ScanA")])
        two = MakeComp("BoltB", mesh_bodies=[MeshBody("ScanB")])
        root = MakeComp("Root", mesh_bodies=[])
        root.allOccurrences = [make_occurrence("SubA:1+Bolt:1", one),
                               make_occurrence("SubB:1+Bolt:1", two)]
        _wire(root, all_components=[root, one, two])
        res = mo.handler(target="Bolt:1")
        assert res["isError"] is True
        assert "2 occurrences" in res["message"]
        assert "SubA:1+Bolt:1" in res["message"] and "SubB:1+Bolt:1" in res["message"]

    def test_a_component_name_two_components_share_is_refused(self):
        # Two components named 'SubPart' - listing the meshes of whichever the design-wide walk
        # reached first would answer about the wrong part, exactly as for a shared occurrence name.
        one = MakeComp("SubPart", mesh_bodies=[MeshBody("ScanA")])
        two = MakeComp("SubPart", mesh_bodies=[MeshBody("ScanB")])
        root = MakeComp("Root", mesh_bodies=[])
        _wire(root, all_components=[root, one, two])
        res = mo.handler(target="SubPart")
        assert res["isError"] is True
        assert "2 components match 'SubPart'" in res["message"]
        # this read takes no handle, so it offers the occurrence vocabulary and its own '' scope -
        # and never a handle it would refuse
        assert "occurrence name/fullPathName" in res["message"]
        assert "target=''" in res["message"]
        assert "find_geometry" not in res["message"]

    def test_unknown_component_name_errors(self):
        root = MakeComp("Root", mesh_bodies=[MeshBody("RootScan")])
        _wire(root, all_components=[root])
        res = mo.handler(target="Ghost")
        assert res["isError"] is True
        assert "Ghost" in res["message"] and "design_get" in res["message"]

    def test_dedup_same_mesh_listed_once(self):
        # the same MeshBody reachable through two components must appear only once (seen-set dedup)
        shared = MeshBody("Shared", token="MTOK::Shared")
        root = MakeComp("Root", mesh_bodies=[shared])
        sub = MakeComp("SubPart", mesh_bodies=[shared])   # same object, different component
        root.allOccurrences = [make_occurrence("SubPart", sub)]
        _wire(root, all_components=[root, sub])
        out = payload(mo.handler(target=""))
        names = [m["name"] for m in out["meshes"]]
        assert names.count("Shared") == 1     # deduped despite being in two components
        assert out["count"] == 1

    def test_under_cap_untruncated_and_unchanged(self):
        meshes = [MeshBody(f"Scan{i}") for i in range(5)]
        _wire(MakeComp("Comp", mesh_bodies=meshes))
        out = payload(mo.handler(target=""))
        assert out["truncated"] is False
        assert out["count"] == 5 and len(out["meshes"]) == 5

    def test_at_cap_truncates_and_flags(self):
        meshes = [MeshBody(f"Scan{i}", token=f"T{i}") for i in range(60)]
        _wire(MakeComp("Comp", mesh_bodies=meshes))
        out = payload(mo.handler(target="", max_results=50))
        assert out["truncated"] is True
        assert len(out["meshes"]) == 50
        # the full count is still honest, even though the array is capped
        assert out["count"] == 60

    def test_the_default_caps_the_array_when_no_max_results_is_named(self):
        # Every other cap test here names max_results, so none of them exercises the DEFAULT the
        # schema promises - the number a caller who names no cap actually gets. 51 meshes with
        # nothing named must come back as 50, and the promise and the applied cap must be one number.
        meshes = [MeshBody(f"Scan{i}", token=f"T{i}") for i in range(51)]
        _wire(MakeComp("Comp", mesh_bodies=meshes))
        out = payload(mo.handler(target=""))
        assert len(out["meshes"]) == 50
        assert out["truncated"] is True and out["count"] == 51
        promised = mo.tool.to_dict()["inputSchema"]["properties"]["max_results"]["default"]
        assert len(out["meshes"]) == promised

    def test_a_zero_max_results_falls_back_to_the_default_not_the_ceiling(self):
        # The constant's SECOND use site: the 'default' argument handed to clamp_rows. The test
        # above only exercises the signature, which a request of 0 never reaches - 0 is a legal
        # wire value (integer, no minimum) and clamp_rows falls a falsy request back to its
        # 'default' argument. Handing the CEILING there instead reads identically until a caller
        # sends 0, and then 120 rows cross the wire where 50 should.
        meshes = [MeshBody(f"Scan{i}", token=f"T{i}") for i in range(120)]
        _wire(MakeComp("Comp", mesh_bodies=meshes))
        out = payload(mo.handler(target="", max_results=0))
        assert len(out["meshes"]) == 50
        assert out["truncated"] is True and out["count"] == 120
        promised = mo.tool.to_dict()["inputSchema"]["properties"]["max_results"]["default"]
        assert len(out["meshes"]) == promised

    def test_a_caller_cannot_lift_the_cap_past_the_ceiling(self):
        # every row crosses the wire: max_results is clamped into 1..200, so an oversized
        # request is held at the ceiling, not honoured.
        meshes = [MeshBody(f"Scan{i}", token=f"T{i}") for i in range(210)]
        _wire(MakeComp("Comp", mesh_bodies=meshes))
        out = payload(mo.handler(target="", max_results=999999))
        assert len(out["meshes"]) == 200
        assert out["truncated"] is True and out["count"] == 210

    def test_reports_area_and_volume_scaled_to_units(self):
        # MeshBody.area/volume are cm^2/cm^3 (Fusion's internal units) - default units=mm scales by
        # inv_scale^2 / inv_scale^3 (10^2 / 10^3), the same cm-based idiom model_inspect uses.
        m = MeshBody("Scan", area=6.0, volume=2.0)     # cm^2, cm^3
        _wire(MakeComp("Comp", mesh_bodies=[m]))
        out = payload(mo.handler(target=""))
        rec = out["meshes"][0]
        assert abs(rec["area"] - 600.0) < 1e-6      # 6 cm^2 -> 600 mm^2
        assert abs(rec["volume"] - 2000.0) < 1e-6   # 2 cm^3 -> 2000 mm^3
        assert out["units"] == "mm"

    def test_area_and_volume_respect_units_param(self):
        m = MeshBody("Scan", area=6.0, volume=2.0)     # cm^2, cm^3
        _wire(MakeComp("Comp", mesh_bodies=[m]))
        out = payload(mo.handler(target="", units="cm"))
        rec = out["meshes"][0]
        assert abs(rec["area"] - 6.0) < 1e-6
        assert abs(rec["volume"] - 2.0) < 1e-6
        assert out["units"] == "cm"

    def test_the_polygon_census_is_published_only_when_it_reads(self):
        # polygon_count comes off the PolygonMesh, not the display triangles, so the two counts are
        # deliberately different here. The guard beside it is what the key MEANS: a mesh whose
        # census does not read carries no key at all, never a null or a 0.
        counted = MeshBody("Counted", tri=1200, polygons=640)
        silent = MeshBody("Silent", tri=1200)
        _wire(MakeComp("Comp", mesh_bodies=[counted, silent]))
        by_name = {m["name"]: m for m in payload(mo.handler(target=""))["meshes"]}
        assert by_name["Counted"]["polygon_count"] == 640
        assert "polygon_count" not in by_name["Silent"]

    def test_an_open_mesh_publishes_volume_zero_not_null(self):
        # MeshBody.volume on a mesh that is not closed RETURNS 0.0 - it does not raise - so 0.0 is
        # the API's answer for a body that encloses nothing and the record publishes it as a number.
        m = MeshBody("OpenScan", is_closed=False, area=4.0, volume=0.0)
        _wire(MakeComp("Comp", mesh_bodies=[m]))
        out = payload(mo.handler(target=""))
        rec = out["meshes"][0]
        assert rec["is_closed"] is False
        assert rec["volume"] == 0.0
        assert rec["volume"] is not None
        assert abs(rec["area"] - 400.0) < 1e-6   # 4 cm^2 -> 400 mm^2

    def test_volume_is_null_only_when_the_field_cannot_be_read(self):
        # the OTHER meaning of the field: a read that raises is published as null and never sinks
        # the record - area (which DOES read cleanly) is still reported.
        m = MeshBody("DeadScan", is_closed=True, area=4.0, volume_readable=False)
        _wire(MakeComp("Comp", mesh_bodies=[m]))
        out = payload(mo.handler(target=""))
        rec = out["meshes"][0]
        assert rec["volume"] is None
        assert abs(rec["area"] - 400.0) < 1e-6   # unaffected by the volume failure

    def test_the_note_tells_a_zero_volume_apart_from_a_null_one(self):
        # the wire note is the only place a reader learns which of the two a number/null means.
        _wire(MakeComp("Comp", mesh_bodies=[MeshBody("Scan")]))
        note = payload(mo.handler(target=""))["note"]
        assert "reads 0.0" in note and "is_closed=false" in note
        assert "could not be read" in note
        assert "is null for a mesh that is not watertight" not in note

    def test_unknown_units_rejected(self):
        _wire(MakeComp("Comp", mesh_bodies=[MeshBody("Scan")]))
        res = mo.handler(target="", units="parsec")
        assert res["isError"] is True
        assert "parsec" in res["message"]


class TestMeshMeasure:

    def test_measures_a_mesh_body(self):
        m = MeshBody("Scan", tri=999, nodes=500, bbox=make_bbox((0, 0, 0), (1, 2, 4)))
        out = payload(mesh_common_mod.mesh_measure_of_body(m, units="mm"))
        assert out["triangle_count"] == 999 and out["node_count"] == 500
        assert out["is_closed"] is True
        # bbox scaled from cm -> mm (x10): 1cm,2cm,4cm -> 10,20,40
        assert abs(out["bbox"]["x"] - 10) < 1e-6
        assert abs(out["bbox"]["z"] - 40) < 1e-6

    def test_non_watertight_carries_warning(self):
        out = payload(mesh_common_mod.mesh_measure_of_body(MeshBody("Open", is_closed=False)))
        assert out["is_closed"] is False and "not watertight" in out["note"].lower()

    def test_measure_reports_area_volume_scaled(self):
        m = MeshBody("Scan", area=10.0, volume=5.0)
        out = payload(mesh_common_mod.mesh_measure_of_body(m, units="mm"))
        assert abs(out["area"] - 1000.0) < 1e-6      # 10 cm^2 -> 1000 mm^2
        assert abs(out["volume"] - 5000.0) < 1e-6    # 5 cm^3 -> 5000 mm^3

    def test_measure_of_an_open_mesh_reports_volume_zero_and_says_why(self):
        # 0.0 is the measured open-mesh reading, so the note has to say the body is not empty - it
        # encloses nothing - or a caller reads the number as a vanished body.
        m = MeshBody("Open", is_closed=False, area=4.0, volume=0.0)
        out = payload(mesh_common_mod.mesh_measure_of_body(m))
        assert out["volume"] == 0.0
        assert "reads 0.0" in out["note"] and "nothing enclosed" in out["note"]

    def test_measure_volume_null_when_the_field_cannot_be_read(self):
        m = MeshBody("Dead", volume_readable=False)   # area absent too -> both null
        out = payload(mesh_common_mod.mesh_measure_of_body(m))
        assert out["volume"] is None
        assert out["area"] is None


class TestMeshGetNote:

    def test_the_note_states_the_measured_open_mesh_volume(self):
        # the note rides beside the number an agent reads, so it carries the 0.0-vs-null split the
        # field has: 0.0 is an ANSWER (nothing enclosed), null is a field that did not read.
        _wire(MakeComp("Comp", mesh_bodies=[MeshBody("Open", is_closed=False, volume=0.0)]))
        note = payload(mo.handler(target=""))["note"]
        assert "reads 0.0 on a mesh that is not watertight" in note
        assert "could not be read at all" in note
        assert "'volume' is null for a mesh that is not watertight" not in note


class TestAreaVolumeSignal:

    def test_reads_area_and_volume_in_internal_cm_units(self):
        m = MeshBody("M", area=150.0, volume=125.0)
        assert mesh_common_mod._area_volume(m) == (150.0, 125.0)

    def test_an_unreadable_field_is_None_not_zero(self):
        # 0.0 is an ANSWER for MeshBody.volume (a body enclosing nothing), so an unreadable read
        # must not borrow it - a coerced 0.0 would read as "the geometry vanished".
        assert mesh_common_mod._area_volume(MeshBody("M", volume_readable=False)) == (None, None)
        assert mesh_common_mod._area_volume(
            MeshBody("M", area=12.0, volume_readable=False)) == (12.0, None)

    def test_a_moved_area_reports_movement(self):
        assert mesh_common_mod._mesh_moved((150.0, 125.0), (90.0, 125.0)) is True

    def test_a_moved_volume_alone_reports_movement(self):
        # Either signal is sufficient: a cut can shave volume while the surface area holds.
        assert mesh_common_mod._mesh_moved((150.0, 125.0), (150.0, 62.5)) is True

    def test_identical_readings_are_flat(self):
        assert mesh_common_mod._mesh_moved((150.0, 125.0), (150.0, 125.0)) is False

    def test_a_difference_inside_the_band_is_flat(self):
        # Float noise on a re-read is not a cut; the band is what keeps it from reading as one.
        assert mesh_common_mod._mesh_moved((150.0, 125.0), (150.0 + 1e-12, 125.0 - 1e-12)) is False

    def test_one_readable_signal_still_decides(self):
        assert mesh_common_mod._mesh_moved((None, 125.0), (None, 62.5)) is True
        assert mesh_common_mod._mesh_moved((None, 125.0), (None, 125.0)) is False

    def test_neither_signal_readable_is_UNKNOWN_never_flat(self):
        # None, not False: a caller that treated unknown as "flat" would refuse a landed cut it
        # simply could not measure.
        assert mesh_common_mod._mesh_moved((None, None), (None, None)) is None
        assert mesh_common_mod._mesh_moved((150.0, 125.0), (None, None)) is None
