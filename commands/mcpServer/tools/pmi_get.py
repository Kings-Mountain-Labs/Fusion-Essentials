# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""RICH READ: pmi_get - the design's PMI (3D annotations: notes, hole/thread callouts, imported
GD&T/dimensions/datums/surface textures) by zoom level. Default: counts by kind + light records.
include=['segments'|'detail'] deepens (detail is per-kind structured data); kind=/component=/
geometry= narrow. Design.pmiSettings is never read - the getter raises when no settings object
exists. Read-only."""

import adsk.core
import adsk.fusion

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item
from ..mcp_primitives.registry import register
from ._common import ok, error, safe
from . import _common
from . import _inputs
from . import _pmi

app = adsk.core.Application.get()

_SLICES = ("segments", "detail")

_MAX_RESULTS_DEFAULT = 50
_MAX_RESULTS_CAP = 200

_KINDS = ("note", "hole_note", "imported_dimension", "imported_note", "imported_gdt_datum",
          "imported_geometric_tolerance", "imported_surface_texture", "imported_graphical",
          "imported_folder")

_GEOMETRY = _inputs.GeometryHandleList("geometry", require="any")
_KIND_FILTER = _inputs.Choice("kind", options=list(_KINDS))


def _normalize_include(include):
    if include in (None, "", []):
        return []
    if isinstance(include, str):
        return [s.strip().lower() for s in include.split(",") if s.strip()]
    return [str(s).strip().lower() for s in include]


def _row_cap(max_results) -> int:
    """The record cap this call runs under: an absent/zero/unparseable request falls back to
    _MAX_RESULTS_DEFAULT, anything else is clamped into 1.._MAX_RESULTS_CAP rather than refused."""
    try:
        n = int(max_results or _MAX_RESULTS_DEFAULT)
    except (TypeError, ValueError):
        n = _MAX_RESULTS_DEFAULT
    return max(1, min(n, _MAX_RESULTS_CAP))


def _slice_segments(rec, ann):
    """The created annotation's {symbol} markup - round-trips into pmi_create/pmi_edit text."""
    markup = _pmi.segments_markup(ann)
    if markup is not None:
        rec["markup"] = markup


# ── per-kind detail builders ────────────────────────────────────────────────

def _detail_common(rec, ann):
    rec["parametric"] = bool(safe(lambda: ann.isParametric, False))
    tl = safe(lambda: ann.timelineObject)
    if tl is not None:
        rec["timeline_index"] = safe(lambda: tl.index)
    refs = safe(lambda: ann.referencedEntities) or []
    rec["referenced_entities"] = [
        (safe(lambda r=r: r.objectType, "") or "").split("::")[-1] for r in refs]


def _detail_created(rec, ann, out_f):
    """Placement + format shared by both Fusion-authored kinds."""
    tp = safe(lambda: ann.annotationTextPoint)
    if tp is not None:
        rec["text_point"] = _common.ptxyz(tp, out_f)
    lp = safe(lambda: ann.annotationTargetPoint)
    if lp is not None:
        rec["leader_point"] = _common.ptxyz(lp, out_f)
    rec["align"] = _pmi.enum_label(adsk.core, "HorizontalAlignments", "HorizontalAlignment",
                                   safe(lambda: ann.horizontalAlignment))
    rec["valign"] = _pmi.enum_label(adsk.core, "VerticalAlignments", "VerticalAlignment",
                                    safe(lambda: ann.verticalAlignment))
    if safe(lambda: ann.isPerpendicularLine, False):
        rec["perpendicular"] = True
    ext = safe(lambda: ann.leaderLineExtension)
    if ext is not None:
        rec["leader_extension"] = round(ext * out_f, 6)


def _detail_note(rec, ann, out_f):
    rec["plane"] = _pmi.enum_label(adsk.fusion, "LeaderLineNotePlaneTypes",
                                   "LeaderLineNotePlaneType",
                                   safe(lambda: ann.annotationPlaneType))
    supported = list(safe(lambda: ann.supportedAnnotationPlaneTypes) or [])
    if supported:
        rec["supported_planes"] = [
            _pmi.enum_label(adsk.fusion, "LeaderLineNotePlaneTypes", "LeaderLineNotePlaneType", v)
            for v in supported]


