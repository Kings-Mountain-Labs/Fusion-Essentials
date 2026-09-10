# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""MCP building block: open a Fusion document from a data-model identifier (a lineage/versioned
URN or a Fusion web URL). openUsingContext handles both normal and configured designs (open()
rejects a configured design); the is_cam_template flag guards the crash-prone API open of a
multi-reference CAM template.
"""

import time

import adsk.core

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import counted, ok, error, safe, read_flag
from ._data_common import _b64url_decode, _urn_candidates, _resolve_data_file
from . import _write_guard

app = adsk.core.Application.get()


# A multi-reference CAM template can crash Fusion via the API open path - do not resolve/touch
# its reference graph here even to inspect it.


def _open_document(data_file):
    """Open a DataFile, returning (doc, method, error); prefers openUsingContext, falling back to
    open() only if it is unavailable."""
    # Preferred path: openUsingContext with a default context.
    try:
        ctx = adsk.core.FileOpenContext.create()
        doc = app.documents.openUsingContext(data_file, ctx, True)
        if doc:
            return doc, "openUsingContext", None
    except Exception as e:
        ctx_err = str(e)
        # Fall back to plain open() (works for normal designs; not for configured ones).
        try:
            doc = app.documents.open(data_file, True)
            if doc:
                return doc, "open", None
            return None, None, f"open() returned no document (openUsingContext: {ctx_err})"
        except Exception as e2:
            return None, None, f"openUsingContext failed ({ctx_err}); open() failed ({e2})"
    return None, None, "openUsingContext returned no document"


def handler(file_id: str = "", is_cam_template: bool = False,
            force_api_open: bool = False) -> dict:
    """See TOOL_DESCRIPTION."""
    raw = (file_id or "").strip()
    if not raw:
        return error("Provide 'file_id' - a DataFile id or URL from the data-model tools: "
    "a lineage 'id', a 'versionId', or a 'fusionWebURL'/'source_url'.")

    # CAM template (wins over force_api_open): refuse the API open WITHOUT resolving/touching the
    # DataFile at all - even resolving it (findFileById + reference walk) has crashed Fusion. Just
    # return the UI-open instruction with the id the operator/agent already holds.
    if is_cam_template:
        return ok({
        "opened": False,
        "refused_api_open": True,
        "acted_on": None,
        "file_id": raw,
        "note": "This is declared a multi-reference CAM template. Opening it (or even resolving "
        "its references) via the API crashes Fusion, so the API open is refused. Open "
        "it MANUALLY in the Fusion UI (Data Panel -> the document), then confirm with "
        "workspace_orient before continuing. This is the only stable path for these docs.",
        })

    # DECLARE-INTENT default: on a multi-ref CAM template the API resolve/open (findFileById +
    # openUsingContext) is the crash on Fusion 2705.1.4, and CAM-ness cannot be auto-detected.
    # Re-probe on the next major: force_api_open=true on a COPY of one template, scratch session.
    if not force_api_open:
        return error(
    "doc_open needs you to DECLARE INTENT. "
    "Pass force_api_open=true to open a NORMAL document via the API, OR is_cam_template=true "
    "if this is a multi-reference CAM/Manufacture template (the tool then instructs a safe UI "
    "open - the API open crashes Fusion for those). Nothing was resolved or opened.")

    data_file, resolved, candidates = _resolve_data_file(raw)
    if not data_file:
        tried = ", ".join(candidates) if candidates else raw
        return error(f"Could not resolve '{raw}' to a file. Tried: {tried}. Pass a DataFile "
    "'id'/'versionId' or a 'fusionWebURL' from data_get / "
    "design_get(include=['tree']) / cam_get(include=['references']) (it may not exist or you may "
    "lack access).")

    is_configured = bool(safe(lambda: data_file.isConfiguredDesign, False))

    # An assembly's open pulls its whole reference family in, so the cost is counted rather than
    # guessed at: the open documents before and after, and the seconds the call itself took.
    open_before = counted(lambda: app.documents.count)
    started = time.monotonic()
    doc, method, err = _open_document(data_file)
    elapsed = round(time.monotonic() - started, 1)
    if not doc:
        return error(f"Failed to open '{safe(lambda: data_file.name) or raw}': {err}")
    open_after = counted(lambda: app.documents.count)
    loaded = (open_after - open_before
              if open_before is not None and open_after is not None else None)

    info = {
    "opened": True,
    "document_name": safe(lambda: doc.name),
    # EQUALITY, never identity: Document wrappers are not identity-stable (measured live -
    # `is` reads False for the active document itself); `==` compares the underlying handle.
    "is_active": read_flag(lambda: app.activeDocument == doc),
    "is_configured_design": is_configured,
    "open_method": method,
    "resolved_id": resolved,
    # This document's OWN direct references, the same read workspace_orient publishes - null when
    # the collection did not read. MEASURED: an assembly reading 9 here loaded 27 documents, so
    # this is not the count the open walked; doc_get's open_count is that one.
    "referenced_documents": counted(lambda: doc.documentReferences.count),
    # What the open actually COST: the seconds it ran, and how many more documents are open now
    # than were before it. null where either census did not read - never a coerced 0.
    "open_seconds": elapsed,
    "documents_loaded": loaded,
    "note": None,
    }
    # Publish the opened handle, and name it as acted_on only after active-document equality confirms it.
    info["document_handle"] = _write_guard.document_handle(doc)
    info["acted_on"] = ({
        "name": info["document_name"],
        "document_id": safe(lambda: doc.dataFile.id),
        "document_handle": info["document_handle"],
    } if info["is_active"] is True else None)
    if resolved and resolved != raw:
        # Be transparent that we normalized a URL/alternate form to a URN.
        info["input"] = raw

    if is_configured:
        info["configured_design_note"] = (
    "This is a Configured Design. It is open at its active configuration. Call "
    "design_get(include=['configurations']), then design_configure(action='activate', "
    "name=...) to switch."
        )

    # Opening a cloud document is asynchronous, and this handler runs on the same main thread that
    # loads it - blocking here would stall the load AND freeze the UI, so the status is reported as
    # read and the caller is told how to confirm.
    parts = []
    if loaded is not None and loaded > 1:
        parts.append(f"This open put {loaded} documents in the session in {elapsed}s - "
                     "'referenced_documents' counts only the DIRECT ones, so it is not that number. "
                     "doc_get reports the session census as open_count.")
    if info["is_active"] is False:
        parts.append(
        "Document is still loading (open is asynchronous). Call workspace_orient after a "
        "moment to confirm it has become the active document before operating on it."
        )
    elif info["is_active"] is None:
        parts.append(
        "The active-document check was unreadable, so activation is unconfirmed. Call doc_get or "
        "workspace_orient before operating on the opened document."
        )
    if parts:
        info["note"] = " ".join(parts)

    return ok(info)


TOOL_DESCRIPTION = (
    "Open a saved cloud document by its data-model id; it becomes the active document."
)

tool = (
    Tool.create_with_string_input(
        name="doc_open",
        description=TOOL_DESCRIPTION,
        input_param_name="file_id",
        input_param_description="A lineage or versioned URN, or a Fusion web URL.",
    )
    .add_input_property("force_api_open", {"type": "boolean",
            "description": "For a NORMAL document."})
    .add_input_property("is_cam_template", {"type": "boolean",
            "description": "For a multi-reference CAM template; a UI open is instructed."})
    .strict_schema()
)

# enforce_timeout=False: the open is a blocking, uninterruptible main-thread call that COMMITS - a
# large assembly's ran past the server's cap and the document was open regardless, so the timeout
# only replaced this payload with a false failure.
item = Item.create_tool_item(
    tool=tool, write="write", handler=handler, run_on_main_thread=True,
    enforce_timeout=False,
    # is_active compares app.activeDocument against the document THIS call opened, and the poller
    # names the active document again once the async load has caught up.
    verification=Verification(
        kind="deferred", poller="workspace_orient",
        evidence_test="tests/unit/test_doc_open.py::TestAsyncLoadHandoff"
                      "::test_a_document_not_yet_active_claims_no_load_and_names_the_poller",
        rung="value"))


def register_tool():
    register(item)
