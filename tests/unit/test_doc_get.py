"""Tests for `doc_get` — the session rich read (active doc identity + open-doc list).

Pins the handler's job: surface the active document's URN/save-state, list the open docs with the terse
razor (a healthy doc collapses to {name, is_active}; an unsaved/modified one keeps its flag), and the
no-active-doc guard. The adsk Document fakes capture the read so a regression to a wrong attribute fails.
"""

import json
import time

import pytest

from conftest import (FakeApplication, FakeDataFile, FakeDocumentReference, FakeDocuments,
                      FakeFeatures, FakeFusionDocument, FakeOccurrence, MakeComp, MakeDesign,
                      _NamedCollection, error_message, load_tool, make_occurrence)

dg = load_tool("doc_get")
# The consumers of what this read publishes: 'open:N' is resolved in _doc_common, so the addresses
# doc_get offers are pinned against the two tools that have to accept them.
dk = load_tool("_doc_common")
da = load_tool("doc_activate")
dcl = load_tool("doc_close")


def _payload(result):
    assert result["isError"] is False, result
    return json.loads(result["content"][0]["text"])


def _DataFile(urn="urn:lineage:abc", vnum=3, latest=3):
    """The active document's cloud file: the lineage urn, the version pair a freshness read
    compares, and the web URL the payload republishes."""
    return FakeDataFile("Bracket", file_id=urn, version=vnum, latest_version=latest,
                        version_id="urn:version:xyz", web_url="https://fusion.example/x")


def _Doc(name, saved=True, modified=False, data_file=None):
    return FakeFusionDocument(name=name, is_saved=saved, is_modified=modified,
                              version="2.0.21", data_file=data_file)


class _Docs(FakeDocuments):
    """item_raises_at models a stale collection slot: item(i) raises while count still includes it.
    One index, or a set of them - a session can hold more than one such slot."""
    def __init__(self, documents=(), item_raises_at=None):
        super().__init__(documents)
        self._raises_at = item_raises_at

    def item(self, i):
        at = self._raises_at
        if i == at or (isinstance(at, (set, list, tuple)) and i in at):
            raise RuntimeError("4 : An API Object refers to a deleted Object")
        return super().item(i)


@pytest.fixture(autouse=True)
def _install(monkeypatch):
    """Point dg.app at an Application with the given active doc + open-docs list."""
    def _wire(active=None, open_docs=None, item_raises_at=None):
        docs = open_docs if open_docs is not None else ([active] if active else [])
        app = FakeApplication(active_document=active,
                              documents=_Docs(docs, item_raises_at=item_raises_at))
        monkeypatch.setattr(dg, "app", app)
        return app
    return _wire


class TestActiveIdentity:
    def test_saved_doc_surfaces_urn_and_state(self, _install):
        d = _Doc("Bracket", data_file=_DataFile(urn="urn:lineage:abc", vnum=3))
        _install(d)
        out = _payload(dg.handler())
        assert out["active"]["name"] == "Bracket"
        assert out["active"]["document_id"] == "urn:lineage:abc"
        assert out["document_id"] == "urn:lineage:abc"             # also hoisted to top level
        assert out["active"]["has_data_file"] is True
        assert "saved and unmodified" in out["active"]["save_state"]

    def test_unsaved_doc_has_no_urn(self, _install):
        d = _Doc("Untitled", saved=False, data_file=None)
        _install(d)
        out = _payload(dg.handler())
        assert out["active"]["document_id"] is None
        assert out["active"]["has_data_file"] is False
        assert "never saved" in out["active"]["save_state"]

    def test_modified_doc_flags_stale_urn(self, _install):
        d = _Doc("WIP", modified=True, data_file=_DataFile(vnum=5))
        _install(d)
        out = _payload(dg.handler())
        assert "unsaved changes" in out["active"]["save_state"]
        assert "5" in out["active"]["save_state"]

    def test_isSaved_false_but_datafile_present_does_not_contradict(self, _install):
        # Platform contradiction (observed live): doc.isSaved can read False on a doc that carries a real URN, version 1,
        # and is unmodified. is_saved must derive from the DataFile (True here), the save_state must
        # read 'saved and unmodified', and the open-doc list must NOT flag it never_saved.
        d = _Doc("Bracket", saved=False, modified=False, data_file=_DataFile(urn="urn:x", vnum=1))
        _install(d)
        out = _payload(dg.handler())
        assert out["active"]["is_saved"] is True
        assert out["active"]["has_data_file"] is True
        assert "saved and unmodified" in out["active"]["save_state"]
        # and no 'never_saved' exception on a doc that plainly has a data file
        assert out["summary"]["exceptions"] == []

    def test_the_active_block_reads_datafile_once(self, _install):
        # every doc.dataFile access is a cloud round-trip on the main thread - the active block must
        # resolve it ONCE and reuse it for the URN/version fields, not re-fetch per field. With no
        # other open docs, the active doc's dataFile is read exactly once across the whole handler.
        class _CountingDoc(FakeFusionDocument):
            """Counts every dataFile fetch - each is a cloud round-trip on the main thread."""
            def __init__(self, data_file):
                super().__init__(name="Bracket", is_saved=True, is_modified=False, version="2.0")
                self._df = data_file
                self.datafile_reads = 0

            @property
            def dataFile(self):
                self.datafile_reads += 1
                return self._df

            @dataFile.setter
            def dataFile(self, value):
                self._df = value

        d = _CountingDoc(_DataFile(urn="urn:x", vnum=2, latest=2))
        _install(active=d, open_docs=[])          # active not in the open list -> isolate the read
        out = _payload(dg.handler())
        assert out["active"]["document_id"] == "urn:x"     # the URN field still populated
        assert d.datafile_reads == 1                        # ... from a single dataFile fetch

    def test_active_block_carries_the_version_lag_sentence(self, _install):
        # ONE sentence covers every held-handle version field and directs cloud identity reads to a
        # fresh data_get; only an affirmative fresh-comparison save verdict confirms advancement.
        d = _Doc("Bracket", data_file=_DataFile(vnum=2, latest=2))
        _install(d)
        note = _payload(dg.handler())["active"]["version_lag_note"]
        assert "version_id" in note and "fresh data_get" in note
        assert "version_confirmed is true only" in note and "false/pending is unknown" in note
        assert "xref_tree" in note and "LAG" in note

    def test_unsaved_active_block_has_no_version_lag_sentence(self, _install):
        # no DataFile -> no version fields -> no lag to warn about
        d = _Doc("Untitled", saved=False, data_file=None)
        _install(d)
        assert "version_lag_note" not in _payload(dg.handler())["active"]


