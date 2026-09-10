# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Capture / revert / delete / report the assembly's flexible POSITION in the timeline. WRITES."""

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import error, ok, safe
from . import _common
from . import _inputs
from . import _joints
from ._assembly_common import _constraint_moves, _constraint_positions

_CAPTURE_ACTIONS = ("capture", "revert", "status", "delete", "discard_pending")
_CAPTURE_ACTION = _inputs.Choice(
    "action", options=list(_CAPTURE_ACTIONS), default="status",
    description="revert drops the latest captured marker; discard_pending drops an uncaptured "
                "move.")


def _capture_markers(snaps):
    """[{name, timeline_index}] for every captured position; timeline_index is guarded because the
    property can raise on a stale reference."""
    return [{"name": safe(lambda s=s: s.name),
             "timeline_index": safe(lambda s=s: s.timelineObject.index)}
            for s in _common.iter_collection(snaps)]


def _find_captured(snaps, want):
    """Every captured marker whose name matches 'want' case-insensitively (exact, not substring)."""
    hits = []
    for s in _common.iter_collection(snaps):
        nm = safe(lambda s=s: s.name)
        if nm and nm.lower() == want.lower():
            hits.append((s, nm))
    return hits


def _positionable_occurrences(design):
    """{fullPathName: occurrence} for every occurrence in the design, falling back to .name only
    when the path will not read. The same wrappers are held across a mutation and re-read after
    it, so a before/after pair describes one occurrence and not two lookups."""
    out = {}
    for occ in _common.all_occurrences(design):
        label = safe(lambda occ=occ: occ.fullPathName) or safe(lambda occ=occ: occ.name)
        if label:
            out[label] = occ
    return out


