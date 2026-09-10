# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Bundle a subset of a setup's operations into a NEW library template; it never overwrites one."""

import adsk.cam

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import iter_collection, ok, error, safe
from ._cam_common import get_cam, find_setup
from ._cam_templates import _LOCATION, _location_enum, _template_library


def _as_cam_template(result):
    """Normalise createFromOperations' result to a CAMTemplate, or None - its annotation says
    list[Operation] while its own documentation says CAMTemplate, so both shapes are handled."""
    cast = safe(lambda: adsk.cam.CAMTemplate.cast(result))
    if cast:
        return cast
    # list-like? (createFromOperations' annotated return). Try to recover a CAMTemplate from it.
    items = None
    if isinstance(result, (list, tuple)):
        items = list(result)
    else:
        cnt = safe(lambda: result.count)
        if cnt is not None:
            items = list(iter_collection(result))
    if items:
        for it in items:
            t = safe(lambda it=it: adsk.cam.CAMTemplate.cast(it))
            if t:
                return t
    return None


def handler(template_name: str = "", operations: str = "",
            setup: str = "", location: str = "cloud",
            folder: str = "", description: str = "") -> dict:
    """Bundle a setup's named 'operations' into a NEW library template, saved into 'folder' under
    'location' and created there; this never overwrites an existing template."""
    template_name = (template_name or "").strip()
    if not template_name:
        return error("Provide 'template_name' for the new template.")
    if not (setup or "").strip():
        return error("Provide 'setup' - the setup containing the operations.")
    op_names = [o.strip() for o in (operations or "").split(",") if o.strip()]
    if not op_names:
        return error("Provide 'operations' - a comma-separated list of operation names to bundle.")

    cam, err = get_cam()
    if err:
        return error(err)
    lib, err = _template_library()
    if err:
        return error(err)

    target_setup, _names, serr = find_setup(cam, setup)
    if not target_setup:
        return error(serr)

    # Collect the requested operations (Operation objects only).
    by_name = {}
    available_ops = []
    try:
        for op in safe(lambda: target_setup.allOperations, []):
            operation = adsk.cam.Operation.cast(op)
            if operation:
                nm = safe(lambda: operation.name)
                if nm:
                    by_name[nm.lower()] = operation
                    available_ops.append(nm)
    except Exception as e:
        return error(f"Could not read operations in '{setup}': {e}")

    selected = []
    missing = []
    for n in op_names:
        op = by_name.get(n.lower())
        if op:
            selected.append(op)
        else:
            missing.append(n)
    if missing:
        return error(f"Operations not found in '{setup}': {', '.join(missing)}. "
                      f"Available: {', '.join(available_ops[:25]) or '(none)'}")

    # createFromOperations may hand back EITHER a CAMTemplate or a list - its documentation and its
    # annotation disagree - so the result is normalised before .name or importTemplate touch it.
    try:
        result = adsk.cam.CAMTemplate.createFromOperations(selected)
    except Exception as e:
        return error(f"Could not build template from operations: {e}")
    if not result:
        return error("createFromOperations returned nothing.")
    template = _as_cam_template(result)
    if template is None:
        return error("createFromOperations did not yield a usable CAMTemplate (got "
                     f"{type(result).__name__}). The operation set may not be templatable together, "
                     "or this Fusion build's API returns an unexpected shape - please report.")
    template.name = template_name
    if description.strip():
        template.description = description.strip()
    if not safe(lambda: template.isValidTemplate, True):
        return error("The created template is not in a valid state (the operation set may "
    "not be templatable together).")

    # Resolve the destination FOLDER url (importTemplate wants a folder url).
    loc_key, lerr = _LOCATION.resolve(location)
    if lerr:
        return error(lerr)
    loc = _location_enum(loc_key)
    if loc is None:
        return error(f"Location '{loc_key}' is not available in this Fusion build.")
    root = safe(lambda: lib.urlByLocation(loc))
    if not root:
        return error(f"Could not resolve the '{location}' library root.")

    dest_url = root
    created_folder = None
    if folder.strip():
        # Find an existing top-level folder with this name, else create it.
        existing = None
        try:
            for furl in (lib.childFolderURLs(root) or []):
                if (safe(lambda: lib.displayName(furl)) or "").lower() == folder.strip().lower():
                    existing = furl
                    break
        except Exception:
            pass
        if existing:
            dest_url = existing
        else:
            try:
                dest_url = lib.createFolder(root, folder.strip())
                created_folder = folder.strip()
            except Exception as e:
                return error(f"Could not create destination folder '{folder}': {e}")

    # Import (save) the template into the folder.
    try:
        new_url = lib.importTemplate(template, dest_url)
    except Exception as e:
        return error(f"Failed to save the template: {e}")
    if not new_url:
        return error("importTemplate returned no URL (save may have failed).")
    stored = safe(lambda: lib.templateAtURL(new_url))
    if stored is None:
        return error("importTemplate returned a URL but no template loads back from it - the save "
                     "did not land.")
    # The name is read off the STORED template, the same read cam_delete_template confirms an asset's
    # contents by: the in-memory object's own .name says what was written, not what was kept.
    landed = (safe(lambda: stored.name) or "").strip()
    if landed.lower() != template_name.lower():
        return error(f"The template was stored at {safe(lambda: new_url.toString())} but it loads "
                     f"back as '{landed}', not '{template_name}' - the saved template is not the one "
                     "this call named. Re-read with cam_get(include=['templates']).")

    return ok({
        "saved": True,
        "template": landed,
        "operation_count": len(selected),
        "operations": [safe(lambda: o.name) for o in selected],
        "location": location,
        "folder": (folder or "(library root)"),
        "created_folder": created_folder,
        "template_url": safe(lambda: new_url.toString()),
        "note": ("New template saved. Verify with cam_get(include=['templates']) (which reports each "
            "template's asset URL). This tool always creates a NEW template; "
            "overwriting an existing one is a separate capability."),
    })


TOOL_DESCRIPTION = (
    "Bundle some of a setup's operations into a NEW toolpath template."
)

tool = (
    Tool.create_with_string_input(
        name="cam_save_template",
        description=TOOL_DESCRIPTION,
        input_param_name="template_name",
        input_param_description="Name for the new template.",
    )
    .add_input_property("operations", {"type": "string",
            "description": "Comma-separated names within 'setup'."})
    .add_input_property("setup", {"type": "string",
            "description": "Setup holding the operations."})
    .add_input_property(*_LOCATION.as_property())
    .add_input_property("folder", {"type": "string",
            "description": "Top-level folder name; created if missing."})
    .add_input_property("description", {"type": "string"})
    .strict_schema()
)
item = Item.create_tool_item(
    tool=tool, write="write", handler=handler,
    run_on_main_thread=True,
    verification=Verification(
        kind="inline",
        evidence_test="tests/unit/test_cam_save_template.py::TestSaveTemplateRename"
                      "::test_a_stored_template_under_another_name_is_an_error",
        rung="value")
)


def register_tool():
    register(item)
