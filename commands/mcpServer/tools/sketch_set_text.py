# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""MCP building block: set (or create) sketch-text entities in the active design (e.g. an engraved
label/nameplate). WRITES. The writable handle is SketchText.textParameter - a ModelParameter
whose expression is the QUOTED string.
"""

import math

import adsk.core
import adsk.fusion

app = adsk.core.Application.get()

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import error, ok, safe, scale, all_sketch_names
from . import _common
from . import _inputs
from . import _param_common
# The SketchText readers live in _sketch_detail (the shared sketch X-ray helper) - a tool imports
# from a helper, never the reverse.
from ._sketch_detail import font_read_back as _font_read_back, unquote_text as _unquote
from . import _sketch_detail

_MAX = 500

_PATH_MODES = ("along_path", "fit_on_path")
_MODE = _inputs.Choice("mode", ["multi_line"] + list(_PATH_MODES),
                       description="NEW text: a box at (x,y), or along 'path'.")
_ALIGN = _inputs.Choice("align", ["left", "center", "right"])

_ALIGN_MEMBERS = {"left": "LeftHorizontalAlignment", "center": "CenterHorizontalAlignment",
                  "right": "RightHorizontalAlignment"}

# Where (x,y) sits along the multi_line box: the fraction of the box width to shift the box LEFT so
# the requested x is the text's left edge / center / right edge.
_ALIGN_ANCHOR = {"left": 0.0, "center": 0.5, "right": 1.0}

# Each mode's definition class, as the created text's objectType reports it. 'FitOnPathTextDefintion'
# is the real string - the misspelling is Fusion's. An objectType matching none of these publishes
# mode_verified false rather than failing the create.
_DEFINITION_TOKENS = {"multi_line": "MultiLineTextDefinition",
                      "along_path": "AlongPathTextDefinition",
                      "fit_on_path": "FitOnPathTextDefintion"}

# The placement values each mode's definition object exposes, for read-back instead of an echo.
_DEFINITION_READBACKS = {
    "multi_line": (("align", "horizontalAlignment"), ("character_spacing", "characterSpacing")),
    "along_path": (("above_path", "isAbovePath"), ("align", "horizontalAlignment"),
                   ("character_spacing", "characterSpacing")),
    "fit_on_path": (("above_path", "isAbovePath"),),
}

# Inputs that only shape NEW text; passing one with create=false is REFUSED. 'height' is NOT one of
# them - SketchText.heightParameter takes a write on an existing text and the glyphs follow it - and
# 'units' is exempt because its non-None default cannot be told apart from a supplied value.
_CREATE_ONLY = ("mode", "path", "above_path", "align", "character_spacing", "angle_deg",
                "flip_h", "flip_v", "x", "y")

# The height a create uses when the caller names none, in 'units'. The input itself defaults to
# None so a supplied height is detectable at edit time; the create path applies this instead.
_DEFAULT_HEIGHT = 5.0

# The read half of this tool: a written text is re-readable as its own entity record, so a caller
# verifying a label does not have to fall back to a screenshot.
_READ_BACK_POINTER = (
    " Read it back with sketch_get(sketch_name=..., include_entities=true): the 'text:<i>' entity "
    "carries the string, height, font and sketch-space bounding box.")


def _given(value) -> bool:
    """True if the caller actually supplied this optional input (False/0 count as supplied)."""
    return value is not None and value != ""


def _refuse_create_only(supplied):
    """The error naming create-only inputs passed with create=false, or ''."""
    named = [n for n, v in supplied if _given(v)]
    if not named:
        return ""
    return (", ".join(f"'{n}'" for n in named) + " shape NEW text only, so add create=true - "
            "editing changes the displayed string, its font and its 'height'. To change an "
            "existing text's layout or rotation, create a replacement.")


def _font_failure(exc, font_name, what):
    """The error for a write that failed while a font was being applied, naming that font."""
    # A font name is only checked when the text is WRITTEN: an unknown one is accepted onto the
    # SketchTextInput and raises '3 : invalid input font name' at add(). No API lists or validates
    # font names first, so that raise is the whole check and its sentence is handed on.
    msg = f"Could not {what} with font '{font_name}': {exc}."
    if "font" in str(exc).lower():
        msg += (" Fusion named the font as the problem and no API lists the legal names - pass a "
                "font name that exists on this machine, spelled with its own capitals ('Arial', "
                "not 'arial'), or omit 'font_name' to keep the current font.")
    return msg


def _already_changed(changed):
    """The clause naming the texts an aborted edit had ALREADY updated, so a refusal never reads as
    'nothing happened' when part of the run landed."""
    if not changed:
        return " No sketch text was changed."
    names = list(dict.fromkeys(c["sketch"] for c in changed))
    where = ", ".join(f"'{n}'" for n in names[:5]) + (", ..." if len(names) > 5 else "")
    return f" {len(changed)} sketch text(s) earlier in this call were already updated ({where})."


def _refuse_wrong_mode_inputs(mode, path, above_path, align, character_spacing, x, y):
    """The error naming an input the chosen mode has no API slot for, or ''."""
    if mode == "multi_line":
        for label, value in (("path", path), ("above_path", above_path)):
            if _given(value):
                return (f"'{label}' belongs to mode 'along_path'/'fit_on_path'; mode is "
                        f"'multi_line', which places the text in a box at (x,y). Set mode, or drop "
                        f"'{label}'.")
        return ""
    for label, value in (("x", x), ("y", y)):
        if _given(value):
            return (f"'{label}' belongs to mode 'multi_line'; in mode '{mode}' the path curve "
                    f"places the text. Drop '{label}', or move the curve itself.")
    if mode == "fit_on_path":
        for label, value in (("align", align), ("character_spacing", character_spacing)):
            if _given(value):
                # setAsFitOnPath(path, isAbovePath) takes two arguments - there is no slot for
                # either, and fitting sets the spacing itself from the path length.
                return (f"'{label}' is not accepted in mode 'fit_on_path', which spaces the "
                        f"characters over the whole path by itself. Use mode 'along_path' to "
                        f"control '{label}'.")
    return ""


def _resolve_path_curve(sketch, ref):
    """(curve, error) for the SKETCH CURVE a path-mode text follows."""
    s = (ref or "").strip()
    if not s:
        return None, ("'path' is required in mode 'along_path'/'fit_on_path' - the sketch curve "
                      "the text follows, as '<type>:<index>' from "
                      "sketch_get(include_entities=true).")
    if _inputs.is_handle(s):
        # Measured live: a BRepEdge path makes setAsAlongPath return true and then add() raises
        # '2 : InternalValidationError : pSketchCurve'. The API doc's "SketchCurve or BRepEdge"
        # does not hold at add(), so the edge is refused here instead of at a failed create.
        return None, ("'path' takes a SKETCH CURVE id, not a find_geometry handle: a model EDGE is "
                      "accepted by the placement call and then REJECTED by Fusion when the text is "
                      "added ('InternalValidationError : pSketchCurve'). Project the edge into the "
                      "sketch with sketch_project first, then pass the projected curve's "
                      "'<type>:<index>' id.")
    if s.lower().rpartition(":")[0] == "point":
        return None, (f"'path' = '{s}' is a sketch POINT; the text follows a CURVE. Pass a line, "
                      "arc, circle, ellipse or spline id - a CLOSED circle wraps the text right "
                      "around it.")
    ent = _common.resolve_entity_ref(sketch, s)
    if ent is None:
        return None, (f"'path' did not resolve '{ref}'. Use '<type>:<index>', type = "
                      + "/".join(_common.ENTITY_REF_KINDS)
                      + " - ids come from sketch_get(include_entities=true), and the curve must "
                      "live in the same sketch as the text.")
    return ent, None


def _apply_formatting(ipt, angle_deg, flip_h, flip_v):
    """Set angle/flips on the SketchTextInput, as (requested values, error); the input echoes an
    assigned angle exactly, so set_verified's exact compare holds here."""
    requested = {}
    if angle_deg is not None:
        try:
            deg = float(angle_deg)
        except Exception:
            return requested, ("'angle_deg' must be a number - the text's rotation in DEGREES from "
                               "the sketch x-axis.")
        err = _common.set_verified(ipt, "angle", math.radians(deg), "angle_deg", "SketchTextInput")
        if err:
            return requested, err
        requested["angle_deg"] = deg
    for value, prop, label in ((flip_h, "isHorizontalFlip", "flip_h"),
                               (flip_v, "isVerticalFlip", "flip_v")):
        if value is None:
            continue
        err = _common.set_verified(ipt, prop, bool(value), label, "SketchTextInput")
        if err:
            return requested, err
        requested[label] = bool(value)
    return requested, ""


