"""Unit tests for ``doc_save_as.py`` - Document.saveAs into a project/folder.

Pinned, no live Fusion: the same-name refusal (saveAs on a colliding name FORKS a new
lineage), the recovery read-back when saveAs raises or returns false AFTER the file
landed, the lineage-URN pump, and the eventual-consistency folder-resolve retry.
"""

import json
import time

import pytest

from conftest import (FakeApplication, FakeData, FakeDataFile, FakeDataFolder, FakeDataProject,
                      FakeFusionDocument, _CloudArray, load_tool)

dm = load_tool("doc_save_as")
dc = load_tool("_data_common")
dcopy = load_tool("doc_copy")   # the same-name rule holds for both writers


def _payload(result):
    assert result["isError"] is False, result
    return json.loads(result["content"][0]["text"])


def FakeFile(name, fid="urn:adsk.file:src"):
    """One cloud file at a destination, addressed by the lineage id that tells same-name twins
    apart."""
    return FakeDataFile(name, file_id=fid)


class _LateUrnFile(FakeDataFile):
    """A DataFile whose id reads the LOCAL pre-upload path for its first `local_reads` reads and the
    lineage urn afterwards - the cloud save settling part-way through the wait, which is the only
    shape where the recovery read spends real seconds AND comes back with an address."""

    def __init__(self, name, urn, local_reads):
        super().__init__(name, file_id="C:/tmp/local-handle")
        self._urn, self._left = urn, local_reads

    @property
    def id(self):
        if self._left > 0:
            self._left -= 1
            return "C:/tmp/local-handle"
        return self._urn

    @id.setter
    def id(self, value):
        pass                     # the settling schedule above is what this file's id reports


class _BlindNameFile(FakeDataFile):
    @property
    def name(self):
        raise RuntimeError("3 : file name unreadable")


class _BlindIdFile(FakeDataFile):
    """A DataFile whose NAME reads but whose lineage id does not - the cloud read that fails one
    step past the name. It is still a file carrying that name, so every same-name count includes
    it; only its URN is unknown."""

    @property
    def id(self):
        raise RuntimeError("3 : cloud read failed")

    @id.setter
    def id(self, value):
        pass                     # nothing this file is built with makes its lineage id readable


def FakeProject(name, pid="p1"):
    """A project whose root folder starts empty - tests append files and subfolders to it."""
    project = FakeDataProject(name=name, project_id=pid,
                              root_folder=FakeDataFolder("Root", is_root=True))
    project.rootFolder.parentProject = project
    return project


def _add_child(folder, name, **kw):
    """One subfolder under `folder`, back-linked - what a folder path resolves through."""
    child = FakeDataFolder(name, folder_id="fid:" + name, parent_folder=folder,
                           parent_project=folder.parentProject, **kw)
    folder._folders.append(child)
    return child


class FakeSaveAsDoc(FakeFusionDocument):
    """An active document that records its saveAs call. raise_on_save/land_on_save model the observed
    false-negative: saveAs RAISES (InternalValidationError) or returns false while the file DID land.

    `land_count` is how many files of that name the folder reads back afterwards. One saveAs cannot
    land two; 2 models the state the documented retry hazard leaves - an earlier saveAs that outlived
    a client timeout had already landed one, this call's pre-check read a lagging folder listing and
    saw none, and the post-error read sees both. `land_blind` lands files whose lineage id will not
    read (it blinds EVERY landed file, so a mixed landing is not constructible here)."""

    def __init__(self, is_saved=False, save_ok=True, new_urn=None,
                 raise_on_save=False, land_on_save=False, land_count=1, land_blind=False):
        # dataFile.id after saveAs: a urn -> surfaced; a local pre-upload handle -> reported null
        super().__init__(name="Untitled", is_saved=is_saved, save_ok=save_ok,
                         save_raises="InternalValidationError" if raise_on_save else None,
                         data_file=FakeDataFile(
                             "Untitled", file_id=new_urn if new_urn is not None
                             else "C:/tmp/local-handle"))
        self._land_on_save = land_on_save
        self._land_count = land_count
        self._land_blind = land_blind

    def saveAs(self, name, folder, description="", tag=""):
        if self._land_on_save:              # the file lands on disk even when the call fails
            for i in range(self._land_count):
                landed = "urn:adsk.file:landed" + (f"-{i + 1}" if i else "")
                folder._files.append(_BlindIdFile(name) if self._land_blind
                                     else FakeDataFile(name, file_id=landed))
        return super().saveAs(name, folder, description, tag)

    @property
    def saveas_args(self):
        """The (name, folder, description, tag) the last saveAs was called with, None if none ran."""
        return self._saves[-1][1] if self._saves else None


