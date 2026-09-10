"""Unit tests for assembly_edit_relations - the lifecycle of the three assembly relations.

Pinned: the kind/action matrix, name resolution (case-insensitive EXACT, a repeated name refused),
and every verification gate - a suppress re-read, a delete re-listed, set_occurrences refused
without reaching the platform call or the timeline, and setMotionData confirmed off
valueOne/valueTwo - in the native rad/cm the display-unit ratio converts to.

AssemblyConstraint has no shared conftest fake (no live SHAPES dump), so the constraint objects are
local; the rigid groups, motion links, timeline and design/component/occurrence skeleton come from
the shared fakes.
"""

import types

import pytest

import adsk.core
import adsk.fusion

from conftest import (_NamedCollection, error_message, FakeMotionLink, FakeMotionLinks,
                      FakeRigidGroup, FakeRigidGroups, FakeTimeline, FakeTimelineObject, go_stale,
                      install, load_tool, make_design, make_occurrence, MakeComp, MakeDesign,
                      payload)

rel = load_tool("assembly_edit_relations")
jt = load_tool("_joints")          # the ratio codec, to pin the payload against its own output

JMT = adsk.fusion.JointMotionTypes
REVOLUTE_DOF = JMT.RevoluteJointRotateMotionType
SLIDER_DOF = JMT.SliderJointSlideMotionType
FHS = adsk.fusion.FeatureHealthStates

# The rack-and-pinion ratio the unit conversion is measured on: this many degrees of pinion per
# millimetre of rack is 0.5 rad per cm, the pair Fusion actually couples on.
RIG_RATIO_DEG_PER_MM = 2.8647889757

# The read that yields no value at all, on any relation member.
_DELETED = "3 : An API Object refers to a deleted Object"


# ── the relation objects (the surface measured on the installed bindings) ───────────────────────

class _Constraint:
    """One assembly constraint - the relation surface with no live SHAPES dump, so it stays local:
    name, entityToken, isSuppressed, deleteMe() -> bool. `home` is the collection a successful
    delete removes it from, so a re-list actually stops finding it."""

    def __init__(self, name, token=None, suppressed=False, delete_ok=True, relationships=2):
        self.name = name
        self.entityToken = token or f"TOK_{name}"
        self.isSuppressed = suppressed
        self.geometricRelationships = _NamedCollection([object()] * relationships)
        self.delete_ok = delete_ok
        self.home = None

    def deleteMe(self):
        if self.delete_ok and self.home is not None:
            self.home._items.remove(self)
        return self.delete_ok


class _StickySuppress(_Constraint):
    """A constraint whose isSuppressed assignment is SWALLOWED - the platform accepting a set that
    never takes, which the read-back gate exists to catch."""

    @property
    def isSuppressed(self):
        return getattr(self, "_suppressed", False)

    @isSuppressed.setter
    def isSuppressed(self, value):
        self._suppressed = getattr(self, "_suppressed", False)


class _BlindSuppress:
    """isSuppressed accepts a write and RAISES on read - a flag that yields no verdict, which
    bool() would coerce into a confirmed False. Mixed into the two kinds that meet it."""

    @property
    def isSuppressed(self):
        raise RuntimeError(_DELETED)

    @isSuppressed.setter
    def isSuppressed(self, value):
        self._written = value


class _BlindGroup(_BlindSuppress, FakeRigidGroup):
    """The blind suppression flag on a rigid group."""


class _BlindConstraint(_BlindSuppress, _Constraint):
    """The blind suppression flag on an assembly constraint."""


class _IntFlagGroup(FakeRigidGroup):
    """A rigid group whose isSuppressed reads back as an INT rather than a bool - a truthy reading
    that is not True."""

    @property
    def isSuppressed(self):
        return int(self._flag)

    @isSuppressed.setter
    def isSuppressed(self, value):
        self._flag = bool(value)


class _RefusingGroup(FakeRigidGroup):
    """A rigid group whose setOccurrences RAISES, as measured on Fusion 2705.0.87: '3 : Cannot be
    edited before rolling back', with the members reading back unchanged after it. The shared fake
    records every attempt in _sets, so a test can prove the tool refuses WITHOUT reaching it."""

    def __init__(self, name, **kw):
        FakeRigidGroup.__init__(self, name=name, **kw)
        self.timelineObject = FakeTimelineObject(name=name)

    def setOccurrences(self, occurrences, include_children):
        self._sets.append((list(occurrences), bool(include_children)))
        raise RuntimeError("3 : Cannot be edited before rolling back")


class _BreakingGroup(FakeRigidGroup):
    """A rigid group whose suppress LANDS and puts `breaks` into error - the downstream feature a
    change costs, which the health diff around the write is what reports."""

    def __init__(self, name, breaks=None, **kw):
        self._breaks = None                    # armed only after the constructor's own write
        FakeRigidGroup.__init__(self, name=name, **kw)
        self._breaks = breaks

    @property
    def isSuppressed(self):
        return self._flag

    @isSuppressed.setter
    def isSuppressed(self, value):
        self._flag = value
        if self._breaks is not None:
            self._breaks.healthState = FHS.ErrorFeatureHealthState


class _BlindReverse(FakeMotionLink):
    """A motion link whose isReversed reads until it is WRITTEN and RAISES afterwards - the write
    lands where no read-back can confirm it."""

    def __init__(self, name, **kw):
        FakeMotionLink.__init__(self, name=name, **kw)
        self._blind = False

    @property
    def isReversed(self):
        if self._blind:
            raise RuntimeError(_DELETED)
        return self._rev

    @isReversed.setter
    def isReversed(self, value):
        self._rev = value
        self._blind = True


