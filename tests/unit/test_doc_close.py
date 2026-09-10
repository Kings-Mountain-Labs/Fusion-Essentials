"""Unit tests for ``doc_close.py`` - closing one open document or every one of them.

Pinned, no live Fusion: a close() answering false is an error, an already-invalidated
reference proxy is skipped rather than failing the call, and acted_on names the document
that closed (null, with the reason, when one identity cannot name the result).
"""

import json

import pytest

from conftest import FakeApplication, FakeDataFile, FakeDocuments, FakeFusionDocument, load_tool

dm = load_tool("doc_close")
dk = load_tool("_doc_common")


def _payload(result):
    assert result["isError"] is False, result
    return json.loads(result["content"][0]["text"])


def _CloseableDoc(name, close_ok=True, urn=None, dead=False):
    """One open document. `urn` is its lineage id (absent = never saved); `dead` is the already-
    invalidated reference proxy a close_all meets."""
    return FakeFusionDocument(name=name, close_ok=close_ok, closed=dead,
                              data_file=FakeDataFile(name, file_id=urn) if urn else None)


class _DiesOnClose(FakeFusionDocument):
    """A document whose dataFile stops reading the moment it closes, beside the name that measurably
    does - so an identity read placed AFTER the close reports nothing at all."""
    def close(self, save_changes=False):
        did = super().close(save_changes)
        del self.dataFile
        return did


class _RaiseThenInvalid(FakeFusionDocument):
    """A proxy whose close() RAISES and leaves the document invalid - not a close failure, since the
    document is gone either way."""
    def close(self, save_changes=False):
        self._closed = True
        raise RuntimeError("already gone")


@pytest.fixture
def install_app(monkeypatch):
    """Point doc_close and the shared open-document resolve at ONE documents walk."""
    def _install(documents, active=None):
        app = FakeApplication(active_document=active,
                              documents=None if documents is None else FakeDocuments(documents))
        if documents is None:
            app.documents = None      # the session that answers no documents collection at all
        monkeypatch.setattr(dm, "app", app)
        monkeypatch.setattr(dk, "app", app)
        return app
    return _install


class TestCloseDocument:
    def test_close_active_document_success(self, install_app):
        d = _CloseableDoc("PartA")
        install_app([d], active=d)
        out = _payload(dm.handler())
        assert out["closed"] == ["PartA"] and out["closed_count"] == 1
        assert out["errors"] == []

    def test_close_named(self, install_app):
        a = _CloseableDoc("A")
        b = _CloseableDoc("B")
        install_app([a, b], active=a)
        out = _payload(dm.handler(name="B", save_changes=True))
        assert out["closed"] == ["B"]
        assert b._closes == [True]

    def test_close_default_discards_unsaved_changes(self, install_app):
        # The default close DISCARDS (save_changes=False on the platform call) - a silent flip to
        # save-on-close would litter the cloud with unwanted versions.
        d = _CloseableDoc("PartA")
        install_app([d], active=d)
        _payload(dm.handler())
        assert d._closes == [False]

    def test_unmatched_name_errors(self, install_app):
        install_app([_CloseableDoc("A")], active=None)
        res = dm.handler(name="Ghost")
        assert res["isError"] is True and "No open document matched" in res["message"]

    def test_close_returning_false_is_now_an_error(self, install_app):
        # A single-target close failure must surface as isError, not a false ok() success.
        d = _CloseableDoc("PartA", close_ok=False)
        install_app([d], active=d)
        res = dm.handler()
        assert res["isError"] is True
        assert "PartA" in res["message"] and "close returned false" in res["message"]

    def test_close_all_partial_failure_reports_ok_with_errors(self, install_app):
        # a MIXED result (one closed, one failed) is a partial success - report both, don't error.
        good = _CloseableDoc("Good")
        bad = _CloseableDoc("Bad", close_ok=False)
        install_app([good, bad], active=good)
        out = _payload(dm.handler(close_all=True))
        assert out["closed"] == ["Good"] and out["closed_count"] == 1
        assert out["errors"] == [{"Bad": "close returned false"}]
        assert "1 of 2" in out["note"]

    def test_no_open_documents_errors(self, install_app):
        install_app(None)
        res = dm.handler()
        assert res["isError"] is True
        assert "No documents are open" in res["message"]


