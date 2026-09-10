# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""MCP building block that FLIPS the normals of a MESH body - MeshReverseNormalFeatures. Unlike its
BRep namesake surface_reverse_normal, the mesh path is createInput(mesh) -> add(input) and the input
carries no options at all. WRITES.
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

app = adsk.core.Application.get()

_MESH = _inputs.MeshBodyRef("mesh", required=True)

_SPEC = [_MESH]

# A reverse negates each normal component exactly (measured); this floor only absorbs float noise.
_NEGATION_TOLERANCE = 1e-9


def _normals(mb):
    """The mesh's normal vectors as one flat x,y,z list, or None when unreadable.

    Measured: a reverse NEGATES this list elementwise. is_closed and is_oriented do NOT move across
    a reverse, so neither is evidence of one."""
    return safe(lambda: list(mb.mesh.normalVectorsAsDouble))


def _negated(before, after) -> bool:
    """True when `after` is `before` negated component by component.

    An all-zero list would satisfy the sum test trivially, so at least one component must be
    non-zero for the comparison to carry a verdict."""
    if not before or not after or len(before) != len(after):
        return False
    if not any(v != 0.0 for v in before):
        return False
    return all(abs(a + b) <= _NEGATION_TOLERANCE for a, b in zip(before, after))


def handler(mesh: str = "") -> dict:
    """See TOOL_DESCRIPTION."""
    design = _common.design()
    if not design:
        return error("No active design. Open or create a document first (see doc_new).")

    vals, verr = _inputs.resolve_inputs(_SPEC, {"mesh": mesh})
    if verr:
        return verr
    mb = vals["mesh"]

    comp = _common.census_host(mb, _target_component(design))
    feats = safe(lambda: comp.features.meshReverseNormalFeatures)
    if feats is None:
        return error("This design has no meshReverseNormalFeatures collection (mesh reverse normal "
                     "unavailable here).")

    mesh_name = safe(lambda: mb.name)
    before_volume = _geom.signed_volume(mb)
    before_normals = _normals(mb)

    try:
        inp = feats.createInput(mb)
    except Exception as e:
        return error(f"Could not create the mesh-reverse-normal input: {e}")
    if inp is None:
        return error("meshReverseNormalFeatures.createInput returned nothing.")

    # MeshReverseNormalFeatureInput carries only `mesh` and `targetBaseFeature` - there is nothing
    # to configure between createInput and add.
    try:
        feature = feats.add(inp)
    except Exception as e:
        return error(f"Mesh reverse normal failed (meshReverseNormalFeatures.add raised): {e}")

    # Measured: meshReverseNormalFeatures.add returns a real MeshReverseNormalFeature at PLAIN
    # parametric scope, and returns None in a DIRECT design while the flip LANDS (the signed volume
    # changed sign). The reads below are the verdict; the return value never is.
    direct_no_feature = _common.direct_feature_absence(design, feature)
    if not feature and not direct_no_feature:
        return error(_common.no_feature_error(design, "Mesh reverse normal"))

    # The signed volume is the cheap gate - measured 1.0 -> -1.0 cm3 on a closed box - and the
    # per-node normals are the deep check that still works when the volume reads zero.
    after_volume = _geom.signed_volume(mb)
    volume_comparable = (before_volume is not None and after_volume is not None
                         and before_volume != 0.0 and after_volume != 0.0)
    volume_sign_flipped = bool(volume_comparable
                               and (before_volume > 0.0) != (after_volume > 0.0))
    after_normals = _normals(mb)
    normals_comparable = bool(before_normals) and bool(after_normals)
    normals_negated = _negated(before_normals, after_normals)

    if not volume_comparable and not normals_comparable:
        return error("Mesh reverse normal raised no error, but neither the body's signed volume nor "
                     "its per-node normals could be read back - the flip is UNVERIFIED, so it is "
                     "reported as a failure. is_closed and is_oriented do not move across a "
                     "reverse, so they cannot stand in.")
    if not volume_sign_flipped and not normals_negated:
        return error(f"Mesh reverse normal reported success but '{mesh_name}' still points the same "
                     f"way - the signed volume kept its sign and the per-node normals are "
                     f"unchanged. " + _common.failed_effect_remedy(design, feature))

    payload = {
        "reversed": True,
        "mesh": mesh_name,
        "handle": safe(lambda: mb.entityToken),
        "volume_sign_flipped": volume_sign_flipped if volume_comparable else None,
        "normals_negated": normals_negated if normals_comparable else None,
    }
    note = ("Mesh normals flipped. is_closed and is_oriented do not move across a reverse, so they "
            "are not evidence - the flip reported here is the signed volume and the per-node "
            "normals. Pair with view_screenshot to see which way the shell now faces.")
    if direct_no_feature:
        payload["no_timeline_feature"] = True
        note += " " + _common.DIRECT_FEATURE_NOTE
    else:
        payload["feature"] = safe(lambda: feature.name)
    payload["note"] = note
    return ok(payload)


TOOL_DESCRIPTION = (
    "Flip the normals of a MESH body - what an inside-out imported mesh needs."
)

tool = _inputs.apply_to_tool(
    Tool.create_simple(name="mesh_reverse_normal", description=TOOL_DESCRIPTION),
    _SPEC).strict_schema()
item = Item.create_tool_item(
    tool=tool, write="write", handler=handler, run_on_main_thread=True,
    verification=Verification(
        kind="inline",
        rung="geometry",
        evidence_test="tests/unit/test_mesh_reverse_normal.py::TestVerification"
                      "::test_nothing_moving_is_an_error"))


def register_tool():
    register(item)
