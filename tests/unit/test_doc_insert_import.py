"""Unit tests for ``doc_insert_import.py``: format resolution from the extension, the refusals the
ImportManager API forces (DXF/SVG never go to a new document; f3z has no import path), the file
guard, per-format targeting (component / sketch / new document), and the landing read-back that
turns an import which created nothing into an error.
"""

import types

import pytest

import adsk.fusion
from conftest import (BRepBody, FakeApplication, FakeDocuments, FakeFusionDocument, FakeProducts,
                      FakeUserInterface, MakeComp, _NamedCollection, error_message, load_tool,
                      make_design, make_occurrence, payload)

mod = load_tool("doc_insert_import")

_ABSENT = object()


def _fake_manager(created=(), dxf_results=(), fail=None, options=_ABSENT, new_document=None,
                  on_import=None):
    """A stand-in adsk.core.ImportManager. ``created`` is what importToTarget2 returns (None models
    the null the API returns for a failed import); ``fail`` is an exception the import raises;
    ``on_import`` fires the side effect a real import has on the target. Returns (manager, calls),
    where calls records the arguments each method received."""
    calls = {"option_paths": [], "targets": [], "planar_entity": _ABSENT, "new_documents": 0}
    opts = types.SimpleNamespace(results=_NamedCollection(list(dxf_results)))

    def factory(path, planar_entity=_ABSENT):
        calls["option_paths"].append(path)
        calls["planar_entity"] = planar_entity
        return opts if options is _ABSENT else options

    def import_to_target2(_options, target):
        calls["targets"].append(target)
        if fail is not None:
            raise fail
        if on_import is not None:
            on_import()
        return None if created is None else _NamedCollection(list(created))

    def import_to_new_document(_options):
        calls["new_documents"] += 1
        # the real manager activates Design BEFORE it can fail, which is why on_import fires first
        if on_import is not None:
            on_import()
        if fail is not None:
            raise fail
        return new_document

    return types.SimpleNamespace(
        createSTEPImportOptions=factory,
        createIGESImportOptions=factory,
        createSATImportOptions=factory,
        createSMTImportOptions=factory,
        createFusionArchiveImportOptions=factory,
        createDXF2DImportOptions=factory,
        createSVGImportOptions=factory,
        importToTarget2=import_to_target2,
        importToNewDocument=import_to_new_document,
    ), calls


def _new_doc(design, name="Imported v1"):
    """A Document whose Design product is ``design`` - what importToNewDocument returns."""
    return FakeFusionDocument(name=name, products=FakeProducts(design=design))


class _RewrappedDocument(FakeFusionDocument):
    """One document as the live session hands it back: every read is a DISTINCT wrapper equal to
    its siblings. MEASURED - documents.item(0) twice gave `a == b` True and `a is b` False, so a
    walk matching on IDENTITY sees every document as new."""

    def __init__(self, identity, **kw):
        super().__init__(**kw)
        self.identity = identity

    def wrap(self):
        """A second wrapper for the same document - a new object, equal to this one, sharing its
        close record so a close through either is visible on both."""
        twin = _RewrappedDocument(self.identity, name=self._name, close_ok=self._close_ok)
        twin._closes = self._closes
        return twin

    def __eq__(self, other):
        return getattr(other, "identity", _ABSENT) == self.identity

    def __hash__(self):
        return hash(self.identity)


class _RewrappingDocuments(FakeDocuments):
    """app.documents answering the way the live one does: item(i) hands back a FRESH wrapper each
    read rather than the object it stored."""

    def item(self, i):
        doc = super().item(i)
        return doc.wrap() if isinstance(doc, _RewrappedDocument) else doc


@pytest.fixture
def cad(tmp_path):
    """Factory: a real file on disk with the given name."""
    def _make(name="part.step"):
        path = tmp_path / name
        path.write_text("cad", encoding="utf-8")
        return str(path)
    return _make


@pytest.fixture
def wire(monkeypatch):
    """Factory: wire a design, a stand-in ImportManager and the ``ui`` the workspace reads see into
    the module, patching BOTH design seams (the handler's own _common and _inputs._common)."""
    def _wire(design=None, ui=None, **manager_kwargs):
        design = make_design() if design is None else design
        manager, calls = _fake_manager(**manager_kwargs)
        monkeypatch.setattr(mod, "app", FakeApplication(import_manager=manager, user_interface=ui))
        monkeypatch.setattr(mod._common, "design", lambda: design)
        monkeypatch.setattr(mod._inputs._common, "design", lambda: design)
        return design, manager, calls
    return _wire


def _sketch(name="Sketch1", curves=()):
    return types.SimpleNamespace(name=name, sketchCurves=_NamedCollection(list(curves)))


_WORKSPACE_NAMES = {"CAMEnvironment": "Manufacture", "FusionSolidEnvironment": "Design"}


class _FakeUI(FakeUserInterface):
    """The shared UserInterface fake plus the two workspace reads it leaves to a test: the
    workspaces.itemById lookup an id-addressed switch goes through, and a read-capped
    activeWorkspace. ``activate`` is what Workspace.activate() answers: True switches, 'lies'
    answers true without switching, False declines, an Exception is raised. ``reads`` caps the
    activeWorkspace property reads that succeed - one workspace read costs two (id and name)."""

    def __init__(self, active="CAMEnvironment", activate=True, reads=None):
        super().__init__()
        self._active = active
        self._activate = activate
        self._reads = reads
        self.activated = []
        self.workspaces = types.SimpleNamespace(
            itemById=lambda ws_id: self._workspace(ws_id) if ws_id in _WORKSPACE_NAMES else None)

    @property
    def activeWorkspace(self):
        if self._reads is not None:
            if self._reads <= 0:
                raise RuntimeError("activeWorkspace is unavailable")
            self._reads -= 1
        return self._workspace(self._active)

    def _workspace(self, ws_id):
        return types.SimpleNamespace(id=ws_id, name=_WORKSPACE_NAMES[ws_id],
                                     activate=lambda: self._do(ws_id))

    def _do(self, ws_id):
        self.activated.append(ws_id)
        if isinstance(self._activate, Exception):
            raise self._activate
        if self._activate is True:
            self._active = ws_id
        return self._activate is not False

    def switch_to(self, ws_id):
        """The side effect importManager has on the UI - it activates the Design workspace."""
        self._active = ws_id


