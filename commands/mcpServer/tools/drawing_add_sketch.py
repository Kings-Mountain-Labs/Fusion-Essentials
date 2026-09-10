# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Add a sketch to a sheet of the active 2D drawing document and draw 2D geometry on it (lines,
rectangles, arcs, circles, ellipses). Drawing-sketch coordinates are in the DRAWING's own length
units - millimetres under ISO, inches under ASME - not the API's centimetres. WRITES the drawing.
"""

import math
from fractions import Fraction

import adsk.core

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import ok, error, safe
from . import _drawing_common
from . import _outputs

# What this tool RETURNS (declared once; drives the PRODUCES: prose + the assert-present contract test).
RETURNS = [
    _outputs.ReturnsName("sketch_name", of="drawing sketch"),
    _outputs.ReturnsValue("curves_landed", "how many curve entities the sketch gained"),
]

# kind -> (the DrawingSketch collection its factory adds to, how many points that factory takes).
# 'line' is the exception: Lines.add takes a LIST of points and draws a connected chain, so N points
# yield N-1 Line entities and its point count is a MINIMUM, not a fixed arity.
_KINDS = {
    "line": ("lines", 2),
    "rectangle": ("rectangles", 2),
    "arc": ("arcs", 3),
    "ellipse": ("ellipses", 3),
    "circle": ("circles", 1),
}

# The curve collections a DrawingSketch carries - the counts the draw is verified against.
_COLLECTIONS = ("lines", "rectangles", "arcs", "circles", "ellipses")

# kind -> what its points MEAN, in the order its factory takes them. Named in the arity refusal, so
# a caller who miscounts learns the form there rather than from the wire description.
_POINT_FORM = {
    "rectangle": "two opposite corners",
    "arc": "start, a point on it, end",
    "ellipse": "center, a major-axis point, a point defining minor-axis distance",
    "circle": "the center, with a numeric 'radius' beside it",
}


def _scalar(raw):
    """A finite numeric scalar as an unrounded float, or None when invalid."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if math.isfinite(value) else None


def _point(raw):
    """One [x, y] pair as finite unrounded floats, or None when invalid."""
    if not isinstance(raw, (list, tuple)) or len(raw) != 2:
        return None
    x, y = _scalar(raw[0]), _scalar(raw[1])
    return (x, y) if x is not None and y is not None else None


def _arc_determinant(points):
    """Exact collinearity determinant built from the normalized float coordinates."""
    (x0, y0), (x1, y1), (x2, y2) = (
        tuple(Fraction.from_float(value) for value in point) for point in points)
    return (x1 - x0) * (y2 - y0) - (y1 - y0) * (x2 - x0)


