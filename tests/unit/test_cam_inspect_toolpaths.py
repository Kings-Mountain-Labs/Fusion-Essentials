"""Unit tests for ``cam_inspect_toolpaths.py`` - the toolpath validity verdict.

Covers the two dispatch paths (no scope -> CAM.checkAllToolpaths; a NAME -> CAM.checkToolpath on the
resolved setup/folder/pattern/operation), the shared resolver's refusals reaching this entry point
(a miss lists the available names, a duplicated name is refused and nothing is checked), the payload
composition (verdict + states tally + the per-operation rows, both from the one shared classifier),
the disagreement branches (the verdict and the per-operation reads are independent), the row cap,
the ACTIVE-operations default (suppressed operations are excluded and the excluded count is named;
include_suppressed=true counts them), the empty-setup answer (a setup with no operations is a fact,
not an exception), and the guards (the shared no-CAM gate, a non-boolean verdict, a raising check).
"""

import json
from types import SimpleNamespace

import pytest

from conftest import load_tool, make_cam
from conftest import FakeSetup, FakeCAMFolder, FakeOperation

mod = load_tool("cam_inspect_toolpaths")
cc = load_tool("_cam_common")


def _payload(result):
    assert result["isError"] is False, result
    return json.loads(result["content"][0]["text"])


def _cam(*setups, verdict=True, scoped_verdict=None, raises=None, all_raises=None,
         setup_verdicts=None, machining_times=None):
    """A CAM product carrying `setups` plus the two validity-check entry points the tool calls.
    Every target asked about is recorded on `checked` ("all" for the whole-document call), so a test
    can pin WHICH object was checked, not just the returned verdict. `all_raises`/`raises` make an
    entry point raise; `setup_verdicts` gives each setup NAME its own verdict.

    `machining_times` is the shared {operation name: seconds} mapping the EMPTY-toolpath overlay
    reads (see conftest.make_cam) - an operation absent from it answers no time at all."""
    cam = make_cam(*setups, machining_times=machining_times)
    cam.checked = []

    def check_all():
        cam.checked.append("all")
        if all_raises is not None:
            raise all_raises
        return verdict

    def check_toolpath(target):
        cam.checked.append(target)
        if raises is not None:
            raise raises
        if setup_verdicts is not None:
            return setup_verdicts[target.name]
        return verdict if scoped_verdict is None else scoped_verdict

    cam.checkAllToolpaths = check_all
    cam.checkToolpath = check_toolpath
    return cam


@pytest.fixture
def wire(monkeypatch):
    """Wire one CAM product into the tool's shared get_cam seam; the patch undoes itself."""
    def _wire(cam):
        monkeypatch.setattr(mod, "get_cam", lambda: (cam, None))
        return cam
    return _wire


# ── scope dispatch: which API entry point, on which object ──────────────────────────────────────────

class TestScopeDispatch:
    def test_no_scope_checks_the_whole_document(self, wire):
        cam = wire(_cam(FakeSetup("S1", ops=[FakeOperation("Face1")])))
        out = _payload(mod.handler())
        assert cam.checked == ["all"]                      # checkAllToolpaths, not a scoped check
        assert out["measured"]["scope"] == "document"
        assert out["checked"] == "checkAllToolpaths"

    def test_named_setup_is_checked_by_that_setup_object(self, wire):
        setup = FakeSetup("Roughing", ops=[FakeOperation("Face1")])
        cam = wire(_cam(setup, FakeSetup("Finishing")))
        out = _payload(mod.handler(scope="roughing"))      # case-insensitive exact
        assert cam.checked == [setup]
        assert out["measured"]["scope"] == "setup 'Roughing'"
        assert out["checked"] == "checkToolpath"

    def test_named_operation_scopes_the_breakdown_to_that_operation(self, wire):
        op = FakeOperation("Drill1", operation_state=1)
        cam = wire(_cam(FakeSetup("S1", ops=[op, FakeOperation("Face1")])))
        out = _payload(mod.handler(scope="Drill1"))
        assert cam.checked == [op]
        assert out["measured"]["scope"] == "operation 'Drill1'"
        assert out["measured"]["states"]["total"] == 1     # the sibling Face1 is out of scope

    def test_folder_scope_checks_the_folder_and_its_nested_operations(self, wire):
        folder = FakeCAMFolder("Drilling", ops=[FakeOperation("D1"), FakeOperation("D2")])
        cam = wire(_cam(FakeSetup("S1", ops=[FakeOperation("Face1")], folders=[folder])))
        out = _payload(mod.handler(scope="Drilling"))
        assert cam.checked == [folder]
        assert out["measured"]["scope"] == "folder 'Drilling'"
        assert out["measured"]["states"]["total"] == 2

    def test_pattern_scope_resolves_too(self, wire):
        pattern = FakeCAMFolder("Bolt Pattern", ops=[FakeOperation("P1")])
        cam = wire(_cam(FakeSetup("S1", patterns=[pattern])))
        out = _payload(mod.handler(scope="Bolt Pattern"))
        assert cam.checked == [pattern]
        assert out["measured"]["scope"] == "pattern 'Bolt Pattern'"


# ── the whole-document path: checkAllToolpaths, then the per-setup fallback ─────────────────────────

_NOT_CAM_OBJECTS = RuntimeError("3 : The operations are not CAM objects")


