"""Unit tests for ``_data_read.py`` - the project/file read cores behind data_get.

The headline behaviour under test is the file lister's optional ``folder`` scoping (so a caller can
list ONE folder instead of dumping a whole large project - data_get(project=..., folder=...)
delegates here). The branches that matter and can silently send a caller to the wrong place: folder
navigation by case-insensitive name, a nested path, ``recursive`` immediate-files-only vs. descend,
the folder-not-found error (with its "available subfolders" hint), project resolution by name/id,
and the whole-project fallback when no folder is given. Plus ``file_facts_handler`` - the
single-file record behind data_get(file=...), whose projection, date conversion and two link reads
are pinned at the bottom of this file.
"""

import json
from types import SimpleNamespace

import pytest

from conftest import (FakeApplication, FakeData, FakeDataFile, FakeDataFolder, FakeDataProject,
                      error_message, load_tool, make_data_tree)

dm = load_tool("_data_read")
dc = load_tool("_data_common")


def _unreadable(name):
    """A DataFile member whose READ raises the way a stalled cloud read does. The setter swallows,
    so the shared file's constructor still runs."""
    def _read(self):
        raise RuntimeError(f"3 : {name} could not be read")

    def _write(self, value):
        pass
    return property(_read, _write)


def _file(name, urn):
    """One cloud file carrying the id/versionId/URL trio _file_summary projects."""
    return FakeDataFile(name, file_id=urn, version_id=urn + "?version=1",
                        web_url="https://example/" + urn)


@pytest.fixture
def cloud(monkeypatch):
    """Point both cloud seams - _data_read's own app and the _data_common one _data() reads
    through - at a hub holding `projects`."""
    def _use(*projects, files_by_id=None):
        app = FakeApplication(data=FakeData(projects=list(projects), files_by_id=files_by_id))
        monkeypatch.setattr(dm, "app", app)
        monkeypatch.setattr(dc, "app", app)
        return app
    return _use


def _payload(result):
    """Unwrap a non-error result envelope into its parsed JSON payload."""
    assert result["isError"] is False, result
    return json.loads(result["content"][0]["text"])


def _sample_project():
    """CAM-like project: root files + a 'Workflow Templates' folder with files +
    a nested 'Parts/Fixtures' chain."""
    templates = FakeDataFolder("Workflow Templates",
                               files=[_file("Template A", "urn:lin:AAA"),
                                      _file("Template B", "urn:lin:BBB")])
    fixtures = FakeDataFolder("Fixtures", files=[_file("Vise", "urn:lin:CCC")])
    parts = FakeDataFolder("Parts", folders=[fixtures])
    return make_data_tree(name="CAM", files=[_file("RootPart", "urn:lin:ROOT")],
                          folders=[templates, parts])


# ── navigate_folder_path: the shared folder-path walk this lister scopes through ────

class TestNavigateFolderPath:
    def test_exact_match(self):
        proj = _sample_project()
        folder, path, miss = dm.navigate_folder_path(proj.rootFolder, "Workflow Templates")
        assert miss is None and folder.name == "Workflow Templates" and path == "Workflow Templates"

    def test_case_insensitive_reports_the_folders_own_name(self):
        proj = _sample_project()
        folder, path, miss = dm.navigate_folder_path(proj.rootFolder, "workflow templates")
        assert miss is None and folder.name == "Workflow Templates"
        assert path == "Workflow Templates"

    def test_whitespace_and_stray_slashes_trimmed(self):
        proj = _sample_project()
        folder, path, miss = dm.navigate_folder_path(proj.rootFolder, "  /Parts/ / Fixtures/ ")
        assert miss is None and folder.name == "Fixtures" and path == "Parts/Fixtures"

    def test_empty_path_is_the_root_itself(self):
        proj = _sample_project()
        folder, path, miss = dm.navigate_folder_path(proj.rootFolder, "")
        assert miss is None and folder is proj.rootFolder and path == ""

    def test_a_miss_names_the_segment_where_it_stopped_and_the_siblings(self):
        proj = _sample_project()
        folder, path, miss = dm.navigate_folder_path(proj.rootFolder, "Parts/Nope")
        assert folder is None and path is None
        assert miss["segment"] == "Nope" and miss["at"] == "Parts"
        assert miss["available"] == ["Fixtures"]

    def test_a_miss_at_the_root_names_the_root(self):
        proj = _sample_project()
        _folder, _path, miss = dm.navigate_folder_path(proj.rootFolder, "Nope")
        assert miss["at"] == "(project root)"
        assert set(miss["available"]) == {"Workflow Templates", "Parts"}

    def test_an_unreadable_folder_reports_available_None_not_an_empty_list(self):
        # A folder whose dataFolders access raises is a HOLE in the search space: the segment may
        # be sitting in a listing that never opened. Reporting [] would say the folder was looked
        # into and is childless - the same lie _walk_folder's truncated['unread'] exists to avoid.
        folder, _path, miss = dm.navigate_folder_path(
            FakeDataFolder("Root", folders_raise="boom"), "x")
        assert folder is None and miss["segment"] == "x"
        assert miss["available"] is None

    def test_a_genuinely_childless_folder_reports_an_empty_list(self):
        # the other side of the same distinction: the walk DID look, and there is nothing there.
        _folder, _path, miss = dm.navigate_folder_path(FakeDataFolder("Empty"), "x")
        assert miss["available"] == []

    def test_the_listing_refusal_says_unread_not_none_when_the_walk_could_not_look(self, cloud):
        # the consumer side: '(none)' would publish an unread folder as an empty one.
        blind = FakeDataFolder("Root", files_raise="boom", folders_raise="boom", is_root=True)
        cloud(FakeDataProject("CAM", project_id="proj-cam-id", root_folder=blind))
        msg = error_message(dm.list_project_files_handler(project="CAM", folder="Nope"))
        assert "could not be read" in msg and "is unknown" in msg
        assert "(none)" not in msg
        assert "not found" not in msg          # a verdict this walk never reached


