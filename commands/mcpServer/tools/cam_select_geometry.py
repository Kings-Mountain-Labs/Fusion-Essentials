# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Set the machining geometry (and optional heights) on a CAM operation.
Two selection mechanisms exist: curve selections, built through a CurveSelections collection, and
direct object-lists, assigned to a parameter's value; heights are a mode+offset parameter group.
A selection on a parameter whose mode decides whether it is READ is engaged in the same call."""

import adsk.cam

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import CM_TO_UNIT, named_with_remainder, ok, error, safe, scale, set_verified
from ._cam_common import (SWARF_CONTOURS_PARAM, enumeration_remedy, expression_error, get_cam,
                          matched_quoting, resolve_cam_node, register_future,
                          strategy_generation_allowed, unquote_expression)
from . import _inputs
from . import _sketch_detail

# The selection kinds. The CURVE (A) family gets one CurveSelections builder each; the DIRECT (B)
# family - the _DIRECT_PARAM keys below, plus surfaces - assigns a CAD-object list instead.
_CHAIN = "chain"
_POCKET = "pocket"
_FACE = "face"
_SILHOUETTE = "silhouette"
_SKETCH = "sketch"
_POCKET_RECOGNITION = "pocket_recognition"
_HOLES = "holes"
_SURFACES = "surfaces"
_GROOVE = "groove"
_THREAD = "thread"
_PROBE = "probe"
_ORIENTATION = "orientation"
_CHAMFER = "chamfer"
_SELECTIONS = (_CHAIN, _POCKET, _FACE, _SILHOUETTE, _SKETCH, _POCKET_RECOGNITION, _HOLES,
               _SURFACES, _GROOVE, _THREAD, _PROBE, _ORIENTATION, _CHAMFER)

# Which operation parameter carries the selection: the first of these the op has, so the ORDER is
# the routing. A deburr op carries edgeSel AND machiningBoundarySel, so the drive param is probed
# first, or the feed lands on the boundary and drives nothing; stockContours is never a drive.
_MACHINING_BOUNDARY_PARAM = "machiningBoundarySel"   # the 3D adaptive/parallel/surfacing boundary
_DEBURR_EDGE_PARAM = "edgeSel"                       # deburr's drive edges
_DRIVE_CURVES_PARAM = "curves"                       # drive curves, where no contours/pockets exist
# turning_trace's own drive input, MEASURED as a CadContours2dParameterValue - the same class as
# 'contours', so the curve builder applies to it unchanged.
_MODEL_CONTOUR_PARAM = "modelContour"
_CURVE_PARAM_CANDIDATES = ("contours", "pockets", SWARF_CONTOURS_PARAM, _DEBURR_EDGE_PARAM,
                           _DRIVE_CURVES_PARAM, _MACHINING_BOUNDARY_PARAM, _MODEL_CONTOUR_PARAM,
                           "stockContours")

# A selection on the 3D machining boundary is INERT while boundaryMode holds its default
# 'silhouette' - the op machines the silhouette surface set instead. boundaryMode is a CAM STRING
# parameter taking 'silhouette' or 'selection'; 'contours' is refused as an invalid enumeration.
_BOUNDARY_MODE_PARAM = "boundaryMode"
_BOUNDARY_MODE_SELECTION = "selection"

# A swarf op's rails are inert the same way: a fresh swarf op reads swarfSelectionMode 'surfaces'
# and reports 'Surfaces: No valid geometry selected.' at generate until that mode reads 'contours'.
# Also a CAM string parameter, so the same quoted codec.
_SWARF_MODE_PARAM = "swarfSelectionMode"
_SWARF_MODE_CONTOURS = "contours"

# three_plus_two reads the orientation faces only while toolAxisMode is 'manual' - the spelling
# Fusion reports as Primary mode 'Selection'. A fresh op already reads 'manual' (measured), so this
# entry confirms the mode rather than assuming it; flat's copy reads isEditable false.
_TOOL_AXIS_MODE_PARAM = "toolAxisMode"
_TOOL_AXIS_SELECTION = "manual"

# The select-and-engage table: selection param -> (mode param, value, the noun the errors use,
# payload key prefix). A selection landing on one of these is applied AND its mode engaged in the
# same call, whichever family the parameter belongs to.
_ENGAGE_MODE = {
    _MACHINING_BOUNDARY_PARAM: (_BOUNDARY_MODE_PARAM, _BOUNDARY_MODE_SELECTION,
                                "machining boundary", "boundary"),
    SWARF_CONTOURS_PARAM: (_SWARF_MODE_PARAM, _SWARF_MODE_CONTOURS,
                           "swarf rail pair", "swarf"),
    "machiningDirections": (_TOOL_AXIS_MODE_PARAM, _TOOL_AXIS_SELECTION,
                            "tool-axis orientations", "tool_axis"),
}

# swarfContours is a RAIL PAIR, not one contour: two rails fed to a single CurveSelection are walked
# into ONE path, so each passed reference seeds its own selection here, and a rail defaults to OPEN
# because closed rails were refused where the same pair open was not.
_RAIL_PAIR_PARAMS = (SWARF_CONTOURS_PARAM,)
_RAILS_REQUIRED = 2
_RAILS_ORDER = "as passed - the LOWER rail must be first"

# Parameters whose references are read as SEPARATE curves, so each seeds its own CurveSelection.
# MEASURED on morph: two rim circles fed to ONE selection walk into a single path and the operation
# reports 'No passes to link'; the same pair as one selection EACH cut 77.7 s of toolpath.
_PER_REFERENCE_PARAMS = (SWARF_CONTOURS_PARAM, _DRIVE_CURVES_PARAM)

# The DIRECT (B) family: per selection kind, the parameter name(s) whose CadObjectParameterValue
# takes a CAD-object list on .value, probed in order. 'holes' carries two spellings - DRILL
# 'holeFaces', BORE/CIRCULAR 'circularFaces'.
_DIRECT_PARAM = {_HOLES: ("holeFaces", "circularFaces"), _GROOVE: ("grooves",),
                 _THREAD: ("threadFaces",), _PROBE: ("probe_selection",),
                 _ORIENTATION: ("machiningDirections",), _CHAMFER: ("chamfers",)}

# What each direct kind is for - the refusal an operation carrying none of its parameters gets.
_DIRECT_MISS = {_HOLES: "drilling/boring strategies (drill / bore / circular / tap / ...)",
                _GROOVE: "a turning groove strategy",
                _THREAD: "a turning thread strategy",
                _PROBE: "the probe and probe_geometry strategies",
                _ORIENTATION: "a strategy that takes tool-axis orientations, e.g. three_plus_two",
                _CHAMFER: "the turning_chamfer strategy"}

# 'surfaces' = the same shape, one parameter per ROLE, and an op can carry several at once - so the
# role is an input, not a probe order. The op's 'model' parameter is the same class but is NOT one
# of these: assigning a face list to it raises 'Parameter is not available through the API.'
_SURFACE_TARGET_PARAM = {"drive": "driveSurfaces", "floor": "floorSurfaces",
                         "wall": "wallSurfaces", "ceiling": "ceilingSurfaces",
                         "check": "checkSurfaceSelection", "swarf": "advancedSwarfSurfaces"}
_DEFAULT_SURFACE_TARGET = "drive"

_CURVE_BUILDER = {
    _CHAIN: "createNewChainSelection",
    _POCKET: "createNewPocketSelection",
    _FACE: "createNewFaceContourSelection",
    _SILHOUETTE: "createNewSilhouetteSelection",
    _SKETCH: "createNewSketchSelection",
    _POCKET_RECOGNITION: "createNewPocketRecognitionSelection",
}

# Each selection takes ONE object type on its input: ChainSelection B-Rep edges, FaceContour/Pocket
# a BRepFace, Silhouette/PocketRecognition a BRepBody, SketchSelection ENTIRE sketches; 'grooves'
# and 'chamfers' a bounding EDGE (a face raises InternalValidationError), threadFaces a face.
_GEOMETRY_INPUT = {_CHAIN: "handles", _POCKET: "handles", _FACE: "handles", _HOLES: "handles",
                   _SURFACES: "handles", _GROOVE: "handles", _THREAD: "handles",
                   _PROBE: "handles", _ORIENTATION: "handles", _CHAMFER: "handles",
                   _SILHOUETTE: "bodies", _POCKET_RECOGNITION: "bodies", _SKETCH: "sketches"}
_HANDLE_REQUIRE = {_CHAIN: "edge", _POCKET: "face", _FACE: "face", _HOLES: "face",
                   _SURFACES: "face", _GROOVE: "edge", _THREAD: "face", _PROBE: "face",
                   _ORIENTATION: "face", _CHAMFER: "edge"}
_BODY_SELECTIONS = (_SILHOUETTE, _POCKET_RECOGNITION)
# loopType/sideType exist on FaceContourSelection, SilhouetteSelection and SketchSelection only.
_LOOP_SIDE_SELECTIONS = (_FACE, _SILHOUETTE, _SKETCH)

_LOOP_TYPE = {"all": "AllLoops", "outside": "OnlyOutsideLoops", "inside": "OnlyInsideLoops"}
_SIDE_TYPE = {"always_outside": "AlwaysOutsideSideType", "always_inside": "AlwaysInsideSideType",
              "start_outside": "StartOutsideSideType", "start_inside": "StartInsideSideType"}

LOOP_TYPE = _inputs.Choice("loop_type", list(_LOOP_TYPE))
SIDE_TYPE = _inputs.Choice("side_type", list(_SIDE_TYPE))
SURFACE_TARGET = _inputs.Choice("surface_target", list(_SURFACE_TARGET_PARAM),
                                description="Defaults to drive.")

# pocket_recognition search criteria -> the PocketRecognitionSelection property each sets.
_POCKET_FILTER_LENGTHS = (("min_hole_diameter", "minimumHoleDiameter"),
                          ("min_corner_radius", "minimumCornerRadius"),
                          ("max_corner_radius", "maximumCornerRadius"),
                          ("min_depth", "minimumPocketDepth"),
                          ("max_depth", "maximumPocketDepth"))
_POCKET_FILTER_KEYS = ("holes",) + tuple(k for k, _ in _POCKET_FILTER_LENGTHS)

# The wire schema for those keys, built from the same tables _apply_pocket_filter reads - a key it
# does not read cannot reach the schema, and every length is stated in the call's own 'units'.
_POCKET_FILTER_PROPERTIES = dict(
    [("holes", {"type": "boolean", "description": "count holes as pockets"})]
    + [(key, {"type": "number"}) for key, _prop in _POCKET_FILTER_LENGTHS])
# The gate _apply_pocket_filter enforces, stated on the key it constrains rather than in prose.
_POCKET_FILTER_PROPERTIES["min_hole_diameter"]["description"] = "needs holes=true"

# Which selection kind each optional knob belongs to - the property simply does not exist on the
# other classes, so passing one is a caller error, not something to drop silently.
_KNOB_SELECTIONS = {"is_open": (_CHAIN,), "reverted": (_CHAIN,),
                    "loop_type": _LOOP_SIDE_SELECTIONS, "side_type": _LOOP_SIDE_SELECTIONS,
                    "pocket_filter": (_POCKET_RECOGNITION,),
                    "min_diameter": (_HOLES,), "max_diameter": (_HOLES,),
                    "surface_target": (_SURFACES,),
                    # 'component' SCOPES the by-name geometry input a kind reads; on a handle-driven
                    # kind it narrows nothing, so it is refused there like any absent property.
                    "component": (_SKETCH,) + _BODY_SELECTIONS}


# The geometry inputs the body/sketch kinds resolve through, one instance each for the schema and
# the resolver. Both take scope_input because Fusion's own defaults make those names shared - it
# numbers sketches per component from 1 and names every component's first body 'Body1'.
BODIES = _inputs.BodyRefList("bodies", scope_input="component")
SKETCHES = _inputs.SketchRefList("sketches", scope_input="component", required=True)


# ── seams (patched in tests) ─────────────────────────────────────────────────

def _curve_param(op):
    """(name, param) for the op's curve-selection parameter, or (None, None) - the NAME is what the
    mode-engage table below keys on."""
    for nm in _CURVE_PARAM_CANDIDATES:
        p = safe(lambda nm=nm: op.parameters.itemByName(nm))
        if p is not None:
            return nm, p
    return None, None


def _launch_generation(cam, op, op_name):
    """(handle, None) or (None, err) - launch toolpath generation and return IMMEDIATELY, the Future
    registered through register_future so Fusion does not abandon it."""
    try:
        fut = cam.generateToolpath(op)
    except Exception as e:
        return None, str(e)
    if not fut:
        return None, "generateToolpath returned no future."
    handle, _total = register_future(fut, f"operation '{op_name}'", "operation", False,
                                     target_name=op_name)
    return handle, None


# The note for a SKIPPED inline generate. The selection itself landed, so this is not a refusal -
# only the launch is withheld, on the same flag cam_generate excludes an operation from a sweep on.
_BLOCKED_GENERATE = (
    "Selection applied; generation was NOT launched - strategy '{strategy}' reads "
    "isGenerationAllowed false. Check the Machining Extension entitlement, or replace the "
    "operation: cam_delete, then cam_create_operation with a strategy "
    "cam_get(include=['strategies']) reads as allowed.")

# An unreadable flag is not a blocked strategy, so no refusal is fabricated from it.
_ENTITLEMENT_UNREAD = " isGenerationAllowed did not read - no entitlement pre-flight."

# The read that shows a parameter's own expression - what an enumeration refusal sends the caller to.
_PARAM_READ = "cam_get(include=['parameters'], operation=<name>)"


# ── input guards ─────────────────────────────────────────────────────────────

def _knob_guard(selection, knobs):
    """The error for an option passed to a selection kind it does not apply to - a property the
    kind's own class does not carry, or the component scope on a kind that reads no NAME - or None.
    Silently dropping it would leave the caller believing an option applied that never did."""
    for key in sorted(knobs):
        if knobs[key] is None:
            continue
        kinds = _KNOB_SELECTIONS[key]
        if selection not in kinds:
            return (f"'{key}' does not apply to the '{selection}' selection - it is a "
                    f"{'/'.join(kinds)} option. Drop it, or change 'selection'.")
    return None


def _resolve_geometry(selection, handles, bodies, sketches, component):
    """(entities, error) - the live objects this selection kind's inputGeometry takes, resolved
    through the input the kind uses. A body kind with no bodies resolves to an empty list: that is
    the setup-models form, switched on by isSetupModelSelected. `component` narrows EVERY name in
    whichever by-name list the kind reads - the sketches, or the bodies - to that one component."""
    want = _GEOMETRY_INPUT[selection]
    for name, raw in (("handles", handles), ("bodies", bodies), ("sketches", sketches)):
        if name != want and raw not in (None, "", []):
            return None, (f"the '{selection}' selection takes its geometry from '{want}', not "
                          f"'{name}'. Move the values to '{want}', or change 'selection'.")
    if want == "bodies":
        return BODIES.resolve(bodies, component)
    if want == "sketches":
        return SKETCHES.resolve(sketches, component)
    return _inputs.GeometryHandleList("handles", require=_HANDLE_REQUIRE[selection],
                                      required=True).resolve(handles)


# ── selection appliers ───────────────────────────────────────────────────────

def _set_knob(sel, prop, value, label, inv=1.0, units=""):
    """(read_back, '') or (None, error) - set ONE property on a curve selection and confirm it took,
    a number within 1e-9 and everything else through set_verified. Lengths are written and returned
    in Fusion's internal cm; only the ERROR text is scaled by inv and labelled with units."""
    if isinstance(value, float):
        try:
            setattr(sel, prop, value)          # MUTATION
        except Exception as e:
            return None, f"Could not set {label}: {e}"
        back = safe(lambda: getattr(sel, prop))
        if back is None or abs(float(back) - value) > 1e-9:
            shown = "None" if back is None else f"{round(float(back) * inv, 6)}{units}"
            return None, (f"Setting {label} did not take - the selection reads back {shown}, so the "
                          "operation would run on its default criteria.")
        return back, ""
    err = set_verified(sel, prop, value, label, "the selection")
    if err:
        return None, err
    return safe(lambda: getattr(sel, prop)), ""