def _align_key_of(value):
    """The align key naming a HorizontalAlignments member, or None if it names none of them."""
    if value is None:
        return None
    for key, member in _ALIGN_MEMBERS.items():
        if getattr(adsk.core.HorizontalAlignments, member, None) == value:
            return key
    return None


def _definition_facts(st, mode, index, sketch_name):
    """(facts, error) read off the created text's definition - which mode actually landed, and the
    placement values the definition itself reports; a value that will not read is None. `index` and
    `sketch_name` address the text, so a refusal hands over the exact sketch_delete_entity call."""
    definition = safe(lambda: st.definition)
    obj_type = safe(lambda: definition.objectType)
    obj_type = obj_type if isinstance(obj_type, str) and obj_type else None
    placement = {}
    for key, prop in _DEFINITION_READBACKS[mode]:
        value = safe(lambda prop=prop: getattr(definition, prop))
        placement[key] = _align_key_of(value) if key == "align" else value
    facts = {"definition_type": obj_type,
             "mode_verified": bool(obj_type and obj_type.endswith(_DEFINITION_TOKENS[mode])),
             "placement": placement}
    if facts["mode_verified"] or obj_type is None:
        return facts, ""
    landed = [m for m, token in _DEFINITION_TOKENS.items() if obj_type.endswith(token)]
    if landed:
        return facts, (f"Asked for mode '{mode}' but the new text reports a '{landed[0]}' "
                       f"definition ({obj_type}), so it is laid out the wrong way. The text WAS "
                       f"created - remove it with sketch_delete_entity(sketch_name="
                       f"'{sketch_name}', target='text:{index}'), then retry.")
    return facts, ""


