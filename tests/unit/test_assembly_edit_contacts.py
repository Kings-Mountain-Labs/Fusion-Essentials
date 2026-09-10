"""Unit tests for assembly_edit_contacts - the lifecycle of a design's contact sets.

Pinned: the >= 2 DISTINCT members guard, the plain-list-only add, name resolution (case-insensitive
EXACT, a repeated name refused), and every verification gate - the landed name read back after
create/rename, the misspelled occurencesAndBodies write re-read, the suppress re-read, the delete
re-list, and the two design-level analysis flags re-read after assignment.

The fakes model what was measured on Fusion 2704.1.39: ContactSets.add takes a PLAIN LIST (an
ObjectCollection raises), a same-case colliding name auto-dedupes to 'Name (1)', a BODY member reads
back as an object neither cast accepts, and the platform refuses a scope write while contact
analysis is disabled.
"""

import pytest

import adsk.core
import adsk.fusion

from conftest import (BRepBody, error_message, FakeContactSet, FakeOccurrence, install, load_tool,
                      make_design, make_occurrence, MakeComp, MakeDesign, MeshBody,
                      _NamedCollection, payload)

ec = load_tool("assembly_edit_contacts")


def _occ(path):
    """An assembly occurrence at `path` - what TargetRefList resolves a name to."""
    return make_occurrence(path=path, component=MakeComp(name=path.split("+")[-1].split(":")[0]),
                           entity_token=f"OCC:{path}")


# ── the platform objects (the surface measured on the installed bindings) ────────────────────────

class _Raw:
    """What a BODY member of a contact set reads back as: an object neither Occurrence.cast nor
    BRepBody.cast accepts, so it counts toward len() but carries no name."""


class _ContactSet(FakeContactSet):
    """The shared set with the two properties that MISBEHAVE as the platform's do: a name another
    set already holds is auto-deduped to 'Name (1)' with no raise (measured), and a member list
    stores an occurrence as itself and a BODY as a _Raw. Assigning `occurrencesAndBodies` (the
    correctly spelled name) lands on a dead attribute here exactly as it does on the SWIG proxy."""

    def __init__(self, name, members=(), suppressed=False, delete_ok=True, home=None):
        self.home = home
        super().__init__(name=name, suppressed=suppressed, delete_ok=delete_ok)
        # written past both properties: a subclass whose name or member write is SWALLOWED must
        # still start out holding what it was built with.
        self._name = name
        self._members = [self._store(m) for m in members]
        self.delete_ok = delete_ok

    @staticmethod
    def _store(m):
        return _Raw() if isinstance(m, BRepBody) else m

    @property
    def name(self):
        return self._name

    @name.setter
    def name(self, value):
        # Measured: a name another set already holds is auto-deduped to 'Name (1)', with no raise.
        taken = {cs.name for cs in (self.home._items if self.home else []) if cs is not self}
        self._name = f"{value} (1)" if value in taken else value

    @property
    def occurencesAndBodies(self):
        return list(self._members)

    @occurencesAndBodies.setter
    def occurencesAndBodies(self, value):
        self._members = [self._store(m) for m in value]

    def deleteMe(self):
        if self.delete_ok and self.home is not None:
            self.home._items.remove(self)
        return self.delete_ok


class _StickyMembers(_ContactSet):
    """A set whose member assignment is SWALLOWED - the platform accepting a write that never takes."""

    @property
    def occurencesAndBodies(self):
        return list(self._members)

    @occurencesAndBodies.setter
    def occurencesAndBodies(self, value):
        pass


class _DropsAMember(_ContactSet):
    """A set that keeps only the FIRST member written - a membership the count gate must reject."""

    @property
    def occurencesAndBodies(self):
        return list(self._members)

    @occurencesAndBodies.setter
    def occurencesAndBodies(self, value):
        self._members = [self._store(m) for m in value[:1]]


class _UnreadableSuppress(_ContactSet):
    """A set whose isSuppressed cannot be read back - the write is UNCONFIRMED, not proven False."""

    @property
    def isSuppressed(self):
        raise RuntimeError("3 : An API Object refers to a deleted Object")

    @isSuppressed.setter
    def isSuppressed(self, value):
        self._flag = value


class _StickyName(_ContactSet):
    """A set whose rename is swallowed - the name reads back unchanged."""

    @property
    def name(self):
        return self._name

    @name.setter
    def name(self, value):
        pass


