# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""MCP building block: scan a part's faces/edges/vertices and return short-lived HANDLES to them."""

import adsk.core
import adsk.fusion

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item
from ..mcp_primitives.registry import register
from ._common import error, ok, safe, scale
from ._cam_common import clamp_rows
from . import _common
from . import _geom
from . import _inputs
from . import _outputs

RETURNS = [
    _outputs.ReturnsHandle("handle", require="any", in_list=True, consumers=[
        "joint_at_geometry", "sketch_create", "model_extrude", "model_fillet", "model_chamfer",
        "model_construction", "model_mirror", "model_combine", "model_inspect", "view_section",
        "pmi_create"]),
]

app = adsk.core.Application.get()

_MAX_RESULTS_DEFAULT = 20    # rows returned when the caller names no cap
_MAX_RESULTS_CEILING = 100   # hard cap on returned match rows (each crosses the wire)

# friendly 'kind' -> what it matches. Faces by surfaceType, edges by curveType, plus vertex.
_FACE_KINDS = {"cylinder_face": "Cylinder", "planar_face": "Plane",
    "cone_face": "Cone", "sphere_face": "Sphere", "torus_face": "Torus"}
_EDGE_KINDS = {"circular_edge": "Circle3D", "line_edge": "Line3D", "arc_edge": "Arc3D",
    "ellipse_edge": "Ellipse3D", "elliptical_arc_edge": "EllipticalArc3D",
    "spline_edge": "NurbsCurve3D"}
# Edge kinds whose reported 'position' is the curve CENTRE rather than the point on the edge. The
# handle locator stays keyed to pointOnEdge either way - that is what _refind_by_locator compares.
_CENTERED_EDGES = ("circular_edge", "arc_edge", "ellipse_edge", "elliptical_arc_edge")



def _resolve_target(design, target):
    """Resolve 'target' (occurrence/component/body name, or '' for the whole design) to
    (pairs, label, error, walk). A target matching several occurrences scans ALL of them - this read
    hands back every candidate handle; `error` is set only for a refusal, never a plain miss."""
    root = design.rootComponent
    name = (target or "").strip()
    # root.allOccurrences RAISES on a design holding an unresolved external reference, so the shared
    # census is used - it falls back to component.occurrences and says which walk answered.
    walk = _common.occurrence_walk(design)
    all_occs = walk.occurrences
    root_bodies = safe(lambda: list(root.bRepBodies)) or []
    pairs = []
    if not name:
        # whole design: root-level bodies (occurrence None) + every occurrence's bodies, recursively.
        for b in root_bodies:
            pairs.append((None, b))
        for o in all_occs:
            for b in (safe(lambda o=o: list(o.bRepBodies)) or []):
                pairs.append((o, b))
        return pairs, "whole design", None, walk
    # A fullPathName can be worn by two siblings - Fusion enforces no name uniqueness - and for this
    # read that means both get scanned.
    matched = [o for o in all_occs
               if (safe(lambda o=o: o.fullPathName) == name or safe(lambda o=o: o.name) == name
                   or safe(lambda o=o: o.component.name) == name)]
    if matched:
        # The whole SUBTREE, not just direct bodies: an inserted xref wraps its own tree and a
        # derive lands its solid one level down, so a wrapper's own bodies can be none.
        prefixes = [p for p in (safe(lambda o=o: o.fullPathName) for o in matched) if p]
        seen = {id(o) for o in matched}
        subtree = list(matched)
        for o2 in all_occs:
            fp = safe(lambda o2=o2: o2.fullPathName) or ""
            if id(o2) not in seen and any(fp.startswith(p + "+") for p in prefixes):
                subtree.append(o2)
                seen.add(id(o2))
        for o in subtree:
            for b in (safe(lambda o=o: list(o.bRepBodies)) or []):
                pairs.append((o, b))
    if pairs:
        return pairs, f"occurrence/component '{name}'", None, walk
    # By BODY name, through the shared ambiguity-refusing resolver.
    body, body_err = _inputs._resolve_any_body("target", name)
    if body is not None:
        if _inputs._is_mesh(body):
            return [], None, (f"Target '{name}' is a MESH body - find_geometry scans BRep "
                              "faces/edges/vertices, which a mesh has none of. Use mesh_get."), walk
        # Label the body that RESOLVED, in the qualified form that resolves back.
        return [(None, body)], f"body '{_inputs.qualified_body_name(body)}'", None, walk
    # A refusal carries its own fix path; only the plain miss is replaced by this tool's vocabulary.
    if body_err and _inputs.BODY_MISS not in body_err:
        return [], None, body_err, walk
    return [], None, None, walk