def _measured_extents(st, f):
    """(width, height) of a landed SketchText in display units - the x and y extents of its
    boundingBox scaled by `f` - or (None, None) when the box or a corner will not read."""
    bb = safe(lambda: st.boundingBox)
    lo = safe(lambda: bb.minPoint) if bb is not None else None
    hi = safe(lambda: bb.maxPoint) if bb is not None else None
    vals = [safe(lambda o=o, c=c: getattr(o, c)) for o in (lo, hi) for c in ("x", "y")]
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in vals):
        return None, None
    x0, y0, x1, y1 = vals
    return round((x1 - x0) * f, 6), round((y1 - y0) * f, 6)


def _height_read_cm(st):
    """A SketchText's own height in internal cm, or None when no number reads - heightParameter is
    the live read, SketchText.height being retired."""
    v = safe(lambda: st.heightParameter.value)
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    return None


# The band a landed height may differ from the requested one by and still be the same size, in
# internal cm - it absorbs their float representation, not a real size difference.
_HEIGHT_MATCH_TOL_CM = 1e-6

# A box extent that moved by less than this SHARE of its own size did not move; relative, because a
# 20 cm box carries proportionally more read noise than a 0.5 cm one (with a 1 cm floor).
_BOX_MOVED_REL = 1e-6


def _box_moved(before, after):
    """True when a box extent moved beyond the read noise a box that size carries, False when it sat
    still, None when either read is unavailable - so an unreadable box makes NO claim either way."""
    if before is None or after is None:
        return None
    return abs(after - before) > _BOX_MOVED_REL * max(1.0, abs(before))


def _apply_height(st, want_cm, sk_name, k, units):
    """Resize ONE existing sketch text to `want_cm` (internal cm) and prove it landed, as (record,
    error) - the record is the height read back plus the box measured afterwards."""
    # TWO reads gate the write: heightParameter.value must report the requested height, and where
    # that value MOVED the boundingBox must move with it, since the glyph geometry follows the
    # height proportionally. A box that will not read makes no claim.
    before_cm = _height_read_cm(st)
    w0, h0 = _measured_extents(st, 1.0)
    try:
        st.heightParameter.value = want_cm
    except Exception as e:
        return None, f"Could not set the height of sketch text in '{sk_name}': {e}."
    after_cm = _height_read_cm(st)
    if after_cm is None:
        return None, (f"Setting the height of sketch text in '{sk_name}' cannot be confirmed - its "
                      "heightParameter did not read back a number after the write, so whether the "
                      "text resized is not known.")
    if abs(after_cm - want_cm) > _HEIGHT_MATCH_TOL_CM:
        return None, (f"Setting the height of sketch text in '{sk_name}' did not take - its "
                      f"heightParameter reads back {round(after_cm / k, 6)} {units}, not the "
                      f"requested {round(want_cm / k, 6)} {units}.")
    w1, h1 = _measured_extents(st, 1.0)
    verdicts = [_box_moved(w0, w1), _box_moved(h0, h1)]
    if any(v is True for v in verdicts):
        moved = True
    elif all(v is False for v in verdicts):
        moved = False
    else:
        moved = None
    resized = before_cm is not None and abs(after_cm - before_cm) > _HEIGHT_MATCH_TOL_CM
    if resized and moved is False:
        return None, (f"Setting the height of sketch text in '{sk_name}' landed the value but the "
                      f"text did not resize - heightParameter reads {round(after_cm / k, 6)} "
                      f"{units} where it read {round(before_cm / k, 6)} before, while its "
                      f"boundingBox reads {round(w1 / k, 6)} x {round(h1 / k, 6)} {units}, "
                      f"unchanged from the {round(w0 / k, 6)} x {round(h0 / k, 6)} it read before.")
    rec = {"height": round(after_cm / k, 6),
           "height_before": None if before_cm is None else round(before_cm / k, 6)}
    if w1 is not None:
        rec["measured_width"] = round(w1 / k, 6)
        rec["measured_height"] = round(h1 / k, 6)
    return rec, ""