# ── list_project_files_handler: project resolution ─────────────────────────

class TestProjectResolution:
    def test_by_name_case_insensitive(self, cloud):
        cloud(_sample_project())
        out = _payload(dm.list_project_files_handler(project="cam"))
        assert out["project"]["name"] == "CAM"

    def test_by_id(self, cloud):
        proj = _sample_project()
        proj.id = "proj-cam-id"
        cloud(proj)
        out = _payload(dm.list_project_files_handler(project_id="proj-cam-id"))
        assert out["project"]["id"] == "proj-cam-id"

    def test_missing_identifier_errors(self, cloud):
        cloud(_sample_project())
        res = dm.list_project_files_handler()
        assert res["isError"] is True
        assert "either 'project'" in res["message"]

    def test_unknown_project_lists_available(self, cloud):
        cloud(_sample_project())
        res = dm.list_project_files_handler(project="Nope")
        assert res["isError"] is True
        assert "Project not found: Nope" in res["message"]
        assert "CAM" in res["message"]  # available list surfaced


# ── list_project_files_handler: whole-project (no folder) ──────────────────

class TestWholeProject:
    def test_lists_all_files_recursively(self, cloud):
        cloud(_sample_project())
        out = _payload(dm.list_project_files_handler(project="CAM"))
        names = {f["name"] for f in out["files"]}
        # root + templates(2) + nested fixture = 4 files total
        assert names == {"RootPart", "Template A", "Template B", "Vise"}
        assert out["folder"] == "(whole project)"
        assert out["file_count"] == 4

    def test_nested_file_records_its_path(self, cloud):
        cloud(_sample_project())
        out = _payload(dm.list_project_files_handler(project="CAM"))
        vise = next(f for f in out["files"] if f["name"] == "Vise")
        assert vise["folder_path"] == "Parts/Fixtures"


# ── list_project_files_handler: folder scoping ─────────────────────────────

class TestFolderScoping:
    def test_scopes_to_named_folder_only(self, cloud):
        cloud(_sample_project())
        out = _payload(dm.list_project_files_handler(
            project="CAM", folder="Workflow Templates", recursive=False))
        names = {f["name"] for f in out["files"]}
        assert names == {"Template A", "Template B"}    # NOT RootPart / Vise
        assert out["folder"] == "Workflow Templates"
        assert out["file_count"] == 2

    def test_folder_is_case_insensitive(self, cloud):
        cloud(_sample_project())
        out = _payload(dm.list_project_files_handler(
            project="CAM", folder="workflow templates", recursive=False))
        assert out["file_count"] == 2

    def test_nested_folder_path(self, cloud):
        cloud(_sample_project())
        out = _payload(dm.list_project_files_handler(
            project="CAM", folder="Parts/Fixtures", recursive=False))
        names = {f["name"] for f in out["files"]}
        assert names == {"Vise"}
        assert out["folder"] == "Parts/Fixtures"

    def test_recursive_true_descends_into_subfolders(self, cloud):
        # 'Parts' has no direct files but its 'Fixtures' subfolder does;
        # recursive=True should reach the nested file.
        cloud(_sample_project())
        out = _payload(dm.list_project_files_handler(
            project="CAM", folder="Parts", recursive=True))
        names = {f["name"] for f in out["files"]}
        assert names == {"Vise"}

    def test_recursive_false_immediate_only(self, cloud):
        # 'Parts' has no direct files; immediate-only should return nothing,
        # NOT descend into Fixtures.
        cloud(_sample_project())
        out = _payload(dm.list_project_files_handler(
            project="CAM", folder="Parts", recursive=False))
        assert out["file_count"] == 0

    def test_stray_slashes_tolerated(self, cloud):
        cloud(_sample_project())
        out = _payload(dm.list_project_files_handler(
            project="CAM", folder="/Workflow Templates/", recursive=False))
        assert out["file_count"] == 2

    def test_missing_folder_errors_with_hint(self, cloud):
        cloud(_sample_project())
        res = dm.list_project_files_handler(project="CAM", folder="Ghost")
        assert res["isError"] is True
        assert "Folder 'Ghost' not found" in res["message"]
        # the hint lists real sibling folders at that level
        assert "Workflow Templates" in res["message"]
        assert "Parts" in res["message"]

    def test_missing_nested_segment_names_the_level(self, cloud):
        cloud(_sample_project())
        res = dm.list_project_files_handler(project="CAM", folder="Parts/Ghost")
        assert res["isError"] is True
        assert "no subfolder 'Ghost' in 'Parts'" in res["message"]


# ── _file_summary: per-file fields + guarded getters ───────────────────────

