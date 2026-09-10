"""Unit tests for ``data_download_file`` - one non-Fusion cloud file onto local disk.

The bugs worth pinning are the ones that would send a caller away with nothing, or with a lie:
the Fusion-native refusal (which must read the file's NAME, since fileExtension is measured wrong
for a non-CAD upload), the local-path composition, the stale-file trap (an existing file at the
target would satisfy the landed check for a download that never wrote), and the landed gate itself -
exercised through the SAME FileLanded postcondition the Item declares.
"""

import json
import os

import pytest

from conftest import FakeDataFile, FakeDataFolder, FakeDataProject, error_message, load_tool

ddf = load_tool("data_download_file")
kernel = load_tool("_assert")


def _payload(result):
    assert result["isError"] is False, result
    return json.loads(result["content"][0]["text"])


class _DownloadableFile(FakeDataFile):
    """A cloud file whose download() writes `writes` at the path it is handed (None writes nothing,
    the platform's silent-failure shape) and answers `returns`. Each (path, handler) pair it is
    called with lands in _calls."""

    def __init__(self, *args, writes=None, returns=True, **kwargs):
        super().__init__(*args, **kwargs)
        self._writes, self._returns = writes, returns
        self._calls = []

    def download(self, path, handler):
        self._calls.append((path, handler))
        if self._writes is not None:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(self._writes)
        return self._returns


def _cloud_file(name="probe_note.txt", file_extension="sql", writes=None, returns=True):
    """The file under download, in a 'Docs' folder of a named project."""
    return _DownloadableFile(name, extension=file_extension, writes=writes, returns=returns,
                             parent_folder=FakeDataFolder("Docs"),
                             parent_project=FakeDataProject("Sample Project"))


@pytest.fixture
def resolves(monkeypatch):
    """Point the tool's file resolution at a stand-in (or at a refusal)."""
    def _use(df=None, err=None, scope_truncated=False, folders_unreadable=0):
        meta = {"matched_by": "urn", "urn": "urn:lin:AAA", "scope_truncated": scope_truncated,
                "folders_unreadable": folders_unreadable}
        monkeypatch.setattr(ddf, "resolve_file_reference", lambda *a, **kw: (df, meta, err))
    return _use


def _landed(**kwargs):
    """Call the handler through the postcondition the Item declares, so the on-disk gate applies."""
    return kernel.wrap(ddf.handler, [kernel.FileLanded("file_path")])(**kwargs)


def _staging_dirs(folder):
    return list(folder.glob(".fusion-download-*"))


class TestFusionNativeRefusal:
    def test_f3d_is_refused_by_name_with_the_export_pointer(self, resolves, tmp_path):
        resolves(_cloud_file(name="Bracket.f3d", file_extension="f3d"))
        msg = error_message(ddf.handler(file="urn:lin:AAA", destination_folder=str(tmp_path)))
        assert "design_export" in msg and "Bracket.f3d" in msg

    def test_f2d_drawing_is_refused_with_the_drawing_pointer(self, resolves, tmp_path):
        resolves(_cloud_file(name="Bracket Drawing.f2d", file_extension="f2d"))
        msg = error_message(ddf.handler(file="urn:lin:AAA", destination_folder=str(tmp_path)))
        assert "drawing_export" in msg

    def test_extensionless_name_falls_back_to_file_extension(self, resolves, tmp_path):
        # Measured: a Fusion design's DataFile name carries no extension ('Gyroscope') while
        # fileExtension reads 'f3d' - so this fallback is the branch a design's refusal travels.
        resolves(_cloud_file(name="Bracket", file_extension="f3d"))
        assert "design_export" in error_message(
            ddf.handler(file="urn:lin:AAA", destination_folder=str(tmp_path)))

    def test_a_lying_file_extension_does_not_refuse_a_plain_text_file(self, resolves, tmp_path):
        # Measured: an uploaded .txt reports fileExtension 'sql'. Reading the NAME is what keeps a
        # real, downloadable file from being refused (or a design from slipping through).
        df = _cloud_file(name="probe_note.txt", file_extension="sql", writes="hello")
        resolves(df)
        out = _payload(ddf.handler(file="urn:lin:AAA", destination_folder=str(tmp_path)))
        assert out["downloaded"] is True
        assert os.path.basename(out["file_path"]) == "probe_note.txt"

    def test_the_name_outranks_a_file_extension_claiming_fusion_data(self, resolves, tmp_path):
        # fileExtension is not trusted where the name disagrees: a named .txt downloads even when
        # the property reports a Fusion extension.
        resolves(_cloud_file(name="notes.txt", file_extension="f3d", writes="hello"))
        out = _payload(ddf.handler(file="urn:lin:AAA", destination_folder=str(tmp_path)))
        assert out["downloaded"] is True


