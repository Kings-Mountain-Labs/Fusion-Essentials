"""Tests for `data_get` — the cloud rich read (hub/projects/folders/files), scope-driven.

Pins the ROUTER's scope dispatch: no project -> projects; project -> files; project+include=['folders']
-> folder tree; include=['hubs'] -> hubs; and the unknown-include guard + cloud-error propagation. The
delegated handlers (_data_read/data_switch_hub) are swapped through conftest's
`stub_tool_module`, which holds under either import order (see TestDeferredImportSeam); their own
cloud logic + caps are covered by their tests and by live validation.
"""

import json

import pytest

from conftest import FakeDataFolder, error_message, load_tool, stub_tool_module

dge = load_tool("data_get")
# The real folder walk the router hands its depth to. Loaded here, before any test stubs
# 'mcpServer.tools._data_read' in sys.modules, so the depth test drives the walk and not a stub.
dops = load_tool("_data_read")


def _payload(result):
    assert result["isError"] is False, result
    return json.loads(result["content"][0]["text"])


def _ok(payload):
    return {"isError": False, "content": [{"type": "text", "text": json.dumps(payload)}]}


def _err(msg):
    return {"isError": True, "message": msg}


@pytest.fixture
def stub(monkeypatch):
    stub_tool_module(monkeypatch, "_data_read",
        type("DR", (), {
            "list_projects_handler": staticmethod(lambda: _ok({"active_hub": "Main", "project_count": 2,
                                                               "projects": [{"name": "P1"}, {"name": "P2"}]})),
            "list_project_files_handler": staticmethod(lambda **kw: _ok({"project": {"name": kw.get("project")},
                                                                        "file_count": 3, "files": ["a", "b", "c"]})),
            "file_facts_handler": staticmethod(lambda **kw: _ok({"matched_by": "urn",
                                                                 "file": {"name": "notes.txt"},
                                                                 "seen": dict(kw)})),
            "list_folders_handler": staticmethod(
                lambda **kw: _ok({"project": kw.get("project"), "folder_count": 4,
                                  "folders": ["f1", "f2"]})),
        }))
    stub_tool_module(monkeypatch, "data_switch_hub",
        type("DH", (), {"handler": staticmethod(
            lambda action="list", hub="": _ok({"hub_count": 2, "hubs": [{"name": "H1", "is_active": True}]}))}))


