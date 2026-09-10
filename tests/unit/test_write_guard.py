"""Tests for the write-document binding guard (_write_guard) - the concurrency targeting fix.

The guard wraps every WRITE handler: an optional expect_document REFUSES the write if the active doc
moved (active_document_changed) OR if a bare NAME is shared by several open documents
(ambiguous_document_name, candidates listed - name-equality alone cannot prove the active doc is the
one the agent read; a URN match is always exact). Every successful write result is stamped with
acted_on={name,urn}. READ tools get wrap_read: no guard, but every result is stamped with
active_document={name,urn} - the document the read came from. We patch the guard's
_active_identity and _open_documents seams.
"""

import json
from types import SimpleNamespace

import pytest

from conftest import (
    FakeApplication,
    FakeDataFile,
    FakeDocuments,
    FakeFusionDocument,
    load_tool,
)

wg = load_tool("_write_guard")

# The guard's real read functions, captured before any test stubs them. _set_active assigns over
# wg._active_identity WITHOUT undoing itself (the conftest seam restore does not track this attr),
# so the live-read tests below must reinstall the real functions explicitly.
_REAL_ACTIVE_IDENTITY = wg._active_identity
_REAL_OPEN_DOCUMENTS = wg._open_documents


def _set_active(name, urn):
    wg._active_identity = lambda: (name, urn)


def _ok(payload):
    return {"content": [{"type": "text", "text": json.dumps(payload)}], "isError": False}


def _decode(result):
    return json.loads(result["content"][0]["text"])


class TestActedOnStamp:
    @pytest.fixture(autouse=True)
    def healthy_scan(self, monkeypatch):
        monkeypatch.setattr(wg, "_open_documents", lambda: [{"name": "Bracket", "document_id": "urn:abc"}])

    def test_successful_write_is_stamped(self):
        _set_active("Bracket", "urn:lineage:abc")
        h = wg.wrap(lambda **kw: _ok({"created": True}))
        out = _decode(h())
        assert out["created"] is True
        assert out["acted_on"] == {"name": "Bracket", "document_id": "urn:lineage:abc"}

    def test_handler_supplied_acted_on_survives_unchanged(self):
        # FILL-IF-ABSENT: a write that targets a NON-active document (doc_close closing an inactive
        # doc) publishes its own acted_on and is authoritative - the guard's post-call active read
        # names a document that was never written, so it must not overwrite this one.
        _set_active("StillOpen", "urn:still-open")
        h = wg.wrap(lambda **kw: _ok({"closed": ["Scratch"],
                                      "acted_on": {"name": "Scratch", "document_id": "urn:scratch"}}))
        out = _decode(h())
        assert out["acted_on"] == {"name": "Scratch", "document_id": "urn:scratch"}

    def test_handler_without_acted_on_is_still_stamped(self):
        # The other half of fill-if-absent: absent the handler's own claim, the active identity stands.
        _set_active("Bracket", "urn:lineage:abc")
        h = wg.wrap(lambda **kw: _ok({"closed": ["Bracket"]}))
        out = _decode(h())
        assert out["acted_on"] == {"name": "Bracket", "document_id": "urn:lineage:abc"}

    def test_an_explicit_null_acted_on_is_left_alone(self):
        # A handler that acted on SEVERAL documents (doc_close close_all) publishes acted_on:null
        # deliberately - one identity cannot name them. The key is PRESENT, so fill-if-absent must
        # leave it: filling it would name the one document still open as the one acted on.
        _set_active("StillOpen", "urn:still-open")
        h = wg.wrap(lambda **kw: _ok({"closed": ["A", "B"], "acted_on": None}))
        out = _decode(h())
        assert "acted_on" in out and out["acted_on"] is None

    def test_error_result_is_not_stamped(self):
        _set_active("Bracket", "urn:abc")
        h = wg.wrap(lambda **kw: {"content": [{"type": "text", "text": "boom"}],
                                  "isError": True, "message": "boom"})
        out = h()
        assert out["isError"] is True and "acted_on" not in out["content"][0]["text"]

    def test_handler_does_not_see_expect_document(self):
        _set_active("Bracket", "urn:abc")
        seen = {}
        h = wg.wrap(lambda **kw: seen.update(kw) or _ok({"ok": True}))
        h(expect_document="Bracket", distance=5)
        assert "expect_document" not in seen and seen == {"distance": 5}   # consumed by the guard


class TestReadStamp:
    """wrap_read: every read result says which document it was read from - two identical tallies
    from two different documents are otherwise indistinguishable."""

    def test_read_result_stamped_with_active_document(self):
        _set_active("Bracket", "urn:lineage:abc")
        h = wg.wrap_read(lambda **kw: _ok({"bodies": 3}))
        out = _decode(h())
        assert out["bodies"] == 3
        assert out["active_document"] == {"name": "Bracket", "document_id": "urn:lineage:abc"}

    def test_handler_payload_wins_over_stamp(self):
        # setdefault semantics: a tool that already reports its own active_document keeps it.
        _set_active("Bracket", "urn:abc")
        h = wg.wrap_read(lambda **kw: _ok({"active_document": {"name": "X", "document_id": "y"}}))
        out = _decode(h())
        assert out["active_document"] == {"name": "X", "document_id": "y"}

    def test_error_result_not_stamped(self):
        _set_active("Bracket", "urn:abc")
        h = wg.wrap_read(lambda **kw: {"content": [{"type": "text", "text": "boom"}],
                                       "isError": True, "message": "boom"})
        out = h()
        assert out["isError"] is True and "active_document" not in out["content"][0]["text"]

    def test_image_result_untouched(self):
        # a screenshot-style result (image block) has no JSON to stamp - passes through unchanged.
        _set_active("Bracket", "urn:abc")
        result = {"content": [{"type": "image", "data": "abc", "mimeType": "image/png"}],
                  "isError": False}
        h = wg.wrap_read(lambda **kw: result)
        assert h() is result

    def test_kwargs_pass_through_unconsumed(self):
        # reads have no expect_document contract - every kwarg reaches the handler.
        _set_active("Bracket", "urn:abc")
        seen = {}
        h = wg.wrap_read(lambda **kw: seen.update(kw) or _ok({"ok": True}))
        h(include=["tree"], max_depth=3)
        assert seen == {"include": ["tree"], "max_depth": 3}


