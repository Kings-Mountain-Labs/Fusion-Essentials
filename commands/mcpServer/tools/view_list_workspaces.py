# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""List the Fusion workspaces the user can switch to - the targets view_switch_workspace takes."""

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item
from ..mcp_primitives.registry import register
from ._common import ok, error
from ._view_common import user_interface as _ui


def _workspace_summary(ws) -> dict:
    out = {}
    for key, getter in (
        ("id", lambda: ws.id),
        ("name", lambda: ws.name),
        ("product_type", lambda: ws.productType),
        ("is_active", lambda: ws.isActive),
    ):
        try:
            out[key] = getter()
        except Exception:
            out[key] = None
    return out


def handler() -> dict:
    """Return all workspaces the user can switch to, flagging the active one."""
    try:
        ui = _ui()
        workspaces = []
        active = None
        for ws in ui.workspaces:
            summ = _workspace_summary(ws)
            workspaces.append(summ)
            if summ.get("is_active"):
                active = summ.get("name")
        return ok({"active_workspace": active, "workspace_count": len(workspaces),
        "workspaces": workspaces})
    except Exception as e:
        return error(f"Could not list workspaces: {e}")


TOOL_DESCRIPTION = (
    "List the Fusion workspaces, each with its id, visible name, product type and whether it is "
    "active - the targets view_switch_workspace takes."
)

tool = Tool.create_simple(
    name="view_list_workspaces",
    description=TOOL_DESCRIPTION,
).strict_schema()
item = Item.create_tool_item(
    tool=tool, write="read", handler=handler, run_on_main_thread=True
)


def register_tool():
    register(item)
