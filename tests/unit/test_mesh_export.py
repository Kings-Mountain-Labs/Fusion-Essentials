"""Unit tests for mesh_export.py - the mesh file writer, its options and the split mode."""

import adsk.fusion
import pytest

from conftest import (BRepBody, FakeExportManager, MakeComp, MakeDesign, MeshBody, _ExportOptions,
                      _MeshBodies, _NamedCollection, install, load_tool, make_occurrence, payload)

mx = load_tool("mesh_export")

# The unitType an STL options object holds when it is NOT the one asked for - anything that is no
# DistanceUnits member reads back as "the write never landed".
_FOREIGN_UNIT = "FACTORY_DEFAULT"


def _record(des, kind, geom, path, **kw):
    """Build the shared options bag (this file's factories call in (geometry, path) order) and put it
    on the export manager's walk - what every create*Options factory here does."""
    rec = _ExportOptions(kind, path, geom, **kw)
    des.exportManager._calls.append(rec)
    return rec


def _refine_member(key):
    """The MeshRefinementSettings member for a refinement key, off the mock - never a hand-typed
    sentinel, so a measured int (MeshRefinementHigh is 0) flows through unchanged."""
    return getattr(mx.adsk.fusion.MeshRefinementSettings, mx._REFINEMENTS[key])


def _options_holding(des, attr, value, factory_name, kind, drop_write=True):
    """Point ONE create*Options factory at options whose FACTORY value for 'attr' ALREADY reads
    'value', optionally dropping the write.

    This is the live shape of the two requests whose read-back cannot bite: measured, a fresh
    options object reads unitType 0 and MillimeterDistanceUnits IS 0, and reads meshRefinement 1
    and MeshRefinementMedium IS 1 - so the property answers the requested value whether the
    assignment took or never happened."""
    def _opt(geom, path):
        return _record(des, kind, geom, path, seeded={attr: value},
                       drops=(attr,) if drop_write else ())
    setattr(des.exportManager, factory_name, _opt)
    return _opt


def _writes_a_file(opts):
    """REALISTIC live divergence: execute() answers True whatever it wrote, but only a BRep body /
    component / occurrence geometry lands a file - a BARE MeshBody writes nothing."""
    return not isinstance(opts.geom, MeshBody)


@pytest.fixture(autouse=True)
def _types(monkeypatch):
    """The adsk types the shared body kinds isinstance-check a resolved target against."""
    monkeypatch.setattr(adsk.fusion, "BRepBody", BRepBody, raising=False)
    monkeypatch.setattr(adsk.fusion, "MeshBody", MeshBody, raising=False)


def _comp(name="Root", bodies=(), occurrences=(), meshes=()):
    """A component whose meshBodies answers the live count/item protocol."""
    return MakeComp(name, bodies=bodies, occurrences=occurrences, mesh_bodies=meshes)


def _wire(comp, all_comps=None, handles=None):
    """mesh_export wired onto a design rooted at `comp` and carrying a FakeExportManager - both
    design seams patched, `handles` the entityToken map a target resolves through."""
    design = MakeDesign(comp=comp, tokens=handles, all_components=all_comps)
    design.exportManager = FakeExportManager(writes=_writes_a_file)
    return install(mx, design)


def test_the_mesh_collection_offers_no_itembyname_lookup():
    # meshbodies-no-itembyname: meshBodies answers count/item(i) and NO itemByName, while
    # bRepBodies answers all three - a mesh name resolves only by iterate-and-match.
    comp = _comp("Root", bodies=[BRepBody("LegBox")], meshes=[MeshBody("ScanA")])
    assert comp.meshBodies.count == 1 and comp.meshBodies.item(0).name == "ScanA"
    assert not hasattr(comp.meshBodies, "itemByName")
    assert comp.bRepBodies.itemByName("LegBox") is not None


class TestExportFormatDispatch:

    def test_obj_uses_obj_options_and_executes(self, tmp_path):
        comp = _comp("Root", bodies=[BRepBody("Body1")])
        des = _wire(comp)
        out = payload(mx.handler(format="obj", file_path=str(tmp_path / "p.obj")))
        assert out["exported"] is True
        assert des.exportManager._calls[-1].kind == "obj"
        assert des.exportManager._executed is not None
        assert out["file_exists"] is True and out["size_bytes"] > 0

    def test_3mf_uses_c3mf_options(self, tmp_path):
        des = _wire(_comp("Root", bodies=[BRepBody("Body1")]))
        out = payload(mx.handler(format="3mf", file_path=str(tmp_path / "p.3mf")))
        assert des.exportManager._calls[-1].kind == "3mf"
        assert out["format"] == "3mf"

    def test_stl_uses_stl_options(self, tmp_path):
        des = _wire(_comp("Root", bodies=[BRepBody("Body1")]))
        payload(mx.handler(format="stl", file_path=str(tmp_path / "p.stl")))
        assert des.exportManager._calls[-1].kind == "stl"

    def test_default_format_is_3mf(self, tmp_path):
        des = _wire(_comp("Root", bodies=[BRepBody("Body1")]))
        out = payload(mx.handler(file_path=str(tmp_path / "p")))
        assert des.exportManager._calls[-1].kind == "3mf"
        assert out["file_path"].lower().endswith(".3mf")   # extension auto-appended

    def test_bad_format_rejected_by_choice(self, tmp_path):
        _wire(_comp("Root", bodies=[BRepBody("Body1")]))
        res = mx.handler(format="dwg", file_path=str(tmp_path / "p.dwg"))
        assert res["isError"] is True and "format" in res["message"]


