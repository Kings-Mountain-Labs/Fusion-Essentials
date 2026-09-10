# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""PMI substrate: the annotation walk, the by-name resolver, the {symbol} markup codec and the
light record every pmi_* tool reports through.

Design.pmiSettings is never read - the getter raises InternalValidationError when no settings
object exists. PMI CONTENT writes raise "3 : Manufacturing or Design Extension is required" on
2705.0.87; reads and non-content writes such as hide/show are free."""

import re

import adsk.core
import adsk.fusion

from . import _common
from ._common import safe

MAP_BLURB = ("the PMI substrate. walk_annotations/find_annotation - the design-wide walk and the "
             "resolve-one REFUSING a shared name; build_segments/"
             "segments_markup/annotation_record - the {symbol} markup codec and light record; "
             "readable_warning - errorOrWarningMessage as one bounded line; "
             "build_tolerance/build_display - the tolerance/display codecs; apply_*/"
             "normalize_extension/set_* - the writers, each re-read")

# The leader extension these tools refuse below, and the value pmi_create pins onto an input
# arriving under it. Whether the platform's own floor is this constant is unmeasured on 2705;
# normalize_extension tries the repair and reports a raise either way.
LEADER_EXT_FLOOR = 0.25
LEADER_EXT_DEFAULT = 0.5

# {token} -> PMISymbolTypes member: the closed markup vocabulary. A bad token's error lists exactly
# these names, so the legal set is enforced by the parser, not asserted in prose.
SYMBOLS = {
    "angularity": "AngularityPMISymbolType",
    "center_line": "CenterLinePMISymbolType",
    "circular_runout": "CircularRunoutPMISymbolType",
    "circularity": "CircularityPMISymbolType",
    "concentricity": "ConcentricityPMISymbolType",
    "conical_taper": "ConicalTaperPMISymbolType",
    "counterbore": "CounterborePMISymbolType",
    "countersink": "CountersinkPMISymbolType",
    "cylindricity": "CylindricityPMISymbolType",
    "degrees": "DegreesPMISymbolType",
    "depth": "DepthPMISymbolType",
    "diameter": "DiameterPMISymbolType",
    "envelope": "EnvelopePMISymbolType",
    "flatness": "FlatnessPMISymbolType",
    "free_state": "FreeStatePMISymbolType",
    "lmc": "LeastMaterialConditionPMISymbolType",
    "line_profile": "LineProfilePMISymbolType",
    "mmc": "MaximumMaterialConditionPMISymbolType",
    "not_equal": "NotEqualPMISymbolType",
    "parallelism": "ParallelismPMISymbolType",
    "perpendicularity": "PerpendicularityPMISymbolType",
    "position": "PositionPMISymbolType",
    "projected_tolerance": "ProjectedTolerancePMISymbolType",
    "slope": "SlopePMISymbolType",
    "squareness": "SquarenessPMISymbolType",
    "straightness": "StraightnessPMISymbolType",
    "surface_profile": "SurfaceProfilePMISymbolType",
    "symmetry": "SymmetryPMISymbolType",
    "tolerance": "TolerancePMISymbolType",
    "total_runout": "TotalRunoutPMISymbolType",
}

_TOKEN = re.compile(r"\{([a-z_]+)\}")

# objectType suffix -> the kind label the pmi_* wire surface speaks in.
_KIND_BY_SUFFIX = {
    "PMILeaderLineNote": "note",
    "PMIHoleThreadNote": "hole_note",
    "PMIImportedDimension": "imported_dimension",
    "PMIImportedNote": "imported_note",
    "PMIImportedGDTDatum": "imported_gdt_datum",
    "PMIImportedGeometricTolerance": "imported_geometric_tolerance",
    "PMIImportedSurfaceTexture": "imported_surface_texture",
    "PMIImportedGraphical": "imported_graphical",
    "PMIImportedFolder": "imported_folder",
}

# The kinds authored in Fusion (PMICreatedAnnotation subclasses) - the only ones whose
# segments/name/extension are writable; imported kinds are read-only until convert_imported.
CREATED_KINDS = ("note", "hole_note")


def kind_of(ann):
    """The annotation's kind label from its objectType suffix ('note', 'hole_note', 'imported_*')."""
    ot = (safe(lambda: ann.objectType, "") or "").split("::")[-1]
    return _KIND_BY_SUFFIX.get(ot, ot or "unknown")


