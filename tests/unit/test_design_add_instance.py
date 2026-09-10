"""Unit tests for ``design_add_instance.py`` - place another instance of an existing component.

Pinned here, no live Fusion: the instance Fusion actually created is read back off the ASSEMBLY
tree (root.allOccurrences), never off the occurrence the call returned - that handle reports a
COMPONENT-LOCAL fullPathName (measured), so a tool that published it would hand back a path no
other tool can resolve. The fakes reproduce that asymmetry, the global per-component instance
counter that names the new instance, and the failure the read-back exists to catch: a call that
returns an occurrence while the tree gains nothing.
"""

from types import SimpleNamespace

import pytest

from conftest import (MakeComp, MakeDesign, _NamedCollection, error_message, go_stale, install,
                      load_tool, payload)

ai = load_tool("design_add_instance")


# ── fake assembly ────────────────────────────────────────────────────────────
#
# A component knows its name + entityToken (same_component compares tokens - component wrappers are
# never identity-stable) and its allOccurrences subtree view (what the cycle guard reads). The
# design's tree is a list of occurrence records; each records its parent, so a path is the chain.


def _comp(name):
    c = MakeComp(name=name)
    c.entityToken = "tok:" + name
    c.allOccurrences = []
    return c


# The collection whose COUNT itself raises - the shape the cycle walk cannot enumerate at all,
# which is not the same as an empty subtree.
_UNREADABLE = "2 : InternalValidationError : occ"


def _path(node):
    parent = node.parent
    return f"{_path(parent)}+{node.name}" if parent is not None else node.name


def _refresh(des):
    """Recompute every derived read the tool walks: assembly paths, the root tree, and each
    component's own subtree (the components living inside it)."""
    for node in des.tree:
        node.fullPathName = _path(node)
        node.sourceComponent = node.parent.component if node.parent is not None else des.rootComponent
    des.rootComponent.allOccurrences = list(des.tree)
    for comp in des.comps.values():
        if comp is des.rootComponent:
            continue
        inside = []
        for node in des.tree:
            anc = node.parent
            while anc is not None:
                if anc.component is comp:
                    inside.append(node)
                    break
                anc = anc.parent
        comp.allOccurrences = inside


def _instance(des, comp, parent=None):
    """Add one occurrence of `comp` under `parent` (None = root), numbered off the GLOBAL
    per-component counter - the measured naming ('BasePlate:2' for the second instance anywhere)."""
    des.counter[comp.name] = des.counter.get(comp.name, 0) + 1
    node = SimpleNamespace(component=comp, parent=parent, isValid=True,
                           name=f"{comp.name}:{des.counter[comp.name]}", fullPathName="",
                           sourceComponent=None)
    des.tree.append(node)
    _refresh(des)
    return node


def _clone_children(des, src, dst):
    """Copy the children of an existing instance under a NEW one. Measured: instancing a component
    that holds sub-components lands a path per child too, and a child KEEPS its own number
    ('Sub:2+Bolt:1', not 'Bolt:2')."""
    for node in [n for n in list(des.tree) if n.parent is src]:
        child = SimpleNamespace(component=node.component, parent=dst, isValid=True,
                                name=node.name, fullPathName="", sourceComponent=None)
        des.tree.append(child)
        _clone_children(des, node, child)


