"""Unit tests for ``doc_new.py`` - the new blank design document.

Pinned: the active flag comes from wrapper EQUALITY (identity is measured unreliable on
live Documents), so a DISTINCT wrapper that compares equal still reads active.
"""

import json

import pytest

from conftest import FakeApplication, FakeDocuments, FakeFusionDocument, load_tool

dm = load_tool("doc_new")


def _payload(result):
    assert result["isError"] is False, result
    return json.loads(result["content"][0]["text"])


class _EqualByHandle(FakeFusionDocument):
    """A document wrapper that compares by HANDLE rather than by identity - measured, two reads of
    ONE live document answer distinct wrappers that compare equal."""
    def __init__(self, handle="h-new", **kw):
        super().__init__(**kw)
        self._handle = handle

    def __eq__(self, other):
        return getattr(other, "_handle", None) == self._handle

    __hash__ = None


class _AddsNothing(FakeDocuments):
    """documents.add answering None - the create that reports a document it never made."""
    def add(self, document_type):
        return None


@pytest.fixture
def install_app(monkeypatch):
    """Point doc_new at a documents walk whose add() hands back `made`, beside `active`."""
    def _install(documents, active=None):
        app = FakeApplication(active_document=active, documents=documents)
        monkeypatch.setattr(dm, "app", app)
        return app
    return _install


class TestNewDocument:
    def test_creates_and_reports_active(self, install_app):
        # The active flag comes from wrapper EQUALITY (identity is measured unreliable on live
        # Documents), so the rig models the platform: add() activates, and the active read hands
        # back a DISTINCT wrapper that compares equal by handle.
        made = _EqualByHandle(name="Untitled")
        install_app(FakeDocuments(new_document=made), active=_EqualByHandle(name="Untitled"))
        out = _payload(dm.handler())
        assert out["created"] is True
        assert out["document_name"] == "Untitled"
        assert out["is_active"] is True
        assert out["is_saved"] is False

    def test_a_different_active_document_reads_inactive(self, install_app):
        made = _EqualByHandle("h-new", name="Untitled")
        install_app(FakeDocuments(new_document=made),
                    active=_EqualByHandle("h-other", name="Untitled"))
        out = _payload(dm.handler())
        assert out["created"] is True and out["is_active"] is False
        assert dm._write_guard.resolve_document_handle(out["document_handle"]) == made

    def test_add_returning_nothing_is_an_error(self, install_app):
        install_app(_AddsNothing())
        res = dm.handler()
        assert res["isError"] is True and "returned nothing" in res["message"]


def test_wrapped_creation_receipt_names_created_document_even_when_another_stays_active(install_app):
    made = _EqualByHandle("created", name="New document")
    other = _EqualByHandle("other", name="Other document")
    install_app(FakeDocuments(new_document=made), active=other)
    out = _payload(dm._write_guard.wrap(dm.handler)())
    assert out["is_active"] is False
    assert out["acted_on"]["name"] == "New document"
    assert dm._write_guard.resolve_document_handle(out["acted_on"]["document_handle"]) == made