def _occurrence(name="Part:1", component=None):
    return make_occurrence(path=name,
                           component=component if component is not None else MakeComp(name))


class TestFormatResolution:
    @pytest.mark.parametrize("filename,expected", [
        ("part.step", "step"), ("part.STP", "step"),
        ("part.iges", "iges"), ("part.igs", "iges"),
        ("part.sat", "sat"), ("part.smt", "smt"), ("part.f3d", "f3d"),
    ])
    def test_extension_names_the_format(self, wire, cad, filename, expected):
        wire(created=[BRepBody("Imported")])
        out = payload(mod.handler(file_path=cad(filename)))
        assert out["format"] == expected

    def test_explicit_format_contradicting_the_extension_is_refused(self, wire, cad):
        _design, _mgr, calls = wire(created=[BRepBody("Imported")])
        msg = error_message(mod.handler(file_path=cad("part.step"), format="iges"))
        assert "iges" in msg and ".step" in msg
        assert calls["option_paths"] == []          # refused before any options were built

    def test_explicit_format_agreeing_with_the_extension_passes(self, wire, cad):
        wire(created=[BRepBody("Imported")])
        out = payload(mod.handler(file_path=cad("part.stp"), format="step"))
        assert out["format"] == "step"

    def test_unknown_format_value_is_refused_by_the_enum(self, wire, cad):
        wire(created=[BRepBody("Imported")])
        msg = error_message(mod.handler(file_path=cad("part.step"), format="parasolid"))
        assert "format" in msg and "parasolid" in msg

    def test_unsupported_extension_lists_the_supported_ones(self, wire, cad):
        wire()
        msg = error_message(mod.handler(file_path=cad("part.3dm")))
        assert ".3dm" in msg
        assert ".step" in msg and ".svg" in msg

    def test_f3z_is_refused_pointing_at_the_upload_path(self, wire, cad):
        _design, _mgr, calls = wire()
        msg = error_message(mod.handler(file_path=cad("assembly.f3z")))
        assert ".f3d" in msg and "data_upload_file" in msg
        assert calls["option_paths"] == []


class TestGuards:
    def test_missing_file_path(self, wire):
        wire()
        assert "file_path" in error_message(mod.handler(file_path=""))

    def test_file_not_on_disk_is_named(self, wire, tmp_path):
        wire()
        missing = str(tmp_path / "ghost.step")
        msg = error_message(mod.handler(file_path=missing))
        assert missing in msg

    def test_no_active_design_points_at_the_new_document_path(self, monkeypatch, wire, cad):
        wire()
        monkeypatch.setattr(mod._common, "design", lambda: None)
        msg = error_message(mod.handler(file_path=cad("part.step")))
        assert "No active design" in msg and "new_document" in msg

    def test_missing_import_manager_errors(self, monkeypatch, wire, cad):
        wire()
        monkeypatch.setattr(mod, "app", types.SimpleNamespace(importManager=None))
        assert "importManager" in error_message(mod.handler(file_path=cad("part.step")))

    @pytest.mark.parametrize("filename", ["plate.dxf", "logo.svg"])
    def test_sketch_formats_cannot_go_to_a_new_document(self, wire, cad, filename):
        _design, _mgr, calls = wire()
        msg = error_message(mod.handler(file_path=cad(filename), new_document=True))
        assert "new document" in msg
        assert "new_document=false" in msg
        assert calls["new_documents"] == 0 and calls["option_paths"] == []

    def test_options_factory_returning_nothing_errors(self, wire, cad):
        wire(options=None)
        msg = error_message(mod.handler(file_path=cad("part.step")))
        assert "createSTEPImportOptions" in msg