class TestExportTarget:

    def test_whole_design_when_no_target(self, tmp_path):
        comp = _comp("Root", bodies=[BRepBody("Body1")])
        des = _wire(comp)
        out = payload(mx.handler(format="obj", file_path=str(tmp_path / "p.obj")))
        # whole-design export passes the ROOT COMPONENT as the geometry
        assert des.exportManager._calls[-1].geom is comp
        assert "design" in out["target"].lower() or "root" in out["target"].lower()

    def test_body_by_name(self, tmp_path):
        comp = _comp("Root", bodies=[BRepBody("Widget")])
        des = _wire(comp)
        out = payload(mx.handler(format="obj", target="Widget", file_path=str(tmp_path / "p.obj")))
        assert des.exportManager._calls[-1].geom.name == "Widget"
        assert "Widget" in out["target"]

    def test_mesh_body_by_handle_redirects_to_its_component(self, tmp_path):
        # A bare MeshBody can't be export-written (execute()->True but no file). The tool
        # REDIRECTS to the mesh's parentComponent (which DOES write a file) and flags the redirect.
        comp = _comp("Root")
        m = MeshBody("ScanA")
        m.parentComponent = comp
        comp.meshBodies = _MeshBodies([m])     # realistic: no itemByName
        des = _wire(comp, handles={"H": m})
        out = payload(mx.handler(format="3mf", target="H", file_path=str(tmp_path / "p.3mf")))
        # the COMPONENT was exported, not the bare mesh — and a file actually landed
        assert des.exportManager._calls[-1].geom is comp
        assert out["redirected_from_mesh"] is True
        assert out["file_exists"] is True and out["size_bytes"] > 0
        assert "mesh" in out["note"].lower()

    def test_mesh_body_by_name_redirects_to_its_component(self, tmp_path):
        # A mesh resolves by NAME via count/item iteration (meshBodies has NO itemByName), then
        # redirects to its parentComponent for the actual file write.
        comp = _comp("Root")
        m = MeshBody("ScanByName")
        m.parentComponent = comp
        comp.meshBodies = _MeshBodies([m])     # realistic: no itemByName -> must iterate
        des = _wire(comp)
        out = payload(mx.handler(format="3mf", target="ScanByName",
                                         file_path=str(tmp_path / "p.3mf")))
        assert des.exportManager._calls[-1].geom is comp
        assert out["redirected_from_mesh"] is True
        assert out["file_exists"] is True

    def test_the_redirect_note_names_the_brep_bodies_and_the_children_the_file_carries(self, tmp_path):
        # The component export writes the component's BRep bodies AND the bodies of the occurrences
        # below it into the mesh file, so the note may not promise its mesh bodies alone.
        comp = _comp("Root", bodies=[BRepBody("LegBox")], occurrences=[make_occurrence("ChildBox:1")])
        m = MeshBody("LegMesh")
        m.parentComponent = comp
        comp.meshBodies = _MeshBodies([m])
        des = _wire(comp, handles={"H": m})
        out = payload(mx.handler(format="stl", target="H",
                                         file_path=str(tmp_path / "p.stl")))
        assert des.exportManager._calls[-1].geom is comp
        note = out["note"]
        assert "EVERY body" in note and "BRep" in note and "tessellated" in note, note
        assert "occurrences below it" in note, note

    def test_the_redirect_census_counts_the_bodies_and_the_child_occurrences(self, tmp_path):
        comp = _comp("Root", bodies=[BRepBody("LegBox"), BRepBody("Plate")],
                     occurrences=[make_occurrence("ChildBox:1"), make_occurrence("ChildBox:2"),
                                  make_occurrence("ChildBox:3")])
        m = MeshBody("LegMesh")
        m.parentComponent = comp
        comp.meshBodies = _MeshBodies([m])
        _wire(comp, handles={"H": m})
        out = payload(mx.handler(format="stl", target="H",
                                         file_path=str(tmp_path / "p.stl")))
        # the three counts differ, so reading any census key off another collection goes red
        assert out["component_census"] == {"mesh_bodies": 1, "brep_bodies": 2,
                                           "child_occurrences": 3}

    def test_a_count_that_does_not_read_is_published_null_not_zero(self, tmp_path):
        # A census 0 says "the file carries none of those"; an unreadable count says nothing.

        class _BlindBodyColl(_NamedCollection):
            @property
            def count(self):
                raise RuntimeError("bRepBodies.count unreadable")

        comp = _comp("Root", bodies=[BRepBody("LegBox")],
                     occurrences=[make_occurrence("ChildBox:1"), make_occurrence("ChildBox:2")])
        comp.bRepBodies = _BlindBodyColl([BRepBody("LegBox")])
        m = MeshBody("LegMesh")
        m.parentComponent = comp
        comp.meshBodies = _MeshBodies([m])
        _wire(comp, handles={"H": m})
        out = payload(mx.handler(format="stl", target="H",
                                         file_path=str(tmp_path / "p.stl")))
        assert out["component_census"] == {"mesh_bodies": 1, "brep_bodies": None,
                                           "child_occurrences": 2}

    def test_a_target_that_was_not_redirected_publishes_no_census(self, tmp_path):
        # The census describes what the REDIRECT widened the file to; a body export wrote one body.
        comp = _comp("Root", bodies=[BRepBody("LegBox")])
        _wire(comp)
        out = payload(mx.handler(format="stl", target="LegBox",
                                         file_path=str(tmp_path / "p.stl")))
        assert out["redirected_from_mesh"] is False
        assert "component_census" not in out

    def test_false_success_when_no_file_written_is_error(self, tmp_path):
        # execute() returns True but NO file lands -> tool must ERROR, not report
        # exported:true. Force the no-write by exporting a BARE mesh whose parent ALSO writes nothing
        # (the FakeExportManager skips the write for any MeshBody geometry).
        comp = _comp("Root")
        m = MeshBody("Orphan")
        m.parentComponent = None                    # redirect falls back to root component...
        # ...but make the root resolve to the mesh itself so execute still writes nothing:
        # simplest: target a mesh whose redirect component has no exportable geometry -> use a comp
        # that the fake exporter treats as a mesh is impossible; instead drive the generic no-file
        # path with a non-mesh geom whose execute is stubbed to not write.
        des = _wire(comp, handles={"H": m})
        # execute() returns True without writing a file — the handler must verify the file exists.
        des.exportManager.execute = lambda opts: True
        res = mx.handler(format="3mf", target="H", file_path=str(tmp_path / "p.3mf"))
        assert res["isError"] is True
        assert "no file" in res["message"].lower() or "wrote no file" in res["message"].lower()

    def test_a_stale_file_at_the_path_is_not_this_exports_deliverable(self, tmp_path):
        # execute() lies (True, writes nothing) while a file of the same name from an EARLIER export
        # sits at the path: existence alone would pass it, so the gate compares against the state
        # captured before the write.
        des = _wire(_comp("Root", bodies=[BRepBody("Body1")]))
        target = tmp_path / "p.3mf"
        target.write_text("a 3MF written by an earlier call")
        des.exportManager.execute = lambda opts: True
        res = mx.handler(format="3mf", file_path=str(target))
        assert res["isError"] is True
        assert "already there before this call" in res["message"]

    def test_brep_target_that_writes_a_file_still_succeeds(self, tmp_path):
        # the verification must NOT regress the happy path: a BRep/component target that DOES write a
        # file still reports exported:true.
        comp = _comp("Root", bodies=[BRepBody("Body1")])
        des = _wire(comp)
        out = payload(mx.handler(format="3mf", target="Body1",
                                         file_path=str(tmp_path / "p.3mf")))
        assert out["exported"] is True
        assert out["redirected_from_mesh"] is False
        assert out["file_exists"] is True and out["size_bytes"] > 0

    def test_component_name_fallback_target(self, tmp_path):
        # a MULTI-body component NAME falls back to the whole component: the shared BodyRef refuses
        # to pick one of its bodies, so find_component resolves it as the export geometry
        root = _comp("Root", bodies=[BRepBody("Body1")])
        sub = _comp("SubPart", bodies=[BRepBody("Inner"), BRepBody("Outer")])
        des = _wire(root, all_comps=[root, sub])
        out = payload(mx.handler(format="obj", target="SubPart",
                                         file_path=str(tmp_path / "p.obj")))
        assert des.exportManager._calls[-1].geom is sub
        assert "component" in out["target"].lower() and "SubPart" in out["target"]

    def test_single_body_component_name_exports_its_body(self, tmp_path):
        # a SINGLE-body component name resolves through the shared BodyRef to that body (the
        # component fallback never runs) - the export geometry is the body, reported as a body
        root = _comp("Root", bodies=[BRepBody("Body1")])
        inner = BRepBody("Inner")
        sub = _comp("SubPart", bodies=[inner])
        des = _wire(root, all_comps=[root, sub])
        out = payload(mx.handler(format="obj", target="SubPart",
                                         file_path=str(tmp_path / "p.obj")))
        assert des.exportManager._calls[-1].geom is inner
        assert "body" in out["target"].lower() and "Inner" in out["target"]

    def test_duplicate_component_name_refused_not_exported(self, tmp_path):
        # two components named 'SubPart': the name picks neither, so the export refuses instead of
        # writing whichever the design-wide walk reached first
        root = _comp("Root", bodies=[BRepBody("Body1")])
        a = _comp("SubPart", bodies=[BRepBody("Inner"), BRepBody("Outer")])
        b = _comp("SubPart", bodies=[BRepBody("Left"), BRepBody("Right")])
        des = _wire(root, all_comps=[root, a, b])
        res = mx.handler(format="obj", target="SubPart",
                                file_path=str(tmp_path / "p.obj"))
        assert res["isError"] is True
        assert "2 components match 'SubPart'" in res["message"]
        # the remedies named are the two this tool still resolves after the component step
        assert "occurrence name/fullPathName" in res["message"]
        assert "find_geometry" in res["message"]
        assert "rename" not in res["message"].lower()
        assert des.exportManager._calls == []              # nothing was exported

    def test_occurrence_by_name_target(self, tmp_path):
        # a name that is neither a body nor a component resolves via the allOccurrences scan
        occ = make_occurrence("Root+Arm:1")
        comp = _comp("Root", bodies=[BRepBody("Body1")], occurrences=[occ])
        des = _wire(comp)
        out = payload(mx.handler(format="obj", target="Arm:1",
                                         file_path=str(tmp_path / "p.obj")))
        assert des.exportManager._calls[-1].geom is occ
        assert "occurrence" in out["target"].lower() and "Arm:1" in out["target"]

    def test_occurrence_by_full_path_target(self, tmp_path):
        occ = make_occurrence("Root+Sub+Arm:1")
        comp = _comp("Root", bodies=[BRepBody("Body1")], occurrences=[occ])
        des = _wire(comp)
        out = payload(mx.handler(format="obj", target="Root+Sub+Arm:1",
                                         file_path=str(tmp_path / "p.obj")))
        assert des.exportManager._calls[-1].geom is occ
        assert "occurrence" in out["target"].lower()

    def test_a_name_two_instances_share_is_refused_with_their_paths(self, tmp_path):
        # Occurrence names are NOT unique - instancing a sub-assembly replicates its children's names
        # verbatim, so two 'Bolt:1' live under different parents. Exporting the first hit would write
        # a file of geometry the caller never named, so the name is refused with the paths that do
        # resolve, and NOTHING is exported.
        a = make_occurrence("SubA:1+Bolt:1")
        b = make_occurrence("SubB:1+Bolt:1")
        comp = _comp("Root", bodies=[BRepBody("Body1")], occurrences=[a, b])
        des = _wire(comp)
        res = mx.handler(format="obj", target="Bolt:1", file_path=str(tmp_path / "p.obj"))
        assert res["isError"] is True
        assert "2 occurrences" in res["message"]
        assert "SubA:1+Bolt:1" in res["message"] and "SubB:1+Bolt:1" in res["message"]
        assert des.exportManager._executed is None          # no file was written for either

    def test_the_full_path_still_picks_one_of_the_shared_names(self, tmp_path):
        # The refusal's own remedy has to work: the fullPathName resolves the instance it names.
        a = make_occurrence("SubA:1+Bolt:1")
        b = make_occurrence("SubB:1+Bolt:1")
        comp = _comp("Root", bodies=[BRepBody("Body1")], occurrences=[a, b])
        des = _wire(comp)
        out = payload(mx.handler(format="obj", target="SubB:1+Bolt:1",
                                         file_path=str(tmp_path / "p.obj")))
        assert des.exportManager._calls[-1].geom is b       # the named instance, not the first hit
        assert out["exported"] is True

    def test_missing_named_target_errors(self, tmp_path):
        _wire(_comp("Root", bodies=[BRepBody("Body1")]))
        res = mx.handler(format="obj", target="Nope", file_path=str(tmp_path / "p.obj"))
        assert res["isError"] is True and "Nope" in res["message"]

    def test_missing_path_errors(self):
        _wire(_comp("Root", bodies=[BRepBody("Body1")]))
        res = mx.handler(format="obj")
        assert res["isError"] is True and "file_path" in res["message"]