def build_segments(text):
    """(segments, error): parse the {symbol} markup into PMISegment objects - '{flatness}0.05' ->
    a symbol segment + a text segment; a newline -> a line-break segment. An unknown token is
    refused listing the legal vocabulary."""
    segs = []
    for line_i, line in enumerate((text or "").split("\n")):
        if line_i:
            segs.append(adsk.fusion.PMILineBreakSegment.create())
        pos = 0
        for m in _TOKEN.finditer(line):
            attr = SYMBOLS.get(m.group(1))
            if attr is None:
                return None, ("Unknown symbol token '{%s}'. Legal tokens: %s."
                              % (m.group(1), ", ".join(sorted(SYMBOLS))))
            if m.start() > pos:
                segs.append(adsk.fusion.PMITextSegment.create(line[pos:m.start()]))
            segs.append(adsk.fusion.PMISymbolSegment.create(
                getattr(adsk.fusion.PMISymbolTypes, attr)))
            pos = m.end()
        if pos < len(line):
            segs.append(adsk.fusion.PMITextSegment.create(line[pos:]))
    if not segs:
        return None, "'text' is empty - a note needs at least one character or {symbol} token."
    return segs, None


def _tokens_by_value():
    """PMISymbolTypes value -> markup token, for re-encoding segments back into markup."""
    out = {}
    for token, attr in SYMBOLS.items():
        v = safe(lambda a=attr: getattr(adsk.fusion.PMISymbolTypes, a))
        if v is not None:
            out[v] = token
    return out


def segments_markup(ann):
    """The annotation's segments re-encoded as {symbol} markup (round-trips through
    build_segments), or None when the annotation carries no readable segments."""
    segs = safe(lambda: ann.segments)
    if segs is None:
        return None
    by_value = _tokens_by_value()
    parts = []
    for s in segs:
        ot = (safe(lambda s=s: s.objectType, "") or "")
        if ot.endswith("PMITextSegment"):
            parts.append(safe(lambda s=s: s.text, "") or "")
        elif ot.endswith("PMISymbolSegment"):
            v = safe(lambda s=s: s.pmiSymbolType)
            parts.append("{%s}" % by_value.get(v, "symbol_%s" % v))
        elif ot.endswith("PMILineBreakSegment"):
            parts.append("\n")
    return "".join(parts)


def walk_annotations(d, stats=None):
    """Yield (component, annotation) across every component. `stats`, when a dict is passed, is
    filled once the walk runs out with 'components_unreadable' and 'items_unreadable' - the holes
    it left behind, which a caller publishing tallies publishes beside them."""
    comps_unreadable = 0
    items_unreadable = 0
    for comp in _common.all_components(d):
        coll = safe(lambda c=comp: c.pmiAnnotations)
        # counted(), not safe(read, 0): a count that raises is unknown, not zero.
        n = _common.counted(lambda: coll.count) if coll is not None else None
        if n is None:
            comps_unreadable += 1
            continue
        for i in range(n):
            a = safe(lambda i=i: coll.item(i))
            if a is None:
                items_unreadable += 1
                continue
            yield comp, a
    if stats is not None:
        stats["components_unreadable"] = comps_unreadable
        stats["items_unreadable"] = items_unreadable


def annotation_hits(d, name, component="", stats=None):
    """([(annotation, component)], available_names): every case-insensitive EXACT name match in
    the design (or only 'component' when given). A caller telling a MISS from an AMBIGUITY reads
    the hit count here rather than find_annotation's error text; `stats` is the walk's hole record,
    since zero hits over an incomplete walk is not proof the name is absent."""
    want = (name or "").strip()
    comp_want = (component or "").strip()
    hits, available = [], []
    for comp, a in walk_annotations(d, stats):
        cname = safe(lambda c=comp: c.name, "") or ""
        if comp_want and cname.lower() != comp_want.lower():
            continue
        nm = safe(lambda a=a: a.name, "") or ""
        available.append(nm)
        if want and nm.lower() == want.lower():
            hits.append((a, comp))
    return hits, available