class TestSolidImport:
    def test_reports_the_objects_and_the_component_growth(self, wire, cad):
        design = make_design()
        room = design.rootComponent
        wire(design=design, created=[BRepBody("Imported")],
             on_import=lambda: room.bRepBodies._items.append(BRepBody("Imported")))
        out = payload(mod.handler(file_path=cad("part.step")))
        assert out["imported"] is True
        assert out["objects_created"] == 1
        assert out["bodies_added"] == 1
        assert out["created"] == [{"name": "Imported", "type": "BRepBody"}]
        assert "Root" in out["into"]

    def test_target_handed_to_the_api_is_the_component(self, wire, cad):
        design, _mgr, calls = wire(created=[BRepBody("Imported")])
        mod.handler(file_path=cad("part.step"))
        assert calls["targets"] == [design.rootComponent]

    def test_an_unreadable_created_object_drops_out_of_the_listing(self, wire, cad):
        # the created objects are counted and named, never addressed by position, so one that will
        # not read is simply absent - the readable ones keep their names and the count matches them
        design = make_design()
        room = design.rootComponent
        wire(design=design, created=[BRepBody("First"), None, BRepBody("Third")],
             on_import=lambda: room.bRepBodies._items.append(BRepBody("First")))
        out = payload(mod.handler(file_path=cad("part.step")))
        assert out["objects_created"] == 2
        assert out["created"] == [{"name": "First", "type": "BRepBody"},
                                  {"name": "Third", "type": "BRepBody"}]

    def test_nothing_landed_is_an_error(self, wire, cad):
        wire(created=[])
        msg = error_message(mod.handler(file_path=cad("part.step")))
        assert "nothing landed" in msg
        assert "no body and no occurrence" in msg

    def test_an_assembly_landing_as_occurrences_counts_as_landed(self, wire, cad):
        design = make_design()
        room = design.rootComponent
        wire(design=design, created=[],
             on_import=lambda: room.occurrences._items.append(_occurrence("Sub:1")))
        out = payload(mod.handler(file_path=cad("part.step")))
        assert out["occurrences_added"] == 1
        assert out["objects_created"] == 0

    def test_null_return_is_reported_as_a_failed_import(self, wire, cad):
        wire(created=None)
        msg = error_message(mod.handler(file_path=cad("part.step")))
        assert "null" in msg and "FAILED" in msg

    def test_a_raising_import_is_an_error_not_a_false_ok(self, wire, cad):
        wire(fail=RuntimeError("bad STEP entity"))
        msg = error_message(mod.handler(file_path=cad("part.step")))
        assert "importToTarget2 raised" in msg and "bad STEP entity" in msg

    def test_into_component_resolves_through_the_occurrence_kind(self, wire, cad):
        sub = MakeComp("Bracket")
        occ = _occurrence("Bracket:1", component=sub)
        design = make_design(occurrences=[occ])
        _design, _mgr, calls = wire(design=design, created=[BRepBody("Imported")])
        out = payload(mod.handler(file_path=cad("part.step"), into_component="Bracket:1"))
        assert calls["targets"] == [sub]
        assert "Bracket" in out["into"]

    def test_unknown_into_component_is_refused_before_importing(self, wire, cad):
        design = make_design(occurrences=[_occurrence("Bracket:1")])
        _design, _mgr, calls = wire(design=design, created=[BRepBody("Imported")])
        msg = error_message(mod.handler(file_path=cad("part.step"), into_component="Ghost"))
        assert "Ghost" in msg
        assert calls["targets"] == []

    def test_an_occurrence_with_no_component_is_refused_before_importing(self, wire, cad):
        occ = make_occurrence(path="Bracket:1", component=None)
        design = make_design(occurrences=[occ])
        _design, _mgr, calls = wire(design=design, created=[BRepBody("Imported")])
        msg = error_message(mod.handler(file_path=cad("part.step"), into_component="Bracket:1"))
        assert "Occurrence 'Bracket:1' has no component to import into" in msg
        assert calls["targets"] == []

    def test_a_design_with_no_component_to_import_into_is_refused(self, wire, cad, monkeypatch):
        _design, _mgr, calls = wire(created=[BRepBody("Imported")])
        monkeypatch.setattr(mod._common, "target_component", lambda design: None)
        msg = error_message(mod.handler(file_path=cad("part.step")))
        assert "exposes no component to import into" in msg
        assert calls["targets"] == []


class TestTheBuildLacksTheFactory:
    """ImportManager gains factories over Fusion versions - a build without the one this format
    needs is named, never AttributeError'd out of the handler."""

    def test_a_missing_options_factory_is_named_with_the_format(self, wire, cad):
        _design, mgr, calls = wire(created=[BRepBody("Imported")])
        del mgr.createSTEPImportOptions
        msg = error_message(mod.handler(file_path=cad("part.step")))
        assert "no ImportManager.createSTEPImportOptions" in msg and "STEP import is unavailable" in msg
        assert calls["targets"] == []

    def test_a_missing_factory_stops_a_new_document_import_too(self, wire, cad):
        _design, mgr, calls = wire()
        del mgr.createIGESImportOptions
        msg = error_message(mod.handler(file_path=cad("part.iges"), new_document=True))
        assert "no ImportManager.createIGESImportOptions" in msg
        assert calls["new_documents"] == 0


def _origin_planes():
    """The root's three origin planes - a DXF import resolves its target through the xY one."""
    return tuple(types.SimpleNamespace(name=n) for n in ("XY", "XZ", "YZ"))


