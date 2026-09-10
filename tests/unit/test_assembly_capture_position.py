"""Unit tests for ``assembly_capture_position.py`` - the timeline POSITION marker verbs.

Fusion keeps geometry history separate from assembly positions, so a moved jointed component's
pose is TRANSIENT until captured. Pinned here, no live Fusion: the pending flag as the
precondition each verb gates on (tri-state - an unread flag is not a 'no'), the phantom capture
(the add reports success while the parts snap back), and every count read back off the collection
rather than computed.
"""

import pytest

from conftest import (FakeMatrix3D, FakeSnapshot as _SharedSnapshot, _NamedCollection, install,
                      load_tool, make_design, make_placed_occurrence, payload)

ja = load_tool("assembly_capture_position")
acom = load_tool("_assembly_common")


# ── fakes ───────────────────────────────────────────────────────────────────
#
# The shared Snapshot and the shared collection protocol, each carrying the delete that LIES in the
# way this tool has to catch.

class FakeSnapshot(_SharedSnapshot):
    """The shared marker plus the three lying deletes: one that reports success and stays, one that
    leaves the collection LARGER, and one that gives up a DIFFERENT marker instead."""
    def __init__(self, name="Snapshot1", timeline_index=0, delete_ok=True, survives_delete=False,
                 spawns_on_delete=False, deletes_instead=None):
        super().__init__(name=name, delete_ok=delete_ok,
                         timeline_object=type("TL", (), {"index": timeline_index})())
        self.deleted = False
        self._survives_delete = survives_delete   # simulate deleteMe()==True but no actual removal
        # deleteMe()==True and the collection ends up LARGER: the count moves the other way, which
        # is the only reading that tells the two numbers in the refusal apart
        self._spawns_on_delete = spawns_on_delete
        # deleteMe()==True and the collection gives up a DIFFERENT marker: the count drops exactly
        # as a working removal's does while THIS one still stands, the shape no count comparison sees
        self._deletes_instead = deletes_instead
        self._parent = None

    def deleteMe(self):
        if not self._delete_ok:
            return False
        self.deleted = True
        if self._parent is not None:
            if self._spawns_on_delete:
                self._parent._add(FakeSnapshot(f"{self.name}_extra"))
            elif self._deletes_instead is not None:
                self._parent._remove(self._deletes_instead)
            elif not self._survives_delete:
                self._parent._remove(self)
            # armed whether or not the collection gave the marker up - a lying delete can leave a
            # survivor AND an unreadable count, a pairing _remove never reaches
            self._parent._delete_attempted()
        return True


class FakeSnapshots(_NamedCollection):
    """design.snapshots: the shared walk plus the pending-position flag, add/revert, and the count
    that stops reading after one named mutation - the blind re-read a verdict has to survive."""
    def __init__(self, pending=False, items=(), revert_pending_ok=True, revert_pending_lies=False,
                 blind_after_revert=False, blind_count_after_delete=False,
                 blind_count_after_add=False, blind_count_after_discard=False,
                 blind_count_after_any_delete=False):
        super().__init__(items)
        self._pending = pending
        for it in self._items:
            it._parent = self
        self.added = False
        self.reverted_pending = False
        self._revert_pending_ok = revert_pending_ok
        self._revert_pending_lies = revert_pending_lies   # returns True, flag stays set
        self._blind_after_revert = blind_after_revert     # the flag read RAISES after the revert
        self._blind = False
        # the COUNT read RAISES once the collection has been mutated - per mutation path, so a test
        # can blind exactly the re-read the handler takes after add / revertPendingSnapshot / delete
        self._blind_count_after_delete = blind_count_after_delete
        self._blind_count_after_add = blind_count_after_add
        self._blind_count_after_discard = blind_count_after_discard
        # blind_count_after_delete arms from _remove, which a marker that SURVIVES its own deleteMe
        # never reaches; this one arms from the deleteMe itself, so a survivor can arrive with a
        # count that will not re-read
        self._blind_count_after_any_delete = blind_count_after_any_delete
        self._blind_count = False

    @property
    def hasPendingSnapshot(self):
        if self._blind:
            raise RuntimeError("pending flag unreadable")
        return self._pending

    @hasPendingSnapshot.setter
    def hasPendingSnapshot(self, value):
        self._pending = value

    @property
    def count(self):
        if self._blind_count:
            raise RuntimeError("snapshot count unreadable")
        return len(self._items)

    def item(self, i):
        return self._items[i]

    def add(self):
        self.added = True
        snap = FakeSnapshot(f"Snapshot{len(self._items) + 1}")
        snap._parent = self
        self._items.append(snap)
        self.hasPendingSnapshot = False
        if self._blind_count_after_add:
            self._blind_count = True
        return snap

    def revertPendingSnapshot(self):
        """Clears hasPendingSnapshot and returns whether that took; the captured snapshots stay put.
        revert_pending_lies models a True return with the flag still set, blind_after_revert a flag
        that cannot be read afterwards. Live, the discarded pose falls back to the last captured
        position, or to the joint rest pose when nothing was ever captured."""
        self.reverted_pending = True
        if not self._revert_pending_ok:
            return False
        if not self._revert_pending_lies:
            self._pending = False
        if self._blind_after_revert:
            self._blind = True
        if self._blind_count_after_discard:
            self._blind_count = True
        return True

    def _remove(self, snap):
        if snap in self._items:
            self._items.remove(snap)
        if self._blind_count_after_delete:
            self._blind_count = True

    def _add(self, snap):
        snap._parent = self
        self._items.append(snap)

    def _delete_attempted(self):
        """Called by every deleteMe() that answered True, removal or not - where
        blind_count_after_any_delete blinds the count read the handler takes next."""
        if self._blind_count_after_any_delete:
            self._blind_count = True


