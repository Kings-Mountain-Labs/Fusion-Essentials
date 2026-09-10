# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""MCP building block: segment a MeshBody into planar FACE GROUPS. A PRISMATIC mesh->BRep conversion
REQUIRES them (mesh_to_brep's error path points here rather than auto-running it). WRITES.
"""

import adsk.core
import adsk.fusion

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import error, ok, safe
from . import _common
from ._common import target_component as _target_component
from . import _inputs
from ._design_common import run_in_base_feature

app = adsk.core.Application.get()

_FG_MESH = _inputs.MeshBodyRef("mesh", required=True)
_FG_METHOD = _inputs.Choice("method", ["fast", "accurate"], default="accurate")


def _group_ids(mb):
    """The face groups' tempIds - a per-GROUP read, so it costs the group count, not the triangles."""
    groups = safe(lambda: mb.faceGroups)
    return None if groups is None else safe(
        lambda: [groups.item(i).tempId for i in range(groups.count)])


def handler(mesh: str = "", method: str = "accurate") -> dict:
    """Segment a MeshBody into planar face groups - the required pre-step for a prismatic mesh_to_brep."""
    design = _common.design()
    if not design:
        return error("No active design. Open or create a document first (see doc_new).")
    mb, merr = _FG_MESH.resolve(mesh)
    if merr:
        return error(merr)
    meth, _ = _FG_METHOD.resolve(method)

    comp = safe(lambda: mb.parentComponent) or _target_component(design)
    feats = safe(lambda: comp.features.meshGenerateFaceGroupsFeatures)
    if feats is None:
        return error("This design has no meshGenerateFaceGroupsFeatures collection (generate face "
    "groups unavailable here).")

    # The design's OWN mode, read BEFORE any scope opens: designType reads DIRECT while a
    # base-feature edit scope is open, and add() returns nothing INSIDE that scope even in a
    # parametric design - so the returned feature is no evidence of the design's mode.
    design_mode = _inputs.current_design_type(design)
    before = safe(lambda: mb.faceGroups.count)
    before_ids = _group_ids(mb)

    def inner_op(base_feature):
        # createInput -> set method -> add, all INSIDE the (possibly open) base-feature scope.
        try:
            inp = feats.createInput(mb)
        except Exception as e:
            return error(f"Could not create the face-groups input: {e}")
        if inp is None:
            return error("meshGenerateFaceGroupsFeatures.createInput returned nothing.")
        mt = safe(lambda: adsk.fusion.MeshGenerateFaceGroupsMethodTypes)
        if mt is None:
            return error("adsk.fusion.MeshGenerateFaceGroupsMethodTypes is unavailable on this "
                         "Fusion version.")
        merr = _common.set_verified(inp, "meshGenerateFaceGroupsMethodType",
                             safe(lambda: (mt.FastGenerateFaceGroupsType if meth == "fast"
                                           else mt.AccurateGenerateFaceGroupsType)),
                             f"method='{meth}'", "MeshGenerateFaceGroupsFeatureInput")
        if merr:
            return error(merr)
        # add() returns nothing for a non-parametric feature (a direct design, or an add inside the
        # base-feature scope), so success is the mesh's own faceGroups, not this return.
        try:
            feat = feats.add(inp)
        except Exception as e:
            return error(f"Generate face groups failed "
                         f"(meshGenerateFaceGroupsFeatures.add raised): {e}")
        # The open BaseFeature cannot be re-found once the scope closes, so capture its name here.
        return {"feat": feat,
    "base_feature_name": safe(lambda: base_feature.name) if base_feature else None}

    result, scope_err = run_in_base_feature(design, comp, inner_op)
    if scope_err:
        return scope_err
    if isinstance(result, dict) and result.get("isError") is True:
        return result   # inner_op returned a _common.error() (createInput failure)

    feat = result["feat"]   # a MeshGenerateFaceGroupsFeature, or None inside a base-feature scope
    bf_name = result["base_feature_name"]
    group_count = safe(lambda: mb.faceGroups.count)
    after_ids = _group_ids(mb)
    changed = None if group_count is None or before is None else group_count != before
    ids_changed = None if after_ids is None or before_ids is None else after_ids != before_ids
    # Measured: neither read carries a verdict. A landed generation can leave the count at 1 (a mesh
    # whose whole surface is one flat region) and can leave the group ids where they stood (a repeat
    # pass reproducing them), so both are reported as observations and neither refuses.
    if feat is None and changed is None and ids_changed is None:
        return error("mesh_generate_face_groups reported no error, but add() returned no feature and "
                     "neither the mesh's face group count nor its group ids read before or after - "
                     "nothing observed what this generation did.")
    if changed is None:
        seen = "the face group count did not read before or after"
    elif changed:
        seen = f"the face group count moved from {before} to {group_count}"
    else:
        ids_seen = {True: "; the group ids moved", False: " and the group ids stood with it",
                    None: " (the group ids did not read)"}[ids_changed]
        seen = (f"the face group count reads {group_count}, where it stood before this "
                f"generation{ids_seen}")
    note = f"Face-group generation ran - {seen}. Convert with mesh_to_brep(method='prismatic')."
    if feat is None:
        note += " " + _common.null_feature_note(design, feat, bf_name, "face-group generation")

    return ok({
        "generated": True,
        "mesh": safe(lambda: mb.name),
        "method": meth,
        "feature": safe(lambda: feat.name) if feat else None,
        "design_mode": design_mode,
        "base_feature": bf_name,
        "face_group_count": group_count,
        "face_group_count_before": before,
        "changed": changed,                    # count moved; null where either read failed
        "face_group_ids_changed": ids_changed,  # the groups' tempIds moved; null where either failed
        "note": note,
    })


TOOL_DESCRIPTION = (
    "Segment a MESH body into planar FACE GROUPS - required before "
    "mesh_to_brep(method='prismatic')."
)

_FG_SPEC = [_FG_MESH, _FG_METHOD]
tool = (
    _inputs.apply_to_tool(
        Tool.create_simple(name="mesh_generate_face_groups", description=TOOL_DESCRIPTION),
        _FG_SPEC)
    .strict_schema()
)
item = Item.create_tool_item(
    tool=tool, write="write", handler=handler,
    run_on_main_thread=True,
    verification=Verification(
        kind="inline",
        rung="exists",
        evidence_test="tests/unit/test_mesh_generate_face_groups.py::TestFaceGroups"
                      "::test_a_null_feature_with_no_readable_readback_is_a_failure"))


def register_tool():
    register(item)
