# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""MCP building block: list the user's Autodesk data hubs (action='list') and SWITCH the active one
(action='switch', hub=<name|id>). The assignment is verified by re-reading activeHub, and a switch
that takes closes every open document."""

import adsk.core

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import iter_collection, ok, error, safe
from . import _inputs

app = adsk.core.Application.get()

_ACTIONS = ("list", "switch")


def _all_hubs(data):
    """Return [(hub, name, id), ...] for every data hub."""
    out = []
    for h in iter_collection(safe(lambda: data.dataHubs)):
        out.append((h, safe(lambda h=h: h.name) or "(unnamed)", safe(lambda h=h: h.id)))
    return out


def handler(action: str = "list", hub: str = "") -> dict:
    """See TOOL_DESCRIPTION."""
    act = (action or "list").strip().lower()
    if act not in _ACTIONS:
        return error(f"Unknown action '{action}'. Use: list, switch.")

    data = safe(lambda: app.data)
    if not data:
        return error("Data not available (not signed in?).")

    active = safe(lambda: data.activeHub)
    active_id = safe(lambda: active.id) if active else None
    hubs = _all_hubs(data)

    if act == "list":
        return ok({
        "active_hub": ({"name": safe(lambda: active.name), "id": active_id} if active else None),
        "hub_count": len(hubs),
        "hubs": [{"name": nm, "id": hid, "is_active": (hid == active_id)} for (_, nm, hid) in hubs],
        })

    # switch
    want = (hub or "").strip()
    if not want:
        return error("Provide 'hub' - the name or id of the hub to switch to (see action='list').")

    # match by id first (exact), then by case-insensitive name
    target = None
    for (h, nm, hid) in hubs:
        if hid == want:
            target = (h, nm, hid)
            break
    if target is None:
        wl = want.lower()
        for (h, nm, hid) in hubs:
            if (nm or "").strip().lower() == wl:
                target = (h, nm, hid)
                break
    if target is None:
        names = ", ".join(nm for (_, nm, _) in hubs) or "(none)"
        return error(f"No hub matched '{want}'. Available: {names}.")

    th, tname, tid = target
    if tid == active_id:
        return ok({
        "switched": False,
        "already_active": True,
        "active_hub": {"name": tname, "id": tid},
        "note": f"'{tname}' is already the active hub - nothing to do.",
        })

    # An assignment that returns is not an assignment that landed, so the active hub is re-read and
    # the switch is judged by that read alone.
    assign_error = None
    try:
        data.activeHub = th
    except Exception as e:
        assign_error = str(e)

    new_active = safe(lambda: data.activeHub)
    new_id = safe(lambda: new_active.id) if new_active else None
    if new_id != tid:
        now = (safe(lambda: new_active.name) or new_id) if new_active else None
        return error(
            f"Could not switch to hub '{tname}' ({tid}): the re-read after the assignment shows the "
            f"active hub as {now!r}, not the target"
            + (f" - the assignment raised: {assign_error}" if assign_error else "")
            + ". Switch hubs from the Fusion data panel (the hub dropdown), then retry.")

    return ok({
        "switched": True,
    "already_active": False,
    "active_hub": {"name": safe(lambda: new_active.name) or tname, "id": new_id or tid},
    "note": ("Active hub switched. This CLOSES documents open before the switch (Fusion reloads the "
            "data context). Re-list projects with data_get, and re-resolve any URNs - "
            "they are hub-scoped. Reopen the document you need on the new hub."),
    })


TOOL_DESCRIPTION = (
    "SWITCH the active Autodesk data hub; a switch that takes closes every open document. List "
    "hubs with data_get(include=['hubs'])."
)

tool = (
    Tool.create_simple(name="data_switch_hub", description=TOOL_DESCRIPTION)
    .add_input_property(*_inputs.Choice("action", list(_ACTIONS), default="list").as_property())
    .add_input_property("hub", {"type": "string", "description": "Hub name or id."})
    .strict_schema()
)

item = Item.create_tool_item(
    tool=tool, write="write", handler=handler, run_on_main_thread=True,
    verification=Verification(
        kind="inline",
        evidence_test="tests/unit/test_data_switch_hub.py::TestTheReReadDecides"
                      "::test_silent_noop_setter_reports_honest_error_not_false_success",
        rung="value"))


def register_tool():
    register(item)
