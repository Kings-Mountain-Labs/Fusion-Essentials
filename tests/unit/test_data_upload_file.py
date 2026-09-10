"""Unit tests for ``data_upload_file.py`` - the async start, its poll handle and the
upload-state enum mapping (0/1/2/unknown).
"""

import json
from types import SimpleNamespace

import pytest

from conftest import (FakeApplication, FakeData, FakeDataFile, FakeDataFolder, FakeDataProject,
                      load_tool)

dm = load_tool("data_upload_file")
dc = load_tool("_data_common")


def _payload(result):
    assert result["isError"] is False, result
    return json.loads(result["content"][0]["text"])


def _future(state, df=None):
    """A DataFileFuture stand-in: the uploadState the start reads, and the dataFile it carries only
    once the transfer has finished."""
    return SimpleNamespace(uploadState=state, dataFile=df)


@pytest.fixture
def cloud(monkeypatch):
    """Point the shared cloud seam at a hub holding `projects`."""
    def _use(*projects):
        monkeypatch.setattr(dc, "app", FakeApplication(data=FakeData(projects=list(projects))))
    return _use


def _at(project, path):
    """The folder at `path` under the project root - the one whose _uploads says which folder
    actually received the file."""
    folder = project.rootFolder
    for segment in path.split("/"):
        folder = folder.dataFolders.itemByName(segment)
    return folder


