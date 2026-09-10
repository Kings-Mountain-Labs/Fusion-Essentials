"""Tests for `doc_restore_version` - promoting a prior cloud version back to latest.

Pins the honesty contract: a promote() that returns false is an ERROR (never a false ok); 'restored'
means the cloud tip ACTUALLY advanced, with the raw API answer kept beside it under
promote_call_returned_true; the confirming read is PUMPED to a deadline (the new tip is not visible the
instant promote returns) and a tip that never moves is reported pending with what was read. Plus the
guards (no cloud DataFile, unknown version, no selector, already-latest no-op).
"""

import json
import time

from conftest import (FakeApplication, FakeData, FakeDataFile, FakeFusionDocument, error_message,
                      load_tool)

drv = load_tool("doc_restore_version")


def _payload(result):
    assert result["isError"] is False, result
    return json.loads(result["content"][0]["text"])


def _ver(num, promote_ok=True, promote_raises=None):
    """One OLDER version of the file: the number/id a restore addresses it by, and promote()."""
    return FakeDataFile(f"Part v{num}", version=num, version_id=f"urn:v:{num}",
                        promote_ok=promote_ok, promote_raises=promote_raises)


def _df(latest, others, lineage="urn:lineage", latest_raises=False, date=1_700_000_000):
    """The active doc's DataFile: itself the tip version, df.versions holds the older ones. `date`
    is when that tip landed - what decides whether a read of it can still be trailing the cloud."""
    return FakeDataFile("Part", file_id=lineage, version=latest, version_id=f"urn:v:{latest}",
                        latest_raises=latest_raises, versions=others, date_created=date)


def _doc(df):
    """The active document, holding `df` as its cloud file."""
    return FakeFusionDocument(name="Part", data_file=df, is_saved=True)


class _ServesInSequence(FakeData):
    """app.data whose findFileById serves ONE file per call (the last repeats), so the cloud's
    'not visible yet, then visible' sequence can be driven; None serves no file at all."""
    def __init__(self, fresh):
        super().__init__()
        self._seq = list(fresh) if isinstance(fresh, (list, tuple)) else [fresh]
        self._calls = 0

    def findFileById(self, lineage):
        self._calls += 1
        return self._seq[min(self._calls - 1, len(self._seq) - 1)]


def _use(monkeypatch, doc, fresh_latest, deadline=0.0):
    """Point the tool at a fake app. deadline=0 makes the confirming pump take a single attempt;
    raise it (the poll sleep is 0) to exercise the retry without waiting."""
    latests = fresh_latest if isinstance(fresh_latest, (list, tuple)) else [fresh_latest]
    fresh = [None if n is None else FakeDataFile("Part", version=n) for n in latests]
    app = FakeApplication(active_document=doc, data=_ServesInSequence(fresh))
    monkeypatch.setattr(drv, "app", app)
    monkeypatch.setattr(drv, "_VERSION_DEADLINE_S", deadline)
    monkeypatch.setattr(drv, "_TIP_RECHECK_S", 0.0)
    monkeypatch.setattr(drv, "_POLL_SLEEP", 0)
    return app