class _UnreadAfterSaveDoc(FakeSaveAsDoc):
    def __init__(self, *args, post_mode="listing", **kwargs):
        super().__init__(*args, **kwargs)
        self._post_mode = post_mode

    def saveAs(self, name, folder, description="", tag=""):
        try:
            return super().saveAs(name, folder, description, tag)
        finally:
            if self._post_mode == "listing":
                folder._files_raise = "3 : listing unreadable"
            else:
                folder._files.append(_BlindNameFile("Hidden", file_id="urn:hidden"))


class _UnreadSavedStateDoc(_UnreadAfterSaveDoc):
    @property
    def isSaved(self):
        raise RuntimeError("3 : saved state unreadable")

    @isSaved.setter
    def isSaved(self, value):
        pass


@pytest.fixture(autouse=True)
def _install(monkeypatch):
    """Point doc_save_as and the shared cloud reads at ONE app.data. Both modules read `app` off
    _data_common, so the seam exists only once something installs it - raising=False keeps this
    order-free."""
    def _wire(projects, active=None, by_id=None):
        data = FakeData(projects=projects, files_by_id=by_id or {})
        app = FakeApplication(active_document=active, data=data)
        monkeypatch.setattr(dc, "app", app, raising=False)
        monkeypatch.setattr(dm, "app", app, raising=False)
        return app, data
    return _wire


@pytest.fixture(autouse=True)
def pump_clock(monkeypatch):
    """A VIRTUAL clock for _settled_lineage_urn's post-saveAs wait, in place of real sleep.

    That wait pumps doEvents/sleep rounds until the cloud replaces the local pre-upload handle with
    a lineage 'urn:', bounded by _URN_WAIT_S. No fake here ever settles one, so every no-URN case
    runs the wait to its bound; sleeping it is dead wall-clock for an outcome that is fixed.

    BOTH time.sleep and time.monotonic are replaced, on the time MODULE _export.pump_until and this
    tool each read: a virtual sleep alone would leave the real monotonic deadline unreached and the
    loop spinning for the full bound in wall-clock. Yields the record so a test can assert the wait
    ran, and urn_wait_seconds is the same virtual span."""
    record = {"calls": 0, "virtual_seconds": 0.0}
    base = time.monotonic()

    def _advance(seconds):
        record["calls"] += 1
        record["virtual_seconds"] += seconds

    monkeypatch.setattr(time, "sleep", _advance)
    monkeypatch.setattr(time, "monotonic", lambda: base + record["virtual_seconds"])
    return record


# ─────────────────────────────────────────────────────────────────────────────
# save_document_as_handler  (Document.saveAs — the skill's template-copy path)
# ─────────────────────────────────────────────────────────────────────────────

