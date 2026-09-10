# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Shared substrate for the drawing (2D document) tool family."""

import adsk.core
import adsk.drawing

from . import _common
from ._common import safe

MAP_BLURB = (
    "active_drawing(_document) - the Drawing gate every tool runs; SHEET_SIZE_MAP/"
    "DIMENSION_STRATEGIES/ORIENTATION_MEMBERS/NO_PORTRAIT - key -> member tables plus the "
    "portrait refusals; sheet_units/SHEET_EXTENT_UNIT/DOCUMENT_UNIT/coordinate_unit - the three "
    "units, never mixed; enum_value + the *_label decoders; resolve_sheet/"
    "sheet_listing/sheet_facts - sheet by name, 1-based index, state"
)

# Sheet.width/height are MILLIMETRES on every drawing, ISO and ASME alike (an ASME B sheet, 17 x 11
# inches, reads 431.8 x 279.4). documentSettings.units - what sheet_units decodes - is the DIMENSION
# display unit and says nothing about those two numbers, so a payload labels them with THIS.
SHEET_EXTENT_UNIT = "mm"

# The (standard, sheet size) pairs Fusion refuses portrait on, in its own words. A raise inside a
# drawing document is not reliably rolled back, so both consumers pre-guard on this table.
NO_PORTRAIT = {("iso", "a0"), ("asme", "e")}

# sheet-size key -> (the standard the size belongs to, its SheetSizes member name). Fusion silently
# IGNORES a size belonging to the other standard at creation and RAISES on one assigned to a sheet.
# CustomSizeSheetSize is absent: it cannot be assigned to Sheet.sheetSize at all.
SHEET_SIZE_MAP = {
    "a4": ("iso", "A4ISOSheetSize"), "a3": ("iso", "A3ISOSheetSize"),
    "a2": ("iso", "A2ISOSheetSize"), "a1": ("iso", "A1ISOSheetSize"), "a0": ("iso", "A0ISOSheetSize"),
    "a": ("asme", "AASMESheetSize"), "b": ("asme", "BASMESheetSize"), "c": ("asme", "CASMESheetSize"),
    "d": ("asme", "DASMESheetSize"), "e": ("asme", "EASMESheetSize"),
}

# auto-dimension strategy key -> the DimensionStrategyTypes member name; the family carries exactly
# these eight members.
DIMENSION_STRATEGIES = {
    "overall": "OverallDimensionStrategyType",
    "automatic": "AutomaticDimensionStrategyType",
    "baseline": "BaselineDimensionStrategyType",
    "chain": "ChainDimensionStrategyType",
    "ordinate": "OrdinateDimensionStrategyType",
    "symmetric": "SymmetricDimensionStrategyType",
    "symmetric_with_baseline": "SymmetricWithBaselineDimensionStrategyType",
    "symmetric_with_ordinate": "SymmetricWithOrdinateDimensionStrategyType",
}


def active_drawing_document():
    """The active document cast to a DrawingDocument, or None when it is not a drawing - the level
    carrying documentReferences and updateAllReferences."""
    doc = safe(lambda: adsk.core.Application.get().activeDocument)
    return safe(lambda: adsk.drawing.DrawingDocument.cast(doc))


def active_drawing():
    """The active document's Drawing, or None when the active document is not a drawing."""
    dd = active_drawing_document()
    return safe(lambda: dd.drawing) if dd else None


def sheet_units(dwg):
    """The drawing's DIMENSION display unit - 'mm' or 'in' from its own documentSettings.units, None
    when unreadable (published as null, never a guessed default). NOT the unit Sheet.width/height
    come back in - see SHEET_EXTENT_UNIT."""
    units = safe(lambda: dwg.documentSettings.units)
    if units is None:
        return None
    mm = safe(lambda: adsk.drawing.DrawingUnitTypes.MillimeterDrawingUnitType)
    inch = safe(lambda: adsk.drawing.DrawingUnitTypes.InchDrawingUnitType)
    if mm is not None and units == mm:
        return "mm"
    if inch is not None and units == inch:
        return "in"
    return None


def enum_value(cls_name, member):
    """One adsk.drawing enum member's value by NAME - family and member both - or None when this
    Fusion build carries neither."""
    return safe(lambda: getattr(getattr(adsk.drawing, cls_name), member))


