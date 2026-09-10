"""Unit tests for ``data_get_upload_status.py`` - the poller that reports a data_upload_file upload's
real state instead of the caller re-listing files and guessing when cloud translation finished.

This poller reads the future kept alive in _data_common._UPLOADS and reports state honestly:
'uploading' (bytes still transferring - future.dataFile is None), 'processing' (transfer finished but
DataFile.isComplete is still False - cloud translation ongoing), 'complete' (DataFile.isComplete is
True - the cloud confirms the file has landed), or 'failed'. It must NEVER sleep/pump - it reports the
CURRENT state and returns.
"""

import json
from types import SimpleNamespace

from conftest import FakeDataFile, load_tool

dc = load_tool("_data_common")
sut = load_tool("data_get_upload_status")


def _payload(result):
    assert result["isError"] is False, result
    return json.loads(result["content"][0]["text"])


def _future(upload_state, df=None):
    """A DataFileFuture stand-in: the uploadState the poll reads and the dataFile it carries only
    once the transfer has finished."""
    return SimpleNamespace(uploadState=upload_state, dataFile=df)


def _datafile(name="p.step", fid="urn:file:1", version_id="urn:file:1?version=2",
              version_number=2, web_url="https://a360.co/x", is_complete=True):
    return FakeDataFile(name, file_id=fid, version_id=version_id, version=version_number,
                        web_url=web_url, is_complete=is_complete)


def _entry(future, source_file="p.step", project="Proj", folder="Imports/STEP", started_at=0.0):
    return {"future": future, "source_file": source_file, "destination_project": project,
            "destination_folder": folder, "started_at": started_at}


class TestGuards:
    def setup_method(self):
        dc._UPLOADS.clear()
        dc._UPLOAD_HANDLE_SEQ[0] = 0

    def test_no_uploads_registered_errors(self):
        res = sut.handler()
        assert res["isError"] is True and "No uploads" in res["message"]

    def test_unknown_handle_lists_active_handles(self):
        dc._UPLOADS["up1"] = _entry(_future(0))
        res = sut.handler(handle="up99")
        assert res["isError"] is True and "up1" in res["message"]

    def test_file_name_no_match_errors_listing_handles(self):
        dc._UPLOADS["up1"] = _entry(_future(0), source_file="other.step")
        res = sut.handler(file_name="p.step")
        assert res["isError"] is True
        assert "p.step" in res["message"] and "up1" in res["message"]


class TestStateReporting:
    def setup_method(self):
        dc._UPLOADS.clear()
        dc._UPLOAD_HANDLE_SEQ[0] = 0

    def test_uploading_state_when_transfer_still_in_progress(self):
        # uploadState=0 (UploadProcessing) and no dataFile yet -> bytes are still transferring.
        dc._UPLOADS["up1"] = _entry(_future(0, df=None))
        out = _payload(sut.handler(handle="up1"))
        assert out["state"] == "uploading"
        assert "up1" in dc._UPLOADS          # not terminal - stays tracked

    def test_processing_state_when_transfer_done_but_cloud_still_working(self):
        # uploadState=1 (UploadFinished) but DataFile.isComplete is False -> transfer landed, cloud
        # translation (e.g. STEP -> Fusion design) is still running.
        df = _datafile(is_complete=False)
        dc._UPLOADS["up1"] = _entry(_future(1, df=df))
        out = _payload(sut.handler(handle="up1"))
        assert out["state"] == "processing"
        assert "up1" in dc._UPLOADS          # not terminal yet - stays tracked
        assert "file_id" not in out                 # honesty: no version info until truly complete

    def test_complete_state_reports_landed_version_and_pops_entry(self):
        df = _datafile(fid="urn:file:1", version_id="urn:file:1?version=3", version_number=3,
                       is_complete=True)
        dc._UPLOADS["up1"] = _entry(_future(1, df=df))
        out = _payload(sut.handler(handle="up1"))
        assert out["state"] == "complete"
        assert out["file_id"] == "urn:file:1"
        assert out["version_id"] == "urn:file:1?version=3"
        assert out["version_number"] == 3
        assert out["fusion_web_url"] == "https://a360.co/x"
        assert "up1" not in dc._UPLOADS       # terminal - drop so it doesn't leak forever

    def test_failed_state_reported_and_pops_entry(self):
        dc._UPLOADS["up1"] = _entry(_future(2))
        out = _payload(sut.handler(handle="up1"))
        assert out["state"] == "failed"
        assert "up1" not in dc._UPLOADS

    def test_latest_resolves_to_most_recent_handle(self):
        dc._UPLOADS["up1"] = _entry(_future(0), source_file="a.step")
        dc._UPLOADS["up2"] = _entry(_future(0), source_file="b.step")
        dc._UPLOAD_HANDLE_SEQ[0] = 2
        out = _payload(sut.handler(handle="latest"))
        assert out["handle"] == "up2" and out["source_file"] == "b.step"

    def test_latest_names_the_newest_STILL_TRACKED_upload(self):
        # up2 was minted, reported complete and popped, while up1 is still transferring. 'latest'
        # read off the mint counter names up2 - a handle nothing holds - and errors instead of
        # reporting the upload that IS running. The tracked dict is the source of truth.
        dc._UPLOADS["up1"] = _entry(_future(0), source_file="a.step")
        dc._UPLOAD_HANDLE_SEQ[0] = 2
        out = _payload(sut.handler(handle="latest"))
        assert out["handle"] == "up1" and out["source_file"] == "a.step"

    def test_a_bare_call_also_reports_the_newest_tracked_upload(self):
        # the no-arguments route takes the same 'latest' branch - the counter is not its truth either
        dc._UPLOADS["up1"] = _entry(_future(0), source_file="a.step")
        dc._UPLOADS["up2"] = _entry(_future(0), source_file="b.step")
        dc._UPLOAD_HANDLE_SEQ[0] = 7
        out = _payload(sut.handler())
        assert out["handle"] == "up2" and out["source_file"] == "b.step"

    def test_file_name_lookup_finds_most_recent_match_and_folder_disambiguates(self):
        # two uploads of the same filename to different folders - a bare file_name lookup picks the
        # MOST RECENT; adding 'folder' disambiguates to the earlier one.
        dc._UPLOADS["up1"] = _entry(_future(0), source_file="p.step", folder="Imports/OLD")
        dc._UPLOADS["up2"] = _entry(_future(0), source_file="p.step", folder="Imports/NEW")
        out = _payload(sut.handler(file_name="p.step"))
        assert out["handle"] == "up2"               # most recent wins with no folder given
        out2 = _payload(sut.handler(file_name="p.step", folder="Imports/OLD"))
        assert out2["handle"] == "up1"               # folder narrows to the earlier upload

    def test_never_blocks_returns_immediately_without_pumping(self, monkeypatch):
        # NON-BLOCKING house rule: a poll of a still-processing upload must return promptly - no
        # sleep, no event-loop pump. Fail loudly if the handler ever tries to sleep.
        import time as time_mod
        def _no_sleep(*a, **kw):
            raise AssertionError("data_get_upload_status must never sleep")
        monkeypatch.setattr(time_mod, "sleep", _no_sleep)
        df = _datafile(is_complete=False)
        dc._UPLOADS["up1"] = _entry(_future(1, df=df))
        out = _payload(sut.handler(handle="up1"))     # must return without ever calling time.sleep
        assert out["state"] == "processing"