class TestExpectDocumentGuard:
    @pytest.fixture(autouse=True)
    def healthy_scan(self, monkeypatch):
        monkeypatch.setattr(wg, "_open_documents", lambda: [{"name": "Bracket", "document_id": "urn:abc"}])

    def test_match_by_name_proceeds(self):
        _set_active("Bracket", "urn:abc")
        called = {"n": 0}
        h = wg.wrap(lambda **kw: called.update(n=1) or _ok({"created": True}))
        out = _decode(h(expect_document="Bracket"))
        assert called["n"] == 1 and out["created"] is True

    def test_match_by_urn_proceeds(self):
        _set_active("Bracket", "urn:lineage:abc")
        h = wg.wrap(lambda **kw: _ok({"created": True}))
        out = _decode(h(expect_document="urn:lineage:abc"))
        assert out["created"] is True

    def test_mismatch_refuses_without_calling_handler(self):
        _set_active("OtherDoc", "urn:other")
        called = {"n": 0}
        h = wg.wrap(lambda **kw: called.update(n=1) or _ok({"created": True}))
        res = h(expect_document="Bracket")
        assert called["n"] == 0                                  # the handler NEVER ran (no mutation)
        assert res["isError"] is True
        payload = _decode(res)
        assert payload["blocked_by"] == ["active_document_changed"]
        assert payload["expected"] == "Bracket"
        assert payload["actual"] == {"name": "OtherDoc", "document_id": "urn:other"}
        assert payload["requires"]["tool"] == "doc_activate"

    def test_omitted_expect_document_proceeds(self):
        _set_active("Whatever", "urn:x")
        h = wg.wrap(lambda **kw: _ok({"created": True}))
        out = _decode(h())                                       # no expect_document -> unchanged behavior
        assert out["created"] is True and out["acted_on"]["name"] == "Whatever"

    def test_doc_switching_write_reports_the_new_doc(self):
        # doc_new/doc_open/doc_activate make a DIFFERENT document active as their own effect.
        # acted_on must stamp the document active AFTER the handler ran, not the one active before it.
        calls = {"n": 0}

        def _identity():
            calls["n"] += 1
            return ("OldDoc", "urn:old") if calls["n"] == 1 else ("NewDoc", "urn:new")
        wg._active_identity = _identity
        h = wg.wrap(lambda **kw: _ok({"opened": True}))
        out = _decode(h())
        assert out["acted_on"] == {"name": "NewDoc", "document_id": "urn:new"}


class TestNameCollisionRefusal:
    """expect_document as a bare NAME is honored only when that name is unique among open docs."""

    def _docs(self, monkeypatch, rows):
        monkeypatch.setattr(wg, "_open_documents", lambda: rows)

    def test_unique_name_still_passes(self, monkeypatch):
        _set_active("Bracket", "urn:lineage:abc")
        self._docs(monkeypatch, [
            {"name": "Bracket", "document_id": "urn:lineage:abc", "open_index": 0, "is_active": True},
            {"name": "Other", "document_id": "urn:lineage:zzz", "open_index": 1, "is_active": False},
        ])
        called = {"n": 0}
        h = wg.wrap(lambda **kw: called.update(n=1) or _ok({"created": True}))
        out = _decode(h(expect_document="Bracket"))
        assert called["n"] == 1 and out["created"] is True

    def test_duplicate_names_refuse_listing_each_candidate(self, monkeypatch):
        # two open docs named "Bracket": one saved (URN), one unsaved (open_index only) - the
        # unsaved-twin case doc_get's open_index convention exists for.
        _set_active("Bracket", "urn:lineage:abc")
        self._docs(monkeypatch, [
            {"name": "Bracket", "document_id": "urn:lineage:abc", "open_index": 0, "is_active": True},
            {"name": "Bracket", "document_id": None, "open_index": 2, "is_active": False},
        ])
        called = {"n": 0}
        h = wg.wrap(lambda **kw: called.update(n=1) or _ok({"created": True}))
        res = h(expect_document="Bracket")
        assert called["n"] == 0                              # REFUSED - no handler call, no mutation
        assert res["isError"] is True
        payload = _decode(res)
        assert payload["blocked_by"] == ["ambiguous_document_name"]
        assert payload["expected"] == "Bracket"
        assert len(payload["candidates"]) == 2
        saved = payload["candidates"][0]
        unsaved = payload["candidates"][1]
        assert saved == {"name": "Bracket", "document_id": "urn:lineage:abc", "document_handle": None}
        assert unsaved["document_id"] is None
        assert unsaved["open_index"] == 2                    # the unsaved twin's session address
        assert "URN" in payload["note"]                      # instructs passing the URN

    def test_urn_match_is_exact_even_when_names_collide(self, monkeypatch):
        _set_active("Bracket", "urn:lineage:abc")
        self._docs(monkeypatch, [
            {"name": "Bracket", "document_id": "urn:lineage:abc", "open_index": 0, "is_active": True},
            {"name": "Bracket", "document_id": None, "open_index": 1, "is_active": False},
        ])
        h = wg.wrap(lambda **kw: _ok({"created": True}))
        out = _decode(h(expect_document="urn:lineage:abc"))   # URN pins ONE doc; collision irrelevant
        assert out["created"] is True

    def test_same_urn_twins_are_one_document_and_pass(self, monkeypatch):
        # An assembly loads its references as real Documents: a visible tab and its own
        # dependency instance share the name AND the lineage URN. That is ONE document - the
        # write must proceed, not refuse (live sighting: expect_document='P6-Vise' refused
        # while the tab and its xref dependency both carried the same URN).
        _set_active("P6-Vise", "urn:lineage:vise")
        self._docs(monkeypatch, [
            {"name": "P6-Vise", "document_id": "urn:lineage:vise", "open_index": 0, "is_active": True},
            {"name": "P6-Vise", "document_id": "urn:lineage:vise", "open_index": 3, "is_active": False},
        ])
        called = {"n": 0}
        h = wg.wrap(lambda **kw: called.update(n=1) or _ok({"edited": True}))
        out = _decode(h(expect_document="P6-Vise"))
        assert called["n"] == 1 and out["edited"] is True

    def test_two_unsaved_twins_are_not_one_document(self):
        # Two unsaved docs have no URN at all - two Nones prove identity of NOTHING. The all(ids)
        # clause is what answers here: without it, [None, None] collapses to one set entry and the
        # predicate would call two unsaved 'Untitled' twins one document.
        assert wg.one_open_document([None, None]) is False
        assert wg.one_open_document([None]) is False

    def test_two_unsaved_same_name_docs_refuse_the_bare_name(self, monkeypatch):
        # The session state Fusion mints by default: several unsaved docs all named 'Untitled',
        # none carrying a URN. A bare-name write cannot prove which one the agent read - the
        # guard must refuse, not land on whichever twin is active.
        _set_active("Untitled", None)
        self._docs(monkeypatch, [
            {"name": "Untitled", "document_id": None, "open_index": 0, "is_active": True},
            {"name": "Untitled", "document_id": None, "open_index": 1, "is_active": False},
        ])
        called = {"n": 0}
        res = wg.wrap(lambda **kw: called.update(n=1) or _ok({}))(expect_document="Untitled")
        assert called["n"] == 0                              # REFUSED - no handler call
        assert res["isError"] is True
        assert _decode(res)["blocked_by"] == ["ambiguous_document_name"]

    def test_mixed_urn_candidates_still_refuse_with_deduped_rows(self, monkeypatch):
        # Genuinely different documents sharing a name still refuse - and a candidate URN
        # appearing twice (tab + dependency of the SAME doc among a real ambiguity) collapses
        # to one row, so the agent never sees the same URN listed as two choices.
        _set_active("Bracket", "urn:lineage:abc")
        self._docs(monkeypatch, [
            {"name": "Bracket", "document_id": "urn:lineage:abc", "open_index": 0, "is_active": True},
            {"name": "Bracket", "document_id": "urn:lineage:abc", "open_index": 2, "is_active": False},
            {"name": "Bracket", "document_id": "urn:lineage:zzz", "open_index": 3, "is_active": False},
        ])
        res = wg.wrap(lambda **kw: _ok({}))(expect_document="Bracket")
        assert res["isError"] is True
        payload = _decode(res)
        assert payload["blocked_by"] == ["ambiguous_document_name"]
        urns = [c["document_id"] for c in payload["candidates"]]
        assert urns.count("urn:lineage:abc") == 1 and "urn:lineage:zzz" in urns

    def test_name_not_matching_active_doc_still_refuses_as_changed(self, monkeypatch):
        # the collision check only runs when the name DOES match the active doc; a plain mismatch
        # keeps the original active_document_changed refusal.
        _set_active("OtherDoc", "urn:other")
        self._docs(monkeypatch, [
            {"name": "Bracket", "document_id": "urn:b1", "open_index": 0, "is_active": False},
            {"name": "Bracket", "document_id": "urn:b2", "open_index": 1, "is_active": False},
        ])
        res = wg.wrap(lambda **kw: _ok({}))(expect_document="Bracket")
        assert res["isError"] is True
        assert _decode(res)["blocked_by"] == ["active_document_changed"]

    def test_empty_census_refuses_without_calling_handler(self, monkeypatch):
        _set_active("Bracket", "urn:lineage:abc")
        self._docs(monkeypatch, [])
        called = []
        result = wg.wrap(lambda **kw: called.append(kw) or _ok({"created": True}))(
            expect_document="Bracket")
        assert called == [] and result["isError"] is True
        payload = _decode(result)
        assert payload["blocked_by"] == ["document_collection_unreadable"]
        assert payload["expected"] == "Bracket"
        assert payload["requires"] == {
            "tool": "doc_get", "result": "open_documents[].document_handle"}


