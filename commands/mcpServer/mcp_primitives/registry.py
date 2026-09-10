# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.
#
# Adapted from Autodesk's Fusion MCP add-in sample (MIT-licensed).

"""Registry for MCP Tool Item collections, with a singleton for shared use.

Tools self-register at import time by calling register(); the server then pulls
the registered items via get_tools(). The singleton is reset on each server start
so a reload does not accumulate duplicate registrations.
"""

from typing import Dict, List
from .item import Item


def family_of(name):
    """The family of a tool = its first underscore segment (cam_get -> 'cam'; find_geometry -> 'find')."""
    return (name or "").split("_", 1)[0]


# Optional domains a user may disable via Settings -> MCP Server (see commands/mcpServer/entry.py).
# Only these: the hub families (doc/find/view/design/model/sketch/joint/assembly/param) are
# cross-referenced from everywhere, and sys/workspace hold the orientation tools named in the
# server's initialize instructions - so those stay always-on.
GATEABLE_FAMILIES = ("appearance", "cam", "data", "drawing", "mesh", "save", "surface")

# Tools the auto-discovery sweep SKIPS ENTIRELY unless a specific settings checkbox allows them
# (see entry.py's _GATED_TOOL_MODULES / _collect_items - the module is never even imported by the
# sweep until the setting reads True). tool name -> the exact checkbox label entry.py assigns that
# setting; entry.py's own DEFAULT_SETTINGS reuses this same string as the checkbox "label" instead
# of a second hand-typed copy, so the two can never drift apart. sys_capability_map reports this
# map (cross-checked against the LIVE registry via has_tool) so an agent that gets a bare "tool not
# found" for one of these has a discoverable next step instead of a dead end.
GATED_TOOLS = {
    "sys_execute_script": "Allow AI to execute arbitrary Fusion API scripts (advanced; security risk)",
}


class Registry:
    """Holds Tool Items keyed by name."""

    def __init__(self):
        self._tools: Dict[str, Item] = {}

    def register(self, item: Item) -> None:
        if not isinstance(item, Item):
            raise ValueError("Can only register Item instances")
        item_type = item.get_type()
        if item_type != "tool":
            raise ValueError(f"Only Tool items can be registered, got type: {item_type}")
        name = item.get_name()
        if name in self._tools:
            raise ValueError(f"Tool with name '{name}' already registered")
        self._tools[name] = item

    def unregister(self, name: str) -> bool:
        """Drop a registered tool by name (a no-op, returning False, if it isn't registered)."""
        return self._tools.pop(name, None) is not None

    def has_tool(self, name: str) -> bool:
        return name in self._tools

    def get_tools(self) -> List[Item]:
        return list(self._tools.values())


_registry_instance: Registry = None


def get_registry() -> Registry:
    global _registry_instance
    if _registry_instance is None:
        _registry_instance = Registry()
    return _registry_instance


def reset_registry() -> None:
    global _registry_instance
    _registry_instance = None


def register(item: Item) -> None:
    get_registry().register(item)


def unregister(name: str) -> bool:
    return get_registry().unregister(name)


def has_tool(name: str) -> bool:
    return get_registry().has_tool(name)


def get_tools() -> List[Item]:
    return get_registry().get_tools()