class TestOpenList:
    def test_terse_healthy_doc_collapses(self, _install):
        active = _Doc("A", data_file=_DataFile())
        other = _Doc("B", data_file=_DataFile())          # healthy, not active
        _install(active, [active, other])
        out = _payload(dg.handler())
        assert out["open_count"] == 2
        rows = {r["name"]: r for r in out["open_documents"]}
        # B is healthy + not active -> collapses to its name + open_index (the addressing key stays)
        assert rows["B"]["name"] == "B"
        assert rows["B"]["open_index"] == 1
        assert rows["B"]["document_handle"].startswith("session:")
        # A is active -> keeps the is_active flag
        assert rows["A"]["is_active"] is True
        assert rows["A"]["open_index"] == 0

    def test_open_index_on_every_row_addresses_unsaved_twins(self, _install):
        # open_index is the STABLE session address doc_activate/doc_close accept as 'open:N' - the only
        # handle for an UNSAVED doc that shares a name ('Untitled') and has no URN.
        u1 = _Doc("Untitled", saved=False, data_file=None)
        u2 = _Doc("Untitled", saved=False, data_file=None)
        _install(u1, [u1, u2])
        rows = _payload(dg.handler())["open_documents"]
        assert [r["open_index"] for r in rows] == [0, 1]

    def test_a_doc_whose_item_read_raises_is_a_null_row_offering_no_index(self, _install):
        # documents.item(i) raising (a stale proxy) leaves the slot answering NO document: the row is
        # published so the listing counts what the session holds, it claims nothing about the dead
        # slot's save state, and it carries NO open_index - the index at that position is the one
        # doc_activate/doc_close refuse (TestASlotThatAnsweredNoDocument drives that side). The
        # documents around it keep the indexes they had.
        a = _Doc("A", data_file=_DataFile())
        c = _Doc("C", saved=False, data_file=None)
        _install(a, [a, _Doc("dead"), c])
        dg.app.documents._raises_at = 1
        out = _payload(dg.handler())
        rows = out["open_documents"]
        assert rows[1] == {"name": None, "readable": False}
        assert [r["open_index"] for r in rows if "open_index" in r] == [0, 2]
        assert rows[2]["name"] == "C"                    # the third doc, at its own address
        assert out["summary"]["open_count"] == 3
        assert [e["name"] for e in out["summary"]["exceptions"]] == ["C"]

    def test_summary_leads_with_unsaved_exceptions(self, _install):
        # the summary names the docs with unsaved work (what close-all would lose) before the
        # full list. Healthy docs are NOT exceptions.
        active = _Doc("Main", data_file=_DataFile())
        clean = _Doc("Clean", data_file=_DataFile())
        never = _Doc("Untitled", saved=False, data_file=None)
        dirty = _Doc("WIP", modified=True, data_file=_DataFile())
        _install(active, [active, clean, never, dirty])
        out = _payload(dg.handler())
        s = out["summary"]
        assert s["open_count"] == 4
        names = {e["name"]: e for e in s["exceptions"]}
        assert set(names) == {"Untitled", "WIP"}                 # clean + active(saved) excluded
        assert names["Untitled"]["unsaved"] == ["never_saved"]
        assert names["WIP"]["unsaved"] == ["modified"]

    def test_modified_dependency_doc_keeps_its_flag(self, _install):
        active = _Doc("Main", data_file=_DataFile())
        dep = _Doc("Ref", modified=True, data_file=_DataFile())
        _install(active, [active, dep])
        rows = {r["name"]: r for r in _payload(dg.handler())["open_documents"]}
        assert rows["Ref"]["is_modified"] is True          # the interesting flag survives the razor


class TestASlotThatAnsweredNoDocument:
    """A slot whose documents.item(i) answered NOTHING is a hole in the listing, not a document.

    The row is published, so the listing counts what the session holds - but it carries no
    'open:N' offer: doc_activate/doc_close resolve that index through _doc_common, which refuses
    the one naming such a slot and lists it as carrying no handle. Both sides are read off ONE
    session here, since an address is only an offer if the tool it names accepts it."""

    def _session(self, monkeypatch, docs, raises_at):
        app = FakeApplication(active_document=docs[0],
                              documents=_Docs(docs, item_raises_at=raises_at))
        for mod in (dg, dk, da, dcl):
            monkeypatch.setattr(mod, "app", app)

    def _three(self, monkeypatch):
        """A readable document, a slot that answers nothing, and an unsaved document behind it."""
        self._session(monkeypatch, [_Doc("A", data_file=_DataFile()), _Doc("dead"),
                                    _Doc("Untitled", saved=False, data_file=None)], 1)

    def test_every_open_index_published_reaches_a_document(self, monkeypatch):
        # the offer side: an index this read publishes is one the resolver behind 'open:N' accepts,
        # and only the hole's row goes without one.
        self._three(monkeypatch)
        rows = _payload(dg.handler())["open_documents"]
        offered = [r["open_index"] for r in rows if "open_index" in r]
        assert offered == [0, 2]
        for idx in offered:
            assert dk._find_open_document("open:%d" % idx)[0] is not None
        # the razor holds: only the hole is marked, so readable=false IS the hole test
        assert [r.get("readable") for r in rows] == [None, False, None]

    def test_the_hole_offers_no_index_because_open_N_there_is_refused(self, monkeypatch):
        # the refusal side, at the position the hole occupies: both tools the note names refuse
        # 'open:1', and their own listing calls that slot handle-less - so publishing the index
        # would offer an address rejected on arrival.
        self._three(monkeypatch)
        assert _payload(dg.handler())["open_documents"][1] == {"name": None, "readable": False}
        for handler in (da.handler, dcl.handler):
            res = handler(name="open:1")
            assert res["isError"] is True
            assert "no handle - the document did not read" in error_message(res)

    def test_the_note_discloses_the_slot_and_promises_nothing_for_it(self, monkeypatch):
        self._three(monkeypatch)
        note = _payload(dg.handler())["note"]
        assert "1 open slot(s) answered NO document" in note
        assert "carries no open_index" in note and "nothing there to retry" in note

    def test_a_session_without_a_hole_says_nothing_about_one(self, monkeypatch):
        # the zero boundary: the disclosure is paid for only by a session that has one.
        self._session(monkeypatch, [_Doc("A", data_file=_DataFile())], None)
        assert "answered NO document" not in _payload(dg.handler())["note"]

    def test_two_holes_are_counted_not_merely_flagged(self, monkeypatch):
        self._session(monkeypatch, [_Doc("A", data_file=_DataFile()), _Doc("dead"), _Doc("gone")],
                      {1, 2})
        out = _payload(dg.handler())
        assert [r for r in out["open_documents"] if r.get("readable") is False] == \
            [{"name": None, "readable": False}] * 2
        assert "2 open slot(s) answered NO document" in out["note"]
        assert out["open_count"] == 3          # the count still states what the session holds

    def test_a_hole_at_the_cap_is_listed_and_the_one_past_it_is_not(self, monkeypatch):
        # the cap boundary both ways: the sentence speaks of the rows LISTED, so a hole the cap cut
        # is neither published nor counted - 'truncated' is what reports that one.
        self._session(monkeypatch, [_Doc("A", data_file=_DataFile()), _Doc("dead")], 1)
        at_cap = _payload(dg.handler(max_results=2))
        assert at_cap["open_documents"][1] == {"name": None, "readable": False}
        assert "1 open slot(s) answered NO document" in at_cap["note"]
        past_cap = _payload(dg.handler(max_results=1))
        assert len(past_cap["open_documents"]) == 1 and past_cap["truncated"] is True
        assert "answered NO document" not in past_cap["note"]


class TestGuards:
    def test_no_active_document_errors(self, _install):
        _install(active=None, open_docs=[])
        res = dg.handler()
        assert "no active document" in error_message(res).lower()