class _ContactSets(_NamedCollection):
    """design.contactSets: the shared walk plus add(list) -> ContactSet."""

    def __init__(self, items=(), add_returns_none=False, raise_on_add="", mint_on_raise=True):
        super().__init__(items)
        for cs in self._items:
            cs.home = self
        self.add_returns_none = add_returns_none
        self.raise_on_add = raise_on_add
        self.mint_on_raise = mint_on_raise
        self.last_add = None

    def _mint(self, members):
        cs = _ContactSet(f"ContactSet{len(self._items) + 1}", members=members, home=self)
        self._items.append(cs)
        return cs

    def add(self, members):
        # LIVE-VERIFIED: ContactSets.add wants a plain Python LIST (the std::vector<Ptr<Base>> SWIG
        # binding) - an ObjectCollection raises "argument 2 of type 'std::vector< adsk::core::Ptr<
        # adsk::core::Base > >'". Reject anything else so a regression to ObjectCollection.create()
        # surfaces here, not only live.
        if not isinstance(members, list):
            raise TypeError("in method 'ContactSets_add', argument 2 of type "
                            "'std::vector< adsk::core::Ptr< adsk::core::Base > > const &'")
        self.last_add = list(members)
        if self.raise_on_add:
            if self.mint_on_raise:       # a raise with a set landed anyway - the adopt-on-raise path
                self._mint(members)
            raise RuntimeError(self.raise_on_add)
        if self.add_returns_none:
            return None
        return self._mint(members)


class _CoupledDesign(MakeDesign):
    """A design where DISABLING contact analysis drops the scope flag to False and re-enabling
    restores it - the derived read measured on the live product."""

    def __init__(self, comp, enabled=True, use_sets=True):
        super().__init__(comp=comp)
        self._enabled = enabled
        self._retained = use_sets
        self.isContactSetAnalysis = use_sets if enabled else False

    @property
    def isContactAnalysisEnabled(self):
        return self._enabled

    @isContactAnalysisEnabled.setter
    def isContactAnalysisEnabled(self, value):
        self._enabled = bool(value)
        self.isContactSetAnalysis = self._retained if value else False


class _StubbornDesign(MakeDesign):
    """A design whose analysis flags accept a write and keep their value - the platform lying about
    a setter, which the read-back gate exists to catch."""

    def __init__(self, comp, enabled=False, use_sets=False):
        super().__init__(comp=comp)
        self._enabled, self._use_sets = enabled, use_sets

    @property
    def isContactAnalysisEnabled(self):
        return self._enabled

    @isContactAnalysisEnabled.setter
    def isContactAnalysisEnabled(self, value):
        pass

    @property
    def isContactSetAnalysis(self):
        return self._use_sets

    @isContactSetAnalysis.setter
    def isContactSetAnalysis(self, value):
        pass


class _UnreadableFlagsDesign(MakeDesign):
    """A design whose analysis flags accept a write and cannot be read back at all - an unreadable
    flag is not a False, and a gate that treats it as one publishes a design-wide claim nothing
    confirmed."""

    def __init__(self, comp, enabled=False, use_sets=False):
        super().__init__(comp=comp)

    @property
    def isContactAnalysisEnabled(self):
        raise RuntimeError("3 : the flag cannot be read")

    @isContactAnalysisEnabled.setter
    def isContactAnalysisEnabled(self, value):
        pass

    @property
    def isContactSetAnalysis(self):
        raise RuntimeError("3 : the flag cannot be read")

    @isContactSetAnalysis.setter
    def isContactSetAnalysis(self, value):
        pass


class _UnreadableScopeDesign(MakeDesign):
    """Analysis reads honestly; only the SCOPE flag is unreadable."""

    def __init__(self, comp, enabled=True, use_sets=False):
        super().__init__(comp=comp)
        self.isContactAnalysisEnabled = enabled

    @property
    def isContactSetAnalysis(self):
        raise RuntimeError("3 : the flag cannot be read")

    @isContactSetAnalysis.setter
    def isContactSetAnalysis(self, value):
        pass


