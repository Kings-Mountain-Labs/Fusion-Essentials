# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Bring an open document to the foreground. The switch is ASYNC, so the verified state is what is
reported: 'activated' reads "pending" while the foreground has not caught up. WRITES."""

import adsk.core

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import ok, error, safe
from ._doc_common import _resolve_open_document

app = adsk.core.Application.get()


def handler(name: str = "") -> dict:
    """Bring an open document to the foreground (make it the active document)."""
    if not name.strip():
        return error("Provide 'name' - the open document to activate (a display name, or a lineage "
                     "URN / web URL to be unambiguous).")
    d, refusal = _resolve_open_document(name, "activate")
    if refusal is not None:
        return refusal
    try:
        did = d.activate()
    except Exception as e:
        return error(f"Activate failed for '{safe(lambda: d.name)}': {e}")
    # Document.activate() returns whether the CALL was accepted, but the switch is ASYNC, so the
    # verified state is reported: 'activated' is "pending" while the foreground has not caught up.
    # EQUALITY, never identity - `is` reads False for the very document that IS active.
    is_active = bool(safe(lambda: app.activeDocument == d, False))
    out = {
        "activated": True if is_active else ("pending" if did else False),
        "document_name": safe(lambda: d.name),
        "is_active": is_active,
        "acted_on": ({"name": safe(lambda: d.name), "document_id": safe(lambda: d.dataFile.id)}
                     if is_active else None),
    }
    if did and not is_active:
        out["note"] = ("Switch ACCEPTED but not yet active - activation is async and hasn't propagated. "
                       "Call doc_get to confirm it took before acting on the new document.")
    return ok(out)


TOOL_DESCRIPTION = (
    "Bring an open document to the foreground (make it the active document).")

tool = (
    Tool.create_with_string_input(
        name="doc_activate",
        description=TOOL_DESCRIPTION,
        input_param_name="name",
        input_param_description="Doc to activate: a display name, a URN / web URL, document_handle, or 'open:N' (doc_get).",
    ).strict_schema()
)
item = Item.create_tool_item(
    tool=tool, write="write", handler=handler,
    run_on_main_thread=True,
    # The poller reports WHICH document is active, which is the requested value here; this call
    # already compares app.activeDocument against the document it activated and says 'pending'
    # rather than true while that comparison has not caught up.
    verification=Verification(
        kind="deferred", poller="doc_get",
        evidence_test="tests/unit/test_doc_activate.py::TestActivateDocument"
                      "::test_activate_async_pending_reports_pending_not_true",
        rung="value"))


def register_tool():
    register(item)