class TestCaps:
    def test_under_cap_untruncated_and_unchanged(self, _install):
        active = _Doc("Main", data_file=_DataFile())
        others = [_Doc(f"D{i}", data_file=_DataFile()) for i in range(5)]
        _install(active, [active] + others)
        out = _payload(dg.handler())
        assert out["truncated"] is False
        assert len(out["open_documents"]) == 6
        assert out["open_count"] == 6

    def test_at_cap_truncates_and_flags(self, _install):
        active = _Doc("Main", data_file=_DataFile())
        others = [_Doc(f"D{i}", data_file=_DataFile()) for i in range(60)]
        _install(active, [active] + others)
        out = _payload(dg.handler(max_results=50))
        assert out["truncated"] is True
        assert len(out["open_documents"]) == 50
        # the full count is still honest, even though the array is capped
        assert out["open_count"] == 61

    def test_unsaved_exceptions_computed_over_the_full_list_even_when_capped(self, _install):
        # a doc with unsaved work beyond the cap must still show up in 'summary.exceptions'.
        active = _Doc("Main", data_file=_DataFile())
        clean = [_Doc(f"D{i}", data_file=_DataFile()) for i in range(60)]
        dirty = _Doc("WIP", modified=True, data_file=_DataFile())
        _install(active, [active] + clean + [dirty])
        out = _payload(dg.handler(max_results=50))
        assert out["truncated"] is True
        names = {e["name"] for e in out["summary"]["exceptions"]}
        assert "WIP" in names


# ── (A) versions slice ────────────────────────────────────────────────────────

def _Ver(num, vid=None, date=1_700_000_000, desc="", is_milestone=False):
    """One older version of the file - itself a DataFile, addressed by its number/id."""
    return FakeDataFile(f"Bracket v{num}", version=num, version_id=vid or f"urn:v:{num}",
                        date_created=date, description=desc, is_milestone=is_milestone)


class _NoNumber(FakeDataFile):
    """A version row whose versionNumber will not read - a hole in the history, never a row to drop
    silently."""
    def __init__(self, version_id):
        super().__init__("Bracket", version_id=version_id)

    @property
    def versionNumber(self, _install):
        raise RuntimeError("3 : cloud read failed")

    @versionNumber.setter
    def versionNumber(self, value):
        pass


class _MStone:
    """One Milestone: a NAME plus the DataFile version it points at (the whole object)."""
    def __init__(self, name, version): self.name = name; self.version = version


class _MStones:
    """The DataFile's Milestones collection; unreadable=True models the measured count of None.
    Counts item() reads so the walk's bound can be asserted."""
    def __init__(self, items=(), unreadable=False):
        self._i = list(items)
        self._unreadable = unreadable
        self.reads = 0

    @property
    def count(self): return None if self._unreadable else len(self._i)

    def item(self, i):
        self.reads += 1
        return self._i[i]


def _DFileVers(open_num, latest, others, desc="open", is_milestone=False, milestones=None):
    """A DataFile that is itself the tip version and carries df.versions for the older ones."""
    return FakeDataFile("Bracket", file_id="urn:lineage", version=open_num, latest_version=latest,
                        version_id=f"urn:v:{open_num}", description=desc,
                        is_milestone=is_milestone, versions=others, milestones=milestones)


class TestVersions:
    def test_newest_first_and_capped(self, _install):
        # versions given out of order; the slice sorts them newest-first and caps the list.
        df = _DFileVers(open_num=3, latest=5, others=[_Ver(1), _Ver(4), _Ver(2), _Ver(5)])
        _install(_Doc("Bracket", data_file=df))
        out = dg._slice_versions(versions_max=3)
        assert out["available"] is True
        assert [r["version_number"] for r in out["versions"]] == [5, 4, 3]
        assert out["truncated"] is True
        assert out["version_count"] == 5          # 1,2,3(open),4,5 - df merged + deduped

    def test_flags_latest_and_open_version(self, _install):
        df = _DFileVers(open_num=3, latest=5, others=[_Ver(5, desc="tip"), _Ver(4)])
        _install(_Doc("Bracket", data_file=df))
        rows = {r["version_number"]: r for r in dg._slice_versions()["versions"]}
        assert rows[5]["is_latest"] is True
        assert rows[3]["is_open_in_session"] is True
        assert rows[5]["date_utc"] is not None    # epoch -> ISO string

    def test_unsaved_document_has_no_history(self, _install):
        _install(_Doc("Untitled", saved=False, data_file=None))
        out = dg._slice_versions()
        assert out["available"] is False
        assert "never saved" in out["note"].lower()


class TestVersionHistoryCompleteness:
    """The readable/complete marker pair, mirroring the xref_tree and used_in slices: a version
    list is authoritative ONLY on a full walk: without these markers an unreadable df.versions
    reads as version_count=1 with truncated=false - a lineage that looks COMPLETE."""

    def test_a_full_read_is_marked_complete(self, _install):
        df = _DFileVers(open_num=2, latest=2, others=[_Ver(1)])
        _install(_Doc("Bracket", data_file=df))
        out = dg._slice_versions()
        assert out["history_readable"] is True and out["history_complete"] is True
        assert out["unreadable_count"] == 0

    def test_unreadable_versions_collection_is_not_a_complete_history(self, _install):
        # the versions collection whose count read RAISES - a cloud read that failed
        df = FakeDataFile("Bracket", file_id="urn:lineage", version=3, latest_version=9,
                          versions_raise="versions unavailable")
        _install(_Doc("Bracket", data_file=df))
        out = dg._slice_versions()
        assert out["version_count"] == 1            # only the open version could be read
        assert out["history_readable"] is False and out["history_complete"] is False
        # the note names the marker that separates a whole lineage from a short one
        assert "only history_complete=true" in out["note"]

    def test_a_version_that_will_not_read_is_counted_not_silently_dropped(self, _install):
        # a row skipped without a marker shortens the history while the payload still reads whole.
        df = _DFileVers(open_num=2, latest=2, others=[_Ver(1), _NoNumber("urn:v:?")])
        _install(_Doc("Bracket", data_file=df))
        out = dg._slice_versions()
        assert out["unreadable_count"] == 1
        assert out["history_readable"] is True and out["history_complete"] is False

    def test_a_duplicate_of_the_open_version_is_not_counted_unreadable(self, _install):
        # the boundary between "already seen" and "could not be read": df is added first, so its
        # own row arriving again through df.versions is a de-dup, not a hole.
        df = _DFileVers(open_num=2, latest=2, others=[_Ver(2), _Ver(1)])
        _install(_Doc("Bracket", data_file=df))
        out = dg._slice_versions()
        assert out["version_count"] == 2 and out["unreadable_count"] == 0
        assert out["history_complete"] is True

    def test_a_capped_list_is_readable_but_not_complete(self, _install):
        df = _DFileVers(open_num=3, latest=3, others=[_Ver(1), _Ver(2)])
        _install(_Doc("Bracket", data_file=df))
        out = dg._slice_versions(versions_max=2)
        assert out["truncated"] is True
        assert out["history_readable"] is True and out["history_complete"] is False


