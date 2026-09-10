# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Move ONE cloud file (DataFile) into another EXISTING folder of its own project.

DataFile.move returns a bool, so the effect is read back: the file is re-resolved from its lineage
URN and its parentFolder re-read - a true return over an unchanged parent is reported as a failure.
"""

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import ok, error, safe
from ._data_common import (resolve_file_reference, _resolve_data_file, navigate_folder_path,
                           _folder_path_string)


def _folder_identity(folder):
    """(id, name, path) of a DataFolder - the triple the move read-back compares on. The id is the
    discriminating one: two folders in different parents may share a name."""
    if folder is None:
        return None, None, None
    return (safe(lambda: folder.id), safe(lambda: folder.name),
            _folder_path_string(folder) or "(project root)")


def handler(file: str = "", project: str = "", folder: str = "", target_folder: str = "") -> dict:
    """See TOOL_DESCRIPTION."""
    want = (target_folder or "").strip()
    if not want:
        return error("Provide 'target_folder' - the destination folder PATH inside the file's own "
                     "project (e.g. 'Parts/Fixtures'), or '/' for the project root.")

    df, meta, err = resolve_file_reference(file, project=project, folder=folder)
    if err:
        return error(err)

    name = safe(lambda: df.name) or "(unnamed)"
    lineage = (meta or {}).get("urn") or safe(lambda: df.id)
    proj = safe(lambda: df.parentProject)
    root = safe(lambda: proj.rootFolder) if proj is not None else None
    if root is None:
        return error(f"Could not read the project root folder that '{name}' lives in - the move "
                     "destination cannot be resolved against its project.")

    target, _target_path_walked, miss = navigate_folder_path(root, want)
    if miss and miss["available"] is None:
        # UNREAD, not absent: "does not exist" would send the caller to data_create_folder to make a
        # folder that may already be there, and the move would land nowhere either way.
        return error(f"Target folder '{target_folder}' could not be resolved: the folders inside "
                     f"'{miss['at']}' could not be read, so whether '{miss['segment']}' exists is "
                     "unknown. Nothing was moved - re-check with "
                     "data_get(project=<name>, include=['folders']) and retry.")
    if miss:
        return error(f"Target folder '{target_folder}' does not exist in project "
                     f"'{safe(lambda: proj.name)}' (missing segment '{miss['segment']}'). Folders at "
                     f"'{miss['at']}': {', '.join(miss['available']) or '(none)'}. This tool creates "
                     "nothing - make the folder with data_create_folder first.")

    before_id, before_name, before_path = _folder_identity(safe(lambda: df.parentFolder))
    target_id, target_name, target_path = _folder_identity(target)
    # The read-back compares the file's new parent against THIS target. With neither the target's id
    # nor its name readable there is nothing to compare it to afterwards, so the move could only be
    # reported on trust - refuse before mutating instead.
    if target_id is None and target_name is None:
        return error(f"The destination '{target_folder}' resolved but neither its id nor its name "
                     "could be read, so a move into it could not be verified afterwards. Refusing "
                     "to move unverifiably - re-check with "
                     "data_get(project=<name>, include=['folders']).")
    if before_id is not None and target_id is not None and before_id == target_id:
        return ok({"moved": False, "already_in_target": True, "name": name, "file_id": lineage,
                   "folder": before_path,
                   "note": f"'{name}' is already in '{before_path}' - nothing was moved."})

    try:
        did = df.move(target)
    except Exception as ex:
        return error(f"Move of '{name}' to '{target_path}' failed: {ex}")
    if not did:
        return error(f"DataFile.move returned false for '{name}' - Fusion declined the move to "
                     f"'{target_path}'. Nothing changed.")

    # A true return is a CLAIM. Re-resolve the file from its lineage URN and read the parent back -
    # the object in hand can still report the folder it was resolved from.
    fresh, _resolved, _tried = _resolve_data_file(lineage or "")
    now_id, now_name, now_path = _folder_identity(safe(lambda: (fresh or df).parentFolder))
    if now_id is None and now_name is None:
        return error(f"move() reported success for '{name}' but its parent folder could not be "
                     "re-read, so the move is UNCONFIRMED. Re-check with "
                     "data_get(project=<name>, include=['folders']) before moving it again.")

    # POSITIVE evidence only: a verdict needs BOTH sides readable on the SAME axis. Two unreadable
    # values comparing equal is not a match, it is an absence of one - reported as unconfirmed.
    matched_on = None
    if now_id is not None and target_id is not None:
        landed, matched_on = (now_id == target_id), "id"
    elif now_name is not None and target_name is not None:
        landed, matched_on = (now_name == target_name), "name"
    else:
        return error(f"move() reported success for '{name}' but its new parent folder and the "
                     "target share no readable identity to compare on, so the move is UNCONFIRMED. "
                     "Re-check with data_get(project=<name>, include=['folders']).")
    if not landed:
        return error(f"move() returned true for '{name}' but it still reports parent folder "
                     f"'{now_path}' instead of '{target_path}' - the move did NOT take. Reporting "
                     "failure rather than a success the data model does not show.")

    note = ("Verified by re-resolving the file and reading its parentFolder back. A project's ROOT "
            "folder reports the PROJECT's name, so a move to '/' shows that name.")
    if matched_on == "name":
        # A NAME match is weaker evidence than an id match and must not be published as the same
        # thing: two sibling folders can share a name, so this confirms the file sits in a folder
        # CALLED that, not in the one that was targeted.
        note += (" One of the two folder ids could not be read, so the match is by folder NAME, not "
                 "by id - it confirms the file is in a folder named '" + str(now_name) + "', not "
                 "that it is the exact folder targeted.")
    if (meta or {}).get("scope_truncated"):
        note += (" The file was matched by NAME inside a capped listing - files beyond the cap were "
                 "never compared, so confirm 'name'/'file_id' is the file you meant.")
    if (meta or {}).get("folders_unreadable"):
        # The file that MOVED is whichever one the name resolved to. A folder that never opened
        # could hold another file of that name, so the caller has to check it moved the right one -
        # and this is the last moment the move is cheap to reverse.
        note += (f" {meta['folders_unreadable']} folder(s) could not be READ while resolving that "
                 "name, so they were never searched - a file of the same name could be sitting in "
                 "one. Confirm 'file_id' is the file you meant to move.")

    return ok({
        "moved": True,
        "verified_by": matched_on,
        "name": name,
        "file_id": lineage,
        "project": safe(lambda: proj.name),
        "from_folder": before_path or "(unknown)",
        "to_folder": now_path,
        "parent_folder_after": now_name,
        "note": note,
    })


TOOL_DESCRIPTION = (
    "Move ONE cloud file into an EXISTING folder of its own project; it creates nothing."
)

tool = (
    Tool.create_simple(name="data_move_file", description=TOOL_DESCRIPTION)
    .add_input_property("file", {"type": "string",
            "description": "Lineage URN, or a name plus 'project'."})
    .add_input_property("project", {"type": "string"})
    .add_input_property("folder", {"type": "string",
            "description": "Cloud path scoping a by-name lookup."})
    .add_input_property("target_folder", {"type": "string",
            "description": "Destination path in the file's project; '/' is its root."})
    .strict_schema()
)

item = Item.create_tool_item(
    tool=tool, write="write", handler=handler, run_on_main_thread=True,
    verification=Verification(
        kind="inline",
        evidence_test="tests/unit/test_data_move_file.py::TestMoveVerification"
                      "::test_true_with_an_unchanged_parent_is_an_error",
        rung="value"))


def register_tool():
    register(item)
