# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Auto-dimension one view on a sheet of the ACTIVE drawing (Sheet.createAutoDimensionInput +
Sheet.autoDimension). adsk.drawing carries no dimension entity class, so the dimensions produced
cannot be counted or read back - the call's own boolean plus the document's modified flag are the
whole verifiable effect, and the sheet must be exported (drawing_export) to see them. WRITES.
"""

import adsk.core
import adsk.drawing

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import ok, error, safe
from . import _common
from . import _drawing_common
from . import _inputs
from . import _outputs

# What this tool RETURNS (declared once; drives the PRODUCES: prose + the assert-present contract test).
RETURNS = [
    _outputs.ReturnsValue("document_modified", "whether the document reads modified after the call"),
]

# datum key -> the DatumPositionsTypes member name (the strategy table is the shared
# _drawing_common.DIMENSION_STRATEGIES). Every member is read by NAME, and one this Fusion version
# does not define is refused rather than silently running the default strategy.
_DATUM_MEMBERS = {
    "bottom_left": "BottomLeftDatumPositionType",
    "bottom_right": "BottomRightDatumPositionType",
    "top_left": "TopLeftDatumPositionType",
    "top_right": "TopRightDatumPositionType",
}

_STRATEGY = _inputs.Choice("strategy", list(_drawing_common.DIMENSION_STRATEGIES),
                           default="baseline")
_DATUM = _inputs.Choice("datum", list(_DATUM_MEMBERS), default="bottom_left",
                        description="Corner to measure from.")

_NO_READBACK_NOTE = (
    "The dimensions themselves are NOT readable: adsk.drawing has no dimension entity, so they "
    "cannot be counted, listed or deleted through the API. Export the sheet (drawing_export) or "
    "open it in Fusion to see what was placed.")


def handler(view: int = None, strategy: str = "baseline", datum: str = "bottom_left", sheet: str = "") -> dict:
    """See TOOL_DESCRIPTION."""
    vals, verr = _inputs.resolve_inputs([_STRATEGY, _DATUM],
                                        {"strategy": strategy, "datum": datum})
    if verr:
        return verr
    strat_key, datum_key = vals["strategy"], vals["datum"]

    dwg = _drawing_common.active_drawing()
    if dwg is None:
        return error("No drawing to dimension: the active document is not a drawing. Open the "
                     "drawing (doc_open by file_id) and make it active, then retry.")
    sheet, sheet_error = _drawing_common.resolve_sheet(dwg, (sheet or "").strip())
    if sheet_error:
        return error(sheet_error)

    views = safe(lambda: sheet.views)
    count = safe(lambda: views.count, 0) or 0
    if count == 0:
        return error(f"Sheet '{safe(lambda: sheet.name)}' has no views to dimension. Drawing views "
                     "are created by the automatic generator (drawing_create) or in the Fusion UI - "
                     "the API cannot add one.")

    if view is None:
        return error(f"Provide 'view' - the index of the view to dimension, 0 to {count - 1} on "
                     f"sheet '{safe(lambda: sheet.name)}' ({count} views).")
    try:
        idx = int(view)
    except (TypeError, ValueError):
        return error(f"'view' must be an integer view index (got {view!r}).")
    if idx < 0 or idx >= count:
        return error(f"'view' index {idx} is out of range: sheet '{safe(lambda: sheet.name)}' has "
                     f"{count} view(s), so the legal indices are 0 to {count - 1}.")
    target = safe(lambda: views.item(idx))
    if target is None:
        return error(f"View index {idx} could not be read off sheet "
                     f"'{safe(lambda: sheet.name)}' - nothing to dimension.")

    try:
        inp = sheet.createAutoDimensionInput()
    except Exception as ex:
        return error(f"createAutoDimensionInput failed: {ex}")
    if inp is None:
        return error("createAutoDimensionInput returned nothing - this sheet cannot be "
                     "auto-dimensioned.")

    serr = _common.set_verified(
        inp, "dimensionStrategy",
        _drawing_common.enum_value("DimensionStrategyTypes",
                                   _drawing_common.DIMENSION_STRATEGIES[strat_key]),
        f"strategy='{strat_key}'", "AutoDimensionInput")
    if serr:
        return error(serr)
    serr = _common.set_verified(
        inp, "datumLocation",
        _drawing_common.enum_value("DatumPositionsTypes", _DATUM_MEMBERS[datum_key]),
        f"datum='{datum_key}'", "AutoDimensionInput")
    if serr:
        return error(serr)

    # A fresh AutoDimensionInput reports view=None and an assigned View reads back as an OBJECT, so
    # this gates on NON-NULL: nothing establishes that the object handed back is the View assigned,
    # so it cannot gate on identity.
    try:
        inp.view = target
    except Exception as ex:
        return error(f"Could not set the view to dimension (index {idx}): {ex}")
    if safe(lambda: inp.view) is None:
        return error(f"Setting the view did not take - AutoDimensionInput.view reads back null after "
                     f"assigning view index {idx}, so the dimensioning would run on no view.")

    doc = safe(lambda: adsk.core.Application.get().activeDocument)
    modified_before = safe(lambda: bool(doc.isModified))

    did = sheet.autoDimension(inp)     # the mutation - a raise must surface, never be swallowed
    if not did:
        return error(f"autoDimension returned false for view index {idx} with strategy "
                     f"'{strat_key}' - Fusion placed nothing. Treating this as a failure.")

    # The flag only convicts when it was readable and FALSE on both sides; an unreadable read is
    # published as null and joins the already-modified case in the inconclusive branch, since
    # neither can tell a placement from a no-op.
    modified_after = safe(lambda: bool(doc.isModified))
    if modified_before is False and modified_after is False:
        return error(f"autoDimension reported success for view index {idx} but the document is still "
                     "unmodified, so nothing was placed. Treating this as a failure. " +
                     _NO_READBACK_NOTE)

    note = ("Auto-dimensioned one view. " + _NO_READBACK_NOTE +
            " Save the drawing with doc_save to keep them.")
    if modified_before is None or modified_after is None:
        note = ("The document's modified flag could not be read, so nothing here confirms the "
                "dimensioning took. " + note)
    elif modified_before:
        note = ("The document was ALREADY modified before this call, so the modified flag cannot "
                "confirm this dimensioning on its own. " + note)

    return ok({
        "dimensioned": True,
        "sheet": safe(lambda: sheet.name),
        "view_index": idx,
        "view_count": count,
        "strategy": strat_key,
        "datum": datum_key,
        "document_modified": modified_after,
        "document_modified_before": modified_before,
        "note": note,
    })


TOOL_DESCRIPTION = (
    "Auto-dimension one view on a named sheet of the active drawing - the API's route to "
    "dimensions."
)

FULL_DESCRIPTION = TOOL_DESCRIPTION + "\n" + _outputs.produces_block(RETURNS)

tool = (
    Tool.create_simple(name="drawing_dimension", description=FULL_DESCRIPTION)
    .add_input_property("view", {"type": "integer",
            "description": "0-based index on the selected sheet."})
    .add_input_property("sheet", {"type": "string",
            "description": "Exact sheet name; omit for the active sheet."})
    .add_input_property(*_STRATEGY.as_property())
    .add_input_property(*_DATUM.as_property())
    .strict_schema()
)

item = Item.create_tool_item(
    tool=tool, write="write", handler=handler, run_on_main_thread=True,
    verification=Verification(kind="gap", defect_id="DRAW-1"))


def register_tool():
    register(item)
