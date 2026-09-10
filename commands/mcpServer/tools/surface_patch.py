# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""MCP building block: fill CLOSED loop(s) of edges with surface face(s) - cap a hole, bridge a gap.
WRITES; the patch body's isSolid is read back, never assumed to be false.
"""

import adsk.core
import adsk.fusion

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import error, ok, safe, target_component
from . import _common
from . import _inputs
from . import _assert
from ._surface_common import _body_names_and_solid

app = adsk.core.Application.get()

# Patch may only create a NEW body or a NEW component.
_PATCH_OPS = ("new", "new_body", "new_component")

_CONTINUITY = {
"connected": "ConnectedSurfaceContinuityType",
"tangent": "TangentSurfaceContinuityType",
"curvature": "CurvatureSurfaceContinuityType",
}

# boundary: the CLOSED loop a patch fills.
_BOUNDARY = _inputs.EdgeLoopRef("boundary", closed=True, required=True)
# interior_rails: B-Rep EDGES the patch surface must pass through. The API property also accepts
# sketch curves/points and construction points, but find_geometry mints handles for BRep faces and
# edges only, so an edge is the one kind this server can reference.
_INTERIOR_RAILS = _inputs.GeometryHandleList("interior_rails", require="edge", required=False)


def _rails_readback(patch_input, expected):
    """Read interiorRailsAndPoints back as (count, error) - the count the payload publishes."""
    # The read-back is a FRESH ObjectCollection, never the object assigned, so identity (and
    # set_verified's != compare) cannot verify it; an EMPTY collection's count reads None.
    got = safe(lambda: patch_input.interiorRailsAndPoints)
    n = safe(lambda: got.count)
    n = 0 if n is None else int(n)
    if n != expected:
        return n, (f"interior_rails did not take - PatchFeatureInput.interiorRailsAndPoints reads "
                   f"back {n} entity(ies) after assigning {expected}, so the patch would run "
                   "without them.")
    return n, ""


def _patch_note(is_solid):
    """The sentence a patch payload states its RESULT with - worded off the isSolid actually read
    back off the patch body, never off the expectation that a patch makes a surface."""
    if is_solid is False:
        return "Closed boundary filled with a surface (isSolid=false)."
    if is_solid is True:
        return ("Closed boundary filled, and the result reads back isSolid=true - a SOLID, not the "
                "open surface a patch normally makes.")
    return ("Closed boundary filled, but no result body's isSolid flag could be read back - "
            "whether the patch is an open surface is UNVERIFIED.")


def _patch_one_loop(comp, boundary, op, cont, cont_key, rails=()):
    """Patch ONE closed loop. boundary = a single edge handle or a list of edge handles forming one
    loop. Returns (result_dict, error_str). On success result_dict has the feature/body info; the
    error_str is None. Resolves the loop's edges via _BOUNDARY, then createInput->add."""
    resolved, berr = _BOUNDARY.resolve(boundary)
    if berr:
        return None, berr
    coll, meta = resolved
    ents = meta["entities"]
    if not ents:
        return None, "boundary resolved to no edges. Pass edge handle(s) forming a closed loop."
    # A single edge -> pass the edge itself (Fusion auto-finds the connected loop); else the collection.
    boundary_arg = ents[0] if len(ents) == 1 else coll
    try:
        patch_input = comp.features.patchFeatures.createInput(boundary_arg, op)
        # The enum class is SurfaceContinuityTypes (PLURAL) - measured live, the singular does not
        # exist. set_verified reads the value back off the input, so an unavailable member is
        # refused instead of running the patch on the API default.
        cerr = _common.set_verified(patch_input, "continuity", cont,
                                    f"continuity={cont_key}", "PatchFeatureInput")
        if cerr:
            return None, cerr
        rail_count = None
        if rails:
            rail_coll = adsk.core.ObjectCollection.create()
            for r in rails:
                rail_coll.add(r)
            patch_input.interiorRailsAndPoints = rail_coll
            rail_count, rerr = _rails_readback(patch_input, len(rails))
            if rerr:
                return None, rerr
        feature = comp.features.patchFeatures.add(patch_input)
    except Exception as e:
        msg = str(e).lower()
        # These strings ('invalid argument chainOptions', ASM_BL_NON_MAN_EDVERT, PATCH_NO_TOOLBODY)
        # are one single-seed auto-complete failure with two causes the string cannot tell apart,
        # so the error names both and points at the probe that does.
        if any(s in msg for s in ("chainoptions", "non_man", "non-man", "toolbody")):
            return None, (f"Patch failed: {e}. This failure has two known causes: (1) a degenerate "
                "TANGENT saddle opening - a radial hole tangent to a flat face splits the rim into "
                "two half-edges; pass the opening's two half-edges as an explicit boundary list. "
                "(2) an edge loop SPLIT by a later feature (e.g. a fillet reaching the opening) into "
                "more than two segments; pass ALL of the loop's edges as an explicit boundary list, "
                "or patch this opening before adding the feature that splits it. Count the opening's "
                "edges with find_geometry to tell them apart: exactly 2 means case (1), more means "
                "case (2).")
        if rails:
            return None, (f"Patch failed: {e}. With interior_rails there are two candidate causes and "
                "this message asserts neither: the boundary does not form a CLOSED loop, or a rail "
                "edge does not lie on the surface the boundary spans (a rail must be interior to the "
                "patch). Retry WITHOUT interior_rails to tell them apart: if it succeeds, the rails "
                "are the cause.")
        return None, (f"Patch failed: {e}. (The boundary must form a CLOSED loop - pass the loop's "
    "edges, or a single edge Fusion can auto-complete.)")
    if not feature:
        return None, _common.no_feature_error(_common.design(), "Patch",
                                             "(The boundary may not form a closed loop.)")
    names, is_solid = _body_names_and_solid(feature)
    if not names:
        return None, ("Patch reported success but the feature owns no result body - the loop was "
                      "not filled. "
                      + _common.failed_effect_remedy(_common.design(), feature))
    return {
    "feature": safe(lambda: feature.name),
    "result_body": names[0] if names else None,
    "result_bodies": names,
    "is_solid": is_solid,
    "boundary_edge_count": len(ents),
    "interior_rail_count": rail_count,
    }, None