def _make(names=("Bolt",), refuse=None, ghost=False):
    """A design holding the named components (each with one instance at root, in order) plus the
    addExistingComponent behaviour under test.

    refuse: 'none' returns nothing, 'raise' raises, 'invalid' returns isValid=false, 'silent' returns
    a live-looking occurrence while the tree gains nothing (the swallowed no-op).
    """
    root = _comp("Root")
    des = MakeDesign(comp=root)
    des.tree, des.counter, des.comps = [], {}, {"Root": root}
    for name in names:
        comp = _comp(name)
        des.comps[name] = comp
        if not ghost:
            _instance(des, comp)
    des._all_components = list(des.comps.values())

    def _wire(host):
        def add_existing(component, transform):
            des.last_transform = transform
            des.added = (host.name, component.name)
            if refuse == "raise":
                raise RuntimeError("the component is locked")
            if refuse == "none":
                return None
            if refuse == "silent":
                return SimpleNamespace(name=f"{component.name}:99", fullPathName=f"{component.name}:99",
                                       isValid=True, component=component)
            parents = ([n for n in des.tree if n.component is host] if host is not root else [None])
            # ONE counter bump for the call: measured, a host with two instances lands the SAME
            # instance name under each ('Frame:1+Bolt:2' and 'Frame:2+Bolt:2'), not two numbers.
            des.counter[component.name] = des.counter.get(component.name, 0) + 1
            inst_name = f"{component.name}:{des.counter[component.name]}"
            source = next((n for n in des.tree if n.component is component), None)
            made = []
            for parent in parents:
                node = SimpleNamespace(component=component, parent=parent, isValid=True,
                                       name=inst_name, fullPathName="", sourceComponent=None)
                des.tree.append(node)
                made.append(node)
                if source is not None:
                    _clone_children(des, source, node)
            _refresh(des)
            # The RETURN is the measured read asymmetry: an occurrence taken from <component>.
            # occurrences reports a COMPONENT-LOCAL fullPathName, not the assembly path.
            return SimpleNamespace(name=made[0].name, fullPathName=made[0].name,
                                   isValid=(refuse != "invalid"), component=component)
        host.occurrences.addExistingComponent = add_existing

    for comp in des.comps.values():
        _wire(comp)
    _refresh(des)
    return des


def _matrix():
    m = SimpleNamespace(translation=None, rotation=None)
    m.setToRotation = lambda angle, axis, origin: (setattr(m, "rotation", (angle, axis, origin))
                                                   or True)
    return m


@pytest.fixture(autouse=True)
def _transforms(monkeypatch):
    """Record what the placement transform was built from (the mocks return opaque objects)."""
    import adsk.core
    monkeypatch.setattr(adsk.core.Matrix3D, "create", staticmethod(_matrix))
    monkeypatch.setattr(adsk.core.Vector3D, "create", staticmethod(lambda x, y, z: ("vec", x, y, z)))
    monkeypatch.setattr(adsk.core.Point3D, "create", staticmethod(lambda x, y, z: ("pt", x, y, z)))


@pytest.fixture
def wire():
    def _install(**kw):
        des = _make(**kw)
        install(ai, des)
        return des
    return _install


# ── the landed instance is READ, never predicted ─────────────────────────────