class _StickyReverse(FakeMotionLink):
    """A motion link whose isReversed assignment is SWALLOWED - the direction a flip, or the sign
    of a ratio, asks for never lands."""

    @property
    def isReversed(self):
        return getattr(self, "_rev", False)

    @isReversed.setter
    def isReversed(self, value):
        self._rev = getattr(self, "_rev", False)


class _FailedLink(FakeMotionLink):
    """A motion link whose entityToken RAISES - a token read that yields no value, which is one of
    the two readings that leave a doubled relation doubled (the other being two wrappers answering
    two different values; _relations states both). Every other read still answers, so nothing but
    id() tells two WRAPPERS of it apart, and id() differs per wrapper. `_EmptyTokenLink` is the
    same no-value reading arriving through a token that does NOT raise."""

    @property
    def entityToken(self):
        raise RuntimeError(_DELETED)


class _EmptyTokenLink(FakeMotionLink):
    """A motion link whose entityToken READS and answers the EMPTY STRING - the third reading, next
    to a raise and a None. An empty token carries no value, so it is no evidence that two rows are
    one relation: ``_common.native_identity`` refuses exactly this reading ('None when the token
    reads empty or not at all'), and the walk's de-dup has to refuse it the same way. Keyed on the
    string itself every relation reading it collapses onto ONE row, and the rest leave the walk with
    no name left to address them by."""

    @property
    def entityToken(self):
        return ""


class _NoComponentList(MakeDesign):
    """A design whose allComponents will not read - the walk degrades to the root alone."""

    @property
    def allComponents(self):
        raise RuntimeError("2 : InternalValidationError : allComponents")


class _NoRootDesign(MakeDesign):
    """A design whose rootComponent does not read at all."""

    def __init__(self, **kw):
        MakeDesign.__init__(self, **kw)
        go_stale(self, attrs=("rootComponent",))


def _link(name, cls=FakeMotionLink, **kw):
    """A motion link coupling two REVOLUTE degrees of freedom - the DOF pair every action but
    set_values is indifferent to, and the one a bare ratio re-values unconverted."""
    kw.setdefault("motion_one", REVOLUTE_DOF)
    kw.setdefault("motion_two", REVOLUTE_DOF)
    return cls(name=name, **kw)


def _occ(path):
    """One assembly occurrence at `path` - a rigid group's member, or a design occurrence."""
    return make_occurrence(path=path)


def _unreadable_items(count=1):
    """A collection that COUNTS but whose item(i) RAISES - a member the collection promises and
    cannot hand over. safe() turns that raise into None, which is the state every reader here has
    to skip rather than publish."""
    return _NamedCollection([None] * count, item_raises=_DELETED)


def _component(name, occurrences=(), rigid=(), links=(), constraints=()):
    # No entityToken: the walk keys on nothing a COMPONENT answers (it asks _common.all_components
    # for each component once), so a component's own identity is not this file's subject.
    comp = MakeComp(name=name, occurrences=list(occurrences))
    # The two shared collections drop a relation whose deleteMe() succeeded, so a re-list stops
    # finding it; the local constraint keeps the `home` back-pointer for the same effect.
    comp.rigidGroups = FakeRigidGroups(groups=rigid)
    comp.motionLinks = FakeMotionLinks(links=links)
    comp.assemblyConstraints = _NamedCollection(list(constraints))
    for c in constraints:
        c.home = comp.assemblyConstraints
    return comp


def _world(monkeypatch, rigid=(), links=(), constraints=(), occurrences=(), subs=(),
           timeline=None):
    """A design whose ROOT carries the given relations, plus optional sub-components. install()
    wires both design seams (the tool's own and _inputs') - a hand-patched single seam would leave
    OccurrenceRefList resolving against no design."""
    root = _component("Root", occurrences=occurrences, rigid=rigid, links=links,
                      constraints=constraints)
    design = make_design(comp=root, all_components=[root] + list(subs),
                         timeline=FakeTimeline() if timeline is None else timeline)
    install(rel, design)
    # A ValueInput carries its number as realValue, which is what setMotionData lands - so a raw
    # float handed straight to the platform would not read back here.
    monkeypatch.setattr(adsk.core.ValueInput, "createByReal",
                        staticmethod(lambda v: types.SimpleNamespace(realValue=v)))
    return design


@pytest.fixture
def world(monkeypatch):
    def _build(**kw):
        return _world(monkeypatch, **kw)
    return _build


def _doubled_root(name, subs=()):
    """A design whose ROOT is reachable through TWO wrappers - design.rootComponent, and the wrapper
    design.allComponents carries - each handing back its OWN wrapper of the same physical FAILED
    motion link. A walk that reads BOTH routes emits that one link twice; the two link wrappers
    share only a name, so nothing downstream can tell the pair back apart."""
    return make_design(comp=_component("Root", links=[_FailedLink(name)]),
                       all_components=[_component("Root", links=[_FailedLink(name)])] + list(subs))


# ── the walk + the resolver (tools/_relations.py, shared with assembly_get) ──────────────────────