class TestWholeDocumentFallback:
    def test_a_raising_check_all_falls_back_to_the_per_setup_checks(self, wire):
        s1 = FakeSetup("S1", ops=[FakeOperation("Face1")])
        s2 = FakeSetup("S2", ops=[FakeOperation("Face2")])
        cam = wire(_cam(s1, s2, verdict=True, all_raises=_NOT_CAM_OBJECTS))
        out = _payload(mod.handler())
        assert cam.checked == ["all", s1, s2]              # tried the document, then every setup
        assert out["passed"] is True
        # 'checked' is where the fallback path is reported; the note no longer narrates it
        assert out["checked"] == "per-setup fallback"
        assert "checkAllToolpaths" not in out["note"]

    def test_the_fallback_verdict_is_the_and_of_the_setups(self, wire):
        s1 = FakeSetup("S1", ops=[FakeOperation("Face1")])
        s2 = FakeSetup("S2", ops=[FakeOperation("Bore", operation_state=1)])
        cam = wire(_cam(s1, s2, all_raises=_NOT_CAM_OBJECTS,
                        setup_verdicts={"S1": True, "S2": False}))
        out = _payload(mod.handler())
        assert out["passed"] is False                      # one failing setup fails the document
        assert cam.checked == ["all", s1, s2]              # every setup asked, no short-circuit
        assert out["measured"]["not_valid"] == [{"operation": "Bore", "state": "out_of_date"}]

    def test_both_paths_raising_is_one_clean_error_naming_each(self, wire):
        cam = wire(_cam(FakeSetup("S1", ops=[FakeOperation("Face1")]),
                        all_raises=_NOT_CAM_OBJECTS, raises=RuntimeError("2 : bad target")))
        res = mod.handler()
        assert res["isError"] is True
        assert "3 : The operations are not CAM objects" in res["message"]
        assert "the per-setup fallback raised 2 : bad target" in res["message"]
        assert cam.checked == ["all", cam.setups.item(0)]

    def test_a_non_boolean_from_the_fallback_is_refused_not_and_ed(self, wire):
        # AND-ing a non-boolean would launder it into a true/false verdict; the guard names it
        wire(_cam(FakeSetup("S1", ops=[FakeOperation("Face1")]), all_raises=_NOT_CAM_OBJECTS,
                  setup_verdicts={"S1": "true"}))
        res = mod.handler()
        assert res["isError"] is True
        assert "not a true/false verdict" in res["message"] and "str" in res["message"]


# ── the scoped path: checkToolpath on the target, then the per-operation fallback ───────────────────

_INPUT_IS_NULL = RuntimeError("3 : input is null")


class TestScopedFallback:
    """A scoped check has the same shape as the document one: when checkToolpath raises on the named
    target, the verdict is the AND of the checks on the operations nested under it (checkToolpath
    takes an Operation too). A target that IS an operation has nothing narrower to ask, so its raise
    is reported as the raise it was."""

    def _wire_raising_target(self, wire, setup, target, op_verdicts=None):
        """A CAM whose checkToolpath raises for `target` only; anything else answers from
        `op_verdicts` (by name), or True."""
        cam = wire(_cam(setup))

        def check(obj):
            cam.checked.append(obj)
            if obj is target:
                raise _INPUT_IS_NULL
            return True if op_verdicts is None else op_verdicts[obj.name]

        cam.checkToolpath = check
        return cam

    def test_a_raising_setup_check_falls_back_to_its_operations(self, wire):
        ops = [FakeOperation("Face1"), FakeOperation("Face2")]
        setup = FakeSetup("Roughing", ops=ops)
        cam = self._wire_raising_target(wire, setup, setup)
        out = _payload(mod.handler(scope="Roughing"))
        assert cam.checked == [setup, ops[0], ops[1]]   # the target, then every nested operation
        assert out["passed"] is True
        assert out["checked"] == "per-operation fallback"   # the key that names which path answered
        assert "fallback" not in out["note"]            # narrated by the key, not by the note

    def test_the_fallback_verdict_is_the_and_of_the_operations(self, wire):
        ops = [FakeOperation("Face1"), FakeOperation("Bore", operation_state=1)]
        setup = FakeSetup("Roughing", ops=ops)
        cam = self._wire_raising_target(wire, setup, setup, {"Face1": True, "Bore": False})
        out = _payload(mod.handler(scope="Roughing"))
        assert out["passed"] is False                   # one failing operation fails the scope
        assert cam.checked == [setup, ops[0], ops[1]]   # every operation asked, no short-circuit

    def test_a_folder_target_falls_back_to_the_operations_nested_in_it(self, wire):
        ops = [FakeOperation("D1"), FakeOperation("D2")]
        folder = FakeCAMFolder("Drilling", ops=ops)
        setup = FakeSetup("S1", ops=[FakeOperation("Face1")], folders=[folder])
        cam = self._wire_raising_target(wire, setup, folder)
        out = _payload(mod.handler(scope="Drilling"))
        assert cam.checked == [folder, ops[0], ops[1]]  # the sibling Face1 is out of scope
        assert out["checked"] == "per-operation fallback"

    def test_one_nested_operation_is_enough_to_fall_back(self, wire):
        # Boundary: exactly 1 child. The fallback needs a target NARROWER than the raising one, not
        # several of them.
        op = FakeOperation("Face1")
        setup = FakeSetup("Roughing", ops=[op])
        cam = self._wire_raising_target(wire, setup, setup, {"Face1": False})
        out = _payload(mod.handler(scope="Roughing"))
        assert cam.checked == [setup, op]
        assert out["passed"] is False and out["checked"] == "per-operation fallback"

    def test_a_setup_with_no_operations_answers_instead_of_raising(self, wire):
        # Boundary: 0 children. Nothing narrower exists to ask - and nothing needs asking: a setup
        # with no operations holds no toolpath that could be out of date, so the empty set IS the
        # answer. A mid-build shop template carries such setups, and a raise is not an answer.
        setup = FakeSetup("Empty")
        cam = self._wire_raising_target(wire, setup, setup)
        out = _payload(mod.handler(scope="Empty"))
        assert out["passed"] is True
        assert out["checked"] == "empty scope"
        assert out["measured"]["states"]["total"] == 0
        assert cam.checked == [setup]                   # nothing else was asked in its place
        assert "no operations counted" in out["note"] and "over an empty set" in out["note"]

    def test_a_scope_whose_only_operations_are_suppressed_still_reports_the_raise(self, wire):
        # The empty-scope answer keys on the RAW census, not the suppression filter: this setup
        # HOLDS an operation, so a raise on it is a raise - not an empty set to answer vacuously.
        setup = FakeSetup("Parked", ops=[FakeOperation("Off", operation_state=2, suppressed=True)])
        cam = self._wire_raising_target(wire, setup, setup)
        res = mod.handler(scope="Parked")
        assert res["isError"] is True
        assert "setup 'Parked'" in res["message"] and "3 : input is null" in res["message"]
        assert cam.checked == [setup]

    def test_an_operation_target_has_nothing_narrower_and_reports_the_raise(self, wire):
        op = FakeOperation("Drill1")
        setup = FakeSetup("S1", ops=[op, FakeOperation("Face1")])
        cam = self._wire_raising_target(wire, setup, op)
        res = mod.handler(scope="Drill1")
        assert res["isError"] is True
        assert "operation 'Drill1'" in res["message"] and "3 : input is null" in res["message"]
        assert cam.checked == [op]                      # a SIBLING is never checked in its place

    def test_both_the_target_and_the_fallback_raising_names_each(self, wire):
        cam = wire(_cam(FakeSetup("Roughing", ops=[FakeOperation("Face1")]),
                        raises=_INPUT_IS_NULL))         # every checkToolpath raises
        res = mod.handler(scope="Roughing")
        assert res["isError"] is True
        assert "setup 'Roughing'" in res["message"]
        assert "the per-operation fallback raised" in res["message"]
        assert res["message"].count("3 : input is null") == 2

    def test_a_non_boolean_from_the_fallback_is_refused_not_and_ed(self, wire):
        setup = FakeSetup("Roughing", ops=[FakeOperation("Face1")])
        self._wire_raising_target(wire, setup, setup, {"Face1": "true"})
        res = mod.handler(scope="Roughing")
        assert res["isError"] is True
        assert "not a true/false verdict" in res["message"] and "str" in res["message"]