def _apply_pocket_filter(sel, flt, factor, units, extra):
    """Set the pocket-recognition search criteria, publishing each read-back through the CALLER'S
    units beside the units it is stated in; returns an error string, or None."""
    if not flt:
        return None
    inv = CM_TO_UNIT[units]                # cm -> the caller's units, for the published read-back
    if not isinstance(flt, dict):
        return f"'pocket_filter' must be an object with the keys {', '.join(_POCKET_FILTER_KEYS)}."
    unknown = sorted(k for k in flt if k not in _POCKET_FILTER_KEYS)
    if unknown:
        return (f"'pocket_filter' has no key(s) {', '.join(unknown)} - it takes "
                f"{', '.join(_POCKET_FILTER_KEYS)}.")
    holes = flt.get("holes")
    if flt.get("min_hole_diameter") is not None and not holes:
        return ("'pocket_filter.min_hole_diameter' needs holes=true - the API accepts the hole "
                "diameter bound only while holes are being interpreted as pockets.")
    applied = {}
    lengths = False
    # areHolesIncluded GATES minimumHoleDiameter, so it is set first.
    if holes is not None:
        back, err = _set_knob(sel, "areHolesIncluded", bool(holes), "pocket_filter.holes")
        if err:
            return err
        applied["holes"] = bool(back)
    for key, prop in _POCKET_FILTER_LENGTHS:
        v = flt.get(key)
        if v is None:
            continue
        try:
            scaled = float(v) * factor
        except (TypeError, ValueError):
            return f"'pocket_filter.{key}' must be a number; got '{v}'."
        back, err = _set_knob(sel, prop, scaled, f"pocket_filter.{key}", inv, f" {units}")
        if err:
            return err
        applied[key] = round(float(back) * inv, 6)
        lengths = True
    if applied:
        extra["pocket_filter_applied"] = applied
        if lengths:
            extra["pocket_filter_units"] = units    # the units every length above is stated in
    return None


