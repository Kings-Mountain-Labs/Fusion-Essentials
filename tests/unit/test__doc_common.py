"""Unit tests for ``_doc_common.py`` - the open-document resolve doc_activate/doc_close share.

Pinned: an 'open:N' index, a lineage URN or web URL matched by LINEAGE EQUALITY, else an
exact display name - with more than one distinct match REFUSED and every candidate row
carrying the address that reaches it.
"""

import json
import re

import pytest

from conftest import FakeApplication, FakeDataFile, FakeDocuments, FakeFusionDocument, load_tool

dm = load_tool("_doc_common")
da = load_tool("doc_activate")   # the two tools driving this resolve
dcl = load_tool("doc_close")
wg = load_tool("_write_guard")   # the shared one-open-document predicate


def _payload(result):
    assert result["isError"] is False, result
    return json.loads(result["content"][0]["text"])


# ── fakes for the app.documents tree ────────────────────────────────────────

def _doc(name="Untitled", urn=None, close_ok=True, name_raises=False):
    """One open document: its display name and the lineage URN its dataFile answers, which
    identifies it when names collide (absent = a never-saved doc). `name_raises` is the stale proxy
    whose display name will not read - still an OPEN document holding its place in app.documents."""
    return FakeFusionDocument(name=name, close_ok=close_ok, closed=name_raises,
                              data_file=FakeDataFile(name, file_id=urn) if urn else None)


class _BlindIdDoc(FakeFusionDocument):
    """A document whose dataFile read itself throws, one step before the lineage id - so it can
    never be shown to be the same document as a neighbour that answered one."""

    @property
    def dataFile(self):
        raise RuntimeError("4 : An API Object refers to a deleted Object")

    @dataFile.setter
    def dataFile(self, value):
        pass                     # nothing this document is built with makes its file readable


class _Documents(FakeDocuments):
    """app.documents with `item_raises_at`: the stale collection slot whose item(i) itself raises -
    the collection still COUNTS it, so the open:N address space keeps its width."""
    def __init__(self, documents=(), item_raises_at=None):
        super().__init__(documents)
        self._raises_at = item_raises_at

    def item(self, i):
        if i == self._raises_at:
            raise RuntimeError("4 : An API Object refers to a deleted Object")
        return super().item(i)


@pytest.fixture
def open_session(monkeypatch):
    """Point every module reading app.documents at ONE open session: the resolver here, and the two
    tools that drive it (each holds its own module-level `app`)."""
    def _wire(documents, active="first", item_raises_at=None):
        docs = list(documents)
        app = FakeApplication(
            active_document=(docs[0] if docs else None) if active == "first" else active,
            documents=_Documents(docs, item_raises_at=item_raises_at))
        for mod in (dm, da, dcl):
            monkeypatch.setattr(mod, "app", app)
        return app
    return _wire


class TestFindOpenDocument:
    def test_exact_match_case_insensitive(self, open_session):
        a = _doc("PartA")
        b = _doc("PartA_CAM")
        open_session([a, b])
        # an exact name resolves to that document, not a same-prefixed sibling
        found, names, ambiguous = dm._find_open_document("parta")   # case-insensitive
        assert found is a
        assert ambiguous is False
        assert "PartA_CAM" in names

    def test_partial_name_is_refused(self, open_session):
        # a partial name must NOT resolve to a substring sibling - documents can share names, so
        # the first partial hit could be the wrong document. Refused: returns None + the listing,
        # whose row names the open document and the address that reaches it.
        a = _doc("PartA_CAM")
        open_session([a])
        found, names, ambiguous = dm._find_open_document("CAM")
        assert found is None
        assert ambiguous is False           # a partial miss is NOT a name-twin ambiguity
        assert names == ["PartA_CAM (no lineage URN - open:0)"]

    def test_shared_name_is_ambiguous_not_first_match(self, open_session):
        # TWO open docs share the display name 'P1-Gimbal' (Fusion allows this). Resolving by that
        # name must REFUSE (ambiguous), never grab the first of two name-twins.
        a = _doc("P1-Gimbal", urn="urn:adsk.wipprod:dm.lineage:AAA")
        b = _doc("P1-Gimbal", urn="urn:adsk.wipprod:dm.lineage:BBB")
        open_session([a, b])
        found, names, ambiguous = dm._find_open_document("P1-Gimbal")
        assert found is None
        assert ambiguous is True

    def test_urn_disambiguates_a_name_twin(self, open_session):
        # the SAME two same-named docs: the lineage URN resolves to exactly the right one.
        a = _doc("P1-Gimbal", urn="urn:adsk.wipprod:dm.lineage:AAA")
        b = _doc("P1-Gimbal", urn="urn:adsk.wipprod:dm.lineage:BBB")
        open_session([a, b])
        found, _names, ambiguous = dm._find_open_document("urn:adsk.wipprod:dm.lineage:BBB")
        assert found is b
        assert ambiguous is False

    def test_web_url_resolves_via_embedded_urn(self, open_session):
        # a Fusion web URL carries the lineage URN as a base64url path segment - it must resolve too.
        import base64
        urn = "urn:adsk.wipprod:dm.lineage:BBB"
        seg = base64.b64encode(urn.encode()).decode().rstrip("=").replace("+", "-").replace("/", "_")
        url = f"https://x.autodesk360.com/g/projects/123/data/FOLDERSEG_LONG_ENOUGH/{seg}?show=overview"
        a = _doc("P1-Gimbal", urn="urn:adsk.wipprod:dm.lineage:AAA")
        b = _doc("P1-Gimbal", urn=urn)
        open_session([a, b])
        found, _names, ambiguous = dm._find_open_document(url)
        assert found is b
        assert ambiguous is False

    def test_urn_with_no_matching_open_doc_is_a_clean_miss(self, open_session):
        a = _doc("P1-Gimbal", urn="urn:adsk.wipprod:dm.lineage:AAA")
        open_session([a])
        found, _names, ambiguous = dm._find_open_document("urn:adsk.wipprod:dm.lineage:ZZZ")
        assert found is None
        assert ambiguous is False           # a URN that matches nothing is a miss, not an ambiguity


