# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Hand control to the USER to pick an entity, then HOLD the call until they do or it times out.

The hold blocks the calling HTTP thread on an Event a main-thread handler sets."""

import threading
import time

import adsk.core

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import (ok, error, safe, iter_collection, design as _active_design,
                      design_wide_counts)
from . import _common
from . import _inputs
from . import _outputs
from . import _write_guard
# Every selection record's 'handle' is minted by the shared _selection_record - the key RETURNS
# declares below, and the one sys_get_selection publishes from the same builder.
from ._sys_common import RETURNS, _selection_record, _ui

# 'what' hint -> human phrase for the prompt.
_KIND_HINTS = {
    "face": "a face (a flat or curved surface)",
    "edge": "an edge (a boundary line/curve between faces)",
    "vertex": "a vertex (a corner point)",
    "body": "a body (a whole solid or surface body)",
    "component": "a component/occurrence (a part in the assembly)",
    "any": "a face, edge, vertex, body, or component",
}


# A blocking wait on the main thread would deadlock against the selection-changed event it waits
# on, so this Item is run_on_main_thread=False and marshals its own adsk.* steps over instead.

_MAX_WAIT_SECONDS = 300.0
_DEFAULT_WAIT_SECONDS = 60.0
_SETUP_TIMEOUT_S = 10.0     # the setup/cleanup marshal only - NOT wait_seconds

# One wait at a time, process-wide. A Fusion event handler needs a strong reference or it is
# garbage-collected and silently stops firing, so 'handler' is held here too.
_pending_lock = threading.Lock()
_pending = {"active": False, "handler": None, "started": None}


def _task_manager():
    """The server's TaskManager, imported lazily so loading this module needs no server package."""
    from ..server.task_manager import TaskManager
    return TaskManager


def _call_on_main_thread(fn, kwargs, timeout=_SETUP_TIMEOUT_S):
    """Run fn(**kwargs) on Fusion's main thread (marshaled via TaskManager) and block THIS thread
    until it completes or `timeout` elapses; returns fn's value, or raises fn's exception."""
    tm = _task_manager()
    if not tm.is_running():
        tm.start()
    done = threading.Event()
    box = {}

    def _cb(data):
        try:
            box["result"] = fn(**data["kwargs"])
        except Exception as e:
            box["error"] = e
        finally:
            done.set()

    task_id = tm.post(command="sys_request_selection", callback=_cb, data={"kwargs": kwargs})
    if not task_id:
        return {"error": "Could not reach Fusion's main thread (TaskManager not running)."}
    if not done.wait(timeout=timeout):
        tm.cancel(task_id)
        return {"error": f"Fusion's main thread did not respond within {timeout:g}s."}
    if "error" in box:
        raise box["error"]
    return box.get("result")


def _validate_wait_seconds(raw):
    """(seconds, error). 0 is fire-and-return, up to _MAX_WAIT_SECONDS holds the call; a negative
    or excessive value is REFUSED naming the number, never clamped."""
    try:
        v = float(raw)
    except Exception:
        return None, f"'wait_seconds' must be a number, got {raw!r}."
    if v < 0:
        return None, f"'wait_seconds' must be >= 0 (0 = fire-and-return), got {v:g}."
    if v > _MAX_WAIT_SECONDS:
        return None, f"'wait_seconds' must be <= {_MAX_WAIT_SECONDS:g}, got {v:g}."
    return v, None


def _pickable_counts():
    """(bodies, sketches, occurrences) design-wide, or None when no design is open - the read behind
    the nothing-to-select refusal. Occurrences count separately: an empty component is pickable."""
    d = _active_design()
    if d is None:
        return None
    bodies, sketches = design_wide_counts(d)
    # root.allOccurrences.count RAISES on a design holding an unresolved external reference, so the
    # shared census answers instead of a coerced 0.
    walk = _common.occurrence_walk(d)
    return bodies, sketches, (walk.total or 0)


def _on_selection_changed(args, box, done):
    """The activeSelectionChanged notify() logic: a change TO EMPTY leaves `done` unset, while the
    first non-empty selection fills `box`, sets `done`, and detaches its OWN listener."""
    sels = safe(lambda: args.currentSelection) or []
    if not len(sels):
        return    # a clear/deselect, not a pick - keep waiting for a real one; done stays unset
    try:
        box["result"] = {
            "selection_count": len(sels),
            "selections": [_selection_record(s) for s in sels],
        }
    except Exception as e:
        box["error"] = str(e)
    finally:
        _detach_pick_handler(box.get("handler"))
        done.set()


class _PickHandler(adsk.core.ActiveSelectionEventHandler):
    """Fires on Fusion's main thread whenever the active selection changes."""

    def __init__(self, box, done):
        super().__init__()
        self._box = box
        self._done = done

    def notify(self, args):
        _on_selection_changed(args, self._box, self._done)


