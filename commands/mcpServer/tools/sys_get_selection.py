# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Read ui.activeSelections back as structured per-entity detail, with a find_geometry handle."""

import adsk.core

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item
from ..mcp_primitives.registry import register
from ._common import ok, error, safe
from ._cam_common import clamp_rows
from . import _inputs
from . import _outputs
# Every selection record's 'handle' is minted by the shared _selection_record - the key RETURNS
# declares below, and the one sys_request_selection publishes from the same builder.
from ._sys_common import RETURNS, _selection_record, _ui

app = adsk.core.Application.get()

_SELECTION_CAP = 50
_SELECTION_CEILING = 200   # every record crosses the wire, so max_results cannot lift the ceiling


def handler(require: str = "", max_results: int = _SELECTION_CAP) -> dict:
    """Read the user's current Fusion selection and describe each selected entity."""
    ui = _ui()
    if not ui:
        return error("No Fusion user interface available.")

    sels = safe(lambda: ui.activeSelections)
    count = safe(lambda: sels.count, 0) if sels is not None else 0
    if not count:
        return error("Nothing is selected in Fusion. Ask the user to click an entity, then "
    "call sys_get_selection again (or re-run sys_request_selection).")

    cap = clamp_rows(max_results, _SELECTION_CAP, _SELECTION_CEILING)
    selections = []
    # A positional walk, not iter_collection: an unreadable selection becomes a refusal here rather
    # than a short list labelled 'truncated'.
    try:
        for i in range(min(count, cap)):
            selections.append(_selection_record(sels.item(i)))
    except Exception as e:
        return error(f"Could not read the selection: {e}")

    truncated = count > len(selections)
    payload = {
    "selection_count": count,
    "selections": selections,
    "truncated": truncated,
    "active_document": safe(lambda: app.activeDocument.name),
    "note": _outputs.produces_block(RETURNS),
    }
    if truncated:
        payload["note"] += ("\nselections was capped at %d of %d; raise max_results to see the rest."
                            % (cap, count))

    want = (require or "").strip().lower()
    if want:
        kinds = {s.get("kind") for s in selections}
        payload["required_kind"] = want
        if want in kinds:
            payload["matches_required"] = True
        elif truncated:
            # The kinds set covers the walked PREFIX only, so absence over the tail has no verdict.
            payload["matches_required"] = None
            payload["note"] += (f" No '{want}' in the first {len(selections)} of {count} "
                                f"selections - the rest were not read, so whether the selection "
                                f"includes a '{want}' is unknown. Raise max_results to walk them.")
        else:
            payload["matches_required"] = False
            payload["note"] += (f" Selection does not include a '{want}'. It contains: "
                                f"{', '.join(k for k in kinds if k)}. Re-prompt with "
                                "sys_request_selection if you need a different kind.")
    return ok(payload)


_REQUIRE_KINDS = ("face", "edge", "vertex", "body", "component")

TOOL_DESCRIPTION = (
    "Read the user's CURRENT selection in Fusion.\n"
    + _outputs.produces_block(RETURNS)
)
tool = (
    Tool.create_simple(name="sys_get_selection", description=TOOL_DESCRIPTION)
    .add_input_property(*_inputs.Choice("require", list(_REQUIRE_KINDS),
            description="Flags a mismatch; nothing is filtered.").as_property())
    .add_input_property("max_results", {"type": "integer",
            "description": f"Max {_SELECTION_CEILING}."})
    .strict_schema()
)
item = Item.create_tool_item(tool=tool, write="read", handler=handler,
                             run_on_main_thread=True)


def register_tool():
    register(item)
