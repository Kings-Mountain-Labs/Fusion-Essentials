# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""MCP building block that DELETES faces from a body, with an optional HEAL. WRITES. Two API paths:
DeleteFaceFeatures.add(faces) heals/fills the opening (a solid stays solid, and it FAILS if the body
cannot be healed); SurfaceDeleteFaceFeatures.add(faces) just removes the faces (a solid becomes a
surface). Both take a single BRepFace or an ObjectCollection. The body's face-count delta is read back
so a delete that consumes the whole body is reported, never a false success.
"""

import adsk.core
import adsk.fusion

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import error, ok, safe, target_component
from . import _common
from . import _geom
from . import _inputs
from . import _outputs

app = adsk.core.Application.get()

# Both are read off the created feature's result bodies, so both are omitted on the direct-mode
# path where no feature object comes back.
RETURNS = [
    _outputs.ReturnsName("feature", of="feature", absent_when="no_timeline_feature"),
    _outputs.ReturnsValue("bodies_consumed", "count of input bodies fully removed by the delete",
                          absent_when="no_timeline_feature"),
]

_FACES = _inputs.GeometryHandleList("faces", require="face", required=True)


def delete_face_handler(faces=None, heal=False) -> dict:
    """Delete faces from their bodies - optionally healing the opening (heal=True) or leaving it open."""
    design = _common.design()
    if not design:
        return error("No active design. Create or open a document first (see doc_new).")
    comp = target_component(design)

    face_ents, ferr = _FACES.resolve(faces)
    if ferr:
        return error(ferr)
    if not face_ents:
        return error("'faces' resolved to no faces. Pass find_geometry face handles.")

    # The names are captured BEFORE because a delete can consume the body outright; the face count
    # is the feature-independent signal the direct-mode path is graded on.
    bodies = _geom.owning_bodies(face_ents)
    body_names = [safe(lambda b=b: b.name) for b in bodies]
    counts_before = _geom.face_counts(bodies)
    faces_before_total = sum(c for c in counts_before.values() if isinstance(c, int))

    coll = adsk.core.ObjectCollection.create()
    for f in face_ents:
        coll.add(f)

    features = comp.features.deleteFaceFeatures if heal else comp.features.surfaceDeleteFaceFeatures
    try:
        feature = features.add(coll)
    except Exception as e:
        if heal:
            return error(f"Delete-face (heal) failed: {e}. The opening could not be healed - retry "
                         "with heal=false to just remove the faces (a solid then becomes a surface).")
        return error(f"Delete-face failed: {e}.")
    # deleteFaceFeatures.add returns None in a DIRECT design while the delete LANDS, so direct mode
    # falls through to the face-count delta below, which needs no feature object. In parametric a
    # None feature stays an error.
    direct_no_feature = _common.direct_feature_absence(design, feature)
    if not feature and not direct_no_feature:
        if heal:
            return error(_common.no_feature_error(
                design, "Delete-face (heal)",
                "The body could not be healed - retry with heal=false to remove the faces "
                "without healing."))
        return error(_common.no_feature_error(design, "Delete-face"))

    payload = {
        "deleted": True,
        "heal": bool(heal),
        "faces_requested": len(face_ents),
        "input_bodies": body_names,
        "faces_before": faces_before_total,
    }

    if direct_no_feature:
        # No feature, so no result-body list and no bodies_consumed - both are read off the feature.
        # The face-count delta on the INPUT bodies is the only evidence there is, so an unreadable
        # count means UNVERIFIED here rather than success.
        delta, readable = _geom.face_count_delta(bodies, counts_before)
        if not readable:
            return error("Delete-face ran in a DIRECT design, which returns no feature object, and "
                         "no input body's face count could be read back - so whether the faces were "
                         "deleted is UNVERIFIED. The body may also have been fully consumed (every "
                         "face gone removes it); design_get(include=['tree']) shows whether it is "
                         "still there.")
        if delta == 0:
            return error("Delete-face reported no error but no input body's face count changed - "
                         "nothing was deleted. "
                         + _common.failed_effect_remedy(design, feature))
        payload["no_timeline_feature"] = True
        payload["faces_after"] = faces_before_total + delta
        payload["faces_delta"] = delta
        # Built from the MEASURED delta, never the request: on a heal the two need not agree, since
        # healing can re-merge neighbours. `heal` is an input flag, so the note says REQUESTED.
        healed = " (heal requested)" if heal else ""
        if delta > 0:
            headline = "The edit landed%s; body face count %d -> %d (%d face(s) requested)." % (
                healed, faces_before_total, faces_before_total + delta, len(face_ents))
        else:
            headline = "The delete landed%s; body face count %d -> %d (%d face(s) requested)." % (
                healed, faces_before_total, faces_before_total + delta, len(face_ents))
        payload["note"] = (
            "%s %s Neither the result-body list nor 'bodies_consumed' is available without a feature "
            "object - check the bodies with design_get(include=['tree'])."
            % (headline, _common.DIRECT_FEATURE_NOTE))
        if delta > 0:
            # A delete that RAISES the face count is neither passed off as normal nor refused: the
            # count moved, so the edit landed, and the surprise is handed to the caller.
            payload["warning"] = (
                "The face count ROSE by %d - unexpected for a delete, which normally lowers it. The "
                "edit did land (the count moved), but the requested face(s) may not be what was "
                "removed: inspect the body with design_get(include=['tree']) / find_geometry before "
                "building on it." % delta)
            payload["note"] += " " + payload["warning"]
        return ok(payload)

    # AFTER: read result bodies off the feature. Fewer result bodies than input bodies means a
    # delete consumed a whole body (every face gone) - a warning below, never fake success.
    result = [{
        "name": safe(lambda b=b: b.name),
        # counted, never safe(..., 0): a faces collection that will not enumerate is not zero faces.
        "faces": _common.counted(lambda b=b: b.faces.count),
        # read_flag: informational here, so a flag that will not read publishes null.
        "is_solid": _common.read_flag(lambda b=b: b.isSolid),
    } for b in _common.result_bodies(feature)]

    bodies_before = len(bodies)
    bodies_after = len(result)
    bodies_consumed = max(0, bodies_before - bodies_after)
    unreadable_after = [r["name"] for r in result if r["faces"] is None]
    faces_after_total = None if unreadable_after else sum(r["faces"] for r in result)

    payload["feature"] = safe(lambda: feature.name)
    payload["result_bodies"] = result
    payload["faces_after"] = faces_after_total
    payload["bodies_consumed"] = bodies_consumed
    if bodies_consumed > 0 or (bodies_before > 0 and bodies_after == 0):
        payload["warning"] = ("%d input body(ies) were fully consumed by the delete - no result body "
                              "remains. Deleting every face of a body removes the body." % bodies_consumed)
        payload["note"] = payload["warning"]
        return ok(payload)

    # A result body that will not report its faces leaves the delta unmeasurable, so the verdict is
    # refused rather than rendered on a coerced zero.
    if faces_after_total is None:
        return error("Delete-face reported no error, but the face count of %d result body(ies) (%s) "
                     "would not read back, so whether any face was deleted is UNVERIFIED - a count "
                     "that will not read is not a count of zero. Check the bodies with "
                     "design_get(include=['tree']) / find_geometry before building on them."
                     % (len(unreadable_after),
                        ", ".join(n or "unnamed" for n in unreadable_after)))

    # A face count that did not move means nothing was deleted, whatever the feature object says.
    # Skipped when no input body's count could be read at all, where the delta is evidence of none.
    delta = faces_after_total - faces_before_total
    if faces_before_total and delta == 0:
        return error("Delete-face reported no error but no input body's face count changed "
                     "(%d -> %d) - nothing was deleted. "
                     % (faces_before_total, faces_after_total)
                     + _common.failed_effect_remedy(design, feature))
    payload["faces_delta"] = delta
    # Built from the MEASURED delta, never the request: on a heal the two need not agree, since
    # healing can re-merge neighbours. `heal` is an input flag, so the note says REQUESTED.
    healed = " (heal requested)" if heal else ""
    verb = "The delete landed" if delta < 0 else "The edit landed"
    payload["note"] = ("%s%s; body face count %d -> %d (%d face(s) requested)."
                       % (verb, healed, faces_before_total, faces_after_total, len(face_ents)))
    if delta > 0:
        payload["warning"] = (
            "The face count ROSE by %d - unexpected for a delete, which normally lowers it. The "
            "edit did land (the count moved), but the requested face(s) may not be what was "
            "removed: inspect the body with design_get(include=['tree']) / find_geometry before "
            "building on it." % delta)
        payload["note"] += " " + payload["warning"]
    return ok(payload)


_DESC = (
"Delete faces from their bodies; 'heal'=false leaves the opening, turning a solid into a surface.\n"
+ _outputs.produces_block(RETURNS)
)

surface_delete_face_tool = (
    Tool.create_simple(name="surface_delete_face", description=_DESC)
    .add_input_property("faces", _FACES.schema())
    .add_input_property("heal", {"type": "boolean"})
    .add_required_input("faces")
    .strict_schema()
)
surface_delete_face_item = Item.create_tool_item(
    tool=surface_delete_face_tool, write="write", handler=delete_face_handler,
    run_on_main_thread=True,
    verification=Verification(
        kind="inline",
        rung="geometry",
        evidence_test="tests/unit/test_surface_delete_face.py::TestParametricFaceCountGate"
                      "::test_unchanged_face_count_is_an_error"))


def register_tool():
    register(surface_delete_face_item)