class TestVersionMilestones:
    def test_milestone_row_carries_flag_and_name(self, _install):
        # the NAME lives only in the Milestones collection - the row is matched to it by version.
        v4 = _Ver(4, is_milestone=True)
        df = _DFileVers(open_num=3, latest=4, others=[v4, _Ver(2)],
                        milestones=_MStones([_MStone("v1 release", v4)]))
        _install(_Doc("Bracket", data_file=df))
        out = dg._slice_versions()
        rows = {r["version_number"]: r for r in out["versions"]}
        assert rows[4]["is_milestone"] is True
        assert rows[4]["milestone_name"] == "v1 release"
        assert rows[2]["is_milestone"] is False
        assert rows[2]["milestone_name"] is None
        assert out["milestone_count"] == 1
        assert out["milestone_names_readable"] is True

    def test_flag_that_cannot_be_read_at_all_is_null_not_false(self, _install):
        # a version whose isMilestone cannot be read, with the collection unreadable too: nothing is
        # known about this row, so it is null and counted as unreadable - never as 'not a milestone'.
        df = _DFileVers(open_num=1, latest=2, others=[_Ver(2, is_milestone=None)],
                        milestones=_MStones(unreadable=True))
        _install(_Doc("Bracket", data_file=df))
        out = dg._slice_versions()
        rows = {r["version_number"]: r for r in out["versions"]}
        assert rows[2]["is_milestone"] is None
        assert rows[2].get("flag_lagging") is None
        assert out["milestone_count"] == 0
        assert out["milestone_unreadable_count"] == 1
        assert out["milestone_names_readable"] is False

    def test_flag_false_while_the_collection_names_it_resolves_to_milestone(self, _install):
        # The defensive precedence, not a measured window: in both measured runs the collection and
        # the flag and the collection arrive together after a doc_save_milestone, so neither
        # source is known to lead. Where the collection DOES list a version whose own flag still
        # reads false, the collection wins and the row publishes flag_lagging.
        v2 = _Ver(2, is_milestone=False)
        df = _DFileVers(open_num=1, latest=2, others=[v2],
                        milestones=_MStones([_MStone("v2 release", v2)]))
        _install(_Doc("Bracket", data_file=df))
        out = dg._slice_versions()
        row = {r["version_number"]: r for r in out["versions"]}[2]
        assert row["is_milestone"] is True
        assert row["milestone_name"] == "v2 release"
        assert row["flag_lagging"] is True
        assert out["milestone_count"] == 1
        assert "flag_lagging" in out["note"]

    def test_the_note_says_the_metadata_lags_and_that_unknown_is_not_absence(self, _install):
        # the two halves a caller acts on: metadata LAGS the save (so re-read), and an unreadable
        # milestone signal is UNKNOWN - never evidence that a version is not a milestone.
        df = _DFileVers(open_num=1, latest=1, others=[], milestones=_MStones([]))
        _install(_Doc("Bracket", data_file=df))
        note = dg._slice_versions()["note"]
        assert "numbers_may_lag" in note and "re-read" in note
        assert "UNKNOWN" in note and "never 'not a milestone'" in note

    def test_a_tip_that_landed_seconds_ago_flags_the_numbers_as_possibly_lagging(self, _install):
        # the measured state a caller has to act on: a read taken right after a save answered
        # latest 2 while the lineage had already reached 3, so a fresh tip is published as such.
        now = int(time.time())
        df = _DFileVers(open_num=1, latest=2, others=[_Ver(2, date=now - 3)])
        _install(_Doc("Bracket", data_file=df))
        out = dg._slice_versions()
        assert out["numbers_may_lag"] is True
        assert out["tip_age_seconds"] == 3

    def test_the_lag_window_ends_exactly_at_twenty_seconds(self, _install):
        # the boundary: a tip AT the window is outside it, one second inside is within.
        window = dg._doc_common.VERSION_LAG_WINDOW_S
        now = int(time.time())
        for age, flagged in ((window, False), (window - 1, True)):
            df = _DFileVers(open_num=1, latest=2, others=[_Ver(2, date=now - age)])
            _install(_Doc("Bracket", data_file=df))
            assert dg._slice_versions()["numbers_may_lag"] is flagged, age

    def test_an_unreadable_tip_date_claims_neither_lag_nor_settled(self, _install):
        # no date is UNKNOWN, so BOTH keys are null - false would call these numbers settled on a
        # read that never established it.
        df = _DFileVers(open_num=1, latest=1, others=[])
        df.dateCreated = None
        _install(_Doc("Bracket", data_file=df))
        out = dg._slice_versions()
        assert out["tip_age_seconds"] is None
        assert out["numbers_may_lag"] is None

    def test_the_lag_window_is_the_one_the_restore_tool_waits_on(self):
        # one measured fact, one home: doc_restore_version pays its re-fetch against this window,
        # so a change here must not leave the two tools describing different clouds.
        drv = load_tool("doc_restore_version")
        assert drv._TIP_RECHECK_S == float(dg._doc_common.VERSION_LAG_WINDOW_S)

    def test_milestone_walk_is_bounded_by_the_row_cap(self, _install):
        # each entry's .version hop is a cloud read - the walk may not outrun the cap that bounds
        # the published rows, and a walk that stopped short says so.
        vers = [_Ver(i, is_milestone=True) for i in range(1, 6)]
        stones = _MStones([_MStone(f"m{v.versionNumber}", v) for v in vers])
        df = _DFileVers(open_num=5, latest=5, others=vers, milestones=stones)
        _install(_Doc("Bracket", data_file=df))
        out = dg._slice_versions(versions_max=2)
        assert stones.reads == 2                       # not all 5 milestones were hopped
        assert out["milestone_walk_truncated"] is True

    def test_milestone_count_covers_every_known_row_not_just_the_capped_ones(self, _install):
        # the cap limits the published list, not the rollup - version_count and milestone_count
        # must describe the SAME set of known versions.
        v1, v2 = _Ver(1, is_milestone=True), _Ver(2, is_milestone=True)
        df = _DFileVers(open_num=3, latest=3, others=[v1, v2],
                        milestones=_MStones([_MStone("a", v1), _MStone("b", v2)]))
        _install(_Doc("Bracket", data_file=df))
        out = dg._slice_versions(versions_max=1)
        assert out["truncated"] is True
        assert len(out["versions"]) == 1
        assert out["version_count"] == 3
        assert out["milestone_count"] == 2      # both milestones fell outside the capped list


# ── (B) xref_tree slice ───────────────────────────────────────────────────────

def _DRef(source, current, latest, ood):
    """One xref link: the source file it points at, the version it holds and the latest one."""
    return FakeDocumentReference(data_file=FakeDataFile(source, latest_version=latest),
                                 version=current, out_of_date=ood)


def _Comp(name, comp_local=None):
    """The component behind an occurrence. `occurrences` is the COMPONENT-LOCAL collection - the one
    place an occurrence with an unresolved reference is visible, since childOccurrences drops it."""
    return MakeComp(name, occurrences=list(comp_local or []))