class TestExportRefinement:

    def _no_refine_export(self, des, tmp_path, refinement="high"):
        """Export through options that DROP the meshRefinement write, so _apply_refinement returns
        None - the density never landed. The dropped set is the only condition modelled; which
        builds/formats behave this way is not asserted."""
        def _stl_opt(geom, path):
            return _record(des, "stl", geom, path, drops=("meshRefinement",))
        des.exportManager.createSTLExportOptions = _stl_opt
        return payload(mx.handler(format="stl", refinement=refinement,
                                          file_path=str(tmp_path / "p.stl")))

    def _refine_options_holding(self, des, member, drop_write=True):
        """OBJ options whose factory meshRefinement ALREADY reads 'member' - the live 'medium'
        shape. OBJ so the note carries the density prose alone, with no STL unit sentence in it."""
        return _options_holding(des, "meshRefinement", member, "createOBJExportOptions", "obj",
                                drop_write)

    def test_refinement_applied_when_supported(self, tmp_path):
        des = _wire(_comp("Root", bodies=[BRepBody("Body1")]))
        out = payload(mx.handler(format="obj", refinement="high",
                                         file_path=str(tmp_path / "p.obj")))
        # the options object carried the high refinement enum
        assert des.exportManager._calls[-1].meshRefinement == _refine_member("high")
        # it LANDED, so applied and requested agree
        assert out["refinement"] == "high"
        assert out["refinement_requested"] == "high"

    def test_bad_refinement_rejected(self, tmp_path):
        _wire(_comp("Root", bodies=[BRepBody("Body1")]))
        res = mx.handler(format="obj", refinement="ultra", file_path=str(tmp_path / "p.obj"))
        assert res["isError"] is True and "refinement" in res["message"]

    def test_refinement_that_never_landed_is_published_null_not_as_the_request(self, tmp_path):
        # The request must never masquerade as the effect: the read-back did not equal the value set,
        # so 'refinement' is null - only the REQUEST is echoed, under its own key.
        des = _wire(_comp("Root", bodies=[BRepBody("Body1")]))
        out = self._no_refine_export(des, tmp_path)
        assert getattr(des.exportManager._calls[-1], "meshRefinement", None) is None
        assert out["refinement"] is None
        assert out["refinement_requested"] == "high"

    def test_a_refinement_that_did_not_land_says_so_on_the_wire(self, tmp_path):
        # The note states the fact the code OBSERVED - the read-back disagreed - and names the key
        # that carries it. The density the writer then used was never read, so nothing else is said.
        des = _wire(_comp("Root", bodies=[BRepBody("Body1")]))
        note = self._no_refine_export(des, tmp_path)["note"]
        assert "did NOT land" in note
        assert "'refinement' is null" in note

    def test_the_unlanded_refinement_note_attributes_no_cause(self, tmp_path):
        # WHY the set did not stick is not readable from the handler, so the note must not name a
        # format or a missing property as the reason - a guessed cause on the wire is a false claim.
        des = _wire(_comp("Root", bodies=[BRepBody("Body1")]))
        note = self._no_refine_export(des, tmp_path)["note"].lower()
        # the guessed CAUSES, not the bare format name: 'stl_units' legitimately names this export's
        # unit knob in the same note, and a substring ban on 'stl' would red on a correct payload.
        for guess in ("stl format", "stl options", "stl export options carry",
                      "carry no meshrefinement", "carries no meshrefinement",
                      "does not support", "default density"):
            assert guess not in note, guess

    def test_a_landed_refinement_adds_no_did_not_land_note(self, tmp_path):
        # the disclosure is conditional - a successful set must not warn about itself
        des = _wire(_comp("Root", bodies=[BRepBody("Body1")]))
        out = payload(mx.handler(format="obj", refinement="low",
                                         file_path=str(tmp_path / "p.obj")))
        assert out["refinement"] == "low"
        assert "did NOT land" not in out["note"]

    def test_a_refinement_the_options_already_read_is_still_reported_as_landed(self, tmp_path):
        # The density the read-back cannot attribute to THIS assignment - the options object reads
        # it before the set and the write is DROPPED. Reported as landed anyway, and that is
        # correct: measured, the value meshRefinement READS determines the file that gets written
        # (untouched and explicit-medium are byte-identical), so the caller does get 'medium'. This
        # is the knob where the unit's disclosure would be noise, and it must NOT appear.
        des = _wire(_comp("Root", bodies=[BRepBody("Body1")]))
        self._refine_options_holding(des, _refine_member("medium"))
        out = payload(mx.handler(format="obj", refinement="medium",
                                         file_path=str(tmp_path / "p.obj")))
        assert out["refinement"] == "medium"
        assert "refinement_verified" not in out
        assert "UNVERIFIED" not in out["note"] and "did NOT land" not in out["note"]

    def test_a_refinement_the_options_did_not_already_read_lands_as_requested(self, tmp_path):
        # The other side of the same pre-read: the property held a DIFFERENT density before the
        # set. Both sides report the same way for this knob - only the unit splits them.
        des = _wire(_comp("Root", bodies=[BRepBody("Body1")]))
        self._refine_options_holding(des, _refine_member("medium"), drop_write=False)
        out = payload(mx.handler(format="obj", refinement="high",
                                         file_path=str(tmp_path / "p.obj")))
        assert out["refinement"] == "high"
        assert des.exportManager._calls[-1].meshRefinement == _refine_member("high")
        assert "UNVERIFIED" not in out["note"]

    def test_a_dropped_refinement_write_is_caught_where_the_read_back_can_see_it(self, tmp_path):
        # The post-read is this knob's whole guard, so it has to bite where it can: the write is
        # dropped and the property holds a density OTHER than the one asked for, which reads back
        # as a miss. THE mechanism test for _applied_pair's after-comparison.
        des = _wire(_comp("Root", bodies=[BRepBody("Body1")]))
        self._refine_options_holding(des, _refine_member("medium"))
        out = payload(mx.handler(format="obj", refinement="high",
                                         file_path=str(tmp_path / "p.obj")))
        assert out["refinement"] is None
        assert "did NOT land" in out["note"]

    def test_the_defaulted_refinement_is_reported_as_landed_with_no_disclosure(self, tmp_path):
        # The most-travelled path: measured, the factory meshRefinement IS the member this tool
        # defaults to. It reports as landed with no caveat, because the read determines the file.
        # Read through the Choice's own default, so a change to it moves this test, not hides it.
        des = _wire(_comp("Root", bodies=[BRepBody("Body1")]))
        default = mx._EXPORT_REFINE.default
        self._refine_options_holding(des, _refine_member(default))
        out = payload(mx.handler(format="obj", file_path=str(tmp_path / "p.obj")))
        assert out["refinement"] == default
        assert "refinement_verified" not in out
        assert "UNVERIFIED" not in out["note"] and "did NOT land" not in out["note"]