def find_annotation(d, name, component=""):
    """(annotation, component, error): case-insensitive EXACT name match across every component
    (or only 'component' when given). Several hits are REFUSED naming each hit's component; a miss
    lists the names that exist."""
    want = (name or "").strip()
    if not want:
        return None, None, "'annotation' is required (a PMI name from pmi_get)."
    comp_want = (component or "").strip()
    hits, available = annotation_hits(d, want, comp_want)
    if not hits:
        scope = f" in component '{comp_want}'" if comp_want else ""
        listing = ", ".join(sorted(available)[:40]) or "none"
        return None, None, f"No PMI named '{want}'{scope}. Available: {listing}."
    if len(hits) > 1:
        where = ", ".join(sorted((safe(lambda c=c: c.name, "") or "?") for _a, c in hits))
        return None, None, (f"'{want}' names a PMI in {len(hits)} components ({where}) - PMI names "
                            "are only unique per component. Pass component= to pick one.")
    return hits[0][0], hits[0][1], None


def normalize_extension(ann):
    """Lift an annotation whose leaderLineExtension reads below LEADER_EXT_FLOOR back to
    LEADER_EXT_DEFAULT before a geometric/segment edit. Returns an error string when the repair
    set itself RAISES - the note is then recreate-only - else None. The ONE normalize both
    set_text_point and pmi_edit's actions run."""
    cur = safe(lambda: ann.leaderLineExtension)
    if cur is None or cur >= LEADER_EXT_FLOOR:
        return None
    try:
        ann.leaderLineExtension = LEADER_EXT_DEFAULT
    except Exception as e:
        return (f"This note's leader extension ({cur} cm) is below the {LEADER_EXT_FLOOR} cm "
                f"floor and the repair set raised ({e}) - delete and recreate it (pmi_delete + "
                "pmi_create pins the extension on the input).")
    return None


def set_text_point(ann, xyz, f):
    """Assign the annotation's text anchor from a model-space [x,y,z] (display units, scaled by f
    to cm), PROJECTED onto the annotation plane first: whether the platform accepts an off-plane
    point is UNMEASURED on 2705 (probe P0's gate), so the projection is unconditional and the
    assignment is re-read. Returns (Point3D_read_back, error)."""
    try:
        x, y, z = (float(v) for v in xyz)
    except Exception:
        return None, "'text_point' must be [x, y, z] numbers (model space, in 'units')."
    nerr = normalize_extension(ann)
    if nerr:
        return None, nerr
    pt = adsk.core.Point3D.create(x * f, y * f, z * f)
    plane = safe(lambda: ann.plane)
    target = pt
    if plane is not None:
        try:
            n, o = plane.normal, plane.origin
            mag = (n.x * n.x + n.y * n.y + n.z * n.z) ** 0.5 or 1.0
            dv = ((pt.x - o.x) * n.x + (pt.y - o.y) * n.y + (pt.z - o.z) * n.z) / (mag * mag)
            target = adsk.core.Point3D.create(pt.x - dv * n.x, pt.y - dv * n.y, pt.z - dv * n.z)
        except Exception:
            target = pt
    try:
        ann.annotationTextPoint = target
    except Exception as e:
        return None, f"Setting the text point failed: {e}"
    got = safe(lambda: ann.annotationTextPoint)
    if got is None:
        return None, "The text point did not take (re-read returned nothing)."
    # The re-read is COMPARED to what was assigned, not just checked for existence: a point that
    # reads back somewhere else is a move that did not take, and a payload built from the read-back
    # alone reports that wrong anchor as the new one. 1e-6 cm absorbs float settle, nothing more.
    landed = tuple(safe(lambda ax=ax: getattr(got, ax)) for ax in ("x", "y", "z"))
    if not all(isinstance(c, (int, float)) and not isinstance(c, bool) for c in landed):
        return None, ("The text point read back without readable x/y/z, so where it landed is "
                      "UNKNOWN - the move is not confirmed.")
    want = (target.x, target.y, target.z)
    if max(abs(landed[i] - want[i]) for i in range(3)) > 1e-6:
        return None, (f"The text point landed at {landed} cm, not the assigned {want} cm - the "
                      "move did not take.")
    return got, None


