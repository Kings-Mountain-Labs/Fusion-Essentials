# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Shared substrate for the edge-treatment tools (fillet, chamfer): the edge set they act on, the
per-edge convexity a filter selects by, and the one apply path that builds the feature and proves it
moved material.
"""

import math
import re

import adsk.core
import adsk.fusion

from ._common import error, ok, safe, scale
from . import _common
from . import _geom
from . import _inputs

MAP_BLURB = (
    "the edge-treatment substrate: EDGES/FACES/BODY/_EDGE_FILTER_DESC - the targeting inputs, with "
    "_edges_of_faces expanding a face set to its edges, each once; _edge_convexity + "
    "_collect_edges - the per-edge dihedral sign a convex/concave filter selects by; "
    "_apply - the ONE build-and-verify path both tools run, gating on "
    "the created faces, the health state and the body's own volume delta")

# Edge-handle-list input (closes the 'fillet THESE specific edges' gap; takes precedence over edge_filter).
_EDGES = _inputs.GeometryHandleList("edges", require="edge",
                                    description="Overrides 'edge_filter'.")
# Body input: a find_geometry handle (precise) OR a name; resolved/kind-checked by BodyRef.
_BODY = _inputs.BodyRef("body_name", kind="solid", required=False,
                        description="Omit = most recent.")
# Face-scoped targeting: the EDGES of the named faces. A chamfer has no rule-fillet twin, so the
# face set is expanded to its edges here rather than handed to a rule API.
_FACES = _inputs.GeometryHandleList("faces", require="face", required=False,
    description="Every edge of these faces; excludes 'edges'.")

# option key -> the API's OWN ChamferCornerTypes member spelling, lowercase 't' in BlendCornertype
# included: no BlendCornerType member exists, so a "corrected" name would getattr-raise and be
# reported as unavailable rather than silently beveling with the default corner.
_CORNER_TYPES = {"chamfer": "ChamferCornerType", "miter": "MiterCornerType",
                 "blend": "BlendCornertype"}

# edge_filter caveat (shared by both tools): convex/concave classify each edge by its LOCAL dihedral
# only, so on a plate with holes every hole rim matches exactly like the outer perimeter - the filter
# cannot mean "outer edges only". The 'edges' handle list is the precise path when the set matters.
_EDGE_FILTER_DESC = "Required without 'edges': the body's edges by dihedral."

# How close to parallel two out-of-material normals may read and still meet smoothly, which is
# neither convex nor concave. Vectors carry 12 decimals: at 6 the dot quantizes in steps of 1e-6,
# coarser than the 3.8e-7 this band leaves below 1.0, so the band has no room to exist there.
_DIHEDRAL_TOL_DEG = 0.05
_SMOOTH_JOIN_COS = math.cos(math.radians(_DIHEDRAL_TOL_DEG))
_VECTOR_DECIMALS = 12

# The ways an edge comes back with no convex/concave answer, named so a refusal says which happened
# rather than asserts a cause.
_UNCLASSIFIED = ("unreadable", "disagreed", "knife")


def _size_hint(platform_text: str, size_key: str) -> str:
    """The retry hint for an add() raise, gated on the platform's own text: an unconditional
    'try a smaller value' sent agents into shrink-and-retry loops against failures (tangent-chain
    conflicts) that no size fixes."""
    if re.search(r"too large|self.?intersect|radius|distance", platform_text, re.IGNORECASE):
        return f" (The {size_key} may be too large for the geometry - try a smaller value.)"
    return (" (The platform text above is the only measured cause - check the edges are genuine "
            "corners and any chain is tangentially connected; the size is not necessarily the "
            "problem.)")


def _qualified_body_name(body):
    """The body's name qualified with the occurrence path it lives in, so two same-named bodies in
    different components are distinguishable in the report - unqualified, two chamfers on two
    components both report the same local 'Body1'. Reuses _inputs._body_context - the shared 'where
    this body lives' idiom (assemblyContext.fullPathName, else the owning component name)."""
    if body is None:
        return None
    name = safe(lambda: body.name)
    ctx = _inputs._body_context(body)
    if name and ctx and ctx not in ("?", name):
        return f"{ctx}/{name}"
    return name


