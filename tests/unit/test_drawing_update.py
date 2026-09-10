"""Unit tests for ``drawing_update.py`` - refresh the active drawing's out-of-date references.

Covers: the active-doc-must-be-a-drawing guard, the already-up-to-date no-op, the happy refresh
(per-reference isOutOfDate flips false and version advances), the honesty gate (refresh ran but a
reference is still stale -> error, never a false success), unreadable references (refuses to refresh
blind), and that the updateAllReferences exception is reported. The gate walks documentReferences -
DrawingDocument.isUpToDate is deliberately not consulted (live it reports True even while a reference
is stale). No live Fusion.
"""

import json
import types

import pytest

import adsk  # the mock package conftest installed at import time
from conftest import FakeDocumentReference, load_tool, make_drawing, make_drawing_session


def _ref(is_out_of_date, version):
    """One documentReferences entry: the stale flag the gate walks and the version each row reports."""
    return FakeDocumentReference(out_of_date=is_out_of_date, version=version)


class _UnreadStaleness:
    version = 7

    @property
    def isOutOfDate(self):
        raise RuntimeError("staleness unavailable")


class _UnreadCount:
    @property
    def count(self):
        raise RuntimeError("reference count unavailable")


du = load_tool("drawing_update")
kernel = load_tool("_assert")
ex = load_tool("_export")            # the postcondition's settle wait pumps through pump_until


@pytest.fixture
def install(monkeypatch):
    """Install a drawing document as the ACTIVE one, on both seams that read it: the stand-in
    adsk.drawing plus adsk.core.Application.get (what _drawing_common's cast goes through) and the
    postcondition kernel's own app. monkeypatch owns both, so neither survives the test."""
    def _install(**document):
        doc = make_drawing(**document)
        monkeypatch.setattr(kernel, "app", make_drawing_session(monkeypatch, doc))
        return doc
    return _install


@pytest.fixture
def settle(monkeypatch):
    """The postcondition's settle wait on a fake clock with a counted pump: sleep ADVANCES the clock
    instead of blocking, so the bound is exercised in exact steps rather than against the wall clock
    (the same seam tests/unit/test__export.py drives pump_until through). The wait is shortened to
    1s / 0.25s here; the shipped budget stays whatever _assert declares. Returns (clock, pumps)."""
    clock = types.SimpleNamespace(now=0.0, slept=[])
    clock.monotonic = lambda: clock.now

    def _sleep(seconds):
        clock.slept.append(seconds)
        clock.now += seconds

    clock.sleep = _sleep
    pumps = []
    monkeypatch.setattr(ex, "time", clock)
    monkeypatch.setattr(adsk, "doEvents", lambda: pumps.append(1), raising=False)
    monkeypatch.setattr(kernel, "_REFERENCE_SETTLE_S", 1.0)
    monkeypatch.setattr(kernel, "_REFERENCE_SETTLE_POLL_SLEEP", 0.25)
    return clock, pumps


def _payload(res):
    assert res["isError"] is False, res
    return json.loads(res["content"][0]["text"])


class TestHappyPath:
    def test_stale_reference_is_refreshed_and_version_advances(self, install):
        doc = install(references=[_ref(True, 1)], references_after=[_ref(False, 2)])
        out = _payload(du.handler())
        assert out["updated"] is True
        assert out["stale_references_before"] == 1
        assert out["is_up_to_date"] is True
        assert out["references"][0]["version"] == 2   # views now reflect the newer design version
        assert doc._update_calls == 1

    def test_gates_on_references_not_the_lying_isuptodate(self, install):
        # isUpToDate is True (the live lie) while the reference IS stale - the refresh must still run.
        doc = install(references=[_ref(True, 3)], references_after=[_ref(False, 4)])
        assert doc.isUpToDate is True
        out = _payload(du.handler())
        assert out["updated"] is True
        assert doc._update_calls == 1

    def test_declared_returns_are_present(self, install):
        install(references=[_ref(True, 1)], references_after=[_ref(False, 2)])
        out = _payload(du.handler())
        for spec in du.RETURNS:
            assert spec.assert_present(out) == "", spec.assert_present(out)

    def test_unread_modified_state_remains_null_after_refresh(self, install):
        install(references=[_ref(True, 1)], references_after=[_ref(False, 2)],
                modified_raises="modified unavailable")
        out = _payload(du.handler())
        assert out["document_modified"] is None
        assert "is modified in-session" not in out["note"]


