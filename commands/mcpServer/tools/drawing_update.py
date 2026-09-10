# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Refresh the ACTIVE drawing's out-of-date references to the latest saved source design (the API
equivalent of the Refresh button). Staleness is judged per reference - DocumentReference.isOutOfDate
is the real signal (DrawingDocument.isUpToDate reports True even while a reference is stale) - and a
refresh that leaves a reference stale is reported as a failure. The refresh dirties the drawing but
does NOT save it: doc_save afterward, then drawing_export. WRITES (in-session state).
"""

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item
from ..mcp_primitives.registry import register
from ._common import ok, error, safe
from . import _assert
from . import _common
from . import _drawing_common
from . import _outputs

# What this tool RETURNS (declared once; drives the PRODUCES: prose + the assert-present contract test).
RETURNS = [
    _outputs.ReturnsValue("is_up_to_date", "whether the drawing's references are current after the refresh"),
]


def _reference_state(dd):
    """Walk documentReferences -> (stale_count, [{index, is_out_of_date, version}], unread_count).
    Per-reference isOutOfDate is the RELIABLE staleness signal (DrawingDocument.isUpToDate reports
    True even while a reference is stale, so it is deliberately not consulted)."""
    refs = safe(lambda: dd.documentReferences)
    if refs is None:
        return None, None, None
    out, stale, unread = [], 0, 0
    # Each reference's INDEX is published on the wire beside its staleness, so this stays a
    # positional walk: an unreadable reference HOLDS its slot with a null verdict (dropping it would
    # slide every later index onto the wrong one, and its staleness is unknown, not fresh).
    count = _common.counted(lambda: refs.count)
    if count is None:
        return None, None, None
    for i in range(count):
        r = safe(lambda k=i: refs.item(k))
        if r is None:
            unread += 1
            out.append({"index": i, "is_out_of_date": None, "version": None})
            continue
        ood = _common.read_flag(lambda rr=r: rr.isOutOfDate)
        ver = safe(lambda rr=r: rr.version)
        if ood is None:
            unread += 1
        elif ood:
            stale += 1
        out.append({"index": i, "is_out_of_date": ood, "version": ver})
    return stale, out, unread


def handler() -> dict:
    """See TOOL_DESCRIPTION."""
    dd = _drawing_common.active_drawing_document()
    if dd is None or safe(lambda: dd.drawing) is None:
        return error("No drawing to update: the active document is not a drawing. Open the drawing "
                     "(doc_open by file_id) and make it active, then retry.")

    stale_before, refs_before, unread_before = _reference_state(dd)
    if stale_before is None:
        return error("The drawing's document references could not be read, so its staleness cannot be "
                     "determined - refusing to refresh blind.")
    if stale_before == 0:
        # A reference that would not read has UNKNOWN staleness - the payload must not report the
        # drawing verified-fresh over a hole in the walk.
        payload = {
            "updated": False,
            "stale_references_before": 0,
            "is_up_to_date": True if unread_before == 0 else None,
            "references": refs_before,
            "document_modified": _common.read_flag(lambda: dd.isModified),
            "note": "Drawing references are already up to date - nothing to refresh. Edit and SAVE the "
                    "source design first, then this refreshes the drawing's views to match.",
        }
        if unread_before:
            payload["unread_references"] = unread_before
            payload["note"] = (f"{unread_before} reference(s) could not be read (null rows) - their "
                               "staleness is unknown, so up-to-date is unverified. Every readable "
                               "reference is current; nothing to refresh.")
        return ok(payload)

    try:
        call_result = dd.updateAllReferences()
    except Exception as ex:
        return error(f"updateAllReferences failed: {ex}")

    # updateAllReferences returning true is not proof the stale state cleared; the ReferencesFresh
    # postcondition re-walks the references under its own settle wait. This re-read is one immediate
    # sample taken BEFORE that wait, so a row can still read stale on a refresh that then settles.
    stale_after, refs_after, unread_after = _reference_state(dd)
    modified_after = _common.read_flag(lambda: dd.isModified)
    immediate_fresh = stale_after == 0 and unread_after == 0

    payload = {
        "updated": True,
        "stale_references_before": stale_before,
        "is_up_to_date": True if immediate_fresh else None,
        "references": refs_after,
        "document_modified": modified_after,
        "update_call_result": bool(call_result),
        "note": ("Refresh request completed. 'references' and 'is_up_to_date' are the immediate "
                 "readback; the write postcondition waits for stale references to settle. "
                 "document_modified is the post-refresh native flag. Call doc_save to persist, then "
                 "drawing_export for the PDF."),
    }
    if unread_after:
        payload["unread_references"] = unread_after
        payload["note"] = (f"Refreshed, but {unread_after} reference(s) could not be read back "
                           "(null rows) - their post-refresh staleness is unknown, so up-to-date "
                           "is unverified. " + payload["note"])
    elif stale_after:
        payload["note"] = (f"The immediate readback still shows {stale_after} stale reference(s); "
                           "the final verification waits for the refresh to settle. "
                           + payload["note"])
    elif stale_after is None:
        payload["note"] = ("The immediate reference collection could not be counted, so "
                           "up-to-date is unverified. " + payload["note"])
    return ok(payload)


TOOL_DESCRIPTION = (
    "Refresh stale references in the active 2D drawing from its saved source. "
    "Does not save; call doc_save."
)

FULL_DESCRIPTION = TOOL_DESCRIPTION + "\n" + _outputs.produces_block(RETURNS)

tool = (
    Tool.create_simple(name="drawing_update", description=FULL_DESCRIPTION)
    .strict_schema()
)

# enforce_timeout=False: updateAllReferences is a blocking, uninterruptible main-thread call that
# can run past the server's call timeout for a large drawing, and the reference read-back is the
# real proof of success.
item = Item.create_tool_item(tool=tool, write="write", handler=handler, run_on_main_thread=True,
                             enforce_timeout=False, deferred_capable=True,
                             postconditions=[_assert.ReferencesFresh()])


def register_tool():
    register(item)
