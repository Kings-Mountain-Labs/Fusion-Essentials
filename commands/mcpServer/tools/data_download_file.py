# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Download ONE non-Fusion cloud file (DataFile) to a local folder.

DataFile.download handles only non-Fusion data and FAILS for an F3D, so Fusion designs are refused
here with a pointer to design_export. The synchronous form used below (handler=None) FREEZES Fusion
until the transfer finishes.
"""

import os
import shutil
import tempfile

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item
from ..mcp_primitives.registry import register
from ._common import ok, error, safe
from ._data_common import (FUSION_NATIVE_EXTENSIONS, name_extension, resolve_file_reference,
                           _folder_path_string)
from . import _assert
from . import _export
from . import _outputs

# What this tool RETURNS. size_bytes is SUPPLIED by the FileLanded postcondition's evidence (it stats
# the path once), so the handler does not re-stat it.
RETURNS = [
    _outputs.ReturnsValue("file_path", "the local path written"),
    _outputs.ReturnsValue("size_bytes", "the file's size on disk (proof it landed)"),
]


def handler(file: str = "", project: str = "", folder: str = "", destination_folder: str = "",
            file_name: str = "", overwrite: bool = False) -> dict:
    """See TOOL_DESCRIPTION."""
    dest = (destination_folder or "").strip().strip('"')
    if not dest:
        return error("Provide 'destination_folder' - the LOCAL folder to write the file into.")

    df, meta, err = resolve_file_reference(file, project=project, folder=folder)
    if err:
        return error(err)

    name = safe(lambda: df.name) or ""
    # The two signals disagree in OPPOSITE directions, so the NAME leads: a non-CAD upload named
    # 'probe_note.txt' reads fileExtension 'sql', while a design named 'Gyroscope' carries no
    # extension in its name at all and only fileExtension ('f3d') answers.
    ext = name_extension(name) or (safe(lambda: df.fileExtension) or "").strip().lower()
    if ext in FUSION_NATIVE_EXTENSIONS:
        return error(f"'{name}' is Fusion-native data (.{ext}) and cannot be downloaded: "
                     "DataFile.download handles only non-Fusion files. Open it (doc_open) and export "
                     "instead - design_export for a design, drawing_export for a drawing, mesh_export "
                     "for a mesh.")

    out_name = (file_name or "").strip().strip('"') or name
    if not out_name:
        return error("The cloud file's name could not be read - pass 'file_name' to choose the local "
                     "filename explicitly.")
    if os.path.basename(out_name) != out_name:
        return error(f"'file_name' must be a bare filename, not a path: '{out_name}'. The folder "
                     "comes from 'destination_folder'.")
    path = os.path.join(dest, out_name)

    # Download into a same-filesystem staging directory, so a failed transfer never writes through
    # the destination and a validated file can be published in one filesystem operation.
    had_existing = os.path.isfile(path)
    if had_existing and not overwrite:
        return error(f"'{path}' already exists. Pass overwrite=true to replace it, or set "
                     "'file_name'. (Refusing keeps a stale file from being reported as this "
                     "download's result.)")

    if not os.path.isdir(dest):
        try:
            os.makedirs(dest, exist_ok=True)
        except Exception as ex:
            return error(f"Could not create destination folder '{dest}': {ex}")

    try:
        stage_dir = tempfile.mkdtemp(prefix=".fusion-download-", dir=dest)
    except Exception as ex:
        return error(f"Could not create download staging directory in '{dest}': {ex}")
    stage_path = os.path.join(stage_dir, out_name)
    failure = None
    cleanup_error = None
    replaced = False
    try:
        try:
            # handler=None is the SYNCHRONOUS form: this call does not return until the transfer is
            # done or has failed, and Fusion is frozen throughout.
            did = df.download(stage_path, None)
        except Exception as ex:
            failure = f"Download failed for '{name}': {ex}"
        else:
            if not did:
                failure = (f"DataFile.download returned false for '{name}' - no file was "
                           "published. Fusion designs cannot be downloaded (use design_export); "
                           "check the file is fully processed (data_get(file=...) reports "
                           "state.is_complete).")
            else:
                _size, landed_error = _export.verify_written(stage_path)
                if landed_error:
                    failure = f"Download failed validation before publication: {landed_error}."
                else:
                    replaced = had_existing or os.path.isfile(path)
                    try:
                        if overwrite:
                            os.replace(stage_path, path)
                        elif os.name == "nt":
                            os.rename(stage_path, path)
                        else:
                            os.link(stage_path, path)
                    except FileExistsError:
                        failure = (f"'{path}' appeared while the download was in progress; "
                                   "publication was refused. Pass overwrite=true to replace it, "
                                   "or set 'file_name'.")
                    except Exception as ex:
                        failure = f"Could not publish the downloaded file to '{path}': {ex}"
    finally:
        try:
            shutil.rmtree(stage_dir)
        except Exception as ex:
            cleanup_error = ex

    if failure:
        if cleanup_error:
            failure += f" Staging cleanup also failed at '{stage_dir}': {cleanup_error}"
        return error(failure)

    note = ("Downloaded synchronously (Fusion was frozen for the transfer) and gated on a non-empty "
            "file landing on disk - see size_bytes.")
    if cleanup_error:
        note += (f" The final file was published at '{path}', but staging cleanup failed; remove "
                 f"the leftover staging path '{stage_dir}': {cleanup_error}")
    if (meta or {}).get("scope_truncated"):
        note += (" Matched by NAME inside a capped listing - files beyond the cap were never "
                 "compared, so check 'source' is the file you meant; a lineage URN is exact.")
    if (meta or {}).get("folders_unreadable"):
        # The file on disk is whichever one the name resolved to. A folder that never opened could
        # hold another file of that name, which would make this the wrong file downloaded.
        note += (f" {meta['folders_unreadable']} folder(s) could not be READ while resolving that "
                 "name, so they were never searched - a file of the same name could be sitting in "
                 "one. Pass the lineage URN to be exact.")

    return ok({
        "downloaded": True,
        "name": name,
        # How settled the by-name match was, as data_get publishes it: a capped listing left files
        # uncompared, an unreadable folder is a hole in the search space. Both read false/0 for a
        # URN match, which needs no search at all.
        "name_scope_truncated": bool((meta or {}).get("scope_truncated")),
        "name_scope_folders_unreadable": (meta or {}).get("folders_unreadable", 0),
        "file_path": path,
        "source": {"project": safe(lambda: df.parentProject.name),
                   "folder_path": _folder_path_string(safe(lambda: df.parentFolder)) or "(project root)",
                   "id": (meta or {}).get("urn")},
        "overwrote_existing": replaced,
        "note": note,
    })


TOOL_DESCRIPTION = (
    "Download ONE non-Fusion cloud file to a local folder; the transfer is SYNCHRONOUS and freezes "
    "Fusion until it finishes.\n"
    + _outputs.produces_block(RETURNS)
)

tool = (
    Tool.create_simple(name="data_download_file", description=TOOL_DESCRIPTION)
    .add_input_property("file", {"type": "string",
            "description": "Lineage URN, or a name plus 'project'."})
    .add_input_property("project", {"type": "string"})
    .add_input_property("folder", {"type": "string",
            "description": "Cloud path scoping a by-name lookup."})
    .add_input_property("destination_folder", {"type": "string",
            "description": "LOCAL folder; created if missing."})
    .add_input_property("file_name", {"type": "string",
            "description": "Bare filename. Default: the cloud name."})
    .add_input_property("overwrite", {"type": "boolean",
            "description": "Replace an existing local file."})
    .strict_schema()
)

# enforce_timeout=False: the synchronous download is an uninterruptible main-thread transfer that can
# run past the server's call timeout; the file-landed gate is the real proof, so we wait for it rather
# than false-failing a download that is still writing.
item = Item.create_tool_item(tool=tool, write="write", handler=handler, run_on_main_thread=True,
                             enforce_timeout=False,
                             postconditions=[_assert.FileLanded("file_path")])


def register_tool():
    register(item)
