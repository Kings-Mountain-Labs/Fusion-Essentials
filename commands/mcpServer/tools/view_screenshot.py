# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.
# Adapted from Autodesk's Fusion MCP add-in sample (MIT-licensed).

"""Capture the Fusion viewport as an MCP image content block, optionally reorienting the camera
first and writing the PNG to local disk."""

import base64

import adsk.core

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import error, safe
from . import _common
from . import _export
from . import _inputs
from . import _view_common

app = adsk.core.Application.get()

# The named views: 'current' (leave the camera as-is) + the shared camera-orientation table,
# which supplies the exact eye/target/up vectors each name maps to.
_VIEWS = ("current",) + tuple(_view_common.VIEW_DIRECTIONS)

_MAX_DIM = 4096

# The pixel size a caller who names none gets. The handler signature and its non-numeric fallback
# both read these, so they cannot state different numbers.
_WIDTH_DEFAULT = 800
_HEIGHT_DEFAULT = 600

# The capture is a PNG whatever the path says - this is the extension _export.prepare_out_path
# appends, so no file lands with PNG bytes under another format's name.
_PNG_EXT = ".png"

# A TargetRef, not an OccurrenceRef: a single-body root design places no occurrence at all, so a
# body handle/name is the only way to frame its part.
_FIT_TO = _inputs.TargetRef("fit_to", allow=("occurrence", "body", "mesh"),
        description="Hides the rest for the shot, then restores them.")


# The frame-on-one-occurrence isolate is shared with view_set(orient, focus=) - ONE visibility walk
# and ONE restore contract, so a fix to either reaches both.
_keep_visible = _view_common.keep_visible


def _isolate_for_fit(name):
    """Hide everything but 'fit_to' (an occurrence's ancestors and descendants stay lit) so vp.fit()
    frames just it - the shared walk, bound to this tool's own input kind so its errors say
    'fit_to'."""
    return _view_common.isolate_for_fit(name, _FIT_TO)


def _restore_message(restore_fit_to):
    """The fit_to restore, plus the sentence naming any bulb it could not put back (None when
    everything came back). A restore that did not take leaves the document changed by a READ tool,
    so EVERY exit that reaches the isolate runs this, not just the successful capture."""
    return _view_common.restore_message(restore_fit_to, "fit_to", "for this shot")


def _restore_camera(vp, saved_camera):
    """Put the user's camera back. Returns the sentence naming the failure, or None.

    EVERY exit that may have moved the camera runs this: a read tool that leaves the viewport
    somewhere else has changed the document's state, and a swallowed restore never says so."""
    if saved_camera is None:
        return None
    try:
        vp.camera = saved_camera
    except Exception as e:
        return (f"The camera could NOT be put back where it was before this shot: {e}. The "
                "viewport is left at the capture camera - view_set(orient) re-aims it.")
    return None


def _active_component_note(design):
    """A one-line warning naming the activated occurrence when a NON-root component is active, or
    None - Fusion renders everything outside it dimmed."""
    # design.activeOccurrence is null exactly when the root is active; comparing activeComponent
    # against rootComponent can never be true, since each property access mints a new proxy.
    occ = safe(lambda: design.activeOccurrence) if design else None
    if occ is None:
        return None
    nm = safe(lambda: occ.name) or "a sub-component"
    return (f"Active component is '{nm}' - everything outside it renders dimmed/translucent in this "
            "image (activation scope, not a lighting issue). Activate the root to see all parts solid.")


def _write_png(b64, path):
    """Decode the captured base64 PNG onto 'path' and read the landed file back for a non-zero
    size. Returns (size_bytes, error)."""
    try:
        raw = base64.b64decode(b64)
    except Exception as e:
        return 0, f"The captured image could not be decoded to write '{path}': {e}"
    try:
        with open(path, "wb") as fh:
            fh.write(raw)
    except Exception as e:
        return 0, f"Could not write the screenshot to '{path}': {e}"
    size, verr = _export.verify_written(path)
    if verr:
        return 0, f"The viewport was captured but {verr}. Treating this as a failure."
    return size, None