def _search_space_note(walk):
    """The sentence disclosing a hole in the occurrence census the scan ran over, or ''."""
    if walk is None:
        return ""
    parts = []
    if not walk.readable:
        parts.append("occurrences_walk='unreadable': NEITHER root.allOccurrences nor the "
                     "component.occurrences fallback enumerated, so the occurrence tree was not "
                     "searched at all and only root-level bodies were scanned - these matches are "
                     "not a complete set, and an empty result here is not an empty design.")
    elif walk.method == _common.WALK_RECURSED:
        parts.append("occurrences_walk='recursed': root.allOccurrences raised, so the occurrence "
                     "census was rebuilt from component.occurrences.")
    if walk.readable and not walk.complete:
        parts.append("The occurrence walk did not run to the end (a collection would not enumerate, "
                     "or a depth/node cap was hit), so part of the design was not scanned.")
    if walk.broken:
        named = _common.named_with_remainder([f"'{n}'" for n in walk.names()])
        parts.append(f"{len(walk.broken)} occurrence(s) hold an unresolved external reference "
                     f"({named}) - reading their component raises, so they carry no geometry to "
                     "scan and were skipped.")
    return (" ".join(parts) + " Check with assembly_get.") if parts else ""


def _dist(a, b):
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2) ** 0.5


def _plane_frame(g, inv_k):
    """A planar face's own orthonormal frame in WORLD coordinates - origin plus x_world/y_world/
    normal - or None when any part of it cannot be read (a partial frame locates nothing)."""
    def build():
        plane = adsk.core.Plane.cast(g)
        if plane is None:
            return None
        x = _geom.unit_vector(plane.uDirection)
        y = _geom.unit_vector(plane.vDirection)
        n = _geom.unit_vector(plane.normal)
        if x is None or y is None or n is None:
            return None
        o = plane.origin
        return {"origin": [round(o.x * inv_k, 3), round(o.y * inv_k, 3), round(o.z * inv_k, 3)],
                "x_world": x, "y_world": y, "normal": n}
    return safe(build)


def _face_record(face, inv_k):
    g = safe(lambda: face.geometry)
    st = safe(lambda: g.surfaceType)
    kind = {adsk.core.SurfaceTypes.CylinderSurfaceType: "cylinder_face",
            adsk.core.SurfaceTypes.PlaneSurfaceType: "planar_face",
            adsk.core.SurfaceTypes.ConeSurfaceType: "cone_face",
            adsk.core.SurfaceTypes.SphereSurfaceType: "sphere_face",
            adsk.core.SurfaceTypes.TorusSurfaceType: "torus_face"}.get(st, "face")
    c = safe(lambda: face.centroid)
    # Composite handle: token + a kind+position locator in cm, so a stale token re-resolves by geometry.
    handle = _inputs.make_handle(face, kind, (c.x, c.y, c.z)) if c else safe(lambda: face.entityToken)
    rec = {"handle": handle, "kind": kind,
            "position": [round(c.x * inv_k, 3), round(c.y * inv_k, 3), round(c.z * inv_k, 3)] if c else None,
            "area": _common.measured(lambda: face.area, inv_k * inv_k, 3)}
    # Outward normal at the reported position (constant for planar, sampled at that point for curved).
    nrm = _geom.evaluator_normal_at(face, c, decimals=4)
    if nrm is not None:
        rec["normal"] = nrm
    if kind == "planar_face":
        rec["frame"] = _plane_frame(g, inv_k)
    if kind == "cylinder_face":
        rec["radius"] = _common.measured(lambda: g.radius, inv_k, 3)
        ax = safe(lambda: g.axis)
        if ax:
            rec["axis"] = [round(ax.x, 3), round(ax.y, 3), round(ax.z, 3)]
    return rec