@pytest.fixture
def world(monkeypatch):
    """A design carrying contact sets, bodies and occurrences, wired into the tool through install()
    (both design seams - TargetRefList resolves through _inputs' own _common)."""
    def _build(sets=None, occurrences=(), bodies=(), tokens=None, enabled=False, use_sets=False,
               design_cls=None):
        root = MakeComp(name="Root", bodies=list(bodies), occurrences=[_occ(p) for p in occurrences])
        if design_cls is None:
            design = make_design(comp=root, tokens=tokens)
            design.isContactAnalysisEnabled = enabled
            design.isContactSetAnalysis = use_sets
        else:
            design = design_cls(root, enabled, use_sets)
        design.contactSets = sets if sets is not None else _ContactSets()
        monkeypatch.setattr(adsk.fusion, "Occurrence", FakeOccurrence, raising=False)
        monkeypatch.setattr(adsk.fusion, "MeshBody", MeshBody, raising=False)
        monkeypatch.setattr(adsk.fusion, "BRepBody", BRepBody, raising=False)
        # The member read-back names an occurrence through Occurrence.cast and a body through
        # BRepBody.cast; neither shared fake carries one, so each gets the isinstance pass-through.
        for kind in (FakeOccurrence, BRepBody, MeshBody):
            monkeypatch.setattr(kind, "cast",
                                staticmethod(lambda x, k=kind: x if isinstance(x, k) else None),
                                raising=False)
        install(ec, design)
        return design
    return _build


def _sets(*names):
    return _ContactSets([_ContactSet(n) for n in names])


class TestGuards:
    def test_unknown_action_is_refused(self, world):
        world()
        assert "action" in error_message(ec.handler(action="melt"))

    def test_no_active_design(self, monkeypatch):
        monkeypatch.setattr(ec._common, "design", lambda: None)
        monkeypatch.setattr(ec._inputs._common, "design", lambda: None)
        assert "design" in error_message(ec.handler(action="create", members=["A:1", "B:1"])).lower()

    def test_a_single_member_is_refused_with_the_measured_raise(self, world):
        # the platform answers '3 : ContactSetRequest: bad occurrences'; refusing first teaches the
        # rule instead of forwarding an opaque message.
        design = world(occurrences=["A:1", "B:1"])
        msg = error_message(ec.handler(action="create", members=["A:1"]))
        assert "2 DISTINCT" in msg and "bad occurrences" in msg
        assert design.contactSets.last_add is None          # the platform is never reached

    def test_no_members_is_refused(self, world):
        world(occurrences=["A:1"])
        msg = error_message(ec.handler(action="create", members=[]))
        assert "holds 0 member(s)" in msg and "Invalid input" in msg

    def test_the_same_member_twice_is_refused_and_named(self, world):
        design = world(occurrences=["A:1", "B:1"])
        msg = error_message(ec.handler(action="create", members=["A:1", "A:1"]))
        assert "A:1" in msg and "twice" in msg
        assert design.contactSets.last_add is None

    def test_a_mesh_member_is_refused_by_name(self, world):
        # the API takes Occurrence or BRepBody objects; a MeshBody is neither.
        mesh = MeshBody(name="ScanData", token="MESH:ScanData")
        design = world(occurrences=["A:1"], tokens={"MESH:ScanData": mesh})
        msg = error_message(ec.handler(action="create", members=["A:1", "MESH:ScanData"]))
        assert "('ScanData') is a MESH body" in msg
        assert design.contactSets.last_add is None

    def test_missing_name_is_refused(self, world):
        world(sets=_sets("ContactSet1"))
        assert "'name' is required" in error_message(ec.handler(action="delete", name=""))

    def test_unknown_name_lists_what_exists(self, world):
        world(sets=_sets("ContactSet1", "ContactSet2"))
        msg = error_message(ec.handler(action="delete", name="Ghost"))
        assert "Ghost" in msg and "ContactSet1" in msg and "ContactSet2" in msg

    def test_a_name_matching_two_sets_is_refused_unmutated(self, world):
        # names differing only in CASE both answer a case-insensitive query; deleting the first hit
        # would destroy the wrong set.
        design = world(sets=_sets("Bracket", "bracket"))
        msg = error_message(ec.handler(action="delete", name="BRACKET"))
        assert "names 2 contact sets" in msg
        assert design.contactSets.count == 2

    def test_name_match_is_exact_not_a_prefix(self, world):
        world(sets=_sets("ContactSet1"))
        assert "No contact set named 'Contact'" in error_message(
            ec.handler(action="suppress", name="Contact"))