class TestRestoreHonesty:
    def test_confirmed_when_new_tip_appears(self, monkeypatch):
        df = _df(5, [_ver(2), _ver(3), _ver(4)])
        _use(monkeypatch, _doc(df), fresh_latest=6)   # after promote the tip advanced to 6
        out = _payload(drv.handler(version_number=2))
        assert out["restored"] is True
        assert out["promote_call_returned_true"] is True
        assert out.get("pending") is None
        assert out["restored_version"] == 2
        assert out["latest_before"] == 5 and out["latest_after"] == 6

    def test_promote_false_is_an_error_not_a_false_ok(self, monkeypatch):
        df = _df(5, [_ver(2, promote_ok=False)])
        _use(monkeypatch, _doc(df), fresh_latest=5)
        res = drv.handler(version_number=2)
        assert res["isError"] is True
        assert "did not take effect" in error_message(res)

    def test_a_settled_equal_tip_is_not_restored(self, monkeypatch):
        # THE boundary: latest_after == latest_before is NOT an advance. promote() returning true is
        # kept as its own raw fact; 'restored' may only claim what the re-read showed.
        df = _df(5, [_ver(2, promote_ok=True)])
        _use(monkeypatch, _doc(df), fresh_latest=5)
        out = _payload(drv.handler(version_number=2))
        assert out["restored"] is False
        assert out["promote_call_returned_true"] is True
        assert out["pending"] is True
        note = out["note"]
        assert "NOT advanced" in note
        assert f"{drv._VERSION_DEADLINE_S:.0f}s of re-reading" in note   # what was actually waited
        assert "latest reads 5, was 5" in note

    def test_one_more_than_the_baseline_is_restored(self, monkeypatch):
        # the other side of the same boundary: exactly +1 counts as the new tip.
        df = _df(5, [_ver(2)])
        _use(monkeypatch, _doc(df), fresh_latest=6)
        assert _payload(drv.handler(version_number=2))["restored"] is True

    def test_a_tip_that_appears_on_a_later_read_is_confirmed_by_the_pump(self, monkeypatch):
        # The cloud tip is not visible the instant promote() returns. A single immediate sample
        # reports this successful restore as pending; the pump re-reads until it lands.
        df = _df(5, [_ver(2)])
        app = _use(monkeypatch, _doc(df), fresh_latest=[5, 5, 6], deadline=5.0)
        out = _payload(drv.handler(version_number=2))
        assert app.data._calls >= 3            # the first fetches did NOT show the new tip
        assert out["restored"] is True
        assert out["latest_after"] == 6

    def test_the_pump_gives_up_at_the_bound_with_the_reading_it_last_got(self, monkeypatch):
        df = _df(5, [_ver(2)])
        app = _use(monkeypatch, _doc(df), fresh_latest=5, deadline=0.02)
        out = _payload(drv.handler(version_number=2))
        assert app.data._calls >= 2            # it retried rather than single-shotting
        assert out["latest_after"] == 5       # the LAST reading, not a dropped one
        assert out["restored"] is False

    def test_an_unreadable_pre_call_tip_is_reported_not_diagnosed(self, monkeypatch):
        # With no pre-call number there is nothing to settle against: the read runs once, and the
        # payload may claim neither a duration it did not spend nor a non-advancement it never saw.
        df = _df(5, [_ver(2)], latest_raises=True)
        app = _use(monkeypatch, _doc(df), fresh_latest=7, deadline=5.0)
        out = _payload(drv.handler(version_number=2))
        assert app.data._calls == 1                  # nothing to settle against - no fake wait
        assert out["restored"] is False and out["pending"] is True
        assert out["latest_before"] is None
        assert out["latest_after"] == 7             # what WAS read is still reported
        note = out["note"]
        assert "could not be read BEFORE the call" in note and "7" in note
        assert "of re-reading" not in note          # no duration was spent

    def test_without_a_lineage_urn_the_held_handle_is_the_only_read(self, monkeypatch):
        # No lineage id means there is nothing to re-fetch BY, so the held handle is all there is -
        # and it carries the pre-call number, which is why this reports pending rather than restored.
        app = _use(monkeypatch, _doc(_df(5, [_ver(2)], lineage=None)), fresh_latest=9)
        out = _payload(drv.handler(version_number=2))
        assert app.data._calls == 0                  # findFileById was never reached
        assert out["latest_after"] == 5 and out["restored"] is False

    def test_an_unresolvable_fresh_file_reports_an_unreadable_tip(self, monkeypatch):
        df = _df(5, [_ver(2)])
        _use(monkeypatch, _doc(df), fresh_latest=None)   # findFileById answers nothing
        out = _payload(drv.handler(version_number=2))
        assert out["restored"] is False and out["pending"] is True
        assert out["latest_after"] is None
        assert "latest reads unreadable" in out["note"]