def _detach_pick_handler(handler):
    """Remove ONE pick handler from activeSelectionChanged, clearing the pending slot only while it
    still holds THAT handler, so an orphan cannot unhook the listener a LATER hold waits on."""
    if handler is None:
        return
    safe(lambda: _ui().activeSelectionChanged.remove(handler))
    if _pending.get("handler") is handler:
        _pending["handler"] = None


def _begin_request(kind, clear_current, wait_seconds, expect_document):
    """Every adsk.* touch for one request, on the MAIN THREAD -> 'doc_name'/'doc_urn' plus exactly
    one of 'refused' (return as-is), 'immediate' (the full answer), or 'box'+'done' (wait, then
    read box['result'])."""
    doc_name, doc_urn = _write_guard._active_identity()
    out = {"doc_name": doc_name, "doc_urn": doc_urn}
    # The same gate every wrapped write tool gets, called here because the auto-wrap is skipped
    # (write=None) and _document_refusal's read is only legal on the main thread.
    if expect_document:
        refusal = _write_guard._document_refusal(expect_document, doc_name, doc_urn)
        if refusal is not None:
            out["refused"] = refusal
            return out

    ui = _ui()
    if not ui:
        out["refused"] = error("No Fusion user interface available.")
        return out

    # A request against a session with nothing pickable can only time out, so it is refused BEFORE
    # the selection is cleared or a listener registered, on both paths.
    counts = _pickable_counts()
    if counts is None:
        out["refused"] = error(
            "Nothing to select: no design is open in the active document. Open or create a design "
            "with geometry first.")
        return out
    if not any(counts):
        out["refused"] = error(
            "Nothing to select: the active design is empty (bodies=0, sketches=0, occurrences=0 "
            "design-wide) - a selection request here can only time out. Build or open geometry "
            "first; origin construction geometry is referenced through typed plane/axis inputs, "
            "not a UI pick.")
        return out

    hint = _KIND_HINTS[kind]
    cleared = bool(safe(lambda: ui.activeSelections.clear(), False)) if clear_current else None

    if not clear_current:
        sels = safe(lambda: ui.activeSelections)
        count = safe(lambda: sels.count, 0) if sels is not None else 0
        if count:
            selections = [_selection_record(s) for s in iter_collection(sels)]
            note = (_outputs.produces_block(RETURNS)
                    + "\nAlready selected when this call ran (clear_current=false) - returned "
                    "without waiting.")
            payload = {
                "status": "picked",
                "requested_kind": kind,
                "selection_count": count,
                "selections": selections,
                "active_document": doc_name,
                "note": note,
            }
            # iter_collection drops a selection that will not read, so the hole is disclosed.
            if len(selections) < count:
                payload["unread_selections"] = count - len(selections)
                payload["note"] = note + (f" {count - len(selections)} of {count} selection(s) "
                                          "could not be read and are missing from 'selections'.")
            out["immediate"] = ok(payload)
            return out

    if wait_seconds <= 0:
        out["immediate"] = ok({
            "status": "awaiting_selection",
            "awaiting_user_selection": True,
            "requested_kind": kind,
            "cleared_previous_selection": cleared,
            "active_document": doc_name,
            "instructions_for_user": (
                f"Click {hint} in the Fusion window (rotate/zoom as needed). You don't need to "
                "press anything in Fusion - just select it, then confirm here when ready."),
            "next_step": ("Present a one-click confirmation to the user (a structured-output "
                "button). When they click it, call sys_get_selection to read the pick, or call "
                "sys_request_selection again with wait_seconds>0 to hold for the pick directly."),
        })
        return out

    # A survivor from an earlier hold still fires, so it is detached before this hold's listener is
    # added and the two cannot both be live on the same event.
    _detach_pick_handler(_pending.get("handler"))
    box, done = {}, threading.Event()
    handler = _PickHandler(box, done)
    box["handler"] = handler        # the handler each detach is keyed to
    # Only this listener wakes the wait below, so a false add() could only run out the clock.
    if ui.activeSelectionChanged.add(handler) is False:
        _detach_pick_handler(handler)   # best-effort: leave nothing registered behind the refusal
        out["refused"] = error(
            "The selection listener did not register - ui.activeSelectionChanged.add() returned "
            "false. Only that listener wakes this call, so no hold was started and nothing is "
            "waiting on a pick. Retry, or call again with wait_seconds=0 and read the pick back "
            "with sys_get_selection.")
        return out
    _pending["handler"] = handler
    out["box"], out["done"] = box, done
    return out


def _cancel_pending_wait(box):
    """Detach THIS hold's pick listener after a timeout, so a LATE click never fires into a stale
    hold; a marshal that failed leaves it registered, which the timeout payload discloses."""
    _detach_pick_handler(box.get("handler"))
    return {"detached": True}


