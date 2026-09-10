"""Unit tests for ``data_delete_file.py`` - the guarded cloud-file delete.

Pinned, no live Fusion: the case-sensitive confirm_name gate, the open-document refusal,
and the FAIL-CLOSED behaviour when the reference read will not answer (an empty list is
not evidence the file is unreferenced).
"""

import json
from types import SimpleNamespace

import pytest

from conftest import (FakeApplication, FakeData, FakeDataFile, FakeDocuments, FakeFusionDocument,
                      load_tool)

dm = load_tool("data_delete_file")
dc = load_tool("_data_common")


def _payload(result):
    assert result["isError"] is False, result
    return json.loads(result["content"][0]["text"])


def _ref(name, urn):
    """One DataFileReference row the orphan guard names in its refusal."""
    return SimpleNamespace(name=name, id=urn)


@pytest.fixture
def cloud(monkeypatch):
    """Point both seams the delete reads through - its own app for the open-document walk and the
    _data_common one findFileById comes off - at a hub holding `by_id`, with `open_urns` open."""
    def _use(by_id=None, open_urns=()):
        docs = [FakeFusionDocument(data_file=FakeDataFile(file_id=urn)) for urn in open_urns]
        app = FakeApplication(data=FakeData(files_by_id=by_id or {}),
                              documents=FakeDocuments(docs))
        monkeypatch.setattr(dc, "app", app)
        monkeypatch.setattr(dm, "app", app)
        return app
    return _use


# ─────────────────────────────────────────────────────────────────────────────
# delete_document_handler  (DataFile.deleteMe - guarded, irreversible)
# ─────────────────────────────────────────────────────────────────────────────

class TestDeleteDocument:
    def test_requires_document_id(self, cloud):
        cloud()
        res = dm.handler(confirm_name="X")
        assert res["isError"] is True and "document_id" in res["message"]

    def test_requires_confirm_name(self, cloud):
        cloud()
        res = dm.handler(document_id="urn:x")
        assert res["isError"] is True and "confirm_name" in res["message"]

    def test_unknown_file_errors(self, cloud):
        cloud()
        res = dm.handler(document_id="urn:missing", confirm_name="X")
        assert res["isError"] is True and "No file found" in res["message"]

    def test_name_mismatch_refuses(self, cloud):
        f = FakeDataFile("RealName", file_id="urn:f")
        cloud({"urn:f": f})
        res = dm.handler(document_id="urn:f", confirm_name="WrongName")
        assert res["isError"] is True
        assert "Name mismatch" in res["message"]
        assert "RealName" in res["message"]
        assert f._deleted is False

    def test_a_case_mismatched_confirm_is_refused(self, cloud):
        # the confirmation gate is case-SENSITIVE by its own comment - a destructive delete demands
        # the exact name, so 'realname' is a mismatch, never a match that happens to read well.
        f = FakeDataFile("RealName", file_id="urn:f")
        cloud({"urn:f": f})
        res = dm.handler(document_id="urn:f", confirm_name="realname")
        assert res["isError"] is True
        assert "Name mismatch" in res["message"]
        assert f._deleted is False

    def test_open_file_refused(self, cloud):
        f = FakeDataFile("PartA", file_id="urn:f")
        cloud({"urn:f": f}, open_urns=["urn:f"])
        res = dm.handler(document_id="urn:f", confirm_name="PartA")
        assert res["isError"] is True and "OPEN" in res["message"]
        assert f._deleted is False

    def test_referenced_file_refused_without_force(self, cloud):
        f = FakeDataFile("PartA", file_id="urn:f", parent_refs=[_ref("Asm1", "urn:a")])
        cloud({"urn:f": f})
        res = dm.handler(document_id="urn:f", confirm_name="PartA")
        assert res["isError"] is True
        assert "referenced by" in res["message"] and "Asm1" in res["message"]
        assert f._deleted is False

    def test_referenced_file_deleted_with_force(self, cloud):
        f = FakeDataFile("PartA", file_id="urn:f", parent_refs=[_ref("Asm1", "urn:a")])
        cloud({"urn:f": f})
        out = _payload(dm.handler(document_id="urn:f", confirm_name="PartA", force=True))
        assert out["deleted"] is True
        assert out["forced"] is True
        assert f._deleted is True
        assert [p["name"] for p in out["was_referenced_by"]] == ["Asm1"]

    def test_unreferenced_file_deleted(self, cloud):
        f = FakeDataFile("PartA", file_id="urn:f")
        cloud({"urn:f": f})
        out = _payload(dm.handler(document_id="urn:f", confirm_name="PartA"))
        assert out["deleted"] is True and out["forced"] is False
        assert f._deleted is True
        # the read ANSWERED and the answer was none: [] here means unreferenced, and only here.
        assert out["was_referenced_by"] == []
        assert "reference_state_unreadable" not in out

    def test_force_on_a_clean_file_publishes_forced_false(self, cloud):
        # 'forced' reports whether a GUARD was overridden, not which flag the call carried: this
        # file's reference read answered and answered none, so force overrode nothing.
        f = FakeDataFile("PartA", file_id="urn:f")
        cloud({"urn:f": f})
        out = _payload(dm.handler(document_id="urn:f", confirm_name="PartA", force=True))
        assert out["deleted"] is True and f._deleted is True
        assert out["forced"] is False
        assert out["was_referenced_by"] == []

    def test_confirm_name_whitespace_forgiven(self, cloud):
        f = FakeDataFile("PartA", file_id="urn:f")
        cloud({"urn:f": f})
        out = _payload(dm.handler(document_id="urn:f", confirm_name="  PartA  "))
        assert out["deleted"] is True

    def test_delete_me_false_reported(self, cloud):
        f = FakeDataFile("PartA", file_id="urn:f", delete_ok=False)
        cloud({"urn:f": f})
        res = dm.handler(document_id="urn:f", confirm_name="PartA")
        assert res["isError"] is True and "declined to delete" in res["message"]