# choice token -> LeaderLineNotePlaneTypes member ('face' needs an adjacent face; 'custom_face'
# any face; the platform enforces both at setAnnotationPlane time).
PLANE_TYPES = {
    "face": "NormalToFaceLeaderLineNotePlaneType",
    "custom_face": "NormalToCustomFaceLeaderLineNotePlaneType",
    "circular_edge": "NormalToCircularEdgeLeaderLineNotePlaneType",
    "cylinder_axis": "AxisCylinderAndConeLeaderLineNotePlaneType",
    "xy": "PrincipalXYLeaderLineNotePlaneType",
    "yz": "PrincipalYZLeaderLineNotePlaneType",
    "zx": "PrincipalZXLeaderLineNotePlaneType",
}
H_ALIGN = {"left": "LeftHorizontalAlignment", "center": "CenterHorizontalAlignment",
           "right": "RightHorizontalAlignment"}
V_ALIGN = {"top": "TopVerticalAlignment", "middle": "MiddleVerticalAlignment",
           "bottom": "BottomVerticalAlignment"}
DISPLAY_UNITS = {"document": "UseDocumentUnitPMIUnitType", "mm": "MillimetersPMIUnitType",
                 "cm": "CentimetersPMIUnitType", "m": "MetersPMIUnitType",
                 "in": "InchesPMIUnitType", "ft": "FeetPMIUnitType"}

# The closed set of keys build_display writes; anything else is refused, so the wire never has to
# carry the list ('secondary' is peeled off by apply_display before the spec gets here).
DISPLAY_KEYS = ("precision", "units", "leading_zeros", "trailing_zeros", "unit_abbreviation")

# The numeric fields a hole/thread note carries as PMIGeometricValue, by wire key. Angle fields
# resolve in radians, lengths in cm.
HOLE_VALUE_PROPS = ("diameter", "radius", "depth", "counterbore_diameter", "counterbore_radius",
                    "counterbore_depth", "countersink_diameter", "countersink_angle_deg",
                    "thread_depth")
HOLE_VALUE_ATTR = {"diameter": "diameter", "radius": "radius", "depth": "depth",
                    "counterbore_diameter": "counterboreDiameter",
                    "counterbore_radius": "counterboreRadius",
                    "counterbore_depth": "counterboreDepth",
                    "countersink_diameter": "countersinkDiameter",
                    "countersink_angle_deg": "countersinkAngle",
                    "thread_depth": "threadDepth"}


def enum_label(owner, cls_name, suffix, value):
    """The snake_case name of `value` in enum class `cls_name` on module `owner` (adsk.fusion /
    adsk.core), with `suffix` stripped - e.g. (fusion, 'PMIStandardTypes', 'PMIStandardType', 1)
    -> 'iso'. Falls back to the raw int as a string when unmapped."""
    cls = getattr(owner, cls_name, None)
    for m in dir(cls or ()):
        if m.startswith("_") or m == "thisown":
            continue
        if safe(lambda m=m: getattr(cls, m)) == value and m.endswith(suffix):
            base = m[: -len(suffix)]
            out, prev = [], ""
            for ch in base:
                if ch.isupper() and prev and (not prev.isupper()):
                    out.append("_")
                out.append(ch.lower())
                prev = ch
            return "".join(out).strip("_")
    return str(value)