def _completed_pick(box, kind, setup, extra_note=""):
    """The result for a CAPTURED pick - the one shape whether it woke the wait or landed while the
    wait was expiring."""
    if "error" in box:
        return error(f"Could not read the completed selection: {box['error']}")
    payload = dict(box["result"])
    payload.update({
        "status": "picked",
        "requested_kind": kind,
        "active_document": setup.get("doc_name"),
        "note": _outputs.produces_block(RETURNS) + extra_note,
    })
    return _write_guard._stamp_acted_on(ok(payload), setup.get("doc_name"), setup.get("doc_urn"))


def handler(what: str = "any", clear_current: bool = True,
            wait_seconds: float = _DEFAULT_WAIT_SECONDS,
            expect_document: str = None) -> dict:
    """Ask the user to click an entity in Fusion, then HOLD the call until they do or it times out.
    Runs OFF Fusion's main thread, so the wait blocks only the calling HTTP thread."""
    kind = (what or "any").strip().lower()
    if kind not in _KIND_HINTS:
        kind = "any"

    wait_s, werr = _validate_wait_seconds(wait_seconds)
    if werr:
        return error(werr)

    with _pending_lock:
        if _pending["active"]:
            started = _pending["started"] or time.monotonic()
            elapsed = time.monotonic() - started
            return error(
                f"A sys_request_selection call is already waiting ({elapsed:.1f}s so far) - only one "
                "can be pending at a time. Wait for it to finish or time out, then retry.")
        _pending["active"] = True
        _pending["started"] = time.monotonic()

    try:
        setup = _call_on_main_thread(_begin_request, {
            "kind": kind, "clear_current": bool(clear_current), "wait_seconds": wait_s,
            "expect_document": expect_document,
        })
        if not isinstance(setup, dict):
            return error("Could not set up the selection request (main thread unreachable).")
        if setup.get("error"):
            return error(f"Could not start the selection request: {setup['error']}")
        if setup.get("refused") is not None:
            return setup["refused"]
        if setup.get("immediate") is not None:
            return _write_guard._stamp_acted_on(setup["immediate"], setup.get("doc_name"), setup.get("doc_urn"))

        box, done = setup["box"], setup["done"]
        if done.wait(timeout=wait_s):
            return _completed_pick(box, kind, setup)

        # The listener and this cancel marshal both run on the MAIN thread, so once the marshal
        # returns no further pick can land and a click that beat it is already in the box.
        cancel = _call_on_main_thread(_cancel_pending_wait, {"box": box})
        if done.is_set():
            return _completed_pick(box, kind, setup, extra_note=(
                f"\nThe pick landed as the {wait_s:g}s wait expired and was captured - it is a real "
                "selection, not a timeout."))

        # Timed out: isError stays False, and 'status' is the machine-checkable signal.
        payload = {
            "status": "timeout",
            "requested_kind": kind,
            "waited_seconds": wait_s,
            "active_document": setup.get("doc_name"),
            "note": (f"No selection was made within {wait_s:g}s. Nothing was picked - an expected "
                "outcome, not a tool defect. Do NOT re-fire this tool in a loop: an unanswered hold "
                "usually means the user is not at the Fusion window or never learned a pick was "
                "wanted. Tell the user what to click and that Fusion will wait, get their go-ahead, "
                "then request once more. (sys_get_selection reads a pick made after this hold ended.)"),
        }
        cancel_error = cancel.get("error") if isinstance(cancel, dict) else "the cleanup did not run"
        if cancel_error:
            payload["listener_detached"] = False
            payload["note"] += (f" The pick listener could NOT be detached ({cancel_error}), so it "
                "is still registered in Fusion and will fire on the user's next click, into this "
                "expired hold that nothing is waiting on. The next sys_request_selection detaches "
                "it before registering its own.")
        return ok(payload)
    finally:
        with _pending_lock:
            _pending["active"] = False
            _pending["started"] = None


TOOL_DESCRIPTION = (
    "Hand the pick to the USER: holds the call, and by default clears their current selection.\n"
    + _outputs.produces_block(RETURNS)
)
tool = (
    Tool.create_simple(name="sys_request_selection", description=TOOL_DESCRIPTION)
    .add_input_property(*_inputs.Choice("what", list(_KIND_HINTS), default="any").as_property())
    .add_input_property("clear_current", {"type": "boolean"})
    .add_input_property("wait_seconds", {"type": "number",
            "description": f"Max {_MAX_WAIT_SECONDS:g}; 0 = fire-and-return."})
    .writes()
    # The automatic write guard is bypassed: its wrap touches adsk.* off the main thread, so
    # expect_document is checked inside _begin_request instead, through _write_guard's own functions.
    .add_input_property(*_write_guard.EXPECT_DOCUMENT_PROP)
    .strict_schema()
)
item = Item.create_tool_item(
    tool=tool, write=None, handler=handler,
    run_on_main_thread=False,
    verification=Verification(
        kind="external",
        evidence_test="tests/unit/test_sys_request_selection.py::TestRequestSelectionCompletedPick"
                      "::test_pick_during_wait_returns_handle_and_classification_in_one_call"))


def register_tool():
    register(item)