class TestUnreadableFolders:
    """A folder whose enumeration RAISES is a hole in the search space, not an empty folder. The
    same listing resolves a file BY NAME, so swallowing the failure turns an ambiguity into a
    confident unique match - the listing has to say a folder went unread."""

    def _project_with_a_dead_folder(self):
        dead = FakeDataFolder("Archive", files_raise="3 : folder could not be enumerated")
        return make_data_tree(name="CAM", files=[_file("RootPart", "urn:lin:ROOT")],
                              folders=[dead])

    def test_the_listing_names_and_counts_the_unread_folder(self, cloud):
        cloud(self._project_with_a_dead_folder())
        out = _payload(dm.list_project_files_handler(project="CAM"))
        assert out["folders_unreadable"] == 1
        assert out["folders_unreadable_at"] == ["Archive"]
        assert out["file_count"] == 1                  # the readable half still lands

    def test_a_fully_readable_project_publishes_no_such_key(self, cloud):
        cloud(_sample_project())
        out = _payload(dm.list_project_files_handler(project="CAM"))
        assert "folders_unreadable" not in out
        assert "folders_unreadable_at" not in out


class TestFileSummary:
    def test_all_fields_populated(self):
        out = dm._file_summary(_file("Widget", "urn:lin:XYZ"), "Parts/Fixtures")
        assert out["name"] == "Widget"
        assert out["id"] == "urn:lin:XYZ"
        assert out["versionId"] == "urn:lin:XYZ?version=1"
        assert out["fileExtension"] == "f3d"
        assert out["versionNumber"] == 1
        assert out["fusionWebURL"] == "https://example/urn:lin:XYZ"
        assert out["folder_path"] == "Parts/Fixtures"

    def test_empty_folder_path_becomes_project_root(self):
        out = dm._file_summary(_file("R", "urn:lin:R"), "")
        assert out["folder_path"] == "(project root)"

    def test_broken_getter_yields_none_not_crash(self):
        class _UnreadableIdFile(FakeDataFile):
            """A cloud file whose lineage id will not read while its other fields answer."""
            id = _unreadable("id")

        out = dm._file_summary(_UnreadableIdFile("Half", version=2), "")
        assert out["name"] == "Half"
        assert out["id"] is None          # guarded: failed getter -> None
        assert out["versionNumber"] == 2


# ── truncation cap (_MAX_FILES) ────────────────────────────────────────────

class TestTruncation:
    def test_whole_project_truncates_at_max_files(self, cloud):
        # Build more files than the cap so the walk stops and flags truncated.
        cap = dm._MAX_FILES
        many = [_file(f"F{i}", f"urn:lin:{i}") for i in range(cap + 5)]
        cloud(make_data_tree(name="Big", files=many))
        out = _payload(dm.list_project_files_handler(project="Big"))
        assert out["file_count"] == cap
        assert out["truncated"] is True

    def test_recursive_field_always_true_for_whole_project(self, cloud):
        # No folder given -> the reported 'recursive' is True regardless of the arg.
        cloud(_sample_project())
        out = _payload(dm.list_project_files_handler(project="CAM", recursive=False))
        assert out["recursive"] is True
        assert out["folder"] == "(whole project)"

    def test_folder_visit_budget_stops_a_wide_walk(self, cloud, monkeypatch):
        # A wide tree with few files per folder never trips the FILE cap, yet each folder is a
        # main-thread cloud fetch - so the VISIT budget must stop the walk and flag truncated.
        subs = [FakeDataFolder(f"Sub{i}", files=[_file(f"F{i}", f"urn:lin:{i}")])
                for i in range(8)]
        cloud(make_data_tree(name="Wide", folders=subs))
        monkeypatch.setattr(dm, "_MAX_FOLDER_VISITS", 2)   # root + exactly one subfolder
        out = _payload(dm.list_project_files_handler(project="Wide"))
        assert out["truncated"] is True
        # root(visit 1, no files) + Sub0(visit 2, one file); Sub1.. exceed the budget and are skipped
        assert out["file_count"] == 1

    def test_visit_budget_not_tripped_within_budget(self, cloud):
        # the sample project (root + 2 folders + 1 nested = 4 visits) is under the default budget.
        cloud(_sample_project())
        out = _payload(dm.list_project_files_handler(project="CAM"))
        assert out["truncated"] is False
        assert out["file_count"] == 4


# ── wall-clock time budget (_TIME_BUDGET_S) ────────────────────────────────
#
# A transient network stall can hang a single cloud round-trip past normal latency, on item #1 of a
# small project - the item-COUNT caps above never catch this. time.monotonic() is monkeypatched with a
# scripted sequence of return values (rather than a real sleep) so the deadline can be crossed
# deterministically after a chosen number of between-item checks.

def _scripted_clock(monkeypatch, mod, values):
    """Patch mod.time.monotonic to return `values` in order, holding the last value for any call past
    the end of the list (so a walk that keeps checking after the deadline stays 'stalled')."""
    idx = {"i": 0}

    def fake_monotonic():
        v = values[min(idx["i"], len(values) - 1)]
        idx["i"] += 1
        return v
    monkeypatch.setattr(mod.time, "monotonic", fake_monotonic)


