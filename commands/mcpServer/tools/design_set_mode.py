# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Convert the active design between parametric and direct modelling. WRITES (destructive one-way)."""

import adsk.fusion

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import ok, error
from . import _common
from . import _inputs

_TARGETS = (_inputs.MODE_PARAMETRIC, _inputs.MODE_DIRECT)


def handler(target: str = "", confirm_history_loss: bool = False) -> dict:
    """Convert the active design between parametric and direct - idempotent, and refusing the
    timeline-destroying direction without confirm_history_loss=true. WRITES."""
    design = _common.design()
    if not design:
        return error("No active design. Create or open a document first (see doc_new).")

    tgt = (target or "").strip().lower()
    if tgt not in _TARGETS:
        return error(f"'target' must be one of: {', '.join(_TARGETS)} (got "
                     f"'{target}').")

    current = _inputs.current_design_type(design)
    if current == tgt:
        # idempotent no-op, NOT an error
        return ok({"converted": False, "from": current, "to": tgt,
        "history_discarded": False, "note": f"Already {tgt}."})

    # Parametric -> Direct is destructive: it discards the timeline. Refuse without explicit confirm.
    going_to_direct = tgt == _inputs.MODE_DIRECT
    if going_to_direct and confirm_history_loss is not True:
        return error("Converting to DIRECT destroys the timeline and all design history "
    "(irreversible). Re-call with confirm_history_loss=true to proceed.")

    # Resolve the target enum value. Do NOT safe()-wrap the assignment - let a real failure surface.
    types = adsk.fusion.DesignTypes
    target_enum = (types.DirectDesignType if going_to_direct else types.ParametricDesignType)
    try:
        design.designType = target_enum
    except Exception as e:
        return error(f"Could not convert to {tgt}: {e}")

    # Both published flags ride on this READ-BACK, never on the request: an assignment that did not
    # take discarded nothing, and a mode that does not read back settles neither flag.
    now = _inputs.current_design_type(design)
    if now == "unknown":
        converted, discarded = None, None
        note = ("The design mode does not read back after the assignment, so the conversion is "
                "UNCONFIRMED - 'converted' and 'history_discarded' are null. Re-read with "
                "design_get(include=['mode']) to see what the design actually is.")
    elif now == tgt:
        converted, discarded = True, going_to_direct
        note = "Re-run design_get(include=['mode']) to see the updated capability map."
    else:
        converted, discarded = False, False
        note = (f"Assignment did not take - design is still {now}. Nothing was converted and no "
                "history was discarded.")
    return ok({
        "converted": converted,
        "from": current,
    "to": tgt,
    "now": now,
    "history_discarded": discarded,
    "note": note,
    })


TOOL_DESCRIPTION = ("Convert the active design between parametric and direct modeling; going "
            "direct destroys the timeline and all design history.")

tool = (
    Tool.create_simple(
        name="design_set_mode",
        description=TOOL_DESCRIPTION)
    .add_input_property("target", {"type": "string", "enum": list(_TARGETS)})
    .add_input_property("confirm_history_loss", {"type": "boolean",
            "description": "Required to go parametric->direct."})
    .add_required_input("target")
    .strict_schema()
)
item = Item.create_tool_item(
    tool=tool, write="destructive", handler=handler, run_on_main_thread=True,
    verification=Verification(
        kind="effect", rung="value",
        evidence_test="tests/unit/test_design_set_mode.py::TestSetMode"
                      "::test_history_discarded_rides_on_the_read_back_not_the_request"))


def register_tool():
    register(item)
