# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""The Fusion SELECTION substrate: the per-entity record both selection tools return."""

import adsk.core

from ._common import safe, measured
from . import _geom
from . import _inputs
from . import _outputs

app = adsk.core.Application.get()

MAP_BLURB = (
    "the Fusion SELECTION substrate: _selection_record - the ONE per-entity record "
    "sys_request_selection and sys_get_selection both publish (a _classify() dispatch by runtime "
    "type, the click point, and a find_geometry-style 'handle' for a face/edge/vertex); RETURNS - "
    "the handle output both declare; _ui - the userInterface read, None when there is none")

RETURNS = [
    _outputs.ReturnsHandle("handle", require="any", in_list=True, consumers=[
        "joint_at_geometry", "model_extrude", "model_fillet", "model_chamfer", "model_construction"]),
]


def _ui():
    return safe(lambda: app.userInterface)


def _component_of(entity):
    occ = safe(lambda: entity.assemblyContext)
    if occ is not None:
        return safe(lambda: occ.component.name), safe(lambda: occ.fullPathName)
    comp = safe(lambda: entity.parentComponent)
    if comp is not None:
        return safe(lambda: comp.name), None
    body = safe(lambda: entity.body)
    if body is not None:
        return safe(lambda: body.parentComponent.name), None
    return None, None


def _xyz(pt):
    """{x, y, z} rounded to 6dp, or None when the point is absent or any component will not read."""
    if pt is None:
        return None
    x = safe(lambda: pt.x)
    y = safe(lambda: pt.y)
    z = safe(lambda: pt.z)
    if not all(isinstance(c, (int, float)) and not isinstance(c, bool) for c in (x, y, z)):
        return None
    return {"x": round(x, 6), "y": round(y, 6), "z": round(z, 6)}


def _face_direction(face):
    """(direction_unit_vector, direction_kind) for a face - a planar face's normal or a
    cylinder/cone/torus axis - or (None, None)."""
    surf = safe(lambda: face.geometry)
    stype = safe(lambda: type(surf).__name__) if surf is not None else None
    if stype == "Plane":
        n = safe(lambda: surf.normal)
        if n is None:
            # Evaluator fallback: normal at the face centroid.
            res = safe(lambda: face.evaluator.getNormalAtPoint(face.centroid))
            if res and res[0]:
                n = res[1]
        return _geom.unit_vector(n), "face_normal"
    if stype in ("Cylinder", "Cone", "Torus"):
        return _geom.unit_vector(safe(lambda: surf.axis)), "axis"
    if stype == "Sphere":
        return None, None  # a sphere has no single axis/normal
    # Other analytic/spline surfaces: try the evaluator normal at the centroid.
    res = safe(lambda: face.evaluator.getNormalAtPoint(face.centroid))
    if res and res[0]:
        return _geom.unit_vector(res[1]), "face_normal"
    return None, None


def _edge_direction(edge):
    """The DIRECTION of a linear edge (end - start), or (axis) of a circular edge. (vec, kind).
    The end-minus-start-then-normalize arithmetic is the shared _geom.unit_vector_between (also
    find_geometry's, off a Line3D's startPoint/endPoint rather than a BRepEdge's vertices)."""
    crv = safe(lambda: edge.geometry)
    ctype = safe(lambda: type(crv).__name__) if crv is not None else None
    if ctype == "Line3D":
        sp = safe(lambda: edge.startVertex.geometry)
        ep = safe(lambda: edge.endVertex.geometry)
        if sp is not None and ep is not None:
            return _geom.unit_vector_between(sp, ep), "edge_direction"
    if ctype in ("Circle3D", "Arc3D", "Ellipse3D"):
        # A circular/arc edge's "direction" is its plane normal (the rotation axis).
        return _geom.unit_vector(safe(lambda: crv.normal)), "axis"
    return None, None


def _classify(entity) -> dict:
    """Structured description keyed off the entity's runtime type."""
    tname = safe(lambda: type(entity).__name__) or "Unknown"
    out = {"object_type": tname}

    if tname == "BRepFace":
        comp, path = _component_of(entity)
        direction, dir_kind = _face_direction(entity)
        out.update({
        "kind": "face",
        "surface_type": safe(lambda: type(entity.geometry).__name__),
        "area_cm2": measured(lambda: entity.area),
        "centroid": _xyz(safe(lambda: entity.centroid)),
        "direction": direction,        # planar -> normal; cyl/cone/torus -> axis (unit vec)
        "direction_kind": dir_kind,    # face_normal | axis | None
        "edge_count": safe(lambda: entity.edges.count),
        "body_name": safe(lambda: entity.body.name),
        "component": comp, "component_path": path,
        })
    elif tname == "BRepEdge":
        comp, path = _component_of(entity)
        direction, dir_kind = _edge_direction(entity)
        out.update({
        "kind": "edge",
        "curve_type": safe(lambda: type(entity.geometry).__name__),
        "length_cm": measured(lambda: entity.length),
        "start": _xyz(safe(lambda: entity.startVertex.geometry)),
        "end": _xyz(safe(lambda: entity.endVertex.geometry)),
        "direction": direction,        # linear -> end-start; circular -> plane normal (unit vec)
        "direction_kind": dir_kind,    # edge_direction | axis | None
        "body_name": safe(lambda: entity.body.name),
        "component": comp, "component_path": path,
        })
    elif tname == "BRepVertex":
        comp, path = _component_of(entity)
        out.update({
        "kind": "vertex",
        "position": _xyz(safe(lambda: entity.geometry)),
        "body_name": safe(lambda: entity.body.name),
        "component": comp, "component_path": path,
        })
    elif tname == "BRepBody":
        out.update({
        "kind": "body",
        "name": safe(lambda: entity.name),
        "is_solid": safe(lambda: entity.isSolid),
        "volume_cm3": measured(lambda: entity.volume),
        "area_cm2": measured(lambda: entity.area),
        "component": safe(lambda: entity.parentComponent.name),
        "component_path": safe(lambda: entity.assemblyContext.fullPathName),
        })
    elif tname == "Occurrence":
        out.update({
        "kind": "component",
        "name": safe(lambda: entity.name),
        "component_name": safe(lambda: entity.component.name),
        "full_path": safe(lambda: entity.fullPathName),
        "is_reference": safe(lambda: entity.isReferencedComponent),
        })
    elif tname == "Component":
        out.update({"kind": "component", "name": safe(lambda: entity.name)})
    else:
        out.update({"kind": "other", "name": safe(lambda: entity.name)})
    return out


def _geometry_handle(entity, kind):
    """A find_geometry-style self-healing handle (_inputs.make_handle) for a face/edge/vertex
    entity, minted through the SAME seam; None for a body/component/other selection."""
    if kind not in ("face", "edge", "vertex"):
        return None
    if kind == "face":
        p = safe(lambda: entity.centroid)
    elif kind == "edge":
        p = safe(lambda: entity.pointOnEdge)
    else:
        p = safe(lambda: entity.geometry)
    if p is None:
        return safe(lambda: entity.entityToken)
    return _inputs.make_handle(entity, kind, (p.x, p.y, p.z))


def _selection_record(sel):
    """One selection's full description: _classify() fields + the click point + a handle (face/edge/
    vertex only) - the ONE record shape both tools build."""
    entity = safe(lambda: sel.entity)
    rec = _classify(entity) if entity is not None else {"object_type": None, "kind": "unknown"}
    rec["picked_point"] = _xyz(safe(lambda: sel.point))
    if entity is not None:
        h = _geometry_handle(entity, rec.get("kind"))
        if h:
            rec["handle"] = h
    return rec
