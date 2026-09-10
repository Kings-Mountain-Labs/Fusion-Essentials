# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""MCP building block: tessellate a BRep body into a persistent MeshBody - the inverse of
mesh_to_brep. The WRITE runs through run_in_base_feature; the read-only tessellation runs outside it.
"""

import adsk.core
import adsk.fusion

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import ok, error, safe
from . import _common
from . import _inputs
from ._design_common import run_in_base_feature

app = adsk.core.Application.get()

# quality -> the TriangleMeshQualityOptions enum member name (LOD for the tessellation calculator).
_QUALITIES = {
"low": "LowQualityTriangleMesh",
"normal": "NormalQualityTriangleMesh",
"high": "HighQualityTriangleMesh",
"very_high": "VeryHighQualityTriangleMesh",
}

# save_as_mesh's source is a BRep body to tessellate (solid OR surface).
_SAVE_BODY = _inputs.BodyRef("body", kind="any", required=True)
_SAVE_QUALITY = _inputs.Choice("quality", options=list(_QUALITIES), default="normal")


def _tessellate(body, quality_key):
    """Run the body's mesh calculator: (TriangleMesh, the quality key that reached setQuality or None,
    error). Read-only, so it runs outside the base-feature scope."""
    mm = safe(lambda: body.meshManager)
    if mm is None:
        return None, None, error("This body has no meshManager - cannot tessellate it into a mesh.")
    calc = safe(lambda: mm.createMeshCalculator())
    if calc is None:
        return None, None, error("meshManager.createMeshCalculator() returned nothing - cannot "
                                 "tessellate.")

    tmo = safe(lambda: adsk.fusion.TriangleMeshQualityOptions)
    qual = safe(lambda: getattr(tmo, _QUALITIES[quality_key])) if tmo is not None else None
    applied = None
    if qual is not None:
        # setQuality answers whether the quality took; a false leaves the DEFAULT tessellation,
        # so the file would not be at the quality the payload reports.
        if not safe(lambda: calc.setQuality(qual)):
            return None, None, error(f"Fusion refused mesh quality '{quality_key}' (setQuality "
                                     "returned false), so nothing was exported at that quality.")
        applied = quality_key

    # calculate is a real computation that can raise on a degenerate body - surface it, don't swallow.
    try:
        tm = calc.calculate()
    except Exception as e:
        return None, applied, error(f"Mesh tessellation (calculate) failed: {e}")
    if tm is None:
        return None, applied, error("Mesh calculator returned no TriangleMesh (tessellation produced "
                                    "nothing).")
    return tm, applied, None


def _weld(coords, coord_idx):
    """Merge vertices agreeing to 1e-6 cm and remap the indices: (welded_coords, welded_idx), or the
    inputs unchanged when malformed. coords is flat [x0,y0,z0, ...], coord_idx per-corner."""
    try:
        n = len(coords)
        if n == 0 or n % 3 != 0 or not coord_idx:
            return coords, coord_idx
        remap = {}                # rounded (x,y,z) -> new vertex index
        new_coords = []
        old_to_new = [0] * (n // 3)
        for v in range(n // 3):
            x, y, z = coords[3 * v], coords[3 * v + 1], coords[3 * v + 2]
            key = (round(x, 6), round(y, 6), round(z, 6))
            idx = remap.get(key)
            if idx is None:
                idx = len(new_coords) // 3
                remap[key] = idx
                new_coords.extend((x, y, z))
            old_to_new[v] = idx
        new_idx = [old_to_new[i] for i in coord_idx]
        return new_coords, new_idx
    except Exception:
        return coords, coord_idx     # never let welding block the tessellation


def handler(body: str = "", quality: str = "normal", name: str = "") -> dict:
    """Tessellate a BRep solid/surface into a persistent MeshBody in the design. WRITES."""
    design = _common.design()
    if not design:
        return error("No active design. Open or create a document first (see doc_new).")

    src, berr = _SAVE_BODY.resolve(body)
    if berr:
        return error(berr)
    if _inputs._is_mesh(src):
        return error("'body' is already a MESH body - save_as_mesh tessellates a BRep solid/surface. "
    "To re-triangulate an existing mesh use mesh_remesh; to copy/export it use "
    "mesh_export.")
    qual, qerr = _SAVE_QUALITY.resolve(quality)
    if qerr:
        return error(qerr)

    # The component that owns the source body (so the new mesh lands beside it), falling back to root.
    comp = safe(lambda: src.parentComponent) or safe(lambda: design.rootComponent)
    if comp is None:
        return error("Could not resolve a component to add the mesh body into.")

    # 1) calculate - READ-ONLY, runs OUTSIDE the base-feature scope.
    tm, applied_quality, terr = _tessellate(src, qual)
    if terr:
        return terr

    coords = safe(lambda: tm.nodeCoordinatesAsDouble)
    coord_idx = safe(lambda: tm.nodeIndices)
    normals = safe(lambda: tm.normalVectorsAsDouble)
    if coords is None or coord_idx is None:
        return error("Tessellation produced no coordinate/index data - cannot build a mesh body.")
    tri_count = safe(lambda: tm.triangleCount)

    # The calculator emits one node per triangle corner, so an unwelded mesh is topologically open
    # (isClosed=false even for a watertight solid) and mesh_to_brep refuses it. Only the coordinate
    # indices are remapped - a TriangleMesh exposes no normal index list to remap.
    coords, coord_idx = _weld(coords, coord_idx)
    node_count = len(coords) // 3

    # 2) addByTriangleMeshData - the WRITE, inside the base-feature scope when parametric.
    def _add(_base_feature):
        return comp.meshBodies.addByTriangleMeshData(coords, coord_idx, normals or [], [])

    before_mb_count = safe(lambda: comp.meshBodies.count)
    result, scope_err = run_in_base_feature(design, comp, _add)
    if scope_err:
        return scope_err
    mb = result
    if mb is None:
        return error("meshBodies.addByTriangleMeshData returned nothing - no mesh body was created.")
    # A returned body object is not proof it joined the component - the count is.
    after_mb_count = safe(lambda: comp.meshBodies.count)
    if (before_mb_count is not None and after_mb_count is not None
            and after_mb_count <= before_mb_count):
        return error("addByTriangleMeshData returned a mesh body but the component's mesh body "
                     f"count did not increase ({before_mb_count} before, {after_mb_count} after) - "
                     "the mesh body did not actually land.")

    final_name, rename_warning = _common.apply_rename(mb, name)

    mode = _inputs.current_design_type(design)
    # 'quality' is what setQuality actually took, null when this build carried no enum member for the
    # request (the calculator then ran at its own default) - the request is echoed separately so the
    # two can never be confused.
    quality_note = ("" if applied_quality is not None else
                    f" Quality '{qual}' did NOT land: this build exposes no "
                    "TriangleMeshQualityOptions member for it, so setQuality was never called and "
                    "the tessellation ran at the calculator's default level of detail. 'quality' is "
                    "null; 'quality_requested' is what was asked for.")
    payload = {
        "saved_as_mesh": True,
        "name": final_name,
        "handle": safe(lambda: mb.entityToken),
        "source_body": safe(lambda: src.name),
        "component": safe(lambda: comp.name),
        "quality": applied_quality,          # what LANDED; null when setQuality was never called
        "quality_requested": qual,
        "triangle_count": tri_count,
        "node_count": node_count,
        "note": ("Tessellated the BRep body into a persistent MESH body. " + (
            "Wrapped in a BaseFeature edit scope (parametric design requires it for a mesh write)."
            if mode == _inputs.MODE_PARAMETRIC else
            "Direct design - no base-feature scope needed.") + quality_note +
            " Inspect it with model_inspect (mesh target), edit with mesh_reduce / mesh_remesh, or "
            "export it with mesh_export."),
    }
    if rename_warning:
        payload["rename_warning"] = rename_warning
    return ok(payload)


TOOL_DESCRIPTION = (
    "Tessellate a BRep solid/surface into a persistent MESH body beside it in the design - the "
    "inverse of mesh_to_brep."
)

_SAVE_SPEC = [_SAVE_BODY, _SAVE_QUALITY]
tool = (
    _inputs.apply_to_tool(
        Tool.create_simple(name="save_as_mesh", description=TOOL_DESCRIPTION),
        _SAVE_SPEC)
    .add_input_property("name", {"type": "string",
            "description": "Names the new mesh body."})
    .strict_schema()
)
item = Item.create_tool_item(
    tool=tool, write="write", handler=handler, run_on_main_thread=True,
    verification=Verification(
        kind="inline", rung="value",
        evidence_test="tests/unit/test_save_as_mesh.py::TestSaveAsMesh"
         "::test_optional_name_renames_the_mesh"))


def register_tool():
    register(item)
