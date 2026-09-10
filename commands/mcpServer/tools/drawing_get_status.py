# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Read one durable deferred drawing-operation record without touching Fusion APIs."""

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item
from ..mcp_primitives.registry import register
from ..server import drawing_jobs
from ._common import ok


def handler(request_key: str = "") -> dict:
    key = request_key.strip() if isinstance(request_key, str) else ""
    if not key:
        return ok({"request_key": request_key, "status": "unknown",
                   "note": "Provide the request_key used for deferred drawing work."})
    record = drawing_jobs.get_store().get(key)
    if record is None:
        return ok({"request_key": key, "status": "unknown",
                   "note": "No durable drawing job has this request_key."})
    record.pop("canonical_request", None)
    record.pop("owner_instance", None)
    record["note"] = ("This is the stored operation state and raw terminal tool result. It does not "
                      "add an effect verdict or replay unresolved work.")
    return ok(record)


TOOL_DESCRIPTION = (
    "Poll deferred drawing_create, drawing_update, or drawing_export by caller-known request_key. "
    "It reads stored state and does not replay work."
)

tool = (
    Tool.create_simple(name="drawing_get_status", description=TOOL_DESCRIPTION)
    .add_input_property("request_key", {"type": "string",
            "description": "The same key sent with deferred=true."})
    .add_required_input("request_key")
    .strict_schema()
)
item = Item.create_tool_item(tool=tool, write="read", handler=handler, run_on_main_thread=False)


def register_tool():
    register(item)