def _apply_knobs(sel, selection, entities, knobs, factor, units, extra):
    """Set the per-selection properties this kind carries, each confirmed by a read-back. Returns an
    error string, or None."""
    if selection == _CHAIN:
        for key, prop in (("is_open", "isOpen"), ("reverted", "isReverted")):
            if knobs.get(key) is not None:
                _back, err = _set_knob(sel, prop, bool(knobs[key]), key)
                if err:
                    return err
    if selection in _BODY_SELECTIONS:
        # With no bodies of its own the selection runs against the bodies set as the setup's models -
        # a named flag, set explicitly rather than left to a default.
        back, err = _set_knob(sel, "isSetupModelSelected", not entities, "isSetupModelSelected")
        if err:
            return err
        extra["setup_models_selected"] = bool(back)
    if selection in _LOOP_SIDE_SELECTIONS:
        for key, prop, table, enum in (("loop_type", "loopType", _LOOP_TYPE, adsk.cam.LoopTypes),
                                       ("side_type", "sideType", _SIDE_TYPE, adsk.cam.SideTypes)):
            v = knobs.get(key)
            if v is not None:
                _back, err = _set_knob(sel, prop, getattr(enum, table[v], None), key)
                if err:
                    return err
                extra[key] = v
    if selection == _POCKET_RECOGNITION:
        return _apply_pocket_filter(sel, knobs.get("pocket_filter"), factor, units, extra)
    return None


