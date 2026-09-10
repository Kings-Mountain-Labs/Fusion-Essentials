"""The cold-start front door: a contextless agent must be routed to the orientation tools.

Two layers, both client-reachable:
  1. The server's `instructions` field (returned on initialize, BEFORE any tool schema is fetched) names
     the two orientation tools. Verified by reading the constant from source (mcp_server imports deep
     Fusion utils that don't load in this harness, so we assert on the source text - which is what
     ships).
  2. A naive onboarding search ('getting started', 'help', 'overview', 'where am i') surfaces the
     orientation tools via sys_find_tool - so an agent that DOES think to search lands on the front door.
"""

import os
import re

from types import SimpleNamespace
import json

from conftest import load_tool

_SERVER_SRC = os.path.join("commands", "mcpServer", "server", "mcp_server.py")


def _instructions_text():
    """Extract the INSTRUCTIONS = ( ... ) literal from mcp_server.py source (it can't be imported in
    this harness). Concatenate the implicit-joined string parts."""
    src = open(_SERVER_SRC, encoding="utf-8").read()
    m = re.search(r"INSTRUCTIONS\s*=\s*\((.*?)\)\n", src, re.DOTALL)
    assert m, "INSTRUCTIONS literal not found in mcp_server.py"
    parts = re.findall(r'"([^"]*)"', m.group(1))
    return "".join(parts)


class TestServerInstructions:
    def test_instructions_returned_on_initialize(self):
        # the field is wired into the initialize result (the one text seen before tool deferral).
        src = open(_SERVER_SRC, encoding="utf-8").read()
        assert '"instructions": INSTRUCTIONS' in src

    def test_instructions_route_to_both_orientation_tools(self):
        text = _instructions_text()
        assert "sys_capability_map" in text          # what the server can do
        assert "workspace_orient" in text            # what's in front of you
        assert "FIRST" in text or "first" in text     # the cold-start imperative


class TestNaiveOnboardingSearchHitsFrontDoor:
    def _find(self, query):
        ft = load_tool("sys_find_tool")
        wo = load_tool("workspace_orient")
        cm = load_tool("sys_capability_map")
        ft.get_tools = lambda: [SimpleNamespace(primitive=wo.tool), SimpleNamespace(primitive=cm.tool)]
        out = json.loads(ft.handler(query=query)["content"][0]["text"])
        return [t["tool"] for t in out["tools"]]

    def test_getting_started_surfaces_orientation(self):
        assert "workspace_orient" in self._find("getting started")

    def test_help_surfaces_capability_map(self):
        assert "sys_capability_map" in self._find("help")

    def test_overview_and_start_here_surface_front_door(self):
        for q in ("overview", "start here", "where am i", "start"):
            hits = self._find(q)
            assert hits, f"naive onboarding query {q!r} found nothing"
            assert "workspace_orient" in hits or "sys_capability_map" in hits
