# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Shared reads over adsk.fusion.MeshBody: the triangle/node counts, the area+volume signal pair,
the per-mesh summary record every mesh payload publishes, and the feature's result-mesh pick.
"""

from ._common import error, ok, safe
from . import _common
from . import _inputs

MAP_BLURB = (
    "the MeshBody read substrate: _tri_count/_node_count - the displayMesh counts every mesh "
    "payload reports; _area_volume + _mesh_moved - the second, independent signal a flat triangle "
    "count is judged beside; _mesh_summary - the one per-mesh record; _result_mesh_of - the mesh a "
    "mesh feature produced; mesh_measure_of_body - the mesh analogue of a BRep measure")

# Display-mesh triangle count of the SOURCE above which reduce/remesh can exceed the 30s
# main-thread handler cap. Above it the op still runs synchronously and the result says so.
_SLOW_TRI_THRESHOLD = 250_000

_MEASURE_UNITS = _inputs.UnitField()


def _tri_count(mb):
    """The TRUE all-triangle count from displayMesh (TriangleMesh), the count to report."""
    return safe(lambda: mb.displayMesh.triangleCount)


def _node_count(mb):
    n = safe(lambda: mb.displayMesh.nodeCount)
    if n is None:
        n = safe(lambda: mb.mesh.nodeCount)
    return n


def _polygon_count(mb):
    return safe(lambda: mb.mesh.polygonCount)


# The band a before/after mesh AREA difference counts as no change at all - the area sibling of
# _common.NO_VOLUME_CHANGE_CM3, in Fusion's internal cm^2.
_NO_AREA_CHANGE_CM2 = 1e-9


def _area_volume(mb):
    """(area cm^2, volume cm^3) of a MeshBody, each None when it could not be read."""
    return _common.measured(lambda: mb.area), _common.measured(lambda: mb.volume)


def _mesh_moved(before, after):
    """True when a signal readable at both ends moved beyond its no-change band, False when every
    readable signal is flat, None when neither was readable at both ends."""
    pairs = ((before[0], after[0], _NO_AREA_CHANGE_CM2),
             (before[1], after[1], _common.NO_VOLUME_CHANGE_CM3))
    readable = [(b, a, band) for b, a, band in pairs if b is not None and a is not None]
    if not readable:
        return None
    return any(abs(a - b) > band for b, a, band in readable)


def _mesh_summary(mb, include_polygon=True, inv_scale=1.0):
    """A JSON-safe summary record for one MeshBody - reads only, area/volume scaled out of the API's
    internal cm^2/cm^3 by inv_scale (1/(units->cm factor)) squared and cubed."""
    area = safe(lambda: mb.area)
    volume = safe(lambda: mb.volume)
    rec = {
    "name": safe(lambda: mb.name),
    "handle": safe(lambda: mb.entityToken),
    "triangle_count": _tri_count(mb),
    "node_count": _node_count(mb),
    "is_closed": safe(lambda: bool(mb.isClosed)),
    "is_oriented": safe(lambda: bool(mb.isOriented)),
    "area": round(area * (inv_scale ** 2), 6) if area is not None else None,
    "volume": round(volume * (inv_scale ** 3), 6) if volume is not None else None,
    }
    if include_polygon:
        pc = _polygon_count(mb)
        if pc is not None:
            rec["polygon_count"] = pc
    return rec


def _bbox_record(mb, inv_scale):
    """bbox in display 'units' (Fusion internal cm -> units via inv_scale). Reads only."""
    bb = safe(lambda: mb.boundingBox)
    if bb is None:
        return None
    mn, mx = safe(lambda: bb.minPoint), safe(lambda: bb.maxPoint)
    if mn is None or mx is None:
        return None

    def scaled(p):
        return {"x": safe(lambda: p.x) * inv_scale, "y": safe(lambda: p.y) * inv_scale,
    "z": safe(lambda: p.z) * inv_scale}
    smn, smx = scaled(mn), scaled(mx)
    return {
    "x": smx["x"] - smn["x"], "y": smx["y"] - smn["y"], "z": smx["z"] - smn["z"],
    "min_point": smn, "max_point": smx,
    "center": {"x": (smn["x"] + smx["x"]) / 2, "y": (smn["y"] + smx["y"]) / 2,
        "z": (smn["z"] + smx["z"]) / 2},
    }


def _iter_meshes(comp):
    """Yield the MeshBodies of a component (safe over count/item)."""
    return list(_common.iter_collection(safe(lambda: comp.meshBodies)))


def _result_mesh_of(feat, fallback):
    """The MeshBody a mesh feature produced (feature.bodies), or `fallback` when it has none."""
    bodies = safe(lambda: feat.bodies)
    if bodies is not None:
        n = safe(lambda: bodies.count, 0) or 0
        if n:
            mb = safe(lambda: bodies.item(0))
            if mb is not None:
                return mb
    return fallback


def _slow_note(tri):
    """Advisory note when the source mesh risks the 30s cap, None for modest meshes."""
    if tri and tri > _SLOW_TRI_THRESHOLD:
        return ("Source mesh has %d triangles (> %d) - this op can exceed the 30s main-thread cap. It "
                        "ran synchronously here; an orchestrator should wrap large meshes in a fire-and-poll "
                        "job (kick, then re-inspect triangle_count) rather than block." % (tri, _SLOW_TRI_THRESHOLD))
    return None


def mesh_measure_of_body(mb, units="mm") -> dict:
    """Measure a MeshBody: bounding box + triangle/vertex counts + watertight (is_closed). model_inspect
    calls this when its TargetRef resolves to a mesh (a mesh has no B-Rep box/mass; this is the analogue)."""
    sf, uerr = _MEASURE_UNITS.resolve(units)
    if uerr:
        return error(uerr)
    inv_scale = 1.0 / sf if sf else 1.0
    rec = _mesh_summary(mb, inv_scale=inv_scale)
    rec["bbox"] = _bbox_record(mb, inv_scale)
    rec["units"] = (units or "mm").strip().lower()
    if rec.get("is_closed") is False:
        rec["note"] = ("This mesh is NOT watertight (is_closed=false), so it has no closed volume for "
    "mesh_to_brep to convert to a solid - repair with mesh_remesh first. Its 'volume' reads 0.0 "
    "because there is nothing enclosed to report, not because the body is empty.")
    return ok(rec)