def _read_back(cs, selection):
    """(record, fusion_error_or_None) - what the operation holds AFTER applyCurveSelections: the
    collection count, what Fusion RESOLVED off it, and the error/warning channel, summed and read
    over EVERY selection in the collection since a rail-pair feed applies more than one."""
    count = (safe(lambda: cs.count, 0) or 0) if cs is not None else 0
    record = {"selections": count}
    sels = [s for s in (safe(lambda i=i: cs.item(i)) for i in range(count)) if s is not None]
    if not sels:
        return record, None
    resolved = {}
    for sel in sels:
        # outputGeometry is a Curve3DPathVector and value a BaseVector: plain iterables carrying
        # neither count nor item, so an iter_collection walk over them would publish a fabricated 0.
        paths = safe(lambda sel=sel: list(sel.outputGeometry))
        if paths is not None:
            resolved["curve_paths"] = resolved.get("curve_paths", 0) + len(paths)
            resolved["curve_segments"] = (resolved.get("curve_segments", 0)
                                          + sum((safe(lambda p=p: p.count, 0) or 0) for p in paths))
        value = safe(lambda sel=sel: list(sel.value))
        if value is not None:
            resolved["entities"] = resolved.get("entities", 0) + len(value)
    if resolved:
        record["resolved"] = resolved
    warnings = [w for w in ((safe(lambda sel=sel: sel.warning) or "").strip() for sel in sels
                            if safe(lambda sel=sel: sel.hasWarning, False)) if w]
    if warnings:
        record["selection_warning"] = named_with_remainder(warnings)
    for sel in sels:
        if not safe(lambda sel=sel: sel.hasError, False):
            continue
        reason = (safe(lambda sel=sel: sel.error) or "").strip()
        # The collection was CLEARED before these selections were built, so the operation is not back
        # on what it held before the call - the caller has to re-select, not just retry differently.
        return record, (f"Fusion rejected the {selection} selection: "
                        f"{reason or 'the selection reports an error with no message'}. The "
                        "operation's previous selection was cleared before this one was applied, so "
                        "it now holds only the rejected selection - select its geometry again.")
    return record, None


def _selected_labels(selection, entities):
    """What the BY-NAME geometry references resolved to, one label per entity, each in the spelling
    that ADDRESSES it back - or [] for a handle-driven kind, whose entities carry no name."""
    if selection == _SKETCH:
        return [f"'{safe(lambda s=s: s.name) or '?'}' in "
                f"{safe(lambda s=s: s.parentComponent.name) or '?'}" for s in entities]
    if selection in _BODY_SELECTIONS:
        return [f"'{_inputs.qualified_body_name(b)}'" for b in entities]
    return []


def _engage_mode(op, mode_param, want, subject):
    """(mode_read_back, whether this call added the quotes, None) or (None, False, error) - set the
    mode that decides whether the operation READS the selection just applied, so it is not left
    inert."""
    p = safe(lambda: op.parameters.itemByName(mode_param))
    if p is None:
        return None, False, (
            f"The selection landed on the operation's {subject}, but the operation has no "
            f"'{mode_param}' parameter, so this call cannot confirm the {subject} is "
            f"engaged - set {mode_param} in the Fusion UI, or re-check that the strategy is "
            "the one you meant.")
    before = safe(lambda: p.expression)
    # A mode parameter already holding a QUOTED expression stores a string, and Fusion refuses the
    # bare spelling - so the request is wrapped to match, through the ONE shared decision.
    written, quoted = matched_quoting(before, want)
    try:
        p.expression = written                         # MUTATION
    except Exception as e:
        return None, False, (
            f"Could not set {mode_param}='{want}' to engage the {subject}: {e}. The "
            f"selection is NOT confirmed engaged - set {mode_param}='{want}' with "
            "cam_edit_operation, then regenerate."
            + enumeration_remedy(str(e), written, _PARAM_READ, p))
    after = safe(lambda: p.expression)
    if after is None:
        return None, False, (
            f"{mode_param} cannot be read back after being set to '{want}', so the "
            f"{subject} engagement is UNCONFIRMED - re-read the operation with "
            "cam_get(include=['operations']).")
    eval_err, _warn = expression_error(p)
    if eval_err:
        return None, False, (
            f"{mode_param} was set to '{want}' and reads back '{after}', but the parameter "
            f"reports '{eval_err}' - the {subject} is not engaged.")
    if unquote_expression(after) != want:
        shown = unquote_expression(after)
        return None, False, (
            f"Setting {mode_param}='{want}' did not take - it reads back '{shown}' (it held "
            f"'{unquote_expression(before)}'), so the selected {subject} is NOT engaged - "
            f"set {mode_param}='{want}' with cam_edit_operation, then regenerate.")
    return unquote_expression(after), quoted, None


def _rail_groups(name, entities, knobs):
    """(one entity list per CurveSelection to build, the knobs each is built with) - one selection
    over all entities, or one PER entity where the parameter reads its references as separate
    curves; a rail pair is that shape plus an is_open that defaults True."""
    if name not in _PER_REFERENCE_PARAMS:
        return [list(entities)], knobs
    if name not in _RAIL_PAIR_PARAMS:
        return [[e] for e in entities], knobs
    rail_knobs = dict(knobs)
    if rail_knobs.get("is_open") is None:
        rail_knobs["is_open"] = True
    return [[e] for e in entities], rail_knobs