def _edge_tangent(edge, point):
    """The edge's unit tangent AT `point`, pointing along the EDGE, or None."""
    # The evaluator follows the CURVE; isParamReversed says whether the edge runs against it. An
    # uncorrected tangent negates the heading the sign is read off, flipping the verdict with
    # nothing to catch it.
    against = _common.read_flag(lambda: edge.isParamReversed)
    ev = safe(lambda: edge.evaluator)
    at = safe(lambda: ev.getParameterAtPoint(point)) if ev is not None else None
    if against is None or not (isinstance(at, (list, tuple)) and len(at) == 2 and at[0]):
        return None
    got = safe(lambda: ev.getTangent(at[1]))
    if not (isinstance(got, (list, tuple)) and len(got) == 2 and got[0]):
        return None
    tangent = _geom.unit_vector(got[1], decimals=_VECTOR_DECIMALS)
    if tangent is None:
        return None
    return tuple(-c for c in tangent) if against else tuple(tangent)


def _coedge_side(coedge, point):
    """(the out-of-material normal at `point` of the face this coEdge bounds, whether the coEdge runs
    against the edge's own direction), or None where either did not read."""
    face = safe(lambda: coedge.loop.face)
    opposed = _common.read_flag(lambda: coedge.isOpposedToEdge)
    normal = _geom.evaluator_normal_at(face, point, decimals=_VECTOR_DECIMALS)
    if normal is None or opposed is None:
        return None
    return normal, opposed


def _edge_convexity(edge):
    """'convex' | 'concave' | 'smooth', or one of _UNCLASSIFIED - BRepEdge exposes no convexity flag,
    so one edge's dihedral is signed here from reads taken AT a point on it."""
    # The geometric fact, entirely LOCAL to that point: cross(n, h) is the in-face direction leading
    # away from the edge, h being the coEdge's own heading round its face. It leans ALONG the other
    # face's out-of-material normal when the material fills the reflex wedge - that is concave.
    coedges = list(_common.iter_collection(safe(lambda: edge.coEdges)))
    point = safe(lambda: edge.pointOnEdge)
    if len(coedges) != 2 or point is None:
        return "unreadable"
    tangent = _edge_tangent(edge, point)
    sides = [_coedge_side(c, point) for c in coedges]
    if tangent is None or any(s is None for s in sides):
        return "unreadable"
    cosine = _geom.dot(sides[0][0], sides[1][0])
    if cosine > _SMOOTH_JOIN_COS:
        return "smooth"
    # Two out-of-material normals pointing at each other bound a knife edge or a crack - no material
    # wedge, so there is no dihedral to sign.
    if cosine < -_SMOOTH_JOIN_COS:
        return "knife"
    # The two coEdges run OPPOSITE ways round one edge, so either one answers for the pair. Two
    # reporting the same isOpposedToEdge cannot both be right, and nothing is signed off them.
    if sides[0][1] == sides[1][1]:
        return "disagreed"
    normal, opposed = sides[0]
    heading = tuple(-c for c in tangent) if opposed else tangent
    return "concave" if _geom.dot(_geom.cross(normal, heading), sides[1][0]) > 0 else "convex"


def _census_phrase(census):
    """The dihedral split as one ASCII clause - the same three the payload publishes."""
    return f"{census['convex']} convex, {census['concave']} concave, {census['smooth']} smooth"


def _unclassified(census):
    """How many edges got no convex/concave answer, over the three ways that happens."""
    return sum(census[k] for k in _UNCLASSIFIED)


def _unclassified_phrase(census):
    """Which unclassified branches actually occurred - a branch that did not happen is not named."""
    return ", ".join(f"{census[k]} {k}" for k in _UNCLASSIFIED if census[k])


def _edges_of_faces(faces):
    """Every edge of `faces`, each ONCE - two faces in one list share their common edge, and the
    same edge twice in a feature's edge set is a duplicate. An edge whose token will not read is
    kept, since nothing there tells it from another."""
    out, seen = [], set()
    for f in faces:
        for e in _common.iter_collection(safe(lambda f=f: f.edges)):
            key = safe(lambda e=e: e.entityToken)
            if key:
                if key in seen:
                    continue
                seen.add(key)
            out.append(e)
    return out