class TestSaveDocumentAs:
    def test_requires_name(self, _install):
        _install([FakeProject("CAM")], active=FakeSaveAsDoc())
        res = dm.handler(name="", project="CAM")
        assert res["isError"] is True and "Provide 'name'" in res["message"]

    def test_requires_destination_project(self, _install):
        _install([FakeProject("CAM")], active=FakeSaveAsDoc())
        res = dm.handler(name="PartA_CAM")
        assert res["isError"] is True and "project" in res["message"]

    def test_no_active_document(self, _install):
        _install([FakeProject("CAM")], active=None)
        res = dm.handler(name="X", project="CAM")
        assert res["isError"] is True and "No active document" in res["message"]

    def test_unknown_project_lists_available(self, _install):
        _install([FakeProject("CAM"), FakeProject("Parts")], active=FakeSaveAsDoc())
        res = dm.handler(name="X", project="Ghost")
        assert res["isError"] is True
        assert "Ghost" in res["message"] and "CAM" in res["message"]

    def test_missing_folder_without_create_path_errors(self, _install):
        _install([FakeProject("CAM")], active=FakeSaveAsDoc())
        res = dm.handler(
            name="X", project="CAM", folder="MCP Test Parts")
        assert res["isError"] is True
        assert "not found" in res["message"] and "create_path" in res["message"]

    def test_saves_to_root_and_tags_description(self, _install):
        doc = FakeSaveAsDoc(is_saved=True, new_urn="urn:adsk.lineage:newcopy")
        _install([FakeProject("CAM")], active=doc)
        out = _payload(dm.handler(
            name="PartA_CAM", project="CAM", description="encap template copy"))
        assert out["saved"] is True
        assert out["name"] == "PartA_CAM"
        assert out["was_previously_saved"] is True
        assert out["destination_folder"] == "(project root)"
        # the saveAs call carried the AI-agent-marked description
        name, target, desc, tag = doc.saveas_args
        assert desc == "[AI agent] encap template copy"
        assert target.isRoot is True

    def test_create_path_makes_nested_folders(self, _install):
        proj = FakeProject("CAM")
        doc = FakeSaveAsDoc(new_urn="urn:adsk.lineage:x")
        _install([proj], active=doc)
        out = _payload(dm.handler(
            name="PartA_CAM", project="CAM", folder="MCP Test Parts", create_path=True))
        assert out["auto_created_parents"] == ["MCP Test Parts"]
        assert out["destination_folder"] == "MCP Test Parts"
        # the doc was saved INTO that freshly-created folder
        _, target, _, _ = doc.saveas_args
        assert target.name == "MCP Test Parts"

    def test_document_id_null_until_urn_assigned(self, pump_clock, _install):
        # right after saveAs the dataFile.id is a local handle, not a urn: -> reported null
        doc = FakeSaveAsDoc(new_urn=None)  # FakeSaveAsDoc gives a non-urn local handle
        _install([FakeProject("CAM")], active=doc)
        out = _payload(dm.handler(name="X", project="CAM"))
        assert out["document_id"] is None
        # the give-up branch is reached by EXHAUSTING the wait, not by skipping it: a wait that
        # stopped early (or never ran) would report the same null having waited for nothing.
        assert pump_clock["virtual_seconds"] >= dm._URN_WAIT_S
        # and how long it waited is ON THE WIRE, in the payload and named in the note - that number
        # is what lets a caller drop a dwell of its own instead of guessing one.
        assert out["urn_wait_seconds"] == dm._URN_WAIT_S
        assert str(dm._URN_WAIT_S) in out["note"] and "LOCAL path" in out["note"]

    def test_the_urn_wait_covers_the_measured_settle_window(self, _install):
        # The wait blocks Fusion's main thread, so it stays bounded - but it must cover the window a
        # cloud read is measured to trail the cloud in, or a caller still needs a dwell of its own.
        assert dm._URN_WAIT_S == float(dm._doc_common.VERSION_LAG_WINDOW_S)
        assert 0 < dm._URN_WAIT_S <= 30.0, f"the URN wait would block the call for {dm._URN_WAIT_S:g}s"

    def test_a_settled_urn_stops_the_pump_instead_of_running_it_out(self, pump_clock, _install):
        # The other side of the boundary: the first read already answers a lineage urn, so the wait
        # must not run at all - the bound is a give-up bound, not a fixed wait.
        _install([FakeProject("CAM")], active=FakeSaveAsDoc(new_urn="urn:adsk.lineage:immediate"))
        out = _payload(dm.handler(name="X", project="CAM"))
        assert out["document_id"] == "urn:adsk.lineage:immediate"
        assert pump_clock["calls"] == 0 and out["urn_wait_seconds"] == 0.0

    def test_document_id_surfaced_when_urn(self, _install):
        doc = FakeSaveAsDoc(new_urn="urn:adsk.lineage:abc")
        _install([FakeProject("CAM")], active=doc)
        out = _payload(dm.handler(name="X", project="CAM"))
        assert out["document_id"] == "urn:adsk.lineage:abc"

    def test_saveas_false_return_is_an_error(self, _install):
        doc = FakeSaveAsDoc(save_ok=False)
        _install([FakeProject("CAM")], active=doc)
        res = dm.handler(name="X", project="CAM")
        assert res["isError"] is True and "declined to save" in res["message"]

    def test_saveas_raises_but_file_landed_recovers_as_ok(self, _install):
        # saveAs raised InternalValidationError while the folder AND file landed. A same-name file NOW
        # present that was NOT there before is read-back evidence it landed - report ok, not the false
        # negative that would send a retry into a collision.
        proj = FakeProject("CAM")
        doc = FakeSaveAsDoc(raise_on_save=True, land_on_save=True)
        _install([proj], active=doc)
        out = _payload(dm.handler(name="X", project="CAM"))
        assert out["saved"] is True
        assert out["recovered_from_error"] is True
        assert out["document_id"] == "urn:adsk.file:landed"
        assert "DID land" in out["note"] and "InternalValidationError" in out["note"]

    def test_saveas_returns_false_but_file_landed_recovers_as_ok(self, _install):
        proj = FakeProject("CAM")
        doc = FakeSaveAsDoc(save_ok=False, land_on_save=True)
        _install([proj], active=doc)
        out = _payload(dm.handler(name="X", project="CAM"))
        assert out["saved"] is True and out["recovered_from_error"] is True
        assert out["document_id"] == "urn:adsk.file:landed"

    def test_saveas_raises_and_nothing_landed_still_errors(self, _install):
        # No file appeared and the never-saved doc has no settled urn (local handle) - the honest
        # failure stands, no false ok.
        proj = FakeProject("CAM")
        doc = FakeSaveAsDoc(raise_on_save=True, land_on_save=False)  # non-urn local handle df
        _install([proj], active=doc)
        res = dm.handler(name="X", project="CAM")
        assert res["isError"] is True and "saveAs failed" in res["message"]

    def test_saveas_raises_never_saved_doc_with_settled_urn_recovers(self, _install):
        # No file visible in the folder listing yet (cloud lag), but the never-saved doc now carries
        # a settled lineage urn - that is also proof it landed.
        proj = FakeProject("CAM")
        doc = FakeSaveAsDoc(raise_on_save=True, land_on_save=False, new_urn="urn:adsk.lineage:settled")
        _install([proj], active=doc)
        out = _payload(dm.handler(name="X", project="CAM"))
        assert out["saved"] is True and out["recovered_from_error"] is True
        assert out["document_id"] == "urn:adsk.lineage:settled"

    def test_the_recovery_reports_the_seconds_it_spent_waiting(self, pump_clock, _install):
        # The recovery read pumps for the urn like the success path does, so it owes the same
        # number: without it a call that spent seconds on the wait looks instant to its caller,
        # against a wire that says urn_wait_seconds is how long this call waited.
        proj = FakeProject("CAM")
        doc = FakeSaveAsDoc(raise_on_save=True, land_on_save=False)
        doc.dataFile = _LateUrnFile("X", "urn:adsk.lineage:late", local_reads=8)
        _install([proj], active=doc)
        out = _payload(dm.handler(name="X", project="CAM"))
        assert out["document_id"] == "urn:adsk.lineage:late"
        # eight local reads then the urn: eight pumps of _URN_POLL_SLEEP, and the payload says so
        assert pump_clock["calls"] == 8
        assert out["urn_wait_seconds"] == round(8 * dm._URN_POLL_SLEEP, 1)

    def test_a_declined_save_refuses_briefly_instead_of_waiting_out_the_window(self, pump_clock,
                                                                               _install):
        # Fusion DECLINED (saveAs false) and the destination folder holds nothing new, so there is
        # no commit to settle - spending the whole settle window here would make every honest
        # refusal take that long, on a tool that is exempt from the server's call timeout.
        proj = FakeProject("CAM")
        _install([proj], active=FakeSaveAsDoc(save_ok=False, land_on_save=False))
        res = dm.handler(name="X", project="CAM")
        assert res["isError"] is True and "declined to save" in res["message"]
        assert pump_clock["virtual_seconds"] <= dm._DECLINED_PROBE_S
        assert dm._DECLINED_PROBE_S < dm._URN_WAIT_S

    def test_saveas_error_does_not_false_recover_a_duplicate_fork(self, _install):
        # A pre-existing same-name file (allow_duplicate_name) means a file being 'present' after the
        # error is NOT proof THIS save landed - the already-saved/pre-existing case must NOT recover.
        proj = FakeProject("CAM")
        proj.rootFolder._files.append(FakeFile("X", fid="urn:pre-existing"))
        doc = FakeSaveAsDoc(is_saved=True, raise_on_save=True, land_on_save=False)
        _install([proj], active=doc)
        res = dm.handler(
            name="X", project="CAM", allow_duplicate_name=True)
        assert res["isError"] is True and "saveAs failed" in res["message"]

    def test_resolves_project_by_id(self, _install):
        doc = FakeSaveAsDoc(new_urn="urn:adsk.lineage:x")
        _install([FakeProject("CAM", pid="p-cam")], active=doc)
        out = _payload(dm.handler(
            name="X", project_id="p-cam"))
        assert out["destination_project"] == "CAM"

    def test_same_name_in_target_folder_refuses_by_default(self, _install):
        # A same-name file in the target folder is a fork risk - refuse by default (consistent with
        # doc_copy), naming the existing URN + the flag, and DO NOT save.
        proj = FakeProject("CAM")
        proj.rootFolder._files.append(FakeFile("PartA_CAM", fid="urn:existing"))
        doc = FakeSaveAsDoc(new_urn="urn:adsk.lineage:new")
        _install([proj], active=doc)
        res = dm.handler(name="PartA_CAM", project="CAM")
        assert res["isError"] is True
        assert "already exists" in res["message"]
        assert "urn:existing" in res["message"]           # the version-in-place remedy handle
        assert "allow_duplicate_name" in res["message"]   # the deliberate opt-in
        assert doc.saveas_args is None                    # refused BEFORE saving - no fork created

    def test_allow_duplicate_name_forks_and_keeps_the_collision_warning(self, _install):
        # With the explicit opt-in, the fork proceeds AND the name_collision block still fires.
        proj = FakeProject("CAM")
        proj.rootFolder._files.append(FakeFile("PartA_CAM", fid="urn:existing"))
        doc = FakeSaveAsDoc(new_urn="urn:adsk.lineage:newfork")
        _install([proj], active=doc)
        out = _payload(dm.handler(
            name="PartA_CAM", project="CAM", allow_duplicate_name=True))
        assert out["saved"] is True
        assert doc.saveas_args is not None                # the fork actually saved
        assert out["name_collision"]["existing_document_id"] == "urn:existing"
        assert "NAME COLLISION" in out["note"]

    def test_no_collision_when_same_name_absent(self, _install):
        # a same-named file in a DIFFERENT context must not false-trigger: only the target folder counts.
        proj = FakeProject("CAM")
        proj.rootFolder._files.append(FakeFile("SomethingElse", fid="urn:adsk.file:x"))
        doc = FakeSaveAsDoc(new_urn="urn:adsk.lineage:new")
        _install([proj], active=doc)
        out = _payload(dm.handler(name="PartA_CAM", project="CAM"))
        assert "name_collision" not in out

    def test_unreadable_preflight_refuses_without_duplicate_opt_in(self, _install):
        proj = FakeProject("CAM")
        proj.rootFolder._files_raise = "3 : listing unreadable"
        doc = FakeSaveAsDoc(new_urn="urn:adsk.lineage:new")
        _install([proj], active=doc)
        res = dm.handler(name="PartA_CAM", project="CAM")
        assert res["isError"] is True and "cannot verify" in res["message"]
        assert doc.saveas_args is None

    def test_duplicate_opt_in_can_proceed_but_discloses_unknown_preflight(self, _install):
        proj = FakeProject("CAM")
        proj.rootFolder._files_raise = "3 : listing unreadable"
        doc = FakeSaveAsDoc(new_urn="urn:adsk.lineage:new")
        _install([proj], active=doc)
        out = _payload(dm.handler(name="PartA_CAM", project="CAM", allow_duplicate_name=True))
        assert out["saved"] is True and doc.saveas_args is not None
        assert "name_census_incomplete" in out and "NAME CENSUS INCOMPLETE" in out["note"]
        assert "name_collision" not in out

    def test_duplicate_opt_in_does_not_turn_unknown_preflight_into_error_recovery(self, _install):
        proj = FakeProject("CAM")
        proj.rootFolder._files_raise = "3 : listing unreadable"
        doc = FakeSaveAsDoc(save_ok=False)
        _install([proj], active=doc)
        res = dm.handler(name="PartA_CAM", project="CAM", allow_duplicate_name=True)
        assert res["isError"] is True and "whether a file landed is unconfirmed" in res["message"]
        assert doc.saveas_args is not None

    @pytest.mark.parametrize("raises", [False, True])
    def test_unreadable_post_read_reports_landing_as_unconfirmed(self, raises, _install):
        proj = FakeProject("CAM")
        doc = _UnreadAfterSaveDoc(save_ok=False, raise_on_save=raises, land_on_save=True)
        _install([proj], active=doc)
        res = dm.handler(name="PartA_CAM", project="CAM")
        assert res["isError"] is True and "whether a file landed is unconfirmed" in res["message"]
        assert "No change made" not in res["message"] and len(proj.rootFolder._files) == 1

    def test_partial_post_census_proves_effect_but_not_lineage_or_total(self, _install):
        proj = FakeProject("CAM")
        doc = _UnreadAfterSaveDoc(save_ok=False, land_on_save=True, post_mode="name")
        _install([proj], active=doc)
        out = _payload(dm.handler(name="PartA_CAM", project="CAM"))
        assert out["saved"] is True and out["document_id"] is None
        assert out["known_same_name_document_ids"] == ["urn:adsk.file:landed"]
        assert "same_name_document_ids" not in out
        assert "not an exhaustive count" in out["note"]

    def test_fresh_document_lineage_recovers_despite_unread_post_census(
            self, monkeypatch, _install):
        proj = FakeProject("CAM")
        target = FakeDataFolder("Made")
        monkeypatch.setattr(dm, "_ensure_folder_path", lambda root, parts: (target, ["Made"]))
        doc = _UnreadAfterSaveDoc(save_ok=False, new_urn="urn:adsk.lineage:fresh")
        _install([proj], active=doc)
        out = _payload(dm.handler(name="PartA_CAM", project="CAM",
                                  folder="Made", create_path=True))
        assert out["saved"] is True and out["document_id"] == "urn:adsk.lineage:fresh"
        assert out["auto_created_parents"] == ["Made"]
        assert "name_census_incomplete" in out

    def test_unknown_prior_saved_state_cannot_prove_fresh_lineage(self, _install):
        proj = FakeProject("CAM")
        doc = _UnreadSavedStateDoc(save_ok=False, new_urn="urn:adsk.lineage:old-or-new")
        _install([proj], active=doc)
        res = dm.handler(name="PartA_CAM", project="CAM")
        assert res["isError"] is True and "unconfirmed" in res["message"]
        assert "old-or-new" not in json.dumps(res)


