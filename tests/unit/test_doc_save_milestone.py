"""Tests for `doc_save_milestone` - saving the active document as a NAMED milestone version.

Pins the measured contract: saveMilestone returns TRUE on a clean document while creating nothing,
so a clean document is REFUSED up front; the confirming read goes through a FRESH findFileById
fetch (the handle the save was issued on never advances) and RETRIES until the new tip appears; the
milestone MARK lags the version, so it is reported pending with the observed reason - never as a
milestone that landed, never as one that did not.
"""

import json

from conftest import (FakeApplication, FakeData, FakeDataFile, FakeFusionDocument, error_message,
                      load_tool)

dsm = load_tool("doc_save_milestone")


def _payload(result):
    assert result["isError"] is False, result
    return json.loads(result["content"][0]["text"])


class _Milestone:
    """One Milestones entry. Milestone carries no live SHAPES dump, so it has no shared fake."""
    def __init__(self, name): self.name = name


class _Milestones:
    """The Milestones collection on a DataFile. itemByName is modelled as measured: a HIT returns
    the milestone, a MISS RAISES '3 : invalid argument name' instead of returning null. Under safe()
    that raise flattens to the same None an unreadable collection gives, so an itemByName check
    cannot tell a miss from an unreadable read - the tool walks the entries instead, which never
    provokes the raise. unreadable=True models the collection whose count reads None."""
    def __init__(self, names=(), unreadable=False):
        self._m = [_Milestone(n) for n in names]
        self._unreadable = unreadable

    @property
    def count(self): return None if self._unreadable else len(self._m)

    def item(self, i): return self._m[i]

    def itemByName(self, name):
        for m in self._m:
            if m.name == name:
                return m
        raise RuntimeError("3 : invalid argument name")


def _FreshFile(latest, is_milestone=True, names=("MS",), unreadable=False,
               lineage="urn:lineage"):
    """A DataFile returned by findFileById, carrying readable cloud lineage/version identity."""
    return FakeDataFile("Bracket", file_id=lineage, version=latest, latest_version=latest,
                        version_id=f"urn:file?version={latest}", is_milestone=is_milestone,
                        milestones=_Milestones(names, unreadable=unreadable))


def _StaleFile(lineage="urn:lineage", vnum=1, latest=1):
    """The handle held across the save. Measured to keep its PRE-save values: the version never
    advances and isMilestone stays False even after the milestone lands."""
    return FakeDataFile("Bracket", file_id=lineage, version=vnum, latest_version=latest,
                        is_milestone=False, milestones=_Milestones(unreadable=True))


def _Doc(df, modified=True, result=True, raises=False, name="Bracket"):
    return FakeFusionDocument(name=name, data_file=df, is_saved=True, is_modified=modified,
                              save_ok=result, save_raises="cloud refused" if raises else None)


class _ForkingMilestone(FakeFusionDocument):
    """A saveMilestone that lands the document on a NEW lineage urn, restarting its versions at 1 -
    what the first save after a configured-design conversion does."""
    def saveMilestone(self, name, description=""):
        did = super().saveMilestone(name, description)
        self.dataFile = _StaleFile(lineage="urn:new", vnum=1, latest=1)
        return did


class _ServesInSequence(FakeData):
    """app.data whose findFileById serves ONE file per call (the last repeats), so the cloud's
    'not visible yet, then visible' sequence can be driven; the lineages asked for are recorded."""
    def __init__(self, fresh):
        super().__init__()
        self._seq = list(fresh) if isinstance(fresh, (list, tuple)) else [fresh]
        self._calls = 0
        self._queried = []

    def findFileById(self, lineage):
        self._calls += 1
        self._queried.append(lineage)
        return self._seq[min(self._calls - 1, len(self._seq) - 1)]


def _use(monkeypatch, doc, fresh, deadline=0.0, fresh_before=None):
    """Point the tool at fresh pre-save identity followed by its post-save cloud readings."""
    if fresh_before is None and doc is not None and doc.dataFile is not None:
        held = doc.dataFile
        try:
            latest = held.latestVersionNumber
            fresh_before = _FreshFile(latest, is_milestone=False, names=(), lineage=held.id)
        except RuntimeError:
            fresh_before = FakeDataFile("Bracket", file_id=held.id,
                                        version=held.versionNumber, latest_raises=True)
    after = list(fresh) if isinstance(fresh, (list, tuple)) else [fresh]
    sequence = ([fresh_before] if fresh_before is not None else []) + after
    app = FakeApplication(active_document=doc, data=_ServesInSequence(sequence))
    monkeypatch.setattr(dsm, "app", app)
    monkeypatch.setattr(dsm, "_VERSION_DEADLINE_S", deadline)
    monkeypatch.setattr(dsm, "_POLL_SLEEP", 0)
    return app