def _FakeDoc(name=None, urn=None, name_raises=False, datafile_raises=False):
    """One open document: the shared FusionDocument fake, carrying a DataFile only when `urn` names
    one. `name_raises`/`datafile_raises` pick the scenario subclass whose one read declines."""
    cls = (_MuteNameDocument if name_raises else
           _MuteDataFileDocument if datafile_raises else FakeFusionDocument)
    return cls(name=name, data_file=FakeDataFile(file_id=urn) if urn else None)


class _MuteNameDocument(FakeFusionDocument):
    """A document whose NAME read raises while its dataFile still answers - a document mid-load or
    mid-close. Declared, not measured: the closed-wrapper row covers a name that raises on a document
    that is GONE, and this one is still in the session's walk."""

    @property
    def name(self):
        raise RuntimeError("name unreadable")


class _MuteDataFileDocument(FakeFusionDocument):
    """The other half of the pair: the dataFile read raises while the name still answers, so an
    identity degrades to name-only rather than going blank."""

    @property
    def dataFile(self):
        raise RuntimeError("dataFile unreadable")

    @dataFile.setter
    def dataFile(self, value):
        self._data_file = value


class _HoledDocuments(FakeDocuments):
    """The session walk with holes: `broken_indices` are the slots whose item() raises (a document
    mid-close) and `count_raises` the length that will not read at all. Declared states - what the
    guard has to walk around instead of dropping the session."""

    def __init__(self, documents=(), count_raises=False, broken_indices=()):
        FakeDocuments.__init__(self, documents)
        self._count_raises = count_raises
        self._broken = set(broken_indices)

    @property
    def count(self):
        if self._count_raises:
            raise RuntimeError("count unreadable")
        return FakeDocuments.count.fget(self)

    def item(self, i):
        if i in self._broken:
            raise RuntimeError("item unreadable")
        return FakeDocuments.item(self, i)


class _SessionApp(FakeApplication):
    """The session seam with its two reads made to decline: `active_raises` makes activeDocument
    throw and `docs_raises` app.documents. `docs` is taken AS GIVEN, so a session answering no
    documents collection at all (None) stays expressible. Declared states, not measured ones."""

    def __init__(self, active=None, docs=None, active_raises=False, docs_raises=False):
        self._active_raises = active_raises
        self._docs_raises = docs_raises
        FakeApplication.__init__(self, active_document=active)
        self._docs = docs

    @property
    def activeDocument(self):
        if self._active_raises:
            raise RuntimeError("activeDocument unreadable")
        return self._active_doc

    @activeDocument.setter
    def activeDocument(self, value):
        self._active_doc = value

    @property
    def documents(self):
        if self._docs_raises:
            raise RuntimeError("documents unreadable")
        return self._docs

    @documents.setter
    def documents(self, value):
        self._docs = value


@pytest.fixture
def live_app(monkeypatch):
    """Install an Application onto the guard's app seam, exercising the REAL
    _active_identity/_open_documents reads (the earlier classes stub those seams instead)."""
    def _install(active=None, docs=None, **kw):
        app = _SessionApp(active=active, docs=docs, **kw)
        monkeypatch.setattr(wg, "app", app)
        monkeypatch.setattr(wg, "_active_identity", _REAL_ACTIVE_IDENTITY)
        monkeypatch.setattr(wg, "_open_documents", _REAL_OPEN_DOCUMENTS)
        return app
    return _install


class TestActiveIdentityLiveReads:
    """The guard's own read of the active document, observed on the wire (acted_on / refusal.actual)."""

    def test_acted_on_reads_name_and_urn_off_the_live_document(self, live_app):
        live_app(active=_FakeDoc("Bracket", "urn:lineage:abc"))
        out = _decode(wg.wrap(lambda **kw: _ok({"created": True}))())
        assert out["acted_on"] == {"name": "Bracket", "document_id": "urn:lineage:abc"}

    def test_refusal_reports_the_live_identity_on_mismatch(self, live_app):
        live_app(active=_FakeDoc("Other", "urn:other"))
        res = wg.wrap(lambda **kw: _ok({}))(expect_document="Bracket")
        assert res["isError"] is True
        assert _decode(res)["actual"] == {"name": "Other", "document_id": "urn:other"}

    def test_no_active_document_stamps_none_identity(self, live_app):
        live_app(active=None)
        out = _decode(wg.wrap(lambda **kw: _ok({"created": True}))())
        assert out["acted_on"] == {"name": None, "document_id": None}

    def test_no_active_document_refuses_an_expectation(self, live_app):
        # expect_document names a doc but NOTHING is active - that is a mismatch, not a pass.
        live_app(active=None)
        called = {"n": 0}
        res = wg.wrap(lambda **kw: called.update(n=1) or _ok({}))(expect_document="Bracket")
        assert called["n"] == 0 and res["isError"] is True
        assert _decode(res)["actual"] == {"name": None, "document_id": None}

    def test_unreadable_active_document_degrades_to_none_identity(self, live_app):
        live_app(active_raises=True)
        out = _decode(wg.wrap(lambda **kw: _ok({"created": True}))())
        assert out["acted_on"] == {"name": None, "document_id": None}

    def test_unsaved_document_stamps_name_without_urn(self, live_app):
        live_app(active=_FakeDoc("Untitled", None))          # no dataFile yet - never saved
        out = _decode(wg.wrap(lambda **kw: _ok({"created": True}))())
        assert out["acted_on"] == {"name": "Untitled", "document_id": None}

    def test_unreadable_name_keeps_the_urn_and_a_urn_match_still_passes(self, live_app):
        live_app(active=_FakeDoc(None, "urn:lineage:abc", name_raises=True))
        out = _decode(wg.wrap(lambda **kw: _ok({"created": True}))(expect_document="urn:lineage:abc"))
        assert out["created"] is True
        assert out["acted_on"] == {"name": None, "document_id": "urn:lineage:abc"}

    def test_unreadable_datafile_keeps_the_name(self, live_app):
        active = _FakeDoc("Bracket", "urn:abc", datafile_raises=True)
        live_app(active=active, docs=_HoledDocuments([active]))
        out = _decode(wg.wrap(lambda **kw: _ok({"created": True}))(expect_document="Bracket"))
        assert out["created"] is True
        assert out["acted_on"] == {"name": "Bracket", "document_id": None}


