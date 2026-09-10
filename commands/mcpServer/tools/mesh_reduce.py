# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""MCP building block: decimate a MeshBody to a target triangle/face count, a percent proportion, or
a maximum deviation. The write runs inside a BaseFeature edit scope in a parametric design.
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
from ._mesh_common import _result_mesh_of, _slow_note, _tri_count
from ._design_common import run_in_base_feature

app = adsk.core.Application.get()

_REDUCE_MESH = _inputs.MeshBodyRef("mesh", required=True)
_REDUCE_TARGET = _inputs.Choice("target", ["proportion", "face_count", "max_deviation"],
                                default="proportion", description="What 'value' means.")
_REDUCE_METHOD = _inputs.Choice("method", ["adaptive", "uniform"], default="adaptive")
_REDUCE_UNITS = _inputs.UnitField()


def handler(mesh: str = "", target: str = "proportion", value: float = 0.0,
            method: str = "adaptive", units: str = "mm") -> dict:
    """Decimate a mesh to a target triangle/face count, a percent proportion, or a max deviation.
    WRITES (a MeshReduceFeature on the timeline)."""
    design = _common.design()
    if not design:
        return error("No active design. Open or create a document first (see doc_new).")
    mb, merr = _REDUCE_MESH.resolve(mesh)
    if merr:
        return error(merr)
    tgt, terr = _REDUCE_TARGET.resolve(target)
    if terr:
        return error(terr)
    meth, _ = _REDUCE_METHOD.resolve(method)
    sf, uerr = _REDUCE_UNITS.resolve(units)
    if uerr:
        return error(uerr)

    try:
        v = float(value)
    except Exception:
        return error("'value' must be a number.")
    if tgt == "proportion" and not (0 < v <= 100):
        return error("For target=proportion, 'value' is a percent in (0, 100].")
    face_target = None
    if tgt == "face_count":
        if v <= 0:
            return error("For target=face_count, 'value' must be a positive integer face count - "
                         f"got {v}.")
        if not v.is_integer():
            return error(f"For target=face_count, 'value' must be a WHOLE face count - {v} is not an "
                         "integer. Truncating it here would silently decimate to a different (or "
                         "zero) target, so pass the exact integer you mean.")
        face_target = int(v)
    if tgt == "max_deviation" and v <= 0:
        return error("For target=max_deviation, 'value' must be a positive length (in 'units').")

    before_tri = _tri_count(mb)
    comp = _common.census_host(mb, _target_component(design))
    feats = safe(lambda: comp.features.meshReduceFeatures)
    if feats is None:
        return error("This design has no meshReduceFeatures collection (mesh reduce unavailable here).")

    # The design's OWN mode, read BEFORE any scope opens: designType reads DIRECT while a
    # base-feature edit scope is open, and add() returns nothing INSIDE that scope even in a
    # parametric design - so the returned feature is no evidence of the design's mode.
    design_mode = _inputs.current_design_type(design)

    def inner_op(base_feature):
        # createInput -> set -> add, all INSIDE the (possibly open) base-feature scope.
        try:
            inp = feats.createInput(mb)
        except Exception as e:
            return error(f"Could not create the mesh-reduce input: {e}")
        if inp is None:
            return error("meshReduceFeatures.createInput returned nothing.")

        tt = safe(lambda: adsk.fusion.MeshReduceTargetTypes)
        try:
            # proportion/facecount/maximumDeviation each take an adsk.core.ValueInput; the API
            # rejects a raw float ("argument 2 of type Ptr<ValueInput>").
            if tgt == "proportion":
                inp.meshReduceTargetType = safe(lambda: tt.ProportionMeshReduceTargetType)
                inp.proportion = adsk.core.ValueInput.createByReal(v)   # PERCENT as-is (25 = 25%)
            elif tgt == "face_count":
                inp.meshReduceTargetType = safe(lambda: tt.FaceCountMeshReduceTargetType)
                # all-lowercase 'facecount' spelling (confirmed live)
                inp.facecount = adsk.core.ValueInput.createByReal(float(face_target))  # face COUNT
            else:
                inp.meshReduceTargetType = safe(lambda: tt.MaximumDeviationMeshReduceTargetType)
                inp.maximumDeviation = adsk.core.ValueInput.createByReal(v * sf)   # length, scaled to cm
            mt = safe(lambda: adsk.fusion.MeshReduceMethodTypes)
            if mt is not None:
                inp.meshReduceMethodType = safe(lambda: (mt.UniformReduceType if meth == "uniform"
                                                         else mt.AdaptiveReduceType))
        except Exception as e:
            return error(f"Could not configure the mesh-reduce input: {e}")

        # add() returns nothing for a non-parametric feature (a direct design, or an add inside the
        # base-feature scope), so success is the mesh's re-read triangle count, not this return.
        try:
            feat = feats.add(inp)
        except Exception as e:
            return error(f"Mesh reduce failed (meshReduceFeatures.add raised): {e}")
        # The open BaseFeature cannot be re-found once the scope closes, so capture its name here.
        return {"feat": feat,
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
    # A reduce that leaves the count where it was reduced nothing - report that, not success.
    # (proportion 100 is the one legitimate keep-everything request.)
    if (before_tri and after_tri is not None and after_tri >= before_tri
            and not (tgt == "proportion" and v >= 100)):
        return error(f"Reduce reported success but the triangle count did not decrease "
                     f"({before_tri} -> {after_tri}). The mesh may already be at/below the "
                     "target; treat it as unreduced.")
    out = {
    "reduced": True,
    "name": safe(lambda: result_mesh.name),
    "handle": safe(lambda: result_mesh.entityToken),
    "before": {"triangle_count": before_tri},
    "after": {"triangle_count": after_tri},
    "feature": safe(lambda: feat.name) if feat else None,
    "design_mode": design_mode,
    "base_feature": bf_name,
    "target": tgt,
    }
    if face_target is not None:
        out["face_count_target"] = face_target      # the integer that reached the feature input
    if before_tri and after_tri is not None and before_tri > 0:
        out["reduced_pct"] = round((1 - after_tri / before_tri) * 100, 2)
    notes = [_common.null_feature_note(design, feat, bf_name, "reduce") if feat is None else None,
             _slow_note(before_tri)]
    note = " ".join(n for n in notes if n)
    if note:
        out["note"] = note
    return ok(out)


TOOL_DESCRIPTION = "Decimate (reduce the triangle count of) a MESH body."

_REDUCE_SPEC = [_REDUCE_MESH, _REDUCE_TARGET, _REDUCE_METHOD, _REDUCE_UNITS]
tool = (
    _inputs.apply_to_tool(
        Tool.create_simple(name="mesh_reduce", description=TOOL_DESCRIPTION),
        _REDUCE_SPEC)
    .add_input_property("value", {"type": "number", "description": "A percent, a face count, or a length in 'units' - as 'target' selects."})
    .add_required_input("value")
    .strict_schema()
)
item = Item.create_tool_item(
    tool=tool, write="write", handler=handler, run_on_main_thread=True,
    verification=Verification(
        kind="inline",
        rung="geometry",
        evidence_test="tests/unit/test_mesh_reduce.py::TestMeshReduce"
          "::test_unreduced_count_is_an_error_not_success"))


def register_tool():
    register(item)
