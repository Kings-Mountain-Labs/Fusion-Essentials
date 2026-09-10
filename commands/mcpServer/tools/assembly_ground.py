# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Set an occurrence's isGroundToParent flag - the stateless parent lock. WRITES."""

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import error, ok, safe
from . import _common
from . import _inputs


def _occ_translation_mm(occ):
    """The occurrence's transform translation as [x,y,z] mm, or None."""
    t = safe(lambda: occ.transform.translation)
    if t is None:
        return None
    return [round((safe(lambda: t.x, 0.0) or 0.0) * 10.0, 3),
            round((safe(lambda: t.y, 0.0) or 0.0) * 10.0, 3),
            round((safe(lambda: t.z, 0.0) or 0.0) * 10.0, 3)]


# Only isGroundToParent is settable here. The other grounding flag, isGrounded (the UI Ground/Fix),
# is NOT exposed: the platform deprecates it and the parent lock is the supported way to fix a
# part. assembly_get's grounded_occurrences therefore reads EMPTY for an agent-grounded build.


def handler(occurrence: str = "", ground_to_parent=None) -> dict:
    """Set an occurrence's isGroundToParent flag (the stateless parent lock). true RE-LOCKS the
    part at its timeline-defined placement, DISCARDING any free move, captured or not; false frees
    it to move/joint. Both grounding flags are read back and reported. WRITES."""
    if ground_to_parent is None:
        return error("Specify 'ground_to_parent' (true/false). true locks the occurrence to its "
                     "timeline placement; false releases it.")
    design = _common.design()
    if not design:
        return error("No active design with components.")
    # The shared OccurrenceRef logic: an entityToken handle (the exact identity) or a
    # fullPathName/name, REFUSING an ambiguous one instead of grabbing the first instance.
    occ, occ_err = _inputs._resolve_occurrence(occurrence, occurrence)
    if not occ:
        return error(occ_err)
    pos_before = _occ_translation_mm(occ)
    try:
        occ.isGroundToParent = bool(ground_to_parent)
    except Exception as e:
        return error(f"Could not set isGroundToParent on '{safe(lambda: occ.name)}': {e}")
    now = _common.read_flag(lambda: occ.isGroundToParent)
    # An UNREADABLE flag is not a confirmation: treating it as one would let a swallowed write
    # through the mismatch gate below and publish a lock nobody read back.
    if now is None:
        return error(f"isGroundToParent cannot be read on '{safe(lambda: occ.name)}' after setting "
                     f"it to {bool(ground_to_parent)}, so the change is UNCONFIRMED. Re-read the "
                     "occurrence with assembly_get.")
    if bool(now) != bool(ground_to_parent):
        return error(f"Assignment was accepted but '{safe(lambda: occ.name)}' still reads "
                     f"isGroundToParent={bool(now)} - the flag did not take.")
    # isGrounded is read-only context here, and null rather than False when it cannot be read.
    out = {
        "occurrence": safe(lambda: occ.name),
        "isGroundToParent": bool(now),
        "isGrounded": _common.read_flag(lambda: occ.isGrounded),
        "note": "Parent lock set. isGroundToParent relocks to the TIMELINE placement and discards "
                "free moves. assembly_get's grounded_occurrences lists only the UI Ground/Fix flag "
                "(not settable here), so it stays empty for agent-grounded builds - read the "
                "per-occurrence ground_to_parent flag instead. To fix a part at a moved position, "
                "leave it FREE and assembly_move + assembly_capture_position.",
    }
    # The parent lock snaps the part back to its timeline placement, silently discarding free
    # moves; report that move with numbers.
    pos_after = _occ_translation_mm(occ)
    if (pos_before is not None and pos_after is not None
            and any(abs(a - b) > 0.01 for a, b in zip(pos_before, pos_after))):
        out["position_reset"] = {"from_mm": pos_before, "to_mm": pos_after}
        if ground_to_parent:
            out["position_warning"] = (
                f"Grounding SNAPPED '{safe(lambda: occ.name)}' back to its timeline placement "
                f"({pos_after} mm, was {pos_before} mm) - the prior free move is discarded. To "
                "keep a moved position, leave the part free and capture the position instead.")
        else:
            out["position_warning"] = (
                f"Freeing '{safe(lambda: occ.name)}' re-applied its captured/solved pose "
                f"({pos_after} mm, was {pos_before} mm at the parent lock).")
    return ok(out)


TOOL_DESCRIPTION = (
"Lock an occurrence to its parent (isGroundToParent): true re-locks it at its TIMELINE placement, "
"DISCARDING any free move; false frees it to move or joint."
)
tool = (
    Tool.create_simple(name="assembly_ground", description=TOOL_DESCRIPTION)
    .add_input_property("occurrence", {"type": "string", "description": "Occurrence name or full path."})
    .add_input_property("ground_to_parent", {"type": "boolean"})
    .strict_schema()
)
item = Item.create_tool_item(
    tool=tool, write="write", handler=handler, run_on_main_thread=True,
    verification=Verification(
        kind="inline", rung="value",
        evidence_test="tests/unit/test_assembly_ground.py::TestGround::test_stuck_flag_bites"))


def register_tool():
    register(item)