def standard_label(dwg):
    """'iso' or 'asme' from the drawing's own documentSettings.standard; None when unreadable.
    DrawingStandardTypes carries exactly these two members, and the standard is fixed at creation
    (documentSettings.standard has no setter)."""
    value = safe(lambda: dwg.documentSettings.standard)
    if value is None:
        return None
    for key, member in (("iso", "ISODrawingStandardType"), ("asme", "ASMEDrawingStandardType")):
        known = enum_value("DrawingStandardTypes", member)
        if known is not None and value == known:
            return key
    return None


# standard label -> the length unit a drawing's OWN numbers are authored in: DrawingSketch
# coordinates are millimetres when the standard includes ISO and inches when it is ASME without ISO,
# and CreateDrawingInput's CustomSheetSize takes the same unit for its width and height.
DOCUMENT_UNIT = {"iso": "mm", "asme": "in"}


def coordinate_unit(dwg):
    """The length unit sheet COORDINATES land in - 'mm' under ISO, 'in' under ASME, None when the
    standard cannot be read. Keyed to the STANDARD, never to documentSettings.units: the two are set
    independently, so standard='iso' with units='inch' takes coordinates in millimetres while its
    dimensions display in inches, and labelling a coordinate with sheet_units is wrong by 25.4x."""
    return DOCUMENT_UNIT.get(standard_label(dwg))


# orientation key -> SheetOrientationTypes member.
ORIENTATION_MEMBERS = {"landscape": "LandscapeSheetOrientationType",
                       "portrait": "PortraitSheetOrientationType"}


def size_label(value):
    """'a3' for the SheetSizes value a sheet reads back, or None for a value outside the preset
    table - CustomSizeSheetSize among them. A custom-sized sheet keeps its extents in width/height."""
    if value is None:
        return None
    for key, (_standard, member) in SHEET_SIZE_MAP.items():
        if value == enum_value("SheetSizes", member):
            return key
    return None


def orientation_label(value):
    """'landscape'/'portrait' for the SheetOrientationTypes value a sheet reads back, or None."""
    if value is None:
        return None
    for key, member in ORIENTATION_MEMBERS.items():
        if value == enum_value("SheetOrientationTypes", member):
            return key
    return None


def sheet_listing(dwg):
    """The drawing's sheets in collection order with a 1-based collection index."""
    sheets = safe(lambda: dwg.sheets)
    return [{"collection_index": i + 1, "export_index": None,
             "name": safe(lambda i=i: sheets.item(i).name)}
            for i in range(safe(lambda: sheets.count, 0) or 0)]


def sheet_facts(sheet):
    """One sheet's readable state; width/height are read-only, in SHEET_EXTENT_UNIT. Sheet.tidyUp is
    NOT read here - it is a property whose READ tidies the sheet."""
    size = safe(lambda: sheet.sheetSize)
    orientation = safe(lambda: sheet.orientation)
    return {
        "name": safe(lambda: sheet.name),
        "sheet_size": size_label(size),
        "orientation": orientation_label(orientation),
        "width": _common.measured(lambda: sheet.width, 1.0, 3),
        "height": _common.measured(lambda: sheet.height, 1.0, 3),
        "width_height_unit": SHEET_EXTENT_UNIT,
        "views": _common.counted(lambda: sheet.views.count),
        "sketches": _common.counted(lambda: sheet.sketches.count),
        "custom_tables": _common.counted(lambda: sheet.customTables.count),
    }


def resolve_sheet(dwg, name):
    """(sheet, error_text) for a sheet name, case-insensitive EXACT match; ''/None means the ACTIVE
    sheet and a miss lists the available names. Sheet names are case-insensitively unique (a
    duplicate Sheets.add raises, a duplicate or case-variant rename silently no-ops), so the
    several-match refusal below is an invariant guard, not an expected path."""
    if not name:
        active = safe(lambda: dwg.activeSheet)
        if active is None:
            return None, "No sheet: the drawing reports no active sheet."
        return active, None
    sheets = safe(lambda: dwg.sheets)
    names = []
    hits = []
    for s in _common.iter_collection(sheets):
        n = safe(lambda: s.name) or ""
        names.append(n)
        if n.lower() == str(name).lower():
            hits.append((s, n))
    if not hits:
        return None, ("No sheet named '%s'. Available sheets: %s." % (name, ", ".join(names) or "none"))
    if len(hits) > 1:
        return None, ("Sheet name '%s' matches %d sheets (%s) - address one exactly."
                      % (name, len(hits), ", ".join(n for _, n in hits)))
    return hits[0][0], None