def _create_text(design, text, sketch_name, height, x, y, units, mode, path, above_path,
                 align, character_spacing, angle_deg, flip_h, flip_v, font_name, component=""):
    """Create a new SketchText in the named sketch, in one of the three layout modes. WRITES."""
    if not (sketch_name or "").strip():
        return error("create=true needs 'sketch_name' - the sketch to add the text to (create one "
    "first with sketch_create).")
    k = scale(units)
    if k is None:
        return error(f"Unknown units '{units}'. Use mm, cm, or in.")
    try:
        h = float(height)
    except Exception:
        return error("'height' must be a number (text height in 'units').")
    if h <= 0:
        return error("'height' must be > 0.")

    merr = _refuse_wrong_mode_inputs(mode, path, above_path, align, character_spacing, x, y)
    if merr:
        return error(merr)
    # 'left' when omitted, which is NOT a schema default: align is create-only and mode
    # 'fit_on_path' refuses it, so sending 'left' is not the same call as omitting it.
    align_key, aerr = _ALIGN.resolve(align or "left")
    if aerr:
        return error(aerr)
    halign = getattr(adsk.core.HorizontalAlignments, _ALIGN_MEMBERS[align_key], None)
    if halign is None:
        return error(f"Alignment '{align_key}' is not available on this Fusion version.")
    # No range guard: the legal span of the spacing percentage is not established, so a value
    # Fusion rejects comes back as Fusion's own error rather than an invented bound.
    spacing = 0.0
    if character_spacing is not None:
        try:
            spacing = float(character_spacing)
        except Exception:
            return error("'character_spacing' must be a number - the percent change from the "
                         "default spacing (0 = default, 50 = half again as wide).")

    # Resolve across the whole design, so a sketch in an activated sub-component is a valid target.
    wanted = sketch_name.strip()
    sk, refusal = _sketch_detail.scoped_sketch(design, wanted, component)
    if refusal:
        return error(refusal)
    if not sk:
        names = all_sketch_names(design)
        return error(f"No sketch named '{wanted}'. Available: "
                     + (", ".join(n for n in names if n) or "(none)")
                     + ". Create it first with sketch_create.")

    curve = None
    if mode in _PATH_MODES:
        curve, perr = _resolve_path_curve(sk, path)
        if perr:
            return error(perr)
    above = True if above_path is None else bool(above_path)
    px = 0.0 if x is None else x
    py = 0.0 if y is None else y

    try:
        texts = sk.sketchTexts
        before = safe(lambda: texts.count, 0) or 0
        ipt = texts.createInput2(text, h * k) # text + height (cm)
        if _given(font_name):
            # The input accepts any string; add() below is where Fusion checks the name, so the
            # font is confirmed off the LANDED text. A font set BEFORE the setAs* placement
            # survives it, so the assignment sits here.
            ipt.fontName = font_name
        requested, ferr = _apply_formatting(ipt, angle_deg, flip_h, flip_v)
        if ferr:
            return error(ferr)
        if mode == "along_path":
            placed = ipt.setAsAlongPath(curve, above, halign, spacing)
        elif mode == "fit_on_path":
            placed = ipt.setAsFitOnPath(curve, above)
        else:
            # setAsMultiLine takes SKETCH-plane coordinates, and its corner->diagonal box needs both
            # offsets strictly non-zero. halign aligns the glyphs WITHIN the box and never moves it,
            # so the BOX is anchored per align - centred on x for 'center', ending at x for 'right'.
            width = max(len(text), 1) * h * k
            corner_x = px * k - _ALIGN_ANCHOR[align_key] * width
            placed = ipt.setAsMultiLine(
                adsk.core.Point3D.create(corner_x, py * k, 0),
                adsk.core.Point3D.create(corner_x + width, py * k + h * k, 0),
                halign,
                adsk.core.VerticalAlignments.BottomVerticalAlignment, spacing)
        if not placed:
            return error(f"Fusion refused the '{mode}' text placement (it returned false), so no "
                         "text was placed.")
        st = texts.add(ipt)
    except Exception as e:
        if _given(font_name):
            return error(_font_failure(e, font_name, f"create sketch text in '{sketch_name}'"))
        return error(f"Could not create sketch text in '{sketch_name}': {e}.")
    after = safe(lambda: texts.count, 0) or 0

    # Honesty read-back: trust the sketchTexts COUNT, not add's return value. On an on-face sketch
    # the API can hand back a text object while the collection stays empty (nothing materialized) -
    # that false ok is the cardinal sin, so confirm the count actually rose before claiming success.
    if not st or after <= before:
        tail = ("On a sketch built on a FACE, (x,y) are SKETCH-plane coordinates (not world) - "
                "place the text using the 'frame' from sketch_create (where sketch (0,0) sits and "
                "where +X/+Y point) so it lands on the face."
                if mode == "multi_line" else
                f"The path curve '{path}' resolved, so re-read it with "
                "sketch_get(include_entities=true) and confirm it is the curve you meant.")
        return error(
            f"Sketch text did not materialize in '{safe(lambda: sk.name)}': sketchTexts count stayed "
            f"at {before} after add(). Nothing was created. " + tail)

    # SketchTexts.add appends, so the text just created is at count - 1 (pinned by the TextDel act
    # in tool_verify), and that is the index the refusals below hand to sketch_delete_entity.
    facts, derr = _definition_facts(st, mode, after - 1, sketch_name)
    if derr:
        return error(derr)

    landed_font = _font_read_back(st) if _given(font_name) else None
    if landed_font and landed_font != font_name:
        return error(f"Asked for font '{font_name}' but the new text reports '{landed_font}', so "
                     "the font did not take. The text WAS created - remove it with "
                     f"sketch_delete_entity(sketch_name='{sketch_name}', "
                     f"target='text:{after - 1}'), then retry.")

    if mode == "multi_line":
        note = (f"Sketch text created (verified: sketchTexts {before} -> {after}). (x,y) are "
                "SKETCH-plane coordinates - on an on-face sketch use the 'frame' from sketch_create "
                "to keep the text on the face. Extrude/emboss the sketch to engrave it, or edit it "
                "later with sketch_set_text (without create).")
    else:
        verified = f"sketchTexts {before} -> {after}"
        if facts["definition_type"]:
            verified += f", definition {facts['definition_type']}"
        note = (f"Sketch text created on '{path}' (verified: {verified}). A CLOSED path such as a "
                "circle wraps the text right around it. Extrude/emboss the sketch to engrave it, "
                "or edit the string later with sketch_set_text (without create).")
    note += _READ_BACK_POINTER
    measured_w, measured_h = _measured_extents(st, 1.0 / k)
    if measured_w is not None:
        note += (" measured_width/measured_height are the x and y extents of the created text's own "
                 f"boundingBox, in {units}, read off it after it landed - check them against the "
                 "space the label has to fit before embossing.")
    out = {
    "created": True,
    "sketch": safe(lambda: sk.name),
    "sketch_text_count": after,
    "text": text,
    "height": round(h, 6),
    "mode": mode,
    "definition_type": facts["definition_type"],
    "mode_verified": facts["mode_verified"],
    }
    if measured_w is not None:
        out["measured_width"] = measured_w
        out["measured_height"] = measured_h
    if mode == "multi_line":
        out["position"] = {"x": px, "y": py, "units": units}
    else:
        out["path"] = (path or "").strip()
    # Placement is READ BACK off the definition object, never echoed from the request; one that
    # will not read is published None and its requested value moves into 'requested' instead.
    asked = {"above_path": above, "align": align_key, "character_spacing": spacing}
    for key, value in facts["placement"].items():
        out[key] = value
        if value is None:
            requested[key] = asked[key]
    if _given(font_name):
        out["font"] = landed_font
        if landed_font is None:
            requested["font"] = font_name
    if requested:
        out["requested"] = requested
        note += (" " + ", ".join(f"{n}={v}" for n, v in requested.items()) + " are the REQUESTED "
                 "values - they were not read back off the created text, so confirm them with "
                 "view_screenshot.")
    out["note"] = note
    return ok(out)