class TestScopeDispatch:
    def test_default_lists_projects(self, stub):
        out = _payload(dge.handler())
        assert out["scope"] == "projects"
        assert out["project_count"] == 2
        assert "data_get" not in out["note"] or "doc_get" in out["note"]   # points to the session sibling

    def test_project_lists_files(self, stub):
        out = _payload(dge.handler(project="P1"))
        assert out["scope"] == "files"
        assert out["file_count"] == 3
        # a file listing's dominant next action is to open one -> doc_open breadcrumb.
        assert "doc_open" in out["pointers"]["open"]

    def test_project_with_folders_shows_tree(self, stub):
        out = _payload(dge.handler(project="P1", include=["folders"]))
        assert out["scope"] == "folders"
        assert out["folder_count"] == 4

    def test_truncated_folder_walk_gets_the_budget_note(self, stub, monkeypatch):
        # a budget-cut walk must TEACH the narrower next step (lower max_depth / scope with
        # 'folder'), not just flag truncated=true.
        stub_tool_module(monkeypatch, "_data_read",
            type("DO", (), {"list_folders_handler": staticmethod(
                lambda **kw: _ok({"project": "P1", "folder_count": 20, "truncated": True,
                                  "folders": []}))}))
        out = _payload(dge.handler(project="P1", include=["folders"]))
        assert "folder budget" in out["note"] and "folders_truncated" in out["note"]

    def test_untruncated_folder_walk_has_no_budget_note(self, stub):
        out = _payload(dge.handler(project="P1", include=["folders"]))
        assert "folder budget" not in out["note"]

    def test_time_truncated_files_walk_gets_the_time_note(self, stub, monkeypatch):
        # A time-budget-cut walk must TEACH the same kind of next step as a size-truncated one, and
        # must name WHERE it stopped.
        stub_tool_module(monkeypatch, "_data_read",
            type("DR", (), {
                "list_project_files_handler": staticmethod(lambda **kw: _ok({
                    "file_count": 1, "files": ["a"],
                    "time_truncated": True, "time_truncated_at": "Parts/Fixtures"})),
                "_TIME_BUDGET_S": 20.0,
            }))
        out = _payload(dge.handler(project="P1"))
        assert "time budget" in out["note"] and "Parts/Fixtures" in out["note"]

    def test_untruncated_files_walk_has_no_time_note(self, stub):
        out = _payload(dge.handler(project="P1"))
        assert "time budget" not in out["note"]

    def test_time_truncated_folder_tree_gets_the_time_note(self, stub, monkeypatch):
        stub_tool_module(monkeypatch, "_data_read",
            type("DO", (), {
                "list_folders_handler": staticmethod(lambda **kw: _ok({
                    "project": kw.get("project"), "folder_count": 1, "truncated": False,
                    "time_truncated": True, "folders": []})),
                "_TIME_BUDGET_S": 20.0,
            }))
        out = _payload(dge.handler(project="P1", include=["folders"]))
        assert "time budget" in out["note"]

    def test_untruncated_folder_tree_has_no_time_note(self, stub):
        out = _payload(dge.handler(project="P1", include=["folders"]))
        assert "time budget" not in out["note"]

    def test_time_truncated_projects_listing_gets_the_time_note(self, stub, monkeypatch):
        stub_tool_module(monkeypatch, "_data_read",
            type("DR", (), {
                "list_projects_handler": staticmethod(lambda: _ok({
                    "active_hub": "Main", "project_count": 1, "projects": [{"name": "P1"}],
                    "time_truncated": True})),
                "_TIME_BUDGET_S": 20.0,
            }))
        out = _payload(dge.handler())
        assert "time budget" in out["note"]

    def test_untruncated_projects_listing_has_no_time_note(self, stub):
        out = _payload(dge.handler())
        assert "time budget" not in out["note"]

    def test_include_hubs_lists_hubs(self, stub):
        out = _payload(dge.handler(include=["hubs"]))
        assert out["scope"] == "hubs"
        assert out["hub_count"] == 2

    def test_folder_path_passed_to_files(self, stub, monkeypatch):
        seen = {}
        stub_tool_module(monkeypatch, "_data_read",
            type("DR", (), {"list_project_files_handler": staticmethod(
                lambda **kw: (seen.update(kw) or _ok({"file_count": 0, "files": []})))}))
        out = _payload(dge.handler(project="P1", folder="Parts/Fixtures", recursive=False))
        assert seen["folder"] == "Parts/Fixtures" and seen["recursive"] is False
        assert "pointers" not in out            # no files -> no doc_open pointer (present-only)


