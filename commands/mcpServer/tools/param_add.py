# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Add one or many USER parameters, rolling back any add that introduces a timeline error. WRITES."""

import adsk.core

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import ok, error, safe
from . import _common
# the shared timeline-health walk (before/after edit guard) - one home in _common
from ._common import timeline_health as _timeline_health
from ._param_common import _find_parameter, _param_summary


def _text_unit_remedy(refusal, unit, expression):
    """The quoting remedy for Fusion's OWN invalid-expression refusal under 'Text', else ''."""
    # Runs on the except path, where a batch item's unit/expression is whatever JSON the schema's
    # plain-object items carried - so every read here coerces rather than assuming a string.
    expr = str(expression or "").strip()
    if str(unit or "").strip().lower() != "text" or not expr or any(q in expr for q in "'\""):
        return ""
    if "invalid expression" not in str(refusal).lower():
        return ""      # a name or unit Fusion refused is a different failure, not this one
    return f" - a 'Text' parameter holds a QUOTED literal: pass \"'{expr}'\", not {expr}."


def _add_one(design, name, expression, unit, comment, favorite):
    """Add a single user parameter, health-guarded. Returns (result_dict, error_str). On success
    error_str is None; on failure result_dict is None and error_str explains why (param rolled back
    if it broke the timeline)."""
    name = (name or "").strip()
    if not name:
        return None, "missing 'name' for a new parameter."
    if (expression or "").strip() == "" and expression != "0":
        return None, f"'{name}': missing 'expression' (the value)."
    if _find_parameter(design, name):
        return None, f"a parameter named '{name}' already exists (use param_set to change it)."

    err_before, _, _ = _timeline_health(design)
    try:
        vi = adsk.core.ValueInput.createByString(expression)
        p = design.userParameters.add(name, vi, unit or "", comment or "")
    except Exception as e:
        return None, f"could not add '{name}': {e}{_text_unit_remedy(e, unit, expression)}"
    if not p:
        return None, f"adding '{name}' returned nothing."
    if favorite:
        safe(lambda: setattr(p, "isFavorite", True))

    err_after, warn_after, _ = _timeline_health(design)
    if len(err_after) > len(err_before):
        safe(lambda: p.deleteMe())
        return None, (f"adding '{name}' introduced a timeline error ({err_after}); rolled back. "
                "Check the expression/unit.")
    # Report the ACTUAL favorite state read back from the parameter, not the request - so a silently
    # failed isFavorite set doesn't surface as a false success.
    return {"parameter": _param_summary(p), "favorite": bool(safe(lambda: p.isFavorite, False)),
                "timeline_warnings": warn_after}, None


def handler(name: str = "", expression: str = "", unit: str = "mm",
            comment: str = "", favorite: bool = False, params: list = None) -> dict:
    """Add one (name+expression) or many ('params' list) user parameters. WRITES; health-guarded."""
    design = _common.design()
    if not design:
        return error("No active design.")

    # batch path
    if params:
        if not isinstance(params, list):
            return error("'params' must be a list of {name, expression, ...} dicts.")
        results = []
        for i, spec in enumerate(params):
            if not isinstance(spec, dict):
                return error(f"params[{i}] must be a dict with 'name' and 'expression'.")
            res, err = _add_one(design, spec.get("name", ""), spec.get("expression", ""),
                                spec.get("unit", "mm"), spec.get("comment", ""),
                                bool(spec.get("favorite", False)))
            if err:
                return error(f"params[{i}]: {err} ({len(results)} added before this).")
            results.append(res)
        return ok({"added_count": len(results), "results": results,
        "note": f"{len(results)} user parameters added; timeline verified."})

    # single path
    res, err = _add_one(design, name, expression, unit, comment, bool(favorite))
    if err:
        return error(err[0].upper() + err[1:])
    return ok({"added": True, **res,
        "note": "User parameter added; timeline verified (no new errors)."})


TOOL_DESCRIPTION = (
    "Add ONE user parameter (name + expression), or MANY with 'params'. Change an existing one "
    "with param_set.")

# NOTE: built with create_simple + a PLAIN name property (not create_with_string_input, which marks
# its input REQUIRED) - batch mode legitimately omits 'name', so the schema must not demand it.
tool = (
    Tool.create_simple(
        name="param_add",
        description=TOOL_DESCRIPTION,
    )
    .add_input_property("name", {"type": "string",
            "description": "Single add; omit with 'params'."})
    .add_input_property("expression", {"type": "string",
            "description": "e.g. '25 mm', 'PartX/2', \"'text'\"; function args use ';': max(a; b)."})
    .add_input_property("unit", {"type": "string",
            "description": "mm/cm/in/deg, '' unitless, 'Text' for text."})
    .add_input_property("comment", {"type": "string"})
    .add_input_property("favorite", {"type": "boolean"})
    .add_input_property("params", {"type": "array",
            "description": "{name, expression, unit?, comment?, favorite?} per entry.",
            "items": {"type": "object"}})
    .strict_schema()
)
item = Item.create_tool_item(
    tool=tool, write="write", handler=handler, run_on_main_thread=True,
    verification=Verification(
        kind="effect", rung="value",
        evidence_test="tests/unit/test_param_add.py::TestAddFavorite"
                      "::test_a_stuck_favorite_is_published_as_the_parameter_reads_it"))


def register_tool():
    register(item)
