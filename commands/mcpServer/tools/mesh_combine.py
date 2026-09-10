# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""MCP building block: boolean-combine MESH bodies (adsk.fusion.MeshBody) - the mesh analogue of
model_combine. Every input is validated to be a MESH body before any mutation. The write runs
through run_in_base_feature (_design_common.py) for the parametric base-feature scope requirement.
WRITES.
"""

import adsk.core
import adsk.fusion

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import error, ok, safe
from . import _common
from . import _geom
from ._common import target_component as _target_component
from . import _inputs
from ._design_common import run_in_base_feature
from ._mesh_common import _result_mesh_of, _tri_count

app = adsk.core.Application.get()

# BodyRefList(kind="mesh") kind-checks EVERY element before returning, so a BRep handle in the list
# fails the call before any mutation - what createInput(target, list[MeshBody]) needs.
_TARGET = _inputs.MeshBodyRef("target", required=True)
_TOOLS = _inputs.BodyRefList("tools", kind="mesh", required=True)
_OPERATION = _inputs.Choice("operation", ["join", "cut", "intersect", "merge"], default="join")
_ALGORITHM = _inputs.Choice("algorithm", ["legacy", "enhanced"], default="enhanced")

_SPEC = [_TARGET, _TOOLS, _OPERATION, _ALGORITHM]

# operation key -> the MeshCombineOperationTypes enum member name.
_MESH_COMBINE_MEMBERS = {
                            "join": "JoinMeshCombineType",
                            "cut": "CutMeshCombineType",
                            "intersect": "IntersectMeshCombineType",
                            "merge": "MergeMeshCombineType",
}

# algorithm key -> the MeshCombineAlgorithmTypes enum member name.
_ALGORITHMS = {
"legacy": "LegacyMeshCombineAlgorithmType",
"enhanced": "EnhancedMeshCombineAlgorithmType",
}


_UNSET = object()


def _algorithm_held(inp, at):
    """(algorithm_key, why_not) - the algorithm inp.algorithmType actually reads back, decoded
    against the enum family member by member, or (None, the reason it could not be decoded)."""
    if at is None:
        return None, ("MeshCombineAlgorithmTypes is not available on this Fusion version, so the "
                      "algorithm was never set and cannot be decoded")
    got = safe(lambda: inp.algorithmType, _UNSET)
    if got is _UNSET:
        return None, "MeshCombineFeatureInput.algorithmType could not be read back"
    for key, member in _ALGORITHMS.items():
        known = safe(lambda member=member: getattr(at, member))
        if known is not None and got == known:
            return key, ""
    return None, ("MeshCombineFeatureInput.algorithmType read back a value matching no member of "
                  "MeshCombineAlgorithmTypes")


def handler(target: str = "", tools=None, operation: str = "join",
            algorithm: str = "enhanced") -> dict:
    """See TOOL_DESCRIPTION."""
    design = _common.design()
    if not design:
        return error("No active design. Create or open a document first (see doc_new).")

    tgt, terr = _TARGET.resolve(target)
    if terr:
        return error(terr)
    tool_bodies, lerr = _TOOLS.resolve(tools)
    if lerr:
        return error(lerr)

    op_key, oerr = _OPERATION.resolve(operation)
    if oerr:
        return error(oerr)
    alg_key, aerr = _ALGORITHM.resolve(algorithm)
    if aerr:
        return error(aerr)

    # The same-body guard keys on native_identity, never Python identity: the API mints a fresh
    # wrapper per access (`is` reads False for one physical body), a native and its proxy carry
    # different tokens, and a token is document-local (two x-refs can answer one token).
    tgt_key = _common.native_identity(tgt)
    for b in tool_bodies:
        b_key = _common.native_identity(b)
        if b is tgt or (tgt_key and b_key and b_key == tgt_key):
            return error("A tool body is the same as the target - pick distinct mesh bodies "
    "(the target is combined INTO, the tools are combined FROM).")

    # Read BEFORE the mutation: the combine CONSUMES the tool bodies, and a consumed body's wrapper
    # answers .name with null.
    tool_names = [safe(lambda b=b: b.name) for b in tool_bodies]
    tgt_name = safe(lambda: tgt.name)
    # Published when the feature hands back no result bodies. It is the NATIVE's token, a different
    # string from a proxy target's own.
    tgt_token = _common.native_token(tgt)

    # A cut/intersect of meshes that do not touch reports success while consuming the tool and
    # changing nothing. A MeshBody carries no lump/shell count, but bounding boxes that do not
    # overlap prove the two cannot touch - checked here, BEFORE the add consumes the tools.
    apart = []
    for b, nm in zip(tool_bodies, tool_names):
        gap = _geom.aabb_gap(tgt, b)
        if gap is not None and gap > 0:
            apart.append({"tool": nm, "gap_cm": round(gap, 4)})
    if apart and op_key in ("cut", "intersect"):
        named = ", ".join(f"'{a['tool']}' (at least {a['gap_cm']} cm clear)" for a in apart)
        return error(f"REFUSED before combining: a {op_key} needs the tool to OVERLAP the target, "
                     f"and {named} cannot touch '{tgt_name}' (bounding boxes are separated; the gap "
                     "is a lower bound). The API would report success while consuming the tool and "
                     "changing nothing. Move the tool into the target (model_move) and combine "
                     "again. Nothing was combined and no body was consumed.")

    # The design's OWN mode, read before any scope opens: designType reads DIRECT while a
    # base-feature edit scope is open, and add() returns nothing INSIDE that scope even in a
    # parametric design - so the returned feature is no evidence of the design's mode.
    design_mode = _inputs.current_design_type(design)

    # The MeshCombine feature lives on the component that owns the target mesh.
    comp = safe(lambda: tgt.parentComponent) or _target_component(design)
    feats = safe(lambda: comp.features.meshCombineFeatures)
    if feats is None:
        return error("This design has no meshCombineFeatures collection (mesh combine unavailable "
    "here).")

    # createInput -> set operation + algorithm -> add, all inside the (possibly open) scope.
    def inner_op(base_feature):
        try:
            inp = feats.createInput(tgt, list(tool_bodies))
        except Exception as e:
            return error(f"Could not create the mesh-combine input: {e}")
        if inp is None:
            return error("meshCombineFeatures.createInput returned nothing.")

        # set_verified reads every assignment back: a SWIG proxy accepts an unknown property name
        # silently, so without the read-back a requested 'cut' would run as the default JOIN and be
        # reported as ok.
        _IN = "MeshCombineFeatureInput"
        ot = safe(lambda: adsk.fusion.MeshCombineOperationTypes)
        oerr2 = _common.set_verified(
            inp, "meshCombineOperationType",
            safe(lambda: getattr(ot, _MESH_COMBINE_MEMBERS[op_key])) if ot is not None else None,
            f"operation='{op_key}'", _IN)
        if oerr2:
            return error(oerr2)

        # algorithmType "is only effective in non-parametric mode - in parametric mode the algorithm
        # type is always LegacyMeshCombineAlgorithmType" (API doc), so a read-back mismatch is a
        # coercion, not a failure: what landed is READ OFF THE INPUT and published, or null.
        at = safe(lambda: adsk.fusion.MeshCombineAlgorithmTypes)
        aerr2 = _common.set_verified(
            inp, "algorithmType",
            safe(lambda: getattr(at, _ALGORITHMS[alg_key])) if at is not None else None,
            f"algorithm='{alg_key}'", _IN)
        algorithm_applied, algorithm_unverified = alg_key, None
        if aerr2:
            algorithm_applied, why_not = _algorithm_held(inp, at)
            if algorithm_applied is None:
                algorithm_unverified = (
                    f"{aerr2} {why_not}, so which algorithm ran is unknown.")

        # Snapshot the target's mesh body set BEFORE the add (inside inner_op so it is valid in both
        # direct and base-feature modes) so a non-parametric None return can still be reported.
        before_mesh_count = safe(lambda: comp.meshBodies.count)
        before_tri = _tri_count(tgt)

        # A falsy return is NOT a failure: this add method "Return nothing in the case where the
        # feature is non-parametric" (DIRECT design OR an add inside the BaseFeature scope). SUCCESS is
        # the resulting mesh body (the target), not the (None) feature object.
        try:
            feature = feats.add(inp)
        except Exception as e:
            return error(f"Mesh combine failed (meshCombineFeatures.add raised): {e}. (For cut / "
    "intersect the meshes must overlap; all must be MESH bodies.)")
        return {"feature": feature, "before_mesh_count": before_mesh_count,
    "before_tri": before_tri, "algorithm_applied": algorithm_applied,
    "algorithm_unverified": algorithm_unverified,
    "base_feature_name": safe(lambda: base_feature.name) if base_feature else None,
    "after_mesh_count": safe(lambda: comp.meshBodies.count)}

    result, scope_err = run_in_base_feature(design, comp, inner_op)
    if scope_err:
        return scope_err
    # inner_op may itself return a ready _common.error dict (a configure/add failure) - surface it.
    if isinstance(result, dict) and result.get("isError") is True:
        return result

    feature = result["feature"]
    after_mesh_count = result["after_mesh_count"]

    # No-op catch: body count AND target triangle count both unchanged means nothing was combined.
    # For cut/intersect the triangle count ALONE decides - the consumed tool drops the body count,
    # so requiring both signals there would let a silent no-op through with the cutter destroyed.
    before_tri = result["before_tri"]
    after_tri = _tri_count(_result_mesh_of(feature, tgt) if feature else tgt)
    if (after_mesh_count is not None and after_mesh_count == result["before_mesh_count"]
            and before_tri and after_tri == before_tri):
        return error(f"Combine reported success but the target mesh is unchanged ({before_tri} "
                     f"triangles, {after_mesh_count} mesh bodies before and after) - the tool "
                     "meshes may not overlap the target.")
    if op_key in ("cut", "intersect") and before_tri and after_tri == before_tri:
        return error(f"This {op_key} reported success but the target mesh '{tgt_name}' is UNCHANGED "
                     f"({before_tri} triangles before and after) - the tool did not overlap it. The "
                     "tool mesh was CONSUMED by the operation and could not be restored (mesh combine "
                     "keeps no tools). The AABB pre-check cannot see overlap-without-contact shapes; "
                     "this read-back catches them.")

    # The feature carries the result .bodies when one came back (result_bodies() handles a None
    # feature); with no feature the combine landed in the TARGET mesh in place - report it from the
    # pre-mutation capture.
    result_bodies = [{"name": safe(lambda b=b: b.name), "handle": safe(lambda b=b: b.entityToken)}
                     for b in _common.result_bodies(feature)]
    if not result_bodies:
        result_bodies.append({"name": tgt_name, "handle": tgt_token})

    # comp = tgt.parentComponent above: every occurrence places that one component. Tool placement
    # is measured by measure_api.py row mesh-combine-tool-lands-where-placed.
    note = ("Mesh bodies combined ('enhanced' yields fewer triangles than 'legacy'). The edit lands "
            "on the COMPONENT, so EVERY instance carries it; a tool addressed '<occurrence>:<mesh>' "
            "lands where that occurrence places it. Inspect with model_inspect (mesh target) or "
            "convert with mesh_to_brep.")
    bf_name = result["base_feature_name"]
    if feature is None:
        note += " " + _common.null_feature_note(design, feature, bf_name, "combine")
    if apart:
        # Each named tool cannot touch the TARGET; it may still have fused through another tool in
        # the same call, so nothing here claims what the result body holds.
        note += (" WARNING: " + ", and ".join(
            f"'{a['tool']}' is at least {a['gap_cm']} cm clear of the target" for a in apart)
            + " - a join cannot fuse what does not touch, so nothing of the target fused with "
              "those directly ('gap_cm' is the bounding-box separation, a LOWER BOUND on the real "
              "clearance). Check the result with model_inspect, or move them into contact "
              "(model_move) and join again.")

    payload = {
        "combined": True,
        "feature": safe(lambda: feature.name) if feature else None,
        "design_mode": design_mode,
        "base_feature": bf_name,
        "operation": op_key,
        "algorithm": result["algorithm_applied"],
        "target": tgt_name,
        "tools": tool_names,
        "result_bodies": result_bodies,
        "mesh_body_count": after_mesh_count,
        "note": note,
    }
    if result["algorithm_unverified"]:
        payload["algorithm_unverified"] = result["algorithm_unverified"]
    if apart:
        payload["disjoint_tools"] = apart
    return ok(payload)


TOOL_DESCRIPTION = (
    "Boolean-combine MESH bodies: 'tools' into 'target', the body that survives; model_combine "
    "sees only BRep solids."
)

mesh_combine_tool = _inputs.apply_to_tool(
    Tool.create_simple(name="mesh_combine", description=TOOL_DESCRIPTION),
    _SPEC).strict_schema()
mesh_combine_item = Item.create_tool_item(
    tool=mesh_combine_tool, write="write", handler=handler, run_on_main_thread=True,
    verification=Verification(
        kind="inline",
        rung="geometry",
        evidence_test="tests/unit/test_mesh_combine.py::TestNoOpGate::test_unchanged_target_bites"))


def register_tool():
    register(mesh_combine_item)