class TestOpenDocumentsSessionWalk:
    """The REAL session walk behind the name-collision check (the earlier class stubs it)."""

    def test_census_omitting_active_name_refuses_without_calling_handler(self, live_app):
        live_app(active=_FakeDoc("Bracket", "urn:a"),
                 docs=_HoledDocuments([_FakeDoc("Other", "urn:o")]))
        called = []
        result = wg.wrap(lambda **kw: called.append(kw) or _ok({"created": True}))(
            expect_document="Bracket")
        assert called == [] and result["isError"] is True
        payload = _decode(result)
        assert payload["blocked_by"] == ["document_collection_unreadable"]
        assert payload["expected"] == "Bracket"
        assert payload["actual"] == {"name": "Bracket", "document_id": "urn:a"}
        assert payload["requires"] == {
            "tool": "doc_get", "result": "open_documents[].document_handle"}

    def test_collision_refusal_lists_live_candidates(self, live_app):
        active = _FakeDoc("Bracket", "urn:lineage:abc")
        live_app(active=active, docs=_HoledDocuments([active, _FakeDoc("Bracket", None)]))
        called = {"n": 0}
        res = wg.wrap(lambda **kw: called.update(n=1) or _ok({}))(expect_document="Bracket")
        assert called["n"] == 0 and res["isError"] is True
        payload = _decode(res)
        assert payload["blocked_by"] == ["ambiguous_document_name"]
        assert payload["candidates"][0] == {"name": "Bracket", "document_id": "urn:lineage:abc",
                                              "document_handle": wg.document_handle(active)}
        assert payload["candidates"][1]["open_index"] == 1   # the unsaved twin's session address

    def test_unreadable_doc_is_not_fatal_to_the_walk(self, live_app):
        # An unreadable item leaves the name census incomplete, so the bare-name write refuses.
        active = _FakeDoc("Bracket", "urn:lineage:abc")
        twin = _FakeDoc("Bracket", None)
        live_app(active=active, docs=_HoledDocuments([active, _FakeDoc("X", "urn:x"), twin],
                                               broken_indices=(1,)))
        res = wg.wrap(lambda **kw: _ok({}))(expect_document="Bracket")
        assert res["isError"] is True
        payload = _decode(res)
        assert payload["blocked_by"] == ["document_collection_unreadable"]
        assert payload["expected"] == "Bracket"

    def test_an_unreadable_slot_is_published_as_the_hole_it_is(self, live_app):
        # A failed item remains an explicit unreadable row and marks the name census incomplete.
        active = _FakeDoc("Bracket", "urn:a")
        live_app(active=active, docs=_HoledDocuments([active, _FakeDoc("Other", "urn:o")],
                                               broken_indices=(1,)))
        rows = wg._open_documents()
        assert len(rows) == 2
        assert rows[1] == {"name": None, "readable": False}
        assert rows[0]["open_index"] == 0 and rows[0]["is_active"] is True

    def test_a_slot_answering_no_document_is_published_the_same_way(self, live_app):
        # item(i) can answer None rather than raise; both forms mark the census incomplete.
        active = _FakeDoc("Bracket", "urn:a")
        live_app(active=active, docs=_HoledDocuments([active, None]))
        assert wg._open_documents()[1] == {"name": None, "readable": False}

    def test_a_published_hole_never_becomes_a_name_collision_candidate(self, live_app):
        # An unreadable slot leaves document-name uniqueness unproven, so the write refuses.
        active = _FakeDoc("Bracket", "urn:a")
        live_app(active=active, docs=_HoledDocuments([active, _FakeDoc("Bracket", None)],
                                               broken_indices=(1,)))
        out = _decode(wg.wrap(lambda **kw: _ok({"created": True}))(expect_document="Bracket"))
        assert out["blocked_by"] == ["document_collection_unreadable"]

    def test_doc_with_unreadable_name_refuses_bare_name(self, live_app):
        active = _FakeDoc("Bracket", "urn:lineage:abc")
        live_app(active=active, docs=_HoledDocuments([active, _FakeDoc("Bracket", "urn:z", name_raises=True)]))
        out = _decode(wg.wrap(lambda **kw: _ok({"created": True}))(expect_document="Bracket"))
        assert out["blocked_by"] == ["document_collection_unreadable"]

    def test_doc_with_name_none_refuses_bare_name(self, live_app):
        active = _FakeDoc("Bracket", "urn:lineage:abc")
        live_app(active=active, docs=_HoledDocuments([active, _FakeDoc(None, "urn:z")]))
        out = _decode(wg.wrap(lambda **kw: _ok({"created": True}))(expect_document="Bracket"))
        assert out["blocked_by"] == ["document_collection_unreadable"]

    def test_candidate_with_unreadable_datafile_is_listed_by_open_index(self, live_app):
        active = _FakeDoc("Bracket", "urn:lineage:abc")
        live_app(active=active, docs=_HoledDocuments([active, _FakeDoc("Bracket", "urn:z", datafile_raises=True)]))
        res = wg.wrap(lambda **kw: _ok({}))(expect_document="Bracket")
        payload = _decode(res)
        assert payload["blocked_by"] == ["ambiguous_document_name"]
        assert payload["candidates"][1]["document_id"] is None
        assert payload["candidates"][1]["open_index"] == 1

    def test_walk_marks_exactly_the_active_row(self, live_app):
        active = _FakeDoc("Bracket", "urn:a")
        live_app(active=active, docs=_HoledDocuments([_FakeDoc("Other", "urn:o"), active]))
        rows = wg._open_documents()
        assert [r["is_active"] for r in rows] == [False, True]
        assert [r["open_index"] for r in rows] == [0, 1]

    def test_unreadable_documents_collection_refuses_bare_name(self, live_app):
        live_app(active=_FakeDoc("Bracket", "urn:a"), docs_raises=True)
        out = _decode(wg.wrap(lambda **kw: _ok({"created": True}))(expect_document="Bracket"))
        assert out["blocked_by"] == ["document_collection_unreadable"]

    def test_missing_documents_collection_refuses_bare_name(self, live_app):
        live_app(active=_FakeDoc("Bracket", "urn:a"), docs=None)
        out = _decode(wg.wrap(lambda **kw: _ok({"created": True}))(expect_document="Bracket"))
        assert out["blocked_by"] == ["document_collection_unreadable"]

    def test_unreadable_count_refuses_bare_name(self, live_app):
        live_app(active=_FakeDoc("Bracket", "urn:a"),
                 docs=_HoledDocuments([_FakeDoc("Bracket", "urn:a")], count_raises=True))
        out = _decode(wg.wrap(lambda **kw: _ok({"created": True}))(expect_document="Bracket"))
        assert out["blocked_by"] == ["document_collection_unreadable"]

    def test_zero_count_refuses_when_active_name_is_known(self, live_app):
        live_app(active=_FakeDoc("Bracket", "urn:a"), docs=_HoledDocuments([]))
        out = _decode(wg.wrap(lambda **kw: _ok({"created": True}))(expect_document="Bracket"))
        assert out["blocked_by"] == ["document_collection_unreadable"]


