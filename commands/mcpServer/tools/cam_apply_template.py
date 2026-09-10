# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Apply a CAM toolpath template to a setup, recreating its operations there. WRITES."""

import adsk.core
import adsk.cam

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import named_with_remainder, ok, error, safe, set_verified
from ._cam_common import get_cam, find_setup, tree_nodes
from ._cam_templates import _LOCATION, _find_template_by_name, _template_library
from . import _inputs

app = adsk.core.Application.get()

# AutomaticGenerationModes member per friendly mode. The _GEN Choice validates against these keys,
# so an unrecognized 'generate' is a hard error, not a silent create-ops-generate-nothing.
_GEN_MODES = {
"skip": "SkipGeneration",            # default: create ops, don't generate toolpaths
"generate": "ForceGeneration",       # create AND generate the toolpaths
}
_GEN = _inputs.Choice("generate", options=list(_GEN_MODES), default="skip")


def _setup_op_nodes(setup_obj) -> list:
    """The operation nodes under one setup, each carrying the walk's 'Setup / ... / op' breadcrumb."""
    return [n for n in tree_nodes(setup_obj) if n.kind == "operation"]


def _path_census(nodes) -> dict:
    """How many of these operation nodes each breadcrumb holds. The PATH is the census key: the
    walk visits a parent's own operations before its folders, so crediting a held 'Drill1' in a
    folder to a new 'Drill1' under the setup would report the held operation as the applied one."""
    census = {}
    for n in nodes:
        census[n.path] = census.get(n.path, 0) + 1
    return census


def _added_operations(setup_obj, before):
    """(the operations a setup GAINED, the names its own walk cannot separate) - the post-apply
    breadcrumbs with the ones it already held taken off, never the template's operations nor what
    the apply call handed back. A breadcrumb that HELD an operation and now carries more cannot say
    WHICH of them landed, so every operation at it is withheld and its name reported instead."""
    nodes = _setup_op_nodes(setup_obj)
    after = _path_census(nodes)
    remaining = dict(before)
    added, collisions = [], []
    for n in nodes:
        held = before.get(n.path, 0)
        if held and after[n.path] > held:
            if n.name not in collisions:
                collisions.append(n.name)
            continue          # nothing here separates the held operation from the one that landed
        if remaining.get(n.path):
            remaining[n.path] -= 1
            continue
        added.append(n.obj)
    return added, collisions


def _applied_rows(ops):
    """(rows, the names carrying no tool) - one {name, strategy, tool} row per applied operation,
    'tool' being what that operation's own Operation.tool describes itself as."""
    rows, unselected = [], []
    for op in ops:
        name = safe(lambda op=op: op.name)
        t = safe(lambda op=op: op.tool)
        row = {"name": name, "strategy": safe(lambda op=op: op.strategy),
               "tool": safe(lambda t=t: t.description) if t is not None else None}
        if t is None:
            unselected.append(name)
        elif row["tool"] is None:
            # A tool IS assigned here and only its description did not read - not an unselected one.
            row["tool_description_unread"] = True
        rows.append(row)
    return rows, unselected


_TOOL_REMEDY = ("cam_edit_operation(operation=<name>, tool_scope='document', tool_index=<n>) "
                "assigns one per operation; cam_edit_tools lists this document's tools, and adds "
                "one when it holds none.")


def _named(names) -> str:
    """A wire list of operation names, a name that did not read MARKED rather than printed as null."""
    return named_with_remainder([n or "(name unread)" for n in names])


def _apply_note(rows, unselected, collisions, added_count, gen_key) -> str:
    """What the post-apply read of the SETUP observed, and the call that follows from it."""
    if collisions:
        return (f"{_named(collisions)} names more than one operation in one container here, so "
                "which of them this apply landed is not established: they are left out of "
                "'operations' and 'ready' is withheld. cam_get(include=['operations'], setup=...) "
                "lists every operation with its path.")
    if unselected:
        return (f"{len(unselected)} of {len(rows)} applied operations carry no tool that could be "
                f"read ({_named(unselected)}), so 'ready' reads false. {_TOOL_REMEDY}")
    if not rows or (added_count is not None and len(rows) != added_count):
        return (f"'operations' carries {len(rows)} row(s) read as new to the setup and does not "
                "account for operations_added, so 'ready' is withheld. "
                "cam_get(include=['operations'], setup=...) lists every operation it holds.")
    return (f"All {len(rows)} applied operations read a tool back."
            + (" Their toolpaths are not generated yet - run cam_generate."
               if gen_key == "skip" else " cam_get(include=['operations']) reads their state."))