class TestCreate:
    def test_create_passes_a_plain_list_and_reports_the_landed_name(self, world):
        design = world(occurrences=["A:1", "B:1"])
        out = payload(ec.handler(action="create", members=["A:1", "B:1"]))
        assert isinstance(design.contactSets.last_add, list)   # never an ObjectCollection
        assert out["contact_set"] == "ContactSet1"             # Fusion's name, not a prediction
        assert out["members"] == ["A:1", "B:1"] and out["member_count"] == 2
        assert design.contactSets.count == 1

    def test_the_reported_name_is_the_one_the_platform_assigned(self, world):
        design = world(sets=_sets("ContactSet1"), occurrences=["A:1", "B:1"])
        out = payload(ec.handler(action="create", members=["A:1", "B:1"]))
        assert out["contact_set"] == "ContactSet2"
        assert design.contactSets.itemByName("ContactSet2") is not None

    def test_a_body_member_is_counted_but_not_named(self, world):
        # measured: a body member reads back as a raw object both casts reject - counting it while
        # reporting no name is the honest read; dropping it would under-report the membership.
        world(occurrences=["A:1"], bodies=["Block"])
        out = payload(ec.handler(action="create", members=["A:1", "Block"]))
        assert out["member_count"] == 2
        assert out["members"] == ["A:1"]
        assert out["members_unreadable"] == 1
        assert "counted in member_count" in out["note"]

    def test_a_fully_readable_membership_omits_the_unreadable_count(self, world):
        # the key's ABSENCE is what says every member was named. A published 0 would make
        # "members_unreadable" in out true for every membership that read back cleanly, and the key
        # otherwise carries a POSITIVE count - so a zero there reads as "some members went
        # unnamed". Two named members is the 0 side of that boundary and one body member the 1 side
        # (test_a_body_member_is_counted_but_not_named). A membership of NO members never reaches
        # this read-back: the >= 2 DISTINCT guard refuses it first, and a set that came back empty
        # is caught by the count gate - so set_members, which publishes the same key through the
        # same read-back, is the other arm of this pin rather than an empty set.
        world(occurrences=["A:1", "B:1"])
        out = payload(ec.handler(action="create", members=["A:1", "B:1"]))
        assert "members_unreadable" not in out
        assert out["member_count"] == 2 and out["members"] == ["A:1", "B:1"]
        assert "not named in members" not in out["note"]

    def test_a_create_that_raises_but_lands_reports_the_set_and_the_raise(self, world):
        # a raise is not proof nothing landed - reporting failure over a created set would leave the
        # caller unable to address it.
        sets = _ContactSets(raise_on_add="3 : Internal error")
        world(sets=sets, occurrences=["A:1", "B:1"])
        out = payload(ec.handler(action="create", members=["A:1", "B:1"]))
        assert out["created"] is True and out["contact_set"] == "ContactSet1"
        assert "3 : Internal error" in out["note"]
        assert sets.count == 1

    def test_a_create_that_raises_with_nothing_landed_surfaces_the_platform_text(self, world):
        # the platform's own message is the only thing that tells the caller WHY; dropping it for a
        # generic failure line strands them.
        sets = _ContactSets(raise_on_add="3 : ContactSetRequest: bad occurrences", mint_on_raise=False)
        world(sets=sets, occurrences=["A:1", "B:1"])
        msg = error_message(ec.handler(action="create", members=["A:1", "B:1"]))
        assert "3 : ContactSetRequest: bad occurrences" in msg
        assert "0 contact set(s)" in msg and sets.count == 0

    def test_two_sets_appearing_under_a_raise_are_refused_not_first_matched(self, world):
        # The raise-but-landed adoption is only sound while EXACTLY ONE new set appeared. With two,
        # which one the call made is unknowable - taking the first names a set the caller never
        # asked for and reports it as theirs.
        sets = _ContactSets(raise_on_add="3 : Internal error")
        original = sets._mint

        def mint_two(members):
            cs = original(members)
            sets._items.append(_ContactSet("StrayContactSet", members=members, home=sets))
            return cs

        sets._mint = mint_two
        world(sets=sets, occurrences=["A:1", "B:1"])
        msg = error_message(ec.handler(action="create", members=["A:1", "B:1"]))
        assert "3 : Internal error" in msg
        assert "2 contact set(s)" in msg
        assert "StrayContactSet" not in msg          # no set is adopted by position

    def test_a_create_that_returns_nothing_and_lands_nothing_is_an_error(self, world):
        world(sets=_ContactSets(add_returns_none=True), occurrences=["A:1", "B:1"])
        msg = error_message(ec.handler(action="create", members=["A:1", "B:1"]))
        assert "failed" in msg and "0 contact set(s)" in msg

    def test_a_set_created_with_the_wrong_membership_is_an_error(self, world):
        sets = _ContactSets()

        def short(members):
            cs = _ContactSet("ContactSet1", members=members[:1], home=sets)
            sets._items.append(cs)
            return cs
        sets._mint = short
        world(sets=sets, occurrences=["A:1", "B:1"])
        assert "not the 2 requested" in error_message(
            ec.handler(action="create", members=["A:1", "B:1"]))

    def test_a_wrong_membership_after_a_raise_carries_the_platform_text_too(self, world):
        # the membership complaint must not swallow the raise that came with it.
        sets = _ContactSets(raise_on_add="3 : Internal error")
        sets._mint = lambda members: sets._items.append(
            _ContactSet("ContactSet1", members=members[:1], home=sets))
        world(sets=sets, occurrences=["A:1", "B:1"])
        msg = error_message(ec.handler(action="create", members=["A:1", "B:1"]))
        assert "not the 2 requested" in msg and "3 : Internal error" in msg

    def test_the_note_says_the_set_is_inert_while_analysis_is_off(self, world):
        # a set created in a design with contact analysis off does nothing at all - reporting only
        # success would be true and misleading.
        world(occurrences=["A:1", "B:1"], enabled=False)
        out = payload(ec.handler(action="create", members=["A:1", "B:1"]))
        assert "INERT" in out["note"] and "enable_analysis" in out["note"]

    def test_the_note_flags_an_all_bodies_scope(self, world):
        world(occurrences=["A:1", "B:1"], enabled=True, use_sets=False)
        out = payload(ec.handler(action="create", members=["A:1", "B:1"]))
        assert "ALL bodies" in out["note"] and "set_analysis_scope" in out["note"]

    def test_no_inert_warning_when_analysis_uses_the_sets(self, world):
        world(occurrences=["A:1", "B:1"], enabled=True, use_sets=True)
        out = payload(ec.handler(action="create", members=["A:1", "B:1"]))
        assert "INERT" not in out["note"] and "ALL bodies" not in out["note"]

    def test_an_unreadable_scope_is_not_reported_as_an_all_bodies_scope(self, world):
        # an unreadable scope flag falling through to the all_bodies branch would tell the caller
        # their new set is ignored when nothing established that.
        world(occurrences=["A:1", "B:1"], enabled=True, design_cls=_UnreadableScopeDesign)
        out = payload(ec.handler(action="create", members=["A:1", "B:1"]))
        assert "scope could not be read" in out["note"]
        assert "ALL bodies" not in out["note"] and "INERT" not in out["note"]