class TestWrapEdges:
    def test_whitespace_expect_document_is_treated_as_omitted(self, live_app):
        # "  " names no document; the guard must not refuse against it - the write proceeds
        # on the active doc, same as omitting expect_document.
        live_app(active=_FakeDoc("Whatever", "urn:x"))
        out = _decode(wg.wrap(lambda **kw: _ok({"created": True}))(expect_document="   "))
        assert out["created"] is True and out["acted_on"]["name"] == "Whatever"

    def test_non_json_text_result_passes_through_unstamped(self, live_app):
        live_app(active=_FakeDoc("Bracket", "urn:x"))
        res = wg.wrap(lambda **kw: {"content": [{"type": "text", "text": "done."}], "isError": False})()
        assert res["content"][0]["text"] == "done."          # byte-identical, no stamp injected

    def test_json_array_result_passes_through_unstamped(self, live_app):
        live_app(active=_FakeDoc("Bracket", "urn:x"))
        res = wg.wrap(lambda **kw: {"content": [{"type": "text", "text": "[1, 2]"}], "isError": False})()
        assert res["content"][0]["text"] == "[1, 2]"         # a JSON list has no keys to stamp

    def test_non_text_first_block_passes_through_unstamped(self, live_app):
        live_app(active=_FakeDoc("Bracket", "urn:x"))
        block = {"type": "image", "data": "abc"}
        res = wg.wrap(lambda **kw: {"content": [block], "isError": False})()
        assert res["content"][0] == {"type": "image", "data": "abc"}

    def test_empty_content_passes_through(self, live_app):
        live_app(active=_FakeDoc("Bracket", "urn:x"))
        res = wg.wrap(lambda **kw: {"content": [], "isError": False})()
        assert res == {"content": [], "isError": False}


class TestIntegrationThroughItem:
    """Item.create_tool_item is where the wrapping is DECIDED. Without these, the whole fleet can
    lose the stamp (or gain one on an off-thread handler) with every other test still green."""

    def _read_item(self, run_on_main_thread):
        from mcpServer.mcp_primitives.item import Item
        from mcpServer.mcp_primitives.tool import Tool
        tool = Tool.create_simple(name="probe_get", description="A read that pins the wrapping.")
        return Item.create_tool_item(tool=tool, write="read",
                                     handler=lambda **kw: _ok({"bodies": 3}),
                                     run_on_main_thread=run_on_main_thread)

    def test_write_tool_gains_expect_document_read_does_not(self):
        # create_tool_item wraps write handlers + adds the arg; read tools are untouched.
        ex = load_tool("model_extrude")
        assert "expect_document" in ex.extrude_tool.to_dict()["inputSchema"]["properties"]
        dg = load_tool("design_get")
        assert "expect_document" not in dg.tool.to_dict()["inputSchema"]["properties"]

    def test_main_thread_read_is_stamped_at_registration(self):
        # The stamp is wired ONCE here, not per tool - unwire it and every read in the fleet stops
        # reporting which document it came from.
        _set_active("Bracket", "urn:lineage:abc")
        out = _decode(self._read_item(True).handler())
        assert out["bodies"] == 3
        assert out["active_document"] == {"name": "Bracket", "document_id": "urn:lineage:abc"}

    def test_off_main_thread_read_is_not_wrapped(self):
        # The carve-out: the identity read touches adsk, so a pure-Python off-thread handler must
        # not be wrapped with it. Stamping one anyway is the crash this condition exists to prevent.
        _set_active("Bracket", "urn:lineage:abc")
        out = _decode(self._read_item(False).handler())
        assert out["bodies"] == 3 and "active_document" not in out

    def test_a_read_is_not_given_the_write_guard(self):
        # wrap_read only - a read must never consume expect_document or stamp acted_on.
        _set_active("Bracket", "urn:lineage:abc")
        out = _decode(self._read_item(True).handler())
        assert "acted_on" not in out

    def test_design_get_is_registered_wrapped_and_sys_find_tool_is_not(self):
        # The two real ends of the carve-out: design_get runs on the main thread (wrapped),
        # sys_find_tool is pure Python and registers run_on_main_thread=False (not wrapped).
        dg = load_tool("design_get")
        assert dg.item.run_on_main_thread is True
        assert getattr(dg.item.handler, "__wrapped__", None) is dg.handler
        sft = load_tool("sys_find_tool")
        assert sft.item.run_on_main_thread is False
        assert sft.item.handler is sft.handler

    def test_off_thread_read_result_carries_no_stamp_end_to_end(self):
        _set_active("Bracket", "urn:lineage:abc")
        sft = load_tool("sys_find_tool")
        assert "active_document" not in _decode(sft.item.handler(query="extrude"))


class TestOpenDocumentsActiveFlag:
    """The REAL _open_documents against the measured wrapper contract: Document wrappers are not
    identity-stable (`is` reads False for the active document itself, live on 2705.0.87), while
    `==` compares the underlying handle - the flag must come from equality."""

    class _DocWrapper:
        def __init__(self, handle, name):
            self._handle = handle
            self.name = name
            self.dataFile = None

        def __eq__(self, other):
            return getattr(other, "_handle", None) == self._handle

        __hash__ = None

    def _app(self, monkeypatch, wrappers, active):
        docs = SimpleNamespace(count=len(wrappers), item=lambda i: wrappers[i])
        monkeypatch.setattr(wg, "app", SimpleNamespace(documents=docs, activeDocument=active))

    def test_equal_but_not_identical_wrapper_reads_active(self, monkeypatch):
        row_doc = self._DocWrapper("h1", "Bracket")
        active = self._DocWrapper("h1", "Bracket")      # a DISTINCT wrapper of the same doc
        assert row_doc is not active
        self._app(monkeypatch, [row_doc], active)
        rows = _REAL_OPEN_DOCUMENTS()
        assert rows[0]["is_active"] is True

    def test_a_different_document_reads_inactive(self, monkeypatch):
        active = self._DocWrapper("h1", "Bracket")
        other = self._DocWrapper("h2", "Bracket")       # same NAME, different handle
        self._app(monkeypatch, [other], active)
        rows = _REAL_OPEN_DOCUMENTS()
        assert rows[0]["is_active"] is False

    def test_a_raising_equality_degrades_to_inactive_not_a_raise(self, monkeypatch):
        class _Hostile(self._DocWrapper):
            def __eq__(self, other):
                raise RuntimeError("equality unavailable")
        active = self._DocWrapper("h1", "Bracket")
        self._app(monkeypatch, [_Hostile("h1", "Bracket")], active)
        rows = _REAL_OPEN_DOCUMENTS()
        assert rows[0]["is_active"] is False


# ── document_key: the identity a store that outlives one MCP call remembers a document by ──────────


class _DocHandle:
    """One read of a document handle. Models the CLOSED-document behaviour as measured: after the
    document closes, a handle handed out earlier compares UNEQUAL to every live document (the
    comparison answers False, it does NOT raise) and isValid reads False."""

    def __init__(self, opened):
        self._opened = opened
        # A never-saved document answers dataFile None (measured - it does not raise); only a saved
        # one hands back a DataFile, and an EMPTY id models one whose id will not read.
        self.dataFile = FakeDataFile(file_id=opened.urn) if opened.urn is not None else None

    @property
    def isValid(self):
        return self._opened.is_open

    def __eq__(self, other):
        if not self._opened.is_open:
            return False
        return isinstance(other, _DocHandle) and other._opened is self._opened

    # Not hashable, like the wrapper it stands in for: anything keying a dict/set on a document
    # instead of comparing handles has to fail loudly here rather than silently mis-key.
    __hash__ = None