def _edge_record(edge, inv_k):
    g = safe(lambda: edge.geometry)
    ct = safe(lambda: g.curveType)
    kind = {adsk.core.Curve3DTypes.Circle3DCurveType: "circular_edge",
            adsk.core.Curve3DTypes.Line3DCurveType: "line_edge",
            adsk.core.Curve3DTypes.Arc3DCurveType: "arc_edge",
            adsk.core.Curve3DTypes.Ellipse3DCurveType: "ellipse_edge",
            adsk.core.Curve3DTypes.EllipticalArc3DCurveType: "elliptical_arc_edge",
            adsk.core.Curve3DTypes.NurbsCurve3DCurveType: "spline_edge"}.get(ct, "edge")
    pt = safe(lambda: edge.pointOnEdge)
    # Keyed to pointOnEdge - what _refind_by_locator compares an edge against, not the circle center
    # the display 'position' may carry below.
    handle = _inputs.make_handle(edge, kind, (pt.x, pt.y, pt.z)) if pt else safe(lambda: edge.entityToken)
    rec = {"handle": handle, "kind": kind,
            "position": [round(pt.x * inv_k, 3), round(pt.y * inv_k, 3), round(pt.z * inv_k, 3)] if pt else None,
            "length": _common.measured(lambda: edge.length, inv_k, 3)}
    if kind in ("circular_edge", "arc_edge"):
        rec["radius"] = _common.measured(lambda: g.radius, inv_k, 3)
    elif kind in ("ellipse_edge", "elliptical_arc_edge"):
        # Two radii, so the single-value 'radius' filter selects no elliptical edge.
        rec["major_radius"] = _common.measured(lambda: g.majorRadius, inv_k, 3)
        rec["minor_radius"] = _common.measured(lambda: g.minorRadius, inv_k, 3)
    if kind in _CENTERED_EDGES:
        ctr = safe(lambda: g.center)
        if ctr:
            rec["position"] = [round(ctr.x * inv_k, 3), round(ctr.y * inv_k, 3), round(ctr.z * inv_k, 3)]
    if kind == "line_edge":
        d = _geom.unit_vector_between(safe(lambda: g.startPoint), safe(lambda: g.endPoint), decimals=4)
        if d is not None:
            rec["direction"] = d
    return rec


