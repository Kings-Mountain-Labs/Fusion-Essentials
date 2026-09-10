# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Delete a data-model folder by id, guarded: a NON-EMPTY folder is a recursive subtree wipe that
needs an explicit second acknowledgment, and force alone returns a blast-radius preview."""

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import ok, error, safe, counted
from ._data_common import _data


def _folder_counts(folder):
    """(file_count, subfolder_count) for a folder's IMMEDIATE children; None for a count that would
    not read. None is NOT zero here - see _unreadable_counts, which is what the delete gate asks."""
    files = safe(lambda: folder.dataFiles.count, None)
    subs = safe(lambda: folder.dataFolders.count, None)
    return files, subs


def _unreadable_counts(file_count, sub_count):
    """The census reads that would not answer, named as the caller sees them ([] when both read). A
    folder whose census failed is NOT provably empty, so the delete gate treats an unreadable count
    exactly like a non-empty folder."""
    return [label for label, value in (("dataFiles.count", file_count),
                                       ("dataFolders.count", sub_count)) if value is None]


# Folder-visit budget for the recursive blast-radius count: each visited folder is a main-thread
# cloud round-trip, so when the budget is spent the counts are a LOWER BOUND
# (_state['truncated']=True), reported as "at least N".
_SUBTREE_VISIT_BUDGET = 60

_UNREADABLE = object()      # a dataFolders enumeration that RAISED - distinct from one that is empty


def _subtree_counts(folder, _depth=0, _state=None):
    """(total_file_count, total_subfolder_count) for the WHOLE subtree under 'folder' - the real blast
    radius of a recursive delete - bounded by _SUBTREE_VISIT_BUDGET visits, past which
    _state['truncated'] makes the counts a lower bound. A folder whose count or enumeration will not
    READ is a HOLE, never a zero, tallied once in _state['unreadable']."""
    if _state is None:
        _state = {"visits": 0, "truncated": False, "unreadable": 0}
    hole = False
    files = counted(lambda: folder.dataFiles.count)
    if files is None:
        hole = True
        files = 0           # contributes nothing it can prove; the hole is disclosed, not counted as 0
    subs = 0
    if _depth < 32:
        children = safe(lambda: folder.dataFolders.asArray(), _UNREADABLE)
        if children is _UNREADABLE:
            hole = True
            children = []
        for sub in children or []:
            if _state["visits"] >= _SUBTREE_VISIT_BUDGET:
                _state["truncated"] = True
                break
            _state["visits"] += 1
            subs += 1
            f, s = _subtree_counts(sub, _depth + 1, _state)
            files += f
            subs += s
    if hole:
        _state["unreadable"] = _state.get("unreadable", 0) + 1
    return files, subs


def handler(folder_id: str = "", confirm_name: str = "",
            force: bool = False, recursive_confirm: str = "") -> dict:
    """Delete a data-model folder by id, guarded; see TOOL_DESCRIPTION for the confirm_name/force/recursive_confirm gates."""
    folder_id = (folder_id or "").strip()
    confirm_name = (confirm_name or "").strip()
    if not folder_id:
        return error("Provide 'folder_id' (the id of the folder to delete; from data_get(include=['folders'])).")
    if not confirm_name:
        return error("Provide 'confirm_name' - the exact current name of the folder, as a "
    "safety confirmation. Get it from data_get(include=['folders']).")

    try:
        data = _data()
    except Exception as e:
        return error(str(e))

    try:
        folder = data.findFolderById(folder_id)
    except Exception as e:
        return error(f"findFolderById failed for '{folder_id}': {e}")
    if not folder:
        return error(f"No folder found for folder_id '{folder_id}'. It may already be "
    "deleted. Verify with data_get(include=['folders']).")

    if safe(lambda: folder.isRoot, False):
        return error("Refusing to delete a project ROOT folder.")

    actual_name = safe(lambda: folder.name) or "(unknown)"
    # Case-SENSITIVE confirmation: safety gate, so require an exact match (only
    # surrounding whitespace is forgiven).
    if actual_name.strip() != confirm_name:
        return error(
            f"Name mismatch - refusing to delete. folder_id resolves to '{actual_name}', but "
            f"confirm_name was '{confirm_name}'. Pass confirm_name='{actual_name}' if you "
            "really mean this folder.")

    file_count, sub_count = _folder_counts(folder)
    unreadable = _unreadable_counts(file_count, sub_count)
    non_empty = bool((file_count or 0) or (sub_count or 0))
    recursive_confirm = (recursive_confirm or "").strip()

    if unreadable:
        # Fail CLOSED: the census that would have shown a subtree is the read that failed, so the
        # empty-folder delete is refused and the recursive wipe's force + recursive_confirm are
        # demanded instead. The refusal names WHICH read failed.
        if not force or recursive_confirm != actual_name:
            return error(
                f"The contents of '{actual_name}' could not be read ({' and '.join(unreadable)} "
                "failed), so it is NOT provably empty - it may hold an entire subtree this delete "
                "would remove irreversibly, and no blast-radius preview can be built. Refusing. "
                "Retry once the folder reads (data_get(include=['folders'])), or pass force=true "
                f"AND recursive_confirm='{actual_name}' to delete it WITHOUT a census. Nothing was "
                "deleted.")
    elif non_empty:
        # NON-EMPTY = a recursive subtree wipe. Compute the full blast radius (nested files too),
        # bounded by a folder-visit budget so a huge subtree can't hang the preview.
        subtree_state = {"visits": 0, "truncated": False, "unreadable": 0}
        total_files, total_subs = _subtree_counts(folder, _state=subtree_state)
        blind_folders = subtree_state.get("unreadable", 0)

        def _n(count):
            # a walk cut by the budget - or one that could not look inside a folder - under-counts;
            # say so instead of implying an exact total.
            return (f"at least {count}"
                    if (subtree_state["truncated"] or blind_folders) else str(count))

        def _holes():
            # The blast radius of what those folders hold is UNKNOWN, so it is named rather than
            # folded into the totals as nothing.
            if not blind_folders:
                return ""
            return (f" {blind_folders} folder(s) in the subtree would not enumerate, so whatever "
                    "they hold is NOT in those totals - the real blast radius is larger.")

        if not force:
            return error(
                f"'{actual_name}' is not empty (immediate files: {file_count}, subfolders: "
                f"{sub_count}). Deleting it RECURSIVELY removes its ENTIRE subtree: "
                f"{_n(total_files)} file(s) and {_n(total_subs)} subfolder(s) total - and bypasses "
                f"the per-file reference-orphan check.{_holes()} Pass force=true AND "
                f"recursive_confirm='{actual_name}' to do this, or empty it first (data_delete_file "
                "for files).")
        # force is set but require the explicit recursive acknowledgment matching the name.
        if recursive_confirm != actual_name:
            return error(
                f"RECURSIVE DELETE of '{actual_name}' would remove its ENTIRE subtree: "
                f"{_n(total_files)} file(s) and {_n(total_subs)} subfolder(s) - and bypasses the "
                "per-file reference-orphan check (nested referenced files would be orphaned). This "
                f"is irreversible.{_holes()} To proceed, pass recursive_confirm='" + actual_name
                + "' (a deliberate second acknowledgment). Nothing was deleted.")

    try:
        did = folder.deleteMe()  # adsk.core: DataFolder.deleteMe() -> bool
    except Exception as e:
        return error(f"Delete failed for folder '{actual_name}': {e}")
    if not did:
        return error(f"Fusion declined to delete folder '{actual_name}'. No change was made.")

    payload = {
    "deleted": True,
    "name": actual_name,
    "folder_id": folder_id,
    "contained_files": file_count,
    "contained_subfolders": sub_count,
    # Whether this delete took a subtree with it is only knowable from a census that READ.
    "recursive": None if unreadable else bool(non_empty),
    }
    if unreadable:
        payload["census_unreadable"] = unreadable
        payload["note"] = ("The folder's contents could not be read before the delete ("
                           + " and ".join(unreadable) + " failed), so what went with it is "
                           "unknown - 'contained_files'/'contained_subfolders' are null, not zero.")
    return ok(payload)


TOOL_DESCRIPTION = (
    "Delete a folder on the cloud, IRREVERSIBLY: 'confirm_name' must EXACTLY match the folder's "
    "current name."
)

tool = (
    Tool.create_with_string_input(
        name="data_delete_folder",
        description=TOOL_DESCRIPTION,
        input_param_name="folder_id",
        input_param_description="From data_get(include=['folders']).",
    )
    .add_input_property("confirm_name", {"type": "string", "description": "Case-sensitive."})
    .add_input_property("force", {"type": "boolean",
        "description": "Allow a non-empty folder."})
    .add_input_property("recursive_confirm", {"type": "string",
        "description": "The folder's name, acknowledging the subtree delete."})
    .strict_schema()
)
item = Item.create_tool_item(
    tool=tool, write="destructive", handler=handler,
    run_on_main_thread=True,
    # deleteMe()'s own answer is the whole gate: whether findFolderById stops resolving a just-deleted
    # folder - and how long the data model takes to show that - is not measured here.
    verification=Verification(
        kind="inline",
        evidence_test="tests/unit/test_data_delete_folder.py::TestDeleteFolderGate"
                      "::test_a_declined_delete_is_an_error_not_a_reported_delete",
        rung="exists")
)


def register_tool():
    register(item)