# ── the shared resolver's refusals reach this entry point ───────────────────────────────────────────

class TestScopeRefusals:
    def test_unknown_scope_lists_the_available_names_and_checks_nothing(self, wire):
        cam = wire(_cam(FakeSetup("Roughing", ops=[FakeOperation("Face1")])))
        res = mod.handler(scope="Ghost")
        assert res["isError"] is True
        assert "Roughing" in res["message"] and "Face1" in res["message"]
        assert cam.checked == []

    def test_duplicate_name_is_refused_with_both_paths(self, wire):
        cam = wire(_cam(FakeSetup("Setup1", ops=[FakeOperation("Drill1")]),
                        FakeSetup("Setup2", ops=[FakeOperation("Drill1")])))
        res = mod.handler(scope="Drill1")
        assert res["isError"] is True and "ambiguous" in res["message"]
        assert "Setup1 / Drill1" in res["message"] and "Setup2 / Drill1" in res["message"]
        assert cam.checked == []                           # no verdict taken on a guessed target


# ── verdict + breakdown composition ─────────────────────────────────────────────────────────────────

class TestVerdictAndBreakdown:
    def test_true_verdict_carries_the_tally_and_no_rows(self, wire):
        wire(_cam(FakeSetup("S1", ops=[FakeOperation("Face1"), FakeOperation("Face2")]),
                  verdict=True))
        out = _payload(mod.handler())
        assert out["relation"] == "toolpaths_valid"
        assert out["passed"] is True
        assert out["measured"]["states"] == {"valid": 2, "out_of_date": 0, "no_toolpath": 0,
                                             "error": 0, "suppressed": 0, "generating": 0,
                                             "unread": 0, "total": 2}
        assert out["measured"]["not_valid"] == []
        assert "the validity check passed; 0 of 2 counted op(s) outside valid" in out["note"]

    def test_an_operation_whose_state_did_not_read_is_named_not_counted_valid(self, wire):
        # a state that RAISED counted as valid tells an agent the toolpath is up to date off a read
        # that never happened - the row and the note both have to say so instead.
        wire(_cam(FakeSetup("S1", ops=[FakeOperation("Face1"),
                                       FakeOperation("Ghost", state_readable=False)]),
                  verdict=True))
        out = _payload(mod.handler())
        assert out["measured"]["states"]["unread"] == 1
        assert out["measured"]["states"]["valid"] == 1
        assert out["measured"]["not_valid"] == [{"operation": "Ghost", "state": "unread"}]
        # the bucket has its own home in states + the row above; the note counts it OUTSIDE valid
        assert "1 of 2 counted op(s) outside valid" in out["note"]

    def test_false_verdict_names_every_operation_outside_the_valid_state(self, wire):
        ops = [FakeOperation("Face1"),
               FakeOperation("Bore", operation_state=1),
               FakeOperation("Slot", operation_state=3),
               FakeOperation("Chamfer", operation_state=2, suppressed=True),
               FakeOperation("Drill", has_error=True, error="Tool is not selected.\nsecond line")]
        wire(_cam(FakeSetup("S1", ops=ops), verdict=False))
        out = _payload(mod.handler(include_suppressed=True))
        assert out["passed"] is False
        assert out["measured"]["not_valid"] == [
            {"operation": "Bore", "state": "out_of_date"},
            {"operation": "Slot", "state": "no_toolpath"},
            {"operation": "Chamfer", "state": "suppressed"},
            {"operation": "Drill", "state": "error", "error": "Tool is not selected."},
        ]
        assert out["measured"]["states"] == {"valid": 1, "out_of_date": 1, "no_toolpath": 1,
                                             "error": 1, "suppressed": 1, "generating": 0,
                                             "unread": 0, "total": 5}
        assert out["measured"]["not_valid_truncated"] is False
        assert "cam_generate regenerates the out-of-date ops." in out["note"]

    def test_tally_and_rows_agree_on_one_vocabulary(self, wire):
        # the shared invariant: uncapped, every non-valid operation is a row, and each row's state
        # is a key of states - one classifier, so a bucket can never be counted two ways
        ops = [FakeOperation("Face1"),
               FakeOperation("Bore", operation_state=1),
               FakeOperation("Chamfer", operation_state=2, suppressed=True)]
        wire(_cam(FakeSetup("S1", ops=ops), verdict=False))
        states = _payload(mod.handler(include_suppressed=True))["measured"]["states"]
        rows = _payload(mod.handler(include_suppressed=True))["measured"]["not_valid"]
        assert len(rows) == states["total"] - states["valid"] == 2
        assert all(r["state"] in states for r in rows)
        assert sum(v for k, v in states.items() if k != "total") == states["total"]

    def test_a_non_operation_in_the_walk_is_skipped(self, wire, monkeypatch):
        import adsk.cam
        # Operation.cast is the gate: a node that does not cast to an Operation is neither tallied
        # nor given a row
        monkeypatch.setattr(adsk.cam.Operation, "cast",
                            staticmethod(lambda x: None if x.name == "NotAnOp" else x))
        wire(_cam(FakeSetup("S1", ops=[FakeOperation("Face1"),
                                       FakeOperation("NotAnOp", operation_state=1)]),
                  verdict=True))
        out = _payload(mod.handler())
        assert out["measured"]["states"]["total"] == 1
        assert out["measured"]["not_valid"] == []

    def test_empty_scope_reports_nothing_to_check(self, wire):
        wire(_cam(FakeSetup("S1"), verdict=True))
        out = _payload(mod.handler())
        assert out["measured"]["states"]["total"] == 0
        assert "no operations counted" in out["note"]