class TestLandedInstance:
    def test_publishes_the_name_the_global_counter_gave_it(self, wire):
        des = wire(names=("Bolt",))
        out = payload(ai.handler(component="Bolt"))
        assert out["created"] is True
        assert out["occurrence"] == "Bolt:2"          # the SECOND instance, off Fusion's counter
        assert out["full_path"] == "Bolt:2"
        assert len(des.tree) == 2

    def test_nested_instance_publishes_the_assembly_path_not_the_local_one(self, wire):
        # the bite: addExistingComponent hands back 'Bolt:2' (component-local). The assembly path
        # is 'Outer:1+Bolt:2', and that is the only one another tool can resolve.
        des = wire(names=("Outer", "Bolt"))
        out = payload(ai.handler(component="Bolt", into_component="Outer:1"))
        assert out["full_path"] == "Outer:1+Bolt:2"
        assert out["occurrence"] == "Bolt:2"
        assert des.added == ("Outer", "Bolt")
        assert "Outer:1+Bolt:2" in {n.fullPathName for n in des.tree}

    def test_a_multi_instance_host_reports_every_path_it_landed(self, wire):
        # measured: the one call lands the SAME instance under each host instance
        # ('Frame:1+Bolt:2' and 'Frame:2+Bolt:2') - two paths, one instance number.
        des = wire(names=("Outer", "Bolt"))
        _instance(des, des.comps["Outer"])            # a second Outer - one add lands under both
        out = payload(ai.handler(component="Bolt", into_component="Outer:1"))
        assert out["paths"] == ["Outer:1+Bolt:2", "Outer:2+Bolt:2"]
        assert out["full_path"] == "Outer:1+Bolt:2"
        assert "The host has 2 instances" in out["note"]

    def test_the_primary_path_is_the_host_instance_that_was_NAMED(self, wire):
        # with two host instances the new paths are siblings; the one the caller asked for is the
        # answer, not whichever sorts first.
        des = wire(names=("Outer", "Bolt"))
        _instance(des, des.comps["Outer"])
        out = payload(ai.handler(component="Bolt", into_component="Outer:2"))
        assert out["full_path"] == "Outer:2+Bolt:2"

    def test_instancing_a_sub_assembly_names_its_children_not_phantom_host_instances(self, wire):
        # measured (single-instance host): instancing 'Sub' lands 'Sub:2' AND 'Sub:2+Bolt:1' - the
        # extra path is the CHILD coming along, keeping its own number. Calling that "the host has
        # several instances" is the false claim this pins.
        des = wire(names=("Sub", "Bolt"))
        des.tree[1].parent = des.tree[0]                 # Bolt:1 lives inside Sub:1
        _refresh(des)
        out = payload(ai.handler(component="Sub"))
        assert out["full_path"] == "Sub:2"
        assert out["paths"] == ["Sub:2", "Sub:2+Bolt:1"]
        assert "child path(s) came with it" in out["note"]
        assert "Bolt:1" in out["note"]                   # the child KEPT its number
        assert "instances" not in out["note"]            # never claims a multi-instance host

    def test_composite_sub_assembly_into_a_multi_instance_host_counts_HOSTS_not_paths(self, wire):
        # both shapes at once: a sub-assembly (its child comes along) into a host with TWO instances
        # (the instance lands under each). Counting leftover paths says "3 instances" for a 2-instance
        # host - the host count is the distinct PREFIXES above the new instance's own segment.
        des = wire(names=("Outer", "Sub", "Bolt"))
        des.tree[2].parent = des.tree[1]              # Bolt:1 lives inside Sub:1
        _instance(des, des.comps["Outer"])            # a second Outer
        _refresh(des)
        out = payload(ai.handler(component="Sub", into_component="Outer:1"))
        assert out["full_path"] == "Outer:1+Sub:2"
        assert out["paths"] == ["Outer:1+Sub:2", "Outer:1+Sub:2+Bolt:1",
                                "Outer:2+Sub:2", "Outer:2+Sub:2+Bolt:1"]
        assert "The host has 2 instances" in out["note"]
        assert "child path(s) came with it" in out["note"]

    def test_component_name_still_resolves_once_it_has_several_instances(self, wire):
        # the tool's headline case: instancing a component that ALREADY has instances. Every
        # occurrence matching 'Bolt' is an instance of the one component, so the component - not an
        # ambiguity refusal - is the answer, and the second call works like the first.
        des = wire(names=("Bolt",))
        first = payload(ai.handler(component="Bolt"))
        assert first["occurrence"] == "Bolt:2"
        second = payload(ai.handler(component="Bolt"))
        assert second["occurrence"] == "Bolt:3"
        assert len(des.tree) == 3

    def test_component_resolves_by_bare_name_when_it_has_no_instance(self, wire):
        des = wire(names=("Spare",), ghost=True)
        out = payload(ai.handler(component="Spare"))
        assert out["component"] == "Spare"
        assert out["occurrence"] == "Spare:1"
        assert len(des.tree) == 1

    def test_declared_outputs_present(self, wire):
        wire()
        out = payload(ai.handler(component="Bolt"))
        for r in ai.RETURNS:
            assert r.assert_present(out) == "", r.key


# ── honesty: a call that changed nothing is an error, never a false ok ───────