def _Occ(path, is_ref=False, dref=None, children=None, comp_local=None):
    # A real Occurrence ALWAYS answers .component; one that raises is the unresolved-reference
    # signal (_BrokenOcc below). Its component-local collection defaults to the assembly-context
    # children, which is what a design with no unresolved reference looks like.
    name = path.split("+")[-1]
    return make_occurrence(
        path=path, referenced=is_ref, document_reference=dref, children=list(children or []),
        component=_Comp(name.split(":")[0],
                        comp_local if comp_local is not None else (children or [])))


# The message each read on an occurrence whose referenced component will not load throws with.
_BROKEN_OCC_RAISE = ("3 : The occurrence's referenced component is unavailable (broken or missing "
                     "external reference).")
_BROKEN_PATH_RAISE = "2 : InternalValidationError : path.valid()"


def _BrokenOcc(name):
    """An occurrence whose referenced component will not load: reading `component` RAISES, which is
    the ONLY reliable signal - isReferencedComponent reads False and documentReference raises the
    same text an ordinary local occurrence gives (both measured on a live specimen)."""
    return make_occurrence(path=name, referenced=False, raises_on={
        "component": _BROKEN_OCC_RAISE,
        "fullPathName": _BROKEN_PATH_RAISE,
        "childOccurrences": _BROKEN_PATH_RAISE,
        "documentReference": "3 : Occurrence is not referencing an external component"})


class _DeriveFeat:
    """A DeriveFeature and the link it holds. DeriveFeature carries no live shape dump, so it has
    no shared fake."""
    def __init__(self, name, dref=None):
        self.name = name
        self.documentReference = dref


def _Root(occs, name="Root", derive_feats=None):
    """The root component of the walked design: its occurrences, and the deriveFeatures collection
    the derive arm reads (empty by default - most tests here do not derive)."""
    root = MakeComp(name, occurrences=list(occs))
    root.component = _Comp(name, occs)
    features = FakeFeatures()
    features.deriveFeatures = _NamedCollection(list(derive_feats or []))
    root.features = features
    return root


def _use_design(monkeypatch, root):
    monkeypatch.setattr(dg, "design", lambda: MakeDesign(comp=root))


class TestXrefTree:
    def test_deep_stale_reference_is_found(self, monkeypatch):
        # a stale xref nested inside a LOCAL sub-assembly must still be walked and flagged.
        stale = _Occ("Sub:1+Part:1", is_ref=True, dref=_DRef("Part", 2, 5, True))
        sub = _Occ("Sub:1", is_ref=False, children=[stale])
        _use_design(monkeypatch, _Root([sub]))
        out = dg._slice_xref_tree()
        assert out["reference_link_count"] == 1
        r = out["references"][0]
        assert r["depth"] == 2 and r["out_of_date"] is True and r["source_document"] == "Part"
        assert out["stale_count"] == 1
        assert out["all_current"] is False

    def test_all_current_true_only_when_every_ref_fresh(self, monkeypatch):
        a = _Occ("A:1", is_ref=True, dref=_DRef("A", 3, 3, False))
        b = _Occ("B:1", is_ref=True, dref=_DRef("B", 1, 1, False))
        _use_design(monkeypatch, _Root([a, b]))
        out = dg._slice_xref_tree()
        assert out["reference_link_count"] == 2
        assert out["stale_count"] == 0
        assert out["all_current"] is True

    def test_cap_truncates_and_blocks_all_current(self, monkeypatch):
        # every EXAMINED ref is fresh, but a capped walk cannot claim all-current (partial knowledge).
        occs = [_Occ(f"R{i}:1", is_ref=True, dref=_DRef(f"R{i}", 1, 1, False)) for i in range(5)]
        _use_design(monkeypatch, _Root(occs))
        out = dg._slice_xref_tree(xref_max=2)
        assert out["truncated"] is True
        assert len(out["references"]) == 2
        assert out["all_current"] is False

    def test_unreadable_reference_blocks_all_current(self, monkeypatch):
        good = _Occ("A:1", is_ref=True, dref=_DRef("A", 1, 1, False))
        bad = _Occ("B:1", is_ref=True, dref=None)   # referenced, but documentReference unreadable
        _use_design(monkeypatch, _Root([good, bad]))
        out = dg._slice_xref_tree()
        assert out["unreadable_count"] == 1
        assert out["all_current"] is False
        bad_row = {x["path"]: x for x in out["references"]}["B:1"]
        assert bad_row["readable"] is False and "warning" in bad_row

    def test_max_depth_bounds_walk_and_flags_partial(self, monkeypatch):
        deep = _Occ("Sub:1+Part:1", is_ref=True, dref=_DRef("Part", 1, 1, False))
        sub = _Occ("Sub:1", is_ref=False, children=[deep])
        _use_design(monkeypatch, _Root([sub]))
        out = dg._slice_xref_tree(max_depth=1)
        assert out["depth_capped"] is True
        assert out["reference_link_count"] == 0        # the depth-2 ref was not walked
        assert out["all_current"] is False        # a depth-capped walk is partial

    def test_occurrence_row_carries_xref_kind(self, monkeypatch):
        a = _Occ("A:1", is_ref=True, dref=_DRef("A", 1, 1, False))
        _use_design(monkeypatch, _Root([a]))
        out = dg._slice_xref_tree()
        assert out["references"][0]["kind"] == "xref"