# ─────────────────────────────────────────────────────────────────────────────
# unsaved-doc addressability (open:N) + close_all skipping dead reference proxies
# ─────────────────────────────────────────────────────────────────────────────

class TestOpenIndexAddressing:
    def test_open_index_addresses_an_unsaved_twin(self, open_session):
        u1, u2 = _doc("Untitled"), _doc("Untitled")
        open_session([u1, u2])
        d, names, ambiguous = dm._find_open_document("open:1")
        assert d is u2 and ambiguous is False

    def test_open_index_out_of_range_is_clean_miss(self, open_session):
        open_session([_doc("Untitled")])
        d, names, ambiguous = dm._find_open_document("open:5")
        assert d is None and ambiguous is False

    def test_open_index_non_integer_is_clean_miss(self, open_session):
        open_session([_doc("Untitled")])
        d, names, ambiguous = dm._find_open_document("open:abc")
        assert d is None and ambiguous is False

    def test_a_doc_with_an_unreadable_name_holds_its_open_index(self, open_session):
        # open:N indexes app.documents, and doc_get publishes the same number. A document whose NAME
        # will not read is still open at its own index - dropping it would slide every later document
        # down one, so open:2 would activate/close the document the caller did not ask for.
        first, broken = _doc("Untitled"), _doc("Ghost", name_raises=True)
        third = _doc("Untitled")
        open_session([first, broken, third])
        d, names, ambiguous = dm._find_open_document("open:2")
        assert d is third and ambiguous is False
        assert names == ["Untitled", "", "Untitled"]   # the unreadable name holds its slot
        assert dm._find_open_document("open:1")[0] is broken

    def test_shared_name_without_index_is_still_refused(self, open_session):
        # two unsaved 'Untitled' (no URN) -> a bare name is ambiguous; open:N is the only handle.
        open_session([_doc("Untitled"), _doc("Untitled")])
        d, names, ambiguous = dm._find_open_document("Untitled")
        assert d is None and ambiguous is True

    def test_a_doc_whose_item_read_raises_burns_its_slot(self, open_session):
        # The stale-proxy shape one step earlier: documents.item(i) itself raises (a deleted
        # object), before .name is ever reachable. The slot is burned - open:2 still reaches the
        # third document, and open:1 is a clean miss, not a propagated exception.
        first, third = _doc("Untitled"), _doc("Untitled")
        open_session([first, _doc("dead"), third], item_raises_at=1)
        d, names, ambiguous = dm._find_open_document("open:2")
        assert d is third and ambiguous is False
        assert names == ["Untitled", "", "Untitled"]
        missed, listing, miss_ambiguous = dm._find_open_document("open:1")
        assert missed is None and miss_ambiguous is False   # a clean miss, not a propagated raise
        # ...and the listing that refusal carries never offers open:1 back: that is the address
        # this very call refused.
        assert "open:1" not in "; ".join(listing)


