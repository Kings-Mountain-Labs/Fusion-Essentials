# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Create a project in the user's active Autodesk hub, refusing a name one already carries."""

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import ok, error, safe
from ._data_common import _data, _find_project


def handler(name: str = "", purpose: str = "") -> dict:
    """See TOOL_DESCRIPTION."""
    name = (name or "").strip()
    if not name:
        return error("Provide 'name' for the new project.")
    try:
        data = _data()
    except Exception as e:
        return error(str(e))

    # Guard against duplicate names (Fusion would otherwise create a second project).
    existing, _ = _find_project(data, name=name)
    if existing:
        return error(f"A project named '{name}' already exists "
                      f"(id {safe(lambda: existing.id)}). Use a different name.")
    try:
        proj = data.dataProjects.add(name, purpose or "", "")
    except Exception as e:
        return error(f"Failed to create project '{name}': {e}")
    if not proj:
        return error(f"Project creation returned nothing for '{name}'.")
    landed, _ = _find_project(data, name=name)
    if landed is None:
        return error(f"dataProjects.add returned a project but '{name}' does not appear when the "
                     "projects are re-listed - the creation did not land.")
    return ok({"created": True, "name": safe(lambda: proj.name),
        "id": safe(lambda: proj.id)})


TOOL_DESCRIPTION = (
    "Create a new project in the user's active Autodesk hub. Fails if a project with "
    "the same name already exists."
)

tool = (
    Tool.create_with_string_input(
        name="data_create_project",
        description=TOOL_DESCRIPTION,
        input_param_name="name",
        input_param_description="Name for the new project.",
    )
    .add_input_property("purpose", {"type": "string",
        "description": "Project description/purpose."})
    .strict_schema()
)
item = Item.create_tool_item(
    tool=tool, write="write", handler=handler,
    run_on_main_thread=True,
    verification=Verification(
        kind="inline",
        evidence_test="tests/unit/test_data_create_project.py::TestCreateProject"
                      "::test_a_project_that_never_relists_is_an_error",
        rung="value")
)


def register_tool():
    register(item)