class _MuteValidityHandle(_DocHandle):
    """A handle whose isValid will not read - the branch an unreadable flag must NOT evict on."""

    @property
    def isValid(self):
        raise RuntimeError("isValid could not be read")

    __hash__ = None


class _UnreadableComparisonHandle(_DocHandle):
    """A handle whose `==` will not read - an ARBITRARY unreadable comparison, claiming nothing
    about any particular platform state (a closed document's handle compares False, it does not
    raise). It drives the safe() default: a comparison that cannot be read is not a match."""

    def __eq__(self, other):
        raise RuntimeError("the comparison could not be read")

    __hash__ = None


class _OpenDoc:
    """One open document, handing back a FRESH handle on every read - what the real API does
    (_open_documents carries the live measurement: `is` reads False for the one active document
    across two reads while `==` reads True). Two handles of THIS document compare equal; handles
    of a different _OpenDoc never do.

    `urn` is settable after construction: each handle snapshots it at read time, which is how a
    document that answers no data-file id and later answers one is modelled without the document
    ever closing."""

    def __init__(self, name="Untitled", urn=None, handle_class=_DocHandle):
        self.name = name
        self.urn = urn
        self.handle_class = handle_class
        self.is_open = True

    def handle(self):
        return self.handle_class(self)

    def close(self):
        """Close it. Handles already handed out (the registry holds one) stay reachable as Python
        objects and go invalid, which is what a closed Fusion document leaves behind."""
        self.is_open = False


class _RewrappingApp:
    """An app whose activeDocument read mints a NEW handle every time, like the real one."""

    def __init__(self, opened=None):
        self.opened = opened

    @property
    def activeDocument(self):
        return None if self.opened is None else self.opened.handle()


class TestDocumentKey:
    """The ONE key a store outliving a single MCP call remembers a document by - a view snapshot, a
    driven-joint registry, a live generation. Its two halves are the lineage urn for a saved
    document and, for one with no readable data-file id, a per-instance token matched by document
    HANDLE, since a never-saved document's NAME is not an identity (two open ones both answer
    'Untitled', measured live) and a name key hands one document's stored state to another.
    """

    @pytest.fixture(autouse=True)
    def _isolated_key_registry(self, monkeypatch):
        """The registry and its counter are module-level SESSION state shared by every consumer, so
        a test asserting on minted tokens has to start both from empty."""
        monkeypatch.setattr(wg, "_UNSAVED_DOC_SEQ", 0)
        wg._UNSAVED_DOC_KEYS.clear()
        yield
        wg._UNSAVED_DOC_KEYS.clear()

    def test_a_saved_document_keys_on_its_lineage_urn(self, monkeypatch):
        monkeypatch.setattr(wg, "app", _RewrappingApp(_OpenDoc("Bracket", urn="urn:lineage:1")))
        assert wg.document_key() == "urn:lineage:1"
        assert wg._UNSAVED_DOC_KEYS == []          # a document with an id needs nothing minted

    def test_a_data_file_whose_id_will_not_read_takes_a_per_instance_key(self, monkeypatch):
        # An EMPTY id is not an identity - keying on it would put every such document in one bucket.
        monkeypatch.setattr(wg, "app", _RewrappingApp(_OpenDoc("Shared", urn="")))
        assert wg.document_key() == "unsaved:1"

    def test_the_same_unsaved_document_keeps_one_key_across_calls(self, monkeypatch):
        # The match is `==`, not `is`: every activeDocument read hands back a NEW handle, and a
        # second key for one document would split its own stored state in half.
        monkeypatch.setattr(wg, "app", _RewrappingApp(_OpenDoc("Untitled")))
        assert wg.document_key() == wg.document_key() == "unsaved:1"
        assert len(wg._UNSAVED_DOC_KEYS) == 1

    def test_two_unsaved_documents_sharing_a_name_get_different_keys(self, monkeypatch):
        # THE BITE: both are named 'Untitled' and neither has a dataFile, so a NAME key hands both
        # the same string - and one document's stored state becomes reachable from the other.
        app = _RewrappingApp(_OpenDoc("Untitled"))
        monkeypatch.setattr(wg, "app", app)
        first = wg.document_key()
        app.opened = _OpenDoc("Untitled")                  # a DIFFERENT document, same name
        assert wg.document_key() != first
        assert len(wg._UNSAVED_DOC_KEYS) == 2

    def test_no_readable_document_is_not_a_key(self, monkeypatch):
        monkeypatch.setattr(wg, "app", _SessionApp(active_raises=True))
        assert wg.document_key() is None
        assert wg._UNSAVED_DOC_KEYS == []                  # and it mints nothing to hand out

    def test_a_comparison_that_will_not_read_is_not_a_match(self, monkeypatch):
        # The safe() default. This handle's `==` raises - an ARBITRARY unreadable comparison (a
        # CLOSED document's handle answers False, it does not raise). A comparison nobody could read
        # is not evidence of a match, so the next document mints its own key instead of inheriting.
        app = _RewrappingApp(_OpenDoc("Untitled", handle_class=_UnreadableComparisonHandle))
        monkeypatch.setattr(wg, "app", app)
        first = wg.document_key()
        app.opened = _OpenDoc("Untitled")
        assert wg.document_key() != first

    def test_a_closed_document_is_evicted_and_cannot_hand_its_key_on(self, monkeypatch):
        # isValid False is the only thing that evicts. The next unsaved document must get a NEW
        # token, never the closed one's - inheriting it would inherit that document's stored state.
        gone = _OpenDoc("Untitled")
        app = _RewrappingApp(gone)
        monkeypatch.setattr(wg, "app", app)
        first = wg.document_key()
        gone.close()                                       # the tab is closed
        app.opened = _OpenDoc("Untitled")
        assert wg.document_key() != first
        assert len(wg._UNSAVED_DOC_KEYS) == 1              # the dead handle was dropped

    def test_several_closed_documents_are_all_evicted_in_one_pass(self, monkeypatch):
        # The eviction walks the registry BACKWARDS so a deletion cannot slide the next entry past
        # the cursor, and so the index it holds stays inside a list that is shrinking under it. Only
        # a registry holding MORE THAN ONE dead entry tells the two walks apart: a forward walk
        # skips the entry that slid into the freed slot and then indexes past the end, raising
        # IndexError out of document_key() and so out of every consumer's handler. A --keep-open
        # session full of scratch documents produces exactly this shape.
        first, second, live = _OpenDoc("Untitled"), _OpenDoc("Untitled"), _OpenDoc("Untitled")
        app = _RewrappingApp()
        monkeypatch.setattr(wg, "app", app)
        keys = []
        for opened in (first, second, live):
            app.opened = opened
            keys.append(wg.document_key())
        assert len(wg._UNSAVED_DOC_KEYS) == 3 and len(set(keys)) == 3
        first.close()
        second.close()                                     # two dead entries, adjacent, at the front
        app.opened = live
        assert wg.document_key() == keys[2]                # the survivor keeps its own key
        assert [k for _d, k in wg._UNSAVED_DOC_KEYS] == [keys[2]]

    def test_a_closed_document_is_evicted_by_a_read_taken_while_a_SAVED_one_is_active(self, monkeypatch):
        # The way a scratch document actually closes is that another document takes the foreground,
        # and that document is usually a saved one - which keys on its data-file id and never
        # reaches the unsaved scan. So the prune has to run before that branch returns, or the dead
        # entry and every consumer's state under its key survive the whole add-in session.
        gone = _OpenDoc("Untitled")
        app = _RewrappingApp(gone)
        monkeypatch.setattr(wg, "app", app)
        heard = []
        monkeypatch.setattr(wg, "_KEY_EVICTION_LISTENERS", [heard.append])
        key = wg.document_key()
        gone.close()
        app.opened = _OpenDoc("Bracket", urn="urn:lineage:1")      # a SAVED document takes over
        assert wg.document_key() == "urn:lineage:1"
        assert wg._UNSAVED_DOC_KEYS == []                          # the dead entry went
        assert heard == [key]                                      # and its holder was told which

    def test_an_unreadable_is_valid_does_not_evict(self, monkeypatch):
        # Only a definite False evicts: an isValid that will not read proves nothing about the
        # document, and dropping a LIVE document's key would mint it a second one.
        app = _RewrappingApp(_OpenDoc("Untitled", handle_class=_MuteValidityHandle))
        monkeypatch.setattr(wg, "app", app)
        first = wg.document_key()
        assert wg.document_key() == first
        assert len(wg._UNSAVED_DOC_KEYS) == 1

    def test_a_saved_document_first_seen_saved_registers_and_announces_nothing(self, monkeypatch):
        # The other side of the rename: a document that already answers an id when it is first seen
        # was never minted a key, so there is no old key to carry anything from. Announcing here
        # would tell every consumer to move state off a key it never used.
        heard = []
        monkeypatch.setattr(wg, "_KEY_RENAME_LISTENERS", [lambda *a: heard.append(a)])
        monkeypatch.setattr(wg, "app", _RewrappingApp(_OpenDoc("Bracket", urn="urn:lineage:1")))
        assert wg.document_key() == wg.document_key() == "urn:lineage:1"
        assert wg._UNSAVED_DOC_KEYS == [] and heard == []

    def test_every_listener_hears_every_evicted_key(self, monkeypatch):
        # The registry is SHARED, so whichever consumer's read happens to trigger the prune must
        # drop what ALL of them parked under that key. A listener told only about its own caller's
        # prune leaves the other consumers' state stranded under a key nothing matches again.
        heard_a, heard_b = [], []
        monkeypatch.setattr(wg, "_KEY_EVICTION_LISTENERS", [])
        wg.on_key_evicted(heard_a.append)
        wg.on_key_evicted(heard_b.append)
        gone_one, gone_two = _OpenDoc("Untitled"), _OpenDoc("Untitled")
        app = _RewrappingApp()
        monkeypatch.setattr(wg, "app", app)
        keys = []
        for opened in (gone_one, gone_two):
            app.opened = opened
            keys.append(wg.document_key())
        assert heard_a == []                               # nothing closed yet, nothing evicted
        gone_one.close()
        gone_two.close()
        app.opened = _OpenDoc("Untitled")
        wg.document_key()
        assert sorted(heard_a) == sorted(keys)
        assert heard_b == heard_a