def handler(target: str = "", kind: str = "", radius: float = None,
            nearest_to=None, units: str = "mm", max_results: int = 20) -> dict:
    """See TOOL_DESCRIPTION."""
    k = scale(units)
    if k is None:
        return error(f"Unknown units '{units}'. Use mm, cm, or in.")
    inv_k = 1.0 / k

    design = _common.design()
    if not design:
        return error("No active design (open or create a document first).")

    pairs, target_label, resolve_err, walk = _resolve_target(design, target)
    space_note = _search_space_note(walk)
    if not pairs:
        miss = (resolve_err or
                f"Could not resolve target '{target}'. Use an occurrence/component name, a body "
                "name (bare, or '<occurrence-or-component>:<body>' when several components hold "
                "that name), or '' for the whole design (see assembly_get / "
                "design_get(include=['tree'])).")
        return error(f"{miss} {space_note}".strip() if space_note else miss)

    knd = (kind or "").strip().lower()
    want_faces = (not knd) or knd in _FACE_KINDS
    want_edges = (not knd) or knd in _EDGE_KINDS
    want_verts = knd == "vertex"

    matches = []
    for occ, body in pairs:
        # BRepBody.isVisible is the EFFECTIVE state, rolling up every ancestor occurrence's bulb;
        # isLightBulbOn alone does not.
        hidden = safe(lambda body=body: body.isVisible, True) is False
        recs = []
        if want_faces:
            for f in (safe(lambda body=body: list(body.faces)) or []):
                rec = _face_record(f, inv_k)
                if knd in _FACE_KINDS and rec["kind"] != knd:
                    continue
                recs.append(rec)
        if want_edges:
            for e in (safe(lambda body=body: list(body.edges)) or []):
                rec = _edge_record(e, inv_k)
                if knd in _EDGE_KINDS and rec["kind"] != knd:
                    continue
                recs.append(rec)
        if want_verts:
            for v in (safe(lambda body=body: list(body.vertices)) or []):
                p = safe(lambda v=v: v.geometry)
                vh = _inputs.make_handle(v, "vertex", (p.x, p.y, p.z)) if p else safe(lambda v=v: v.entityToken)
                recs.append({"handle": vh, "kind": "vertex",
        "position": [round(p.x * inv_k, 3), round(p.y * inv_k, 3),
                                             round(p.z * inv_k, 3)] if p else None})
        if hidden:
            for rec in recs:
                rec["hidden"] = True
        matches.extend(recs)

    # radius filter (cylinder faces / circular edges). A record whose radius did not read carries
    # None, and cannot match a radius it never answered - it is skipped, not compared.
    if radius is not None:
        r = float(radius)
        matches = [m for m in matches if m.get("radius") is not None
                   and abs(m["radius"] - r) <= max(0.05 * r, 1e-6)]

    # sort by distance to nearest_to, else leave in discovery order
    if isinstance(nearest_to, (list, tuple)) and len(nearest_to) == 3:
        npt = [float(nearest_to[i]) for i in range(3)]
        matches = [m for m in matches if m.get("position")]
        matches.sort(key=lambda m: _dist(m["position"], npt))

    total = len(matches)
    matches = matches[:clamp_rows(max_results, _MAX_RESULTS_DEFAULT, _MAX_RESULTS_CEILING)]

    payload = {
        "target": target_label,
        "kind_filter": knd or "faces+edges",
        "match_count": total,
        "returned": len(matches),
        "units": units,
        # Always published, so a reader can tell an absent key from a whole search space.
        "occurrences_walk": walk.method if walk is not None else None,
        "matches": matches,
        "note": "A match on a body that is not visible carries hidden:true.\n"
        "A planar face's 'frame' is that plane in world space: the point at local (u, v) is "
        "frame.origin + u*frame.x_world + v*frame.y_world, and frame.origin is the plane's "
        "PARAMETRIC origin while 'position' is the centroid. Feed consumers world coordinates "
        "(model_hole points_space='world'), not a sketch frame converted by hand.",
    }
    if space_note:
        payload["note"] += "\n" + space_note
    return ok(payload)


TOOL_DESCRIPTION = (
    "Scan a part's faces/edges/vertices and return the short-lived handles other tools consume, "
    "each with kind, world position and shape data.\n"
    + _outputs.produces_block(RETURNS)
)

find_tool = (
    Tool.create_simple(name="find_geometry", description=TOOL_DESCRIPTION)
    .add_input_property("target", {"type": "string", "description":
            "Occurrence/component/body; a shared name scans every match. '' = whole design."})
    .add_input_property(*_inputs.Choice("kind",
        ["cylinder_face", "planar_face", "cone_face", "sphere_face", "torus_face",
         "circular_edge", "line_edge", "arc_edge", "ellipse_edge", "elliptical_arc_edge",
         "spline_edge", "vertex"]).as_property())
    .add_input_property("radius", {"type": "number",
            "description": "In 'units', 5% tolerance."})
    .add_input_property("nearest_to", {"type": "array", "items": {"type": "number"},
            "description": "[x,y,z] world point in 'units' to sort by."})
    .add_input_property(*_inputs.UNITS.as_property())
    .add_input_property("max_results", {"type": "integer", "description":
            f"Max {_MAX_RESULTS_CEILING}."})
    .strict_schema()
)
find_item = Item.create_tool_item(tool=find_tool, write="read", handler=handler, run_on_main_thread=True)


def register_tool():
    register(find_item)