def _apply_curve(op, selection, entities, knobs, factor, units, extra, explicit_groups=None):
    """(record, None) or (None, error) - build the CurveSelection(s) of this kind from `entities`,
    apply them, read them back, and engage the drive parameter's mode in the same call."""
    name, p = _curve_param(op)
    if p is None:
        # An op driven by an OBJECT set carries no curve parameter at all, so the refusal hands over
        # EVERY settable set this one carries - an op with both would otherwise hide one remedy.
        direct, direct_blocked = _direct_params(op)
        carried, blocked = _surface_params(op)
        tails = []
        if direct:
            listed = named_with_remainder([f"'{nm}' (selection='{kind}')" for kind, nm in direct])
            tails.append(f" It carries the selection parameter(s) {listed}.")
        if carried:
            listed = named_with_remainder([k for k in _SURFACE_TARGET_PARAM if k in carried])
            tails.append(f" It carries the surface set(s) {listed} - pass selection='surfaces' with "
                         "surface_target and face handles.")
        if not tails and (blocked or direct_blocked):
            tails.append(f" Its set(s) {named_with_remainder(blocked + direct_blocked)} did not "
                         "read isEditable true, so no selection kind is offered for them.")
        if not tails:
            tails.append(" It carries none of the selection parameters this call routes either - "
                         f"read what it does carry with {_PARAM_READ}.")
        return None, (f"Operation '{safe(lambda: op.name)}' has no curve-selection parameter "
                      f"(looked for {', '.join(_CURVE_PARAM_CANDIDATES)}).{''.join(tails)}")
    pv = p.value
    cs = safe(lambda: pv.getCurveSelections())
    if cs is None:
        return None, "Could not read the operation's curve selections."
    groups, knobs = (_rail_groups(name, entities, knobs) if explicit_groups is None
                     else (explicit_groups, knobs))
    # Ahead of every line below: getCurveSelections() hands back a DETACHED collection, so nothing
    # reaches the operation until applyCurveSelections and a refusal here performs no work.
    if name in _RAIL_PAIR_PARAMS and len(groups) < _RAILS_REQUIRED:
        return None, (f"'{name}' is a RAIL PAIR - {len(groups)} reference(s) were passed and it needs "
                      f"{_RAILS_REQUIRED}, the LOWER rail first. One contour makes the operation "
                      "report 'Invalid contours.' at generate, while two applied lower-rail-first "
                      "produced passes.")
    safe(lambda: cs.clear())
    builder = _CURVE_BUILDER[selection]
    for group in groups:
        sel = safe(lambda: getattr(cs, builder)())
        if sel is None:
            return None, f"createNew...({selection}) returned nothing on this operation."
        try:
            sel.inputGeometry = group         # MUTATION
        except Exception as e:
            return None, f"Could not set inputGeometry for the {selection} selection: {e}"
        kerr = _apply_knobs(sel, selection, group, knobs, factor, units, extra)
        if kerr:
            return None, kerr
    try:
        pv.applyCurveSelections(cs)           # MUTATION
    except Exception as e:
        return None, f"applyCurveSelections failed: {e}"
    applied = safe(lambda: pv.getCurveSelections())
    record, ferr = _read_back(applied, selection)
    if ferr:
        return record, ferr
    if explicit_groups is not None and record.get("selections") != len(explicit_groups):
        return record, (f"Requested {len(explicit_groups)} chain groups but read back "
                        f"{record.get('selections')} selections. Selection changes remain; inspect "
                        "the operation before retrying.")
    if explicit_groups is not None:
        extra["chain_groups_read"] = record["selections"]
    if name in _RAIL_PAIR_PARAMS:
        # The order the selections were built in is the order the references arrived in, and it is
        # published because getting it wrong fails SILENTLY - an upper-first pair generates valid and
        # EMPTY, so nothing downstream names the order as the thing to change.
        extra["rails_order"] = _RAILS_ORDER
        # Read off the collection the OPERATION hands back, not the objects written to, and
        # published only where every rail answers the SAME value.
        opened = {safe(lambda i=i: applied.item(i).isOpen) for i in range(record["selections"])}
        if len(opened) == 1 and None not in opened:
            extra["rails_open"] = bool(opened.pop())
    # A selection on a drive parameter with a mode is inert until that mode is engaged, so
    # select-and-engage is one call. Gated on a selection that actually took.
    if name in _ENGAGE_MODE and record.get("selections"):
        mode_param, want, subject, key = _ENGAGE_MODE[name]
        mode, quoted, merr = _engage_mode(op, mode_param, want, subject)
        if merr:
            return None, merr
        extra[f"{key}_engaged"] = True
        extra[f"{key}_mode"] = mode
        if quoted:
            extra.setdefault("quoted", []).append(mode_param)
    return record, None


def _direct_params(op):
    """([(selection kind, parameter name)] for every SETTABLE direct-family parameter this operation
    carries, [the names it carries that did not read isEditable true]) - a refusal advertises only
    the first, so the remedy it hands back is one the operation will accept."""
    out, blocked = [], []
    for kind, names in _DIRECT_PARAM.items():
        for nm in names:
            p = safe(lambda nm=nm: op.parameters.itemByName(nm))
            if p is None:
                continue
            if safe(lambda p=p: p.isEditable) is True:
                out.append((kind, nm))
            else:
                blocked.append(nm)
    return out, blocked


def _set_object_set(nm, p, entities, noun):
    """(count, None) or (None, error) - assign a CAD-object list to ONE direct-family parameter and
    read the count back off the parameter, since an assignment that raises nothing proves nothing."""
    pv = safe(lambda: p.value)
    if pv is None:
        return None, f"Could not read the operation's '{nm}' parameter value."
    wanted = list(entities)
    try:
        pv.value = wanted                     # MUTATION
    except Exception as e:
        return None, f"Could not set {nm}: {e}"
    back = safe(lambda: list(pv.value))
    if back is None:
        return None, (f"{nm} cannot be read back after {len(wanted)} {noun}(s) were assigned, so "
                      f"the selection is UNCONFIRMED - re-read the operation with {_PARAM_READ}.")
    if len(back) != len(wanted):
        return None, (f"Setting {nm} did not take - {len(wanted)} {noun}(s) were assigned and the "
                      f"operation reads back {len(back)}.")
    return len(back), None


def _apply_direct(op, selection, entities, extra):
    """(count, None) or (None, error) - mechanism (B): land the entities on the first SETTABLE
    parameter this kind names, then engage the mode that decides whether the operation reads it."""
    name = safe(lambda: op.name)
    blocked = []
    for nm in _DIRECT_PARAM[selection]:
        p = safe(lambda nm=nm: op.parameters.itemByName(nm))
        if p is None:
            continue
        # A set reading isEditable false silently drops the write, so it is refused before the
        # assignment rather than reported through the read-back as a count that did not take.
        if safe(lambda p=p: p.isEditable) is not True:
            blocked.append(nm)
            continue
        extra["selection_param"] = nm
        count, err = _set_object_set(nm, p, entities, _HANDLE_REQUIRE[selection])
        if err:
            return None, err
        merr = _engage_direct_mode(op, nm, extra)
        return (None, merr) if merr else (count, None)
    if blocked:
        return None, (f"Operation '{name}' carries {named_with_remainder(blocked)} but it did not "
                      f"read isEditable true, so the '{selection}' selection is not offered on it. "
                      "Select this geometry in the Fusion UI, or use an operation whose set reads "
                      "editable.")
    return None, (f"Operation '{name}' carries none of "
                  f"{', '.join(_DIRECT_PARAM[selection])} - the '{selection}' selection is for "
                  f"{_DIRECT_MISS[selection]}.")