class TestHappyPath:
    def test_confirmed_when_fresh_read_shows_the_new_milestone_version(self, monkeypatch):
        doc = _Doc(_StaleFile(vnum=1, latest=1))
        _use(monkeypatch, doc, _FreshFile(latest=2, is_milestone=True, names=("v2 release",)))
        out = _payload(dsm.handler(milestone_name="v2 release", description="ready"))
        assert out["save_call_returned_true"] is True
        assert out["latest_version_before"] == 1 and out["latest_version_after"] == 2
        assert out["cloud_tip_advanced"] is True
        assert out["milestone_confirmed"] is True
        assert out.get("pending") is None
        assert out["milestone_count_after"] == 1
        assert out["document_id"] == "urn:lineage"

    def test_item_postcondition_reuses_the_handler_cloud_observation(self, monkeypatch):
        kernel = load_tool("_assert")
        doc = _Doc(_StaleFile(vnum=1, latest=1))
        before = _FreshFile(latest=1, is_milestone=False, names=())
        after = _FreshFile(latest=2, is_milestone=True, names=("MS",))
        app = _use(monkeypatch, doc, [before, after], fresh_before=before)
        monkeypatch.setattr(kernel, "app", app)
        post = kernel.VersionAdvanced()
        monkeypatch.setattr(post, "_DEADLINE_S", 0.0)
        out = _payload(kernel.wrap(dsm.handler, [post])(milestone_name="MS"))
        assert out["local_save_confirmed"] is True and out["version_confirmed"] is True
        assert app.data._calls == 3       # post baseline + handler baseline/after, no second after read

    def test_description_carries_the_ai_agent_marker(self, monkeypatch):
        doc = _Doc(_StaleFile())
        _use(monkeypatch, doc, _FreshFile(latest=2))
        out = _payload(dsm.handler(milestone_name="MS", description="ready"))
        assert doc._saves == [("milestone", ("MS", "[AI agent] ready"))]   # the marker reaches the call
        assert out["description"] == "[AI agent] ready"

    def test_confirmation_uses_a_fresh_baseline_when_the_held_handle_lags(self, monkeypatch):
        doc = _Doc(_StaleFile(vnum=1, latest=1))
        _use(monkeypatch, doc, _FreshFile(latest=5, is_milestone=True, names=("MS",)),
             fresh_before=_FreshFile(latest=4, is_milestone=False, names=()))
        out = _payload(dsm.handler(milestone_name="MS"))
        assert out["cloud_tip_advanced"] is True
        assert out["latest_version_before"] == 4 and out["latest_version_after"] == 5
        assert out["milestone_confirmed"] is True
        assert doc.dataFile.latestVersionNumber == 1

    def test_version_is_confirmed_only_because_the_pump_retried(self, monkeypatch):
        # measured: the new tip is not visible the instant saveMilestone returns - it appears on a
        # later fresh fetch. A single-shot fetch would report this successful save pending.
        doc = _Doc(_StaleFile(latest=1))
        not_yet = _FreshFile(latest=1, is_milestone=False, names=())
        landed = _FreshFile(latest=2, is_milestone=True, names=("MS",))
        app = _use(monkeypatch, doc, [not_yet, landed], deadline=5.0)
        out = _payload(dsm.handler(milestone_name="MS"))
        assert app.data._calls >= 3                  # baseline + first miss + the landed tip
        assert out["cloud_tip_advanced"] is True
        assert out["latest_version_after"] == 2