def _quote(text):
    """Quote a plain string for a text-parameter expression, escaping any single quotes."""
    return "'" + str(text).replace("'", "\\'") + "'"


# The unit a TEXT parameter reads, and the one param_add takes to make one (measured: a text
# parameter reads 'Text' where a length parameter reads 'mm').
_TEXT_UNIT = "Text"


def _find_text_parameter(design, name):
    """(the user parameter `name`, error) - the string source a BOUND sketch text follows."""
    p = _param_common._find_parameter(design, name)
    if p is None:
        return None, (f"No parameter named '{name}' to bind the text to. param_get lists the "
                      "parameters this design carries.")
    # Fusion ACCEPTS a length parameter in a text parameter's expression and renders its value as
    # the label (measured), so this read is the only thing that refuses one.
    unit = _common.safe(lambda: p.unit)
    if unit != _TEXT_UNIT:
        return None, (f"'{name}' is a {unit or 'unitless'} parameter, so binding the label to it "
                      f"would render a dimension as the string. Bind a {_TEXT_UNIT} parameter "
                      f"(param_add takes unit='{_TEXT_UNIT}').")
    return p, ""


def _bind_parameter(st, name, sk_name):
    """Bind ONE sketch text to a user parameter and read the binding back, as (record, error)."""
    # A text parameter's EXPRESSION carries the BARE parameter name for a binding; the same
    # expression QUOTED is a literal string instead, which is what _quote writes.
    before = safe(lambda st=st: st.textParameter.expression)
    try:
        st.textParameter.expression = name
    except Exception as e:
        return None, f"Could not bind the sketch text in '{sk_name}' to parameter '{name}': {e}."
    landed = safe(lambda st=st: st.textParameter.expression)
    if landed is None:
        return None, (f"Binding the sketch text in '{sk_name}' to '{name}' cannot be confirmed - "
                      "its textParameter did not read back an expression after the write.")
    if str(landed).strip() != name:
        return None, (f"Binding the sketch text in '{sk_name}' to '{name}' did not take - its "
                      f"textParameter reads back '{landed}'.")
    return {"parameter": name, "expression": str(landed).strip(),
            "before": _unquote(before)}, ""