class TestFilesWalkTimeBudget:
    def test_stops_partway_and_flags_time_truncated(self, cloud, monkeypatch):
        # A flat folder of 3 files. Scripted clock: call#1 sets the deadline at t0; call#2 (the walk's
        # own folder-visit check) and call#3 (the check before file 0) both land AT t0 (not exceeded,
        # so file 0 is collected); call#4 (the check before file 1) lands past the deadline - the walk
        # must stop there, never reading file 1 or file 2.
        t0 = 1000.0
        cloud(make_data_tree(name="Stall",
                             files=[_file(f"F{i}", f"urn:lin:{i}") for i in range(3)]))
        _scripted_clock(monkeypatch, dm, [t0, t0, t0, t0 + dm._TIME_BUDGET_S + 1])

        out = _payload(dm.list_project_files_handler(project="Stall"))
        assert out["time_truncated"] is True
        assert out["truncated"] is True
        assert out["file_count"] == 1                      # only F0 landed before the stall
        assert {f["name"] for f in out["files"]} == {"F0"}  # partial results present, not empty
        assert out["time_truncated_at"] == "(project root)"

    def test_stops_partway_through_a_subfolder(self, cloud, monkeypatch):
        # The stall happens while walking a NAMED subfolder - time_truncated_at must name it, not the
        # project root, so the caller knows exactly where to retry/narrow.
        t0 = 2000.0
        inner = FakeDataFolder("Inner", files=[_file(f"G{i}", f"urn:lin:g{i}") for i in range(2)])
        cloud(make_data_tree(name="Stall2", folders=[inner]))
        # calls: deadline calc, root-visit check(ok, no root files), subfolder-loop check for
        # 'Inner'(ok, recurse in), Inner-visit check(ok), Inner file0 check(ok), Inner file1 check(stall)
        _scripted_clock(monkeypatch, dm,
                        [t0, t0, t0, t0, t0, t0 + dm._TIME_BUDGET_S + 1])
        out = _payload(dm.list_project_files_handler(project="Stall2"))
        assert out["time_truncated"] is True
        assert out["time_truncated_at"] == "Inner"
        assert out["file_count"] == 1

    def test_fast_walk_has_no_time_truncated_flag(self, cloud):
        # No monkeypatched clock: the real, fast walk must report time_truncated=false and full
        # results - the flag must never fire on an ordinary call.
        cloud(_sample_project())
        out = _payload(dm.list_project_files_handler(project="CAM"))
        assert out["time_truncated"] is False
        assert "time_truncated_at" not in out
        assert out["file_count"] == 4                       # nothing lost

    def test_immediate_files_only_scope_also_honors_the_budget(self, cloud, monkeypatch):
        # folder=<path> with recursive=false takes the OTHER loop (not _walk_folder) - it must be
        # budgeted too.
        t0 = 3000.0
        named = FakeDataFolder("Templates",
                               files=[_file(f"H{i}", f"urn:lin:h{i}") for i in range(3)])
        cloud(make_data_tree(name="Stall3", folders=[named]))
        # calls: deadline calc, then per-item checks in the immediate-files loop: item0 ok, item1 stall
        _scripted_clock(monkeypatch, dm, [t0, t0, t0 + dm._TIME_BUDGET_S + 1])
        out = _payload(dm.list_project_files_handler(
            project="Stall3", folder="Templates", recursive=False))
        assert out["time_truncated"] is True
        assert out["file_count"] == 1
        assert out["time_truncated_at"] == "Templates"


class TestProjectsListingTimeBudget:
    def _projects(self, count):
        return [FakeDataProject(f"P{i}", project_id=f"id{i}") for i in range(count)]

    def test_stops_partway_and_flags_time_truncated(self, cloud, monkeypatch):
        t0 = 4000.0
        cloud(*self._projects(5))
        # calls: deadline calc, item0 check(ok), item1 check(ok), item2 check(stall)
        _scripted_clock(monkeypatch, dm, [t0, t0, t0, t0 + dm._TIME_BUDGET_S + 1])
        out = _payload(dm.list_projects_handler())
        assert out["time_truncated"] is True
        assert out["project_count"] == 2
        assert {p["name"] for p in out["projects"]} == {"P0", "P1"}

    def test_fast_listing_has_no_time_truncated_flag(self, cloud):
        cloud(*self._projects(3))
        out = _payload(dm.list_projects_handler())
        assert out["time_truncated"] is False
        assert out["project_count"] == 3


# ── file_facts_handler: ONE file's record (data_get(file=...)) ──────────────
#
# Resolution is stubbed out here (it is _data_common's job, covered in test__data_common.py);
# what is pinned is the PROJECTION: which fields land, users flattened, dates converted, and the two
# link reads - one that is safe while unshared, one that RAISES while unshared.

class _CloudFile(FakeDataFile):
    """A cloud file carrying the metadata the facts read projects on top of the shared file's
    identity: the description, dates, users, state flags and the two link reads. publicLink is a
    property because its measured state on an unshared file is a RAISE, which only a property can
    do."""

    def __init__(self, public_link=None, description="a note", date_created=None,
                 date_modified=None, created_by=None, last_updated_by=None, shared_link=None,
                 is_read_only=False, is_in_use=False, **kwargs):
        super().__init__(**kwargs)
        self.description = description
        self.dateCreated = date_created
        self.dateModified = date_modified
        self.createdBy = created_by
        self.lastUpdatedBy = last_updated_by
        self.sharedLink = shared_link
        self.isReadOnly = is_read_only
        self.isInUse = is_in_use
        self._public = public_link

    @property
    def publicLink(self):
        if isinstance(self._public, Exception):
            raise self._public
        return self._public


class _UnreadableLinkFile(_CloudFile):
    """A cloud file whose sharedLink and version pair will not READ - both the link state and the
    freshness verdict have to answer unknown rather than guess."""
    sharedLink = _unreadable("sharedLink")
    versionNumber = _unreadable("versionNumber")
    latestVersionNumber = _unreadable("latestVersionNumber")


