"""Unit tests for ``model_set_material.py`` - assign a PHYSICAL material to bodies/component.

Pinned: the material search (exact name across document + libraries), the document-scope win, the
no-match candidate hint, the cross-library ambiguity refusal, per-body density read-back, and
partial-success reporting across a multi-body target.

Material has no shape dump, so that one double is built locally; the library catalog, the design,
its components and its bodies come from conftest, and ``install`` wires BOTH design seams (the
tool's own ``_common`` and ``_inputs._common``) to the same design - the dual-seam trap - so
TargetRef('') resolves against the same root component the handler reads.
"""

from types import SimpleNamespace

import pytest

from conftest import (load_tool, install, make_design, make_material_library,
                      payload as _payload, BRepBody, FakeMaterialLibraries, MakeComp, MakeDesign,
                      _NamedCollection)

mm = load_tool("model_set_material")


_DEFAULT_ID = object()

def _material(name, material_id=_DEFAULT_ID):
    """One catalog material - a name is its whole surface here; Material has no shape dump."""
    if material_id is _DEFAULT_ID:
        material_id = "synthetic-" + name.lower().replace(" ", "-")
    return SimpleNamespace(name=name, id=material_id)


def _Lib(name, materials):
    """One loaded library holding `materials`, over the shared MaterialLibrary fake."""
    return make_material_library(name, materials=materials)


class _Body(BRepBody):
    """A body whose ``.material`` setter records the assigned material (or raises when fail=True, to
    exercise partial success). ``physicalProperties.density`` is kg/cm3 (the API's unit)."""
    def __init__(self, name, density=None, fail=False):
        super().__init__(name=name)
        self._mat = None
        self._fail = fail
        self.physicalProperties = SimpleNamespace(density=density)

    @property
    def material(self):
        return self._mat

    @material.setter
    def material(self, m):
        if self._fail:
            raise RuntimeError("body material is read-only in this context")
        self._mat = m


class _NoComponentList(MakeDesign):
    """A design whose component list does not read - all_components degrades to the root alone."""
    @property
    def allComponents(self):
        raise AttributeError("allComponents")


@pytest.fixture
def wired(monkeypatch):
    """Build a fake design + material catalog, wire both design seams, and point the module ``app``
    (whose ``materialLibraries`` the search reads) at the libraries. Returns the design."""
    def _make(bodies, doc_materials=(), libraries=(), child_components=()):
        root = MakeComp(name="Root", bodies=list(bodies))
        if child_components:
            # design.allComponents carries the ROOT plus every child - the shape _common
            # .all_components reads; a design whose list does not read degrades to root-only, as live.
            design = make_design(comp=root, all_components=[root] + list(child_components))
        else:
            design = _NoComponentList(comp=root)
        design.materials = _NamedCollection(doc_materials)
        install(mm, design)
        monkeypatch.setattr(mm._materials, "app",
                            SimpleNamespace(materialLibraries=FakeMaterialLibraries(libraries)))
        return design
    return _make