def build_tolerance(spec, f):
    """(PMIGeometricValueTolerance, error) from a wire spec dict: type= symmetric (value) |
    deviation (upper, lower) | limits | limits_linear (min, max) | max | min | fits_stacked |
    fits_linear | fits_size_limits | fits_tolerance (size, hole_fit, shaft_fit). Bounds are in
    display units and scale by f to cm. Every set*() bool is gated."""
    if not isinstance(spec, dict) or not spec.get("type"):
        return None, ("'tolerance' must be an object with 'type' - one of: symmetric, deviation, "
                      "limits, limits_linear, max, min, fits_stacked, fits_linear, "
                      "fits_size_limits, fits_tolerance.")
    t = str(spec["type"]).strip().lower()
    tol = adsk.fusion.PMIGeometricValueTolerance.create()
    def num(key):
        v = spec.get(key)
        return None if v is None else float(v) * f
    try:
        if t == "symmetric":
            done = tol.setSymmetric(num("value") or 0.0)
        elif t == "deviation":
            done = tol.setDeviation(num("upper") or 0.0, num("lower") or 0.0)
        elif t == "limits":
            done = tol.setLimitsStacked(num("min") or 0.0, num("max") or 0.0)
        elif t == "limits_linear":
            done = tol.setLimitsLinear(num("min") or 0.0, num("max") or 0.0)
        elif t == "max":
            done = tol.setMAX()
        elif t == "min":
            done = tol.setMIN()
        elif t in ("fits_stacked", "fits_linear", "fits_size_limits", "fits_tolerance"):
            setter = {"fits_stacked": tol.setLimitsFitsStacked,
                      "fits_linear": tol.setLimitsFitsLinear,
                      "fits_size_limits": tol.setLimitsFitsSizeLimits,
                      "fits_tolerance": tol.setLimitsFitsTolerance}[t]
            done = setter(num("size") or 0.0, str(spec.get("hole_fit") or ""),
                          str(spec.get("shaft_fit") or ""))
        else:
            return None, (f"Unknown tolerance type '{t}'. Use symmetric, deviation, limits, "
                          "limits_linear, max, min, or fits_stacked/linear/size_limits/tolerance.")
    except Exception as e:
        return None, f"Tolerance '{t}' construction failed: {e}"
    if not done:
        return None, (f"Tolerance '{t}' was declined by the platform (set returned false) - "
                      "check the values (fits need size + hole_fit/shaft_fit like 'H7'/'h6').")
    return tol, None


def tolerance_record(tol, out_f):
    """The readable record of a PMIGeometricValueTolerance, or None. The bounds scale by out_f -
    callers pass the factor the bounded value itself was converted with."""
    if tol is None or not safe(lambda: tol.hasTolerances, False):
        return None
    rec = {"type": enum_label(adsk.fusion, "PMIToleranceTypes", "PMIToleranceType",
                              safe(lambda: tol.toleranceType))}
    if safe(lambda: tol.hasUpperTolerance, False):
        rec["upper"] = round(safe(lambda: tol.upperTolerance, 0.0) * out_f, 6)
    if safe(lambda: tol.hasLowerTolerance, False):
        rec["lower"] = round(safe(lambda: tol.lowerTolerance, 0.0) * out_f, 6)
    if safe(lambda: tol.hasToleranceClass, False):
        rec["hole_fit"] = "%s%s" % (safe(lambda: tol.toleranceClassDeviation, ""),
                                    safe(lambda: tol.toleranceClassGrade, ""))
    if safe(lambda: tol.hasShaftToleranceClass, False):
        rec["shaft_fit"] = "%s%s" % (safe(lambda: tol.shaftToleranceClassDeviation, ""),
                                     safe(lambda: tol.shaftToleranceClassGrade, ""))
    return rec


def value_record(gv, out_f, angle=False):
    """The readable record of a PMIGeometricValue {value, overridden?, tolerance?}, or None.
    Angles report degrees, lengths in display units."""
    if gv is None or not safe(lambda: gv.hasValue, False):
        return None
    import math
    raw = safe(lambda: gv.value)
    if raw is None:
        return None
    rec = {"value": round(math.degrees(raw), 4) if angle else round(raw * out_f, 6)}
    if safe(lambda: gv.isOverriddenValue, False):
        rec["overridden"] = True
    # The bounds are published through the same conversion as the value they bound: the degrees
    # conversion for an angle, out_f (a LENGTH factor, meaningless on an angle) otherwise.
    tr = tolerance_record(safe(lambda: gv.tolerance), math.degrees(1.0) if angle else out_f)
    if tr:
        rec["tolerance"] = tr
    return rec


def build_display(spec):
    """(PMIDisplaySettings, error) from a wire spec dict: precision (0-8), units
    (document/mm/cm/m/in/ft), leading_zeros, trailing_zeros, unit_abbreviation."""
    if not isinstance(spec, dict):
        return None, "'display' must be an object: {%s}." % ", ".join(DISPLAY_KEYS)
    for key in spec:
        if key not in DISPLAY_KEYS:
            return None, (f"Unknown display key '{key}'. Legal keys: "
                          f"{', '.join(DISPLAY_KEYS)} (plus secondary: {{...}} at the top level).")
    ds = adsk.fusion.PMIDisplaySettings.create()
    try:
        if spec.get("precision") is not None:
            ds.precision = int(spec["precision"])
        if spec.get("units") is not None:
            attr = DISPLAY_UNITS.get(str(spec["units"]).strip().lower())
            if attr is None:
                return None, f"display.units must be one of: {', '.join(sorted(DISPLAY_UNITS))}."
            ds.unitType = getattr(adsk.fusion.PMIUnitTypes, attr)
        for key, prop in (("leading_zeros", "hasLeadingZeros"), ("trailing_zeros", "hasTrailingZeros"),
                          ("unit_abbreviation", "hasUnitAbbreviation")):
            if spec.get(key) is not None:
                setattr(ds, prop, bool(spec[key]))
    except Exception as e:
        return None, f"Display settings construction failed: {e}"
    return ds, None