def _collect_edges(body, edge_filter):
    """(ObjectCollection of the body's edges matching 'edge_filter', the body's edge count, the
    per-edge census - None under 'all', which takes every edge and classifies none)."""
    flt = (edge_filter or "all").strip().lower()
    coll = adsk.core.ObjectCollection.create()
    edges = safe(lambda: body.edges)
    n = safe(lambda: edges.count, 0) if edges else 0
    if flt == "all":
        for e in _common.iter_collection(edges):
            coll.add(e)
        return coll, n, None
    census = dict.fromkeys(("convex", "concave", "smooth") + _UNCLASSIFIED, 0)
    for e in _common.iter_collection(edges):
        kind = _edge_convexity(e)
        census[kind] += 1
        if flt == kind:
            coll.add(e)
    return coll, n, census


def _build_edge_set(fillet_input, variant, edges, val, k):
    """Add the requested edge set to a FilletFeatureInput. Returns an error string, or ''."""
    if variant is None:
        added = fillet_input.addConstantRadiusEdgeSet(edges, val, True)
        label = "constant-radius"
    elif variant["type"] == "chord_length":
        added = fillet_input.addChordLengthEdgeSet(edges, val, True)
        label = "chord-length"
    else:
        # positions and radii cross as plain Python lists (the API takes a vector there); an
        # ObjectCollection raises a vector-type argument error.
        added = fillet_input.addVariableRadiusEdgeSet(
            edges, val, adsk.core.ValueInput.createByReal(variant["end_radius"] * k),
            [adsk.core.ValueInput.createByReal(p) for p in variant["positions"]],
            [adsk.core.ValueInput.createByReal(r * k) for r in variant["radii"]])
        label = "variable-radius"
    if added is False:
        return (f"The {label} edge set was refused, so no fillet was created. Re-run find_geometry "
                "for fresh edge handles; a variable-radius chain must be tangentially connected "
                "and listed in order from its start end.")
    return ""


def _is_are(names):
    """' is' or ' are' for a list of field names, so a one-field note reads as a sentence."""
    return " is" if len(names) == 1 else " are"


def _distance_mismatch(got_cm, want_cm, k):
    """The refusal when the chamfer's own distance is not the one asked for, else '' - the ONE
    comparison both the angle definition and an expression-driven distance are judged by. A
    want_cm of None is an expression the units engine answered no number for: there is nothing to
    compare against, and the caller publishes the distance unconfirmed instead."""
    if want_cm is None or not isinstance(got_cm, float) or abs(got_cm - want_cm) <= 1e-6:
        return ""
    return (f"The chamfer was created but its distance reads back {round(got_cm / k, 6)}, "
            f"not the requested {round(want_cm / k, 6)}.")


