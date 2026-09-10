# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Save the ACTIVE document in place - a new cloud version of the same file. Document.save()
returning True is NOT proof a version was created, so VersionAdvanced compares fresh cloud reads."""

import adsk.core

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item
from ..mcp_primitives.registry import register
from ._common import ok, error, safe
from . import _assert
from ._data_common import _agent_description

app = adsk.core.Application.get()


def _report_lineage_change(payload, doc, lineage_before):
    """Report a save that moved the document onto a NEW lineage URN: a caller holding the superseded
    URN learns the new one from this payload, never from a silently swapped acted_on."""
    if not (isinstance(lineage_before, str) and lineage_before.startswith("urn:")):
        return
    lineage_after = safe(lambda: doc.dataFile.id)
    if (isinstance(lineage_after, str) and lineage_after.startswith("urn:")
            and lineage_before != lineage_after):
        payload["lineage_changed"] = {"from": lineage_before, "to": lineage_after}
        payload["note"] = (payload.get("note", "") +
                           " THIS SAVE MOVED THE DOCUMENT TO A NEW LINEAGE URN. Address the file by "
                           "lineage_changed.to from now on - lineage_changed.from opens the file "
                           "this one forked from, and its version history does not "
                           "continue.").strip()


def handler(description: str = "") -> dict:
    """Save the ACTIVE document in place - a new cloud version of the same file."""
    doc = safe(lambda: app.activeDocument)
    if not doc:
        return error("No active document to save.")
    if not safe(lambda: doc.isSaved, False):
        return error("The active document has never been saved (no cloud file yet). Use "
    "doc_save_as to give it a name and folder first.")

    # Nothing to version if the document is clean - Document.save would no-op and return True.
    if not safe(lambda: doc.isModified, True):
        return ok({
            "saved": True,
            "already_current": True,
            "document_name": safe(lambda: doc.name),
            "note": "Document had no unsaved changes - nothing to version.",
        })

    lineage_before = safe(lambda: doc.dataFile.id)
    try:
        did = doc.save(_agent_description(description))  # adsk.core: Document.save(description)
    except Exception as e:
        return error(f"Save failed for '{safe(lambda: doc.name)}': {e}")
    if not did:
        return error(f"Fusion declined to save '{safe(lambda: doc.name)}'.")

    # The postcondition reports local completion separately from fresh cloud version advancement.
    payload = {
        "saved": True,
        "document_name": safe(lambda: doc.name),
        "description": _agent_description(description),
        "note": "Document.save returned true; confirmation fields report local completion and observed cloud version state.",
    }
    _report_lineage_change(payload, doc, lineage_before)
    return ok(payload)


TOOL_DESCRIPTION = (
    "Save the ACTIVE document in place as a new cloud version; a never-saved one needs "
    "doc_save_as.")

tool = (
    Tool.create_simple(
        name="doc_save",
        description=TOOL_DESCRIPTION,
    )
    .add_input_property("description", {"type": "string",
            "description": "Version description (the AI-agent marker is prepended)."})
    .strict_schema()
)
item = Item.create_tool_item(
    tool=tool, write="write", handler=handler, run_on_main_thread=True,
    postconditions=[_assert.VersionAdvanced()])


def register_tool():
    register(item)
