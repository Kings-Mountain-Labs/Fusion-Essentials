# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""MCP building block: extend an OPEN surface body outward from its open edges. WRITES; the landed
distance is read back off the created ExtendFeature's own ModelParameter, never echoed.
"""

import adsk.core
import adsk.fusion

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import error, ok, safe, scale, target_component
from . import _common
from . import _inputs
from . import _assert
from ._surface_common import _body_names_and_solid, _landed_length

app = adsk.core.Application.get()

_EXTEND_TYPES = {
"natural": "NaturalSurfaceExtendType",
"tangent": "TangentSurfaceExtendType",
"perpendicular": "PerpendicularSurfaceExtendType",
}
# SurfaceExtendAlignment's member names are BARE - no '...SurfaceExtendAlignment' suffix, unlike
# every neighbouring enum. set_verified turns a name that does not resolve into a refusal rather
# than a silent default.
_EXTEND_ALIGNMENTS = {
"free_edges": "FreeEdges",
"align_edges": "AlignEdges",
}

_EXTEND_EDGES = _inputs.EdgeLoopRef("edges", closed=False, required=True)


def handler(edges=None, distance: float = 0.0, units: str = "mm",
            extend_type: str = "natural", chaining: bool = True,
            extend_alignment: str = "") -> dict:
    """Extend a surface outward from its open edges."""
    k = scale(units)
    if k is None:
        return error(f"Unknown units '{units}'. Use mm, cm, or in.")
    if distance == 0:
        return error("Provide a non-zero 'distance' to extend.")
    et_key = (extend_type or "natural").strip().lower()
    if et_key not in _EXTEND_TYPES:
        return error(f"Unknown extend_type '{extend_type}'. Use: natural, tangent, perpendicular.")
    ea_key = (extend_alignment or "").strip().lower()
    if ea_key and ea_key not in _EXTEND_ALIGNMENTS:
        return error(f"Unknown extend_alignment '{extend_alignment}'. Use: free_edges, align_edges.")

    design = _common.design()
    if not design:
        return error("No active design. Create or open a document first (see doc_new).")
    comp = target_component(design)

    resolved, eerr = _EXTEND_EDGES.resolve(edges)   # enforces single-body open chain before mutating
    if eerr:
        return error(eerr)
    coll, meta = resolved
    if not meta["entities"]:
        return error("'edges' resolved to no edges. Pass the outer edges of ONE surface body.")

    dist_val = adsk.core.ValueInput.createByReal(float(distance) * k)
    ext_type = getattr(adsk.fusion.SurfaceExtendTypes, _EXTEND_TYPES[et_key])
    try:
        ext_input = comp.features.extendFeatures.createInput(coll, dist_val, ext_type, bool(chaining))
        # extendAlignment has no createInput slot, so it is a post-createInput write - and a fresh
        # input's extendAlignment reads 0 (measured), so an omitted value writes nothing.
        if ea_key:
            align = safe(lambda: getattr(adsk.fusion.SurfaceExtendAlignment,
                                         _EXTEND_ALIGNMENTS[ea_key]))
            aerr = _common.set_verified(ext_input, "extendAlignment", align,
                                        f"extend_alignment={ea_key}", "ExtendFeatureInput")
            if aerr:
                return error(aerr)
        feature = comp.features.extendFeatures.add(ext_input)
    except Exception as e:
        return error(f"Extend failed: {e}. (Extend the OUTER edges of ONE open body; tangent/"
    "perpendicular need edges connected at endpoints.)")
    if not feature:
        return error(_common.no_feature_error(design, "Extend"))

    names, any_solid = _body_names_and_solid(feature)
    landed, rerr = _landed_length(lambda: feature.distance.value, float(distance) * k, k,
                                  "surface extend", "distance")
    if rerr:
        return error(rerr + " " + _common.failed_effect_remedy(design, feature))
    payload = {
        "extended": True,
        "feature": safe(lambda: feature.name),
        "extend_type": et_key,
        "result_body": names[0] if names else None,
        "result_bodies": names,
        "is_solid": any_solid,
        "distance": round(float(distance), 6),
        "units": units,
        "note": "Surface extended from its open edges.",
    }
    unverified = ["is_solid"] if any_solid is None else []
    if landed is None:
        unverified.append("distance")
    else:
        payload["distance"] = landed
    if unverified:
        payload["unverified"] = unverified
        payload["note"] += " Not read back off the feature: " + ", ".join(unverified) + "."
    if ea_key:
        payload["extend_alignment"] = ea_key
    return ok(payload)


TOOL_DESCRIPTION = (
"Extend an open surface outward from its open edges."
)
tool = (
    Tool.create_simple(name="surface_extend", description=TOOL_DESCRIPTION)
    .add_input_property("edges", _EXTEND_EDGES.schema())
    .add_input_property("distance", {"type": "number", "description": "In 'units'; non-zero."})
    .add_input_property(*_inputs.UNITS.as_property())
    .add_input_property(*_inputs.Choice("extend_type", ["natural", "tangent", "perpendicular"],
        default="natural").as_property())
    .add_input_property("chaining", {"type": "boolean", "description": "Follow the connected edge chain."})
    .add_input_property(*_inputs.Choice("extend_alignment", ["free_edges", "align_edges"],
        description="Alignment of the extended side edges.").as_property())
    .add_required_input("edges")
    .add_required_input("distance")
    .strict_schema()
)
item = Item.create_tool_item(tool=tool, write="write", handler=handler,
                             run_on_main_thread=True,
                             postconditions=[_assert.FeatureHealthy(), _assert.SurfaceAreaAdded()],
                             verification=Verification(
                                 kind="inline", rung="value",
                                 evidence_test="tests/unit/test_surface_extend.py"
                                               "::TestSurfaceExtend"
                                               "::test_distance_that_reads_back_wrong_is_an_error"))


def register_tool():
    register(item)
