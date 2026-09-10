"""Unit tests for ``doc_activate.py`` - bringing an open document to the foreground.

Pinned: the async switch reports the VERIFIED state ('pending' while the foreground has
not caught up), and a name several open documents share is REFUSED, not guessed.
"""

import json

import pytest

from conftest import FakeApplication, FakeDataFile, FakeDocuments, FakeFusionDocument, load_tool

dm = load_tool("doc_activate")
dc = load_tool("_doc_common")   # the resolve reads app.documents through this seam


def _payload(result):
    assert result["isError"] is False, result
    return json.loads(result["content"][0]["text"])


def _doc(name, urn=None):
    """One open document. A lineage URN identifies it unambiguously when names collide; None is the
    unsaved document, which carries no dataFile at all."""
    return FakeFusionDocument(name=name, is_saved=True, is_modified=False, is_active=False,
                              data_file=FakeDataFile(name, file_id=urn) if urn else None)


@pytest.fixture
def install_app(monkeypatch):
    """Point doc_activate and the shared open-document resolve at ONE app.documents walk."""
    def _install(documents, active=None):
        app = FakeApplication(active_document=active, documents=FakeDocuments(documents))
        monkeypatch.setattr(dm, "app", app)
        monkeypatch.setattr(dc, "app", app)
        return app
    return _install


class TestActivateDocument:
    def test_activate_taken_reports_true(self, install_app):
        # the switch propagated (active doc is now B) -> activated:true, is_active:true, no pending note.
        a, b = _doc("A"), _doc("B")
        app = install_app([a, b], active=a)
        app.activeDocument = b                 # model the switch having taken
        out = _payload(dm.handler(name="B"))
        assert b._activates == 1               # the .activate() call was issued
        assert out["activated"] is True and out["is_active"] is True
        assert out["document_name"] == "B" and "note" not in out

    def test_activate_async_pending_reports_pending_not_true(self, install_app):
        # the switch was ACCEPTED but the active doc hasn't propagated yet (real async behavior,
        # observed live). Must report 'pending', NOT a false 'true'.
        a, b = _doc("A"), _doc("B")
        install_app([a, b], active=a)          # active stays A after activate() -> not propagated
        out = _payload(dm.handler(name="B"))
        assert out["activated"] == "pending"   # honest: not done yet
        assert out["is_active"] is False
        assert "async" in out["note"] and "doc_get" in out["note"]

    def test_requires_name(self, install_app):
        install_app([_doc("A")])
        res = dm.handler()
        assert res["isError"] is True and "Provide 'name'" in res["message"]

    def test_unmatched_errors(self, install_app):
        install_app([_doc("A")])
        res = dm.handler(name="Ghost")
        assert res["isError"] is True and "No open document matched" in res["message"]

    def test_a_lineage_urn_activates_the_twin_a_bare_name_cannot(self, install_app):
        # The URN is accepted wherever the display NAME is, and it resolves EXACTLY: two open docs
        # answer to 'P1-Gimbal', so only the URN can say which one to bring forward.
        a = _doc("P1-Gimbal", urn="urn:adsk.wipprod:dm.lineage:AAA")
        b = _doc("P1-Gimbal", urn="urn:adsk.wipprod:dm.lineage:BBB")
        app = install_app([a, b], active=a)
        app.activeDocument = b                     # model the switch having taken
        out = _payload(dm.handler(name="urn:adsk.wipprod:dm.lineage:BBB"))
        assert b._activates == 1 and a._activates == 0
        assert out["is_active"] is True

    def test_the_bare_name_refusal_hands_back_both_lineage_urns(self, install_app):
        # The same pair by NAME: refused, and the refusal carries the two URNs the retry needs -
        # naming only the shared display name would ask for the value that just failed.
        a = _doc("P1-Gimbal", urn="urn:adsk.wipprod:dm.lineage:AAA")
        b = _doc("P1-Gimbal", urn="urn:adsk.wipprod:dm.lineage:BBB")
        install_app([a, b], active=a)
        res = dm.handler(name="P1-Gimbal")
        assert res["isError"] is True
        assert "P1-Gimbal (urn:adsk.wipprod:dm.lineage:AAA)" in res["message"]
        assert "P1-Gimbal (urn:adsk.wipprod:dm.lineage:BBB)" in res["message"]
        assert a._activates == 0 and b._activates == 0


def test_pending_activation_does_not_stamp_the_previous_document(install_app):
    a, b = _doc("A"), _doc("B")
    install_app([a, b], active=a)
    out = _payload(dc._write_guard.wrap(dm.handler)(name="B"))
    assert out["activated"] == "pending" and out["acted_on"] is None