# ── the verdict and the per-operation reads are independent - the note must say so ──────────────────

class TestDisagreement:
    def test_false_verdict_with_every_operation_valid_states_both_facts(self, wire):
        wire(_cam(FakeSetup("S1", ops=[FakeOperation("Face1"), FakeOperation("Face2")]),
                  verdict=False))
        out = _payload(mod.handler())
        assert out["passed"] is False
        assert out["measured"]["not_valid"] == []
        # the two numbers are stated side by side, so the disagreement is READ off them; the
        # workspace state they were read in rides as its own key
        assert "the validity check failed; 0 of 2 counted op(s) outside valid" in out["note"]
        assert out["tolerance_used"]["validity_basis"]

    def test_true_verdict_with_a_suppressed_operation_does_not_claim_all_valid(self, wire):
        wire(_cam(FakeSetup("S1", ops=[FakeOperation("Face1"),
                                       FakeOperation("Chamfer", operation_state=2,
                                                     suppressed=True)]),
                  verdict=True))
        out = _payload(mod.handler(include_suppressed=True))
        assert out["passed"] is True
        assert out["measured"]["not_valid"] == [{"operation": "Chamfer", "state": "suppressed"}]
        assert "the validity check passed; 1 of 2 counted op(s) outside valid" in out["note"]
        assert out["tolerance_used"]["validity_basis"]

    def test_a_finished_operation_under_a_raised_flag_counts_valid(self, wire):
        # MEASURED across one regeneration: isGenerating stayed true for 1.1 s AFTER the Future
        # completed, over an operation reading state 0 with its toolpath. Bucketing that as
        # 'generating' reported 7 of 12 finished operations as unfinished work on a live sweep.
        op = FakeOperation("Face1", operation_state=0)      # the shared fake carries a toolpath
        op.isGenerating = True
        wire(_cam(FakeSetup("S1", ops=[op]), verdict=False))
        out = _payload(mod.handler())
        assert out["measured"]["states"] == {"valid": 1, "out_of_date": 0, "no_toolpath": 0,
                                             "error": 0, "suppressed": 0, "generating": 0,
                                             "unread": 0, "total": 1}
        assert out["measured"]["not_valid"] == []

    def test_an_operation_with_work_left_under_the_flag_still_counts_generating(self, wire):
        # the boundary: state 1 is work in flight, and the flag is what it says it is there - one
        # bucket either way, so the tally and the rows cannot disagree about one operation.
        op = FakeOperation("Face1", operation_state=1)
        op.isGenerating = True
        wire(_cam(FakeSetup("S1", ops=[op]), verdict=False))
        out = _payload(mod.handler())
        assert out["measured"]["states"]["generating"] == 1
        assert out["measured"]["not_valid"] == [{"operation": "Face1", "state": "generating"}]

    def test_scoped_verdict_is_the_one_reported(self, wire):
        # the document is dirty overall; the named setup's own verdict is what a scoped call returns
        wire(_cam(FakeSetup("Roughing", ops=[FakeOperation("Face1")]),
                  verdict=False, scoped_verdict=True))
        out = _payload(mod.handler(scope="Roughing"))
        assert out["passed"] is True


# ── the row cap ─────────────────────────────────────────────────────────────────────────────────────

class TestRowCap:
    def _stale_document(self, count):
        return FakeSetup("S1", ops=[FakeOperation(f"Op{i}", operation_state=1)
                                    for i in range(count)])

    def test_rows_are_capped_and_flagged_while_the_tally_stays_whole(self, wire):
        wire(_cam(self._stale_document(5), verdict=False))
        out = _payload(mod.handler(max_results=2))
        assert [r["operation"] for r in out["measured"]["not_valid"]] == ["Op0", "Op1"]
        assert out["measured"]["not_valid_truncated"] is True
        assert out["measured"]["states"]["out_of_date"] == 5      # the tally is never capped
        assert "not_valid capped at 2 - raise max_results." in out["note"]

    def test_under_the_cap_nothing_is_flagged(self, wire):
        wire(_cam(self._stale_document(3), verdict=False))
        out = _payload(mod.handler(max_results=25))
        assert len(out["measured"]["not_valid"]) == 3
        assert out["measured"]["not_valid_truncated"] is False
        assert "capped at" not in out["note"]

    def test_max_results_cannot_lift_the_ceiling(self, wire):
        # Every row crosses the wire, so the request is CLAMPED - a caller asking for 10000 rows
        # still gets at most _ROWS_MAX, and the truncation is flagged rather than silently obeyed.
        wire(_cam(self._stale_document(mod._ROWS_MAX + 5), verdict=False))
        out = _payload(mod.handler(max_results=10000))
        assert len(out["measured"]["not_valid"]) == mod._ROWS_MAX
        assert out["measured"]["not_valid_truncated"] is True
        assert out["measured"]["states"]["out_of_date"] == mod._ROWS_MAX + 5   # the tally is whole

    def test_a_non_numeric_max_results_falls_back_to_the_default(self, wire):
        # max_results arrives off the wire; a bare int() on it RAISES instead of answering.
        wire(_cam(self._stale_document(30), verdict=False))
        out = _payload(mod.handler(max_results="lots"))
        assert len(out["measured"]["not_valid"]) == mod._ROWS_CAP

    def test_a_negative_max_results_still_returns_one_row(self, wire):
        wire(_cam(self._stale_document(3), verdict=False))
        out = _payload(mod.handler(max_results=-5))
        assert len(out["measured"]["not_valid"]) == 1