class TestHappyPath:
    def test_assigns_and_reads_back_density_in_kg_per_m3(self, wired):
        # steel 0.00785 kg/cm3 -> 7850 kg/m3
        wired([_Body("Body1", density=0.00785)],
              libraries=[_Lib("Fusion Material Library", [_material("Steel", "steel-source")])])
        out = _payload(mm.handler(target="", material="Steel"))
        assert out["assigned"] is True
        assert out["material"] == "Steel"
        assert out["source"] == "Fusion Material Library"
        assert out["selected_source"] == {
            "name": "Steel", "id": "steel-source", "library": "Fusion Material Library"}
        assert out["density_kg_per_m3"] == 7850.0
        assert out["applied_to"][0]["body"] == "Body1"
        assert out["applied_to"][0]["material"] == "Steel"
        assert out["applied_to"][0]["material_id"] == "steel-source"
        assert out["applied_to"][0]["density_kg_per_m3"] == 7850.0

    def test_missing_material_ids_remain_null_in_the_receipt(self, wired):
        source = _material("Steel", None)
        wired([_Body("Body1", density=0.00785)],
              libraries=[_Lib("Fusion Material Library", [source])])
        res = mm.handler(target="", material="Steel")
        assert res["isError"] is True
        assert "identity is unavailable" in res["message"]

    def test_same_prefix_with_wrong_id_is_unverified(self, wired):
        source = _material("Steel", "source-steel")

        class _WrongIdBody(_Body):
            @property
            def material(self):
                return self._mat

            @material.setter
            def material(self, value):
                self._mat = SimpleNamespace(name=value.name + " (2)", id="source-steel")

        body = _WrongIdBody("Body1")
        wired([body], libraries=[_Lib("Lib", [source])])
        res = mm.handler(target="", material="Steel")
        assert res["isError"] is True
        assert "selected name='Steel', id='source-steel'" in res["message"]
        assert "actual name='Steel (2)', id='source-steel'" in res["message"]

    def test_exact_name_without_assigned_id_is_unverified(self, wired):
        source = _material("Steel", "source-steel")

        class _NoIdBody(_Body):
            @property
            def material(self):
                return self._mat

            @material.setter
            def material(self, value):
                self._mat = SimpleNamespace(name=value.name, id=None)

        body = _NoIdBody("Body1")
        wired([body], libraries=[_Lib("Lib", [source])])
        res = mm.handler(target="", material="Steel")
        assert res["isError"] is True
        assert "actual name='Steel', id='None'" in res["message"]

    def test_one_identity_mismatch_is_partial_failure(self, wired):
        source = _material("Steel", "source-steel")

        class _WrongIdBody(_Body):
            @property
            def material(self):
                return self._mat

            @material.setter
            def material(self, value):
                self._mat = SimpleNamespace(name=value.name, id="different-id")

        bad = _WrongIdBody("Bad")
        good = _Body("Good")
        wired([bad, good], libraries=[_Lib("Lib", [source])])
        out = _payload(mm.handler(target="", material="Steel"))
        assert [row["body"] for row in out["applied_to"]] == ["Good"]
        assert out["failed"][0]["body"] == "Bad"
        assert "UNVERIFIED" in out["failed"][0]["error"]

    def test_case_insensitive_exact_match(self, wired):
        wired([_Body("B", density=0.0027)],
              libraries=[_Lib("Lib", [_material("Aluminum 6061")])])
        out = _payload(mm.handler(target="", material="aluminum 6061"))
        assert out["material"] == "Aluminum 6061"

    def test_declared_outputs_present(self, wired):
        wired([_Body("B", density=0.00785)], libraries=[_Lib("Lib", [_material("Steel")])])
        out = _payload(mm.handler(target="", material="Steel"))
        for r in mm.RETURNS:
            assert r.assert_present(out) == "", r.key