class TestRelationWalk:
    def test_empty_design_lists_nothing(self, world):
        world()
        assert rel._relations.all_relations(rel._common.design(), "rigid_group") == []
        assert rel._relations.relation_names(rel._common.design(), "motion_link") == []

    def test_one_two_and_many_are_all_listed(self, world):
        for n in (1, 2, 5):
            design = world(rigid=[FakeRigidGroup(f"RG{i}") for i in range(n)])
            assert rel._relations.relation_names(design, "rigid_group") == [f"RG{i}" for i in range(n)]

    def test_subcomponent_relations_are_found(self, world):
        # a rigid group created inside a sub-assembly lives on THAT component - a root-only walk
        # would report it missing and the edit would refuse a relation that exists.
        sub = _component("Tower", rigid=[FakeRigidGroup("SubGroup")])
        design = world(rigid=[FakeRigidGroup("RootGroup")], subs=[sub])
        assert sorted(rel._relations.relation_names(design, "rigid_group")) == ["RootGroup", "SubGroup"]

    def test_two_readings_answering_one_token_are_listed_once(self):
        # the entityToken de-dup is the walk's SECOND line, over the relation objects: two readings
        # that answer ONE token are one relation, however they were reached. Which live shape hands
        # one relation back through two components is unmeasured - see all_relations - so this pins
        # the line's behaviour, not an assembly anyone has read.
        design = make_design(comp=_component("Root", rigid=[FakeRigidGroup("RG1", entity_token="T")]),
                             all_components=[_component("Root", rigid=[FakeRigidGroup("RG1", entity_token="T")]),
                                             _component("Tower", rigid=[FakeRigidGroup("RG1", entity_token="T")])])
        assert rel._relations.relation_names(design, "rigid_group") == ["RG1"]

    def test_two_relations_reading_an_empty_token_are_both_listed_and_both_addressable(self, world):
        # THE BOUNDARY of the de-dup key, against the one-character token above: "T" is a value, so
        # two readings of it are one relation; "" is a reading that carries no value, so keying on
        # it merges two DISTINCT relations onto one row. Beta then leaves all_relations entirely and
        # no name reaches it - the silent DROP, which is strictly worse than the id() over-count the
        # fallback trades for. _common.native_identity refuses the same reading.
        sub = _component("Tower", links=[_EmptyTokenLink("Beta")])
        design = world(links=[_EmptyTokenLink("Alpha")], subs=[sub])
        assert sorted(rel._relations.relation_names(design, "motion_link")) == ["Alpha", "Beta"]
        assert len(rel._relations.all_relations(design, "motion_link")) == 2
        obj, comp, err = rel._relations.find_relation(design, "motion_link", "Beta")
        assert err is None and obj.name == "Beta" and comp.name == "Tower"

    def test_a_failed_root_relation_is_listed_once_not_once_per_root_wrapper(self, world):
        # the root answers as design.rootComponent AND inside design.allComponents, as two distinct
        # wrappers. Prepending the first to the second reads the one link twice, and the token
        # de-dup cannot collapse the pair: a token that yields no value keys on id(), which two
        # wrappers of one link never share. Asking only the collection is what keeps the row single.
        world()
        design = install(rel, _doubled_root("Motion Link1"))
        assert rel._relations.relation_names(design, "motion_link") == ["Motion Link1"]
        assert len(rel._relations.all_relations(design, "motion_link")) == 1

    def test_a_design_whose_component_collection_does_not_read_still_reads_the_roots_relations(self):
        # _common.all_components degrades to [root] when design.allComponents will not read; a walk
        # that read that collection directly would answer nothing for a design holding relations.
        design = _NoComponentList(comp=_component("Root", links=[_link("RootLink")]))
        assert rel._relations.relation_names(design, "motion_link") == ["RootLink"]

    def test_a_design_whose_root_does_not_read_yields_nothing(self):
        # the other end of that walk's contract: with no readable rootComponent there is no design
        # to trust, and all_components answers [] rather than mining a collection whose owner did
        # not read. Nothing is published from such a design, in place of a partial list - so an
        # unreadable root answers here exactly as a design holding no relations does.
        design = _NoRootDesign(all_components=[_component("Tower", links=[_link("SubLink")])])
        assert rel._relations.relation_names(design, "motion_link") == []

    def test_a_relation_the_collection_will_not_hand_over_is_skipped_not_paired_as_none(self, world):
        # the collection counts one and hands over nothing; that row is dropped here, not carried
        # out as (None, component) for every consumer to read name and health fields off.
        design = world(links=[_link("ML1")])
        design.rootComponent.motionLinks = _unreadable_items(1)
        assert rel._relations.all_relations(design, "motion_link") == []

    def test_kinds_do_not_bleed_into_each_other(self, world):
        design = world(rigid=[FakeRigidGroup("RG1")], links=[_link("ML1")],
                       constraints=[_Constraint("AC1")])
        assert rel._relations.relation_names(design, "rigid_group") == ["RG1"]
        assert rel._relations.relation_names(design, "motion_link") == ["ML1"]
        assert rel._relations.relation_names(design, "constraint") == ["AC1"]

    def test_name_match_is_exact_not_a_prefix(self, world):
        # a substring/prefix resolver would hand back RigidGroup1 for 'Rigid' - the wrong-entity bug.
        design = world(rigid=[FakeRigidGroup("RigidGroup1")])
        obj, _comp, err = rel._relations.find_relation(design, "rigid_group", "Rigid")
        assert obj is None and "No rigid group named 'Rigid'" in err and "RigidGroup1" in err

    def test_name_match_is_case_insensitive(self, world):
        design = world(rigid=[FakeRigidGroup("RigidGroup1")])
        obj, _comp, err = rel._relations.find_relation(design, "rigid_group", "rigidgroup1")
        assert err is None and obj.name == "RigidGroup1"

    def test_rigid_group_members_are_bounded_and_report_the_true_total(self, world):
        rg = FakeRigidGroup("RG1", occurrences=[_occ(f"P{i}:1") for i in range(20)])
        world(rigid=[rg])
        names, total = rel._relations.rigid_group_members(rg, 12)
        assert total == 20 and len(names) == 12 and names[0] == "P0:1"

    def test_a_member_that_does_not_read_is_skipped_while_the_total_still_counts_it(self, world):
        # a member the collection will not hand over has no name to publish; appending it anyway
        # would put a null in the member list. The TOTAL still reports what the group claims.
        rg = FakeRigidGroup("RG1")
        rg.occurrences = _unreadable_items(3)
        world(rigid=[rg])
        assert rel._relations.rigid_group_members(rg) == ([], 3)


