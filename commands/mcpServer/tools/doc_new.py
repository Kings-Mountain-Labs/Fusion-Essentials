# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Create and open a new, empty Fusion design document; it becomes the active document. WRITES."""

import adsk.core

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import ok, error, safe
from . import _write_guard

app = adsk.core.Application.get()


def handler() -> dict:
    """Create and open a new, empty Fusion design document; it becomes the active document."""
    try:
        doc = app.documents.add(adsk.core.DocumentTypes.FusionDesignDocumentType)
    except Exception as e:
        return error(f"Failed to create a new design document: {e}")
    if not doc:
        return error("New-document creation returned nothing.")

    # EQUALITY, never identity or name: Document wrappers are not identity-stable (`is` reads False
    # even for the one active document) while `==` compares the underlying handle, and a NAME
    # compare would false-positive on two 'Untitled' docs.
    new_name = safe(lambda: doc.name)
    is_active = bool(safe(lambda: app.activeDocument == doc, False))
    handle = _write_guard.document_handle(doc)
    info = {
    "created": True,
    "document_handle": handle,
    "acted_on": {"name": new_name, "document_id": safe(lambda: doc.dataFile.id),
                 "document_handle": handle},
    "document_name": new_name,
    "is_active": is_active,
    "is_saved": safe(lambda: doc.isSaved),
    "note": ("New blank design created. Use document_handle with doc_activate/expect_document; "
             "it expires on close/add-in reload. Check is_active before modelling; save with doc_save_as."),
    }
    return ok(info)


TOOL_DESCRIPTION = (
    "Create and open a new, empty design document; it becomes active and stays unsaved until "
    "doc_save_as. Start modelling with sketch_create."
)

tool = Tool.create_simple(
    name="doc_new",
    description=TOOL_DESCRIPTION,
).strict_schema()
item = Item.create_tool_item(
    tool=tool, write="write", handler=handler, run_on_main_thread=True,
    # is_active compares app.activeDocument against the document this call created. The tool takes
    # no inputs, so there is no requested value for a read-back to be held against.
    verification=Verification(
        kind="effect",
        evidence_test="tests/unit/test_doc_new.py::TestNewDocument"
                      "::test_a_different_active_document_reads_inactive",
        rung="exists")
)


def register_tool():
    register(item)
