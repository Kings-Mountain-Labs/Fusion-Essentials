"""Tests for `pmi_delete` - the deleteMe() gate, the survivor re-check, and the isDeletable
refusal: a delete only claims success when the annotation is actually gone."""

import json
from types import SimpleNamespace

import pytest

from conftest import load_tool, error_message, FakePMILeaderLineNote, MakeComp

pd = load_tool("pmi_delete")


def _payload(result):
    assert result["isError"] is False, result
    return json.loads(result["content"][0]["text"])


class _DeadHandle(FakePMILeaderLineNote):
    """A deleted proxy that refuses every read - the shape read_flag answers None for, and the one
    an isValid coerced to False would misread as proof the annotation is gone."""

    @property
    def isValid(self):
        raise RuntimeError("object has been deleted")


@pytest.fixture
def rig(monkeypatch):
    ann = FakePMILeaderLineNote()
    comp = MakeComp("Root")
    # holes = (components_unreadable, items_unreadable) the post-delete re-walk reports; (0, 0) is
    # a COMPLETE walk, the only shape in which zero hits proves the annotation is gone.
    state = SimpleNamespace(ann=ann, comp=comp, monkeypatch=monkeypatch, holes=(0, 0))

    def find(d, name, component=""):
        # after a successful delete the annotation no longer resolves
        if state.ann._deleted:
            return None, None, f"No PMI named '{name}'. Available: none."
        return state.ann, state.comp, None

    def hits(d, name, component="", stats=None):
        if stats is not None:
            stats["components_unreadable"] = state.holes[0]
            stats["items_unreadable"] = state.holes[1]
        return ([] if state.ann._deleted else [(state.ann, state.comp)]), []
    monkeypatch.setattr(pd._common, "design", lambda: object())
    monkeypatch.setattr(pd._pmi, "find_annotation", find)
    monkeypatch.setattr(pd._pmi, "annotation_hits", hits)
    monkeypatch.setattr(pd._pmi, "walk_annotations", lambda d: iter([]))
    return state


class TestDelete:
    def test_deletes_and_confirms_gone(self, rig):
        out = _payload(pd.handler(annotation="Note1"))
        assert out["deleted"] == "Note1" and out["remaining_pmi"] == 0

    def test_not_deletable_is_refused_without_change(self, rig):
        rig.ann.isDeletable = False
        msg = error_message(pd.handler(annotation="Note1"))
        assert "isDeletable=false" in msg and rig.ann._deleted is False

    def test_a_declined_delete_is_an_error(self, rig):
        rig.ann._delete_ok = False
        assert "declined" in error_message(pd.handler(annotation="Note1"))

    def test_a_survivor_after_success_is_an_error(self, rig):
        # deleteMe() lies (returns True) but the annotation still resolves afterwards
        rig.ann.deleteMe = lambda: True                # does NOT flip .deleted
        assert "still resolves" in error_message(pd.handler(annotation="Note1"))

    def test_a_handle_still_reporting_isValid_is_an_error_even_when_the_name_is_gone(self, rig):
        # The two survivor checks are independent: the NAME can stop resolving (a re-walk after
        # the collection dropped it) while the deleted handle still reads isValid=True.
        # Trusting the name alone reports a delete that did not happen.
        rig.ann.isValid = True
        assert "still reports isValid" in error_message(pd.handler(annotation="Note1"))

    def test_a_handle_that_went_invalid_confirms_the_delete(self, rig):
        rig.ann.isValid = False
        out = _payload(pd.handler(annotation="Note1"))
        assert out["deleted"] == "Note1"

    def test_an_unreadable_isValid_does_not_block_a_confirmed_delete(self, rig):
        # A deleted proxy commonly refuses every read; that is not evidence of a survivor, and
        # the COMPLETE re-walk is what carries the confirmation there.
        rig.ann = _DeadHandle()
        out = _payload(pd.handler(annotation="Note1"))
        assert out["deleted"] == "Note1"

    def test_an_incomplete_walk_with_an_unreadable_isValid_refuses_the_gone_verdict(self, rig):
        # Zero hits over a walk that could not read everything is not proof of absence, and the
        # deleted handle's own isValid will not read either - so there is NO proof, and claiming
        # "deleted" would be a false ok on a destructive call.
        rig.ann = _DeadHandle()
        rig.holes = (1, 0)
        msg = error_message(pd.handler(annotation="Note1"))
        assert "INCOMPLETE" in msg and "1 component(s)" in msg

    def test_an_unreadable_item_alone_also_refuses_the_gone_verdict(self, rig):
        # the other half of the hole record: an annotation that would not read is a hole too
        rig.ann = _DeadHandle()
        rig.holes = (0, 1)
        assert "INCOMPLETE" in error_message(pd.handler(annotation="Note1"))

    def test_an_unreadable_name_is_not_a_walk_that_proved_anything(self, rig):
        # A name that never read matches nothing for a reason that has nothing to do with the
        # annotation being gone - a vacuous zero-hit walk must not stand in as the proof.
        nameless = _DeadHandle()
        nameless.name = None
        rig.ann = nameless
        rig.holes = (0, 0)
        assert "INCOMPLETE" in error_message(pd.handler(annotation="Note1"))

    def test_a_complete_walk_with_zero_hits_confirms_the_delete(self, rig):
        # the exact boundary against the two tests above: same unreadable isValid, holes at (0, 0)
        rig.ann = _DeadHandle()
        rig.holes = (0, 0)
        assert _payload(pd.handler(annotation="Note1"))["deleted"] == "Note1"

    def test_an_incomplete_walk_is_still_settled_by_a_false_isValid(self, rig):
        # the second, independent proof: the object itself reports it is gone, so the holes in the
        # walk do not leave the delete unverified - they are disclosed beside the count instead.
        rig.ann.isValid = False
        rig.holes = (2, 3)
        out = _payload(pd.handler(annotation="Note1"))
        assert out["deleted"] == "Note1"
        assert out["components_unreadable"] == 2 and out["items_unreadable"] == 3
        assert "remaining_pmi" in out and "reachable" in out["note"]

    def test_the_remaining_count_comes_from_the_design_wide_walk(self, rig):
        rig.monkeypatch.setattr(
            pd._pmi, "walk_annotations",
            lambda d: iter([(rig.comp, object()), (rig.comp, object())]))
        assert _payload(pd.handler(annotation="Note1"))["remaining_pmi"] == 2

    def test_resolver_error_surfaces(self, rig):
        rig.monkeypatch.setattr(pd._pmi, "find_annotation",
                                lambda d, n, c="": (None, None, "No PMI named 'X'."))
        assert "No PMI named" in error_message(pd.handler(annotation="X"))

    def test_no_active_design_is_an_error(self, rig):
        rig.monkeypatch.setattr(pd._common, "design", lambda: None)
        assert "No active design" in error_message(pd.handler(annotation="N"))
