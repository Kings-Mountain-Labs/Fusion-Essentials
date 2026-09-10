# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Delete a USER parameter, refusing one another expression references. WRITES."""

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import ok, error, safe
from . import _common
from ._param_common import _expression_identifiers
# the shared timeline-health walk (before/after edit guard) - one home in _common
from ._common import timeline_health as _timeline_health


def _user_parameter_names(design):
    """The design's user-parameter names right now, or None when the collection would not enumerate."""
    # None is "the walk declined", never "the design holds none": coerced to a list, an unreadable
    # walk would prove the deleted name absent without reading anything.
    coll = safe(lambda: design.userParameters)
    n = _common.counted(lambda: coll.count) if coll is not None else None
    if n is None:
        return None
    return [safe(lambda i=i: coll.item(i).name) for i in range(n)]


def handler(name: str = "") -> dict:
    """Delete a USER parameter, guarded against a referencing consumer or a timeline regression. WRITES."""
    name = (name or "").strip()
    if not name:
        return error("Provide 'name' - the parameter to delete.")
    design = _common.design()
    if not design:
        return error("No active design.")
    p = safe(lambda: design.userParameters.itemByName(name))
    if not p:
        return error(f"No USER parameter named '{name}' (only user parameters can be deleted; "
    "model/feature parameters cannot).")

    # who references it? scan expressions so we can warn precisely instead of a cryptic failure.
    consumers = []
    for mp in safe(lambda: design.allParameters, []) or []:
        e = safe(lambda mp=mp: mp.expression) or ""
        if name in _expression_identifiers(e) and \
                (safe(lambda mp=mp: mp.name) != name):
            consumers.append(safe(lambda mp=mp: mp.name))
    if consumers:
        return error(f"'{name}' is referenced by: {', '.join(c for c in consumers if c)}. "
    "Re-point or remove those first.")

    err_before, _, _ = _timeline_health(design)
    try:
        did = p.deleteMe()
    except Exception as e:
        return error(f"Could not delete '{name}': {e}")
    if not did:
        return error(f"Fusion refused to delete '{name}' (it may be in use).")
    names_after = _user_parameter_names(design)
    if names_after is None:
        return error(f"deleteMe() reported success for '{name}' but the design's user parameters "
                     "would not re-read, so whether it is gone is UNVERIFIED - a list that did not "
                     "read is not a list without it. Re-read with param_get before deleting more.")
    if name in names_after:
        return error(f"deleteMe() reported success but '{name}' is still in the design's user "
                     "parameters - it was NOT deleted. Nothing was rolled back; re-read with "
                     "param_get to see what is actually there.")
    err_after, _, _ = _timeline_health(design)
    if len(err_after) > len(err_before):
        return error(f"Deleting '{name}' introduced a timeline error ({err_after}). "
    "The deletion stands - undo in Fusion if needed.")
    return ok({"deleted": True, "name": name,
        "note": "User parameter deleted - the name is gone from the design's user parameters, and "
                "the timeline walks clean (no new errors)."})


TOOL_DESCRIPTION = (
    "Delete a USER parameter. A parameter another expression references is refused, naming the "
    "consumers.")

tool = (
    Tool.create_with_string_input(
        name="param_delete",
        description=TOOL_DESCRIPTION,
        input_param_name="name",
        input_param_description="User parameter to delete.",
    ).strict_schema()
)
item = Item.create_tool_item(
    tool=tool, write="destructive", handler=handler, run_on_main_thread=True,
    verification=Verification(
        kind="inline", rung="value",
        evidence_test="tests/unit/test_param_delete.py::TestAbsenceReRead"
                      "::test_a_survivor_after_a_true_delete_is_an_error"))


def register_tool():
    register(item)