class TestPendingReportsWhatWasObserved:
    def test_flag_reading_false_is_named_as_false(self, monkeypatch):
        # inside the lag window: the tip is there, its isMilestone flag still reads False.
        doc = _Doc(_StaleFile())
        _use(monkeypatch, doc, _FreshFile(latest=2, is_milestone=False, names=()))
        out = _payload(dsm.handler(milestone_name="MS"))
        assert out["cloud_tip_advanced"] is True
        assert out["milestone_confirmed"] is False
        assert out["pending"] is True
        assert "reads FALSE" in out["note"]
        # The note may not read as evidence the milestone failed, and it may not quote a duration
        # nothing in the repo measures - it points at the re-read instead.
        assert "confirmation remains pending" in out["note"]
        assert "doc_get include=['versions']" in out["note"]

    def test_unreadable_flag_is_named_as_unreadable_not_as_false(self, monkeypatch):
        _use(monkeypatch, _Doc(_StaleFile()),
             _FreshFile(latest=2, is_milestone=None, names=("MS",)))   # the flag will not read
        note = _payload(dsm.handler(milestone_name="MS"))["note"]
        assert "could not be read" in note and "reads FALSE" not in note

    def test_collection_without_the_name_is_named_as_such(self, monkeypatch):
        # the flag says milestone, but no entry carries this name - report exactly that.
        doc = _Doc(_StaleFile())
        _use(monkeypatch, doc, _FreshFile(latest=2, is_milestone=True, names=("SomeoneElse",)))
        out = _payload(dsm.handler(milestone_name="MS"))
        assert out["milestone_confirmed"] is False
        assert "holds no entry named 'MS'" in out["note"]

    def test_named_entry_is_required_for_confirmation(self, monkeypatch):
        # the name term is load-bearing: an isMilestone flag alone does not prove THIS milestone.
        doc = _Doc(_StaleFile())
        _use(monkeypatch, doc, _FreshFile(latest=2, is_milestone=True, names=("Other", "Another")))
        assert _payload(dsm.handler(milestone_name="MS"))["milestone_confirmed"] is False

    def test_unresolvable_fresh_file_is_pending_not_confirmed(self, monkeypatch):
        doc = _Doc(_StaleFile())
        _use(monkeypatch, doc, None)                 # findFileById returns nothing
        out = _payload(dsm.handler(milestone_name="MS"))
        assert out["cloud_tip_advanced"] is False
        assert out["milestone_confirmed"] is False
        assert out["latest_version_after"] is None
        assert out["milestone_count_after"] is None
        assert out["version_before"] == 1
        assert "before/after" in out["note"]
        assert "pre-save identity" not in out["note"]


class TestReviewRegressions:
    def test_unreadable_post_save_lineage_cannot_confirm_the_old_file(self, monkeypatch):
        class UnreadableAfterSave(FakeFusionDocument):
            """A successful milestone call whose post-save DataFile is unreadable."""
            def saveMilestone(self, name, description=""):
                result = super().saveMilestone(name, description)
                self.dataFile = None
                return result

        doc = UnreadableAfterSave(data_file=_StaleFile(), is_saved=True, is_modified=True)
        app = _use(monkeypatch, doc, [_FreshFile(1), _FreshFile(2)])
        kernel = load_tool("_assert")
        monkeypatch.setattr(kernel, "app", app)
        out = _payload(kernel.wrap(dsm.handler, [kernel.VersionAdvanced()])(milestone_name="MS"))
        assert out["local_save_confirmed"] is True
        assert out["cloud_tip_advanced"] is False and out["version_confirmed"] is False
        assert out["milestone_confirmed"] is False and out["pending"] is True
        assert out["identity_unreadable"] is True and out["document_id"] is None
        assert "AFTER the save" in out["note"]
        assert app.data._calls == 2
        assert dsm.RETURNS[0].assert_present(out) == ""

    def test_ordered_version_number_fallback_is_reported_without_a_none_version(self, monkeypatch):
        before = FakeDataFile("Bracket", file_id="urn:lineage", version=1, latest_raises=True)
        after = FakeDataFile("Bracket", file_id="urn:lineage", version=2, latest_raises=True,
                             is_milestone=True, milestones=_Milestones(["MS"]))
        _use(monkeypatch, _Doc(_StaleFile()), after, fresh_before=before)
        out = _payload(dsm.handler(milestone_name="MS"))
        assert out["cloud_tip_advanced"] is True and out["milestone_confirmed"] is True
        assert (out["version_before"], out["version_after"]) == (1, 2)
        assert out["latest_version_before"] is None and out["latest_version_after"] is None
        assert "Version None" not in out["note"]