def display_record(ds):
    """The readable record of a PMIDisplaySettings, or None."""
    if ds is None:
        return None
    return {
        "precision": safe(lambda: ds.precision),
        "units": enum_label(adsk.fusion, "PMIUnitTypes", "PMIUnitType", safe(lambda: ds.unitType)),
        "leading_zeros": bool(safe(lambda: ds.hasLeadingZeros, False)),
        "trailing_zeros": bool(safe(lambda: ds.hasTrailingZeros, False)),
        "unit_abbreviation": bool(safe(lambda: ds.hasUnitAbbreviation, False)),
    }


def apply_note_format(obj, align="", valign="", perpendicular=None, extension_cm=None,
                      units="cm"):
    """Apply the shared leader/text format knobs to a note or note-input `obj`. EVERY set is
    re-read and a value that did not take is an error, reported in the caller's `units` (the
    extension arrives in cm). Returns an error string, or None."""
    try:
        if align:
            attr = H_ALIGN.get(align.strip().lower())
            if attr is None:
                return f"'align' must be one of: {', '.join(sorted(H_ALIGN))}."
            want = getattr(adsk.core.HorizontalAlignments, attr)
            obj.horizontalAlignment = want
            if safe(lambda: obj.horizontalAlignment) != want:
                return f"'align'={align} did not take on this annotation."
        if valign:
            attr = V_ALIGN.get(valign.strip().lower())
            if attr is None:
                return f"'valign' must be one of: {', '.join(sorted(V_ALIGN))}."
            want = getattr(adsk.core.VerticalAlignments, attr)
            obj.verticalAlignment = want
            if safe(lambda: obj.verticalAlignment) != want:
                return f"'valign'={valign} did not take on this annotation."
        if perpendicular is not None:
            want_perp = bool(perpendicular)
            obj.isPerpendicularLine = want_perp
            got_perp = safe(lambda: obj.isPerpendicularLine)
            if got_perp is None or bool(got_perp) != want_perp:
                return (f"'perpendicular'={want_perp} did not take on this annotation "
                        f"(re-read {got_perp}).")
        if extension_cm is not None:
            per_cm = _common.scale(units)
            unit = units if per_cm else "cm"
            per_cm = per_cm or 1.0
            if extension_cm < LEADER_EXT_FLOOR:
                return (f"'leader_extension'={round(extension_cm / per_cm, 6)} {unit} is under the "
                        f"{round(LEADER_EXT_FLOOR / per_cm, 6)} {unit} floor these tools refuse "
                        "below.")
            obj.leaderLineExtension = float(extension_cm)
            got_ext = safe(lambda: obj.leaderLineExtension)
            if got_ext is None or abs(got_ext - float(extension_cm)) > 1e-6:
                return (f"'leader_extension'={round(extension_cm / per_cm, 6)} {unit} did not take "
                        f"on this annotation (re-read "
                        f"{round(got_ext / per_cm, 6) if got_ext is not None else None} {unit}).")
    except Exception as e:
        return f"Note format set failed: {e}"
    return None


def apply_display(obj, display):
    """Apply a display spec - {precision, units, leading_zeros, trailing_zeros,
    unit_abbreviation, secondary: {...}} - to a hole/thread note. The ONE display writer both
    pmi_create and pmi_edit run. Returns an error string, or None."""
    spec = dict(display) if isinstance(display, dict) else display
    secondary = spec.pop("secondary", None) if isinstance(spec, dict) else None
    if isinstance(spec, dict) and spec:
        ds, derr = build_display(spec)
        if derr:
            return derr
        try:
            obj.primaryDisplaySettings = ds
        except Exception as e:
            return f"Primary display settings set failed: {e}"
    if secondary is not None:
        ds2, derr2 = build_display(secondary)
        if derr2:
            return "display.secondary: " + derr2
        try:
            obj.hasSecondaryDisplaySettings = True
            obj.secondaryDisplaySettings = ds2
        except Exception as e:
            return f"Secondary display settings set failed: {e}"
    return None