def _ns(**kw):
    return SimpleNamespace(**kw)


def _unshared_link():
    return _ns(isShared=False, linkURL="", isDownloadAllowed=True, isPasswordRequired=False)


def _full_file(cls=_CloudFile, **overrides):
    """The file every facts projection is read off: identity, the v2-of-3 version pair, dates,
    users, location, and an UNSHARED link pair - the state whose publicLink raises."""
    fields = dict(
        name="probe_note.txt", file_id="urn:lin:AAA", version_id="urn:lin:AAA?version=2",
        extension="sql", web_url="https://example/AAA", version=2, latest_version=3,
        versions=[FakeDataFile("probe_note.txt", version=n) for n in (1, 2, 3)],
        date_created=1783893584, date_modified=1783893999,
        created_by=_ns(displayName="Ada L", userName="ada", email="ada@example.com"),
        last_updated_by=_ns(displayName="Bob K", userName="bob", email="bob@example.com"),
        parent_folder=FakeDataFolder("Docs"),
        parent_project=FakeDataProject("Sample Project", project_id="proj-1"),
        shared_link=_unshared_link(),
        public_link=RuntimeError("3 : No public link available. Use sharedLink.isShared to "
                                 "create a public link."),
    )
    fields.update(overrides)
    return cls(**fields)


@pytest.fixture
def resolves(monkeypatch):
    """Point the facts read at a given DataFile stand-in (or a resolution error)."""
    def _use(df=None, err=None, meta=None):
        monkeypatch.setattr(dm, "resolve_file_reference",
                            lambda *a, **kw: (df, meta or {"matched_by": "urn"}, err))
    return _use


class TestFileFacts:
    def test_projects_the_record_a_caller_acts_on(self, resolves):
        resolves(_full_file())
        out = _payload(dm.file_facts_handler(file="urn:lin:AAA"))
        assert out["file"]["name"] == "probe_note.txt"
        assert out["file"]["id"] == "urn:lin:AAA"
        assert out["version"]["number"] == 2 and out["version"]["latest_number"] == 3
        assert out["version"]["is_latest"] is False        # v2 of 3 - not the tip
        assert out["version"]["version_count"] == 3
        assert out["location"]["project"]["name"] == "Sample Project"
        assert out["location"]["parent_folder"]["path"] == "Docs"
        assert out["state"] == {"is_read_only": False, "is_in_use": False, "is_complete": True}

    def test_latest_version_reads_as_latest(self, resolves):
        resolves(_full_file(version=3))
        out = _payload(dm.file_facts_handler(file="urn:lin:AAA"))
        assert out["version"]["is_latest"] is True

    def test_users_are_flattened_to_their_three_fields(self, resolves):
        resolves(_full_file())
        out = _payload(dm.file_facts_handler(file="urn:lin:AAA"))
        assert out["created_by"] == {"display_name": "Ada L", "user_name": "ada",
                                     "email": "ada@example.com"}
        assert out["last_updated_by"]["user_name"] == "bob"

    def test_dates_carry_both_the_raw_epoch_and_the_utc_iso_string(self, resolves):
        import datetime
        resolves(_full_file())
        out = _payload(dm.file_facts_handler(file="urn:lin:AAA"))
        assert out["dates"]["created_unix"] == 1783893584
        assert out["dates"]["modified_unix"] == 1783893999
        # The ISO string must be the SAME instant in UTC - a local-time conversion round-trips to a
        # different epoch, which is exactly what publishing the raw value beside it exposes.
        iso = out["dates"]["created_iso"]
        assert iso.endswith("Z")
        back = datetime.datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=datetime.timezone.utc)
        assert int(back.timestamp()) == 1783893584

    def test_an_unreadable_date_is_null_not_a_fabricated_epoch(self, resolves):
        resolves(_full_file(date_created=None))
        out = _payload(dm.file_facts_handler(file="urn:lin:AAA"))
        assert out["dates"]["created_unix"] is None and out["dates"]["created_iso"] is None

    def test_unshared_file_reports_link_state_without_leaking_an_empty_url(self, resolves):
        resolves(_full_file())
        out = _payload(dm.file_facts_handler(file="urn:lin:AAA"))
        assert out["shared_link"]["is_shared"] is False
        assert "link_url" not in out["shared_link"]        # the binding returns '' when unshared
        assert out["shared_link"]["is_download_allowed"] is True
        assert out["shared_link"]["is_password_required"] is False

    def test_shared_file_publishes_the_link_url(self, resolves):
        resolves(_full_file(shared_link=_ns(isShared=True, linkURL="https://a360/x",
                                            isDownloadAllowed=False, isPasswordRequired=True)))
        out = _payload(dm.file_facts_handler(file="urn:lin:AAA"))
        assert out["shared_link"]["link_url"] == "https://a360/x"
        assert out["shared_link"]["is_password_required"] is True

    def test_the_raising_public_link_is_caught_and_reported_not_sunk(self, resolves):
        # publicLink RAISES on an unshared file - the whole read must still succeed, carrying the
        # reason rather than a bare false.
        resolves(_full_file())
        result = dm.file_facts_handler(file="urn:lin:AAA")
        out = _payload(result)
        assert out["public_link"]["available"] is False
        assert "No public link available" in out["public_link"]["reason"]

    def test_a_present_public_link_is_published(self, resolves):
        resolves(_full_file(public_link="https://a360.co/abc"))
        out = _payload(dm.file_facts_handler(file="urn:lin:AAA"))
        assert out["public_link"] == {"available": True, "url": "https://a360.co/abc"}

    def test_an_unreadable_shared_link_does_not_sink_the_read(self, resolves):
        resolves(_full_file(cls=_UnreadableLinkFile, name="x.txt",
                            public_link="https://a360.co/abc"))
        out = _payload(dm.file_facts_handler(file="urn:lin:AAA"))
        assert out["shared_link"] == {"readable": False}
        assert out["file"]["name"] == "x.txt"
        assert out["version"]["is_latest"] is None         # unknown, not a guessed True

    def test_a_name_matched_over_unread_folders_publishes_the_hole_count(self, resolves):
        # The resolver counts folders whose enumeration RAISED; publishing only the cap flag leaves
        # an agent reading name_scope_truncated=false on a resolution that skipped folders entirely.
        resolves(_full_file(), meta={"matched_by": "name", "folders_unreadable": 2})
        out = _payload(dm.file_facts_handler(file="probe_note.txt", project="P"))
        assert out["name_scope_folders_unreadable"] == 2
        assert out["name_scope_truncated"] is False    # a DIFFERENT hole - the cap never tripped

    def test_a_match_over_a_fully_read_scope_publishes_zero_holes(self, resolves):
        resolves(_full_file(), meta={"matched_by": "name", "folders_unreadable": 0})
        out = _payload(dm.file_facts_handler(file="probe_note.txt", project="P"))
        assert out["name_scope_folders_unreadable"] == 0

    def test_a_resolution_error_is_returned_verbatim(self, resolves):
        resolves(err="'notes.txt' names 2 files in project 'P1' - refusing to guess which")
        res = dm.file_facts_handler(file="notes.txt", project="P1")
        assert "names 2 files" in error_message(res)