_part = make_placed_occurrence


@pytest.fixture
def capture():
    """Factory: install a design carrying `occurrences` and a configurable snapshots collection.

    snapshots=False is the design that exposes NO snapshots surface at all, where the collection
    itself is what cannot be reached rather than the flag on it.
    """
    def _make(pending=False, occurrences=(), snapshot_items=(), snapshots=True, **snapshot_kwargs):
        snaps = FakeSnapshots(pending=pending, items=snapshot_items, **snapshot_kwargs)
        design = make_design(occurrences=list(occurrences),
                             snapshots=snaps if snapshots else None)
        install(ja, design)
        return design, snaps
    return _make


# ── assembly_capture_position ─────────────────────────────────────────────────────────

class TestCapturePosition:
    def test_capture_when_pending(self, capture):
        _design, snaps = capture(pending=True)
        out = payload(ja.handler(action="capture"))
        assert snaps.added is True
        assert out["captured"] is True

    def test_capture_with_an_unreadable_count_publishes_null_not_the_arithmetic(self, capture):
        # the count re-read RAISES after add(): publishing "one more than before" would hand the
        # caller a number the tool computed, not one it read off the collection.
        capture(pending=True, blind_count_after_add=True)
        out = payload(ja.handler(action="capture"))
        assert out["captured"] is True
        assert out["snapshot_count"] is None
        assert "'snapshot_count' is null" in out["note"]

    def test_capture_publishes_the_count_it_read_back(self, capture):
        capture(pending=True, snapshot_items=[FakeSnapshot("Position1")])
        out = payload(ja.handler(action="capture"))
        assert out["snapshot_count"] == 2
        assert "null" not in out["note"]

    def test_a_capture_that_REVERTS_the_pending_move_bites(self, capture):
        # The cardinal sin this gate exists for: snapshots.add() answers with a snapshot object AND
        # an advanced count while the moved part snaps back to where it stood before the move, so the
        # marker records the PRE-move pose. Neither the object nor the count says anything about the
        # POSITION - only the parts' own transforms do.
        moved = _part("PartB:1", pos=(2.5, 0.0, 0.0))       # cm: the pending, moved pose
        _design, snaps = capture(pending=True, occurrences=[moved])
        real_add = snaps.add

        def reverting_add():
            snap = real_add()
            moved.transform2 = FakeMatrix3D()                # the platform throws the move away
            return snap

        snaps.add = reverting_add
        res = ja.handler(action="capture")
        assert res["isError"] is True
        assert "does NOT hold the pose you captured" in res["message"]
        assert "PartB:1" in res["message"]
        assert "25.0 mm" in res["message"]                  # the snap-back, in mm
        assert "action='delete'" in res["message"]          # the stale marker is named for removal

    def test_a_capture_that_HOLDS_the_pose_is_confirmed(self, capture):
        # the same shape with the pose held: ok, and 'pose_held' says the transforms were re-read
        capture(pending=True, occurrences=[_part("PartB:1", pos=(2.5, 0.0, 0.0))])
        out = payload(ja.handler(action="capture"))
        assert out["captured"] is True
        assert out["pose_held"] is True

    def test_a_pose_that_cannot_be_read_is_null_not_a_confident_yes(self, capture):
        # the occurrence exists but its transform will not read, so whether the marker holds the
        # pending pose is UNKNOWN - published as null with the reason, never as a confirmed hold.
        capture(pending=True, occurrences=[_part("PartB:1")])
        out = payload(ja.handler(action="capture"))
        assert out["captured"] is True
        assert out["pose_held"] is None
        assert "'pose_held' is null" in out["note"]

    # The snap-back tolerance is an EXACT boundary: _MOVE_TOL_CM is joint-solver noise, so a shift OF
    # exactly that much is not a revert (strictly greater wins) while a hair more is.
    def _capture_after_shift(self, capture, shift_cm):
        occ = _part("PartB:1", pos=(0.0, 0.0, 0.0))
        _design, snaps = capture(pending=True, occurrences=[occ])
        real_add = snaps.add

        def shifting_add():
            snap = real_add()
            occ.transform2 = FakeMatrix3D(t=(shift_cm, 0.0, 0.0))
            return snap

        snaps.add = shifting_add
        return ja.handler(action="capture")

    def test_a_snap_back_of_exactly_the_tolerance_is_not_a_revert(self, capture):
        res = self._capture_after_shift(capture, acom._MOVE_TOL_CM)
        assert payload(res)["captured"] is True

    def test_a_snap_back_just_over_the_tolerance_is_a_revert(self, capture):
        res = self._capture_after_shift(capture, acom._MOVE_TOL_CM * 1.001)
        assert res["isError"] is True
        assert "does NOT hold the pose you captured" in res["message"]

    def test_capture_with_nothing_pending_errors(self, capture):
        capture(pending=False)
        res = ja.handler(action="capture")
        assert res["isError"] is True
        assert "no pending" in res["message"].lower()

    def test_phantom_capture_bites(self, capture):
        # add() returns a snapshot object but the count never advances -> error, not ok
        _design, snaps = capture(pending=True)
        snaps.add = lambda: FakeSnapshot("Phantom")
        res = ja.handler(action="capture")
        assert res["isError"] is True
        assert "did not advance" in res["message"]

    def test_status_reports_pending_and_count(self, capture):
        capture(pending=True, snapshot_items=[FakeSnapshot()])
        out = payload(ja.handler(action="status"))
        assert out["has_pending"] is True
        assert out["snapshot_count"] == 1

    def test_status_lists_captured_markers(self, capture):
        capture(snapshot_items=[FakeSnapshot("Position1", timeline_index=3),
                                FakeSnapshot("Position2", timeline_index=5)])
        out = payload(ja.handler(action="status"))
        assert out["markers"] == [{"name": "Position1", "timeline_index": 3},
                                  {"name": "Position2", "timeline_index": 5}]

    def test_delete_removes_named_marker(self, capture):
        snap1, snap2 = FakeSnapshot("Position1"), FakeSnapshot("Position2")
        _design, snaps = capture(snapshot_items=[snap1, snap2])
        out = payload(ja.handler(action="delete", marker="Position1"))
        assert out["deleted"] is True and out["marker"] == "Position1"
        assert snap1.deleted is True
        assert snaps.count == 1
        assert snaps.item(0).name == "Position2"

    def test_delete_is_case_insensitive(self, capture):
        capture(snapshot_items=[FakeSnapshot("Position1")])
        out = payload(ja.handler(action="delete", marker="position1"))
        assert out["deleted"] is True

    def test_delete_declining_bool_errors(self, capture):
        # Fusion declines the delete (deleteMe() returns False) -> error, not a false success.
        snap = FakeSnapshot("Position1", delete_ok=False)
        capture(snapshot_items=[snap])
        res = ja.handler(action="delete", marker="Position1")
        assert res["isError"] is True
        assert "declined" in res["message"].lower()
        assert snap.deleted is False

    def test_delete_unknown_name_lists_candidates(self, capture):
        capture(snapshot_items=[FakeSnapshot("Position1"), FakeSnapshot("Position2")])
        res = ja.handler(action="delete", marker="Ghost")
        assert res["isError"] is True
        assert "Position1" in res["message"] and "Position2" in res["message"]

    def test_delete_survivor_after_delete_is_error(self, capture):
        # deleteMe() reports True but the snapshot is still in the collection on re-read -> error.
        capture(snapshot_items=[FakeSnapshot("Position1", survives_delete=True)])
        res = ja.handler(action="delete", marker="Position1")
        assert res["isError"] is True
        assert "still present" in res["message"].lower()

    def test_delete_with_an_unreadable_count_publishes_null_not_zero(self, capture):
        # the count re-read RAISES after the delete: a fabricated 0 would tell the caller every
        # captured position is gone when a later marker is still standing.
        capture(snapshot_items=[FakeSnapshot("Position1"), FakeSnapshot("Position2")],
                blind_count_after_delete=True)
        out = payload(ja.handler(action="delete", marker="Position1"))
        assert out["deleted"] is True
        assert out["snapshot_count"] is None
        assert "'snapshot_count' is null" in out["note"]

    def test_delete_publishes_the_count_it_read_back(self, capture):
        capture(snapshot_items=[FakeSnapshot("Position1"), FakeSnapshot("Position2")])
        out = payload(ja.handler(action="delete", marker="Position1"))
        assert out["snapshot_count"] == 1

    def test_delete_needs_marker_argument(self, capture):
        capture(snapshot_items=[FakeSnapshot("Position1")])
        res = ja.handler(action="delete", marker="")
        assert res["isError"] is True and "marker" in res["message"].lower()

    def test_delete_with_no_snapshots_errors(self, capture):
        capture(snapshot_items=[])
        res = ja.handler(action="delete", marker="Position1")
        assert res["isError"] is True and "nothing to delete" in res["message"].lower()

    def test_revert_deletes_latest_snapshot(self, capture):
        snap = FakeSnapshot()
        capture(snapshot_items=[snap])
        out = payload(ja.handler(action="revert"))
        assert snap.deleted is True
        assert out["reverted"] is True

    def test_revert_that_KEEPS_the_marker_is_an_error(self, capture):
        # The lying delete: deleteMe() answers True while the marker stays in the collection.
        # Gating on the bool alone publishes reverted:true for a timeline nothing left; here both
        # re-reads convict it, and the refusal states each one it took.
        capture(snapshot_items=[FakeSnapshot("Position1", survives_delete=True)])
        res = ja.handler(action="revert")
        assert res["isError"] is True
        assert "still in the snapshot collection" in res["message"]
        assert "did not drop" in res["message"]
        assert "1 before, 1 after" in res["message"]

    def test_a_surviving_marker_is_caught_even_when_the_COUNT_DROPPED(self, capture):
        # The narrower state no count comparison sees: the collection gave up a DIFFERENT marker,
        # so the count falls exactly as a working revert's does (2 before, 1 after) while the one
        # this arm deleted is still standing. The count gate passes it; the object the arm HELD,
        # read back by identity, is the only read that convicts it.
        other = FakeSnapshot("Position1")
        latest = FakeSnapshot("Position2", deletes_instead=other)
        capture(snapshot_items=[other, latest])
        res = ja.handler(action="revert")
        assert res["isError"] is True
        assert "still in the snapshot collection" in res["message"]
        assert "was not removed" in res["message"]
        assert "did not drop" not in res["message"]      # the count gate passed it, as it must

    def test_a_revert_whose_count_will_not_read_says_the_marker_could_not_be_looked_for(self, capture):
        # BOTH confirming reads run off the collection's own count (_common.iter_collection ranges
        # over it), so a count that will not re-read leaves the survivor check unable to run at
        # all. The receipt says that too - 'snapshot_count is null' alone would let a caller read
        # the marker as looked for and not found.
        capture(snapshot_items=[FakeSnapshot("Position1", survives_delete=True)],
                blind_count_after_any_delete=True)
        out = payload(ja.handler(action="revert"))
        assert out["snapshot_count"] is None
        assert "the marker could not be looked for either" in out["note"]

    def test_revert_reads_back_the_object_it_DELETED_not_a_marker_of_the_same_name(self, capture):
        # The re-read is by IDENTITY, not by name: Fusion enforces no uniqueness on marker names,
        # so a second marker wearing the deleted one's name makes a name-keyed re-read refuse a
        # removal that took. The count DID drop here, so nothing else in this arm can refuse.
        twin, gone = FakeSnapshot("Position1"), FakeSnapshot("Position1")
        capture(snapshot_items=[twin, gone])
        out = payload(ja.handler(action="revert"))
        assert out["reverted"] is True and gone.deleted is True and twin.deleted is False
        assert out["snapshot_count"] == 1

    def test_the_revert_note_does_not_claim_the_joint_defined_state_with_markers_LEFT(self, capture):
        # Dropping the latest of several markers does not put the assembly back at the
        # joint-defined state - that is where it lands only when nothing else was ever captured,
        # which is how the discard arm words the same restore target.
        capture(snapshot_items=[FakeSnapshot("Position1"), FakeSnapshot("Position2")])
        out = payload(ja.handler(action="revert"))
        assert "the last captured position that remains" in out["note"]
        assert "(back to the joint-defined state)" not in out["note"]
        assert "when nothing else was ever captured" in out["note"]

    def test_a_revert_after_which_the_count_GREW_labels_the_two_numbers(self, capture):
        # The equal-count refusal reads the same whichever way round its two numbers are printed,
        # so it cannot pin the labels. A count that moved the other way can: the number taken
        # BEFORE the delete is 1 and the one read back after is 2, and a receipt that swaps them
        # sends the caller looking for a marker that never existed.
        capture(snapshot_items=[FakeSnapshot("Position1", spawns_on_delete=True)])
        res = ja.handler(action="revert")
        assert res["isError"] is True
        assert "did not drop" in res["message"]
        assert "1 before, 2 after" in res["message"]

    def test_revert_that_drops_the_count_by_one_is_accepted(self, capture):
        # The other side of that comparison's boundary: one fewer than before IS the removal, so a
        # gate written as "did not change" rather than "did not drop" would refuse a working revert.
        capture(snapshot_items=[FakeSnapshot("Position1"), FakeSnapshot("Position2")])
        out = payload(ja.handler(action="revert"))
        assert out["reverted"] is True and out["snapshot_count"] == 1

    def test_revert_with_an_unreadable_count_publishes_null_not_the_arithmetic(self, capture):
        # the count re-read RAISES after the delete: publishing "one fewer than before" would hand
        # the caller a number the tool computed, not one it read off the collection.
        capture(snapshot_items=[FakeSnapshot()], blind_count_after_delete=True)
        out = payload(ja.handler(action="revert"))
        assert out["reverted"] is True
        assert out["snapshot_count"] is None
        assert "'snapshot_count' is null" in out["note"]

    def test_revert_publishes_the_count_it_read_back(self, capture):
        capture(snapshot_items=[FakeSnapshot("Position1"), FakeSnapshot("Position2")])
        out = payload(ja.handler(action="revert"))
        assert out["snapshot_count"] == 1
        assert "null" not in out["note"]

    def test_revert_with_no_snapshots_errors(self, capture):
        capture(snapshot_items=[])
        res = ja.handler(action="revert")
        assert res["isError"] is True and "no captured" in res["message"].lower()

    def test_discard_pending_throws_the_uncaptured_move_away(self, capture):
        snap = FakeSnapshot("Position1")
        _design, snaps = capture(pending=True, snapshot_items=[snap])
        out = payload(ja.handler(action="discard_pending"))
        assert out["discarded"] is True
        assert out["has_pending"] is False
        assert snaps.hasPendingSnapshot is False
        # discarding the PENDING move is not reverting a CAPTURED one - the marker survives
        assert snap.deleted is False
        assert out["snapshot_count"] == 1

    def test_discard_with_an_unreadable_count_publishes_null_not_the_pre_read(self, capture):
        # the count read RAISES after the discard: echoing the count taken BEFORE the call would
        # publish a stale number as a fresh read-back.
        capture(pending=True, snapshot_items=[FakeSnapshot("Position1")],
                blind_count_after_discard=True)
        out = payload(ja.handler(action="discard_pending"))
        assert out["discarded"] is True
        assert out["snapshot_count"] is None
        assert "'snapshot_count' is null" in out["note"]

    def test_discard_pending_with_nothing_pending_errors_without_calling_the_api(self, capture):
        _design, snaps = capture(pending=False)
        res = ja.handler(action="discard_pending")
        assert res["isError"] is True and "nothing to discard" in res["message"].lower()
        assert snaps.reverted_pending is False

    def test_discard_pending_declining_bool_errors(self, capture):
        # revertPendingSnapshot() returns false -> the move still stands; never a false success.
        capture(pending=True, revert_pending_ok=False)
        res = ja.handler(action="discard_pending")
        assert res["isError"] is True and "declined" in res["message"].lower()

    def test_discard_pending_that_leaves_the_flag_set_is_an_error(self, capture):
        # the platform returns True while the pending change survives - the re-read must catch it.
        capture(pending=True, revert_pending_lies=True)
        res = ja.handler(action="discard_pending")
        assert res["isError"] is True and "still" in res["message"].lower()

    def test_discard_pending_with_an_unreadable_flag_afterwards_is_an_error(self, capture):
        # the confirming re-read RAISES: publishing has_pending False here would report a
        # measurement the tool never took, so the unreadable flag is an error, not a success.
        capture(pending=True, blind_after_revert=True)
        res = ja.handler(action="discard_pending")
        assert res["isError"] is True
        assert "could not be re-read" in res["message"]
        assert "status" in res["message"]

    def test_unknown_action(self, capture):
        capture()
        res = ja.handler(action="frobnicate")
        assert res["isError"] is True and "must be one of" in res["message"]

    def test_a_design_exposing_no_snapshots_is_refused_before_any_verb(self, capture):
        # The collection itself is what will not read, so there is no flag to be tri-state about -
        # every verb needs it, and the refusal names the capability rather than the flag.
        capture(snapshots=False)
        res = ja.handler(action="status")
        assert res["isError"] is True
        assert "does not expose snapshots" in res["message"]