class TestSetMembers:
    def test_members_are_replaced_through_the_misspelled_property_and_read_back(self, world):
        # occurencesAndBodies (ONE 'r') IS the property; writing the correctly spelled name lands on
        # a dead attribute and changes nothing, which this fake reproduces.
        cs = _ContactSet("ContactSet1", members=[_occ("A:1"), _occ("B:1")])
        world(sets=_ContactSets([cs]), occurrences=["A:1", "B:1", "C:1"])
        out = payload(ec.handler(action="set_members", name="ContactSet1", members=["A:1", "C:1"]))
        assert [m.fullPathName for m in cs._members] == ["A:1", "C:1"]
        assert out["members"] == ["A:1", "C:1"] and out["member_count"] == 2
        assert out["member_count_before"] == 2

    def test_a_swallowed_member_write_is_an_error_not_a_false_ok(self, world):
        # the same COUNT with the wrong members is what a count-only check would wave through.
        cs = _StickyMembers("ContactSet1", members=[_occ("A:1"), _occ("B:1")])
        world(sets=_ContactSets([cs]), occurrences=["A:1", "B:1", "C:1"])
        msg = error_message(ec.handler(action="set_members", name="ContactSet1",
                                       members=["A:1", "C:1"]))
        assert "does not list C:1" in msg and "did not take" in msg
        assert [m.fullPathName for m in cs._members] == ["A:1", "B:1"]

    def test_a_membership_that_lost_a_member_is_an_error(self, world):
        cs = _DropsAMember("ContactSet1", members=[_occ("A:1"), _occ("B:1")])
        world(sets=_ContactSets([cs]), occurrences=["A:1", "B:1", "C:1"])
        assert "not the 2 requested" in error_message(
            ec.handler(action="set_members", name="ContactSet1", members=["A:1", "C:1"]))

    def test_set_members_also_enforces_two_distinct_members(self, world):
        cs = _ContactSet("ContactSet1", members=[_occ("A:1"), _occ("B:1")])
        world(sets=_ContactSets([cs]), occurrences=["A:1", "B:1"])
        assert "2 DISTINCT" in error_message(
            ec.handler(action="set_members", name="ContactSet1", members=["A:1"]))

    def test_a_body_member_is_counted_but_not_named(self, world):
        # the read-back a body member takes here is create's: an object both casts reject counts
        # toward member_count and is disclosed as unreadable, rather than dropped from the
        # membership the caller is being told the set now holds.
        cs = _ContactSet("ContactSet1", members=[_occ("A:1"), _occ("B:1")])
        world(sets=_ContactSets([cs]), occurrences=["A:1"], bodies=["Block"])
        out = payload(ec.handler(action="set_members", name="ContactSet1",
                                 members=["A:1", "Block"]))
        assert out["member_count"] == 2 and out["members"] == ["A:1"]
        assert out["members_unreadable"] == 1
        assert "counted in member_count" in out["note"]

    def test_a_fully_readable_membership_omits_the_unreadable_count(self, world):
        # the other arm of create's pin, on the second action publishing the key: withholding it at
        # zero has to hold here too, or a fully named replacement membership tells the caller it
        # landed members the set cannot name.
        cs = _ContactSet("ContactSet1", members=[_occ("A:1"), _occ("B:1")])
        world(sets=_ContactSets([cs]), occurrences=["A:1", "B:1", "C:1"])
        out = payload(ec.handler(action="set_members", name="ContactSet1", members=["A:1", "C:1"]))
        assert "members_unreadable" not in out
        assert out["member_count"] == 2 and out["members"] == ["A:1", "C:1"]
        assert "not named in members" not in out["note"]