class TestDxfImport:
    def _design_with_plane(self, sketches=()):
        return make_design(comp=MakeComp("Root", sketches=sketches,
                                         origin_planes=_origin_planes()))

    def test_sketches_land_and_are_reported(self, wire, cad):
        design = self._design_with_plane()
        room = design.rootComponent
        wire(design=design, created=[_sketch("Outline")],
             on_import=lambda: room.sketches._items.append(_sketch("Outline")))
        out = payload(mod.handler(file_path=cad("plate.dxf")))
        assert out["format"] == "dxf"
        assert out["sketches_added"] == 1
        assert [c["name"] for c in out["created"]] == ["Outline"]

    def test_the_resolved_plane_is_handed_to_the_options_factory(self, wire, cad):
        design = self._design_with_plane()
        _design, _mgr, calls = wire(design=design, created=[_sketch("Outline")])
        mod.handler(file_path=cad("plate.dxf"))
        assert calls["planar_entity"] is design.rootComponent.xYConstructionPlane

    def test_results_on_the_options_object_is_the_fallback(self, wire, cad):
        design = self._design_with_plane()
        room = design.rootComponent
        wire(design=design, created=[], dxf_results=[_sketch("Layer0")],
             on_import=lambda: room.sketches._items.append(_sketch("Layer0")))
        out = payload(mod.handler(file_path=cad("plate.dxf")))
        assert [c["name"] for c in out["created"]] == ["Layer0"]

    def test_no_sketch_landed_is_an_error(self, wire, cad):
        wire(design=self._design_with_plane(), created=[])
        msg = error_message(mod.handler(file_path=cad("plate.dxf")))
        assert "no sketch landed" in msg
        assert "3D geometry" in msg

    def test_unresolvable_plane_is_refused_before_importing(self, wire, cad):
        design = self._design_with_plane()
        _design, _mgr, calls = wire(design=design, created=[_sketch("Outline")])
        msg = error_message(mod.handler(file_path=cad("plate.dxf"), plane="NoSuchPlane"))
        assert "NoSuchPlane" in msg
        assert calls["targets"] == []

    def test_options_the_factory_would_not_build_are_reported_with_both_causes(self, wire, cad):
        # createDXF2DImportOptions answering nothing means the FILE or the PLANE was rejected -
        # the refusal names both rather than guessing which.
        _design, _mgr, calls = wire(design=self._design_with_plane(), options=None)
        msg = error_message(mod.handler(file_path=cad("plate.dxf")))
        assert "createDXF2DImportOptions returned nothing" in msg
        assert "planar face" in msg
        assert calls["targets"] == []

    def test_a_raising_dxf_import_is_an_error_not_a_false_ok(self, wire, cad):
        wire(design=self._design_with_plane(), fail=RuntimeError("layer table is corrupt"))
        msg = error_message(mod.handler(file_path=cad("plate.dxf")))
        assert "importToTarget2 raised" in msg and "layer table is corrupt" in msg

    def test_a_design_with_no_component_refuses_the_dxf_import(self, wire, cad, monkeypatch):
        _design, _mgr, calls = wire(design=self._design_with_plane(), created=[_sketch("Outline")])
        monkeypatch.setattr(mod._common, "target_component", lambda design: None)
        msg = error_message(mod.handler(file_path=cad("plate.dxf")))
        assert "exposes no component to import into" in msg
        assert calls["targets"] == []


class TestSvgImport:
    def test_curves_land_in_the_named_sketch(self, wire, cad):
        target = _sketch("Logo")
        design = make_design(sketches=[target])
        wire(design=design, created=[types.SimpleNamespace(name="Curve")],
             on_import=lambda: target.sketchCurves._items.append(object()))
        out = payload(mod.handler(file_path=cad("logo.svg"), sketch="Logo"))
        assert out["curves_added"] == 1
        assert out["into"] == "sketch 'Logo'"

    def test_the_sketch_is_the_import_target(self, wire, cad):
        target = _sketch("Logo")
        design = make_design(sketches=[target])
        _design, _mgr, calls = wire(design=design, created=[types.SimpleNamespace(name="Curve")])
        mod.handler(file_path=cad("logo.svg"), sketch="Logo")
        assert calls["targets"] == [target]

    def test_no_sketch_in_the_design_names_sketch_create(self, wire, cad):
        wire(design=make_design())
        msg = error_message(mod.handler(file_path=cad("logo.svg")))
        assert "sketch_create" in msg

    def test_unknown_sketch_name_lists_the_available_ones(self, wire, cad):
        design = make_design(sketches=[_sketch("Logo")])
        wire(design=design)
        msg = error_message(mod.handler(file_path=cad("logo.svg"), sketch="Ghost"))
        assert "Ghost" in msg and "Logo" in msg

    def test_a_padded_sketch_name_is_reported_stripped(self, wire, cad):
        # the walk searches the STRIPPED name, so the miss must name that one - quoting the padded
        # input sends the caller looking for a sketch whose name carries the spaces it typed.
        wire(design=make_design(sketches=[_sketch("Logo")]))
        msg = error_message(mod.handler(file_path=cad("logo.svg"), sketch="  Ghost  "))
        assert "No sketch named 'Ghost'" in msg
        assert "'  Ghost  '" not in msg

    def test_a_blank_sketch_name_with_no_sketch_never_quotes_none(self, wire, cad):
        # a blank name leaves the requested name None, so the named-miss wording would print
        # "No sketch named 'None'" - a sketch nobody asked for. The blank branch words its own.
        wire(design=make_design())
        msg = error_message(mod.handler(file_path=cad("logo.svg"), sketch=""))
        assert msg == ("No sketch to import the SVG into. SVG curves land in an EXISTING sketch - "
                       "make one with sketch_create, then name it in 'sketch'.")
        assert "'None'" not in msg

    def test_a_whitespace_only_sketch_name_falls_back_to_the_most_recent_sketch(self, wire, cad):
        # ' ' strips to blank, which is the most-recent-sketch request - not a search for a sketch
        # named with a space.
        recent = _sketch("Last")
        wire(design=make_design(sketches=[_sketch("First"), recent]),
             created=[types.SimpleNamespace(name="Curve")],
             on_import=lambda: recent.sketchCurves._items.append(object()))
        out = payload(mod.handler(file_path=cad("logo.svg"), sketch=" "))
        assert out["into"] == "sketch 'Last'"

    def test_a_shared_sketch_name_is_refused_with_its_owners(self, wire, cad, monkeypatch):
        # Two components can each hold a "Logo". The refusal names them and nothing is imported;
        # calling it "No sketch named 'Logo'" states the opposite of what the walk read.
        design = make_design(sketches=[_sketch("Logo")])
        _design, _mgr, calls = wire(design=design)
        refusal = "2 sketches are named 'Logo' ('Logo' in Root, 'Logo' in Frame)"
        monkeypatch.setattr(mod._sketch_detail, "scoped_or_recent_sketch",
                            lambda d, n, c, input_name="component": (None, n, refusal))
        msg = error_message(mod.handler(file_path=cad("logo.svg"), sketch="Logo"))
        assert msg == refusal and "No sketch named" not in msg
        assert calls["targets"] == []          # nothing was imported

    def test_the_svg_scope_is_declared_on_the_wire_beside_into_component(self):
        # the schema is strict, so a handler parameter no property declares is unreachable. Both
        # scopes are declared, and each keeps its own meaning.
        sd = load_tool("_sketch_detail")
        props = mod.tool.input_schema["properties"]
        assert props["sketch_component"] == sd.component_scope("sketch_component",
                                                               narrows="sketch")[1]
        # 'into_component' is a second, differently-scoped component input, so this one names the
        # reference it narrows instead of the family's generic wording
        assert "'sketch'" in props["sketch_component"]["description"]
        assert props["sketch_component"] != props["into_component"]

    def test_the_svg_scope_is_its_own_input_not_into_component(self, wire, cad, monkeypatch):
        # 'into_component' names where a DXF's new sketches or a solid's occurrence LAND; an SVG
        # goes into a sketch that already exists, so its scope is 'sketch_component'.
        wire(design=make_design(sketches=[_sketch("Logo")]))
        seen = {}

        def _scoped(d, n, c, input_name="component"):
            seen.update(component=c, input_name=input_name)
            return None, n, "refused"

        monkeypatch.setattr(mod._sketch_detail, "scoped_or_recent_sketch", _scoped)
        mod.handler(file_path=cad("logo.svg"), sketch="Logo", into_component="Carrier",
                    sketch_component="Frame")
        assert seen == {"component": "Frame", "input_name": "sketch_component"}

    def test_a_sketch_that_gained_no_curve_is_an_error(self, wire, cad):
        design = make_design(sketches=[_sketch("Logo")])
        wire(design=design, created=[])
        msg = error_message(mod.handler(file_path=cad("logo.svg"), sketch="Logo"))
        assert "gained no" in msg and "Logo" in msg

    def test_options_the_factory_would_not_build_are_reported(self, wire, cad):
        design = make_design(sketches=[_sketch("Logo")])
        _design, _mgr, calls = wire(design=design, options=None)
        msg = error_message(mod.handler(file_path=cad("logo.svg"), sketch="Logo"))
        assert "createSVGImportOptions returned nothing" in msg
        assert calls["targets"] == []

    def test_a_raising_svg_import_is_an_error_not_a_false_ok(self, wire, cad):
        target = _sketch("Logo")
        design = make_design(sketches=[target])
        wire(design=design, fail=RuntimeError("path data is malformed"))
        msg = error_message(mod.handler(file_path=cad("logo.svg"), sketch="Logo"))
        assert "importToTarget2 raised" in msg and "path data is malformed" in msg


