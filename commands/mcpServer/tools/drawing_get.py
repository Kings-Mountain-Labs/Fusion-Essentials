# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Read the ACTIVE 2D drawing document by zoom level - the drawing family's one read tool.
Scripts are a poor fallback here: a sys_execute_script that references a DrawingDocument's
.products collection dies at the executeTextCommand level (measured; app.activeProduct is the
one measured-safe script route in), so this typed read is the reliable read path."""

import adsk.core
import adsk.drawing

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item
from ..mcp_primitives.registry import register
from ._common import error, ok, safe
from . import _common
from . import _drawing_common

app = adsk.core.Application.get()

# ViewTypes value -> wire label. The family's own vocabulary; an unknown/unreadable value
# publishes null rather than a guessed type.
_VIEW_TYPE_MEMBERS = {
    "base": "BaseViewType",
    "projected": "ProjectedViewType",
    "section": "SectionViewType",
    "detail": "DetailViewType",
    "auxiliary": "AuxiliaryViewType",
    "flat_pattern": "FlatPatternViewType",
}


def _view_type_label(value):
    if value is None:
        return None
    for key, member in _VIEW_TYPE_MEMBERS.items():
        if value == _drawing_common.enum_value("ViewTypes", member):
            return key
    return None


def _custom_size_facts(sheet):
    """The sheet's customSize record when the build exposes it - {width, height, unit} in the
    drawing's own coordinate unit - or None. Read defensively: an earlier build RAISED on this
    property (the repo's measured note), the current API doc declares it gettable, so the read
    is trusted only when it answers."""
    cs = safe(lambda: sheet.customSize)
    if cs is None:
        return None
    w = _common.measured(lambda: cs.width, 1.0, 3)
    h = _common.measured(lambda: cs.height, 1.0, 3)
    if w is None or h is None:
        return None
    return {"width": w, "height": h}


def _views_rows(sheet, cap):
    """Per-view rows for one sheet: {index, type}. Type is the only readable fact a drawing View
    carries - it has no name, scale or position, and its populated viewCurves collection hands back
    ViewCurve instances with no readable geometry."""
    views = safe(lambda: sheet.views)
    count = _common.counted(lambda: views.count)
    if count is None:
        return None, False
    rows = []
    for i in range(min(count, cap)):
        v = safe(lambda i=i: views.item(i))
        rows.append({"index": i, "type": _view_type_label(safe(lambda: v.type)) if v else None})
    return rows, count > cap


_MAX_VIEWS_PER_SHEET = 50

_SLICES = ("views",)


def handler(include=None, sheet: str = "") -> dict:
    # include accepts a list or a comma-string, like the family's other rich reads.
    if isinstance(include, str):
        raw = [p.strip().lower() for p in include.split(",") if p.strip()]
    else:
        raw = [str(x).strip().lower() for x in (include or [])]
    bad = [x for x in raw if x not in _SLICES]
    if bad:
        return error(f"Unknown include value(s): {', '.join(bad)}. "
                     f"This read offers: {', '.join(_SLICES)}.")
    want_views = "views" in raw

    dd = _drawing_common.active_drawing_document()
    dwg = safe(lambda: dd.drawing) if dd is not None else None
    if dwg is None:
        return error("The active document is not a 2D drawing. Activate the drawing document "
                     "first (doc_activate), then read it.")

    current_app = adsk.core.Application.get()
    doc_name = safe(lambda: dd.name)
    standard = _drawing_common.standard_label(dwg)
    sheets = safe(lambda: dwg.sheets)
    sheet_count = _common.counted(lambda: sheets.count)
    # ONE activeSheet read for the whole payload: the property can return DIFFERENT sheets across
    # close-together reads, so one read is what makes active_sheet and every is_active flag a
    # consistent snapshot.
    active_name = safe(lambda: dwg.activeSheet.name)
    payload = {
        "drawing": doc_name,
        "standard": standard,
        "dimension_display_unit": _drawing_common.sheet_units(dwg),
        "coordinate_unit": _drawing_common.coordinate_unit(dwg),
        "sheet_count": sheet_count,
        "active_sheet": active_name,
        "is_modified": _common.read_flag(lambda: dd.isModified),
        "active_command_id": safe(lambda: current_app.userInterface.activeCommand),
    }

    if sheet:
        if sheet_count is None:
            return error(f"The drawing's sheet count could not be read, so sheet name '{sheet}' "
                         "cannot be resolved. Retry drawing_get after the drawing finishes updating.")
        target, serr = _drawing_common.resolve_sheet(dwg, sheet)
        if serr:
            return error(serr)
        sheets_to_read = [(None, target)]
    elif sheet_count is None:
        sheets_to_read = None
    else:
        sheets_to_read = [(i, safe(lambda i=i: sheets.item(i))) for i in range(sheet_count)]

    rows = None if sheets_to_read is None else []
    for idx, s in sheets_to_read or []:
        if s is None:
            rows.append(None)
            continue
        facts = _drawing_common.sheet_facts(s)
        if idx is not None:
            facts["collection_index"] = idx + 1
            facts["export_index"] = None
        sheet_name = facts.get("name")
        facts["is_active"] = (None if active_name is None or sheet_name is None
                              else active_name == sheet_name)
        custom = _custom_size_facts(s)
        if custom is not None and facts.get("sheet_size") is None:
            facts["custom_size"] = dict(custom, unit=_drawing_common.coordinate_unit(dwg))
        if want_views:
            vrows, truncated = _views_rows(s, _MAX_VIEWS_PER_SHEET)
            facts["view_rows"] = vrows
            if truncated:
                facts["view_rows_truncated"] = True
        rows.append(facts)
    payload["sheets"] = rows

    payload["note"] = (
        "collection_index is 1-based in the native collection; export_index is unknown. Export all "
        "sheets and inspect the PDF before choosing a page range. Width/height are mm; custom-size "
        "sheets read sheet_size null. include=['views'] adds index/type. viewCurves exposes no readable "
        "geometry. View names, scales, positions and placed dimensions have no read API.")
    return ok(payload)


TOOL_DESCRIPTION = (
    "Read the ACTIVE 2D drawing: standard, units, and a sheet listing with per-sheet facts and a "
    "1-based collection_index (export order unavailable)."
)

tool = (
    Tool.create_simple(name="drawing_get", description=TOOL_DESCRIPTION)
    .add_input_property("include", {"type": "array",
            "items": {"type": "string", "enum": list(_SLICES)},
            "description": "Omit for the orientation read."})
    .add_input_property("sheet", {"type": "string",
            "description": "One sheet by name; omit for all."})
    .strict_schema()
)
item = Item.create_tool_item(tool=tool, write="read", handler=handler, run_on_main_thread=True)


def register_tool():
    register(item)