class TestAFailedRelationStaysAddressable:
    """A relation the token de-dup cannot collapse is still ONE relation: it resolves by name and
    the edit tool acts on it. Emitted twice by the walk it refuses its own name as ambiguous, and a
    failed relation is exactly the one a caller needs to suppress or delete."""

    def test_find_relation_resolves_it_instead_of_refusing_itself(self, world):
        world()
        design = install(rel, _doubled_root("Motion Link1"))
        obj, comp, err = rel._relations.find_relation(design, "motion_link", "Motion Link1")
        assert err is None
        assert obj.name == "Motion Link1" and comp.name == "Root"

    def test_the_edit_tool_acts_on_it_rather_than_refusing_the_name(self, world):
        world()
        install(rel, _doubled_root("Motion Link1"))
        out = payload(rel.handler(kind="motion_link", name="Motion Link1", action="suppress"))
        assert out["name"] == "Motion Link1" and out["is_suppressed"] is True

    def test_two_failed_relations_sharing_a_name_are_still_refused(self, world):
        # the walk collapses ONE relation reached twice, never two relations that merely share a
        # name - which is the refusal that keeps an edit off the wrong assembly's link.
        world()
        sub = _component("Tower", links=[_FailedLink("Motion Link1")])
        install(rel, _doubled_root("Motion Link1", subs=[sub]))
        msg = error_message(rel.handler(kind="motion_link", name="Motion Link1", action="suppress"))
        assert "names 2 motion links" in msg and "Root" in msg and "Tower" in msg


class TestGuards:
    def test_unknown_kind_is_refused(self, world):
        world()
        assert "kind" in error_message(rel.handler(kind="bolt", name="X", action="delete"))

    def test_unknown_action_is_refused(self, world):
        world(rigid=[FakeRigidGroup("RG1")])
        assert "action" in error_message(rel.handler(kind="rigid_group", name="RG1", action="melt"))

    def test_action_not_supported_by_the_kind_names_what_is(self, world):
        # set_occurrences has no meaning for a motion link; the refusal must list the real actions
        # rather than fall through and act on the wrong property.
        world(links=[_link("ML1")])
        msg = error_message(rel.handler(kind="motion_link", name="ML1", action="set_occurrences"))
        assert "does not apply to a motion link" in msg and "set_values" in msg

    def test_constraint_refuses_the_motion_link_actions(self, world):
        world(constraints=[_Constraint("AC1")])
        msg = error_message(rel.handler(kind="constraint", name="AC1", action="reverse"))
        assert "assembly constraint" in msg and "delete" in msg

    def test_missing_name_is_refused(self, world):
        world(rigid=[FakeRigidGroup("RG1")])
        msg = error_message(rel.handler(kind="rigid_group", name="", action="delete"))
        assert "'name' is required" in msg

    def test_unknown_name_lists_what_exists(self, world):
        world(rigid=[FakeRigidGroup("RG1"), FakeRigidGroup("RG2")])
        msg = error_message(rel.handler(kind="rigid_group", name="Ghost", action="delete"))
        assert "Ghost" in msg and "RG1" in msg and "RG2" in msg

    def test_duplicate_name_across_components_is_refused_unmutated(self, world):
        # two sub-assemblies can each hold a 'RigidGroup1'; deleting the first hit would destroy the
        # wrong assembly's group.
        a = FakeRigidGroup("RigidGroup1", entity_token="A")
        b = FakeRigidGroup("RigidGroup1", entity_token="B")
        sub = _component("Tower", rigid=[b])
        design = world(rigid=[a], subs=[sub])
        msg = error_message(rel.handler(kind="rigid_group", name="RigidGroup1", action="delete"))
        assert "names 2 rigid groups" in msg and "Root" in msg and "Tower" in msg
        # neither was deleted - a deleted group leaves its collection's walk
        assert design.rootComponent.rigidGroups.count == 1 and sub.rigidGroups.count == 1

    def test_no_active_design(self, monkeypatch):
        monkeypatch.setattr(rel._common, "design", lambda: None)
        monkeypatch.setattr(rel._inputs._common, "design", lambda: None)
        msg = error_message(rel.handler(kind="rigid_group", name="RG1", action="delete"))
        assert "design" in msg.lower()


class TestFlagContract:
    """_flag is the single reader behind all three read-back gates: it answers a real bool, or None
    when the flag gave no verdict. Both arms are load-bearing - None is what the gates refuse on,
    and the bool is what reaches the wire."""

    def test_a_flag_that_reads_none_is_no_verdict(self):
        # a member that READS None never raises, so a raise-only sentinel would call it a False.
        assert rel._flag(types.SimpleNamespace(isSuppressed=None), "isSuppressed") is None

    def test_a_flag_that_raises_is_no_verdict(self):
        assert rel._flag(_BlindGroup("RG1"), "isSuppressed") is None

    def test_a_readable_flag_answers_a_real_bool(self):
        assert rel._flag(types.SimpleNamespace(isSuppressed=False), "isSuppressed") is False

    def test_a_truthy_non_bool_reading_lands_as_true(self, world):
        # a flag read back as 1 must publish true, not 1: the payload is the caller's boolean, and
        # an unnormalized reading also invites a spurious mismatch against the requested value.
        rg = _IntFlagGroup("RG1", suppressed=True)
        world(rigid=[rg])
        out = payload(rel.handler(kind="rigid_group", name="RG1", action="suppress"))
        assert out["is_suppressed"] is True and out["was_suppressed"] is True