class TestPathHandling:
    def test_writes_into_the_destination_folder_under_the_cloud_name_synchronously(self, resolves,
                                                                                   tmp_path):
        df = _cloud_file(writes="hello")
        resolves(df)
        out = _payload(ddf.handler(file="urn:lin:AAA", destination_folder=str(tmp_path)))
        assert len(df._calls) == 1 and df._calls[0][1] is None
        staged = df._calls[0][0]
        assert os.path.basename(staged) == "probe_note.txt"
        assert staged != str(tmp_path / "probe_note.txt")
        assert out["file_path"] == str(tmp_path / "probe_note.txt")
        assert _staging_dirs(tmp_path) == []

    def test_the_destination_does_not_appear_while_the_transfer_is_running(self, resolves,
                                                                           tmp_path):
        target = tmp_path / "probe_note.txt"
        df = _cloud_file()

        def download(path, handler):
            assert handler is None and not target.exists()
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("fresh")
            return True

        df.download = download
        resolves(df)
        out = _payload(ddf.handler(file="urn:lin:AAA", destination_folder=str(tmp_path)))
        assert out["file_path"] == str(target) and target.read_text(encoding="utf-8") == "fresh"

    def test_file_name_overrides_the_local_name(self, resolves, tmp_path):
        df = _cloud_file(writes="hello")
        resolves(df)
        out = _payload(ddf.handler(file="urn:lin:AAA", destination_folder=str(tmp_path),
                                   file_name="renamed.txt"))
        assert out["file_path"] == str(tmp_path / "renamed.txt")

    def test_file_name_carrying_a_path_is_refused(self, resolves, tmp_path):
        resolves(_cloud_file(writes="hello"))
        msg = error_message(ddf.handler(file="urn:lin:AAA", destination_folder=str(tmp_path),
                                        file_name="sub/renamed.txt"))
        assert "bare filename" in msg

    def test_missing_destination_folder_is_named(self, resolves):
        df = _cloud_file()
        resolves(df)
        msg = error_message(ddf.handler(file="urn:lin:AAA"))
        assert "Provide 'destination_folder'" in msg and "LOCAL folder" in msg
        assert df._calls == []            # refused before the transfer, never into the process cwd

    def test_an_unreadable_cloud_name_asks_for_file_name_instead_of_guessing(self, resolves,
                                                                             tmp_path):
        # No name and no 'file_name' leaves nothing to write to - joining an empty name would
        # target the destination FOLDER itself.
        df = _cloud_file(name="", file_extension="")
        resolves(df)
        msg = error_message(ddf.handler(file="urn:lin:AAA", destination_folder=str(tmp_path)))
        assert "pass 'file_name'" in msg
        assert df._calls == []

    def test_an_unnamed_file_downloads_under_the_file_name_given(self, resolves, tmp_path):
        # the guard above is about the MISSING pair, not about an unreadable name alone.
        resolves(_cloud_file(name="", file_extension="", writes="hello"))
        out = _payload(ddf.handler(file="urn:lin:AAA", destination_folder=str(tmp_path),
                                   file_name="chosen.txt"))
        assert out["file_path"] == str(tmp_path / "chosen.txt")

    def test_a_destination_folder_that_cannot_be_created_is_named(self, resolves, tmp_path,
                                                                  monkeypatch):
        df = _cloud_file(writes="hello")
        resolves(df)
        dest = str(tmp_path / "new")

        def refuse(path, exist_ok=False):
            raise OSError("permission denied")

        monkeypatch.setattr(ddf.os, "makedirs", refuse)
        msg = error_message(ddf.handler(file="urn:lin:AAA", destination_folder=dest))
        assert f"Could not create destination folder '{dest}'" in msg and "permission denied" in msg
        assert df._calls == []

    def test_a_missing_destination_folder_is_created(self, resolves, tmp_path):
        df = _cloud_file(writes="hello")
        resolves(df)
        dest = tmp_path / "new" / "deeper"
        out = _payload(ddf.handler(file="urn:lin:AAA", destination_folder=str(dest)))
        assert os.path.isdir(str(dest)) and out["downloaded"] is True


