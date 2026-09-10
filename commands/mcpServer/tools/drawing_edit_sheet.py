# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Manage the ACTIVE 2D drawing document's sheets: add, copy, delete, rename, set size, set
orientation, tidy up. Sheet.width/height are read-only and derive from size + orientation, and
EVERY drawing reports them in millimetres - not the adsk-standard centimetres, and not the
drawing's own dimension unit. WRITES (destructive: Sheet.deleteMe cannot be undone).
"""

import adsk.core
import adsk.drawing

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import error, ok, safe
from . import _drawing_common
from ._drawing_common import SHEET_SIZE_MAP
from . import _inputs

app = adsk.core.Application.get()

_ACTIONS = ("add", "copy", "delete", "rename", "set_size", "set_orientation", "tidy_up")

_ACTION = _inputs.Choice("action", list(_ACTIONS), required=True,
                         description="The sheet operation to perform.")
_SHEET_SIZE = _inputs.Choice("sheet_size", list(SHEET_SIZE_MAP),
                             description="For set_size.")
_ORIENTATION = _inputs.Choice("orientation", ["landscape", "portrait"],
                              description="For set_orientation.")
# The shared decoders/records live in _drawing_common (drawing_get reads through the same ones,
# so a write's read-back and the read tool can never disagree about a sheet's facts).
_ORIENTATION_MEMBERS = _drawing_common.ORIENTATION_MEMBERS
_sheet_listing = _drawing_common.sheet_listing
_sheet_facts = _drawing_common.sheet_facts


def _do_add(dwg, new_name):
    sheets = safe(lambda: dwg.sheets)
    if sheets is None:
        return error("The drawing's sheets could not be read - cannot add a sheet.")
    before = safe(lambda: sheets.count, 0) or 0
    want = (new_name or "").strip()
    try:
        sheet_input = sheets.createInput()
        if want:
            # SheetInput carries ONLY a name: size and orientation are set on the sheet AFTER the
            # add. A name another sheet already holds makes add() raise - carried, not swallowed.
            sheet_input.name = want
        sheet = sheets.add(sheet_input)
    except Exception as ex:
        return error(f"Fusion refused the sheet add: {ex}")
    if sheet is None:
        return error("Sheets.add returned nothing - no sheet was added.")
    after = safe(lambda: sheets.count, 0) or 0
    if after <= before:
        return error(f"Sheets.add returned a sheet but the drawing still holds {after} sheet(s) - "
                     "the add did not take.")
    facts = _sheet_facts(sheet)
    out = {
        "added": True,
        "sheet": facts["name"],
        "requested_name": want or None,
        "sheet_count_before": before,
        "sheet_count": after,
        "sheet_units": _drawing_common.sheet_units(dwg),
        "facts": facts,
        "sheets": _sheet_listing(dwg),
        "note": ("Sheet added, inheriting its size and orientation from the ACTIVE sheet. "
                 "'sheets' reports collection positions; export/PDF order is unavailable. "
                 "Export all sheets and inspect the PDF before selecting a page range. "
                 "Set its size with action='set_size', its shape with action='set_orientation'."),
    }
    if want and facts["name"] != want:
        out["name_warning"] = (f"The sheet reports the name '{facts['name']}', not the requested "
                               f"'{want}' - the published name is the one it reports.")
    return ok(out)


def _do_copy(dwg, sheet, new_name):
    sheets = safe(lambda: dwg.sheets)
    before = safe(lambda: sheets.count, 0) or 0
    source = safe(lambda: sheet.name)
    want = (new_name or "").strip()
    try:
        # The native collection position does not establish the copy's PDF page order.
        copied = sheet.copy(want, False)
    except Exception as ex:
        return error(f"Copying sheet '{source}' failed: {ex}")
    if copied is None:
        return error(f"Sheet.copy returned nothing for '{source}'; its effect is unverified. "
                     "The drawing may still be updating asynchronously. Re-read drawing_get "
                     "before retrying, to avoid a duplicate copy.")
    after = safe(lambda: sheets.count, 0) or 0
    if after <= before:
        return error(f"Sheet.copy returned '{safe(lambda: copied.name)}' but the sheet count "
                     f"still reads {after} (before {before}); its effect is unverified. Nothing "
                     "was rolled back. Re-read drawing_get before retrying.")
    facts = _sheet_facts(copied)
    return ok({
        "copied": True,
        "sheet": facts["name"],
        "copied_from": source,
        "requested_name": want or None,
        "sheet_count_before": before,
        "sheet_count": after,
        "sheet_units": _drawing_common.sheet_units(dwg),
        "facts": facts,
        "sheets": _sheet_listing(dwg),
        "note": ("Sheet copied; the facts come from the returned copy, which carries the SOURCE "
                 "sheet's size, orientation, sketches and tables (not the active sheet's). "
                 "Collection positions are listed; export/PDF order is unavailable. "
                 "Export all sheets and inspect the PDF before selecting a page range."),
    })


def _do_delete(dwg, sheet):
    sheets = safe(lambda: dwg.sheets)
    before = safe(lambda: sheets.count, 0) or 0
    name = safe(lambda: sheet.name)
    if before <= 1:
        return error(f"'{name}' is the only sheet this drawing holds ({before}) - refusing to delete "
                     "it. Add a sheet first (action='add'), then delete this one.")
    try:
        did = sheet.deleteMe()
    except Exception as ex:
        return error(f"Deleting sheet '{name}' failed: {ex}")
    if not did:
        return error(f"Fusion refused to delete sheet '{name}' (deleteMe returned false). The sheet "
                     "is still there.")
    # A drawing delete is NOT observable inside the call that performs it: the collection still
    # reports its pre-delete count here. The boolean acknowledges the request; removal remains
    # unverified until a later read.
    reads = safe(lambda: sheets.count, 0) or 0
    return ok({
        "deleted": None,
        "delete_accepted": True,
        "verification": "pending",
        "sheet": name,
        "sheet_count_before": before,
        "sheet_count_still_reads": reads,
        "sheets_still_read": _sheet_listing(dwg),
        "note": ("Fusion accepted deletion (deleteMe=true); deleted=null until a later call confirms "
                 "removal. This cannot be undone. The count and 'sheets_still_read' are observations "
                 "inside this call; neither is a verification. Re-read drawing_get in a later call. "
                 "Collection positions do not establish PDF order."),
    })


def _do_rename(sheet, new_name):
    want = (new_name or "").strip()
    if not want:
        return error("Provide 'new_name' - the name to give the sheet.")
    previous = safe(lambda: sheet.name)
    if previous == want:
        return ok({"renamed": True, "changed": False, "sheet": previous, "previous_name": previous,
                   "requested_name": want,
                   "note": f"The sheet already holds the name '{want}' - nothing changed."})
    try:
        # The MUTATION - a refusal raises here instead of being swallowed into a false success.
        sheet.name = want
    except Exception as ex:
        return error(f"Could not rename sheet '{previous}' to '{want}': {ex}")
    landed = safe(lambda: sheet.name)
    if landed is None:
        return error(f"Renamed '{previous}' to '{want}' but the sheet name could not be read back, so "
                     "the rename is unverified.")
    if landed == previous:
        # The measured no-op: a sheet name another sheet holds - or a case variant of it - is
        # ignored without raising. Sheet names are case-insensitively unique in a drawing.
        return error(f"The rename did not take - the sheet still reads '{previous}' after being set "
                     f"to '{want}'. Sheet names are case-insensitively unique in a drawing: a name "
                     "another sheet holds, or a case variant of it, is ignored. Pick another name.")
    out = {"renamed": True, "changed": True, "sheet": landed, "previous_name": previous,
           "requested_name": want}
    if landed != want:
        out["name_warning"] = (f"The sheet reports the name '{landed}', not the requested '{want}' - "
                               "the published name is the one it reports.")
    return ok(out)


def _do_set_size(dwg, sheet, size_key):
    size_standard, member = SHEET_SIZE_MAP[size_key]
    value = _drawing_common.enum_value("SheetSizes", member)
    if value is None:
        return error(f"This Fusion build has no sheet size '{member}', so '{size_key}' cannot be set.")
    name = safe(lambda: sheet.name)
    standard = _drawing_common.standard_label(dwg)
    if standard is not None and standard != size_standard:
        # Fusion RAISES on a size belonging to the other standard, and a raise inside a drawing
        # document is not reliably rolled back - so the mismatch is refused before anything is set.
        return error(f"The {size_standard.upper()} sheet size '{size_key}' is not valid for this "
                     f"drawing: its standard reads {standard.upper()}, and Fusion rejects a size "
                     f"that does not belong to the active drawing standard. Choose one of the "
                     f"{standard.upper()} sizes.")
    before = _sheet_facts(sheet)
    try:
        sheet.sheetSize = value
    except Exception as ex:
        return error(f"Fusion refused sheet size '{size_key}' for sheet '{name}': {ex}")
    after = _sheet_facts(sheet)
    if after["sheet_size"] != size_key:
        return error(f"The size did not take - sheet '{name}' still reads '{after['sheet_size']}' "
                     f"after being set to '{size_key}'.")
    return ok({
        "sheet": name,
        "sheet_size": after["sheet_size"],
        "previous_sheet_size": before["sheet_size"],
        "width": after["width"],
        "height": after["height"],
        "previous_width": before["width"],
        "previous_height": before["height"],
        "width_height_unit": _drawing_common.SHEET_EXTENT_UNIT,
        "sheet_units": _drawing_common.sheet_units(dwg),
        "note": ("Sheet size set and read back - width and height follow the size and cannot be set "
                 "directly. action='tidy_up' lays the sheet's views out again."),
    })


def _do_set_orientation(dwg, sheet, orientation_key):
    member = _ORIENTATION_MEMBERS[orientation_key]
    value = _drawing_common.enum_value("SheetOrientationTypes", member)
    if value is None:
        return error(f"This Fusion build has no sheet orientation '{member}', so "
                     f"'{orientation_key}' cannot be set.")
    name = safe(lambda: sheet.name)
    before = _sheet_facts(sheet)
    standard = _drawing_common.standard_label(dwg)
    if (orientation_key == "portrait" and standard is not None
            and (standard, before["sheet_size"]) in _drawing_common.NO_PORTRAIT):
        # Fusion RAISES portrait on this size, and a raise inside a drawing document is not
        # reliably rolled back - refused before anything is set.
        return error(f"Fusion does not support portrait orientation on the {standard.upper()} "
                     f"{(before['sheet_size'] or '').upper()} sheet size, so '{name}' keeps its "
                     f"current orientation ('{before['orientation'] or 'unreadable'}'). Set a "
                     "smaller size first (action='set_size').")
    try:
        sheet.orientation = value
    except Exception as ex:
        return error(f"Fusion refused orientation '{orientation_key}' for sheet '{name}': {ex}")
    after = _sheet_facts(sheet)
    if after["orientation"] != orientation_key:
        return error(f"The orientation did not take - sheet '{name}' still reads "
                     f"'{after['orientation']}' after being set to '{orientation_key}' (its size "
                     f"reads '{after['sheet_size']}').")
    return ok({
        "sheet": name,
        "orientation": after["orientation"],
        "previous_orientation": before["orientation"],
        "sheet_size": after["sheet_size"],
        "width": after["width"],
        "height": after["height"],
        "previous_width": before["width"],
        "previous_height": before["height"],
        "width_height_unit": _drawing_common.SHEET_EXTENT_UNIT,
        "sheet_units": _drawing_common.sheet_units(dwg),
        "note": ("Orientation set and read back - the sheet's width and height swap with it. "
                 "action='tidy_up' lays the sheet's views out again."),
    })


def _do_tidy_up(sheet):
    name = safe(lambda: sheet.name)
    views_before = safe(lambda: sheet.views.count, 0) or 0
    modified_before = safe(lambda: app.activeDocument.isModified)
    try:
        # Sheet.tidyUp is a PROPERTY whose READ performs the tidy-up. This is the ONE place that
        # touches it, and it is the mutation this action was called to make - no read path may.
        did = sheet.tidyUp
    except Exception as ex:
        return error(f"Tidying sheet '{name}' failed: {ex}")
    if not did:
        return error(f"Sheet.tidyUp returned false for '{name}' - Fusion did not tidy the sheet.")
    modified_after = safe(lambda: app.activeDocument.isModified)
    if modified_before is False and modified_after is False:
        return error(f"Tidy up reported success for '{name}' but the document is still unmodified - "
                     "nothing on the sheet changed.")
    out = {
        "tidied": True,
        "sheet": name,
        "views": safe(lambda: sheet.views.count, views_before),
        "document_modified": bool(modified_after),
        # The flag only CONFIRMS this tidy when the document was clean beforehand: tidying an
        # already-modified document returns true with isModified already true.
        "modified_confirmed": modified_before is False and bool(modified_after),
        "note": ("Sheet tidied (tidyUp returned true); the view COUNT does not change. The drawing "
                 "is modified in-session but NOT saved - doc_save persists it, drawing_export shows "
                 "the result."),
    }
    if not out["modified_confirmed"]:
        out["note"] = ("The document was already modified before this call, so the modified flag "
                       "cannot confirm this tidy on its own - drawing_export is the check. "
                       + out["note"])
    return ok(out)


def handler(action: str = "", sheet: str = "", new_name: str = "", sheet_size: str = "",
            orientation: str = "") -> dict:
    """See TOOL_DESCRIPTION."""
    act, act_err = _ACTION.resolve(action)
    if act_err:
        return error(act_err)

    dwg = _drawing_common.active_drawing()
    if dwg is None:
        return error("The active document is not a drawing, so it has no sheets. Open the drawing "
                     "(doc_open by file_id) and make it active, then retry.")

    if act == "add":
        return _do_add(dwg, new_name)

    # The shared resolver's refusal is returned verbatim: it is the one place that knows whether the
    # name was absent, and only it can list what the drawing holds.
    target, terr = _drawing_common.resolve_sheet(dwg, (sheet or "").strip())
    if terr:
        return error(terr)

    if act == "copy":
        return _do_copy(dwg, target, new_name)
    if act == "delete":
        return _do_delete(dwg, target)
    if act == "rename":
        return _do_rename(target, new_name)
    if act == "set_size":
        size_key, serr = _SHEET_SIZE.resolve(sheet_size)
        if serr or not size_key:
            return error(serr or "Provide 'sheet_size' - the preset size to give the sheet.")
        return _do_set_size(dwg, target, size_key)
    if act == "set_orientation":
        orient_key, oerr = _ORIENTATION.resolve(orientation)
        if oerr or not orient_key:
            return error(oerr or "Provide 'orientation' - landscape or portrait.")
        return _do_set_orientation(dwg, target, orient_key)
    if act == "tidy_up":
        return _do_tidy_up(target)
    return error(f"Unhandled action '{act}'.")


TOOL_DESCRIPTION = (
    "Manage the active 2D drawing's sheets - add, copy, delete, rename, set_size, set_orientation, "
    "or tidy_up (lay a sheet's views out again)."
)

tool = (
    Tool.create_simple(name="drawing_edit_sheet", description=TOOL_DESCRIPTION)
    .add_input_property(*_ACTION.as_property())
    .add_input_property("sheet", {"type": "string",
            "description": "By name; omit for the active sheet."})
    .add_input_property("new_name", {"type": "string",
            "description": "For add, copy or rename."})
    .add_input_property(*_SHEET_SIZE.as_property())
    .add_input_property(*_ORIENTATION.as_property())
    .strict_schema()
)
item = Item.create_tool_item(
    tool=tool, write="destructive", handler=handler, run_on_main_thread=True,
    # add / copy / rename / set_size / set_orientation / tidy_up each re-read what they wrote and
    # error on a mismatch. delete is the one arm that cannot: a drawing delete is invisible inside
    # its own call, so its payload publishes the boolean and marks the count a reading.
    verification=Verification(
        kind="inline", rung="value",
        evidence_test="tests/unit/test_drawing_edit_sheet.py::TestRename"
                      "::test_a_silently_ignored_rename_is_an_error"))


def register_tool():
    register(item)