class TestUploadFile:
    def _project(self, future=None, upload_raises=None):
        """Proj with an Imports/STEP path, every folder in it - and every folder a create_path
        mkdir -p adds under the root - handing back the same upload outcome, so the payload's
        destination is never evidence of where the file went: each folder's own _uploads is."""
        knobs = {"upload_future": future, "upload_raises": upload_raises}
        step = FakeDataFolder("STEP", **knobs)
        imports = FakeDataFolder("Imports", folders=[step], **knobs)
        root = FakeDataFolder("Root", folders=[imports], is_root=True, **knobs)
        return FakeDataProject("Proj", project_id="pid", root_folder=root)

    def _landed(self, name="p.step", file_id="urn:1"):
        return _future(1, df=FakeDataFile(name, file_id=file_id))

    def test_file_not_found_errors(self, cloud, tmp_path):
        cloud()
        res = dm.handler(file_path=str(tmp_path / "nope.step"), project="Proj")
        assert res["isError"] is True and "not found" in res["message"].lower()

    def test_requires_project(self, cloud, tmp_path):
        f = tmp_path / "p.step"
        f.write_text("x")
        cloud()
        res = dm.handler(file_path=str(f))
        assert res["isError"] is True and "project" in res["message"]

    def test_upload_state_finished_maps_to_word(self, cloud, tmp_path):
        proj = self._project(self._landed())
        cloud(proj)
        f = tmp_path / "p.step"
        f.write_text("x")
        out = _payload(dm.handler(file_path=str(f), project="Proj"))
        assert out["upload_state"] == "finished"     # 1 -> finished
        assert out["uploaded_name"] == "p.step"
        assert out["uploaded_id"] == "urn:1"
        assert out["destination_folder"] == "(project root)"
        assert proj.rootFolder._uploads == [str(f)]   # no folder given -> the root got it

    def test_upload_state_processing_and_unknown(self, cloud, tmp_path):
        f = tmp_path / "p.step"
        f.write_text("x")
        cloud(self._project(_future(0)))
        out = _payload(dm.handler(file_path=str(f), project="Proj"))
        assert out["upload_state"] == "processing"   # 0 -> processing
        # an unmapped state value falls back to str(state)
        cloud(self._project(_future(99)))
        out2 = _payload(dm.handler(file_path=str(f), project="Proj"))
        assert out2["upload_state"] == "99"

    def test_upload_state_failed_is_an_error_not_ok(self, cloud, tmp_path):
        # uploadState 2 = failed - the same terminal state the data_get_upload_status poller
        # reports as FAILED. The upload tool must refuse with isError naming the file, never
        # return ok with upload_state 'failed'.
        f = tmp_path / "p.step"
        f.write_text("x")
        cloud(self._project(_future(2)))
        res = dm.handler(file_path=str(f), project="Proj")
        assert res["isError"] is True
        assert "FAILED" in res["message"] and "p.step" in res["message"]

    def test_existing_nested_folder_target(self, cloud, tmp_path):
        f = tmp_path / "p.step"
        f.write_text("x")
        proj = self._project(self._landed())
        cloud(proj)
        out = _payload(dm.handler(
            file_path=str(f), project="Proj", folder="Imports/STEP"))
        assert out["destination_folder"] == "Imports/STEP"
        assert out["auto_created_parents"] == []
        # The published destination is a CLAIM. Every folder here answers the same future, so only
        # the folder that was handed the path shows the upload - the read that separates a file
        # landing in 'Imports/STEP' from one landing in the root under that label.
        assert _at(proj, "Imports/STEP")._uploads == [str(f)]
        assert proj.rootFolder._uploads == []

    def test_missing_folder_without_create_path_errors(self, cloud, tmp_path):
        f = tmp_path / "p.step"
        f.write_text("x")
        cloud(self._project())
        res = dm.handler(file_path=str(f), project="Proj", folder="Imports/Ghost")
        assert res["isError"] is True
        assert "not found" in res["message"] and "Ghost" in res["message"]
        # hint names the folders that DO exist at that level
        assert "STEP" in res["message"]

    def test_create_path_makes_missing_folders(self, cloud, tmp_path):
        f = tmp_path / "p.step"
        f.write_text("x")
        proj = self._project(self._landed())
        cloud(proj)
        out = _payload(dm.handler(
            file_path=str(f), project="Proj", folder="New/Deep", create_path=True))
        assert out["auto_created_parents"] == ["New", "Deep"]
        assert out["destination_folder"] == "New/Deep"
        # the mkdir -p made the destination, so the upload has to have gone into the DEEPEST
        # folder it created, not the root the path was walked from.
        assert _at(proj, "New/Deep")._uploads == [str(f)]
        assert proj.rootFolder._uploads == []

    def test_an_upload_that_will_not_start_names_the_folders_create_path_left(self, cloud,
                                                                              tmp_path):
        f = tmp_path / "p.step"
        f.write_text("x")
        cloud(self._project(upload_raises="3 : upload rejected"))
        res = dm.handler(file_path=str(f), project="Proj", folder="New/Deep", create_path=True)
        assert res["isError"] is True
        assert "'New'" in res["message"] and "'Deep'" in res["message"]
        assert "NOT removed" in res["message"]

    def test_an_immediately_failed_upload_also_names_the_retained_folders(self, cloud, tmp_path):
        # The upload is refused, but the destination path it created for that upload stays.
        f = tmp_path / "p.step"
        f.write_text("x")
        cloud(self._project(_future(2)))
        res = dm.handler(file_path=str(f), project="Proj", folder="New/Deep", create_path=True)
        assert res["isError"] is True and "FAILED" in res["message"]
        assert "'New'" in res["message"] and "'Deep'" in res["message"]

    def test_the_start_names_the_poller_and_claims_no_completion(self, cloud, tmp_path):
        # The upload LANDS asynchronously on the cloud, so this call claims only that it started and
        # hands back the handle data_get_upload_status polls - the payload confirms no completion of
        # its own, and the future it registers is what the poller reads the real state off.
        f = tmp_path / "p.step"
        f.write_text("x")
        cloud(self._project(_future(0)))          # still transferring: no DataFile exists yet
        out = _payload(dm.handler(file_path=str(f), project="Proj"))
        assert out["upload_started"] is True
        assert out["uploaded_id"] is None and out["uploaded_name"] is None
        assert out["upload_handle"] in dm._UPLOADS
        assert "data_get_upload_status" in out["note"] and "upload_handle" in out["note"]

    def test_a_failed_upload_into_an_existing_folder_claims_no_retained_folders(self, cloud,
                                                                                tmp_path):
        # The boundary: create_path made nothing, so there is no partial success to disclose.
        f = tmp_path / "p.step"
        f.write_text("x")
        cloud(self._project(_future(2)))
        res = dm.handler(file_path=str(f), project="Proj", folder="Imports/STEP")
        assert res["isError"] is True
        assert "NOT removed" not in res["message"]
