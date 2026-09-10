# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Manage a CAM setup's folders: list, create, rename, and move operations into them, via
CAMFolders.addFolder / OperationBase.moveInto. Patterns (mirror/linear/rotary) cannot be created via
the API - only read and edited; create those in the Manufacture UI."""

import adsk.core

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import iter_collection, ok, error, safe
from ._cam_common import CHILD_COLLECTIONS, get_cam, find_setup, resolve_cam_node

app = adsk.core.Application.get()

_ACTIONS = ("list", "create", "rename", "move")


def _do_list(setup):
    folders = safe(lambda: setup.folders)
    out = []
    for f in iter_collection(folders):
        out.append({
            "name": safe(lambda f=f: f.name),
            "operations": safe(lambda f=f: f.operations.count, 0),
            "patterns": safe(lambda f=f: f.patterns.count, 0),
            "subfolders": safe(lambda f=f: f.folders.count, 0),
        })
    return ok({"setup": safe(lambda: setup.name), "folder_count": len(out), "folders": out,
               "note": "Folders organise the operation tree. Create with action='create', move ops in "
                       "with action='move'. (Patterns are created in the UI - the API won't add them.)"})


def _folder_names(setup):
    """Every name in the setup's OWN folders collection, in its order - the membership list a
    create is read back against."""
    return [safe(lambda f=f: f.name) for f in iter_collection(safe(lambda: setup.folders))]


def _member_names(folder):
    """Every DIRECT child name of a folder - its operations, sub-folders and patterns, each read
    from its own collection - the membership list a moveInto is read back against."""
    names = []
    for attr in CHILD_COLLECTIONS.values():
        for c in iter_collection(safe(lambda a=attr: getattr(folder, a))):
            names.append(safe(lambda c=c: c.name))
    return names


def _do_create(setup, name):
    name = (name or "").strip()
    if not name:
        return error("Provide 'name' for the new folder.")
    setup_name = safe(lambda: setup.name)
    if safe(lambda: setup.folders.itemByName(name)):
        return error(f"A folder named '{name}' already exists in setup '{setup_name}'.")
    before = _folder_names(setup)
    f = safe(lambda: setup.folders.addFolder(name))
    if not f:
        return error(f"Creating folder '{name}' failed.")
    # addFolder handing back a folder is not proof the SETUP carries one: the returned folder's own
    # name is re-read and looked for in the setup's re-listed folders, which must also have GROWN -
    # the name alone would be satisfied by a folder that was already there.
    landed = safe(lambda: f.name)
    after = _folder_names(setup)
    if landed is None or landed not in after or len(after) <= len(before):
        return error(
            f"Creating folder '{name}' did not take - addFolder returned a folder whose name reads "
            f"back as {landed!r}, and setup '{setup_name}' re-lists {len(after)} folder(s) "
            f"({', '.join(n for n in after if n) or 'none'}) against {len(before)} before the call.")
    return ok({"created": True, "folder": landed, "setup": setup_name, "folder_count": len(after),
               "note": "Folder created and found in the setup's re-listed folders. Move operations "
                       "into it with action='move'."})


def _do_rename(setup, folder, new_name):
    folder = (folder or "").strip()
    new_name = (new_name or "").strip()
    if not folder or not new_name:
        return error("Provide 'folder' (the existing folder) and 'new_name'.")
    node, ferr = resolve_cam_node(None, folder, kinds=("folder",), setup=setup, label="folder")
    if ferr:
        return error(ferr)
    f = node.obj
    try:
        f.name = new_name
    except Exception as e:
        return error(f"Could not rename folder '{folder}': {e}")
    if safe(lambda: f.name) != new_name:
        return error(f"Rename of '{folder}' did not take.")
    return ok({"renamed": True, "from": folder, "to": new_name, "setup": safe(lambda: setup.name)})


def _do_move(setup, folder, operations):
    folder = (folder or "").strip()
    operations = operations or []
    if not folder or not operations:
        return error("Provide 'folder' (destination) and 'operations' (names to move into it).")
    node, ferr = resolve_cam_node(None, folder, kinds=("folder",), setup=setup, label="folder")
    if ferr:
        return error(ferr)
    dest = node.obj
    # Resolve ALL operations before moving any (setup-scoped: the shared resolver refuses a name
    # duplicated within the setup and lists the available names on a miss - nothing has moved yet).
    resolved = []
    for nm in operations:
        node, rerr = resolve_cam_node(None, nm, kinds=("operation", "folder", "pattern"),
                                      setup=setup, label="operation/folder/pattern")
        if rerr:
            return error(rerr)
        resolved.append((nm, node.obj))
    moved, unattributed, counts = [], [], []
    # The destination's membership, re-read after every moveInto: a call that returns true and
    # leaves the item out of the folder is caught rather than counted, and every published name is
    # the moved item's own re-read found in that membership.
    members = _member_names(dest)
    for nm, o in resolved:
        okmove = safe(lambda o=o: o.moveInto(dest), False)
        if not okmove:
            return error(f"Could not move '{nm}' into '{folder}' (move not allowed). "
                         f"(Moved so far: {', '.join(moved) or 'none'}.)")
        landed = safe(lambda o=o: o.name)
        after = _member_names(dest)
        if landed is None or landed not in after:
            return error(
                f"Move of '{nm}' into '{folder}' did not take - moveInto returned true, but the "
                f"folder re-lists {len(after)} item(s) "
                f"({', '.join(n for n in after if n) or 'none'}) against {len(members)} before this "
                f"move, and the moved item reads its name back as {landed!r}. "
                f"(Moved so far: {', '.join(moved) or 'none'}.)")
        # The membership is a row of NAMES, so the compare counts the moved item's own name before
        # and after. No growth against a name the folder already listed cannot say whether the move
        # took, so that row is disclosed rather than counted or refused.
        held, now = members.count(landed), after.count(landed)
        if now > held:
            moved.append(landed)
        else:
            unattributed.append(landed)
            counts.append((landed, held, now))
        members = after
    out = {"moved": len(moved), "into": folder, "operations": moved,
           "setup": safe(lambda: setup.name),
           "note": "Each move was read back off the destination folder's own membership; "
                   "'operations' are the names it carries them under."}
    if unattributed:
        out["unattributed"] = unattributed
        tally = "; ".join(f"{b} item(s) named '{n}' before that move and {a} after"
                          for n, b, a in counts)
        out["note"] += (
            f" {len(unattributed)} requested item(s) are in 'unattributed' rather than 'moved': "
            f"'{folder}' listed {tally} - so nothing joined it under those names, and whether each "
            "item was already one of the items listed under its name or its move did not take was "
            "not measured.")
    return ok(out)


def handler(action: str = "list", setup: str = "", name: str = "", folder: str = "",
            new_name: str = "", operations=None) -> dict:
    """See TOOL_DESCRIPTION."""
    action = (action or "list").strip().lower()
    if action not in _ACTIONS:
        return error(f"Unknown action '{action}'. Use one of: {', '.join(_ACTIONS)}.")

    cam, cerr = get_cam()
    if cerr:
        return error(cerr)
    # The resolver's own refusal is returned verbatim: it is the one place that knows whether the
    # name was ABSENT or AMBIGUOUS, and only it can say which.
    target, _names, serr = find_setup(cam, setup)
    if not target:
        return error(serr)

    if action == "list":
        return _do_list(target)
    if action == "create":
        return _do_create(target, name)
    if action == "rename":
        return _do_rename(target, folder, new_name)
    if action == "move":
        return _do_move(target, folder, operations)
    return error(f"Unhandled action '{action}'.")


TOOL_DESCRIPTION = (
    "Manage a CAM setup's folders: list, create, rename, or move operations into one. "
    "Patterns are created in the Manufacture UI, not through the API."
)

tool = (
    Tool.create_simple(name="cam_edit_folders", description=TOOL_DESCRIPTION)
    .add_input_property("action", {"type": "string", "enum": list(_ACTIONS)})
    .add_input_property("setup", {"type": "string", "description": "Setup name (from cam_get)."})
    .add_input_property("name", {"type": "string", "description": "New folder name (create)."})
    .add_input_property("folder", {"type": "string", "description": "Exact folder name; duplicate refusals return name#n selectors."})
    .add_input_property("new_name", {"type": "string", "description": "New name (rename)."})
    .add_input_property("operations", {"type": "array", "items": {"type": "string"},
            "description": "Operation names to move in (move)."})
    .strict_schema()
)
item = Item.create_tool_item(
    tool=tool, write="write", handler=handler, run_on_main_thread=True,
    # Every acting arm re-reads its effect: rename off f.name, create off the setup's re-listed
    # folders, and move off the destination folder's own membership - each publishing that re-read
    # and erroring when the collection did not carry the change.
    verification=Verification(
        kind="inline",
        evidence_test="tests/unit/test_cam_edit_folders.py::TestLyingReturns::"
                      "test_a_create_that_never_joined_the_setup_errors",
        rung="value"))


def register_tool():
    register(item)