class TestWhichCandidatesAUrnReaches:
    """Which candidate a document id ADDRESSES decides how its row is written: a lineage exactly one
    candidate answers to is that candidate's handle, and every other candidate needs its open index,
    because a refusal that offered a URN reaching two documents would ask for a value that returns
    that same refusal."""

    def test_two_distinct_lineages_each_reach_one_candidate(self):
        assert dm._unique_lineages(
            ["urn:adsk.wipprod:dm.lineage:AB", "urn:adsk.wipprod:dm.lineage:CD"]) == [True, True]

    def test_a_lone_candidate_is_reached_by_its_own_lineage(self):
        assert dm._unique_lineages(["urn:adsk.wipprod:dm.lineage:AB"]) == [True]

    def test_two_versions_of_one_lineage_reach_neither(self):
        # THE boundary: ONE candidate under a lineage is reached by it, TWO are reached by neither -
        # a '?version=N' suffix is dropped before matching, so both ids answer to lineage AB.
        assert dm._unique_lineages(
            ["urn:adsk.wipprod:dm.lineage:AB",
             "urn:adsk.wipprod:dm.lineage:AB?version=2"]) == [False, False]

    def test_an_id_that_did_not_read_reaches_nothing(self):
        # an unreadable id is no address at all, and a second one beside it is a different document
        # rather than the same one - neither is singled out by 'no id'.
        assert dm._unique_lineages([None]) == [False]
        assert dm._unique_lineages([None, None]) == [False, False]

    def test_a_shared_lineage_costs_only_the_candidates_that_share_it(self):
        # the mixed session: the pair under one lineage needs indexes, the outsider keeps its URN.
        assert dm._unique_lineages(
            ["urn:adsk.wipprod:dm.lineage:AB", "urn:adsk.wipprod:dm.lineage:AB?version=2",
             "urn:adsk.wipprod:dm.lineage:CD"]) == [False, False, True]


