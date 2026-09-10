# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Lint: sys_capability_map's authored _FAMILY rows cover EXACTLY the registry's families, and
every entry tool they name is a registered tool.

Red five ways: a family with no row, a row outliving its family, a row naming no registered tool,
a published capability name the live sweep's own probe registry does not carry, and a capability
pointing at a tool the registry no longer has."""

from conftest import load_tool, load_tool_verify, register_all_tools

cm = load_tool("sys_capability_map")


def _registry():
    items = register_all_tools()
    names = {it.get_name() for it in items}
    families = {cm._family_of(n) for n in names}
    return names, families


class TestCapabilityMapComplete:
    def test_every_registry_family_has_an_authored_row(self):
        _, families = _registry()
        missing = sorted(families - set(cm._FAMILY))
        assert not missing, (
            "these registered tool families have NO authored row in sys_capability_map._FAMILY - "
            "agents get only the generic fallback summary for them; add a one-line summary + entry "
            "tool per family:\n  " + "\n  ".join(missing))

    def test_no_authored_row_outlives_its_family(self):
        _, families = _registry()
        stale = sorted(set(cm._FAMILY) - families)
        assert not stale, (
            "these _FAMILY rows name families the registry no longer carries - delete the row or "
            "restore the family:\n  " + "\n  ".join(stale))

    def test_every_authored_entry_tool_is_registered(self):
        names, _ = _registry()
        bad = sorted(f"{fam} -> {entry}" for fam, (_s, entry) in cm._FAMILY.items()
                     if entry not in names)
        assert not bad, (
            "these _FAMILY rows point at an entry tool the registry does not carry (renamed or "
            "deleted) - re-point the row:\n  " + "\n  ".join(bad))

    def test_every_published_capability_name_is_one_the_sweep_knows(self):
        # ONE vocabulary. A name the map publishes that no sweep probe answers to is a second
        # spelling of the same entitlement, and an agent planning against it can never be told
        # which steps it gates. The sweep may hold names of its own (an environment the tool
        # cannot see), so the rule runs one way.
        known = set(load_tool_verify().CAPABILITY_PROBES)
        stray = sorted(set(cm._CAPABILITIES) - known)
        assert not stray, (
            "sys_capability_map publishes these capability names, which the live sweep's probe "
            "registry (tests/live/verify_core.py CAPABILITY_PROBES) does not carry - spell them "
            "the way the sweep does, or add the sweep's probe:\n  " + "\n  ".join(stray))

    def test_every_capability_points_at_a_registered_tool(self):
        # The map takes no verdict of its own - it names the tool whose read does. A read_with
        # naming a renamed or unregistered tool sends a cold agent at a 'No such tool available'.
        names, _ = _registry()
        bad = sorted(f"{cap} -> {tool}" for cap, tool in cm._CAPABILITIES.items()
                     if tool not in names)
        assert not bad, (
            "these capability rows point at a tool the registry does not carry - re-point the "
            "row:\n  " + "\n  ".join(bad))