def _chamfer_readback(feature, size_cm, k, angle, corner_key, as_expression=False):
    """(verified payload fields, unverified field names, error) read off the CREATED chamfer. A
    declined corner type leaves no trace anywhere else, and a distance-and-angle definition carries
    .distance in CM and .angle in RADIANS, which the comparisons below convert to."""
    fields, unverified = {}, []
    if corner_key:
        cts = safe(lambda: adsk.fusion.ChamferCornerTypes)
        want = safe(lambda: getattr(cts, _CORNER_TYPES[corner_key])) if cts is not None else None
        got = safe(lambda: feature.cornerType)
        if want is None or got is None:
            unverified.append("corner_type")
        elif got != want:
            return fields, unverified, (
                f"The chamfer was created but its corner type reads back {got}, not the requested "
                f"'{corner_key}' ({want}) - the corner where several chamfered edges meet is not "
                "the one asked for.")
        else:
            fields["corner_type"] = corner_key
    if angle is None:
        if as_expression:
            # An expression is compared against the cm the units engine evaluated it to, so a
            # feature that took some other number is never reported as the expression asked for.
            td = safe(lambda: feature.chamferTypeDefinition)
            got = safe(lambda: td.distance.value) if td is not None else None
            derr = _distance_mismatch(got, size_cm, k)
            if derr:
                return fields, unverified, derr
            if isinstance(got, float) and size_cm is not None:
                fields["distance"] = round(got / k, 6)
            else:
                unverified.append("distance")
        return fields, unverified, ""

    # chamferType names WHICH definition got built: a distance-and-angle chamfer that silently fell
    # back to an equal-distance one would still read back a plausible distance and no angle at all.
    types = safe(lambda: adsk.fusion.ChamferTypes)
    want_type = safe(lambda: types.DistanceAndAngleChamferType) if types is not None else None
    got_type = safe(lambda: feature.chamferType)
    if want_type is None or got_type is None:
        unverified.append("chamfer_type")
    elif got_type != want_type:
        return fields, unverified, (
            f"The chamfer was created but Fusion reports its definition as chamfer type {got_type}, "
            f"not the distance-and-angle type ({want_type}) that was requested.")

    td = safe(lambda: feature.chamferTypeDefinition)
    got_angle = safe(lambda: td.angle.value) if td is not None else None
    got_dist = safe(lambda: td.distance.value) if td is not None else None
    if isinstance(got_angle, float):
        deg = math.degrees(got_angle)
        if abs(deg - angle) > 1e-6:
            return fields, unverified, (
                f"The chamfer was created but its angle reads back {round(deg, 6)} deg, not the "
                f"requested {round(angle, 6)}.")
        fields["angle_deg"] = round(deg, 6)
    else:
        unverified.append("angle_deg")
    if isinstance(got_dist, float) and size_cm is not None:
        derr = _distance_mismatch(got_dist, size_cm, k)
        if derr:
            return fields, unverified, derr
        fields["distance"] = round(got_dist / k, 6)
    else:
        unverified.append("distance")
    return fields, unverified, ""