# ─────────────────────────────────────────────────────────────────────────────
# a folder holding SEVERAL files of ONE name — the by-name file resolver
# ─────────────────────────────────────────────────────────────────────────────

class TestSameNameFilesInOneFolder:
    """A folder holds several files of one name: two saveAs calls into one folder under one name
    produce two DISTINCT lineages, and the folder reads back both files under that name. So a file
    name is not an identity there - the resolver must REFUSE and name the candidates by the lineage
    URN, the one thing that tells them apart, never hand back the first sibling."""

    _URN_A = "urn:adsk.wipprod:dm.lineage:hW1WC_3CRkurSsn8eRCmaQ"
    _URN_B = "urn:adsk.wipprod:dm.lineage:zTj_JYIcRyqZ35BGQj6N1Q"

    def _twins(self):
        proj = FakeProject("CAM")
        proj.rootFolder._files.append(FakeFile("AR44-Dup", fid=self._URN_A))
        proj.rootFolder._files.append(FakeFile("AR44-Dup", fid=self._URN_B))
        return proj

    def test_two_files_of_one_name_are_refused_naming_both_urns(self, _install):
        proj = self._twins()
        found, refusal = dc._file_in_folder_by_name(proj.rootFolder, "AR44-Dup")
        assert found is None                       # never one of the two
        assert self._URN_A in refusal and self._URN_B in refusal
        assert "2 files" in refusal

    def test_one_file_of_that_name_still_resolves(self, _install):
        proj = FakeProject("CAM")
        only = FakeFile("AR44-Dup", fid=self._URN_A)
        proj.rootFolder._files.append(only)
        assert dc._file_in_folder_by_name(proj.rootFolder, "AR44-Dup") == (only, None)

    def test_no_file_of_that_name_is_a_clean_miss_not_a_refusal(self, _install):
        proj = FakeProject("CAM")
        proj.rootFolder._files.append(FakeFile("Other", fid=self._URN_A))
        assert dc._file_in_folder_by_name(proj.rootFolder, "AR44-Dup") == (None, None)

    def test_a_longer_name_is_a_different_file(self, _install):
        # whole-name match: 'AR44-Dup2' neither resolves as nor collides with 'AR44-Dup'
        proj = FakeProject("CAM")
        proj.rootFolder._files.append(FakeFile("AR44-Dup2", fid=self._URN_B))
        assert dc._file_in_folder_by_name(proj.rootFolder, "AR44-Dup") == (None, None)

    def test_an_unreadable_id_is_named_as_such_beside_its_twin(self, _install):
        # the URN is what the refusal is FOR: an id that will not read must say so, not vanish and
        # leave a caller reading one URN for two files.
        proj = FakeProject("CAM")
        proj.rootFolder._files.append(FakeFile("AR44-Dup", fid=self._URN_A))
        proj.rootFolder._files.append(_BlindIdFile("AR44-Dup"))
        found, refusal = dc._file_in_folder_by_name(proj.rootFolder, "AR44-Dup")
        assert found is None
        assert self._URN_A in refusal and "(id unreadable)" in refusal

    def test_doc_copy_refuses_an_ambiguous_destination_and_copies_nothing(self, monkeypatch, _install):
        proj = self._twins()
        src = FakeFile("Template", fid="urn:adsk.file:src")
        _install([proj], by_id={"urn:adsk.file:src": src})
        res = dcopy.handler(
            document_id="urn:adsk.file:src", project="CAM", name="AR44-Dup")
        assert res["isError"] is True
        assert self._URN_A in res["message"] and self._URN_B in res["message"]
        # the remedy is in doc_copy's OWN input vocabulary, not "go rename/delete a cloud file"
        assert "'folder'" in res["message"] and "'name'" in res["message"]
        assert len(proj.rootFolder._files) == 2            # nothing was copied in

    def test_doc_save_as_refuses_by_default_naming_both_urns_and_the_optin(self, monkeypatch, _install):
        proj = self._twins()
        doc = FakeSaveAsDoc(new_urn="urn:adsk.lineage:new")
        _install([proj], active=doc)
        res = dm.handler(name="AR44-Dup", project="CAM")
        assert res["isError"] is True
        assert self._URN_A in res["message"] and self._URN_B in res["message"]
        assert "doc_open" in res["message"] and "allow_duplicate_name" in res["message"]
        assert doc.saveas_args is None                     # refused BEFORE saving - no third fork

    def test_the_opt_in_fork_lists_every_pre_existing_lineage(self, monkeypatch, _install):
        # one 'existing_document_id' cannot state two, so the collision block names them all rather
        # than dropping the warning (or picking a sibling) when the name was already shared.
        proj = self._twins()
        doc = FakeSaveAsDoc(new_urn="urn:adsk.lineage:third")
        _install([proj], active=doc)
        out = _payload(dm.handler(
            name="AR44-Dup", project="CAM", allow_duplicate_name=True))
        assert out["saved"] is True and doc.saveas_args is not None
        assert out["name_collision"]["existing_document_ids"] == [self._URN_A, self._URN_B]
        assert "NAME COLLISION" in out["note"]

    def test_the_fork_warning_names_an_unreadable_id_instead_of_dropping_it(self, monkeypatch, _install):
        # A file whose id will not read is still one of the files carrying that name. Dropping it
        # renders 2 files under ONE URN - which reads as though both were that lineage - and hands
        # back an id list one entry short of the count beside it.
        proj = FakeProject("CAM")
        proj.rootFolder._files.append(FakeFile("AR44-Dup", fid=self._URN_A))
        proj.rootFolder._files.append(_BlindIdFile("AR44-Dup"))
        doc = FakeSaveAsDoc(new_urn="urn:adsk.lineage:third")
        _install([proj], active=doc)
        out = _payload(dm.handler(
            name="AR44-Dup", project="CAM", allow_duplicate_name=True))
        collision = out["name_collision"]
        assert collision["existing_document_ids"] == [self._URN_A, None]   # a slot per file
        assert "2 files named 'AR44-Dup'" in collision["warning"]
        assert "(id unreadable)" in collision["warning"]                   # named, not vanished

    def test_a_recovery_read_finding_two_files_does_not_name_one_as_this_save(self, monkeypatch, _install):
        # The documented retry hazard: a saveAs outlived a client timeout and landed, the retry
        # raised, and the folder now reads back TWO files of the name. WHICH lineage this call wrote
        # is not readable off the folder, so document_id comes from the document's own settled URN.
        proj = FakeProject("CAM")
        doc = FakeSaveAsDoc(raise_on_save=True, land_on_save=True, land_count=2,
                            new_urn="urn:adsk.lineage:settled")
        _install([proj], active=doc)
        out = _payload(dm.handler(name="X", project="CAM"))
        assert out["saved"] is True and out["recovered_from_error"] is True
        assert out["document_id"] == "urn:adsk.lineage:settled"
        assert len(proj.rootFolder._files) == 2            # both really are there
        # the duplicate it just measured is DISCLOSED, not discarded, even where document_id resolved
        assert out["same_name_document_ids"] == ["urn:adsk.file:landed", "urn:adsk.file:landed-2"]
        assert "2 files named 'X'" in out["note"]

    def test_an_already_saved_doc_withholds_the_id_rather_than_naming_its_source_lineage(
            self, monkeypatch, _install):
        # The other side of the was_saved boundary, and the damaging one: on an ALREADY-SAVED
        # document dataFile.id still reads the lineage it was saved FROM - a different file, under a
        # different name, in a different folder - so it must not stand in for the file this call
        # wrote. Null, plus the candidates, beats a confident wrong URN.
        proj = FakeProject("CAM")
        doc = FakeSaveAsDoc(is_saved=True, raise_on_save=True, land_on_save=True, land_count=2,
                            new_urn="urn:adsk.wipprod:dm.lineage:SOURCE")
        _install([proj], active=doc)
        out = _payload(dm.handler(name="X", project="CAM"))
        assert out["saved"] is True and out["recovered_from_error"] is True
        assert out["document_id"] is None                  # never the source lineage
        assert "SOURCE" not in json.dumps(out)
        # and the ambiguity this recovery MEASURED reaches the caller: the count and both URNs
        assert out["same_name_document_ids"] == ["urn:adsk.file:landed", "urn:adsk.file:landed-2"]
        assert "2 files named 'X'" in out["note"]
        assert "urn:adsk.file:landed" in out["note"] and "urn:adsk.file:landed-2" in out["note"]
        assert "'document_id' is null" in out["note"] and "data_get" in out["note"]

    def test_a_single_landed_file_with_an_unreadable_id_also_withholds_it(self, monkeypatch, _install):
        # The same gate one file down: exactly one file landed but its id will not read, so there is
        # nothing to publish - and an already-saved doc's own URN is still the wrong answer.
        proj = FakeProject("CAM")
        doc = FakeSaveAsDoc(is_saved=True, raise_on_save=True, land_on_save=True, land_blind=True,
                            new_urn="urn:adsk.wipprod:dm.lineage:SOURCE")
        _install([proj], active=doc)
        out = _payload(dm.handler(name="X", project="CAM"))
        assert out["saved"] is True and out["recovered_from_error"] is True
        assert out["document_id"] is None
        assert "same_name_document_ids" not in out         # one file is not an ambiguity
        assert "'document_id' is null" in out["note"]

    def test_several_landed_files_with_unreadable_ids_keep_a_slot_each(self, monkeypatch, _install):
        # The same dropped-slot defect as the fork warning, in the recovery disclosure: an id list
        # that skips a blind file comes back shorter than the count in the note beside it, so the
        # two surfaces disagree about how many files carry the name. One slot per file, always.
        proj = FakeProject("CAM")
        doc = FakeSaveAsDoc(is_saved=True, raise_on_save=True, land_on_save=True, land_count=2,
                            land_blind=True, new_urn="urn:adsk.wipprod:dm.lineage:SOURCE")
        _install([proj], active=doc)
        out = _payload(dm.handler(name="X", project="CAM"))
        assert out["saved"] is True
        assert out["same_name_document_ids"] == [None, None]
        assert out["note"].count("(id unreadable)") == 2   # named once per file, not collapsed
        assert "2 files named 'X'" in out["note"]