class TestNewDocument:
    def test_bodies_are_read_back_off_the_new_document(self, wire, cad):
        imported = make_design(bodies=["Imported"])
        wire(new_document=_new_doc(imported, name="Gearbox v1"))
        out = payload(mod.handler(file_path=cad("part.step"), new_document=True))
        assert out["bodies"] == 1
        assert out["into"] == "new document 'Gearbox v1'"

    def test_null_return_is_an_error(self, wire, cad):
        wire(new_document=None)
        msg = error_message(mod.handler(file_path=cad("part.step"), new_document=True))
        assert "null" in msg and "FAILED" in msg

    def test_a_document_with_no_design_product_is_an_error(self, wire, cad):
        wire(new_document=_new_doc(None))
        msg = error_message(mod.handler(file_path=cad("part.step"), new_document=True))
        assert "no Design product" in msg

    def test_an_empty_new_document_is_an_error(self, wire, cad):
        # the document is HELD on this path, so it is closed here rather than described to the
        # caller - the address is only quoted when the close itself refuses.
        empty = _new_doc(make_design())
        wire(new_document=empty)
        msg = error_message(mod.handler(file_path=cad("part.step"), new_document=True))
        assert "no body and no occurrence" in msg
        assert "was closed again" in msg
        assert empty._closes == [False]

    def test_a_raising_import_is_an_error(self, wire, cad):
        wire(fail=RuntimeError("unreadable archive"))
        msg = error_message(mod.handler(file_path=cad("part.f3d"), new_document=True))
        assert "importToNewDocument raised" in msg


