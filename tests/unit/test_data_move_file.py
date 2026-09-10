"""Unit tests for ``data_move_file`` - move one cloud file into another folder of its project.

DataFile.move answers with a bool, and a bool is a claim: the behaviour worth pinning is the
read-back that turns "true but the file is still where it was" into an error, alongside the
destination guard (this tool creates nothing), the already-there no-op, and the ambiguity refusal
arriving from the shared resolver.
"""

import json

import pytest

from conftest import FakeDataFile, FakeDataFolder, FakeDataProject, error_message, load_tool

dmv = load_tool("data_move_file")


def _payload(result):
    assert result["isError"] is False, result
    return json.loads(result["content"][0]["text"])


def _folder(name, subs=(), is_root=False, folders_raise=None):
    """A cloud folder: name/id/isRoot, a parent chain, and the dataFolders walk
    _resolve_folder_path uses. `folders_raise` is the enumeration that fails the way a cloud read
    can - the walk must not read the refusal it produces as 'that folder is empty'."""
    return FakeDataFolder(name, folder_id="fid:" + name, folders=list(subs), is_root=is_root,
                          folders_raise=folders_raise)


class _MoveThatNeverLands(FakeDataFile):
    """A cloud file whose move() answers true and leaves the file exactly where it was - the
    platform lie the post-move read-back exists to catch."""

    def move(self, folder):
        self._moves.append(folder)
        return True