# ── the ACTIVE-operations default (suppressed excluded, and named) ──────────────────────────────────

class TestSuppressedScoping:
    """A suppressed operation is left out of the post, so it is left out of the count. The number
    excluded is named on the payload, the note says what the verdict counted, and
    include_suppressed=true counts them."""

    def _template(self):
        # a mid-build shop template: two active operations, one of them stale, beside three PARKED
        # (suppressed) ones - the shape whose parked operations swamped the verdict's evidence.
        return FakeSetup("Template", ops=[
            FakeOperation("Face1"),
            FakeOperation("Bore", operation_state=1),
            FakeOperation("Park1", operation_state=2, suppressed=True),
            FakeOperation("Park2", operation_state=2, suppressed=True),
            FakeOperation("Park3", operation_state=2, suppressed=True),
        ])

    def test_the_default_counts_active_operations_and_names_what_it_excluded(self, wire):
        wire(_cam(self._template(), verdict=False))
        out = _payload(mod.handler())
        assert out["measured"]["states"] == {"valid": 1, "out_of_date": 1, "no_toolpath": 0,
                                             "error": 0, "suppressed": 0, "generating": 0,
                                             "unread": 0, "total": 2}
        assert out["measured"]["not_valid"] == [{"operation": "Bore", "state": "out_of_date"}]
        assert out["measured"]["suppressed_excluded"] == 3
        assert out["tolerance_used"]["tally_counts"] == "active_operations"

    def test_a_parked_op_whose_suppression_flag_reads_false_is_still_excluded(self, wire):
        # _split_suppressed reads op_primary_state, which answers suppression off the isSuppressed
        # flag OR operationState Suppressed (2). An op carrying the state without the flag is a
        # PARKED op here too - counting it among the active ones would put a parked op into the
        # verdict's evidence.
        wire(_cam(FakeSetup("Template", ops=[
            FakeOperation("Face1"),
            FakeOperation("Off", operation_state=2, suppressed=False)]), verdict=False))
        out = _payload(mod.handler())
        assert out["measured"]["states"]["total"] == 1
        assert out["measured"]["suppressed_excluded"] == 1
        assert out["measured"]["not_valid"] == []

    def test_include_suppressed_restores_the_whole_census(self, wire):
        wire(_cam(self._template(), verdict=False))
        out = _payload(mod.handler(include_suppressed=True))
        assert out["measured"]["states"]["suppressed"] == 3
        assert out["measured"]["states"]["total"] == 5
        assert out["measured"]["suppressed_excluded"] == 0      # nothing was left out to report
        assert out["tolerance_used"]["tally_counts"] == "all_operations"
        assert [r["operation"] for r in out["measured"]["not_valid"]] == ["Bore", "Park1", "Park2",
                                                                         "Park3"]

    def test_the_note_states_what_the_tally_left_out(self, wire):
        wire(_cam(self._template(), verdict=False))
        out = _payload(mod.handler())
        assert "3 suppressed op(s) excluded from the tally" in out["note"]
        assert out["measured"]["suppressed_excluded"] == 3

    def test_the_measured_suppression_fact_rides_the_flag_that_counts_them(self):
        # WHY those three are left out, measured: setting isSuppressed True flips hasToolpath True
        # -> False and clearing the flag leaves it False, so a suppressed operation carries no
        # toolpath until it is regenerated. It sits on the input an agent meets at the moment it
        # decides whether to count them - a slim that drops the measured clause goes red here.
        desc = mod.tool.to_dict()["inputSchema"]["properties"]["include_suppressed"]["description"]
        assert "flips hasToolpath to False" in desc
        assert "only cam_generate brings it back" in desc

    def test_the_note_says_nothing_about_exclusions_when_suppressed_are_counted(self, wire):
        wire(_cam(self._template(), verdict=False))
        note = _payload(mod.handler(include_suppressed=True))["note"]
        assert "4 of 5 counted op(s) outside valid" in note  # the parked three are counted now
        assert "suppressed" not in note                      # nothing was excluded to report

    def test_a_scope_holding_no_suppressed_operations_carries_no_clause(self, wire):
        # the clause is CONDITIONAL: riding every read would make the payload it describes
        # indistinguishable from the ones that excluded nothing.
        wire(_cam(FakeSetup("S1", ops=[FakeOperation("Face1")]), verdict=True))
        out = _payload(mod.handler())
        assert out["measured"]["suppressed_excluded"] == 0
        assert "suppressed" not in out["note"]

    def test_the_flag_flips_the_fallback_verdict_itself(self, wire):
        # the VERDICT, not just the tally: the per-operation fallback ANDs the operations that were
        # counted, so a parked operation answering false cannot fail an otherwise-clean active job.
        setup = FakeSetup("Template", ops=[FakeOperation("Face1"),
                                           FakeOperation("Park1", operation_state=2,
                                                         suppressed=True)])
        cam = wire(_cam(setup))

        def check(obj):
            cam.checked.append(obj)
            if obj is setup:
                raise _INPUT_IS_NULL
            return obj.name != "Park1"

        cam.checkToolpath = check
        assert _payload(mod.handler(scope="Template"))["passed"] is True
        assert _payload(mod.handler(scope="Template", include_suppressed=True))["passed"] is False

    def test_the_verdict_is_declared_wider_than_the_tally_on_cams_own_check(self, wire):
        # MEASURED on 2705.1.4: CAM.checkToolpath answers False for a setup whose only non-valid
        # operation is suppressed. So 'passed' is NOT the active-operation verdict the tally
        # describes, and the payload must not let the two be read as one number.
        wire(_cam(self._template(), verdict=False))
        out = _payload(mod.handler())
        assert out["tolerance_used"]["tally_counts"] == "active_operations"
        assert out["tolerance_used"]["verdict_counts"] == "all_operations"
        assert "3 suppressed op(s) excluded from the tally, counted by 'passed'." in out["note"]

    def test_the_fallback_verdict_is_declared_as_narrow_as_the_tally(self, wire):
        # the ONE path where the split really does control the verdict: this tool's own AND. Here
        # verdict_counts follows the tally and the disclosure sentence must NOT appear.
        setup = FakeSetup("Template", ops=[FakeOperation("Face1"),
                                           FakeOperation("Park1", operation_state=2,
                                                         suppressed=True)])
        cam = wire(_cam(setup))

        def check(obj):
            cam.checked.append(obj)
            if obj is setup:
                raise _INPUT_IS_NULL
            return True

        cam.checkToolpath = check
        out = _payload(mod.handler(scope="Template"))
        assert out["checked"] == "per-operation fallback"
        assert out["tolerance_used"]["verdict_counts"] == "active_operations"
        assert "counted by 'passed'" not in out["note"]

    def test_no_verdict_scope_sentence_when_nothing_was_excluded(self, wire):
        # the sentence exists to separate two DIFFERENT sets; with no suppressed operations there
        # is no difference to disclose and it would be noise.
        wire(_cam(FakeSetup("S1", ops=[FakeOperation("Face1")]), verdict=False))
        out = _payload(mod.handler())
        assert out["tolerance_used"]["verdict_counts"] == "all_operations"
        assert "counted by 'passed'" not in out["note"]

    def test_no_verdict_scope_sentence_when_suppressed_are_counted(self, wire):
        wire(_cam(self._template(), verdict=False))
        assert "counted by 'passed'" not in _payload(
            mod.handler(include_suppressed=True))["note"]

    def test_a_scope_of_only_suppressed_operations_counts_zero_and_says_so(self, wire):
        wire(_cam(FakeSetup("Parked", ops=[FakeOperation("Off", operation_state=2,
                                                         suppressed=True)]), verdict=True))
        out = _payload(mod.handler(scope="Parked"))
        assert out["measured"]["states"]["total"] == 0
        assert out["measured"]["suppressed_excluded"] == 1
        assert "no operations counted" in out["note"]


