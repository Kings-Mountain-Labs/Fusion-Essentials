# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Place an image file on the ACTIVE drawing's active sheet (Images.createInput + Images.insert).
The Images collection exposes only createInput and insert - no count, no item, no delete - so an
inserted image cannot be listed, verified, moved or removed through the API; the insert boolean plus
the document's modified flag are the whole verifiable effect. WRITES.
"""

import math
import os

import adsk.core

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import ok, error, safe
from . import _common
from . import _drawing_common
from . import _outputs

# What this tool RETURNS (declared once; drives the PRODUCES: prose + the assert-present contract test).
RETURNS = [
    _outputs.ReturnsValue("document_modified", "whether the document reads modified after the call"),
]

# The extensions measured to both INSERT and RENDER on a sheet. The guard is what backs the input's
# claim about which files this tool takes; nothing wider is measured, so nothing wider is accepted.
_IMAGE_EXTS = (".bmp", ".jpg", ".jpeg", ".png", ".tif", ".tiff")

_NO_READBACK_NOTE = (
    "The image is NOT readable back: the Images collection has no count, item or delete, so an "
    "inserted image cannot be listed, verified, moved or removed through the API - undo it in "
    "Fusion. Export the sheet (drawing_export) to see it. A file no decoder can read inserts "
    "successfully and renders nothing, so a readable image file is the caller's responsibility.")

# The one failure the insert boolean and the modified flag BOTH miss: an insert anchored off the
# sheet returns true, flips the document to modified, and renders nothing at all.
_OFF_SHEET_UNCHECKED_NOTE = (
    "The position was NOT bounds-checked (%s): an insert anchored off the sheet returns true and "
    "renders nothing, and no read-back can tell that from a real placement - export the sheet to "
    "confirm the image is on it.")


# The missing-file refusal. It is HELD rather than returned the moment it is found (see handler),
# so it lives here as one string rather than inline at a return.
_FILE_NOT_FOUND = ("Image file not found: %s. Pass a local path that exists (a cloud file must be "
                   "downloaded first - see data_download_file).")


def _off_sheet_error(dwg, sheet, px, py):
    """(refusal, unchecked_reason) for an image anchor: the refusal is non-empty only for an anchor
    measurably outside the sheet, unchecked_reason whenever the bound could not run at all and the
    caller must publish it. An image POSITION is standard-keyed (mm under ISO, in under ASME) while
    Sheet.width/height are always mm, so the anchor converts through DOCUMENT_UNIT first."""
    width = _common.measured(lambda: sheet.width)
    height = _common.measured(lambda: sheet.height)
    if width is None or height is None:
        return "", (f"sheet '{safe(lambda: sheet.name)}' does not report both a width and a height")
    unit = _drawing_common.coordinate_unit(dwg)
    if unit is None:
        return "", "the drawing standard is unreadable, so the position's unit is unknown"
    per_mm = _common.scale(unit) / _common.scale(_drawing_common.SHEET_EXTENT_UNIT)
    x_mm, y_mm = px * per_mm, py * per_mm
    if 0 <= x_mm <= width and 0 <= y_mm <= height:
        return "", ""
    return (f"position ({px}, {py}) {unit} is {round(x_mm, 3)} x {round(y_mm, 3)} mm, off sheet "
            f"'{safe(lambda: sheet.name)}', which spans 0 to {width} x 0 to {height} "
            f"{_drawing_common.SHEET_EXTENT_UNIT}. An off-sheet insert returns success and renders "
            "nothing, and an image cannot be read back or moved afterwards, so nothing was placed. "
            "Pass a position inside the sheet."), ""


def _composed(held, message):
    """One refusal text carrying every fact the call is wrong about: the HELD refusals (see handler)
    in front of the one just hit, space-separated. Each sentence is the same text that failure
    states on its own, so a call wrong in one way reads as that failure alone, and a call wrong in
    two names both."""
    return " ".join(list(held) + [message])


def handler(image_path: str = "", x=None, y=None, scale=None, rotate_deg=None) -> dict:
    """See TOOL_DESCRIPTION."""
    path = (image_path or "").strip().strip('"')
    if not path:
        return error("Provide 'image_path' - the local path of the image file to place.")
    ext = os.path.splitext(path)[1].lower()
    if ext not in _IMAGE_EXTS:
        return error(f"Unsupported image file '{ext or path}'. A sheet image is one of: "
                     f"{', '.join(_IMAGE_EXTS)}.")
    # The file is checked BEFORE createInput: a Fusion failure raised inside the call rolls the whole
    # MCP script transaction back. Its refusal is HELD, not returned, so the independent off-sheet
    # refusal below stays observable to a caller whose file is also missing.
    held = []
    if not safe(lambda: os.path.isfile(path)):
        held.append(_FILE_NOT_FOUND % path)

    if x is None or y is None:
        return error(_composed(held, "Provide both 'x' and 'y' - the sheet position to place the "
                                    "image at."))
    try:
        px, py = float(x), float(y)
    except (TypeError, ValueError):
        return error(_composed(held, f"'x' and 'y' must be numbers in sheet units (got {x!r} / "
                                     f"{y!r})."))

    factor = None
    if scale is not None:
        try:
            factor = float(scale)
        except (TypeError, ValueError):
            return error(_composed(held, f"'scale' must be a number (got {scale!r})."))
        if factor <= 0:
            return error(_composed(held, f"'scale' must be greater than 0 (got {factor})."))

    # ImageInsertInput.rotationAngle is RADIANS - a pi assignment renders the image flipped a half
    # turn about its insert position, where the same number read as degrees would have left the
    # image visually where it was. The caller works in degrees and the conversion happens here.
    degrees = angle_rad = None
    if rotate_deg is not None:
        try:
            degrees = float(rotate_deg)
        except (TypeError, ValueError):
            return error(_composed(held, f"'rotate_deg' must be a number of degrees (got "
                                         f"{rotate_deg!r})."))
        angle_rad = math.radians(degrees)

    dwg = _drawing_common.active_drawing()
    if dwg is None:
        return error(_composed(held, "No drawing to place an image on: the active document is "
                                    "not a drawing. Open the drawing (doc_open by file_id) and "
                                    "make it active, then retry."))
    sheet = safe(lambda: dwg.activeSheet)
    if sheet is None:
        return error(_composed(held, "The active drawing has no active sheet to place an image "
                                    "on."))
    images = safe(lambda: sheet.images)
    if images is None:
        return error(_composed(held, "This sheet exposes no images collection - an image cannot "
                                    "be placed on it."))

    bounds_err, unchecked = _off_sheet_error(dwg, sheet, px, py)
    if bounds_err:
        return error(_composed(held, bounds_err))
    if held:
        # The position is good and the file is not - the held refusal is the whole failure.
        return error(" ".join(held))
    sheet_extent = [_common.measured(lambda: sheet.width), _common.measured(lambda: sheet.height)]

    try:
        inp = images.createInput()
    except Exception as ex:
        return error(f"Images.createInput failed: {ex}")
    if inp is None:
        return error("Images.createInput returned nothing - no image can be placed on this sheet.")

    # imageFilePath reads back EXACTLY the string assigned - separators are not normalised either
    # way - so the shared set-then-read-back check is an equality test here, not a truthiness one.
    serr = _common.set_verified(inp, "imageFilePath", path, f"image_path '{path}'",
                                "ImageInsertInput")
    if serr:
        return error(serr)
    try:
        inp.position = adsk.core.Point2D.create(px, py)
    except Exception as ex:
        return error(f"Could not set the image position: {ex}")
    # scale is a RATIO on the image's natural size, which itself varies with the sheet - so no
    # absolute rendered size is published. An omitted scale is left untouched, leaving the API's own
    # default, and the payload reports null.
    if factor is not None:
        serr = _common.set_verified(inp, "scale", factor, f"scale={factor}", "ImageInsertInput")
        if serr:
            return error(serr)
    # An omitted rotate_deg leaves rotationAngle at the API's own default, like scale.
    if angle_rad is not None:
        serr = _common.set_verified(inp, "rotationAngle", angle_rad,
                                    f"rotate_deg={degrees} ({angle_rad} radians)",
                                    "ImageInsertInput")
        if serr:
            return error(serr)

    doc = safe(lambda: adsk.core.Application.get().activeDocument)
    modified_before = safe(lambda: bool(doc.isModified))

    did = images.insert(inp)      # the mutation - a raise must surface, never be swallowed
    if not did:
        return error(f"Images.insert returned false for '{path}' - Fusion placed nothing. Treating "
                     "this as a failure.")

    # The flag only convicts when it was readable and FALSE on both sides; an unreadable read is
    # published as null and joins the already-modified case in the inconclusive branch, since
    # neither can tell a placement from a no-op.
    modified_after = safe(lambda: bool(doc.isModified))
    if modified_before is False and modified_after is False:
        return error(f"Images.insert reported success for '{path}' but the document is still "
                     "unmodified, so nothing was placed. Treating this as a failure. " +
                     _NO_READBACK_NOTE)

    note = "Image placed on the sheet. " + _NO_READBACK_NOTE + " doc_save to keep it."
    if unchecked:
        note = (_OFF_SHEET_UNCHECKED_NOTE % unchecked) + " " + note
    if modified_before is None or modified_after is None:
        note = ("The document's modified flag could not be read, so nothing here confirms the "
                "insert took. " + note)
    elif modified_before:
        note = ("The document was ALREADY modified before this call, so the modified flag cannot "
                "confirm this insert on its own. " + note)

    return ok({
        "inserted": True,
        "sheet": safe(lambda: sheet.name),
        "image_path": path,
        "position": [px, py],
        # An image position is standard-keyed - mm under ISO, in under ASME - which is what this key
        # names; documentSettings.units is the DIMENSION display unit and does not label x/y. The
        # sheet spans 0..width x 0..height from a corner, and a NEGATIVE anchor is silently CLAMPED.
        "coordinate_unit": _drawing_common.coordinate_unit(dwg),
        "sheet_units": _drawing_common.sheet_units(dwg),
        "sheet_extent": sheet_extent,
        "sheet_extent_unit": _drawing_common.SHEET_EXTENT_UNIT,
        "position_bounds_checked": not unchecked,
        "scale": factor,
        # Both faces of the one rotation: what the caller passed, and the radians that landed on
        # the input - the rotation turns the image about the position above, not about the sheet.
        "rotate_deg": degrees,
        "rotation_radians": angle_rad,
        "document_modified": modified_after,
        "document_modified_before": modified_before,
        "note": note,
    })


TOOL_DESCRIPTION = (
    "Place an image file from local disk onto the active drawing's active sheet."
)

FULL_DESCRIPTION = TOOL_DESCRIPTION + "\n" + _outputs.produces_block(RETURNS)

tool = (
    Tool.create_simple(name="drawing_insert_image", description=FULL_DESCRIPTION)
    .add_input_property("image_path", {"type": "string",
            "description": "Local path of the image file."})
    .add_input_property("x", {"type": "number",
            "description": "Sheet x position, in the sheet's own coordinate unit."})
    .add_input_property("y", {"type": "number",
            "description": "Sheet y position, in the sheet's own coordinate unit."})
    .add_input_property("scale", {"type": "number",
            "description": "Multiplies the image's natural size."})
    .add_input_property("rotate_deg", {"type": "number",
            "description": "Degrees, about the image's position."})
    .strict_schema()
)

item = Item.create_tool_item(
    tool=tool, write="write", handler=handler, run_on_main_thread=True,
    verification=Verification(kind="gap", defect_id="DRAW-2"))


def register_tool():
    register(item)