class TestListFolders:
    def _tree(self):
        fixtures = FakeDataFolder("Fixtures", folder_id="fid:Fixtures")
        parts = FakeDataFolder("Parts", folder_id="fid:Parts", folders=[fixtures])
        templates = FakeDataFolder("Templates", folder_id="fid:Templates")
        return make_data_tree(name="Proj", folders=[parts, templates])

    def test_lists_tree_with_paths(self, cloud):
        cloud(self._tree())
        out = _payload(dm.list_folders_handler(project="Proj"))
        top = {n["name"]: n for n in out["folders"]}
        assert set(top) == {"Parts", "Templates"}
        assert top["Parts"]["path"] == "Parts"
        # nested folder appears under Parts with full path
        nested = top["Parts"]["folders"][0]
        assert nested["name"] == "Fixtures" and nested["path"] == "Parts/Fixtures"
        assert out["folder_count"] == 3

    def test_max_depth_clamped_to_at_least_one(self, cloud):
        cloud(self._tree())
        out = _payload(dm.list_folders_handler(project="Proj", max_depth=0))
        # clamped to 1 -> top-level folders only. Whether a depth-capped folder has children is
        # UNKNOWN (checking would cost a cloud fetch) - flagged children_unknown, on every capped
        # node, never a guessed 'no children'.
        assert out["max_depth"] == 1
        top = {n["name"]: n for n in out["folders"]}
        assert top["Parts"].get("children_unknown") is True
        assert "folders" not in top["Parts"]

    def test_invalid_max_depth_defaults(self, cloud):
        cloud(self._tree())
        out = _payload(dm.list_folders_handler(project="Proj", max_depth="oops"))
        assert out["max_depth"] == 4

    def test_within_budget_is_not_truncated(self, cloud):
        cloud(self._tree())
        out = _payload(dm.list_folders_handler(project="Proj"))
        assert out["truncated"] is False

    def test_folder_budget_cuts_the_walk_and_flags_it(self, cloud, monkeypatch):
        # every dataFolders fetch is a slow MAIN-THREAD cloud round-trip (a large project's walk
        # can stall Fusion past the 30 s handler cap, live-verified) - the walk must stop at the
        # budget, report truncated=true, and mark each unexpanded node folders_truncated so the
        # caller knows WHICH subtrees were cut, not just that something was.
        subs = [FakeDataFolder(f"Sub{i}", folders=[FakeDataFolder(f"Sub{i}Deep")])
                for i in range(4)]
        cloud(make_data_tree(name="Proj", folders=subs))
        monkeypatch.setattr(dm, "_LF_FOLDER_BUDGET", 2)   # root + Sub0 only
        out = _payload(dm.list_folders_handler(project="Proj"))
        assert out["truncated"] is True
        top = {n["name"]: n for n in out["folders"]}
        assert set(top) == {"Sub0", "Sub1", "Sub2", "Sub3"}   # breadth-first: all shallow nodes land
        assert top["Sub0"]["folders"][0]["name"] == "Sub0Deep"  # the one budgeted fetch descended
        # the three unexpanded siblings are each flagged - their subtrees were NOT searched
        for name in ("Sub1", "Sub2", "Sub3"):
            assert top[name].get("folders_truncated") is True
            assert "folders" not in top[name]

    def test_time_budget_cuts_the_walk_and_flags_it(self, cloud, monkeypatch):
        # A transient network stall can hang a single dataFolders fetch past normal latency - unlike
        # the fetch-COUNT budget above, this exercises the WALL-CLOCK deadline (checked between folder
        # visits, since an in-flight fetch can't be interrupted). time.monotonic() is scripted rather
        # than really slept.
        cloud(make_data_tree(name="Proj",
                             folders=[FakeDataFolder(f"Sub{i}") for i in range(4)]))
        t0 = 5000.0
        # calls: 1) deadline calc, 2) root-visit check(ok), 3) Sub0-visit check(ok, no children),
        # 4) Sub1-visit check(stall) - Sub2/Sub3 never even get fetched.
        _scripted_clock(monkeypatch, dm, [t0, t0, t0, t0 + dm._TIME_BUDGET_S + 1])

        out = _payload(dm.list_folders_handler(project="Proj"))
        assert out["truncated"] is True
        assert out["time_truncated"] is True
        top = {n["name"]: n for n in out["folders"]}
        assert set(top) == {"Sub0", "Sub1", "Sub2", "Sub3"}
        # Sub0 was actually fetched (visited before the stall) and had no children - a genuine leaf,
        # not a truncation.
        assert top["Sub0"].get("folders_truncated") is None
        # Sub1 onward were never fetched once the deadline was crossed.
        for name in ("Sub1", "Sub2", "Sub3"):
            assert top[name].get("folders_truncated") is True

    def test_time_budget_not_tripped_on_a_fast_walk(self, cloud):
        cloud(self._tree())
        out = _payload(dm.list_folders_handler(project="Proj"))
        assert out["time_truncated"] is False

    def test_an_unreadable_folder_is_flagged_on_its_node_and_counted(self, cloud):
        # the fetch RAISES: the node is neither a leaf nor a budget cut, and a caller proving
        # absence needs the counterpart of the flat listing's folders_unreadable.
        dead = FakeDataFolder("Archive", folder_id="fid:Archive",
                              folders_raise="3 : folder could not be enumerated")
        cloud(make_data_tree(name="Proj", folders=[dead, FakeDataFolder("Leaf", folder_id="fid:L")]))
        out = _payload(dm.list_folders_handler(project="Proj"))
        top = {n["name"]: n for n in out["folders"]}
        assert top["Archive"]["children_unreadable"] is True
        assert "folders" not in top["Archive"]
        assert top["Leaf"].get("children_unreadable") is None    # a genuine leaf stays a leaf
        assert out["folders_unreadable"] == 1
        assert out["folders_unreadable_at"] == ["Archive"]
        assert out["truncated"] is False and out["time_truncated"] is False

    def test_a_root_that_will_not_enumerate_is_counted_at_the_project_root(self, cloud):
        cloud(FakeDataProject("Proj", root_folder=FakeDataFolder(
            "Root", is_root=True, folders_raise="3 : root would not enumerate")))
        out = _payload(dm.list_folders_handler(project="Proj"))
        assert out["folders"] == [] and out["folder_count"] == 0
        assert out["folders_unreadable"] == 1
        assert out["folders_unreadable_at"] == ["(project root)"]

    def test_a_fully_readable_tree_publishes_no_unreadable_key(self, cloud):
        cloud(self._tree())
        out = _payload(dm.list_folders_handler(project="Proj"))
        assert "folders_unreadable" not in out and "folders_unreadable_at" not in out

    def test_walk_is_breadth_first_shallow_before_deep(self, cloud, monkeypatch):
        # a deep chain must not eat the budget before the shallow siblings are even listed.
        chain = FakeDataFolder("A", folders=[FakeDataFolder(
            "A1", folders=[FakeDataFolder("A2", folders=[FakeDataFolder("A3")])])])
        cloud(make_data_tree(name="Proj", folders=[chain, FakeDataFolder("B"),
                                                  FakeDataFolder("C")]))
        monkeypatch.setattr(dm, "_LF_FOLDER_BUDGET", 3)   # root + A + B (never reaches A1's child)
        out = _payload(dm.list_folders_handler(project="Proj", max_depth=6))
        top = {n["name"]: n for n in out["folders"]}
        assert set(top) == {"A", "B", "C"}                # every shallow folder listed first
        assert out["truncated"] is True


