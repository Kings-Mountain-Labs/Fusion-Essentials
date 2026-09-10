# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Activate a Fusion workspace by id, visible name, or alias (e.g. Design <-> Manufacture)."""

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import ok, error
from . import _common
from . import _view_common
from ._view_common import user_interface as _ui

# Friendly aliases -> the workspace id, so callers don't have to know Fusion's
# internal ids. Matching also falls back to the workspace's visible name.
_ALIASES = {
    "design": "FusionSolidEnvironment",
    "model": "FusionSolidEnvironment",
    "manufacture": "CAMEnvironment",
    "manufacturing": "CAMEnvironment",
    "cam": "CAMEnvironment",
}


def handler(workspace: str = "") -> dict:
    """Activate a workspace by id, visible name, or alias (design/manufacture/cam)."""
    want = (workspace or "").strip()
    if not want:
        return error("Provide 'workspace' - an id, visible name, or alias "
    "(e.g. 'design', 'manufacture').")

    target_id = _ALIASES.get(want.lower())  # alias -> id, else None
    want_lower = want.lower()

    try:
        ui = _ui()
    except Exception as e:
        return error(str(e))

    match = None
    available = []
    try:
        for ws in ui.workspaces:
            try:
                available.append(ws.name)
            except Exception:
                pass
            try:
                if (ws.id == want) or (target_id and ws.id == target_id) \
                        or (ws.name and ws.name.lower() == want_lower):
                    match = ws
                    break
            except Exception:
                continue
    except Exception as e:
        return error(f"Could not enumerate workspaces: {e}")

    if not match:
        return error(f"Workspace not found: '{workspace}'. "
                      f"Available: {', '.join(available) or '(none)'}")

    try:
        # An UNREADABLE isActive is not "already active" and not a reason to refuse the switch -
        # only a confirmed True short-circuits; None falls through to the activate + read-back.
        if _common.read_flag(lambda: match.isActive) is True:
            return ok({"switched": False, "active_workspace": match.name,
        "note": "Workspace was already active."})
        # The refusal is worded from WHICHEVER read produced the verdict.
        now, active_name, basis = _view_common.activate_workspace(ui, match)
        if basis == "refused":
            return error(f"Activation of '{match.name}' failed (it may not be valid "
    "to switch to right now, e.g. no document open).")
        if now is False:
            if basis == "flag":
                return error(f"activate() returned true for '{match.name}' but it still reads "
                             "isActive=false - the workspace did not become active.")
            return error(f"activate() returned true for '{match.name}', its isActive flag would "
                         f"not read, and the UI reports "
                         f"'{active_name or 'another workspace'}' as the active workspace - "
                         "the switch did not take.")
        out = {"switched": True, "active_workspace": match.name,
               "activation_verified": now is True}
        if now is None:
            out["note"] = ("activate() returned true, but neither Workspace.isActive nor the UI's "
                           "active workspace could be read back - the switch is UNVERIFIED. "
                           "view_list_workspaces reports which workspace is active.")
        return ok(out)
    except Exception as e:
        return error(f"Failed to switch to '{match.name}': {e}")


TOOL_DESCRIPTION = (
    "Switch the active Fusion workspace; view_list_workspaces lists the targets."
)

tool = Tool.create_with_string_input(
    name="view_switch_workspace",
    description=TOOL_DESCRIPTION,
    input_param_name="workspace",
    input_param_description="Id, visible name, or alias (design/manufacture/cam).",
).strict_schema()
item = Item.create_tool_item(
    tool=tool, write="write", handler=handler, run_on_main_thread=True,
    verification=Verification(
        kind="inline", rung="value",
        evidence_test="tests/unit/test_view_switch_workspace.py::TestSwitchReadBack"
                      "::test_an_activate_that_lies_is_an_error_not_a_switch"),
)


def register_tool():
    register(item)
