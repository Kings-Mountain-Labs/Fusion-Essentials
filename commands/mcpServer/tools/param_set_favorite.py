# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Toggle a user parameter's 'favorite' flag, publishing the flag as the parameter reads it back."""

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import ok, error, safe
from . import _common


def handler(name: str = "", favorite: bool = True) -> dict:
    """Toggle a user parameter's 'favorite' flag (whether it shows in the favorites list). WRITES."""
    name = (name or "").strip()
    if not name:
        return error("Provide 'name'.")
    design = _common.design()
    if not design:
        return error("No active design.")
    p = safe(lambda: design.userParameters.itemByName(name))
    if not p:
        return error(f"No USER parameter named '{name}'.")
    try:
        p.isFavorite = bool(favorite)
    except Exception as e:
        return error(f"Could not set favorite on '{name}': {e}")
    return ok({"name": name, "favorite": safe(lambda: p.isFavorite)})


TOOL_DESCRIPTION = ("Toggle a user parameter's 'favorite' flag (whether it appears in the favorites "
            "list).")

tool = (
    Tool.create_with_string_input(
        name="param_set_favorite",
        description=TOOL_DESCRIPTION,
        input_param_name="name",
        input_param_description="User parameter name.",
    )
    .add_input_property("favorite", {"type": "boolean",
            "description": "Favorite on/off."})
    .strict_schema()
)
item = Item.create_tool_item(
    tool=tool, write="write", handler=handler, run_on_main_thread=True,
    verification=Verification(
        kind="effect", rung="value",
        evidence_test="tests/unit/test_param_set_favorite.py::TestFavoriteHandler"
                      "::test_a_stuck_flag_is_published_as_it_reads_not_as_asked"))


def register_tool():
    register(item)