class TestCloseActedOn:
    """A close is the write whose target need not be the active document, so the handler publishes
    acted_on itself: the write guard would otherwise stamp the post-call ACTIVE document, which names
    a document that was NOT closed (measured live, both when the closed doc was inactive and when it
    was the active one Fusion replaced with a fallback)."""

    def test_closing_an_inactive_doc_names_the_closed_doc(self, install_app):
        a, b = _CloseableDoc("A", urn="urn:a"), _CloseableDoc("B", urn="urn:b")
        install_app([a, b], active=a)                     # A stays open and active; B is closed
        out = _payload(dm.handler(name="B"))
        assert out["acted_on"] == {"name": "B", "document_id": "urn:b"}

    def test_closing_the_active_doc_names_the_closed_doc(self, install_app):
        a, b = _CloseableDoc("A", urn="urn:a"), _CloseableDoc("B", urn="urn:b")
        install_app([a, b], active=b)
        out = _payload(dm.handler())    # no name = the active doc
        assert out["acted_on"] == {"name": "B", "document_id": "urn:b"}

    def test_an_unsaved_doc_reports_a_null_document_id(self, install_app):
        u = _CloseableDoc("Untitled")                       # never saved - no dataFile, no URN
        install_app([u], active=u)
        out = _payload(dm.handler())
        assert out["acted_on"] == {"name": "Untitled", "document_id": None}

    def test_identity_is_captured_before_the_close(self, install_app):
        # The document is dead by the time the payload is built, so an identity read placed after
        # d.close() reports {None, None} - the capture must precede the close.
        d = _DiesOnClose(name="Scratch", data_file=FakeDataFile("Scratch", file_id="urn:scratch"))
        install_app([d], active=d)
        out = _payload(dm.handler(name="Scratch"))
        assert out["acted_on"] == {"name": "Scratch", "document_id": "urn:scratch"}

    def test_two_closed_documents_publish_an_explicit_null_acted_on(self, install_app):
        # Boundary: 2 closed. One acted_on cannot state two documents - and leaving the key ABSENT
        # hands it to the guard's fill-if-absent stamp, which reads the post-call ACTIVE document
        # (a document this call did not close). An explicit null keeps the guard off it.
        a, b = _CloseableDoc("A", urn="urn:a"), _CloseableDoc("B", urn="urn:b")
        install_app([a, b], active=a)
        out = _payload(dm.handler(close_all=True))
        assert out["closed"] == ["A", "B"]
        assert "acted_on" in out and out["acted_on"] is None

    def test_the_null_acted_on_note_points_at_the_closed_list(self, install_app):
        # A null with no pointer leaves the caller with no record of what was closed; 'closed' is it.
        a, b = _CloseableDoc("A", urn="urn:a"), _CloseableDoc("B", urn="urn:b")
        install_app([a, b], active=a)
        note = _payload(dm.handler(close_all=True))["note"]
        assert "acted_on is null" in note and "2 documents were closed" in note
        assert "'closed'" in note

    def test_three_closed_documents_are_the_same_null(self, install_app):
        # Nothing about the shape changes past the boundary - 3 is as unstatable as 2.
        docs = [_CloseableDoc(n, urn=f"urn:{n}") for n in ("A", "B", "C")]
        install_app(docs, active=docs[0])
        out = _payload(dm.handler(close_all=True))
        assert out["closed_count"] == 3 and out["acted_on"] is None

    def test_close_all_that_closes_exactly_one_still_names_it(self, install_app):
        # The other side of the same boundary: 1 closed (the second target failed), so the single
        # closed document IS statable and is named.
        good, bad = _CloseableDoc("Good", urn="urn:good"), _CloseableDoc("Bad", close_ok=False)
        install_app([good, bad], active=good)
        out = _payload(dm.handler(close_all=True))
        assert out["closed"] == ["Good"]
        assert out["acted_on"] == {"name": "Good", "document_id": "urn:good"}

    def test_a_failed_close_is_never_claimed_as_acted_on(self, install_app):
        # Boundary: 0 closed of 2 targets. A document that did NOT close was not acted on.
        bad1, bad2 = _CloseableDoc("B1", close_ok=False), _CloseableDoc("B2", close_ok=False)
        install_app([bad1, bad2], active=bad1)
        res = dm.handler(close_all=True)
        assert res["isError"] is True                       # nothing closed at all
        assert "acted_on" not in res["content"][0]["text"]

    def test_a_skipped_dead_proxy_is_not_named_as_acted_on(self, install_app):
        good = _CloseableDoc("Good", urn="urn:good")
        dead = _CloseableDoc("Dead", urn="urn:dead", dead=True)
        install_app([good, dead], active=good)
        out = _payload(dm.handler(close_all=True))
        assert out["skipped_invalid"] == 1
        assert out["acted_on"] == {"name": "Good", "document_id": "urn:good"}

    def test_every_target_skipped_publishes_an_explicit_null_acted_on(self, install_app):
        # Boundary: 0 closed with NO close failure (so the call succeeds and reaches the payload).
        # Leaving acted_on absent hands it to the guard's fill-if-absent stamp, which names the
        # still-active document - a document this call did not close.
        alive = _CloseableDoc("Alive", urn="urn:alive")
        dead = _CloseableDoc("Dead", urn="urn:dead", dead=True)
        install_app([dead], active=alive)
        out = _payload(dm.handler(close_all=True))
        assert out["closed"] == [] and out["closed_count"] == 0
        assert out["skipped_invalid"] == 1
        assert "acted_on" in out and out["acted_on"] is None
        assert dead._closes == []

    def test_the_zero_closed_note_says_so_instead_of_claiming_a_close(self, install_app):
        dead = _CloseableDoc("Dead", urn="urn:dead", dead=True)
        install_app([dead], active=_CloseableDoc("Alive", urn="urn:alive"))
        note = _payload(dm.handler(close_all=True))["note"]
        assert "No document was closed." in note
        assert "acted_on is null" in note and "none of the 1 target(s) closed" in note
        assert "discarding unsaved changes" not in note   # nothing was closed, with or without save


