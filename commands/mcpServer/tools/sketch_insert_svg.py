# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""MCP building block: import an SVG file from local disk INTO a sketch, offset and scaled.
Sketch.importSVG honours the sketch it is called ON (not the UI-active one), and a path that is not
a file RAISES and poisons the surrounding transaction - so the file is checked before the call.
"""

import os

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item
from ..mcp_primitives.registry import register
from ._common import error, ok, safe
from . import _assert
from . import _common
from . import _inputs
from . import _outputs
from . import _sketch_detail

RETURNS = [
    _outputs.ReturnsValue("curves_added", "the curves the SVG added (proof it landed)"),
    _outputs.ReturnsValue("sketch_extent", "the sketch's measured size after the import"),
]

_X = _inputs.Distance("x", allow_zero=True, default=0.0)
_Y = _inputs.Distance("y", allow_zero=True, default=0.0,
                      description="SVG y runs DOWN from here.")
_UNITS = _inputs.UnitField()

_SPEC = [_X, _Y, _UNITS]


def _extent(sketch, f, unit):
    """The WHOLE sketch's measured size and corners right now, in the caller's units - or None when
    the bounding box will not read. Read off the geometry, never computed from the request:
    importSVG ignores the file's own width/height, so the request cannot predict the result."""
    bb = safe(lambda: sketch.boundingBox)
    lo = safe(lambda: bb.minPoint) if bb is not None else None
    hi = safe(lambda: bb.maxPoint) if bb is not None else None
    if lo is None or hi is None:
        return None
    span = {"width": _common.measured(lambda: hi.x - lo.x, f, 4),
            "height": _common.measured(lambda: hi.y - lo.y, f, 4)}
    if span["width"] is None or span["height"] is None:
        return None
    span["min"] = {"x": _common.measured(lambda: lo.x, f, 4),
                   "y": _common.measured(lambda: lo.y, f, 4)}
    span["max"] = {"x": _common.measured(lambda: hi.x, f, 4),
                   "y": _common.measured(lambda: hi.y, f, 4)}
    span["units"] = unit
    return span


def _resolve_path(file_path):
    """(path, error) for the SVG on this machine's disk."""
    path = (file_path or "").strip().strip('"')
    if not path:
        return None, ("'file_path' is required - the full path to an .svg file on this machine's "
                      "disk.")
    if os.path.splitext(path)[1].lower() != ".svg":
        return None, (f"'{path}' is not an .svg file. This tool imports SVG only; for STEP/IGES/"
                      "SAT/SMT/F3D or DXF use doc_insert_import - which imports svg as well, on "
                      "the same 1/96-inch convention, with no offset or scale to pass.")
    # MANDATORY pre-call check: importSVG on a path that is not a file raises RuntimeError
    # ("3 : invalid argument filename, not found") and that raise rolls back the whole surrounding
    # transaction, so the miss is caught HERE, by name, before Fusion is touched.
    if not safe(lambda: os.path.isfile(path), False):
        return None, (f"No readable file at '{path}'. Pass a full path on THIS machine's disk; a "
                      "cloud file must be downloaded first (data_download_file).")
    return path, None