class TestFileScope:
    def test_file_wins_over_the_project_file_listing(self, stub):
        # 'file' is a NARROWER scope than the project listing - a caller passing both must get the
        # one file's record, not the folder's contents.
        out = _payload(dge.handler(project="P1", file="notes.txt"))
        assert out["scope"] == "file"
        assert out["file"]["name"] == "notes.txt"

    def test_file_scope_passes_its_resolution_scope_through(self, stub):
        out = _payload(dge.handler(project="P1", folder="Docs", file="notes.txt"))
        assert out["seen"] == {"file": "notes.txt", "project": "P1", "project_id": "",
                               "folder": "Docs"}

    def test_file_note_advertises_the_extension_trap_and_the_read_only_link_state(self, stub):
        note = _payload(dge.handler(file="urn:adsk.wipprod:fs.file:vf.abc"))["note"]
        assert "NAME carries the true extension" in note
        assert "data_download_file" in note and "data_move_file" in note

    def test_a_name_matched_in_a_capped_listing_gets_the_uniqueness_caveat(self, stub, monkeypatch):
        stub_tool_module(monkeypatch, "_data_read",
            type("DR", (), {"file_facts_handler": staticmethod(
                lambda **kw: _ok({"matched_by": "name", "name_scope_truncated": True,
                                  "file": {"name": "notes.txt"}}))}))
        note = _payload(dge.handler(file="notes.txt", project="P1"))["note"]
        assert "CAPPED listing" in note and "lineage URN" in note

    def test_an_untruncated_match_carries_no_caveat(self, stub):
        assert "CAPPED listing" not in _payload(dge.handler(file="notes.txt", project="P1"))["note"]

    def test_a_name_matched_over_unread_folders_gets_the_unsearched_caveat(self, stub, monkeypatch):
        # A folder that never opened is a hole in the search space the cap flag does not describe:
        # the same name could sit in it, which would make this "unique" match the wrong file.
        stub_tool_module(monkeypatch, "_data_read",
            type("DR", (), {"file_facts_handler": staticmethod(
                lambda **kw: _ok({"matched_by": "name", "name_scope_folders_unreadable": 2,
                                  "file": {"name": "notes.txt"}}))}))
        note = _payload(dge.handler(file="notes.txt", project="P1"))["note"]
        assert "2 folder(s) could not be READ" in note
        assert "lineage URN" in note                  # the exact reference that dodges the hole

    def test_a_match_over_a_fully_read_scope_carries_no_unsearched_caveat(self, stub):
        assert "could not be READ" not in _payload(
            dge.handler(file="notes.txt", project="P1"))["note"]

    def test_file_with_include_is_refused_rather_than_silently_ignored(self, stub):
        res = dge.handler(file="notes.txt", project="P1", include=["folders"])
        assert "does not apply to the 'file' scope" in error_message(res)

    def test_file_scope_propagates_a_resolution_error(self, stub, monkeypatch):
        stub_tool_module(monkeypatch, "_data_read",
            type("DR", (), {"file_facts_handler": staticmethod(
                lambda **kw: _err("'notes.txt' names 2 files in project 'P1'"))}))
        res = dge.handler(file="notes.txt", project="P1")
        assert "names 2 files" in error_message(res)


def _folder(name, children=()):
    """One cloud folder. Enumerating dataFolders is the round-trip the walk is budgeted on, so the
    children are only ever reachable through it."""
    return FakeDataFolder(name, folder_id=f"id:{name}", folders=list(children))


def _folder_chain(depth):
    """A root holding one branch `depth` levels deep: L1 -> L2 -> ... -> L<depth>."""
    node = None
    for level in range(depth, 0, -1):
        node = _folder(f"L{level}", [node] if node else [])
    return _folder("root", [node])


def _paths(nodes):
    out = []
    for n in nodes:
        out.append(n["path"])
        out += _paths(n.get("folders", []))
    return out


class TestFolderDepthDefault:
    """The default folder depth is a CLOUD cost - each level is a round-trip - and data_get names no
    depth of its own at the walk: it forwards the one its description promises. Nothing else here
    calls the router without naming max_depth, so nothing else exercises that default."""

    def test_the_default_depth_reaches_the_walk_and_is_the_depth_it_descends(self, stub,
                                                                             monkeypatch):
        seen = {}
        stub_tool_module(monkeypatch, "_data_read",
            type("DO", (), {"list_folders_handler": staticmethod(
                lambda **kw: (seen.update(kw)
                              or _ok({"project": "P1", "folder_count": 0, "folders": []})))}))
        _payload(dge.handler(project="P1", include=["folders"]))
        assert seen["max_depth"] == 4

        promised = dge.tool.to_dict()["inputSchema"]["properties"]["max_depth"]["default"]
        assert seen["max_depth"] == promised

        # what that number DOES: a six-level chain read at the forwarded depth stops four levels
        # down, and the last node says its children are unknown rather than implying it has none.
        tree, count, truncated, _stalled, unread = dops._folder_tree_bounded(_folder_chain(6),
                                                                             seen["max_depth"])
        assert _paths(tree) == ["L1", "L1/L2", "L1/L2/L3", "L1/L2/L3/L4"]
        assert count == 4 and truncated is False    # the DEPTH cap stopped it, not the fetch budget
        assert unread == {}                         # every folder on the way down enumerated
        assert tree[0]["folders"][0]["folders"][0]["folders"][0]["children_unknown"] is True


