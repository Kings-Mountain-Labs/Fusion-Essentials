"""Unit tests for ``design_set_name.py`` - rename a body, occurrence, or component.

Pinned here, no live Fusion: the rename is judged by the name READ BACK off the model (never by the
assignment returning), so an auto-deduped name is published as what LANDED rather than what was
asked for; an occurrence target renames its COMPONENT and the instance name/fullPathName that
follows is read back as the evidence; a refused set is an error, not a false ok; and the guards
(empty name, ambiguous target, no-op) refuse before or instead of mutating.

The fakes model two measured platform behaviours the shared conftest fakes do not: a name setter
that dedupes against its siblings, and an occurrence whose displayed name derives from its
component's.
"""

from types import SimpleNamespace

import pytest

from conftest import (BRepBody, FakeOccurrence, MakeComp, MakeDesign, MeshBody, _NamedCollection,
                      entity_proxy, error_message, install, load_tool, payload)

sn = load_tool("design_set_name")


# ── fakes ────────────────────────────────────────────────────────────────────

class _Deduping:
    """A named entity whose setter reproduces the measured platform behaviour: assigning a name a
    sibling already holds lands '<name> (1)' instead. ``refuse=True`` models the other failure the
    read-back exists to catch - the assignment is accepted and silently changes nothing; ``raises``
    models the platform declining out loud."""

    def __init__(self, name, siblings=None, token=None, refuse=False, raises="", parent=None):
        self._siblings = siblings if siblings is not None else []
        self._siblings.append(self)
        self._name = name
        self._refuse = refuse
        self._raises = raises
        self.entityToken = token or name
        self.parentComponent = parent

    @property
    def name(self):
        return self._name

    @name.setter
    def name(self, value):
        if self._raises:
            raise RuntimeError(self._raises)
        if self._refuse:
            return
        taken = {s._name for s in self._siblings if s is not self}
        self._name = f"{value} (1)" if value in taken else value


class _Occurrence(FakeOccurrence):
    """An occurrence whose displayed name and fullPathName FOLLOW its component's name (measured:
    setting Component.name makes the occurrence read 'BasePlate:1')."""

    def __init__(self, component, index=1, parent_path="", bodies=()):
        self._index = index
        self._parent_path = parent_path
        super().__init__(path="", component=component)
        self.bRepBodies = _NamedCollection(list(bodies))

    @property
    def name(self):
        return f"{self.component.name}:{self._index}"

    @name.setter
    def name(self, value):
        # the base seeds a path-derived name here; this occurrence reads its component's instead
        pass

    @property
    def fullPathName(self):
        return f"{self._parent_path}+{self.name}" if self._parent_path else self.name


@pytest.fixture
def wire(monkeypatch):
    """Build a design and patch BOTH design seams (the tool's own ``_common`` and the one
    ``_inputs`` resolves the target through). Returns (design, root)."""
    def _make(bodies=(), occurrences=(), components=(), root_name="Root", meshes=()):
        # Every component carries an entityToken, and _common.same_component is measured to compare
        # on it: the root-rename guard cannot tell a sub-component from the root without one, and it
        # refuses rather than guess. A test that wants the unreadable state deletes the attribute.
        root = MakeComp(name=root_name, bodies=list(bodies), occurrences=list(occurrences),
                        entity_token="TOKEN:Root")
        for i, sub in enumerate(components):
            if not hasattr(sub, "entityToken"):
                sub.entityToken = f"TOKEN:Sub{i}"
        root.meshBodies = _NamedCollection(list(meshes))
        design = MakeDesign(comp=root, all_components=[root, *components])
        occs = list(occurrences)
        root.allOccurrencesByComponent = lambda comp: _NamedCollection(
            [o for o in occs if o.component is comp])
        install(sn, design)
        return design, root
    return _make


# ── renaming a body ──────────────────────────────────────────────────────────