class TestNoOp:
    def test_current_references_do_not_refresh(self, install):
        doc = install(references=[_ref(False, 2)])
        out = _payload(du.handler())
        assert out["updated"] is False
        assert out["stale_references_before"] == 0
        assert doc._update_calls == 0     # no refresh issued when nothing is stale

    def test_zero_references_is_a_no_op(self, install):
        doc = install(references=[])
        out = _payload(du.handler())
        assert out["updated"] is False
        assert doc._update_calls == 0


class TestReferenceIndexAlignment:
    def test_an_unreadable_reference_holds_its_index_with_a_null_verdict(self, install):
        # 'index' is the address the payload publishes each reference's staleness against. A
        # reference that cannot be read holds its slot as a null row - dropping it would slide the
        # third reference's version under the second one's index, and claiming False for its
        # staleness would coerce an unknown into a verdict.
        install(references=[_ref(False, 1), _ref(False, 2), _ref(False, 3)], unreadable_at=1)
        out = _payload(du.handler())
        assert [r["index"] for r in out["references"]] == [0, 1, 2]
        assert out["references"][1] == {"index": 1, "is_out_of_date": None, "version": None}
        assert out["references"][2]["version"] == 3      # the THIRD reference, at its own address

    def test_an_unreadable_reference_is_not_counted_stale_but_is_disclosed(self, install):
        # a failed READ is not evidence of staleness - but it is not evidence of freshness either:
        # the refresh is driven by the readable ones, and the payload must not claim verified
        # up-to-date over the hole.
        doc = install(references=[_ref(False, 1), _ref(False, 2)], unreadable_at=1)
        out = _payload(du.handler())
        assert out["stale_references_before"] == 0 and doc._update_calls == 0
        assert out["unread_references"] == 1
        assert out["is_up_to_date"] is None
        assert "unknown" in out["note"]

    def test_an_unreadable_staleness_flag_is_null_not_fresh(self, install):
        doc = install(references=[_UnreadStaleness()])
        out = _payload(du.handler())
        assert doc._update_calls == 0
        assert out["references"] == [
            {"index": 0, "is_out_of_date": None, "version": 7}]
        assert out["unread_references"] == 1
        assert out["is_up_to_date"] is None


class TestHonestyGate:
    def test_still_stale_after_refresh_is_an_error(self, install, settle):
        # updateAllReferences ran but a reference is STILL stale - the ReferencesFresh postcondition on
        # the Item converts the handler's ok into an error (the handler no longer gates this itself).
        doc = install(references=[_ref(True, 1)], references_after=[_ref(True, 1)])
        res = kernel.wrap(du.handler, [kernel.ReferencesFresh()])()
        assert res["isError"] is True
        assert "still out of date" in res["message"].lower()
        assert doc._update_calls == 1

    def test_kernel_confirms_a_clean_refresh(self, install):
        install(references=[_ref(True, 1)], references_after=[_ref(False, 2)])
        out = _payload(kernel.wrap(du.handler, [kernel.ReferencesFresh()])())
        assert out["updated"] is True
        assert out["stale_references_after"] == 0

    def test_item_declares_references_fresh(self):
        h = du.item.handler
        posts = getattr(h, "__assert_postconditions__", None)
        while posts is None and getattr(h, "__wrapped__", None) is not None:
            h = h.__wrapped__
            posts = getattr(h, "__assert_postconditions__", None)
        assert posts and any(p.name == "references_fresh" for p in posts)

    def test_update_exception_is_reported(self, install):
        install(references=[_ref(True, 1)], update_raises=RuntimeError("refresh boom"))
        res = du.handler()
        assert res["isError"] is True
        assert "refresh boom" in res["message"]

    def test_unreadable_references_refuse_to_refresh_blind(self, install):
        doc = install(references=[_ref(True, 1)], references_raise="references unavailable")
        res = du.handler()
        assert res["isError"] is True
        assert "could not be read" in res["message"].lower()
        assert doc._update_calls == 0

    def test_unreadable_reference_count_refuses_to_refresh_blind(self, install, monkeypatch):
        doc = install(references=[_ref(True, 1)])
        monkeypatch.setattr(type(doc), "documentReferences",
                            property(lambda _self: _UnreadCount()))
        res = du.handler()
        assert res["isError"] is True
        assert "could not be read" in res["message"].lower()
        assert doc._update_calls == 0

    def test_unread_item_after_refresh_stays_unconfirmed(self, install):
        install(references=[_ref(True, 1), _ref(False, 2)],
                references_after=[_ref(False, 3), _ref(False, 2)], unreadable_at=1)
        out = _payload(kernel.wrap(du.handler, [kernel.ReferencesFresh()])())
        assert out["references_confirmed"] is False
        assert "stale_references_after" not in out

    def test_unread_staleness_after_refresh_stays_unconfirmed(self, install):
        install(references=[_ref(True, 1)], references_after=[_UnreadStaleness()])
        out = _payload(kernel.wrap(du.handler, [kernel.ReferencesFresh()])())
        assert out["is_up_to_date"] is None
        assert out["references_confirmed"] is False
        assert "stale_references_after" not in out

    def test_known_stale_after_refresh_still_bites_beside_an_unread_item(self, install, settle):
        install(references=[_ref(True, 1), _ref(False, 2)],
                references_after=[_ref(True, 1), _ref(False, 2)], unreadable_at=1)
        res = kernel.wrap(du.handler, [kernel.ReferencesFresh()])()
        assert res["isError"] is True
        assert "still out of date" in res["message"].lower()

    def test_unread_count_after_refresh_stays_unconfirmed(self, install, monkeypatch):
        doc = install(references=[_ref(True, 1)], references_after=[_ref(False, 2)])
        original = type(doc).documentReferences

        def _references(current):
            if current._updated:
                return _UnreadCount()
            return original.fget(current)

        monkeypatch.setattr(type(doc), "documentReferences", property(_references))
        out = _payload(kernel.wrap(du.handler, [kernel.ReferencesFresh()])())
        assert out["is_up_to_date"] is None
        assert out["references_confirmed"] is False
        assert "stale_references_after" not in out


