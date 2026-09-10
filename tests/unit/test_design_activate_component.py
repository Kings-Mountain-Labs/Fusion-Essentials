"""Unit tests for ``design_activate_component.py`` - the re-activate-an-existing-component primitive.

Pinned: an occurrence resolves by occurrence OR component name, an ambiguous name is REFUSED rather
than first-matched, an activate() that reports true but does not take is an error, and 'root'/''
returns the edit target to the root through Design.activateRootComponent.
"""

from conftest import (FakeOccurrence, MakeComp, MakeDesign, error_message, install, load_tool,
                      payload)

dm = load_tool("design_activate_component")


class _Occ(FakeOccurrence):
    """An occurrence placing a component of its own; an activation that TOOK is what the design
    reads back as its activeOccurrence."""

    _design = None

    def __init__(self, path, comp_name=None, component=None, **kw):
        # `component` places an EXISTING component, so two occurrences can share one - the shape
        # that makes the component name useless for telling an activation apart.
        super().__init__(path=path,
                         component=component if component is not None else MakeComp(name=comp_name),
                         **kw)

    def activate(self):
        did = super().activate()
        if self.isActive and self._design is not None:
            self._design.activeOccurrence = self
        return did


class _ActivateDesign(MakeDesign):
    """A design whose activeComponent follows the occurrence holding the edit target - the read an
    activation is judged by. The return-to-root knobs are MakeDesign's."""

    def __init__(self, occurrences, **kw):
        super().__init__(comp=MakeComp(name="RootComp", occurrences=occurrences), **kw)
        for occ in occurrences:
            occ._design = self

    @property
    def activeComponent(self):
        occ = self.activeOccurrence
        return occ.component if occ is not None else self.rootComponent

    @activeComponent.setter
    def activeComponent(self, value):
        # MakeDesign seeds the root here; this design derives the answer from the active occurrence.
        pass


class _UnreadableRootDesign(_ActivateDesign):
    """A design whose isRootComponentActive declines - a DECLARED worst case (live the read always
    answers), so an unconfirmed return to root cannot pass as an ok."""

    @property
    def isRootComponentActive(self):
        raise RuntimeError("3 : the active-component flag is unavailable")


class _UnreadableOccurrenceDesign(_ActivateDesign):
    """A design whose activeOccurrence read declines - the DECLARED worst case that separates the
    None the design answers at the root from a read that never answered."""

    @property
    def activeOccurrence(self):
        raise RuntimeError("3 : the active occurrence is unavailable")

    @activeOccurrence.setter
    def activeOccurrence(self, value):
        pass