class TestRename:
    def test_rename_lands_and_reports_the_new_name(self, world):
        design = world(sets=_sets("ContactSet1"))
        out = payload(ec.handler(action="rename", name="ContactSet1", new_name="FrameToPanel"))
        assert out["contact_set"] == "FrameToPanel" and out["previous_name"] == "ContactSet1"
        assert design.contactSets.itemByName("FrameToPanel") is not None

    def test_a_colliding_rename_reports_the_deduped_name_it_landed(self, world):
        # measured: Fusion silently lands 'Name (1)'. Echoing the request would hand the caller a
        # name that addresses a DIFFERENT set.
        world(sets=_sets("Taken", "ContactSet2"))
        out = payload(ec.handler(action="rename", name="ContactSet2", new_name="Taken"))
        assert out["contact_set"] == "Taken (1)" and out["requested_name"] == "Taken"
        assert "auto-deduped" in out["note"]

    def test_a_landed_name_of_another_shape_is_reported_without_a_dedupe_cause(self, world):
        # The 'Name (1)' dedupe is the MEASURED cause, and it has a shape. A landed name that does
        # not match it changed for a reason nothing here read - claiming the dedupe over it hands
        # the caller a diagnosis the tool invented.

        class _Rewriting(_ContactSet):
            @property
            def name(self):
                return self._name

            @name.setter
            def name(self, value):
                self._name = value.upper() + "_X"

        world(sets=_ContactSets([_Rewriting("ContactSet1")]))
        out = payload(ec.handler(action="rename", name="ContactSet1", new_name="Frame"))
        assert out["contact_set"] == "FRAME_X"
        assert "auto-deduped" not in out["note"]
        assert "not readable from here" in out["note"]

    def test_a_swallowed_rename_is_an_error(self, world):
        world(sets=_ContactSets([_StickyName("ContactSet1")]))
        assert "did not take" in error_message(
            ec.handler(action="rename", name="ContactSet1", new_name="Frame"))

    def test_an_empty_new_name_is_refused(self, world):
        world(sets=_sets("ContactSet1"))
        assert "new_name" in error_message(ec.handler(action="rename", name="ContactSet1"))