class TestUrnIdentityIsExact:
    """The URN branch resolves by LINEAGE EQUALITY and refuses a second hit. It serves doc_activate
    and the DESTRUCTIVE doc_close, so a prefix match (one lineage id can be another's prefix) or a
    first-of-several pick closes a document the caller never named."""

    def test_an_exact_lineage_urn_resolves(self, open_session):
        a = _doc("P1", urn="urn:adsk.wipprod:dm.lineage:AB")
        b = _doc("P2", urn="urn:adsk.wipprod:dm.lineage:CD")
        open_session([a, b])
        d, _names, ambiguous = dm._find_open_document(
            "urn:adsk.wipprod:dm.lineage:CD")
        assert d is b and ambiguous is False

    def test_a_urn_the_open_id_merely_STARTS_WITH_is_a_miss(self, open_session):
        # THE boundary: candidate 'urn:...:AB' is a PREFIX of the open doc's 'urn:...:ABC'. Equal is
        # a hit; shorter-by-one is a different file and must not resolve.
        doc = _doc("P1", urn="urn:adsk.wipprod:dm.lineage:ABC")
        open_session([doc])
        assert dm._find_open_document(
            "urn:adsk.wipprod:dm.lineage:ABC")[0] is doc
        d, _names, ambiguous = dm._find_open_document(
            "urn:adsk.wipprod:dm.lineage:AB")
        assert d is None and ambiguous is False

    def test_a_version_suffixed_urn_still_addresses_its_lineage(self, open_session):
        # the '?version=N' suffix names a VERSION of the same lineage - dropped on both sides, then
        # compared for equality (the reading the prefix test was standing in for).
        doc = _doc("P1", urn="urn:adsk.wipprod:dm.lineage:AB")
        open_session([doc])
        d, _names, ambiguous = dm._find_open_document(
            "urn:adsk.wipprod:dm.lineage:AB?version=3")
        assert d is doc and ambiguous is False

    def test_a_lineage_open_as_tab_and_dependency_is_ONE_document(self, open_session):
        # MEASURED (and held by wg.one_open_document): inserting a saved part into a
        # second document loads it as a real Document, so app.documents lists the visible tab AND
        # the dependency instance with the same name and byte-identical dataFile.id. Refusing that
        # tells the caller to pass the URN they just passed - and both handles are the same
        # document, so the first resolves.
        tab = _doc("P1", urn="urn:adsk.wipprod:dm.lineage:AB")
        dependency = _doc("P1", urn="urn:adsk.wipprod:dm.lineage:AB")
        open_session([tab, dependency])
        d, _names, ambiguous = dm._find_open_document(
            "urn:adsk.wipprod:dm.lineage:AB")
        assert d is tab and ambiguous is False

    def test_close_by_urn_works_while_a_dependency_instance_is_loaded(self, open_session):
        # the functional consequence: an ordinary assembly must not break close-by-URN.
        tab = _doc("P1", urn="urn:adsk.wipprod:dm.lineage:AB")
        dependency = _doc("P1", urn="urn:adsk.wipprod:dm.lineage:AB")
        open_session([tab, dependency])
        out = _payload(dcl.handler(
            name="urn:adsk.wipprod:dm.lineage:AB"))
        assert out["closed"] == ["P1"]
        assert tab._closes == [False]

    def test_two_versions_of_one_lineage_are_refused_naming_both_ids(self, open_session):
        # DISTINCT ids under one lineage key - the same file open at two versions. No URN settles
        # it (both answer to that lineage), so it refuses and names the ids that differ.
        v_latest = _doc("P1", urn="urn:adsk.wipprod:dm.lineage:AB")
        v_pinned = _doc("P1", urn="urn:adsk.wipprod:dm.lineage:AB?version=2")
        open_session([v_latest, v_pinned])
        d, names, ambiguous = dm._find_open_document(
            "urn:adsk.wipprod:dm.lineage:AB")
        assert d is None and ambiguous is True             # never `v_latest`
        # each row states the id that candidate answered - suffix and all - and the open index,
        # since neither id reaches one document: both answer to lineage AB.
        assert names == ["P1 (urn:adsk.wipprod:dm.lineage:AB - open:0)",
                         "P1 (urn:adsk.wipprod:dm.lineage:AB?version=2 - open:1)"]

    def test_close_refuses_two_versions_and_points_at_open_n(self, open_session):
        a = _doc("P1", urn="urn:adsk.wipprod:dm.lineage:AB")
        b = _doc("P1", urn="urn:adsk.wipprod:dm.lineage:AB?version=2")
        open_session([a, b])
        res = dcl.handler(name="urn:adsk.wipprod:dm.lineage:AB")
        assert res["isError"] is True
        assert "?version=2" in res["message"]              # both ids named
        assert "open:N" in res["message"]                  # the handle that CAN settle it
        assert a._closes == [] and b._closes == []

    def test_the_version_twin_refusal_never_asks_for_a_urn_retry(self, open_session):
        # THE wording boundary: every candidate here answers to lineage AB, so a retry with either
        # listed id returns this same refusal. The message must hand back the open index each
        # candidate carries and must NOT carry the URN-retry imperative the name-twin path gives.
        a = _doc("P1", urn="urn:adsk.wipprod:dm.lineage:AB")
        b = _doc("P1", urn="urn:adsk.wipprod:dm.lineage:AB?version=2")
        open_session([a, b])
        msg = dcl.handler(
            name="urn:adsk.wipprod:dm.lineage:AB")["message"]
        assert "Retry with one of those URNs" not in msg
        assert "open:0" in msg and "open:1" in msg          # the handle each candidate carries
        assert a._closes == [] and b._closes == []

    def test_a_name_twin_is_addressed_by_the_urn_it_carries_and_no_index(self, open_session):
        # the other side of that boundary: two DISTINCT lineages, so each id reaches one document
        # and the rows offer the URN alone - an open index beside it would say no URN settles this.
        open_session([_doc("P1", urn="urn:adsk.wipprod:dm.lineage:AB"),
                                 _doc("P1", urn="urn:adsk.wipprod:dm.lineage:CD")])
        msg = da.handler(name="P1")["message"]
        assert "P1 (urn:adsk.wipprod:dm.lineage:AB)" in msg
        assert "P1 (urn:adsk.wipprod:dm.lineage:CD)" in msg
        assert "open:0" not in msg and "open:1" not in msg

    def test_an_unreadable_id_beside_a_readable_one_is_not_one_document(self, open_session):
        # a hit whose id did not read cannot be shown to be the same document as its neighbour.
        readable = _doc("P1", urn="urn:adsk.wipprod:dm.lineage:AB")
        open_session([readable, _BlindIdDoc(name="P1")])
        # the blind doc never matches the lineage, so this stays a single hit and resolves
        d, _names, ambiguous = dm._find_open_document(
            "urn:adsk.wipprod:dm.lineage:AB")
        assert d is readable and ambiguous is False
        # ...and the predicate itself refuses to call a None id 'the same document'
        assert wg.one_open_document(["urn:x", None]) is False
        assert wg.one_open_document(["urn:x", "urn:x"]) is True

    def test_a_doc_with_an_unreadable_urn_never_matches(self, open_session):
        # dataFile.id reading None must not collapse into the '' an unreadable read gives and match
        # a candidate that carries no lineage either.
        open_session([_doc("Untitled", urn=None)])
        d, _names, ambiguous = dm._find_open_document(
            "urn:adsk.wipprod:dm.lineage:AB")
        assert d is None and ambiguous is False

    def test_close_by_an_exact_urn_still_closes_that_document(self, open_session):
        a = _doc("P1", urn="urn:adsk.wipprod:dm.lineage:AB")
        b = _doc("P2", urn="urn:adsk.wipprod:dm.lineage:ABC")
        open_session([a, b])
        out = _payload(dcl.handler(
            name="urn:adsk.wipprod:dm.lineage:ABC"))
        assert out["closed"] == ["P2"]
        assert a._closes == []


