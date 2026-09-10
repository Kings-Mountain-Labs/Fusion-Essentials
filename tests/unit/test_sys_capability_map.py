"""Unit tests for ``sys_capability_map`` - the LIVE family index (breadth map).

Patch get_tools to a known set of fake tools and assert: the grouping by name-prefix family,
summary/entry_tool/tool_count per family, the registry-derived fallback for an UNMAPPED family, that
counts sum to the tool total (it's an index of the real registry), and the capability rows the
_CAPABILITIES registry derives. No adsk.* - the map points at the tool that reads a capability
rather than probing one itself.
"""

import json
from types import SimpleNamespace

from conftest import load_tool

cm = load_tool("sys_capability_map")


def _item(name):
    return SimpleNamespace(primitive=SimpleNamespace(name=name))


def _install(names):
    cm.get_tools = lambda: [_item(n) for n in names]


def _payload(result):
    assert result["isError"] is False, result
    return json.loads(result["content"][0]["text"])


class TestFamilyMap:
    def test_groups_by_prefix_with_summary_entry_and_count(self):
        _install(["cam_get", "cam_create_setup", "cam_generate",
                  "sketch_create", "sketch_constrain"])
        out = _payload(cm.handler())
        fams = {f["family"]: f for f in out["families"]}
        assert set(fams) == {"cam", "sketch"}
        assert fams["cam"]["tool_count"] == 3
        assert fams["cam"]["entry_tool"] == "cam_create_setup"     # the curated family fact
        assert fams["sketch"]["entry_tool"] == "sketch_create"
        assert fams["cam"]["summary"]                              # a factual line, present

    def test_counts_sum_to_tool_total(self):
        names = ["cam_get", "cam_generate", "model_extrude", "doc_get", "doc_save"]
        _install(names)
        out = _payload(cm.handler())
        assert out["tool_count"] == len(names)
        assert sum(f["tool_count"] for f in out["families"]) == len(names)
        assert out["family_count"] == 3                            # cam, model, doc

    def test_unmapped_family_falls_back_honestly(self):
        # a family with no curated entry -> entry derived (a *_get/_create if present, else first),
        # summary states only the fact, never invented advice.
        _install(["widget_frob", "widget_get", "widget_zap"])
        out = _payload(cm.handler())
        w = {f["family"]: f for f in out["families"]}["widget"]
        assert w["entry_tool"] == "widget_get"                     # derived: the _get
        assert w["tool_count"] == 3
        assert "widget" in w["summary"]

    def test_note_cross_links_to_find_tool(self):
        _install(["cam_get"])
        out = _payload(cm.handler())
        assert "sys_find_tool" in out["note"]                      # breadth <-> depth cross-link


class TestCapabilities:
    # The block is DERIVED from _CAPABILITIES (capability name -> the tool whose read answers it),
    # so swapping the registry swaps the published rows.

    def test_one_row_per_capability_naming_the_tool_that_reads_it(self, monkeypatch):
        monkeypatch.setattr(cm, "_CAPABILITIES", {"z_cap": "z_get", "a_cap": "a_get"})
        _install(["sys_capability_map"])
        out = _payload(cm.handler())
        assert out["capabilities"]["names"] == {
            "a_cap": {"read_with": "a_get"},
            "z_cap": {"read_with": "z_get"},
        }

    def test_the_map_takes_no_verdict_of_its_own(self, monkeypatch):
        # It touches no adsk.*, which is what keeps it answering while the main thread is parked -
        # so a row carries the POINTER and never an entitlement the map would have had to probe.
        monkeypatch.setattr(cm, "_CAPABILITIES", {"a_cap": "a_get"})
        _install(["sys_capability_map"])
        row = _payload(cm.handler())["capabilities"]["names"]["a_cap"]
        assert set(row) == {"read_with"}
        assert cm.item.run_on_main_thread is False

    def test_the_note_is_the_pointer_and_claims_nothing_it_cannot_back(self):
        # It names no key of the pointed-at tool's payload: nothing here fails when one of those
        # is renamed, so the note would go stale silently.
        _install(["sys_capability_map"])
        note = _payload(cm.handler())["capabilities"]["note"]
        assert "read_with names the tool" in note
        assert "not taken here" in note
        for key in ("machining_capabilities", "observed_generation", "entitled"):
            assert key not in note