class TestSuppress:
    def test_suppress_sets_and_reports_both_states(self, world):
        rg = FakeRigidGroup("RG1")
        world(rigid=[rg])
        out = payload(rel.handler(kind="rigid_group", name="RG1", action="suppress"))
        assert rg.isSuppressed is True
        assert out["is_suppressed"] is True and out["was_suppressed"] is False

    def test_unsuppress_clears_it(self, world):
        ml = _link("ML1", suppressed=True)
        world(links=[ml])
        out = payload(rel.handler(kind="motion_link", name="ML1", action="unsuppress"))
        assert ml.isSuppressed is False
        assert out["is_suppressed"] is False and out["was_suppressed"] is True

    def test_a_swallowed_suppress_is_an_error_not_a_false_ok(self, world):
        world(constraints=[_StickySuppress("AC1")])
        msg = error_message(rel.handler(kind="constraint", name="AC1", action="suppress"))
        assert "did not take" in msg

    def test_a_suppress_that_breaks_a_feature_reports_it(self, world):
        # timeline health is diffed around the set: a downstream feature newly in error is named,
        # instead of an ok that hides the breakage.
        extrude = FakeTimelineObject(name="Extrude1")
        world(rigid=[_BreakingGroup("RG1", breaks=extrude)],
              timeline=FakeTimeline(items=[extrude]))
        out = payload(rel.handler(kind="rigid_group", name="RG1", action="suppress"))
        assert out["timeline_errors_after"] == ["Extrude1"]
        assert "Extrude1" in out["note"]

    def test_an_unreadable_prior_flag_reports_null_not_false(self, world):
        # False would claim the relation WAS unsuppressed; null says the prior state is unknown.
        rg = FakeRigidGroup("RG1")
        del rg.isSuppressed
        world(rigid=[rg])
        out = payload(rel.handler(kind="rigid_group", name="RG1", action="suppress"))
        assert out["was_suppressed"] is None and out["is_suppressed"] is True

    def test_an_unreadable_flag_after_unsuppress_is_not_a_confirmed_false(self, world):
        # unsuppress asks for False, and bool(unreadable) is False too - so a flag that cannot be
        # read passes a bool() gate and publishes "isSuppressed now reads False" as a measurement.
        world(rigid=[_BlindGroup("RG1")])
        msg = error_message(rel.handler(kind="rigid_group", name="RG1", action="unsuppress"))
        assert "isSuppressed cannot be read" in msg and "UNCONFIRMED" in msg

    def test_an_unreadable_flag_after_suppress_is_refused_too(self, world):
        world(constraints=[_BlindConstraint("AC1")])
        msg = error_message(rel.handler(kind="constraint", name="AC1", action="suppress"))
        assert "isSuppressed cannot be read" in msg and "UNCONFIRMED" in msg

    def test_the_note_is_per_kind_and_claims_only_what_was_observed(self, world):
        # a motion link COUPLES MOTION - it holds no parts - and what suppression does to an
        # assembly constraint's relationships is unmeasured, so neither note may assert it.
        world(rigid=[FakeRigidGroup("RG1")], links=[_link("ML1")],
              constraints=[_Constraint("AC1")])
        rg_note = payload(rel.handler(kind="rigid_group", name="RG1", action="suppress"))["note"]
        ml_note = payload(rel.handler(kind="motion_link", name="ML1", action="suppress"))["note"]
        ac_note = payload(rel.handler(kind="constraint", name="AC1", action="suppress"))["note"]
        assert len({rg_note, ml_note, ac_note}) == 3
        assert "parts" not in ml_note and "couples" in ml_note
        assert "not measured" in ac_note
        assert all("isSuppressed now reads True" in n for n in (rg_note, ml_note, ac_note))


class TestDelete:
    def test_delete_removes_it_and_confirms_by_re_listing(self, world):
        rg = FakeRigidGroup("RG1")
        design = world(rigid=[rg, FakeRigidGroup("RG2")])
        out = payload(rel.handler(kind="rigid_group", name="RG1", action="delete"))
        assert out["deleted"] is True and out["remaining"] == 1
        assert rel._relations.relation_names(design, "rigid_group") == ["RG2"]

    def test_a_declined_delete_is_an_error(self, world):
        rg = FakeRigidGroup("RG1", delete_ok=False)
        world(rigid=[rg])
        msg = error_message(rel.handler(kind="rigid_group", name="RG1", action="delete"))
        assert "declined" in msg and "deleteMe returned false" in msg

    def test_a_survivor_after_a_true_delete_is_an_error(self, world):
        # deleteMe() returning true while the relation is still listed is the swallowed no-op the
        # re-list exists to catch.
        rg = FakeRigidGroup("RG1")
        rg.deleteMe = lambda: True                 # says yes, removes nothing
        world(rigid=[rg])
        msg = error_message(rel.handler(kind="rigid_group", name="RG1", action="delete"))
        assert "still listed" in msg

    def test_motion_link_and_constraint_delete_the_same_way(self, world):
        ml, ac = _link("ML1"), _Constraint("AC1")
        design = world(links=[ml], constraints=[ac])
        assert payload(rel.handler(kind="motion_link", name="ML1", action="delete"))["deleted"] is True
        assert payload(rel.handler(kind="constraint", name="AC1", action="delete"))["deleted"] is True
        assert rel._relations.relation_names(design, "motion_link") == []
        assert rel._relations.relation_names(design, "constraint") == []

    def test_delete_reports_a_feature_it_broke(self, world):
        joint = FakeTimelineObject(name="Joint1")
        rg = FakeRigidGroup("RG1")
        original = rg.deleteMe

        def delete_and_break():
            joint.healthState = FHS.ErrorFeatureHealthState
            return original()
        rg.deleteMe = delete_and_break
        world(rigid=[rg], timeline=FakeTimeline(items=[joint]))
        out = payload(rel.handler(kind="rigid_group", name="RG1", action="delete"))
        assert out["timeline_errors_after"] == ["Joint1"]
        assert "Joint1" in out["note"]

    def test_the_note_points_at_the_kinds_own_create_tool(self, world):
        world(rigid=[FakeRigidGroup("RG1")], links=[_link("ML1")],
              constraints=[_Constraint("AC1")])
        for kind, name, tool_name in (("rigid_group", "RG1", "assembly_rigid_group"),
                                      ("motion_link", "ML1", "joint_motion_link"),
                                      ("constraint", "AC1", "assembly_constrain")):
            out = payload(rel.handler(kind=kind, name=name, action="delete"))
            assert tool_name in out["note"] and "no longer listed" in out["note"]
            assert "readable" in out["note"]        # 'remaining' counts readable names only


