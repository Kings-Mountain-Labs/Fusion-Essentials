# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""MCP building block: regenerate a cleaner, more uniform triangulation of a MeshBody (repair / even
density). The write runs inside a BaseFeature edit scope in a parametric design.
"""

import adsk.core

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import error, ok, safe
from . import _common
from ._common import target_component as _target_component
from . import _inputs
from ._mesh_common import _area_volume, _mesh_moved, _result_mesh_of, _slow_note, _tri_count
from ._design_common import run_in_base_feature

app = adsk.core.Application.get()

_REMESH_MESH = _inputs.MeshBodyRef("mesh", required=True)


def handler(mesh: str = "", density: float = 0.0) -> dict:
    """Regenerate a cleaner, more uniform triangulation (repair / even density). WRITES."""
    design = _common.design()
    if not design:
        return error("No active design. Open or create a document first (see doc_new).")
    mb, merr = _REMESH_MESH.resolve(mesh)
    if merr:
        return error(merr)

    before_tri = _tri_count(mb)
    # A retriangulation can land with the triangle count flat, so the mesh's own area and volume are
    # read as a second, independent signal.
    before_av = _area_volume(mb)
    comp = _common.census_host(mb, _target_component(design))
    feats = safe(lambda: comp.features.meshRemeshFeatures)
    if feats is None:
        return error("This design has no meshRemeshFeatures collection (mesh remesh unavailable here).")

    # The design's OWN mode, read BEFORE any scope opens: designType reads DIRECT while a
    # base-feature edit scope is open, and add() returns nothing INSIDE that scope even in a
    # parametric design - so the returned feature is no evidence of the design's mode.
    design_mode = _inputs.current_design_type(design)

    def inner_op(base_feature):
        # createInput -> set -> add, all INSIDE the (possibly open) base-feature scope.
        try:
            inp = feats.createInput(mb)
        except Exception as e:
            return error(f"Could not create the mesh-remesh input: {e}")
        if inp is None:
            return error("meshRemeshFeatures.createInput returned nothing.")

        # density takes a ValueInput, not a raw float (a raw float raises in the SWIG layer), and a
        # dropped set is silent - the read-back below is what catches it.
        try:
            d = float(density)
        except Exception:
            d = 0.0
        density_applied = None
        if d > 0:
            try:
                inp.density = adsk.core.ValueInput.createByReal(d)
            except Exception as e:
                return error(f"'density' did not take on this build: {e}. Re-run without "
                             "'density' for the default remesh.")
            echoed = safe(lambda: inp.density.realValue)
            if echoed is None or abs(echoed - d) > 1e-9:
                return error(f"'density' did not land: set {d}, read back {echoed}. Re-run "
                             "without 'density' for the default remesh.")
            density_applied = d

        # add() returns nothing for a non-parametric feature (a direct design, or an add inside the
        # base-feature scope), so success is the mesh's re-read counts, not this return.
        try:
            feat = feats.add(inp)
        except Exception as e:
            return error(f"Mesh remesh failed (meshRemeshFeatures.add raised): {e}")
        # The open BaseFeature cannot be re-found once the scope closes, so capture its name here.
        return {"feat": feat, "density_applied": density_applied,
    "base_feature_name": safe(lambda: base_feature.name) if base_feature else None}

    result, scope_err = run_in_base_feature(design, comp, inner_op)
    if scope_err:
        return scope_err
    if isinstance(result, dict) and result.get("isError") is True:
        return result # inner_op returned a _common.error

    feat = result["feat"]
    bf_name = result["base_feature_name"]
    result_mesh = _result_mesh_of(feat, mb) if feat else mb
    after_tri = _tri_count(result_mesh)
    after_av = _area_volume(result_mesh)
    geometry_moved = _mesh_moved(before_av, after_av)
    out = {
    "remeshed": True,
    "changed": (after_tri != before_tri) if (before_tri and after_tri is not None) else None,
    "geometry_moved": geometry_moved,
    "name": safe(lambda: result_mesh.name),
    "handle": safe(lambda: result_mesh.entityToken),
    "before": {"triangle_count": before_tri, "area_cm2": before_av[0],
               "volume_cm3": before_av[1]},
    "after": {"triangle_count": after_tri, "area_cm2": after_av[0], "volume_cm3": after_av[1]},
    "feature": safe(lambda: feat.name) if feat else None,
    "design_mode": design_mode,
    "base_feature": bf_name,
    }
    if result.get("density_applied") is not None:
        out["density_applied"] = result["density_applied"]
    if out["changed"] is False:
        second = {True: "but its area or volume MOVED, so the retriangulation landed",
                  False: "and its area and volume did not move either",
                  None: "and its area/volume could not be read as a second check"}[geometry_moved]
        out["note"] = (f"Triangle count is unchanged ({before_tri}) {second}. Read the mesh back "
                       "with mesh_get before building on it.")
    extra = [_common.null_feature_note(design, feat, bf_name, "remesh") if feat is None else None,
             _slow_note(before_tri)]
    for note in (n for n in extra if n):
        out["note"] = (out.get("note", "") + " " + note).strip()
    return ok(out)


TOOL_DESCRIPTION = (
    "Regenerate a cleaner, more uniform triangulation of a MESH body."
)

tool = (
    Tool.create_simple(name="mesh_remesh", description=TOOL_DESCRIPTION)
    .add_input_property(_REMESH_MESH.name, _REMESH_MESH.schema())
    .add_required_input(_REMESH_MESH.name)
    .add_input_property("density", {"type": "number", "description": "Relative target density, positive. Omit for the API default."})
    .strict_schema()
)
item = Item.create_tool_item(
    tool=tool, write="write", handler=handler, run_on_main_thread=True,
    verification=Verification(
        kind="effect",
        rung="geometry",
        evidence_test="tests/unit/test_mesh_remesh.py::TestMeshRemesh"
          "::test_a_flat_count_is_judged_on_the_meshs_own_area_and_volume"))


def register_tool():
    register(item)