class TestAmbiguityNamesEveryLineageUrn:
    """A refusal that lists only the shared display NAME asks the caller to retry with the value that
    just failed. Every candidate is listed with the lineage URN that tells it apart, so the retry is
    exact - and a repeat that is ONE document listed twice is not an ambiguity to refuse at all."""

    def test_activate_refusal_names_each_candidates_urn(self, open_session):
        open_session([_doc("P1-Gimbal", urn="urn:adsk.wipprod:dm.lineage:AAA"),
                                 _doc("P1-Gimbal", urn="urn:adsk.wipprod:dm.lineage:BBB")])
        res = da.handler(name="P1-Gimbal")
        assert res["isError"] is True
        assert "P1-Gimbal (urn:adsk.wipprod:dm.lineage:AAA)" in res["message"]
        assert "P1-Gimbal (urn:adsk.wipprod:dm.lineage:BBB)" in res["message"]
        assert "which to activate" in res["message"]        # the acting word is this tool's

    def test_close_refusal_names_each_candidates_urn_and_closes_nothing(self, open_session):
        a = _doc("P1-Gimbal", urn="urn:adsk.wipprod:dm.lineage:AAA")
        b = _doc("P1-Gimbal", urn="urn:adsk.wipprod:dm.lineage:BBB")
        open_session([a, b])
        res = dcl.handler(name="P1-Gimbal")
        assert res["isError"] is True
        assert "P1-Gimbal (urn:adsk.wipprod:dm.lineage:AAA)" in res["message"]
        assert "P1-Gimbal (urn:adsk.wipprod:dm.lineage:BBB)" in res["message"]
        assert "which to close" in res["message"]           # the acting word is this tool's
        assert a._closes == [] and b._closes == []

    def test_a_candidate_that_answered_no_urn_says_so_beside_one_that_did(self, open_session):
        # A row is published per candidate whether or not its id read: dropping the URN-less one
        # would show two documents under one address, and dropping its row would show one document.
        # Each is addressed by what reaches it - the URN one, the open index the other.
        open_session([_doc("P1", urn="urn:adsk.wipprod:dm.lineage:AAA"),
                                 _doc("P1")])
        msg = da.handler(name="P1")["message"]
        assert "P1 (urn:adsk.wipprod:dm.lineage:AAA)" in msg
        assert "P1 (no lineage URN - open:1)" in msg

    def test_two_unsaved_twins_are_listed_at_the_distinct_indexes_that_address_them(
            self, open_session):
        # Two unsaved 'Untitled' answer no URN, so a refusal advising 'open:N' has to SAY which N
        # each one is: rows reading identically leave the caller with nothing to retry with.
        open_session([_doc("Untitled"), _doc("Untitled")])
        msg = da.handler(name="Untitled")["message"]
        assert "Untitled (no lineage URN - open:0)" in msg
        assert "Untitled (no lineage URN - open:1)" in msg

    def test_two_versions_sharing_a_name_are_addressed_by_index_on_the_NAME_path_too(
            self, open_session):
        # The same file open at two versions, reached by its display NAME: the ids differ, so this
        # refuses - and telling the caller to retry with either id would return this refusal again,
        # since both answer to lineage AB. Each row carries the index that does address one.
        open_session([_doc("P1", urn="urn:adsk.wipprod:dm.lineage:AB"),
                                 _doc("P1", urn="urn:adsk.wipprod:dm.lineage:AB?version=2")])
        msg = da.handler(name="P1")["message"]
        assert "P1 (urn:adsk.wipprod:dm.lineage:AB - open:0)" in msg
        assert "P1 (urn:adsk.wipprod:dm.lineage:AB?version=2 - open:1)" in msg

    def test_a_tab_and_its_dependency_instance_resolve_by_NAME_too(self, open_session):
        # MEASURED, and held by wg.one_open_document: an assembly loads its references as
        # real Documents, so the visible tab and the dependency instance repeat the name AND the
        # lineage URN. Refusing that would refuse an ordinary assembly by its own display name -
        # both handles address one document, so the first resolves.
        tab = _doc("P1", urn="urn:adsk.wipprod:dm.lineage:AB")
        dependency = _doc("P1", urn="urn:adsk.wipprod:dm.lineage:AB")
        open_session([tab, dependency])
        out = _payload(dcl.handler(name="P1"))
        assert out["closed"] == ["P1"]
        assert tab._closes == [False] and dependency._closes == []

    def test_two_DISTINCT_documents_sharing_a_name_still_refuse(self, open_session):
        # The boundary the collapse must not cross: one name, two LINEAGES. Equal ids collapse;
        # anything else is a genuine ambiguity and a close here would destroy the wrong document.
        a = _doc("P1", urn="urn:adsk.wipprod:dm.lineage:AB")
        b = _doc("P1", urn="urn:adsk.wipprod:dm.lineage:CD")
        open_session([a, b])
        res = dcl.handler(name="P1")
        assert res["isError"] is True
        assert a._closes == [] and b._closes == []

    def test_a_name_matching_one_document_still_resolves(self, open_session):
        # the other side of the collapse: a unique name is not touched by any of it.
        a = _doc("P1", urn="urn:adsk.wipprod:dm.lineage:AB")
        b = _doc("P2", urn="urn:adsk.wipprod:dm.lineage:CD")
        open_session([a, b])
        out = _payload(dcl.handler(name="P2"))
        assert out["closed"] == ["P2"] and a._closes == []


