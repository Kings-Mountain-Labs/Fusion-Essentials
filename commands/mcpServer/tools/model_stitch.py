# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""MCP building block: join SURFACE bodies into a SOLID iff they form a watertight boundary within
'tolerance'. WRITES; became_solid is read off every result body's own isSolid, never assumed.
"""

import adsk.core

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import error, ok, safe, scale, target_component
from . import _common
from . import _inputs
from . import _assert
from ._surface_common import _OPERATION_KEYS, _feature_operation, _result_body_report

app = adsk.core.Application.get()

_STITCH_BODIES = _inputs.SurfaceBodyRefList("bodies", required=True)
_STITCH_TOLERANCE = _inputs.Distance("tolerance", allow_zero=False, allow_negative=False, required=False,
    description="Gap to close (default 0.01 mm).")


def handler(bodies=None, tolerance=None, units="mm", operation="new") -> dict:
    """Join surface bodies into a SOLID iff they form a watertight boundary within 'tolerance'."""
    op_key = (operation or "new").strip().lower()
    if op_key not in _OPERATION_KEYS:
        return error(f"Unknown operation '{operation}'. Use: new, join, cut, intersect.")

    k = scale(units)
    if k is None:
        return error(f"Unknown units '{units}'. Use mm, cm, or in.")

    design = _common.design()
    if not design:
        return error("No active design. Create or open a document first (see doc_new).")

    # SurfaceBodyRefList validates >=2 AND that EVERY input is an open surface (rejects solids up front
    # with a precise per-index message) BEFORE we mutate anything.
    surf_bodies, berr = _STITCH_BODIES.resolve(bodies)
    if berr:
        return error(berr)
    if not surf_bodies or len(surf_bodies) < 2:
        return error(f"Stitch needs at least 2 surface bodies (got "
                     f"{len(surf_bodies) if surf_bodies else 0}).")

    # tolerance: a positive length in display units -> internal cm. Default 0.01 mm.
    tol_default_cm = 0.01 * 0.1   # 0.01 mm in cm
    tol_cm, tolerr = _STITCH_TOLERANCE.resolve_scaled(tolerance, k)
    if tolerr:
        return error(tolerr)
    if tol_cm is None:
        tol_cm = tol_default_cm

    coll = adsk.core.ObjectCollection.create()
    for b in surf_bodies:
        coll.add(b)

    root = target_component(design)
    op = _feature_operation(op_key)
    try:
        tol_val = adsk.core.ValueInput.createByReal(tol_cm)
        stitch_input = root.features.stitchFeatures.createInput(coll, tol_val, op)
    except Exception as e:
        return error(f"Could not start stitch: {e}")

    try:
        feature = root.features.stitchFeatures.add(stitch_input)
    except Exception as e:
        return error(f"Stitch failed: {e}. (Surfaces must be adjacent/overlapping within tolerance.)")
    if not feature:
        return error(_common.no_feature_error(design, "Stitch"))

    # became_solid is true ONLY if every RESULT body reads a closed solid; a flag that would not
    # read makes the verdict null, NOT false, since the false branch is a gap diagnosis.
    body_names, flags = _result_body_report(feature)
    # An EMPTY result set is not that diagnosis either - it means nothing was stitched at all.
    if not flags:
        return error("Stitch reported success but the feature owns no result body - nothing was "
                     "stitched. " + _common.failed_effect_remedy(design, feature))
    if None in flags:
        became_solid = None
    else:
        became_solid = all(flags)
    # The default is a raw internal value (0.01 cm); when the caller didn't pass one, report it
    # converted INTO the caller's 'units' - reporting the bare cm number would be false for cm/in callers.
    reported_tolerance = (round(float(tolerance), 6) if tolerance is not None
                          else round(tol_default_cm / k, 6))
    payload = {
    "stitched": True,
    "feature": safe(lambda: feature.name),
    "operation": op_key,
    "tolerance": reported_tolerance,
    "units": units,
    "input_body_count": len(surf_bodies),
    "result_bodies": body_names,
    "is_solid": flags,
    "became_solid": became_solid,
    }
    if became_solid is True:
        payload["note"] = "Surfaces closed into a SOLID within tolerance."
    elif became_solid is False:
        payload["note"] = (
            f"Surfaces did NOT close into a solid within tolerance ({payload['tolerance']} {units}). "
            "The result is still a surface - increase tolerance or check for gaps/overlaps.")
    else:
        payload["unverified"] = ["became_solid"]
        payload["note"] = (
            "The stitch ran, but at least one result body's isSolid flag could not be read back, so "
            "whether the surfaces closed into a SOLID is UNVERIFIED - check the body with "
            "model_inspect or design_get(include=['tree'], tree_bodies=true).")
    return ok(payload)


TOOL_DESCRIPTION = (
"Stitch surface bodies into a solid; 'became_solid' reports whether they closed."
)

tool = (
    Tool.create_simple(name="model_stitch", description=TOOL_DESCRIPTION)
    .add_input_property("bodies", _STITCH_BODIES.schema())
    .add_input_property("tolerance", _STITCH_TOLERANCE.schema())
    .add_input_property(*_inputs.UNITS.as_property())
    .add_input_property(*_inputs.boolean_op(
        default="new", description="Applies once the result is a solid.").as_property())
    .add_required_input("bodies")
    .strict_schema()
)
item = Item.create_tool_item(
    tool=tool, write="write", handler=handler, run_on_main_thread=True,
    postconditions=[_assert.FeatureHealthy(), _assert.FreeEdgesChanged("sealed")],
    verification=Verification(
        kind="inline", rung="exists",
        evidence_test="tests/unit/test_model_stitch.py::TestStitch"
                      "::test_an_empty_result_set_is_an_error_not_a_gap_diagnosis"))


def register_tool():
    register(item)