class _StaysValid(FakeFusionDocument):
    """A document whose close() answers true and leaves the wrapper reading isValid True - the one
    shape a close judged by its own bool reports as a closed document."""
    def close(self, save_changes=False):
        self._closes.append(bool(save_changes))
        return True


class _MuteValidity(FakeFusionDocument):
    """A closed wrapper whose isValid will not READ at all - neither closed nor open."""
    @property
    def isValid(self):
        raise RuntimeError("3 : cloud read failed")


class TestCloseReadBack:
    """close() answering true is a CLAIM; the wrapper's own isValid is the evidence."""

    def test_a_close_that_leaves_the_document_valid_is_an_error(self, install_app):
        d = _StaysValid(name="PartA")
        install_app([d], active=d)
        res = dm.handler()
        assert res["isError"] is True
        assert "PartA" in res["message"] and "isValid=true" in res["message"]

    def test_a_close_that_invalidates_the_wrapper_is_a_clean_close(self, install_app):
        d = _CloseableDoc("PartA")
        install_app([d], active=d)
        out = _payload(dm.handler())
        assert out["closed"] == ["PartA"] and out["close_unconfirmed"] == []

    def test_an_unreadable_isValid_is_unconfirmed_not_a_failure(self, install_app):
        # The flag did not answer, so this is neither a closed document nor an open one: the close
        # stands and the payload says it was not confirmed.
        d = _MuteValidity(name="Scratch")
        install_app([d], active=d)
        out = _payload(dm.handler())
        assert out["closed"] == ["Scratch"]
        assert out["close_unconfirmed"] == ["Scratch"]
        assert "UNCONFIRMED" in out["note"]

    def test_one_stuck_close_among_several_is_reported_beside_the_ones_that_took(self, install_app):
        good = _CloseableDoc("Good")
        stuck = _StaysValid(name="Stuck")
        install_app([good, stuck], active=good)
        out = _payload(dm.handler(close_all=True))
        assert out["closed"] == ["Good"]
        assert out["errors"] == [{"Stuck": dm._STILL_VALID}]


class TestCloseAllSkipsDeadProxies:
    def test_already_invalid_proxy_is_skipped_not_errored(self, install_app):
        good = _CloseableDoc("Good")
        dead = _CloseableDoc("Dead", dead=True)  # an already-invalidated reference-doc proxy
        install_app([good, dead], active=good)
        out = _payload(dm.handler(close_all=True))
        assert out["closed"] == ["Good"]
        assert out["skipped_invalid"] == 1
        assert out["errors"] == []               # a dead proxy is NOT a close failure
        assert dead._closes == []                # never even attempted
        assert "Skipped 1 already-invalidated" in out["note"]

    def test_close_that_invalidates_is_counted_skipped_not_errored(self, install_app):
        # a proxy whose close() RAISES but is invalid afterward -> counted skipped, not a hard error.
        good = _CloseableDoc("Good")
        weird = _RaiseThenInvalid(name="Weird")
        install_app([good, weird], active=good)
        out = _payload(dm.handler(close_all=True))
        assert out["closed"] == ["Good"]
        assert out["skipped_invalid"] == 1
        assert out["errors"] == []
