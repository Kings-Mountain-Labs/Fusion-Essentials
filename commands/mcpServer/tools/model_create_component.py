# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""MCP building block: create a new empty component occurrence in the active design.

Prerequisite for building an assembly: the modelling tools build into the active component, so
create one component per part, activate it, then model into it (sketch_create / extrude). WRITES.
"""

import adsk.core
import adsk.fusion

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import error, ok, safe, scale
from . import _common
from . import _inputs

app = adsk.core.Application.get()

# parent: nest the new component INSIDE an existing occurrence's component (occurrences.addNewComponent
# on the PARENT component - verified live). Omitted = the root component (the back-compat default).
_PARENT = _inputs.OccurrenceRef("parent", required=False)


def _ensure_multi_component_intent(design):
    """Promote a PART-intent design to HYBRID so it accepts components; a note, or None."""
    T = adsk.fusion.DesignIntentTypes
    intent = safe(lambda: design.designIntent)
    if intent != T.PartDesignIntentType:
        return None      # ASSEMBLY / HYBRID already accept components; leave the intent as set
    try:
        # HYBRID, not ASSEMBLY: it allows several internal components AND keeps modeling enabled.
        design.designIntent = T.HybridDesignIntentType
    except Exception:
        return None      # let addNewComponent raise its own error below
    if safe(lambda: design.designIntent) == T.HybridDesignIntentType:
        return ("design intent was PART (one-component-only); promoted to HYBRID so multiple "
                "components are allowed while modeling stays enabled.")
    return None


def handler(name: str = "", x: float = 0.0, y: float = 0.0, z: float = 0.0,
            units: str = "mm", activate: bool = False,
            rotate_deg: float = 0.0, rotate_axis: str = "z", parent: str = "") -> dict:
    """See TOOL_DESCRIPTION."""
    k = scale(units)
    if k is None:
        return error(f"Unknown units '{units}'. Use mm, cm, or in.")
    design = _common.design()
    if not design:
        return error("No active design. Create or open a document first (see doc_new).")

    # parent: resolve the occurrence to nest INSIDE (its component receives the new child). Omitted =
    # root. OccurrenceRef refuses an ambiguous name instead of grabbing the wrong instance.
    parent_occ = None
    if (parent or "").strip():
        parent_occ, perr = _PARENT.resolve(parent)
        if perr:
            return error(perr)

    import math
    matrix = adsk.core.Matrix3D.create()
    if rotate_deg:
        axis_vec = _inputs._AXIS_VECS.get((rotate_axis or "z").strip().lower())
        if not axis_vec:
            return error(f"Unknown rotate_axis '{rotate_axis}'. Use x, y, or z.")
        # Rotate about the world origin; the translation assigned below places the component.
        matrix.setToRotation(math.radians(float(rotate_deg)),
                             adsk.core.Vector3D.create(*axis_vec), adsk.core.Point3D.create(0, 0, 0))
    if x or y or z:
        matrix.translation = adsk.core.Vector3D.create(float(x) * k, float(y) * k, float(z) * k)

    # A fresh (Part-intent) design refuses addNewComponent - promote it to Hybrid first so the build
    # can hold multiple components. Report the promotion if it happened.
    intent_note = _ensure_multi_component_intent(design)

    # ROOT (default) or NESTED: addNewComponent on the parent occurrence's COMPONENT nests the new
    # component inside it (verified live: the child then reads a nested fullPathName). Omitted = root.
    host_occurrences = (safe(lambda: parent_occ.component.occurrences) if parent_occ is not None
                        else safe(lambda: design.rootComponent.occurrences))
    if host_occurrences is None:
        return error("Could not access the target occurrences collection to create the component.")
    try:
        occ = host_occurrences.addNewComponent(matrix)
    except Exception as e:
        return error(f"Could not create component: {e}")
    if not occ:
        return error("Component creation returned nothing.")

    _final_name, name_warning = _common.apply_rename(occ.component, name)

    # A component created via a sub-component's occurrences is NATIVE to it, so its own fullPathName
    # shows only the child; the proxy into the chosen parent's context carries the nested path.
    # Fusion joins fullPathName segments with '+', which is the fallback's shape.
    read_occ = occ
    if parent_occ is not None:
        proxy = safe(lambda: occ.createForAssemblyContext(parent_occ))
        if proxy is not None:
            read_occ = proxy
    full_path = safe(lambda: read_occ.fullPathName)
    if parent_occ is not None and read_occ is occ:
        pp, cn = safe(lambda: parent_occ.fullPathName), safe(lambda: occ.name)
        if pp and cn:
            full_path = f"{pp}+{cn}"

    activated = False
    if activate:
        activated = bool(safe(lambda: read_occ.activate(), False))

    # The FIRST component created in an empty design comes back with isGroundToParent True and the
    # next one False; it decides which member a joint drive displaces, so it is disclosed at create
    # time. read_flag keeps an unreadable flag None instead of coercing it to False.
    ground_to_parent = _common.read_flag(lambda: occ.isGroundToParent)

    out = {
        "created": True,
        "occurrence": safe(lambda: occ.name),
        "component": safe(lambda: occ.component.name),
        "full_path": full_path,               # nested path shows the parent when 'parent' was given
        "nested_in": safe(lambda: parent_occ.fullPathName) if parent_occ is not None else None,
        "position": {"x": x, "y": y, "z": z} if (x or y or z) else "origin",
        "rotate_deg": float(rotate_deg or 0.0),
        "rotate_axis": (rotate_axis or "z").lower() if rotate_deg else None,
        "units": units,
        "activated": activated,
        "ground_to_parent": ground_to_parent,
        "note": ("Empty component created" + (f" nested inside '{safe(lambda: parent_occ.fullPathName)}'"
                 if parent_occ is not None else " at root")
                 + ". Activate it (or it is active) then model into it with sketch_create / extrude; "
                 "ground / joint it as an assembly part."),
    }
    if ground_to_parent:
        out["note"] += (" ground_to_parent reads TRUE on this new occurrence - it is locked to its "
                        "parent (the FIRST component of an empty design lands locked; the next one "
                        "does not), so a joint drive displaces the OTHER member instead of this "
                        "one. assembly_ground(ground_to_parent=false) releases it.")
    if name_warning:
        out["name_warning"] = name_warning
    if intent_note:
        out["design_intent_promoted"] = intent_note
    return ok(out)


TOOL_DESCRIPTION = (
"Create a new EMPTY component occurrence, at root unless 'parent' nests it. Its placement composes "
"with the component-local coordinates you then sketch in."
)

tool = (
    Tool.create_simple(name="model_create_component", description=TOOL_DESCRIPTION)
    .add_input_property("name", {"type": "string"})
    .add_input_property("x", {"type": "number", "description": "Placement X, in 'units'."})
    .add_input_property("y", {"type": "number"})
    .add_input_property("z", {"type": "number"})
    .add_input_property(*_inputs.UNITS.as_property())
    .add_input_property("activate", {"type": "boolean",
            "description": "Make it the active edit target."})
    .add_input_property("rotate_deg", {"type": "number",
            "description": "Orientation about 'rotate_axis'."})
    .add_input_property(*_inputs.frame_axis("rotate_axis", default="z").as_property())
    .add_input_property(*_PARENT.as_property())
    .strict_schema()
)
item = Item.create_tool_item(
    tool=tool, write="write", handler=handler, run_on_main_thread=True,
    verification=Verification(
        kind="effect",
        rung="value",
        evidence_test="tests/unit/test_model_create_component.py::TestCreateComponent"
                      "::test_rejected_rename_surfaces_warning_not_false_success"))


def register_tool():
    register(item)