class TestTheDocumentAFailedNewDocumentImportLeavesOpen:
    """A FAILED importToNewDocument leaves an empty untitled document open and ACTIVE while the
    error names none of it. The session list is read on both sides of the call, so the ONE document
    that appeared is closed again - or its open:N address is named when the close refuses."""

    def _leaking_import(self, wire, cad, close_ok=True, **manager_kwargs):
        """Wire an import whose manager mints a document into the session list, then fails.
        Returns (the pre-existing document, the minted one, the error message)."""
        home = FakeFusionDocument(name="Home")
        leaked = FakeFusionDocument(name="Untitled", close_ok=close_ok)
        docs = FakeDocuments([home])
        wire(on_import=lambda: docs._items.append(leaked), **manager_kwargs)
        mod.app.documents = docs
        return home, leaked, error_message(mod.handler(file_path=cad("part.step"),
                                                       new_document=True))

    def test_a_raise_that_left_a_document_open_closes_it_and_says_so(self, wire, cad):
        home, leaked, msg = self._leaking_import(
            wire, cad, fail=RuntimeError("2 : InternalValidationError : isSuccessfullyOpened"))
        assert "importToNewDocument raised" in msg          # the import failure still leads
        assert "empty document 'Untitled' this call opened was closed again" in msg
        assert leaked._closes == [False]                    # discarded, never saved
        assert home._closes == []                           # the document already open is untouched

    def test_a_leaked_document_that_will_not_close_is_named_by_its_open_index(self, wire, cad):
        _home, leaked, msg = self._leaking_import(
            wire, cad, close_ok=False, fail=RuntimeError("unreadable archive"))
        assert "open:1" in msg and "doc_close(name='open:1')" in msg
        assert leaked._closes == [False]                    # the close was attempted, and refused

    def test_a_failure_that_opened_nothing_claims_no_document(self, wire, cad):
        # the session list is unchanged, so there is no newcomer to close - and closing whatever
        # else is open would take a document the caller owns.
        home = FakeFusionDocument(name="Home")
        docs = FakeDocuments([home])
        wire(fail=RuntimeError("bad STEP entity"))
        mod.app.documents = docs
        msg = error_message(mod.handler(file_path=cad("part.step"), new_document=True))
        assert "importToNewDocument raised" in msg
        assert "closed again" not in msg and "open:" not in msg
        assert home._closes == []

    def test_the_newcomer_is_told_apart_by_equality_not_identity(self, wire, cad):
        # the session re-wraps a Document per read, so the walk before and the walk after share NO
        # objects. Matching on identity would call every document new and close none of them.
        home = _RewrappedDocument("home", name="Home")
        leaked = _RewrappedDocument("leaked", name="Untitled")
        docs = _RewrappingDocuments([home])
        wire(on_import=lambda: docs._items.append(leaked),
             fail=RuntimeError("2 : InternalValidationError : isSuccessfullyOpened"))
        mod.app.documents = docs
        msg = error_message(mod.handler(file_path=cad("part.step"), new_document=True))
        assert "empty document 'Untitled' this call opened was closed again" in msg
        assert leaked._closes == [False]
        assert home._closes == []

    def test_two_new_documents_leave_the_choice_unmade(self, wire, cad):
        # WHICH one this call opened is undecidable with two newcomers, and closing either could
        # take a document another session opened - so nothing is closed and nothing is claimed.
        home = FakeFusionDocument(name="Home")
        first = FakeFusionDocument(name="Untitled")
        second = FakeFusionDocument(name="Untitled")
        docs = FakeDocuments([home])
        wire(on_import=lambda: docs._items.extend([first, second]),
             fail=RuntimeError("unreadable archive"))
        mod.app.documents = docs
        msg = error_message(mod.handler(file_path=cad("part.step"), new_document=True))
        assert "importToNewDocument raised" in msg
        assert "closed again" not in msg and "open:" not in msg
        assert first._closes == [] and second._closes == [] and home._closes == []

    def test_a_new_document_that_landed_nothing_is_closed_rather_than_left_in_front(self, wire, cad):
        home = FakeFusionDocument(name="Home")
        empty = _new_doc(make_design(), name="Empty v1")
        docs = FakeDocuments([home])
        wire(new_document=empty, on_import=lambda: docs._items.append(empty))
        mod.app.documents = docs
        msg = error_message(mod.handler(file_path=cad("part.step"), new_document=True))
        assert "landed nothing" in msg
        assert "was closed again" in msg and "Discard it with doc_close" not in msg
        assert empty._closes == [False]