class TestBodyRename:
    def test_publishes_the_name_read_back_off_the_model(self, wire):
        wire(bodies=[BRepBody("Body1")])
        out = payload(sn.handler(target="Body1", new_name="PlateMain"))
        assert out["renamed"] is True and out["changed"] is True
        assert out["kind"] == "body"
        assert out["previous_name"] == "Body1"
        assert out["name"] == "PlateMain"
        assert out["deduped"] is False

    def test_strips_the_requested_name(self, wire):
        _design, root = wire(bodies=[BRepBody("Body1")])
        out = payload(sn.handler(target="Body1", new_name="  Plate  "))
        assert out["name"] == "Plate"
        assert root.bRepBodies.item(0).name == "Plate"      # the model, not just the payload

    def test_declared_outputs_present(self, wire):
        wire(bodies=[BRepBody("Body1")])
        out = payload(sn.handler(target="Body1", new_name="Plate"))
        for r in sn.RETURNS:
            assert r.assert_present(out) == "", r.key


class TestDedupe:
    def test_landed_name_is_published_not_the_request(self, wire):
        # measured: renaming a second body onto a taken name silently lands 'Name (1)'.
        siblings = []
        first = _Deduping("PlateMain", siblings, token="t1")
        second = _Deduping("Body2", siblings, token="t2")
        wire(bodies=[first, second])
        out = payload(sn.handler(target="Body2", new_name="PlateMain"))
        assert out["name"] == "PlateMain (1)"          # what LANDED
        assert out["requested_name"] == "PlateMain"    # what was asked for, kept distinct
        assert out["deduped"] is True
        assert second.name == "PlateMain (1)"

    def test_note_names_the_dedupe(self, wire):
        siblings = []
        _Deduping("PlateMain", siblings, token="t1")
        _Deduping("Body2", siblings, token="t2")
        wire(bodies=list(siblings))
        out = payload(sn.handler(target="Body2", new_name="PlateMain"))
        assert "PlateMain (1)" in out["note"]
        assert "dedup" in out["note"].lower()


# ── renaming a component (directly, or through one of its occurrences) ───────