class TestAUrnMissNamesWhatItTried:
    """A URN that matches no open document is a clean miss - and the caller addressed the call by
    URN, so the miss answers in URNs: what was searched for, and what each open document answers."""

    def test_the_miss_lists_every_open_document_by_urn(self, open_session):
        open_session([_doc("P1-Gimbal", urn="urn:adsk.wipprod:dm.lineage:AAA"),
                                 _doc("Untitled")])
        res = da.handler(
            name="urn:adsk.wipprod:dm.lineage:ZZZ")
        assert res["isError"] is True
        msg = res["message"]
        assert "urn:adsk.wipprod:dm.lineage:ZZZ" in msg                  # what it tried
        assert "P1-Gimbal (urn:adsk.wipprod:dm.lineage:AAA)" in msg      # what is open, by URN
        assert "Untitled (no lineage URN - open:1)" in msg               # ...and by index where none

    def test_a_lineage_open_at_two_versions_is_listed_at_the_indexes_that_reach_it(self, open_session):
        # The miss listing is the set the caller retries against, so every row states an address
        # that reaches ONE document. Two versions of lineage AB both answer to that lineage, so
        # offering either id alone hands back a value that resolves to the pair - the rows carry the
        # open index instead. The third document is the boundary: its own id singles it out, so it
        # keeps the URN alone and an index beside it would say no URN reaches it.
        open_session([_doc("P1", urn="urn:adsk.wipprod:dm.lineage:AB"),
                                 _doc("P1", urn="urn:adsk.wipprod:dm.lineage:AB?version=2"),
                                 _doc("P2", urn="urn:adsk.wipprod:dm.lineage:CD")])
        msg = da.handler(
            name="urn:adsk.wipprod:dm.lineage:ZZZ")["message"]
        assert "P1 (urn:adsk.wipprod:dm.lineage:AB - open:0)" in msg
        assert "P1 (urn:adsk.wipprod:dm.lineage:AB?version=2 - open:1)" in msg
        assert "P2 (urn:adsk.wipprod:dm.lineage:CD)" in msg
        assert "open:2" not in msg

    def test_a_version_suffixed_miss_names_the_lineage_it_compared(self, open_session):
        # The value COMPARED is the lineage key, not the string typed - the '?version=N' suffix is
        # dropped first. A miss echoing only the input leaves the caller unable to tell which of the
        # two missed.
        open_session([_doc("P1", urn="urn:adsk.wipprod:dm.lineage:AAA")])
        msg = da.handler(
            name="urn:adsk.wipprod:dm.lineage:ZZZ?version=4")["message"]
        assert "?version=4" in msg                                       # the value typed
        assert "(lineage urn:adsk.wipprod:dm.lineage:ZZZ)" in msg         # the value compared

    def test_a_bare_urn_miss_adds_no_lineage_clause(self, open_session):
        # the boundary: when the typed value IS the lineage key, the echo already states it and a
        # second copy of the same string is noise.
        open_session([_doc("P1", urn="urn:adsk.wipprod:dm.lineage:AAA")])
        msg = da.handler(
            name="urn:adsk.wipprod:dm.lineage:ZZZ")["message"]
        assert "(lineage " not in msg

    def test_a_display_name_miss_invents_no_lineage_clause(self, open_session):
        open_session([_doc("P1", urn="urn:adsk.wipprod:dm.lineage:AAA")])
        msg = da.handler(name="Ghost")["message"]
        assert "(lineage " not in msg.split("Open:")[0]

    def test_a_web_url_miss_names_the_urn_decoded_out_of_it(self, open_session):
        # a URL carries the lineage base64url-encoded, so the string typed shares no characters with
        # the value compared - this is the miss that most needs to say what it searched for.
        import base64
        urn = "urn:adsk.wipprod:dm.lineage:ZZZ"
        seg = base64.b64encode(urn.encode()).decode().rstrip("=").replace("+", "-").replace("/", "_")
        url = (f"https://x.autodesk360.com/g/projects/123/data/FOLDERSEG_LONG_ENOUGH/{seg}"
               "?show=overview")
        open_session([_doc("P1", urn="urn:adsk.wipprod:dm.lineage:AAA")])
        msg = da.handler(name=url)["message"]
        assert f"(lineage {urn})" in msg


