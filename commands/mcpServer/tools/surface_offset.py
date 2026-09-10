# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""MCP building block: offset faces by a distance into ANOTHER surface (positive = along the face
normal). WRITES; isSolid is read off the bodies owning the CREATED faces, never off feature.bodies.
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
from ._surface_common import _any_solid, _created_bodies

app = adsk.core.Application.get()

_OFFSET_OPS = ("new", "new_body", "new_component")

_OFFSET_FACES = _inputs.GeometryHandleList("faces", require="face", required=True)


def handler(faces=None, distance: float = 0.0, units: str = "mm",
            chaining: bool = False, operation: str = "new") -> dict:
    """Offset faces by a distance into ANOTHER surface (positive = along the face normal)."""
    k = scale(units)
    if k is None:
        return error(f"Unknown units '{units}'. Use mm, cm, or in.")
    # distance=0 is LEGAL: it copies the face as a coincident surface (measured live - the feature
    # lands and space_measure reads distance 0), the standard machining-prep copy-face idiom. The
    # zero refusals on extrude/extend/thicken stay: zero there genuinely produces nothing.
    op_key = (operation or "new").strip().lower()
    if op_key not in _OFFSET_OPS:
        return error(f"Unknown operation '{operation}'. Offset supports: new, new_component.")

    design = _common.design()
    if not design:
        return error("No active design. Create or open a document first (see doc_new).")
    comp = target_component(design)

    face_ents, ferr = _OFFSET_FACES.resolve(faces)
    if ferr:
        return error(ferr)
    coll = adsk.core.ObjectCollection.create()
    for f in face_ents:
        coll.add(f)

    dist_val = adsk.core.ValueInput.createByReal(float(distance) * k)
    op = getattr(adsk.fusion.FeatureOperations, _common.OPERATIONS[op_key])
    try:
        off_input = comp.features.offsetFeatures.createInput(coll, dist_val, op, bool(chaining))
        feature = comp.features.offsetFeatures.add(off_input)
    except Exception as e:
        return error(f"Offset failed: {e}.")
    if not feature:
        return error(_common.no_feature_error(design, "Offset"))

    # Read the CREATED surface back - feature.bodies also lists the pre-existing source solid, which
    # made is_solid report true for a genuine open surface (live-verified).
    created, faces_offset, readable = _created_bodies(feature)
    if readable and faces_offset == 0:
        return error("Offset reported success but created no faces - nothing was offset. The feature "
                     "remains in the timeline; remove it with design_delete_feature.")
    requested = len(face_ents)
    unverified = []
    if readable:
        names = [safe(lambda b=b: b.name) for b in created]
        any_solid = _any_solid(created)
        if any_solid is None:
            unverified.append("is_solid")
    else:
        # feature.faces could not be read at all, so NOTHING about the created surface was checked:
        # a 0 face count, an empty body list and is_solid=false would each be a fabricated read of
        # the very thing that would not read. Publish null and name what went unverified.
        names, any_solid, faces_offset = None, None, None
        unverified = ["faces_offset", "result_bodies", "is_solid"]
    note = "Faces offset into a new surface."
    if distance == 0:
        note = ("Faces copied as a COINCIDENT surface (distance=0) - the zero-offset "
                "copy-face idiom.")
    if any_solid is False:
        note += " The created body reads back isSolid=false (a surface)."
    elif any_solid is True:
        note += (" The created body reads back isSolid=true - offsetting a face of a SOLID yields a "
                 "solid, not a surface.")
    if faces_offset is not None and faces_offset > requested:
        note += (f" chaining=true EXPANDED the selection: {requested} face(s) requested, "
                 f"{faces_offset} tangent-connected face(s) offset. Pass chaining=false to offset "
                 "only the picked faces.")
    payload = {
        "offset": True,
        "feature": safe(lambda: feature.name),
        "operation": op_key,
        "result_bodies": names,
        "is_solid": any_solid,       # read off the CREATED surface bodies only
        "faces_requested": requested,
        "faces_offset": faces_offset,
        "distance": round(float(distance), 6),
        "units": units,
        "note": note,
    }
    if unverified:
        payload["unverified"] = unverified
        payload["note"] += " Not read back off the feature: " + ", ".join(unverified) + "."
    return ok(payload)


TOOL_DESCRIPTION = (
"Offset faces into another surface body."
)
tool = (
    Tool.create_simple(name="surface_offset", description=TOOL_DESCRIPTION)
    .add_input_property("faces", _OFFSET_FACES.schema())
    .add_input_property("distance", {"type": "number", "description": "In 'units'; positive = along the face normal, 0 = a coincident copy."})
    .add_input_property(*_inputs.UNITS.as_property())
    .add_input_property("chaining", {"type": "boolean", "description": "Expand across tangent-connected faces."})
    .add_input_property(*_inputs.boolean_op(options=("new", "new_component"), default="new").as_property())
    .add_required_input("faces")
    .add_required_input("distance")
    .strict_schema()
)
item = Item.create_tool_item(tool=tool, write="write", handler=handler,
                             run_on_main_thread=True,
                             postconditions=[_assert.FeatureHealthy()],
                             verification=Verification(
                                 kind="inline", rung="geometry",
                                 evidence_test="tests/unit/test_surface_offset.py"
                                               "::TestOffsetThickenKind"
                                               "::test_offset_that_creates_no_faces_bites"))


def register_tool():
    register(item)