def set_leader_target(note, xyz, f):
    """Move a leader note's target point (where the leader meets the geometry) via
    setAnnotationTargetPoint; the bool and the re-read gate the claim. Returns (Point3D, error)."""
    try:
        x, y, z = (float(v) for v in xyz)
    except Exception:
        return None, "'leader_point' must be [x, y, z] numbers (model space, in 'units')."
    try:
        done = bool(note.setAnnotationTargetPoint(
            adsk.core.Point3D.create(x * f, y * f, z * f)))
    except Exception as e:
        return None, f"setAnnotationTargetPoint failed: {e}"
    if not done:
        return None, ("setAnnotationTargetPoint declined the point - it must lie on the "
                      "annotated geometry.")
    got = safe(lambda: note.annotationTargetPoint)
    if got is None:
        return None, "The leader point did not take (re-read returned nothing)."
    return got, None


# wire key -> hole note bool property; the flags object edits these in one call.
HOLE_FLAGS = {"quantity_note": "isWantQuantityNote", "all_matching": "isWantSelectAllMatchingHoles",
              "flip_normal": "isFlipHoleNormal", "through": "isThrough", "threaded": "isThreaded",
              "threaded_through": "isThreadedThrough",
              "show_imported_geometry": "isShowImportedGeometry"}


def apply_hole_flags(note, flags):
    """Apply a {wire_key: bool} flags dict to a hole/thread note; every set is re-read and a flag
    that did not take is an error. Returns (applied_dict, error)."""
    if not isinstance(flags, dict):
        return None, f"'flags' must be an object with any of: {', '.join(sorted(HOLE_FLAGS))}."
    applied = {}
    for key, val in flags.items():
        prop = HOLE_FLAGS.get(str(key).strip().lower())
        if prop is None:
            return None, f"Unknown flag '{key}'. Legal flags: {', '.join(sorted(HOLE_FLAGS))}."
        try:
            setattr(note, prop, bool(val))
        except Exception as e:
            return None, f"Flag '{key}' set failed: {e}"
        got = bool(safe(lambda p=prop: getattr(note, p), not bool(val)))
        if got != bool(val):
            return None, f"Flag '{key}'={val} did not take (re-read {got})."
        applied[key] = got
    return applied, None


def apply_hole_values(note, values, f):
    """Override a hole note's geometric values from a {wire_key: number or {value, tolerance}}
    dict (display units, countersink_angle_deg in degrees). Every refusal is decided before the
    first write; a failure DURING the writes stops at that key and leaves the earlier ones.
    Returns (applied_dict, error)."""
    import math
    if not isinstance(values, dict):
        return None, f"'values' must be an object with any of: {', '.join(HOLE_VALUE_PROPS)}."
    # PRE-PASS: resolve every key and refuse every refusable spec BEFORE the first write, so a
    # refused call leaves the note exactly as it was instead of half-applied.
    plan = []
    for key, spec in values.items():
        norm = str(key).strip().lower()
        attr = HOLE_VALUE_ATTR.get(norm)
        if attr is None:
            return None, f"Unknown value '{key}'. Legal values: {', '.join(HOLE_VALUE_PROPS)}."
        angle = norm == "countersink_angle_deg"
        num = spec.get("value") if isinstance(spec, dict) else spec
        tol_spec = spec.get("tolerance") if isinstance(spec, dict) else None
        # The coercion is decidable from the request alone, so it refuses HERE beside the other
        # four refusal classes rather than raising mid-write with earlier keys already applied.
        if num is not None:
            try:
                num = float(num)
            except (TypeError, ValueError):
                return None, (f"'{key}' must be a number (in 'units'"
                              + (", degrees" if angle else "") + f"), got {num!r}.")
        # The unit an angle BOUND is stored in is not measured, and the two candidates differ by
        # 57x, so this tool writes no angle tolerance at all rather than a possibly wrong one.
        if tol_spec is not None and angle:
            return None, (f"'{key}'.tolerance was not written - the unit Fusion stores an angle "
                          "tolerance in is unmeasured here, so any bound written could be wrong "
                          "by a factor of 57. Nothing was written. Add the angle tolerance in the "
                          "Fusion PMI dialog, or set a tolerance on a length value instead.")
        gv = safe(lambda a=attr: getattr(note, a))
        if gv is None:
            return None, (f"'{key}' is not readable on this note (not applicable to this "
                          "hole/boss shape).")
        tol = None
        if tol_spec is not None:
            # Free-standing object: building it here touches no note state, so a malformed spec
            # refuses with the earlier keys still unwritten.
            tol, terr = build_tolerance(tol_spec, f)
            if terr:
                return None, f"'{key}'.tolerance: {terr}"
        plan.append((key, attr, angle, num, tol, gv))

    applied = {}
    for key, attr, angle, num, tol, gv in plan:
        try:
            if num is not None:
                gv.value = math.radians(num) if angle else num * f
            if tol is not None:
                gv.tolerance = tol
            setattr(note, attr, gv)
        except Exception as e:
            return None, f"'{key}' set failed: {e}"
        back = value_record(safe(lambda a=attr: getattr(note, a)), 1.0 / f if not angle else 1.0,
                            angle=angle)
        if back is None:
            return None, f"'{key}' did not read back after the set."
        applied[key] = back
    return applied, None


