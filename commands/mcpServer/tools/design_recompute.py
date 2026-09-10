# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""MCP building block: force a full computeAll() of the active design and report the timeline
health afterwards, so a rebuild that breaks a downstream feature surfaces immediately. WRITES."""

import adsk.core
import adsk.fusion

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import ok, error
from . import _common
from ._common import timeline_health as _timeline_health

app = adsk.core.Application.get()


def handler() -> dict:
    """See TOOL_DESCRIPTION."""
    design = _common.design()
    if not design:
        return error("No active design.")
    errors_before, _wb, _ = _timeline_health(design)
    try:
        design.computeAll()
    except Exception as e:
        return error(f"computeAll failed: {e}")
    errors, warnings, _ = _timeline_health(design)
    new_errors = [n for n in errors if n not in errors_before]
    out = {"recomputed": True, "error_count": len(errors),
        "warnings": warnings, "errors": errors,
        "note": "Full recompute done; downstream features rebuilt."}
    if new_errors:
        out["new_errors"] = new_errors
        out["note"] = (f"Recompute ran and surfaced {len(new_errors)} feature error(s) not present "
                       "when it started: " + ", ".join(new_errors) + ". Inspect with design_get.")
    return ok(out)


TOOL_DESCRIPTION = (
    "Force a full recompute so downstream features rebuild against current values. "
    "Reports timeline health afterwards.")

tool = Tool.create_simple(name="design_recompute", description=TOOL_DESCRIPTION).strict_schema()
item = Item.create_tool_item(
    tool=tool, write="write", handler=handler, run_on_main_thread=True,
    verification=Verification(
        kind="effect", rung="exists",
        evidence_test="tests/unit/test_design_recompute.py::TestRecomputeHandler"
                      "::test_errors_surfaced_by_the_recompute_are_named"))


def register_tool():
    register(item)
