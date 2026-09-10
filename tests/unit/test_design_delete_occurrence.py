"""Unit tests for ``design_delete_occurrence.py`` - delete one component occurrence.

The logic pinned here, no live Fusion: occurrence resolution via the shared OccurrenceRef path
(exact fullPathName, then name, ambiguity REFUSED - never the wrong instance), the actual
``Occurrence.deleteMe()`` call, the joints-removed warning (deleting an occurrence drops its joints),
the deleteMe-returns-false path (a pattern/mirror child Fusion won't delete on its own), and the
before/after timeline-health guard (a delete that introduces a new error is reported, the deletion
still standing). The fake CAPTURES the deleteMe call so a regression to a wrong method name fails
here.
"""

import live_api_facts as _api_facts
from conftest import (FakeJoint, FakeOccurrence, FakeTimeline, FakeTimelineObject, MakeComp,
                      _NamedCollection, error_message, install, load_tool, make_design, payload)

dd = load_tool("design_delete_occurrence")

_HEALTHY = _api_facts.ENUMS["fusion.FeatureHealthStates"]["HealthyFeatureHealthState"]
_WARNING = _api_facts.ENUMS["fusion.FeatureHealthStates"]["WarningFeatureHealthState"]
_ERROR = _api_facts.ENUMS["fusion.FeatureHealthStates"]["ErrorFeatureHealthState"]


# ── fakes ────────────────────────────────────────────────────────────────────

class FakeOcc(FakeOccurrence):
    """An occurrence carrying the joints/isGrounded a delete reports and the deleteMe it is made
    with. `delete_returns` is the bool the call answers; `delete_takes` False is the platform lie -
    it answers true and the instance stays in the assembly walk. `on_delete_breaks_timeline` is a
    timeline the delete injects a downstream error into."""

    def __init__(self, name, full_path=None, joints=(), grounded=False, delete_returns=True,
                 delete_takes=True):
        super().__init__(path=full_path or name, component=MakeComp(name=name.split(":")[0]))
        self.name = name
        self.joints = _NamedCollection([FakeJoint(name=j) for j in joints])
        self.isGrounded = grounded
        self._delete_returns = delete_returns
        self._delete_takes = delete_takes
        self._deleted = False
        self._siblings = None      # the allOccurrences list this instance lives in (set by _install)
        self.on_delete_breaks_timeline = None

    def deleteMe(self):
        self._deleted = True
        if self.on_delete_breaks_timeline is not None:
            self.on_delete_breaks_timeline._items.append(
                FakeTimelineObject(name="BrokenFeature", index=99, health=_ERROR))
        if self._delete_returns and self._delete_takes and self._siblings is not None:
            if self in self._siblings:
                self._siblings.remove(self)
        return self._delete_returns


def _timeline(*rows):
    """A timeline of (name, healthState) pairs - what _common.timeline_health walks."""
    return FakeTimeline([FakeTimelineObject(name=n, index=i, health=h)
                         for i, (n, h) in enumerate(rows)])


def _install(occs, timeline=None):
    """Point the design seams at one fake design holding `occs`.

    Each occurrence is told which allOccurrences list it lives in, so a successful deleteMe takes it
    out of the assembly walk - the absence the handler re-reads the path census for."""
    design = make_design(comp=MakeComp("Root", occurrences=occs),
                         timeline=timeline if timeline is not None else _timeline())
    for o in design.rootComponent.allOccurrences:
        o._siblings = design.rootComponent.allOccurrences
    return install(dd, design)


def _occ(occs, name):
    return next(o for o in occs if o.name == name)


# ── helpers ──────────────────────────────────────────────────────────────────

class TestTimelineHealthHelper:
    def test_rolls_up_errors_and_warnings(self):
        design = make_design(timeline=_timeline(("A", _HEALTHY), ("B", _ERROR),
                                                ("C", _WARNING), ("D", _ERROR)))
        errors, warnings, total = dd._timeline_health(design)
        assert total == 4
        assert errors == ["B", "D"]
        assert warnings == ["C"]

    def test_no_timeline_is_empty(self):
        # a direct-modelling design has no timeline -> empty, not a crash
        assert dd._timeline_health(make_design()) == ([], [], 0)