def suppressed_pmi_features(d):
    """[(timeline_item, name)] for every SUPPRESSED timeline feature - the only handle a suppressed
    PMI can be reached through, since its timeline entity reads as a bare Feature whose NAME is its
    whole identity. Callers verify the annotation reappears in pmiAnnotations after unsuppressing
    rather than trusting this list."""
    out = []
    tl = safe(lambda: d.timeline)
    n = int(safe(lambda: tl.count, 0) or 0) if tl else 0
    for i in range(n):
        item = safe(lambda i=i: tl.item(i))
        if item is None or not safe(lambda: item.isSuppressed, False):
            continue
        nm = safe(lambda: item.entity.name)
        if nm:
            out.append((item, nm))
    return out


# A reference-failure message arrives with its count marked up in HTML, its sentences glued
# together, and a title plus the annotation's own NAME after the last sentence:
# 'Face 1 missingFace 2 missing<b>3 Reference Failures</b><br/>The model is... Lost' + 'ProbeNote'.
_MARKUP = re.compile(r"<[^>]*>")
_GLUED = re.compile(r"([a-z.])([A-Z])")
_TERMINATORS = ".!?"
_WARNING_LIMIT = 240


def readable_warning(msg, limit=_WARNING_LIMIT):
    """One readable line from a raw errorOrWarningMessage: markup dropped, the trailing title and
    entity name cut at the LAST sentence terminator (kept whole when there is none), glued
    sentences split on what remains, bounded with a trailing ' ...'. The ONE reader every consumer
    of a PMI warning goes through."""
    text = _MARKUP.sub(" ", msg or "")
    end = max(text.rfind(c) for c in _TERMINATORS)
    if end >= 0:
        text = text[:end + 1]
    text = " ".join(_GLUED.sub(r"\1 \2", text).split())
    return text[:limit].rstrip() + " ..." if len(text) > limit else text


def annotation_record(comp, ann):
    """The light per-annotation record every pmi_* read/verify reports: name, kind, component,
    visibility, warning flags only when set, and 'text' only when plainText reads (the property
    lives on PMILeaderLineNote and PMIHoleThreadNote, on no imported PMI class)."""
    rec = {
        "name": safe(lambda: ann.name),
        "kind": kind_of(ann),
        "component": safe(lambda: comp.name),
        "visible": bool(safe(lambda: ann.isVisible, False)),
    }
    text = safe(lambda: ann.plainText)
    if text is not None:
        rec["text"] = text
    if safe(lambda: ann.isOutOfDate, False):
        rec["out_of_date"] = True
    if safe(lambda: ann.isSuppressed, False):
        rec["suppressed"] = True
    msg = readable_warning(safe(lambda: ann.errorOrWarningMessage, "") or "")
    if msg:
        rec["warning"] = msg
    return rec