class TestTheMissListingCarriesOnlyRealRows:
    """The miss refusal's 'Open:' listing is the set the caller retries against, so it states an
    empty session as such and never publishes a separator standing in for a row."""

    def test_a_session_with_nothing_open_says_none_rather_than_trailing_off(self, open_session):
        # An empty listing renders as 'Open: .' - a sentence stating no fact, where the fact is
        # that there is nothing open to retry against at all.
        open_session([])
        msg = da.handler(name="Ghost")["message"]
        assert "Open: (none)." in msg

    def test_a_document_whose_name_will_not_read_is_listed_at_its_open_index(self, open_session):
        # A document whose name raises still holds its open index, and open:N is exactly what
        # addresses it - so it is listed as an unnamed row carrying that index, never as a bare
        # separator with no document on either side of it.
        open_session([_doc("P1"), _doc("Ghost", name_raises=True)])
        msg = da.handler(name="Nope")["message"]
        assert "Open: P1 (no lineage URN - open:0); (unnamed) (no lineage URN - open:1)." in msg
        assert "; ;" not in msg and "Open: ;" not in msg
        # the row above offers open:1 because open:1 REACHES that document - the row is an offer,
        # and this is the reading the burned-slot test below is the other side of.
        assert dm._find_open_document("open:1")[0] is not None

    def test_a_slot_that_answered_no_document_is_named_and_carries_no_address(self, open_session):
        # One step earlier than the row above: documents.item(1) itself raises, so the slot holds
        # its place in the address space but answers NO document - and 'open:1', the address its
        # position would name, is refused by the very resolve this listing is the retry set for.
        # So the hole is named and carries no address, and every 'open:N' the listing does print
        # reaches a document - on the NAME miss and the URN miss alike.
        first, third = _doc("Untitled"), _doc("Untitled")
        open_session([first, _doc("dead"), third], item_raises_at=1)
        for asked in ("Nope", "urn:adsk.wipprod:dm.lineage:ZZZ"):
            msg = da.handler(name=asked)["message"]
            assert "(unreadable slot) (no handle - the document did not read)" in msg
            offered = re.findall(r"open:(\d+)", msg)
            assert offered == ["0", "2"]              # the two slots that DO answer a document
            for n in offered:
                assert dm._find_open_document("open:" + n)[0] is not None


