# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""MCP building block: list the MeshBody objects (adsk.fusion.MeshBody) of a component or the whole
design. A MeshBody lives in comp.meshBodies, not comp.bRepBodies, so the BRep tools cannot see it.
"""

import adsk.core

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item
from ..mcp_primitives.registry import register
from ._common import error, ok, safe
from ._cam_common import clamp_rows
from . import _common
from . import _export          # find_component - the one design-wide by-name component resolve
from . import _inputs
from ._mesh_common import _MEASURE_UNITS, _iter_meshes, _mesh_summary

app = adsk.core.Application.get()

# mesh_get's row cap: the default a caller who names none gets, and the ceiling max_results cannot
# lift past (every mesh row crosses the wire). The names follow clamp_rows' own two parameters.
_MESH_ROWS_DEFAULT = 50
_MESH_ROWS_CEILING = 200


def handler(target: str = "", max_results: int = _MESH_ROWS_DEFAULT,
            units: str = "mm") -> dict:
    """List the MeshBody objects in a component (target name) or the whole design (target='')."""
    design = _common.design()
    if not design:
        return error("No active design. Open or create a document first (see doc_new).")
    name = (target or "").strip()

    sf, uerr = _MEASURE_UNITS.resolve(units)
    if uerr:
        return error(uerr)
    inv_scale = 1.0 / sf if sf else 1.0

    comps = []
    if not name:
        root = safe(lambda: design.rootComponent)
        if root is not None:
            comps.append(root)
        # The shared census, not a bare root.allOccurrences: that property RAISES on a design holding
        # an unresolved external reference, and the empty walk would sweep the ROOT ONLY while
        # reporting the result as design-wide.
        for o in _common.all_occurrences(design):
            c = safe(lambda o=o: o.component)
            if c is not None and c not in comps:
                comps.append(c)
    else:
        found, comp_err = _export.find_component(design, name)
        if comp_err:
            return error(comp_err + " List one instance's meshes by its occurrence "
                         "name/fullPathName (design_get(include=['tree']) lists the instances), or "
                         "pass target='' to scan the whole design.")
        if found is None:
            occ, occ_err = _inputs._resolve_occurrence("target", name)
            if occ is not None:
                found = safe(lambda: occ.component)
            elif occ_err and _inputs.OCCURRENCE_MISS not in occ_err:
                return error(occ_err)          # a REFUSAL (several instances), not a plain miss
        if found is None:
            return error(f"No component/occurrence named '{name}'. List the tree with design_get(include=['tree']), "
    "or pass target='' to scan the whole design.")
        comps = [found]

    meshes = []
    seen = set()
    for comp in comps:
        for mb in _iter_meshes(comp):
            tok = safe(lambda: mb.entityToken)
            key = tok if tok else id(mb)
            if key in seen:
                continue
            seen.add(key)
            meshes.append(_mesh_summary(mb, inv_scale=inv_scale))

    total = len(meshes)
    cap = clamp_rows(max_results, _MESH_ROWS_DEFAULT, _MESH_ROWS_CEILING)
    meshes_out = meshes[:cap]
    truncated = total > len(meshes_out)

    note = ("These are MESH bodies (not BRep). Inspect one with model_inspect (it reports mesh "
            "stats on a mesh target), edit with mesh_reduce / mesh_remesh, or convert with "
            "mesh_to_brep. A mesh has no BRep faces/edges, so find_geometry returns nothing on it. "
            "'volume' reads 0.0 on a mesh that is not watertight (is_closed=false) - it encloses "
            "nothing; a null 'volume' means the field could not be read at all.")
    if truncated:
        note += f" meshes was capped at {cap} of {total}; raise max_results to see the rest."

    return ok({
    "count": total,
    "meshes": meshes_out,
    "truncated": truncated,
    "scope": name or "(whole design)",
    "units": (units or "mm").strip().lower(),
    "note": note,
    })


TOOL_DESCRIPTION = (
    "List the MESH bodies in a component or the whole design - the BRep tools cannot see them. "
    "Convert with mesh_to_brep."
)

tool = (
    Tool.create_simple(name="mesh_get", description=TOOL_DESCRIPTION)
    .add_input_property("target", {"type": "string", "description": "Component/occurrence name; '' scans the whole design."})
    .add_input_property("max_results", {"type": "integer", "description": f"Max {_MESH_ROWS_CEILING}."})
    .add_input_property(_MEASURE_UNITS.name, _MEASURE_UNITS.schema())
    .strict_schema()
)
item = Item.create_tool_item(tool=tool, write="read", handler=handler, run_on_main_thread=True)


def register_tool():
    register(item)