def _detail_hole_note(rec, ann, out_f):
    # is_hole rides only when isHoleAnnotation READS: a failed read defaulted to True publishes
    # "this callout annotates a hole" as though it had been measured.
    is_hole = safe(lambda: ann.isHoleAnnotation)
    if is_hole is not None:
        rec["is_hole"] = bool(is_hole)
    rec["quantity"] = safe(lambda: ann.quantity)
    for key, on in (("is_through", "isThrough"), ("is_threaded", "isThreaded"),
                    ("threaded_through", "isThreadedThrough"),
                    ("quantity_note", "isWantQuantityNote"),
                    ("all_matching", "isWantSelectAllMatchingHoles"),
                    ("flip_normal", "isFlipHoleNormal")):
        rec[key] = bool(safe(lambda o=on: getattr(ann, o), False))
    for key in _pmi.HOLE_VALUE_PROPS:
        angle = key == "countersink_angle_deg"
        vr = _pmi.value_record(safe(lambda k=key: getattr(ann, _pmi.HOLE_VALUE_ATTR[k])),
                               out_f, angle=angle)
        if vr is not None:
            rec[key] = vr
    st = _pmi.tolerance_record(safe(lambda: ann.shaftTolerance), out_f)
    if st:
        rec["shaft_tolerance"] = st
    ti = safe(lambda: ann.threadInfo)
    if ti is not None:
        rec["thread"] = {"designation": safe(lambda: ti.threadDesignation),
                         "thread_class": safe(lambda: ti.threadClass),
                         "thread_type": safe(lambda: ti.threadType)}
    ds = _pmi.display_record(safe(lambda: ann.primaryDisplaySettings))
    if ds:
        rec["display"] = ds
    if safe(lambda: ann.hasSecondaryDisplaySettings, False):
        ds2 = _pmi.display_record(safe(lambda: ann.secondaryDisplaySettings))
        if ds2:
            rec["display_secondary"] = ds2


def _detail_imported_dimension(rec, ann, out_f):
    nv = _pmi.value_record(safe(lambda: ann.nominalDistance), out_f)
    if nv is not None:
        rec["nominal"] = nv
    st = _pmi.tolerance_record(safe(lambda: ann.shaftTolerance), out_f)
    if st:
        rec["shaft_tolerance"] = st
    rec["angle_relator"] = _pmi.enum_label(adsk.fusion, "PMIAngleRelatorTypes",
                                           "PMIAngleRelatorType",
                                           safe(lambda: ann.angleRelatorType))


def _detail_imported_geometric_tolerance(rec, ann, out_f):
    tv = safe(lambda: ann.tolerance)
    if tv is not None:
        rec["tolerance"] = round(tv * out_f, 6)
    datums = []
    for dr in safe(lambda: ann.datumReferences) or []:
        ref = safe(lambda dr=dr: dr.referenceDatum)
        entry = {"datum": safe(lambda: ref.label) if ref is not None else None}
        mods = []
        for m in safe(lambda dr=dr: dr.modifiers) or []:
            mrec = {"type": _pmi.enum_label(adsk.fusion, "PMIDatumModifierTypes",
                                            "PMIDatumModifierType", safe(lambda m=m: m.type))}
            if safe(lambda m=m: m.hasValue, False):
                mrec["value"] = safe(lambda m=m: m.value)
            mods.append(mrec)
        if mods:
            entry["modifiers"] = mods
        datums.append(entry)
    if datums:
        rec["datum_references"] = datums


def _detail_imported_gdt_datum(rec, ann, out_f):
    rec["label"] = safe(lambda: ann.label)
    targets = []
    for t in safe(lambda: ann.datumTargets) or []:
        trec = {"id": safe(lambda t=t: t.targetId),
                "type": _pmi.enum_label(adsk.fusion, "PMIDatumTargetTypes", "PMIDatumTargetType",
                                        safe(lambda t=t: t.type))}
        if safe(lambda t=t: t.hasPointTarget, False):
            trec["point"] = _common.ptxyz(safe(lambda t=t: t.pointTarget), out_f)
        lens = list(safe(lambda t=t: t.lengths) or [])
        if lens:
            trec["lengths"] = [round(v * out_f, 6) for v in lens]
        targets.append(trec)
    if targets:
        rec["datum_targets"] = targets


def _detail_imported_note(rec, ann, out_f):
    rec["note_text"] = safe(lambda: ann.note)
    ref = safe(lambda: ann.reference)
    if ref is not None:
        rec["references_pmi"] = safe(lambda: ref.name)