class TestSetOccurrencesIsRefused:
    """setOccurrences is unusable on this Fusion build, so the action answers with the measured
    failure and the path that works - and must reach NEITHER the platform call nor the timeline on
    its way there."""

    def _group(self):
        return _RefusingGroup("RG1", occurrences=[_occ("Frame:1"), _occ("Carrier:1")])

    def test_refused_without_touching_the_platform_or_the_timeline(self, world):
        rg = self._group()
        tl = FakeTimeline()
        world(rigid=[rg], occurrences=[_occ("Frame:1"), _occ("Carrier:1"), _occ("Pedestal:1")],
              timeline=tl)
        error_message(rel.handler(kind="rigid_group", name="RG1", action="set_occurrences",
                                  occurrences=["Frame:1", "Pedestal:1"]))
        assert rg._sets == []                            # the platform call is never attempted
        assert rg.timelineObject._rolls == []            # nothing rolls
        assert tl._moves == []                           # and no marker restore is needed
        assert [o.fullPathName for o in rg.occurrences] == ["Frame:1", "Carrier:1"]

    def test_the_refusal_quotes_the_measured_failure_and_pins_it_to_the_build(self, world):
        # The refusal is a platform fact with a build attached: quoting a raise measured on an older
        # build as if it were this one's is the provenance defect. The message names the build it was
        # measured on and the sentence that build actually raises.
        world(rigid=[self._group()], occurrences=[_occ("Frame:1"), _occ("Carrier:1")])
        msg = error_message(rel.handler(kind="rigid_group", name="RG1", action="set_occurrences",
                                        occurrences=["Frame:1", "Carrier:1"]))
        assert "Cannot be edited before rolling back" in msg
        assert "2705.0.87" in msg
        assert "2704" not in msg
        assert "members read back unchanged" in msg

    def test_the_refusal_names_the_workaround(self, world):
        world(rigid=[self._group()], occurrences=[_occ("Frame:1"), _occ("Carrier:1")])
        msg = error_message(rel.handler(kind="rigid_group", name="RG1", action="set_occurrences",
                                        occurrences=["Frame:1", "Carrier:1"]))
        assert "action='delete'" in msg and "assembly_rigid_group" in msg
        assert "Nothing was changed" in msg

    def test_refused_before_the_group_or_its_occurrences_are_resolved(self, world):
        # neither an unknown group nor an unresolvable occurrence changes the answer - resolving
        # first would only put a second failure in front of the teaching.
        world(rigid=[self._group()], occurrences=[_occ("Frame:1")])
        for name, occs in (("Ghost", ["Frame:1", "Carrier:1"]), ("RG1", ["Ghost:1"]), ("RG1", None)):
            msg = error_message(rel.handler(kind="rigid_group", name=name,
                                            action="set_occurrences", occurrences=occs))
            assert "set_occurrences' is refused" in msg

    def test_the_action_is_still_offered_so_the_agent_is_taught(self, world):
        # keeping it in the kind's action set is what turns a dead end into a teaching moment: the
        # kind/action refusal for a motion link must still LIST it for a rigid group.
        world(links=[_link("ML1")])
        msg = error_message(rel.handler(kind="motion_link", name="ML1", action="set_occurrences"))
        assert "does not apply to a motion link" in msg
        assert "set_occurrences" in rel._KIND_ACTIONS["rigid_group"]


class TestReverse:
    def test_reverse_flips_and_reports_both_states(self, world):
        ml = _link("ML1", reversed_link=False)
        world(links=[ml])
        out = payload(rel.handler(kind="motion_link", name="ML1", action="reverse"))
        assert ml.isReversed is True
        assert out["reversed"] is True and out["was_reversed"] is False

    def test_reverse_flips_back(self, world):
        ml = _link("ML1", reversed_link=True)
        world(links=[ml])
        out = payload(rel.handler(kind="motion_link", name="ML1", action="reverse"))
        assert ml.isReversed is False and out["reversed"] is False

    def test_an_unreadable_direction_is_refused(self, world):
        ml = _link("ML1")
        del ml.isReversed
        world(links=[ml])
        assert "no direction to flip" in error_message(
            rel.handler(kind="motion_link", name="ML1", action="reverse"))

    def test_an_unreadable_direction_after_the_flip_is_not_a_confirmed_forward(self, world):
        # flipping a REVERSED link asks for False, and bool(unreadable) is False - so an unreadable
        # flag agrees with the request and the payload would claim the link now runs forward.
        ml = _BlindReverse("ML1", reversed_link=True)
        world(links=[ml])
        msg = error_message(rel.handler(kind="motion_link", name="ML1", action="reverse"))
        assert "isReversed cannot be read" in msg and "UNCONFIRMED" in msg

    def test_a_swallowed_flip_is_an_error(self, world):
        ml = _link("ML1", cls=_StickyReverse)
        world(links=[ml])
        msg = error_message(rel.handler(kind="motion_link", name="ML1", action="reverse"))
        assert "did not take" in msg