class TestVersionNeverAdvanced:
    def test_an_unchanged_tip_stays_pending_without_claiming_no_version_exists(self, monkeypatch):
        doc = _Doc(_StaleFile(latest=1))
        _use(monkeypatch, doc, _FreshFile(latest=1, is_milestone=False, names=()))
        out = _payload(dsm.handler(milestone_name="MS"))
        assert out["cloud_tip_advanced"] is False
        assert out["pending"] is True
        note = out["note"]
        assert "versioned nothing" not in note
        assert "not observed" in note
        assert f"{dsm._VERSION_DEADLINE_S:.0f}s of re-fetching" in note   # what was actually waited
        assert "lag" not in note.lower()

    def test_an_unreadable_pre_save_tip_is_reported_as_such_not_diagnosed(self, monkeypatch):
        # With no PRE-save number there is no comparison to wait on: the confirming read runs once,
        # so the payload may claim neither a duration it did not spend nor a non-advancement it
        # never observed.
        doc = _Doc(FakeDataFile("Bracket", file_id="urn:lineage", version=1, latest_raises=True))
        unreadable = FakeDataFile("Bracket", file_id="urn:lineage", version=None,
                                  version_id=None, latest_raises=True)
        app = _use(monkeypatch, doc, _FreshFile(latest=7, is_milestone=True, names=("MS",)),
                   deadline=5.0, fresh_before=unreadable)
        out = _payload(dsm.handler(milestone_name="MS"))
        assert app.data._calls == 2                  # fresh baseline + one after read, no settle loop
        assert out["latest_version_before"] is None
        assert out["latest_version_after"] == 7     # what WAS read is still reported
        assert out["cloud_tip_advanced"] is False and out["pending"] is True
        note = out["note"]
        assert "before/after lineage/version reads were not comparable" in note
        assert "versioned nothing" not in note      # a verdict on a comparison that never happened
        assert "of re-fetching" not in note         # no duration was spent

    def test_the_bounded_retry_gives_up_and_reports_the_tip_it_last_read(self, monkeypatch):
        # a tip that never moves must end the wait at the bound with the reading it actually got -
        # never an unbounded retry, and never a None that would read as "could not be read".
        doc = _Doc(_StaleFile(latest=3))
        app = _use(monkeypatch, doc, _FreshFile(latest=3, is_milestone=False, names=()),
                   deadline=0.02)
        out = _payload(dsm.handler(milestone_name="MS"))
        assert app.data._calls >= 3                  # baseline plus more than one after read
        assert out["latest_version_after"] == 3     # the LAST reading, not a dropped one
        assert out["cloud_tip_advanced"] is False
        assert "versioned nothing" not in out["note"]
        assert "not observed" in out["note"]


class TestHonesty:
    def test_clean_document_is_refused_not_falsely_saved(self, monkeypatch):
        # the measured false OK: saveMilestone returns True on an unmodified doc and creates nothing.
        doc = _Doc(_StaleFile(), modified=False)
        _use(monkeypatch, doc, _FreshFile(latest=2))
        res = dsm.handler(milestone_name="MS")
        msg = error_message(res)
        assert "no unsaved changes" in msg
        assert "only creates a NEW milestone version" in msg   # the tool's limit, not a platform claim
        assert doc._saves == []                                 # the API was never called

    def test_false_return_is_an_error_not_a_false_ok(self, monkeypatch):
        doc = _Doc(_StaleFile(), result=False)
        _use(monkeypatch, doc, _FreshFile(latest=2))
        res = dsm.handler(milestone_name="MS")
        assert "returned false" in error_message(res)

    def test_raised_failure_surfaces(self, monkeypatch):
        doc = _Doc(_StaleFile(), raises=True)
        _use(monkeypatch, doc, _FreshFile(latest=2))
        assert "cloud refused" in error_message(dsm.handler(milestone_name="MS"))


class TestGuards:
    def test_empty_name_is_refused(self, monkeypatch):
        doc = _Doc(_StaleFile())
        _use(monkeypatch, doc, _FreshFile(latest=2))
        assert "milestone_name" in error_message(dsm.handler(milestone_name="   "))
        assert doc._saves == []

    def test_never_saved_document_points_at_doc_save_as(self, monkeypatch):
        doc = _Doc(None)
        _use(monkeypatch, doc, None)
        assert "doc_save_as" in error_message(dsm.handler(milestone_name="MS"))

    def test_no_active_document_is_guarded(self, monkeypatch):
        _use(monkeypatch, None, None)
        assert "no active document" in error_message(dsm.handler(milestone_name="MS")).lower()