# ── an empty setup is a reportable fact, not an exception ───────────────────────────────────────────

class TestEmptySetups:
    """CAM.checkToolpath RAISES "3 : The operations are not CAM objects" on a setup holding zero
    operations - measured on 2705.1.4 against a populated setup in the same call, and against the
    same setup after one operation was added to it. ONE empty setup is enough to leave a whole
    document with no answer while it is asked, and a mid-build shop template carries empty setups.
    An empty setup holds no toolpath that could be out of date, so excluding it from the per-setup
    AND skips nothing."""

    def test_an_empty_setup_no_longer_blocks_the_document_answer(self, wire):
        populated = FakeSetup("Roughing", ops=[FakeOperation("Face1")])
        empty = FakeSetup("Blank")
        cam = wire(_cam(populated, empty, all_raises=_NOT_CAM_OBJECTS))

        def check(obj):
            cam.checked.append(obj)
            if obj is empty:
                raise _INPUT_IS_NULL
            return True

        cam.checkToolpath = check
        out = _payload(mod.handler())
        assert out["passed"] is True
        assert cam.checked == ["all", populated]        # the empty setup is never asked at all
        assert out["measured"]["empty_setups_excluded"] == 1
        assert out["measured"]["empty_setups"] == ["Blank"]   # the names ride the key, not the note

    def test_the_populated_setups_still_decide_the_verdict(self, wire):
        # setup_verdicts has no entry for the empty setup, so asking it would KeyError into the
        # fallback's error - the verdict below is proof only the populated setup was consulted.
        populated = FakeSetup("Roughing", ops=[FakeOperation("Bore", operation_state=1)])
        wire(_cam(populated, FakeSetup("Blank"), all_raises=_NOT_CAM_OBJECTS,
                  setup_verdicts={"Roughing": False}))
        out = _payload(mod.handler())
        assert out["passed"] is False
        assert out["measured"]["states"]["out_of_date"] == 1
        assert out["measured"]["empty_setups_excluded"] == 1

    def test_every_setup_empty_counts_nothing_and_says_so(self, wire):
        # Boundary: N empty setups, none populated. The AND over an empty set is what `all` answers.
        cam = wire(_cam(FakeSetup("A"), FakeSetup("B"), all_raises=_NOT_CAM_OBJECTS,
                        raises=_INPUT_IS_NULL))
        out = _payload(mod.handler())
        assert out["passed"] is True
        assert cam.checked == ["all"]                   # no setup was asked
        assert out["measured"]["states"]["total"] == 0
        assert out["measured"]["empty_setups_excluded"] == 2
        assert "no operations counted" in out["note"]

    def test_the_named_setups_are_capped_while_the_count_stays_exact(self, wire):
        empties = [FakeSetup(f"E{i}") for i in range(mod._EMPTY_NAMES_CAP + 2)]
        wire(_cam(*empties, all_raises=_NOT_CAM_OBJECTS, raises=_INPUT_IS_NULL))
        out = _payload(mod.handler())
        named = out["measured"]["empty_setups"]
        assert out["measured"]["empty_setups_excluded"] == mod._EMPTY_NAMES_CAP + 2
        assert len([s for s in empties if s.name in named]) == mod._EMPTY_NAMES_CAP
        # the overflow is MARKED, never silently dropped - a bare list past a silent cap reads as
        # the whole set, which is the defect the count alone does not close.
        assert named[-1] == "... and 2 more"

    def test_exactly_at_the_cap_adds_no_overflow_marker(self, wire):
        # the boundary: N == cap lists every name and must NOT claim a remainder.
        empties = [FakeSetup(f"E{i}") for i in range(mod._EMPTY_NAMES_CAP)]
        wire(_cam(*empties, all_raises=_NOT_CAM_OBJECTS, raises=_INPUT_IS_NULL))
        out = _payload(mod.handler())
        named = out["measured"]["empty_setups"]
        assert out["measured"]["empty_setups_excluded"] == mod._EMPTY_NAMES_CAP
        assert len([s for s in empties if s.name in named]) == mod._EMPTY_NAMES_CAP
        assert not [n for n in named if "more" in n]

    def test_a_document_check_that_answers_excludes_nothing(self, wire):
        # the per-setup fallback is what excludes empty setups; when checkAllToolpaths answers,
        # nothing was excluded and neither the payload nor the note may imply otherwise.
        wire(_cam(FakeSetup("Roughing", ops=[FakeOperation("Face1")]), FakeSetup("Blank"),
                  verdict=True))
        out = _payload(mod.handler())
        assert out["checked"] == "checkAllToolpaths"
        assert out["measured"]["empty_setups_excluded"] == 0
        assert out["measured"]["empty_setups"] == []


