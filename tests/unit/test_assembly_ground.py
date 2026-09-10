"""Unit tests for ``assembly_ground.py`` - the isGroundToParent parent lock.

The logic pinned here, no live Fusion: occurrence resolution through the shared ambiguity-refusing
resolver, the flag's read-back (a stuck flag is an error, never a reported lock), and the
position_reset the lock's snap-back is disclosed with.
"""

import pytest

from conftest import (FakeMatrix3D, FakeOccurrence, FakeVector3D, MakeComp, MakeDesign, install,
                      load_tool, payload)


asm = load_tool("assembly_ground")


def _occurrence(path="Block:1", cls=FakeOccurrence, **kw):
    """One occurrence a ground call can target, both grounding flags and both placements readable.

    Starting UNLOCKED and UNGROUNDED is what makes the read-backs bite: a flag already reading the
    value being written would report a lock no write had to land.
    """
    kw.setdefault("component", MakeComp(name=path.split("+")[-1].split(":")[0]))
    kw.setdefault("transform", FakeMatrix3D())
    kw.setdefault("transform2", FakeMatrix3D())
    kw.setdefault("ground_to_parent", False)
    kw.setdefault("grounded", False)
    return cls(path=path, **kw)


class SnappingOcc(FakeOccurrence):
    """An occurrence the parent lock snaps back to its timeline placement, discarding a free move."""

    @property
    def isGroundToParent(self):
        return FakeOccurrence.isGroundToParent.fget(self)

    @isGroundToParent.setter
    def isGroundToParent(self, value):
        FakeOccurrence.isGroundToParent.fset(self, value)
        if value:
            self.transform.translation = FakeVector3D(0.0, 0.0, 0.0)


@pytest.fixture
def wire():
    """Build a design placing `occurrences` and wire it into both tool seams."""
    def build(*occurrences):
        install(asm, MakeDesign(comp=MakeComp(occurrences=list(occurrences))))
    return build