class TestHonesty:
    def test_an_occurrence_returned_with_an_unchanged_tree_is_an_error(self, wire):
        wire(refuse="silent")
        msg = error_message(ai.handler(component="Bolt"))
        assert "no new instance appeared" in msg

    def test_nothing_returned_is_an_error(self, wire):
        wire(refuse="none")
        assert "returned nothing" in error_message(ai.handler(component="Bolt"))

    def test_an_unreadable_census_is_named_as_such_not_as_no_instance_appeared(self, wire):
        # An unreadable allOccurrences walk gives the SAME empty set an empty assembly gives. The
        # host this instanced into proves the assembly is not empty, so "no new instance appeared"
        # would state a verdict the walk never delivered - a wrong cause the caller acts on.
        wire()
        original = ai.occurrence_paths
        calls = {"n": 0}

        def blind(design):
            calls["n"] += 1
            return original(design) if calls["n"] == 1 else set()

        ai.occurrence_paths = blind
        try:
            msg = error_message(ai.handler(component="Bolt"))
        finally:
            ai.occurrence_paths = original
        assert "could not be read" in msg
        assert "may or may not have landed" in msg
        assert "no new instance appeared" not in msg

    def test_a_raising_call_reports_its_reason(self, wire):
        wire(refuse="raise")
        msg = error_message(ai.handler(component="Bolt"))
        assert "Could not instance 'Bolt'" in msg and "locked" in msg

    def test_an_invalid_occurrence_is_an_error(self, wire):
        wire(refuse="invalid")
        assert "isValid=false" in error_message(ai.handler(component="Bolt"))

    def test_an_UNREADABLE_isValid_is_not_treated_as_false(self, wire):
        # read_flag answers None for a flag that could not be read, and None is not a refusal: the
        # instance is in the tree, so refusing here would report a landed instance as failed.
        des = wire()
        real = des.rootComponent.occurrences.addExistingComponent

        def add_then_hide_the_flag(component, transform):
            occ = real(component, transform)
            del occ.isValid
            return occ

        des.rootComponent.occurrences.addExistingComponent = add_then_hide_the_flag
        out = payload(ai.handler(component="Bolt"))
        assert out["created"] is True and out["occurrence"] == "Bolt:2"


# ── the self-nesting refusal ─────────────────────────────────────────────────

class TestSelfNestingRefused:
    def test_instancing_a_component_into_itself_is_refused(self, wire):
        des = wire(names=("Outer",))
        msg = error_message(ai.handler(component="Outer", into_component="Outer:1"))
        assert "instance of itself" in msg
        assert len(des.tree) == 1                     # nothing was created
        assert getattr(des, "added", None) is None    # the mutation was never reached

    def test_instancing_into_something_nested_inside_it_is_refused(self, wire):
        des = wire(names=("Outer", "Inner"))
        # move Inner's instance under Outer so Outer now contains Inner
        des.tree[1].parent = des.tree[0]
        _refresh(des)
        msg = error_message(ai.handler(component="Outer", into_component="Outer:1+Inner:1"))
        assert "sits inside it" in msg
        assert getattr(des, "added", None) is None

    def test_a_sibling_target_is_not_refused(self, wire):
        # the guard must not over-refuse: two unrelated components nest fine.
        des = wire(names=("Outer", "Bolt"))
        out = payload(ai.handler(component="Bolt", into_component="Outer:1"))
        assert out["created"] is True and des.added == ("Outer", "Bolt")

    def test_a_subtree_NEITHER_walk_can_read_is_refused_not_allowed(self, wire):
        # component_contains answers None when the subtree did not enumerate (the measured cause is
        # an unresolved external reference). Reading that as "no cycle" is what lets the illegal
        # instance through, so the guard refuses and says the question could not be answered.
        des = wire(names=("Outer", "Bolt"))
        bolt = des.comps["Bolt"]
        del bolt.allOccurrences                              # the fast walk raises...
        bolt.occurrences = _NamedCollection(raises=_UNREADABLE)     # ...and so does the fallback
        msg = error_message(ai.handler(component="Bolt", into_component="Outer:1"))
        assert "could not be determined" in msg and "not be searched to a verdict" in msg
        assert "unresolved external references" in msg          # the remedy that reaches the cause
        assert getattr(des, "added", None) is None   # the mutation was never reached


# ── placement + guards ───────────────────────────────────────────────────────