class TestFolderTreeScopeAndBudgets:
    """The tree read's scope and its two budgets are the caller's: a project-wide walk hits the
    fetch budget on a real project, so 'folder' and the budgets have to REACH the walk."""

    def _seen(self, monkeypatch):
        seen = {}
        stub_tool_module(monkeypatch, "_data_read",
            type("DO", (), {"list_folders_handler": staticmethod(
                lambda **kw: (seen.update(kw)
                              or _ok({"project": "P1", "folder_count": 0, "folders": []})))}))
        return seen

    def test_the_folder_scope_and_both_budgets_reach_the_walk(self, stub, monkeypatch):
        seen = self._seen(monkeypatch)
        _payload(dge.handler(project="P1", include=["folders"], folder="Parts/Fixtures",
                             folder_budget=60, time_budget_s=45))
        assert seen["folder"] == "Parts/Fixtures"
        assert seen["folder_budget"] == 60 and seen["time_budget_s"] == 45

    def test_the_published_defaults_are_the_walks_own(self, stub, monkeypatch):
        # the schema promises numbers a caller sizes against - they come off the core, not a copy.
        props = dge.tool.to_dict()["inputSchema"]["properties"]
        assert f"default {dops._LF_FOLDER_BUDGET}" in props["folder_budget"]["description"]
        assert f"max {dops._LF_FOLDER_BUDGET_MAX}" in props["folder_budget"]["description"]
        assert f"default {int(dops._TIME_BUDGET_S)}" in props["time_budget_s"]["description"]

    def test_an_unreadable_folder_is_taught_in_the_note(self, stub, monkeypatch):
        stub_tool_module(monkeypatch, "_data_read",
            type("DO", (), {"list_folders_handler": staticmethod(
                lambda **kw: _ok({"project": "P1", "folder_count": 3, "truncated": False,
                                  "folders_unreadable": 2,
                                  "folders_unreadable_at": ["Archive"], "folders": []}))}))
        note = _payload(dge.handler(project="P1", include=["folders"]))["note"]
        assert "2 folder(s) would not enumerate" in note and "children_unreadable" in note

    def test_a_readable_tree_gets_no_unreadable_clause(self, stub):
        assert "would not enumerate" not in _payload(
            dge.handler(project="P1", include=["folders"]))["note"]


class TestGuards:
    def test_unknown_include_errors(self, stub):
        res = dge.handler(include=["bogus"])
        assert "bogus" in error_message(res).lower() or "unknown" in error_message(res).lower()

    def test_the_include_enum_matches_the_slice_tuple(self, stub):
        # Catches a HAND-EDITED schema drifting from the tuple. It cannot catch a name added to the
        # tuple itself - both sides read it - which is what the dispatch test below covers.
        enum = dge.tool.input_schema["properties"]["include"]["items"]["enum"]
        assert sorted(enum) == sorted(dge._SLICES)

    def test_every_advertised_slice_actually_dispatches(self, stub):
        # A name in _SLICES that no branch reads falls THROUGH to the default project listing - a
        # silent ok for a slice never built. Each must switch the read to its own 'scope'.
        reached_by = {"hubs": {}, "folders": {"project": "P1"}}
        assert sorted(reached_by) == sorted(dge._SLICES), "a slice with no scope stated to reach it"
        for name in dge._SLICES:
            out = _payload(dge.handler(include=[name], **reached_by[name]))
            assert out["scope"] == name, name

    def test_cloud_error_propagates(self, monkeypatch):
        stub_tool_module(monkeypatch, "_data_read",
            type("DR", (), {"list_projects_handler": staticmethod(lambda: _err("not signed in"))}))
        res = dge.handler()
        assert "not signed in" in error_message(res).lower()