def _roughness(r):
    if r is None or not safe(lambda: r.hasValue, False):
        return None
    return {"value": safe(lambda: r.value),
            "parameter": _pmi.enum_label(adsk.fusion, "PMISurfaceTextureParameterTypes",
                                         "PMISurfaceTextureParameterType",
                                         safe(lambda: r.parameterType)),
            "is_maximum": bool(safe(lambda: r.isMaximum, False))}


def _detail_imported_surface_texture(rec, ann, out_f):
    rec["standard"] = _pmi.enum_label(adsk.fusion, "PMISurfaceTextureStandardTypes",
                                      "PMISurfaceTextureStandardType", safe(lambda: ann.standard))
    rec["texture_type"] = _pmi.enum_label(adsk.fusion, "PMISurfaceTextureTypes",
                                          "PMISurfaceTextureType",
                                          safe(lambda: ann.surfaceTextureType))
    rec["lay"] = _pmi.enum_label(adsk.fusion, "PMILaySymbolTypes", "PMILaySymbolType",
                                 safe(lambda: ann.laySymbolType))
    if safe(lambda: ann.hasRoughness, False):
        rec["roughness_um"] = safe(lambda: ann.roughness)
    if safe(lambda: ann.hasRoughnessLimits, False):
        rec["roughness_min_um"] = safe(lambda: ann.minimumRoughness)
        rec["roughness_max_um"] = safe(lambda: ann.maximumRoughness)
    mm = safe(lambda: ann.machineMethod)
    if mm:
        rec["machine_method"] = mm
    if safe(lambda: ann.hasProcessingAllowance, False):
        rec["processing_allowance"] = safe(lambda: ann.processingAllowance)
    for key, get in (("cutoff", lambda: ann.cutoff), ("waviness", lambda: ann.waviness),
                     ("secondary_roughness", lambda: ann.secondaryRoughness),
                     ("tertiary_roughness", lambda: ann.tertiaryRoughness)):
        rr = _roughness(safe(get))
        if rr:
            rec[key] = rr


def _detail_imported_folder(rec, ann, out_f):
    rec["contains"] = [safe(lambda a=a: a.name) for a in safe(lambda: ann.containedPMI) or []]


_DETAIL_BY_KIND = {
    "note": _detail_note,
    "hole_note": _detail_hole_note,
    "imported_dimension": _detail_imported_dimension,
    "imported_geometric_tolerance": _detail_imported_geometric_tolerance,
    "imported_gdt_datum": _detail_imported_gdt_datum,
    "imported_note": _detail_imported_note,
    "imported_surface_texture": _detail_imported_surface_texture,
    "imported_folder": _detail_imported_folder,
}


def _slice_detail(rec, ann, out_f):
    """Health + references + per-kind structured data (values/tolerances scale to 'units')."""
    _detail_common(rec, ann)
    kind = rec.get("kind")
    if kind in _pmi.CREATED_KINDS:
        _detail_created(rec, ann, out_f)
    builder = _DETAIL_BY_KIND.get(kind)
    if builder:
        builder(rec, ann, out_f)