class TestJointNamesHelper:
    def test_lists_joint_names(self):
        occ = FakeOcc("Wheel:1", joints=["Axle_Rev", "Rigid2"])
        assert dd._joint_names(occ) == ["Axle_Rev", "Rigid2"]

    def test_none_when_no_joints(self):
        assert dd._joint_names(FakeOcc("Block:1")) == []


# ── happy path ───────────────────────────────────────────────────────────────

class TestDelete:
    def test_deletes_named_occurrence(self):
        occs = [FakeOcc("Block:1")]
        _install(occs)
        out = payload(dd.handler(occurrence="Block:1"))
        assert out["deleted"] is True
        assert out["occurrence"] == "Block:1"
        assert _occ(occs, "Block:1")._deleted is True       # deleteMe actually called

    def test_substring_match(self):
        occs = [FakeOcc("Wheel_RL:1")]
        _install(occs)
        out = payload(dd.handler(occurrence="wheel"))
        assert out["occurrence"] == "Wheel_RL:1"
        assert occs[0]._deleted is True

    def test_reports_removed_joints(self):
        # deleting a jointed occurrence drops its joints - the result must NAME them.
        occs = [FakeOcc("Arm:1", joints=["Loader_Pivot", "Rigid3"])]
        _install(occs)
        out = payload(dd.handler(occurrence="Arm:1"))
        assert out["removed_joints"] == ["Loader_Pivot", "Rigid3"]
        assert "Loader_Pivot" in out["joints_warning"]

    def test_no_joints_warning_when_unjointed(self):
        _install([FakeOcc("Block:1")])
        out = payload(dd.handler(occurrence="Block:1"))
        assert out["removed_joints"] == []
        assert "joints_warning" not in out

    def test_reports_grounded_state(self):
        _install([FakeOcc("Base:1", grounded=True)])
        out = payload(dd.handler(occurrence="Base:1"))
        assert out["was_grounded"] is True


# ── absence is proved, never assumed ─────────────────────────────────────────

class TestAbsenceReRead:
    """deleteMe()'s bool is the platform's claim; the assembly path census re-read is the proof. A
    survivor is an error, and a census that never carried the path is disclosed, never counted as
    absence."""

    def test_a_survivor_is_an_error_not_a_false_ok(self):
        # deleteMe returns true and the instance is still in the walk: the payload may not publish
        # deleted:true off the bool alone.
        occs = [FakeOcc("Block:1", delete_takes=False), FakeOcc("Keep:1")]
        _install(occs)
        msg = error_message(dd.handler(occurrence="Block:1"))
        assert "still in the assembly" in msg
        assert "Block:1" in msg

    def test_a_sibling_of_the_same_name_does_not_read_as_a_survivor(self):
        # the census keys on the full PATH, so the other Bolt:1 standing is not this one surviving.
        a = FakeOcc("Bolt:1", full_path="Sub-A:1+Bolt:1")
        b = FakeOcc("Bolt:1", full_path="Sub-B:1+Bolt:1")
        _install([a, b])
        out = payload(dd.handler(occurrence="Sub-B:1+Bolt:1"))
        assert out["deleted"] is True
        assert a._deleted is False

    def test_a_path_the_census_never_carried_is_unverified_not_absence(self):
        # fullPathName does not read, so the walk records this instance under '' and the delete's
        # absence cannot be shown either way: the flag is null and the note says why.
        occ = FakeOcc("Blind:1")
        occ._raises_on["fullPathName"] = "fullPathName unavailable"
        _install([occ])
        out = payload(dd.handler(occurrence="Blind:1"))
        assert out["deleted"] is None
        assert "UNVERIFIED" in out["note"]
        assert "Occurrence deleted." not in out["note"]   # no claim the null flag denies
        assert occ._deleted is True                       # the delete itself still happened


# ── guards ───────────────────────────────────────────────────────────────────