class _MoveThatLandsElsewhere(FakeDataFile):
    """A cloud file whose move() answers true and puts the file in a DIFFERENT folder than the one
    it was handed."""

    def __init__(self, *args, lands_in=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._lands_in = lands_in

    def move(self, folder):
        self._moves.append(folder)
        self.parentFolder = self._lands_in
        return True


def _cloud_file(parent, project, cls=FakeDataFile, **kwargs):
    """The file under move: it lives in `parent` and belongs to `project`."""
    return cls("probe_note.txt", file_id="urn:lin:AAA", parent_folder=parent,
               parent_project=project, **kwargs)


def _project(root):
    return FakeDataProject("Sample Project", project_id="proj-1", root_folder=root)


@pytest.fixture
def wired(monkeypatch):
    """Resolve `file` to a stand-in, and make the post-move re-resolution return the same object
    (the handler re-fetches by lineage URN, which in Fusion is what shows the new parent)."""
    def _use(df=None, err=None):
        monkeypatch.setattr(dmv, "resolve_file_reference",
                            lambda *a, **kw: (df, {"matched_by": "urn", "urn": "urn:lin:AAA"}, err))
        monkeypatch.setattr(dmv, "_resolve_data_file", lambda raw: (df, raw, [raw]))
    return _use


def _tree(root_folders_raise=None):
    """root/{Docs, Parts/Fixtures} - returns (root, docs, parts, fixtures)."""
    fixtures = _folder("Fixtures")
    parts = _folder("Parts", subs=[fixtures])
    docs = _folder("Docs")
    root = _folder("Sample Project", subs=[docs, parts], is_root=True,
                   folders_raise=root_folders_raise)
    return root, docs, parts, fixtures


class TestDestinationGuard:
    def test_missing_target_folder_is_named(self, wired):
        root, docs, _p, _f = _tree()
        wired(_cloud_file(docs, _project(root)))
        assert "target_folder" in error_message(dmv.handler(file="urn:lin:AAA"))

    def test_a_nonexistent_destination_is_refused_with_what_is_there(self, wired):
        root, docs, _p, _f = _tree()
        wired(_cloud_file(docs, _project(root)))
        msg = error_message(dmv.handler(file="urn:lin:AAA", target_folder="Parts/Nope"))
        assert "missing segment 'Nope'" in msg
        assert "Fixtures" in msg                     # what DOES exist at the failure point
        assert "data_create_folder" in msg           # this tool creates nothing

    def test_a_destination_whose_folder_list_will_not_read_is_unknown_not_absent(self, wired):
        # An unreadable dataFolders enumeration is a HOLE, not an absence: "does not exist" would
        # send the caller to data_create_folder to make a folder that may already be there.
        root, docs, _p, _f = _tree(root_folders_raise="cloud read failed")
        df = _cloud_file(docs, _project(root))
        wired(df)
        msg = error_message(dmv.handler(file="urn:lin:AAA", target_folder="Parts"))
        assert "could not be read" in msg and "is unknown" in msg
        assert "does not exist" not in msg
        assert "data_create_folder" not in msg      # the wrong next step for an unread listing
        assert df._moves == []                      # and nothing was moved

    def test_nested_path_resolves(self, wired):
        root, docs, _p, fixtures = _tree()
        df = _cloud_file(docs, _project(root))
        wired(df)
        out = _payload(dmv.handler(file="urn:lin:AAA", target_folder="Parts/Fixtures"))
        assert df._moves == [fixtures]
        assert out["to_folder"] == "Parts/Fixtures"

    def test_slash_targets_the_project_root(self, wired):
        root, docs, _p, _f = _tree()
        df = _cloud_file(docs, _project(root))
        wired(df)
        out = _payload(dmv.handler(file="urn:lin:AAA", target_folder="/"))
        assert df._moves == [root]
        assert out["to_folder"] == "(project root)"


class TestMoveVerification:
    def test_a_successful_move_reports_both_ends(self, wired):
        root, docs, _p, _f = _tree()
        df = _cloud_file(docs, _project(root))
        wired(df)
        out = _payload(dmv.handler(file="urn:lin:AAA", target_folder="Parts/Fixtures"))
        assert out["moved"] is True
        assert out["from_folder"] == "Docs" and out["to_folder"] == "Parts/Fixtures"
        assert out["parent_folder_after"] == "Fixtures"

    def test_true_with_an_unchanged_parent_is_an_error(self, wired):
        # The platform lie this read-back exists for: move() answers true, the file never moved.
        root, docs, _p, _f = _tree()
        wired(_cloud_file(docs, _project(root), cls=_MoveThatNeverLands))
        msg = error_message(dmv.handler(file="urn:lin:AAA", target_folder="Parts/Fixtures"))
        assert "did NOT take" in msg and "Docs" in msg

    def test_landing_in_the_wrong_folder_is_an_error(self, wired):
        root, docs, _p, _f = _tree()
        wired(_cloud_file(docs, _project(root), cls=_MoveThatLandsElsewhere,
                          lands_in=_folder("Somewhere Else")))
        assert "Somewhere Else" in error_message(
            dmv.handler(file="urn:lin:AAA", target_folder="Parts/Fixtures"))

    def test_the_verification_reads_a_FRESH_file_not_the_handle_move_was_called_on(self, wired,
                                                                                     monkeypatch):
        # The re-resolve IS the verification. The handle move() was called on can keep reporting the
        # folder it was resolved from, so a handler that trusts it would confirm a move that never
        # happened. Here the stale handle still says 'Docs' while a fresh fetch shows the landing.
        root, docs, _p, fixtures = _tree()
        stale = _cloud_file(docs, _project(root), cls=_MoveThatNeverLands)
        fresh = _cloud_file(fixtures, _project(root))
        monkeypatch.setattr(dmv, "resolve_file_reference",
                            lambda *a, **kw: (stale, {"matched_by": "urn", "urn": "urn:lin:AAA"},
                                              None))
        monkeypatch.setattr(dmv, "_resolve_data_file", lambda raw: (fresh, raw, [raw]))
        out = _payload(dmv.handler(file="urn:lin:AAA", target_folder="Parts/Fixtures"))
        assert out["moved"] is True
        assert out["parent_folder_after"] == "Fixtures"

    def test_an_id_match_is_published_as_an_id_match(self, wired):
        root, docs, _p, _f = _tree()
        wired(_cloud_file(docs, _project(root)))
        out = _payload(dmv.handler(file="urn:lin:AAA", target_folder="Parts/Fixtures"))
        assert out["verified_by"] == "id"
        assert "by folder NAME" not in out["note"]

    def test_a_name_only_match_is_not_published_with_id_confidence(self, wired):
        # Two sibling folders can share a name, so a NAME match confirms the file is in a folder
        # CALLED that - not the one that was targeted. Publishing it as the same verdict an id match
        # gives hands the caller confidence the read does not support.
        root, docs, _p, fixtures = _tree()
        fixtures.id = None                              # the target's id will not read
        wired(_cloud_file(docs, _project(root)))
        out = _payload(dmv.handler(file="urn:lin:AAA", target_folder="Parts/Fixtures"))
        assert out["moved"] is True
        assert out["verified_by"] == "name"
        assert "by folder NAME, not" in out["note"]

    def test_a_false_return_is_an_error_and_nothing_is_claimed(self, wired):
        root, docs, _p, _f = _tree()
        wired(_cloud_file(docs, _project(root), move_ok=False))
        msg = error_message(dmv.handler(file="urn:lin:AAA", target_folder="Parts/Fixtures"))
        assert "returned false" in msg and "Nothing changed" in msg

    def test_an_unreadable_parent_after_the_move_is_unconfirmed_not_ok(self, wired):
        root, docs, _p, _f = _tree()
        df = _cloud_file(docs, _project(root))
        wired(df)
        original_move = df.move

        def move(target):
            original_move(target)
            df.parentFolder = None            # the re-read cannot see where it landed
            return True

        df.move = move
        assert "UNCONFIRMED" in error_message(
            dmv.handler(file="urn:lin:AAA", target_folder="Parts/Fixtures"))

    def test_an_unverifiable_target_refuses_before_moving(self, wired):
        # Neither id nor name readable on the DESTINATION (here the project root): the post-move
        # read-back would have nothing to compare against, so the mutation must not happen at all.
        blind_root = FakeDataFolder(None, is_root=True)      # no id, no name to read
        df = _cloud_file(_folder("Docs"), _project(blind_root))
        wired(df)
        msg = error_message(dmv.handler(file="urn:lin:AAA", target_folder="/"))
        assert "could not be verified" in msg
        assert df._moves == []

    def test_no_comparable_identity_after_the_move_is_unconfirmed_not_landed(self, wired):
        # Target readable by NAME only, new parent readable by ID only: nothing lines up, so two
        # missing values must not pass as a match.
        target = _folder("Fixtures")
        target.id = None
        root = _folder("Sample Project", subs=[target], is_root=True)
        df = _cloud_file(_folder("Docs"), _project(root))
        wired(df)
        original_move = df.move

        def move(dest):
            original_move(dest)
            df.parentFolder = FakeDataFolder(None, folder_id="fid:Fixtures")
            return True

        df.move = move
        assert "UNCONFIRMED" in error_message(
            dmv.handler(file="urn:lin:AAA", target_folder="Fixtures"))

    def test_already_in_the_target_moves_nothing_and_is_not_an_error(self, wired):
        root, docs, _p, _f = _tree()
        df = _cloud_file(docs, _project(root))
        wired(df)
        out = _payload(dmv.handler(file="urn:lin:AAA", target_folder="Docs"))
        assert out["moved"] is False and out["already_in_target"] is True
        assert df._moves == []

    def test_a_name_match_over_unread_folders_says_so_in_the_note(self, monkeypatch):
        # The file that moved is whichever one the name resolved to. A folder that never opened
        # could hold another of that name, so the caller is told while reversing it is still cheap.
        root, docs, _p, _f = _tree()
        df = _cloud_file(docs, _project(root))
        monkeypatch.setattr(dmv, "resolve_file_reference",
                            lambda *a, **kw: (df, {"matched_by": "name", "urn": "urn:lin:AAA",
                                                   "folders_unreadable": 1}, None))
        monkeypatch.setattr(dmv, "_resolve_data_file", lambda raw: (df, raw, [raw]))
        out = _payload(dmv.handler(file="probe_note.txt", project="P1",
                                   target_folder="Parts/Fixtures"))
        assert out["moved"] is True
        assert "1 folder(s) could not be READ" in out["note"]
        assert "file_id" in out["note"]

    def test_a_move_over_a_fully_read_scope_carries_no_such_caveat(self, wired):
        root, docs, _p, _f = _tree()
        wired(_cloud_file(docs, _project(root)))
        out = _payload(dmv.handler(file="urn:lin:AAA", target_folder="Parts/Fixtures"))
        assert "could not be READ" not in out["note"]

    def test_an_ambiguous_name_refusal_is_passed_through(self, wired):
        wired(err="'notes.txt' names 2 files in project 'P1' - refusing to guess which")
        assert "names 2 files" in error_message(
            dmv.handler(file="notes.txt", project="P1", target_folder="Docs"))

    def test_an_unreadable_project_root_refuses_before_moving(self, wired):
        blind_project = FakeDataProject("P", project_id="p")
        blind_project.rootFolder = None                 # the project's root will not read
        df = _cloud_file(_folder("Docs"), blind_project)
        wired(df)
        assert "project root folder" in error_message(
            dmv.handler(file="urn:lin:AAA", target_folder="Docs"))
        assert df._moves == []