class TestComponentRename:
    def test_occurrence_target_renames_the_component_and_the_instance_follows(self, wire):
        comp = MakeComp(name="Component1")
        occ = _Occurrence(comp, index=1)
        wire(occurrences=[occ], components=[comp])
        out = payload(sn.handler(target="Component1:1", new_name="BasePlate"))
        assert out["kind"] == "component"               # the occurrence has no name of its own
        assert "occurrence 'Component1:1'" in out["addressed"]
        assert out["name"] == "BasePlate"
        assert out["occurrence_name"] == "BasePlate:1"  # the displayed name followed
        assert out["occurrence_path"] == "BasePlate:1"
        assert comp.name == "BasePlate"

    def test_nested_occurrence_path_follows_the_rename(self, wire):
        comp = MakeComp(name="Inner")
        occ = _Occurrence(comp, index=1, parent_path="Outer:1")
        wire(occurrences=[occ], components=[comp])
        out = payload(sn.handler(target="Outer:1+Inner:1", new_name="Gearbox"))
        assert out["occurrence_path"] == "Outer:1+Gearbox:1"

    def test_every_instance_is_reported(self, wire):
        # one component, two instances: renaming it renames both browser names - say how many, and
        # read the ADDRESSED instance back (a name fabricated from the request would read ':1').
        comp = MakeComp(name="Component1")
        occs = [_Occurrence(comp, index=1), _Occurrence(comp, index=2)]
        wire(occurrences=occs, components=[comp])
        out = payload(sn.handler(target="Component1:2", new_name="Bracket"))
        assert out["occurrences"] == 2
        assert out["occurrence_name"] == "Bracket:2"
        assert "all 2" in out["note"]

    def test_deduped_component_instances_follow_the_LANDED_name(self, wire):
        # measured: a component renamed onto a sibling component's name dedupes, and every instance
        # reads the LANDED name - 'Component1 (1):1', not the requested 'Component1:1'.
        siblings = []
        _Deduping("Component1", siblings, token="c1")
        comp = _Deduping("Component2", siblings, token="c2")
        wire(occurrences=[_Occurrence(comp, index=1), _Occurrence(comp, index=2)],
             components=list(siblings))
        out = payload(sn.handler(target="Component2:1", new_name="Component1"))
        assert out["deduped"] is True
        assert out["name"] == "Component1 (1)"
        assert out["occurrence_name"] == "Component1 (1):1"
        assert out["occurrence_path"] == "Component1 (1):1"
        assert out["occurrences"] == 2

    def test_single_instance_does_not_claim_others(self, wire):
        comp = MakeComp(name="Component1")
        wire(occurrences=[_Occurrence(comp, index=1)], components=[comp])
        out = payload(sn.handler(target="Component1:1", new_name="Bracket"))
        assert out["occurrences"] == 1
        assert "instances" not in out["note"]

    def test_component_without_an_occurrence_renames_and_reports_none(self, wire):
        # a sub-component nobody instanced: it renames, and the payload claims no instances.
        comp = MakeComp(name="Orphan")
        wire(components=[comp])
        out = payload(sn.handler(target="Orphan", new_name="Spare"))
        assert out["kind"] == "component"
        assert out["name"] == "Spare"
        assert out["occurrences"] == 0
        assert out["occurrence_name"] is None

    def test_root_component_rename_is_refused(self, wire):
        # measured: the platform raises '3 : root component name cannot be changed', and that raise
        # aborts the enclosing transaction - so the guard refuses BEFORE attempting the set.
        _design, root = wire(bodies=[BRepBody("Body1")], root_name="Root")
        msg = error_message(sn.handler(target="Root", new_name="MainAssembly"))
        assert "ROOT component" in msg and "doc_save_as" in msg
        assert root.name == "Root"

    def test_root_refusal_does_not_rely_on_object_identity(self, wire):
        # component identity is measured NEVER stable: every read of rootComponent is a DIFFERENT
        # object. The guard must still fire when the design hands back a fresh wrapper.
        design, root = wire(bodies=[BRepBody("Body1")], root_name="Root")
        design.rootComponent = entity_proxy(root)
        msg = error_message(sn.handler(target="Root", new_name="MainAssembly"))
        assert "ROOT component" in msg
        assert root.name == "Root"

    def test_an_unreadable_identity_refuses_instead_of_attempting_the_rename(self, wire):
        # same_component answers None when a token will not read, and the platform's root refusal
        # aborts the enclosing transaction even when caught - so an unproven not-root is never
        # attempted. The component keeps its name: nothing was set.
        comp = MakeComp(name="Component1")
        occ = _Occurrence(comp, index=1)
        wire(occurrences=[occ], components=[comp])
        del comp.entityToken
        msg = error_message(sn.handler(target="Component1:1", new_name="BasePlate"))
        assert "could not be read" in msg and "not attempted" in msg
        assert comp.name == "Component1"


# ── guards and honesty ───────────────────────────────────────────────────────

