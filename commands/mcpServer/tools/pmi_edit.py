# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""MCP building block that EDITS an existing PMI annotation. WRITES. Every action re-reads the
mutated property and gates its claim on the read-back - a set that did not take is an error."""

import adsk.core
import adsk.fusion

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import ok, error, safe
from . import _common
from . import _inputs
from . import _pmi

app = adsk.core.Application.get()

_ACTION = _inputs.Choice(
    "action", required=True,
    options=["set_text", "rename", "show", "hide", "set_text_point", "set_leader_point",
             "set_plane", "set_alignment", "set_extension", "set_flags", "set_values",
             "set_display", "suppress", "unsuppress", "mark_up_to_date", "convert_imported"],
    description="mark_up_to_date clears the stale flag; convert_imported makes imported PMI "
                "editable.")
_PLANE = _inputs.Choice("plane", options=sorted(_pmi.PLANE_TYPES))
_PLANE_FACE = _inputs.GeometryHandle("plane_face", require="face")
_ALIGN = _inputs.Choice("align", options=sorted(_pmi.H_ALIGN))
_VALIGN = _inputs.Choice("valign", options=sorted(_pmi.V_ALIGN))


def _require_created(ann, what):
    kind = _pmi.kind_of(ann)
    if kind not in _pmi.CREATED_KINDS:
        return (f"'{safe(lambda: ann.name)}' is {kind} - imported PMI is read-only; {what} needs "
                "a Fusion-authored annotation. Try action='convert_imported' first.")
    return None


def _do_set_text(ann, comp, text):
    gerr = _require_created(ann, "set_text")
    if gerr:
        return error(gerr)
    segs, serr = _pmi.build_segments(text)
    if serr:
        return error(serr)
    nerr = _pmi.normalize_extension(ann)
    if nerr:
        return error(nerr)
    try:
        ann.segments = segs
    except Exception as e:
        return error(f"Setting the note text failed: {e}")
    got = _pmi.segments_markup(ann)
    if got is None:
        return error("The text edit did not take (segments unreadable after the set).")
    # build_segments -> segments_markup round-trips exactly (one segment per markup piece,
    # re-encoded by the same table), so a difference is the platform keeping something else.
    if got != text:
        return error(f"The note's segments read back as '{got}', not the requested '{text}' - the "
                     "text edit did not take as asked. The annotation is left carrying what is "
                     "quoted above, not the request.")
    rec = _pmi.annotation_record(comp, ann)
    rec["markup"] = got
    return ok(rec)


def _do_rename(ann, comp, new_name):
    want = (new_name or "").strip()
    if not want:
        return error("action='rename' needs 'new_name'.")
    try:
        ann.name = want
    except Exception as e:
        return error(f"Rename failed: {e}")
    got = safe(lambda: ann.name)
    if got != want:
        return error(f"The rename did not take - the annotation is still named '{got}'.")
    return ok(_pmi.annotation_record(comp, ann))


def _do_visibility(ann, comp, on):
    try:
        ann.isLightBulbOn = on
    except Exception as e:
        return error(f"Visibility toggle failed: {e}")
    got = bool(safe(lambda: ann.isLightBulbOn, not on))
    if got != on:
        return error(f"The visibility set did not take (isLightBulbOn is still {got}).")
    rec = _pmi.annotation_record(comp, ann)
    rec["light_bulb_on"] = got
    if on and not rec.get("visible"):
        rec["note"] = ("Light bulb is on but the PMI is still not visible - a containing "
                       "folder's or the component's PMI light bulb is off.")
    return ok(rec)


def _do_set_text_point(ann, comp, text_point, f):
    gerr = _require_created(ann, "set_text_point")
    if gerr:
        return error(gerr)
    got, err = _pmi.set_text_point(ann, text_point or [], f)
    if err:
        return error(err)
    rec = _pmi.annotation_record(comp, ann)
    rec["text_point"] = _common.ptxyz(got, 1.0 / f)
    return ok(rec)


def _do_set_leader_point(ann, comp, leader_point, f):
    if _pmi.kind_of(ann) != "note":
        return error(f"set_leader_point applies to leader notes only ('{safe(lambda: ann.name)}' "
                     f"is {_pmi.kind_of(ann)} - a hole callout leads to its hole).")
    nerr = _pmi.normalize_extension(ann)
    if nerr:
        return error(nerr)
    got, err = _pmi.set_leader_target(ann, leader_point or [], f)
    if err:
        return error(err)
    rec = _pmi.annotation_record(comp, ann)
    rec["leader_point"] = _common.ptxyz(got, 1.0 / f)
    return ok(rec)


def _do_set_plane(ann, comp, plane, plane_face):
    if _pmi.kind_of(ann) != "note":
        return error("set_plane applies to leader notes only.")
    plane_v, perr = _PLANE.resolve(plane)
    if perr or not plane_v:
        return error(perr or "action='set_plane' needs 'plane'.")
    pface = None
    if plane_face:
        pface, pferr = _PLANE_FACE.resolve(plane_face)
        if pferr:
            return error(pferr)
    nerr = _pmi.normalize_extension(ann)
    if nerr:
        return error(nerr)
    ptype = getattr(adsk.fusion.LeaderLineNotePlaneTypes, _pmi.PLANE_TYPES[plane_v])
    supported = list(safe(lambda: ann.supportedAnnotationPlaneTypes) or [])
    if supported and ptype not in supported:
        names = [_pmi.enum_label(adsk.fusion, "LeaderLineNotePlaneTypes",
                                 "LeaderLineNotePlaneType", v) for v in supported]
        return error(f"plane='{plane_v}' is not supported on this note's geometry. "
                     f"Supported: {', '.join(names)}.")
    try:
        done = (bool(ann.setAnnotationPlane(ptype, pface)) if pface is not None
                else bool(ann.setAnnotationPlane(ptype)))
    except Exception as e:
        return error(f"setAnnotationPlane failed: {e}. plane=face needs plane_face (an ADJACENT "
                     "face); custom_face needs plane_face.")
    if not done or safe(lambda: ann.annotationPlaneType) != ptype:
        return error(f"The plane set did not take (still "
                     f"{safe(lambda: ann.annotationPlaneType)}).")
    rec = _pmi.annotation_record(comp, ann)
    rec["plane"] = plane_v
    return ok(rec)


def _do_set_alignment(ann, comp, align, valign, perpendicular):
    gerr = _require_created(ann, "set_alignment")
    if gerr:
        return error(gerr)
    if not align and not valign and perpendicular is None:
        return error("action='set_alignment' needs align, valign, and/or perpendicular.")
    ferr = _pmi.apply_note_format(ann, align, valign, perpendicular, None)
    if ferr:
        return error(ferr)
    rec = _pmi.annotation_record(comp, ann)
    rec["align"] = _pmi.enum_label(adsk.core, "HorizontalAlignments", "HorizontalAlignment",
                                   safe(lambda: ann.horizontalAlignment))
    rec["valign"] = _pmi.enum_label(adsk.core, "VerticalAlignments", "VerticalAlignment",
                                    safe(lambda: ann.verticalAlignment))
    rec["perpendicular"] = bool(safe(lambda: ann.isPerpendicularLine, False))
    return ok(rec)


def _do_set_extension(ann, comp, leader_extension, f, units):
    gerr = _require_created(ann, "set_extension")
    if gerr:
        return error(gerr)
    if leader_extension is None:
        return error("action='set_extension' needs 'leader_extension' (in 'units').")
    try:
        ext_cm = float(leader_extension) * f
    except Exception:
        return error("'leader_extension' must be a number.")
    ferr = _pmi.apply_note_format(ann, "", "", None, ext_cm, units)
    if ferr:
        return error(ferr)
    got = safe(lambda: ann.leaderLineExtension)
    if got is None or abs(got - ext_cm) > 1e-6:
        return error("The extension set did not take (re-read %s %s)."
                     % (round(got / f, 6) if got is not None else None, units))
    rec = _pmi.annotation_record(comp, ann)
    rec["leader_extension"] = round(got / f, 6)
    return ok(rec)


def _do_set_flags(ann, comp, flags):
    if _pmi.kind_of(ann) != "hole_note":
        return error("set_flags applies to hole/thread callouts only.")
    if not flags:
        return error("action='set_flags' needs a non-empty 'flags' - "
                     "{threaded: true, through: false}.")
    applied, err = _pmi.apply_hole_flags(ann, flags)
    if err:
        return error(err)
    rec = _pmi.annotation_record(comp, ann)
    rec["flags"] = applied
    return ok(rec)


def _do_set_values(ann, comp, values, f):
    if _pmi.kind_of(ann) != "hole_note":
        return error("set_values applies to hole/thread callouts only.")
    if not values:
        return error("action='set_values' needs a non-empty 'values' - {diameter: 6.2} or "
                     "{diameter: {value, tolerance: {type, ...}}}.")
    applied, err = _pmi.apply_hole_values(ann, values, f)
    if err:
        return error(err)
    rec = _pmi.annotation_record(comp, ann)
    rec["values"] = applied
    return ok(rec)


def _do_set_display(ann, comp, display):
    if _pmi.kind_of(ann) != "hole_note":
        return error("set_display applies to hole/thread callouts only.")
    if not isinstance(display, dict) or not display:
        return error("action='set_display' needs 'display' - {precision, units, leading_zeros, "
                     "trailing_zeros, unit_abbreviation, secondary: {...}}.")
    derr = _pmi.apply_display(ann, display)
    if derr:
        return error(derr)
    rec = _pmi.annotation_record(comp, ann)
    rec["display"] = _pmi.display_record(safe(lambda: ann.primaryDisplaySettings))
    if safe(lambda: ann.hasSecondaryDisplaySettings, False):
        rec["display_secondary"] = _pmi.display_record(safe(lambda: ann.secondaryDisplaySettings))
    return ok(rec)


def _do_suppress(ann, comp, on):
    tl = safe(lambda: ann.timelineObject)
    if tl is None:
        return error(f"'{safe(lambda: ann.name)}' has no timeline feature - only parametric PMI "
                     "can be suppressed.")
    try:
        tl.isSuppressed = on
    except Exception as e:
        return error(f"Timeline suppression toggle failed: {e}")
    got = bool(safe(lambda: tl.isSuppressed, not on))
    if got != on:
        return error(f"The suppression set did not take (isSuppressed is still {got}).")
    rec = _pmi.annotation_record(comp, ann)
    rec["suppressed"] = got
    if on:
        rec["note"] = ("A suppressed PMI is expected to drop out of the pmi_get listing entirely "
                       "(unconfirmed on this Fusion build) - pmi_edit(action='unsuppress') "
                       "brings it back by name either way, and verifies it reappeared.")
    return ok(rec)


def _do_unsuppress_by_timeline(d, name, component=""):
    """Unsuppress the timeline feature whose name matches, then verify the annotation reappears in
    the PMI collection; a wrong same-named feature is re-suppressed."""
    # The re-check reads the HIT COUNT, not find_annotation's error text: several hits means the
    # PMI DID come back in more than one component, which must not be re-suppressed.
    want = (name or "").strip().lower()
    hits = [(item, nm) for item, nm in _pmi.suppressed_pmi_features(d) if nm.lower() == want]
    if not hits:
        return None
    if len(hits) > 1:
        return error(f"'{name}' names {len(hits)} suppressed timeline features - rename the "
                     "non-PMI feature or unsuppress it in the timeline first.")
    item, nm = hits[0]
    try:
        item.isSuppressed = False
    except Exception as e:
        return error(f"Timeline unsuppress failed: {e}")
    found, _available = _pmi.annotation_hits(d, nm, component)
    if len(found) > 1:
        where = ", ".join(sorted((safe(lambda c=c: c.name, "") or "?") for _a, c in found))
        return error(f"'{nm}' was unsuppressed and now names a PMI in {len(found)} components "
                     f"({where}) - the annotation IS back and was left unsuppressed. Re-run "
                     "action='unsuppress' with component= to report which one.")
    if not found:
        try:
            item.isSuppressed = True
        except Exception:
            return error(f"Unsuppressed timeline feature '{nm}' is not a PMI annotation, and "
                         "re-suppressing it failed - check the timeline.")
        return error(f"Suppressed feature '{nm}' is not a PMI annotation (no PMI reappeared) - "
                     "it was left suppressed.")
    ann, comp = found[0]
    rec = _pmi.annotation_record(comp, ann)
    rec["suppressed"] = False
    return ok(rec)


def _do_mark_up_to_date(ann, comp):
    if not safe(lambda: ann.isOutOfDate, False):
        rec = _pmi.annotation_record(comp, ann)
        rec["note"] = "Already up to date - nothing to dismiss."
        return ok(rec)
    try:
        dismissed = bool(ann.markUpToDate())
    except Exception as e:
        return error(f"markUpToDate() failed: {e}")
    still_out = bool(safe(lambda: ann.isOutOfDate, True))
    if not dismissed or still_out:
        return error("markUpToDate() declined - the warnings cannot be dismissed without changes; "
                     "the PMI stays out of date. Re-attach or edit the referenced geometry.")
    return ok(_pmi.annotation_record(comp, ann))


def _do_convert_imported(ann, comp):
    kind = _pmi.kind_of(ann)
    if kind in _pmi.CREATED_KINDS:
        return error(f"'{safe(lambda: ann.name)}' is already Fusion-authored ({kind}) - "
                     "nothing to convert.")
    try:
        converted = ann.convertImportedToFusionPMI()
    except Exception as e:
        return error(f"Conversion failed: {e}")
    if converted is None:
        return error(f"Conversion declined - {kind} with this reference geometry is not "
                     "convertible (imported dimensions -> hole notes and imported notes -> "
                     "leader notes are the supported paths). The original PMI is unchanged.")
    rec = _pmi.annotation_record(comp, converted)
    rec["converted_to"] = _pmi.kind_of(converted)
    return ok(rec)


def handler(action=None, annotation="", component="", text="", new_name="", text_point=None,
            leader_point=None, plane="", plane_face="", align="", valign="", perpendicular=None,
            leader_extension=None, flags=None, values=None, display=None, units="mm") -> dict:
    """See TOOL_DESCRIPTION."""
    d = _common.design()
    if not d:
        return error("No active design. Create or open a document first (see doc_new).")
    action_v, aerr = _ACTION.resolve(action)
    if aerr:
        return error(aerr)
    f = _common.scale(units)
    if f is None:
        return error(f"Unknown units '{units}'. Use mm, cm, or in.")
    ann, comp, ferr = _pmi.find_annotation(d, annotation, component)
    if ferr:
        # A suppressed PMI is absent from every collection - unsuppress reaches it through the
        # suppressed timeline features instead of failing the name lookup.
        if action_v == "unsuppress":
            res = _do_unsuppress_by_timeline(d, annotation, component)
            if res is not None:
                return res
        return error(ferr)

    if action_v == "set_text":
        return _do_set_text(ann, comp, text)
    if action_v == "rename":
        return _do_rename(ann, comp, new_name)
    if action_v == "show":
        return _do_visibility(ann, comp, True)
    if action_v == "hide":
        return _do_visibility(ann, comp, False)
    if action_v == "set_text_point":
        return _do_set_text_point(ann, comp, text_point, f)
    if action_v == "set_leader_point":
        return _do_set_leader_point(ann, comp, leader_point, f)
    if action_v == "set_plane":
        return _do_set_plane(ann, comp, plane, plane_face)
    if action_v == "set_alignment":
        return _do_set_alignment(ann, comp, align, valign, perpendicular)
    if action_v == "set_extension":
        return _do_set_extension(ann, comp, leader_extension, f, units)
    if action_v == "set_flags":
        return _do_set_flags(ann, comp, flags)
    if action_v == "set_values":
        return _do_set_values(ann, comp, values, f)
    if action_v == "set_display":
        return _do_set_display(ann, comp, display)
    if action_v == "suppress":
        return _do_suppress(ann, comp, True)
    if action_v == "unsuppress":
        return _do_suppress(ann, comp, False)
    if action_v == "mark_up_to_date":
        return _do_mark_up_to_date(ann, comp)
    return _do_convert_imported(ann, comp)


TOOL_DESCRIPTION = (
"Edit one PMI annotation, named from pmi_get: 'action' picks the edit and the inputs it reads."
)

tool = (
    Tool.create_simple(name="pmi_edit", description=TOOL_DESCRIPTION)
    .add_input_property("action", _ACTION.schema())
    .add_input_property("annotation", {"type": "string",
        "description": "A PMI name from pmi_get."})
    .add_input_property("component", {"type": "string"})
    .add_input_property("text", {"type": "string",
        "description": "{symbol} markup; newlines break lines."})
    .add_input_property("new_name", {"type": "string"})
    .add_input_property("text_point", {
        "type": "array", "items": {"type": "number"},
        "description": "[x,y,z] in 'units'."})
    .add_input_property("leader_point", {
        "type": "array", "items": {"type": "number"},
        "description": "[x,y,z] in 'units'."})
    .add_input_property("plane", _PLANE.schema())
    .add_input_property("plane_face", _PLANE_FACE.schema())
    .add_input_property("align", _ALIGN.schema())
    .add_input_property("valign", _VALIGN.schema())
    .add_input_property("perpendicular", {"type": "boolean",
        "description": "Text perpendicular to the leader line."})
    .add_input_property("leader_extension", {"type": "number",
        "description": "In 'units'."})
    .add_input_property("flags", {"type": "object",
        "description": "hole_note booleans, e.g. {threaded: true}."})
    .add_input_property("values", {"type": "object",
        "description": "hole_note overrides: {diameter: 6.2} or {diameter: {value, tolerance}}."})
    .add_input_property("display", {"type": "object",
        "description": "hole_note number formatting; secondary{} nests the same keys."})
    .add_input_property(*_inputs.UNITS.as_property())
    .add_required_input("action")
    .add_required_input("annotation")
    .strict_schema()
)
item = Item.create_tool_item(
    tool=tool, write="write", handler=handler, run_on_main_thread=True,
    # set_leader_point gates the platform bool and that the point re-reads at all, and set_display /
    # set_values / convert_imported gate their own return and publish the object's read-back; every
    # other action compares its re-read to what it asked for and errors on a mismatch.
    verification=Verification(
        kind="inline", rung="value",
        evidence_test="tests/unit/test_pmi_edit.py::TestSetPlane"
                      "::test_set_plane_gates_on_the_reread_even_when_the_call_returns_true"))


def register_tool():
    register(item)