class TestLineageFork:
    def test_a_forking_save_confirms_on_the_new_lineage_and_reports_the_fork(self, monkeypatch):
        # The first save after a configured-design conversion moves the document to a NEW lineage
        # URN whose versions restart at 1 - confirming against the superseded URN watches a stream this
        # save never advances, so the check must re-anchor on the lineage the doc holds NOW.
        doc = _ForkingMilestone(name="Bracket", is_saved=True,
                                data_file=_StaleFile(lineage="urn:old", vnum=3, latest=3))
        app = _use(monkeypatch, doc, _FreshFile(latest=1, is_milestone=False, names=(),
                                                 lineage="urn:new"))
        out = _payload(dsm.handler(milestone_name="MS"))
        assert out["lineage_changed"] == {"from": "urn:old", "to": "urn:new"}
        assert out["document_id"] == "urn:new"
        assert app.data._queried == ["urn:old", "urn:new"]
        # NOT confirmed: the pre-save number belongs to the abandoned stream, so no comparison ran.
        # A tip number coming back on the new lineage is a reading, not a confirmation.
        assert out["cloud_tip_advanced"] is False
        assert out["latest_version_after"] == 1      # what WAS read is still reported
        assert out["pending"] is True                # the mark is unconfirmed there
        assert "advancement is unknown" in out["note"]
        assert "lineage_changed.to" in out["note"]
        assert "NOT applied" not in out["note"]

    def test_a_fork_whose_new_lineage_reads_back_nothing_is_not_diagnosed(self, monkeypatch):
        # The pre-save number belongs to the ABANDONED stream, so it is no baseline for the new
        # lineage: the confirming read runs once, and the payload may claim neither a duration it
        # did not spend nor a non-advancement it measured against the wrong stream.
        doc = _ForkingMilestone(name="Bracket", is_saved=True,
                                data_file=_StaleFile(lineage="urn:old", vnum=3, latest=3))
        app = _use(monkeypatch, doc, None, deadline=5.0)   # findFileById answers nothing
        out = _payload(dsm.handler(milestone_name="MS"))
        assert app.data._calls == 2                  # fresh baseline + one after read, no settle loop
        assert out["lineage_changed"] == {"from": "urn:old", "to": "urn:new"}
        assert out["cloud_tip_advanced"] is False and out["pending"] is True
        note = out["note"]
        assert "new lineage" in note and "advancement is unknown" in note
        assert "versioned nothing" not in note      # a verdict against an abandoned version stream
        assert "of re-fetching" not in note         # no duration was spent

    def test_a_same_lineage_save_reports_no_fork(self, monkeypatch):
        doc = _Doc(_StaleFile(lineage="urn:same", vnum=1, latest=1))
        _use(monkeypatch, doc, _FreshFile(latest=2, is_milestone=True, names=("MS",)))
        out = _payload(dsm.handler(milestone_name="MS"))
        assert "lineage_changed" not in out
        assert out["document_id"] == "urn:same"
        assert "NEW LINEAGE" not in out["note"]


class TestPostconditionKeyIsNotShadowed:
    """The handler publishes its fresh comparison for VersionAdvanced to reuse without another wait."""

    def test_handler_publishes_cloud_tip_advanced_not_version_confirmed(self, monkeypatch):
        doc = _Doc(_StaleFile())
        _use(monkeypatch, doc, _FreshFile(latest=2, is_milestone=True, names=("MS",)))
        out = _payload(dsm.handler(milestone_name="MS"))
        assert out["cloud_tip_advanced"] is True
        assert "version_confirmed" not in out       # left free for the postcondition to supply


class TestMilestoneWalkIsBounded:
    """The name match walks a CLOUD collection on the main thread, so it is capped. Past the cap the
    answer is UNKNOWN, never "the collection holds no entry named that" - an unbounded walk both
    stalls the thread and, if it were merely cut short, would report a miss it never established."""

    def _huge(self, over_cap):
        names = tuple(f"MS-{i}" for i in range(dsm._MILESTONE_WALK_CAP + over_cap))
        return _FreshFile(latest=2, is_milestone=True, names=names)

    def test_a_name_past_the_cap_is_unknown_not_absent(self, monkeypatch):
        _use(monkeypatch, _Doc(_StaleFile()), self._huge(5))
        out = _payload(dsm.handler(milestone_name=f"MS-{dsm._MILESTONE_WALK_CAP + 2}"))
        assert out["milestone_confirmed"] is False
        note = out["note"]
        assert f"more than the {dsm._MILESTONE_WALK_CAP} this call walks" in note
        assert "holds no entry named" not in note        # never a miss it did not establish

    def test_a_name_inside_the_cap_still_confirms(self, monkeypatch):
        _use(monkeypatch, _Doc(_StaleFile()), self._huge(5))
        out = _payload(dsm.handler(milestone_name="MS-0"))
        assert out["milestone_confirmed"] is True

    def test_a_short_collection_still_reports_a_real_miss(self, monkeypatch):
        _use(monkeypatch, _Doc(_StaleFile()),
             _FreshFile(latest=2, is_milestone=True, names=("Other",)))
        note = _payload(dsm.handler(milestone_name="MS"))["note"]
        assert "holds no entry named 'MS'" in note