# ─────────────────────────────────────────────────────────────────────────────
# doc_save_as folder-resolution retry on the cloud eventual-consistency self-contradiction
# ─────────────────────────────────────────────────────────────────────────────

class _FlakyRoot(FakeDataFolder):
    """A project root whose dataFolders enumeration lags (eventual-consistency): the first read returns
    an empty list (so the first resolve MISSES the child) but every later read includes it - a cloud
    eventual-consistency self-contradiction (observed live): the folder appears in its own
    available-folders list yet does not resolve until a retry."""
    def __init__(self, child_name, empty_calls=1):
        super().__init__("Root", is_root=True, folders=[FakeDataFolder(child_name)])
        self._child = self._folders[0]
        self._calls = 0
        self._empty_calls = empty_calls

    @property
    def dataFolders(self):
        return _LaggingFolders(self)


class _LaggingFolders(_CloudArray):
    """The dataFolders read behind _FlakyRoot: asArray() answers EMPTY for the first `empty_calls`
    fetches and the child from then on. The lag lives in the FETCH, not in the attribute read, so a
    resolve that enumerates twice spends two of them."""
    def __init__(self, root):
        super().__init__([root._child])
        self._root = root

    def asArray(self):
        self._root._calls += 1
        return [] if self._root._calls <= self._root._empty_calls else [self._root._child]