def handler(action: str = "status", marker: str = "") -> dict:
    """See TOOL_DESCRIPTION."""
    act, aerr = _CAPTURE_ACTION.resolve(action)
    if aerr:
        return error(aerr)
    design = _common.design()
    if not design:
        return error("No active design.")
    snaps = safe(lambda: design.snapshots)
    if snaps is None:
        return error("This design does not expose snapshots (capture position).")

    # The shared flag read every joint CREATE gates on. TRI-state: None means the flag could not be
    # read and is published as-is; only a real True satisfies the preconditions below.
    pending_flag = _joints.pending_position(design)
    pending = pending_flag is True
    count = safe(lambda: snaps.count, 0)

    if act == "status":
        note = ("has_pending = a moved-but-uncaptured position exists (a joint_drive pose sets it "
                "the same way a free move does; a design_add_instance placement and an "
                "assembly_constrain relationship do NOT). Use capture to record it into the "
                "timeline, revert to drop the latest capture, or delete a specific marker by name.")
        if pending_flag is None:
            note = ("has_pending is null - the pending-position flag could not be read, so whether "
                    "a moved-but-uncaptured position exists is UNKNOWN here (it is not a 'no'). "
                    "The captured markers below were still read. " + note)
        return ok({"has_pending": pending_flag, "snapshot_count": count,
        "markers": _capture_markers(snaps), "note": note})

    if act == "capture":
        # Live-verified: with no pending position change snapshots.add() RAISES
        # "3 : Has no pending snapshot" - the pending flag is the precondition, not a hint.
        if not pending:
            return error("Nothing to capture - there is no pending position change. Move a jointed "
    "component first (its pose is transient until captured).")
        # The pose the capture is supposed to RECORD, sampled before the add: snapshots.add() can
        # answer with a snapshot object and an advanced count while every moved part snaps back, so
        # neither is evidence about the POSITION - only the parts' own transforms are.
        targets = _positionable_occurrences(design)
        before_pos = _constraint_positions(targets)
        try:
            snap = snaps.add()
        except Exception as e:
            return error(f"Capture failed: {e}")
        if not snap:
            return error("snapshots.add() returned nothing - the position was not captured.")
        # The count the collection reports, never the arithmetic the add expected: an unreadable
        # re-read publishes null, so no caller reads a computed number back as a measurement.
        count_after = _common.counted(lambda: snaps.count)
        if count_after is not None and count_after <= count:
            return error(f"Capture reported success but the snapshot count did not advance "
                         f"({count} before, {count_after} after) - the position was not captured.")
        # A capture must record the pending pose, not undo it. Any part that MOVED across the add
        # moved back off the pose being captured, so the marker records a position nobody asked for.
        snap_name = safe(lambda: snap.name)
        moved, measured = _constraint_moves(before_pos, targets)
        if moved:
            shown = "; ".join(f"{m['occurrence']} by {m['distance_mm']} mm" for m in moved[:6])
            more = f" (+{len(moved) - 6} more)" if len(moved) > 6 else ""
            return error(
                f"Capture reported success (Fusion named the marker '{snap_name}') but taking it "
                f"MOVED {len(moved)} occurrence(s): {shown}{more}. The marker does NOT hold the "
                "pose you captured, and it REMAINS: remove it with "
                f"assembly_capture_position(action='delete', marker='{snap_name}'), re-apply the "
                "move, and read the positions back with assembly_get before and after any retry.")
        note = "Current position captured into the timeline."
        if count_after is None:
            note += (" 'snapshot_count' is null - the snapshot count could not be re-read after the "
                     f"capture, so the advance could not be confirmed; {count} marker(s) were "
                     "counted before it.")
        # 'pose_held' answers "did the parts stay where they were when I captured them" - True only
        # where a transform was actually sampled on BOTH sides. No occurrence sampled means no
        # verdict (null), never a confident yes.
        if targets and not measured:
            note += (" 'pose_held' is null - no occurrence's transform could be read on both sides "
                     "of the capture, so whether the marker holds the pose that was pending is "
                     "UNKNOWN here (it is not a 'yes'). Read the positions with assembly_get.")
        return ok({"captured": True, "snapshot": snap_name,
        "snapshot_count": count_after,
        "pose_held": True if measured else None,
        "note": note})

    if act == "discard_pending":
        # Live-verified: with nothing pending revertPendingSnapshot() RAISES "3 : Has no pending
        # snapshot" (it does not return False), so the flag is the precondition and this guard
        # refuses first. The bool it returns on the valid path is read back below.
        if not pending:
            return error("Nothing to discard - there is no pending position change.")
        try:
            did = snaps.revertPendingSnapshot()
        except Exception as e:
            return error(f"Discard failed: {e}")
        if not did:
            return error("Fusion declined to discard the pending position change "
                         "(revertPendingSnapshot returned false) - the move still stands.")
        still_pending = _common.read_flag(lambda: snaps.hasPendingSnapshot)
        if still_pending is None:
            return error("Discard ran, but the pending-position flag could not be re-read - the "
                         "confirming read could not be taken, so the move may or may not have been "
                         "thrown away. Call action='status' before acting on this result.")
        if still_pending:
            return error("Discard reported success but a pending position change is still "
                         "reported - the move was not thrown away.")
        # The count the collection reports, never the pre-read echoed back: an unreadable re-read
        # publishes null, so no caller takes a stale number for a fresh measurement.
        count_after = _common.counted(lambda: snaps.count)
        note = ("Uncaptured move thrown away - the assembly is back at its last captured position "
                "(or the joint-defined state when nothing was ever captured). Captured markers are "
                "untouched; use revert to drop the latest of those.")
        if count_after is None:
            note += (" 'snapshot_count' is null - the snapshot count could not be read after the "
                     f"discard; {count} marker(s) were counted before it.")
        return ok({"discarded": True, "has_pending": bool(still_pending),
        "snapshot_count": count_after, "note": note})

    if act == "delete":
        want = (marker or "").strip()
        if not want:
            return error("action='delete' needs 'marker' (the captured position's name, from "
                         "action='status').")
        if count < 1:
            return error("Nothing to delete - there are no captured positions.")
        hits = _find_captured(snaps, want)
        if not hits:
            names = sorted(m["name"] for m in _capture_markers(snaps) if m["name"])
            return error(f"No captured position named '{marker}'. Captured: "
                         f"{', '.join(names) or 'none'}.")
        if len(hits) > 1:
            return error(f"'{marker}' matches {len(hits)} captured positions - marker names should "
                         "be unique; check the timeline directly.")
        snap, found_name = hits[0]
        try:
            did = snap.deleteMe()
        except Exception as e:
            return error(f"Delete failed: {e}")
        if not did:
            return error(f"Fusion declined to delete captured position '{found_name}'.")
        # The count the collection reports, never a fabricated 0: an unreadable re-read publishes
        # null, so no caller reads "no captured positions remain" off a read that never answered.
        count_after = _common.counted(lambda: snaps.count)
        survivors = _find_captured(snaps, want)
        if survivors:
            return error(f"Delete reported success but '{found_name}' is still present in the "
                         "snapshot collection.")
        note = ("Captured position removed from the timeline; later captured positions (if any) "
                "survive a recompute unchanged.")
        if count_after is None:
            note += (" 'snapshot_count' is null - the snapshot count could not be re-read after the "
                     f"delete; {count} marker(s) were counted before it.")
        return ok({"deleted": True, "marker": found_name, "snapshot_count": count_after,
        "note": note})

    # revert
    if count < 1:
        return error("Nothing to revert - there are no captured positions.")
    try:
        latest = snaps.item(count - 1)
        did = latest.deleteMe()
    except Exception as e:
        return error(f"Revert failed: {e}")
    if not did:
        return error("Fusion declined to revert the latest captured position.")
    # deleteMe() answering True is not a removal. This arm holds the object it deleted, so the
    # collection is re-read for THAT object by IDENTITY - Fusion enforces no name uniqueness on
    # markers - with the count comparison as an independent second gate.
    survived = any(s is latest for s in _common.iter_collection(snaps))
    # The count the collection reports, never the arithmetic the delete expected: an unreadable
    # re-read publishes null, so no caller reads a computed number back as a measurement.
    count_after = _common.counted(lambda: snaps.count)
    count_held = count_after is not None and count_after >= count
    if survived or count_held:
        seen = (["the marker is still in the snapshot collection"] if survived else []) + (
            [f"the snapshot count did not drop ({count} before, {count_after} after)"]
            if count_held else [])
        return error("Revert reported success but " + " and ".join(seen)
                     + " - the latest captured position was not removed. List the markers with "
                       "assembly_capture_position(action='status').")
    note = ("Latest captured position discarded - back to the last captured position that remains "
            "(or the joint-defined state when nothing else was ever captured).")
    if count_after is None:
        # The survivor check runs off the same count (_common.iter_collection ranges over it), so a
        # count that will not re-read leaves BOTH confirming reads untaken - saying only that the
        # count is null would let a caller read the marker as looked for and not found.
        note += (" 'snapshot_count' is null - the snapshot count could not be re-read after the "
                 "delete, and the collection is enumerated through that same count, so the marker "
                 f"could not be looked for either; {count} marker(s) were counted before it.")
    return ok({"reverted": True, "snapshot_count": count_after, "note": note})


TOOL_DESCRIPTION = (
"Capture the assembly's current pose into the timeline as a Position marker - a pose is TRANSIENT "
"until captured."
)
tool = (
    Tool.create_simple(name="assembly_capture_position", description=TOOL_DESCRIPTION)
    .add_input_property("action", _CAPTURE_ACTION.schema())
    .add_input_property("marker", {"type": "string"})
    .strict_schema()
)
item = Item.create_tool_item(
    tool=tool, write="write", handler=handler, run_on_main_thread=True,
    verification=Verification(
        kind="inline", rung="value",
        evidence_test="tests/unit/test_assembly_capture_position.py::TestCapturePosition"
                      "::test_a_capture_that_REVERTS_the_pending_move_bites"))


def register_tool():
    register(item)
