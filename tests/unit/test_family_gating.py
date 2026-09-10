# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Unit tests for per-family tool gating.

Covers the pieces that live OUTSIDE entry.py (which needs the live Fusion add-in host and can't be
imported here): the shared ``family_of``/``GATEABLE_FAMILIES`` facts and ``Registry.unregister`` in
``mcp_primitives/registry.py``, workspace_orient's pointer hardening against a disabled family's
pointer naming a 404ing tool, and - since entry.py itself can't be imported - a regex check of its
source text for the ``family_enabled_<fam>`` settings-key contract (same source-parsing style as
``test_cold_start_onboarding.py``).
"""

import json
import os
import re
import types

import pytest

from conftest import FakeProducts, load_tool, make_cam, TOOLS_DIR

wo = load_tool("workspace_orient")

from mcpServer.mcp_primitives import registry
from mcpServer.mcp_primitives.tool import Tool
from mcpServer.mcp_primitives.item import Item


# ── family_of ───────────────────────────────────────────────────────────────────────────────────

class TestFamilyOf:
    def test_cam_get_is_cam(self):
        assert registry.family_of("cam_get") == "cam"

    def test_find_geometry_is_find(self):
        # a single-word-prefix tool: the whole first underscore segment is the family.
        assert registry.family_of("find_geometry") == "find"

    def test_workspace_orient_is_workspace(self):
        assert registry.family_of("workspace_orient") == "workspace"

    def test_empty_string_is_safe(self):
        assert registry.family_of("") == ""

    def test_none_is_safe(self):
        assert registry.family_of(None) == ""


# ── GATEABLE_FAMILIES ───────────────────────────────────────────────────────────────────────────

def _real_family_set():
    """Every family that actually appears in the registered tool surface - ground truth for
    checking GATEABLE_FAMILIES membership. Built by sweeping tools/ and registering everything,
    the same way entry._collect_items() and test_tool_autodiscovery.py's sweep do."""
    names = [fn[:-3] for fn in sorted(os.listdir(TOOLS_DIR))
             if fn.endswith(".py") and not fn.startswith("_")]
    load_tool(names[0])
    registry.reset_registry()
    for name in names:
        mod = load_tool(name)
        reg = getattr(mod, "register_tool", None)
        if callable(reg):
            reg()
    return {registry.family_of(it.get_name()) for it in registry.get_tools()}


class TestGateableFamilies:
    def test_excludes_hub_and_orientation_families(self):
        # these are cross-referenced from everywhere (hub families) or hold the orientation tools
        # named in the server's initialize instructions (sys/workspace) - never gateable.
        always_on = {"sys", "workspace", "doc", "find", "view", "design", "model",
                     "sketch", "joint", "assembly", "param"}
        assert always_on.isdisjoint(registry.GATEABLE_FAMILIES)

    def test_every_member_is_a_real_registered_family(self):
        real = _real_family_set()
        missing = [fam for fam in registry.GATEABLE_FAMILIES if fam not in real]
        assert not missing, f"GATEABLE_FAMILIES names families with no registered tools: {missing}"


# ── Registry.unregister ─────────────────────────────────────────────────────────────────────────

def _throwaway_item(name):
    tool = Tool.create_simple(name=name, description="throwaway test tool").strict_schema().reads()
    return Item.create_tool_item(tool=tool, write="read", handler=lambda: {"ok": True})


class TestRegistryUnregister:
    def test_removes_a_registered_tool(self):
        reg = registry.Registry()
        reg.register(_throwaway_item("widget_get"))
        assert reg.has_tool("widget_get") is True
        assert reg.unregister("widget_get") is True
        assert reg.has_tool("widget_get") is False
        assert "widget_get" not in [it.get_name() for it in reg.get_tools()]

    def test_unknown_name_returns_false(self):
        reg = registry.Registry()
        assert reg.unregister("nope_get") is False

    def test_module_level_wrapper_mirrors_the_instance_method(self):
        registry.reset_registry()
        registry.register(_throwaway_item("gadget_get"))
        assert registry.has_tool("gadget_get") is True
        assert registry.unregister("gadget_get") is True
        assert registry.has_tool("gadget_get") is False
        # a second unregister of the same (now-absent) name is a no-op, not an error.
        assert registry.unregister("gadget_get") is False


# ── workspace_orient pointer filtering ──────────────────────────────────────────────────────────

class _Coll:
    def __init__(self, items=()):
        self._items = list(items)

    @property
    def count(self):
        return len(self._items)

    def item(self, i):
        return self._items[i]


class _FakeDoc:
    def __init__(self, cam):
        self.products = FakeProducts(cam=cam)


def _payload(res):
    assert res["isError"] is False, res
    return json.loads(res["content"][0]["text"])