class TestFolderResolveEventual:
    def test_retries_on_self_contradiction(self, _install):
        # first resolve misses; the child IS in the (now-fresh) sibling list -> ONE retry resolves it.
        root = _FlakyRoot("Stage-v1", empty_calls=1)
        target, missing, retried = dm._resolve_folder_eventual(root, ["Stage-v1"])
        assert retried is True
        assert missing is None
        assert target is root._child

    def test_genuine_miss_is_not_retried(self, _install):
        # a folder truly absent from the siblings must NOT be retried (only the self-contradiction is).
        root = FakeDataFolder("Root", is_root=True)          # no children at all
        target, missing, retried = dm._resolve_folder_eventual(root, ["Ghost"])
        assert target is None
        assert missing == "Ghost"
        assert retried is False

    def test_first_read_success_is_not_retried(self, _install):
        root = FakeDataFolder("Root", is_root=True)
        _add_child(root, "Stage-v1")
        target, missing, retried = dm._resolve_folder_eventual(root, ["Stage-v1"])
        assert target is not None and retried is False   # resolved on the first read, no retry

    def test_saveas_recovers_and_notes_eventual_consistency(self, _install):
        doc = FakeSaveAsDoc(new_urn="urn:adsk.lineage:x")
        proj = FakeProject("CAM")
        proj.rootFolder = _FlakyRoot("Stage-v1", empty_calls=1)
        _install([proj], active=doc)
        out = _payload(dm.handler(
            name="P5", project="CAM", folder="Stage-v1"))
        assert out.get("folder_resolve_retried") is True
        assert "eventual-consistency" in out["note"]
        # it actually saved INTO the recovered folder
        _, target, _, _ = doc.saveas_args
        assert target.name == "Stage-v1"