class TestGuards:
    def test_restoring_the_latest_is_a_noop(self, monkeypatch):
        df = _df(5, [_ver(2)])
        _use(monkeypatch, _doc(df), fresh_latest=5)
        out = _payload(drv.handler(version_number=5))
        assert out["restored"] is False
        assert "already the latest" in out["note"]

    def test_unknown_version_errors_and_lists_available(self, monkeypatch):
        df = _df(5, [_ver(2), _ver(3), _ver(4)])
        _use(monkeypatch, _doc(df), fresh_latest=5)
        res = drv.handler(version_number=99)
        assert res["isError"] is True
        msg = error_message(res)
        assert "99" in msg and "5" in msg      # available numbers surfaced

    def test_no_cloud_datafile_is_guarded(self, monkeypatch):
        _use(monkeypatch, _doc(None), fresh_latest=5)      # never saved: no cloud file at all
        res = drv.handler(version_number=2)
        assert res["isError"] is True
        assert "no cloud DataFile" in error_message(res)


class TestTheTipReadThatLagsASave:
    """The cloud's version metadata trails a completed save: a read right after a milestone whose own
    re-fetch had reported the tip advanced still answered latest 2. Early-returning on that read
    answers 'nothing to restore' for a version the lineage has already moved past, so the tip is
    re-fetched before the no-op verdict."""

    def _lagging(self, monkeypatch, stale_latest, serves, recheck=0.05, tip_age_s=0):
        """The held handle reads `stale_latest` as a tip `tip_age_s` old, while findFileById serves
        `serves`. A tip inside the lag window is the only one worth re-fetching."""
        held = _df(stale_latest, [_ver(2)], date=int(time.time()) - tip_age_s)
        app = FakeApplication(active_document=_doc(held), data=_ServesInSequence(serves))
        monkeypatch.setattr(drv, "app", app)
        monkeypatch.setattr(drv, "_VERSION_DEADLINE_S", 0.0)
        monkeypatch.setattr(drv, "_TIP_RECHECK_S", recheck)
        monkeypatch.setattr(drv, "_POLL_SLEEP", 0)
        return app

    def test_a_version_the_stale_read_calls_the_tip_is_restored_once_the_re_fetch_answers(
            self, monkeypatch):
        # the held handle says the tip IS 2, the fresh fetch says 3 - so version 2 is restorable
        # and the promote runs, instead of the call reporting nothing to restore.
        app = self._lagging(monkeypatch, 2, [_df(3, [_ver(2)]), _df(4, [])])
        out = _payload(drv.handler(version_number=2))
        assert app.data._calls >= 2                  # the re-fetch, then the confirming read
        assert out["promote_call_returned_true"] is True
        assert out["restored_version"] == 2
        assert out["latest_before"] == 3             # the FRESH tip, not the stale 2
        assert "already the latest" not in out["note"]

    def test_a_tip_that_really_has_not_moved_still_reports_nothing_to_restore(self, monkeypatch):
        # the boundary the re-fetch must not cross: an EQUAL fresh tip is not an advance, so the
        # no-op verdict stands - and the note says the re-fetch ran rather than implying one read.
        app = self._lagging(monkeypatch, 2, [_df(2, [_ver(2)])])
        out = _payload(drv.handler(version_number=2))
        assert out["restored"] is False
        assert "already the latest version" in out["note"]
        assert "without advancing past 2" in out["note"]
        assert out["tip_rechecked"] is True
        assert app.data._calls >= 1                  # the tip WAS re-fetched before the verdict

    def test_a_tip_older_than_the_window_is_not_re_fetched_at_all(self, monkeypatch):
        # THE COST GATE: a tip that has stood past the lag window is a settled reading, so the
        # no-op answers immediately instead of pumping the main thread for the full wait.
        app = self._lagging(monkeypatch, 2, [_df(9, [_ver(2)])],
                            tip_age_s=drv._doc_common.VERSION_LAG_WINDOW_S)
        out = _payload(drv.handler(version_number=2))
        assert out["restored"] is False
        assert out["tip_rechecked"] is False
        assert app.data._calls == 0                  # no re-fetch was paid for
        assert "past the 20s a version read can trail the cloud" in out["note"]
        assert "re-fetched for" not in out["note"]   # no wait was spent, so none is claimed

    def test_one_second_inside_the_window_still_re_fetches(self, monkeypatch):
        # the other side of that boundary - the exact age at which the wait is still worth paying.
        app = self._lagging(monkeypatch, 2, [_df(3, [_ver(2)]), _df(4, [])],
                            tip_age_s=drv._doc_common.VERSION_LAG_WINDOW_S - 1)
        out = _payload(drv.handler(version_number=2))
        assert app.data._calls >= 1
        assert out["restored_version"] == 2 and "already the latest" not in out["note"]

    def test_a_document_with_no_lineage_id_spends_no_wait_and_claims_no_re_fetch(self, monkeypatch):
        # there is nothing to re-fetch BY, so the pump would spin the whole window for no reading -
        # and reporting it as a re-fetch would claim a check that never ran.
        held = _df(2, [_ver(2)], lineage=None, date=int(time.time()))
        app = FakeApplication(active_document=_doc(held), data=_ServesInSequence([_df(9, [])]))
        monkeypatch.setattr(drv, "app", app)
        monkeypatch.setattr(drv, "_VERSION_DEADLINE_S", 0.0)
        monkeypatch.setattr(drv, "_POLL_SLEEP", 0)
        out = _payload(drv.handler(version_number=2))
        assert app.data._calls == 0                  # no lineage id, so no cloud read at all
        assert out["tip_rechecked"] is False
        assert "no lineage id to re-fetch the tip by" in out["note"]
        assert "re-fetched for" not in out["note"]

    def test_a_missing_version_with_no_lineage_id_spends_no_wait_either(self, monkeypatch):
        # the OTHER _fresh_tip call site: the above-the-tip miss hands it the lineage ungated, so
        # the no-lineage guard has to live inside _fresh_tip, not only in the equal-tip branch.
        held = _df(2, [_ver(2)], lineage=None, date=int(time.time()))
        app = FakeApplication(active_document=_doc(held), data=_ServesInSequence([_df(9, [])]))
        monkeypatch.setattr(drv, "app", app)
        monkeypatch.setattr(drv, "_POLL_SLEEP", 0)
        res = drv.handler(version_number=5)
        assert res["isError"] is True
        assert "No version matching number 5" in error_message(res)
        assert app.data._calls == 0              # nothing to re-fetch BY - no read, no wait

    def test_the_window_is_paid_at_most_once_per_call(self, monkeypatch):
        # the above-the-tip miss re-fetches, lands on a version that IS the fresh tip, and the
        # equal-tip branch must not spend the window a second time on the tip it just read. The
        # served tip is dated NOW, so 'settled' cannot mask the gate under test.
        app = self._lagging(monkeypatch, 2,
                            [_df(3, [_ver(2), _ver(3)], date=int(time.time()))])
        out = _payload(drv.handler(version_number=3))
        assert out["restored"] is False
        assert out["tip_rechecked"] is False
        assert "already re-fetched once for this call" in out["note"]

    def test_a_version_above_the_tip_re_reads_before_reporting_it_missing(self, monkeypatch):
        # the OTHER lagging-read shape: the caller names a version the stale history has not got
        # yet, so the miss is re-checked against a fresh fetch instead of refusing outright.
        app = self._lagging(monkeypatch, 2, [_df(3, [_ver(2), _ver(3)]), _df(4, [])])
        out = _payload(drv.handler(version_number=3))
        assert app.data._calls >= 1
        assert out["restored_version"] == 3

    def test_the_shipped_recheck_covers_the_measured_lag(self):
        # the measured lag of the cloud's version metadata behind a completed save is ~20 s; a
        # shorter re-fetch would give up before the tip a caller is racing becomes readable.
        assert drv._TIP_RECHECK_S >= 20


class TestPendingDescribedByWhatWasRead:
    """'pending' is set by ONE observation: the tip had not advanced by the time the pumped re-read
    gave up. What the cloud was doing meanwhile is not readable from here, so the wire may not name
    it as the cause."""

    def test_the_description_states_the_observation_not_a_cause(self):
        desc = drv.TOOL_DESCRIPTION
        assert "a NEW tip version carries its content" in desc
        assert "cloud is still processing" not in desc

    def test_the_pending_note_states_the_same_observation(self, monkeypatch):
        df = _df(5, [_ver(2)])
        _use(monkeypatch, _doc(df), fresh_latest=5)      # the tip never advances past latest_before
        note = _payload(drv.handler(version_number=2))["note"]
        assert "NOT advanced" in note
        assert "still processing" not in note