def _apply(kind, body_name, size, units, edge_filter, edge_handles=None, distance_two=0.0,
           variant=None, angle=None, corner_key=None, face_handles=None):
    vtype = variant["type"] if variant else "constant"
    size_key = ("distance" if kind == "chamfer"
                else "chord_length" if vtype == "chord_length" else "radius")
    k = scale(units)
    if k is None:
        return error(f"Unknown units '{units}'. Use mm, cm, or in.")
    # A fillet RADIUS and a chamfer DISTANCE may arrive as a parameter EXPRESSION string
    # ('WallT/2'); a chord_length stays literal, its read-back comparing against that number. Only a
    # LITERAL is judged this early, before the design that resolves an expression is in hand.
    takes_expression = size_key in ("radius", "distance")
    as_expression = takes_expression and _inputs.looks_like_expression(size)
    sz = None
    if not as_expression:
        try:
            sz = float(size)
        except Exception:
            return error(f"'{size_key}' must be a number"
                         + (" or a parameter-expression string like 'WallT/2'."
                            if takes_expression else "."))
        if sz <= 0:
            return error(f"Provide a positive {size_key}.")

    design = _common.design()
    if not design:
        return error("No active design. Create or open a document first (see doc_new).")
    comp = _common.target_component(design)
    # One ValueInput for both forms: a literal scaled to internal cm, an expression crossing as a
    # string once the units engine evaluated it, so an unresolvable one is refused BY NAME. The
    # evaluated cm puts an expression under the same positivity guard a literal gets.
    val, size_cm, verr = _inputs.length_value_input(size, k, design, size_key)
    if verr:
        return error(verr)
    if as_expression and size_cm is not None and size_cm <= 0:
        return error(f"Provide a positive {size_key}: the expression '{str(size).strip()}' "
                     f"evaluates to {round(size_cm / k, 6)} {units}.")
    # How the refusals below name the size: an expression as itself (it carries its own units), a
    # literal as the number plus the caller's units.
    size_text = f"'{str(size).strip()}'" if as_expression else f"{sz} {units}"

    edge_src = "filter"
    body_label = None
    census = None
    faces_selected = None
    want_faces = face_handles not in (None, "", [])
    if want_faces and edge_handles not in (None, "", []):
        return error("Pass 'faces' or 'edges', not both: 'faces' works on every edge of the named "
                     "faces, 'edges' on exactly the edges handed in.")
    # 'edges' (a GeometryHandleList of edge handles) takes precedence - closes the
    # 'fillet THESE specific edges' gap. The kind resolves+validates each handle to a BRep edge.
    blanket_note = None
    if want_faces:
        fents, ferr = _FACES.resolve(face_handles)
        if ferr:
            return error(ferr)
        ents = _edges_of_faces(fents)
        if not ents:
            return error(f"None of the {len(fents)} face(s) answered any edges, so there is nothing "
                         f"to {kind}. Pass 'edges' handles from find_geometry instead.")
        edges = adsk.core.ObjectCollection.create()
        for e in ents:
            edges.add(e)
        faces_selected = len(fents)
        edge_src = f"{edges.count} edge(s) of {faces_selected} face(s)"
        body_label = _qualified_body_name(safe(lambda: ents[0].body))
        verify_bodies = _geom.owning_bodies(ents)
    elif edge_handles not in (None, "", []):
        ents, herr = _EDGES.resolve(edge_handles)
        if herr:
            # Refuse BEFORE creating any feature: a handle that fails to resolve (stale entityToken,
            # no locator recovery) must not be silently dropped from the edge set. Name the total
            # requested so the caller knows the scale of what it must re-find, not just the one index.
            requested_n = len(edge_handles) if isinstance(edge_handles, (list, tuple)) else None
            count_note = (f" ({requested_n} edge handle(s) were requested; the call is refused before "
                          "any fillet/chamfer feature is created, rather than silently rounding fewer "
                          "edges than asked.)" if requested_n else "")
            return error(herr + count_note)
        edges = adsk.core.ObjectCollection.create()
        for e in ents:
            edges.add(e)
        edge_src = f"{edges.count} handle(s)"
        body_label = _qualified_body_name(safe(lambda: ents[0].body))
        verify_bodies = _geom.owning_bodies(ents)
    else:
        # The edge SCOPE must be stated - an omitted scope must not silently mean the whole body
        # (blanket rounding was every executor's default design language while 'all' was implicit;
        # explicit scope makes body-wide edge treatment a stated choice).
        flt = (edge_filter or "").strip().lower()
        if not flt:
            return error(f"State the edge scope: pass 'edges' (find_geometry edge handles - the "
                         f"precise set to {kind}) or 'faces' (every edge of the named faces) or an "
                         f"explicit edge_filter ('all' | "
                         f"'convex' | 'concave') to sweep the body. An omitted scope never means "
                         f"the whole body.")
        if flt not in ("all", "convex", "concave"):
            return error("edge_filter must be: all | convex | concave.")
        body, berr = _common.resolve_body_or_recent(
            _BODY, comp, body_name,
            "No body in the active component to fillet/chamfer. Model one first, or pass 'edges' = "
            "edge handles from find_geometry.")
        if berr:
            return error(berr)
        edges, total, census = _collect_edges(body, flt)
        if census is not None and _unclassified(census):
            return error(
                f"{_unclassified(census)} of the {total} edges on '{safe(lambda: body.name)}' could "
                f"not be classified convex or concave ({_unclassified_phrase(census)}), so the "
                f"'{flt}' set cannot be stated. The rest read {_census_phrase(census)}. Pass "
                "'edges' handles from find_geometry to name the set, or edge_filter='all'.")
        if edges.count == 0:
            return error(f"No matching edges on '{safe(lambda: body.name)}' (filter '{flt}', body "
                         f"has {total} edges"
                         + (f": {_census_phrase(census)}" if census else "") + ").")
        body_label = _qualified_body_name(body)
        verify_bodies = [body]
        blanket_note = (f" BLANKET call: {edges.count} of the body's {total} edges swept by "
                        f"filter '{flt}'"
                        + (f" ({_census_phrase(census)})" if census else "")
                        + " - pass edges=[...] handles to target a specific set.")

    vol_before = _geom.volumes(verify_bodies)
    try:
        if kind == "fillet":
            fi = comp.features.filletFeatures.createInput()
            set_err = _build_edge_set(fi, variant, edges, val, k)
            if set_err:
                return error(set_err)
            feature = comp.features.filletFeatures.add(fi)
        else:
            ci = comp.features.chamferFeatures.createInput(edges, True)
            d2 = float(distance_two or 0.0)
            if angle is not None:
                # A ValueInput built from a REAL carries RADIANS for an angle, so the wire's
                # degrees are converted here; 'distance' is the leg along the first face and the
                # angle turns the bevel off it.
                ang = adsk.core.ValueInput.createByReal(math.radians(angle))
                if not ci.setToDistanceAndAngle(val, ang):
                    return error(f"Fusion refused a distance-and-angle chamfer of {size_text} "
                                 f"at {angle} deg, so nothing was chamfered.")
            elif d2 > 0:
                # two-distance (asymmetric) chamfer
                val2 = adsk.core.ValueInput.createByReal(d2 * k)
                if not ci.setToTwoDistances(val, val2):
                    return error(f"Fusion refused a two-distance chamfer ({size_text} / "
                                 f"{d2} {units}), so nothing was chamfered.")
            else:
                if not ci.setToEqualDistance(val):
                    return error(f"Fusion refused an equal-distance chamfer of {size_text}"
                                 ", so nothing was chamfered.")
            if corner_key:
                # An omitted corner_type never assigns, so the input keeps the API's own default.
                cts = safe(lambda: adsk.fusion.ChamferCornerTypes)
                kerr = _common.set_verified(
                    ci, "cornerType",
                    safe(lambda: getattr(cts, _CORNER_TYPES[corner_key])) if cts is not None else None,
                    f"corner_type='{corner_key}'", "ChamferFeatureInput")
                if kerr:
                    return error(kerr)
            feature = comp.features.chamferFeatures.add(ci)
    except Exception as e:
        return error(f"{kind.capitalize()} failed: {e}.{_size_hint(str(e), size_key)}")
    if not feature:
        return error(_common.no_feature_error(design, kind.capitalize()))

    # READ-BACK off the created feature - the input collection's count is only the request, and a
    # fillet/chamfer can consume fewer edges than handed in. feature.faces.count is the fillet FACES
    # created: a real fillet reports >=1, and the 0-face no-op is gated separately below.
    faces_created = safe(lambda: feature.faces.count)
    # FilletFeature/ChamferFeature expose no .edges collection - "edges" is absent from dir() and
    # reading it raises AttributeError - so the created faces are the only per-edge effect read-back
    # the feature offers.

    # A feature can come back with an ERROR health state and no message at all: a variable-radius
    # chain listed out of connected order does that, and it reads 0 faces like a tangent no-op. The
    # health state separates the two, so it is checked BEFORE the no-op gate.
    health = safe(lambda: feature.healthState)
    if health == adsk.fusion.FeatureHealthStates.ErrorFeatureHealthState:
        detail = safe(lambda: feature.errorOrWarningMessage) or ""
        removed = safe(lambda: feature.deleteMe())
        return error(
            f"{kind.capitalize()} created a feature Fusion reports as FAILED"
            + (f": {detail}" if detail else " (it reports no message)")
            + f". No {kind} was applied. For a variable-radius chain, the edges must be "
              "tangentially connected AND listed from one end of the chain to the other; otherwise "
              "check the edges really are corners at this radius, and re-run find_geometry for "
              "fresh handles."
            + ("" if removed else " (The failed feature could not be auto-removed.)"))

    # Fillet NO-OP guard: a fillet on a TANGENT edge - two faces meeting smoothly - creates the
    # feature but rounds nothing, reading 0 faces with the body volume unchanged. Scoped to fillet,
    # where the 0-face read-back is proven to mean no-op.
    if kind == "fillet" and faces_created == 0:
        removed = safe(lambda: feature.deleteMe())
        return error(
            "Fillet reported success but rounded nothing - the created feature holds 0 faces. A "
            "TANGENT edge does this: its two faces meet smoothly (zero dihedral), e.g. a hole "
            "drilled tangent to a face with its diameter equal to the wall thickness. Make the "
            "corner non-tangent (a smaller hole diameter), or fillet a convex/concave edge."
            + ("" if removed else " (The inert fillet feature could not be auto-removed.)"))

    # Partial-application guard (both kinds): a stale handle can resolve to SOME live entity through
    # the locator fallback yet not participate in the feature. A fully-applied fillet creates one
    # face per requested edge, even on a tangent LOOP, so a shortfall means an edge was dropped.
    applied = faces_created
    if applied is not None and applied < edges.count:
        removed = safe(lambda: feature.deleteMe())
        return error(
            f"{kind.capitalize()} reported success but only PARTIALLY applied: {edges.count} edge(s) "
            f"requested, but the created feature holds only {applied} "
            f"face(s) - at least one requested edge was "
            "dropped (a stale handle recovered the wrong/dead geometry, or an edge the operation could "
            "not reach). The feature has been rolled back; re-run find_geometry for fresh handles and "
            "retry."
            + ("" if removed else " (The partial feature could not be auto-removed.)"))

    # What the FEATURE says it built - never the request echoed back. A corner type or an angle the
    # platform declined leaves no other trace, so a mismatch rolls the feature back.
    verified, unverified = {}, []
    if kind == "chamfer":
        verified, unverified, rerr = _chamfer_readback(feature, size_cm, k, angle, corner_key,
                                                       as_expression)
        if rerr:
            removed = safe(lambda: feature.deleteMe())
            return error(rerr + (" The feature has been rolled back."
                                 if removed else " (The feature could not be auto-removed.)"))

    # A fillet or chamfer either cuts a corner away or fills a concave one, so an unchanged volume
    # means nothing was rounded/beveled however healthy the feature looks. Both kinds publish the
    # same volume_delta_cm3, and both are gated on it.
    vol_delta, vol_readable = _geom.volume_delta(verify_bodies, vol_before)
    if vol_readable and abs(vol_delta) < _common.NO_VOLUME_CHANGE_CM3:
        removed = safe(lambda: feature.deleteMe())
        size_desc = f"{vtype} fillet" if kind == "fillet" else "chamfer"
        return error(
            f"{kind.capitalize()} reported success but moved no material - the body's measured "
            f"volume is unchanged after the {size_desc}. The feature has been rolled back; check "
            "that the requested edges really are corners at this size, and re-run find_geometry "
            "for fresh handles."
            + ("" if removed else f" (The inert {kind} feature could not be auto-removed.)"))

    measured_note = ("" if faces_created is None
                     else " faces_created is read from the created feature"
                          " (corner patches count too, so faces_created can exceed"
                          " edges_requested).")

    payload = {
        kind + "ed": True,
        "feature": safe(lambda: feature.name),
        "body": body_label,
        # An expression is echoed as itself; a literal as the rounded number it was.
        size_key: _inputs.expression_report(size),
        "units": units,
        "edge_selection": edge_src,
        "edges_requested": edges.count,
        "note": (f"Edges {'rounded' if kind == 'fillet' else 'beveled'}. Pair with view_screenshot."
                 + measured_note
                 + (" " + "/".join(sorted(verified)) + _is_are(verified)
                    + " read back off the created feature, not echoed." if verified else "")
                 + (" " + "/".join(sorted(unverified)) + " could NOT be read back off the feature, "
                    "so the requested value is unconfirmed." if unverified else "")
                 + (blanket_note or "")),
    }
    # Omit rather than report null: a None from safe() means the attribute did not answer.
    if faces_created is not None:
        payload["faces_created"] = faces_created
    if faces_selected is not None:
        payload["faces_selected"] = faces_selected
    if census is not None:
        payload["edges_convex"] = census["convex"]
        payload["edges_concave"] = census["concave"]
        payload["edges_smooth"] = census["smooth"]
    if vol_readable:
        payload["volume_delta_cm3"] = round(vol_delta, 6)
    if kind == "fillet":
        payload["fillet_type"] = vtype
    if vtype == "variable":
        payload["end_radius"] = round(variant["end_radius"], 6)
        if variant["positions"]:
            payload["positions"] = variant["positions"]
            payload["radii"] = variant["radii"]
    if kind == "chamfer" and float(distance_two or 0.0) > 0:
        payload["distance_two"] = round(float(distance_two), 6)
    # The verified values REPLACE the request in the payload; a field the feature would not answer
    # is flagged instead of being echoed as though it had been confirmed.
    payload.update(verified)
    for field in unverified:
        payload[f"{field}_unverified"] = True
    if as_expression and size_key in verified:
        # size_key now carries the value the FEATURE reports; without this the caller cannot tell
        # that number came from a parameter rather than from one they fixed.
        payload[f"{size_key}_expression"] = str(size).strip()
    return ok(payload)
