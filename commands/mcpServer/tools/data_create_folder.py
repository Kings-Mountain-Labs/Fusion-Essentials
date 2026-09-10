# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Create a folder in a project, auto-creating missing parent path segments (mkdir -p). WRITES."""

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import ok, error, safe
from ._data_common import (
    _data, _find_project, _split_path, _child_folder_by_name,
    _ensure_folder_path, _folder_path_string, _retained_parents,
)


def handler(folder_name: str = "", project: str = "", project_id: str = "",
            parent_folder: str = "") -> dict:
    """Create a folder, auto-creating missing parent path segments (mkdir -p)."""
    folder_name = (folder_name or "").strip()
    if not folder_name:
        return error("Provide 'folder_name'.")
    if not (project or project_id):
        return error("Provide 'project' (name) or 'project_id'.")
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

    # Resolve (creating as needed) the parent path. Empty -> project root.
    parent_segments = _split_path(parent_folder)
    # auto_created is passed IN so a path that raises halfway still names the folders it made.
    auto_created = []
    try:
        parent, auto_created = _ensure_folder_path(root, parent_segments, created_out=auto_created)
    except Exception as e:
        return error(f"Could not prepare parent path '{parent_folder}': {e}"
                     + _retained_parents(auto_created))

    # Duplicate guard scoped to the resolved parent (a same-named folder elsewhere is fine).
    existing = _child_folder_by_name(parent, folder_name)
    if existing:
        return error(f"A folder named '{folder_name}' already exists at "
                      f"'{_folder_path_string(parent) or '(project root)'}' "
                      f"(id {safe(lambda: existing.id)})." + _retained_parents(auto_created))

    try:
        folder = parent.dataFolders.add(folder_name)
    except Exception as e:
        return error(f"Failed to create folder '{folder_name}': {e}"
                     + _retained_parents(auto_created))
    if not folder:
        return error(f"Folder creation returned nothing for '{folder_name}'."
                     + _retained_parents(auto_created))
    if _child_folder_by_name(parent, folder_name) is None:
        return error(f"dataFolders.add returned a folder but '{folder_name}' does not appear when "
                     f"'{_folder_path_string(parent) or '(project root)'}' is re-listed - the "
                     "creation did not land." + _retained_parents(auto_created))
    return ok({"created": True, "name": safe(lambda: folder.name),
        "id": safe(lambda: folder.id),
        "project": safe(lambda: proj.name),
        "path": _folder_path_string(folder),
        "auto_created_parents": auto_created})


TOOL_DESCRIPTION = (
    "Create a folder in a project; a nested 'parent_folder' path creates its missing folders."
)

tool = (
    Tool.create_with_string_input(
        name="data_create_folder",
        description=TOOL_DESCRIPTION,
        input_param_name="folder_name",
        input_param_description="Name for the new folder.",
    )
    .add_input_property("project", {"type": "string", "description": "Destination project name."})
    .add_input_property("project_id", {"type": "string", "description": "Alt to 'project'."})
    .add_input_property("parent_folder", {"type": "string",
        "description": "Path, e.g. 'Fixtures/Vises'. Default: the project root."})
    .strict_schema()
)
item = Item.create_tool_item(
    tool=tool, write="write", handler=handler, run_on_main_thread=True,
    verification=Verification(
        kind="inline",
        evidence_test="tests/unit/test_data_create_folder.py::TestCreateFolder"
                      "::test_a_folder_that_never_relists_is_an_error",
        rung="value")
)


def register_tool():
    register(item)