class TestPlacement:
    def test_translation_is_scaled_to_centimetres(self, wire):
        des = wire()
        payload(ai.handler(component="Bolt", x=10.0, y=0.0, z=5.0, units="mm"))
        assert des.last_transform.translation == ("vec", 1.0, 0.0, 0.5)

    def test_no_placement_leaves_the_transform_untouched(self, wire):
        des = wire()
        out = payload(ai.handler(component="Bolt"))
        assert des.last_transform.translation is None
        assert out["position"] == "origin"

    def test_rotation_is_about_the_named_world_axis(self, wire):
        des = wire()
        out = payload(ai.handler(component="Bolt", rotate_deg=90.0, rotate_axis="x"))
        angle, axis, origin = des.last_transform.rotation
        assert round(angle, 6) == round(3.141592653589793 / 2, 6)
        assert axis == ("vec", 1, 0, 0) and origin == ("pt", 0, 0, 0)
        assert out["rotate_axis"] == "x"

    def test_unknown_units_refused_before_mutating(self, wire):
        des = wire()
        assert "Unknown units" in error_message(ai.handler(component="Bolt", units="furlong"))
        assert getattr(des, "added", None) is None

    def test_unknown_rotate_axis_refused_before_mutating(self, wire):
        des = wire()
        msg = error_message(ai.handler(component="Bolt", rotate_deg=45.0, rotate_axis="w"))
        assert "Unknown rotate_axis 'w'" in msg
        assert getattr(des, "added", None) is None


class TestGuards:
    def test_missing_component_refused(self, wire):
        wire()
        assert "'component' is required" in error_message(ai.handler(component=""))

    def test_unknown_component_refused(self, wire):
        wire()
        assert "did not resolve" in error_message(ai.handler(component="Ghost"))

    def test_ambiguous_into_component_refused_without_instancing(self, wire):
        des = wire(names=("Outer", "Bolt"))
        _instance(des, des.comps["Outer"])
        msg = error_message(ai.handler(component="Bolt", into_component="Outer"))
        assert "ambiguous" in msg.lower()
        assert getattr(des, "added", None) is None

    def test_no_active_design_refused(self, monkeypatch):
        monkeypatch.setattr(ai._common, "design", lambda: None)
        monkeypatch.setattr(ai._inputs._common, "design", lambda: None)
        assert "No active design" in error_message(ai.handler(component="Bolt"))


class TestUnreadableStructure:
    """Each read between the resolved target and the mutation is guarded; an unreadable one names
    what could not be reached and stops BEFORE addExistingComponent, so no instance lands off a
    half-resolved target."""

    def test_an_occurrence_that_cannot_name_its_component_is_refused(self, wire):
        # An occurrence whose `component` read RAISES is an unresolved external reference. The
        # refusal must NOT read as "no such occurrence" / "did not resolve" - the instance exists and
        # the browser tree shows it; what is missing is the component behind it.
        des = wire()
        go_stale(des.tree[0], attrs=("component",))
        msg = error_message(ai.handler(component="Bolt:1"))
        assert "referenced component could not be loaded" in msg
        assert "Bolt:1" in msg
        assert "did not resolve" not in msg
        assert getattr(des, "added", None) is None

    def test_a_host_occurrence_that_cannot_name_its_component_is_refused(self, wire):
        des = wire(names=("Outer", "Bolt"))
        go_stale(des.tree[0], attrs=("component",))
        msg = error_message(ai.handler(component="Bolt", into_component="Outer:1"))
        assert "referenced component could not be loaded" in msg
        assert "Outer:1" in msg
        assert "no occurrence matching" not in msg
        assert getattr(des, "added", None) is None

    def test_an_occurrence_whose_component_reads_NONE_is_refused_before_the_add(self, wire):
        # Distinct from an unresolved external reference, where the read RAISES: here the read
        # SUCCEEDS and answers None, so the occurrence resolves normally and this guard - not the
        # resolver - is the only thing standing between a null component and addExistingComponent.
        des = wire()
        des.tree[0].component = None
        msg = error_message(ai.handler(component="Bolt:1"))
        assert "Could not reach the component behind 'Bolt:1'" in msg
        assert getattr(des, "added", None) is None

    def test_a_HOST_occurrence_whose_component_reads_NONE_is_refused(self, wire):
        des = wire(names=("Outer", "Bolt"))
        des.tree[0].component = None
        msg = error_message(ai.handler(component="Bolt", into_component="Outer:1"))
        assert "Occurrence 'Outer:1' has no component to instance into" in msg
        assert getattr(des, "added", None) is None

    def test_an_unreachable_root_is_refused(self, wire, monkeypatch):
        # the root answers while the target resolves and not when the tool reads it for the host -
        # the guard is what keeps that from being carried into addExistingComponent as None.
        des = wire()
        real = ai._COMPONENT.resolve

        def resolve_then_lose_the_root(raw):
            out = real(raw)
            des.rootComponent = None
            return out

        monkeypatch.setattr(ai._COMPONENT, "resolve", resolve_then_lose_the_root)
        msg = error_message(ai.handler(component="Bolt"))
        assert "Could not reach the root component to instance into" in msg
        assert getattr(des, "added", None) is None

    def test_a_host_with_no_occurrences_collection_is_refused(self, wire):
        des = wire()
        des.rootComponent.occurrences = None
        msg = error_message(ai.handler(component="Bolt"))
        assert "Could not access the occurrences of the root component" in msg
        assert len(des.tree) == 1

    def test_a_rotation_the_matrix_refuses_stops_the_call(self, wire, monkeypatch):
        # setToRotation returns a bool. Ignoring a false would place the instance UNROTATED while
        # the payload reported the angle - so the call stops instead.
        import adsk.core
        des = wire()
        refusing = _matrix()
        refusing.setToRotation = lambda angle, axis, origin: False
        monkeypatch.setattr(adsk.core.Matrix3D, "create", staticmethod(lambda: refusing))
        msg = error_message(ai.handler(component="Bolt", rotate_deg=45.0, rotate_axis="z"))
        assert "Could not build the placement rotation (45.0 deg about 'z')" in msg
        assert "the instance was not created" in msg
        assert getattr(des, "added", None) is None