def _plan(geometry):
    """Validate the WHOLE request before anything is drawn: (entries, expected, error), entries being
    [(kind, points, radius)] in call order and expected the per-collection curve count the sketch
    must gain. A rejected entry part-way through would leave geometry the API cannot delete."""
    if not isinstance(geometry, (list, tuple)) or not geometry:
        return None, None, ("Provide 'geometry' - a non-empty list of entities to draw, each "
                            "{'kind': ..., 'points': [[x, y], ...]}. Kinds: "
                            + ", ".join(sorted(_KINDS)) + ".")
    entries, expected = [], {c: 0 for c in _COLLECTIONS}
    for i, spec in enumerate(geometry):
        if not isinstance(spec, dict):
            return None, None, (f"geometry[{i}] is {spec!r} - each entity is an object with a 'kind' "
                                "and 'points'.")
        kind = str(spec.get("kind") or "").strip().lower()
        if kind not in _KINDS:
            return None, None, (f"geometry[{i}] has kind '{spec.get('kind')}'. Kinds: "
                                + ", ".join(sorted(_KINDS)) + ".")
        collection, need = _KINDS[kind]
        raw_points = spec.get("points")
        if not isinstance(raw_points, (list, tuple)):
            return None, None, (f"geometry[{i}] ('{kind}') needs 'points' - a list of [x, y] pairs. "
                                f"Got {raw_points!r}.")
        points = []
        for j, raw in enumerate(raw_points):
            if not isinstance(raw, (list, tuple)) or len(raw) != 2:
                return None, None, f"geometry[{i}].points[{j}] is not an [x, y] pair: {raw!r}."
            pt = _point(raw)
            if pt is None:
                return None, None, (f"geometry[{i}].points[{j}] must contain finite numeric x and y "
                                    f"values: {raw!r}.")
            points.append(pt)
        if kind == "line":
            if len(points) < need:
                return None, None, (f"geometry[{i}] ('line') needs at least {need} points - a chain of "
                                    f"N points draws N-1 segments. Got {len(points)}.")
            for j in range(1, len(points)):
                if points[j] == points[j - 1]:
                    return None, None, (f"geometry[{i}] ('line') has consecutive duplicate points at "
                                        f"indices {j - 1} and {j}.")
        elif len(points) != need:
            return None, None, (f"geometry[{i}] ('{kind}') needs exactly {need} points "
                                f"({_POINT_FORM[kind]}). Got {len(points)}.")
        if kind == "rectangle":
            if points[0][0] == points[1][0] or points[0][1] == points[1][1]:
                return None, None, (f"geometry[{i}] ('rectangle') needs opposite corners with "
                                    "different x and y coordinates.")
        elif kind == "arc":
            if len(set(points)) != 3:
                return None, None, f"geometry[{i}] ('arc') needs three distinct points."
            if _arc_determinant(points) == 0:
                return None, None, f"geometry[{i}] ('arc') points must not be collinear."
        elif kind == "ellipse":
            if points[0] == points[1] or points[0] == points[2]:
                return None, None, (f"geometry[{i}] ('ellipse') needs the center distinct from both "
                                    "defining points.")
        radius = spec.get("radius")
        if kind == "circle":
            radius = _scalar(radius)
            if radius is None:
                return None, None, (f"geometry[{i}] ('circle') needs a finite numeric 'radius'. "
                                    f"Got {spec.get('radius')!r}.")
            if radius <= 0:
                return None, None, f"geometry[{i}] ('circle') needs a radius greater than 0. Got {radius}."
        expected[collection] += (len(points) - 1) if kind == "line" else 1
        entries.append((kind, points, radius))
    return entries, expected, None


def _p2(xy):
    """A drawing-sketch point: Point2D in the drawing's own length units, never scaled to cm."""
    return adsk.core.Point2D.create(xy[0], xy[1])


def _draw(sketch, kind, points, radius):
    """Call the kind's own 2D factory and hand back what it created."""
    if kind == "line":
        # Lines.add takes a LIST of Point2D and draws a connected chain: N points -> N-1 Lines. It
        # hands back a SINGLE Line whatever the chain length, so the entities that landed are only
        # ever counted off the collection, never off this return.
        return sketch.lines.add([_p2(p) for p in points])
    if kind == "rectangle":
        return sketch.rectangles.addTwoPointRectangle(_p2(points[0]), _p2(points[1]))
    if kind == "arc":
        return sketch.arcs.addByThreePoints(_p2(points[0]), _p2(points[1]), _p2(points[2]))
    if kind == "ellipse":
        return sketch.ellipses.add(_p2(points[0]), _p2(points[1]), _p2(points[2]))
    return sketch.circles.addByCenterRadius(_p2(points[0]), float(radius))


def _counts(sketch):
    """Each curve collection's count as the sketch itself reports it - the only read-back a drawing
    sketch offers (the created entities expose nothing but cast/classType/isValid/objectType)."""
    return {c: (safe(lambda c=c: getattr(sketch, c).count, 0) or 0) for c in _COLLECTIONS}