class TestDeferredImportSeam:
    """`data_get`'s handlers import their delegates INSIDE the body (`from . import _data_read`), and
    that resolves through the `mcpServer.tools` PACKAGE ATTRIBUTE whenever the attribute is bound.
    A real import of the sibling binds it - `doc_get` imports `_data_read` at top level - so any
    order that loads the registry ahead of this file leaves it bound. A sys.modules-only stub is
    then never read there, and every stubbed test calls the real cloud handler instead. These pin
    BOTH of `stub_tool_module`'s seams directly, so none of them depends on collection order."""

    def _stub(self, monkeypatch):
        return stub_tool_module(monkeypatch, "_data_read", type("DR", (), {
            "list_projects_handler": staticmethod(
                lambda: _ok({"active_hub": "Main", "project_count": 7, "projects": []}))}))

    def test_the_stub_routes_even_when_the_package_attribute_is_already_bound(self, monkeypatch):
        # The worst-case order, made deterministic: bind the REAL module as the package attribute
        # first, exactly as a genuine `from . import _data_read` elsewhere leaves it, then stub.
        import sys
        pkg = sys.modules["mcpServer.tools"]
        monkeypatch.setattr(pkg, "_data_read", load_tool("_data_read"), raising=False)
        self._stub(monkeypatch)
        assert _payload(dge.handler())["project_count"] == 7

    def test_the_stub_routes_when_no_package_attribute_is_bound(self, monkeypatch):
        # The clean order: nothing has bound the attribute, so the helper CREATES it. `from . import`
        # routes through that attribute either way, and this pins that the create path resolves as
        # the already-bound one does - the two orders are one behaviour, not two.
        import sys
        pkg = sys.modules["mcpServer.tools"]
        monkeypatch.delattr(pkg, "_data_read", raising=False)
        self._stub(monkeypatch)
        assert _payload(dge.handler())["project_count"] == 7

    def test_the_stub_answers_the_call_time_importlib_route_too(self, monkeypatch):
        # The OTHER seam, and the only one sys.modules serves: a sibling reached by
        # `importlib.import_module('.<name>', __package__)` at call time - what
        # `_sketch_detail._detail_engine` and `sys_get_api_doc`'s module walk do - reads the module table
        # and never the package attribute. Routing `from . import` is not enough for that shape.
        import importlib
        stub = self._stub(monkeypatch)
        assert importlib.import_module("._data_read", "mcpServer.tools") is stub

    def test_the_stubbed_attribute_is_removed_again_when_the_package_had_none(self, monkeypatch):
        # The attribute patch must not LEAK a stub into the next test as the package's own module.
        import sys
        from _pytest.monkeypatch import MonkeyPatch
        pkg = sys.modules["mcpServer.tools"]
        monkeypatch.delattr(pkg, "_data_read", raising=False)
        inner = MonkeyPatch()
        stub_tool_module(inner, "_data_read", type("DR", (), {}))
        assert getattr(pkg, "_data_read", None) is not None
        inner.undo()
        assert not hasattr(pkg, "_data_read")

    def test_the_helper_refuses_before_the_tools_package_exists(self, monkeypatch):
        # There is nothing to hang the attribute on yet, and the refusal names the call that creates
        # the package - without it monkeypatch raises on None, which names nothing.
        import sys
        from _pytest.monkeypatch import MonkeyPatch
        monkeypatch.delitem(sys.modules, "mcpServer.tools")
        with pytest.raises(RuntimeError, match="load_tool"):
            stub_tool_module(MonkeyPatch(), "_data_read", type("DR", (), {}))
