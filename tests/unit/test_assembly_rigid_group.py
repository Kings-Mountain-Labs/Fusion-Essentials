"""Unit tests for ``assembly_rigid_group.py`` - collecting occurrences into RigidGroups.add.

The logic pinned here, no live Fusion: the at-least-two guard, the resolve of every named
occurrence through the shared ambiguity-refusing resolver, and the member-count read-back.
"""

import pytest

from conftest import (FakeOccurrence, FakeRigidGroup, FakeRigidGroups, MakeComp, MakeDesign,
                      error_message, go_stale, install, load_tool, payload)


asm = load_tool("assembly_rigid_group")


def _occurrence(path):
    """One occurrence a group can take in, placing a component of its own."""
    return FakeOccurrence(path=path, component=MakeComp(name=path.split("+")[-1].split(":")[0]))


def _mute(path):
    """A group member that answers NEITHER identity read - the unlabelled member a missing-member
    verdict may not be read off."""
    occ = FakeOccurrence(path=path, raises_on={"fullPathName": "declined"})
    go_stale(occ, attrs=("name",))
    return occ


@pytest.fixture
def wire():
    """Build a design placing `paths` under a RigidGroups collection and wire both tool seams."""
    def build(*paths, new_group=None):
        occs = [_occurrence(p) for p in paths]
        root = MakeComp(occurrences=occs)
        root.rigidGroups = FakeRigidGroups(new_group=new_group)
        install(asm, MakeDesign(comp=root))
        return occs, root.rigidGroups
    return build


class TestRigidGroup:
    def test_group_reporting_fewer_members_bites(self, wire):
        # the group was created but reads fewer members than were requested -> error, not ok
        wire("A:1", "B:1", new_group=FakeRigidGroup(occurrences=["A:1"]))
        res = asm.handler(occurrences="A:1, B:1")
        assert res["isError"] is True
        assert "1 member(s)" in res["message"]

    def test_groups_named_occurrences(self, wire):
        _occs, groups = wire("A:1", "B:1", "C:1")
        out = payload(asm.handler(occurrences="A:1, B:1"))
        coll, _include = groups._added[-1]
        assert coll.count == 2
        assert out["grouped"] == ["A:1", "B:1"]

    def test_include_children_flag(self, wire):
        _occs, groups = wire("A:1", "B:1")
        asm.handler(occurrences="A:1, B:1", include_children=True)
        _coll, include = groups._added[-1]
        assert include is True

    def test_needs_at_least_two(self, wire):
        wire("A:1")
        res = asm.handler(occurrences="A:1")
        assert res["isError"] is True and "at least two" in res["message"].lower()

    def test_missing_reported(self, wire):
        wire("A:1")
        res = asm.handler(occurrences="A:1, Ghost")
        assert res["isError"] is True and "Ghost" in res["message"]

    def test_accepts_a_list_not_just_comma_string(self, wire):
        # _resolve_many handles both a comma string and an actual list of names.
        _occs, groups = wire("A:1", "B:1", "C:1")
        out = payload(asm.handler(occurrences=["A:1", "C:1"]))
        coll, _include = groups._added[-1]
        assert coll.count == 2
        assert out["grouped"] == ["A:1", "C:1"]

    def test_list_with_blank_entries_filtered(self, wire):
        # empty/whitespace entries are dropped before resolution.
        wire("A:1", "B:1")
        out = payload(asm.handler(occurrences=["A:1", "  ", "B:1"]))
        assert out["grouped"] == ["A:1", "B:1"]


class TestMembersReadBack:
    """WHICH occurrences landed in the group, not just how many - a count match over a swapped
    member is a group locking parts nobody asked for."""

    def test_a_group_holding_the_right_count_of_the_wrong_parts_bites(self, wire):
        swapped = FakeRigidGroup(occurrences=[_occurrence("A:1"), _occurrence("C:1")])
        wire("A:1", "B:1", "C:1", new_group=swapped)
        msg = error_message(asm.handler(occurrences="A:1, B:1"))
        assert "B:1" in msg and "not asked for" in msg

    def test_the_requested_members_landing_is_a_success(self, wire):
        held = FakeRigidGroup(occurrences=[_occurrence("A:1"), _occurrence("B:1")])
        wire("A:1", "B:1", "C:1", new_group=held)
        assert payload(asm.handler(occurrences="A:1, B:1"))["member_count"] == 2

    def test_extra_members_from_include_children_are_not_a_miss(self, wire):
        wider = FakeRigidGroup(occurrences=[_occurrence("A:1"), _occurrence("B:1"),
                                            _occurrence("A:1+Inner:1")])
        wire("A:1", "B:1", new_group=wider)
        out = payload(asm.handler(occurrences="A:1, B:1", include_children=True))
        assert out["member_count"] == 3

    def test_members_that_will_not_name_themselves_are_not_reported_missing(self, wire):
        # an occurrence whose reads all decline: unlabelled is not absent, so the group passes.
        unreadable = FakeRigidGroup(occurrences=[_mute("A:1"), _mute("B:1")])
        wire("A:1", "B:1", new_group=unreadable)
        assert payload(asm.handler(occurrences="A:1, B:1"))["member_count"] == 2