def handler(geometry=None, sheet_name: str = "", name: str = "") -> dict:
    """See TOOL_DESCRIPTION."""
    entries, expected, plan_error = _plan(geometry)
    if plan_error:
        return error(plan_error)

    dwg = _drawing_common.active_drawing()
    if dwg is None:
        return error("Nothing to draw on: the active document is not a drawing. Open the drawing and "
                     "make it active (doc_open, or the Fusion UI), then retry.")
    sheet, sheet_error = _drawing_common.resolve_sheet(dwg, (sheet_name or "").strip())
    if sheet_error:
        return error(sheet_error)
    on_sheet = safe(lambda: sheet.name)

    sketches = safe(lambda: sheet.sketches)
    if sketches is None:
        return error(f"Sheet '{on_sheet}' exposes no sketches collection - cannot add a sketch to it.")
    wanted = (name or "").strip()
    try:
        # The name argument is optional: with none, Fusion auto-names the sketch ('Sketch1').
        sketch = sketches.add(wanted) if wanted else sketches.add()
    except Exception as ex:
        return error(f"Could not add a sketch to sheet '{on_sheet}': {ex}")
    if sketch is None:
        return error(f"Adding a sketch to sheet '{on_sheet}' returned nothing.")

    before = _counts(sketch)
    drawn, failure = 0, None
    for i, (kind, points, radius) in enumerate(entries):
        try:
            entity = _draw(sketch, kind, points, radius)
        except Exception as ex:
            failure = f"geometry[{i}] ('{kind}') was refused: {ex}"
            break
        if entity is None:
            failure = f"geometry[{i}] ('{kind}') returned no entity."
            break
        drawn += 1

    after = _counts(sketch)
    landed = {c: after[c] - before[c] for c in _COLLECTIONS}
    total_landed = sum(landed.values())
    sketch_name = safe(lambda: sketch.name)
    short = [f"{c} {landed[c]} of {expected[c]}" for c in _COLLECTIONS if landed[c] < expected[c]]

    if failure or short:
        detail = (failure or ("the sketch's own counts came up short: " + "; ".join(short))).rstrip(".")
        kept = "curve" if total_landed == 1 else "curves"
        left = (f"That sketch keeps the {total_landed} {kept} already drawn on it"
                if total_landed else "That empty sketch stays behind")
        return error(f"Drew {drawn} of {len(entries)} entities onto sketch '{sketch_name}' on sheet "
                     f"'{on_sheet}' - {detail}. {left}: Drawing.deleteEntities raises 'API Function "
                     "not yet implemented' on a drawn curve, so delete the whole sketch in the Fusion "
                     "UI if it is not wanted.")

    # The coordinates the factories were handed are in the STANDARD's length unit; sheet_units is
    # the separate dimension display unit and does not move them.
    units = _drawing_common.sheet_units(dwg)
    coordinate_unit = _drawing_common.coordinate_unit(dwg)
    units_said = (coordinate_unit if coordinate_unit
                  else "the standard's own length unit (mm under ISO, in under ASME)")
    return ok({
        "created": True,
        "sketch_name": sketch_name,
        "sheet_name": on_sheet,
        "coordinate_unit": coordinate_unit,
        "sheet_units": units,
        "entities_drawn": len(entries),
        "curves_requested": sum(expected.values()),
        "curves_landed": total_landed,
        "landed": landed,
        "note": (f"Sketch '{sketch_name}' on sheet '{on_sheet}' carries {total_landed} curves, counted "
                 f"off its own collections. Coordinates were taken as {units_said}, which the "
                 "drawing STANDARD fixes - 'sheet_units' is the dimension display unit and does not "
                 "move the geometry. Drawing.deleteEntities raises 'API Function not yet "
                 "implemented' on a drawn curve, so only the whole sketch is deletable, in the "
                 "Fusion UI. drawing_export writes the PDF that shows it."),
    })


TOOL_DESCRIPTION = (
    "Draw 2D geometry on a NEW sketch on a sheet of the active 2D drawing document."
)

FULL_DESCRIPTION = TOOL_DESCRIPTION + "\n" + _outputs.produces_block(RETURNS)

tool = (
    Tool.create_simple(name="drawing_add_sketch", description=FULL_DESCRIPTION)
    .add_input_property("geometry", {
        "type": "array",
        "description": "Entities to draw, in order; 'points' are [x, y] in coordinate_unit - "
                       "mm under ISO, in under ASME.",
        "items": {"type": "object", "properties": {
            "kind": {"type": "string", "enum": sorted(_KINDS)},
            "points": {"type": "array", "items": {"type": "array"}},
            "radius": {"type": "number"}}}})
    .add_required_input("geometry")
    .add_input_property("sheet_name", {"type": "string",
            "description": "Omit for the active sheet."})
    .add_input_property("name", {"type": "string",
            "description": "Omit to let Fusion name it."})
    .strict_schema()
)

item = Item.create_tool_item(
    tool=tool, write="write", handler=handler, run_on_main_thread=True,
    verification=Verification(
        kind="inline", rung="value",
        evidence_test="tests/unit/test_drawing_add_sketch.py::TestHonesty"
                      "::test_an_entity_handed_back_while_the_count_stands_still_is_an_error"))


def register_tool():
    register(item)