class TestReferenceSettleRace:
    """The refresh lands asynchronously, so the freshness re-read is a bounded settle wait: a
    reference that freshens on a later sample is the race WON, not a failure."""

    def test_a_reference_that_freshens_on_a_later_sample_passes(self, install, settle):
        _clock, pumps = settle
        # settle_reads=2: the handler's own immediate re-read and the wait's FIRST sample both still
        # see stale rows; the sample after one pump reads clean.
        doc = install(references=[_ref(True, 1)], references_after=[_ref(False, 2)],
                      settle_reads=2)
        out = _payload(kernel.wrap(du.handler, [kernel.ReferencesFresh()])())
        assert out["stale_references_after"] == 0     # settled - not failed on the first sample
        assert pumps == [1]                           # exactly one pump bought the fresh read
        assert doc._update_calls == 1                 # and no second refresh was issued
        # the payload's rows are the handler's IMMEDIATE sample, taken before the wait: still stale
        # here, which is why the settled verdict is stale_references_after, not the rows.
        assert out["references"][0]["is_out_of_date"] is True
        assert out["is_up_to_date"] is None  # immediate stale is not the settled verdict

    def test_a_reference_that_never_freshens_still_errors_with_the_settle_fact(self, install, settle):
        doc = install(references=[_ref(True, 1)], references_after=[_ref(True, 1)])
        res = kernel.wrap(du.handler, [kernel.ReferencesFresh()])()
        assert res["isError"] is True
        assert "still out of date" in res["message"].lower()
        assert "given 1s to settle" in res["message"]   # how long it was waited, not just the verdict
        assert doc._update_calls == 1

    def test_the_settle_wait_is_clock_bounded(self, install, settle):
        clock, pumps = settle
        install(references=[_ref(True, 1)], references_after=[_ref(True, 1)])
        assert kernel.wrap(du.handler, [kernel.ReferencesFresh()])()["isError"] is True
        # samples at 0.00 0.25 0.50 0.75 1.00, then the bound stops it - never an open-ended poll
        assert clock.now == 1.0
        assert pumps == [1, 1, 1, 1]
        assert clock.slept == [0.25, 0.25, 0.25, 0.25]


class TestGuards:
    def test_active_doc_not_a_drawing_errors(self, monkeypatch):
        make_drawing_session(monkeypatch, object())
        res = du.handler()
        assert res["isError"] is True
        assert "not a drawing" in res["message"].lower()