def _engage_direct_mode(op, nm, extra):
    """The error for a direct-family selection whose mode could not be engaged, or None - the same
    select-and-engage step the curve family runs, so orientations are not left inert."""
    if nm not in _ENGAGE_MODE:
        return None
    mode_param, want, subject, key = _ENGAGE_MODE[nm]
    mode, quoted, merr = _engage_mode(op, mode_param, want, subject)
    if merr:
        return merr
    extra[f"{key}_engaged"] = True
    extra[f"{key}_mode"] = mode
    if quoted:
        extra.setdefault("quoted", []).append(mode_param)
    return None


def _surface_params(op):
    """({target key: (parameter name, param)} for every SETTABLE surface set THIS operation carries,
    in the canonical order of _SURFACE_TARGET_PARAM, [the parameter names it carries that did not
    read isEditable true]) - the list a refused target is named against, and what is left out."""
    # MEASURED on SURFACE sets only: checkSurfaceSelection reads isEditable False and assigning to
    # it raises '3 : Parameter is deprecated', while advancedSwarfSurfaces reads True. So a SURFACE
    # set is offered only where isEditable reads True; the curve params route by presence.
    out, blocked = {}, []
    for key, nm in _SURFACE_TARGET_PARAM.items():
        p = safe(lambda nm=nm: op.parameters.itemByName(nm))
        if p is None:
            continue
        if safe(lambda p=p: p.isEditable) is True:
            out[key] = (nm, p)
        else:
            blocked.append(nm)
    return out, blocked


def _apply_surfaces(op, faces, target, extra):
    """(count, None) or (None, error) - set ONE of the op's surface sets to `faces`. The role is
    never guessed: a target the op does not carry is refused naming the ones it does. The count is
    read back off the parameter, since an assignment that raises nothing proves nothing."""
    name = safe(lambda: op.name)
    carried, blocked = _surface_params(op)
    if not carried and blocked:
        return None, (f"Operation '{name}' carries no SETTABLE surface set - "
                      f"{named_with_remainder(blocked)} did not read isEditable true, so this call "
                      "will not assign faces there. Select this strategy's surfaces in the Fusion "
                      "UI, or use an operation whose surface set reads editable.")
    if not carried:
        return None, (f"Operation '{name}' carries none of the surface sets "
                      f"({', '.join(_SURFACE_TARGET_PARAM)}) - the 'surfaces' selection is for a "
                      "surface-driven strategy (geodesic, multi-axis finishing/roughing, ...).")
    listed = named_with_remainder([k for k in _SURFACE_TARGET_PARAM if k in carried])
    if not target:
        if _DEFAULT_SURFACE_TARGET not in carried:
            return None, (f"'surface_target' is needed here: operation '{name}' has no "
                          f"'{_SURFACE_TARGET_PARAM[_DEFAULT_SURFACE_TARGET]}' parameter to default "
                          f"to. It carries {listed} - pass whichever of those these faces are.")
        target = _DEFAULT_SURFACE_TARGET
    if target not in carried:
        # A blocked set is PRESENT on the operation, so 'has no parameter' would be the wrong read
        # to hand back - the refusal says which of the two it saw.
        param = _SURFACE_TARGET_PARAM[target]
        if param in blocked:
            return None, (f"Operation '{name}' carries '{param}' but it did not read isEditable "
                          f"true, so surface_target='{target}' is not offered on it. It carries "
                          f"{listed}.")
        return None, (f"Operation '{name}' has no '{param}' parameter, so surface_target="
                      f"'{target}' cannot be applied to it. It carries {listed}.")
    nm, p = carried[target]
    count, err = _set_object_set(nm, p, faces, _HANDLE_REQUIRE[_SURFACES])
    if err:
        return None, err
    extra["surface_target"] = target
    extra["surface_param"] = nm
    return count, None


def _filter_by_diameter(faces, min_d, max_d, factor):
    """Keep cylinder faces whose diameter (in display units; 'factor' = cm per unit) is within
    [min_d, max_d]. Non-cylinder faces are dropped. Returns (kept, non_cylinder, out_of_range)."""
    kept, non_cyl, out_range = [], 0, 0
    for f in faces:
        g = safe(lambda f=f: f.geometry)
        r = safe(lambda g=g: g.radius)        # cm; cylinder faces only
        if r is None:
            non_cyl += 1
            continue
        d = (2.0 * r) / factor                # cm radius -> diameter in display units
        if (min_d is not None and d < min_d - 1e-6) or (max_d is not None and d > max_d + 1e-6):
            out_range += 1
            continue
        kept.append(f)
    return kept, non_cyl, out_range


def _retained(applied, msg):
    """The error text for a failure that lands AFTER the height writes. The heights are set while the
    operation is still settled (see the ordering comment in the handler) and this call does not undo
    them, so a later refusal has to NAME what it left on the operation instead of reading as a
    no-op."""
    if not applied:
        return msg
    return (f"{msg} The height setting(s) {', '.join(applied)} were applied BEFORE this failure and "
            "REMAIN on the operation - this call did not undo them; set them back if the selection "
            "is not going to be applied.")


def _set_height_param(op, param_name, value):
    """(read_back, whether this call added the quotes, '') or (None, False, error) - set ONE height
    parameter's expression and CONFIRM the operation kept it. A stored-but-UNEVALUATED expression is
    reported only through .error, never through the expression the store echoes back."""
    p = safe(lambda: op.parameters.itemByName(param_name))
    if p is None:
        return None, False, f"{param_name} not found on this operation."
    before = safe(lambda: p.expression)
    # A height _mode is a STRING parameter: where the parameter already stores a QUOTED expression
    # the bare request is wrapped to match it, through the ONE shared decision.
    written, quoted = matched_quoting(before, value)
    try:
        p.expression = written            # ChoiceParameterValue takes the choice string
    except Exception as e:
        return None, False, (f"Could not set {param_name}='{value}': {e}"
                             + enumeration_remedy(str(e), written, _PARAM_READ, p))
    after = safe(lambda: p.expression)
    if after is None:
        return None, False, (f"{param_name} cannot be read back after being set to '{value}', so "
                             "the height is UNCONFIRMED.")
    eval_err, _warn = expression_error(p)
    if eval_err:
        return None, False, (f"{param_name} was set to '{value}' and reads back '{after}', but the "
                             f"parameter reports '{eval_err}' - the expression did not evaluate.")
    # Both sides of the compare go through the shared codec rather than over bytes: the store can
    # answer the same value in its own quoting.
    if unquote_expression(after) != unquote_expression(str(value)):
        return None, False, (f"Setting {param_name}='{value}' did not take - it reads back "
                             f"'{after}' (it held '{before}').")
    return after, quoted, ""