@pytest.fixture
def small_design_with_cam(monkeypatch):
    """The minimum surface for workspace_orient.handler() to emit the always-present
    'assembly_structure'/'geometry' pointers plus a 'cam' pointer (CAM data present) - everything
    else in the object model is left unset and swallowed by the handler's own safe() wrapping."""
    design = types.SimpleNamespace()
    cam = make_cam()
    doc = _FakeDoc(cam)
    ui = types.SimpleNamespace(activeWorkspace=types.SimpleNamespace(name="Design"),
                                activeSelections=_Coll([]))
    app = types.SimpleNamespace(version="TEST.0", activeDocument=doc, activeProduct=design,
                                 userInterface=ui, activeViewport=None)
    monkeypatch.setattr(wo, "app", app)
    monkeypatch.setattr(wo._common, "app", app)

    import adsk.fusion
    import adsk.cam
    monkeypatch.setattr(adsk.fusion.Design, "cast", lambda x: design if x is design else None)
    monkeypatch.setattr(adsk.cam.CAM, "cast", lambda x: x if x is cam else None)
    monkeypatch.setattr(wo._cam_common, "get_cam", lambda: (cam, None))
    return wo


class TestPointerFiltering:
    def test_disabled_family_pointer_dropped_others_kept(self, small_design_with_cam, monkeypatch):
        # a NON-EMPTY registry seam where has_tool('cam_get') is False (the 'cam' family disabled)
        # but every other pointer's target tool IS registered - only the cam pointer should vanish.
        monkeypatch.setattr(wo.registry, "get_tools", lambda: [object()])
        monkeypatch.setattr(wo.registry, "has_tool", lambda name: name != "cam_get")
        out = _payload(wo.handler())
        assert "cam" not in out["pointers"]
        assert "assembly_structure" in out["pointers"]
        assert "geometry" in out["pointers"]

    def test_empty_registry_keeps_every_pointer(self, small_design_with_cam, monkeypatch):
        # the escape valve: an EMPTY registry (no live server backing the call - the unit-test
        # context) skips filtering entirely, even though has_tool is forced False for everything.
        monkeypatch.setattr(wo.registry, "get_tools", lambda: [])
        monkeypatch.setattr(wo.registry, "has_tool", lambda name: False)
        out = _payload(wo.handler())
        assert "cam" in out["pointers"]
        assert "assembly_structure" in out["pointers"]
        assert "geometry" in out["pointers"]


# ── entry.py settings-key contract (source-parsed; entry.py can't be imported here) ────────────

_ENTRY_SRC = os.path.join("commands", "mcpServer", "entry.py")


def _entry_source():
    with open(_ENTRY_SRC, encoding="utf-8") as f:
        return f.read()


class TestEntrySettingsKeyContract:
    def test_family_enabled_keys_match_gateable_families_exactly(self):
        src = _entry_source()
        found = set(re.findall(r'"family_enabled_([a-z]+)"', src))
        assert found == set(registry.GATEABLE_FAMILIES)

    def test_default_settings_defaults_every_family_key_to_true(self):
        # match each family_enabled_<fam> literal block and its immediately-following default so
        # a future edit can't silently flip one family's default without this test catching it.
        src = _entry_source()
        for fam in registry.GATEABLE_FAMILIES:
            m = re.search(
                r'"family_enabled_%s"\s*:\s*\{[^}]*"default"\s*:\s*(True|False)' % re.escape(fam),
                src)
            assert m, f"family_enabled_{fam} block not found in entry.py"
            assert m.group(1) == "True", f"family_enabled_{fam} must default to True"


# ── GATED_TOOLS: entry.py's gate must be DERIVED from the shared map, not a second hand-typed copy
# (sys_capability_map reports this same map - see test_sys_capability_map.py's TestGatedTools) ────

class TestGatedToolsSourcedFromSharedMap:
    def test_gated_tool_modules_derived_from_gated_tools_keys(self):
        src = _entry_source()
        assert "_GATED_TOOL_MODULES = frozenset(GATED_TOOLS)" in src, (
            "entry.py must derive its skip-set from mcp_primitives.GATED_TOOLS, not a separate "
            "hand-typed frozenset literal (the shape sys_capability_map's gated report depends on)")

    def test_settings_label_references_gated_tools_not_a_second_literal(self):
        src = _entry_source()
        assert 'GATED_TOOLS["sys_execute_script"]' in src, (
            "entry.py's DEFAULT_SETTINGS label must reference GATED_TOOLS, not hand-type its own copy")
        # the raw label text itself must appear NOWHERE ELSE in entry.py - a second hand-typed copy
        # is exactly the drift sys_capability_map's gated report is meant never to have.
        label = registry.GATED_TOOLS["sys_execute_script"]
        assert src.count(label) == 0, (
            f"entry.py hand-types the GATED_TOOLS label text ({label!r}) a second time - "
            "reference GATED_TOOLS[...] instead so the two can't drift apart")
