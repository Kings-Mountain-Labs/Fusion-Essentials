# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""MCP building block that UNTRIMS surface faces - restores a trimmed face to its underlying (natural)
extent, or removes an internal hole loop. WRITES. UntrimFeatures.createInputFromFaces(faces,
untrimLoopType, extensionDistance) -> UntrimFeatureInput; .add(input) -> UntrimFeature. Only loops with
no connected face (whose edges are owned by a single face) can be removed, and only on OPEN surface
bodies (isSolid==false).
"""

import adsk.core
import adsk.fusion

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import error, ok, safe, scale, target_component
from . import _common
from . import _inputs
from . import _outputs

app = adsk.core.Application.get()

# What this tool RETURNS: the feature name + the total untrimmed-face area, the read-back that proves
# the extent actually grew (an untrim that changed nothing leaves area unchanged).
RETURNS = [
    _outputs.ReturnsName("feature", of="feature"),
    _outputs.ReturnsValue("area_after", "total area (cm^2) of the untrimmed faces"),
]

# loop_type -> adsk.fusion.UntrimLoopTypes attribute. Manual/loops selection is not offered here (the
# face-driven path covers restore-a-trimmed-face and remove-a-hole, the two common repairs).
_LOOP_TYPES = {
    "all": "AllLoopsUntrimType",
    "external": "ExternalLoopsUntrimType",
    "internal": "InternalLoopsUntrimType",
}

_FACES = _inputs.GeometryHandleList("faces", require="face", required=True)


def _sum_area(faces):
    """Total area (cm^2) over an iterable of BRepFace - BRepFace.area is the read-back an untrim grows."""
    return round(sum(float(safe(lambda f=f: f.area, 0.0) or 0.0) for f in faces), 6)


def untrim_handler(faces=None, loop_type="all", extension=None, units="mm") -> dict:
    """Untrim surface faces - remove a boundary loop so the face returns to its natural extent."""
    lt_key = (loop_type or "all").strip().lower()
    if lt_key not in _LOOP_TYPES:
        return error(f"Unknown loop_type '{loop_type}'. Use: all, external, internal.")
    k = scale(units)
    if k is None:
        return error(f"Unknown units '{units}'. Use mm, cm, or in.")

    design = _common.design()
    if not design:
        return error("No active design. Create or open a document first (see doc_new).")
    comp = target_component(design)

    face_ents, ferr = _FACES.resolve(faces)
    if ferr:
        return error(ferr)
    if not face_ents:
        return error("'faces' resolved to no faces. Pass find_geometry face handles.")

    # Guard: untrim only acts on faces of OPEN surface bodies. Name the offending face if any is a solid
    # face, rather than letting the API raise an opaque error mid-mutation.
    for i, f in enumerate(face_ents):
        body = safe(lambda f=f: f.body)
        if body is not None and bool(safe(lambda: body.isSolid)):
            return error(f"'faces'[{i}] belongs to a SOLID body - untrim only restores faces on OPEN "
                         "surface bodies. Unstitch or delete-face the solid first.")

    area_before = _sum_area(face_ents)

    ext_cm = 0.0
    if extension is not None:
        try:
            ev = float(extension)
        except (TypeError, ValueError):
            return error("'extension' must be a number.")
        if ev < 0:
            return error("'extension' must be positive (0 = untrim to the natural boundary, no extension).")
        ext_cm = ev * k

    loop_type_enum = getattr(adsk.fusion.UntrimLoopTypes, _LOOP_TYPES[lt_key])
    ext_val = adsk.core.ValueInput.createByReal(ext_cm)
    try:
        untrim_input = comp.features.untrimFeatures.createInputFromFaces(face_ents, loop_type_enum, ext_val)
        if not untrim_input:
            return error("Untrim could not build an input from those faces - a selected loop may have "
                         "a connected face (only single-face loops can be untrimmed).")
        feature = comp.features.untrimFeatures.add(untrim_input)
    except Exception as e:
        return error(f"Untrim failed: {e}. (Only loops with no connected face, on an OPEN surface, "
                     "can be untrimmed.)")
    if not feature:
        return error(_common.no_feature_error(design, "Untrim",
                                              "The selected loops could not be removed."))

    # Read the RESULT faces back. The untrimmed face is a NEW, larger face, so total created-face area
    # vs the input area is the honest signal the extent actually grew (removing an internal hole fills
    # it; removing an external loop extends to the natural boundary - both increase area).
    created = list(_common.iter_collection(safe(lambda: feature.faces)))
    area_after = _sum_area(created)

    result_bodies = [safe(lambda b=b: b.name) for b in _common.result_bodies(feature)]

    grew = bool(created and area_after > area_before + 1e-9)
    payload = {
        "untrimmed": True,
        "feature": safe(lambda: feature.name),
        "loop_type": lt_key,
        "faces_in": len(face_ents),
        "faces_created": len(created),
        "area_before": area_before,
        "area_after": area_after,
        "extent_grew": grew,
        "extension": round(ext_cm / k, 6) if ext_cm else 0.0,
        "units": units,
        "result_bodies": result_bodies,
    }
    if grew:
        payload["note"] = ("Faces untrimmed - extent restored (area %.6f -> %.6f cm^2)."
                           % (area_before, area_after))
    else:
        payload["note"] = (
            "Untrim feature created, but the untrimmed area did not exceed the original "
            "(area_before=%.6f, area_after=%.6f cm^2). The loop may already be at the natural boundary, "
            "or the created faces could not be read back - verify with view_screenshot."
            % (area_before, area_after))
    return ok(payload)


_DESC = (
"Restore trimmed faces to their natural extent, or remove an internal hole loop.\n"
+ _outputs.produces_block(RETURNS)
)

surface_untrim_tool = (
    Tool.create_simple(name="surface_untrim", description=_DESC)
    .add_input_property("faces", _FACES.schema())
    .add_input_property(*_inputs.Choice("loop_type", ["all", "external", "internal"], default="all",
        description="Which boundary loops to remove.").as_property())
    .add_input_property("extension", {"type": "number",
        "description": "In 'units'; past the natural boundary."})
    .add_input_property(*_inputs.UNITS.as_property())
    .add_required_input("faces")
    .strict_schema()
)
surface_untrim_item = Item.create_tool_item(
    tool=surface_untrim_tool, write="write", handler=untrim_handler, run_on_main_thread=True,
    verification=Verification(
        kind="effect",
        rung="geometry",
        evidence_test="tests/unit/test_surface_untrim.py::test_no_growth_reported_honestly"))


def register_tool():
    register(surface_untrim_item)