class TestFamilyOf:
    def test_prefix_split(self):
        assert cm._family_of("cam_get") == "cam"
        assert cm._family_of("find_geometry") == "find"
        assert cm._family_of("workspace_orient") == "workspace"


# ── gated tools: derived from mcp_primitives.GATED_TOOLS, never hand-listed ──────────────────────
#
# The map's own 'gated' entry is built from cm.GATED_TOOLS (imported from mcp_primitives.registry,
# the SAME map entry.py's auto-discovery sweep acts on to skip a tool's module until its setting
# reads True) and cm.has_tool (the live registry). Swapping either seam proves the output is
# DERIVED, not a second hand-maintained copy that could drift stale.

class TestGatedTools:
    def test_lists_a_gated_tool_with_its_enable_path(self, monkeypatch):
        monkeypatch.setattr(cm, "GATED_TOOLS", {"sys_execute_script": "Allow AI to execute scripts"})
        monkeypatch.setattr(cm, "has_tool", lambda name: False)
        _install(["sys_find_tool", "sys_capability_map"])
        out = _payload(cm.handler())
        assert out["gated"]["tools"] == [{
            "tool": "sys_execute_script",
            "enabled_now": False,
            "enable_path": "Fusion Essentials Settings command -> MCP Server tab -> "
                           "'Allow AI to execute scripts' checkbox",
        }]

    def test_enabled_now_reflects_the_live_registry_not_a_settings_read(self, monkeypatch):
        monkeypatch.setattr(cm, "GATED_TOOLS", {"sys_execute_script": "label"})
        monkeypatch.setattr(cm, "has_tool", lambda name: name == "sys_execute_script")
        _install(["sys_capability_map"])
        out = _payload(cm.handler())
        assert out["gated"]["tools"][0]["enabled_now"] is True

    def test_empty_gated_tools_yields_an_empty_list_not_a_hardcoded_entry(self, monkeypatch):
        # proves the list is DERIVED from GATED_TOOLS rather than a string hand-typed into the map.
        monkeypatch.setattr(cm, "GATED_TOOLS", {})
        _install(["sys_capability_map"])
        out = _payload(cm.handler())
        assert out["gated"]["tools"] == []

    def test_multiple_gated_tools_are_all_listed_sorted(self, monkeypatch):
        monkeypatch.setattr(cm, "GATED_TOOLS", {"z_tool": "Z label", "a_tool": "A label"})
        monkeypatch.setattr(cm, "has_tool", lambda name: False)
        _install(["sys_capability_map"])
        out = _payload(cm.handler())
        assert [t["tool"] for t in out["gated"]["tools"]] == ["a_tool", "z_tool"]

    def test_gated_note_names_stale_schema_and_client_deny(self):
        _install(["sys_capability_map"])
        note = _payload(cm.handler())["gated"]["note"]
        assert "No such tool available" in note
        assert "stale client tool list" in note
        assert "deny rule" in note
        assert "schema_fingerprint" in note
        assert "reconnect or refresh" in note
        assert "not whether the client consumed it" in note

    def test_the_note_keeps_server_disabled_distinct_from_present_but_hidden(
            self, monkeypatch):
        monkeypatch.setattr(cm, "GATED_TOOLS", {"sys_execute_script": "Allow AI to execute scripts"})
        monkeypatch.setattr(cm, "has_tool", lambda name: False)
        _install(["sys_capability_map"])
        out = _payload(cm.handler())
        note = out["gated"]["note"]
        assert out["gated"]["tools"][0]["enabled_now"] is False
        assert "disabled here" in note and "enable_path" in note
        present = note[note.index("For a tool this map names as present"):]
        assert "stale client tool list" in present and "deny rule" in present