class TestGuards:
    def test_empty_name_refused(self, wire):
        _design, root = wire(bodies=[BRepBody("Body1")])
        assert "'new_name' is required" in error_message(sn.handler(target="Body1", new_name=""))
        assert root.bRepBodies.item(0).name == "Body1"

    def test_whitespace_only_name_refused(self, wire):
        _design, root = wire(bodies=[BRepBody("Body1")])
        assert "required" in error_message(sn.handler(target="Body1", new_name="   "))
        assert root.bRepBodies.item(0).name == "Body1"

    def test_empty_target_refused_nothing_to_rename(self, wire):
        # '' means "the whole design" to other TargetRef consumers; here there is nothing to rename.
        wire(bodies=[BRepBody("Body1")])
        assert "'target' is required" in error_message(sn.handler(target="", new_name="Plate"))

    def test_unknown_target_refused(self, wire):
        wire(bodies=[BRepBody("Body1")])
        assert "did not resolve" in error_message(sn.handler(target="Ghost", new_name="Plate"))

    def test_ambiguous_target_refused_without_renaming_either(self, wire):
        # two bodies share the name "Bolt" - TargetRef's own refusal passes straight through, and
        # NEITHER body may be touched.
        root_bolt = _Deduping("Bolt", token="t1")
        sub_bolt = _Deduping("Bolt", token="t2")
        comp = MakeComp(name="Sub")
        wire(bodies=[root_bolt], occurrences=[_Occurrence(comp, bodies=[sub_bolt])],
             components=[comp])
        msg = error_message(sn.handler(target="Bolt", new_name="M6Bolt"))
        assert "ambiguous" in msg.lower()
        assert root_bolt.name == "Bolt" and sub_bolt.name == "Bolt"

    def test_no_active_design_refused(self, monkeypatch):
        monkeypatch.setattr(sn._common, "design", lambda: None)
        monkeypatch.setattr(sn._inputs._common, "design", lambda: None)
        assert "No active design" in error_message(sn.handler(target="Body1", new_name="Plate"))

    def test_no_op_rename_is_a_clean_ok(self, wire):
        wire(bodies=[BRepBody("Plate")])
        out = payload(sn.handler(target="Plate", new_name="Plate"))
        assert out["changed"] is False
        assert out["name"] == "Plate" and out["previous_name"] == "Plate"
        assert "nothing changed" in out["note"]

    def test_a_silently_refused_set_is_an_error_not_a_false_ok(self, wire):
        # the cardinal sin: the assignment is accepted, the name never moves. The read-back must
        # turn that into an error naming what the target still reads.
        body = _Deduping("Body1", token="t1", refuse=True)
        wire(bodies=[body])
        msg = error_message(sn.handler(target="Body1", new_name="Plate"))
        assert "did not take" in msg and "Body1" in msg

    def test_a_raising_set_is_reported_with_its_reason(self, wire):
        wire(bodies=[_Deduping("Body1", token="t1",
                               raises="the name is read-only in this context")])
        msg = error_message(sn.handler(target="Body1", new_name="Plate"))
        assert "Could not rename" in msg and "read-only in this context" in msg


class TestLabel:
    def test_occurrence_label_uses_the_full_path(self):
        occ = SimpleNamespace(fullPathName="Outer:1+Inner:1", name="Inner:1")
        assert sn._label(occ, "occurrence") == "occurrence 'Outer:1+Inner:1'"

    def test_body_label_names_the_kind(self):
        assert sn._label(SimpleNamespace(name="Plate"), "body") == "body 'Plate'"


@pytest.fixture
def mesh_wire(wire, monkeypatch):
    """`wire` plus the MeshBody TYPE the mesh/body kind split is decided on - _is_mesh asks
    isinstance(entity, adsk.fusion.MeshBody), so an unbound type would classify every mesh as a
    BRep body."""
    import adsk.fusion
    monkeypatch.setattr(adsk.fusion, "MeshBody", MeshBody, raising=False)
    return wire


class TestMeshRename:
    """MeshBody.name is settable and the assignment sticks - the new name reads back off the wrapper
    it was set on AND off a fresh meshBodies fetch. That is why 'mesh' is in the target kinds; drop
    it and these go red."""

    def test_mesh_target_renames_and_publishes_the_landed_name(self, mesh_wire):
        _design, root = mesh_wire(meshes=[MeshBody(name="Scan1")])
        out = payload(sn.handler(target="Scan1", new_name="ScannedHousing"))
        assert out["renamed"] is True and out["changed"] is True
        assert out["kind"] == "mesh"
        assert out["previous_name"] == "Scan1"
        assert out["name"] == "ScannedHousing"
        # the MODEL, re-fetched from the collection - not just the payload the handler assembled
        assert root.meshBodies.item(0).name == "ScannedHousing"

    def test_mesh_kind_is_advertised_on_the_target_input(self):
        assert "mesh" in sn._TARGET.allow

    def test_mesh_rename_that_silently_does_not_take_is_an_error(self, mesh_wire):
        class _StuckMesh(MeshBody):
            """The other failure the read-back exists to catch: the setter is ACCEPTED and the name
            silently does not move."""
            @property
            def name(self):
                return "Scan1"

            @name.setter
            def name(self, value):
                pass

        mesh_wire(meshes=[_StuckMesh(name="Scan1")])
        res = sn.handler(target="Scan1", new_name="ScannedHousing")
        assert res["isError"] is True
        assert "did not take" in error_message(res)