class TestActivateComponent:
    def test_no_active_design(self):
        install(dm, None)
        assert "No active design" in error_message(dm.handler(occurrence="Chassis:1"))

    def test_activate_by_occurrence_name(self):
        occ = _Occ("Chassis:1", "Chassis")
        install(dm, _ActivateDesign([occ, _Occ("Wheel:1", "Wheel")]))
        out = payload(dm.handler(occurrence="Chassis:1"))
        assert occ.isActive is True
        assert out["activated"] == "Chassis:1" and out["component"] == "Chassis"
        assert out["active_component"] == "Chassis"
        assert out["active_occurrence"] == "Chassis:1"   # the INSTANCE the design reports back

    def test_a_sibling_instance_that_did_not_take_is_refused(self):
        # MEASURED: two occurrences of ONE component both read activeComponent.name 'Twin', so a
        # component-name check calls this a success while Twin:1 keeps the edit target.
        shared = MakeComp(name="Twin")
        first = _Occ("Twin:1", component=shared, is_active=True)
        second = _Occ("Twin:2", component=shared, activate_lies=True)
        des = _ActivateDesign([first, second], active_occurrence=first)
        install(dm, des)
        msg = error_message(dm.handler(occurrence="Twin:2"))
        assert "Twin:1" in msg and "not confirmed on that instance" in msg
        assert des.activeOccurrence is first

    def test_activate_by_component_name(self):
        occ = _Occ("Chassis:1", "Chassis")
        install(dm, _ActivateDesign([occ]))
        out = payload(dm.handler(occurrence="Chassis"))   # component name
        assert occ.isActive is True and out["activated"] == "Chassis:1"

    def test_activation_that_does_not_take_bites(self):
        # activate() returns true but the design still reports no active occurrence -> error, not ok
        occ = _Occ("Chassis:1", "Chassis", activate_lies=True)
        install(dm, _ActivateDesign([occ]))
        assert "not confirmed on that instance" in error_message(dm.handler(occurrence="Chassis:1"))

    def test_unknown_component_errors_and_lists(self):
        install(dm, _ActivateDesign([_Occ("Wheel:1", "Wheel")]))
        msg = error_message(dm.handler(occurrence="Ghost"))
        assert "Ghost" in msg and "Wheel:1" in msg

    def test_ambiguous_name_refused_not_first_match(self):
        # two instances share local name "Bolt:1" under different sub-assemblies - a bare "Bolt"
        # substring must ERROR (naming both fullPathNames), NOT silently activate the first.
        a = _Occ("Sub-A:1+Bolt:1", "Bolt")
        b = _Occ("Sub-B:1+Bolt:1", "Bolt")
        install(dm, _ActivateDesign([a, b]))
        msg = error_message(dm.handler(occurrence="Bolt"))
        assert "ambiguous" in msg.lower()
        assert "Sub-A:1+Bolt:1" in msg and "Sub-B:1+Bolt:1" in msg
        assert a.isActive is False and b.isActive is False

    def test_activate_root_via_empty(self):
        # the measured pair after activateRootComponent(): activeOccurrence null, root active
        occ = _Occ("Chassis:1", "Chassis", is_active=True)
        des = _ActivateDesign([occ], active_occurrence=occ)
        install(dm, des)
        out = payload(dm.handler(occurrence=""))
        assert out["activated"] == "root" and out["active_component"] == "RootComp"
        # both read-backs travel in the payload, not just the verdict they produced
        assert out["is_root_component_active"] is True and out["active_occurrence"] is None
        assert des.activeOccurrence is None and des.isRootComponentActive is True

    def test_root_activation_that_does_not_take_bites(self):
        # activateRootComponent() answers true while the child STILL holds the edit target - the
        # read-back is what catches it, and the error names the occurrence still active.
        occ = _Occ("Chassis:1", "Chassis", is_active=True)
        install(dm, _ActivateDesign([occ], active_occurrence=occ, root_activate_lies=True))
        msg = error_message(dm.handler(occurrence=""))
        assert "not confirmed at the root" in msg and "Chassis:1" in msg

    def test_an_unreadable_root_readback_is_refused(self):
        # neither read confirms the root, so the ok would rest on nothing - refuse positively
        occ = _Occ("Chassis:1", "Chassis", is_active=True)
        install(dm, _UnreadableRootDesign([occ], active_occurrence=occ))
        assert "not confirmed at the root" in error_message(dm.handler(occurrence=""))

    def test_a_child_still_active_is_refused_when_the_flag_says_root(self):
        # DECLARED worst case: isRootComponentActive answers True while activeOccurrence still holds
        # the child - the occurrence read is the only one that catches it.
        occ = _Occ("Chassis:1", "Chassis", is_active=True)
        install(dm, _ActivateDesign([occ], active_occurrence=occ, root_activate_lies=True,
                                    root_active_reads=True))
        msg = error_message(dm.handler(occurrence=""))
        assert "Chassis:1" in msg and "not confirmed at the root" in msg

    def test_an_unread_active_occurrence_is_not_read_as_the_root(self):
        # the design answering None and a read that DECLINED are different states; only the first
        # confirms the root, so the refusal names the second as unread.
        occ = _Occ("Chassis:1", "Chassis", is_active=True)
        install(dm, _UnreadableOccurrenceDesign([occ], root_active_reads=True))
        msg = error_message(dm.handler(occurrence=""))
        assert "unread" in msg and "not confirmed at the root" in msg

    def test_root_activation_returning_false_errors(self):
        occ = _Occ("Chassis:1", "Chassis", is_active=True)
        install(dm, _ActivateDesign([occ], active_occurrence=occ, root_activate_ok=False))
        assert "returned false" in error_message(dm.handler(occurrence="root"))

    def test_activate_returns_false_errors(self):
        occ = _Occ("Chassis:1", "Chassis", activate_ok=False)
        install(dm, _ActivateDesign([occ]))
        assert "returned false" in error_message(dm.handler(occurrence="Chassis:1"))