class TestPlacementCannotBeCaptured:
    """A placed instance reads durable off the assembly tree, and measured, it SURVIVES a later joint
    in a design holding no captured position markers - but in a design that holds markers, two such
    placements came back at the ORIGIN after a joint creation. The placement also sets no
    pending-position flag, so capturing it is impossible (assembly_capture_position refuses). The
    payload states the risk and a remedy that exists; naming capture as the remedy would send the
    caller into a refusal."""

    def test_a_translated_instance_is_told_the_placement_cannot_be_captured(self, wire):
        wire()
        out = payload(ai.handler(component="Bolt", x=10.0, units="mm"))
        assert "cannot be captured" in out["note"]
        assert "sets no pending-position flag" in out["note"]

    def test_the_note_never_prescribes_capture(self, wire):
        # capture REFUSES for a placement ("Nothing to capture"), so prescribing it is a dead end
        wire()
        note = payload(ai.handler(component="Bolt", z=4.0))["note"]
        assert "action='capture'" not in note
        assert "action='discard_pending'" not in note

    def test_the_note_names_the_marker_condition_and_a_real_remedy(self, wire):
        wire()
        note = payload(ai.handler(component="Bolt", x=10.0))["note"]
        assert "HOLDS captured position markers" in note      # the condition it reverts under
        assert "ORIGIN" in note                               # where it reverts to
        assert "model_inspect" in note                        # the remedy that actually works

    def test_a_rotated_instance_gets_the_same_warning(self, wire):
        wire()
        assert "cannot be captured" in payload(ai.handler(component="Bolt", rotate_deg=90.0))["note"]

    def test_an_instance_at_the_origin_makes_no_placement_claim(self, wire):
        wire()
        out = payload(ai.handler(component="Bolt"))
        assert out["position"] == "origin"
        assert "cannot be captured" not in out["note"]


class TestHostPrefixes:
    """Which HOST instances received the new instance - the count the payload reports."""

    def test_a_path_without_the_new_instance_contributes_no_host(self):
        # the leftover paths (siblings, and the new instance own children) must not each add a
        # host, or a one-host add would report itself as having landed in several places.
        assert ai._host_prefixes(["Frame:1+Bolt:2", "Frame:1+Nut:1"], "Bolt:2") == {"Frame:1"}

    def test_a_top_level_instance_reports_the_root_as_its_host(self):
        assert ai._host_prefixes(["Bolt:2"], "Bolt:2") == {""}

    def test_each_host_instance_is_counted_once(self):
        assert ai._host_prefixes(
            ["Frame:1+Bolt:2", "Frame:2+Bolt:2", "Frame:1+Bolt:2+Washer:1"], "Bolt:2") == {
            "Frame:1", "Frame:2"}