class TestSetValues:
    def test_ratio_reaches_setmotiondata_with_the_links_own_dofs(self, world):
        # the coupled DOF must be re-passed off the link (motionOne/motionTwo) - guessing one here
        # would silently re-couple a different degree of freedom. This pair is rotation-to-slide, so
        # the ratio also converts: 2 mm of travel per degree is 11.4591559026 cm per radian.
        ml = _link("ML1", motion_one=REVOLUTE_DOF, motion_two=SLIDER_DOF)
        world(links=[ml])
        out = payload(rel.handler(kind="motion_link", name="ML1", action="set_values", ratio=2.0))
        m1, v1, m2, v2, _rev = ml._motion_data[-1]
        assert m1 == REVOLUTE_DOF and m2 == SLIDER_DOF
        assert v1.realValue == 1.0
        assert v2.realValue == pytest.approx(11.4591559026, abs=1e-9)
        assert out["ratio"] == 2.0 and out["value_one"] == 1.0
        assert out["value_two"] == pytest.approx(11.4591559026, abs=1e-9)
        assert out["reversed"] is False

    def test_a_same_dof_link_re_values_at_the_bare_ratio(self, world):
        # BACK-COMPAT: deg-to-deg cancels, so a rev/rev link is re-valued with exactly the number
        # the caller passed - the pair every existing caller of this action sends.
        ml = _link("ML1", motion_one=REVOLUTE_DOF, motion_two=REVOLUTE_DOF)
        world(links=[ml])
        out = payload(rel.handler(kind="motion_link", name="ML1", action="set_values", ratio=2.0))
        assert ml._motion_data[-1][1].realValue == 1.0
        assert ml._motion_data[-1][3].realValue == 2.0
        assert out["value_one"] == 1.0 and out["value_two"] == 2.0
        assert out["ratio_units"] == "deg of joint_two per deg of joint_one"
        assert out["value_units"] == "value_one in rad, value_two in rad"

    def test_the_rack_and_pinion_ratio_re_values_in_native_units(self, world):
        # THE MEASURED PAIR, on the re-value path: 2.8647889757 deg of pinion per mm of rack IS
        # 0.5 rad per cm. The read-back gate below compares against that CONVERTED number - against
        # the raw display number it would reject the correct coupling as "did not take".
        ml = _link("ML1", motion_one=SLIDER_DOF, motion_two=REVOLUTE_DOF)
        world(links=[ml])
        out = payload(rel.handler(kind="motion_link", name="ML1", action="set_values",
                                  ratio=-RIG_RATIO_DEG_PER_MM))
        assert ml._motion_data[-1][3].realValue == pytest.approx(0.5, abs=1e-9)
        assert ml._motion_data[-1][4] is True               # the sign is still the reversal flag
        assert out["ratio"] == -RIG_RATIO_DEG_PER_MM       # the caller's own number, unscaled
        assert out["value_two"] == pytest.approx(0.5, abs=1e-9)
        assert out["ratio_units"] == "deg of joint_two per mm of joint_one"
        assert out["value_units"] == "value_one in cm, value_two in rad"
        assert "2.8647889757 deg of joint_two per 1 mm of joint_one" in out["interpreted"]

    def test_the_published_facts_are_the_codecs_own(self, world):
        # PARITY with the create path (joint_motion_link): both writers publish exactly what the one
        # codec returned for the same DOF pair and ratio, so a divergence between them reds here.
        ml = _link("ML1", motion_one=SLIDER_DOF, motion_two=REVOLUTE_DOF)
        world(links=[ml])
        out = payload(rel.handler(kind="motion_link", name="ML1", action="set_values",
                                  ratio=RIG_RATIO_DEG_PER_MM))
        facts = jt.link_ratio_values(SLIDER_DOF, REVOLUTE_DOF, RIG_RATIO_DEG_PER_MM)[2]
        assert {k: out[k] for k in facts} == facts

    def test_a_dof_that_answers_no_unit_is_sent_unconverted_and_says_so(self, world):
        # a link whose coupled DOF is outside the rotate/slide tables establishes no display unit,
        # so the magnitude goes out as given and the payload withholds both unit names.
        ml = _link("ML1", motion_one=object(), motion_two=REVOLUTE_DOF)
        world(links=[ml])
        out = payload(rel.handler(kind="motion_link", name="ML1", action="set_values",
                                  ratio=RIG_RATIO_DEG_PER_MM))
        assert ml._motion_data[-1][3].realValue == RIG_RATIO_DEG_PER_MM
        assert out["ratio_units"] is None and out["value_units"] is None
        assert "NO unit conversion" in out["interpreted"]

    def test_a_read_back_off_the_converted_value_by_float_noise_still_passes(self, world):
        # the converted number carries a float tail (0.5000000000080081 for the rig ratio); a
        # platform storing the clean 0.5 differs only in that tail, and the band must not call it a
        # failed set.
        ml = _link("ML1", motion_one=SLIDER_DOF, motion_two=REVOLUTE_DOF)

        def rounded(m1, v1, m2, v2, r):
            ml.valueOne = types.SimpleNamespace(value=1.0)
            ml.valueTwo = types.SimpleNamespace(value=0.5)
            ml.isReversed = r
            return True
        ml.setMotionData = rounded
        world(links=[ml])
        out = payload(rel.handler(kind="motion_link", name="ML1", action="set_values",
                                  ratio=RIG_RATIO_DEG_PER_MM))
        assert out["value_two"] == 0.5

    def test_a_read_back_that_holds_the_DISPLAY_number_is_a_failed_set(self, world):
        # the other side of that band: a link left holding the raw display number (5.7x the native
        # coupling) has not taken the ratio, and the error names the native value expected.
        ml = _link("ML1", motion_one=SLIDER_DOF, motion_two=REVOLUTE_DOF)

        def raw(m1, v1, m2, v2, r):
            ml.valueOne = types.SimpleNamespace(value=1.0)
            ml.valueTwo = types.SimpleNamespace(value=RIG_RATIO_DEG_PER_MM)
            ml.isReversed = r
            return True
        ml.setMotionData = raw
        world(links=[ml])
        msg = error_message(rel.handler(kind="motion_link", name="ML1", action="set_values",
                                        ratio=RIG_RATIO_DEG_PER_MM))
        assert "did not take" in msg and "0.5000000000080081" in msg

    def test_a_negative_ratio_reverses_with_the_magnitude(self, world):
        ml = _link("ML1")
        world(links=[ml])
        out = payload(rel.handler(kind="motion_link", name="ML1", action="set_values", ratio=-3))
        assert ml._motion_data[-1][3].realValue == 3.0 and ml._motion_data[-1][4] is True
        assert out["reversed"] is True

    def test_zero_and_non_numeric_ratios_are_refused(self, world):
        ml = _link("ML1")
        world(links=[ml])
        assert "non-zero" in error_message(
            rel.handler(kind="motion_link", name="ML1", action="set_values", ratio=0))
        assert "must be a number" in error_message(
            rel.handler(kind="motion_link", name="ML1", action="set_values", ratio="banana"))
        assert ml._motion_data == []

    def test_unreadable_coupled_motions_are_refused_before_setting(self, world):
        ml = _link("ML1")
        del ml.motionTwo
        world(links=[ml])
        msg = error_message(rel.handler(kind="motion_link", name="ML1", action="set_values", ratio=2))
        assert "coupled motions" in msg and ml._motion_data == []

    def test_a_declined_set_is_an_error(self, world):
        ml = _link("ML1", set_motion_ok=False)
        world(links=[ml])
        assert "setMotionData returned false" in error_message(
            rel.handler(kind="motion_link", name="ML1", action="set_values", ratio=2))

    def test_a_ratio_that_did_not_take_is_an_error(self, world):
        # true from setMotionData while the parameters still read 1:1 - the read-back gate.
        ml = _link("ML1")
        ml.setMotionData = lambda m1, v1, m2, v2, r: True      # accepts, changes nothing
        world(links=[ml])
        msg = error_message(rel.handler(kind="motion_link", name="ML1", action="set_values", ratio=4))
        assert "did not take" in msg and "1.0:1.0" in msg

    def test_the_ratio_is_compared_not_the_raw_values(self, world):
        # a platform that stores the coupling scaled (2:8 for a requested 4) still expresses the
        # requested RATIO - comparing raw values would reject a correct link.
        ml = _link("ML1")

        def scaled(m1, v1, m2, v2, r):
            ml.valueOne = types.SimpleNamespace(value=2.0)
            ml.valueTwo = types.SimpleNamespace(value=8.0)
            ml.isReversed = r
            return True
        ml.setMotionData = scaled
        world(links=[ml])
        out = payload(rel.handler(kind="motion_link", name="ML1", action="set_values", ratio=4))
        assert out["value_one"] == 2.0 and out["value_two"] == 8.0

    def test_a_zero_first_value_is_an_error(self, world):
        ml = _link("ML1")

        def zeroed(m1, v1, m2, v2, r):
            ml.valueOne = types.SimpleNamespace(value=0.0)
            ml.valueTwo = types.SimpleNamespace(value=2.0)
            return True
        ml.setMotionData = zeroed
        world(links=[ml])
        assert "no coupling at all" in error_message(
            rel.handler(kind="motion_link", name="ML1", action="set_values", ratio=2))

    def test_unreadable_values_after_the_set_are_an_error(self, world):
        ml = _link("ML1")

        def blind(m1, v1, m2, v2, r):
            del ml.valueTwo
            return True
        ml.setMotionData = blind
        world(links=[ml])
        assert "nothing confirms" in error_message(
            rel.handler(kind="motion_link", name="ML1", action="set_values", ratio=2))

    def test_a_direction_that_did_not_take_is_an_error(self, world):
        ml = _link("ML1", cls=_StickyReverse)
        world(links=[ml])
        msg = error_message(rel.handler(kind="motion_link", name="ML1", action="set_values", ratio=-2))
        assert "isReversed" in msg and "did not take" in msg

    def test_an_unreadable_direction_after_the_re_value_is_an_error(self, world):
        # a positive ratio asks for isReversed=False, which bool(unreadable) matches - the ratio
        # landed, but the direction the sign SETS is unconfirmed and must not be published as False.
        ml = _link("ML1")

        def blind_direction(m1, v1, m2, v2, r):
            ml.valueOne = types.SimpleNamespace(value=1.0)
            ml.valueTwo = types.SimpleNamespace(value=2.0)
            del ml.isReversed
            return True
        ml.setMotionData = blind_direction
        world(links=[ml])
        msg = error_message(rel.handler(kind="motion_link", name="ML1", action="set_values", ratio=2))
        assert "isReversed cannot be read back" in msg and "UNCONFIRMED" in msg

    def test_a_positive_ratio_clears_an_existing_reversal_and_says_so(self, world):
        # the SIGN of ratio SETS the direction, so re-valuing a deliberately reversed link at a
        # positive ratio un-reverses it - the payload must report what it overwrote.
        ml = _link("ML1", reversed_link=True)
        world(links=[ml])
        out = payload(rel.handler(kind="motion_link", name="ML1", action="set_values", ratio=2))
        assert out["was_reversed"] is True and out["reversed"] is False
        assert ml.isReversed is False
        assert "CLEARS" in out["note"] and "action='reverse'" in out["note"]
