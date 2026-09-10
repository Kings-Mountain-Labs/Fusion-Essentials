"""Unit tests for ``data_create_folder.py`` - mkdir -p, its duplicate guard, and the
disclosure of parent folders a failed call already created.
"""

import json

import pytest

from conftest import FakeApplication, FakeData, FakeDataFolder, load_tool, make_data_tree

dm = load_tool("data_create_folder")
dc = load_tool("_data_common")


def _payload(result):
    assert result["isError"] is False, result
    return json.loads(result["content"][0]["text"])


@pytest.fixture
def cloud(monkeypatch):
    """Point the shared cloud seam at a hub holding `projects`."""
    def _use(*projects):
        monkeypatch.setattr(dc, "app", FakeApplication(data=FakeData(projects=list(projects))))
    return _use


def _refuse_child(monkeypatch, name):
    """Make folder creation fail for ONE named child - the cloud declining a create partway through
    a mkdir -p, which is what leaves earlier segments behind."""
    original = FakeDataFolder._add_folder

    def guarded(self, child_name):
        if child_name == name:
            raise RuntimeError("3 : folder creation refused")
        return original(self, child_name)

    monkeypatch.setattr(FakeDataFolder, "_add_folder", guarded)


class TestCreateFolder:
    def _proj(self):
        project = make_data_tree(name="Proj")
        return project, project.rootFolder

    def test_creates_at_root(self, cloud):
        proj, root = self._proj()
        cloud(proj)
        out = _payload(dm.handler(folder_name="Parts", project="Proj"))
        assert out["created"] is True and out["name"] == "Parts"
        assert out["auto_created_parents"] == []
        assert [c.name for c in root._folders] == ["Parts"]

    def test_mkdir_p_reports_auto_created_parents(self, cloud):
        proj, _root = self._proj()
        cloud(proj)
        out = _payload(dm.handler(
            folder_name="Vises", project="Proj", parent_folder="Fixtures/Mills"))
        # both intermediate parents were created
        assert out["auto_created_parents"] == ["Fixtures", "Mills"]
        assert out["path"] == "Fixtures/Mills/Vises"

    def test_duplicate_in_same_parent_refused(self, cloud):
        proj, root = self._proj()
        root.dataFolders.add("Parts")
        cloud(proj)
        res = dm.handler(folder_name="parts", project="Proj")  # case-insensitive dup
        assert res["isError"] is True and "already exists" in res["message"]

    def test_missing_project_lists_available(self, cloud):
        proj, _root = self._proj()
        cloud(proj)
        res = dm.handler(folder_name="X", project="Ghost")
        assert res["isError"] is True
        assert "Ghost" in res["message"] and "Proj" in res["message"]

    def test_requires_project_identifier(self, cloud):
        cloud()
        res = dm.handler(folder_name="X")
        assert res["isError"] is True and "project" in res["message"]

    def test_a_failure_after_mkdir_p_names_the_parents_it_left_behind(self, cloud, monkeypatch):
        # auto_created_parents only ships on the ok path, so an error is the ONLY place a caller
        # hears that this call already made two folders it will not be cleaning up.
        proj, _root = self._proj()
        cloud(proj)
        _refuse_child(monkeypatch, "Vises")
        res = dm.handler(folder_name="Vises", project="Proj", parent_folder="Fixtures/Mills")
        assert res["isError"] is True
        assert "'Fixtures'" in res["message"] and "'Mills'" in res["message"]
        assert "NOT removed" in res["message"]

    def test_a_mkdir_p_that_raises_partway_names_only_what_it_had_created(self, cloud, monkeypatch):
        # The raise loses the returned list, so the created names have to have been recorded as
        # they were made - and a segment that never got created must not be claimed as retained.
        proj, _root = self._proj()
        cloud(proj)
        _refuse_child(monkeypatch, "Mills")
        res = dm.handler(folder_name="Vises", project="Proj", parent_folder="Fixtures/Mills")
        assert res["isError"] is True
        assert "'Fixtures'" in res["message"] and "NOT removed" in res["message"]
        assert "'Mills'" not in res["message"]

    def test_a_failure_that_created_nothing_claims_no_partial_success(self, cloud, monkeypatch):
        # The boundary: no parent path, so nothing was auto-created and the error must not invent
        # folders for the caller to go hunting.
        proj, _root = self._proj()
        cloud(proj)
        _refuse_child(monkeypatch, "Parts")
        res = dm.handler(folder_name="Parts", project="Proj")
        assert res["isError"] is True
        assert "NOT removed" not in res["message"]

    def test_a_folder_that_never_relists_is_an_error(self, cloud, monkeypatch):
        # dataFolders.add() answering with a folder object is not the folder existing. The parent is
        # re-listed after the add, and a folder missing from that listing is an error, not created:true.
        proj, root = self._proj()
        cloud(proj)

        def ghost(self, child_name):
            return FakeDataFolder(child_name, parent_folder=self)   # never joins the parent's walk

        monkeypatch.setattr(FakeDataFolder, "_add_folder", ghost)
        res = dm.handler(folder_name="Parts", project="Proj")
        assert res["isError"] is True
        assert "re-listed" in res["message"] and "did not land" in res["message"]
        assert [c.name for c in root._folders] == []