def handler(view: str = "current", width: int = _WIDTH_DEFAULT, height: int = _HEIGHT_DEFAULT,
            zoom: float = 1.0, fit_to: str = "", transparent_background=None,
            anti_aliased=None, file_path: str = "") -> dict:
    """See TOOL_DESCRIPTION."""
    view = (view or "current").strip().lower()
    if view not in _VIEWS:
        return error(f"Unknown view '{view}'. Valid: {', '.join(_VIEWS)}")

    try:
        width = max(1, min(int(width), _MAX_DIM))
        height = max(1, min(int(height), _MAX_DIM))
    except Exception:
        width, height = _WIDTH_DEFAULT, _HEIGHT_DEFAULT

    vp = app.activeViewport
    if not vp:
        return error("No active viewport (is a document open?).")

    # The output path is prepared BEFORE the camera moves, so an unusable destination refuses
    # without having reoriented the user's view for a picture that is not going to land.
    out_path, perr = _export.prepare_out_path(file_path, _PNG_EXT)
    if perr:
        return error(perr)

    # 'fit_to' frames the camera on ONE occurrence (best-effort: temporarily isolate it so fit()
    # tightens onto it, then restore visibility). Returns the original visibility so a read tool
    # leaves no permanent change.
    saved_camera = None
    restore_fit_to = None
    want_fit = (fit_to or "").strip()
    if want_fit:
        restore_fit_to, _fit_target, fit_err = _isolate_for_fit(want_fit)
        if restore_fit_to is None:
            return error(fit_err or f"fit_to: nothing matched '{_common.short_ref(want_fit)}'. "
                         "Use design_get(include=['tree']) for occurrences, find_geometry for a body.")

    # Reorient the camera if a specific view was requested, saving the user's current
    # camera so we can restore it afterward (a read tool shouldn't permanently change
    # the user's view as a side effect).
    zoom_warning = None
    camera_snapshot_warning = None
    standoff_note = None
    if view != "current" or want_fit or (zoom and zoom != 1.0):
        # Captured OUTSIDE the try: a failure inside can already have moved the camera, and the
        # failure exit below can only put it back if the snapshot was taken first.
        saved_camera = safe(lambda: vp.camera)
        if saved_camera is None:
            camera_snapshot_warning = (
                "The camera could not be captured before this shot, so the viewport is LEFT at "
                "the capture view (no restore was possible) - view_set(orient) re-aims it.")
        try:
            if view != "current":
                # Every named view resolves in the shared table (_VIEWS is built from it). The
                # orient answers the standoff it FELL BACK to, or None when the camera's own
                # eye-target distance was the one it placed the eye at.
                fallback_cm = _view_common.apply_named_view(vp, view)
                if fallback_cm is not None:
                    cm = f"{fallback_cm:g}"
                    standoff_note = (
                        f"standoff_fallback_cm={cm}: the camera's eye-target distance did not read "
                        f"as a positive number, so the eye was placed {cm} cm from the target along "
                        "the view direction before the fit.")
            else:
                vp.fit()
            # zoom: scale the camera-to-target distance after fitting (>1 zooms OUT, <1 zooms IN).
            z = float(zoom or 1.0)
            if z and z != 1.0 and z > 0:
                cam = vp.camera
                try:
                    cam.viewExtents = cam.viewExtents * z   # smaller extents = zoomed in
                    vp.camera = cam
                except Exception as e:
                    # A dropped zoom is a request the image does not honour - say so rather than
                    # returning a fitted shot as if the zoom had applied.
                    zoom_warning = (f"zoom={z} could NOT be applied ({e}) - the image is at the "
                                    "fitted extents.")
        except Exception as e:
            # The orient failed AFTER the camera may already have moved and AFTER fit_to hid the
            # other occurrences, so this exit owes both restores: the camera goes back here or
            # nowhere, and a bulb the restore could not put back is named here or nowhere.
            cam_msg = _restore_camera(vp, saved_camera)
            stuck_msg = _restore_message(restore_fit_to)
            return error(" ".join(m for m in (f"Failed to set view '{view}': {e}",
                                              cam_msg, stuck_msg) if m))

    try:
        b64, cerr = _view_common.capture_png_b64(
            vp, width, height, transparent_background=transparent_background,
            anti_aliased=anti_aliased)
        result = error(cerr) if cerr else None
        if result is None:
            content = []
            note = _active_component_note(_common.design())
            if note:
                content.append({"type": "text", "text": note})
            written = None
            if out_path:
                # A requested file that did not land is a FAILURE, not an ok carrying the inline
                # image: the caller asked for a file to hand on (drawing_insert_image takes a local
                # path), and a false ok would send it looking for one that is not there.
                size, werr = _write_png(b64, out_path)
                if werr:
                    result = error(werr)
                else:
                    written = (f"PNG also written to disk: file_path={out_path} "
                               f"size_bytes={size}. drawing_insert_image places it on a drawing "
                               "sheet.")
            if result is None:
                if written:
                    content.append({"type": "text", "text": written})
                content.append({"type": "image", "data": b64, "mimeType": "image/png"})
                result = {"content": content, "isError": False}
    except Exception as e:
        result = error(f"Screenshot error: {e}")

    # Restore the user's original camera if we changed it, and the visibility fit_to isolated -
    # each failure is NAMED, never swallowed.
    cam_msg = _restore_camera(vp, saved_camera)
    stuck_msg = _restore_message(restore_fit_to)
    # The standoff rides only a shot that produced an image: a failed capture placed an eye but
    # returned no picture to describe. view_screenshot_multi records the same way.
    placed = standoff_note if not result.get("isError") else None
    extra = " ".join(m for m in (placed, zoom_warning, camera_snapshot_warning,
                                 cam_msg, stuck_msg) if m)
    if extra:
        if result.get("isError"):
            return error(result.get("message", "") + " " + extra)
        result["content"].insert(0, {"type": "text", "text": extra})
    return result


TOOL_DESCRIPTION = (
    "Capture the current Fusion viewport as an image; 'file_path' also writes the PNG to disk, and "
    "the image still returns inline. view_screenshot_multi captures several views at once."
)

tool = (
    Tool.create_simple(name="view_screenshot", description=TOOL_DESCRIPTION)
    .add_input_property(*_inputs.Choice("view", list(_VIEWS), default="current").as_property())
    .add_input_property("width", {"type": "integer"})
    .add_input_property("height", {"type": "integer"})
    .add_input_property("zoom", {"type": "number",
            "description": "Applied after fitting: >1 out, <1 in."})
    .add_input_property(*_FIT_TO.as_property())
    .add_input_property("transparent_background", {"type": "boolean"})
    .add_input_property("anti_aliased", {"type": "boolean"})
    .add_input_property("file_path", {"type": "string",
            "description": "Also write the PNG to this local path."})
    .strict_schema()
)

# write="write": 'file_path' writes a caller-named PNG to local disk and an existing file at that
# path is overwritten without a refusal, so this tool cannot sit in the auto-approve read bucket -
# the name keeps its Acquire verb (see test_tool_naming.py's write-verb exemption).
item = Item.create_tool_item(
    tool=tool, write="write", handler=handler, run_on_main_thread=True,
    verification=Verification(
        kind="inline", rung="value",
        evidence_test="tests/unit/test_view_screenshot.py::TestFilePathWrite"
                      "::test_a_zero_byte_write_is_a_failure_not_an_ok_carrying_the_image"))


def register_tool():
    register(item)