class TestTheWorkspaceTheImportSwitchedAway:
    """importManager activates the Design workspace, so an import run with Manufacture active
    leaves the UI in Design. The workspace read BEFORE the import is re-activated by its id and
    read back; a restore that does not read back is disclosed, never claimed."""

    def _solid_import_flipping_the_workspace(self, wire, ui):
        design = make_design()
        room = design.rootComponent

        def _side_effect():
            room.bRepBodies._items.append(BRepBody("Imported"))
            ui.switch_to("FusionSolidEnvironment")

        return wire(design=design, ui=ui, created=[BRepBody("Imported")], on_import=_side_effect)

    def test_a_failed_import_puts_the_workspace_back_too(self, wire, cad):
        # MEASURED: importToTarget2 activates Design and THEN raises, so an error path that just
        # returns leaves a Manufacture session in Design with nothing said. The restore has to ride
        # the error, not only the success.
        ui = _FakeUI()
        design = make_design()

        def _fail_after_switching():
            ui.switch_to("FusionSolidEnvironment")
            raise RuntimeError("2 : InternalValidationError : pResult")

        wire(design=design, ui=ui, created=[], on_import=_fail_after_switching)
        msg = error_message(mod.handler(file_path=cad("part.step")))
        assert "InternalValidationError" in msg
        assert ui.activated == ["CAMEnvironment"]          # put back
        assert ui.activeWorkspace.name == "Manufacture"
        assert "view_switch_workspace" not in msg          # a clean restore says nothing extra

    def test_a_failed_import_whose_restore_fails_names_it_in_the_error(self, wire, cad):
        ui = _FakeUI(activate="lies")
        design = make_design()

        def _fail_after_switching():
            ui.switch_to("FusionSolidEnvironment")
            raise RuntimeError("2 : InternalValidationError : pResult")

        wire(design=design, ui=ui, created=[], on_import=_fail_after_switching)
        msg = error_message(mod.handler(file_path=cad("part.step")))
        assert "InternalValidationError" in msg            # the import failure still leads
        assert "left 'Design' active" in msg
        assert "view_switch_workspace" in msg

    # The remaining post-import error paths, one test each: every one of them returns AFTER
    # importToTarget2 has already activated Design, so each has to put the workspace back too.

    def _switching(self, ui, then_raise=None):
        def _side_effect():
            ui.switch_to("FusionSolidEnvironment")
            if then_raise is not None:
                raise then_raise
        return _side_effect

    def test_a_solid_import_that_landed_nothing_puts_the_workspace_back(self, wire, cad):
        ui = _FakeUI()
        wire(design=make_design(), ui=ui, created=[], on_import=self._switching(ui))
        msg = error_message(mod.handler(file_path=cad("part.step")))
        assert "nothing landed" in msg
        assert ui.activated == ["CAMEnvironment"]
        assert ui.activeWorkspace.name == "Manufacture"

    def test_a_raising_dxf_import_puts_the_workspace_back(self, wire, cad):
        ui = _FakeUI()
        design = make_design(comp=MakeComp("Root", origin_planes=_origin_planes()))
        wire(design=design, ui=ui, created=[],
             on_import=self._switching(ui, RuntimeError("dxf blew up")))
        msg = error_message(mod.handler(file_path=cad("plate.dxf")))
        assert "importToTarget2 raised" in msg
        assert ui.activated == ["CAMEnvironment"]

    def test_a_dxf_import_that_landed_no_sketch_puts_the_workspace_back(self, wire, cad):
        ui = _FakeUI()
        design = make_design(comp=MakeComp("Root", origin_planes=_origin_planes()))
        wire(design=design, ui=ui, created=[], on_import=self._switching(ui))
        msg = error_message(mod.handler(file_path=cad("plate.dxf")))
        assert "no sketch landed" in msg
        assert ui.activated == ["CAMEnvironment"]

    def test_a_raising_svg_import_puts_the_workspace_back(self, wire, cad):
        ui = _FakeUI()
        design = make_design(sketches=[_sketch("Logo")])
        wire(design=design, ui=ui, created=[],
             on_import=self._switching(ui, RuntimeError("path data is malformed")))
        msg = error_message(mod.handler(file_path=cad("logo.svg"), sketch="Logo"))
        assert "importToTarget2 raised" in msg
        assert ui.activated == ["CAMEnvironment"]

    def test_an_svg_import_that_gained_no_curve_puts_the_workspace_back(self, wire, cad):
        ui = _FakeUI()
        design = make_design(sketches=[_sketch("Logo")])
        wire(design=design, ui=ui, created=[], on_import=self._switching(ui))
        msg = error_message(mod.handler(file_path=cad("logo.svg"), sketch="Logo"))
        assert "gained no" in msg
        assert ui.activated == ["CAMEnvironment"]

    # new_document=true: whether importToNewDocument switches the workspace is NOT MEASURED, so its
    # four post-import error paths restore defensively - these drive the fake switching to prove the
    # restore RUNS. A SUCCESSFUL one hands over a new document and keeps Design.

    def test_a_raising_new_document_import_puts_the_workspace_back(self, wire, cad):
        ui = _FakeUI()
        wire(ui=ui, on_import=self._switching(ui),
             fail=RuntimeError("2 : InternalValidationError : isSuccessfullyOpened"))
        msg = error_message(mod.handler(file_path=cad("part.step"), new_document=True))
        assert "importToNewDocument raised" in msg
        assert ui.activated == ["CAMEnvironment"]
        assert ui.activeWorkspace.name == "Manufacture"

    def test_a_null_new_document_import_puts_the_workspace_back(self, wire, cad):
        ui = _FakeUI()
        wire(ui=ui, on_import=self._switching(ui), new_document=None)
        msg = error_message(mod.handler(file_path=cad("part.step"), new_document=True))
        assert "returned null" in msg
        assert ui.activated == ["CAMEnvironment"]

    def test_a_new_document_with_no_design_product_puts_the_workspace_back(self, wire, cad):
        ui = _FakeUI()
        wire(ui=ui, on_import=self._switching(ui), new_document=_new_doc(None, name="Empty v1"))
        msg = error_message(mod.handler(file_path=cad("part.step"), new_document=True))
        assert "no Design product" in msg
        assert ui.activated == ["CAMEnvironment"]

    def test_a_new_document_that_landed_nothing_puts_the_workspace_back(self, wire, cad):
        ui = _FakeUI()
        wire(ui=ui, on_import=self._switching(ui),
             new_document=_new_doc(make_design(), name="Empty v1"))
        msg = error_message(mod.handler(file_path=cad("part.step"), new_document=True))
        assert "landed nothing" in msg
        assert ui.activated == ["CAMEnvironment"]

    def test_a_successful_new_document_import_keeps_design_active(self, wire, cad):
        # the caller ASKED for a new document and Design is where it belongs - the restore is for
        # the error paths, which hand over nothing.
        ui = _FakeUI()
        wire(ui=ui, on_import=self._switching(ui),
             new_document=_new_doc(make_design(bodies=["Imported"]), name="Gearbox v1"))
        payload(mod.handler(file_path=cad("part.step"), new_document=True))
        assert ui.activated == []
        assert ui.activeWorkspace.name == "Design"

    def test_a_restored_workspace_is_published_by_name(self, wire, cad):
        ui = _FakeUI()
        self._solid_import_flipping_the_workspace(wire, ui)
        out = payload(mod.handler(file_path=cad("part.step")))
        assert out["workspace_restored"] == "Manufacture"
        assert "workspace_changed" not in out
        assert ui.activated == ["CAMEnvironment"]
        assert ui.activeWorkspace.name == "Manufacture"
        assert "view_switch_workspace" not in out["note"]

    def test_an_activate_that_lies_is_a_changed_workspace_not_a_restore(self, wire, cad):
        ui = _FakeUI(activate="lies")
        self._solid_import_flipping_the_workspace(wire, ui)
        out = payload(mod.handler(file_path=cad("part.step")))
        assert out["workspace_changed"] == {"from": "Manufacture", "to": "Design"}
        assert "workspace_restored" not in out
        assert "view_switch_workspace" in out["note"]
        assert out["imported"] is True                  # the import itself still succeeded

    @pytest.mark.parametrize("activate", [False, RuntimeError("no document to switch in")])
    def test_a_declined_or_raising_activate_is_reported_the_same_way(self, wire, cad, activate):
        ui = _FakeUI(activate=activate)
        self._solid_import_flipping_the_workspace(wire, ui)
        out = payload(mod.handler(file_path=cad("part.step")))
        assert out["workspace_changed"] == {"from": "Manufacture", "to": "Design"}
        assert "workspace_restored" not in out

    def test_a_restore_that_reads_back_nothing_is_disclosed_not_claimed(self, wire, cad):
        # the before and after reads answer, the one AFTER the re-activate does not: an unverified
        # restore is the workspace the import changed, never a restored one.
        ui = _FakeUI(reads=4)
        self._solid_import_flipping_the_workspace(wire, ui)
        out = payload(mod.handler(file_path=cad("part.step")))
        assert ui.activated == ["CAMEnvironment"]
        assert "workspace_restored" not in out
        assert out["workspace_changed"] == {"from": "Manufacture", "to": "Design"}

    def test_a_workspace_the_import_left_alone_publishes_neither_key(self, wire, cad):
        ui = _FakeUI()
        design = make_design()
        room = design.rootComponent
        wire(design=design, ui=ui, created=[BRepBody("Imported")],
             on_import=lambda: room.bRepBodies._items.append(BRepBody("Imported")))
        out = payload(mod.handler(file_path=cad("part.step")))
        assert "workspace_restored" not in out and "workspace_changed" not in out
        assert ui.activated == []              # an unchanged workspace is never re-activated

    def test_a_ui_that_will_not_read_claims_nothing(self, wire, cad):
        # with no userInterface the before-read is None, so the import publishes no workspace
        # verdict rather than a confident "unchanged".
        design = make_design()
        room = design.rootComponent
        wire(design=design, created=[BRepBody("Imported")],
             on_import=lambda: room.bRepBodies._items.append(BRepBody("Imported")))
        out = payload(mod.handler(file_path=cad("part.step")))
        assert "workspace_restored" not in out and "workspace_changed" not in out

    def test_an_after_read_that_will_not_read_publishes_no_verdict(self, wire, cad):
        # the workspace read BEFORE the import answers and the one AFTER raises: an unreadable
        # 'to' is not a workspace the import switched to, so nothing is re-activated and no
        # {from, to} pair carrying a null is published.
        ui = _FakeUI(reads=2)
        design = make_design()
        room = design.rootComponent
        wire(design=design, ui=ui, created=[BRepBody("Imported")],
             on_import=lambda: room.bRepBodies._items.append(BRepBody("Imported")))
        out = payload(mod.handler(file_path=cad("part.step")))
        assert "workspace_restored" not in out and "workspace_changed" not in out
        assert ui.activated == []               # never a blind re-activate on an unread workspace
        assert "None" not in out["note"]

    def test_the_dxf_arm_discloses_the_workspace_too(self, wire, cad):
        ui = _FakeUI()
        comp = MakeComp("Root", origin_planes=_origin_planes())
        design = make_design(comp=comp)

        def _side_effect():
            comp.sketches._items.append(_sketch("Outline"))
            ui.switch_to("FusionSolidEnvironment")

        wire(design=design, ui=ui, created=[_sketch("Outline")], on_import=_side_effect)
        out = payload(mod.handler(file_path=cad("plate.dxf")))
        assert out["workspace_restored"] == "Manufacture"

    def test_the_worst_composed_note_fits_the_wire_budget(self, wire, cad):
        # the SVG note is the longest base and the changed arm appends the workspace sentence to
        # it, a composition test_prose_budget's per-literal measurement never sees.
        ui = _FakeUI(activate=False)
        target = _sketch("Logo")
        design = make_design(sketches=[target])

        def _side_effect():
            target.sketchCurves._items.append(object())
            ui.switch_to("FusionSolidEnvironment")

        wire(design=design, ui=ui, created=[types.SimpleNamespace(name="Curve")],
             on_import=_side_effect)
        out = payload(mod.handler(file_path=cad("logo.svg"), sketch="Logo"))
        assert out["workspace_changed"]["to"] == "Design"
        assert len(out["note"]) <= 400, len(out["note"])    # test_prose_budget.NOTE_BUDGET_CHARS


