# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""MCP building block that CREATES a PMI annotation (a 3D note attached to model geometry). WRITES.
kind='note' is a leader-line note on ONE face/edge/vertex whose text may embed GD&T/modifier
symbols as {symbol} tokens; kind='hole_note' reads its callout (dia/depth/counterbore/thread) off
the hole/boss faces, with optional appended text, value overrides, tolerances, and display
settings. The created annotation is read back - a null add() is an error, never a silent success."""

import adsk.core
import adsk.fusion

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import ok, error, safe
from . import _common
from . import _inputs
from . import _outputs
from . import _pmi

app = adsk.core.Application.get()

RETURNS = [
    _outputs.ReturnsName("annotation", of="PMI annotation", consumers=["pmi_edit", "pmi_delete"]),
]

_KIND = _inputs.Choice("kind", options=["note", "hole_note"], required=True)
_GEOMETRY = _inputs.GeometryHandleList("geometry", require="any", required=True)
_PLANE = _inputs.Choice("plane", options=sorted(_pmi.PLANE_TYPES))
_PLANE_FACE = _inputs.GeometryHandle("plane_face", require="face")
_ALIGN = _inputs.Choice("align", options=sorted(_pmi.H_ALIGN))
_VALIGN = _inputs.Choice("valign", options=sorted(_pmi.V_ALIGN))


def _resolve_note_entity(ents):
    """The single face/edge/vertex a leader note attaches to, or an error naming the problem."""
    if len(ents) != 1:
        return None, (f"kind='note' takes exactly ONE geometry handle (got {len(ents)}). "
                      "A leader note attaches to a single face/edge/vertex.")
    e = ents[0]
    if type(e).__name__ not in ("BRepFace", "BRepEdge", "BRepVertex"):
        return None, (f"kind='note' needs a face/edge/vertex handle, got {type(e).__name__}. "
                      "Use find_geometry(kind=...) for the right one.")
    return e, None


def _set_plane(note_in, plane_v, plane_face):
    """Apply plane= to a leader note input; the setAnnotationPlane bool is gated."""
    ptype = getattr(adsk.fusion.LeaderLineNotePlaneTypes, _pmi.PLANE_TYPES[plane_v])
    try:
        done = (bool(note_in.setAnnotationPlane(ptype, plane_face)) if plane_face is not None
                else bool(note_in.setAnnotationPlane(ptype)))
    except Exception as e:
        return (f"setAnnotationPlane({plane_v}) failed: {e}. plane=face needs plane_face (an "
                "ADJACENT face); custom_face needs plane_face.")
    if not done:
        return (f"setAnnotationPlane({plane_v}) declined - face/custom_face need plane_face "
                "(face: an ADJACENT face), circular_edge/cylinder_axis need matching geometry.")
    return None


def handler(kind=None, geometry=None, text="", name="", text_point=None, leader_point=None,
            plane="", plane_face="", align="", valign="", perpendicular=None,
            leader_extension=None, flags=None, values=None, display=None, units="mm") -> dict:
    """See TOOL_DESCRIPTION."""
    d = _common.design()
    if not d:
        return error("No active design. Create or open a document first (see doc_new).")
    kind_v, kerr = _KIND.resolve(kind)
    if kerr:
        return error(kerr)
    ents, gerr = _GEOMETRY.resolve(geometry)
    if gerr:
        return error(gerr)
    f = _common.scale(units)
    if f is None:
        return error(f"Unknown units '{units}'. Use mm, cm, or in.")
    ext_cm = None
    if leader_extension is not None:
        try:
            ext_cm = float(leader_extension) * f
        except Exception:
            return error("'leader_extension' must be a number (in 'units').")

    if kind_v == "note":
        if flags is not None or values is not None or display is not None:
            return error("'flags'/'values'/'display' apply to kind='hole_note' only.")
        ann, comp, cerr = _create_note(d, ents, text, leader_point, plane, plane_face,
                                       align, valign, perpendicular, ext_cm, f, units)
    else:
        if plane or plane_face or leader_point is not None:
            return error("'plane'/'plane_face'/'leader_point' apply to kind='note' only - a "
                         "hole note derives its plane and leader from the hole faces.")
        ann, comp, cerr = _create_hole_note(d, ents, text, align, valign, perpendicular,
                                            ext_cm, flags, values, display, f, units)
    if cerr:
        return error(cerr)

    rename_warning = None
    if (name or "").strip():
        try:
            ann.name = name.strip()
        except Exception as e:
            rename_warning = f"created, but the rename to '{name}' failed: {e}"
        if safe(lambda: ann.name) != name.strip() and rename_warning is None:
            rename_warning = f"created, but the name did not take (still '{safe(lambda: ann.name)}')."

    if text_point is not None:
        _pt, pt_err = _pmi.set_text_point(ann, text_point, f)
        if pt_err:
            return error(pt_err + " (the annotation WAS created: "
                         f"'{safe(lambda: ann.name)}' - reposition with pmi_edit)")

    rec = _pmi.annotation_record(comp, ann)
    rec["annotation"] = rec.get("name")
    markup = _pmi.segments_markup(ann)
    if markup is not None:
        rec["markup"] = markup
    if rename_warning:
        rec["rename_warning"] = rename_warning
    rec["note"] = "Verify placement visually with view_screenshot; read all PMI with pmi_get."
    return ok(rec)


def _create_note(d, ents, text, leader_point, plane, plane_face, align, valign,
                 perpendicular, ext_cm, f, units):
    """Create a leader-line note from resolved inputs. Returns (annotation, component, error)."""
    ent, eerr = _resolve_note_entity(ents)
    if eerr:
        return None, None, eerr
    segs, serr = _pmi.build_segments(text)
    if serr:
        return None, None, serr
    plane_v, perr = _PLANE.resolve(plane)
    if perr:
        return None, None, perr
    pface = None
    if plane_face:
        pface, pferr = _PLANE_FACE.resolve(plane_face)
        if pferr:
            return None, None, pferr
    comp = safe(lambda: ent.body.parentComponent) or d.rootComponent
    notes = safe(lambda: comp.pmiAnnotations.leaderLineNotes)
    if notes is None:
        return None, None, (f"Component '{safe(lambda: comp.name)}' has no PMI collection - "
                            "this Fusion build may not support PMI authoring.")
    try:
        note_in = notes.createInput(ent)
        # Lift only when the caller named no leader_extension AND the input arrived under the
        # floor (an unreadable extension counts). An explicit value is the caller's, and
        # apply_note_format below refuses it when it is under the floor.
        cur = safe(lambda: note_in.leaderLineExtension)
        if ext_cm is None and (cur is None or cur < _pmi.LEADER_EXT_FLOOR):
            note_in.leaderLineExtension = _pmi.LEADER_EXT_DEFAULT
        if plane_v:
            sperr = _set_plane(note_in, plane_v, pface)
            if sperr:
                return None, None, sperr
        ferr = _pmi.apply_note_format(note_in, align, valign, perpendicular, ext_cm, units)
        if ferr:
            return None, None, ferr
        if leader_point is not None:
            try:
                x, y, z = (float(v) for v in leader_point)
                note_in.annotationTargetPoint = adsk.core.Point3D.create(x * f, y * f, z * f)
            except Exception as e:
                return None, None, (f"'leader_point' rejected: {e}. It must be [x,y,z] ON the "
                                    "annotated geometry.")
        note_in.segments = segs
        ann = notes.add(note_in)
    except Exception as e:
        return None, None, f"Leader note creation failed: {e}"
    if ann is None:
        return None, None, ("The PMI add() returned nothing - no annotation was created. The "
                            "geometry may not support this note kind.")
    return ann, comp, None


def _create_hole_note(d, ents, text, align, valign, perpendicular, ext_cm, flags, values,
                      display, f, units):
    """Create a hole/thread note from resolved inputs. Returns (annotation, component, error)."""
    non_faces = [type(e).__name__ for e in ents if type(e).__name__ != "BRepFace"]
    if non_faces:
        return None, None, (f"kind='hole_note' takes FACE handles only (got {non_faces}). "
                            "Use find_geometry(kind='cylinder_face') on the hole.")
    comp = safe(lambda: ents[0].body.parentComponent) or d.rootComponent
    notes = safe(lambda: comp.pmiAnnotations.holeThreadNotes)
    if notes is None:
        return None, None, (f"Component '{safe(lambda: comp.name)}' has no PMI collection - "
                            "this Fusion build may not support PMI authoring.")
    try:
        note_in = notes.createInput(ents)
        ferr = _pmi.apply_note_format(note_in, align, valign, perpendicular, ext_cm, units)
        if ferr:
            return None, None, ferr
        ann = notes.add(note_in)
    except Exception as e:
        return None, None, (f"Hole/thread note creation failed: {e}. The faces must belong to "
                            "geometric holes (cylinder/counterbore/countersink faces) or "
                            "cylindrical bosses.")
    if ann is None:
        return None, None, ("The PMI add() returned nothing - no annotation was created. The "
                            "geometry may not support a hole/thread callout (hole_note needs a "
                            "recognizable hole or cylindrical boss).")
    # Extras land on the CREATED note (get-modify-set, each verified) - appended text keeps the
    # auto callout tokens; a failure after this point reports the annotation as created.
    post_err = None
    if (text or "").strip() and post_err is None:
        segs, serr = _pmi.build_segments(text)
        if serr:
            post_err = serr
        else:
            try:
                ann.segments = list(ann.segments or []) + segs
            except Exception as e:
                post_err = f"Appending text failed: {e}"
    if flags is not None and post_err is None:
        _applied, post_err = _pmi.apply_hole_flags(ann, flags)
    if values is not None and post_err is None:
        _vals, post_err = _pmi.apply_hole_values(ann, values, f)
    if display is not None and post_err is None:
        post_err = _pmi.apply_display(ann, display)
    if post_err:
        return None, None, (post_err + f" (the annotation WAS created: '{safe(lambda: ann.name)}'"
                            " - adjust with pmi_edit)")
    return ann, comp, None


TOOL_DESCRIPTION = (
"Create a PMI annotation: kind='note' a leader note on ONE face/edge/vertex, kind='hole_note' a "
"callout off the hole/boss faces, 'text' appended.\n"
+ _outputs.produces_block(RETURNS)
)

tool = (
    Tool.create_simple(name="pmi_create", description=TOOL_DESCRIPTION)
    .add_input_property("kind", _KIND.schema())
    .add_input_property("geometry", _GEOMETRY.schema())
    .add_input_property("text", {"type": "string",
        "description": "{symbol} markup ('{flatness}0.05'); newlines break lines."})
    .add_input_property("name", {"type": "string",
        "description": "Default: Fusion's numbering."})
    .add_input_property("text_point", {
        "type": "array", "items": {"type": "number"},
        "description": "[x,y,z] in 'units', projected onto the annotation plane."})
    .add_input_property("leader_point", {
        "type": "array", "items": {"type": "number"},
        "description": "[x,y,z] in 'units'."})
    .add_input_property("plane", _PLANE.schema())
    .add_input_property("plane_face", _PLANE_FACE.schema())
    .add_input_property("align", _ALIGN.schema())
    .add_input_property("valign", _VALIGN.schema())
    .add_input_property("perpendicular", {"type": "boolean"})
    .add_input_property("leader_extension", {"type": "number",
        "description": "In 'units'."})
    .add_input_property("flags", {"type": "object",
        "description": "hole_note booleans, e.g. {threaded: true}."})
    .add_input_property("values", {"type": "object",
        "description": "hole_note overrides: {diameter: 6.2} or {diameter: {value, tolerance}}."})
    .add_input_property("display", {"type": "object",
        "description": "hole_note number formatting; secondary{} nests the same keys."})
    .add_input_property(*_inputs.UNITS.as_property())
    .add_required_input("kind")
    .add_required_input("geometry")
    .strict_schema()
)
item = Item.create_tool_item(
    tool=tool, write="write", handler=handler, run_on_main_thread=True,
    # The payload is annotation_record + segments_markup off the created annotation, a null add() is
    # an error, and a name that did not take is published as the annotation reports it beside a
    # rename_warning. The format knobs additionally gate inline (_pmi.apply_note_format).
    verification=Verification(
        kind="effect", rung="value",
        evidence_test="tests/unit/test_pmi_create.py::TestCreate"
                      "::test_rename_that_does_not_take_is_reported"))


def register_tool():
    register(item)