def _set_height(op, which, mode, offset):
    """Set a top/bottom height via _mode and/or _offset (never the resolved _value), each write
    confirmed by its own read-back. Returns (applied, the parameters this call quoted, error-or-None);
    `applied` carries one '<param>=<read-back>' entry per write that LANDED, so a half-applied pair
    still names its half."""
    applied, quoted = [], []
    for suffix, value in (("mode", mode), ("offset", offset)):
        if value is None:
            continue
        param_name = f"{which}Height_{suffix}"
        back, wrapped, err = _set_height_param(op, param_name, value)
        if err:
            return applied, quoted, err
        applied.append(f"{param_name}={back}")
        if wrapped:
            quoted.append(param_name)
    return applied, quoted, None


def _rail_tail(result) -> str:
    """The launched note's rail clause, or '' - gated on rails_order, the key only the rail path
    sets. The triage itself lives where the empty toolpath shows up: cam_get_status."""
    if "rails_order" not in result:
        return ""
    return " For a rail pair, see cam_get_status's rail_triage."


def _unread_entitlement(result, allowed):
    """`result`, with the disclosure a launch made under an UNREAD isGenerationAllowed flag carries -
    no pre-flight was made, so nothing here excluded anything."""
    if allowed is None:
        result["entitlement_checked"] = False
        result["note"] += _ENTITLEMENT_UNREAD
    return result


def handler(operation: str = "", selection: str = "", handles=None, bodies=None, sketches=None,
            component: str = "",
            is_open: bool = None, reverted: bool = None,
            loop_type: str = None, side_type: str = None, pocket_filter=None,
            min_diameter: float = None, max_diameter: float = None,
            surface_target: str = None,
            top_mode: str = None, top_offset: str = None,
            bottom_mode: str = None, bottom_offset: str = None,
            units: str = "mm", generate: bool = True,
            allow_pocket_recognition: bool = False, chain_groups=None) -> dict:
    """See TOOL_DESCRIPTION."""
    selection = (selection or "").strip().lower()
    if selection not in _SELECTIONS:
        return error(f"selection must be one of {', '.join(_SELECTIONS)}; got '{selection}'.")
    if selection == _POCKET_RECOGNITION and not allow_pocket_recognition:
        return error("selection='pocket_recognition' is disabled by default because native pocket "
                     "recognition can terminate Fusion. Pass allow_pocket_recognition=true for "
                     "an explicit diagnostic attempt; use selection='pocket' with a floor-face "
                     "handle or selection='chain' with edge handles instead.")

    if chain_groups is not None:
        if selection != _CHAIN or handles:
            return error("chain_groups requires selection='chain' and replaces handles.")
        if (not isinstance(chain_groups, list) or not chain_groups
                or any(not isinstance(group, list) or not group
                       or any(not isinstance(h, str) or not h.strip() for h in group)
                       for group in chain_groups)):
            return error("chain_groups must contain nonempty lists of edge handles.")
        handles = [h for group in chain_groups for h in group]

    scope = (component or "").strip()
    knobs = {"is_open": is_open, "reverted": reverted, "pocket_filter": pocket_filter or None,
             "min_diameter": min_diameter, "max_diameter": max_diameter,
             "component": scope or None}
    for kind, raw in ((LOOP_TYPE, loop_type), (SIDE_TYPE, side_type),
                      (SURFACE_TARGET, surface_target)):
        if raw is None:
            knobs[kind.name] = None
            continue
        value, kerr = kind.resolve(raw)
        if kerr:
            return error(kerr)
        knobs[kind.name] = value
    kerr = _knob_guard(selection, knobs)
    if kerr:
        return error(kerr)

    units_key = (units or "mm").strip().lower()
    factor = scale(units_key)
    if factor is None:
        return error(f"Unknown units '{units}'. Use mm, cm, or in.")

    cam, cerr = get_cam()
    if cerr:
        return error(cerr)
    node, oerr = resolve_cam_node(cam, operation, kinds=("operation",), label="operation")
    if oerr:
        return error(oerr)
    op = node.obj

    entities, herr = _resolve_geometry(selection, handles, bodies, sketches, scope)
    if herr:
        return error(herr)

    explicit_groups = None
    if selection == _CHAIN:
        curve_name, _parameter = _curve_param(op)
        if chain_groups is not None:
            if curve_name in _PER_REFERENCE_PARAMS:
                return error(f"'{curve_name}' already separates each reference; use handles here.")
            if len(entities) != sum(len(group) for group in chain_groups):
                return error("chain_groups did not resolve one edge per handle; refresh the handles.")
            explicit_groups, offset = [], 0
            for group in chain_groups:
                explicit_groups.append(entities[offset:offset + len(group)])
                offset += len(group)
        elif len(entities) > 1 and curve_name not in _PER_REFERENCE_PARAMS:
            return error("Multiple chain handles need chain_groups: one list per contour. "
                         "Wrap connected edges in one group; separate disconnected contours. "
                         "No heights or selections were changed.")
    result = {"operation": safe(lambda: op.name), "selection": selection}

    # ── every refusal that can be decided WITHOUT touching the operation runs here ──
    # The diameter filter reads the passed faces only, so it refuses while the operation is still as
    # found - before the height block below, which MUTATES.
    faces = entities
    diam_note = None
    if selection == _HOLES and (min_diameter is not None or max_diameter is not None):
        faces, non_cyl, out_range = _filter_by_diameter(entities, min_diameter, max_diameter, factor)
        diam_note = (f"diameter filter [{min_diameter},{max_diameter}]{units_key} kept {len(faces)} "
                     f"(dropped {out_range} out-of-range, {non_cyl} non-cylinder).")
        if not faces:
            return error("No cylinder faces left after the diameter filter. " + diam_note)

    # ── heights FIRST (before the selection) ──
    # A height _mode's valid enum is CONTEXT-DEPENDENT: setting bottomHeight_mode after re-applying
    # a chain threw 'Invalid enumeration value', so heights are set while the op is settled.
    applied, wrapped = [], []
    for which, mode, offset in (("top", top_mode, top_offset), ("bottom", bottom_mode, bottom_offset)):
        if mode is None and offset is None:
            continue
        landed, quoted, herr = _set_height(op, which, mode, offset)
        # The writes that LANDED are kept whichever way the group ends: they are what _retained
        # names as remaining on the operation, and what heights_set publishes when it succeeds.
        applied.extend(landed)
        wrapped.extend(quoted)
        if herr:
            return error(_retained(applied, herr))
    if applied:
        result["heights_set"] = applied

    # ── apply the selection ──
    # 'quoted' names every parameter this call WRAPPED to match what it already stored - the mode
    # engage below appends to the same list. Absent means each request was written as it was sent.
    extra = {"quoted": wrapped} if wrapped else {}
    if selection in _DIRECT_PARAM:
        count, aerr = _apply_direct(op, selection, faces, extra)
        record = None if aerr else {"selections": count}
    elif selection == _SURFACES:
        count, aerr = _apply_surfaces(op, entities, knobs.get("surface_target"), extra)
        record = None if aerr else {"selections": count}
    else:
        record, aerr = _apply_curve(op, selection, entities, knobs, factor, units_key, extra,
                                    explicit_groups=explicit_groups)
    if aerr:
        return error(_retained(applied, aerr))
    if not record.get("selections"):
        return error(_retained(applied,
                     "Selection applied but the operation reports 0 selections - the geometry was "
                     "rejected. Check the geometry matches the strategy (edges for chain, the pocket "
                     "floor face for pocket, bodies for silhouette/pocket_recognition, whole sketches "
                     "for sketch, cylinder faces for holes, the surface set's faces for surfaces)."))
    result.update(record)
    result.update(extra)
    # Bounded through the shared capped-list renderer: this list is as long as the selection, and a
    # silently cut one would read as the complete set of what is now being machined.
    labels = _selected_labels(selection, entities)
    if labels:
        result["selected"] = named_with_remainder(labels)
    if diam_note:
        result["diameter_filter"] = diam_note

    # ── generate: LAUNCH async and return - generation runs in the background on its own ──
    if not generate:
        result["note"] = "Selection applied; pass generate=true (or cam_generate) to compute the toolpath."
        return ok(result)

    op_name = result["operation"] or operation
    # The entitlement pre-flight, before the launch and on the same seam cam_generate excludes on.
    # `is False`, not a falsiness test: None is the flag that would not read.
    strategy = safe(lambda: op.strategy)
    allowed = strategy_generation_allowed(strategy)
    if allowed is False:
        result["launched"] = False
        result["entitlement_blocked"] = {"operation": op_name, "strategy": strategy}
        result["note"] = _BLOCKED_GENERATE.format(strategy=strategy)
        return ok(result)

    handle, gerr = _launch_generation(cam, op, op_name)
    if gerr:
        result["generate_error"] = gerr
        result["note"] = (f"Selection applied but generation failed to launch: {gerr}. The selection "
                          f"is saved - fix the cause, then run cam_generate(target='{op_name}').")
        return ok(_unread_entitlement(result, allowed))
    result["launched"] = True
    result["handle"] = handle
    result["note"] = (f"Selection applied; generation is launched - check "
                      f"cam_get_status(target='{op_name}') until completed=true. has_toolpath False "
                      "on completion means no path was produced, and the warning channel can be "
                      "silent there - check the heights and the selection.") + _rail_tail(result)
    return ok(_unread_entitlement(result, allowed))


