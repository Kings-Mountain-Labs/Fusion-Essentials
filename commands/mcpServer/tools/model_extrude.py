# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""MCP building block: extrude a sketch profile into a solid (the back half of the modelling flow).

  extrude -> turn a closed sketch profile into a 3D body by extruding it a distance, or up to a
             face. Choose the operation, distance/taper, and optional surface (no end caps). WRITES.
             Companion to sketch_create / sketch_add_geometry.
"""

import adsk.core
import adsk.fusion

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import error, ok, safe, scale, target_component, root_body_advisory
from . import _common
from . import _geom
from . import _inputs
from . import _sketch_detail
from . import _assert

app = adsk.core.Application.get()

# to_object: extrude UP TO a face (handle) instead of a blind distance.
_TO_OBJECT = _inputs.GeometryHandle("to_object", require="face", required=False,
    description="Extrude up to this face.")
# target_bodies: scope a cut/join/intersect to these bodies so it doesn't bleed through others.
_TARGET_BODIES = _inputs.BodyRefList("target_bodies", required=False)

# extent: the depth STYLE. 'distance' is the legacy default (distance/symmetric/taper_deg); the other
# three map to measured ExtrudeFeatureInput setters, each reading its own inputs below.
_EXTENTS = ("distance", "through_all", "to_face", "two_side")
_EXTENT = _inputs.Choice("extent", _EXTENTS, default="distance")

# profile_index may carry a profile HANDLE (entityToken from sketch_get) - resolved via ProfileRef.
# _inputs.is_handle distinguishes a handle from an int/list/'all' selector.
_PROFILE = _inputs.ProfileRef("profile_index", scope_input="component")
_looks_like_handle = _inputs.is_handle

# profile_index may instead carry a sketch TEXT address ('text:<i>'). ExtrudeFeatures.createInput
# takes "a single SketchText object" as a profile, so a nameplate sketch holding no closed profile
# extrudes as itself. allow_text is on THIS resolution alone - the handle/index path refuses a text.
_TEXT_PROFILE = _inputs.ProfileRef("profile_index", allow_text=True, scope_input="component")
_is_text_ref = _inputs.is_text_ref


def _text_ref_in_multi(profile_index):
    """The sketch-TEXT address hiding inside a MULTI-region selector - a list entry, or one field of
    a comma string - or None. The multi forms address CLOSED profiles by index and a text carries no
    index in that space, so a mixed selector is refused rather than silently dropping the text."""
    if isinstance(profile_index, (list, tuple)):
        items = profile_index
    elif isinstance(profile_index, str) and "," in profile_index and not _is_text_ref(profile_index):
        items = profile_index.split(",")
    else:
        return None
    for x in items:
        if _is_text_ref(x):
            return x
    return None


def _is_all_selector(profile_index) -> bool:
    """True for the 'all' (or '*') profile selector - one spelling shared by the resolver below and
    the result note, so the two can never disagree about what 'all' was."""
    return isinstance(profile_index, str) and profile_index.strip().lower() in ("all", "*")


def _resolve_profile_indices(profile_index, pcount, profiles=None):
    """Normalise the profile_index selector (int, list, '0,2,3' or 'all') to a sorted list of
    in-range indices, or (None, error)."""
    sel = profile_index
    if isinstance(sel, str):
        s = sel.strip().lower()
        if _is_all_selector(sel):
            return list(range(pcount)), None
        try:
            sel = [int(x) for x in s.split(",") if x.strip() != ""]
        except Exception:
            return None, (f"profile_index '{profile_index}' is not an int, list, 'all', or '0,1,2'. "
                          "To target a specific region, pass a profile handle from sketch_get instead.")
    if isinstance(sel, (list, tuple)):
        idxs = []
        for x in sel:
            try:
                idxs.append(int(x))
            except Exception:
                return None, f"profile_index list has a non-integer entry: {x!r}."
    else:
        try:
            idxs = [int(sel)]
        except Exception:
            idxs = [0]
    idxs = sorted(set(idxs))
    bad = [i for i in idxs if i < 0 or i >= pcount]
    if bad:
        return None, (f"profile_index {bad} out of range - sketch has {pcount} profile(s) "
                      f"(0..{pcount-1}).")
    return (idxs or [0]), None


# A region 'all' selected is ENCLOSED when it fills a HOLE of another selected region: a frame and
# its bays come out of ONE sketch as a profile with an inner loop per bay PLUS a profile per bay. A
# ProfileLoop carries no bounding box, so a hole's extent is the union of its curves' boxes.
_ENCLOSED_TOL_CM = 1e-6


def _extent3(bbox):
    """(xmin, ymin, zmin, xmax, ymax, zmax) of a BoundingBox3D, or None unless all six read as real
    numbers. All THREE axes are kept: a sketch on a vertical plane holds one axis constant for every
    region in it, so dropping an axis would call a far-away region enclosed."""
    mn, mx = safe(lambda: bbox.minPoint), safe(lambda: bbox.maxPoint)
    if mn is None or mx is None:
        return None
    lo = (safe(lambda: mn.x), safe(lambda: mn.y), safe(lambda: mn.z))
    hi = (safe(lambda: mx.x), safe(lambda: mx.y), safe(lambda: mx.z))
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in lo + hi):
        return None
    return (tuple(min(a, b) for a, b in zip(lo, hi))
            + tuple(max(a, b) for a, b in zip(lo, hi)))


def _union(a, b):
    """The extent covering both - how a loop's curves add up to the loop's own extent."""
    return (tuple(min(a[k], b[k]) for k in range(3))
            + tuple(max(a[k], b[k]) for k in range(3, 6)))


def _hole_extents(profile):
    """The extent of each HOLE (a loop with isOuter false) of one profile - the openings another
    selected profile can fill. None when ANY read behind them fails, since a dropped hole would
    publish a false 'nothing is enclosed'."""
    loops = safe(lambda: profile.profileLoops)
    n = safe(lambda: loops.count) if loops is not None else None
    if not isinstance(n, int) or isinstance(n, bool):
        return None
    out = []
    # EVERY loop (and every curve of it below) must read or the verdict is withdrawn (None), so these
    # stay positional walks: iter_collection drops an unreadable member, which would answer with the
    # holes that happened to read instead of declining.
    for i in range(n):
        loop = safe(lambda i=i: loops.item(i))
        if loop is None:
            return None
        outer = safe(lambda loop=loop: loop.isOuter)
        if not isinstance(outer, bool):     # unreadable: neither 'a hole' nor 'not a hole'
            return None
        if outer:
            continue
        curves = safe(lambda loop=loop: loop.profileCurves)
        cn = safe(lambda: curves.count) if curves is not None else None
        if not isinstance(cn, int) or isinstance(cn, bool):
            return None
        box = None
        for j in range(cn):
            cb = _extent3(safe(lambda j=j: curves.item(j).boundingBox))
            if cb is None:
                return None
            box = cb if box is None else _union(box, cb)
        if box is None:                     # an inner loop no curve of which could be measured
            return None
        out.append(box)
    return out


def _within(inner, outer):
    """True when the `inner` extent sits inside `outer` on every axis (tolerance-inclusive, since a
    bay's own extent and the hole it fills are the same curves)."""
    return (all(inner[k] >= outer[k] - _ENCLOSED_TOL_CM for k in range(3))
            and all(inner[k] <= outer[k] + _ENCLOSED_TOL_CM for k in range(3, 6)))


def _enclosed_regions(profiles, indices):
    """The SELECTED profile indices that fill a hole of ANOTHER selected profile - the regions 'all'
    took sight-unseen. None (no verdict) when any selected profile's extent or loops cannot be read:
    the note then says what it could not rule out instead of claiming a count it did not measure."""
    extents, holes = {}, {}
    for i in indices:
        p = safe(lambda i=i: profiles.item(i))
        if p is None:
            return None
        extent, hole = _extent3(safe(lambda p=p: p.boundingBox)), _hole_extents(p)
        if extent is None or hole is None:
            return None
        extents[i], holes[i] = extent, hole
    return [i for i in indices
            if any(_within(extents[i], h) for j in indices if j != i for h in holes[j])]


def _through_all_direction_key(symmetric, distance):
    """'positive' | 'negative' | 'symmetric' - which way extent='through_all' cuts. The SIGN of
    'distance' (its magnitude is unused for through_all) picks a one-sided direction - the same
    'negative reverses' convention 'distance' already carries for a blind extrude; symmetric=true
    goes both ways. 0/positive 'distance' defaults to the profile-normal (positive) direction."""
    if symmetric:
        return "symmetric"
    try:
        if distance and float(distance) < 0:   # a non-numeric expression carries no sign hint
            return "negative"
    except (TypeError, ValueError):
        pass
    return "positive"


def _qualified_body_name(body):
    """The body's name qualified with its owning component or occurrence path, so two bodies sharing
    Fusion's default 'Body1' read distinctly in the 'scoped_to_bodies' echo."""
    if body is None:
        return None
    name = safe(lambda: body.name)
    ctx = _inputs._body_context(body)
    if name and ctx and ctx not in ("?", name):
        return f"{ctx}/{name}"
    return name


def _solo_solid_body(comp):
    """The SOLE solid body directly in `comp` - the implied through_all cut/intersect target when
    'target_bodies' wasn't given (never guessed when there are zero or several bodies; Fusion's own
    intersection search still runs regardless - this only backs the pre/post volume proof below)."""
    solids = [b for b in _common.iter_collection(safe(lambda: comp.bRepBodies))
              if safe(lambda b=b: b.isSolid)]
    return solids if len(solids) == 1 else []


def _solid_bodies_snapshot(design):
    """(body, name, component_name, volume) for EVERY solid body in the design - the pre-image a
    cut/intersect reads back against. feature.bodies returns only the feature's OWN-component result
    body, so it cannot reveal a cut that bled into a co-located component; this read can."""
    snap = []
    for comp in _common.all_components(design):
        cname = safe(lambda c=comp: c.name)
        for b in _common.iter_collection(safe(lambda c=comp: c.bRepBodies)):
            if safe(lambda b=b: b.isSolid):
                snap.append((b, safe(lambda b=b: b.name), cname, _geom.signed_volume(b)))
    return snap


def _affected_bodies(snap):
    """From a pre-cut snapshot, the (name, component_name, removed_cm3) rows whose solid volume DROPPED
    (or whose body was consumed whole) once the feature ran - the bodies a cut/intersect actually acted
    on. 'removed_cm3' is None for a consumed body (e.g. an intersect that kept no overlap)."""
    out = []
    for b, name, cname, v0 in snap:
        if v0 is None:
            continue
        v1 = safe(lambda b=b: b.volume)
        if v1 is None:
            out.append((name, cname, None))          # body consumed
        elif v0 - v1 > _common.NO_VOLUME_CHANGE_CM3:   # material removed
            out.append((name, cname, round(v0 - v1, 6)))
    return out


def _solid_count(design) -> int:
    """The design-wide SOLID body count - the census a cut/intersect is judged against beside the
    volume diff: it RISES when the cut disconnects the target into pieces and FALLS when a body is
    consumed whole, so an unmoved census PLUS no volume drop is what proves nothing happened."""
    n = 0
    for comp in _common.all_components(design):
        for b in _common.iter_collection(safe(lambda c=comp: c.bRepBodies)):
            if safe(lambda b=b: b.isSolid):
                n += 1
    return n


def _effect_rows(affected) -> str:
    """ASCII 'what actually changed' phrase over the affected-bodies rows - what a refusal states
    before it decides whether the feature may be removed at all."""
    rows = []
    for name, cname, removed in affected:
        where = f" in {cname}" if cname else ""
        rows.append(f"{name or '?'}{where}"
                    + (" (consumed)" if removed is None else f" (-{removed} cm3)"))
    return ", ".join(rows)


def _roll_back(design, feature, fname) -> str:
    """Remove a just-created extrude that failed or changed nothing, and return the ASCII sentence a
    RE-READ backs - deleteMe's own answer is not proof, the timeline count is. A rollback that did
    not take names what REMAINS, so the caller is never told the design is clean while it is not."""
    def _count(tl):
        c = safe(lambda: tl.count) if tl is not None else None
        return c if isinstance(c, int) and not isinstance(c, bool) else None

    tl = safe(lambda: design.timeline)
    before = _count(tl)
    try:
        did = feature.deleteMe()
    except Exception as e:
        return (f" '{fname}' could NOT be rolled back ({e}) - it REMAINS in the timeline; remove it "
                "with design_delete_feature.")
    after = _count(tl)
    if before is not None and after is not None:
        if after < before:
            return (f" The feature has been rolled back - the timeline re-reads {after} item(s), "
                    f"down from {before}.")
        return (f" '{fname}' REMAINS in the timeline ({after} item(s) after the rollback, deleteMe "
                f"returned {bool(did)}) - remove it with design_delete_feature.")
    return (f" A rollback of '{fname}' ran (deleteMe returned {bool(did)}) but the timeline could "
            "not be re-read to confirm it - check with design_get.")


def _distance_missing(distance) -> bool:
    """True when 'distance' supplies no extrude depth: None, blank, or a literal 0. A non-numeric string
    is an EXPRESSION (a real depth), and any non-zero number is a real depth."""
    if distance is None:
        return True
    if isinstance(distance, str):
        s = distance.strip()
        if not s:
            return True
        try:
            return float(s) == 0
        except ValueError:
            return False        # an expression string - a real depth
    try:
        return float(distance) == 0
    except (TypeError, ValueError):
        return True


def _feature_parameters(feature) -> dict:
    """The model parameters (dNN) this extrude created, keyed distance / distance2 / taper / taper2,
    so param_set can retarget it. A key is present only when that ModelParameter exists - a
    through_all / to_face extent has no distance parameter."""
    out = {}

    def pname(getter):
        p = safe(getter)
        return safe(lambda: p.name) if p is not None else None

    ext1 = safe(lambda: feature.extentOne)
    d1 = pname(lambda: ext1.distance) if ext1 is not None else None
    if d1:
        out["distance"] = d1
    if safe(lambda: feature.hasTwoExtents, False):
        ext2 = safe(lambda: feature.extentTwo)
        d2 = pname(lambda: ext2.distance) if ext2 is not None else None
        if d2:
            out["distance2"] = d2
    t1 = pname(lambda: feature.taperAngleOne)
    if t1:
        out["taper"] = t1
    t2 = pname(lambda: feature.taperAngleTwo)
    if t2:
        out["taper2"] = t2
    return out


def _depth_mismatch(fname, what, got_cm, want_cm, raw, k, units) -> str:
    """The refusal for a landed depth that disagrees with the request. ONE wording for every side,
    so a two-sided extrude and a blind one cannot describe the same failure differently. `what`
    names WHICH depth read back; the feature has landed, so the sentence says what remains and how
    to remove it, and an expression request names itself beside the number it evaluated to."""
    asked = f"{round(want_cm / k, 6)} {units}"
    if _inputs.looks_like_expression(raw):
        asked += f" (what the expression '{str(raw).strip()}' evaluates to)"
    return (f"Extrude built '{fname}' but its {what} reads back {round(got_cm / k, 6)} {units}, "
            f"not the requested {asked}. '{fname}' REMAINS in the timeline - inspect it with "
            "design_get and remove it with design_delete_feature.")


def _no_target_body_hint(ext_key, distance, symmetric=False) -> str:
    """The remedy for an extrude Fusion answered with no body to reach: which way this one went and
    the sign that reverses it - or, going both ways already, that no sign can reach one."""
    negative = None
    try:
        negative = float(distance) < 0
    except (TypeError, ValueError):
        pass
    flip_to = "POSITIVE" if negative else "NEGATIVE"
    # Symmetric is read FIRST: a symmetric extent never reads the sign at all, so a direction remedy
    # would send the caller after a knob that changed nothing.
    if symmetric:
        return (" This extrude already goes BOTH ways from the sketch plane, so no 'distance' sign "
                "reaches a body it missed - the profile overlaps no participant body. Check where "
                "the profile sits, and 'target_bodies' if it names any.")
    if ext_key == "through_all":
        return (" extent=through_all follows the sketch-plane normal; a sketch ON a body's face "
                "points AWAY from the material, so this direction hits only air. Pass a "
                f"{flip_to} 'distance' to cut the other way into the body.")
    went = "" if negative is None else f" - 'distance' was {'negative' if negative else 'positive'}"
    return (f" The extrude reached no body the way it went{went}, and a sketch ON a body's face "
            f"points its normal AWAY from the material. Pass a {flip_to} 'distance' to go the "
            "other way, or check the profile overlaps the body at all.")


def handler(sketch_name: str = "", profile_index=0, distance: float = 0.0,
            units: str = "mm", operation: str = "new", symmetric: bool = False,
            taper_deg: float = 0.0, to_object: str = "", target_bodies=None,
            as_surface: bool = False, extent: str = "distance", distance2: float = 0.0,
            component: str = "") -> dict:
    """See TOOL_DESCRIPTION."""
    k = scale(units)
    if k is None:
        return error(f"Unknown units '{units}'. Use mm, cm, or in.")
    ext_key = (extent or "distance").strip().lower()
    if ext_key not in _EXTENTS:
        return error(f"Unknown extent '{extent}'. Use: {', '.join(_EXTENTS)}.")
    has_to_object = bool((to_object or "").strip())
    # 'to_object' implies the to-face extent when 'extent' is left at its default ('distance' - the
    # original shorthand, kept back-compat) or when 'to_face' is explicitly requested.
    use_to_object = has_to_object and ext_key in ("distance", "to_face")
    if has_to_object and not use_to_object:
        return error(f"'to_object' is not used with extent='{ext_key}'. Drop 'to_object', or use "
                     "extent='to_face' (or the default 'distance').")
    if ext_key == "to_face" and not use_to_object:
        return error("extent='to_face' needs 'to_object' (a find_geometry face handle).")
    if ext_key == "distance" and _distance_missing(distance) and not use_to_object:
        return error("Provide a non-zero 'distance' to extrude, or 'to_object' to extrude up to a face.")
    if ext_key == "two_side":
        if _distance_missing(distance) or _distance_missing(distance2):
            return error("extent='two_side' needs non-zero 'distance' and 'distance2' (one per side).")
        if symmetric:
            return error("extent='two_side' does not use 'symmetric' - pass equal 'distance' and "
                         "'distance2' for a symmetric two-sided extrude, or use extent='distance' "
                         "with symmetric=true.")
    op_key = (operation or "new").strip().lower()
    if op_key not in _common.OPERATIONS:
        return error(f"Unknown operation '{operation}'. Use: new, join, cut, intersect.")

    design = _common.design()
    if not design:
        return error("No active design. Create or open a document first (see doc_new).")

    root = target_component(design)
    selected_profile = None
    if _looks_like_handle(profile_index):
        selected_profile, perr = _PROFILE.resolve(profile_index, component)
        if perr:
            return error(perr)
        sketch = safe(lambda: selected_profile.parentSketch)
        if sketch is None:
            return error("profile_index resolved a profile whose parent sketch could not be read.")
    else:
        sketch, requested, ambiguous = _sketch_detail.scoped_or_recent_sketch(
            design, sketch_name, component)
        if ambiguous:
            return error(ambiguous)
        if not sketch:
            if requested:
                names = _common.all_sketch_names(design)
                avail = f" Available: {', '.join(names)}." if names else ""
                return error(f"No sketch named '{requested}'.{avail} Use sketch_get or sketch_create.")
            return error("No sketch to extrude. Create one and draw a closed profile first.")

    profiles = safe(lambda: sketch.profiles)
    pcount = safe(lambda: profiles.count, 0) if profiles else 0

    # A sketch TEXT address selects the text itself, which is what a text-only sketch carries INSTEAD
    # of a closed profile - so it is read BEFORE the zero-profile routing below, which would
    # otherwise send a nameplate sketch down the open-curve surface path and dead-end there.
    mixed_text = _text_ref_in_multi(profile_index)
    if mixed_text:
        return error(f"profile_index mixes the sketch text '{mixed_text}' with other regions. A "
                     f"sketch text extrudes on its own - pass just '{mixed_text}', and a separate "
                     "call for the closed regions.")
    text_addr = profile_index.strip() if _is_text_ref(profile_index) else None
    if text_addr and as_surface:
        return error(f"as_surface is not used with the sketch text '{text_addr}' - a text extrudes "
                     "as a solid. Drop as_surface, or pass a closed profile / an open path.")

    # `pcount` is read off the sketch, so a DEFERRED one can read 0 while holding regions - the AUTO
    # clause below must not choose the feature type off that count. A forced as_surface still runs:
    # it builds from the sketch's curves, which are current under a deferral.
    stale = _inputs.deferred_sketch_refusal("profile_index", sketch)

    # SURFACE path: forced via as_surface, OR auto when there is no closed profile but the sketch has
    # open curves. Build an OPEN profile and set ExtrudeFeatureInput.isSolid = False (no end caps).
    want_surface = bool(as_surface) or (pcount == 0 and not text_addr and not stale)
    open_surface = False
    indices = [0]
    took_all = False
    if want_surface:
        profile_arg, perr = _common.open_profile_from_sketch(
            safe(lambda: sketch.parentComponent) or root, sketch, "for a surface extrude",
            no_curves_error=(f"Sketch '{safe(lambda: sketch.name)}' has no curves to extrude as a "
                             "surface. Draw an open path (a line/arc) or a closed region first."))
        if perr:
            # No closed profile AND no open curves -> the original dead-end, but now points at the
            # surface path so the agent knows as_surface exists.
            if pcount == 0:
                return error(perr)
            # as_surface was forced but no open curves: fall back to the closed profile path below.
            profile_arg, perr = None, None
            want_surface = False
        else:
            open_surface = True

    if not want_surface:
        # Three ways to name what is extruded: a TEXT address, a profile HANDLE (an entityToken from
        # sketch_get - the robust way to pick one of several regions, face ring vs the region you
        # drew), or an index/list/'all' selector. Each predicate says no to the other two's forms.
        if text_addr:
            # ProfileRef resolves the address's OWN sketch, so a bare 'text:<i>' is qualified with
            # the sketch this call already resolved by name - otherwise the two reads can land on
            # different sketches (the address alone falls back to the most recent one).
            addr = text_addr
            if "/" not in addr and (sketch_name or "").strip():
                addr = f"{sketch_name.strip()}/{addr}"
            prof, perr = _TEXT_PROFILE.resolve(addr, component)
            if perr:
                return error(perr)
            profile_arg, indices = prof, [None]
        elif selected_profile is not None:
            profile_arg, indices = selected_profile, [None]
        else:
            # BEFORE the count is trusted: an index reads straight off sketch.profiles here, never
            # through ProfileRef, so a deferred sketch's pcount and item(i) are both pre-deferral.
            if stale:
                return error(stale)
            if pcount == 0:
                return error(f"Sketch '{safe(lambda: sketch.name)}' has no closed profile to extrude. "
    "Draw a closed region (e.g. a rectangle or circle) first, or pass "
    "as_surface=true to extrude an open path into a surface.")
            indices, ierr = _resolve_profile_indices(profile_index, pcount, profiles)
            if ierr:
                return error(ierr)
            took_all = _is_all_selector(profile_index)
            # One profile -> pass it directly; several -> an ObjectCollection (extrudeFeatures.createInput
            # accepts either, so N profiles of one sketch extrude in ONE feature/call).
            if len(indices) == 1:
                profile_arg = profiles.item(indices[0])
            else:
                coll = adsk.core.ObjectCollection.create()
                for i in indices:
                    coll.add(profiles.item(i))
                profile_arg = coll

    op = getattr(adsk.fusion.FeatureOperations, _common.OPERATIONS[op_key])
    host = _inputs.profile_host_component(profile_arg, sketch, root)
    try:
        ext_input = host.features.extrudeFeatures.createInput(profile_arg, op)
        if open_surface:
            ext_input.isSolid = False   # surface: no end caps (confirmed-live ExtrudeFeatureInput.isSolid)
        elif text_addr:
            # as_surface is refused with a text, so the solid this call promises is STATED and read
            # back rather than inherited from a default the code never read.
            serr = _common.set_verified(ext_input, "isSolid", True,
                                        "isSolid=true (a sketch text extrudes as a solid)",
                                        "ExtrudeFeatureInput")
            if serr:
                return error(serr)
    except Exception as e:
        return error(f"Could not start extrude: {e}")

    # extent: 'to_object'/'to_face' wins; then through_all/two_side; else a blind distance.
    taper = float(taper_deg or 0.0)
    through_all_dir = None
    # The depth sides this extent carries, in the order their parameters live on the feature (index
    # 0 = extentOne, 1 = extentTwo): (what, requested cm, raw request, is this side two-sided).
    # Empty for an extent with no distance parameter (through_all, to_face).
    depth_request = []
    try:
        if use_to_object:
            if taper:
                return error("taper_deg is not supported with extent=to_object/to_face - a to-entity "
                             "extrude takes no taper. Use a distance extent, or drop the taper.")
            face, ferr = _TO_OBJECT.resolve(to_object)
            if ferr:
                return error(ferr)
            to_extent = adsk.fusion.ToEntityExtentDefinition.create(face, False)  # chained=False
            # Every setter here documents "Returns true if successful". A false answer leaves the
            # input on its DEFAULT extent, and add() then builds a feature nobody asked for.
            if not ext_input.setOneSideExtent(to_extent,
                                              adsk.fusion.ExtentDirections.PositiveExtentDirection):
                return error("Fusion refused the to_object extent (setOneSideExtent returned false), "
                             "so nothing was extruded. Check the target face is reachable from the "
                             "profile in the extrude direction.")
        elif ext_key == "through_all":
            if taper:
                return error("taper_deg is not supported with extent=through_all (a through-all "
                             "extent carries no taper).")
            # setAllExtent(SymmetricExtentDirection) answers true while cutting ONE direction only,
            # so symmetric sets BOTH sides through ThroughAllExtentDefinition and a one-sided extent
            # names its direction. Either can still fail AT add() with "body not found".
            through_all_dir = _through_all_direction_key(symmetric, distance)
            all_extent = adsk.fusion.ThroughAllExtentDefinition
            if through_all_dir == "symmetric":
                if not ext_input.setTwoSidesExtent(all_extent.create(), all_extent.create()):
                    return error("Fusion rejected a symmetric extent=through_all "
                                 "(setTwoSidesExtent returned false).")
            else:
                ext_dirs = adsk.fusion.ExtentDirections
                direction = {"positive": ext_dirs.PositiveExtentDirection,
                             "negative": ext_dirs.NegativeExtentDirection}[through_all_dir]
                if not ext_input.setOneSideExtent(all_extent.create(), direction):
                    return error(f"Fusion rejected a {through_all_dir} extent=through_all "
                                 "(setOneSideExtent returned false).")
        elif ext_key == "two_side":
            if taper:
                return error("taper_deg is not supported with extent=two_side "
                             "(setTwoSidesDistanceExtent takes no taper).")
            d1, d1_cm, d1err = _inputs.length_value_input(distance, k, design, "distance")
            if d1err:
                return error(d1err)
            d2, d2_cm, d2err = _inputs.length_value_input(distance2, k, design, "distance2")
            if d2err:
                return error(d2err)
            if not ext_input.setTwoSidesDistanceExtent(d1, d2):
                return error("Fusion rejected extent=two_side (setTwoSidesDistanceExtent returned false).")
            depth_request = [("side-one distance", d1_cm, distance, True),
                             ("side-two distance", d2_cm, distance2, True)]
        else:
            dist_val, want_distance_cm, dverr = _inputs.length_value_input(
                distance, k, design, "distance")
            if dverr:
                return error(dverr)
            # One side, on extentOne. Its NEGATIVE case IS measured (the parameter keeps the
            # requested sign, -15 mm reads -1.5), so this side is judged whichever way it points.
            depth_request = [("distance", want_distance_cm, distance, False)]
            if taper and symmetric:
                # symmetric WITH taper: setDistanceExtent carries no taper, so setSymmetricExtent does.
                # isFullLength=False -> 'distance' is the per-side half-length, matching setDistanceExtent
                # (isSymmetric=True, distance), which live-measures as 'distance' on EACH side (2x total).
                taper_val = adsk.core.ValueInput.createByString(f"{taper} deg")
                if not ext_input.setSymmetricExtent(dist_val, False, taper_val):
                    return error(f"Fusion refused a symmetric tapered extent ({distance} {units} per "
                                 f"side, {taper} deg), so nothing was extruded.")
            elif taper:
                # one-sided with taper: build a DistanceExtentDefinition + taper ValueInput
                extent_def = adsk.fusion.DistanceExtentDefinition.create(dist_val)
                taper_val = adsk.core.ValueInput.createByString(f"{taper} deg")
                if not ext_input.setOneSideExtent(
                        extent_def, adsk.fusion.ExtentDirections.PositiveExtentDirection, taper_val):
                    return error(f"Fusion refused a one-sided tapered extent ({distance} {units}, "
                                 f"{taper} deg), so nothing was extruded.")
            else:
                if not ext_input.setDistanceExtent(bool(symmetric), dist_val):
                    return error(f"Fusion refused a {'symmetric ' if symmetric else ''}distance "
                                 f"extent of {distance} {units}, so nothing was extruded.")
    except Exception as e:
        return error(f"Could not set extrude extent: {e}")

    # target_bodies: scope a cut/join/intersect to specific bodies so it can't bleed through others.
    scoped_to = None
    bodies_ents = None
    if target_bodies not in (None, "", []):
        if op_key == "new":
            return error("'target_bodies' only applies to cut/join/intersect (a 'new' body has no "
    "participants). Remove it, or change the operation.")
        bodies_ents, berr = _TARGET_BODIES.resolve(target_bodies)
        if berr:
            return error(berr)
        try:
            ext_input.participantBodies = list(bodies_ents)
            scoped_to = [_qualified_body_name(b) for b in bodies_ents]
        except Exception as e:
            return error(f"Could not scope to target_bodies: {e}")

    # through_all CUT/INTERSECT: pre-capture the volume of the bodies it will act on, so a silent
    # no-op is caught instead of a false ok. 'All' extends until it exits the geometry, so ANY volume
    # drop on a targeted body proves the cut went all the way through it.
    check_bodies = []
    if ext_key == "through_all" and op_key in ("cut", "intersect"):
        check_bodies = list(bodies_ents) if bodies_ents else _solo_solid_body(host)
    # check_bodies are HELD across the mutation and re-read afterwards, which is exactly the
    # id()-keying precondition _geom.volumes documents - so this is the shared sample, not a local one.
    vol_before = _geom.volumes(check_bodies)

    # cut/intersect read-back: capture every solid body's volume design-wide BEFORE the op, so we can
    # report which bodies (and whose components) actually changed - and warn when an UNSCOPED cut bled
    # into a co-located component (feature.bodies sees only the own-component result body; see helper).
    solid_snap = _solid_bodies_snapshot(design) if op_key in ("cut", "intersect") else []
    # A join is told "grew a body" from "made a second one" by the host's body NAMES before the add.
    bodies_before = _common.component_body_names(host) if op_key == "join" else None

    try:
        feature = host.features.extrudeFeatures.add(ext_input)
    except Exception as e:
        # A bad expression reference can slip past the pre-check and only fail here, so it is named.
        # The direction hint points the OTHER way from the distance that just failed.
        platform = str(e).lower()
        if _inputs.looks_like_expression(distance):
            hint = f" The distance expression '{distance.strip()}' may be unresolvable - check param_get."
        elif ((ext_key == "through_all" and "body not found" in platform)
              or (op_key in ("cut", "intersect") and "no target body found" in platform)):
            hint = _no_target_body_hint(ext_key, distance, bool(symmetric))
        else:
            hint = " (A 'cut'/'intersect' needs existing geometry to act on.)"
        return error(f"Extrude failed: {e}.{hint}")
    if not feature:
        return error(_common.no_feature_error(design, "Extrude"))

    through_all_removed = None
    if check_bodies:
        deltas, vol_after = {}, _geom.volumes(check_bodies)
        for b in check_bodies:
            # The OCCURRENCE-QUALIFIED name, the same spelling 'scoped_to_bodies' echoes: Fusion
            # auto-names every component's first body 'Body1', so a bare-name key collapses a cut
            # scoped across two components into one entry and overwrites the first.
            nm = _qualified_body_name(b) or "?"
            before, after = vol_before.get(id(b)), vol_after.get(id(b))
            if isinstance(before, (int, float)) and isinstance(after, (int, float)):
                deltas[nm] = round(before - after, 6)
        if deltas:
            through_all_removed = deltas
            if all(abs(d) < _common.NO_VOLUME_CHANGE_CM3 for d in deltas.values()):
                # The check reads only the bodies this cut was aimed at, so an effect elsewhere in
                # the design is not ruled out - the feature is DISCLOSED, never deleted from under
                # a caller whose evidence stops at those bodies.
                fname = safe(lambda: feature.name) or "the new extrude feature"
                return error("Extrude reported success but extent=through_all removed no material "
                             f"from {', '.join(deltas)}. through_all follows the sketch-plane "
                             "normal, which on an on-face sketch points away from the body - pass "
                             f"the opposite 'distance' sign. '{fname}' remains in the timeline (this "
                             "check read only those bodies); remove it with design_delete_feature.")

    # cut/intersect EFFECT evidence, read BEFORE any rollback below: which bodies lost material, and
    # whether the solid census moved at all. Both are needed to claim nothing happened - a consumed
    # body reports no volume drop, and a split adds a body.
    affected = _affected_bodies(solid_snap) if solid_snap else []
    solid_delta = (_solid_count(design) - len(solid_snap)) if solid_snap else 0
    nothing_changed = bool(solid_snap) and not affected and solid_delta == 0
    scoped_names = ", ".join(n for n in (scoped_to or []) if n)

    # add() hands back a truthy feature object for a FAILED compute, so the health state is the only
    # signal here - and a scoped cut reaching none of its bodies leaves a healthy FEATURE with a
    # WARNING timeline item, which is why compute_state asks both.
    _state, failed = _assert.compute_state(feature)
    if failed:
        state_label, detail = failed
        fname = safe(lambda: feature.name) or "the new extrude feature"
        parts = [f"Extrude built '{fname}' but Fusion reports it as a FAILED compute (health state: "
                 f"{state_label})" + (f": {detail}" if detail else " (it reports no message)") + "."]
        if nothing_changed:
            parts.append(f" No solid body lost material and none was consumed, so this {op_key} "
                         "removed nothing.")
        elif affected:
            parts.append(f" Material DID change: {_effect_rows(affected)}.")
        elif solid_delta:
            parts.append(f" The design-wide solid body count changed by {solid_delta:+d}.")
        else:
            parts.append(f" Whether anything landed is NOT read for a '{op_key}' extrude, so it is "
                         "not claimed either way.")
        if scoped_names:
            parts.append(f" Scoping to 'target_bodies' ({scoped_names}) makes Fusion build this "
                         "failed feature instead of refusing a profile that reaches none of them, so "
                         "check the profile overlaps those bodies in the extrude direction (a "
                         "negative 'distance' reverses it).")
        if nothing_changed:
            parts.append(_roll_back(design, feature, fname))
        else:
            parts.append(f" '{fname}' is LEFT in the timeline - nothing is rolled back while an "
                         "effect is unruled-out. Inspect it with design_get and remove it with "
                         "design_delete_feature if it is unwanted.")
        return error("".join(parts))

    # A SCOPED cut/intersect that changed nothing: the feature computed, but no body named in
    # 'target_bodies' lost material and the solid census is unmoved. Only named bodies can be
    # affected, so this is a no-op reported as success.
    if scoped_to and op_key in ("cut", "intersect") and nothing_changed:
        fname = safe(lambda: feature.name) or "the new extrude feature"
        return error(f"Extrude reported success but this {op_key} changed nothing: no solid body "
                     "lost material and none was consumed, so the scoped bodies "
                     f"({scoped_names}) are untouched. A cut/intersect can only affect bodies named "
                     "in 'target_bodies' - check the profile overlaps them in the extrude direction "
                     "(a negative 'distance' reverses it)."
                     + _roll_back(design, feature, fname))

    # DEPTH read-back per side: the feature's own distance ModelParameter against the cm number the
    # units engine evaluated THAT side's request to. Side one lands on extentOne and side two on
    # extentTwo, so a side is judged against its own request. A side this cannot judge is disclosed.
    unverified = []
    landed_side = (_common.landed_extent_cm, _common.landed_extent2_cm)
    for i, (what, want_cm, raw, two_sided) in enumerate(depth_request):
        got_cm = landed_side[i](feature)
        if want_cm is None:
            unverified.append((what, "the units engine answered no number for its request, so "
                                     "there was nothing to judge it against"))
        elif two_sided and want_cm <= 0:
            unverified.append((what, "the depth a NEGATIVE two-sided request stores is unmeasured, "
                                     "so its landed depth was read but not judged"))
        elif got_cm is None:
            unverified.append((what, "the feature reported no depth number for it"))
        elif abs(got_cm - want_cm) > _common.EXTENT_MATCH_TOL_CM:
            fname = safe(lambda: feature.name) or "the new extrude feature"
            return error(_depth_mismatch(fname, what, got_cm, want_cm, raw, k, units))

    body_names = [f["name"] for f in _common.body_facts(_common.result_bodies(feature))]

    # body-split: the extruded profile removes no bodies, so any NET increase in the design-wide
    # solid count is split-off pieces. Counting solids - not feature.bodies, which for a cut reports
    # only the own-component result - also catches a split in a co-located component.
    split_count = solid_delta if op_key in ("cut", "intersect") else 0

    # 'component' names where the cut landed - not merely where the sketch lives - from the same
    # affected-bodies read, and an unscoped cut that reached a co-located component is flagged
    # (the footgun).
    sketch_owner = safe(lambda: sketch.parentComponent.name)
    affected_comps = []
    for _n, _cn, _rem in affected:
        if _cn and _cn not in affected_comps:
            affected_comps.append(_cn)
    if affected_comps:
        # the sketch's own component if it was touched (the expected primary), else the one that lost
        # the most material (a consumed body counts as maximal).
        component_field = (sketch_owner if sketch_owner in affected_comps
                           else max(affected, key=lambda r: float("inf") if r[2] is None else r[2])[1])
    else:
        component_field = safe(lambda: feature.parentComponent.name)

    # Surface the result either way: read isSolid back off the feature (never assumed).
    is_solid = safe(lambda: feature.isSolid)
    if open_surface:
        note = ("Open profile extruded into a SURFACE (no end caps) - pair with model_stitch to "
    "close several surfaces into a solid.")
    elif text_addr:
        note = ("Sketch text extruded into a solid. To stamp text onto an existing face instead, "
                "use model_emboss.")
    else:
        note = "Profile extruded into a solid. Pair with view_screenshot (iso) to view it."
    if op_key == "new":
        adv = root_body_advisory(design, host)          # advise on where the body actually landed
        if adv:
            note += " " + adv
    join_clause = _common.join_new_body_clause(op_key, bodies_before, body_names)
    if join_clause:
        note += " " + join_clause
    # 'all' takes every closed region, so a bay inside a frame extrudes into material and the payload
    # cannot show it - the enclosed regions are counted and named here instead, behind safe() so a
    # misbehaving containment read cannot sink an extrude that landed.
    enclosed = None
    if took_all and len(indices) > 1:
        enclosed = safe(lambda: _enclosed_regions(profiles, indices))
        note += f" 'all' selected every closed region in this sketch ({len(indices)})"
        if enclosed is not None and not enclosed:
            note += "; none of them sits inside another selected region."
        else:
            note += ((", INCLUDING any region enclosed by another selected one" if enclosed is None
                      else (f", INCLUDING {len(enclosed)} region(s) enclosed by another selected one "
                            f"(profile index {', '.join(str(i) for i in enclosed)})"))
                     + " - the openings inside a frame outline are closed regions too, so this "
                     f"{op_key} acted on them as well. To act on only the regions you mean, read "
                     "them with sketch_get (area/centroid per region) and pass a profile 'handle' "
                     "or an index list.")

    if use_to_object:
        extent_report, distance_report = "to_object", None
    elif ext_key == "through_all":
        extent_report, distance_report = "through_all", None
    else:
        extent_report, distance_report = ext_key, _inputs.expression_report(distance)

    # Name the model parameters the feature created so the retarget path is discoverable without
    # fishing through param_get to guess which dNN is which (param_set '<dNN>' '<expression>').
    model_params = _feature_parameters(feature)
    if model_params:
        note += (" Distance/taper are model parameters (see 'model_parameters') - param_set one to an "
                 "expression to drive this feature parametrically.")

    # The one disclosure for every side the compare above could not judge, so a success that skipped
    # a depth check never reads as one that passed it.
    for what, why in unverified:
        note += f" {what[0].upper()}{what[1:]} was not depth-verified: {why}."

    result = {
        "extruded": True,
        "feature": safe(lambda: feature.name),
        "operation": op_key,
        "sketch": safe(lambda: sketch.name),
        "component": component_field,
        "profile_index": ("text" if text_addr
                          else "handle" if indices == [None]
                          else (indices[0] if len(indices) == 1 else indices)),
        "profiles_extruded": len(indices),
        "as_surface": bool(open_surface),
        "is_solid": is_solid,
        "distance": distance_report,
        "extent": extent_report,
        "units": units,
        "symmetric": bool(symmetric),
        "taper_deg": float(taper_deg or 0.0),
        "scoped_to_bodies": scoped_to,
        "result_bodies": body_names,
        "note": note,
    }
    if model_params:
        result["model_parameters"] = model_params
    if enclosed:
        result["enclosed_profile_indices"] = enclosed
    if ext_key == "two_side":
        result["distance2"] = _inputs.expression_report(distance2)
    if ext_key == "through_all":
        result["direction"] = through_all_dir
    if through_all_removed is not None:
        result["through_all_volume_removed_cm3"] = through_all_removed
    if len(affected_comps) > 1:
        result["affected_components"] = affected_comps
    # An UNSCOPED cut/intersect takes every body that is BOTH coincident with the cut shape AND
    # visible; a hidden co-located body is spared. 'target_bodies' overrides visibility - a named
    # body is cut even while hidden - so the two levers are that input, or hiding what must survive.
    if scoped_to is None and op_key in ("cut", "intersect"):
        foreign = [c for c in affected_comps if c != sketch_owner]
        if foreign:
            result["cut_touched_other_components"] = foreign
            result["note"] += (f" WARNING: with no 'target_bodies' this {op_key} removed material from "
                               f"every VISIBLE body it intersects, including {', '.join(foreign)} "
                               "(hidden bodies are spared). Scope it by passing 'target_bodies' (a named "
                               "body is cut even if hidden), or hide the bodies that must be spared.")
    if split_count > 0:
        result["body_split"] = body_names
        result["note"] += (f" WARNING: this {op_key} DISCONNECTED the target - it created "
                           f"{split_count} additional disconnected body/bodies (the feature now yields "
                           f"{len(body_names)}: {', '.join(n for n in body_names if n)}). Reference "
                           "each piece by name; a later op assuming one body may hit the wrong piece.")
    return ok(result)


TOOL_DESCRIPTION = (
"Extrude a closed sketch profile into a solid; sketch_get returns profile handles."
)

extrude_tool = (
    Tool.create_simple(name="model_extrude", description=TOOL_DESCRIPTION)
    .add_input_property("sketch_name", {"type": "string",
            "description": "Omit for the most recent sketch."})
    .add_input_property("profile_index", {"type": ["integer", "string", "array"],
            "description": "An index (default 0), a list, 'all', one profile 'handle', or 'text:<i>'."})
    .add_input_property("distance", {"type": ["number", "string"],
            "description": "Depth in 'units' (negative reverses), or a parameter expression."})
    .add_input_property("distance2", {"type": ["number", "string"]})
    .add_input_property(*_inputs.UNITS.as_property())
    .add_input_property(*_inputs.boolean_op(default="new").as_property())
    .add_input_property(*_EXTENT.as_property())
    .add_input_property("symmetric", {"type": "boolean"})
    .add_input_property("taper_deg", {"type": "number"})
    .add_input_property("to_object", _TO_OBJECT.schema())
    .add_input_property("target_bodies", _TARGET_BODIES.schema())
    .add_input_property(*_sketch_detail.COMPONENT_SCOPE)
    .add_input_property("as_surface", {"type": "boolean",
            "description": "Make a SURFACE wall (no end caps)."})
    .strict_schema()
)
extrude_item = Item.create_tool_item(tool=extrude_tool, write="write", handler=handler, run_on_main_thread=True,
                                     postconditions=[_assert.FeatureHealthy()],
                                     verification=Verification(
                                         kind="inline", rung="geometry",
                                         evidence_test="tests/unit/test_model_extrude.py"
                                         "::TestThroughAllVolumeCheck"
                                         "::test_no_volume_change_is_reported_as_error"))


def register_tool():
    register(extrude_item)
