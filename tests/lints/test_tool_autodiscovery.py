"""Lint/contract for the AUTO-DISCOVERED tool registration (entry.py::_collect_items).

Sweeping tools/ the way entry.py does - without importing entry.py, which needs the live add-in
host - registers every non-helper module under a unique name, gated and helper modules excluded."""

import os

import _corpus
from conftest import load_tool, TOOLS_DIR

# Mirror entry.py's gated set + helper-skip rule.
_GATED = {"sys_execute_script"}


def _tool_module_names():
    return [fn[:-3] for fn in sorted(os.listdir(TOOLS_DIR))
            if fn.endswith(".py") and not fn.startswith("_") and fn != "__init__.py"]


def _sweep_register():
    """Replicate entry._collect_items()'s sweep: load every non-gated tool module and call its
    register_tool() if present. Returns the registry's tool Items."""
    from mcpServer.mcp_primitives import registry
    names = _tool_module_names()
    load_tool(names[0])              # bootstrap sys.path + the mcpServer.tools stub
    registry.reset_registry()
    for name in names:
        if name in _GATED:
            continue
        mod = load_tool(name)
        reg = getattr(mod, "register_tool", None)
        if callable(reg):
            reg()
    return registry.get_tools()


class TestAutoDiscovery:
    def test_every_swept_module_registers_a_tool(self):
        # Shared engines/helpers are _-prefixed, so both _tool_module_names() and the real sweep
        # skip them - that is how a read CORE behind a Get, like _sketch_detail / _data_read,
        # declares "I am not a tool".
        names = [n for n in _tool_module_names() if n not in _GATED]
        without = [n for n in names if not callable(getattr(load_tool(n), "register_tool", None))]
        assert not without, (f"non-gated, non-underscore modules missing register_tool() (would be "
                             f"silently UNREGISTERED - _-prefix them if they are shared engines): {without}")
        assert len(_sweep_register()) >= len(names)

    def test_sweep_registers_a_full_nonempty_set(self):
        items = _sweep_register()
        assert len(items) >= 100, f"expected the full tool surface, got {len(items)}"

    def test_tool_names_are_unique(self):
        items = _sweep_register()
        names = [it.get_name() for it in items]
        dupes = {n for n in names if names.count(n) > 1}
        assert not dupes, f"duplicate tool names (collision guard would raise at runtime): {dupes}"

    def test_gated_tool_not_in_swept_set(self):
        items = _sweep_register()
        assert "sys_execute_script" not in {it.get_name() for it in items}

    def test_helper_modules_have_no_register_tool(self):
        for helper in ("_common", "_inputs", "_outputs", "_data_common"):
            mod = load_tool(helper)
            assert getattr(mod, "register_tool", None) is None, (
                f"{helper} should be a helper, not a tool (no register_tool)")

    def test_explicitly_referenced_modules_are_importable_with_their_entry_points(self):
        # entry._collect_items() references three modules OUTSIDE the sweep and imports them
        # explicitly: a `tools.<name>` attribute access on a module the sweep skips raises
        # AttributeError at startup, so the server never binds its port and /health 404s.
        assert callable(getattr(load_tool("sys_execute_script"), "register_tool", None)), \
            "gated sys_execute_script must expose register_tool() (entry.py calls it when enabled)"
        reload_mod = load_tool("sys_reload_addin")
        assert callable(getattr(reload_mod, "register_tool", None)), \
            "sys_reload_addin must expose register_tool()"
        assert callable(getattr(reload_mod, "install_reload_event", None)), \
            "sys_reload_addin must expose install_reload_event() (entry.py installs its event)"

    def test_entry_does_not_attribute_access_swept_or_gated_modules(self):
        # A module is a bound attribute of the `tools` package only if something imported it, so
        # `tools.<name>.foo()` for a module the sweep SKIPS (gated) aborts server startup.
        import os
        entry = os.path.join(os.path.dirname(TOOLS_DIR), "entry.py")
        src = _corpus.text(entry)
        for bad in ("tools.sys_execute_script", "tools.sys_reload_addin"):
            assert bad not in src, (
                f"entry.py references `{bad}` as an attribute — import it explicitly instead "
                f"(`from .tools import {bad.split('.')[-1]}`); the attribute may not exist after the "
                "pkgutil sweep, which aborts startup.")