def _texts_in_sketch(sk, comp_name):
    """Yield (component_name, sketch_name, sketch_text) for ONE sketch, ``comp_name`` being the
    owning component's name as its caller read it."""
    # A text's INDEX within its sketch is its address, so this stays a positional walk: a stale text
    # proxy burns its slot (st None) rather than sliding every later text onto the wrong index.
    sk_name = safe(lambda: sk.name) or ""
    texts = safe(lambda: sk.sketchTexts)
    if not texts:
        return
    for j in range(safe(lambda texts=texts: texts.count, 0)):
        yield comp_name, sk_name, safe(lambda texts=texts, j=j: texts.item(j))


def _iter_sketch_texts(design, only_component=None):
    """Yield (component_name, sketch_name, sketch_text) for EVERY sketch in scope - the no-name
    edit, design-wide or narrowed to ``only_component``, the ALREADY-RESOLVED component a
    'component' scope selected. It carries no name filter: a NAMED edit resolves its one sketch
    through _sketch_detail.scoped_sketch and walks that sketch alone."""
    comps = [only_component] if only_component is not None else (
        safe(lambda: design.allComponents, []) or [])
    for comp in comps:
        try:
            sketches = comp.sketches
        except Exception:
            continue
        comp_name = safe(lambda comp=comp: comp.name)
        for sk in _common.iter_collection(sketches):
            yield from _texts_in_sketch(sk, comp_name)