class TestFolderTreeScope:
    """A project-wide tree read spends its whole budget on the top of a real project, so the tree
    takes the same 'folder' scope the file listing does - walking only under that path."""

    def _tree(self):
        vises = FakeDataFolder("Vises", folder_id="fid:Vises")
        fixtures = FakeDataFolder("Fixtures", folder_id="fid:Fixtures", folders=[vises])
        parts = FakeDataFolder("Parts", folder_id="fid:Parts", folders=[fixtures])
        return make_data_tree(name="Proj", folders=[parts,
                                                    FakeDataFolder("Templates", folder_id="fid:T")])

    def test_the_walk_starts_at_the_named_folder(self, cloud):
        cloud(self._tree())
        out = _payload(dm.list_folders_handler(project="Proj", folder="Parts"))
        assert out["folder"] == "Parts"
        assert [n["name"] for n in out["folders"]] == ["Fixtures"]   # NOT Templates, NOT Parts
        assert out["folder_count"] == 2                              # Fixtures + Vises

    def test_paths_stay_project_absolute_so_they_can_be_passed_back(self, cloud):
        cloud(self._tree())
        out = _payload(dm.list_folders_handler(project="Proj", folder="Parts"))
        assert out["folders"][0]["path"] == "Parts/Fixtures"
        assert out["folders"][0]["folders"][0]["path"] == "Parts/Fixtures/Vises"

    def test_no_folder_scopes_to_the_whole_project(self, cloud):
        cloud(self._tree())
        out = _payload(dm.list_folders_handler(project="Proj"))
        assert out["folder"] == "(project root)"
        assert {n["name"] for n in out["folders"]} == {"Parts", "Templates"}

    def test_a_scope_that_is_not_there_is_refused_with_its_siblings(self, cloud):
        cloud(self._tree())
        res = dm.list_folders_handler(project="Proj", folder="Ghost")
        assert res["isError"] is True
        assert "Folder 'Ghost' not found" in res["message"] and "Parts" in res["message"]

    def test_a_scope_whose_sibling_list_will_not_read_says_so(self, cloud):
        # 'not found' would be a verdict this walk never reached - the refusals differ.
        cloud(FakeDataProject("Proj", root_folder=FakeDataFolder(
            "Root", is_root=True, folders_raise="3 : would not enumerate")))
        res = dm.list_folders_handler(project="Proj", folder="Parts")
        assert res["isError"] is True and "could not be resolved" in res["message"]