TOOL_DESCRIPTION = (
    "Select the machining geometry on a CAM operation; 'selection' picks the family and fixes "
    "which input carries it. Native pocket recognition requires allow_pocket_recognition=true."
)

tool = (
    Tool.create_simple(name="cam_select_geometry", description=TOOL_DESCRIPTION)
    .add_input_property("operation", {"type": "string",
            "description": "Operation name (from cam_get)."})
    .add_input_property("selection", {"type": "string", "enum": list(_SELECTIONS)})
    .add_input_property("handles", {"type": "array", "items": {"type": "string"}})
    .add_input_property(*BODIES.as_property())
    .add_input_property(*SKETCHES.as_property())
    .add_input_property(*_sketch_detail.component_scope("component", narrows="sketches / bodies"))
    .add_input_property("chain_groups", {"type": "array", "minItems": 1,
            "items": {"type": "array", "minItems": 1, "items": {"type": "string"}},
            "description": "Chain only: one edge-handle list per contour; replaces handles."})
    .add_input_property("is_open", {"type": "boolean", "description": "Chain: open profile (default closed)."})
    .add_input_property("reverted", {"type": "boolean"})
    .add_input_property(*LOOP_TYPE.as_property())
    .add_input_property(*SIDE_TYPE.as_property())
    .add_input_property("pocket_filter", {"type": "object", "additionalProperties": False,
            "properties": _POCKET_FILTER_PROPERTIES,
            "description": "Lengths in 'units'."})
    .add_input_property("min_diameter", {"type": "number",
            "description": "In 'units'; filters the handles passed, never discovers them."})
    .add_input_property("max_diameter", {"type": "number", "description": "In 'units'."})
    .add_input_property(*SURFACE_TARGET.as_property())
    .add_input_property(*_inputs.UNITS.as_property())
    .add_input_property("top_mode", {"type": "string", "description": "e.g. 'from stock top'."})
    .add_input_property("top_offset", {"type": "string"})
    .add_input_property("bottom_mode", {"type": "string", "description": "e.g. 'from contour'."})
    .add_input_property("bottom_offset", {"type": "string"})
    .add_input_property("generate", {"type": "boolean",
            "description": "Async - read cam_get_status."})
    .add_input_property("allow_pocket_recognition", {"type": "boolean",
            "description": "Required for native pocket recognition; defaults false."})
    .strict_schema()
)
item = Item.create_tool_item(
    tool=tool, write="write", handler=handler, run_on_main_thread=True,
    # The selection effect only: generate=true LAUNCHES a background generation this call never
    # reads back, and the payload sends the caller to cam_get_status for it. The height arm reads
    # each {which}Height_mode/_offset back and heights_set publishes those read-backs.
    verification=Verification(
        kind="inline",
        evidence_test="tests/unit/test_cam_select_geometry.py::TestHoles"
                      "::test_a_hole_set_that_keeps_fewer_faces_than_assigned_is_an_error",
        rung="value"))


def register_tool():
    register(item)