def handler(text: str = None, sketch_name: str = "", index: int = -1,
            create: bool = False, height: float = None, x: float = None, y: float = None,
            units: str = "mm", mode: str = "", path: str = "", above_path: bool = None,
            align: str = "", character_spacing: float = None, angle_deg: float = None,
            flip_h: bool = None, flip_v: bool = None, font_name: str = "",
            component: str = "", parameter: str = "") -> dict:
    """See TOOL_DESCRIPTION."""
    bind_to = (parameter or "").strip()
    if text is None and not bind_to:
        return error("Provide 'text' or 'parameter' - the string to display or the Text parameter to bind.")

    design = _common.design()
    if not design:
        return error("No active design (open a document with sketch text).")

    if bind_to:
        if create:
            return error("'parameter' binds an EXISTING sketch text, so it cannot ride a create. "
                         "Create the text with its starting string first, then call again with "
                         f"parameter='{bind_to}' to bind it.")
        if text:
            return error(f"'text' ('{text}') and parameter='{bind_to}' are two different string "
                         "sources for one text. Pass 'parameter' alone to bind it, or 'text' alone "
                         "to store a literal.")
        _p, perr = _find_text_parameter(design, bind_to)
        if perr:
            return error(perr)

    if create:
        # 'multi_line' when omitted, which is NOT a schema default: mode is create-only and an edit
        # refuses it, so sending 'multi_line' is not the same call as omitting it.
        mode_key, merr = _MODE.resolve(mode or "multi_line")
        if merr:
            return error(merr)
        return _create_text(design, text, sketch_name,
                            _DEFAULT_HEIGHT if height is None else height,
                            x, y, units, mode_key, path,
                            above_path, align, character_spacing, angle_deg, flip_h, flip_v,
                            font_name, component)

    cerr = _refuse_create_only(zip(_CREATE_ONLY, (mode, path, above_path, align,
                                                  character_spacing, angle_deg, flip_h, flip_v,
                                                  x, y)))
    if cerr:
        return error(cerr)

    # 'height' RESIZES on the edit path: heightParameter takes a write and the glyphs follow it
    # (measured). Guarded here, once, before any text is touched - a bad unit or a non-positive
    # height must not land the string on some texts and then refuse.
    k = scale(units)
    if k is None:
        return error(f"Unknown units '{units}'. Use mm, cm, or in.")
    height_cm = None
    if _given(height):
        try:
            h = float(height)
        except Exception:
            return error("'height' must be a number (text height in 'units').")
        if h <= 0:
            return error("'height' must be > 0.")
        height_cm = h * k

    # The name is stripped before it is resolved, so the stripped form is the name that was searched
    # for - the one both the miss and the no-match error quote.
    want = (sketch_name or "").strip()
    scope = (component or "").strip()
    if want:
        # A sketch name is unique only WITHIN a component (Fusion numbers sketches per component
        # from 1), so a NAMED edit acts on ONE sketch and a shared name is REFUSED rather than
        # written into each of them.
        sk, refusal = _sketch_detail.scoped_sketch(design, want, scope)
        if refusal:
            return error(refusal)
        targets = ([] if sk is None
                   else list(_texts_in_sketch(sk, safe(lambda: sk.parentComponent.name))))
    else:
        # No name: every sketch text in the design, or in the scoped component alone.
        scoped_comp = None
        if scope:
            scoped_comp, _occ, scope_error = _sketch_detail.scope_component(design, scope)
            if scope_error:
                return error(scope_error)
        targets = list(_iter_sketch_texts(design, scoped_comp))
    if not targets:
        where = f" inside component '{scope}'" if scope else ""
        if want:
            return error(f"No sketch text found in a sketch named '{want}'{where}. (Use "
    "sketch_get to list sketches; the text must live in a sketch with "
    "that exact name.)")
        return error(f"No sketch text found{where or ' in the active design'}.")

    want_index = int(index) if index is not None else -1
    changed = []
    skipped = 0
    truncated = False
    # Track per-sketch running index so 'index' selects the Nth text within that sketch. Keyed by
    # (component, sketch) - a sketch NAME alone is not unique across components, and a name-keyed
    # counter would interleave two same-named sketches' texts onto wrong indices.
    per_sketch_counter = {}
    for comp_name, sk_name, st in targets:
        if len(changed) >= _MAX:
            truncated = True
            break
        nth = per_sketch_counter.get((comp_name, sk_name), 0)
        per_sketch_counter[(comp_name, sk_name)] = nth + 1
        if want_index >= 0 and nth != want_index:
            skipped += 1
            continue
        if st is None:
            # The slot is burned (the index space must not slide), but the text itself would not
            # read - editing it blind is impossible, and silently skipping a SELECTED index would
            # report success over a hole.
            if want_index >= 0:
                return error(f"Sketch text {nth} in '{sk_name}' could not be read (a stale or "
                             "deleted text proxy holds that index). Re-read the sketch with "
                             "sketch_get(include_entities=true) and retry with a readable index."
                             + _already_changed(changed))
            skipped += 1
            continue
        before = _unquote(safe(lambda st=st: st.textParameter.expression))
        font_now = None
        if _given(font_name):
            # The font goes on FIRST: a name Fusion refuses raises here, leaving this text's string
            # untouched too, so the refusal is reported against unchanged state.
            try:
                st.fontName = font_name
            except Exception as e:
                return error(_font_failure(e, font_name,
                                           f"set the font of sketch text in '{sk_name}'")
                             + _already_changed(changed))
            font_now = _font_read_back(st)
            if font_now and font_now != font_name:
                return error(f"Setting the font of sketch text in '{sk_name}' did not take - "
                             f"SketchText.fontName reads back '{font_now}', not '{font_name}'."
                             + _already_changed(changed))
        if bind_to:
            brec, berr = _bind_parameter(st, bind_to, sk_name)
            if berr:
                return error(berr + _already_changed(changed))
            record = {"component": comp_name, "sketch": sk_name}
            record.update(brec)
            if height_cm is not None:
                hrec, herr = _apply_height(st, height_cm, sk_name, k, units)
                if herr:
                    return error(herr + f" It WAS bound to '{bind_to}' before the resize was "
                                 "checked." + _already_changed(changed))
                record.update(hrec)
            changed.append(record)
            continue
        try:
            st.textParameter.expression = _quote(text)
        except Exception as e:
            msg = f"Failed to set sketch text in sketch '{sk_name}': {e}"
            if _given(font_name):
                # The font already landed on THIS text - a bare 'failed' would read as no effect.
                landed = f"'{font_now}'" if font_now else f"the requested '{font_name}'"
                msg += (f". Its font WAS changed to {landed} before the string failed, so that one "
                        "text now carries the new font with its old string."
                        + _already_changed(changed))
            return error(msg)
        # The expression holds the string QUOTED and keeps an inner quote escaped as written, so a
        # landed write matches either unquoted or as the expression itself.
        expr = safe(lambda st=st: st.textParameter.expression)
        after = _unquote(expr)
        if expr is not None and after != text and expr != _quote(text):
            msg = (f"Setting the string of sketch text in '{sk_name}' did not take - its "
                   f"textParameter reads back '{after}', not '{text}'.")
            if _given(font_name):
                landed = f"'{font_now}'" if font_now else f"the requested '{font_name}'"
                msg += f" Its font WAS changed to {landed} before the string was checked."
            return error(msg + _already_changed(changed))
        record = {"component": comp_name, "sketch": sk_name, "before": before, "after": after}
        if _given(font_name):
            record["font"] = font_now
        if height_cm is not None:
            # The resize goes LAST: the string write moves the glyph box on its own, and the
            # geometry gate below can only attribute a box that moved to the height when the string
            # is already settled.
            hrec, herr = _apply_height(st, height_cm, sk_name, k, units)
            if herr:
                return error(herr + f" Its string WAS set to '{text}' before the resize was "
                             "checked, so that one text carries the new string at its old size."
                             + _already_changed(changed))
            record.update(hrec)
        changed.append(record)

    if not changed:
        return error(f"No sketch text matched index {want_index} in sketch '{want}'.")

    # Changing textParameter.expression updates the sketch, but a feature consuming the text can
    # show STALE geometry until the design recomputes; computeAll makes the visible model match.
    # Only meaningful in parametric mode - direct mode has no tree.
    recomputed = False
    try:
        if safe(lambda: design.designType) == 1:  # ParametricDesignType
            design.computeAll()
            recomputed = True
    except Exception:
        recomputed = False

    out = {
    "set": True,
    "text": text,
    "changed_count": len(changed),
    "changed": changed,
    "truncated": truncated,
    "recomputed": recomputed,
    "note": ("Sketch text updated" + (" and design recomputed so any engraving/emboss that "
                "consumes it rebuilt" if recomputed else "") + "." + _READ_BACK_POINTER),
    }
    if bind_to:
        out["bound_to"] = bind_to
        out.pop("text")
        out["note"] += (f" The string now FOLLOWS parameter '{bind_to}': each entry's 'expression' "
                        "is the binding read back off the text, and param_set on that parameter "
                        "restrings every text bound to it.")
    if _given(font_name):
        out["note"] += (f" Each entry's 'font' is what the text reports after applying "
                        f"'{font_name}'.")
    if height_cm is not None:
        out["note"] += (f" Each entry's 'height' is what the text's heightParameter reports after "
                        f"the resize, in {units}, beside the 'height_before' it read.")
        # The box keys are omitted for a text whose boundingBox would not read, so the sentence that
        # names them is gated on the same read the create path gates its own on.
        if any("measured_width" in c for c in changed):
            out["note"] += (" 'measured_width'/'measured_height' are its boundingBox read back "
                            "afterwards - check them against the space the label has to fit.")
    if truncated:
        out["note"] += f" Hit the {_MAX}-edit cap - not every match was updated; narrow with 'sketch_name'/'index' and call again."
    return ok(out)


