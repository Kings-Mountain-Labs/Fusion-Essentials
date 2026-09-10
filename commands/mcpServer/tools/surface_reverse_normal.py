# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""MCP building block that FLIPS the normals of OPEN surface bodies - for a stitch/thicken/offset that
solidified toward the wrong side. WRITES. ReverseNormalFeatures.add(surfaces: ObjectCollection)
reverses ALL faces of each input surface body (it takes bodies, not individual faces). The flip is
read back via BRepFace.isParamReversed.
"""

import adsk.core
import adsk.fusion

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import error, ok, safe, target_component
from . import _common
from . import _inputs
from . import _outputs

app = adsk.core.Application.get()

# What this tool RETURNS: the feature name + whether the isParamReversed read-back actually CONFIRMS
# every face flipped - a reversed normal looks identical in most reads, so the flag is the only truth.
RETURNS = [
    _outputs.ReturnsName("feature", of="feature"),
    _outputs.ReturnsValue("reversed_confirmed",
                          "whether the isParamReversed read-back confirms every face flipped"),
]

_BODIES = _inputs.SurfaceBodyRefList("bodies", required=True)


def _count_reversed(bodies):
    """(total_faces, reversed_faces) across an iterable of BRepBody. reversed = faces whose
    isParamReversed is True - the one field a normal flip toggles, so summing it before and after the
    feature gives a read-back the operation can be checked against (survives face reordering)."""
    total = 0
    reversed_ct = 0
    for b in bodies:
        faces = safe(lambda b=b: b.faces)
        total += int(safe(lambda: faces.count, 0) or 0) if faces else 0
        for f in _common.iter_collection(faces):
            if bool(safe(lambda f=f: f.isParamReversed)):
                reversed_ct += 1
    return total, reversed_ct


def reverse_normal_handler(bodies=None) -> dict:
    """Reverse the normals of one or more open surface bodies - flip which side is 'out'."""
    design = _common.design()
    if not design:
        return error("No active design. Create or open a document first (see doc_new).")
    comp = target_component(design)

    surf_bodies, berr = _BODIES.resolve(bodies) # validates each isSolid == false before mutating
    if berr:
        return error(berr)
    if not surf_bodies:
        return error("'bodies' resolved to no surface bodies. Pass open surface body handles/names.")

    # BEFORE: record the isParamReversed state so the flip can be measured against it afterwards.
    before_total, before_reversed = _count_reversed(surf_bodies)
    input_names = [safe(lambda b=b: b.name) for b in surf_bodies]

    coll = adsk.core.ObjectCollection.create()
    for b in surf_bodies:
        coll.add(b)

    try:
        feature = comp.features.reverseNormalFeatures.add(coll)
    except Exception as e:
        return error(f"Reverse normal failed: {e}. (Pass OPEN surface bodies - a solid has no free "
                     "normal to flip.)")
    if not feature:
        return error(_common.no_feature_error(design, "Reverse normal"))

    # AFTER: read the flip back off the feature's OWN result bodies (fresh references; the input
    # references can go invalid once the parametric feature rebuilds the bodies).
    result_bodies = _common.result_bodies(feature)
    after_total, after_reversed = _count_reversed(result_bodies) if result_bodies else (0, 0)
    faces_consumed = int(safe(lambda: feature.faces.count, 0) or 0)

    # A flip toggles isParamReversed on EVERY face, so the reversed-count must become
    # (total - before_reversed) with the face count unchanged. Confirm that exact relationship; report
    # the raw counts either way so the caller sees what was actually observed, not a claim.
    expected_after = before_total - before_reversed
    confirmed = bool(before_total > 0 and after_total == before_total
                     and after_reversed == expected_after)

    payload = {
        "reversed": True,
        "feature": safe(lambda: feature.name),
        "bodies": input_names,
        "body_count": len(surf_bodies),
        "faces_total": before_total,
        "faces_consumed": faces_consumed,
        "reversed_before": before_reversed,
        "reversed_after": after_reversed,
        "reversed_confirmed": confirmed,
    }
    if confirmed:
        payload["note"] = ("Normals flipped - isParamReversed toggled on all %d face(s), read back off "
                           "the feature." % before_total)
    else:
        payload["note"] = (
            "Reverse Normal feature created and consumed %d face(s), but the isParamReversed read-back "
            "did NOT confirm a full flip (before_reversed=%d, after_reversed=%d of %d faces). Verify "
            "with view_screenshot." % (faces_consumed, before_reversed, after_reversed, before_total))
    return ok(payload)


_DESC = (
"Reverse the normals of open surface bodies; ALL faces of each are flipped.\n"
+ _outputs.produces_block(RETURNS)
)

surface_reverse_normal_tool = (
    Tool.create_simple(name="surface_reverse_normal", description=_DESC)
    .add_input_property("bodies", _BODIES.schema())
    .add_required_input("bodies")
    .strict_schema()
)
surface_reverse_normal_item = Item.create_tool_item(
    tool=surface_reverse_normal_tool, write="write", handler=reverse_normal_handler,
    run_on_main_thread=True,
    verification=Verification(
        kind="effect",
        rung="geometry",
        evidence_test="tests/unit/test_surface_reverse_normal.py"
                      "::test_noop_reported_honestly_not_confirmed"))


def register_tool():
    register(surface_reverse_normal_item)