def handler(setup: str = "", template_url: str = "",
            template_name: str = "", location: str = "cloud",
            generate: str = "skip") -> dict:
    """Apply a CAM template to a setup, recreating its operations there - identified by
    'template_url' or by 'template_name' within 'location'."""
    if not (setup or "").strip():
        return error("Provide 'setup' - the name of the setup to apply the template to.")
    if not (template_url.strip() or template_name.strip()):
        return error("Provide 'template_url' or 'template_name'.")
    # Validate the enums up front (fail fast, before any CAM work) - an unknown 'generate' must error,
    # not silently skip generation.
    gen_key, gerr = _GEN.resolve(generate)
    if gerr:
        return error(gerr)
    loc_key, lerr = _LOCATION.resolve(location)
    if lerr:
        return error(lerr)

    cam, err = get_cam()
    if err:
        return error(err)
    lib, err = _template_library()
    if err:
        return error(err)

    # Find the target setup.
    # The resolver's own refusal is returned verbatim: it is the one place that knows whether the
    # name was ABSENT or AMBIGUOUS, and only it can say which.
    target_setup, _names, serr = find_setup(cam, setup)
    if not target_setup:
        return error(serr)

    # Resolve the template.
    template = None
    if template_url.strip():
        u = safe(lambda: adsk.core.URL.create(template_url.strip()))
        if not u:
            return error(f"Invalid template URL: '{template_url}'.")
        template = safe(lambda: lib.templateAtURL(u))
        if not template:
            return error(f"No template found at URL: {template_url}")
        # Both given: the url is an ADDRESS and the name is what the caller believes is there. A
        # url published by folder position is only as good as that pairing, so a disagreement is
        # refused rather than applying whichever the url reached.
        resolved = (safe(lambda: template.name) or "").strip()
        if template_name.strip() and resolved.lower() != template_name.strip().lower():
            return error(f"'template_url' loads the template '{resolved}', but 'template_name' says "
                         f"'{template_name.strip()}' - nothing was applied. Pass the url alone to "
                         f"apply '{resolved}', or the name alone to search for "
                         f"'{template_name.strip()}'.")
    else:
        template, where = _find_template_by_name(lib, loc_key, template_name.strip())
        if not template:
            # An ambiguity hint is already a complete, self-contained message - don't wrap it as
            # 'not found' (it WAS found, in more than one place).
            if where and "ambiguous" in where:
                return error(where)
            return error(f"Template named '{template_name}' not found under '{loc_key}'. "
                          + (where or ""))

    if not safe(lambda: template.isValidTemplate, True):
        return error(f"Template '{safe(lambda: template.name)}' is not in a valid state to apply.")

    # Build the input + apply.
    ops_before = safe(lambda: target_setup.allOperations.count)
    before_census = _path_census(_setup_op_nodes(target_setup))
    try:
        ti = adsk.cam.CreateFromCAMTemplateInput.create()
        ti.camTemplate = template
        mode_name = _GEN_MODES[gen_key]
        mode_val = safe(lambda: getattr(adsk.cam.AutomaticGenerationModes, mode_name))
        mode_err = set_verified(
            ti, "mode", mode_val, f"AutomaticGenerationModes.{mode_name}",
            "CreateFromCAMTemplateInput")
        if mode_err:
            return error(mode_err)
        created = target_setup.createFromCAMTemplate2(ti)
    except Exception as e:
        return error(f"Failed to apply template: {e}")
    ops_after = safe(lambda: target_setup.allOperations.count)
    if ops_before is not None and ops_after is not None and ops_after <= ops_before:
        return error(f"createFromCAMTemplate2 ran but the setup's operation count did not increase "
                     f"({ops_before} before and after) - no operations were added. The template may "
                     "not be compatible with this setup.")

    created_names = []
    try:
        for ob in (created or []):
            created_names.append(safe(lambda: ob.name))
    except Exception:
        pass

    added_count = ((ops_after - ops_before)
                   if (ops_before is not None and ops_after is not None) else None)
    # The per-op status is read off the SETUP's own walk: a template can land operations carrying
    # no tool, and what the apply call returned is not what the setup took.
    added_ops, collisions = _added_operations(target_setup, before_census)
    rows, unselected = _applied_rows(added_ops)
    return ok({
        "applied": True,
        "template": safe(lambda: template.name),
        "setup": safe(lambda: target_setup.name),
        "generation_mode": gen_key,
        "created_count": len(created_names),
        "created_operations": created_names,
        "operations_added": added_count,
        "operations": rows,
        "tool_unselected": unselected,
        # present-and-empty: the names withheld from 'operations' above, so the caller sees what
        # this read could not attribute rather than inferring it from a short list.
        "collisions": collisions,
        "ready": (bool(rows) and not unselected and not collisions
                  and (added_count is None or len(rows) == added_count)),
        "note": _apply_note(rows, unselected, collisions, added_count, gen_key),
    })


TOOL_DESCRIPTION = (
    "Apply a CAM toolpath template to a setup, recreating its operations there. "
    "cam_get(include=['templates']) lists the templates; generate='generate' can time out while "
    "the work still runs."
)

tool = (
    Tool.create_with_string_input(
        name="cam_apply_template",
        description=TOOL_DESCRIPTION,
        input_param_name="setup",
        input_param_description="Setup name (from cam_get).",
    )
    .add_input_property("template_url", {"type": "string"})
    .add_input_property("template_name", {"type": "string"})
    .add_input_property(*_LOCATION.as_property())
    .add_input_property(*_GEN.as_property())
    .strict_schema()
)
item = Item.create_tool_item(
    tool=tool, write="write", handler=handler,
    run_on_main_thread=True,
    # The setup's own walk is what the applied operations are read off, gated on a count that grew.
    # A CAMTemplate exposes name/description/validity only, so nothing here can be compared against
    # the operations the template holds.
    verification=Verification(
        kind="inline",
        evidence_test="tests/unit/test_cam_apply_template.py::TestApplyTemplateToolStatus"
                      "::test_an_operation_with_no_tool_is_named_with_its_remedy",
        rung="exists")
)


def register_tool():
    register(item)