class TestFeatureHealthAcrossADocumentSwitch:
    """FeatureHealthy captures the ACTIVE design's timeline count before the handler and walks the
    items past it after. new_document=True activates a DIFFERENT document, so the two reads describe
    two different timelines - the walked slice is meaningless and can skip a feature that failed to
    compute. The gate declares itself unrun instead."""

    def test_the_gate_is_declared_unrun_when_the_import_made_a_new_document(self):
        post = mod._FeatureHealthyHere()
        reason, evidence = post.verify({"new_document": True}, {"imported": True}, 0)
        assert reason == ""
        assert evidence["feature_health_verified"] is False
        assert "NEW document" in evidence["feature_health_note"]

    def test_a_same_document_import_still_runs_the_real_gate(self, monkeypatch):
        # The skip may only apply to the new-document path: an import into the OPEN design must
        # still fail on a feature that computed with an error.
        post = mod._FeatureHealthyHere()
        error_state = adsk.fusion.FeatureHealthStates.ErrorFeatureHealthState
        broken = types.SimpleNamespace(name="Imported1", healthState=error_state,
                                       errorOrWarningMessage="bad geometry")
        timeline = types.SimpleNamespace(count=1, item=lambda i: broken)
        monkeypatch.setattr(post, "_timeline", lambda: timeline)
        reason, _evidence = post.verify({"new_document": False}, {"imported": True}, 0)
        assert "FAILED to compute" in reason
        assert "Imported1" in reason

    def test_the_kind_declares_the_input_it_reads(self):
        # wrap() checks input_keys against the handler signature, so a typo here would fail at
        # registration rather than silently reading None and skipping nothing.
        assert mod._FeatureHealthyHere.input_keys == ("new_document",)