class TestTheComposedNoteFitsTheWireBudget:
    """The note is assembled at run time from up to six pieces, so test_prose_budget measures none
    of the compositions - and every piece it does not carry has a payload key that does."""

    def _worst(self, wire, scope_name):
        # every clause at once: a long scope label, suppressed operations the tally dropped while
        # CAM's own check kept them, empty toolpaths inside states['valid'], a capped row list, an
        # unverified basis and operations left to generate.
        ops = [FakeOperation("Empty1", has_toolpath=False),
               FakeOperation("Empty2", has_toolpath=False)]
        ops += [FakeOperation(f"Stale{i}", operation_state=1) for i in range(12)]
        ops += [FakeOperation(f"Park{i}", operation_state=2, suppressed=True) for i in range(3)]
        wire(_cam(FakeSetup(scope_name, ops=ops), verdict=False, scoped_verdict=False))
        return _payload(mod.handler(scope=scope_name, max_results=1))

    def test_every_clause_at_once_fits_the_wire_budget(self, wire):
        out = self._worst(wire, "Op 2 - Finishing WCS1 Vise Left")
        note = out["note"]
        assert "3 suppressed op(s) excluded from the tally, counted by 'passed'." in note
        assert "2 counted op(s) read valid and cut nothing" in note
        assert "not_valid capped at 1" in note and "Enter Manufacture" in note
        assert "cam_generate regenerates" in note
        assert len(note) <= 400, len(note)          # test_prose_budget.NOTE_BUDGET_CHARS

    def test_the_clauses_it_does_not_carry_are_keys_that_do(self, wire):
        # the cut sentences are not lost facts: each names a key still on the payload.
        out = self._worst(wire, "Roughing")
        assert out["checked"] == "checkToolpath"                     # which path answered
        assert out["measured"]["states"]["unread"] == 0              # the unread bucket
        assert out["measured"]["empty_toolpaths"] == ["Empty1", "Empty2"]
        assert out["measured"]["empty_setups"] == []
        assert out["tolerance_used"]["verdict_counts"] == "all_operations"
        assert out["tolerance_used"]["validity_basis"] == "unverified_design_workspace"


# ── guards ──────────────────────────────────────────────────────────────────────────────────────────

class TestGuards:
    def test_no_active_document_surfaces_the_shared_cam_gate(self, monkeypatch):
        # the real _cam_common.get_cam decides this - the tool must report its reason, not its own
        monkeypatch.setattr(cc, "app", SimpleNamespace(activeDocument=None))
        res = mod.handler()
        assert res["isError"] is True
        assert res["message"] == "No active document."

    def test_missing_cam_product_reason_is_passed_through(self, monkeypatch):
        monkeypatch.setattr(mod, "get_cam", lambda: (None, "This document has no CAM data."))
        res = mod.handler()
        assert res["isError"] is True
        assert res["message"] == "This document has no CAM data."

    def test_non_boolean_verdict_is_refused_rather_than_coerced(self, wire):
        cam = wire(_cam(FakeSetup("S1", ops=[FakeOperation("Face1")])))
        cam.checkAllToolpaths = lambda: "true"
        res = mod.handler()
        assert res["isError"] is True
        assert "not a true/false verdict" in res["message"] and "str" in res["message"]

    def test_a_raising_check_is_reported_with_its_scope(self, wire):
        wire(_cam(FakeSetup("Roughing", ops=[FakeOperation("Face1")]),
                  raises=RuntimeError("3 : invalid argument")))
        res = mod.handler(scope="Roughing")
        assert res["isError"] is True
        assert "setup 'Roughing'" in res["message"] and "3 : invalid argument" in res["message"]


# ── the Manufacture-workspace trust gate ────────────────────────────────────────────────────────────