class TestXrefTreeUnresolved:
    """The measured defect: this slice returned unreadable_count 0 / all_current true on a document
    holding an occurrence whose referenced component would not load. Two independent reasons - the
    walk gate reads isReferencedComponent (FALSE on a broken reference) and the walk descends
    childOccurrences (which drops it)."""

    def test_zero_unresolved_leaves_the_rollup_clean(self, monkeypatch):
        a = _Occ("A:1", is_ref=True, dref=_DRef("A", 1, 1, False))
        _use_design(monkeypatch, _Root([a]))
        out = dg._slice_xref_tree()
        assert out["unresolved_count"] == 0
        assert out["unreadable_count"] == 0 and out["all_current"] is True
        assert "UNRESOLVED" not in out["note"]

    def test_a_broken_TOP_LEVEL_occurrence_is_counted_and_blocks_all_current(self, monkeypatch):
        _use_design(monkeypatch, _Root([_BrokenOcc("45740")]))
        out = dg._slice_xref_tree()
        assert out["unresolved_count"] == 1
        assert out["unreadable_count"] == 1        # it flows into the EXISTING unreadable machinery
        assert out["all_current"] is False
        row = out["references"][0]
        assert row["kind"] == "unresolved" and row["readable"] is False
        assert "45740" in row["path"]
        assert _BROKEN_OCC_RAISE in row["warning"]
        assert "out_of_date" not in row and "source_document" not in row

    def test_a_child_that_only_the_component_local_collection_holds_is_found(self, monkeypatch):
        # the structural blindness: the broken child is absent from childOccurrences, so a walk that
        # only descends there can never reach it however its gate is written.
        broken = _BrokenOcc("45740")
        container = _Occ("Op1:1", is_ref=False, children=[_Occ("Part:1", is_ref=False)],
                         comp_local=[broken, _Occ("Part:1", is_ref=False)])
        _use_design(monkeypatch, _Root([container]))
        out = dg._slice_xref_tree()
        assert out["unresolved_count"] == 1
        assert out["references"][0]["path"] == "Op1:1+45740"
        assert out["references"][0]["depth"] == 2

    def test_the_note_says_the_reference_cannot_be_refreshed(self, monkeypatch):
        # doc_update_xref acts on a DocumentReference, and a broken reference has none - promising
        # a refresh would send the agent at a tool that cannot touch it.
        _use_design(monkeypatch, _Root([_BrokenOcc("45740")]))
        note = dg._slice_xref_tree()["note"]
        assert "doc_update_xref cannot refresh it" in note
        assert "browser tree" in note

    def test_the_cap_stops_collecting_unresolved_rows_and_flags_the_walk_partial(self, monkeypatch):
        # a cap reached is a PARTIAL rollup: all_current must not be claimed over rows never walked,
        # and that is just as true when the row that hit the cap was an unresolved one.
        a = _Occ("A:1", is_ref=True, dref=_DRef("A", 1, 1, False))
        _use_design(monkeypatch, _Root([a, _BrokenOcc("45740")]))
        out = dg._slice_xref_tree(xref_max=1)
        assert out["truncated"] is True
        assert out["all_current"] is False
        assert out["reference_link_count"] == 1
        assert out["unresolved_count"] == 0        # never collected, so never counted

    def test_the_cap_also_stops_the_component_local_unresolved_scan(self, monkeypatch):
        # the unresolved children are found on a SECOND collection; that pass shares the one cap, or
        # a capped walk would keep growing past the bound the caller set.
        broken = _BrokenOcc("45740")
        container = _Occ("Op1:1", is_ref=True, dref=_DRef("Op1", 1, 1, False),
                         comp_local=[broken])
        _use_design(monkeypatch, _Root([container]))
        out = dg._slice_xref_tree(xref_max=1)
        assert out["truncated"] is True
        assert out["reference_link_count"] == 1
        assert out["unresolved_count"] == 0

    def test_the_count_key_names_its_noun(self, monkeypatch):
        # UNRES-2: this counts LINKS while workspace_orient's references key counts DOCUMENTS; the
        # two disagreed under one name.
        _use_design(monkeypatch, _Root([_Occ("A:1", is_ref=True, dref=_DRef("A", 1, 1, False))]))
        out = dg._slice_xref_tree()
        assert "reference_link_count" in out and "reference_count" not in out
        assert "referenced_documents" in out["note"]


class _SpyOcc(FakeOccurrence):
    """An occurrence that counts every read of its component - the evidence for whether the walk
    reached it at all, which a row count alone cannot show."""
    def __init__(self, path):
        super().__init__(path=path, component=_Comp(path.split("+")[-1].split(":")[0], []))
        self.reads = 0

    @property
    def component(self):
        self.reads += 1
        return self._component


class TestXrefTreeWalkGuards:
    """Every read between the root and a row is guarded, and each guard leaves the rollup HONEST -
    an unwalked branch never reads as an examined-and-clean one."""

    def test_no_active_design_is_unavailable_not_an_empty_walk(self, monkeypatch):
        monkeypatch.setattr(dg, "design", lambda: None)
        out = dg._slice_xref_tree()
        assert out["available"] is False
        assert "needs a design document" in out["note"]
        assert "all_current" not in out          # no verdict is formed at all

    def test_a_design_with_no_root_component_is_unavailable(self, monkeypatch):
        rootless = MakeDesign()
        rootless.rootComponent = None      # the walk has nothing to start from
        monkeypatch.setattr(dg, "design", lambda: rootless)
        out = dg._slice_xref_tree()
        assert out["available"] is False
        assert "no root component" in out["note"]

    def test_an_unreadable_child_collection_stops_that_branch_without_a_crash(self, monkeypatch):
        # childOccurrences reading None is a branch NOT walked; the rest of the tree still reports.
        sub = make_occurrence(path="Sub:1", component=_Comp("Sub", []),
                              raises_on={"childOccurrences": "3 : cloud read failed"})
        keeper = _Occ("A:1", is_ref=True, dref=_DRef("A", 1, 1, False))
        _use_design(monkeypatch, _Root([sub, keeper]))
        out = dg._slice_xref_tree()
        assert out["reference_link_count"] == 1
        assert out["references"][0]["path"] == "A:1"
        assert out["walk_complete"] is False and out["all_current"] is False
        assert out["unreadable_count"] == 0

    def test_an_item_the_collection_will_not_hand_over_is_skipped(self, monkeypatch):
        keeper = _Occ("A:1", is_ref=True, dref=_DRef("A", 1, 1, False))
        _use_design(monkeypatch, _Root([None, keeper]))
        out = dg._slice_xref_tree()
        assert out["reference_link_count"] == 1
        assert out["walk_complete"] is False and out["all_current"] is False
        assert out["unreadable_count"] == 0

    def test_the_cap_reached_in_a_NESTED_branch_stops_the_outer_walk_too(self, monkeypatch):
        # once truncated, the remaining siblings are not even VISITED - a walk that kept descending
        # would pay the whole tree's cost to publish nothing, and the cap exists to bound that cost.
        deep = [_Occ(f"Sub:1+X{i}:1", is_ref=True, dref=_DRef("X", 1, 1, False)) for i in (1, 2)]
        sub = _Occ("Sub:1", is_ref=False, children=deep)
        tail = _SpyOcc("Tail:1")
        root = _Root([sub, tail])
        original, reads = root.occurrences.item, []

        def item(i):
            reads.append(i)
            return original(i)

        monkeypatch.setattr(root.occurrences, "item", item)
        _use_design(monkeypatch, root)
        out = dg._slice_xref_tree(xref_max=1)
        assert out["truncated"] is True
        assert out["reference_link_count"] == 1
        assert [r["path"] for r in out["references"]] == ["Sub:1+X1:1"]
        assert out["all_current"] is False
        assert tail.reads == 0        # the sibling after the cap was never touched
        assert reads == [0]           # even fetching the outer sibling would exceed the stop

    def test_a_reference_whose_freshness_fields_will_not_read_is_unreadable_not_current(self, monkeypatch):
        # the DocumentReference EXISTS but its isOutOfDate does not read: publishing out_of_date
        # false there would assert freshness nothing was read from.
        a = _Occ("A:1", is_ref=True, dref=_DRef("A", 1, 1, None))
        _use_design(monkeypatch, _Root([a]))
        out = dg._slice_xref_tree()
        row = out["references"][0]
        assert row["readable"] is False
        assert "out_of_date" not in row
        assert out["unreadable_count"] == 1 and out["all_current"] is False


# ── (B2) xref_tree slice - derive links ───────────────────────────────────────