class TestExportStlUnits:

    """The STL file's UNIT. Measured, an STL whose unitType is left untouched is written in the unit
    of the LAST EXPLICIT unitType assignment made anywhere in the Fusion session, carried across
    documents (measure_api stl-export-unittype-is-sticky-session-state), and no read of the options
    object names it - so an export that asked for no unit inherits an unrelated earlier export's,
    while mesh_insert's own default is mm. mesh_export therefore ASSIGNS mm when nothing is asked
    for - the unit mesh_insert defaults to - and publishes the unit the options object read back
    either way."""

    def _stl_dropping_the_unit(self, des, only_for=None):
        """Point the STL options factory at options that DROP the unitType write over a FOREIGN
        factory value (for the named occurrences only, when given): the set is accepted silently and
        the read-back still answers something else, so _apply_stl_units returns None."""
        names = set(only_for or ())

        def _stl_opt(geom, path):
            if names and getattr(geom, "name", "") not in names:
                return _record(des, "stl", geom, path)
            return _record(des, "stl", geom, path, drops=("unitType",),
                           seeded={"unitType": _FOREIGN_UNIT})
        des.exportManager.createSTLExportOptions = _stl_opt

    def _stl_options_holding(self, des, unit_member, drop_write=True):
        """STL options whose factory unitType ALREADY reads 'unit_member' - the live 'mm' shape."""
        return _options_holding(des, "unitType", unit_member, "createSTLExportOptions", "stl",
                                drop_write)

    def test_an_omitted_unit_is_not_advertised_as_a_schema_default(self, tmp_path):
        # The unit an omitted stl_units bakes in is NOT the value of a schema `default`: a client
        # that materialized such a default into the call would be REFUSED on every other format, so
        # the omitted unit is substituted by the handler and read back off the payload instead.
        assert "default" not in mx._EXPORT_UNITS.schema()
        _wire(_comp("Root", bodies=[BRepBody("Body1")]))
        res = mx.handler(format="3mf", file_path=str(tmp_path / "p.3mf"), stl_units="mm")
        assert res["isError"] is True and "stl_units" in res["message"]

    def test_omitting_the_unit_writes_mm_the_unit_mesh_insert_defaults_to(self, tmp_path):
        # mm, NOT whatever an untouched unitType would write (measured: the session's last explicit
        # unit, inherited across documents): the two tools' defaults have to name one unit, or an
        # export/import round trip that asks for nothing carries an unrelated export's.
        des = _wire(_comp("Root", bodies=[BRepBody("Body1")]))
        out = payload(mx.handler(format="stl", file_path=str(tmp_path / "p.stl")))
        assert des.exportManager._calls[-1].unitType is mx.adsk.fusion.DistanceUnits.MillimeterDistanceUnits
        assert out["options_applied"]["stl_units"] == "mm"
        assert out["options_requested"]["stl_units"] == "mm"

    def test_omitting_the_unit_on_the_live_factory_shape_still_lands_mm(self, tmp_path):
        # The measured live shape of the OMITTED case: unitType already reads MillimeterDistanceUnits
        # (it is 0, the value a fresh options object holds), so the pre-read half of _export's
        # applied_pair is what separates it from an explicit unit - the value lands, and
        # options_verified is false because the read-back could not have failed.
        des = _wire(_comp("Root", bodies=[BRepBody("Body1")]))
        self._stl_options_holding(des, mx.adsk.fusion.DistanceUnits.MillimeterDistanceUnits)
        out = payload(mx.handler(format="stl", file_path=str(tmp_path / "p.stl")))
        assert out["options_applied"]["stl_units"] == "mm"
        assert out["options_verified"]["stl_units"] is False
        assert "mesh_insert units='mm'" in out["note"]

    def test_a_requested_unit_overrides_the_default(self, tmp_path):
        # INCHES asked for explicitly - the discriminating twin of the omitted case above. A handler
        # that ignored 'stl_units' and always wrote its default would pass that test and fail this.
        des = _wire(_comp("Root", bodies=[BRepBody("Body1")]))
        out = payload(mx.handler(format="stl", stl_units="in",
                                         file_path=str(tmp_path / "p.stl")))
        assert des.exportManager._calls[-1].unitType is mx.adsk.fusion.DistanceUnits.InchDistanceUnits
        assert out["options_applied"]["stl_units"] == "in"
        assert out["options_requested"]["stl_units"] == "in"

    def test_the_note_names_the_unit_to_re_import_with(self, tmp_path):
        # the whole point of the row: the unit is on the wire, in the vocabulary mesh_insert takes.
        _wire(_comp("Root", bodies=[BRepBody("Body1")]))
        out = payload(mx.handler(format="stl", stl_units="mm",
                                         file_path=str(tmp_path / "p.stl")))
        assert "read back units 'mm'" in out["note"]
        assert "mesh_insert units='mm'" in out["note"]

    def test_bad_unit_rejected_by_the_choice(self, tmp_path):
        des = _wire(_comp("Root", bodies=[BRepBody("Body1")]))
        res = mx.handler(format="stl", stl_units="parsecs",
                                file_path=str(tmp_path / "p.stl"))
        assert res["isError"] is True and "stl_units" in res["message"]
        assert des.exportManager._calls == []               # nothing was exported

    def test_obj_reports_no_unit(self, tmp_path):
        # STL is the only format this tool bakes a unit into, so an OBJ export must not publish a
        # unit key or a unit sentence - a reported unit nothing was written from is a false claim.
        _wire(_comp("Root", bodies=[BRepBody("Body1")]))
        out = payload(mx.handler(format="obj", file_path=str(tmp_path / "p.obj")))
        assert "options_applied" not in out and "options_requested" not in out
        assert "options_verified" not in out
        assert "units" not in out["note"]

    def test_a_unit_asked_for_on_a_non_stl_format_is_refused_naming_it(self, tmp_path):
        # Dropping it silently would hand back a file whose unit nothing states - the defect this
        # input exists to close. The refusal names the value AND the format it was asked with.
        des = _wire(_comp("Root", bodies=[BRepBody("Body1")]))
        res = mx.handler(format="obj", stl_units="mm", file_path=str(tmp_path / "p.obj"))
        assert res["isError"] is True
        assert "'mm'" in res["message"] and "format=obj" in res["message"]
        assert des.exportManager._calls == []               # nothing was written

    def test_the_schema_declares_the_scope_the_handler_refuses_outside(self, tmp_path):
        # The description and the guard are one promise: a description that dropped 'format=stl
        # only' would advertise a knob every non-stl call is then refused for.
        assert "format=stl only" in mx._EXPORT_UNITS.schema()["description"]
        _wire(_comp("Root", bodies=[BRepBody("Body1")]))
        res = mx.handler(format="3mf", stl_units="mm", file_path=str(tmp_path / "p.3mf"))
        assert res["isError"] is True and "format=stl only" in res["message"]

    def test_an_omitted_unit_on_a_non_stl_format_is_not_refused(self, tmp_path):
        # the refusal keys on what the CALLER asked for, not on the Choice's default - an OBJ export
        # that never mentioned a unit must still run.
        _wire(_comp("Root", bodies=[BRepBody("Body1")]))
        out = payload(mx.handler(format="obj", file_path=str(tmp_path / "p.obj")))
        assert out["exported"] is True

    def test_a_unit_that_never_landed_is_published_null_not_as_the_request(self, tmp_path):
        # The request must never masquerade as the effect: the options object kept its own value, so
        # 'options_applied' is null and only 'options_requested' echoes what was asked for.
        des = _wire(_comp("Root", bodies=[BRepBody("Body1")]))
        self._stl_dropping_the_unit(des)
        out = payload(mx.handler(format="stl", file_path=str(tmp_path / "p.stl")))
        assert out["options_applied"]["stl_units"] is None
        assert out["options_requested"]["stl_units"] == "mm"
        assert out["options_verified"]["stl_units"] is False   # no landed value to be backed
        assert "did NOT land" in out["note"] and "'options_applied' is null" in out["note"]
        assert "read back units" not in out["note"]        # never both sentences

    def test_an_unlanded_unit_note_attributes_no_cause(self, tmp_path):
        # WHY the set did not stick, and which unit the writer then used, are not readable here - so
        # the sentence must not name inches, a format, or a missing property as the reason.
        des = _wire(_comp("Root", bodies=[BRepBody("Body1")]))
        self._stl_dropping_the_unit(des)
        note = payload(mx.handler(format="stl",
                                          file_path=str(tmp_path / "p.stl")))["note"].lower()
        for guess in ("inch", "factory default", "does not support", "carries no unittype"):
            assert guess not in note, guess

    def test_a_unit_the_options_already_read_is_published_unverified(self, tmp_path):
        # THE VACUOUS GUARD. The options object reads the requested unit BEFORE the set and the
        # write is DROPPED, so set-then-read-back answers exactly what it answers when the
        # assignment takes. The payload must publish that the claim is unbacked instead of
        # presenting the same equality it publishes for a unit the property did not already hold.
        des = _wire(_comp("Root", bodies=[BRepBody("Body1")]))
        self._stl_options_holding(des, mx.adsk.fusion.DistanceUnits.MillimeterDistanceUnits)
        out = payload(mx.handler(format="stl", stl_units="mm",
                                         file_path=str(tmp_path / "p.stl")))
        assert out["options_applied"]["stl_units"] == "mm"
        assert out["options_verified"]["stl_units"] is False
        assert "stl_units 'mm' UNVERIFIED for this file" in out["note"]
        assert "'options_verified' is false" in out["note"]
        assert "read back units" not in out["note"]     # the verified sentence must NOT appear

    def test_a_unit_the_options_did_not_already_read_is_published_verified(self, tmp_path):
        # The other side of the same read: the property held a DIFFERENT unit before the set, so
        # equality after it could have failed - and did not. This is a real verification, and it
        # keeps its verified sentence.
        des = _wire(_comp("Root", bodies=[BRepBody("Body1")]))
        self._stl_options_holding(des, mx.adsk.fusion.DistanceUnits.MillimeterDistanceUnits,
                                  drop_write=False)
        out = payload(mx.handler(format="stl", stl_units="in",
                                         file_path=str(tmp_path / "p.stl")))
        assert out["options_applied"]["stl_units"] == "in"
        assert out["options_verified"]["stl_units"] is True
        assert "read back units 'in'" in out["note"]
        assert "UNVERIFIED" not in out["note"]

    def test_a_dropped_write_is_caught_where_the_read_back_can_see_it(self, tmp_path):
        # Same dropped write as the unverified case, asked for a unit the property does NOT already
        # hold: there the read-back bites, so nothing landed and nothing is verified.
        des = _wire(_comp("Root", bodies=[BRepBody("Body1")]))
        self._stl_options_holding(des, mx.adsk.fusion.DistanceUnits.MillimeterDistanceUnits)
        out = payload(mx.handler(format="stl", stl_units="in",
                                         file_path=str(tmp_path / "p.stl")))
        assert out["options_applied"]["stl_units"] is None
        assert out["options_verified"]["stl_units"] is False
        assert "did NOT land" in out["note"]

    def test_the_unverified_unit_sentence_claims_nothing_about_the_file(self, tmp_path):
        # What the WRITER did with an options object that reads the unit either way is not readable
        # here, so the sentence must not say the file is in the unit - it names it as the request.
        des = _wire(_comp("Root", bodies=[BRepBody("Body1")]))
        self._stl_options_holding(des, mx.adsk.fusion.DistanceUnits.MillimeterDistanceUnits)
        note = payload(mx.handler(format="stl", stl_units="mm",
                                          file_path=str(tmp_path / "p.stl")))["note"].lower()
        for guess in ("the file is in", "written in mm", "the file was written", "the writer used",
                      "landed"):
            assert guess not in note, guess
        assert "re-import with mesh_insert units='mm'" in note

    def test_a_split_publishes_the_unverified_unit_per_file_and_counts_it(self, tmp_path):
        # per FILE, like options_applied: each file got its own options object, so each carries its
        # own evidence - and the note counts the files rather than claiming the unit for them.
        des = _wire(_comp("Root", occurrences=[make_occurrence("A:1"), make_occurrence("B:1")]))
        self._stl_options_holding(des, mx.adsk.fusion.DistanceUnits.MillimeterDistanceUnits)
        out = payload(mx.handler(format="stl", stl_units="mm", file_path=str(tmp_path),
                                         split_by_component=True))
        assert [f["options_applied"] for f in out["files"]] == [{"stl_units": "mm"},
                                                                {"stl_units": "mm"}]
        assert [f["options_verified"] for f in out["files"]] == [{"stl_units": False},
                                                                 {"stl_units": False}]
        assert "UNVERIFIED for 2 of 2 file(s)" in out["note"]
        assert "read back units" not in out["note"]

    def test_a_build_without_the_distance_units_member_publishes_null(self, tmp_path, monkeypatch):
        # No DistanceUnits member for the key -> nothing is assigned at all, and the payload says
        # null rather than reporting a unit the options object never held.
        des = _wire(_comp("Root", bodies=[BRepBody("Body1")]))
        monkeypatch.setattr(mx._export, "stl_unit_enum", lambda key: None)
        out = payload(mx.handler(format="stl", file_path=str(tmp_path / "p.stl")))
        assert not hasattr(des.exportManager._calls[-1], "unitType")
        assert out["options_applied"]["stl_units"] is None
        assert "did NOT land" in out["note"]

    def test_each_split_stl_file_carries_the_unit_that_landed_for_it(self, tmp_path):
        des = _wire(_comp("Root", occurrences=[make_occurrence("A:1"), make_occurrence("B:1")]))
        out = payload(mx.handler(format="stl", stl_units="cm", file_path=str(tmp_path),
                                         split_by_component=True))
        assert [f["options_applied"] for f in out["files"]] == [{"stl_units": "cm"},
                                                                {"stl_units": "cm"}]
        assert [f["options_verified"] for f in out["files"]] == [{"stl_units": True},
                                                                 {"stl_units": True}]
        assert out["options_requested"] == {"stl_units": "cm"}
        assert all(c.unitType is mx.adsk.fusion.DistanceUnits.CentimeterDistanceUnits
                   for c in des.exportManager._calls)
        assert "did NOT land" not in out["note"]
        assert "read back units 'cm'" in out["note"]       # the split note names it too

    def test_a_split_counts_only_the_files_the_unit_missed(self, tmp_path):
        # the MIXED case: one file's options kept the unit and one did not, so the per-file key
        # differs between them and the note names the count rather than all files.
        des = _wire(_comp("Root", occurrences=[make_occurrence("A:1"), make_occurrence("B:1")]))
        self._stl_dropping_the_unit(des, only_for=["B:1"])
        out = payload(mx.handler(format="stl", file_path=str(tmp_path),
                                         split_by_component=True))
        by_occ = {f["occurrence"]: f["options_applied"]["stl_units"] for f in out["files"]}
        assert by_occ == {"A:1": "mm", "B:1": None}
        assert {f["occurrence"]: f["options_verified"]["stl_units"] for f in out["files"]} == {
            "A:1": True, "B:1": False}
        assert "stl_units 'mm' did NOT land for 1 of 2 file(s)" in out["note"]
        # ...and the landed-unit sentence must NOT also appear. Both at once would tell a caller
        # its files re-import at 'in' while one of them has no confirmed unit at all.
        assert "read back units" not in out["note"]

    def test_a_split_of_a_non_stl_format_reports_no_unit_at_all(self, tmp_path):
        _wire(_comp("Root", occurrences=[make_occurrence("A:1")]))
        out = payload(mx.handler(format="3mf", file_path=str(tmp_path),
                                         split_by_component=True))
        assert "options_requested" not in out
        assert "options_applied" not in out["files"][0]


