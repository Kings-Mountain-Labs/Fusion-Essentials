# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Generate a machinist SETUP SHEET (HTML or Excel) for a setup/folder/operation or the whole
document. generateSetupSheet returns True BEFORE the sheet exists - a temp PNG lands first, then
the sheet arrives asynchronously and consumes it (live-measured) - so success here is gated on the
sheet file actually landing, advanced by a bounded adsk.doEvents pump.
"""

import os
import time

import adsk.cam
import adsk.core

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item
from ..mcp_primitives.registry import register
from . import _assert
from . import _export
from . import _inputs
from . import _outputs
from ._cam_common import get_cam, resolve_cam_node
from ._common import error, ok, safe

app = adsk.core.Application.get()

# The async landing is sub-second live (~0.3s of pumping); the cap only bounds a wedged generation.
_PUMP_SECONDS = 10.0
_PUMP_POLL_SLEEP = 0.05
_SHEET_SUFFIXES = {"html": (".html", ".htm"), "excel": (".xlsx", ".xls")}

_FORMAT = _inputs.Choice(
    "format", ["html", "excel"], default="html",
    description="excel needs Windows.")

RETURNS = [
    _outputs.ReturnsValue("file_path", "the setup-sheet document written to disk"),
]


def _sheet_files(folder, suffixes):
    """{path: (mtime, size)} for the sheet-format files in folder (empty if it does not exist)."""
    snap = {}
    if safe(lambda: os.path.isdir(folder), False):
        for n in safe(lambda: os.listdir(folder), []) or []:
            if not n.lower().endswith(suffixes):
                continue
            p = os.path.join(folder, n)
            if safe(lambda p=p: os.path.isfile(p), False):
                snap[p] = (safe(lambda p=p: os.path.getmtime(p), 0.0),
                           safe(lambda p=p: os.path.getsize(p), 0))
    return snap


def handler(scope: str = "", format: str = "html", output_folder: str = "") -> dict:
    """See TOOL_DESCRIPTION."""
    values, verr = _inputs.resolve_inputs([_FORMAT], {"format": format})
    if verr:
        return verr
    fmt_key = values["format"]

    cam, cerr = get_cam()
    if cerr:
        return error(cerr)
    if not (output_folder or "").strip():
        return error("Provide 'output_folder' - the directory the setup sheet will be written to.")
    out_dir = os.path.abspath(output_folder.strip())
    try:
        os.makedirs(out_dir, exist_ok=True)
    except Exception as e:
        return error(f"Could not create output folder '{out_dir}': {e}")

    # Resolve the scope up front so a bad name fails before anything is generated.
    target, kind = (None, "document")
    want = (scope or "").strip()
    if want and want.lower() not in ("all", "document", "*"):
        node, serr = resolve_cam_node(cam, want, kinds=("setup", "folder", "operation"),
                                      label="setup/folder/operation")
        if serr:
            return error(serr + " Omit 'scope' to sheet the whole document.")
        target, kind = node.obj, node.kind

    fmt_enum = safe(lambda: (adsk.cam.SetupSheetFormats.ExcelFormat if fmt_key == "excel"
                             else adsk.cam.SetupSheetFormats.HTMLFormat))
    if fmt_enum is None:
        return error("adsk.cam.SetupSheetFormats is unavailable on this Fusion version.")

    suffixes = _SHEET_SUFFIXES[fmt_key]
    before = _sheet_files(out_dir, suffixes)
    started = time.time()

    # openDocument=False ALWAYS: the API's default True opens the generated sheet in the UI, an
    # unsolicited window the calling agent cannot see or close.
    try:
        if target is None:
            did = cam.generateAllSetupSheets(fmt_enum, out_dir, False)
        else:
            did = cam.generateSetupSheet(target, fmt_enum, out_dir, False)
    except Exception as e:
        return error(f"Setup-sheet generation failed: {e}")
    if not did:
        return error(f"Fusion declined to generate the setup sheet (returned false) for "
                     f"{kind} scope '{want or 'document'}' - nothing was written.")

    # The sheet lands asynchronously and only advances while the main thread pumps, and the file
    # appears at 0 bytes before it is written - so the deliverable is judged by a new or modified
    # file that is NON-EMPTY and stable across two samples, never by the bool.
    prev_sizes = None

    def probe():
        nonlocal prev_sizes
        now = _sheet_files(out_dir, suffixes)
        fresh = {p: v for p, v in now.items() if p not in before or v != before[p]}
        sizes = {p: v[1] for p, v in fresh.items()}
        stable = bool(fresh) and all(sz > 0 for sz in sizes.values()) and sizes == prev_sizes
        prev_sizes = sizes
        return stable, fresh

    # _PUMP_SECONDS bounds generation AND landing together, so the wait gets what is left of it.
    stable, fresh = _export.pump_until(probe, _PUMP_SECONDS - (time.time() - started),
                                       _PUMP_POLL_SLEEP)
    landed = fresh if stable else {}
    if not landed:
        return error(f"generateSetupSheet returned true but no {fmt_key} sheet landed in "
                     f"'{out_dir}' within {int(_PUMP_SECONDS)}s - the generation did not complete, "
                     "so there is no deliverable to report.")

    path = sorted(landed)[0]
    overwrote = path in before
    payload = {
        "generated": True,
        "format": fmt_key,
        "scope": (want or "document"),
        "scope_kind": kind,
        "file_path": path,
        "size_bytes": landed[path][1],
        "overwrote_existing": overwrote,
        "note": "Setup sheet written. The file is named after the DOCUMENT, not the scope, so "
                "another call into this folder OVERWRITES it - use a distinct output_folder per "
                "sheet you want to keep.",
    }
    if overwrote:
        payload["note"] = ("Setup sheet written OVER an existing sheet of the same name (the file "
                           "is named after the document, not the scope). " + payload["note"])
    return ok(payload)


TOOL_DESCRIPTION = (
    "Generate a machinist SETUP SHEET, named after the DOCUMENT - a second call to the same folder "
    "overwrites it.\n"
    + _outputs.produces_block(RETURNS)
)

tool = (
    Tool.create_simple(name="cam_generate_setup_sheet", description=TOOL_DESCRIPTION)
    .add_input_property("scope", {"type": "string",
        "description": "Omit for all setups."})
    .add_input_property(_FORMAT.name, _FORMAT.schema())
    .add_input_property("output_folder", {"type": "string"})
    .strict_schema()
)
item = Item.create_tool_item(tool=tool, write="write", handler=handler, run_on_main_thread=True,
                             postconditions=[_assert.DeliverablesExist()])


def register_tool():
    register(item)