class TestWholeDesignTarget:
    def test_an_empty_target_reaches_a_child_components_bodies(self, wired):
        # The only body here belongs to a CHILD component: a root-only walk assigns nothing and
        # leaves model_inspect's mass wrong for the one part that has a body.
        child_body = _Body("Child1", density=0.00785)
        child = MakeComp(name="Child", bodies=[child_body], mesh_bodies=[])
        wired([], libraries=[_Lib("Lib", [_material("Steel")])], child_components=[child])
        out = _payload(mm.handler(target="", material="Steel"))
        assert [a["body"] for a in out["applied_to"]] == ["Child1"]
        assert child_body.material.name == "Steel"
        assert out["components_covered"] == 2
        assert "all 2 component(s) the design listed" in out["note"]
        assert "component_walk_complete" not in out

    def test_every_row_names_the_component_that_owns_the_body(self, wired):
        # Two components each holding a 'Body1': rows keyed on the body name alone are two
        # identical rows, and a failed one names nothing the caller can act on.
        twin = _Body("Body1", density=0.00785)
        child = MakeComp(name="Child", bodies=[twin], mesh_bodies=[])
        wired([_Body("Body1", density=0.00785)],
              libraries=[_Lib("Lib", [_material("Steel")])], child_components=[child])
        out = _payload(mm.handler(target="", material="Steel"))
        assert [(a["component"], a["body"]) for a in out["applied_to"]] == [
            ("Root", "Body1"), ("Child", "Body1")]

    def test_a_degraded_walk_is_not_reported_as_a_one_component_design(self, wired):
        # all_components falls back to [root] when the design's collection will not read. Saying
        # "all 1 component(s)" there claims a design-wide census nothing performed.
        root_body = _Body("Root1", density=0.00785)
        design = wired([root_body], libraries=[_Lib("Lib", [_material("Steel")])])
        assert not hasattr(design, "allComponents")     # the unreadable-collection case
        out = _payload(mm.handler(target="", material="Steel"))
        assert out["component_walk_complete"] is False
        assert "component list did not read" in out["note"]
        assert "all 1 component(s)" not in out["note"]

    def test_a_named_component_target_stays_that_component(self, monkeypatch, wired):
        # Only the EMPTY target means the whole design. A named component assigns to its own
        # bodies and publishes no design-wide count.
        root_body = _Body("Root1", density=0.00785)
        child_body = _Body("Child1", density=0.00785)
        child = MakeComp(name="Child", bodies=[child_body], mesh_bodies=[])
        wired([root_body], libraries=[_Lib("Lib", [_material("Steel")])], child_components=[child])
        monkeypatch.setattr(mm._TARGET, "resolve", lambda raw: ((child, "component"), None))
        out = _payload(mm.handler(target="Child", material="Steel"))
        assert [a["body"] for a in out["applied_to"]] == ["Child1"]
        assert root_body.material is None
        assert "components_covered" not in out


class TestSearchGuards:
    def test_no_match_lists_nearest_candidates(self, wired):
        wired([_Body("B")],
              libraries=[_Lib("Lib", [_material("Steel"), _material("Stainless Steel")])])
        res = mm.handler(target="", material="Steal")
        assert res["isError"] is True
        assert "No material named 'Steal'" in res["message"]
        assert "Steel" in res["message"]              # nearest offered, not silence

    def test_ambiguous_across_libraries_is_refused(self, wired):
        wired([_Body("B")],
              libraries=[_Lib("LibA", [_material("Brass")]), _Lib("LibB", [_material("Brass")])])
        res = mm.handler(target="", material="Brass")
        assert res["isError"] is True
        assert "ambiguous" in res["message"].lower()
        assert "LibA" in res["message"] and "LibB" in res["message"]

    def test_document_material_wins_over_ambiguous_libraries(self, wired):
        # The same name in the document AND two libraries: the document copy is unambiguous and wins,
        # so this must NOT refuse.
        wired([_Body("B", density=0.0027)],
              doc_materials=[_material("Aluminum")],
              libraries=[_Lib("LibA", [_material("Aluminum")]), _Lib("LibB", [_material("Aluminum")])])
        out = _payload(mm.handler(target="", material="Aluminum"))
        assert out["source"] == "document"

    def test_empty_material_name_errors(self, wired):
        wired([_Body("B")], libraries=[_Lib("Lib", [_material("Steel")])])
        res = mm.handler(target="", material="")
        assert res["isError"] is True
        assert "material" in res["message"].lower()

    def test_no_catalog_reports_no_materials(self, wired):
        wired([_Body("B")], doc_materials=(), libraries=())
        res = mm.handler(target="", material="Steel")
        assert res["isError"] is True
        assert "No materials available" in res["message"]


