# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Close an open document, or every one of them. app.documents includes referenced/dependency docs
with no visible tab, so close_all closes those too. DESTRUCTIVE (unsaved edits are discarded)."""

import adsk.core

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import iter_collection, ok, error, read_flag, safe
from ._doc_common import _resolve_open_document

app = adsk.core.Application.get()

# MEASURED: a closed document's wrapper reads isValid False without raising. So a
# close() that answered true over a wrapper STILL reading valid did not take.
_STILL_VALID = "close returned true but the document still reads isValid=true"


def handler(name: str = "", save_changes: bool = False,
            close_all: bool = False) -> dict:
    """Close an open document (or all), discarding or saving unsaved changes; see TOOL_DESCRIPTION."""
    docs = safe(lambda: app.documents)
    if docs is None:
        return error("No documents are open.")

    if close_all:
        targets = list(iter_collection(docs))
    elif name.strip():
        d, refusal = _resolve_open_document(name, "close")
        if refusal is not None:
            return refusal
        targets = [d]
    else:
        active = safe(lambda: app.activeDocument)
        if not active:
            return error("No active document to close.")
        targets = [active]

    closed, errors, skipped_invalid = [], [], 0
    closed_identities = []
    unconfirmed = []
    for d in targets:
        # A close_all closes reference/dependency docs too; closing one INVALIDATES its now-orphaned
        # reference proxies, so a later close on such a dead proxy raises a cosmetic error. Skip a proxy
        # that is already invalid (count it, don't fail the call over it).
        if safe(lambda d=d: d.isValid, True) is False:
            skipped_invalid += 1
            continue
        nm = safe(lambda d=d: d.name)
        # The closed document's identity is read BEFORE the close - afterwards the document is gone
        # and neither its name nor its lineage URN reads back.
        ident = {"name": nm, "document_id": safe(lambda d=d: d.dataFile.id)}
        try:
            if d.close(bool(save_changes)):
                # read_flag, not safe(..., False): a flag that did not answer is not a closed
                # document, and it is not an open one either.
                still = read_flag(lambda d=d: d.isValid)
                if still is True:
                    errors.append({nm: _STILL_VALID})
                    continue
                if still is None:
                    unconfirmed.append(nm)
                closed.append(nm)
                closed_identities.append(ident)
            else:
                errors.append({nm: "close returned false"})
        except Exception as e:
            # If the close itself invalidated it (it was a dead proxy after all), that's not a failure.
            if safe(lambda d=d: d.isValid, True) is False:
                skipped_invalid += 1
            else:
                errors.append({nm: str(e)[:60]})

    if not closed and errors:
        detail = "; ".join(f"{nm}: {msg}" for e in errors for nm, msg in e.items())
        return error(f"Close failed: {detail}. No document was closed.")

    note = (("Closed " + ("with save" if save_changes else "discarding unsaved changes") +
             ". Fusion keeps at least one document open.") if closed else
            "No document was closed.")
    if skipped_invalid:
        note += f" Skipped {skipped_invalid} already-invalidated reference doc(s)."
    if errors:
        note += f" {len(errors)} of {len(targets)} target(s) failed to close - see 'errors'."
    if unconfirmed:
        note += (f" {len(unconfirmed)} closed document(s) did not answer isValid afterwards, so the "
                 "close is UNCONFIRMED for them - see 'close_unconfirmed'.")
    payload = {
    "closed": closed, "closed_count": len(closed),
    "errors": errors,
    "close_unconfirmed": unconfirmed,
    "skipped_invalid": skipped_invalid,
    "save_changes": bool(save_changes),
    "remaining_open": safe(lambda: app.documents.count),
    "note": note,
    }
    # The write guard stamps acted_on from the POST-call ACTIVE document, which a close never leaves
    # pointing at what it closed, so this handler publishes acted_on itself. Several documents - or
    # none - publish an explicit null, which the guard's fill-if-absent stamp then leaves alone.
    if len(closed_identities) == 1:
        payload["acted_on"] = closed_identities[0]
    elif len(closed_identities) > 1:
        payload["acted_on"] = None
        payload["note"] += (f" acted_on is null: {len(closed_identities)} documents were closed and "
                            "one identity cannot name them - 'closed' (with 'closed_count') is the "
                            "record of which documents closed.")
    else:
        payload["acted_on"] = None
        payload["note"] += (f" acted_on is null: none of the {len(targets)} target(s) closed, so no "
                            "document was acted on - 'skipped_invalid' counts the targets skipped "
                            "as already invalidated.")
    return ok(payload)


TOOL_DESCRIPTION = (
    "Close an open document, or every one (close_all). Unsaved edits are DISCARDED unless "
    "save_changes=true.")

tool = (
    Tool.create_simple(
        name="doc_close",
        description=TOOL_DESCRIPTION,
    )
    .add_input_property("name", {"type": "string",
            "description": "A name, a URN / web URL, document_handle, or 'open:N' (doc_get); omit = active."})
    .add_input_property("save_changes", {"type": "boolean"})
    .add_input_property("close_all", {"type": "boolean",
            "description": "Also closes referenced documents that have no tab."})
    .strict_schema()
)
item = Item.create_tool_item(
    tool=tool, write="destructive", handler=handler,
    run_on_main_thread=True,
    # isValid is read off each document AFTER its close and a wrapper still reading valid is an
    # error, not a closed document; a flag that did not answer publishes close_unconfirmed.
    verification=Verification(
        kind="inline",
        evidence_test="tests/unit/test_doc_close.py::TestCloseReadBack"
                      "::test_a_close_that_leaves_the_document_valid_is_an_error",
        rung="value"))


def register_tool():
    register(item)
