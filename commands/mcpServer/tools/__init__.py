# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""MCP tools package - one tool per ``<domain>_<verb>.py`` module.

``entry.py::_collect_items()`` sweeps this package with ``pkgutil.iter_modules`` and calls each
module's ``register_tool()``, skipping ``_``-prefixed helpers and the gated ``sys_execute_script``
(entry.py registers that one explicitly when the user opts in)."""