class TestANameMissListsTheAddressThatReachesEachDocument:
    """A display-name miss ends on 'a shared name needs a lineage URN or the open:N index' - so the
    listing beside it has to STATE them. Bare display names render two documents sharing a name as
    one name printed twice, which names neither address the sentence asks for and leaves the caller
    a second call away from any retry."""

    def test_name_twins_are_listed_at_the_urns_that_tell_them_apart(self, open_session):
        # THE case: two documents answer to 'P1-Gimbal', so a listing of display names prints that
        # name twice and the caller cannot build the URN retry the sentence asks for.
        open_session([_doc("P1-Gimbal", urn="urn:adsk.wipprod:dm.lineage:AAA"),
                                 _doc("P1-Gimbal", urn="urn:adsk.wipprod:dm.lineage:BBB")])
        msg = da.handler(name="Ghost")["message"]
        assert "P1-Gimbal (urn:adsk.wipprod:dm.lineage:AAA)" in msg
        assert "P1-Gimbal (urn:adsk.wipprod:dm.lineage:BBB)" in msg
        assert "open:0" not in msg and "open:1" not in msg   # each id reaches one; no index needed

    def test_unsaved_twins_are_listed_at_the_open_indexes_that_address_them(self, open_session):
        # The other half of the sentence: two unsaved 'Untitled' answer no URN at all, so the only
        # retry left is the index - and rows reading identically state neither one.
        open_session([_doc("Untitled"), _doc("Untitled")])
        msg = dcl.handler(name="Ghost")["message"]
        assert "Untitled (no lineage URN - open:0)" in msg
        assert "Untitled (no lineage URN - open:1)" in msg

    def test_a_lineage_open_at_two_versions_carries_its_index_on_the_name_miss_too(self, open_session):
        # The boundary between the two halves: both ids READ, so a bare id-per-row listing looks
        # complete - but both answer to lineage AB, so neither retry reaches one document.
        open_session([_doc("P1", urn="urn:adsk.wipprod:dm.lineage:AB"),
                                 _doc("P1", urn="urn:adsk.wipprod:dm.lineage:AB?version=2")])
        msg = da.handler(name="Ghost")["message"]
        assert "P1 (urn:adsk.wipprod:dm.lineage:AB - open:0)" in msg
        assert "P1 (urn:adsk.wipprod:dm.lineage:AB?version=2 - open:1)" in msg

    def test_a_single_open_document_is_listed_by_the_urn_that_reaches_it(self, open_session):
        # The quiet case, and the one that must not gain noise: one document, one id that singles
        # it out, no index.
        open_session([_doc("P1", urn="urn:adsk.wipprod:dm.lineage:AAA")])
        msg = da.handler(name="Ghost")["message"]
        assert "Open: P1 (urn:adsk.wipprod:dm.lineage:AAA)." in msg


class TestAnOpenIndexMissListsTheAddressesThatDoReach:
    """An 'open:N' reaching no document refuses like every other miss, so it lists what the name and
    URN misses list: one row per candidate carrying the address that reaches it. Bare display names
    hand two unsaved 'Untitled' back as one name printed twice - which names neither of the indexes
    that address them, and the index is the only handle either of them has."""

    def test_an_index_past_the_end_lists_the_indexes_that_do_address(self, open_session):
        # THE case: two unsaved twins answer no URN, so the listing has to state open:0 and open:1
        # or the caller is left with 'Untitled; Untitled' and no retry.
        open_session([_doc("Untitled"), _doc("Untitled")])
        msg = da.handler(name="open:9")["message"]
        assert "Untitled (no lineage URN - open:0)" in msg
        assert "Untitled (no lineage URN - open:1)" in msg

    def test_the_last_index_resolves_and_the_next_one_refuses(self, open_session):
        # THE boundary of the in-range test, both sides: with two documents open, open:1 is the
        # last index that addresses one and open:2 is one past the end - and a negative index
        # addresses nothing, rather than indexing backwards off the end of the list.
        a, b = _doc("Untitled"), _doc("Untitled")
        open_session([a, b])
        assert dm._find_open_document("open:1")[0] is b
        d, listing, ambiguous = dm._find_open_document("open:2")
        assert d is None and ambiguous is False
        assert listing == ["Untitled (no lineage URN - open:0)",
                           "Untitled (no lineage URN - open:1)"]
        assert dm._find_open_document("open:-1")[0] is None

    def test_an_index_that_is_not_a_number_lists_the_same_rows(self, open_session):
        # the second refusal path: 'open:abc' parses to no index at all, and that caller needs the
        # same set to retry against as the out-of-range one.
        open_session([_doc("Untitled"), _doc("Untitled")])
        msg = dcl.handler(name="open:abc")["message"]
        assert "Untitled (no lineage URN - open:0)" in msg
        assert "Untitled (no lineage URN - open:1)" in msg

    def test_an_index_naming_a_slot_that_answered_no_document_lists_them_too(self, open_session):
        # the third: the index is IN range and the slot behind it answers no document. The rows for
        # the two that do answer carry their indexes; the hole is named and carries no address.
        first, third = _doc("Untitled"), _doc("Untitled")
        open_session([first, _doc("dead"), third], item_raises_at=1)
        msg = da.handler(name="open:1")["message"]
        assert "Untitled (no lineage URN - open:0)" in msg
        assert "Untitled (no lineage URN - open:2)" in msg
        assert "(unreadable slot) (no handle - the document did not read)" in msg