class TestStatusPublishesTheFlagTriState:
    """has_pending is the flag itself, not a coerced boolean: True, False, or null when it could not
    be read. Publishing an unreadable flag as false would tell a caller "nothing is pending" on the
    one reading that supports no answer at all - and that caller then creates a joint through it."""

    def test_a_readable_true_publishes_true(self, capture):
        capture(pending=True)
        assert payload(ja.handler(action="status"))["has_pending"] is True

    def test_a_readable_false_publishes_false(self, capture):
        capture(pending=False)
        assert payload(ja.handler(action="status"))["has_pending"] is False

    def test_an_unreadable_flag_publishes_null_not_false(self, capture):
        _design, snaps = capture(pending=False)
        snaps._blind = True
        out = payload(ja.handler(action="status"))
        assert out["has_pending"] is None
        assert "UNKNOWN" in out["note"] and "not a 'no'" in out["note"]

    def test_the_markers_still_come_back_when_the_flag_is_unreadable(self, capture):
        # the flag and the marker list are independent reads - losing one must not blank the other
        _design, snaps = capture(pending=False,
                                 snapshot_items=[FakeSnapshot("Position1", timeline_index=3)])
        snaps._blind = True
        out = payload(ja.handler(action="status"))
        assert out["markers"] == [{"name": "Position1", "timeline_index": 3}]

    def test_an_unreadable_flag_still_refuses_a_capture(self, capture):
        # tri-state on the wire does not loosen the act precondition: only a real True may capture
        _design, snaps = capture(pending=False)
        snaps._blind = True
        res = ja.handler(action="capture")
        assert res["isError"] is True and "no pending" in res["message"].lower()

    def test_the_status_note_names_both_exceptions(self, capture):
        # neither a placement nor a constraint sets this flag - a caller told otherwise chases a
        # capture that refuses
        capture(pending=True)
        note = payload(ja.handler(action="status"))["note"]
        assert ("a design_add_instance placement and an assembly_constrain relationship do NOT"
                in note)