class TestGround:
    # assembly_ground sets ONLY isGroundToParent (the stateless parent lock). The UI Ground/Fix
    # flag (isGrounded) is deliberately not settable - the platform treats it as legacy - but
    # both flags are reported back so the caller sees the full grounding state.
    def test_lock_to_parent(self, wire):
        occ = _occurrence()
        wire(occ)
        out = payload(asm.handler(occurrence="Block:1", ground_to_parent=True))
        assert occ.isGroundToParent is True
        assert out["isGroundToParent"] is True

    def test_unground_from_parent_releases_lock(self, wire):
        occ = _occurrence(ground_to_parent=True)
        wire(occ)
        out = payload(asm.handler(occurrence="Block:1", ground_to_parent=False))
        assert occ.isGroundToParent is False
        assert out["isGroundToParent"] is False

    def test_stuck_flag_bites(self, wire):
        # the assignment is accepted but the flag still reads its prior value -> error, not ok
        wire(_occurrence(ground_to_parent=True, ground_lies=True))
        res = asm.handler(occurrence="Block:1", ground_to_parent=False)
        assert res["isError"] is True
        assert "did not take" in res["message"]

    def test_unreadable_flag_after_the_set_is_unconfirmed_not_ok(self, wire):
        # the confirming read RAISES: a flag that cannot be read is not a confirmation, so a
        # swallowed write must not pass as a set lock. Unconfirmed is an error, never an ok.
        wire(_occurrence(raises_on={"isGroundToParent": "isGroundToParent unreadable"}))
        res = asm.handler(occurrence="Block:1", ground_to_parent=False)
        assert res["isError"] is True
        assert "UNCONFIRMED" in res["message"]
        assert "cannot be read" in res["message"]

    def test_unreadable_isGrounded_reports_null_not_false(self, wire):
        # the read-only context flag is published as null when it cannot be read - False is an
        # answer ("not fixed in the UI") and would be a reading nobody took.
        wire(_occurrence(raises_on={"isGrounded": "isGrounded unreadable"}))
        out = payload(asm.handler(occurrence="Block:1", ground_to_parent=True))
        assert out["isGrounded"] is None
        assert out["isGroundToParent"] is True

    def test_grounding_snap_back_is_reported_with_numbers(self, wire):
        # Setting ground_to_parent=true snaps the part back to its timeline placement, discarding
        # free moves captured or not (live-verified) - the payload must report the snap, not let the
        # caller discover a teleported part later.
        wire(_occurrence(cls=SnappingOcc, transform=FakeMatrix3D(t=(2.0, 0.0, 0.0))))
        out = payload(asm.handler(occurrence="Block:1", ground_to_parent=True))
        assert out["position_reset"] == {"from_mm": [20.0, 0.0, 0.0], "to_mm": [0.0, 0.0, 0.0]}
        assert "SNAPPED" in out["position_warning"]

    def test_no_snap_reports_no_reset(self, wire):
        # position held through the flag set
        wire(_occurrence(transform=FakeMatrix3D(t=(2.0, 0.0, 0.0))))
        out = payload(asm.handler(occurrence="Block:1", ground_to_parent=True))
        assert "position_reset" not in out and "position_warning" not in out

    def test_isGrounded_is_never_written(self, wire):
        # The tool sets ONLY isGroundToParent; the UI Ground/Fix flag (isGrounded) is not settable
        # through this surface and stays exactly as it was.
        occ = _occurrence()
        wire(occ)
        asm.handler(occurrence="Block:1", ground_to_parent=True)
        assert occ.isGrounded is False              # untouched

    def test_no_grounded_param_is_rejected_by_strict_schema(self, wire):
        # There is no 'grounded' input - only 'ground_to_parent'. (At the MCP boundary the strict
        # schema rejects it; at the Python level it's a TypeError.)
        wire(_occurrence())
        with pytest.raises(TypeError):
            asm.handler(occurrence="Block:1", grounded=True)

    def test_mode_input_is_gone(self, wire):
        # There is no 'mode' selector - isGrounded is not reachable through any input. (Strict
        # schema at the MCP boundary; TypeError at the Python level.)
        wire(_occurrence())
        with pytest.raises(TypeError):
            asm.handler(occurrence="Block:1", ground_to_parent=True, mode="fixed")

    def test_reports_both_flags_distinctly(self, wire):
        # the payload carries BOTH flags so the caller sees the full grounding state (a human may
        # have set isGrounded in the UI).
        wire(_occurrence())
        out = payload(asm.handler(occurrence="Block:1", ground_to_parent=True))
        assert "isGroundToParent" in out and "isGrounded" in out
        assert out["isGroundToParent"] is True

    def test_substring_match(self, wire):
        wire(_occurrence())
        out = payload(asm.handler(occurrence="block", ground_to_parent=True))
        assert out["occurrence"] == "Block:1"

    def test_missing_occurrence_errors(self, wire):
        wire(_occurrence())
        res = asm.handler(occurrence="Ghost", ground_to_parent=True)
        assert res["isError"] is True and "no occurrence matching" in res["message"].lower()

    def test_no_change_requested_errors(self, wire):
        wire(_occurrence())
        res = asm.handler(occurrence="Block:1")
        assert res["isError"] is True and "ground_to_parent" in res["message"]

    def test_ambiguous_name_refused_not_wrong_instance(self, wire):
        # The wrong-instance bug: two instances share the local name "Bolt:1" under different
        # sub-assemblies. A bare "Bolt" substring must ERROR (naming both fullPathNames), NOT silently
        # ground the first one.
        a, b = _occurrence("Sub-A:1+Bolt:1"), _occurrence("Sub-B:1+Bolt:1")
        wire(a, b)
        res = asm.handler(occurrence="Bolt", ground_to_parent=True)
        assert res["isError"] is True
        assert "ambiguous" in res["message"].lower()
        assert "Sub-A:1+Bolt:1" in res["message"] and "Sub-B:1+Bolt:1" in res["message"]
        # And neither was mutated (the ambiguous call refused before touching either).
        assert a.isGroundToParent is False and b.isGroundToParent is False

    def test_exact_full_path_targets_the_right_instance(self, wire):
        a, b = _occurrence("Sub-A:1+Bolt:1"), _occurrence("Sub-B:1+Bolt:1")
        wire(a, b)
        res = asm.handler(occurrence="Sub-B:1+Bolt:1", ground_to_parent=True)
        assert res["isError"] is False
        assert b.isGroundToParent is True and a.isGroundToParent is False   # the RIGHT one
