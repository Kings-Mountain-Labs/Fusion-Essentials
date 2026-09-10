# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""MCP building block: split a body into pieces, or split its faces, with a cutter.

  model_split -> the SplitBody / SplitFace feature, dispatched by 'split'. The cutter is a plane
                 (SplitBodyFeatureInput / SplitFaceFeatureInput 'splittingTool') or another body/
                 surface. WRITES.
"""

import adsk.core
import adsk.fusion

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import error, ok, safe, target_component
from . import _common
from . import _assert
from . import _geom
from . import _inputs
from . import _outputs


RETURNS = [
    _outputs.ReturnsName("feature", of="feature", consumers=["design_delete_feature"],
                         absent_when="no_timeline_feature"),
    _outputs.ReturnsValue("result_count",
                          "split=body: the number of resulting bodies; split=face: the net face-count increase"),
]

_SPLIT = _inputs.Choice("split", ["body", "face"], default="body")
_TARGET = _inputs.BodyRef("target", kind="brep")
_FACES = _inputs.GeometryHandleList("faces", require="face")
# The cutter, supplied ONE of two ways: a plane (alias/name/planar-face handle) OR a body/surface.
_PLANE = _inputs.PlaneRef("split_plane")
_TOOLBODY = _inputs.BodyRef("split_tool_body", kind="brep")

app = adsk.core.Application.get()


def _resolve_cutter(split_plane, split_tool_body):
    """Resolve the single splitting tool from EXACTLY ONE of split_plane / split_tool_body. Returns
    (entity, error_string). Giving both, or neither, is a guarded error (the cutter is one entity)."""
    has_plane = isinstance(split_plane, str) and split_plane.strip()
    has_body = isinstance(split_tool_body, str) and split_tool_body.strip()
    if has_plane and has_body:
        return None, ("Give the cutter ONE way: 'split_plane' OR 'split_tool_body', not both.")
    if not has_plane and not has_body:
        return None, ("No cutter. Pass 'split_plane' (a plane) or 'split_tool_body' (a body/surface) "
                      "to split with.")
    if has_plane:
        return _PLANE.resolve(split_plane)
    return _TOOLBODY.resolve(split_tool_body)


def _health_error(design, feature):
    """An error result if the feature computed with a health ERROR, else None."""
    if safe(lambda: feature.healthState) == adsk.fusion.FeatureHealthStates.ErrorFeatureHealthState:
        msg = safe(lambda: feature.errorOrWarningMessage) or "no detail"
        return error(f"Split feature was created but failed to compute: {msg}. The cutter may not "
                     "fully cross the target - try extend_tool=true or a larger cutter. "
                     + _common.failed_effect_remedy(design, feature))
    return None


def _split_body(design, comp, target, cutter, extend_tool):
    """Split a solid body with the cutter, reporting the resulting body count honestly."""
    body, berr = _TARGET.resolve(target)
    if berr:
        return error(berr)
    if body is None:
        return error("'target' is required for split=body (the body to split).")
    # Captured BEFORE the mutation: the census host, counted again afterwards rather than
    # re-derived, and the target's name, which a post-mutation proxy can stop answering.
    host = _common.census_host(body, comp)
    before_count = _common.body_count(host)
    body_name = safe(lambda: body.name)
    try:
        si = comp.features.splitBodyFeatures.createInput(body, cutter, bool(extend_tool))
        feature = comp.features.splitBodyFeatures.add(si)
    except Exception as e:
        return error(f"Split body failed: {e}. (The cutter must fully cross the body - try "
                     "extend_tool=true, or a larger cutter/plane.)")
    # splitBodyFeatures.add returns None in a DIRECT design while the split lands; with no feature
    # to read result bodies off, the component's body census is the verdict.
    direct_no_feature = _common.direct_feature_absence(design, feature)
    if not feature and not direct_no_feature:
        return error(_common.no_feature_error(design, "Split body"))
    herr = _health_error(design, feature)
    if herr:
        return herr

    if direct_no_feature:
        after_count = _common.body_count(host)
        if before_count is None or after_count is None:
            return error("Split body ran in a DIRECT design, which returns no feature object, and "
                         "the component's body count could not be read back - so whether the body "
                         "was divided is UNVERIFIED. Check with design_get(include=['tree']).")
        # One body divided into N pieces raises the count by N-1, in the TARGET's own component.
        rc = after_count - before_count + 1
        names = None                      # the pieces were COUNTED, never named - see the payload
    else:
        fb = safe(lambda: feature.bodies)
        rc = safe(lambda: fb.count, 0) if fb else 0
        names = [safe(lambda b=b: b.name) for b in _common.iter_collection(fb)]
    # A split that yields a single body did not divide anything.
    if rc < 2:
        return error(f"Split produced {rc} body - the cutter did not divide "
                     f"'{body_name}'. It must fully intersect the body; try "
                     "extend_tool=true or a cutter that crosses it. "
                     + _common.failed_effect_remedy(design, feature))
    payload = {
        "split": "body",
        "target": body_name,
        "result_count": rc,
        "note": "Body split into pieces. Pair with design_get(include=['tree']) / view_screenshot.",
    }
    if direct_no_feature:
        payload["no_timeline_feature"] = True
        payload["note"] += (" " + _common.DIRECT_FEATURE_NOTE + " The pieces were COUNTED from the "
                            "component's body census, not named - list them with "
                            "design_get(include=['tree']).")
    else:
        payload["feature"] = safe(lambda: feature.name)
        payload["result_bodies"] = names
    return ok(payload)


def _split_face(design, comp, faces, cutter, extend_tool):
    """Split the given faces with the cutter, reporting the net face-count increase honestly."""
    face_ents, ferr = _FACES.resolve(faces)
    if ferr:
        return error(ferr)
    if not face_ents:
        return error("'faces' is required for split=face (the faces to split).")

    # Dedupe by entityToken, NOT by identity: face.body hands back a FRESH PROXY on every read, so
    # an id()-keyed dedupe would keep one body once per face and sum its change that many times.
    bodies = _geom.owning_bodies(face_ents)
    before = _geom.face_counts(bodies)
    coll = adsk.core.ObjectCollection.create()
    for f in face_ents:
        coll.add(f)
    try:
        si = comp.features.splitFaceFeatures.createInput(coll, cutter, bool(extend_tool))
        feature = comp.features.splitFaceFeatures.add(si)
    except Exception as e:
        return error(f"Split face failed: {e}. (The cutter must cross the faces - try "
                     "extend_tool=true or a larger cutter.)")
    # A DIRECT design can return no feature while the split lands; the before/after face census on
    # the owning bodies needs none. In parametric a None feature stays an error.
    direct_no_feature = _common.direct_feature_absence(design, feature)
    if not feature and not direct_no_feature:
        return error(_common.no_feature_error(design, "Split face"))
    herr = _health_error(design, feature)
    if herr:
        return herr

    # `bodies` was resolved BEFORE the mutation and is counted again here - the same objects.
    delta, readable = _geom.face_count_delta(bodies, before)
    if direct_no_feature:
        if not readable:
            return error("Split face ran in a DIRECT design, which returns no feature object, and "
                         "the owning bodies' face count could not be read back - so whether the "
                         "faces were split is UNVERIFIED. Check with model_inspect / find_geometry.")
        created = None
        no_op = delta <= 0
    else:
        ff = safe(lambda: feature.faces)
        created = safe(lambda: ff.count, 0) if ff else 0
        if not readable:
            delta = created
        no_op = delta <= 0 and created == 0
    if no_op:
        return error(f"Split produced no new faces - the cutter did not cross the {len(face_ents)} "
                     "target face(s). It must intersect them; try extend_tool=true or a larger "
                     "cutter. " + _common.failed_effect_remedy(design, feature))
    payload = {
        "split": "face",
        "faces_targeted": len(face_ents),
        "result_count": delta,
        "note": "Faces split; result_count is the net face-count increase. Pair with view_screenshot.",
    }
    if direct_no_feature:
        payload["no_timeline_feature"] = True
        payload["note"] += (" " + _common.DIRECT_FEATURE_NOTE + " result_count is the measured "
                            "before/after face count on the owning bodies.")
    else:
        payload["feature"] = safe(lambda: feature.name)
        payload["faces_created"] = created
    return ok(payload)


def handler(split: str = "body", target: str = "", faces=None, split_plane: str = "",
            split_tool_body: str = "", extend_tool: bool = True) -> dict:
    """See TOOL_DESCRIPTION."""
    kind, kerr = _SPLIT.resolve(split)
    if kerr:
        return error(kerr)

    design = _common.design()
    if not design:
        return error("No active design. Create or open a document first (see doc_new).")
    comp = target_component(design)

    cutter, cerr = _resolve_cutter(split_plane, split_tool_body)
    if cerr:
        return error(cerr)

    if kind == "body":
        return _split_body(design, comp, target, cutter, extend_tool)
    return _split_face(design, comp, faces, cutter, extend_tool)


TOOL_DESCRIPTION = (
    "Split a body into pieces, or its faces along a curve."
)

FULL_DESCRIPTION = TOOL_DESCRIPTION + "\n" + _outputs.produces_block(RETURNS)

split_tool = (
    Tool.create_simple(name="model_split", description=FULL_DESCRIPTION)
    .add_input_property(*_SPLIT.as_property())
    .add_input_property(_TARGET.name, _TARGET.schema())
    .add_input_property(_FACES.name, _FACES.schema())
    .add_input_property(_PLANE.name, _PLANE.schema())
    .add_input_property(_TOOLBODY.name, _TOOLBODY.schema())
    .add_input_property("extend_tool", {"type": "boolean"})
    .strict_schema()
)
split_item = Item.create_tool_item(tool=split_tool, write="write", handler=handler, run_on_main_thread=True,
                                   postconditions=[_assert.FeatureHealthy()],
                                   verification=Verification(
                                       kind="inline", rung="geometry",
                                       evidence_test="tests/unit/test_model_split.py::TestSplitFace"
                                       "::test_no_new_faces_is_error"))


def register_tool():
    register(split_item)
