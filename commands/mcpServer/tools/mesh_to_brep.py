# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""MCP building block: convert a MeshBody into a BRep solid/surface - the bridge back to the BRep
tools. The write runs inside a BaseFeature edit scope in a parametric design.
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

_CONVERT_MESH = _inputs.MeshBodyRef("mesh", required=True)
_CONVERT_METHOD = _inputs.Choice("method", ["prismatic", "faceted", "organic"], default="prismatic")
_CONVERT_RES = _inputs.Choice("resolution", ["by_accuracy", "by_facet_number"], default="by_accuracy",
                              description="For method='organic'.")
_CONVERT_ACC = _inputs.Choice("accuracy", ["low", "medium", "high", "precise"], default="medium",
                              description="Organic, by_accuracy only.")
_CONVERT_OP = _inputs.Choice("operation", ["parametric", "base_feature"], default="parametric")


def _organic_available():
    """True only if the Product Design Extension method is actually present. We don't pretend: if we
    can't confirm OrganicMeshConvertMethodType exists, organic is treated as unavailable."""
    mct = safe(lambda: adsk.fusion.MeshConvertMethodTypes)
    if mct is None:
        return True
    return safe(lambda: mct.OrganicMeshConvertMethodType) is not None


def handler(mesh: str = "", method: str = "prismatic", resolution: str = "by_accuracy",
            accuracy: str = "medium", face_count: int = 0,
            operation: str = "parametric") -> dict:
    """Convert a MeshBody into a BRep solid/surface (the bridge back to the BRep tools). WRITES."""
    design = _common.design()
    if not design:
        return error("No active design. Open or create a document first (see doc_new).")
    mb, merr = _CONVERT_MESH.resolve(mesh)
    if merr:
        return error(merr)
    meth, _ = _CONVERT_METHOD.resolve(method)
    op, _ = _CONVERT_OP.resolve(operation)

    # Pre-check watertight: a non-watertight mesh isn't a closed volume, so refuse up front with the
    # actionable next step rather than letting `add` fail opaquely.
    is_closed = safe(lambda: bool(mb.isClosed))
    if is_closed is False:
        return error(
    "This mesh is NOT watertight (is_closed=false), so it has no closed volume to convert to a solid. "
    "Repair it first with mesh_remesh (or fill the holes), then retry. Refusing up front so you don't "
    "get an opaque conversion failure.")

    # ORGANIC is gated behind the Product Design Extension - be honest, do NOT silently fall back.
    if meth == "organic" and not _organic_available():
        return error(
    "method='organic' requires the Product Design Extension to be active - it is not available "
    "in this session. Use method='prismatic' (best for machined/scanned parts) or 'faceted' "
    "(exact, one BRep face per triangle, heavy), or enable the extension. Not silently falling "
    "back to a different method.")

    comp = _common.census_host(mb, _target_component(design))
    feats = safe(lambda: comp.features.meshConvertFeatures)
    if feats is None:
        return error("This design has no meshConvertFeatures collection (mesh->BRep unavailable here).")

    # The design's OWN mode, read BEFORE any scope opens: designType reads DIRECT while a
    # base-feature edit scope is open, and add() returns nothing INSIDE that scope even in a
    # parametric design - so the returned feature is no evidence of the design's mode.
    design_mode = _inputs.current_design_type(design)

    # Prismatic convert needs face groups; without them the add raises 'MESH_FAILED_BREP - Use
    # Generate Face Groups'.
    _face_groups_hint = (" If the failure mentions face groups (MESH_FAILED_BREP / 'Use Generate "
                         "Face Groups'), run mesh_generate_face_groups on this mesh first, then retry "
                         "mesh_to_brep(method='prismatic') - prismatic convert needs them."
                         if meth == "prismatic" else "")

    # add() returns nothing for a non-parametric feature (a direct design, or an add inside the
    # base-feature scope), so success is a NEW BRep body in the before/after snapshot below.
    def _brep_snapshot():
        # (physical-body key, name, handle, body). A proxy and its native carry DIFFERENT tokens, so
        # the diff keys on native_identity; the published handle stays the wrapper's own token.
        return [(_common.native_identity(b), safe(lambda b=b: b.name),
                 safe(lambda b=b: b.entityToken), b)
                for b in _common.iter_collection(safe(lambda: comp.bRepBodies))]

    def inner_op(base_feature):
        # createInput -> configure -> snapshot -> add, all INSIDE the (possibly open) base-feature scope
        # so the before/after BRep-body diff is taken in the same scope the add runs in.
        try:
            inp = feats.createInput([mb])
        except Exception as e:
            return error(f"Could not create the mesh-convert input: {e}")
        if inp is None:
            return error("meshConvertFeatures.createInput returned nothing.")

        mct = safe(lambda: adsk.fusion.MeshConvertMethodTypes)
        try:
            if meth == "prismatic":
                inp.meshConvertMethodType = safe(lambda: mct.PrismaticMeshConvertMethodType)
            elif meth == "faceted":
                inp.meshConvertMethodType = safe(lambda: mct.FacetedMeshConvertMethodType)
            else:
                inp.meshConvertMethodType = safe(lambda: mct.OrganicMeshConvertMethodType)
                res, _ = _CONVERT_RES.resolve(resolution)
                rt = safe(lambda: adsk.fusion.MeshConvertResolutionTypes)
                if res == "by_facet_number":
                    safe(lambda: setattr(inp, "meshConvertResolutionType",
                                         rt.ByFacetNumberMeshConvertResolutionType))
                    safe(lambda: setattr(inp, "numberOfFaces", int(face_count)))
                else:
                    safe(lambda: setattr(inp, "meshConvertResolutionType",
                                         rt.ByAccuracyMeshConvertResolutionType))
                    acc, _ = _CONVERT_ACC.resolve(accuracy)
                    at = safe(lambda: adsk.fusion.MeshConvertAccuracyTypes)
                    acc_map = {
                    "low": safe(lambda: at.LowMeshConvertAccuracyType),
                    "medium": safe(lambda: at.MediumMeshConvertAccuracyType),
                    "high": safe(lambda: at.HighMeshConvertAccuracyType),
                    "precise": safe(lambda: at.PreciseMeshConvertAccuracyType),
                    }
                    safe(lambda: setattr(inp, "meshConvertAccuracyType", acc_map.get(acc)))
            ot = safe(lambda: adsk.fusion.MeshConvertOperationTypes)
            if ot is not None:
                safe(lambda: setattr(inp, "meshConvertOperationType",
                                     ot.BaseFeatureMeshConvertOperationType if op == "base_feature"
                                     else ot.ParametricFeatureMeshConvertOperationType))
        except Exception as e:
            return error(f"Could not configure the mesh-convert input: {e}")

        before_keys = {k for (k, _n, _h, _b) in _brep_snapshot() if k is not None}

        # Mutation - direct call, no safe. Only an EXCEPTION is a hard failure.
        try:
            feat = feats.add(inp)
        except Exception as e:
            return error(f"Mesh->BRep conversion failed (meshConvertFeatures.add raised): {e}. "
    "A common cause is a non-watertight or very dense mesh." + _face_groups_hint)
        # The open BaseFeature can never be re-found once the scope closes, so its name is captured
        # HERE - it is what explains a null feature to the caller.
        return {"feat": feat, "before_keys": before_keys,
    "base_feature_name": safe(lambda: base_feature.name) if base_feature else None}

    result, scope_err = run_in_base_feature(design, comp, inner_op)
    if scope_err:
        return scope_err
    if isinstance(result, dict) and result.get("isError") is True:
        return result # inner_op returned a _common.error

    feat = result["feat"]
    before_keys = result["before_keys"]
    bf_name = result["base_feature_name"]

    brep_bodies = []
    # Parametric path: the feature object carries .bodies - use it directly.
    if feat is not None:
        for b in _common.iter_collection(safe(lambda: feat.bodies)):
            brep_bodies.append({"name": safe(lambda b=b: b.name),
        "handle": safe(lambda b=b: b.entityToken)})

    # Non-parametric path (feat is None) OR a feature with no readable .bodies: diff the component's
    # BRep bodies - the NEW body(ies) are the conversion result.
    if not brep_bodies:
        for (key, name, handle, _b) in _brep_snapshot():
            if key is None or key not in before_keys:
                brep_bodies.append({"name": name, "handle": handle})

    if not brep_bodies:
        # No feature AND no new BRep body appeared -> a REAL failure. Keep the face-groups hint.
        return error("Mesh->BRep conversion did not produce a BRep body. The mesh may be "
    "non-watertight or too dense to convert." + _face_groups_hint)

    note = ("Converted to BRep - find_geometry / fillet / chamfer / CAM can now act on these "
            "bodies. 'prismatic' merges flat face groups (fewest faces); 'faceted' is one face "
            "per triangle (exact, heavy).")
    if feat is None:
        note += " " + _common.null_feature_note(design, feat, bf_name, "conversion")

    return ok({
        "converted": True,
        "source_mesh": safe(lambda: mb.name),
        "brep_bodies": brep_bodies,
        "method": meth,
        "operation": op,
        "feature": safe(lambda: feat.name) if feat else None,
        "design_mode": design_mode,
        "base_feature": bf_name,
        "note": note,
    })


TOOL_DESCRIPTION = (
    "Convert a MESH body into a BRep solid/surface - the bridge back to "
    "find_geometry / fillet / CAM."
)

_CONVERT_SPEC = [_CONVERT_MESH, _CONVERT_METHOD, _CONVERT_RES, _CONVERT_ACC, _CONVERT_OP]
tool = (
    _inputs.apply_to_tool(
        Tool.create_simple(name="mesh_to_brep", description=TOOL_DESCRIPTION),
        _CONVERT_SPEC)
    .add_input_property("face_count", {"type": "integer", "description": "Organic, by_facet_number only."})
    .strict_schema()
)
item = Item.create_tool_item(
    tool=tool, write="write", handler=handler, run_on_main_thread=True,
    verification=Verification(
        kind="inline",
        rung="geometry",
        evidence_test="tests/unit/test_mesh_to_brep.py::TestMeshToBrep"
         "::test_none_feature_with_no_new_body_is_real_failure_with_hint"))


def register_tool():
    register(item)
