# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Poll a data_upload_file upload for its real uploading/processing/complete/failed state, so the
caller never has to re-list files and guess when cloud translation finished. Reads the live
DataFileFuture kept referenced in _data_common._UPLOADS. NON-BLOCKING: reports the CURRENT observed
state and returns immediately - never sleeps or hot-loops.
"""

import time

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item
from ..mcp_primitives.registry import register
from ._common import ok, error, safe
from . import _data_common


def _find_by_name(file_name, folder):
    """Search _data_common._UPLOADS (most-recent-first) for an entry matching source_file (+ folder if
    given). Returns (handle, entry) or (None, None)."""
    want_name = (file_name or "").strip().lower()
    want_folder = (folder or "").strip().lower()
    if not want_name:
        return None, None
    for h in reversed(list(_data_common._UPLOADS.keys())):
        entry = _data_common._UPLOADS[h]
        if (entry.get("source_file") or "").lower() != want_name:
            continue
        if want_folder and (entry.get("destination_folder") or "").lower() != want_folder:
            continue
        return h, entry
    return None, None


def handler(handle: str = "", file_name: str = "", folder: str = "") -> dict:
    """Report an upload's current state; see TOOL_DESCRIPTION."""
    if not _data_common._UPLOADS:
        return error("No uploads have been launched in this session. Call data_upload_file first.")

    key = (handle or "").strip()
    entry = None

    if key and key.lower() != "latest":
        entry = _data_common._UPLOADS.get(key)
        if not entry:
            return error(f"No upload with handle '{handle}'. Active handles: "
                         f"{', '.join(_data_common._UPLOADS.keys()) or '(none)'}.")
    elif key.lower() == "latest" or (not key and not file_name):
        # 'latest' = the newest STILL-TRACKED upload, read from the live dict in insertion order:
        # the mint counter climbs past entries popped at their terminal state, so a handle derived
        # from it names an upload already gone while an older one is still running.
        key = list(_data_common._UPLOADS)[-1]
        entry = _data_common._UPLOADS[key]

    if entry is None and file_name:
        key, entry = _find_by_name(file_name, folder)
        if not entry:
            where = f" folder='{folder}'" if folder else ""
            return error(f"No tracked upload matches file_name='{file_name}'{where}. Active "
                         f"handles: {', '.join(_data_common._UPLOADS.keys()) or '(none)'}.")

    if entry is None:
        return error("Provide 'handle' (from data_upload_file's upload_handle, or 'latest') or "
                     "'file_name' to look up an upload.")

    future = entry["future"]
    # DataFileFuture.uploadState: 0=UploadProcessing (transferring), 1=UploadFinished (.dataFile
    # becomes non-null), 2=UploadFailed. .dataFile.isComplete separately reports whether CLOUD
    # processing finished - transfer and cloud processing are two different signals.
    upload_state = safe(lambda: future.uploadState, None)
    df = safe(lambda: future.dataFile, None)

    if upload_state == 2:
        state = "failed"
    elif df is None:
        state = "uploading"
    else:
        state = "complete" if bool(safe(lambda: df.isComplete, False)) else "processing"

    payload = {
        "handle": key,
        "source_file": entry.get("source_file"),
        "destination_project": entry.get("destination_project"),
        "destination_folder": entry.get("destination_folder"),
        "state": state,
        "elapsed_seconds": round(time.time() - entry.get("started_at", time.time()), 1),
    }

    if state == "complete":
        payload["file_id"] = safe(lambda: df.id)
        payload["version_id"] = safe(lambda: df.versionId)
        payload["version_number"] = safe(lambda: df.versionNumber)
        payload["fusion_web_url"] = safe(lambda: df.fusionWebURL)
        payload["note"] = ("Upload complete - the cloud confirms the file has fully landed and "
                            "processed. Use file_id with doc_open or data_get.")
        _data_common._UPLOADS.pop(key, None)
    elif state == "failed":
        payload["note"] = ("Upload failed. Check the source file's format/permissions and retry "
                            "data_upload_file.")
        _data_common._UPLOADS.pop(key, None)
    elif state == "uploading":
        payload["note"] = "Still transferring the file to the cloud - poll again."
    else:  # processing
        payload["note"] = ("File transfer finished; the cloud is still processing it (e.g. "
                            "translating a neutral format into a Fusion design) - poll again.")

    return ok(payload)


TOOL_DESCRIPTION = (
    "Poll a data_upload_file upload: 'state' is uploading, processing, complete (file_id included) "
    "or failed."
)

tool = (
    Tool.create_simple(name="data_get_upload_status", description=TOOL_DESCRIPTION)
    .add_input_property("handle", {"type": "string",
            "description": "An upload_handle, or 'latest'."})
    .add_input_property("file_name", {"type": "string",
            "description": "Alt to 'handle': the uploaded file's name."})
    .add_input_property("folder", {"type": "string",
            "description": "With 'file_name': its destination folder."})
    .strict_schema()
)
item = Item.create_tool_item(tool=tool, write="read", handler=handler, run_on_main_thread=True)


def register_tool():
    register(item)