class TestXrefTreeDerive:
    def test_derive_row_present_with_kind_field(self, monkeypatch):
        # a derive's DocumentReference lives on the FEATURE, not an occurrence - this is the row the
        # occurrence-only walk above could never produce.
        dref = _DRef("DeriveSrc", 1, 1, False)
        root = _Root([], name="Root", derive_feats=[_DeriveFeat("Derive1", dref)])
        _use_design(monkeypatch, root)
        out = dg._slice_xref_tree()
        assert out["reference_link_count"] == 1
        row = out["references"][0]
        assert row["kind"] == "derive"
        assert row["path"] == "Root:Derive1"
        assert row["source_document"] == "DeriveSrc"
        assert row["out_of_date"] is False
        assert out["all_current"] is True

    def test_stale_derive_flips_all_current_false(self, monkeypatch):
        dref = _DRef("DeriveSrc", 1, 2, True)   # pinned at v1, source latest v2, out of date
        root = _Root([], derive_feats=[_DeriveFeat("Derive1", dref)])
        _use_design(monkeypatch, root)
        out = dg._slice_xref_tree()
        assert out["stale_count"] == 1
        assert out["all_current"] is False
        assert out["references"][0]["out_of_date"] is True

    def test_empty_derive_features_no_crash(self, monkeypatch):
        root = _Root([], derive_feats=[])
        _use_design(monkeypatch, root)
        out = dg._slice_xref_tree()
        assert out["reference_link_count"] == 0
        assert out["all_current"] is True

    def test_component_without_features_attribute_is_incomplete(self, monkeypatch):
        # A missing derive collection is unknown coverage, not proof that the component has no links.
        bare = MakeComp("Root")
        _use_design(monkeypatch, bare)
        out = dg._slice_xref_tree()
        assert out["reference_link_count"] == 0
        assert out["walk_complete"] is False and out["all_current"] is False

    def test_unreadable_derive_reference_blocks_all_current(self, monkeypatch):
        feat = _DeriveFeat("Derive1", dref=None)   # a derive feature whose documentReference is unreadable
        root = _Root([], derive_feats=[feat])
        _use_design(monkeypatch, root)
        out = dg._slice_xref_tree()
        assert out["unreadable_count"] == 1
        assert out["all_current"] is False
        row = out["references"][0]
        assert row["readable"] is False and "warning" in row

    def test_both_kinds_present_and_rolled_up_together(self, monkeypatch):
        occ = _Occ("A:1", is_ref=True, dref=_DRef("A", 1, 1, False))
        dref = _DRef("DeriveSrc", 1, 1, False)
        root = _Root([occ], derive_feats=[_DeriveFeat("Derive1", dref)])
        _use_design(monkeypatch, root)
        out = dg._slice_xref_tree()
        assert out["reference_link_count"] == 2
        kinds = {r["kind"] for r in out["references"]}
        assert kinds == {"xref", "derive"}

    def test_cap_is_shared_across_both_kinds(self, monkeypatch):
        occ = _Occ("A:1", is_ref=True, dref=_DRef("A", 1, 1, False))
        feats = [_DeriveFeat(f"Derive{i}", _DRef(f"D{i}", 1, 1, False)) for i in range(2)]
        root = _Root([occ], derive_feats=feats)
        _use_design(monkeypatch, root)
        out = dg._slice_xref_tree(xref_max=2)
        assert len(out["references"]) == 2         # 1 xref + 2 derive = 3 total, capped to 2
        assert out["truncated"] is True
        assert out["all_current"] is False          # a capped walk is partial knowledge


# ── (C) used_in slice (where-used / reverse references) ───────────────────────

def _Parent(name="P", urn="urn:p", ext="f3d", vnum=2, latest=3):
    """A parent DataFile - a document that REFERENCES the active one (drawing / assembly)."""
    return FakeDataFile(name, file_id=urn, extension=ext, version=vnum, latest_version=latest,
                        web_url="https://fusion.example/p")


def _DFileParents(parents, has=True):
    """A DataFile carrying its incoming (parent) references for the used_in slice. A parent slot
    holding None is the reference that could not be resolved."""
    df = FakeDataFile("Bracket", file_id="urn:lineage", parent_refs=parents)
    if not has:
        df._parent_refs = []
    return df


class TestUsedIn:
    def test_drawing_reference_appears_and_is_typed(self, _install):
        # the prove-it bite: a drawing made from this design shows up in used_in, typed 'drawing'.
        drawing = _Parent(name="Bracket Drawing", urn="urn:draw", ext="f2d", vnum=1, latest=1)
        _install(_Doc("Bracket", data_file=_DFileParents([drawing])))
        out = dg._slice_used_in()
        assert out["available"] is True
        assert out["query_complete"] is True
        assert out["reference_count"] == 1
        r = out["references"][0]
        assert r["name"] == "Bracket Drawing"
        assert r["type"] == "drawing"
        assert r["document_id"] == "urn:draw"
        assert out["by_type"] == {"drawing": 1}

    def test_mixed_parents_rolled_up_by_type(self, _install):
        parents = [_Parent(name="Asm", ext="f3d"), _Parent(name="Dwg", ext="f2d")]
        _install(_Doc("Part", data_file=_DFileParents(parents)))
        out = dg._slice_used_in()
        assert out["by_type"] == {"design": 1, "drawing": 1}
        assert out["parent_count"] == 2

    def test_no_references_is_empty_not_error(self, _install):
        # a design nothing uses -> empty list, query_complete True, NOT an error.
        _install(_Doc("Lonely", data_file=_DFileParents([], has=False)))
        out = dg._slice_used_in()
        assert out["available"] is True
        assert out["reference_count"] == 0
        assert out["references"] == []
        assert out["query_complete"] is True
        assert out["has_parent_references"] is False

    def test_cap_truncates_and_blocks_query_complete(self, _install):
        parents = [_Parent(name=f"P{i}") for i in range(5)]
        _install(_Doc("Hub", data_file=_DFileParents(parents)))
        out = dg._slice_used_in(used_in_max=2)
        assert out["truncated"] is True
        assert len(out["references"]) == 2
        assert out["parent_count"] == 5          # the true total stays honest
        assert out["query_complete"] is False    # a capped walk is partial knowledge

    def test_unreadable_parent_does_not_read_as_none(self, _install):
        # a parent slot that can't be resolved must be flagged, not silently dropped - so a
        # short/empty list is never mistaken for 'nothing uses this'.
        good = _Parent(name="Asm")
        _install(_Doc("Part", data_file=_DFileParents([good, None])))
        out = dg._slice_used_in()
        assert out["unreadable_count"] == 1
        assert out["query_complete"] is False

    def test_unsaved_document_has_no_where_used(self, _install):
        _install(_Doc("Untitled", saved=False, data_file=None))
        out = dg._slice_used_in()
        assert out["available"] is False
        assert "never saved" in out["note"].lower()

    def test_query_failure_is_unknown_not_empty(self, _install):
        # parentReferences unreadable -> the relationship is UNKNOWN; must not imply nothing uses this.
        blind = FakeDataFile("Part", file_id="urn:lineage", parent_refs_raise="property")
        _install(_Doc("Part", data_file=blind))
        out = dg._slice_used_in()
        assert out["available"] is True
        assert out["query_complete"] is False
        assert "unknown" in out["note"].lower()


# ── router composition ────────────────────────────────────────────────────────