TOOL_DESCRIPTION = (
"Set the displayed string of sketch text, or add new text with create=true."
)

tool = (
    Tool.create_simple(
        name="sketch_set_text",
        description=TOOL_DESCRIPTION,
    ).add_input_property("text", {"type": "string",
            "description": "The new string to display; omit when binding 'parameter'."})
    .add_input_property("sketch_name", {"type": "string",
            "description": "Omit this AND 'index' to update EVERY sketch text."})
    .add_input_property(*_sketch_detail.COMPONENT_SCOPE)
    .add_input_property("index", {"type": "integer",
            "description": "0-based within the sketch (default all)."})
    .add_input_property("create", {"type": "boolean"})
    .add_input_property("height", {"type": "number",
            "description": "In 'units'; create defaults to 5, an EDIT RESIZES."})
    .add_input_property("x", {"type": "number", "description": "In 'units'; 'align' says which edge."})
    .add_input_property("y", {"type": "number", "description": "In 'units'; the box BOTTOM."})
    .add_input_property(*_inputs.units_property(description="Scales an EDIT's height too."))
    .add_input_property(*_MODE.as_property())
    .add_input_property("path", {"type": "string"})
    .add_input_property("above_path", {"type": "boolean"})
    .add_input_property(*_ALIGN.as_property())
    .add_input_property("character_spacing", {"type": "number",
            "description": "Percent change from the default."})
    .add_input_property("angle_deg", {"type": "number",
            "description": "Degrees from the sketch x-axis."})
    .add_input_property("flip_h", {"type": "boolean"})
    .add_input_property("flip_v", {"type": "boolean"})
    .add_input_property("parameter", {"type": "string",
            "description": "Bind the string to this user parameter; param_set then restrings it."})
    .add_input_property("font_name", {"type": "string",
            "description": "Omit to keep the current font."})
    .strict_schema()
)

item = Item.create_tool_item(
    tool=tool, write="write", handler=handler, run_on_main_thread=True,
    verification=Verification(
        kind="inline", rung="value",
        evidence_test="tests/unit/test_sketch_set_text.py::TestLandedString"
                      "::test_a_string_that_does_not_land_is_an_error_not_a_false_ok"))


def register_tool():
    register(item)
