# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Upload a local CAD file into a cloud project. ASYNCHRONOUS: the call returns once the upload has
started, and mints a poll handle data_get_upload_status reads the real state from. WRITES."""

import os
import time

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import ok, error, safe
from ._data_common import (
    _UPLOAD_HANDLE_SEQ, _UPLOAD_STATE, _UPLOADS,
    _data, _find_project, _split_path,
    _resolve_folder_path, _ensure_folder_path, _folder_path_string, _retained_parents,
)
from . import _outputs

# What data_upload_file RETURNS: a poll handle so data_get_upload_status can report the upload's
# real uploading/processing/complete/failed state instead of the caller re-listing files and guessing.
RETURNS = [
    _outputs.ReturnsValue("upload_handle", "an upload poll handle - poll until state='complete'",
                          consumers=["data_get_upload_status"]),
]


def handler(file_path: str = "", project: str = "", project_id: str = "",
            folder: str = "", create_path: bool = False) -> dict:
    """Upload a local CAD file; create_path=true auto-creates a missing destination folder path."""
    file_path = (file_path or "").strip().strip('"')
    if not file_path:
        return error("Provide 'file_path' - the full path to a local CAD file.")
    if not os.path.isfile(file_path):
        return error(f"File not found on disk: {file_path}")
    if not (project or project_id):
        return error("Provide 'project' (name) or 'project_id' for the destination.")

    try:
        data = _data()
    except Exception as e:
        return error(str(e))

    proj, available = _find_project(data, name=project or None, project_id=project_id or None)
    if not proj:
        ident = project_id or project
        return error(f"Project not found: {ident}. Available: {', '.join(available) or '(none)'}")

    try:
        root = proj.rootFolder
    except Exception as e:
        return error(f"Could not access project root folder: {e}")

    target = root
    auto_created = []
    segments = _split_path(folder)
    if segments:
        if create_path:
            try:
                target, auto_created = _ensure_folder_path(root, segments,
                                                           created_out=auto_created)
            except Exception as e:
                return error(f"Could not prepare destination path '{folder}': {e}"
                             + _retained_parents(auto_created))
        else:
            target, missing = _resolve_folder_path(root, segments)
            if not target:
                # Help the agent: show what folders DO exist at the point of failure.
                partial, _ = _resolve_folder_path(
                    root, segments[:segments.index(missing)]) if missing in segments else (root, None)
                here = partial or root
                opts = [safe(lambda: f.name) for f in safe(lambda: here.dataFolders.asArray(), [])]
                return error(
                    f"Destination folder path not found: '{folder}' (missing segment "
                    f"'{missing}'). Folders available at "
                    f"'{_folder_path_string(here) or '(project root)'}': "
                    f"{', '.join(n for n in opts if n) or '(none)'}. "
                    "Pass create_path=true to create missing folders, or use data_get(include=['folders']) "
                    "to see the structure.")

    try:
        # Synchronous-start upload; returns a future. We do NOT block waiting for it to
        # finish (that would freeze the UI thread) - we report the initial state.
        future = target.uploadFile(file_path)
    except Exception as e:
        return error(f"Upload failed to start for '{file_path}': {e}"
                     + _retained_parents(auto_created))
    if not future:
        return error("Upload returned no future object." + _retained_parents(auto_created))

    state = safe(lambda: future.uploadState)
    if state == 2:
        return error(f"Upload of '{os.path.basename(file_path)}' reports FAILED immediately - "
                     "the file was not accepted. Check the format and the destination folder."
                     + _retained_parents(auto_created))
    new_name = None
    new_id = None
    try:
        df = future.dataFile  # only present once finished
        if df:
            new_name = safe(lambda: df.name)
            new_id = safe(lambda: df.id)
    except Exception:
        pass

    # Keep the future referenced + mint a poll handle - see the _UPLOADS comment in _data_common.
    _UPLOAD_HANDLE_SEQ[0] += 1
    upload_handle = f"up{_UPLOAD_HANDLE_SEQ[0]}"
    _UPLOADS[upload_handle] = {
        "future": future,
        "source_file": os.path.basename(file_path),
        "destination_project": safe(lambda: proj.name),
        "destination_folder": (_folder_path_string(target) or "(project root)"),
        "started_at": time.time(),
    }

    return ok({
        "upload_started": True,
        "upload_handle": upload_handle,
        "source_file": os.path.basename(file_path),
        "destination_project": safe(lambda: proj.name),
        "destination_folder": (_folder_path_string(target) or "(project root)"),
        "auto_created_parents": auto_created,
        "upload_state": _UPLOAD_STATE.get(state, str(state)),
        "uploaded_name": new_name,
        "uploaded_id": new_id,
        "note": ("Upload is asynchronous and processes on the cloud (neutral formats like "
            "STEP are translated into a Fusion design). Poll data_get_upload_status("
            "handle=upload_handle) for the actual uploading/processing/complete/failed state - "
            "do not guess from a re-listed data_get."),
    })


TOOL_DESCRIPTION = (
    "Upload a local CAD file into a project. ASYNCHRONOUS: it returns once the upload has "
    "started.\n"
    + _outputs.produces_block(RETURNS)
)

tool = (
    Tool.create_with_string_input(
        name="data_upload_file",
        description=TOOL_DESCRIPTION,
        input_param_name="file_path",
        input_param_description="Full path to the local file.",
    )
    .add_input_property("project", {"type": "string", "description": "Destination project name."})
    .add_input_property("project_id", {"type": "string", "description": "Alt to 'project'."})
    .add_input_property("folder", {"type": "string",
        "description": "Destination path, e.g. 'Imports/STEP'."})
    .add_input_property("create_path", {"type": "boolean",
        "description": "Create missing folders."})
    .strict_schema()
)
item = Item.create_tool_item(
    tool=tool, write="write", handler=handler, run_on_main_thread=True,
    # The poller gates on DataFile.isComplete and publishes the landed file's id, versionId and
    # version number - the identity of the file this upload produced, not the request echoed back.
    verification=Verification(
        kind="deferred", poller="data_get_upload_status",
        evidence_test="tests/unit/test_data_upload_file.py::TestUploadFile"
                      "::test_the_start_names_the_poller_and_claims_no_completion",
        rung="value")
)


def register_tool():
    register(item)