class TestSliceRouter:
    def test_default_projection_omits_cloud_slices_but_advertises_them(self, _install):
        _install(_Doc("A", data_file=_DataFile()))
        out = _payload(dg.handler())
        assert "versions" not in out and "xref_tree" not in out and "used_in" not in out
        assert "include=['versions']" in out["note"]
        assert "include=['xref_tree']" in out["note"]
        assert "include=['used_in']" in out["note"]

    def test_include_adds_only_the_requested_slice(self, monkeypatch, _install):
        _install(_Doc("A", data_file=_DataFile()))
        monkeypatch.setattr(dg, "_slice_versions", lambda versions_max=25: {"marker": "V"})
        monkeypatch.setattr(dg, "_slice_xref_tree", lambda xref_max=50, max_depth=None: {"marker": "X"})
        monkeypatch.setattr(dg, "_slice_used_in", lambda used_in_max=50: {"marker": "U"})
        out = _payload(dg.handler(include=["versions"]))
        assert out["versions"] == {"marker": "V"}
        assert "xref_tree" not in out and "used_in" not in out

    def test_a_slice_only_read_omits_the_session_projection(self, monkeypatch, _install):
        # the cloud slice is what was asked for; re-sending the active record and the open-document
        # list beside it pays for the session read on every version/xref call.
        _install(_Doc("A", data_file=_DataFile()))
        monkeypatch.setattr(dg, "_slice_versions", lambda versions_max=25: {"marker": "V"})
        assert _payload(dg.handler(include=["versions"])) == {"versions": {"marker": "V"}}

    def test_default_beside_a_slice_keeps_the_projection(self, monkeypatch, _install):
        _install(_Doc("A", data_file=_DataFile()))
        monkeypatch.setattr(dg, "_slice_versions", lambda versions_max=25: {"marker": "V"})
        out = _payload(dg.handler(include=["default", "versions"]))
        assert out["active"]["name"] == "A" and out["versions"] == {"marker": "V"}
        assert out["document_id"] == "urn:lineage:abc" and "note" in out

    def test_a_slice_only_read_does_not_walk_the_open_documents(self, monkeypatch, _install):
        # the walk is the projection's cost (one DataFile read per open document), so a slice-only
        # read must skip it, not build the rows and drop them.
        _install(_Doc("A", data_file=_DataFile()))
        calls = []
        monkeypatch.setattr(dg, "_open_documents",
                            lambda max_results: (calls.append(1)
                                                 or ([], {"open_count": 0, "exceptions": []}, False)))
        monkeypatch.setattr(dg, "_slice_versions", lambda versions_max=25: {"marker": "V"})
        dg.handler(include=["versions"])
        assert calls == []

    def test_the_active_record_is_read_once_for_the_guard_and_the_payload(self, monkeypatch, _install):
        # the guard and the published record must be the SAME read: the record carries a cloud
        # DataFile fetch, so a second one can answer differently from the one that was checked.
        _install(_Doc("A", data_file=_DataFile()))
        calls = []
        real = dg._active_document_facts
        monkeypatch.setattr(dg, "_active_document_facts", lambda: (calls.append(1) or real()))
        out = _payload(dg.handler())
        assert len(calls) == 1 and out["active"]["name"] == "A"

    def test_the_no_active_document_guard_still_fires_on_a_slice_read(self, _install):
        # the slices each answer for the ACTIVE document, so with none open the read is refused by
        # name rather than answered with a slice's own "never saved to the cloud".
        _install(None)
        assert "no active document" in error_message(dg.handler(include=["versions"])).lower()

    def test_include_used_in_adds_where_used_slice(self, monkeypatch, _install):
        _install(_Doc("A", data_file=_DataFile()))
        monkeypatch.setattr(dg, "_slice_used_in", lambda used_in_max=50: {"marker": "U"})
        out = _payload(dg.handler(include=["used_in"]))
        assert out["used_in"] == {"marker": "U"}
        assert "versions" not in out and "xref_tree" not in out


@pytest.fixture
def xref_read(_install, monkeypatch):
    def read(root, **kwargs):
        _install(_Doc("Assembly", data_file=_DataFile()))
        _use_design(monkeypatch, root)
        return _payload(dg.handler(include=["xref_tree"], **kwargs))["xref_tree"]
    return read


class TestXrefTreeCoverageStatus:
    def test_unknown_reference_flag_keeps_readable_sibling_but_blocks_verdict(
            self, xref_read, monkeypatch):
        good = _Occ("Good:1", is_ref=True, dref=_DRef("Good", 1, 1, False))
        unknown = _Occ("Unknown:1", is_ref=False)
        monkeypatch.setattr(unknown, "isReferencedComponent", "unknown")
        out = xref_read(_Root([good, unknown]))
        assert [r["path"] for r in out["references"]] == ["Good:1"]
        assert out["walk_complete"] is False and out["all_current"] is False
        assert out["unreadable_count"] == 0

    def test_empty_leaf_at_depth_cap_is_complete(self, xref_read):
        leaf = _Occ("Leaf:1", is_ref=True, dref=_DRef("Leaf", 1, 1, False))
        out = xref_read(_Root([leaf]), max_depth=1)
        assert out["depth_capped"] is False
        assert out["walk_complete"] is True and out["all_current"] is True

    def test_unreadable_root_collection_keeps_derive_row_but_blocks_verdict(
            self, xref_read, monkeypatch):
        root = _Root([], derive_feats=[_DeriveFeat("D1", _DRef("Src", 1, 1, False))])
        monkeypatch.setattr(root, "occurrences", _NamedCollection(raises="occurrences unreadable"))
        out = xref_read(root)
        assert [r["kind"] for r in out["references"]] == ["derive"]
        assert out["walk_complete"] is False and out["all_current"] is False

    @pytest.mark.parametrize("failure", ["none", "count", "item"])
    def test_partial_component_enumeration_retains_root_without_certifying_coverage(
            self, xref_read, monkeypatch, failure):
        root = _Root([], derive_feats=[_DeriveFeat("D1", _DRef("Src", 1, 1, False))])
        coll = (None if failure == "none" else _NamedCollection(
            [None], raises="count unreadable" if failure == "count" else None))
        monkeypatch.setattr(MakeDesign, "allComponents", property(lambda self: coll))
        out = xref_read(root)
        assert [r["path"] for r in out["references"]] == ["Root:D1"]
        assert out["walk_complete"] is False and out["all_current"] is False
        assert out["unreadable_count"] == 0
        assert out["truncated"] is False and out["depth_capped"] is False

    def test_derive_cap_stops_before_reading_another_feature(self, xref_read, monkeypatch):
        root = _Root([], derive_feats=[_DeriveFeat("D1", _DRef("Src", 1, 1, False)),
                                       _DeriveFeat("D2", _DRef("Tail", 1, 1, False))])
        coll = root.features.deriveFeatures
        original, reads = coll.item, []

        def item(i):
            reads.append(i)
            return original(i)

        monkeypatch.setattr(coll, "item", item)
        out = xref_read(root, xref_max=1)
        assert [r["path"] for r in out["references"]] == ["Root:D1"]
        assert reads == [0]
        assert out["truncated"] is True and out["walk_complete"] is False

    def test_depth_limit_does_not_visit_component_local_unresolved_children(self, xref_read):
        child = _SpyOcc("Deep:1")
        out = xref_read(_Root([_Occ("Sub:1", comp_local=[child])]), max_depth=1)
        assert child.reads == 0 and out["references"] == []
        assert out["depth_capped"] is True and out["walk_complete"] is False