class TestStaleFileTrap:
    def test_an_existing_local_file_is_refused_and_left_alone(self, resolves, tmp_path):
        target = tmp_path / "probe_note.txt"
        target.write_text("previous", encoding="utf-8")
        df = _cloud_file(writes="fresh")
        resolves(df)
        msg = error_message(ddf.handler(file="urn:lin:AAA", destination_folder=str(tmp_path)))
        assert "overwrite=true" in msg
        assert target.read_text(encoding="utf-8") == "previous"     # untouched
        assert df._calls == []                                       # and never downloaded

    def test_overwrite_keeps_the_prior_file_when_the_download_writes_nothing(
            self, resolves, tmp_path):
        target = tmp_path / "probe_note.txt"
        target.write_bytes(b"previous")
        resolves(_cloud_file(writes=None))
        res = _landed(file="urn:lin:AAA", destination_folder=str(tmp_path), overwrite=True)
        assert "no file was written" in error_message(res)
        assert target.read_bytes() == b"previous"
        assert _staging_dirs(tmp_path) == []

    def test_a_download_that_cannot_be_published_leaves_the_prior_bytes(
            self, resolves, tmp_path, monkeypatch):
        target = tmp_path / "probe_note.txt"
        target.write_bytes(b"previous")
        df = _cloud_file(writes="fresh")
        resolves(df)

        def refuse(source, destination):
            assert destination == str(target)
            raise OSError("file is locked")

        monkeypatch.setattr(ddf.os, "replace", refuse)
        msg = error_message(ddf.handler(file="urn:lin:AAA", destination_folder=str(tmp_path),
                                        overwrite=True))
        assert f"Could not publish the downloaded file to '{target}'" in msg
        assert "file is locked" in msg and len(df._calls) == 1
        assert target.read_bytes() == b"previous"
        assert _staging_dirs(tmp_path) == []

    def test_a_destination_appearing_during_transfer_is_not_overwritten(self, resolves, tmp_path):
        target = tmp_path / "probe_note.txt"
        df = _cloud_file()

        def download(path, handler):
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("fresh")
            target.write_bytes(b"intruder")
            return True

        df.download = download
        resolves(df)
        msg = error_message(ddf.handler(file="urn:lin:AAA", destination_folder=str(tmp_path)))
        assert "appeared while the download was in progress" in msg
        assert target.read_bytes() == b"intruder"
        assert _staging_dirs(tmp_path) == []

    def test_overwrite_replaces_the_content(self, resolves, tmp_path):
        target = tmp_path / "probe_note.txt"
        target.write_text("previous", encoding="utf-8")
        resolves(_cloud_file(writes="fresh"))
        out = _payload(_landed(file="urn:lin:AAA", destination_folder=str(tmp_path), overwrite=True))
        assert target.read_text(encoding="utf-8") == "fresh"
        assert out["size_bytes"] == 5
        assert out["overwrote_existing"] is True
        assert _staging_dirs(tmp_path) == []

    def test_overwrite_over_an_empty_destination_reports_no_overwrite(self, resolves, tmp_path):
        # overwrote_existing is the OBSERVED removal, not an echo of the input flag: nothing was
        # there, so nothing was replaced.
        resolves(_cloud_file(writes="hello"))
        out = _payload(ddf.handler(file="urn:lin:AAA", destination_folder=str(tmp_path),
                                   overwrite=True))
        assert out["overwrote_existing"] is False