def handler(file_path: str = "", sketch_name: str = "", x=None, y=None, units: str = "mm",
            scale=None, component: str = "") -> dict:
    """See TOOL_DESCRIPTION."""
    path, perr = _resolve_path(file_path)
    if perr:
        return error(perr)

    vals, verr = _inputs.resolve_inputs(_SPEC, {"x": x, "y": y, "units": units})
    if verr:
        return verr
    unit = (vals["units"] or "mm").strip().lower()
    f = _common.CM_TO_UNIT[unit]          # cm -> display unit, for the position read back out

    # 'scale' is a plain MULTIPLIER on the SVG's own size, never a length - it is the one number
    # here that must not be converted through 'units'.
    factor = 1.0 if scale is None or scale == "" else scale
    try:
        factor = float(factor)
    except (TypeError, ValueError):
        return error(f"'scale' must be a number - a multiplier on the SVG's own size (got "
                     f"'{scale}').")
    if factor <= 0:
        return error(f"'scale' must be greater than 0, got {factor}.")

    design = _common.design()
    if not design:
        return error("No active design. Open or create a document first (see doc_new).")

    sketch, requested, refusal = _sketch_detail.scoped_or_recent_sketch(
        design, sketch_name, component)
    if refusal:
        return error(refusal)
    if sketch is None:
        if requested:
            return error(f"No sketch named '{requested}'. Available: "
                         + (", ".join(n for n in _common.all_sketch_names(design) if n) or "(none)")
                         + ". SVG curves land in an EXISTING sketch - make one with sketch_create.")
        return error("No sketch to import the SVG into. SVG curves land in an EXISTING sketch - "
                     "make one with sketch_create, then name it in 'sketch_name'.")

    name = safe(lambda: sketch.name)
    before = safe(lambda: sketch.sketchCurves.count, 0) or 0
    # sketch.boundingBox covers the WHOLE sketch, so on a sketch that already holds geometry the
    # after-box is not the imported art. Both ends are published and the caller differences them.
    extent_before = _extent(sketch, f, unit)
    try:
        # The curves land in THIS sketch (measured): importSVG acts on the sketch it is called on,
        # not on whichever sketch the Fusion UI has open for edit. x/y are internal cm.
        placed = sketch.importSVG(path, vals["x"], vals["y"], factor)
    except Exception as e:
        return error(f"Could not import '{path}' into sketch '{name}': {e}")
    after = safe(lambda: sketch.sketchCurves.count, 0) or 0

    if not placed:
        return error(f"Fusion refused the SVG import (importSVG returned false); sketch '{name}' "
                     f"holds {after} curve(s), {before} before the call. Check the file opens as "
                     "SVG.")
    # importSVG answers with a bare bool, so the COUNT is the evidence: a true return over an
    # unchanged sketch is a no-op reported as success - the cardinal sin - and is failed here.
    if after <= before:
        return error(f"importSVG returned true but sketch '{name}' gained no curves (still "
                     f"{after}) - nothing was imported. Measured: an empty-but-valid SVG and a "
                     "non-SVG file carrying an .svg name both answer true this way.")

    if before == 0:
        extent_note = ("'sketch_extent' is the imported art's own size - the sketch was empty "
                       "before the import. ")
    else:
        extent_note = (f"'sketch_extent' measures the WHOLE sketch, which already held {before} "
                       "curve(s) before this import - difference it against "
                       "'sketch_extent_before' for the art's own size. ")

    return ok({
        "imported": True,
        "file": path,
        "sketch": name,
        "curves_added": after - before,
        "curve_count_before": before,
        "curve_count_after": after,
        "position": {"x": round(vals["x"] * f, 6), "y": round(vals["y"] * f, 6),
                     "units": unit},
        "scale": factor,
        # The sketch's MEASURED size either side of the import, never an echo of the request:
        # importSVG ignores the file's width/height and viewBox (1 user unit = 1/96 inch x scale),
        # so the request predicts nothing. Both ends ship because the box spans the whole sketch.
        "sketch_extent_before": extent_before,
        "sketch_extent": _extent(sketch, f, unit),
        "note": ("SVG curves APPEND to the sketch: the '<type>:<index>' ids already in use keep "
                 "their entities and the new curves take the ids after them - list them with "
                 "sketch_get(include_entities=true). Each lands as a cv_spline whose control "
                 "points read EMPTY and whose degree raises; sketch_edit_curve(offset) copies "
                 "them as more of the same. " + extent_note
                 + "If the art is the wrong size, change 'scale' and re-import."),
    })


TOOL_DESCRIPTION = (
    "Import an SVG into a sketch at (x,y).\n"
    + _outputs.produces_block(RETURNS)
)

tool = (
    _inputs.apply_to_tool(
        Tool.create_simple(name="sketch_insert_svg", description=TOOL_DESCRIPTION), _SPEC)
    .add_input_property("file_path", {"type": "string"})
    .add_required_input("file_path")
    .add_input_property("sketch_name", {"type": "string"})
    .add_input_property(*_sketch_detail.COMPONENT_SCOPE)
    .add_input_property("scale", {"type": "number",
            "description": "1 SVG unit = 1/96 inch x this. Default 1."})
    .strict_schema()
)

item = Item.create_tool_item(tool=tool, write="write", handler=handler, run_on_main_thread=True,
                             postconditions=[_assert.SketchCurvesChanged(
                                 scope_keys=("component",))])


def register_tool():
    register(item)