class TestExportSplitByComponent:

    def _split_dropping_refinement_for(self, des, drop_for=()):
        """Drop the meshRefinement write for the named occurrences only, so a split export can have
        some files land the refinement and some not."""
        drop = set(drop_for)

        def _stl_opt(geom, path):
            dropped = ("meshRefinement",) if getattr(geom, "name", "") in drop else ()
            return _record(des, "stl", geom, path, drops=dropped)
        des.exportManager.createSTLExportOptions = _stl_opt

    def test_one_file_per_occurrence(self, tmp_path):
        occs = [make_occurrence("Body:1"), make_occurrence("Wheels:1")]
        des = _wire(_comp("Root", occurrences=occs))
        out = payload(mx.handler(format="stl", file_path=str(tmp_path), split_by_component=True))
        assert out["split_by_component"] is True
        assert out["file_count"] == 2
        geoms = [c.geom.name for c in des.exportManager._calls]
        assert set(geoms) == {"Body:1", "Wheels:1"}

    def test_filenames_sanitized(self, tmp_path):
        _wire(_comp("Root", occurrences=[make_occurrence("Loader Arm:1")]))
        out = payload(mx.handler(format="3mf", file_path=str(tmp_path), split_by_component=True))
        assert out["files"][0]["file_path"].replace("\\", "/").endswith("/Loader_Arm.3mf")

    def test_duplicate_stems_disambiguated(self, tmp_path):
        _wire(_comp("Root", occurrences=[make_occurrence("Wheel:1"),
                                         make_occurrence("Wheel:2")]))
        out = payload(mx.handler(format="stl", file_path=str(tmp_path), split_by_component=True))
        paths = [f["file_path"] for f in out["files"]]
        assert len(set(paths)) == 2
        assert any(p.endswith("Wheel.stl") for p in paths) and any(p.endswith("Wheel_2.stl") for p in paths)

    def test_a_target_with_split_by_component_is_refused_naming_it(self, tmp_path):
        # The split walk exports EVERY top-level occurrence off its own census and never consults
        # 'target', so a dropped one hands back a directory of parts the caller did not ask for.
        # Refused by name, the shape the sibling design_export and this tool's stl_units guard set.
        des = _wire(_comp("Root", occurrences=[make_occurrence("Body:1"),
                                              make_occurrence("Wheels:1")]))
        res = mx.handler(format="stl", file_path=str(tmp_path), split_by_component=True,
                                target="Wheels:1")
        assert res["isError"] is True
        assert "'target' ('Wheels:1')" in res["message"]
        assert "split_by_component" in res["message"]
        assert des.exportManager._calls == []                 # nothing built, nothing written
        assert list(tmp_path.iterdir()) == []

    def test_an_omitted_target_still_splits(self, tmp_path):
        # THE BOUNDARY: "" is the default every split call carries, so a guard keyed on the input
        # existing rather than on what was PASSED would refuse the ordinary split outright.
        _wire(_comp("Root", occurrences=[make_occurrence("Body:1")]))
        out = payload(mx.handler(format="stl", file_path=str(tmp_path),
                                         split_by_component=True, target=""))
        assert out["file_count"] == 1

    def test_no_occurrences_errors(self, tmp_path):
        _wire(_comp("Root", occurrences=[]))
        res = mx.handler(format="stl", file_path=str(tmp_path), split_by_component=True)
        assert res["isError"] is True and "no top-level occurrences" in res["message"].lower()

    def test_an_unreadable_occurrence_collection_refuses_as_unread_not_as_empty(self, tmp_path):
        # An unread census is not an empty design: "no top-level occurrences" would state a fact
        # about the design nothing read, and a zero-file split would read as a clean export.
        comp = _comp("Root", occurrences=[make_occurrence("Body:1")])

        class _Blind(_NamedCollection):
            @property
            def count(self):
                raise RuntimeError("boom")

            def item(self, i):
                raise RuntimeError("boom")

        comp.occurrences = _Blind()
        _wire(comp)
        res = mx.handler(format="stl", file_path=str(tmp_path), split_by_component=True)
        assert res["isError"] is True
        assert "did not read" in res["message"]
        assert "no top-level occurrences" not in res["message"].lower()
        assert list(tmp_path.iterdir()) == []               # and nothing was written

    def test_split_reports_per_occurrence_failure_without_aborting(self, tmp_path):
        # one occurrence writes a file, one fails (execute raises) -> 1 file, the failure in 'failed'
        good = make_occurrence("Good:1")
        bad = make_occurrence("Bad:1")
        des = _wire(_comp("Root", occurrences=[good, bad]))
        real_execute = des.exportManager.execute
        def _selective(opts):
            if getattr(opts.geom, "name", "") == "Bad:1":
                raise RuntimeError("write blew up")
            return real_execute(opts)
        des.exportManager.execute = _selective
        out = payload(mx.handler(format="stl", file_path=str(tmp_path),
                                         split_by_component=True))
        assert out["file_count"] == 1
        assert out["exported"] is True            # at least one landed
        assert out["files"][0]["occurrence"] == "Good:1"
        assert "failed" in out
        assert out["failed"][0]["occurrence"] == "Bad:1"
        # a shortfall is disclosed as its own flag AND in the note - file_count alone would read
        # like a complete export of a one-part design
        assert out["partial"] is True
        assert "PARTIAL" in out["note"] and "1 of 2" in out["note"]

    def test_each_split_file_carries_the_refinement_that_landed_for_it(self, tmp_path):
        # The applied value _write_mesh_file read back is per FILE - the split payload publishes it
        # beside the one request, the same applied/requested pair the single-target export does.
        des = _wire(_comp("Root", occurrences=[make_occurrence("A:1"), make_occurrence("B:1")]))
        out = payload(mx.handler(format="stl", refinement="low", file_path=str(tmp_path),
                                         split_by_component=True))
        assert out["refinement_requested"] == "low"
        assert [f["refinement"] for f in out["files"]] == ["low", "low"]
        assert all(c.meshRefinement == _refine_member("low") for c in des.exportManager._calls)

    def test_a_split_reports_a_density_the_options_already_read_as_landed(self, tmp_path):
        # split mode carries the same per-knob interpretation as the single-target path: every
        # file's options object already read 'medium' and dropped the write, and every file still
        # reports it landed, with no caveat - the read determines the file.
        des = _wire(_comp("Root", occurrences=[make_occurrence("A:1"), make_occurrence("B:1")]))
        _options_holding(des, "meshRefinement", _refine_member("medium"),
                         "createSTLExportOptions", "stl")
        out = payload(mx.handler(format="stl", refinement="medium", file_path=str(tmp_path),
                                         split_by_component=True))
        assert [f["refinement"] for f in out["files"]] == ["medium", "medium"]
        assert all("refinement_verified" not in f for f in out["files"])
        assert "UNVERIFIED" not in out["note"] and "did NOT land" not in out["note"]

    def test_a_split_file_whose_refinement_was_dropped_is_null_not_the_request(self, tmp_path):
        # The request must never masquerade as the effect in split mode either: the read-back
        # disagreed for this file, so its 'refinement' is null and only 'refinement_requested' echoes.
        des = _wire(_comp("Root", occurrences=[make_occurrence("A:1")]))
        self._split_dropping_refinement_for(des, drop_for=["A:1"])
        out = payload(mx.handler(format="stl", refinement="high", file_path=str(tmp_path),
                                         split_by_component=True))
        assert out["files"][0]["refinement"] is None
        assert out["refinement_requested"] == "high"
        assert "did NOT land" in out["note"] and "'refinement' is null" in out["note"]

    def test_a_split_counts_only_the_files_the_refinement_missed(self, tmp_path):
        # The MIXED case is the boundary: one file landed the density and one did not, so the
        # per-file key differs between them and the note names the count, not "all files".
        des = _wire(_comp("Root", occurrences=[make_occurrence("A:1"), make_occurrence("B:1")]))
        self._split_dropping_refinement_for(des, drop_for=["B:1"])
        out = payload(mx.handler(format="stl", refinement="medium", file_path=str(tmp_path),
                                         split_by_component=True))
        by_occ = {f["occurrence"]: f["refinement"] for f in out["files"]}
        assert by_occ == {"A:1": "medium", "B:1": None}
        assert "1 of 2 file(s)" in out["note"]

    def test_a_split_where_every_refinement_landed_adds_no_did_not_land_note(self, tmp_path):
        # the disclosure is conditional - a fully successful split must not warn about itself
        _wire(_comp("Root", occurrences=[make_occurrence("A:1"), make_occurrence("B:1")]))
        out = payload(mx.handler(format="stl", refinement="low", file_path=str(tmp_path),
                                         split_by_component=True))
        assert "did NOT land" not in out["note"]

    def test_split_that_lands_nothing_is_an_error_carrying_the_reasons(self, tmp_path):
        # every occurrence fails -> ZERO deliverables, which is a FAILED export, not an ok payload
        # carrying exported:false. The per-occurrence reasons ride in the error text.
        des = _wire(_comp("Root", occurrences=[make_occurrence("A:1"), make_occurrence("B:1")]))
        des.exportManager.execute = lambda opts: (_ for _ in ()).throw(RuntimeError("nope"))
        res = mx.handler(format="stl", file_path=str(tmp_path), split_by_component=True)
        assert res["isError"] is True
        assert "wrote NO files" in res["message"]
        assert "A:1" in res["message"] and "B:1" in res["message"]
        assert "nope" in res["message"]