class TestSuppress:
    def test_suppress_sets_and_reports_both_states(self, world):
        cs = _ContactSet("ContactSet1")
        world(sets=_ContactSets([cs]))
        out = payload(ec.handler(action="suppress", name="ContactSet1"))
        assert cs.isSuppressed is True
        assert out["is_suppressed"] is True and out["was_suppressed"] is False

    def test_unsuppress_clears_it(self, world):
        cs = _ContactSet("ContactSet1", suppressed=True)
        world(sets=_ContactSets([cs]))
        out = payload(ec.handler(action="unsuppress", name="ContactSet1"))
        assert cs.isSuppressed is False and out["was_suppressed"] is True

    def test_a_swallowed_suppress_is_an_error(self, world):
        cs = _ContactSet("ContactSet1")
        type(cs).isSuppressed = property(lambda self: False, lambda self, v: None)
        world(sets=_ContactSets([cs]))
        try:
            msg = error_message(ec.handler(action="suppress", name="ContactSet1"))
        finally:
            del type(cs).isSuppressed
        assert "did not take" in msg

    def test_an_unreadable_prior_flag_reports_null_not_false(self, world):
        # False would claim the set WAS unsuppressed; null says the prior state is unknown.
        cs = _ContactSet("ContactSet1")
        del cs.isSuppressed
        world(sets=_ContactSets([cs]))
        out = payload(ec.handler(action="suppress", name="ContactSet1"))
        assert out["was_suppressed"] is None and out["is_suppressed"] is True

    def test_a_flag_that_cannot_be_read_back_is_unconfirmed_not_ok(self, world):
        # an unreadable read-back treated as False would pass the mismatch gate for unsuppress and
        # report a swallowed write as ok.
        world(sets=_ContactSets([_UnreadableSuppress("ContactSet1")]))
        msg = error_message(ec.handler(action="unsuppress", name="ContactSet1"))
        assert "UNCONFIRMED" in msg and "cannot be read" in msg


class TestScopeWriteGuard:
    def test_an_unreadable_analysis_flag_is_not_treated_as_off(self, world):
        # The "the platform refuses a scope write while analysis is off" refusal is a fact about
        # analysis being OFF. Asserting it over a flag that never answered names a cause nothing
        # observed - the same conflation this file refuses everywhere else.
        world(design_cls=_UnreadableFlagsDesign)
        msg = error_message(ec.handler(action="set_analysis_scope", scope="contact_sets"))
        assert "cannot be read" in msg and "UNKNOWN" in msg
        assert "Contact analysis is disabled" not in msg     # never the asserted platform cause

    def test_an_unreadable_scope_flag_after_the_write_is_unconfirmed_not_wrong(self, world):
        world(design_cls=_UnreadableScopeDesign, enabled=True)
        msg = error_message(ec.handler(action="set_analysis_scope", scope="contact_sets"))
        assert "UNCONFIRMED" in msg


class TestDelete:
    def test_delete_removes_it_and_confirms_by_re_listing(self, world):
        design = world(sets=_sets("ContactSet1", "ContactSet2"))
        out = payload(ec.handler(action="delete", name="ContactSet1"))
        assert out["deleted"] is True and out["remaining"] == 1
        assert design.contactSets.itemByName("ContactSet1") is None

    def test_a_declined_delete_is_an_error(self, world):
        world(sets=_ContactSets([_ContactSet("ContactSet1", delete_ok=False)]))
        msg = error_message(ec.handler(action="delete", name="ContactSet1"))
        assert "declined" in msg and "deleteMe returned false" in msg

    def test_a_survivor_after_a_true_delete_is_an_error(self, world):
        # deleteMe() true while the set is still listed is the swallowed no-op the re-list catches.
        cs = _ContactSet("ContactSet1")
        cs.deleteMe = lambda: True                  # says yes, removes nothing
        world(sets=_ContactSets([cs]))
        assert "still listed" in error_message(ec.handler(action="delete", name="ContactSet1"))

    def test_a_stale_proxy_delete_is_reported_not_swallowed(self, world):
        # measured: deleteMe on an already-deleted set raises '4 : An API Object refers to a deleted
        # Object' - never a False.
        cs = _ContactSet("ContactSet1")

        def raise_deleted():
            raise RuntimeError("4 : An API Object refers to a deleted Object")
        cs.deleteMe = raise_deleted
        world(sets=_ContactSets([cs]))
        assert "deleted Object" in error_message(ec.handler(action="delete", name="ContactSet1"))