class TestExactMaterialSelectors:
    def test_library_id_and_material_id_select_the_requested_duplicate(self, wired):
        first, second = _material("Brass", "a"), _material("Brass", "b")
        lib_a, lib_b = _Lib("Shared", [first]), _Lib("Shared", [second])
        lib_a.id, lib_b.id = "library-a", "library-b"
        body = _Body("B")
        wired([body], doc_materials=[_material("Brass", "doc")], libraries=[lib_a, lib_b])
        assert mm.handler(material="Brass", library="Shared")["isError"]
        out = _payload(mm.handler(material="Brass", library="library-b", material_id="b"))
        assert out["assigned"] is True and body.material is second
        _payload(mm.handler(material="Brass", library="library-a", material_id="a"))
        assert body.material is first

    def test_material_id_disambiguates_same_named_entries_and_miss_does_not_assign(self, wired):
        first, second = _material("Steel", "a"), _material("Steel", "b")
        body = _Body("B")
        wired([body], libraries=[_Lib("Lib", [first, second])])
        assert mm.handler(material="Steel", library="Lib")["isError"]
        assert mm.handler(material="Steel", library="Lib", material_id="missing")["isError"]
        assert body.material is None
        _payload(mm.handler(material="Steel", library="Lib", material_id="b"))
        assert body.material is second

    def test_duplicate_document_names_refuse_and_name_filters_a_shared_asset_id(self, wired):
        first, second = _material("Steel", "asset"), _material("Steel copy", "asset")
        body = _Body("B")
        wired([body], doc_materials=[first, second])
        _payload(mm.handler(material="Steel copy", library="document", material_id="asset"))
        assert body.material is second
        wired([body], doc_materials=[first, _material("Steel", "other")])
        assert mm.handler(material="Steel", library="document")["isError"]


class TestPartialSuccess:
    def test_partial_success_surfaced(self, wired):
        wired([_Body("Good", density=0.00785), _Body("Bad", fail=True)],
              libraries=[_Lib("Lib", [_material("Steel")])])
        out = _payload(mm.handler(target="", material="Steel"))
        assert out["assigned"] is True
        assert [a["body"] for a in out["applied_to"]] == ["Good"]
        assert out["failed"][0]["body"] == "Bad"
        assert "1 of 2" in out["note"]

    def test_all_bodies_fail_is_error(self, wired):
        wired([_Body("Bad", fail=True)], libraries=[_Lib("Lib", [_material("Steel")])])
        res = mm.handler(target="", material="Steel")
        assert res["isError"] is True
        assert "No body assignment was verified" in res["message"]

    def test_a_silently_swallowed_assignment_lands_in_failed_not_applied(self, wired):
        # the honesty core: the assignment raises nothing but the body still reads its OLD
        # material - the read-back must route it to 'failed', never report it applied
        class _StuckBody(_Body):
            @property
            def material(self):
                return _material("OldPaint")

            @material.setter
            def material(self, m):
                pass                                  # accepted and ignored

        wired([_StuckBody("Stuck"), _Body("Good", density=0.00785)],
              libraries=[_Lib("Lib", [_material("Steel")])])
        out = _payload(mm.handler(target="", material="Steel"))
        assert [a["body"] for a in out["applied_to"]] == ["Good"]
        assert out["failed"][0]["body"] == "Stuck"
        assert "OldPaint" in out["failed"][0]["error"]

    def test_a_swallowed_assignment_on_the_only_body_is_an_error(self, wired):
        # the same swallowed assignment with nothing else to succeed: the read-back is the only
        # thing that can convict, and with no body applied the call must be isError - an ok here
        # would report a material assignment over a body still carrying its old one.
        class _StuckBody(_Body):
            @property
            def material(self):
                return _material("OldPaint")

            @material.setter
            def material(self, m):
                pass                                  # accepted and ignored

        wired([_StuckBody("Stuck")], libraries=[_Lib("Lib", [_material("Steel")])])
        res = mm.handler(target="", material="Steel")
        assert res["isError"] is True
        assert "No body assignment was verified" in res["message"]
        assert "OldPaint" in res["message"]           # names what the body actually reads


class TestDesignGuard:
    def test_no_active_design(self, monkeypatch):
        monkeypatch.setattr(mm._common, "design", lambda: None)
        res = mm.handler(target="", material="Steel")
        assert res["isError"] is True
        assert "design" in res["message"].lower()