class TestExportNoteBudget:

    """Both export paths hand ok() a note COMPOSED at run time, so test_prose_budget's _note_sites
    sees a bare Name and measures nothing - these two drive the worst composition instead."""

    def _worst_options(self, des, no_unit=()):
        """STL options that drop the refinement write, and for the occurrences named in 'no_unit'
        drop the unit write off a foreign factory value (nothing lands) while the rest pre-read the
        requested unit and drop it (it lands unverifiably) - every clause rides at once."""
        drop = set(no_unit)

        def _opt(geom, path):
            unit = (_FOREIGN_UNIT if getattr(geom, "name", "") in drop
                    else mx.adsk.fusion.DistanceUnits.MillimeterDistanceUnits)
            return _record(des, "stl", geom, path, drops=("meshRefinement", "unitType"),
                           seeded={"unitType": unit})
        des.exportManager.createSTLExportOptions = _opt

    def test_the_worst_composed_single_target_note_fits_the_wire_budget(self, tmp_path):
        comp = _comp("Root", bodies=[BRepBody("LegBox")], occurrences=[make_occurrence("Child:1")])
        m = MeshBody("LegMesh")
        m.parentComponent = comp
        comp.meshBodies = _MeshBodies([m])
        des = _wire(comp, handles={"H": m})
        self._worst_options(des)
        out = payload(mx.handler(format="stl", stl_units="mm", target="H",
                                         file_path=str(tmp_path / "p.stl")))
        assert out["redirected_from_mesh"] is True and out["refinement"] is None
        assert out["options_verified"]["stl_units"] is False
        note = out["note"]
        assert "redirected" in note and "did NOT land" in note and "UNVERIFIED" in note
        assert len(note) <= 400, len(note)          # test_prose_budget.NOTE_BUDGET_CHARS

    def test_the_worst_composed_split_note_fits_the_wire_budget(self, tmp_path):
        des = _wire(_comp("Root", occurrences=[
            make_occurrence("A:1"), make_occurrence("NoUnit:1"), make_occurrence("Bad:1")]))
        self._worst_options(des, no_unit=["NoUnit:1"])
        real_execute = des.exportManager.execute

        def _selective(opts):
            if getattr(opts.geom, "name", "") == "Bad:1":
                raise RuntimeError("write blew up")
            return real_execute(opts)
        des.exportManager.execute = _selective
        out = payload(mx.handler(format="stl", stl_units="mm", file_path=str(tmp_path),
                                         split_by_component=True))
        assert out["partial"] is True
        note = out["note"]
        assert "PARTIAL" in note and "did NOT land" in note and "UNVERIFIED" in note
        assert len(note) <= 400, len(note)          # test_prose_budget.NOTE_BUDGET_CHARS
