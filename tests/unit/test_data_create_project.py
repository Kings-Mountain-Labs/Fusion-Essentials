"""Unit tests for ``data_create_project.py`` - the duplicate-name guard and the re-list."""

import json

import pytest

from conftest import FakeApplication, FakeData, FakeDataProject, load_tool

dm = load_tool("data_create_project")
dc = load_tool("_data_common")


def _payload(result):
    assert result["isError"] is False, result
    return json.loads(result["content"][0]["text"])


@pytest.fixture
def cloud(monkeypatch):
    """Point the shared cloud seam at a hub holding `projects`, and hand back its app.data."""
    def _use(*projects):
        data = FakeData(projects=list(projects))
        monkeypatch.setattr(dc, "app", FakeApplication(data=data))
        return data
    return _use


class TestCreateProject:
    def test_creates_and_reports_id(self, cloud):
        data = cloud()
        out = _payload(dm.handler(name="Alpha", purpose="testing"))
        assert out["created"] is True
        assert out["name"] == "Alpha"
        assert out["id"] == "newid:Alpha"
        assert data.dataProjects._added == [("Alpha", "testing", "")]

    def test_blank_name_errors(self, cloud):
        cloud()
        res = dm.handler(name="   ")
        assert res["isError"] is True and "name" in res["message"]

    def test_duplicate_name_refused(self, cloud):
        data = cloud(FakeDataProject("Alpha", project_id="p1"))
        res = dm.handler(name="alpha")   # case-insensitive duplicate
        assert res["isError"] is True
        assert "already exists" in res["message"]
        assert data.dataProjects._added == []            # nothing created

    def test_a_project_that_never_relists_is_an_error(self, cloud, monkeypatch):
        # add() handing back a project object is not the project existing. The re-list is the
        # verification: a hub that does not carry the name afterwards is an error, never created:true.
        data = cloud()

        def ghost(name, purpose="", contributors=""):
            return FakeDataProject(name, project_id="newid:" + name)   # never joins the walk

        monkeypatch.setattr(data.dataProjects, "add", ghost)
        res = dm.handler(name="Alpha")
        assert res["isError"] is True
        assert "re-listed" in res["message"] and "did not land" in res["message"]
