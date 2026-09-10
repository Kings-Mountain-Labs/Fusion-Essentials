# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""MCP building block: capture SEVERAL views of the model in one call (multi-view "eyes").

Re-orients + fits + saveAsImageFile per requested view (the same mechanism view_screenshot uses for
one viewport per call), restoring the user's camera at the end. Read-only.
"""

import adsk.core

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item
from ..mcp_primitives.registry import register
from ._common import error
from . import _view_common

app = adsk.core.Application.get()

# The named views - the shared camera-orientation table (no 'current' here: every view in a
# multi-shot is an explicit orientation).
_VIEWS = tuple(_view_common.VIEW_DIRECTIONS)
_DEFAULT_VIEWS = ["front", "top", "right", "iso-top-right"]
_ALL_ORTHOS = ["front", "back", "left", "right", "top", "bottom"]
_MAX_DIM = 4096
_MAX_VIEWS = 8
# The per-image pixel size a caller who names none gets - smaller than view_screenshot's, since
# several images ride back in one payload. Held as constants so the clamp fallback and the wire
# sentences cannot state different numbers.
_WIDTH_DEFAULT = 600
_HEIGHT_DEFAULT = 500


def _parse_views(views):
    """Resolve 'views' (a list, or a comma-separated string) to an ordered, de-duplicated list of
    known view names; "" / [] -> the default set, "all" -> the six orthographic views. Returns
    (list, None) or (None, error)."""
    if isinstance(views, (list, tuple)):
        tokens = [str(v).strip().lower() for v in views if str(v).strip()]
    else:
        s = (views or "").strip().lower()
        if s == "all":
            return list(_ALL_ORTHOS), None
        tokens = [tok.strip() for tok in s.split(",") if tok.strip()]
    if not tokens:
        return list(_DEFAULT_VIEWS), None
    if len(tokens) == 1 and tokens[0] == "all":
        return list(_ALL_ORTHOS), None
    out, seen = [], set()
    for name in tokens:
        if name not in _VIEWS:
            return None, (f"Unknown view '{name}'. Valid: {', '.join(_VIEWS)} "
    "(or 'all' for the six orthographic views).")
        if name not in seen:
            seen.add(name)
            out.append(name)
    return out, None


def handler(views=None, width: int = _WIDTH_DEFAULT, height: int = _HEIGHT_DEFAULT,
            transparent_background=None,
            anti_aliased=None) -> dict:
    """See TOOL_DESCRIPTION."""
    names, err = _parse_views(views)
    if err:
        return error(err)
    # Cap the view count, but NAME the dropped views (below) rather than truncating silently.
    dropped = names[_MAX_VIEWS:]
    names = names[:_MAX_VIEWS]

    # Clamp each dimension INDEPENDENTLY: a non-numeric width must not discard a valid height.
    def _clamp_dim(value, default):
        try:
            return max(1, min(int(value), _MAX_DIM))
        except Exception:
            return default
    width = _clamp_dim(width, _WIDTH_DEFAULT)
    height = _clamp_dim(height, _HEIGHT_DEFAULT)

    vp = app.activeViewport
    if not vp:
        return error("No active viewport (is a document open?).")

    content = []
    saved_camera = vp.camera   # restore once at the end
    camera_warning = None
    captured = []
    standoff_views, standoff_cm = [], None
    try:
        for name in names:
            fallback_cm = None
            try:
                # every parsed view name resolves in the shared table (_VIEWS is built from it); the
                # shared apply sets exact world-axis vectors (guaranteed square) + ortho + fit, and
                # answers the eye-target standoff it FELL BACK to when the camera's own did not read.
                fallback_cm = _view_common.apply_named_view(vp, name)
            except Exception as e:
                content.append({"type": "text", "text": f"[{name}] failed to orient: {e}"})
                continue
            b64, cerr = _view_common.capture_png_b64(
                vp, width, height, prefix="fe_mcp_views",
                transparent_background=transparent_background, anti_aliased=anti_aliased)
            if cerr:
                content.append({"type": "text", "text": f"[{name}] capture failed."})
                continue
            content.append({"type": "text", "text": f"View: {name}"})
            content.append({"type": "image", "data": b64, "mimeType": "image/png"})
            captured.append(name)
            if fallback_cm is not None:
                standoff_cm = fallback_cm
                standoff_views.append(name)
    finally:
        try:
            vp.camera = saved_camera
        except Exception as e:
            # This tool is write="read": a camera it cannot put back leaves the viewport moved by
            # a read, so the failure is reported rather than swallowed.
            camera_warning = (f"The camera could NOT be put back where it was before these shots: "
                              f"{e}. The viewport is left at the last captured view - "
                              "view_set(orient) re-aims it.")

    if not captured:
        return error("No views were captured."
                     + (" " + camera_warning if camera_warning else ""))
    summary = (f"Captured {len(captured)} view(s): {', '.join(captured)}. "
               "Each image is labelled with its view above it.")
    if camera_warning:
        summary += " " + camera_warning
    if standoff_views:
        cm = f"{standoff_cm:g}"
        summary += (f" standoff_fallback_cm={cm} on {', '.join(standoff_views)}: the camera's "
                    "eye-target distance did not read as a positive number, so the eye was placed "
                    f"{cm} cm from the target along each of those view directions before the fit.")
    if dropped:
        summary += (f" Dropped {len(dropped)} view(s) over the {_MAX_VIEWS}-view cap: "
                    f"{', '.join(dropped)} - request them in a second call.")
    content.insert(0, {"type": "text", "text": summary})
    return {"content": content, "isError": False}


TOOL_DESCRIPTION = (
    "Capture SEVERAL views of the model in ONE call, as separate labelled images - the shape to "
    "reach for when judging a 3D layout. The camera is put back afterward."
)

tool = (
    Tool.create_simple(name="view_screenshot_multi", description=TOOL_DESCRIPTION)
    .add_input_property("views", {"type": "array",
            "items": {"type": "string", "enum": list(_VIEWS) + ["all"]},
            "description": "['all'] = the six orthographic views; omit for front/top/right/iso."})
    .add_input_property("width", {"type": "integer", "description": "Pixels."})
    .add_input_property("height", {"type": "integer", "description": "Pixels."})
    .add_input_property("transparent_background", {"type": "boolean"})
    .add_input_property("anti_aliased", {"type": "boolean"})
    .strict_schema()
)
item = Item.create_tool_item(tool=tool, write="read", handler=handler, run_on_main_thread=True)


def register_tool():
    register(item)
