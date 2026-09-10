# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""MCP building block that SPLITS a MESH body into its disconnected shells - MeshSeparateFeatures.
Measured: the input body is CONSUMED and the shells are minted as auto-named mesh bodies in the same
component, so the pieces are named by a before/after census of that component. WRITES.
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

app = adsk.core.Application.get()

_MESH = _inputs.MeshBodyRef("mesh", required=True)

_SPEC = [_MESH]

# A separate that divided the mesh mints at least this many bodies. Measured: a 2-shell mesh took
# the component from 4 mesh bodies to 5 - one consumed, two minted.
_MIN_PIECES = 2


def _mesh_names(comp):
    """Every mesh body NAME in `comp`, or None when the collection cannot be read.

    A MeshFeature reports no bodies of its own, and the separate mints AUTO-NAMED bodies, so the
    only way to name the pieces is the before/after difference of this list."""
    coll = safe(lambda: comp.meshBodies)
    if coll is None:
        return None
    n = safe(lambda: coll.count)
    if n is None:
        return None
    names = []
    for mb in _common.iter_collection(coll):
        nm = safe(lambda mb=mb: mb.name)
        if nm is not None:
            names.append(nm)
    return names


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
    feats = safe(lambda: comp.features.meshSeparateFeatures)
    if feats is None:
        return error("This design has no meshSeparateFeatures collection (mesh separate "
                     "unavailable here).")

    # Identity is captured BEFORE the add because the separate CONSUMES the input body: measured,
    # the held wrapper still answers isValid=True afterwards while the body is gone from the
    # component, so no read taken after the mutation can be trusted to describe it.
    mesh_name = safe(lambda: mb.name)
    before_names = _mesh_names(comp)

    try:
        inp = feats.createInput(mb)
    except Exception as e:
        return error(f"Could not create the mesh-separate input: {e}")
    if inp is None:
        return error("meshSeparateFeatures.createInput returned nothing.")

    # A SWIG proxy ACCEPTS an assignment to a name it does not define, so a missing member would
    # leave the input on its default and still report success - set_verified reads it back.
    st = safe(lambda: adsk.fusion.MeshSeparateTypes)
    serr = _common.set_verified(inp, "meshSeparateType",
                                safe(lambda: st.ShellMeshSeparateType),
                                "shell separation", "MeshSeparateFeatureInput")
    if serr:
        return error(serr)

    try:
        feature = feats.add(inp)
    except Exception as e:
        return error(f"Mesh separate failed (meshSeparateFeatures.add raised): {e}")

    # Measured: meshSeparateFeatures.add returns a real MeshSeparateFeature at PLAIN parametric
    # scope, and returns None in a DIRECT design while the split LANDS (5 mesh bodies -> 6). The
    # component census below is the verdict; the return value never is.
    direct_no_feature = _common.direct_feature_absence(design, feature)
    if not feature and not direct_no_feature:
        return error(_common.no_feature_error(design, "Mesh separate"))

    after_names = _mesh_names(comp)
    if before_names is None or after_names is None:
        return error("Mesh separate raised no error, but the component's mesh body list could not "
                     "be read back - whether the mesh was divided is UNVERIFIED, so it is reported "
                     "as a failure. Check the component with mesh_get.")

    was = set(before_names)
    pieces = [n for n in after_names if n not in was]
    if len(pieces) < _MIN_PIECES:
        appeared = (f"{len(pieces)} new body name appeared" if len(pieces) == 1
                    else f"{len(pieces)} new body names appeared")
        msg = (f"Mesh separate did not divide '{mesh_name}': the component held "
               f"{len(before_names)} mesh bodies before and {len(after_names)} after, and "
               f"{appeared}. A separate that splits a mesh mints at least {_MIN_PIECES}.")
        # Measured signature of a single-shell mesh: the input is consumed, ONE body appears and the
        # count is unchanged - the same shell under a new auto-name. That is visible right here, so
        # the caller is told rather than left to re-derive it from mesh_get.
        if len(pieces) == 1 and mesh_name not in set(after_names):
            msg += (f" '{mesh_name}' is no longer in the component - the single new body "
                    f"'{pieces[0]}' is that same shell renamed.")
        return error(msg + " " + _common.failed_effect_remedy(design, feature))

    payload = {
        "separated": True,
        "mesh": mesh_name,
        "component": safe(lambda: comp.name),
        "pieces": pieces,
        "piece_count": len(pieces),
        "input_consumed": mesh_name not in set(after_names),
        "mesh_body_count": {"before": len(before_names), "after": len(after_names)},
    }
    note = (f"Mesh split into {len(pieces)} shells. 'pieces' names them as Fusion auto-named them, "
            "read back from the component - a mesh feature reports no bodies of its own. Re-read "
            "them with mesh_get.")
    if payload["input_consumed"]:
        note += f" The input body '{mesh_name}' was consumed by the split."
    if direct_no_feature:
        payload["no_timeline_feature"] = True
        note += " " + _common.DIRECT_FEATURE_NOTE
    else:
        payload["feature"] = safe(lambda: feature.name)
    payload["note"] = note
    return ok(payload)


TOOL_DESCRIPTION = (
    "Split a MESH body into its disconnected shells; the input body is CONSUMED."
)

tool = _inputs.apply_to_tool(
    Tool.create_simple(name="mesh_separate", description=TOOL_DESCRIPTION), _SPEC).strict_schema()
item = Item.create_tool_item(
    tool=tool, write="write", handler=handler, run_on_main_thread=True,
    verification=Verification(
        kind="inline",
        rung="geometry",
        evidence_test="tests/unit/test_mesh_separate.py::TestVerification"
                      "::test_no_new_bodies_is_an_error_naming_the_census"))


def register_tool():
    register(item)
