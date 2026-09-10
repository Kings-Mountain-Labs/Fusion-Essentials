# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""MCP building block that DELETES one MESH body - the mesh-side delete design_delete_feature and
design_delete_occurrence cannot reach. Parametric designs get a timeline MeshRemoveFeature, direct
designs a MeshBody.deleteMe(); either way the mesh is re-resolved afterwards. DESTRUCTIVE.
"""

import adsk.core
import adsk.fusion

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import ok, error, safe
from . import _common
from . import _inputs

app = adsk.core.Application.get()

_MESH = _inputs.MeshBodyRef("mesh", required=True)

_SPEC = [_MESH]


def _count_named_in_component(design, comp_name, name):
    """How many meshes named `name` live in the component named `comp_name`."""
    n = 0
    for c, m in _common.all_meshes(design):
        if safe(lambda c=c: c.name) == comp_name and safe(lambda m=m: m.name) == name:
            n += 1
    return n


def handler(mesh: str = "") -> dict:
    """See TOOL_DESCRIPTION."""
    design = _common.design()
    if not design:
        return error("No active design. Create or open a document first (see doc_new).")

    vals, verr = _inputs.resolve_inputs(_SPEC, {"mesh": mesh})
    if verr:
        return verr
    mb = vals["mesh"]

    name = safe(lambda: mb.name)
    comp = _common.census_host(mb, _common.target_component(design))
    comp_name = safe(lambda: comp.name)
    n_before = _count_named_in_component(design, comp_name, name)

    mode = _inputs.current_design_type(design)

    if mode == _inputs.MODE_PARAMETRIC:
        feats = safe(lambda: comp.features.meshRemoveFeatures)
        if feats is None:
            return error("This design has no meshRemoveFeatures collection (parametric mesh delete "
                         "unavailable here).")

        # createInput binds std::vector, so it takes a plain list - an ObjectCollection raises. This
        # add() must run OUTSIDE a base-feature scope: inside one the design presents as direct and
        # the remove raises "Mesh remove only available in parametric mode".
        try:
            inp = feats.createInput([mb])
        except Exception as e:
            return error(f"Could not create the mesh-remove input: {e}")
        if inp is None:
            return error("meshRemoveFeatures.createInput returned nothing.")
        try:
            feat = feats.add(inp)
        except Exception as e:
            return error(f"Mesh delete failed (meshRemoveFeatures.add raised): {e}")
        deleted_via = "meshRemoveFeatures"
        feature_name = safe(lambda: feat.name) if feat else None
    else:
        # DIRECT: no timeline to preserve - a straight deleteMe() is the natural direct-mode delete.
        # Not safe()-wrapped: a real failure must surface, never a swallowed no-op.
        try:
            did = bool(mb.deleteMe())
        except Exception as e:
            return error(f"deleteMe() failed: {e}")
        if not did:
            n_now = _count_named_in_component(design, comp_name, name)
            return error(f"deleteMe() answered false for mesh '{name}' - it was NOT deleted "
                         f"({n_now} mesh(es) named '{name}' resolve in component '{comp_name}' "
                         f"now, {n_before} before the call).")
        deleted_via = "MeshBody.deleteMe"
        feature_name = None

    # Survivor check: only a fresh collection walk is trustworthy after a remove - the held
    # wrapper's isValid stays True and an entityToken lookup still resolves the pre-remove body.
    remaining = len(_common.all_meshes(design))
    n_after = _count_named_in_component(design, comp_name, name)
    if n_after != n_before - 1:
        return error(f"Delete reported success but a mesh named '{name}' still resolves in "
                     f"component '{comp_name}' - treat the delete as failed.")

    return ok({
        "deleted": name,
        "component": comp_name,
        "deleted_via": deleted_via,
        "feature": feature_name,
        "remaining_meshes": remaining,
        "note": ("Mesh body removed. (design_delete_feature / design_delete_occurrence don't reach "
                "mesh bodies - this is the mesh-side delete.)"),
    })


TOOL_DESCRIPTION = (
"Delete a MESH body - design_delete_feature and design_delete_occurrence cannot reach one."
)

tool = _inputs.apply_to_tool(
    Tool.create_simple(name="mesh_delete", description=TOOL_DESCRIPTION), _SPEC).strict_schema()
item = Item.create_tool_item(
    tool=tool, write="destructive", handler=handler, run_on_main_thread=True,
    verification=Verification(
        kind="inline",
        rung="value",
        evidence_test="tests/unit/test_mesh_delete.py::TestDirectDelete"
                      "::test_a_survivor_after_success_is_an_error"))


def register_tool():
    register(item)