class TestValidityBasis:
    def test_outside_manufacture_the_verdict_carries_its_caveat(self, wire):
        wire(_cam(FakeSetup("S1", ops=[FakeOperation("Face1")])))
        out = _payload(mod.handler())
        assert out["tolerance_used"] == {"criterion": "valid_and_up_to_date",
                                         "tally_counts": "active_operations",
                                         "verdict_counts": "all_operations",
                                         "validity_basis": "unverified_design_workspace"}
        assert "Enter Manufacture to trust op validity." in out["note"]

    def test_inside_manufacture_the_caveat_drops(self, wire, monkeypatch):
        wire(_cam(FakeSetup("S1", ops=[FakeOperation("Face1")])))
        monkeypatch.setattr(mod, "validity_basis", lambda: "manufacture_verified")
        out = _payload(mod.handler())
        assert out["tolerance_used"]["validity_basis"] == "manufacture_verified"
        assert "Enter Manufacture" not in out["note"]


# ── the EMPTY overlay on states['valid'] - generated, up to date, and cutting nothing ────────────────

class TestEmptyToolpathOverlay:
    """An operation whose toolpath generated EMPTY reads hasToolpath True and state IsValid, so
    CAM's own check passes it and the tally buckets it valid - both correctly, since it IS generated
    and up to date. Only its machining time says it cuts nothing (0.0 s against 4.193 s for the same
    operation once it really cut), and without the overlay the payload closes the question on it."""

    def _job(self, times, **kw):
        setup = FakeSetup("S1", ops=[FakeOperation("Swarf1", has_warning=True,
                                                   warning="No passes to link."),
                                     FakeOperation("Cut")])
        return _cam(setup, machining_times=times, **kw)

    def test_the_empty_operation_is_counted_and_named(self, wire):
        wire(self._job({"Swarf1": 0.0, "Cut": 4.193083}))
        measured = _payload(mod.handler())["measured"]
        assert measured["empty_toolpath_count"] == 1
        assert measured["empty_toolpaths"] == ["Swarf1"]
        # the tally is unchanged: the overlay never moves an operation out of its bucket
        assert measured["states"]["valid"] == 2 and measured["not_valid"] == []

    def test_the_note_names_it_beside_a_passing_verdict(self, wire):
        wire(self._job({"Swarf1": 0.0, "Cut": 4.193083}))
        out = _payload(mod.handler())
        assert out["passed"] is True
        # the count and the pointer are the note's job; the NAMES have their own capped key
        assert "1 counted op(s) read valid and cut nothing (measured.empty_toolpaths)" in out["note"]
        assert "Swarf1" not in out["note"]

    def test_a_job_that_cuts_names_nothing_empty(self, wire):
        wire(self._job({"Swarf1": 9.66, "Cut": 4.193083}))
        out = _payload(mod.handler())
        assert out["measured"]["empty_toolpath_count"] == 0
        assert out["measured"]["empty_toolpaths"] == []
        assert "cut nothing" not in out["note"]

    def test_an_unread_time_names_nothing(self, wire):
        # every getMachiningTime raises: an unread signal is not a measurement of zero
        wire(self._job({}))
        assert _payload(mod.handler())["measured"]["empty_toolpath_count"] == 0

    def test_the_flags_shape_is_counted_by_the_same_overlay(self, wire):
        setup = FakeSetup("S1", ops=[FakeOperation("NoPath", has_toolpath=False),
                                     FakeOperation("Cut")])
        wire(_cam(setup, machining_times={"Cut": 4.193083}))
        measured = _payload(mod.handler())["measured"]
        assert measured["empty_toolpath_count"] == 1 and measured["empty_toolpaths"] == ["NoPath"]

    def test_two_operations_of_one_name_are_told_apart_by_position(self, wire):
        # an operation name is unique only within a setup, and these arrive with no walk beside
        # them - so a repeated name is replaced by the position it holds in this walk
        cam = _cam(FakeSetup("S1", ops=[FakeOperation("Bore", has_toolpath=False)]),
                   FakeSetup("S2", ops=[FakeOperation("Bore", has_toolpath=False)]))
        wire(cam)
        assert _payload(mod.handler())["measured"]["empty_toolpaths"] == [
            "Bore (operation 1)", "Bore (operation 2)"]

    def test_the_position_counts_over_the_WALK_not_over_the_empty_rows(self, wire):
        # a CUTTING operation before the namesakes and another between them: the number is the
        # operation's place in the walk, so numbering the empty rows 1..n instead would print
        # 'operation 1'/'operation 2' and address the wrong operations
        cam = _cam(FakeSetup("S1", ops=[FakeOperation("Cut"),
                                        FakeOperation("Bore", has_toolpath=False),
                                        FakeOperation("Face"),
                                        FakeOperation("Bore", has_toolpath=False)]),
                   machining_times={"Cut": 4.193083, "Face": 9.66})
        wire(cam)
        measured = _payload(mod.handler())["measured"]
        assert measured["empty_toolpaths"] == ["Bore (operation 2)", "Bore (operation 4)"]
        assert measured["empty_toolpath_count"] == 2

    def test_the_name_list_is_capped_while_the_count_stays_exact(self, wire):
        n = mod._EMPTY_OP_NAMES_CAP + 3
        setup = FakeSetup("S1", ops=[FakeOperation(f"Empty{i}", has_toolpath=False)
                                     for i in range(n)])
        wire(_cam(setup))
        measured = _payload(mod.handler())["measured"]
        assert measured["empty_toolpath_count"] == n
        assert len(measured["empty_toolpaths"]) == mod._EMPTY_OP_NAMES_CAP
        # the COUNT is what says the list is partial - the note carries the exact one
        assert f"{n} counted op(s) read valid and cut nothing" in _payload(mod.handler())["note"]

    def test_a_suppressed_operation_is_never_named_empty(self, wire):
        # a parked operation carries no toolpath by design, and it is out of the tally entirely
        setup = FakeSetup("S1", ops=[FakeOperation("Parked", has_toolpath=False, valid=False,
                                                   suppressed=True, operation_state=2),
                                     FakeOperation("Cut")])
        wire(_cam(setup, machining_times={"Cut": 4.193083}))
        measured = _payload(mod.handler())["measured"]
        assert measured["empty_toolpath_count"] == 0
        assert measured["suppressed_excluded"] == 1