class TestGuards:
    def test_no_active_design_errors(self):
        install(dd, None)
        assert "no active design" in error_message(dd.handler(occurrence="Block:1")).lower()

    def test_missing_occurrence_errors(self):
        _install([FakeOcc("Block:1")])
        msg = error_message(dd.handler(occurrence="Ghost"))
        assert "no occurrence matching" in msg.lower()

    def test_empty_occurrence_errors(self):
        _install([FakeOcc("Block:1")])
        assert "required" in error_message(dd.handler(occurrence="")).lower()

    def test_ambiguous_name_refused_not_wrong_instance(self):
        # two instances share local name "Bolt:1" under different sub-assemblies - a bare "Bolt"
        # substring must ERROR (naming both fullPathNames), NOT delete the first one.
        a = FakeOcc("Bolt:1", full_path="Sub-A:1+Bolt:1")
        b = FakeOcc("Bolt:1", full_path="Sub-B:1+Bolt:1")
        _install([a, b])
        msg = error_message(dd.handler(occurrence="Bolt"))
        assert "ambiguous" in msg.lower()
        assert "Sub-A:1+Bolt:1" in msg and "Sub-B:1+Bolt:1" in msg
        assert a._deleted is False and b._deleted is False  # neither deleted

    def test_exact_full_path_targets_right_instance(self):
        a = FakeOcc("Bolt:1", full_path="Sub-A:1+Bolt:1")
        b = FakeOcc("Bolt:1", full_path="Sub-B:1+Bolt:1")
        _install([a, b])
        out = payload(dd.handler(occurrence="Sub-B:1+Bolt:1"))
        assert out["occurrence"] == "Bolt:1"
        assert b._deleted is True and a._deleted is False   # the RIGHT one

    def test_delete_me_false_is_a_refusal_not_a_false_success(self):
        # deleteMe returns false (no exception) - report the refusal, never a false ok.
        _install([FakeOcc("Wheel:2", delete_returns=False)])
        msg = error_message(dd.handler(occurrence="Wheel:2"))
        assert "deleteMe() returned false" in msg
        assert "Wheel:2" in msg

    def test_delete_me_false_states_the_read_not_a_guessed_cause(self):
        # Nothing in this call reads WHY Fusion refused - the bool carries no reason - so the error
        # may not assert one. It names the read that CAN answer it instead.
        _install([FakeOcc("Wheel:2", delete_returns=False)])
        msg = error_message(dd.handler(occurrence="Wheel:2"))
        assert "likely" not in msg.lower()          # no hedged cause guess
        assert "design_get(include=['timeline'])" in msg

    def test_timeline_error_after_delete_is_reported(self):
        # the before/after health walk saw an error the timeline did not carry before; the warning
        # names it and the payload still reports what the delete itself read back.
        tl = _timeline(("Sketch1", _HEALTHY))
        occ = FakeOcc("Block:1")
        occ.on_delete_breaks_timeline = tl
        _install([occ], tl)
        out = payload(dd.handler(occurrence="Block:1"))
        assert out["deleted"] is True
        assert "timeline_warning" in out
        assert "BrokenFeature" in out["timeline_warning"]


class TestTimelineWarningClaims:
    """The warning may state only what was read: the health walk's before/after delta. WHY the new
    error appeared is never read here, and 'deleted' - not the warning - is what says whether the
    occurrence's absence was verified."""

    def _breaks_timeline(self):
        tl = _timeline(("Sketch1", _HEALTHY))
        occ = FakeOcc("Block:1")
        occ.on_delete_breaks_timeline = tl
        _install([occ], tl)
        return occ

    def test_it_states_the_delta_and_claims_no_cause(self):
        self._breaks_timeline()
        warning = payload(dd.handler(occurrence="Block:1"))["timeline_warning"]
        assert "did not carry" in warning and "BrokenFeature" in warning
        # nothing in the handler reads WHICH feature referenced the removed geometry
        assert "referenced the removed geometry" not in warning
        assert "downstream" not in warning

    def test_it_defers_to_deleted_instead_of_asserting_the_delete_stands(self, monkeypatch):
        # A census that cannot see the path even BEFORE the delete leaves 'deleted' null, so a
        # warning asserting the deletion stands would contradict the same payload.
        self._breaks_timeline()
        monkeypatch.setattr(dd._common, "occurrence_paths", lambda d: set())
        out = payload(dd.handler(occurrence="Block:1"))
        assert out["deleted"] is None               # unverified - the claim the warning must not make
        warning = out["timeline_warning"]
        assert "'deleted'" in warning and "null = it was not" in warning
        assert "deletion stands" not in warning
