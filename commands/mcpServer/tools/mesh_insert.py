# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""MCP building block: import an STL / OBJ / 3MF from a local path as a MeshBody. In a PARAMETRIC
design the import runs inside a BaseFeature edit scope (run_in_base_feature), which the API requires.
"""

import os

import adsk.core
import adsk.fusion

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import error, ok, safe
from . import _common
from ._common import target_component as _target_component
from . import _export          # find_component - the one design-wide by-name component resolve
from ._mesh_common import _mesh_summary
from ._design_common import run_in_base_feature

app = adsk.core.Application.get()

# A parametric mesh WRITE must run inside a BaseFeature edit scope, which run_in_base_feature opens
# and closes. That scope's open-state is undetectable from the public API (BaseFeature has no
# isEditing), so a recheck after startEdit reads false-negative on a write that succeeded.

_VALID_EXTS = (".stl", ".obj", ".3mf")

# authored unit -> (MeshUnits enum member the import takes, cm per unit its stats are scaled by).
# mm/cm/in take their factor from the shared _common.scale(); m and ft are exact multiples of those
# (1 m = 1000 mm, 1 ft = 12 in), so this wider authored set holds no cm constant of its own.
_MESH_UNIT_TABLE = {
    "mm": ("MillimeterMeshUnit", _common.scale("mm")),
    "cm": ("CentimeterMeshUnit", _common.scale("cm")),
    "m": ("MeterMeshUnit", 1000.0 * _common.scale("mm")),
    "in": ("InchMeshUnit", _common.scale("in")),
    "inch": ("InchMeshUnit", _common.scale("inch")),
    "ft": ("FootMeshUnit", 12.0 * _common.scale("in")),
}


def _mesh_units(units):
    """(MeshUnits enum value, unit key, cm per unit) for an authored-unit string. The enum is what the
    import API wants; the factor scales what the payload reports back. Guarded with safe so a
    mocked/absent enum degrades to None (the caller then errors honestly)."""
    u = (units or "mm").strip().lower()
    row = _MESH_UNIT_TABLE.get(u)
    mu = safe(lambda: adsk.fusion.MeshUnits)
    if row is None or mu is None:
        return None, u, None
    return safe(lambda: getattr(mu, row[0])), u, row[1]


def _insert_meshes(comp, design, full_path, mesh_units):
    """Import through run_in_base_feature; returns (mesh_list, base_feature_name, error or None)."""

    def inner_op(base_feature):
        # meshBodies.add takes the open BaseFeature (parametric) or None (direct) as its third arg.
        try:
            mesh_list = comp.meshBodies.add(full_path, mesh_units, base_feature)
        except Exception as e:
            return error(f"Mesh import failed (meshBodies.add raised): {e}")
        return {"mesh_list": mesh_list,
    "base_feature_name": safe(lambda: base_feature.name) if base_feature else None}

    result, scope_err = run_in_base_feature(design, comp, inner_op)
    if scope_err:
        return None, None, scope_err
    if isinstance(result, dict) and result.get("isError") is True:
        return None, None, result # inner_op returned a _common.error (meshBodies.add raised)

    return result["mesh_list"], result["base_feature_name"], None


def handler(file_path: str = "", target_component: str = "",
            units: str = "mm", name: str = "") -> dict:
    """Import an STL/OBJ/3MF from a local path as a MeshBody into the active (or named) component.
    In a PARAMETRIC design the import is wrapped in a BaseFeature edit scope (API-required). WRITES."""
    path = (file_path or "").strip()
    if not path:
        return error("file_path is required - a full path to a .stl / .obj / .3mf file.")
    ext = os.path.splitext(path)[1].lower()
    if ext not in _VALID_EXTS:
        return error(f"Unsupported mesh file '{ext or path}'. Import needs one of: "
                     f"{', '.join(_VALID_EXTS)}.")
    if not safe(lambda: os.path.isfile(path)):
        return error(f"File not found: {path}. (To import from the data model, first resolve the file "
                     "to a local path with the data_* tools, then pass that path.)")

    design = _common.design()
    if not design:
        return error("No active design. Open or create a document first (see doc_new).")

    comp = _target_component(design)
    tc = (target_component or "").strip() if isinstance(target_component, str) else ""
    if tc:
        picked, comp_err = _export.find_component(design, tc)
        if comp_err:
            return error(comp_err + " Omit target_component to import into the ACTIVE component, "
                         "and set which that is with design_activate_component (it takes the "
                         "occurrence, so it can name one of them).")
        if picked is None:
            return error(f"No component named '{tc}' to import into. Omit target_component to use the "
    "active component, or list components with design_get(include=['tree']).")
        comp = picked

    mesh_units, ukey, unit_cm = _mesh_units(units)
    if mesh_units is None:
        return error(f"Unknown units '{units}' for mesh import. Use mm, cm, m, in, or ft.")

    mesh_list, bf_name, ins_err = _insert_meshes(comp, design, path, mesh_units)
    if ins_err:
        return ins_err

    count = safe(lambda: mesh_list.count, 0) or 0
    if not mesh_list or count == 0:
        return error("Mesh import returned no bodies (the file may be empty or unreadable as a mesh).")

    inv_scale = 1.0 / unit_cm
    want_name = (name or "").strip()
    bodies = []
    rename_warning = None
    name_applied = None
    for mb in _common.iter_collection(mesh_list):
        # count is the LANDED body count read off what meshBodies.add returned - a name addresses
        # one body, so it is applied only when exactly one landed, never silently dropped.
        if want_name and count == 1:
            _final, rename_warning = _common.apply_rename(mb, want_name)
            name_applied = rename_warning is None
        bodies.append(_mesh_summary(mb, inv_scale=inv_scale))

    payload = {
        "imported": True,
        "bodies": bodies,
        "component": safe(lambda: comp.name),
        "units": ukey,
        "base_feature": bf_name,
        "file": path,
        "note": ("Imported as MESH body(ies). " + (
            "Wrapped in BaseFeature '%s' (parametric design requires it)." % bf_name if bf_name
            else "Direct design - no base-feature scope needed.") +
            " Convert to BRep with mesh_to_brep to use find_geometry / fillet / CAM on it."),
    }
    if want_name and count != 1:
        name_applied = False
        payload["note"] = payload["note"] + (
            " 'name' was not applied: the import landed %d mesh bodies - rename each with "
            "design_set_name, using the names in 'bodies'." % count)
    if want_name:
        payload["name_applied"] = bool(name_applied)
    if rename_warning:
        payload["rename_warning"] = rename_warning
    return ok(payload)


TOOL_DESCRIPTION = (
    "Import an STL / OBJ / 3MF from a LOCAL path as a MESH body."
)

tool = (
    Tool.create_simple(name="mesh_insert", description=TOOL_DESCRIPTION)
    .add_input_property("file_path", {"type": "string", "description": "Local path to the mesh file."})
    .add_input_property("target_component", {"type": "string", "description": "Default: the active component."})
    .add_input_property("units", {"type": "string", "enum": list(_MESH_UNIT_TABLE),
            "description": "The unit the file is authored in; stats are reported in it."})
    .add_input_property("name", {"type": "string", "description": "Renames the imported body; single-body imports only."})
    .add_required_input("file_path")
    .strict_schema()
)
item = Item.create_tool_item(
    tool=tool, write="write", handler=handler, run_on_main_thread=True,
    verification=Verification(
        kind="inline",
        rung="value",
        evidence_test="tests/unit/test_mesh_insert.py::TestMeshInsert"
          "::test_empty_import_result_errors"))


def register_tool():
    register(item)