def handler(include=None, geometry=None, kind="", component="", max_results=None,
            units="mm") -> dict:
    """See TOOL_DESCRIPTION."""
    d = _common.design()
    if not d:
        return error("No active design. Create or open a document first (see doc_new).")
    f = _common.scale(units)
    if f is None:
        return error(f"Unknown units '{units}'. Use mm, cm, or in.")
    out_f = _common.CM_TO_UNIT[(units or "mm").strip().lower()]

    inc = _normalize_include(include)
    bad = [s for s in inc if s not in _SLICES]
    if bad:
        return error(f"Unknown include slice(s) {bad}. Available: {list(_SLICES)}.")
    kind_v, kerr = _KIND_FILTER.resolve(kind)
    if kerr:
        return error(kerr)
    comp_want = (component or "").strip().lower()

    cap = _row_cap(max_results)

    # geometry= narrows via each collection's own itemsByEntities query, keyed by (component,
    # name): an adsk collection hands out a fresh wrapper per access, so an identity intersection
    # would silently return nothing.
    only = None
    if geometry:
        ents, gerr = _GEOMETRY.resolve(geometry)
        if gerr:
            return error(gerr)
        only = set()
        for comp in _common.all_components(d):
            coll = safe(lambda c=comp: c.pmiAnnotations)
            if coll is None:
                continue
            cname = safe(lambda c=comp: c.name)
            for a in safe(lambda: coll.itemsByEntities(ents)) or []:
                only.add((cname, safe(lambda a=a: a.name)))

    by_kind = {}
    records = []
    total = 0
    truncated = False
    walk_holes = {}
    for comp, ann in _pmi.walk_annotations(d, walk_holes):
        cname = safe(lambda: comp.name)
        if comp_want and (cname or "").lower() != comp_want:
            continue
        if only is not None and (cname, safe(lambda: ann.name)) not in only:
            continue
        akind = _pmi.kind_of(ann)
        if kind_v and akind != kind_v:
            continue
        total += 1
        by_kind[akind] = by_kind.get(akind, 0) + 1
        if len(records) >= cap:
            truncated = True
            continue
        rec = _pmi.annotation_record(comp, ann)
        if "segments" in inc:
            _slice_segments(rec, ann)
        if "detail" in inc:
            _slice_detail(rec, ann, out_f)
        records.append(rec)

    out = {"total": total, "by_kind": by_kind, "annotations": records, "units": units}
    if truncated:
        out["truncated"] = True
    # 'total'/'by_kind' count what the walk could READ; the holes ride beside them so a partial
    # design is never handed over as the whole one. 'truncated' covers only the row cap.
    comps_bad = walk_holes.get("components_unreadable", 0)
    items_bad = walk_holes.get("items_unreadable", 0)
    if comps_bad or items_bad:
        out["components_unreadable"] = comps_bad
        out["items_unreadable"] = items_bad
        out["incomplete_note"] = (
            f"{comps_bad} component(s) and {items_bad} annotation(s) could not be read, so 'total' "
            "and 'by_kind' count only what was reachable - the design may hold more PMI than this.")
    if only is not None:
        # The geometry= intersection is keyed by (component, name) - see the resolution above - so
        # two annotations SHARING a name in one component both pass when either one matched.
        out["filter_identity"] = "component+name"
        out["filter_note"] = (
            "geometry= matched by (component, name), not by object identity. If one component "
            "holds two annotations with the same name, a match on either publishes both - compare "
            "the rows before acting on one.")
    # Every SUPPRESSED timeline feature by name: a suppressed PMI leaves the collections above
    # with only its timeline name surviving, and the list is unfiltered because nothing read here
    # says which rows are the PMI.
    suppressed = [nm for _item, nm in _pmi.suppressed_pmi_features(d)]
    if suppressed:
        out["suppressed_features"] = suppressed
        out["suppressed_note"] = ("Every suppressed timeline feature. A PMI suppressed here is "
                                  "expected to be among them and not in 'annotations' above "
                                  "(unconfirmed on this Fusion build); these names are not "
                                  "narrowed to PMI. Bring one back with "
                                  "pmi_edit(action='unsuppress', annotation=<name>), which "
                                  "verifies the annotation reappeared.")
    remaining = [s for s in _SLICES if s not in inc]
    if remaining:
        out["note"] = ("Light records. Pull deeper with include=" + str(remaining) +
                       " - 'segments' adds the {symbol} markup (round-trips into pmi_create/"
                       "pmi_edit text), 'detail' adds per-kind structure (placement/format, hole "
                       "values+tolerances+thread+display, imported dimension/GDT/datum/surface-"
                       "texture data). kind=/component=/geometry= narrow.")
    return ok(out)


TOOL_DESCRIPTION = (
"Read the design's PMI - Product Manufacturing Information, the 3D annotations on model geometry, "
"authored and imported alike: counts by kind plus light records, 'include' deepens. Author PMI "
"with pmi_create."
)

tool = (
    Tool.create_simple(name="pmi_get", description=TOOL_DESCRIPTION)
    .add_input_property("include", {
        "type": "array", "items": {"type": "string", "enum": list(_SLICES)}})
    .add_input_property("geometry", _GEOMETRY.schema())
    .add_input_property("kind", _KIND_FILTER.schema())
    .add_input_property("component", {"type": "string",
        "description": "One component's exact name."})
    .add_input_property("max_results", {
        "type": "integer",
        "description": f"Default {_MAX_RESULTS_DEFAULT}, max {_MAX_RESULTS_CAP}."})
    .add_input_property(*_inputs.UNITS.as_property())
    .strict_schema()
)
item = Item.create_tool_item(tool=tool, write="read", handler=handler, run_on_main_thread=True)


def register_tool():
    register(item)