class TestKeyRename:
    """A key that changes while its document stays OPEN - the other thing that happens to a key.

    A never-saved document keys on a minted token; the moment it is saved, dataFile.id reads and the
    key becomes that id. Nothing about that is a close, so the eviction listeners never fire, and a
    store still keyed on the superseded token holds state no live document keys to again - a view
    snapshot that can no longer be restored, a driven-joint entry that stops arming a crash guard.
    The change is announced instead, and BOTH keys name the same open document, so a consumer moves
    state rather than dropping it.
    """

    @pytest.fixture(autouse=True)
    def _isolated(self, monkeypatch):
        """The registry, the mint counter and the listener list are all shared session state."""
        monkeypatch.setattr(wg, "_UNSAVED_DOC_SEQ", 0)
        monkeypatch.setattr(wg, "_KEY_RENAME_LISTENERS", [])
        wg._UNSAVED_DOC_KEYS.clear()
        yield
        wg._UNSAVED_DOC_KEYS.clear()

    def _heard(self):
        """A listener registered the way a consumer registers one, collecting (old, new) pairs."""
        pairs = []
        wg.on_key_renamed(lambda old, new: pairs.append((old, new)))
        return pairs

    def test_a_save_re_keys_the_open_document_and_announces_it(self, monkeypatch):
        # THE BITE: one open document, first with no data-file id and then with one. The key it
        # answers changes, and without the announcement every consumer's state stays parked under
        # the token nothing keys to again.
        heard = self._heard()
        doc = _OpenDoc("Untitled")
        monkeypatch.setattr(wg, "app", _RewrappingApp(doc))
        assert wg.document_key() == "unsaved:1"
        doc.urn = "urn:lineage:saved"                      # doc_save_as lands; the id now reads
        assert wg.document_key() == "urn:lineage:saved"
        assert heard == [("unsaved:1", "urn:lineage:saved")]

    def test_the_second_flip_is_announced_from_the_key_it_last_held(self, monkeypatch):
        # The id may not arrive settled - IF a path form answers before the lineage urn (PROBE
        # NEEDED, KEY-2). The mechanism is tested regardless of what triggers a second flip: the
        # entry is REWRITTEN rather than dropped at the first, so the second is announced as
        # (path form -> urn). Announcing it as (unsaved:1 -> urn) would tell every consumer to move
        # state off a key it stopped using one call ago, and leave it under the path form.
        heard = self._heard()
        doc = _OpenDoc("Untitled")
        monkeypatch.setattr(wg, "app", _RewrappingApp(doc))
        assert wg.document_key() == "unsaved:1"
        doc.urn = "a.b.c:/Projects/Bracket.f3d"
        assert wg.document_key() == "a.b.c:/Projects/Bracket.f3d"
        doc.urn = "urn:lineage:saved"
        assert wg.document_key() == "urn:lineage:saved"
        assert heard == [("unsaved:1", "a.b.c:/Projects/Bracket.f3d"),
                         ("a.b.c:/Projects/Bracket.f3d", "urn:lineage:saved")]

    def test_a_key_that_did_not_change_announces_nothing(self, monkeypatch):
        # Repeated reads of one saved document are the common case; an announcement per call would
        # have every consumer re-addressing state on every read of every key.
        heard = self._heard()
        doc = _OpenDoc("Untitled")
        monkeypatch.setattr(wg, "app", _RewrappingApp(doc))
        wg.document_key()
        doc.urn = "urn:lineage:saved"
        wg.document_key()
        for _ in range(3):
            assert wg.document_key() == "urn:lineage:saved"
        assert heard == [("unsaved:1", "urn:lineage:saved")]      # exactly one, not four

    def test_the_renamed_document_keeps_ONE_registry_entry(self, monkeypatch):
        # The entry is rewritten in place. A second entry for the same document would let it answer
        # two keys depending on which the scan reached first, splitting its own stored state.
        doc = _OpenDoc("Untitled")
        monkeypatch.setattr(wg, "app", _RewrappingApp(doc))
        wg.document_key()
        doc.urn = "urn:lineage:saved"
        wg.document_key()
        assert [k for _d, k in wg._UNSAVED_DOC_KEYS] == ["urn:lineage:saved"]

    def test_every_listener_hears_the_rename(self, monkeypatch):
        # The registry is SHARED: whichever consumer's read triggers the flip must move what ALL of
        # them parked there. A listener told only about its own caller's flip leaves the other
        # consumers' state stranded under a key nothing answers again.
        first, second = self._heard(), self._heard()
        doc = _OpenDoc("Untitled")
        monkeypatch.setattr(wg, "app", _RewrappingApp(doc))
        wg.document_key()
        doc.urn = "urn:lineage:saved"
        wg.document_key()
        assert first == [("unsaved:1", "urn:lineage:saved")] and second == first

    def test_an_id_that_stops_reading_keeps_the_key_the_document_holds(self, monkeypatch):
        # An id that will not read is not evidence the document went back to having none. Falling
        # through to a fresh mint here would strand the state parked under the key it already
        # answers - the very defect the announcement exists to prevent, reintroduced by it.
        heard = self._heard()
        doc = _OpenDoc("Untitled")
        monkeypatch.setattr(wg, "app", _RewrappingApp(doc))
        wg.document_key()
        doc.urn = "urn:lineage:saved"
        assert wg.document_key() == "urn:lineage:saved"
        doc.urn = None                                     # the dataFile read goes quiet
        assert wg.document_key() == "urn:lineage:saved"    # not a new token, not a second entry
        assert heard == [("unsaved:1", "urn:lineage:saved")]
        assert len(wg._UNSAVED_DOC_KEYS) == 1

    def test_a_different_document_does_not_inherit_the_renamed_key(self, monkeypatch):
        # The rename rewrites ONE entry, matched by handle. A second never-saved document mints its
        # own token: inheriting the renamed one would hand it the first document's stored state.
        doc = _OpenDoc("Untitled")
        app = _RewrappingApp(doc)
        monkeypatch.setattr(wg, "app", app)
        wg.document_key()
        doc.urn = "urn:lineage:saved"
        assert wg.document_key() == "urn:lineage:saved"
        app.opened = _OpenDoc("Untitled")                  # a DIFFERENT never-saved document
        assert wg.document_key() == "unsaved:2"

    def test_closing_a_renamed_document_evicts_the_key_it_last_held(self, monkeypatch):
        # Eviction reports the key the closed document ANSWERED, not the token minted for it: a
        # consumer that moved its state on the rename holds it under the new key, and naming the
        # superseded token would leave that state parked behind a closed document forever.
        evicted = []
        monkeypatch.setattr(wg, "_KEY_EVICTION_LISTENERS", [evicted.append])
        self._heard()
        doc = _OpenDoc("Untitled")
        app = _RewrappingApp(doc)
        monkeypatch.setattr(wg, "app", app)
        wg.document_key()
        doc.urn = "urn:lineage:saved"
        wg.document_key()
        doc.close()
        app.opened = _OpenDoc("Untitled")
        wg.document_key()
        assert evicted == ["urn:lineage:saved"]
        assert wg._UNSAVED_DOC_KEYS and [k for _d, k in wg._UNSAVED_DOC_KEYS] == ["unsaved:2"]