class TestDeleteFailsClosedOnUnreadableReferences:
    """The orphan guard is only as good as the read behind it. When the reference read does not
    answer, the file is NOT provably unreferenced - so the destructive path is REFUSED (the
    unreadable-census shape data_delete_folder uses), and a forced delete publishes null rather than
    an empty list that reads as 'nothing pointed at it'."""

    def test_an_unreadable_flag_refuses_the_delete(self, cloud):
        f = FakeDataFile("PartA", file_id="urn:f", parent_refs_raise="flag")
        cloud({"urn:f": f})
        res = dm.handler(document_id="urn:f", confirm_name="PartA")
        assert res["isError"] is True
        assert "hasParentReferences" in res["message"]     # names WHICH read failed
        assert "force=true" in res["message"]
        assert f._deleted is False                         # deleteMe() was never reached

    def test_an_unreadable_reference_list_refuses_the_delete(self, cloud):
        f = FakeDataFile("PartA", file_id="urn:f", parent_refs_raise="array")
        cloud({"urn:f": f})
        res = dm.handler(document_id="urn:f", confirm_name="PartA")
        assert res["isError"] is True
        assert "parentReferences.asArray()" in res["message"]
        assert f._deleted is False

    def test_force_deletes_and_publishes_null_not_an_empty_list(self, cloud):
        f = FakeDataFile("PartA", file_id="urn:f", parent_refs_raise="array")
        cloud({"urn:f": f})
        out = _payload(dm.handler(document_id="urn:f", confirm_name="PartA", force=True))
        assert out["deleted"] is True and f._deleted is True
        assert out["was_referenced_by"] is None            # NOT [] - the read never answered
        assert out["forced"] is True
        assert out["reference_state_unreadable"] == "parentReferences.asArray()"
        assert "unknown" in out["note"]