class TestAnalysisFlags:
    def test_enable_sets_the_flag_and_reports_both(self, world):
        design = world(enabled=False, use_sets=True)
        out = payload(ec.handler(action="enable_analysis"))
        assert design.isContactAnalysisEnabled is True
        assert out["analysis_enabled"] is True and out["scope"] == "contact_sets"

    def test_disable_reports_the_derived_scope_coupling(self, world):
        # measured: with analysis off the scope flag reads False whatever was set, and the scope in
        # force before comes back on re-enable - reporting it as lost would mislead.
        design = world(enabled=True, use_sets=True, design_cls=_CoupledDesign)
        out = payload(ec.handler(action="disable_analysis"))
        assert design.isContactSetAnalysis is False
        assert out["analysis_enabled"] is False and out["scope"] == "all_bodies"
        assert "comes back" in out["note"] and "inert" in out["note"]

    def test_re_enabling_reports_the_scope_that_came_back(self, world):
        world(enabled=False, use_sets=True, design_cls=_CoupledDesign)
        out = payload(ec.handler(action="enable_analysis"))
        assert out["analysis_enabled"] is True and out["scope"] == "contact_sets"

    def test_a_swallowed_enable_is_an_error(self, world):
        world(enabled=False, design_cls=_StubbornDesign)
        assert "did not take" in error_message(ec.handler(action="enable_analysis"))

    def test_a_flag_that_cannot_be_read_back_is_unconfirmed_not_ok(self, world):
        # an unreadable flag read as False matched the requested False and published "contact
        # analysis is OFF" - a design-wide physics claim nothing confirmed.
        world(enabled=True, design_cls=_UnreadableFlagsDesign)
        msg = error_message(ec.handler(action="disable_analysis"))
        assert "UNCONFIRMED" in msg and "isContactAnalysisEnabled cannot be read" in msg

    def test_an_unreadable_scope_is_published_as_null_not_all_bodies(self, world):
        # all_bodies would be a fabricated answer for a flag that never answered.
        world(enabled=False, design_cls=_UnreadableScopeDesign)
        out = payload(ec.handler(action="enable_analysis"))
        assert out["analysis_enabled"] is True and out["scope"] is None
        assert "cannot be read" in out["note"]

    def test_an_unreadable_scope_after_the_write_is_unconfirmed(self, world):
        world(enabled=True, design_cls=_UnreadableScopeDesign)
        msg = error_message(ec.handler(action="set_analysis_scope", scope="contact_sets"))
        assert "UNCONFIRMED" in msg

    def test_scope_is_refused_while_analysis_is_disabled(self, world):
        # measured: the platform itself raises on the write - the refusal quotes it and teaches
        # enable-first rather than guessing at a silent no-op.
        design = world(enabled=False, use_sets=False)
        msg = error_message(ec.handler(action="set_analysis_scope", scope="contact_sets"))
        assert "3 : Contact analysis is disabled." in msg
        assert "enable_analysis" in msg and "Nothing was changed" in msg
        assert design.isContactSetAnalysis is False

    def test_scope_sets_and_reads_back_when_analysis_is_on(self, world):
        design = world(enabled=True, use_sets=False)
        out = payload(ec.handler(action="set_analysis_scope", scope="contact_sets"))
        assert design.isContactSetAnalysis is True
        assert out["scope"] == "contact_sets" and out["analysis_enabled"] is True

    def test_scope_all_bodies_reports_the_sets_are_ignored(self, world):
        world(enabled=True, use_sets=True)
        out = payload(ec.handler(action="set_analysis_scope", scope="all_bodies"))
        assert out["scope"] == "all_bodies" and "ignoring every contact set" in out["note"]

    def test_a_swallowed_scope_write_is_an_error(self, world):
        world(enabled=True, use_sets=False, design_cls=_StubbornDesign)
        assert "did not take" in error_message(
            ec.handler(action="set_analysis_scope", scope="contact_sets"))

    def test_an_unknown_scope_is_refused(self, world):
        world(enabled=True)
        assert "scope" in error_message(ec.handler(action="set_analysis_scope", scope="everything"))

    def test_a_missing_scope_is_refused(self, world):
        world(enabled=True)
        assert "'scope' is required" in error_message(ec.handler(action="set_analysis_scope"))

    def test_the_analysis_actions_need_no_contact_set(self, world):
        # they change design-wide state, so resolving a name first would refuse a legitimate call in
        # a design that holds no sets at all.
        world(enabled=False)
        assert payload(ec.handler(action="enable_analysis"))["analysis_enabled"] is True