class TestFailureIsNeverASuccess:
    def test_a_false_return_after_a_partial_write_keeps_the_prior_file(self, resolves, tmp_path):
        target = tmp_path / "probe_note.txt"
        target.write_bytes(b"previous")
        resolves(_cloud_file(writes="partial", returns=False))
        assert "returned false" in error_message(
            ddf.handler(file="urn:lin:AAA", destination_folder=str(tmp_path), overwrite=True))
        assert target.read_bytes() == b"previous"
        assert _staging_dirs(tmp_path) == []

    def test_a_raising_download_after_a_partial_write_keeps_the_prior_file(
            self, resolves, tmp_path):
        target = tmp_path / "probe_note.txt"
        target.write_bytes(b"previous")

        def boom(path, handler):
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("partial")
            raise RuntimeError("network is down")

        df = _cloud_file()
        df.download = boom
        resolves(df)
        assert "network is down" in error_message(
            ddf.handler(file="urn:lin:AAA", destination_folder=str(tmp_path), overwrite=True))
        assert target.read_bytes() == b"previous"
        assert _staging_dirs(tmp_path) == []

    def test_cleanup_failure_after_publication_is_disclosed_as_partial(
            self, resolves, tmp_path, monkeypatch):
        original = ddf.shutil.rmtree
        resolves(_cloud_file(writes="fresh"))

        def refuse(path):
            raise OSError("cleanup denied")

        monkeypatch.setattr(ddf.shutil, "rmtree", refuse)
        out = _payload(_landed(file="urn:lin:AAA", destination_folder=str(tmp_path)))
        stage_dir = _staging_dirs(tmp_path)[0]
        assert out["downloaded"] is True
        assert str(tmp_path / "probe_note.txt") in out["note"]
        assert str(stage_dir) in out["note"] and "cleanup denied" in out["note"]
        original(stage_dir)

    def test_true_with_nothing_on_disk_fails_the_landed_gate(self, resolves, tmp_path):
        # The platform can answer true and write nothing - the postcondition is what catches it.
        resolves(_cloud_file(writes=None, returns=True))
        res = _landed(file="urn:lin:AAA", destination_folder=str(tmp_path))
        assert "no file was written" in error_message(res)

    def test_an_empty_file_is_not_accepted_as_landed(self, resolves, tmp_path):
        resolves(_cloud_file(writes=""))
        res = _landed(file="urn:lin:AAA", destination_folder=str(tmp_path))
        assert "size_bytes=0" in error_message(res)

    def test_a_name_matched_inside_a_capped_listing_says_so_in_the_note(self, resolves, tmp_path):
        # A unique match inside a CAPPED listing is not proof of uniqueness - files past the cap
        # were never compared, and the download is of whichever one was found.
        resolves(_cloud_file(writes="hello"), scope_truncated=True)
        out = _payload(ddf.handler(file="probe_note.txt", project="P1",
                                   destination_folder=str(tmp_path)))
        assert "capped listing" in out["note"] and "lineage URN is exact" in out["note"]

    def test_an_uncapped_match_does_not_carry_that_caveat(self, resolves, tmp_path):
        resolves(_cloud_file(writes="hello"))
        out = _payload(ddf.handler(file="probe_note.txt", project="P1",
                                   destination_folder=str(tmp_path)))
        assert "capped listing" not in out["note"]

    def test_a_capped_name_scope_is_a_payload_FACT_not_only_prose(self, resolves, tmp_path):
        # A caller that reads the payload rather than the note still has to learn the match was
        # not settled, so the flag travels as its own key - the shape data_get publishes.
        resolves(_cloud_file(writes="hello"), scope_truncated=True)
        out = _payload(ddf.handler(file="probe_note.txt", project="P1",
                                   destination_folder=str(tmp_path)))
        assert out["name_scope_truncated"] is True

    def test_folders_that_would_not_read_while_resolving_the_name_are_published(self, resolves,
                                                                                tmp_path):
        # A folder that never opened is a HOLE in the search space: another file of this name could
        # be in it, which would make the file now on disk the wrong one.
        resolves(_cloud_file(writes="hello"), folders_unreadable=2)
        out = _payload(ddf.handler(file="probe_note.txt", project="P1",
                                   destination_folder=str(tmp_path)))
        assert out["name_scope_folders_unreadable"] == 2
        assert "2 folder(s) could not be READ" in out["note"]
        assert "lineage URN" in out["note"]

    def test_a_clean_match_publishes_no_cap_and_no_holes(self, resolves, tmp_path):
        # The other side of the boundary: nothing was capped and every folder read, so the keys say
        # so positively (false/0) and neither caveat reaches the note.
        resolves(_cloud_file(writes="hello"))
        out = _payload(ddf.handler(file="probe_note.txt", project="P1",
                                   destination_folder=str(tmp_path)))
        assert out["name_scope_truncated"] is False
        assert out["name_scope_folders_unreadable"] == 0
        assert "could not be READ" not in out["note"] and "capped listing" not in out["note"]

    def test_an_ambiguous_name_refusal_is_passed_through(self, resolves, tmp_path):
        resolves(err="'notes.txt' names 2 files in project 'P1' - refusing to guess which")
        assert "names 2 files" in error_message(
            ddf.handler(file="notes.txt", project="P1", destination_folder=str(tmp_path)))
