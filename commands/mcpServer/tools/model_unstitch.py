# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""MCP building block: explode a body (or specific faces) into per-face SURFACE bodies - the inverse
of model_stitch. WRITES; an unstitch of an already-loose surface is refused as the identity op it is.
"""

import adsk.core

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import error, ok, safe, target_component
from . import _common
from . import _inputs
from . import _assert
from ._surface_common import _result_body_report

app = adsk.core.Application.get()

# a whole body (BodyRef any) OR specific faces (GeometryHandleList).
_UNSTITCH_BODY = _inputs.BodyRef("target", kind="any", required=False)
_UNSTITCH_FACES = _inputs.GeometryHandleList("faces", require="face", required=False,
    description="Peeled off instead of a whole body.")


def handler(target="", faces=None, chain=True) -> dict:
    """Explode a body (or specific faces) into per-face SURFACE bodies - the inverse of stitch."""
    design = _common.design()
    if not design:
        return error("No active design. Create or open a document first (see doc_new).")

    has_faces = faces not in (None, "", [])
    has_target = bool((target or "").strip()) if isinstance(target, str) else target not in (None, [])
    if not has_faces and not has_target:
        return error("Unstitch needs a 'target' body (to fully explode) or 'faces' (to peel off).")
    if has_faces and has_target:
        return error("Pass EITHER 'target' (a whole body) OR 'faces' (specific faces), not both.")

    coll = adsk.core.ObjectCollection.create()
    input_desc = None
    if has_faces:
        face_ents, ferr = _UNSTITCH_FACES.resolve(faces)
        if ferr:
            return error(ferr)
        for f in face_ents:
            coll.add(f)
        input_desc = f"{len(face_ents)} face(s)"
    else:
        body, berr = _UNSTITCH_BODY.resolve(target)
        if berr:
            return error(berr)
        coll.add(body)
        input_desc = safe(lambda: body.name)

    root = target_component(design)
    # Census BEFORE the add: an unstitch of an ALREADY-LOOSE surface is an IDENTITY op the API
    # reports as success (measured: same bbox/area, body-count delta 0, the body merely
    # re-serialized under a new name) - the count diff below is the gate.
    hosts = ([safe(lambda f=f: f.body.parentComponent) for f in face_ents] if has_faces
             else [safe(lambda: body.parentComponent)])
    census_hosts = []
    for h in hosts:
        # identity de-dupe, not a set: component wrappers are not reliably hashable/equal-stable.
        if h is not None and all(h is not g for g in census_hosts):
            census_hosts.append(h)
    if not census_hosts:
        census_hosts = [root]
    before_count = sum(_common.body_count(h) or 0 for h in census_hosts)
    try:
        feature = root.features.unstitchFeatures.add(coll, bool(chain))
    except Exception as e:
        return error(f"Unstitch failed: {e}. (Target may already be loose surfaces, or the faces "
    "aren't unstitchable.)")
    if not feature:
        return error(_common.no_feature_error(design, "Unstitch",
                                              "The target may already be loose surfaces, or the "
                                              "faces are not unstitchable."))

    body_names, _flags = _result_body_report(feature)
    after_count = sum(_common.body_count(h) or 0 for h in census_hosts)
    if before_count and after_count == before_count and len(body_names) <= 1:
        rolled = bool(safe(lambda: feature.deleteMe(), False))
        return error(f"Unstitch divided NOTHING - '{input_desc}' produced the same body count "
                     f"({before_count}) and a single result body: the input was already a loose "
                     "surface, so this was an identity operation. "
                     + ("The feature was rolled back." if rolled
                        else "Remove the empty feature with design_delete_feature."))
    return ok({
        "unstitched": True,
        "feature": safe(lambda: feature.name),
        "input": input_desc,
        "chain": bool(chain),
        "result_bodies": body_names,
        "surface_body_count": len(body_names),
        "bodies_before": before_count,
        "bodies_after": after_count,
        "note": ("Exploded into %d surface body(ies) - each is now an open surface. Edit a face, then "
            "model_stitch to re-close." % len(body_names)),
    })


TOOL_DESCRIPTION = (
"Explode a body into per-face surface bodies - the inverse of model_stitch."
)

tool = (
    Tool.create_simple(name="model_unstitch", description=TOOL_DESCRIPTION)
    .add_input_property("target", _UNSTITCH_BODY.schema())
    .add_input_property("faces", _UNSTITCH_FACES.schema())
    .add_input_property("chain", {"type": "boolean",
            "description": "Include adjacent faces."})
    .strict_schema()
)
item = Item.create_tool_item(tool=tool, write="write", handler=handler, run_on_main_thread=True,
                             postconditions=[_assert.FeatureHealthy(),
                                             _assert.FreeEdgesChanged("opened")],
                             verification=Verification(
                                 kind="inline", rung="count",
                                 evidence_test="tests/unit/test_model_unstitch.py"
                                 "::TestUnstitchIdentityGate"
                                 "::test_identity_unstitch_on_a_loose_surface_is_refused"))


def register_tool():
    register(item)