def handler(boundary=None, boundaries=None, continuity: str = "connected",
            operation: str = "new", interior_rails=None) -> dict:
    """Fill closed loop(s) of edges with surface face(s) - "cap the hole(s)"."""
    op_key = (operation or "new").strip().lower()
    if op_key not in _PATCH_OPS:
        return error(f"Unknown operation '{operation}'. Patch supports: new, new_component.")
    cont_key = (continuity or "connected").strip().lower()
    if cont_key not in _CONTINUITY:
        return error(f"Unknown continuity '{continuity}'. Use: connected, tangent, curvature.")

    design = _common.design()
    if not design:
        return error("No active design. Create or open a document first (see doc_new).")
    comp = target_component(design)
    op = getattr(adsk.fusion.FeatureOperations, _common.OPERATIONS[op_key])
    cont = safe(lambda: getattr(adsk.fusion.SurfaceContinuityTypes, _CONTINUITY[cont_key]))

    has_rails = interior_rails not in (None, "", [])
    if has_rails and boundaries not in (None, "", []):
        return error("'interior_rails' fits ONE patch surface, so it goes with 'boundary' (a single "
                     "loop). With 'boundaries' every loop would be handed the same rails.")
    rail_ents = []
    if has_rails:
        rail_ents, rerr = _INTERIOR_RAILS.resolve(interior_rails)
        if rerr:
            return error(rerr)

    # Normalise to a list of loops. 'boundaries' (multi) wins; else the single 'boundary'.
    if boundaries not in (None, "", []):
        loops = boundaries if isinstance(boundaries, (list, tuple)) else [boundaries]
        multi = True
    elif boundary not in (None, "", []):
        loops = [boundary]
        multi = False
    else:
        return error("Pass 'boundary' (one loop) or 'boundaries' (a list of loops, each an edge "
    "handle Fusion auto-completes - the way to patch every hole in one call).")

    results, errors = [], []
    for i, loop in enumerate(loops):
        res, lerr = _patch_one_loop(comp, loop, op, cont, cont_key, rail_ents)
        if lerr:
            errors.append({"index": i, "error": lerr})
        else:
            results.append(res)

    # Single-loop call keeps the original flat shape (back-compat).
    if not multi:
        if errors:
            return error(errors[0]["error"])
        r = results[0]
        payload = {
        "patched": True,
        "feature": r["feature"],
        "operation": op_key,
        "continuity": cont_key,
        "result_body": r["result_body"],
        "result_bodies": r["result_bodies"],
        "is_solid": r["is_solid"],      # read off the patch body, not the module's expectation
        "boundary_edge_count": r["boundary_edge_count"],
        "note": _patch_note(r["is_solid"]),
        }
        if r["is_solid"] is None:
            payload["unverified"] = ["is_solid"]
        if r["interior_rail_count"] is not None:
            # the count PatchFeatureInput.interiorRailsAndPoints reads back, not the number asked for
            payload["interior_rail_count"] = r["interior_rail_count"]
        return ok(payload)

    # Multi-loop: report how many patched + per-loop bodies + any per-loop failures.
    all_bodies = [n for r in results for n in r["result_bodies"]]
    flags = [r["is_solid"] for r in results]
    agg = True if True in flags else (False if False in flags else None)
    payload = {
        "patched": len(results),
        "requested": len(loops),
        "failed": len(errors),
        "operation": op_key,
        "continuity": cont_key,
        "result_bodies": all_bodies,
        "patches": [{"feature": r["feature"], "bodies": r["result_bodies"],
                     "is_solid": r["is_solid"]} for r in results],
        "errors": errors,
        "is_solid": agg,
        "note": (f"Patched {len(results)} of {len(loops)} loop(s)."
                 + (" " + _patch_note(agg) if results else "")
                 + (" Some loops failed - see 'errors'." if errors else "")),
    }
    if results and agg is None:
        payload["unverified"] = ["is_solid"]
    return ok(payload)


TOOL_DESCRIPTION = (
"Fill closed edge loop(s) with surface face(s) - cap a hole, bridge a gap."
)

tool = (
    Tool.create_simple(name="surface_patch", description=TOOL_DESCRIPTION)
    .add_input_property("boundary", _BOUNDARY.schema())
    .add_input_property("boundaries", {"type": "array", "items": {"type": ["string", "array"]},
            "description": "One entry per loop: an edge handle, or a list of handles."})
    .add_input_property(*_inputs.Choice("continuity", ["connected", "tangent", "curvature"],
        default="connected").as_property())
    .add_input_property("interior_rails", _INTERIOR_RAILS.schema())
    .add_input_property(*_inputs.boolean_op(options=("new", "new_component"), default="new").as_property())
    .strict_schema()
)
item = Item.create_tool_item(tool=tool, write="write", handler=handler,
                             run_on_main_thread=True,
                             postconditions=[_assert.FeatureHealthy(), _assert.SurfaceAreaAdded()],
                             verification=Verification(
                                 kind="inline", rung="exists",
                                 evidence_test="tests/unit/test_surface_patch.py"
                                               "::TestSurfacePatch"
                                               "::test_patch_with_no_result_body_is_an_error"))


def register_tool():
    register(item)