class TestSessionDocumentHandles:
    @pytest.fixture(autouse=True)
    def isolate(self, monkeypatch):
        monkeypatch.setattr(wg, "_SESSION_DOCUMENTS", [])

    def test_equal_wrappers_reuse_handle_across_save(self):
        doc = _OpenDoc()
        first = wg.document_handle(doc.handle())
        assert first.startswith("session:") and len(first) == 40
        assert wg.document_handle(doc.handle()) == first
        doc.urn = "urn:saved"
        assert wg.document_handle(doc.handle()) == first
        other = _OpenDoc(name=doc.name)
        assert wg.document_handle(other.handle()) != first
        assert wg.resolve_document_handle(first) == doc.handle()

    @pytest.mark.parametrize("validity", [False, None])
    def test_uncertain_validity_cannot_mint_or_resolve(self, validity):
        doc = _OpenDoc()
        handle = wg.document_handle(doc.handle())
        doc.is_open = validity
        assert wg.resolve_document_handle(handle) is None
        assert wg.document_handle(doc.handle()) is None

    def test_raising_validity_cannot_mint_or_resolve(self):
        doc = _OpenDoc(handle_class=_MuteValidityHandle)
        wrapper = doc.handle()
        handle = "session:" + "a" * 32
        wg._SESSION_DOCUMENTS.append((wrapper, handle))
        assert wg.resolve_document_handle(handle) is None
        assert wg.document_handle(wrapper) is None

    @pytest.mark.parametrize("address", ["session:" + "f" * 32, "session:malformed"])
    def test_unknown_handle_never_falls_back_to_same_named_document(self, monkeypatch, address):
        dc = load_tool("_doc_common")
        twin = FakeFusionDocument(name=address)
        monkeypatch.setattr(dc, "app", FakeApplication(documents=FakeDocuments([twin])))
        found, _names, ambiguous = dc._find_open_document(address)
        assert found is None and ambiguous is False
        assert wg._document_refusal(address, address, None) is not None

    def test_close_and_registry_reset_invalidate_old_addresses(self):
        doc = _OpenDoc()
        first = wg.document_handle(doc.handle())
        doc.close()
        assert wg.resolve_document_handle(first) is None
        another = _OpenDoc()
        second = wg.document_handle(another.handle())
        wg._SESSION_DOCUMENTS.clear()
        assert wg.resolve_document_handle(second) is None
        assert wg.document_handle(another.handle()) != second

    def test_closed_handle_requires_doc_get_reacquisition(self, monkeypatch):
        expected = _OpenDoc()
        handle = wg.document_handle(expected.handle())
        expected.close()
        active = _OpenDoc("Active")
        monkeypatch.setattr(wg, "app", _RewrappingApp(active))
        monkeypatch.setattr(wg, "_active_identity", lambda: ("Active", None))
        called = {"n": 0}
        res = wg.wrap(lambda **kw: called.update(n=1) or _ok({}))(expect_document=handle)
        payload = _decode(res)
        assert called["n"] == 0 and payload["blocked_by"] == ["unknown_document_handle"]
        assert payload["requires"] == {
            "tool": "doc_get", "result": "open_documents[].document_handle"}

    def test_reload_expired_handle_requires_doc_get_reacquisition(self, monkeypatch):
        expected = _OpenDoc()
        handle = wg.document_handle(expected.handle())
        wg._SESSION_DOCUMENTS.clear()
        active = _OpenDoc("Active")
        monkeypatch.setattr(wg, "app", _RewrappingApp(active))
        monkeypatch.setattr(wg, "_active_identity", lambda: ("Active", None))
        called = {"n": 0}
        res = wg.wrap(lambda **kw: called.update(n=1) or _ok({}))(expect_document=handle)
        payload = _decode(res)
        assert called["n"] == 0 and payload["blocked_by"] == ["unknown_document_handle"]
        assert payload["requires"]["tool"] == "doc_get"

    def test_live_wrong_active_handle_keeps_doc_activate_remedy(self, monkeypatch):
        expected = _OpenDoc("Expected")
        handle = wg.document_handle(expected.handle())
        active = _OpenDoc("Active")
        monkeypatch.setattr(wg, "app", _RewrappingApp(active))
        monkeypatch.setattr(wg, "_active_identity", lambda: ("Active", None))
        called = {"n": 0}
        res = wg.wrap(lambda **kw: called.update(n=1) or _ok({}))(expect_document=handle)
        payload = _decode(res)
        assert called["n"] == 0 and payload["blocked_by"] == ["active_document_changed"]
        assert payload["requires"] == {"tool": "doc_activate", "argument": handle}
