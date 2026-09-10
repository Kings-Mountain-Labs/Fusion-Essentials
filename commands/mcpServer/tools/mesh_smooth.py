# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""MCP building block that SMOOTHS a MESH body - MeshSmoothFeatures. Measured: a smooth moves the
node COORDINATES while triangle count, vertex count and is_closed all hold still, so the coordinate
sample is the only fact that can judge it and a count census cannot. WRITES.
"""

import adsk.core
import adsk.fusion

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import error, ok, safe
from . import _common
from ._common import target_component as _target_component
from . import _geom
from . import _inputs
from ._mesh_common import _node_count, _tri_count

app = adsk.core.Application.get()

_MESH = _inputs.MeshBodyRef("mesh", required=True)

_SPEC = [_MESH]

# MeshSmoothFeatureInput.smoothness is a UNITLESS strength factor - measured: 0.5 lands on the
# feature as the expression '0.5', not as a length. The API documents the range as 0 to 1 and this
# guard is what makes that claim fail when it is false.
_SMOOTHNESS_MIN, _SMOOTHNESS_MAX = 0.0, 1.0

# The volume change is always reported as measured; at or below this the note ALSO carries the
# lower-smoothness advice. It is an advice trigger and nothing else - the number is the fact.
_VOLUME_ADVICE_PERCENT = -50.0


def _coords(mb):
    """The mesh's node coordinates as one flat x,y,z list, or None when unreadable. This is the
    fact a smooth moves - the triangle and vertex counts measurably do not."""
    return safe(lambda: list(mb.mesh.nodeCoordinatesAsDouble))


def _moved_nodes(before, after) -> int:
    """How many nodes sit at a different coordinate in `after` than in `before`."""
    n = min(len(before), len(after)) // 3
    return sum(1 for i in range(n)
               if before[3 * i] != after[3 * i]
               or before[3 * i + 1] != after[3 * i + 1]
               or before[3 * i + 2] != after[3 * i + 2])


def handler(mesh: str = "", smoothness=None) -> dict:
    """See TOOL_DESCRIPTION."""
    design = _common.design()
    if not design:
        return error("No active design. Open or create a document first (see doc_new).")

    vals, verr = _inputs.resolve_inputs(_SPEC, {"mesh": mesh})
    if verr:
        return verr
    mb = vals["mesh"]

    smooth = None
    if smoothness not in (None, ""):
        try:
            smooth = float(smoothness)
        except (TypeError, ValueError):
            return error(f"'smoothness' must be a number between {_SMOOTHNESS_MIN} and "
                         f"{_SMOOTHNESS_MAX} (got '{smoothness}').")
        if not (_SMOOTHNESS_MIN <= smooth <= _SMOOTHNESS_MAX):
            return error(f"'smoothness' must be between {_SMOOTHNESS_MIN} and {_SMOOTHNESS_MAX} "
                         f"(got {smooth}).")

    comp = _common.census_host(mb, _target_component(design))
    feats = safe(lambda: comp.features.meshSmoothFeatures)
    if feats is None:
        return error("This design has no meshSmoothFeatures collection (mesh smooth unavailable "
                     "here).")

    mesh_name = safe(lambda: mb.name)
    before_coords = _coords(mb)
    before_tri, before_nodes = _tri_count(mb), _node_count(mb)
    before_volume = _geom.signed_volume(mb)

    try:
        inp = feats.createInput(mb)
    except Exception as e:
        return error(f"Could not create the mesh-smooth input: {e}")
    if inp is None:
        return error("meshSmoothFeatures.createInput returned nothing.")

    # smoothness is a unitless core.ValueInput (a bare number is refused) that does NOT survive
    # set_verified - a read-back hands out a different proxy - so it is confirmed off the feature's
    # ModelParameter after the add. Omitted, the property is left alone for the API's own default.
    if smooth is not None:
        try:
            inp.smoothness = adsk.core.ValueInput.createByReal(smooth)
        except Exception as e:
            return error(f"Could not set the smoothness: {e}")

    try:
        feature = feats.add(inp)
    except Exception as e:
        return error(f"Mesh smooth failed (meshSmoothFeatures.add raised): {e}")

    # Measured: meshSmoothFeatures.add returns a real MeshSmoothFeature at PLAIN parametric scope,
    # and returns None in a DIRECT design while the smooth LANDS (the coordinates moved). The
    # coordinate diff below is the verdict; the return value never is.
    direct_no_feature = _common.direct_feature_absence(design, feature)
    if not feature and not direct_no_feature:
        return error(_common.no_feature_error(design, "Mesh smooth"))

    after_coords = _coords(mb)
    if not before_coords or not after_coords:
        return error("Mesh smooth raised no error, but the mesh node coordinates could not be read "
                     "back. Triangle and vertex counts hold still across a smooth, so there is "
                     "nothing else to judge it on - the smooth is UNVERIFIED and reported as a "
                     "failure.")
    moved_nodes = _moved_nodes(before_coords, after_coords)
    if before_coords == after_coords:
        return error(f"Mesh smooth reported success but every one of the {len(after_coords) // 3} "
                     f"nodes of '{mesh_name}' is at its original coordinate - nothing was "
                     f"smoothed. " + _common.failed_effect_remedy(design, feature))

    # A percentage, not a scaled length: this tool takes no measurement in display units, so it
    # publishes no unit-bearing number either.
    after_volume = _geom.signed_volume(mb)
    volume_change_percent = None
    if before_volume and after_volume is not None:
        volume_change_percent = round((after_volume - before_volume) / before_volume * 100.0, 3)

    # mesh_nodes counts the PolygonMesh nodes the coordinate diff runs over. It is a different
    # quantity from vertex_count (displayMesh.nodeCount), which duplicates a vertex per hard edge.
    mesh_nodes = len(after_coords) // 3
    payload = {
        "smoothed": True,
        "mesh": mesh_name,
        "handle": safe(lambda: mb.entityToken),
        "nodes_moved": moved_nodes,
        "mesh_nodes": mesh_nodes,
        "triangle_count": {"before": before_tri, "after": _tri_count(mb)},
        "vertex_count": {"before": before_nodes, "after": _node_count(mb)},
        "volume_change_percent": volume_change_percent,
    }

    # smoothness is READ BACK off the feature's own ModelParameter, never echoed - and a null means
    # the API's own default was left in place, which is a different fact from an unreadable one.
    got = safe(lambda: feature.smoothness.value) if feature else None
    if got is None:
        payload["smoothness"] = None
        if smooth is not None:
            payload["smoothness_unverified"] = "smoothness could not be read back off the feature."
    elif smooth is not None and abs(float(got) - smooth) > 1e-6:
        return error(f"The smooth was created with smoothness = {got}, but {smooth} was requested "
                     f"- Fusion did not take the value. "
                     + _common.failed_effect_remedy(design, feature))
    else:
        payload["smoothness"] = float(got)

    note = f"Mesh smoothed - {moved_nodes} of {mesh_nodes} mesh nodes moved."
    if volume_change_percent is not None:
        note += f" Enclosed volume changed {volume_change_percent} percent."
        if volume_change_percent <= _VOLUME_ADVICE_PERCENT:
            note += " Re-run with a lower smoothness if that is more than intended."
    note += " Re-read the body with mesh_get."
    if direct_no_feature:
        payload["no_timeline_feature"] = True
        note += " " + _common.DIRECT_FEATURE_NOTE
    else:
        payload["feature"] = safe(lambda: feature.name)
    payload["note"] = note
    return ok(payload)


TOOL_DESCRIPTION = (
    "Smooth a MESH body - relaxes scan noise and faceting."
)

tool = (
    _inputs.apply_to_tool(
        Tool.create_simple(name="mesh_smooth", description=TOOL_DESCRIPTION), _SPEC)
    .add_input_property("smoothness", {"type": "number",
                                       "description": "Unitless strength. Omit for the API "
                                                      "default."})
    .strict_schema()
)
item = Item.create_tool_item(
    tool=tool, write="write", handler=handler, run_on_main_thread=True,
    verification=Verification(
        kind="inline",
        rung="geometry",
        evidence_test="tests/unit/test_mesh_smooth.py::TestVerification"
                      "::test_a_smooth_that_moved_no_node_is_an_error"))


def register_tool():
    register(item)