class TestWalkBudgetsAreInputs:
    """The walk's limits are the caller's to size: a caller proving a folder is absent needs a read
    that reaches, and one that cannot has to say which limit stopped it."""

    def _chain(self):
        return make_data_tree(name="Proj", folders=[FakeDataFolder(
            "A", folders=[FakeDataFolder("A1", folders=[FakeDataFolder("A2")])])])

    def test_the_default_is_published_when_no_budget_is_given(self, cloud):
        cloud(self._chain())
        out = _payload(dm.list_folders_handler(project="Proj"))
        assert out["folder_budget"] == dm._LF_FOLDER_BUDGET
        assert out["time_budget_s"] == dm._TIME_BUDGET_S

    def test_a_budget_of_one_fetches_the_root_only_and_flags_the_rest(self, cloud):
        cloud(self._chain())
        out = _payload(dm.list_folders_handler(project="Proj", max_depth=6, folder_budget=1))
        assert out["folder_budget"] == 1 and out["truncated"] is True
        assert out["folders"][0]["name"] == "A"
        assert out["folders"][0]["folders_truncated"] is True
        # a budget cut is a folder NOT REACHED, not a folder that would not read: counting it as
        # unreadable would tell a caller its tree has holes the cloud put there.
        assert "folders_unreadable" not in out
        assert "children_unreadable" not in out["folders"][0]

    def test_a_budget_of_zero_is_clamped_to_one_and_published_as_used(self, cloud):
        cloud(self._chain())
        out = _payload(dm.list_folders_handler(project="Proj", max_depth=6, folder_budget=0))
        assert out["folder_budget"] == 1 and out["truncated"] is True

    def test_a_budget_over_the_ceiling_is_clamped_to_it(self, cloud):
        cloud(self._chain())
        out = _payload(dm.list_folders_handler(project="Proj", folder_budget=10 ** 6))
        assert out["folder_budget"] == dm._LF_FOLDER_BUDGET_MAX

    def test_a_bigger_budget_reaches_past_the_default(self, cloud, monkeypatch):
        cloud(self._chain())
        monkeypatch.setattr(dm, "_LF_FOLDER_BUDGET", 1)      # the default alone cuts at the root
        assert _payload(dm.list_folders_handler(project="Proj", max_depth=6))["truncated"] is True
        out = _payload(dm.list_folders_handler(project="Proj", max_depth=6, folder_budget=8))
        assert out["truncated"] is False and out["folder_count"] == 3

    def test_a_non_numeric_budget_takes_the_default(self, cloud):
        cloud(self._chain())
        out = _payload(dm.list_folders_handler(project="Proj", folder_budget="lots"))
        assert out["folder_budget"] == dm._LF_FOLDER_BUDGET

    def test_a_nan_budget_takes_the_default_not_the_floor(self):
        # NaN compares False against everything, so an unguarded max(1.0, min(nan, ceiling))
        # returns 1.0 - the SMALLEST budget - for a caller who asked for nothing measurable.
        assert dm._budget(float("nan"), 20, 120) == 20.0
        assert dm._budget(float("inf"), 20, 120) == 120.0

    def test_the_time_budget_is_the_deadline_the_walk_stops_at(self, cloud, monkeypatch):
        cloud(self._chain())
        t0 = 6000.0
        # calls: deadline calc, root check(ok), then A's check lands past the 5 s asked for
        _scripted_clock(monkeypatch, dm, [t0, t0, t0 + 6])
        out = _payload(dm.list_folders_handler(project="Proj", max_depth=6, time_budget_s=5))
        assert out["time_budget_s"] == 5.0
        assert out["time_truncated"] is True and out["truncated"] is True
        assert "folders_unreadable" not in out          # a stall is not an unreadable folder

    def test_the_time_budget_is_clamped_at_both_ends(self, cloud):
        cloud(self._chain())
        assert _payload(dm.list_folders_handler(
            project="Proj", time_budget_s=0))["time_budget_s"] == 1.0
        assert _payload(dm.list_folders_handler(
            project="Proj", time_budget_s=10 ** 6))["time_budget_s"] == dm._TIME_BUDGET_MAX_S

    def test_the_file_walk_takes_the_same_time_budget(self, cloud):
        cloud(_sample_project())
        out = _payload(dm.list_project_files_handler(project="CAM", time_budget_s=45))
        assert out["time_budget_s"] == 45.0 and out["file_count"] == 4

    def test_the_file_walks_budget_is_clamped_and_published(self, cloud):
        cloud(_sample_project())
        out = _payload(dm.list_project_files_handler(project="CAM", time_budget_s=0))
        assert out["time_budget_s"] == 1.0
