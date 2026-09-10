"""Unit tests for ``_cam_common.py`` (the shared CAM substrate) and ``_cam_read.py`` (the slice
handlers behind cam_get, which reach the substrate through it). ``cc`` is the substrate, ``cr`` the
read cores.

Covers the bounded-read caps (CLAUDE.md "Bound it"): setups_handler's top-level 'truncated'
and per-setup 'model_lists_truncated', operations_handler's per-setup 'operations_truncated', and
get_setup_references_handler's per-setup 'references_truncated' - plus the third incompleteness
those flags carry, a model/fixture/stock PROPERTY that raises instead of reading a collection (the
list goes null and 'model_lists_unreadable' names it, so it never reads as "nothing selected").
Also covers the pure Tier-1 logic:
``_invalidation_reasons`` (parsing op.messageLog into categorical reasons / parameter-change count /
machine-changed flag), ``op_primary_state`` (the one-bucket-per-op priority order), ``_hms`` (seconds
-> h:m:s), and the machining-time estimate's feed_scale/rapid_feed/tool_change constants.

Plus the shared CAM tree walk + resolvers - walk_cam_tree / resolve_cam_node / operations_under and
the find_setup / find_operation / setup_names wrappers. This is the ONE traversal + by-name
resolution every CAM tool shares: case-insensitive EXACT, a miss lists the available names, and a
DUPLICATED name is REFUSED naming each hit's setup path (operation names legitimately collide
across setups).
"""

import json
import math
from types import SimpleNamespace

import adsk.cam
import pytest

from conftest import (_FakeObjectCollection, _NamedCollection, _Strategy, _make_object_collection,
                      load_tool, make_cam, make_occurrence, strategy_factory, wcs_params)
from conftest import (FakeApplication, FakeCAMParameter, FakeCAMParameters, FakeDataFile,
                      FakeDocumentReference, FakeMachine, FakeMatrix3D, FakeSetup, FakeCAMFolder,
                      FakeOperation, FakeTool)

cc = load_tool("_cam_common")
cr = load_tool("_cam_read")


def _payload(result):
    assert result["isError"] is False, result
    return json.loads(result["content"][0]["text"])


def FakeCAM(setups, machining_times=None):
    """A CAM product carrying `setups`, with the shared machining-time answer."""
    return make_cam(*setups, machining_times=machining_times)


@pytest.fixture
def install(monkeypatch):
    """Wire a fake CAM product into BOTH get_cam seams - _cam_common's own and the name _cam_read
    imported from it, which its handlers call - so one fixture serves a test in either
    module; patches undo themselves."""
    def _install(cam):
        monkeypatch.setattr(cc, "get_cam", lambda: (cam, None))
        monkeypatch.setattr(cr, "get_cam", lambda: (cam, None))
        return cam
    return _install


@pytest.fixture
def operation_cast_passthrough(monkeypatch):
    # cam.Operation.cast is a SHARED mock other test modules also wire; pin it to a pass-through
    # for this test only (monkeypatch restores it) rather than relying on session-wide state.
    import adsk.cam
    monkeypatch.setattr(adsk.cam.Operation, "cast", lambda x: x)


@pytest.fixture
def occurrence_cast_passthrough(monkeypatch):
    # pass-through: treat every fake as an Occurrence, restored after the test.
    import adsk.fusion
    monkeypatch.setattr(adsk.fusion.Occurrence, "cast", lambda x: x)


# ── get_cam_setups_handler: top-level 'truncated' (the setups walk itself) ───────────────────────

class TestSetupsCap:
    def test_under_cap_untruncated_and_unchanged(self, install):
        install(FakeCAM([object() for _ in range(3)]))
        out = _payload(cr.get_cam_setups_handler())
        assert out["truncated"] is False
        assert len(out["setups"]) == 3
        assert out["setup_count"] == 3

    def test_at_cap_truncates_and_flags(self, install):
        install(FakeCAM([object() for _ in range(cr._MAX_ITEMS + 5)]))
        out = _payload(cr.get_cam_setups_handler())
        assert out["truncated"] is True
        assert len(out["setups"]) == cr._MAX_ITEMS


# ── get_cam_setups_handler: per-setup 'model_lists_truncated' (selected_models/fixtures/stock) ────

class _ModelStub:
    """One entry of a setup's model/fixture/stock list, read for its name alone. The lists hold
    Occurrences AND BRepBodies, so no one shared fake stands for the row."""
    def __init__(self, name):
        self.name = name


class _Setup(FakeSetup):
    """A setup carrying the three model collections the setups and references slices read - the
    shared setup fake models the operation tree, not the selection lists."""
    def __init__(self, models=(), fixtures=(), stock=(), name="Setup1"):
        super().__init__(name)
        self.models = list(models)
        self.fixtures = list(fixtures)
        self.stockSolids = list(stock)


class TestModelListsCap:
    def test_under_cap_untruncated(self, install):
        s = _Setup(models=[_ModelStub("A"), _ModelStub("B")])
        install(FakeCAM([s]))
        out = _payload(cr.get_cam_setups_handler())
        rec = out["setups"][0]
        assert rec["model_lists_truncated"] is False
        assert rec["selected_models"] == ["A", "B"]

    def test_at_cap_truncates_and_flags(self, install):
        many = [_ModelStub(f"M{i}") for i in range(cr._MAX_ITEMS + 3)]
        s = _Setup(models=many)
        install(FakeCAM([s]))
        out = _payload(cr.get_cam_setups_handler())
        rec = out["setups"][0]
        assert rec["model_lists_truncated"] is True
        assert len(rec["selected_models"]) == cr._MAX_ITEMS


class _RaisingSetup(FakeSetup):
    """A setup whose named model collections RAISE on the PROPERTY read - measured: Setup.models
    raises "3 : input is null" on a setup whose selected occurrence was removed by
    doc_insert_occurrence(remove_existing=...)."""

    def __init__(self, raising=("models",), models=(), fixtures=(), stock=(), name="Setup1"):
        super().__init__(name)
        self._raising = set(raising)
        self._lists = {"models": list(models), "fixtures": list(fixtures),
                       "stockSolids": list(stock)}

    def _read(self, key):
        if key in self._raising:
            raise RuntimeError("3 : input is null")
        return self._lists[key]

    @property
    def models(self):
        return self._read("models")

    @property
    def fixtures(self):
        return self._read("fixtures")

    @property
    def stockSolids(self):
        return self._read("stockSolids")


class _StockSetup(FakeSetup):
    """A setup carrying the two reads that say HOW it machines: the stock it comes from, and the
    matrix its own +Z is read off."""
    def __init__(self, stock_mode=None, matrix=None, name="Setup1"):
        super().__init__(name)
        if stock_mode is not None:
            self.stockMode = stock_mode
        if matrix is not None:
            self.workCoordinateSystem = matrix


class TestStockModeAndFrame:
    """The setups slice answers what a second setup machines FROM and which way its tool comes -
    the pair a mill-turn job is sequenced by."""

    def test_previous_setup_stock_reads_back_by_name(self, install):
        install(FakeCAM([_StockSetup(
            stock_mode=adsk.cam.SetupStockModes.PreviousSetupStock)]))
        rec = _payload(cr.get_cam_setups_handler())["setups"][0]
        assert rec["stock_mode"] == "previous_setup"

    def test_a_setup_whose_mode_does_not_read_publishes_null(self, install):
        install(FakeCAM([_StockSetup()]))
        rec = _payload(cr.get_cam_setups_handler())["setups"][0]
        assert rec["stock_mode"] is None

    def test_a_flipped_frame_publishes_its_own_z(self, install):
        # 180 deg about X is the turning frame the hub is chucked in: +Z runs along world -Z, the
        # end a top milling setup does NOT come at the part from.
        flipped = FakeMatrix3D()
        flipped._r = ((1.0, 0.0, 0.0), (0.0, -1.0, 0.0), (0.0, 0.0, -1.0))
        install(FakeCAM([_StockSetup(matrix=flipped)]))
        rec = _payload(cr.get_cam_setups_handler())["setups"][0]
        assert rec["wcs"]["z_world"] == [0.0, 0.0, -1.0]

    def test_a_setup_with_no_matrix_carries_no_z_key(self, install):
        install(FakeCAM([_StockSetup()]))
        rec = _payload(cr.get_cam_setups_handler())["setups"][0]
        assert "z_world" not in (rec["wcs"] or {})


class TestUnreadableModelLists:
    """A model collection whose PROPERTY raises is published as null, never as []. The two answers
    are different facts - "this setup selected nothing" vs "the collection could not be read" - and
    safe(read, []) makes them indistinguishable."""

    def test_a_raising_models_property_publishes_null_not_an_empty_list(self, install):
        install(FakeCAM([_RaisingSetup(raising=("models",))]))
        rec = _payload(cr.get_cam_setups_handler())["setups"][0]
        assert rec["selected_models"] is None
        assert rec["model_lists_truncated"] is True
        assert rec["model_lists_unreadable"] == ["selected_models"]

    def test_a_readable_empty_collection_stays_an_empty_list(self, install):
        # The discriminating pair: an empty read is an ANSWER, so it keeps [] , stays untruncated,
        # and carries no unreadable marker.
        install(FakeCAM([_RaisingSetup(raising=())]))
        rec = _payload(cr.get_cam_setups_handler())["setups"][0]
        assert rec["selected_models"] == [] and rec["fixtures"] == [] and rec["stock_solids"] == []
        assert rec["model_lists_truncated"] is False
        assert "model_lists_unreadable" not in rec

    def test_only_the_raising_list_goes_null(self, install):
        # One unreadable collection must not blank the two that DID read.
        install(FakeCAM([_RaisingSetup(raising=("fixtures",), models=[_ModelStub("A")],
                                       stock=[_ModelStub("Stock1")])]))
        rec = _payload(cr.get_cam_setups_handler())["setups"][0]
        assert rec["selected_models"] == ["A"] and rec["stock_solids"] == ["Stock1"]
        assert rec["fixtures"] is None
        assert rec["model_lists_unreadable"] == ["fixtures"]

    def test_every_raising_list_is_named(self, install):
        install(FakeCAM([_RaisingSetup(raising=("models", "fixtures", "stockSolids"))]))
        rec = _payload(cr.get_cam_setups_handler())["setups"][0]
        assert rec["model_lists_unreadable"] == ["selected_models", "fixtures", "stock_solids"]
        assert (rec["selected_models"], rec["fixtures"], rec["stock_solids"]) == (None, None, None)

    def test_the_references_slice_flags_the_same_raise_as_incomplete(self, install,
                                                                     occurrence_cast_passthrough):
        # The references walk reads the SAME three properties; a raise there leaves an empty
        # reference list that must not read as "this setup references nothing".
        setup = _RaisingSetup(raising=("models",))
        setup.name = "S1"
        install(FakeCAM([setup]))
        rec = _payload(cr.get_setup_references_handler())["setups"][0]
        assert rec["references"] == []
        assert rec["references_truncated"] is True

    def test_the_references_slice_stays_untruncated_when_the_read_succeeds(self, install,
                                                                           occurrence_cast_passthrough):
        setup = _RaisingSetup(raising=())
        setup.name = "S1"
        install(FakeCAM([setup]))
        rec = _payload(cr.get_setup_references_handler())["setups"][0]
        assert rec["references"] == [] and rec["references_truncated"] is False


# ── get_cam_operations_handler: per-setup 'operations_truncated' ─────────────────────────────────

def _OpSetup(name, ops):
    """A setup holding `ops` - what the operations slice walks."""
    return FakeSetup(name, ops=ops)


class TestOperationsCap:
    def test_under_cap_untruncated(self, install, operation_cast_passthrough):
        s = _OpSetup("S1", [object() for _ in range(3)])
        install(FakeCAM([s]))
        out = _payload(cr.get_cam_operations_handler())
        rec = out["setups"][0]
        assert rec["operations_truncated"] is False
        assert len(rec["operations"]) == 3

    def test_at_cap_truncates_and_flags(self, install, operation_cast_passthrough):
        s = _OpSetup("S1", [object() for _ in range(cr._MAX_ITEMS + 7)])
        install(FakeCAM([s]))
        out = _payload(cr.get_cam_operations_handler())
        rec = out["setups"][0]
        assert rec["operations_truncated"] is True
        assert len(rec["operations"]) == cr._MAX_ITEMS


class TestOperationsFilterNamedBranch:
    """The `setup=` filter resolves through the shared resolver: a duplicated setup name is
    REFUSED (naming each candidate), never silently narrowed to the first matching setup."""

    def test_duplicate_setup_name_is_refused(self, install, operation_cast_passthrough):
        install(FakeCAM([_OpSetup("Dup", [object()]), _OpSetup("Dup", [object(), object()])]))
        res = cr.get_cam_operations_handler(setup="Dup")
        assert res["isError"] is True and "ambiguous" in res["message"].lower()
        assert res["message"].count("Dup") >= 3          # the input + both candidates named

    def test_named_setup_miss_lists_available(self, install, operation_cast_passthrough):
        install(FakeCAM([_OpSetup("S1", [])]))
        res = cr.get_cam_operations_handler(setup="Ghost")
        assert res["isError"] is True
        assert "Ghost" in res["message"] and "S1" in res["message"]


class TestOperationSummaryStateNaming:
    def test_operation_state_1_is_named_out_of_date(self, install, operation_cast_passthrough):
        # the op row's state comes from op_primary_state, so it speaks that one vocabulary.
        op = SimpleNamespace(name="Op1", tool=None, strategy="adaptive", operationState=1,
                             hasWarning=False, hasError=False, hasToolpath=True,
                             isToolpathValid=False, isGenerating=False, isSuppressed=False,
                             isOptional=False, messageLog="")
        s = _OpSetup("S1", [op])
        install(FakeCAM([s]))
        out = _payload(cr.get_cam_operations_handler())
        assert out["setups"][0]["operations"][0]["state"] == "out_of_date"

    def test_a_state_that_did_not_read_is_unread_and_blocks(self, install,
                                                            operation_cast_passthrough):
        # the row an agent reads before posting: 'valid' here would publish a lifecycle nothing
        # answered, and the blocker is what keeps the summary's exception list honest about it.
        install(FakeCAM([FakeSetup("S1", ops=[_unread_state_op()])]))
        out = _payload(cr.get_cam_operations_handler())
        row = out["setups"][0]["operations"][0]
        assert row["state"] == "unread"
        assert "state_unread" in row["blocked_by"]
        # the remedy has to match the CAUSE: a state that raised is not stale work, so the row
        # points at another READ, never at cam_generate - and the note says so too.
        assert row["requires"] == {"tool": "cam_get", "workspace": "Manufacture"}
        assert "'unread'" in out["note"] and "not cam_generate" in out["note"]
        assert out["setups"][0]["summary"]["states"] == {"unread": 1}
        assert [e["name"] for e in out["setups"][0]["summary"]["exceptions"]] == ["Contour1"]


# ── the two op-state aggregations agree by construction ─────────────────────────────────────────
# hasError must outrank operationState in every rollup: a state derived from operationState alone
# would bucket an errored op by its state while the per-setup op_states rollup puts it in "error" -
# one response contradicting itself. MEASURED (measure_api row cam-errored-op-state-pair): an
# errored op reads operationState 3 (NoToolpath) with hasError=True; OperationStates carries no
# error member. The state-0 pairing below is a deliberate STRESS case - the platform has not been
# observed producing it - so the classification is proven to hold even where state would read
# "valid". Both surfaces derive through op_primary_state.

class TestErroredOpNeverReadsValid:
    def _cam(self):
        """Three healthy ops plus an errored one pinned at operationState 0: the stress case where
        state alone would read valid, so hasError must win (measured errored ops read state 3)."""
        errored = FakeOperation("Drill1", has_toolpath=False, valid=False, operation_state=0,
                                has_error=True, error="Toolpath is empty")
        assert errored.operationState == 0        # the stress pairing this class exists to cover
        return FakeCAM([FakeSetup("Setup1", ops=[
            FakeOperation("Face1"), FakeOperation("Adaptive1"), FakeOperation("Contour1"),
            errored])])

    def test_the_errored_row_reads_error_not_valid(self, install, operation_cast_passthrough):
        install(self._cam())
        rows = _payload(cr.get_cam_operations_handler())["setups"][0]["operations"]
        drill = next(r for r in rows if r["name"] == "Drill1")
        assert drill["state"] == "error"
        assert drill["has_toolpath"] is False and drill["has_error"] is True
        assert [r["state"] for r in rows if r["has_error"]] == ["error"]

    def test_both_aggregations_report_the_same_buckets(self, install,
                                                        operation_cast_passthrough):
        install(self._cam())
        op_states = _payload(cr.get_cam_setups_handler())["setups"][0]["op_states"]
        summary = _payload(cr.get_cam_operations_handler())["setups"][0]["summary"]
        assert op_states == {"valid": 3, "error": 1}
        assert summary["states"] == op_states           # no response contradicts itself
        drill = next(e for e in summary["exceptions"] if e["name"] == "Drill1")
        assert "operation_error" in drill["blocked_by"]

    def test_a_generating_op_reads_generating_in_both(self, install, operation_cast_passthrough):
        # the priority order is the row's too: generating outranks the stale operationState, so the
        # summary cannot bucket an op as out_of_date while op_states calls it generating.
        op = SimpleNamespace(name="Adaptive1", tool=None, strategy="adaptive", operationState=1,
                             hasWarning=False, hasError=False, hasToolpath=True,
                             isToolpathValid=False, isGenerating=True, isSuppressed=False,
                             isOptional=False, messageLog="")
        install(FakeCAM([FakeSetup("Setup1", ops=[op])]))
        assert _payload(cr.get_cam_setups_handler())["setups"][0]["op_states"] == {"generating": 1}
        summary = _payload(cr.get_cam_operations_handler())["setups"][0]["summary"]
        assert summary["states"] == {"generating": 1}


# ── get_setup_references_handler: per-setup 'references_truncated' ──────────────────────────────

def _ref_occ(name):
    """An occurrence placing an EXTERNAL component whose source link does not read - the row the
    references walk counts but cannot name a file for."""
    return make_occurrence(path=name, referenced=True)


class _RefSetup(_Setup):
    """The same model-carrying setup, addressed by NAME - what the references filter resolves over."""
    def __init__(self, name, models=()):
        super().__init__(models=models, name=name)


class TestReferencesFilterNamedBranch:
    def test_duplicate_setup_name_is_refused(self, install, occurrence_cast_passthrough):
        # same contract as the operations filter: named -> shared resolver -> duplicate REFUSED.
        install(FakeCAM([_RefSetup("Dup"), _RefSetup("Dup")]))
        res = cr.get_setup_references_handler(setup="Dup")
        assert res["isError"] is True and "ambiguous" in res["message"].lower()
        assert res["message"].count("Dup") >= 3

    def test_named_setup_miss_lists_available(self, install, occurrence_cast_passthrough):
        install(FakeCAM([_RefSetup("S1")]))
        res = cr.get_setup_references_handler(setup="Ghost")
        assert res["isError"] is True
        assert "Ghost" in res["message"] and "S1" in res["message"]


class TestReferencesCap:
    def test_under_cap_untruncated(self, install, occurrence_cast_passthrough):
        s = _RefSetup("S1", models=[_ref_occ("A"), _ref_occ("B")])
        install(FakeCAM([s]))
        out = _payload(cr.get_setup_references_handler())
        rec = out["setups"][0]
        assert rec["references_truncated"] is False
        assert len(rec["references"]) == 2

    def test_at_cap_truncates_and_flags(self, install, occurrence_cast_passthrough):
        many = [_ref_occ(f"O{i}") for i in range(cr._MAX_ITEMS + 4)]
        s = _RefSetup("S1", models=many)
        install(FakeCAM([s]))
        out = _payload(cr.get_setup_references_handler())
        rec = out["setups"][0]
        assert rec["references_truncated"] is True
        assert len(rec["references"]) == cr._MAX_ITEMS


# ── _invalidation_reasons: the messageLog regex - category vs per-parameter-delta split ──────────

class TestInvalidationReasons:
    def test_design_changed_line_is_a_categorical_reason(self):
        op = SimpleNamespace(messageLog="2024-01-01T00:00:00 I Invalidated: Design changed: WCS origin")
        reasons, param_changes, machine_changed = cr._invalidation_reasons(op)
        assert reasons == ["Design changed: WCS origin"]
        assert param_changes == 0
        assert machine_changed is False

    def test_parameter_delta_line_is_counted_not_added_as_a_reason(self):
        op = SimpleNamespace(
            messageLog="2024-01-01 I Op1 used a different value for parameter 'tool_feedCutting' before")
        reasons, param_changes, machine_changed = cr._invalidation_reasons(op)
        assert reasons == []
        assert param_changes == 1
        assert machine_changed is False

    def test_machine_changed_line_sets_the_flag_not_a_reason(self):
        op = SimpleNamespace(messageLog="2024-01-01 I External changed: machine.limits")
        reasons, param_changes, machine_changed = cr._invalidation_reasons(op)
        assert reasons == []
        assert param_changes == 0
        assert machine_changed is True

    def test_noncategorical_invalidated_line_is_dropped(self):
        # "Invalidated: <x>" where <x> doesn't start with a known category prefix - not surfaced,
        # not counted as a parameter change either.
        op = SimpleNamespace(messageLog="2024-01-01 I Invalidated: Something obscure happened")
        reasons, param_changes, machine_changed = cr._invalidation_reasons(op)
        assert reasons == [] and param_changes == 0 and machine_changed is False

    def test_duplicate_reasons_are_deduped(self):
        log = "\n".join(["2024-01-01 I Invalidated: Design changed: WCS origin"] * 3)
        op = SimpleNamespace(messageLog=log)
        reasons, _, _ = cr._invalidation_reasons(op)
        assert reasons == ["Design changed: WCS origin"]

    def test_reasons_are_capped(self):
        lines = [f"2024-01-01 I Invalidated: Design changed: item {i}"
                 for i in range(cr._INVAL_REASON_CAP + 3)]
        op = SimpleNamespace(messageLog="\n".join(lines))
        reasons, _, _ = cr._invalidation_reasons(op)
        assert len(reasons) == cr._INVAL_REASON_CAP

    def test_blank_message_log_yields_nothing(self):
        op = SimpleNamespace(messageLog="")
        reasons, param_changes, machine_changed = cr._invalidation_reasons(op)
        assert reasons == [] and param_changes == 0 and machine_changed is False


# ── op_primary_state: one bucket per op, priority-ordered ────────────────────────────────────────
# op_primary_state classifies from the op_state_facts dict (the same raw facts op_state_tally
# shares) rather than a live op, so each test builds the raw op then reads it through op_state_facts
# first - exercising the two functions exactly as every real caller composes them.

class _RaisingFlagOp(FakeOperation):
    """An operation whose toolpath flags RAISE - the read that must answer None, not False."""

    _raising = ()
    _values = {"hasToolpath": True, "isToolpathValid": True}

    def __init__(self, name="Op", raising=("hasToolpath", "isToolpathValid")):
        super().__init__(name, operation_state=adsk.cam.OperationStates.IsValidOperationState)
        self._raising = set(raising)

    def _read(self, name):
        if name in self._raising:
            raise RuntimeError(f"{name} cannot be read on this operation")
        return self._values[name]

    @property
    def hasToolpath(self):
        return self._read("hasToolpath")

    @hasToolpath.setter
    def hasToolpath(self, value):
        pass

    @property
    def isToolpathValid(self):
        return self._read("isToolpathValid")

    @isToolpathValid.setter
    def isToolpathValid(self, value):
        pass


class TestOpStateFactsToolpathFlags:
    """The toolpath pair is what tells an EMPTY op from one that never generated, so an unreadable
    flag has to answer None - safe(read, False) would publish a state nothing observed."""

    def test_a_raising_toolpath_flag_reads_none_not_false(self):
        facts = cc.op_state_facts(_RaisingFlagOp())
        assert facts["has_toolpath"] is None and facts["is_toolpath_valid"] is None

    def test_one_raising_flag_leaves_the_other_readable(self):
        facts = cc.op_state_facts(_RaisingFlagOp(raising=("hasToolpath",)))
        assert facts["has_toolpath"] is None and facts["is_toolpath_valid"] is True

    def test_readable_flags_read_as_themselves(self):
        facts = cc.op_state_facts(_RaisingFlagOp(raising=()))
        assert facts["has_toolpath"] is True and facts["is_toolpath_valid"] is True


class TestIsEmptyToolpath:
    """The EMPTY class needs the bucket AND the raw state: not suppressed / not errored / not
    generating (op_primary_state), operationState reading IsValid, isToolpathValid True, hasToolpath
    False. Every other combination - including an unreadable flag or an unreadable state - is not
    empty, because 'this op cut nothing' is a claim about what was read."""

    def _facts(self, **over):
        base = {"name": "Op", "has_error": False, "has_warning": False, "is_suppressed": False,
                "is_generating": False,
                "operation_state": adsk.cam.OperationStates.IsValidOperationState,
                "generating_progress": None, "has_toolpath": False, "is_toolpath_valid": True}
        base.update(over)
        return base

    def test_generated_with_no_toolpath_is_empty(self):
        assert cc.is_empty_toolpath(self._facts()) is True

    def test_an_unreadable_has_toolpath_is_not_empty(self):
        # None means the flag did not answer; publishing EMPTY off it invents the state
        assert cc.is_empty_toolpath(self._facts(has_toolpath=None)) is False

    def test_an_unreadable_toolpath_valid_is_not_empty(self):
        assert cc.is_empty_toolpath(self._facts(is_toolpath_valid=None)) is False

    def test_a_never_generated_op_is_not_empty(self):
        # isToolpathValid False + hasToolpath False = nothing has been computed yet, not "cuts
        # nothing" - the discriminator that separates the empty class from the ungenerated one
        assert cc.is_empty_toolpath(self._facts(is_toolpath_valid=False)) is False

    def test_an_op_holding_a_toolpath_is_not_empty(self):
        assert cc.is_empty_toolpath(self._facts(has_toolpath=True)) is False

    def test_a_suppressed_op_is_not_empty(self):
        assert cc.is_empty_toolpath(self._facts(
            is_suppressed=True,
            operation_state=adsk.cam.OperationStates.SuppressedOperationState)) is False

    def test_an_errored_op_is_not_empty(self):
        assert cc.is_empty_toolpath(self._facts(has_error=True)) is False

    def test_a_generating_op_is_not_empty(self):
        assert cc.is_empty_toolpath(self._facts(is_generating=True)) is False

    def test_an_out_of_date_op_is_not_empty(self):
        assert cc.is_empty_toolpath(self._facts(
            operation_state=adsk.cam.OperationStates.IsInvalidOperationState)) is False

    def test_a_no_toolpath_state_is_not_empty(self):
        # the far side of the IsValid boundary: NoToolpath (3) is the never-generated state
        assert cc.is_empty_toolpath(self._facts(
            operation_state=adsk.cam.OperationStates.NoToolpathOperationState)) is False

    def test_a_suppressed_state_whose_flag_did_not_read_is_not_empty(self):
        # the shape where only the STATE half answers suppression - isSuppressed raised and
        # op_state_facts coerced it to False. Suppressed (2) is not the IsValid the empty claim
        # rests on.
        assert cc.is_empty_toolpath(self._facts(
            operation_state=adsk.cam.OperationStates.SuppressedOperationState)) is False

    def test_the_suppressed_state_buckets_suppressed_and_refuses_the_empty_class(self):
        # the distinction the two predicates draw over ONE facts dict, with the empty class's own
        # toolpath pair riding on it: Suppressed (2) is a PARKED op, never 'generated and cut
        # nothing'. Reading 2 as the empty/failed class would answer both the wrong way round.
        facts = self._facts(
            operation_state=adsk.cam.OperationStates.SuppressedOperationState)
        assert cc.op_primary_state(facts) == "suppressed"
        assert cc.is_empty_toolpath(facts) is False

    def test_an_unreadable_operation_state_is_not_empty(self):
        # None is the state that RAISED: it buckets 'unread', and publishing EMPTY off it would
        # state a lifecycle nothing read
        assert cc.is_empty_toolpath(self._facts(operation_state=None)) is False


class TestIsEmptyToolpathTimeShape:
    """The second measured shape of the EMPTY class: an operation whose toolpath generated EMPTY
    reads hasToolpath TRUE, so the flags read it as a clean valid and only its own machining time
    separates it from one that cut - 0.0 s for the empty toolpath, 4.193 s for the same operation
    once it really cut."""

    def _facts(self, **over):
        base = {"name": "Swarf1", "has_error": False, "has_warning": True, "is_suppressed": False,
                "is_generating": False,
                "operation_state": adsk.cam.OperationStates.IsValidOperationState,
                "generating_progress": None, "has_toolpath": True, "is_toolpath_valid": True,
                "machining_time": 0.0}
        base.update(over)
        return base

    def test_a_generated_toolpath_reading_zero_seconds_is_empty(self):
        assert cc.is_empty_toolpath(self._facts()) is True

    def test_an_operation_that_cut_is_not_empty(self):
        assert cc.is_empty_toolpath(self._facts(machining_time=4.193083)) is False

    def test_the_zero_boundary_bites_on_the_smallest_time_above_it(self):
        # the ON/OFF pair of the only comparison here: a time is a measurement, so anything the
        # read answered ABOVE zero leaves the operation out of the class
        assert cc.is_empty_toolpath(self._facts(machining_time=0.000001)) is False
        assert cc.is_empty_toolpath(self._facts(machining_time=0.0)) is True

    def test_a_negative_time_is_not_empty(self):
        # a negative figure is no measurement of nothing - only an answered 0.0 is
        assert cc.is_empty_toolpath(self._facts(machining_time=-0.000001)) is False

    def test_an_unread_time_is_not_empty(self):
        # the read raised (or answered a non-number): None never equals 0.0, so the operation keeps
        # the clean-valid reading rather than gaining a state nothing measured
        assert cc.is_empty_toolpath(self._facts(machining_time=None)) is False

    def test_facts_carrying_no_time_key_at_all_are_not_empty(self):
        # the shape a caller that never asked for the signal hands over (op_state_facts with no cam)
        facts = self._facts()
        del facts["machining_time"]
        assert cc.is_empty_toolpath(facts) is False

    def test_the_time_shape_still_needs_the_state_to_have_answered_is_valid(self):
        assert cc.is_empty_toolpath(self._facts(
            operation_state=adsk.cam.OperationStates.IsInvalidOperationState)) is False
        assert cc.is_empty_toolpath(self._facts(operation_state=None)) is False

    def test_the_time_shape_still_needs_toolpath_valid(self):
        assert cc.is_empty_toolpath(self._facts(is_toolpath_valid=False)) is False
        assert cc.is_empty_toolpath(self._facts(is_toolpath_valid=None)) is False

    def test_a_suppressed_operation_reading_zero_is_not_empty(self):
        assert cc.is_empty_toolpath(self._facts(is_suppressed=True)) is False

    def test_an_errored_operation_reading_zero_is_not_empty(self):
        assert cc.is_empty_toolpath(self._facts(has_error=True)) is False

    def test_an_unreadable_toolpath_flag_reading_zero_is_not_empty(self):
        # hasToolpath None answers neither shape: the flags arm needs False, this arm needs True
        assert cc.is_empty_toolpath(self._facts(has_toolpath=None)) is False

    def test_the_flags_shape_needs_no_time_at_all(self):
        # the two arms are disjoint on hasToolpath, so the older shape still answers with the time
        # unread - which is what keeps every caller that does not pay for the signal working
        assert cc.is_empty_toolpath(
            self._facts(has_toolpath=False, machining_time=None)) is True

    def test_a_manual_nc_operation_is_never_empty(self):
        # a Manual NC op emits canned G-code and carries no toolpath by construction, reading state
        # IsValid (0) with hasToolpath False - the flags' empty shape. Calling it 'cuts nothing'
        # describes a pass-through operation as a failed one.
        facts = self._facts(strategy="manual", has_toolpath=False, machining_time=None)
        assert cc.is_empty_toolpath(facts) is False
        # the same flags WITHOUT the manual strategy are the empty class - so the strategy is what
        # this test moves, not the shape around it
        assert cc.is_empty_toolpath(dict(facts, strategy="contour2d")) is True

    def test_a_manual_nc_operation_reading_zero_seconds_is_also_excluded(self):
        assert cc.is_empty_toolpath(self._facts(strategy="manual")) is False

    def test_a_cutting_operation_carrying_a_warning_is_not_empty(self):
        # a warning is no signal either way: an operation that really cut carried 'Tool was
        # lifted.' beside 4.193 s, and warning text is what this predicate deliberately never reads
        assert cc.is_empty_toolpath(self._facts(has_warning=True, machining_time=4.193083)) is False


class TestOpStateFactsMachiningTime:
    """op_state_facts reads the time signal only where it can decide, and only when asked: it is
    the one fact there that costs a platform computation."""

    def _op(self, name="Swarf1", has_toolpath=True, state=0, **over):
        return FakeOperation(name, has_toolpath=has_toolpath, operation_state=state, **over)

    def test_without_a_cam_no_time_is_read(self):
        assert cc.op_state_facts(self._op())["machining_time"] is None

    def test_with_a_cam_a_generated_valid_op_carries_its_time(self):
        cam = make_cam(machining_times={"Swarf1": 4.193083})
        assert cc.op_state_facts(self._op(), cam)["machining_time"] == 4.193083

    def test_a_zero_time_reads_as_zero_not_as_unread(self):
        # the trap the whole signal turns on: a falsy 0.0 coerced to None would read as 'not
        # measured' and the empty operation would go on publishing as a clean valid
        cam = make_cam(machining_times={"Swarf1": 0.0})
        facts = cc.op_state_facts(self._op(), cam)
        assert facts["machining_time"] == 0.0
        assert cc.is_empty_toolpath(facts) is True

    def test_no_call_is_made_for_an_operation_holding_no_toolpath(self):
        # the flags already answer the empty question there, and the call RAISES on such an op
        cam = make_cam(machining_times={"Swarf1": 0.0})
        facts = cc.op_state_facts(self._op(has_toolpath=False), cam)
        assert facts["machining_time"] is None and cam.machining_time_calls == []
        assert cc.is_empty_toolpath(facts) is True      # the flags shape, unaffected

    def test_no_call_is_made_for_an_out_of_date_operation(self):
        cam = make_cam(machining_times={"Swarf1": 0.0})
        state = adsk.cam.OperationStates.IsInvalidOperationState
        assert cc.op_state_facts(self._op(state=state), cam)["machining_time"] is None
        assert cam.machining_time_calls == []

    def test_no_call_is_made_for_a_suppressed_operation(self):
        cam = make_cam(machining_times={"Swarf1": 0.0})
        state = adsk.cam.OperationStates.SuppressedOperationState
        cc.op_state_facts(self._op(state=state, suppressed=True), cam)
        assert cam.machining_time_calls == []

    def test_no_call_is_made_when_the_toolpath_flag_did_not_read(self):
        cam = make_cam(machining_times={"Op": 0.0})
        assert cc.op_state_facts(_RaisingFlagOp(), cam)["machining_time"] is None
        assert cam.machining_time_calls == []

    def test_no_call_is_made_when_the_state_did_not_read(self):
        cam = make_cam(machining_times={"Swarf1": 0.0})
        cc.op_state_facts(self._op(state=0, state_readable=False), cam)
        assert cam.machining_time_calls == []

    def test_a_raising_time_read_answers_none_rather_than_a_number(self):
        cam = make_cam(machining_times={})       # every operation raises
        facts = cc.op_state_facts(self._op(), cam)
        assert facts["machining_time"] is None and len(cam.machining_time_calls) == 1
        assert cc.is_empty_toolpath(facts) is False

    def test_the_strategy_is_read_onto_the_facts(self):
        # the member the Manual NC exclusion keys on - unread it is None, which is not 'manual'
        assert cc.op_state_facts(self._op(strategy="manual"))["strategy"] == "manual"
        assert cc.op_state_facts(SimpleNamespace(name="Op"))["strategy"] is None

    def test_a_generated_manual_nc_operation_is_not_named_empty(self):
        # end to end through the flags arm: state IsValid, hasToolpath False, no time - the shape
        # that reached the empty class before the strategy was read
        facts = cc.op_state_facts(self._op(strategy="manual", has_toolpath=False))
        assert facts["has_toolpath"] is False and facts["operation_state"] == 0
        assert cc.is_empty_toolpath(facts) is False

    def test_the_call_carries_the_pinned_knob_values(self):
        # ONE spelling of the three knobs, shared with the estimate slice
        cam = make_cam(machining_times={"Swarf1": 0.0})
        cc.op_state_facts(self._op(), cam)
        assert cam.machining_time_calls[0][1] == (100.0, 10.58, 1.5)


class _UnreadableSuppressionFlagOp(FakeOperation):
    """A warned operation whose isSuppressed read RAISES while operationState reads Suppressed.
    op_state_facts reads that flag through safe(read, False), so the facts it hands on carry
    is_suppressed False beside operation_state 2 - the one shape only the state half answers for.

    has_error=True is that shape with a fault beside it, where the bucket answers 'error' and the
    Suppressed state must not reach any other field on the row."""

    def __init__(self, has_error=False):
        super().__init__("Chamfer1", has_toolpath=False, valid=False,
                         operation_state=adsk.cam.OperationStates.SuppressedOperationState,
                         has_error=has_error, has_warning=True,
                         error=("Drive Surfaces: No valid drive surfaces selected."
                                if has_error else ""))

    @property
    def isSuppressed(self):
        raise RuntimeError("isSuppressed cannot be read on this operation")

    @isSuppressed.setter
    def isSuppressed(self, value):
        pass


class TestOpIsSuppressed:
    """The ONE suppression read every classifier and cam_get gate shares. Either half answers,
    because either can be the only one that reads."""

    def test_the_flag_half_answers_on_its_own(self):
        assert cc.op_is_suppressed({"is_suppressed": True}) is True

    def test_the_state_half_answers_on_its_own(self):
        assert cc.op_is_suppressed(
            {"is_suppressed": False,
             "operation_state": adsk.cam.OperationStates.SuppressedOperationState}) is True

    def test_neither_half_answering_is_not_suppression(self):
        assert cc.op_is_suppressed(
            {"is_suppressed": False,
             "operation_state": adsk.cam.OperationStates.IsValidOperationState}) is False

    @pytest.mark.parametrize("state", [adsk.cam.OperationStates.IsInvalidOperationState,
                                       adsk.cam.OperationStates.NoToolpathOperationState])
    def test_the_neighbouring_states_are_not_suppression(self, state):
        # the exact boundary of the == comparison: IsInvalid (1) and NoToolpath (3) sit either side
        # of Suppressed (2) and are ordinary lifecycle states
        assert cc.op_is_suppressed({"is_suppressed": False, "operation_state": state}) is False

    def test_a_dict_carrying_neither_key_answers_false(self):
        # read BY KEY: cam_get's per-op record reaches this with no raw state at all
        assert cc.op_is_suppressed({}) is False


class TestCountsAsWarningSuppressionHalves:
    """The overlay drops a SUPPRESSED op's warning, and it reads suppression TWO ways because the
    two shapes carrying these facts answer differently: cam_get's per-op record
    (_cam_read._operation_summary) publishes is_suppressed and no operation_state at all, while
    op_state_facts reads isSuppressed through safe(read, False), which lands an unreadable flag as
    False beside operation_state 2. Each half is pinned on the shape the other cannot answer for -
    a fake setting both together leaves either one deletable."""

    def _facts(self, **over):
        base = {"name": "Contour1", "has_error": False, "has_warning": True, "is_suppressed": False,
                "is_generating": False,
                "operation_state": adsk.cam.OperationStates.IsValidOperationState,
                "generating_progress": None, "has_toolpath": False, "is_toolpath_valid": True}
        base.update(over)
        return base

    def test_a_warned_unsuppressed_op_counts(self):
        assert cc.counts_as_warning(self._facts()) is True

    def test_the_flag_alone_drops_the_warning_where_no_operation_state_rides(self):
        facts = self._facts(is_suppressed=True)
        del facts["operation_state"]                 # the record shape carries no such key
        assert cc.counts_as_warning(facts) is False

    def test_the_flag_alone_drops_the_warning_whatever_state_rides_beside_it(self):
        # the two halves are OR'd, not coupled: the flag answers on its own, so a state reading
        # anything but Suppressed cannot carry a flagged op's warning back into the count.
        assert cc.counts_as_warning(self._facts(
            is_suppressed=True,
            operation_state=adsk.cam.OperationStates.IsInvalidOperationState)) is False

    def test_the_state_alone_drops_the_warning_where_the_flag_did_not_read(self):
        facts = cc.op_state_facts(_UnreadableSuppressionFlagOp())
        assert facts["is_suppressed"] is False       # the raise was coerced, not observed as False
        assert facts["operation_state"] == adsk.cam.OperationStates.SuppressedOperationState
        assert cc.counts_as_warning(facts) is False

    @pytest.mark.parametrize("state", [adsk.cam.OperationStates.IsInvalidOperationState,
                                       adsk.cam.OperationStates.NoToolpathOperationState])
    def test_only_the_suppressed_state_drops_the_warning_not_its_neighbours(self, state):
        # the exact boundary of the == comparison: IsInvalid (1) and NoToolpath (3) sit either side
        # of Suppressed (2) and are ordinary lifecycle buckets a warning still demotes.
        assert cc.counts_as_warning(self._facts(operation_state=state)) is True


def _unread_state_op(has_toolpath=False, is_toolpath_valid=False):
    """A warned operation whose operationState read RAISES while every other lifecycle member reads.
    op_state_facts reads that member through safe() with NO default, so the facts it hands on carry
    operation_state None. The toolpath pair is a parameter because the EMPTY-class combination
    (isToolpathValid True, hasToolpath False) rides on this same unread state."""
    return FakeOperation("Contour1", has_toolpath=has_toolpath, valid=is_toolpath_valid,
                         has_warning=True,
                         warning="no geometry is selected\nassign a machining boundary",
                         state_readable=False)


class TestOpStateFactsUnreadableState:
    """operationState is read with NO safe() default, because EVERY value it can answer IS a state
    (IsValid 0, IsInvalid 1, Suppressed 2, NoToolpath 3): any default publishes one of them off a
    read that never happened. None is what the classifiers below have to answer for."""

    def _facts(self):
        return cc.op_state_facts(_unread_state_op())

    def test_a_raising_state_reads_none_not_a_lifecycle_value(self):
        facts = self._facts()
        assert facts["operation_state"] is None
        assert facts["has_warning"] is True and facts["is_suppressed"] is False

    def test_the_warning_survives_a_state_that_did_not_read(self):
        # the overlay drops a warning only where suppression was OBSERVED - a state that never
        # answered is not suppression, and dropping the warning here buries a live fault
        assert cc.counts_as_warning(self._facts()) is True

    def test_the_op_is_not_bucketed_suppressed(self):
        # the negative is the whole claim: an op nothing read a state off is not a PARKED op
        assert cc.op_primary_state(self._facts()) != "suppressed"

    def test_the_empty_class_is_not_claimed_off_a_state_that_did_not_read(self):
        # 'generated and cut nothing' rests on IsValid having been OBSERVED: a state that never
        # answered buckets 'unread', so the toolpath pair alone cannot carry the claim.
        facts = cc.op_state_facts(_unread_state_op(has_toolpath=False, is_toolpath_valid=True))
        assert facts["operation_state"] is None
        assert cc.is_empty_toolpath(facts) is False

    def test_the_state_that_never_answered_is_its_own_bucket(self):
        # the bucket the whole row rests on: 'valid' is the answer to operationState IsValid, and a
        # read that raised must not borrow it.
        assert cc.op_primary_state(self._facts()) == "unread"

    def test_an_unread_op_stays_on_the_entitlement_blocked_list(self, monkeypatch):
        # entitlement_blocked_names skips the FINISHED buckets; 'unread' is not one, so a
        # generation-blocked op whose state never read is still named as work left to do.
        monkeypatch.setattr(cc, "strategy_generation_allowed", lambda name: False)
        assert cc.entitlement_blocked_names([_unread_state_op()]) == ["Contour1"]

    def test_the_tally_counts_the_warning_and_claims_no_lifecycle_bucket(
            self, operation_cast_passthrough):
        tally = cc.op_state_tally([_unread_state_op()])
        assert tally["total"] == 1 and tally["warnings"] == 1
        assert tally["warning_sample"] == {"name": "Contour1",
                                           "warning": "no geometry is selected"}
        assert (tally["valid"], tally["out_of_date"],
                tally["suppressed"], tally["errored"]) == (0, 0, 0, 0)


class TestOpPrimaryState:
    def _facts(self, **kw):
        base = dict(isSuppressed=False, hasError=False, isGenerating=False, operationState=0)
        base.update(kw)
        return cc.op_state_facts(SimpleNamespace(**base))

    def test_suppressed_outranks_error_generating_and_state(self):
        facts = self._facts(isSuppressed=True, hasError=True, isGenerating=True, operationState=1)
        assert cc.op_primary_state(facts) == "suppressed"

    def test_error_outranks_generating_and_state(self):
        facts = self._facts(hasError=True, isGenerating=True, operationState=3)
        assert cc.op_primary_state(facts) == "error"

    def test_generating_outranks_operation_state(self):
        facts = self._facts(isGenerating=True, operationState=3)
        assert cc.op_primary_state(facts) == "generating"

    def test_state_3_is_no_toolpath(self):
        assert cc.op_primary_state(self._facts(operationState=3)) == "no_toolpath"

    def test_state_1_is_out_of_date(self):
        assert cc.op_primary_state(self._facts(operationState=1)) == "out_of_date"

    def test_state_0_is_valid(self):
        assert cc.op_primary_state(self._facts(operationState=0)) == "valid"

    def test_state_2_is_suppressed(self):
        # Suppressed (2) sits between IsInvalid (1) and NoToolpath (3), and the two tests above pin
        # those neighbours: a comparison that widened either way would relabel one of them.
        assert cc.op_primary_state(self._facts(operationState=2)) == "suppressed"

    def test_the_state_half_answers_where_the_suppression_flag_did_not_read(self):
        # op_state_facts reads isSuppressed through safe(read, False), so a raising flag arrives as
        # False beside operationState Suppressed - the shape where the state is the only half left
        # to answer. Reading the flag alone publishes 'valid' for an op Fusion answered SUPPRESSED.
        facts = cc.op_state_facts(_UnreadableSuppressionFlagOp())
        assert facts["is_suppressed"] is False
        assert facts["operation_state"] == adsk.cam.OperationStates.SuppressedOperationState
        assert cc.op_primary_state(facts) == "suppressed"

    def test_an_errored_op_at_the_suppressed_state_stays_errored(self):
        # the state half is read AFTER the error and generating checks, so admitting it cannot
        # relabel a reported fault as a parked op
        assert cc.op_primary_state(self._facts(hasError=True, operationState=2)) == "error"

    def test_a_generating_op_at_the_suppressed_state_stays_generating(self):
        assert cc.op_primary_state(self._facts(isGenerating=True, operationState=2)) == "generating"

    def test_the_bucket_and_the_tally_answer_one_facts_dict_alike(
            self, operation_cast_passthrough):
        # the two rollups reading these facts must not disagree: op_state_tally already counts
        # state 2 as suppressed, so a bucket that called the same op valid would have cam_get's
        # per-setup op_states and cam_get_status's live_states describe one op two ways.
        op = _UnreadableSuppressionFlagOp()
        facts = cc.op_state_facts(op)
        tally = cc.op_state_tally([op])
        assert cc.op_primary_state(facts) == "suppressed"
        assert (tally["suppressed"], tally["valid"], tally["out_of_date"]) == (1, 0, 0)


# ── _operations_summary: readiness derives from the per-op error state it ships beside ──────────
# The summary must NOT read "ready to post" while an op carries has_error - a toolpath can read valid
# on an errored op (live: "4 of 4 valid, ready to post" while a Drill op had has_error). Derive the
# verdict from BOTH toolpath_valid AND has_error, never toolpath_valid alone.

class TestOperationsSummaryErrorGate:
    def _rec(self, name, **kw):
        base = {"name": name, "state": "valid", "toolpath_valid": True, "is_suppressed": False,
                "has_error": False, "blocked_by": []}
        base.update(kw)
        return base

    def test_errored_op_is_not_ready_to_post(self, monkeypatch):
        monkeypatch.setattr(cr, "validity_basis", lambda: "manufacture_verified")
        records = [self._rec("Face1"),
                   self._rec("Drill1", has_error=True)]   # toolpath reads valid but the op is errored
        summary = cr._operations_summary(records)
        assert "ready to post" not in summary["readiness"]
        drill = next(e for e in summary["exceptions"] if e["name"] == "Drill1")
        assert "operation_error" in drill["blocked_by"]

    def test_the_errored_op_is_not_counted_among_the_valid_toolpaths(self, monkeypatch):
        # The COUNT the verdict quotes, not just its wording. The errored op reads toolpath_valid
        # True, so a tally taken off that flag alone says "2 of 2 active ops have valid toolpaths"
        # in the very sentence that sends the reader to resolve an exception - the two halves of
        # one string disagreeing about the same job. Pinned as the whole sentence: the demoted
        # wording is identical either way, so only the number separates them.
        monkeypatch.setattr(cr, "validity_basis", lambda: "manufacture_verified")
        summary = cr._operations_summary([self._rec("Face1"),
                                          self._rec("Drill1", has_error=True)])
        assert summary["readiness"] == (
            "1 of 2 active ops have valid toolpaths - resolve the exceptions "
            "(cam_get(include=['operations']) has the error text) before posting.")
        assert summary["active_count"] == 2          # the errored op is still ACTIVE, just not valid

    def test_all_valid_no_errors_is_ready(self, monkeypatch):
        monkeypatch.setattr(cr, "validity_basis", lambda: "manufacture_verified")
        summary = cr._operations_summary([self._rec("Face1"), self._rec("Adaptive1")])
        # the whole sentence, not a substring: every demoted verdict CONTAINS "ready to post"
        # inside "'ready to post' is NOT established", so a substring check asserts nothing.
        assert summary["readiness"] == "2 of 2 active ops have valid toolpaths - ready to post."
        assert summary["exceptions"] == []


class TestReadinessRemedyIsWordedFromTheExceptionKinds:
    """The parenthetical names the remedy for the codes PRESENT: a scope whose only exception is a
    state that never read is not sent to cam_generate, which would regenerate nothing."""

    def _rec(self, name, **kw):
        base = {"name": name, "state": "valid", "toolpath_valid": True, "is_suppressed": False,
                "has_error": False, "blocked_by": []}
        base.update(kw)
        return base

    def _readiness(self, monkeypatch, records, setup_blocked=None):
        monkeypatch.setattr(cr, "validity_basis", lambda: "manufacture_verified")
        return cr._operations_summary(records, setup_blocked)["readiness"]

    def test_an_unread_state_alone_asks_for_a_re_read_not_a_generate(self, monkeypatch):
        # THE BITE: a generate over a scope whose state never READ regenerates nothing - the state
        # is unread because op validity is not trustworthy outside Manufacture.
        line = self._readiness(monkeypatch, [
            self._rec("Ghost", state="unread", toolpath_valid=False, blocked_by=["state_unread"])])
        assert "(re-read with cam_get in the Manufacture workspace)" in line
        assert "cam_generate" not in line

    def test_an_out_of_date_toolpath_still_asks_for_a_generate(self, monkeypatch):
        line = self._readiness(monkeypatch, [
            self._rec("Bore", state="out_of_date", toolpath_valid=False,
                      blocked_by=["toolpath_out_of_date"])])
        assert "(run cam_generate)" in line

    def test_mixed_kinds_name_every_remedy_once_in_first_seen_order(self, monkeypatch):
        line = self._readiness(monkeypatch, [
            self._rec("Bore", state="out_of_date", toolpath_valid=False,
                      blocked_by=["toolpath_out_of_date"]),
            self._rec("Slot", state="out_of_date", toolpath_valid=False,
                      blocked_by=["toolpath_out_of_date"]),
            self._rec("Ghost", state="unread", toolpath_valid=False,
                      blocked_by=["state_unread"])])
        assert ("(run cam_generate; re-read with cam_get in the Manufacture workspace)") in line

    def test_a_blocked_setup_names_the_tool_that_clears_it(self, monkeypatch):
        # the setup-level vocabulary has ONE home (_cam_common._SETUP_BLOCKER_REMEDY) and this
        # parenthetical reads it there rather than re-rolling the sentence.
        line = self._readiness(monkeypatch,
                               [self._rec("Ghost", state="unread", toolpath_valid=False,
                                          blocked_by=["state_unread"])],
                               [{"name": "S1", "blocked_by": ["no_machine_selected"]}])
        assert cc._SETUP_BLOCKER_REMEDY["no_machine_selected"] in line

    def test_a_code_with_no_known_remedy_adds_no_parenthetical(self, monkeypatch):
        # a remedy nobody measured is not invented: the sentence still names the exceptions.
        line = self._readiness(monkeypatch, [
            self._rec("Odd", toolpath_valid=False, blocked_by=["something_new"])])
        assert line == "0 of 1 active ops have valid toolpaths - resolve the exceptions before posting."


# ── _hms: seconds -> h:m:s ─────────────────────────────────────────────────────────────────────

class TestHms:
    def test_zero_seconds(self):
        assert cr._hms(0) == "0:00:00"

    def test_formats_hours_minutes_seconds(self):
        assert cr._hms(3661) == "1:01:01"

    def test_rounds_fractional_seconds(self):
        assert cr._hms(59.6) == "0:01:00"

    def test_non_numeric_input_falls_back_to_zero(self):
        assert cr._hms(None) == "0:00:00"


# ── machining-time estimate: pin the exact constants passed to getMachiningTime ──────────────────

class _MTResult:
    def __init__(self, seconds, feed_distance=0.0, rapid_distance=0.0, tool_changes=0):
        self.machiningTime = seconds
        self.totalFeedTime = 0.0
        self.totalRapidTime = 0.0
        self.toolChangeCount = tool_changes
        self.feedDistance = feed_distance      # CM, per MachiningTime
        self.rapidDistance = rapid_distance


def _MTOp(name="Op", valid=True, suppressed=False, has_toolpath=True):
    """One operation as the machining-time walk reads it."""
    return FakeOperation(
        name, has_toolpath=has_toolpath, valid=valid, suppressed=suppressed,
        operation_state=(adsk.cam.OperationStates.SuppressedOperationState if suppressed
                         else adsk.cam.OperationStates.IsValidOperationState))


def _MTSetup(name, has_valid_toolpath=True, ops=None):
    """A setup holding the operations the estimate times."""
    rows = ops if ops is not None else [_MTOp(name + " op", valid=has_valid_toolpath)]
    return FakeSetup(name, ops=rows)


def _MTCam(setups, seconds=120.0, per_op=60.0, per_op_by_name=None):
    """A CAM product whose getMachiningTime answers a full MachiningTime and RECORDS every call in
    `calls`. `per_op_by_name` gives a NAMED operation its own seconds - 0.0 is the EMPTY class's
    second shape, an operation that generated a toolpath and cuts nothing."""
    cam = make_cam(*setups)
    cam.calls = []
    by_name = dict(per_op_by_name or {})

    def get_machining_time(obj, feed_scale, rapid_feed, tool_change):
        cam.calls.append((obj, feed_scale, rapid_feed, tool_change))
        if isinstance(obj, FakeOperation):             # a per-OPERATION call
            return _MTResult(by_name.get(obj.name, per_op), feed_distance=100.0,
                             rapid_distance=25.0)
        return _MTResult(seconds, feed_distance=1000.0, rapid_distance=250.0, tool_changes=17)

    cam.getMachiningTime = get_machining_time
    return cam


def _aggregate_calls(cam):
    """Only the whole-collection calls - the per-operation ones are a different question."""
    return [c for c in cam.calls if not isinstance(c[0], FakeOperation)]


class _RefusingCollection(_FakeObjectCollection):
    """An ObjectCollection whose add() takes nothing - the False the handler counts."""

    def add(self, item):
        return False


@pytest.fixture
def object_collection(monkeypatch):
    """Hand the handler REAL collections (a Mock would accept everything silently); returns the
    list of collections it created, newest last."""
    import adsk.core
    made = []

    def _create():
        made.append(_make_object_collection())
        return made[-1]
    monkeypatch.setattr(adsk.core.ObjectCollection, "create", _create)
    return made


class TestMachiningTimeConstants:
    def test_feed_scale_is_100_percent_not_a_0_to_1_fraction(self, install, object_collection,
                                                             operation_cast_passthrough):
        # getMachiningTime's feedScale is a PERCENT (100 = full programmed feed); passing 1.0 would
        # mean 1% feed and inflate the estimate roughly 100x.
        cam = _MTCam([_MTSetup("S1")])
        install(cam)
        _payload(cr.get_machining_time_handler())
        assert cam.calls[0][1] == 100.0

    def test_rapid_feed_is_10_58_centimeters_per_second(self, install, object_collection,
                                                        operation_cast_passthrough):
        # getMachiningTime's rapidFeed is centimeters per SECOND, not cm/min - passing a cm/min
        # value (e.g. 1000) would understate rapids by roughly 60x.
        cam = _MTCam([_MTSetup("S1")])
        install(cam)
        _payload(cr.get_machining_time_handler())
        assert cam.calls[0][2] == 10.58

    def test_tool_change_time_is_1_5_seconds(self, install, object_collection,
                                             operation_cast_passthrough):
        cam = _MTCam([_MTSetup("S1")])
        install(cam)
        _payload(cr.get_machining_time_handler())
        assert cam.calls[0][3] == 1.5

    def test_total_seconds_sums_the_setup_aggregates_not_the_per_op_rows(
            self, install, object_collection, operation_cast_passthrough):
        # the total is the sum of the per-SETUP aggregates; the per-op figures (60.0 each here) are
        # a different, deliberately non-summing number and must never reach the total.
        cam = _MTCam([_MTSetup("S1"), _MTSetup("S2")])
        install(cam)
        out = _payload(cr.get_machining_time_handler())
        assert out["total_machining_time_seconds"] == 240.0
        assert out["setups"][0]["machining_time_seconds"] == 120.0
        assert out["setups"][0]["machining_time_hms"] == "0:02:00"

    def test_setup_without_a_valid_toolpath_reports_an_error_not_a_crash(self, install,
                                                                        object_collection,
                                                                        operation_cast_passthrough):
        cam = _MTCam([_MTSetup("S1", has_valid_toolpath=False)])
        install(cam)
        out = _payload(cr.get_machining_time_handler())
        assert "error" in out["setups"][0]
        assert cam.calls == []                 # never called getMachiningTime for it
        assert out["total_machining_time_seconds"] == 0.0


# ── tool_holder: a CAM tool's assigned HOLDER identity, read from its JSON (adsk.cam.Tool has no ──
# ── holder accessor). Shared by cam_get(include=['tool']) and the cam_edit_tools library listing. ──

def _HolderTool(json_str):
    """A CAM tool whose toJson() is where the holder identity is read from."""
    return FakeTool(json_text=json_str)


class TestToolHolder:
    def test_reads_full_identity(self):
        j = json.dumps({"description": "flat 10mm", "holder": {
            "description": "CAT40-ER32", "product-id": "H-123", "vendor": "Acme",
            "segments": [{}, {}, {}]}})
        assert cc.tool_holder(_HolderTool(j)) == {
            "name": "CAT40-ER32", "product_id": "H-123", "vendor": "Acme", "segment_count": 3}

    def test_none_when_no_holder_key(self):
        assert cc.tool_holder(_HolderTool(json.dumps({"description": "flat 10mm"}))) is None

    def test_none_when_holder_empty(self):
        # a default/empty holder sub-doc carries nothing meaningful -> None (not a bag of empties)
        assert cc.tool_holder(_HolderTool(json.dumps({"holder": {}}))) is None

    def test_partial_fields_only_what_is_present(self):
        j = json.dumps({"holder": {"description": "Basic Holder"}})   # a name but no product-id/vendor
        assert cc.tool_holder(_HolderTool(j)) == {"name": "Basic Holder"}

    def test_bad_json_is_none_not_a_raise(self):
        assert cc.tool_holder(_HolderTool("{not json")) is None


# ── find_setup (setup, available_names, error) / find_operation (obj, available_names) / setup_names ──


class TestFindSetup:
    def test_found_case_insensitive(self):
        cam = make_cam(FakeSetup("Setup1"), FakeSetup("Setup2"))
        s, avail, err = cc.find_setup(cam, "setup2")     # lowercase input resolves 'Setup2'
        assert s is not None and s.name == "Setup2"
        assert avail == ["Setup1", "Setup2"]
        assert err is None                               # a hit carries no refusal

    def test_not_found_returns_available(self):
        cam = make_cam(FakeSetup("Setup1"))
        s, avail, err = cc.find_setup(cam, "Ghost")
        assert s is None and avail == ["Setup1"]
        assert "No setup named 'Ghost'" in err and "Setup1" in err

    def test_empty_cam_is_safe(self):
        s, avail, err = cc.find_setup(make_cam(), "x")
        assert s is None and avail == []
        assert "No setup named 'x'" in err


class TestFindSetupDuplicate:
    def test_duplicate_setup_name_is_refused(self):
        cam = make_cam(FakeSetup("Dup"), FakeSetup("Dup"))
        s, _avail, err = cc.find_setup(cam, "Dup")
        assert s is None                                  # refused, never the first hit
        assert err is not None

    def test_the_refusal_says_ambiguous_not_missing(self):
        # a name found TWICE is not absent - the refusal a caller returns must not say it is.
        cam = make_cam(FakeSetup("Dup"), FakeSetup("Other"), FakeSetup("Dup"))
        s, _avail, err = cc.find_setup(cam, "Dup")
        assert s is None
        assert "is ambiguous" in err and "2 CAM items share that name" in err
        assert "No setup named" not in err

    def test_the_name_list_stays_a_name_list(self):
        # the refusal travels in its OWN field: a sentence injected into available_names would be
        # split into garbage by the ', '.join a caller builds a choice list with.
        cam = make_cam(FakeSetup("Dup"), FakeSetup("Dup"), FakeSetup("Other"))
        _s, avail, _err = cc.find_setup(cam, "Dup")
        assert avail == ["Dup", "Dup", "Other"]
        assert all("ambiguous" not in (n or "") for n in avail)

    def test_the_duplicate_refusal_is_case_insensitive_like_the_match(self):
        cam = make_cam(FakeSetup("Dup"), FakeSetup("DUP"))
        s, _avail, err = cc.find_setup(cam, "dup")
        assert s is None and "is ambiguous" in err

    def test_a_plain_miss_is_worded_as_absence(self):
        cam = make_cam(FakeSetup("Dup"), FakeSetup("Dup"), FakeSetup("Other"))
        s, avail, err = cc.find_setup(cam, "Ghost")
        assert s is None and avail == ["Dup", "Dup", "Other"]
        assert "No setup named 'Ghost'" in err and "ambiguous" not in err


class TestSetupNames:
    def test_lists_all_setup_names(self):
        assert cc.setup_names(make_cam(FakeSetup("A"), FakeSetup("B"))) == ["A", "B"]


class TestWalkOperations:
    def test_flattens_across_setups_countitem(self):
        cam = make_cam(FakeSetup("S1", ops=[FakeOperation("Face1"), FakeOperation("Adaptive1")]),
                       FakeSetup("S2", ops=[FakeOperation("Drill1")]))
        assert [o.name for o in cc.walk_operations(cam)] == ["Face1", "Adaptive1", "Drill1"]

    def test_empty_setup_walks_to_nothing(self):
        assert cc.walk_operations(make_cam(FakeSetup("S1"))) == []


class TestFindOperation:
    def test_found_case_insensitive_across_setups(self):
        cam = make_cam(FakeSetup("S1", ops=[FakeOperation("Face1")]),
                       FakeSetup("S2", ops=[FakeOperation("Drill1")]))
        op, avail = cc.find_operation(cam, "drill1")     # lowercase input resolves 'Drill1'
        assert op is not None and op.name == "Drill1"
        assert avail == ["Face1", "Drill1"]

    def test_not_found_returns_available(self):
        cam = make_cam(FakeSetup("S1", ops=[FakeOperation("Face1")]))
        op, avail = cc.find_operation(cam, "Ghost")
        assert op is None and avail == ["Face1"]

    def test_duplicate_name_is_refused_with_setup_paths(self):
        # a name duplicated across setups returns NO op (refusal, never first-match) and each
        # duplicate's 'Setup / op' path as the available list, so even a plain not-found error
        # surfaces the collision.
        cam = make_cam(FakeSetup("S1", ops=[FakeOperation("Drill1")]),
                       FakeSetup("S2", ops=[FakeOperation("Drill1")]))
        op, avail = cc.find_operation(cam, "Drill1")
        assert op is None
        assert avail == ["S1 / Drill1", "S2 / Drill1"]


# ── walk_cam_tree / resolve_cam_node / operations_under: the shared traversal + refusal resolver ──


def _tree_cam():
    """Two setups; Setup1 nests an op in a folder, a pattern in that folder, and a loose op."""
    pattern = FakeCAMFolder("Pat1", ops=[FakeOperation("Bore1")])
    folder = FakeCAMFolder("Holes", ops=[FakeOperation("Drill1")], patterns=[pattern])
    s1 = FakeSetup("Setup1", ops=[FakeOperation("Face1")], folders=[folder])
    s2 = FakeSetup("Setup2", ops=[FakeOperation("Face2")])
    return make_cam(s1, s2), s1, s2, folder, pattern


class TestWalkCamTree:
    def test_structural_kinds_and_paths(self):
        cam, s1, s2, folder, pattern = _tree_cam()
        nodes = {(n.kind, n.path): n for n in cc.walk_cam_tree(cam)}
        assert ("setup", "Setup1") in nodes
        assert ("operation", "Setup1 / Face1") in nodes
        assert ("folder", "Setup1 / Holes") in nodes
        assert ("operation", "Setup1 / Holes / Drill1") in nodes
        assert ("pattern", "Setup1 / Holes / Pat1") in nodes
        assert ("operation", "Setup1 / Holes / Pat1 / Bore1") in nodes
        assert ("operation", "Setup2 / Face2") in nodes
        assert nodes[("operation", "Setup1 / Holes / Drill1")].setup == "Setup1"

    def test_containers_unreachable_via_alloperations_are_walked(self):
        # setup.allOperations DROPS folder/pattern containers (the measured fact the conftest trio
        # encodes) - the walk still reaches them through the explicit .folders/.patterns recursion.
        cam, s1, _, folder, pattern = _tree_cam()
        assert all(getattr(o, "name") != "Holes" for o in s1.allOperations)   # dropped by flatten
        kinds = {n.name: n.kind for n in cc.walk_cam_tree(cam)}
        assert kinds["Holes"] == "folder" and kinds["Pat1"] == "pattern"

    def test_walk_operations_projection_includes_nested(self):
        cam, *_ = _tree_cam()
        assert sorted(o.name for o in cc.walk_operations(cam)) == \
            ["Bore1", "Drill1", "Face1", "Face2"]

    def test_operations_under_scopes_to_one_container(self):
        cam, s1, s2, folder, pattern = _tree_cam()
        assert sorted(o.name for o in cc.operations_under(s1)) == ["Bore1", "Drill1", "Face1"]
        assert [o.name for o in cc.operations_under(folder)] == ["Drill1", "Bore1"]
        assert [o.name for o in cc.operations_under(s2)] == ["Face2"]

    def test_operation_nodes_under_keeps_the_full_breadcrumb_from_the_nodes_own_path(self):
        # THE POINT of the node form: a scoped walk started at "" prints ' / Drill1', which names
        # neither the setup nor the folder - and that breadcrumb is the only thing separating two
        # operations of one name. Walked from the node's own path, every row is addressable.
        cam, s1, _s2, _folder, _pattern = _tree_cam()
        node = {n.name: n for n in cc.walk_cam_tree(cam)}["Setup1"]
        assert [n.path for n in cc.operation_nodes_under(node)] == [
            "Setup1 / Face1", "Setup1 / Holes / Drill1", "Setup1 / Holes / Pat1 / Bore1"]

    def test_operation_nodes_under_a_FOLDER_starts_at_that_folders_path(self):
        cam, *_ = _tree_cam()
        folder_node = {n.path: n for n in cc.walk_cam_tree(cam)}["Setup1 / Holes"]
        rows = cc.operation_nodes_under(folder_node)
        assert [n.path for n in rows] == ["Setup1 / Holes / Drill1",
                                          "Setup1 / Holes / Pat1 / Bore1"]
        assert all(n.setup == "Setup1" for n in rows)   # the node's own setup, not a re-resolve

    def test_operation_nodes_under_an_empty_container_is_empty(self):
        cam, *_ = _tree_cam()
        empty = cc.tree_nodes(FakeSetup("Bare"))[0]
        assert cc.operation_nodes_under(empty) == []


class _UnreadableNameFolder(FakeCAMFolder):
    """A CAM folder whose .name RAISES - the read the walk cannot answer, which safe() reports as
    None. The setter is inert so the container protocol still builds."""

    @property
    def name(self):
        raise RuntimeError("folder name unreadable")

    @name.setter
    def name(self, value):
        pass


class TestBreadcrumbSegments:
    """Every segment of a walked path is a name that READ, or a marker saying that level did not.

    The walk joins each level into an f-string, so a name that did not answer joins as the literal
    'None' - a segment nothing tells apart from a container really NAMED that, which makes an
    address to a container nobody identified look complete. The decision is made on the READ, never
    on the joined text, so both halves of that pair are driven here."""

    def test_a_folder_whose_name_raises_is_disclosed_not_joined_as_None(self):
        blind = _UnreadableNameFolder("ignored", ops=[FakeOperation("Drill1")])
        cam = make_cam(FakeSetup("S1", folders=[blind]))
        paths = [n.path for n in cc.walk_cam_tree(cam)]
        assert f"S1 / {cc._UNREAD_SEGMENT} / Drill1" in paths
        assert not any("None" in p for p in paths)

    def test_a_folder_LITERALLY_named_None_keeps_its_own_segment(self):
        # THE GUARD: the repair keys on the READ answering None, never on a string match over the
        # joined path. A folder really named 'None' is a container that answered, so its address is
        # its own name and no marker appears anywhere in the tree.
        cam = make_cam(FakeSetup("S1", folders=[FakeCAMFolder("None",
                                                              ops=[FakeOperation("Drill1")])]))
        paths = [n.path for n in cc.walk_cam_tree(cam)]
        assert "S1 / None" in paths and "S1 / None / Drill1" in paths
        assert not any(cc._UNREAD_SEGMENT in p for p in paths)

    def test_an_operations_own_unreadable_name_is_disclosed_too(self):
        # the leaf level joins through the same helper - a row named after nothing is not a row
        # named 'None'.
        op = FakeOperation("placeholder")
        op.name = None
        cam = make_cam(FakeSetup("S1", ops=[op]))
        assert [n.path for n in cc.walk_cam_tree(cam)] == ["S1", f"S1 / {cc._UNREAD_SEGMENT}"]

    def test_a_setup_whose_name_does_not_read_leaves_a_marked_root_not_a_blank_one(self):
        # A blank root segment prints ' / Drill1', which reads as an operation sitting in no
        # container at all rather than one whose container did not answer.
        s = FakeSetup("placeholder", ops=[FakeOperation("Drill1")])
        s.name = None
        cam = make_cam(s)
        assert [n.path for n in cc.walk_cam_tree(cam)] == [
            cc._UNREAD_SEGMENT, f"{cc._UNREAD_SEGMENT} / Drill1"]

    def test_a_name_that_reads_empty_is_not_marked(self):
        # '' is a read that ANSWERED. Only a name that did not read is disclosed, so the marker
        # never stands in for a value Fusion actually handed back.
        cam = make_cam(FakeSetup("S1", folders=[FakeCAMFolder("", ops=[FakeOperation("Drill1")])]))
        assert "S1 /  / Drill1" in [n.path for n in cc.walk_cam_tree(cam)]

    def test_the_raw_name_read_is_untouched_by_the_disclosure(self):
        # _op_breadcrumb-style consumers decide per LEVEL off node.name, so the marker must live in
        # the PATH only - a name field carrying it would make the level look readable.
        blind = _UnreadableNameFolder("ignored", ops=[FakeOperation("Drill1")])
        cam = make_cam(FakeSetup("S1", folders=[blind]))
        folder_node = [n for n in cc.walk_cam_tree(cam) if n.kind == "folder"][0]
        assert folder_node.name is None


class TestResolveCamNode:
    def test_unique_hit_returns_node(self):
        cam, *_ = _tree_cam()
        node, err = cc.resolve_cam_node(cam, "drill1")            # case-insensitive exact
        assert err is None and node.kind == "operation" and node.name == "Drill1"
        assert node.setup == "Setup1" and node.path == "Setup1 / Holes / Drill1"

    def test_kinds_filter_excludes_other_kinds(self):
        # a folder name is NOT resolvable when only operations are asked for.
        cam, *_ = _tree_cam()
        node, err = cc.resolve_cam_node(cam, "Holes", kinds=("operation",), label="operation")
        assert node is None and "No operation named 'Holes'" in err

    def test_miss_lists_available_names(self):
        cam, *_ = _tree_cam()
        node, err = cc.resolve_cam_node(cam, "Ghost", kinds=("operation",), label="operation")
        assert node is None
        assert "Ghost" in err and "Face1" in err and "Drill1" in err

    def test_duplicate_refused_with_count_and_paths(self):
        cam = make_cam(FakeSetup("Setup1", ops=[FakeOperation("Drill1")]),
                       FakeSetup("Setup2", ops=[FakeOperation("Drill1")]))
        node, err = cc.resolve_cam_node(cam, "Drill1")
        assert node is None and "ambiguous" in err and "2" in err
        assert "Setup1 / Drill1" in err and "Setup2 / Drill1" in err

    def test_setup_scoped_resolution(self):
        # setup= scopes the walk: the same name in ANOTHER setup neither resolves nor collides.
        cam, s1, s2, *_ = _tree_cam()
        node, err = cc.resolve_cam_node(None, "Face1", setup=s1)
        assert err is None and node.obj.name == "Face1"
        node, err = cc.resolve_cam_node(None, "Face2", setup=s1, label="operation")
        assert node is None and "Face2" in err

    def test_a_setup_scope_OVERRIDES_a_handed_in_pool(self):
        # `nodes` is the unscoped caller's own walk; `setup` is a narrower question. A scoped call
        # that reused the wider pool would resolve a node from ANOTHER setup - the scope silently
        # doing nothing - so setup= must win over any pool handed alongside it.
        cam, s1, s2, *_ = _tree_cam()
        wide = cc.walk_cam_tree(cam)
        assert any(n.name == "Face2" for n in wide)        # the foreign node IS in the pool
        node, err = cc.resolve_cam_node(cam, "Face2", setup=s1, label="operation", nodes=wide)
        assert node is None and "No operation named 'Face2'" in err
        # and the setup's OWN operation still resolves through the same call shape
        node, err = cc.resolve_cam_node(cam, "Face1", setup=s1, label="operation", nodes=wide)
        assert err is None and node.obj.name == "Face1"

    def test_setup_kind_matches_setups_only(self):
        cam, *_ = _tree_cam()
        node, err = cc.resolve_cam_node(cam, "setup2", kinds=("setup",), label="setup")
        assert err is None and node.kind == "setup" and node.name == "Setup2"


# The four kinds cam_delete passes. A resolve whose kinds include `setup` alongside another kind is
# how a SETUP lands in a candidate list beside a namesake; two setups cannot share a name (Fusion
# dedupes), so that collision is always setup-vs-other.
_ANY_KIND = ("setup", "operation", "folder", "pattern")


class TestDuplicateNameAddress:
    """One SETUP and one OPERATION sharing a name - the buildable collision a setup's path cannot
    discriminate, since _setup_node makes a setup's path its own bare name. Reached through the
    mixed-kind resolve the delete/post/setup-sheet/inspect tools all run: the setup candidate is
    told apart by its OPERATION COUNT, the operation by its 'Setup / op' path."""

    def _dup(self):
        """A setup 'Dup' holding two operations, and an operation 'Dup' in a differently-named
        setup - measured: names collide across parents, never between two setups."""
        setup = FakeSetup("Dup", ops=[FakeOperation("Face1"), FakeOperation("Face2")])
        other = FakeSetup("S2", ops=[FakeOperation("Dup")])
        return make_cam(setup, other), setup, other.operations.item(0)

    def test_candidates_carry_an_address_and_the_fact_that_tells_them_apart(self):
        cam, _setup, _op = self._dup()
        node, err = cc.resolve_cam_node(cam, "Dup", kinds=_ANY_KIND, label="CAM item")
        assert node is None
        assert "Dup#1 (2 operations)" in err        # the SETUP - its path is its own bare name
        assert "Dup#2 (at S2 / Dup)" in err         # the operation - its breadcrumb discriminates

    def test_the_refusal_asks_for_no_rename(self):
        cam, *_ = self._dup()
        _node, err = cc.resolve_cam_node(cam, "Dup", kinds=_ANY_KIND, label="CAM item")
        assert "ename" not in err          # "Rename"/"rename" - the address is performable instead

    def test_each_offered_address_resolves_that_item(self):
        # the discriminating assertion: identity, not just "an error-free answer". A scope-blind
        # resolver returns the FIRST hit for both addresses and reports no error.
        cam, setup, op = self._dup()
        one, err_one = cc.resolve_cam_node(cam, "Dup#1", kinds=_ANY_KIND)
        two, err_two = cc.resolve_cam_node(cam, "Dup#2", kinds=_ANY_KIND)
        assert err_one is None and one.obj is setup and one.kind == "setup"
        assert err_two is None and two.obj is op and two.kind == "operation"

    def test_an_ordinal_past_the_end_names_the_range_it_stops_at(self):
        cam, *_ = self._dup()
        node, err = cc.resolve_cam_node(cam, "Dup#3", kinds=_ANY_KIND)
        assert node is None
        assert "numbered 1 to 2" in err and "Dup#1 (2 operations)" in err

    def test_an_ordinal_of_zero_is_not_an_address(self):
        # the boundary below the 1-based range: '#0' addresses nothing, so it must not answer the
        # first item.
        cam, *_ = self._dup()
        node, err = cc.resolve_cam_node(cam, "Dup#0", kinds=_ANY_KIND)
        assert node is None and "numbered 1 to 2" in err

    def test_the_last_address_in_range_resolves_rather_than_overrunning(self):
        # the upper boundary of `1 <= ordinal <= len(same)`: '#2' of two is the last VALID address.
        cam, _setup, op = self._dup()
        node, err = cc.resolve_cam_node(cam, "Dup#2", kinds=_ANY_KIND)
        assert err is None and node.obj is op

    def test_a_name_that_itself_carries_a_hash_wins_over_the_address(self):
        # a literal name resolves by its own spelling; '#n' is read only where none does. Only ONE
        # item is named 'Dup' here, so 'Dup#2' addresses nothing and the two readings cannot clash.
        # Two setups named 'Dup' and 'Dup#2' are DIFFERENT names, so this document is buildable.
        literal = FakeSetup("Dup#2", ops=[FakeOperation("Face1")])
        cam = make_cam(FakeSetup("Dup"), literal)
        node, err = cc.resolve_cam_node(cam, "Dup#2", kinds=("setup",), label="setup")
        assert err is None and node.obj is literal

    def test_a_literal_name_that_also_reads_as_an_address_is_refused(self):
        # 'Dup#2' names one setup outright AND addresses the second of the two items named 'Dup'
        # (an operation under S1, then the setup) - the refusal for 'Dup' itself prints that very
        # address, so the tool would be inducing a wrong pick. Both readings are named; neither is
        # taken. The addressed reading here is the SETUP, so its operation count is what names it.
        cam = make_cam(FakeSetup("S1", ops=[FakeOperation("Dup")]),
                       FakeSetup("Dup", ops=[FakeOperation("B"), FakeOperation("C")]),
                       FakeSetup("Dup#2", ops=[FakeOperation("Face1")]))
        node, err = cc.resolve_cam_node(cam, "Dup#2", kinds=_ANY_KIND, label="CAM item")
        assert node is None
        assert "reads two ways" in err and "CARRYING that name" in err
        assert "of the 2 named 'Dup'" in err
        assert "Dup#2 (2 operations)" in err        # the OTHER reading, named as it is addressed

    def test_the_address_still_resolves_when_only_one_reading_exists(self):
        # the guard above must not swallow the ordinary case: with no literal 'Dup#2' in the pool,
        # the address resolves as before.
        cam, _setup, op = self._dup()
        node, err = cc.resolve_cam_node(cam, "Dup#2", kinds=_ANY_KIND)
        assert err is None and node.obj is op

    def test_a_name_carrying_a_hash_is_addressed_on_its_LAST_separator(self):
        # a setup and an operation both named 'Op#3': the address for the second is 'Op#3#2'.
        # Splitting on the FIRST separator reads the base as 'Op' and the ordinal as '3#2', which
        # is no number at all, so the address would resolve nothing.
        setup = FakeSetup("Op#3", ops=[FakeOperation("A")])
        other = FakeSetup("S2", ops=[FakeOperation("Op#3")])
        cam = make_cam(setup, other)
        node, err = cc.resolve_cam_node(cam, "Op#3#2", kinds=_ANY_KIND)
        assert err is None and node.obj is other.operations.item(0)
        first_node, first_err = cc.resolve_cam_node(cam, "Op#3#1", kinds=_ANY_KIND)
        assert first_err is None and first_node.obj is setup

    def test_duplicate_operations_are_addressed_and_keep_their_paths(self):
        cam = make_cam(FakeSetup("Setup1", ops=[FakeOperation("Drill1")]),
                       FakeSetup("Setup2", ops=[FakeOperation("Drill1")]))
        node, err = cc.resolve_cam_node(cam, "Drill1")
        assert node is None
        assert "Drill1#1 (at Setup1 / Drill1)" in err
        assert "Drill1#2 (at Setup2 / Drill1)" in err
        picked, perr = cc.resolve_cam_node(cam, "Drill1#2")
        assert perr is None and picked.path == "Setup2 / Drill1"


class TestTwoReadingsRemedy:
    """A name reading BOTH as a literal and as an ordinal address over a same-named set.

    The fake carries its two 'Dup' operations in DIFFERENT setups, plus a third operation literally
    named 'Dup#2'. Two siblings under ONE setup are reported not to hold one name - Fusion storing
    the second as 'Face11' - but that has no ledger row, so the split across setups is the
    arrangement nothing contradicts rather than the only one a document can hold. PROBE NEEDED
    (CAM-34).

    The refusal names both readings and takes neither; what it can also offer is a sibling ADDRESS
    free to be renamed, since the ordinal reading dies once fewer than `ordinal` nodes carry the
    base name."""

    def _cam(self):
        return make_cam(FakeSetup("S1", ops=[FakeOperation("Dup"), FakeOperation("Dup#2")]),
                        FakeSetup("S2", ops=[FakeOperation("Dup")]))

    def test_both_readings_are_named_and_neither_is_taken(self):
        node, err = cc.resolve_cam_node(self._cam(), "Dup#2")
        assert node is None
        assert "reads two ways" in err and "CARRYING that name" in err
        assert "of the 2 named 'Dup'" in err

    def test_the_refusal_offers_the_free_sibling_address(self):
        # the ON boundary of the gate: ONE rename dissolves the reading (ordinal == the count named
        # 'Dup'), so the address list is cashable whichever entry the caller picks.
        _node, err = cc.resolve_cam_node(self._cam(), "Dup#2")
        assert "Rename any ONE of the items named 'Dup'" in err
        assert "addressed here as Dup#1," in err
        assert "fewer than 2 carry 'Dup'" in err

    def _three(self):
        # THREE operations named 'Dup', one per setup, plus the literal 'Dup#2': the shape where
        # 'Dup#2' addresses the MIDDLE item and two renames are needed to dissolve the reading.
        return make_cam(FakeSetup("S1", ops=[FakeOperation("Dup"), FakeOperation("Dup#2")]),
                        FakeSetup("S2", ops=[FakeOperation("Dup")]),
                        FakeSetup("S3", ops=[FakeOperation("Dup")]))

    def test_two_renames_are_offered_in_DESCENDING_order(self):
        # Each rename leaves the same-named set one item shorter, so a highest-first list keeps
        # every address still to come inside the set that remains - the order the sentence has to
        # state for the list to be worth printing at all.
        node, err = cc.resolve_cam_node(self._three(), "Dup#2")
        assert node is None
        # ordinal AND total, on the one fixture where they differ: with ordinal == total (the
        # need == 1 shape next door) a sentence printing either number reads the same, so only this
        # fixture pins that the refusal names the item it was actually asked for.
        ordinal, total = 2, 3
        assert f"item {ordinal} of the {total} named 'Dup'" in err
        assert "Rename 2 of the items named 'Dup' - Dup#3, Dup#1 - in the order given" in err
        assert "fewer than 2 carry 'Dup'" in err
        assert "keeps every address still to come inside the set that remains" in err
        # The promise stops at what the set's SIZE settles. Which item a given address reaches after
        # a rename rests on the walk returning the survivors in the same relative order, which is
        # not established here - so the sentence must not offer that.
        assert "the same items" not in err

    def test_performing_the_two_renames_in_the_order_given_dissolves_the_collision(self):
        # the round trip the multi-rename sentence claims, executed one address at a time: each
        # address still lands inside the same-named set when its turn comes, and after the last one
        # the input reads exactly one way. Taken in the printed order this passes; taken
        # lowest-first the second address names nothing (the leg below).
        #
        # WHICH of the namesakes an address reaches is deliberately not asserted - the sentence does
        # not promise it, and any `need` of them dissolve the reading equally.
        cam = self._three()
        _node, err = cc.resolve_cam_node(cam, "Dup#2")
        offered = err.split("named 'Dup' - ")[1].split(" - in the order given")[0].split(", ")
        assert offered == ["Dup#3", "Dup#1"]
        for addr in offered:
            target, terr = cc.resolve_cam_node(cam, addr)
            assert terr is None, f"'{addr}' stopped resolving part-way through the remedy: {terr}"
            assert target.name == "Dup"          # it reached one of the items the remedy names
            target.obj.name = f"Roughing ({addr})"
        node, err2 = cc.resolve_cam_node(cam, "Dup#2")
        assert err2 is None and node.path == "S1 / Dup#2"

    def test_no_remedy_is_offered_when_too_few_sibling_addresses_are_free(self):
        # need is 2 and only 'Dup#1' addresses a sibling ('Dup#2' and 'Dup#3' are carried literally,
        # so each reaches the node wearing that spelling). A list that cannot dissolve the reading
        # is not offered.
        cam = make_cam(FakeSetup("S1", ops=[FakeOperation("Dup"), FakeOperation("Dup#2"),
                                            FakeOperation("Dup#3")]),
                       FakeSetup("S2", ops=[FakeOperation("Dup")]),
                       FakeSetup("S3", ops=[FakeOperation("Dup")]))
        node, err = cc.resolve_cam_node(cam, "Dup#2")
        assert node is None and "reads two ways" in err
        assert "ename" not in err              # "Rename"/"rename" - none is performable here

    def test_the_renumbering_the_descending_order_exists_for_is_real(self):
        # what makes an ASCENDING list un-cashable, executed: rename the lowest address first and
        # the highest one no longer names anything.
        cam = self._three()
        third, err = cc.resolve_cam_node(cam, "Dup#3")
        assert err is None and third.path == "S3 / Dup"
        first, _e = cc.resolve_cam_node(cam, "Dup#1")
        first.obj.name = "Roughing"
        gone, err2 = cc.resolve_cam_node(cam, "Dup#3")
        assert gone is None and "numbered 1 to 2" in err2

    def test_the_address_a_node_CARRIES_is_never_offered_as_a_handle(self):
        # 'Dup#2' counts to a sibling AND is one operation's own name, so passing it back reaches
        # that operation rather than the sibling - it is no handle on a sibling and stays unlisted.
        _node, err = cc.resolve_cam_node(self._cam(), "Dup#2")
        offered = err.split("addressed here as ")[1].split(", so fewer")[0]
        assert offered == "Dup#1"            # the WHOLE offered list, not just its first entry

    def test_performing_the_remedy_makes_the_address_resolve(self):
        # the round trip the remedy sentence claims: rename the sibling it addressed, and the
        # literal reading is the only one left.
        cam = self._cam()
        sibling, err = cc.resolve_cam_node(cam, "Dup#1")
        assert err is None and sibling.path == "S1 / Dup"
        sibling.obj.name = "Roughing"
        node, err2 = cc.resolve_cam_node(cam, "Dup#2")
        assert err2 is None and node.path == "S1 / Dup#2"

    def test_no_remedy_is_offered_when_no_sibling_address_is_free(self):
        # 'Dup#1' and 'Dup#2' are both carried literally, so neither addresses a sibling: the
        # refusal stays remedy-less rather than printing an address that reaches the wrong node.
        cam = make_cam(FakeSetup("S1", ops=[FakeOperation("Dup"), FakeOperation("Dup#1")]),
                       FakeSetup("S2", ops=[FakeOperation("Dup"), FakeOperation("Dup#2")]))
        node, err = cc.resolve_cam_node(cam, "Dup#2")
        assert node is None and "reads two ways" in err
        assert "ename" not in err                  # "Rename"/"rename" - none is performable here

    def test_two_nodes_CARRYING_the_name_get_the_ordinary_ambiguity_refusal(self):
        # four same-named nodes across two spellings: two operations literally named 'Dup#2' AND two
        # named 'Dup'. The literal name is itself AMBIGUOUS, so the refusal that helps is the
        # standard one - it hands back 'Dup#2#1' / 'Dup#2#2', addresses this same input resolves -
        # not the two-readings text, which describes a single carrier and offers no such address.
        cam = make_cam(FakeSetup("S1", ops=[FakeOperation("Dup"), FakeOperation("Dup#2")]),
                       FakeSetup("S2", ops=[FakeOperation("Dup"), FakeOperation("Dup#2")]))
        node, err = cc.resolve_cam_node(cam, "Dup#2")
        assert node is None
        assert "2 CAM items share that name" in err and "reads two ways" not in err
        assert "Dup#2#1 (at S1 / Dup#2)" in err and "Dup#2#2 (at S2 / Dup#2)" in err
        first, e1 = cc.resolve_cam_node(cam, "Dup#2#1")
        second, e2 = cc.resolve_cam_node(cam, "Dup#2#2")
        assert e1 is None and first.path == "S1 / Dup#2"
        assert e2 is None and second.path == "S2 / Dup#2"


class TestAvailableListIsCappedByName:
    """A not-found lists what IS there, and that list crosses the wire - so it is capped by NAME
    COUNT with the remainder counted. A character cap ends the list mid-name, printing a spelling
    the caller cannot pass back."""

    def _cam(self, count):
        return make_cam(FakeSetup("S1", ops=[FakeOperation(f"Operation-{i:02d}-LongEnoughToTruncate")
                                             for i in range(count)]))

    def test_every_listed_name_is_whole_and_the_rest_are_counted(self):
        node, err = cc.resolve_cam_node(self._cam(20), "Ghost", label="operation")
        assert node is None
        listed = err.split("Available: ")[1].rstrip(".").split(", ")
        assert listed[:8] == [f"Operation-{i:02d}-LongEnoughToTruncate" for i in range(8)]
        assert listed[8:] == ["... (+12 more not listed)"]

    def test_a_short_list_is_printed_whole_with_no_remainder_clause(self):
        _node, err = cc.resolve_cam_node(self._cam(3), "Ghost", label="operation")
        assert "more not listed" not in err
        assert "Operation-02-LongEnoughToTruncate." in err

    def test_a_pool_with_no_named_item_says_none(self):
        _node, err = cc.resolve_cam_node(make_cam(FakeSetup("S1")), "Ghost", label="operation")
        assert "Available: (none)." in err


class TestResolveOperationOnePool:
    """The unscoped operation resolve and the available list a caller words its OWN remedy from come
    off ONE operation pool, in ONE walk. Two independently-filtered pools agree only by convention."""

    def _cam(self):
        # a FOLDER rides in the tree: the pool is walk_cam_tree filtered to operations, so a pool
        # filtered any other way lists the container too and the equalities below separate them.
        return make_cam(FakeSetup("S1", ops=[FakeOperation("Drill1")],
                                  folders=[FakeCAMFolder("Holes", ops=[FakeOperation("Face1")])]),
                        FakeSetup("S2", ops=[FakeOperation("Drill1")]))

    def test_a_duplicate_lists_exactly_what_find_operation_lists(self):
        cam = self._cam()
        node, err, avail = cc.resolve_operation(cam, "Drill1")
        assert node is None and "ambiguous" in err
        assert avail == ["S1 / Drill1", "S2 / Drill1"] == cc.find_operation(cam, "Drill1")[1]

    def test_a_miss_lists_exactly_what_find_operation_lists(self):
        cam = self._cam()
        node, err, avail = cc.resolve_operation(cam, "Ghost")
        assert node is None and "No operation named 'Ghost'" in err
        assert avail == ["Drill1", "Face1", "Drill1"] == cc.find_operation(cam, "Ghost")[1]

    def test_a_unique_name_resolves_and_still_lists_every_operation(self):
        cam = self._cam()
        node, err, avail = cc.resolve_operation(cam, "face1")     # case-insensitive exact
        assert err is None and node.path == "S1 / Holes / Face1"
        assert avail == ["Drill1", "Face1", "Drill1"]

    def test_the_resolve_and_its_list_share_ONE_walk(self, monkeypatch):
        # the efficiency half, and the structural one: a second walk is exactly what would let the
        # refusal and the remedy describe different censuses of the same tree.
        real = cc.walk_cam_tree
        walks = []
        monkeypatch.setattr(cc, "walk_cam_tree",
                            lambda c: (walks.append(c), real(c))[1])
        cc.resolve_operation(self._cam(), "Drill1")
        assert len(walks) == 1


# â”€â”€ inspection results: the recorded probing measurements (cam_get(include=['inspection'])) â”€â”€â”€â”€â”€â”€â”€
#
# The fakes live in conftest beside the other CAM fakes: _InspMeasure carries NO .name, because a
# measure folder exposes none live.

import adsk.cam  # noqa: E402 - the state values below come from the mock enum, never hand-seeded
import adsk.fusion  # noqa: E402 - the Occurrence cast the setup-reference walk filters on

from conftest import _InspMeasure, _InspPath, _InspPoint  # noqa: E402
from conftest import make_gated_cam  # noqa: E402
from conftest import make_inspection_cam as _inspection_cam  # noqa: E402

_WITHIN = adsk.cam.InspectionPointState.WithinTolerance
_ABOVE = adsk.cam.InspectionPointState.AboveTolerance
_BELOW = adsk.cam.InspectionPointState.BelowTolerance
_UNPROJECTED = adsk.cam.InspectionPointState.Unprojected


class TestInspectionEmptyState:
    def test_none_collection_publishes_an_empty_state_not_an_error(self, install):
        # A never-probed document reads inspectionResults as None on some documents and as an empty
        # collection on others; this is the None side (the empty side is two tests down). Both are a
        # zero answer, so neither may come back as a failure.
        install(_inspection_cam(None))
        out = _payload(cr.get_inspection_results_handler())
        assert out["available"] is False and out["readable"] is True
        assert out["measure_count"] == 0 and out["measures"] == []
        assert "None" in out["note"] and "probing" in out["note"]
        assert "read_error" not in out          # nothing raised - this is an answer, not a failure

    def test_a_raising_property_is_an_unreadable_state_carrying_the_reason(self, install):
        # A gated CAM member RAISES rather than reading empty (measured on stockMaterialLibrary),
        # which must not be collapsed into "this document has no results".
        install(make_gated_cam(text="preview feature is not enabled"))
        out = _payload(cr.get_inspection_results_handler())
        assert out["available"] is False and out["readable"] is False
        assert "preview feature is not enabled" in out["read_error"]
        assert "RAISED" in out["note"]

    def test_present_but_empty_collection_reads_available_with_zero_measures(self, install):
        install(_inspection_cam([]))
        out = _payload(cr.get_inspection_results_handler())
        assert out["available"] is True and out["measure_count"] == 0
        assert "no measures" in out["note"]

    def test_null_path_results_is_zero_paths_not_a_crash(self, install):
        # CAMMeasure.inspectionPathResults is documented to return null when the measure holds none.
        install(_inspection_cam([_InspMeasure(None)]))
        out = _payload(cr.get_inspection_results_handler())
        assert out["measures"][0]["path_count"] == 0
        assert out["measures"][0]["point_count"] == 0
        assert "states" not in out["measures"][0]


class TestInspectionRollup:
    def _cam(self):
        clean = _InspMeasure([_InspPath([_InspPoint(_WITHIN) for _ in range(4)])])
        mixed = _InspMeasure([
            _InspPath([_InspPoint(_WITHIN),
                       _InspPoint(_ABOVE, deviation=0.5, error=0.2)]),
            _InspPath([_InspPoint(_BELOW, deviation=0.9, error=0.7),
                       _InspPoint(_UNPROJECTED),
                       _InspPoint(_ABOVE, deviation=0.3, error=0.1)])])
        return _inspection_cam([clean, mixed])

    def test_clean_measure_drops_the_zero_buckets(self, install):
        install(self._cam())
        row = _payload(cr.get_inspection_results_handler())["measures"][0]
        assert row["states"] == {"within_tolerance": 4}
        assert row["out_of_tolerance"] == 0 and "worst" not in row

    def test_out_of_tolerance_sums_above_below_and_unprojected(self, install):
        install(self._cam())
        row = _payload(cr.get_inspection_results_handler())["measures"][1]
        assert row["states"] == {"within_tolerance": 1, "above_tolerance": 2,
                                 "below_tolerance": 1, "unprojected": 1}
        assert row["out_of_tolerance"] == 4
        assert row["point_count"] == 5 and row["path_count"] == 2

    def test_worst_point_is_the_highest_error_and_names_its_position(self, install):
        install(self._cam())
        worst = _payload(cr.get_inspection_results_handler())["measures"][1]["worst"]
        # the below-tolerance point at path 1 / point 0 carries the largest error (0.7 cm -> 7 mm)
        assert worst["path"] == 1 and worst["point"] == 0
        assert worst["state"] == "below_tolerance" and worst["error"] == 7.0

    def test_worst_is_the_largest_error_MAGNITUDE_and_publishes_the_raw_sign(self, install):
        # error's sign semantics are not measured, so the pick ranks on abs(): identity when the
        # value is unsigned, correct when it is signed. The row keeps the raw value.
        install(_inspection_cam([_InspMeasure([_InspPath([
            _InspPoint(_ABOVE, deviation=0.4, error=0.4),
            _InspPoint(_BELOW, deviation=0.9, error=-0.9)])])]))
        worst = _payload(cr.get_inspection_results_handler())["measures"][0]["worst"]
        assert worst["point"] == 1 and worst["state"] == "below_tolerance"
        assert worst["error"] == -9.0

    def test_rows_are_indexed_and_carry_no_name(self, install):
        install(self._cam())
        out = _payload(cr.get_inspection_results_handler())
        assert [r["index"] for r in out["measures"]] == [0, 1]
        assert all("name" not in r for r in out["measures"])
        assert "INDEX" in out["note"]


class TestInspectionDeepRead:
    def _oot_cam(self, count=300):
        pts = [_InspPoint(_ABOVE, deviation=0.1, error=0.1) for _ in range(count)]
        return _inspection_cam([_InspMeasure([_InspPath(pts)])])

    def test_deep_read_filters_to_out_of_tolerance_points(self, install):
        install(_inspection_cam([_InspMeasure([_InspPath(
            [_InspPoint(_WITHIN), _InspPoint(_ABOVE, deviation=0.2, error=0.1),
             _InspPoint(_WITHIN), _InspPoint(_UNPROJECTED)])])]))
        out = _payload(cr.get_inspection_results_handler(measure="0"))
        assert out["point_count"] == 4 and out["out_of_tolerance"] == 2
        assert [p["index"] for p in out["points"]] == [1, 3]
        assert out["filter"] == "out_of_tolerance" and out["truncated"] is False

    def test_default_cap_truncates_and_reports_the_honest_totals(self, install):
        install(self._oot_cam(300))
        out = _payload(cr.get_inspection_results_handler(measure="0"))
        assert out["returned"] == cr._INSPECTION_ROW_DEFAULT == len(out["points"])
        assert out["truncated"] is True
        assert out["point_count"] == 300 and out["out_of_tolerance"] == 300

    def test_max_results_is_capped_hard(self, install):
        install(self._oot_cam(300))
        out = _payload(cr.get_inspection_results_handler(measure="0", max_results=1000))
        assert out["returned"] == cr._INSPECTION_ROW_CAP and out["truncated"] is True

    def test_path_scope_reads_only_that_path(self, install):
        install(_inspection_cam([_InspMeasure([
            _InspPath([_InspPoint(_ABOVE, deviation=0.1, error=0.1)]),
            _InspPath([_InspPoint(_BELOW, deviation=0.2, error=0.2),
                       _InspPoint(_BELOW, deviation=0.3, error=0.3)])])]))
        out = _payload(cr.get_inspection_results_handler(measure="0/1"))
        assert out["path"] == 1 and out["point_count"] == 2 and out["returned"] == 2
        assert {p["state"] for p in out["points"]} == {"below_tolerance"}

    def test_point_row_scales_every_length_out_of_cm(self, install):
        install(_inspection_cam([_InspMeasure([_InspPath([
            _InspPoint(_ABOVE, deviation=1.0, error=0.5, offset=0.2, nominal=(2.0, 0.0, -1.0),
                       contact=(2.1, 0.0, -1.0), projected=(2.05, 0.0, -1.0),
                       delta=(0.1, 0.0, 0.0))])])]))
        row = _payload(cr.get_inspection_results_handler(measure="0"))["points"][0]
        assert row["deviation"] == 10.0 and row["error"] == 5.0 and row["offset"] == 2.0
        assert row["nominal"] == [20.0, 0.0, -10.0]
        assert row["contact"] == [21.0, 0.0, -10.0]
        assert row["projected"] == [20.5, 0.0, -10.0]
        assert row["delta"] == [1.0, 0.0, 0.0]

    def test_cm_units_leave_the_internal_value_alone(self, install):
        install(_inspection_cam([_InspMeasure([_InspPath([
            _InspPoint(_ABOVE, deviation=1.0, error=0.5)])])]))
        row = _payload(cr.get_inspection_results_handler(measure="0", units="cm"))["points"][0]
        assert row["deviation"] == 1.0 and row["error"] == 0.5

    def test_unreadable_length_reads_null_never_zero(self, install):
        # 0.0 is an ANSWER ("dead on nominal"), so an unreadable field must not report one.
        install(_inspection_cam([_InspMeasure([_InspPath([
            _InspPoint(_ABOVE, deviation=1.0, error=0.5, readable=False)])])]))
        row = _payload(cr.get_inspection_results_handler(measure="0"))["points"][0]
        assert row["deviation"] is None and row["error"] == 5.0


class TestInspectionStateNames:
    def test_unknown_state_value_degrades_to_str_and_is_not_called_out_of_tolerance(self, install):
        install(_inspection_cam([_InspMeasure([_InspPath([_InspPoint(7)])])]))
        row = _payload(cr.get_inspection_results_handler())["measures"][0]
        assert row["states"] == {"7": 1}
        assert row["out_of_tolerance"] == 0     # no verdict on a state this build cannot name

    def test_unreadable_state_reads_unknown(self):
        assert cr._point_state_name(None, {}) == "unknown"

    def test_named_members_map_to_the_wire_names(self):
        names = cr._point_state_map()
        assert names[_WITHIN] == "within_tolerance" and names[_ABOVE] == "above_tolerance"
        assert names[_BELOW] == "below_tolerance" and names[_UNPROJECTED] == "unprojected"


class TestInspectionGuards:
    def test_a_name_is_refused_because_measures_have_none(self, install):
        install(_inspection_cam([_InspMeasure([])]))
        res = cr.get_inspection_results_handler(measure="Measure1")
        assert res["isError"] is True
        assert "Measure1" in res["message"] and "index" in res["message"]

    def test_measure_index_out_of_range_names_the_range(self, install):
        install(_inspection_cam([_InspMeasure([]), _InspMeasure([])]))
        res = cr.get_inspection_results_handler(measure="5")
        assert res["isError"] is True
        assert "5" in res["message"] and "2 measure(s)" in res["message"]

    def test_a_three_part_scope_is_refused_naming_how_many_parts_it_has(self, install):
        # 'measure' addresses at most <measure>/<path>. A third part is a caller who means something
        # the scope cannot express; parsing it as measure 0 and dropping the rest would answer a
        # DIFFERENT question than the one asked, and report success doing it.
        install(_inspection_cam([_InspMeasure([_InspPath([])])]))
        res = cr.get_inspection_results_handler(measure="0/1/2")
        assert res["isError"] is True
        assert "'0/1/2'" in res["message"] and "3 parts" in res["message"]

    def test_the_two_legal_scope_shapes_still_parse(self, install):
        # The refusal above must not swallow the shapes that ARE addressable.
        assert cr._parse_measure_scope("1") == (1, None, None)
        assert cr._parse_measure_scope("1/2") == (1, 2, None)

    def test_path_index_out_of_range_names_the_path_count(self, install):
        install(_inspection_cam([_InspMeasure([_InspPath([])])]))
        res = cr.get_inspection_results_handler(measure="0/3")
        assert res["isError"] is True
        assert "path index 3" in res["message"] and "1 path(s)" in res["message"]

    def test_unknown_units_refused_naming_the_value(self, install):
        install(_inspection_cam([]))
        res = cr.get_inspection_results_handler(units="furlong")
        assert res["isError"] is True and "furlong" in res["message"]

    def test_no_cam_product_surfaces_the_shared_gate(self, monkeypatch):
        monkeypatch.setattr(cr, "get_cam", lambda: (None, "This document has no CAM product yet."))
        res = cr.get_inspection_results_handler()
        assert res["isError"] is True and "CAM" in res["message"]

    def test_measure_rollup_is_capped_but_the_measure_count_stays_honest(self, install):
        install(_inspection_cam([_InspMeasure([]) for _ in range(cr._MAX_ITEMS + 2)]))
        out = _payload(cr.get_inspection_results_handler())
        assert out["measures_truncated"] is True
        assert len(out["measures"]) == cr._MAX_ITEMS
        assert out["measure_count"] == cr._MAX_ITEMS + 2      # the TRUE total, not the row count

    def test_a_measure_index_that_does_not_resolve_is_refused(self, install):
        # the collection counts 1 but item(0) hands back nothing - an honest refusal, never an
        # empty-but-successful read of a measure that was never obtained.
        install(SimpleNamespace(inspectionResults=SimpleNamespace(count=1, item=lambda i: None)))
        res = cr.get_inspection_results_handler(measure="0")
        assert res["isError"] is True and "did not resolve" in res["message"]

    def test_an_absent_point_reads_null_not_a_zero_triple(self):
        # _xyz's absent-point answer: [0,0,0] would claim a measured position at the origin.
        assert cr._xyz(None, 10.0) is None


# --- expression_error: the post-set CAMParameter read-back every CAM param editor gates on ---
# The CAM param store is NOT the CAD one: a broken expression is STORED verbatim and its value reads
# back a finite 0.0, so only .error reveals it. A .warning fires on VALID expressions too.

class TestExpressionError:
    def test_an_error_message_is_returned_and_gates(self):
        p = SimpleNamespace(error="Failed to evaluate expression.", warning="")
        assert cc.expression_error(p) == ("Failed to evaluate expression.", None)

    def test_a_warning_alone_never_reads_as_an_error(self):
        # a warning fires on VALID expressions ("stock less than the model width") - gating on it
        # would refuse edits that landed correctly.
        err, warn = cc.expression_error(
            SimpleNamespace(error="", warning="stock less than the model width"))
        assert err is None and warn == "stock less than the model width"

    def test_an_uninterpolated_template_token_is_tagged_cosmetic(self):
        err, warn = cc.expression_error(
            SimpleNamespace(error="", warning="${self.title} is out of range"))
        assert err is None
        assert warn.startswith("${self.title} is out of range")
        assert "uninterpolated" in warn and "cosmetic" in warn

    def test_whitespace_only_fields_are_not_a_fault(self):
        assert cc.expression_error(SimpleNamespace(error="   ", warning="\n")) == (None, None)

    def test_unreadable_fields_read_as_clean_not_as_a_raise(self):
        assert cc.expression_error(SimpleNamespace()) == (None, None)


# --- clamp_rows: the ONE max_results clamp, so no caller can lift a wire cap ---

class TestClampRows:
    def test_a_value_inside_the_band_is_kept(self):
        assert cc.clamp_rows(25, 50, 200) == 25

    def test_zero_and_none_fall_back_to_the_reads_own_default(self):
        assert cc.clamp_rows(0, 50, 200) == 50
        assert cc.clamp_rows(None, 50, 200) == 50

    def test_a_non_numeric_request_falls_back_rather_than_raising(self):
        # max_results arrives off the wire: a string/list must not sink the whole read.
        assert cc.clamp_rows("lots", 50, 200) == 50
        assert cc.clamp_rows([1, 2], 50, 200) == 50

    def test_a_numeric_string_is_honoured(self):
        assert cc.clamp_rows("25", 50, 200) == 25

    def test_above_the_ceiling_is_held_at_the_ceiling(self):
        assert cc.clamp_rows(10000, 50, 200) == 200

    def test_a_negative_request_lifts_to_one_row(self):
        assert cc.clamp_rows(-5, 50, 200) == 1


# --- get_cam / validity_basis: the two module-level `app` reads every CAM tool sits on ---

class TestGetCamGuards:
    def test_no_active_document(self, monkeypatch):
        monkeypatch.setattr(cc, "app", SimpleNamespace(activeDocument=None))
        cam, err = cc.get_cam()
        assert cam is None and err == "No active document."

    def test_unreadable_products_is_its_own_reason_not_the_manufacture_gate(self, monkeypatch):
        # reporting the "enter Manufacture" teaching here would send the agent to switch workspace
        # over a document whose products collection could not be read at all.
        monkeypatch.setattr(cc, "app",
                            SimpleNamespace(activeDocument=SimpleNamespace(products=None)))
        cam, err = cc.get_cam()
        assert cam is None and err == "Could not access document products."

    def test_a_document_without_a_cam_product_teaches_the_one_fix(self, monkeypatch):
        monkeypatch.setattr(adsk.cam.CAM, "cast", lambda x: None)
        monkeypatch.setattr(cc, "app", SimpleNamespace(activeDocument=SimpleNamespace(
            products=SimpleNamespace(itemByProductType=lambda t: None))))
        cam, err = cc.get_cam()
        assert cam is None and "view_switch_workspace('manufacture')" in err


class TestValidityBasis:
    def test_the_manufacture_workspace_is_the_only_verified_basis(self, monkeypatch):
        monkeypatch.setattr(cc, "app", SimpleNamespace(userInterface=SimpleNamespace(
            activeWorkspace=SimpleNamespace(id="CAMEnvironment"))))
        assert cc.validity_basis() == "manufacture_verified"

    def test_the_design_workspace_is_unverified(self, monkeypatch):
        monkeypatch.setattr(cc, "app", SimpleNamespace(userInterface=SimpleNamespace(
            activeWorkspace=SimpleNamespace(id="FusionSolidEnvironment"))))
        assert cc.validity_basis() == "unverified_design_workspace"

    def test_an_unreadable_workspace_is_unverified_not_a_raise(self, monkeypatch):
        # the trust gate degrades to "do not trust" - claiming verified on an unreadable read is the
        # one answer that would publish a toolpath verdict nobody measured.
        monkeypatch.setattr(cc, "app", None)
        assert cc.validity_basis() == "unverified_design_workspace"


# --- the shared CAM-gate refusal: every read hands back get_cam's reason verbatim ---

class TestSharedCamGate:
    def test_every_read_returns_the_gate_reason_unwrapped(self, monkeypatch):
        monkeypatch.setattr(cr, "get_cam", lambda: (None, "no CAM product here."))
        for handler in (cr.get_cam_setups_handler, cr.get_cam_operations_handler,
                        cr.get_setup_references_handler, cr.get_tool_list_handler,
                        cr.get_machining_time_handler, cr.get_nc_programs_handler):
            res = handler()
            assert res["isError"] is True, handler.__name__
            assert res["message"] == "no CAM product here.", handler.__name__


# --- op_state_tally / live_readiness: the edges the whole-document health signal turns on ---

def _unreadable_item(*_args):
    raise RuntimeError("setup read failed")


def _op_with_unreadable_tool(name):
    """An Operation whose .tool RAISES - a broken tool reference, which must not sink a whole read."""
    def _raise(_self):
        raise RuntimeError("tool reference broken")
    return type("_BrokenToolOp", (), {"name": name, "tool": property(_raise)})()


class TestOpStateTallyEdges:
    def test_a_folder_in_the_op_list_is_skipped_not_counted(self, monkeypatch):
        # allOperations can hand back a node that is not an Operation; counting it would inflate the
        # total a poller waits on, so it never completes.
        monkeypatch.setattr(adsk.cam.Operation, "cast",
                            lambda x: x if getattr(x, "operationState", None) is not None else None)
        tally = cc.op_state_tally([SimpleNamespace(name="Holes"),
                                   SimpleNamespace(name="Face1", operationState=0, hasError=False,
                                                   isGenerating=False)])
        assert tally["total"] == 1 and tally["valid"] == 1


def _tally_op(name, state=0, error=False, warning=False, suppressed=False, generating=False,
              warning_text="Contour Selection: One or more contours are missing selections.",
              error_text="Toolpath is empty"):
    """One operation as the tally reads it. The warning default is the measured live text a
    geometry-less 2D Contour carries while its state still reads valid."""
    op = FakeOperation(name, operation_state=state, suppressed=suppressed,
                       has_error=error, error=error_text if error else "",
                       has_warning=warning, warning=warning_text if warning else "")
    op.isGenerating = generating
    return op


class TestWarningOverlayTally:
    """A WARNED op keeps its lifecycle bucket (measured live: an unselected-geometry 2D Contour reads
    hasWarning=True, hasError=False, operationState=0 - 'valid', with no toolpath), so the warning is
    counted as an OVERLAY beside the buckets, and sampled through the SAME predicate it is counted by."""

    def test_a_warned_valid_op_is_counted_in_both_valid_and_warnings(self, operation_cast_passthrough):
        t = cc.op_state_tally([_tally_op("Contour1", warning=True)])
        assert t["valid"] == 1 and t["warnings"] == 1 and t["total"] == 1
        assert t["warning_sample"]["name"] == "Contour1"
        assert t["warning_sample"]["warning"].startswith("Contour Selection")

    def test_no_warnings_counts_zero_and_samples_nothing(self, operation_cast_passthrough):
        t = cc.op_state_tally([_tally_op("Face1")])
        assert t["warnings"] == 0 and t["warning_sample"] is None

    def test_an_errored_ops_warning_is_not_counted(self, operation_cast_passthrough):
        # its error already blocks the post; counting the warning too would demote a verdict that is
        # already a BLOCKER, and name a warning as the thing to read instead of the error.
        t = cc.op_state_tally([_tally_op("Drill1", error=True, warning=True)])
        assert t["errored"] == 1 and t["warnings"] == 0 and t["warning_sample"] is None

    def test_a_suppressed_ops_warning_is_not_counted(self, operation_cast_passthrough):
        # a suppressed op carries no toolpath - measured: setting isSuppressed True discards it -
        # so its warning demotes nothing in this tally.
        t = cc.op_state_tally([_tally_op("Off1", state=2, suppressed=True, warning=True)])
        assert t["suppressed"] == 1 and t["warnings"] == 0 and t["warning_sample"] is None

    def test_the_sample_is_the_FIRST_counted_warning_not_a_later_one(self, operation_cast_passthrough):
        t = cc.op_state_tally([_tally_op("Off1", state=2, suppressed=True, warning=True),
                               _tally_op("Contour1", warning=True, warning_text="first real"),
                               _tally_op("Contour2", warning=True, warning_text="second real")])
        assert t["warnings"] == 2 and t["warning_sample"]["name"] == "Contour1"

    def test_only_the_first_warning_LINE_is_sampled(self, operation_cast_passthrough):
        t = cc.op_state_tally([_tally_op("Contour1", warning=True,
                                         warning_text="line one\nline two\nline three")])
        assert t["warning_sample"]["warning"] == "line one"

    def test_an_out_of_date_warned_op_counts_in_both(self, operation_cast_passthrough):
        t = cc.op_state_tally([_tally_op("Adaptive1", state=1, warning=True)])
        assert t["out_of_date"] == 1 and t["warnings"] == 1

    def test_an_op_that_never_generated_is_tallied_as_out_of_date(self,
                                                                  operation_cast_passthrough):
        # NoToolpath is a SECOND bucket the poll must count as unfinished work: an op that never
        # generated is what cam_generate(skip_valid=true) redoes, and counting it anywhere else
        # leaves a poller reporting a job ready that holds no toolpath.
        t = cc.op_state_tally([_tally_op(
            "Rough1", state=adsk.cam.OperationStates.NoToolpathOperationState)])
        assert t["out_of_date"] == 1 and t["total"] == 1
        assert t["valid"] == 0 and t["suppressed"] == 0 and t["errored"] == 0


class TestPreviousSetupStockIsNotReadable:
    """MEASURED on a setup switched to 'previous_setup': stockSolids reads 0, no previousSetup
    parameter exists, and stockXLow..stockZHigh keep the RELATIVE-BOX numbers a plain setup reads.
    A row that published those extents silently would have an agent size a clearing strategy off a
    box the preceding setup already cut away - which is what a 400 s adaptive was."""

    def _setups(self, install, mode):
        setup = FakeSetup("S1")
        setup.stockMode = cc.stock_mode_member(mode)
        install(make_cam(setup))
        return _payload(cr.get_cam_setups_handler())

    def test_a_previous_setup_row_says_its_extents_are_not_the_rest_stock(self, install):
        out = self._setups(install, "previous_setup")
        row = out["setups"][0]
        assert row["stock_mode"] == "previous_setup"
        assert row["stock_extents_describe"] == "the relative box, NOT the rest stock this mode cuts from"
        assert "nothing readable describes that" in out["note"]

    def test_a_plain_box_setup_carries_no_such_key_or_sentence(self, install):
        # the quiet default: the caveat belongs to the one mode it is true of.
        out = self._setups(install, "relative_box")
        assert "stock_extents_describe" not in out["setups"][0]
        assert "note" not in out


class TestSettledOverTheGeneratingFlag:
    """MEASURED: an operation can read isGenerating true while its own state reads valid and it
    carries a machining time. A poll settling on the flag waits on work already finished, so the
    tally counts those apart and the completion verdict reads the STATES."""

    def test_a_generating_flag_over_a_valid_state_counts_as_settled(self,
                                                                    operation_cast_passthrough):
        t = cc.op_state_tally([_tally_op("Rough1", state=0, generating=True)])
        assert t["generating"] == 1 and t["generating_settled"] == 1
        assert cc.unsettled_count(t) == 0

    def test_a_generating_flag_over_an_unfinished_state_is_still_unsettled(
            self, operation_cast_passthrough):
        # state 3 (NoToolpath) has generating left to do, so the flag is what it says it is.
        t = cc.op_state_tally([_tally_op("Rough1", state=3, generating=True)])
        assert t["generating"] == 1 and t["generating_settled"] == 0
        assert cc.unsettled_count(t) == 1

    def test_an_errored_op_is_settled_however_the_flag_reads(self):
        assert cc.op_settled({"has_error": True, "operation_state": 1}) is True

    def test_a_suppressed_op_is_settled(self):
        assert cc.op_settled({"is_suppressed": True, "operation_state": None}) is True

    def test_an_out_of_date_op_is_not_settled(self):
        assert cc.op_settled({"has_error": False, "operation_state": 1}) is False

    def test_a_state_zero_op_with_no_toolpath_yet_is_not_settled(self):
        # state 0 alone is not proof of a finished generation: an operation with nothing to show for
        # it has produced no toolpath, so completing on it would call a job done that is not.
        assert cc.op_settled({"has_error": False, "is_suppressed": False, "is_generating": True,
                              "strategy": "adaptive", "operation_state": 0,
                              "has_toolpath": None, "is_toolpath_valid": None}) is False

    def test_a_state_zero_op_carrying_a_toolpath_is_settled(self):
        assert cc.op_settled({"has_error": False, "operation_state": 0,
                              "has_toolpath": True}) is True

    def test_the_empty_class_is_settled_too(self):
        # generated and cuts nothing: hasToolpath False with the state and validity that say it ran.
        assert cc.op_settled({"has_error": False, "is_suppressed": False, "operation_state": 0,
                              "has_toolpath": False, "is_toolpath_valid": True,
                              "strategy": "contour2d", "is_generating": False}) is True

    def test_a_settled_op_under_a_raised_flag_buckets_valid_not_generating(self):
        # THE BITE the sweep caught: cam_inspect_toolpaths read 7 of 12 operations as 'generating'
        # once the poll stopped waiting on the flag. Every reader shares this classifier, so the
        # state has to outrank the flag HERE or the tools disagree about one operation.
        facts = {"is_suppressed": False, "has_error": False, "is_generating": True,
                 "operation_state": 0, "has_toolpath": True, "is_toolpath_valid": True,
                 "strategy": "face"}
        assert cc.op_primary_state(facts) == "valid"

    def test_an_unfinished_op_under_the_flag_still_buckets_generating(self):
        # the boundary: state 1 under the flag is work in flight, and calling it out_of_date would
        # have a poller stop waiting on a generation that is genuinely running.
        facts = {"is_suppressed": False, "has_error": False, "is_generating": True,
                 "operation_state": 1, "has_toolpath": True, "is_toolpath_valid": False,
                 "strategy": "face"}
        assert cc.op_primary_state(facts) == "generating"

    def test_the_clause_is_said_only_where_the_two_counts_differ(self):
        assert cc.settled_clause({"generating": 2, "generating_settled": 0}) == ""
        assert "2 operation(s) read isGenerating true" in cc.settled_clause(
            {"generating": 2, "generating_settled": 2})

    def test_live_readiness_publishes_the_settled_count(self, install,
                                                        operation_cast_passthrough):
        install(make_cam(_machined_setup([_tally_op("Rough1", state=0, generating=True)])))
        sig, err = cc.live_readiness()
        assert err is None
        assert sig["generating"] == 1 and sig["generating_settled"] == 1


_HAAS = FakeMachine(description="Haas VF-2")


def _machined_setup(ops, name="Setup1", machine=_HAAS, error=None):
    """A setup with an assigned machine and a readable fault channel: setup_blockers reads
    Setup.machine (through machine_label), so machine=None is a setup blocked by
    no_machine_selected; error=None is the no-fault setup live_readiness counts as clean."""
    return FakeSetup(name, ops=ops, machine=machine,
                     has_error=error is not None, error=error or "")


class TestLiveReadinessEdges:
    def _cam(self, ops):
        return SimpleNamespace(setups=_NamedCollection([_machined_setup(ops)]), ncPrograms=_NamedCollection([]))

    def test_a_document_with_no_active_ops_gives_no_verdict(self, monkeypatch,
                                                            operation_cast_passthrough):
        monkeypatch.setattr(cc, "get_cam", lambda: (self._cam([]), None))
        sig, err = cc.live_readiness()
        assert err is None and sig["total"] == 0
        assert sig["readiness"] == "no active operations to assess."

    def test_only_suppressed_ops_still_gives_no_verdict(self, monkeypatch,
                                                        operation_cast_passthrough):
        # suppressed ops are excluded from posting by design, so they are not "active" - a
        # "0 of 0 valid" verdict would read as a job that needs generating.
        op = SimpleNamespace(name="Off", operationState=2, hasError=False, isGenerating=False)
        monkeypatch.setattr(cc, "get_cam", lambda: (self._cam([op]), None))
        sig, _err = cc.live_readiness()
        assert sig["suppressed"] == 1 and sig["total"] == 1
        assert sig["readiness"] == "no active operations to assess."

    def test_a_raising_walk_is_reported_as_a_reason_never_an_all_clear(self, monkeypatch):
        def _boom(_cam):
            raise RuntimeError("CAM tree read failed")
        monkeypatch.setattr(cc, "get_cam", lambda: (self._cam([]), None))
        monkeypatch.setattr(cc, "walk_operations", _boom)
        sig, err = cc.live_readiness()
        assert sig is None and "CAM tree read failed" in err


class TestReadinessWarningVerdict:
    """The verdict may not overstate. A warning does NOT block a post, so the job stays postable -
    but 'ready to post' on its own hides an op that reads valid and cut nothing, so a warned job
    reports the count and names the first warning."""

    def _sig(self, monkeypatch, ops, machine=SimpleNamespace(description="Haas VF-2")):
        cam = SimpleNamespace(setups=_NamedCollection([_machined_setup(ops, machine=machine)]),
                              ncPrograms=_NamedCollection([]))
        monkeypatch.setattr(cc, "get_cam", lambda: (cam, None))
        sig, err = cc.live_readiness()
        assert err is None
        return sig

    def test_zero_warnings_keeps_the_plain_ready_verdict(self, monkeypatch,
                                                          operation_cast_passthrough):
        sig = self._sig(monkeypatch, [_tally_op("Face1"), _tally_op("Face2")])
        assert sig["warnings"] == 0
        assert sig["readiness"] == "2 of 2 active ops valid - ready to post."
        assert sig["samples"]["warning"] is None

    def test_one_warning_demotes_the_verdict_and_names_the_op_and_its_line(
            self, monkeypatch, operation_cast_passthrough):
        # the exact boundary the whole change turns on: 1 warning, everything else valid.
        sig = self._sig(monkeypatch, [_tally_op("Face1"),
                                      _tally_op("2D Contour1", warning=True)])
        assert sig["warnings"] == 1
        assert "ready to post." not in sig["readiness"]      # never the plain verdict
        assert "postable" in sig["readiness"]                # a warning does not hard-block
        assert "1 with WARNINGS" in sig["readiness"]
        assert "2D Contour1" in sig["readiness"]
        assert "Contour Selection" in sig["readiness"]
        assert sig["samples"]["warning"]["name"] == "2D Contour1"

    def test_an_errored_job_still_reads_BLOCKER_not_the_warning_verdict(self, monkeypatch,
                                                                        operation_cast_passthrough):
        # an error outranks: the warning wording must not displace the verdict that stops the post.
        sig = self._sig(monkeypatch, [_tally_op("Drill1", error=True),
                                      _tally_op("2D Contour1", warning=True)])
        assert sig["readiness"].startswith("BLOCKER:")
        assert sig["warnings"] == 1                          # still reported as a number

    def test_a_stale_job_with_a_warning_keeps_the_generate_verdict(self, monkeypatch,
                                                                    operation_cast_passthrough):
        # not-all-valid already refuses to say ready, so it keeps its own next-action wording.
        sig = self._sig(monkeypatch, [_tally_op("Adaptive1", state=1, warning=True),
                                      _tally_op("Face1")])
        assert "run cam_generate" in sig["readiness"]

    def test_a_suppressed_warned_op_leaves_the_plain_ready_verdict(self, monkeypatch,
                                                                    operation_cast_passthrough):
        # boundary on the other side: a warning that does NOT count must not demote anything.
        sig = self._sig(monkeypatch, [_tally_op("Face1"),
                                      _tally_op("Off1", state=2, suppressed=True, warning=True)])
        assert sig["warnings"] == 0
        assert sig["readiness"] == "1 of 1 active ops valid - ready to post."

    def test_an_unreadable_warning_TEXT_still_names_the_op_and_says_so(
            self, monkeypatch, operation_cast_passthrough):
        sig = self._sig(monkeypatch, [_tally_op("Contour1", warning=True, warning_text="")])
        assert sig["warnings"] == 1
        assert "ready to post." not in sig["readiness"]      # the demotion does not depend on text
        assert "'Contour1'" in sig["readiness"] and "unreadable" in sig["readiness"]

    def test_an_unnamed_warning_op_points_at_the_read_rather_than_quoting_a_blank_name(
            self, monkeypatch, operation_cast_passthrough):
        sig = self._sig(monkeypatch, [_tally_op(None, warning=True)])
        assert sig["warnings"] == 1
        assert "cam_get(include=['operations'])" in sig["readiness"]
        assert "''" not in sig["readiness"]


class TestOperationsSummaryWarningVerdict:
    """cam_get's operations slice ends on the SAME shared verdict live_readiness does, so the two
    surfaces cannot disagree about one job: a warned op reads isToolpathValid True with hasToolpath
    False (measured), which counts as postable here and would otherwise ride inside a plain
    'ready to post'. The per-op ROWS are untouched by the demotion - only the sentence changes."""

    def _rec(self, name, **kw):
        base = {"name": name, "state": "valid", "toolpath_valid": True, "is_suppressed": False,
                "has_error": False, "has_warning": False, "blocked_by": []}
        base.update(kw)
        return base

    @pytest.fixture(autouse=True)
    def _manufacture(self, monkeypatch):
        monkeypatch.setattr(cr, "validity_basis", lambda: "manufacture_verified")

    def test_zero_warnings_keeps_the_plain_ready_verdict(self):
        summary = cr._operations_summary([self._rec("Face1"), self._rec("Adaptive1")])
        assert summary["readiness"] == "2 of 2 active ops have valid toolpaths - ready to post."

    def test_one_warning_demotes_the_verdict_and_names_the_op_and_its_first_line(self):
        # the exact boundary: 1 counted warning, everything else valid and unblocked.
        summary = cr._operations_summary([
            self._rec("Face1"),
            self._rec("2D Contour1", has_warning=True,
                      warning="Contour Selection: contours are missing selections.\nSECONDLINE")])
        readiness = summary["readiness"]
        assert "ready to post." not in readiness            # never the plain verdict
        assert "postable" in readiness                      # a warning does not hard-block
        assert "1 with WARNINGS" in readiness
        assert "2D Contour1" in readiness
        assert "contours are missing selections" in readiness
        assert "SECONDLINE" not in readiness                # only the FIRST warning line rides
        assert summary["exceptions"] == []                  # the warning still blocks nothing
        assert summary["active_count"] == 2                 # the rows/tallies are unchanged

    def test_a_suppressed_warned_op_leaves_the_plain_ready_verdict(self):
        # the other boundary: a warning that does not count must demote nothing.
        summary = cr._operations_summary([
            self._rec("Face1"),
            self._rec("Off1", state="suppressed", is_suppressed=True, toolpath_valid=False,
                      has_warning=True, warning="empty toolpath")])
        assert summary["readiness"] == "1 of 1 active ops have valid toolpaths - ready to post."

    def test_an_errored_ops_warning_does_not_displace_the_exception_verdict(self):
        summary = cr._operations_summary([
            self._rec("Face1"),
            self._rec("Drill1", has_error=True, has_warning=True, warning="also warned")])
        assert "ready to post" not in summary["readiness"]
        assert "resolve the exceptions" in summary["readiness"]

    def test_an_unverified_workspace_still_gives_no_toolpath_verdict_at_all(self, monkeypatch):
        monkeypatch.setattr(cr, "validity_basis", lambda: "unverified_design_workspace")
        summary = cr._operations_summary([self._rec("Contour1", has_warning=True, warning="w")])
        assert "ready to post" not in summary["readiness"]
        assert "Manufacture" in summary["readiness"]


class TestSharedReadyVerdict:
    """The ONE builder all three readiness surfaces end on - pinned directly, because it is what
    makes them agree."""

    def test_no_warnings_is_the_plain_verdict(self):
        assert cc.ready_verdict("3 of 3 active ops valid", 0, None) == \
            "3 of 3 active ops valid - ready to post."

    def test_one_warning_demotes_and_names_the_sample(self):
        out = cc.ready_verdict("3 of 3 active ops valid", 1,
                               {"name": "Contour1", "warning": "empty toolpath"})
        assert "ready to post." not in out
        assert "1 with WARNINGS" in out and "'Contour1'" in out and "empty toolpath" in out

    def test_an_unnamed_sample_points_at_the_read_rather_than_quoting_a_blank_name(self):
        out = cc.ready_verdict("1 of 1 active ops valid", 1, {"name": None, "warning": "w"})
        assert "cam_get(include=['operations'])" in out and "''" not in out


# --- setup_blockers / the blocked-setup verdict: ONE input set for 'is this job postable' ---
#
# A setup's blocked_by and the readiness verdict were computed from different inputs, so a poll read
# "ready to post" on a setup cam_get flagged blocked_by ["no_machine_selected"]. The fixture that
# discriminates is a setup with NOTHING wrong at op level - every op valid, no warnings, no errors -
# and no machine: a verdict reading only the op tally says "ready to post" on it.

def _machine(description="Haas VF-2"):
    """A machine off a setup - machine_label reads its description."""
    return FakeMachine(description=description)


class TestSetupBlockers:
    """setup_blockers - the ONE read of a setup's own post prerequisites."""

    def test_a_setup_with_no_machine_is_blocked_by_that(self):
        assert cc.setup_blockers(SimpleNamespace(machine=None)) == ["no_machine_selected"]

    def test_a_setup_whose_machine_property_raises_is_blocked_not_cleared(self):
        class _Raises(FakeSetup):
            @property
            def machine(self):
                raise RuntimeError("machine cannot be read")
        # an unreadable machine is not a read machine - it may never clear the block by default
        assert cc.setup_blockers(_Raises("Setup1")) == ["no_machine_selected"]

    def test_an_assigned_machine_clears_the_block(self):
        assert cc.setup_blockers(SimpleNamespace(machine=_machine())) == []

    def test_a_machine_with_no_readable_label_still_counts_as_assigned(self):
        # machine_label falls back to '(unnamed machine)' for a Machine carrying no description or
        # vendor/model - the machine IS assigned, so nothing here may report it as unselected.
        m = SimpleNamespace(description=None, vendor=None, model=None)
        assert cc.machine_label(m) == "(unnamed machine)"
        assert cc.setup_blockers(SimpleNamespace(machine=m)) == []

    def test_blocked_setup_records_names_only_the_blocked_ones(self):
        rows = cc.blocked_setup_records([
            SimpleNamespace(name="Top", machine=_machine()),
            SimpleNamespace(name="Bottom", machine=None),
            SimpleNamespace(name="Side", machine=None)])
        assert rows == [{"name": "Bottom", "blocked_by": ["no_machine_selected"]},
                        {"name": "Side", "blocked_by": ["no_machine_selected"]}]

    def test_no_blocked_setups_is_an_empty_list_not_a_null(self):
        assert cc.blocked_setup_records([SimpleNamespace(name="Top", machine=_machine())]) == []


class TestBlockedReadyVerdict:
    """ready_verdict consults the blockers: op state alone cannot earn 'ready to post'."""

    def _blocked(self, *names):
        return [{"name": n, "blocked_by": ["no_machine_selected"]} for n in names]

    def test_a_fully_valid_warning_free_scope_is_still_not_ready_while_a_setup_is_blocked(self):
        out = cc.ready_verdict("3 of 3 active ops valid", 0, None, self._blocked("Setup1"))
        assert "- ready to post." not in out                 # the plain verdict is withheld
        assert "'ready to post' is NOT established" in out   # and the withholding is stated
        assert "blocked_by" in out and "no_machine_selected" in out and "'Setup1'" in out

    def test_the_remedy_is_named_in_the_tools_own_vocabulary(self):
        out = cc.ready_verdict("3 of 3 active ops valid", 0, None, self._blocked("Setup1"))
        assert "cam_edit_setup assigns a machine." in out

    def test_an_unknown_blocker_code_names_no_remedy_it_cannot_back(self):
        out = cc.ready_verdict("1 of 1 active ops valid", 0, None,
                               [{"name": "S1", "blocked_by": ["some_future_code"]}])
        assert "some_future_code" in out and "cam_edit_setup" not in out

    def test_no_remedy_is_offered_for_a_blocker_the_sentence_did_not_print(self):
        # the sentence names the FIRST blocked setup and counts the rest, so a remedy gathered
        # across every row would name a fix for a code the caller never saw. Row 2's code is the
        # only one with a remedy on file, and row 2 is not printed - so no remedy rides.
        out = cc.ready_verdict("1 of 1 active ops valid", 0, None,
                               [{"name": "S1", "blocked_by": ["some_future_code"]},
                                {"name": "S2", "blocked_by": ["no_machine_selected"]}])
        assert "some_future_code" in out and "and 1 more" in out
        assert "cam_edit_setup" not in out          # the unprinted row's remedy stays unprinted
        assert "S2" not in out

    def test_one_blocked_setup_is_named_without_a_remainder_clause(self):
        # boundary: len(blocked) == 1 -> no "and N more" tail at all
        out = cc.ready_verdict("1 of 1 active ops valid", 0, None, self._blocked("Setup1"))
        assert "more" not in out

    def test_two_blocked_setups_name_the_first_and_count_the_rest(self):
        out = cc.ready_verdict("1 of 1 active ops valid", 0, None, self._blocked("Setup1", "Setup2"))
        assert "2 setup(s) carry blocked_by" in out
        assert "'Setup1'" in out and "and 1 more" in out
        assert "Setup2" not in out                           # the sentence stays one line

    def test_an_empty_blocked_list_is_the_plain_verdict_not_a_demotion(self):
        # boundary on the other side of the same branch: [] must behave exactly like None
        assert cc.ready_verdict("2 of 2 active ops valid", 0, None, []) == \
            cc.ready_verdict("2 of 2 active ops valid", 0, None) == \
            "2 of 2 active ops valid - ready to post."

    def test_a_blocked_setup_whose_name_did_not_read_is_described_not_quoted_blank(self):
        out = cc.ready_verdict("1 of 1 active ops valid", 0, None,
                               [{"name": None, "blocked_by": ["no_machine_selected"]}])
        assert "''" not in out and "did not read" in out

    def test_the_warning_count_still_rides_a_blocked_verdict(self):
        # a blocked job may also carry warnings; the blocker outranks, but the warnings are not lost
        out = cc.ready_verdict("2 of 2 active ops valid", 1, {"name": "C1", "warning": "w"},
                               self._blocked("Setup1"))
        assert "'ready to post' is NOT established" in out
        assert "1 active op(s) also carry warnings" in out

    def test_a_blocked_verdict_with_no_warnings_says_nothing_about_warnings(self):
        out = cc.ready_verdict("2 of 2 active ops valid", 0, None, self._blocked("Setup1"))
        assert "warning" not in out


class TestLiveReadinessConsumesSetupBlockers:
    """live_readiness and cam_get's setups projection answer 'is this postable' off ONE input set."""

    def _cam(self, setups):
        return SimpleNamespace(setups=_NamedCollection(setups), ncPrograms=_NamedCollection([]))

    def _sig(self, monkeypatch, setups):
        monkeypatch.setattr(cc, "get_cam", lambda: (self._cam(setups), None))
        sig, err = cc.live_readiness()
        assert err is None
        return sig

    def test_a_clean_but_machine_less_document_never_reads_ready_to_post(
            self, monkeypatch, operation_cast_passthrough):
        # THE BITE: two valid ops, no warnings, no errors - a verdict built from the op tally alone
        # reads "2 of 2 active ops valid - ready to post." on this exact document.
        sig = self._sig(monkeypatch, [_machined_setup([_tally_op("Face1"), _tally_op("Face2")],
                                                      machine=None)])
        assert sig["valid"] == 2 and sig["errored"] == 0 and sig["warnings"] == 0
        assert "- ready to post." not in sig["readiness"]
        assert sig["setups_blocked"] == [{"name": "Setup1",
                                          "blocked_by": ["no_machine_selected"]}]

    def test_the_same_document_with_a_machine_reads_plainly_ready(
            self, monkeypatch, operation_cast_passthrough):
        sig = self._sig(monkeypatch, [_machined_setup([_tally_op("Face1"), _tally_op("Face2")])])
        assert sig["setups_blocked"] == []
        assert sig["setups_errored"] == 0
        assert sig["readiness"] == "2 of 2 active ops valid - ready to post."

    def test_a_setup_level_fault_blocks_the_job_its_operations_alone_read_as_ready(
            self, monkeypatch, operation_cast_passthrough):
        # the discriminating pair to the test above: the same two valid ops, and the BLOCKER comes
        # from the setup's own fault channel - a verdict built from the op tally alone misses it.
        sig = self._sig(monkeypatch, [_machined_setup([_tally_op("Face1"), _tally_op("Face2")],
                                                      error="Stock is smaller than the model.\nfix it")])
        assert sig["valid"] == 2 and sig["errored"] == 0
        assert sig["setups_errored"] == 1
        assert sig["samples"]["setup"] == {"name": "Setup1",
                                           "error": "Stock is smaller than the model."}
        assert sig["readiness"] == ("BLOCKER: 1 setup(s) have errors - the job will not post "
                                    "until fixed.")

    def test_only_the_blocked_setup_of_several_is_named(self, monkeypatch,
                                                        operation_cast_passthrough):
        sig = self._sig(monkeypatch, [_machined_setup([_tally_op("Face1")], name="Top"),
                                      _machined_setup([_tally_op("Face2")], name="Bottom",
                                                      machine=None)])
        assert sig["setups_blocked"] == [{"name": "Bottom",
                                          "blocked_by": ["no_machine_selected"]}]
        assert "'Bottom'" in sig["readiness"] and "Top" not in sig["readiness"]

    def test_an_errored_job_keeps_the_BLOCKER_verdict_and_still_publishes_the_blockers(
            self, monkeypatch, operation_cast_passthrough):
        # an op error outranks the wording, but the machine-readable list is still there to branch on
        sig = self._sig(monkeypatch, [_machined_setup([_tally_op("Drill1", error=True)],
                                                      machine=None)])
        assert sig["readiness"].startswith("BLOCKER:")
        assert sig["setups_blocked"] == [{"name": "Setup1",
                                          "blocked_by": ["no_machine_selected"]}]

    def test_the_setups_slice_and_the_verdict_report_the_SAME_blocked_by(
            self, monkeypatch, operation_cast_passthrough):
        # the one-input-set proof: the codes cam_get publishes per setup are the codes the verdict
        # was built from - the two surfaces cannot report different answers about one setup.
        setups = [_machined_setup([_tally_op("Face1")], machine=None)]
        monkeypatch.setattr(cc, "get_cam", lambda: (self._cam(setups), None))
        monkeypatch.setattr(cr, "get_cam", lambda: (self._cam(setups), None))
        sig, _err = cc.live_readiness()
        slice_rec = _payload(cr.get_cam_setups_handler())["setups"][0]
        assert slice_rec["blocked_by"] == ["no_machine_selected"]
        assert [r["blocked_by"] for r in sig["setups_blocked"]] == [slice_rec["blocked_by"]]


class TestOperationsSummaryConsumesSetupBlockers:
    """cam_get's own operations summary is the third surface on the shared verdict."""

    @pytest.fixture(autouse=True)
    def _manufacture(self, monkeypatch):
        monkeypatch.setattr(cr, "validity_basis", lambda: "manufacture_verified")

    def _rec(self, name):
        return {"name": name, "state": "valid", "toolpath_valid": True, "is_suppressed": False,
                "has_error": False, "has_warning": False, "blocked_by": []}

    def test_a_blocked_setup_demotes_the_operations_summary_verdict(self):
        summary = cr._operations_summary(
            [self._rec("Face1")], [{"name": "S1", "blocked_by": ["no_machine_selected"]}])
        assert "- ready to post." not in summary["readiness"]
        assert "no_machine_selected" in summary["readiness"]
        assert summary["exceptions"] == []          # a SETUP blocker is not an op exception

    def test_no_blockers_keeps_the_plain_summary_verdict(self):
        summary = cr._operations_summary([self._rec("Face1")], [])
        assert summary["readiness"] == "1 of 1 active ops have valid toolpaths - ready to post."

    def _postable_op(self, name):
        """An op row the summary counts as good to post - tool selected, toolpath valid, no fault -
        so ONLY a setup-level blocker can demote a verdict built over it."""
        return SimpleNamespace(name=name, tool=SimpleNamespace(description="6mm flat"),
                               strategy="adaptive", operationState=0, hasToolpath=True,
                               isToolpathValid=True, isGenerating=False, isSuppressed=False,
                               isOptional=False, hasWarning=False, hasError=False, messageLog="")

    def test_the_operations_handler_feeds_each_setups_own_blockers(self, install,
                                                                    operation_cast_passthrough):
        # end to end through cam_get's slice: the machine-less setup is demoted, the machined one
        # beside it is not - so the blockers are read PER setup, not once for the document.
        blocked = FakeSetup("Bottom", ops=[self._postable_op("Face2")])
        blocked.machine = None
        machined = FakeSetup("Top", ops=[self._postable_op("Face1")])
        machined.machine = _machine()
        install(FakeCAM([machined, blocked]))
        out = _payload(cr.get_cam_operations_handler())
        by_name = {s["setup"]: s["summary"]["readiness"] for s in out["setups"]}
        assert by_name["Top"] == "1 of 1 active ops have valid toolpaths - ready to post."
        assert "- ready to post." not in by_name["Bottom"]
        assert "no_machine_selected" in by_name["Bottom"]


class TestOwningSetup:
    """owning_setup - the parent-chain walk a scoped verdict reads its setup blockers through."""

    def test_a_setup_node_is_its_own_owner(self, install):
        setup = FakeSetup("S1")
        install(FakeCAM([setup]))
        node = cc.walk_cam_tree(cc.get_cam()[0])[0]
        assert node.kind == "setup" and cc.owning_setup(node) is setup

    def test_an_op_nested_two_folders_deep_still_resolves_to_the_setup(self, install):
        inner = FakeCAMFolder("Inner", ops=[FakeOperation("Face1")])
        setup = FakeSetup("S1", folders=[FakeCAMFolder("Outer", folders=[inner])])
        install(FakeCAM([setup]))
        op_node = next(n for n in cc.walk_cam_tree(cc.get_cam()[0]) if n.name == "Face1")
        assert cc.owning_setup(op_node) is setup

    def test_a_parentless_non_setup_node_owns_no_setup(self):
        # nothing is invented for a node the walk did not build - the caller reads "no blockers",
        # never some other setup's.
        orphan = cc.CamNode(object(), "operation", "Face1", None, "Face1", None)
        assert cc.owning_setup(orphan) is None


# --- _attach_setup_invalidation: the per-setup op_states rollup + WHY the setup is stale ---

def _rollup_op(name, state=0, warning=False, log=""):
    """One operation as the per-setup rollup reads it - messageLog is where the WHY is parsed from."""
    op = FakeOperation(name, operation_state=state, has_warning=warning)
    op.messageLog = log
    return op


class TestSetupInvalidationRollup:
    def _rec(self, install, ops):
        install(FakeCAM([FakeSetup("S1", ops=ops)]))
        return _payload(cr.get_cam_setups_handler())["setups"][0]

    def test_a_clean_setup_carries_only_the_valid_bucket(self, install, operation_cast_passthrough):
        rec = self._rec(install, [_rollup_op("A"), _rollup_op("B")])
        assert rec["op_states"] == {"valid": 2}
        assert "invalidation_reasons" not in rec and "machine_out_of_date" not in rec

    def test_an_op_whose_state_did_not_read_gets_its_own_bucket(self, install,
                                                                operation_cast_passthrough):
        # the per-setup rollup counts the same buckets the rows do: an unread op added to 'valid'
        # is how a setup reads "ready" while one of its operations answered nothing.
        rec = self._rec(install, [_rollup_op("A"), _rollup_op("B", state=None)])
        assert rec["op_states"] == {"valid": 1, "unread": 1}

    def test_a_setup_with_no_operations_carries_no_tally_at_all(self, install,
                                                                operation_cast_passthrough):
        install(FakeCAM([FakeSetup("S1")]))
        rec = _payload(cr.get_cam_setups_handler())["setups"][0]
        assert "op_states" not in rec
        assert rec["operation_count"] == 0 and rec["folder_count"] == 0

    def test_the_distinct_reasons_are_rolled_up_across_the_stale_ops(self, install,
                                                                     operation_cast_passthrough):
        wcs = "2024-01-01 I Invalidated: Design changed: WCS origin"
        rec = self._rec(install, [_rollup_op("A", state=1, log=wcs),
                                  _rollup_op("B", state=1, log=wcs),
                                  _rollup_op("C", state=1,
                                             log="2024-01-01 I Invalidated: Tool changed")])
        assert rec["op_states"] == {"out_of_date": 3}
        assert rec["invalidation_reasons"] == ["Design changed: WCS origin", "Tool changed"]

    def test_a_warning_is_an_overlay_so_the_buckets_still_sum_to_the_op_total(
            self, install, operation_cast_passthrough):
        rec = self._rec(install, [_rollup_op("A", warning=True), _rollup_op("B")])
        assert rec["op_states"]["valid"] == 2        # the warned op stays in its lifecycle bucket
        assert rec["op_states"]["warning"] == 1

    def test_a_machine_change_is_flagged_setup_wide_and_is_not_a_reason(self, install,
                                                                        operation_cast_passthrough):
        rec = self._rec(install, [_rollup_op("A", state=1,
                                             log="2024-01-01 I External changed: machine.limits")])
        assert rec["machine_out_of_date"] is True
        assert "invalidation_reasons" not in rec

    def test_a_valid_ops_stale_log_is_never_read_for_reasons(self, install,
                                                             operation_cast_passthrough):
        # reasons are only meaningful for an OUT-OF-DATE op; a valid op's leftover log would
        # otherwise report a setup as stale for a change that has already been generated in.
        rec = self._rec(install, [_rollup_op("A", state=0,
                                             log="2024-01-01 I Invalidated: Design changed: WCS")])
        assert rec["op_states"] == {"valid": 1} and "invalidation_reasons" not in rec

    def test_a_setup_with_no_machine_is_blocked_by_that(self, install, operation_cast_passthrough):
        rec = self._rec(install, [_rollup_op("A")])
        assert rec["blocked_by"] == ["no_machine_selected"]

    def test_a_failed_setup_read_is_an_error_not_a_partial_ok(self, install):
        install(SimpleNamespace(setups=SimpleNamespace(count=2, item=_unreadable_item)))
        res = cr.get_cam_setups_handler()
        assert res["isError"] is True
        assert "Could not read setups" in res["message"] and "setup read failed" in res["message"]

    def test_a_non_operation_node_is_not_tallied(self, install, monkeypatch):
        # allOperations can hand back a node the Operation cast drops; tallying it would put a
        # bucket count in op_states that no operation stands behind.
        monkeypatch.setattr(adsk.cam.Operation, "cast",
                            lambda x: x if getattr(x, "operationState", None) is not None else None)
        install(FakeCAM([FakeSetup("S1", ops=[_rollup_op("A"), SimpleNamespace(name="Holes")])]))
        rec = _payload(cr.get_cam_setups_handler())["setups"][0]
        assert rec["op_states"] == {"valid": 1}


# --- setup_wcs: the read-back of what cam_edit_setup's 'wcs' binding did ---
# The WCS lives in Setup.parameters: a mode parameter answers its choice string, a geometry binding
# answers an iterable of the entities it is bound to.

class TestSetupWcs:
    def _rec(self, install, params):
        install(FakeCAM([FakeSetup("Setup1", parameters=params)]))
        return _payload(cr.get_cam_setups_handler())["setups"][0]

    def test_a_geometry_bound_wcs_publishes_its_modes_and_entities(self, install):
        rec = self._rec(install, wcs_params(
            origin_mode="point", orientation_mode="axesZX",
            origin=[("adsk::fusion::JointOrigin", "StockCenter")],
            z_axis=[("adsk::fusion::BRepFace", None)]))
        assert rec["wcs"] == {
            "origin_mode": "point", "orientation_mode": "axesZX",
            "origin_entities": [{"type": "JointOrigin", "name": "StockCenter"}],
            # a BRepFace carries no readable name - the type still identifies what is bound
            "orientation_z_entities": [{"type": "BRepFace"}]}

    def test_an_unbound_wcs_carries_the_modes_and_no_entity_lists(self, install):
        # the parameters are PRESENT and bound to nothing; an empty list is omitted, not published
        # as a key an agent would read as "there is a binding here".
        rec = self._rec(install, wcs_params(origin_mode="point",
                                            orientation_mode="modelOrientation",
                                            origin=[], z_axis=[]))
        assert rec["wcs"] == {"origin_mode": "point", "orientation_mode": "modelOrientation"}

    def test_every_bound_entity_is_listed_not_just_the_first(self, install):
        rec = self._rec(install, wcs_params(
            origin_mode="point",
            origin=[("adsk::fusion::JointOrigin", "StockCenter"),
                    ("adsk::fusion::BRepVertex", "Corner")]))
        assert rec["wcs"]["origin_entities"] == [{"type": "JointOrigin", "name": "StockCenter"},
                                                 {"type": "BRepVertex", "name": "Corner"}]

    def test_one_unreadable_parameter_does_not_cost_the_rest(self, install):
        # only wcs_orientation_mode reads; the block still reports what it could read rather than
        # collapsing to null and claiming nothing is known about the WCS.
        rec = self._rec(install, wcs_params(orientation_mode="modelOrientation"))
        assert rec["wcs"] == {"orientation_mode": "modelOrientation"}

    def test_an_unreadable_parameter_collection_makes_the_whole_block_null(self, install):
        def _boom(_name):
            raise RuntimeError("parameters unavailable")
        rec = self._rec(install, SimpleNamespace(itemByName=_boom))
        assert rec["wcs"] is None                  # nothing about the WCS can be claimed

    def test_a_setup_whose_parameters_do_not_read_is_still_a_full_row(self, install):
        rec = self._rec(install, None)
        assert rec["wcs"] is None
        assert rec["name"] == "Setup1" and rec["operation_count"] == 0


class TestInvalidationReasonsBlankLines:
    def test_blank_and_carriage_returned_lines_are_skipped(self):
        op = SimpleNamespace(messageLog="\r\n \r\n2024-01-01 I Invalidated: Design changed: WCS\r\n\r\n")
        reasons, param_changes, machine_changed = cr._invalidation_reasons(op)
        assert reasons == ["Design changed: WCS"]
        assert param_changes == 0 and machine_changed is False

    def test_an_ordinary_log_line_is_neither_a_reason_nor_a_parameter_change(self):
        # the log carries plenty that is not an invalidation - counting it would inflate
        # invalidation_param_changes and put noise in the reasons list.
        op = SimpleNamespace(messageLog=("2024-01-01 I Generating toolpath\n"
                                         "2024-01-01 I Toolpath generated in 1.2s\n"
                                         "2024-01-01 I Invalidated: Stock changed"))
        reasons, param_changes, machine_changed = cr._invalidation_reasons(op)
        assert reasons == ["Stock changed"]
        assert param_changes == 0 and machine_changed is False


# --- _op_blocked_by / _operations_summary: the verified reason codes an agent branches on ---

class TestOpBlockedBy:
    def test_a_suppressed_op_blocks_nothing_even_with_no_tool_and_a_stale_path(self):
        blocked, requires = cr._op_blocked_by(
            {"state": "suppressed", "is_suppressed": True, "tool": None, "is_out_of_date": True})
        assert blocked == [] and requires is None

    def test_the_bucket_answers_where_the_suppression_flag_did_not_read(self):
        # the flag RAISED, so the row's own is_suppressed key is null; the bucket carries the
        # Suppressed state, and a parked op is not reported as blocked on a tool it will never need
        blocked, requires = cr._op_blocked_by(
            {"state": "suppressed", "is_suppressed": None, "tool": None, "is_out_of_date": True})
        assert blocked == [] and requires is None

    def test_an_errored_row_is_still_read_for_blockers_whatever_state_rode_under_it(self):
        # the bucket is the ONE signal: while it says 'error', the suppressed short-circuit stays
        # shut, so a fault's own prerequisites are still reported
        blocked, requires = cr._op_blocked_by(
            {"state": "error", "is_suppressed": None, "tool": None, "is_out_of_date": False})
        assert blocked == ["tool_unselected"] and requires is None

    def test_an_op_with_no_tool_is_blocked_on_that(self):
        blocked, requires = cr._op_blocked_by(
            {"state": "valid", "tool": None, "is_out_of_date": False})
        assert blocked == ["tool_unselected"] and requires is None

    def test_a_stale_op_names_the_tool_and_workspace_that_unblock_it(self):
        blocked, requires = cr._op_blocked_by(
            {"state": "out_of_date", "tool": "flat 10mm", "is_out_of_date": True})
        assert blocked == ["toolpath_out_of_date"]
        assert requires == {"tool": "cam_generate", "workspace": "Manufacture"}


class TestOperationsSummarySuppressed:
    def test_a_suppressed_op_is_tallied_but_never_active_and_never_an_exception(self, monkeypatch):
        monkeypatch.setattr(cr, "validity_basis", lambda: "manufacture_verified")
        records = [{"name": "Face1", "state": "valid", "toolpath_valid": True,
                    "is_suppressed": False, "has_error": False, "blocked_by": []},
                   {"name": "Off1", "state": "suppressed", "toolpath_valid": False,
                    "is_suppressed": True, "has_error": False, "blocked_by": []}]
        summary = cr._operations_summary(records)
        assert summary["states"] == {"valid": 1, "suppressed": 1}
        assert summary["active_count"] == 1          # the suppressed op is excluded from posting
        assert summary["exceptions"] == []
        assert summary["readiness"] == ("1 of 1 active ops have valid toolpaths - ready to post.")


class TestOperationsPayloadAgreesOnOneParkedOp:
    """A parked op whose isSuppressed flag RAISES - the Suppressed state is the only half that
    answers it. Every surface of the operations payload has to answer alike: the state tally, the
    active census, and the row's own comparison."""

    def test_the_suppressed_bucket_and_the_active_census_never_both_count_one_op(
            self, install, operation_cast_passthrough):
        install(FakeCAM([_OpSetup("S1", [_UnreadableSuppressionFlagOp()])]))
        summary = _payload(cr.get_cam_operations_handler())["setups"][0]["summary"]
        assert summary["states"] == {"suppressed": 1}
        assert summary["active_count"] == 0
        # the partition: every counted row lands in exactly one of the two
        assert sum(summary["states"].values()) == (summary["active_count"]
                                                   + summary["states"].get("suppressed", 0))

    def test_the_row_withholds_the_spindle_comparison_rather_than_computing_one(
            self, install, operation_cast_passthrough):
        # a parked op is excluded from posting, so its comparison is WITHHELD - the marker the
        # operations note promises, never a computed flag
        install(FakeCAM([_OpSetup("S1", [_UnreadableSuppressionFlagOp()])]))
        row = _payload(cr.get_cam_operations_handler())["setups"][0]["operations"][0]
        assert row["state"] == "suppressed"
        assert row["spindle_check"] == "suppressed_not_compared"
        assert "spindle_over_machine_max" not in row
        assert row["blocked_by"] == []

    def test_a_withheld_spindle_comparison_only_ever_rides_a_suppressed_row(
            self, install, operation_cast_passthrough):
        # The coherence invariant every row surface holds to: _OPERATIONS_NOTE teaches
        # 'suppressed_not_compared' as excluded-from-posting, so no row may carry it while its own
        # state and is_suppressed deny suppression. This shape - the Suppressed state under a flag
        # that RAISED, with a fault beside it - is REPRESENTABLE rather than live-manufacturable:
        # measured, an op reading hasError True with state NoToolpath and a drive-surface error,
        # then suppressed, reads isSuppressed True, state Suppressed, hasError False and error '' -
        # the platform clears the fault channel on suppression, so the direct route never lands
        # here. The guard defends the representable shape.
        install(FakeCAM([_OpSetup("S1", [_UnreadableSuppressionFlagOp(has_error=True)])]))
        row = _payload(cr.get_cam_operations_handler())["setups"][0]["operations"][0]
        assert row["state"] == "error"               # the fault outranks the state half
        assert row["is_suppressed"] is None          # the flag raised; nothing read it
        assert row.get("spindle_check") != "suppressed_not_compared"
        assert row["blocked_by"] == ["tool_unselected"]     # read for blockers, not short-circuited


# --- _operation_summary: the per-op disclosure a machinist reads (text, not just bools) ---

class TestOperationSummaryDisclosure:
    def _stale_op(self):
        return SimpleNamespace(
            name="Adaptive1", tool=SimpleNamespace(description="flat 10mm"), strategy="adaptive",
            operationState=1, hasWarning=True, warning=" Spindle speed is larger than supported ",
            hasError=True, error="Top height must not be below the bottom height",
            hasToolpath=True, isToolpathValid=False, isGenerating=False, isSuppressed=False,
            isOptional=False,
            messageLog=("2024-01-01 I Invalidated: Design changed: WCS origin\n"
                        "2024-01-01 I used a different value for parameter 'tool_feedCutting'\n"
                        "2024-01-01 I used a different value for parameter 'tool_spindleSpeed'\n"
                        "2024-01-01 I External changed: machine.limits"))

    def test_a_stale_errored_op_publishes_the_texts_and_the_reasons(self, install,
                                                                    operation_cast_passthrough):
        install(FakeCAM([_OpSetup("S1", [self._stale_op()])]))
        out = _payload(cr.get_cam_operations_handler())
        rec = out["setups"][0]["operations"][0]
        assert rec["tool"] == "flat 10mm"
        assert rec["warning"] == "Spindle speed is larger than supported"    # stripped, not a bool
        assert rec["error"] == "Top height must not be below the bottom height"
        assert rec["is_out_of_date"] is True
        assert rec["invalidation_reasons"] == ["Design changed: WCS origin"]
        assert rec["invalidation_param_changes"] == 2       # collapsed to a count, not listed
        assert rec["machine_changed"] is True
        assert rec["blocked_by"] == ["toolpath_out_of_date"]
        assert rec["requires"] == {"tool": "cam_generate", "workspace": "Manufacture"}

    def test_an_op_that_never_generated_is_out_of_date_beside_its_own_state_bucket(
            self, install, operation_cast_passthrough):
        # NoToolpath (operationState 3) and IsInvalid (1) are DIFFERENT buckets - the row publishes
        # 'no_toolpath' - and both are what cam_generate(skip_valid=true) redoes, so is_out_of_date
        # covers both. A no_toolpath row reading is_out_of_date false tells an agent looking for
        # work that an operation holding no toolpath at all needs no generating.
        op = SimpleNamespace(
            name="Rough", tool=SimpleNamespace(description="flat 10mm"), strategy="adaptive",
            operationState=adsk.cam.OperationStates.NoToolpathOperationState,
            hasWarning=False, hasError=False, hasToolpath=False, isToolpathValid=False,
            isGenerating=False, isSuppressed=False, isOptional=False, messageLog="")
        install(FakeCAM([_OpSetup("S1", [op])]))
        row = _payload(cr.get_cam_operations_handler())["setups"][0]["operations"][0]
        assert row["state"] == "no_toolpath"                    # its own bucket, not 'out_of_date'
        assert row["is_out_of_date"] is True                    # and still work cam_generate redoes
        assert row["blocked_by"] == ["toolpath_out_of_date"]

    @pytest.mark.parametrize("state", [adsk.cam.OperationStates.IsInvalidOperationState,
                                       adsk.cam.OperationStates.NoToolpathOperationState])
    def test_a_suppressed_op_is_never_out_of_date_whichever_state_it_reads(
            self, install, operation_cast_passthrough, state):
        # This op's operationState is IsInvalid or NoToolpath, so the isSuppressed flag is the only
        # half that can answer suppression here: op_primary_state reads that flag before it reads
        # the state, and this conjunct is what keeps the two reads from disagreeing on one row.
        # Suppressing DISCARDS the toolpath (measured,
        # measure_api cam-suppress-discards-toolpath), and a parked op is not work
        # cam_generate(skip_valid=true) redoes: the row must not call it out of date, nor hang the
        # invalidation diagnostic that answer gates off it.
        op = SimpleNamespace(
            name="Chamfer", tool=SimpleNamespace(description="chamfer 6mm"), strategy="chamfer",
            operationState=state, hasWarning=False, hasError=False,
            hasToolpath=False, isToolpathValid=False, isGenerating=False,
            isSuppressed=True, isOptional=False,
            messageLog="2024-01-01 I Invalidated: Design changed: Model")
        install(FakeCAM([_OpSetup("S1", [op])]))
        row = _payload(cr.get_cam_operations_handler())["setups"][0]["operations"][0]
        assert row["state"] == "suppressed"
        assert row["is_out_of_date"] is False
        assert "invalidation_reasons" not in row     # the WHY rides only on an out-of-date row

    def test_the_distinct_tools_are_tallied_across_the_returned_ops(self, install,
                                                                    operation_cast_passthrough):
        flat = SimpleNamespace(description="flat 10mm")
        op1 = SimpleNamespace(name="Face1", tool=flat, operationState=0, isSuppressed=False)
        op2 = SimpleNamespace(name="Face2", tool=flat, operationState=0, isSuppressed=False)
        install(FakeCAM([_OpSetup("S1", [op1, op2])]))
        out = _payload(cr.get_cam_operations_handler())
        assert out["tools_used"] == [{"tool": "flat 10mm", "operation_count": 2}]

    def test_a_named_setup_narrows_to_that_setup_only(self, install, operation_cast_passthrough):
        install(FakeCAM([_OpSetup("S1", [object()]), _OpSetup("S2", [object(), object()])]))
        out = _payload(cr.get_cam_operations_handler(setup="s2"))     # case-insensitive exact
        assert out["setup_count"] == 1
        assert out["setups"][0]["setup"] == "S2" and len(out["setups"][0]["operations"]) == 2


# --- the operation row's WHERE and WHAT-IT-ASKS-FOR: folder path, preset, spindle vs machine ---

def _row_op(name, rpm=None, preset=None, suppressed=False):
    """One operation as the operations row reads it: its tool, the preset it runs, and the spindle
    speed it asks for."""
    op = FakeOperation(
        name, strategy="adaptive", suppressed=suppressed,
        operation_state=(adsk.cam.OperationStates.SuppressedOperationState if suppressed
                         else adsk.cam.OperationStates.IsValidOperationState))
    op.tool = FakeTool(description="flat 10mm")
    op.isOptional = False
    op.toolPreset = SimpleNamespace(name=preset) if preset else None
    op.parameters = _SpindleParams(rpm)
    return op


class TestOperationRowContext:
    def _rows(self, install, setup, machine=None, machining_times=None):
        setup.machine = machine
        install(FakeCAM([setup], machining_times=machining_times))
        out = _payload(cr.get_cam_operations_handler())
        return out["setups"][0]

    def test_an_empty_toolpath_row_is_marked_while_its_state_stays_valid(
            self, install, operation_cast_passthrough):
        # the reading this key exists to correct: state 'valid' beside has_toolpath true is what an
        # agent takes for a finished pass, and on an operation that generated EMPTY it is not one
        setup = FakeSetup("Op1", ops=[_row_op("Swarf1"), _row_op("Cut")])
        out = self._rows(install, setup,
                         machining_times={"Swarf1": 0.0, "Cut": 4.193083})
        rows = {r["name"]: r for r in out["operations"]}
        assert rows["Swarf1"]["empty_toolpath"] is True
        assert rows["Swarf1"]["state"] == "valid" and rows["Swarf1"]["has_toolpath"] is True
        assert "empty_toolpath" not in rows["Cut"]
        assert out["summary"]["empty_toolpath_count"] == 1

    def test_a_job_with_nothing_empty_carries_no_count(self, install,
                                                       operation_cast_passthrough):
        setup = FakeSetup("Op1", ops=[_row_op("Cut")])
        out = self._rows(install, setup, machining_times={"Cut": 4.193083})
        assert "empty_toolpath" not in out["operations"][0]
        assert "empty_toolpath_count" not in out["summary"]

    def test_the_count_reads_the_rows_own_key(self, install, operation_cast_passthrough):
        # two empties and one cutter: the count is the rows carrying the key, never a second
        # derivation that could select a different set
        setup = FakeSetup("Op1", ops=[_row_op("Swarf1"), _row_op("Swarf2"), _row_op("Cut")])
        out = self._rows(install, setup,
                         machining_times={"Swarf1": 0.0, "Swarf2": 0.0, "Cut": 4.193083})
        marked = [r["name"] for r in out["operations"] if r.get("empty_toolpath")]
        assert marked == ["Swarf1", "Swarf2"]
        assert out["summary"]["empty_toolpath_count"] == len(marked)

    def test_a_folder_nested_op_carries_its_breadcrumb_and_folder_name(
            self, install, operation_cast_passthrough):
        folder = FakeCAMFolder("Metric Threads Unsuppress as Needed",
                               ops=[_row_op("Thread1", suppressed=True)])
        setup = FakeSetup("Op1", ops=[_row_op("Rough")], folders=[folder])
        rows = {r["name"]: r for r in self._rows(install, setup)["operations"]}
        assert rows["Thread1"]["path"] == "Op1 / Metric Threads Unsuppress as Needed / Thread1"
        assert rows["Thread1"]["folder"] == "Metric Threads Unsuppress as Needed"
        assert rows["Thread1"]["is_suppressed"] is True     # the intent sits beside the flag

    def test_an_op_directly_under_the_setup_has_no_folder(self, install,
                                                          operation_cast_passthrough):
        setup = FakeSetup("Op1", ops=[_row_op("Rough")])
        row = self._rows(install, setup)["operations"][0]
        assert row["path"] == "Op1 / Rough" and "folder" not in row

    def test_an_op_inside_a_pattern_names_the_pattern(self, install, operation_cast_passthrough):
        # a CAMPattern is a container the walk yields exactly like a folder (kind is structural,
        # from the collection that produced it), and its name is where the op sits.
        pattern = FakeCAMFolder("Bolt Circle Pattern", ops=[_row_op("Drill")])
        setup = FakeSetup("Op1", patterns=[pattern])
        row = self._rows(install, setup)["operations"][0]
        assert row["path"] == "Op1 / Bolt Circle Pattern / Drill"
        assert row["folder"] == "Bolt Circle Pattern"

    def test_a_nested_folder_names_the_innermost_one(self, install, operation_cast_passthrough):
        inner = FakeCAMFolder("Bonus", ops=[_row_op("Extra")])
        outer = FakeCAMFolder("Finishing", folders=[inner])
        setup = FakeSetup("Op1", folders=[outer])
        row = self._rows(install, setup)["operations"][0]
        assert row["path"] == "Op1 / Finishing / Bonus / Extra" and row["folder"] == "Bonus"

    def test_the_preset_the_op_uses_is_published(self, install, operation_cast_passthrough):
        setup = FakeSetup("Op1", ops=[_row_op("Wall", preset="Wall_Finishing"),
                                      _row_op("Floor", preset="Floor_Finishing")])
        rows = {r["name"]: r for r in self._rows(install, setup)["operations"]}
        # one tool, two presets - the distinction the tool description cannot carry
        assert rows["Wall"]["tool"] == rows["Floor"]["tool"] == "flat 10mm"
        assert rows["Wall"]["preset"] == "Wall_Finishing"
        assert rows["Floor"]["preset"] == "Floor_Finishing"

    def test_an_op_without_a_preset_reads_null(self, install, operation_cast_passthrough):
        setup = FakeSetup("Op1", ops=[_row_op("Rough")])
        assert self._rows(install, setup)["operations"][0]["preset"] is None

    def test_an_op_over_the_machine_maximum_carries_both_numbers(self, install,
                                                                 operation_cast_passthrough):
        setup = FakeSetup("Op1", ops=[_row_op("Rough", rpm=24999.0)])
        rec = self._rows(install, setup, machine=_haas())
        row = rec["operations"][0]
        assert rec["machine_spindle_max_rpm"] == 12000.0
        assert row["spindle_over_machine_max"] is True
        assert row["spindle_rpm"] == 24999.0 and row["machine_max_rpm"] == 12000.0

    def test_an_op_at_the_machine_maximum_is_not_over(self, install, operation_cast_passthrough):
        setup = FakeSetup("Op1", ops=[_row_op("Rough", rpm=12000.0)])
        row = self._rows(install, setup, machine=_haas())["operations"][0]
        assert row["spindle_over_machine_max"] is False
        assert "spindle_check" not in row              # checked and fine, not unknown

    def test_a_setup_without_a_machine_marks_the_check_unavailable(self, install,
                                                                   operation_cast_passthrough):
        setup = FakeSetup("Op1", ops=[_row_op("Rough", rpm=24999.0)])
        rec = self._rows(install, setup)
        row = rec["operations"][0]
        assert row["spindle_over_machine_max"] is None          # never a coerced false
        assert row["spindle_check"] == "machine_max_unavailable"
        assert "machine_spindle_max_rpm" not in rec

    def test_an_unreadable_op_speed_marks_its_own_side(self, install, operation_cast_passthrough):
        setup = FakeSetup("Op1", ops=[_row_op("Rough")])        # no tool_spindleSpeed parameter
        row = self._rows(install, setup, machine=_haas())["operations"][0]
        assert row["spindle_over_machine_max"] is None
        assert row["spindle_check"] == "op_spindle_speed_unreadable"
        assert row["machine_max_rpm"] == 12000.0                # the readable half still rides

    def test_a_row_built_without_a_walk_node_carries_no_path(self):
        # _folder_of reads the walk's parent node; with no node there is nothing to name
        assert cr._folder_of(None) is None


class TestSpindleScopedToActiveOps:
    """A SUPPRESSED op is excluded from posting, so what it asks of the spindle never reaches the
    machine. Its row carries no comparison at all - and the marker says that is why, which is a
    different fact from 'at or under the maximum' and from 'a number would not read'."""

    def _rows(self, install, setup, machine=None):
        setup.machine = machine
        install(FakeCAM([setup]))
        return _payload(cr.get_cam_operations_handler())["setups"][0]

    def test_a_suppressed_row_is_not_compared_even_when_both_numbers_read(
            self, install, operation_cast_passthrough):
        # both halves are readable and the op asks for twice the maximum: the comparison is
        # WITHHELD, not answered - a suppressed op posts nothing, so it is no warning to act on.
        setup = FakeSetup("Op1", ops=[_row_op("Parked", rpm=24999.0, suppressed=True)])
        row = self._rows(install, setup, machine=_haas())["operations"][0]
        assert row["is_suppressed"] is True
        assert "spindle_over_machine_max" not in row
        assert row["spindle_check"] == "suppressed_not_compared"
        assert "spindle_rpm" not in row and "machine_max_rpm" not in row

    def test_an_active_row_beside_it_is_still_compared(self, install, operation_cast_passthrough):
        # the scoping is per row, not per setup: the parked op's withheld comparison must not take
        # the active op's with it.
        setup = FakeSetup("Op1", ops=[_row_op("Parked", rpm=24999.0, suppressed=True),
                                      _row_op("Cut", rpm=24999.0)])
        rows = {r["name"]: r for r in self._rows(install, setup, machine=_haas())["operations"]}
        assert "spindle_over_machine_max" not in rows["Parked"]
        assert rows["Cut"]["spindle_over_machine_max"] is True
        assert rows["Cut"]["spindle_rpm"] == 24999.0

    def test_the_summary_counts_only_the_active_rows_over_the_maximum(
            self, install, operation_cast_passthrough):
        setup = FakeSetup("Op1", ops=[_row_op("Parked", rpm=24999.0, suppressed=True),
                                      _row_op("Cut", rpm=24999.0),
                                      _row_op("Also", rpm=13000.0),
                                      _row_op("Fine", rpm=8000.0)])
        rec = self._rows(install, setup, machine=_haas())
        assert rec["summary"]["spindle_over_machine_max_count"] == 2      # NOT 3 - Parked is parked
        assert rec["summary"]["active_count"] == 3

    def test_an_op_AT_the_maximum_is_not_counted(self, install, operation_cast_passthrough):
        # the boundary the flag is built on, carried through the aggregate: at the maximum is not
        # over it, so a job whose only fast op sits exactly on the limit publishes no count at all.
        setup = FakeSetup("Op1", ops=[_row_op("Cut", rpm=12000.0)])
        rec = self._rows(install, setup, machine=_haas())
        assert rec["operations"][0]["spindle_over_machine_max"] is False
        assert "spindle_over_machine_max_count" not in rec["summary"]

    def test_an_uncomparable_row_is_not_counted_as_over(self, install,
                                                        operation_cast_passthrough):
        # null is "the comparison could not be made" - counting it would state a number the reads
        # do not support.
        setup = FakeSetup("Op1", ops=[_row_op("Cut", rpm=24999.0)])       # no machine on the setup
        rec = self._rows(install, setup)
        assert rec["operations"][0]["spindle_over_machine_max"] is None
        assert "spindle_over_machine_max_count" not in rec["summary"]

    def test_a_truthy_non_true_flag_is_not_counted_as_over(self):
        # The count is over the flag as the BOOLEAN the row publishes - true, false, or null where
        # the comparison could not be made - never "anything truthy". A marker string, a number or
        # a list in that slot is none of those three answers, so none of them may raise a count a
        # reader takes as ops asking the spindle for more than the machine allows.
        for flag in ("suppressed_not_compared", 1, [24999.0]):
            summary = cr._operations_summary([
                {"name": "Cut", "state": "valid", "toolpath_valid": True, "is_suppressed": False,
                 "has_error": False, "blocked_by": [], "spindle_over_machine_max": flag}])
            assert "spindle_over_machine_max_count" not in summary

    def test_the_note_states_the_scoping_it_applies(self, install, operation_cast_passthrough):
        # the rule is invisible to a caller unless the payload says it: an absent flag otherwise
        # reads as a comparison that came out fine.
        setup = FakeSetup("Op1", ops=[_row_op("Parked", rpm=24999.0, suppressed=True)])
        install(FakeCAM([setup]))
        note = _payload(cr.get_cam_operations_handler())["note"]
        assert "suppressed_not_compared" in note and "spindle_over_machine_max_count" in note

    def test_a_setup_name_containing_a_separator_does_not_invent_a_folder(
            self, install, operation_cast_passthrough):
        # the breadcrumb is a JOINED string: splitting 'Op1 / Rev2 / Rough' on ' / ' would name
        # 'Rev2' as the folder of an op that sits directly under the setup.
        setup = FakeSetup("Op1 / Rev2", ops=[_row_op("Rough")])
        row = self._rows(install, setup)["operations"][0]
        assert row["path"] == "Op1 / Rev2 / Rough"
        assert "folder" not in row

    def test_a_folder_name_containing_a_separator_is_reported_whole(
            self, install, operation_cast_passthrough):
        folder = FakeCAMFolder("Holes / Move and unsuppress", ops=[_row_op("Drill")])
        setup = FakeSetup("Op1", folders=[folder])
        row = self._rows(install, setup)["operations"][0]
        assert row["folder"] == "Holes / Move and unsuppress"

    def test_the_folder_comes_from_the_container_not_the_string(self):
        # the structural read: a node whose parent is a FOLDER names it; a node whose parent is the
        # SETUP names nothing, whatever the two names look like.
        setup_node = cc.CamNode(object(), "setup", "S / 1", "S / 1", "S / 1", None)
        folder_node = cc.CamNode(object(), "folder", "F", "S / 1", "S / 1 / F", setup_node)
        under_setup = cc.CamNode(object(), "operation", "Op", "S / 1", "S / 1 / Op", setup_node)
        under_folder = cc.CamNode(object(), "operation", "Op", "S / 1", "S / 1 / F / Op",
                                  folder_node)
        assert cr._folder_of(under_setup) is None
        assert cr._folder_of(under_folder) == "F"

    def test_a_container_that_slips_through_the_walk_is_skipped(self, install, monkeypatch):
        monkeypatch.setattr(adsk.cam.Operation, "cast",
                            lambda x: None if getattr(x, "name", "") == "Folderish" else x)
        setup = FakeSetup("Op1", ops=[SimpleNamespace(name="Folderish"), _row_op("Rough")])
        rec = self._rows(install, setup)
        assert [r["name"] for r in rec["operations"]] == ["Rough"]

    def test_the_rows_walk_is_seeded_the_way_tree_nodes_seeds_it(self, install, monkeypatch,
                                                                 operation_cast_passthrough):
        # _operations_in drives _walk_children itself (it owns the output list so a partial walk
        # keeps its rows), so it has to hand the walk the SAME seed tree_nodes does - the setup
        # node. Omitting it leaves a top-level op with no parent at all, which reads as "the
        # container is unknown" rather than "this op sits directly under the setup".
        seen = {}
        real = cr._walk_children

        def _record(parent, setup_name, path, out, parent_node=None):
            seen["parent_node"] = parent_node
            return real(parent, setup_name, path, out, parent_node)
        monkeypatch.setattr(cr, "_walk_children", _record)
        setup = FakeSetup("Op1", ops=[_row_op("Rough")])
        self._rows(install, setup)
        assert seen["parent_node"] is not None
        assert seen["parent_node"].kind == "setup" and seen["parent_node"].name == "Op1"

    def test_a_walk_that_raises_keeps_the_rows_it_reached(self, install, monkeypatch,
                                                          operation_cast_passthrough):
        # _walk_children appends as it goes, so a raise mid-walk leaves the nodes already collected
        real = cr._walk_children

        def _dies(parent, setup_name, path, out, parent_node=None):
            real(parent, setup_name, path, out, parent_node)
            raise RuntimeError("the CAM tree stopped answering")
        monkeypatch.setattr(cr, "_walk_children", _dies)
        setup = FakeSetup("Op1", ops=[_row_op("Rough")])
        rec = self._rows(install, setup)
        assert [r["name"] for r in rec["operations"]] == ["Rough"]
        assert rec["operations_truncated"] is True

    def test_a_row_read_that_raises_flags_the_list_incomplete(self, install, monkeypatch,
                                                              operation_cast_passthrough):
        def _boom(*_a, **_k):
            raise RuntimeError("operation read failed")
        monkeypatch.setattr(cr, "_operation_summary", _boom)
        setup = FakeSetup("Op1", ops=[_row_op("Rough")])
        rec = self._rows(install, setup)
        assert rec["operations"] == [] and rec["operations_truncated"] is True


# --- get_setup_references_handler / _references_in: X-ref occurrences -> their source docs ---

def _xref_occ(name, source_id="urn:a", version=3, ood=False, source_name="Fixture.f3d",
              url="https://fusion/a", referenced=True):
    """An occurrence placing an external component, with the source file its documentReference
    names - the chain a reference row is built from."""
    ref = FakeDocumentReference(
        data_file=FakeDataFile(name=source_name, file_id=source_id, web_url=url),
        version=version, out_of_date=ood)
    return make_occurrence(path=name, referenced=referenced, document_reference=ref)


class TestSetupReferences:
    def test_an_xref_occurrence_resolves_to_its_source_file(self, install,
                                                            occurrence_cast_passthrough):
        install(FakeCAM([_RefSetup("S1", models=[_xref_occ("Vise:1")])]))
        rec = _payload(cr.get_setup_references_handler())["setups"][0]
        assert rec["reference_count"] == 1
        assert rec["references"][0] == {
            "role": "model", "occurrence_name": "Vise:1", "source_id": "urn:a",
            "source_name": "Fixture.f3d", "version": 3, "fusion_web_url": "https://fusion/a",
            "is_out_of_date": False}

    def test_a_local_component_is_not_a_reference(self, install, occurrence_cast_passthrough):
        install(FakeCAM([_RefSetup("S1", models=[_xref_occ("Local:1", referenced=False)])]))
        rec = _payload(cr.get_setup_references_handler())["setups"][0]
        assert rec["reference_count"] == 0 and rec["references"] == []

    def test_a_body_in_the_model_list_carries_no_reference(self, install, monkeypatch):
        # models/fixtures/stockSolids also hold BRepBody/MeshBody, which the Occurrence cast drops.
        monkeypatch.setattr(adsk.fusion.Occurrence, "cast",
                            lambda x: x if getattr(x, "isReferencedComponent", None) is not None
                            else None)
        install(FakeCAM([_RefSetup("S1", models=[SimpleNamespace(name="Body1")])]))
        rec = _payload(cr.get_setup_references_handler())["setups"][0]
        assert rec["reference_count"] == 0

    def test_one_source_used_in_two_roles_is_listed_once(self, install,
                                                         occurrence_cast_passthrough):
        s = _RefSetup("S1", models=[_xref_occ("Vise:1")])
        s.fixtures = [_xref_occ("Vise:1")]
        install(FakeCAM([s]))
        rec = _payload(cr.get_setup_references_handler())["setups"][0]
        assert rec["reference_count"] == 1 and rec["references"][0]["role"] == "model"

    def test_two_distinct_sources_both_survive_the_dedupe(self, install,
                                                          occurrence_cast_passthrough):
        s = _RefSetup("S1", models=[_xref_occ("Vise:1", source_id="urn:a")])
        s.fixtures = [_xref_occ("Soft jaws:1", source_id="urn:b", source_name="Jaws.f3d")]
        install(FakeCAM([s]))
        rec = _payload(cr.get_setup_references_handler())["setups"][0]
        assert [r["source_id"] for r in rec["references"]] == ["urn:a", "urn:b"]
        assert [r["role"] for r in rec["references"]] == ["model", "fixture"]

    def test_an_out_of_date_reference_reports_its_staleness(self, install,
                                                            occurrence_cast_passthrough):
        install(FakeCAM([_RefSetup("S1", models=[_xref_occ("Vise:1", version=2, ood=True)])]))
        ref = _payload(cr.get_setup_references_handler())["setups"][0]["references"][0]
        assert ref["is_out_of_date"] is True and ref["version"] == 2

    def test_a_named_setup_narrows_to_that_setup_only(self, install, occurrence_cast_passthrough):
        install(FakeCAM([_RefSetup("S1"), _RefSetup("S2", models=[_xref_occ("Vise:1")])]))
        out = _payload(cr.get_setup_references_handler(setup="s2"))
        assert out["setup_count"] == 1 and out["setups"][0]["setup"] == "S2"
        assert out["setups"][0]["reference_count"] == 1


# --- get_tool_list_handler: the distinct cutting tools, with the ops that use each ---

class TestToolList:
    def _cam(self):
        flat = SimpleNamespace(description="flat 10mm")
        drill = SimpleNamespace(description="drill 5mm")
        return FakeCAM([
            _OpSetup("S1", [SimpleNamespace(name="Face1", tool=flat),
                            SimpleNamespace(name="Adaptive1", tool=flat)]),
            _OpSetup("S2", [SimpleNamespace(name="Face1", tool=flat),
                            SimpleNamespace(name="Drill1", tool=drill)])])

    def test_operations_are_qualified_by_setup_so_a_shared_name_is_not_a_duplicate(
            self, install, operation_cast_passthrough):
        install(self._cam())
        out = _payload(cr.get_tool_list_handler())
        assert out["tools"][0]["operations"] == ["S1 / Face1", "S1 / Adaptive1", "S2 / Face1"]
        assert out["tools"][0]["setups"] == ["S1", "S2"]

    def test_an_unread_operation_name_is_disclosed_not_joined_as_None(
            self, install, operation_cast_passthrough):
        # This list is the same join every breadcrumb takes, so it takes the same disclosure. An
        # unread name would otherwise read as a setup holding an operation NAMED 'None'.
        flat = SimpleNamespace(description="flat 10mm")
        install(FakeCAM([_OpSetup("S1", [SimpleNamespace(name=None, tool=flat),
                                         SimpleNamespace(name="Face1", tool=flat)])]))
        out = _payload(cr.get_tool_list_handler())
        rows = out["tools"][0]["operations"]
        assert rows == [f"S1 / {cc._UNREAD_SEGMENT}", "S1 / Face1"]
        # the row is a STRING either way: a bare null here would still count in operation_count
        # while addressing nothing a caller can read.
        assert all(isinstance(r, str) for r in rows)
        assert out["tools"][0]["operation_count"] == 2

    def test_an_unread_SETUP_name_still_qualifies_the_row(self, install,
                                                          operation_cast_passthrough):
        # Dropping the setup half leaves a bare 'Face1', which reads as a document with one setup.
        # The marker says which half did not read instead. 'setups' takes no marker - it is a list
        # of real setup names, and a name nothing read is not one.
        flat = SimpleNamespace(description="flat 10mm")
        install(FakeCAM([_OpSetup(None, [SimpleNamespace(name="Face1", tool=flat)])]))
        out = _payload(cr.get_tool_list_handler())
        assert out["tools"][0]["operations"] == [f"{cc._UNREAD_SEGMENT} / Face1"]
        assert out["tools"][0]["setups"] == []

    def test_the_most_used_tool_comes_first(self, install, operation_cast_passthrough):
        install(self._cam())
        out = _payload(cr.get_tool_list_handler())
        assert out["distinct_tool_count"] == 2
        assert [t["tool"] for t in out["tools"]] == ["flat 10mm", "drill 5mm"]
        assert [t["operation_count"] for t in out["tools"]] == [3, 1]

    def test_an_operation_with_no_tool_is_skipped(self, install, operation_cast_passthrough):
        install(FakeCAM([_OpSetup("S1", [SimpleNamespace(name="Manual1", tool=None)])]))
        out = _payload(cr.get_tool_list_handler())
        assert out["distinct_tool_count"] == 0 and out["tools"] == []

    def test_an_operation_whose_tool_reference_is_broken_is_skipped(self, install,
                                                                    operation_cast_passthrough):
        # a raising .tool must not sink the whole tool list - the readable ops still report.
        good = SimpleNamespace(name="Face1", tool=SimpleNamespace(description="flat 10mm"))
        install(FakeCAM([_OpSetup("S1", [_op_with_unreadable_tool("Broken1"), good])]))
        out = _payload(cr.get_tool_list_handler())
        assert out["distinct_tool_count"] == 1
        assert out["tools"][0]["operations"] == ["S1 / Face1"]

    def test_a_non_operation_node_is_skipped(self, install, monkeypatch):
        monkeypatch.setattr(adsk.cam.Operation, "cast",
                            lambda x: x if getattr(x, "tool", None) is not None else None)
        install(FakeCAM([_OpSetup("S1", [SimpleNamespace(name="Holes"),
                                         SimpleNamespace(name="Face1", tool=SimpleNamespace(
                                             description="flat 10mm"))])]))
        out = _payload(cr.get_tool_list_handler())
        assert out["tools"][0]["operations"] == ["S1 / Face1"]

    def test_a_failed_read_is_an_error_not_an_empty_tool_list(self, install):
        install(SimpleNamespace(setups=SimpleNamespace(count=1, item=_unreadable_item)))
        res = cr.get_tool_list_handler()
        assert res["isError"] is True
        assert "Could not read tools" in res["message"] and "setup read failed" in res["message"]


# --- get_nc_programs_handler: what IS readable on an NCProgram (the UI fields are not) ---

class TestNcPrograms:
    def test_reads_name_machine_post_and_the_parameters_present(self, install):
        nc = SimpleNamespace(
            name="Main", machine=SimpleNamespace(description="Haas VF-2"),
            postConfiguration=SimpleNamespace(description="haas next generation"),
            operations=[object(), object(), object()],
            postParameters=_NamedCollection([SimpleNamespace(name="metric", title="Use metric",
                                                  expression="true")]))
        install(SimpleNamespace(ncPrograms=_NamedCollection([nc])))
        out = _payload(cr.get_nc_programs_handler())
        assert out["nc_program_count"] == 1
        entry = out["nc_programs"][0]
        assert entry["name"] == "Main" and entry["machine"] == "Haas VF-2"
        assert entry["post"] == "haas next generation"
        # NCProgram.operations holds the setups/folders assigned - an ITEM count; the operations
        # figure is the filtered read, which this program does not answer at all.
        assert entry["item_count"] == 3 and entry["operation_count"] is None
        assert entry["post_parameters"] == [
            {"name": "metric", "title": "Use metric", "expression": "true"}]

    def test_discloses_native_nc_program_unit_and_choices(self, install):
        unit = FakeCAMParameter("nc_program_unit", "'metric'", value="metric",
                                title="NC units", choices=["metric", "imperial"])
        nc = SimpleNamespace(name="Main", machine=None, postConfiguration=None, operations=[],
                             parameters=_NamedCollection([unit]),
                             postParameters=_NamedCollection([]))
        install(SimpleNamespace(ncPrograms=_NamedCollection([nc])))
        entry = _payload(cr.get_nc_programs_handler())["nc_programs"][0]
        assert entry["nc_program_unit"] == {
            "expression": "'metric'", "value": "metric",
            "get_choices": {"ok": True, "titles": ["Metric", "Imperial"],
                             "values": ["metric", "imperial"]}}

    def test_discloses_false_native_choice_status_without_inventing_values(self, install):
        value = SimpleNamespace(value="metric", getChoices=lambda: (False, None, None))
        unit = SimpleNamespace(name="nc_program_unit", title="NC units", expression="metric",
                               value=value)
        nc = SimpleNamespace(name="Main", machine=None, postConfiguration=None, operations=[],
                             parameters=_NamedCollection([unit]),
                             postParameters=_NamedCollection([]))
        install(SimpleNamespace(ncPrograms=_NamedCollection([nc])))
        unit_out = _payload(cr.get_nc_programs_handler())["nc_programs"][0]["nc_program_unit"]
        assert unit_out["get_choices"] == {"ok": False}

    def test_omits_malformed_native_choice_disclosure(self, install):
        value = SimpleNamespace(value="metric", getChoices=lambda: ("yes", ["Metric"], ["metric"]))
        unit = SimpleNamespace(name="nc_program_unit", title="NC units", expression="metric",
                               value=value)
        nc = SimpleNamespace(name="Main", machine=None, postConfiguration=None, operations=[],
                             parameters=_NamedCollection([unit]),
                             postParameters=_NamedCollection([]))
        install(SimpleNamespace(ncPrograms=_NamedCollection([nc])))
        unit_out = _payload(cr.get_nc_programs_handler())["nc_programs"][0]["nc_program_unit"]
        assert "get_choices" not in unit_out

    def test_omits_unreadable_native_choice_disclosure(self, install):
        def get_choices():
            raise RuntimeError("choices unavailable")
        value = SimpleNamespace(value="metric", getChoices=get_choices)
        unit = SimpleNamespace(name="nc_program_unit", title="NC units", expression="metric",
                               value=value)
        nc = SimpleNamespace(name="Main", machine=None, postConfiguration=None, operations=[],
                             parameters=_NamedCollection([unit]),
                             postParameters=_NamedCollection([]))
        install(SimpleNamespace(ncPrograms=_NamedCollection([nc])))
        unit_out = _payload(cr.get_nc_programs_handler())["nc_programs"][0]["nc_program_unit"]
        assert "get_choices" not in unit_out

    def test_omits_missing_native_unit_parameter(self, install):
        other = SimpleNamespace(name="other", title="Other", expression="x",
                                value=SimpleNamespace(value="x"))
        nc = SimpleNamespace(name="Main", machine=None, postConfiguration=None, operations=[],
                             postParameters=_NamedCollection([other]))
        install(SimpleNamespace(ncPrograms=_NamedCollection([nc])))
        entry = _payload(cr.get_nc_programs_handler())["nc_programs"][0]
        assert "nc_program_unit" not in entry
    def test_an_unassigned_program_reads_nulls_not_fabricated_values(self, install):
        # operation_count especially: an unreadable count must not report 0 operations.
        install(SimpleNamespace(ncPrograms=_NamedCollection([SimpleNamespace(
            name="Setup1", machine=None, postConfiguration=None, postParameters=None)])))
        entry = _payload(cr.get_nc_programs_handler())["nc_programs"][0]
        assert entry["machine"] is None and entry["post"] is None
        assert entry["operation_count"] is None
        assert entry["post_parameters"] == []

    def test_a_broken_post_parameter_does_not_sink_the_program_list(self, install):
        nc = SimpleNamespace(name="Main", machine=None, postConfiguration=None, operations=[],
                             postParameters=SimpleNamespace(count=1, item=_unreadable_item))
        install(SimpleNamespace(ncPrograms=_NamedCollection([nc])))
        out = _payload(cr.get_nc_programs_handler())
        assert out["nc_program_count"] == 1
        assert out["nc_programs"][0]["name"] == "Main"
        assert out["nc_programs"][0]["post_parameters"] == []
        assert out["nc_programs"][0]["item_count"] == 0

    def test_a_gated_programs_collection_is_an_error_not_an_empty_list(self, install):
        install(make_gated_cam(member="ncPrograms", text="programs unavailable"))
        res = cr.get_nc_programs_handler()
        assert res["isError"] is True
        assert "Could not read NC programs" in res["message"]
        assert "programs unavailable" in res["message"]


class TestNcProgramPostedOperations:
    """What a program HOLDS is filteredOperations, not the single-entry 'operations' list - and it
    holds operations with no toolpath, which the posted NC file does not carry."""

    def _program(self, posted, **kw):
        return SimpleNamespace(name="Main", machine=None, postConfiguration=None,
                               operations=[object()], postParameters=None,
                               filteredOperations=posted, **kw)

    def test_the_posted_list_is_counted_beside_the_operations_property(self, install,
                                                                       operation_cast_passthrough):
        posted = [_row_op("Cut"), _row_op("Rough")]
        install(SimpleNamespace(ncPrograms=_NamedCollection([self._program(posted)])))
        entry = _payload(cr.get_nc_programs_handler())["nc_programs"][0]
        # MEASURED: NCProgram.operations holds the setups/folders - so it is the ITEM count, and
        # the operations figure is the filtered read that says what the program holds.
        assert entry["item_count"] == 1
        assert entry["operation_count"] == 2 and entry["posted_operations"] == 2
        assert entry["empty_toolpath_count"] == 0
        assert "empty_toolpaths" not in entry

    def test_a_held_operation_with_no_toolpath_is_not_counted_as_posted(
            self, install, operation_cast_passthrough):
        # the partition this slice exists to publish: four operations in the program's scope, one
        # operation block in the file it posts.
        held = [_row_op("FaceLeg")] + [self._empty(n) for n in ("Chamfer1", "Drill1", "Drill2")]
        install(SimpleNamespace(ncPrograms=_NamedCollection([self._program(held)])))
        entry = _payload(cr.get_nc_programs_handler())["nc_programs"][0]
        assert entry["operation_count"] == 4 and entry["posted_operations"] == 1
        assert "toolpath_unread" not in entry

    def test_a_row_whose_toolpath_flag_does_not_read_is_disclosed_not_counted(
            self, install, operation_cast_passthrough):
        mystery = _row_op("Mystery")
        del mystery.hasToolpath
        install(SimpleNamespace(ncPrograms=_NamedCollection([self._program([_row_op("Cut"), mystery])])))
        entry = _payload(cr.get_nc_programs_handler())["nc_programs"][0]
        assert entry["operation_count"] == 2
        assert entry["posted_operations"] == 1 and entry["toolpath_unread"] == 1

    def test_an_empty_toolpath_operation_is_named_among_the_held(
            self, install, operation_cast_passthrough):
        empty = _row_op("Rest Wall Finishing 1")
        empty.hasToolpath = False
        install(SimpleNamespace(ncPrograms=_NamedCollection([self._program([_row_op("Cut"), empty])])))
        entry = _payload(cr.get_nc_programs_handler())["nc_programs"][0]
        assert entry["empty_toolpath_count"] == 1
        assert entry["empty_toolpaths"] == ["Rest Wall Finishing 1"]

    def test_an_operation_whose_toolpath_generated_empty_is_named_too(
            self, install, operation_cast_passthrough):
        # the EMPTY class's second shape reaching the posted list: hasToolpath TRUE, state IsValid,
        # 0.0 s. It would post with nothing to cut exactly as the flags shape does, and the flags
        # alone read it as the cutting operation beside it.
        posted = [_row_op("Cut"), _row_op("Swarf1")]
        install(SimpleNamespace(
            ncPrograms=_NamedCollection([self._program(posted)]),
            getMachiningTime=make_cam(
                machining_times={"Cut": 4.193083, "Swarf1": 0.0}).getMachiningTime))
        entry = _payload(cr.get_nc_programs_handler())["nc_programs"][0]
        assert entry["empty_toolpath_count"] == 1
        assert entry["empty_toolpaths"] == ["Swarf1"]

    def test_the_named_empties_are_capped_while_the_count_is_not(self, install, monkeypatch,
                                                                 operation_cast_passthrough):
        monkeypatch.setattr(cr, "_NC_EMPTY_NAME_CAP", 2)
        empties = []
        for i in range(5):
            op = _row_op(f"Empty{i}")
            op.hasToolpath = False
            empties.append(op)
        install(SimpleNamespace(ncPrograms=_NamedCollection([self._program(empties)])))
        entry = _payload(cr.get_nc_programs_handler())["nc_programs"][0]
        assert entry["empty_toolpath_count"] == 5 and len(entry["empty_toolpaths"]) == 2

    def test_a_non_operation_entry_is_skipped(self, install, monkeypatch):
        monkeypatch.setattr(adsk.cam.Operation, "cast",
                            lambda x: x if getattr(x, "name", "") != "folder" else None)
        install(SimpleNamespace(ncPrograms=_NamedCollection([
            self._program([SimpleNamespace(name="folder"), _row_op("Cut")])])))
        entry = _payload(cr.get_nc_programs_handler())["nc_programs"][0]
        assert entry["operation_count"] == 2 and entry["empty_toolpath_count"] == 0
        # the skipped row carries no hasToolpath either, so it is disclosed rather than counted
        assert entry["posted_operations"] == 1 and entry["toolpath_unread"] == 1

    def test_an_unreadable_posted_list_claims_nothing(self, install):
        # no filteredOperations at all: the keys are absent rather than reported as zero
        install(SimpleNamespace(ncPrograms=_NamedCollection([SimpleNamespace(
            name="Main", machine=None, postConfiguration=None, operations=[],
            postParameters=None)])))
        entry = _payload(cr.get_nc_programs_handler())["nc_programs"][0]
        assert "posted_operations" not in entry and "empty_toolpath_count" not in entry

    def test_the_note_tells_the_two_lists_apart(self, install):
        install(SimpleNamespace(ncPrograms=_NamedCollection([])))
        note = _payload(cr.get_nc_programs_handler())["note"]
        assert "filteredOperations" in note and "empty_toolpath_count" in note

    def _empty(self, name):
        op = _row_op(name)
        op.hasToolpath = False
        return op

    def test_a_name_two_held_operations_share_is_told_apart_by_its_position(
            self, install, operation_cast_passthrough):
        # A program's held list can draw operations from several setups and an operation name is
        # unique only within one, so a bare name printed twice addresses two operations and
        # separates neither. The position is the fact this read holds.
        install(SimpleNamespace(ncPrograms=_NamedCollection([self._program(
            [self._empty("Rough"), _row_op("Cut"), self._empty("Rough")])])))
        entry = _payload(cr.get_nc_programs_handler())["nc_programs"][0]
        assert entry["empty_toolpaths"] == ["Rough (operation 1)",
                                            "Rough (operation 3)"]
        assert entry["empty_toolpath_count"] == 2

    def test_a_name_only_one_held_operation_carries_stays_the_bare_name(
            self, install, operation_cast_passthrough):
        # The spelling a caller passes to cam_get/cam_generate crosses unchanged where it already
        # identifies one row - a position there separates nothing that was not already separate.
        install(SimpleNamespace(ncPrograms=_NamedCollection([self._program(
            [self._empty("Bore"), self._empty("Face")])])))
        entry = _payload(cr.get_nc_programs_handler())["nc_programs"][0]
        assert entry["empty_toolpaths"] == ["Bore", "Face"]

    def test_the_position_counts_over_the_held_list_not_the_empty_rows(
            self, install, operation_cast_passthrough):
        # The discriminator has to address the HELD list, which is what a reader is looking at;
        # numbering the empty rows instead would print '2' for the fourth held operation.
        install(SimpleNamespace(ncPrograms=_NamedCollection([self._program(
            [self._empty("Rough"), _row_op("Cut"), _row_op("Drill"),
             self._empty("Rough")])])))
        entry = _payload(cr.get_nc_programs_handler())["nc_programs"][0]
        assert entry["empty_toolpaths"] == ["Rough (operation 1)",
                                            "Rough (operation 4)"]

    def test_a_skipped_non_operation_entry_still_holds_its_position(
            self, install, monkeypatch):
        # Operation.cast returning None skips the row without consuming a position: the number is
        # the index into filteredOperations, which is the list a reader counts along.
        monkeypatch.setattr(adsk.cam.Operation, "cast",
                            lambda x: x if getattr(x, "name", "") != "folder" else None)
        install(SimpleNamespace(ncPrograms=_NamedCollection([self._program(
            [self._empty("Rough"), SimpleNamespace(name="folder"),
             self._empty("Rough")])])))
        entry = _payload(cr.get_nc_programs_handler())["nc_programs"][0]
        assert entry["empty_toolpaths"] == ["Rough (operation 1)",
                                            "Rough (operation 3)"]

    def test_the_substitution_is_judged_over_every_empty_row_not_the_capped_head(
            self, install, monkeypatch, operation_cast_passthrough):
        # The cap is applied AFTER the substitution, or the visible list would print an address
        # that reaches two operations while looking unique.
        monkeypatch.setattr(cr, "_NC_EMPTY_NAME_CAP", 2)
        install(SimpleNamespace(ncPrograms=_NamedCollection([self._program(
            [self._empty("Dup"), self._empty("Solo"), self._empty("Dup")])])))
        entry = _payload(cr.get_nc_programs_handler())["nc_programs"][0]
        assert entry["empty_toolpaths"] == ["Dup (operation 1)", "Solo"]
        assert entry["empty_toolpath_count"] == 3

    def test_an_empty_operation_whose_name_does_not_read_keeps_it(
            self, install, operation_cast_passthrough):
        # An empty discriminator is not a label: told_apart keeps the row's own name, so a nameless
        # operation never renders as a bare position with nothing in front of it.
        nameless = self._empty(None)
        install(SimpleNamespace(ncPrograms=_NamedCollection([self._program([nameless])])))
        entry = _payload(cr.get_nc_programs_handler())["nc_programs"][0]
        assert entry["empty_toolpaths"] == [None]

    def test_the_note_describes_the_shape_the_rows_actually_take(
            self, install, operation_cast_passthrough):
        # The note and the label have to agree on ONE shape: the row KEEPS the operation's name and
        # carries the position BESIDE it. A note saying the position replaces the name sends a
        # reader hunting for a name that is still sitting there.
        install(SimpleNamespace(ncPrograms=_NamedCollection([self._program(
            [self._empty("Rough"), self._empty("Rough")])])))
        out = _payload(cr.get_nc_programs_handler())
        rows = out["nc_programs"][0]["empty_toolpaths"]
        assert all(r.startswith("Rough (") for r in rows)     # the name is KEPT, not replaced
        assert "carries its position in that list" in out["note"]


# --- get_machining_time_handler: the setup scope + the getMachiningTime precondition ---

class TestMachiningTimeScope:
    def test_a_named_setup_scopes_the_estimate(self, install, object_collection,
                                               operation_cast_passthrough):
        cam = _MTCam([_MTSetup("S1"), _MTSetup("S2")])
        install(cam)
        out = _payload(cr.get_machining_time_handler(setup="s2"))    # case-insensitive exact
        assert out["setup_count"] == 1 and out["setups"][0]["setup"] == "S2"
        assert out["total_machining_time_seconds"] == 120.0
        assert len(_aggregate_calls(cam)) == 1                          # S1 was never timed
        assert [op.name for op in object_collection[0]] == ["S2 op"]

    def test_a_duplicated_setup_name_is_refused(self, install, operation_cast_passthrough):
        install(_MTCam([_MTSetup("Dup"), _MTSetup("Dup")]))
        res = cr.get_machining_time_handler(setup="Dup")
        assert res["isError"] is True and "ambiguous" in res["message"].lower()

    def test_a_named_setup_miss_lists_the_available_names(self, install,
                                                          operation_cast_passthrough):
        install(_MTCam([_MTSetup("S1")]))
        res = cr.get_machining_time_handler(setup="Ghost")
        assert res["isError"] is True and "Ghost" in res["message"] and "S1" in res["message"]

    def test_an_unreadable_op_list_reports_the_precondition_not_a_crash(self, install):
        # getMachiningTime fails UNCATCHABLY without a valid toolpath, so an unreadable op list has
        # to be treated as "nothing to time" rather than optimistically calling through.
        cam = _MTCam([SimpleNamespace(name="S1")])
        install(cam)
        out = _payload(cr.get_machining_time_handler())
        assert "error" in out["setups"][0] and cam.calls == []

    def test_a_raising_estimate_names_the_setup_total_unavailable(
            self, install, object_collection, operation_cast_passthrough, monkeypatch):
        cam = _MTCam([_MTSetup("S1")])
        install(cam)

        def _boom(*_a):
            raise RuntimeError("post engine unavailable")
        monkeypatch.setattr(cam, "getMachiningTime", _boom)
        out = _payload(cr.get_machining_time_handler())
        assert out["setups"][0]["setup_total_unavailable"] == "post engine unavailable"
        assert out["total_machining_time_seconds"] == 0.0

    def test_a_raising_setup_total_still_publishes_the_per_operation_times(
            self, install, object_collection, operation_cast_passthrough, monkeypatch):
        # CAM.getMachiningTime over a SETUP raises while any operation in it is errored, which would
        # hide every good per-operation reading behind one message. The per-op calls still answer.
        good, bad = _MTOp("Good"), _MTOp("Bad")
        bad.hasError = True
        cam = _MTCam([_MTSetup("S1", ops=[good, bad])], per_op_by_name={"Good": 42.0, "Bad": 7.0})
        install(cam)
        whole = cam.getMachiningTime

        def _only_per_op(obj, *knobs):
            if not isinstance(obj, FakeOperation):
                raise RuntimeError("3 : Machining time could not be calculated.")
            return whole(obj, *knobs)
        monkeypatch.setattr(cam, "getMachiningTime", _only_per_op)
        row = _payload(cr.get_machining_time_handler())["setups"][0]
        assert row["setup_total_unavailable"] == "3 : Machining time could not be calculated."
        assert [(r["operation"], r["machining_time_seconds"]) for r in row["operations"]] == [
            ("Good", 42.0), ("Bad", 7.0)]
        assert row["operations_time_sum_seconds"] == 49.0
        assert row["operations_with_errors"] == ["Bad"]

    def test_a_setup_left_out_of_the_document_total_is_named(self, install, object_collection,
                                                             operation_cast_passthrough,
                                                             monkeypatch):
        # THE BITE: its per-op rows are published, so the payload looks complete while the document
        # figure silently omits it. The total says which setups it does not cover.
        cam = _MTCam([_MTSetup("S1"), _MTSetup("S2")], seconds=120.0)
        install(cam)
        whole = cam.getMachiningTime

        def _s1_total_raises(obj, *knobs):
            # the whole-collection call carries the setup's own operations, which is what names it
            if not isinstance(obj, FakeOperation) and any(
                    getattr(o, "name", "") == "S1 op" for o in obj):
                raise RuntimeError("3 : Machining time could not be calculated.")
            return whole(obj, *knobs)
        monkeypatch.setattr(cam, "getMachiningTime", _s1_total_raises)
        out = _payload(cr.get_machining_time_handler())
        assert out["total_excludes_setups"] == ["S1"]
        assert "total_excludes_setups names every setup left out of it" in out["note"]

    def test_a_job_whose_setups_all_totalled_names_no_exclusions(self, install, object_collection,
                                                                 operation_cast_passthrough):
        # the quiet default: the key and its sentence appear only where a setup was actually left
        # out, or every clean read carries a caveat about a state it is not in.
        install(_MTCam([_MTSetup("S1")]))
        out = _payload(cr.get_machining_time_handler())
        assert "total_excludes_setups" not in out
        assert "total_excludes_setups" not in out["note"]

    def test_unknown_units_are_refused_by_name(self, install):
        install(_MTCam([_MTSetup("S1")]))
        res = cr.get_machining_time_handler(units="furlongs")
        assert res["isError"] is True and "furlongs" in res["message"]


# --- CAM-13: SUPPRESSED operations are held out of the timed collection, and the note says so ---

class TestMachiningTimeExcludesSuppressed:
    """The timed target is a collection of the non-suppressed ops, and what was left out is
    published rather than silently dropped. The exclusion is a CONSTRUCTION, not an API need:
    measured (measure_api cam-machining-time-suppressed-op-contributes-nothing), a suppressed op
    in the collection contributes nothing to the figure and the call succeeds - so leaving it out
    changes no number, keeps excluded_suppressed honest, and stays robust on a collection whose
    other members' states this handler never reads."""

    def _cam(self, install, ops, per_op_by_name=None):
        cam = _MTCam([_MTSetup("S1", ops=ops)], per_op_by_name=per_op_by_name)
        install(cam)
        return cam

    def test_an_op_whose_toolpath_generated_empty_is_named_not_timed(
            self, install, object_collection, operation_cast_passthrough):
        # the EMPTY class's second shape: hasToolpath TRUE, state IsValid, and the operation's own
        # call answering 0.0 s. Nothing in the flags separates it from the operation beside it, so
        # the call this row was going to make anyway is what settles it - and it is made ONCE.
        cam = self._cam(install, [_MTOp("Cut", valid=True), _MTOp("Swarf1", valid=True)],
                        per_op_by_name={"Swarf1": 0.0})
        out = _payload(cr.get_machining_time_handler())
        rows = {r["operation"]: r for r in out["setups"][0]["operations"]}
        assert rows["Swarf1"] == {"operation": "Swarf1", "empty_toolpath": True}
        assert rows["Cut"]["machining_time_seconds"] == 60.0
        assert [c[0].name for c in cam.calls if isinstance(c[0], FakeOperation)] == ["Cut", "Swarf1"]
        # the empty row carries no figure, so it is out of the summed count
        assert out["setups"][0]["operations_time_summed"] == 1
        assert out["setups"][0]["operations_time_sum_seconds"] == 60.0

    def test_suppressed_ops_are_kept_out_of_the_timed_collection(self, install, object_collection,
                                                                 operation_cast_passthrough):
        cam = self._cam(install, [_MTOp("Cut", valid=True),
                                  _MTOp("Parked", valid=False, suppressed=True),
                                  _MTOp("AlsoParked", valid=False, suppressed=True)])
        out = _payload(cr.get_machining_time_handler())
        assert [op.name for op in object_collection[0]] == ["Cut"]
        assert out["setups"][0]["excluded_suppressed"] == 2
        assert out["setups"][0]["timed_operations"] == 1
        # the SETUP object itself is never the target - that is the shape that fails live
        assert not any(isinstance(c[0], FakeSetup) for c in cam.calls)

    def test_empty_toolpath_ops_stay_in_the_collection(self, install, object_collection,
                                                       operation_cast_passthrough):
        # an op with a valid toolpath flag but nothing to cut is harmless to the call (measured),
        # so excluding it would understate the job for no reason.
        self._cam(install, [_MTOp("Cut", valid=True), _MTOp("Empty", valid=True)])
        out = _payload(cr.get_machining_time_handler())
        assert [op.name for op in object_collection[0]] == ["Cut", "Empty"]
        assert out["setups"][0]["excluded_suppressed"] == 0

    def test_a_setup_of_only_suppressed_ops_reports_the_precondition(self, install,
                                                                     object_collection,
                                                                     operation_cast_passthrough):
        cam = self._cam(install, [_MTOp("Parked", valid=True, suppressed=True)])
        out = _payload(cr.get_machining_time_handler())
        assert "error" in out["setups"][0]
        assert out["setups"][0]["excluded_suppressed"] == 1
        assert cam.calls == []            # nothing timed - the one valid toolpath was suppressed

    def test_a_collection_that_refuses_an_item_is_an_error_not_a_short_estimate(
            self, install, monkeypatch, operation_cast_passthrough):
        import adsk.core
        monkeypatch.setattr(adsk.core.ObjectCollection, "create",
                            lambda: _RefusingCollection())
        cam = self._cam(install, [_MTOp("Cut", valid=True)])
        out = _payload(cr.get_machining_time_handler())
        assert "0 of 1" in out["setups"][0]["error"]
        assert cam.calls == []

    def test_an_empty_toolpath_op_is_named_not_timed(self, install, object_collection,
                                                     operation_cast_passthrough):
        # measured: a per-op call on an op with no toolpath raises "Machining time could not be
        # calculated", while the same op inside the setup's collection is harmless. Reporting the
        # state beats reporting a platform error the flags already predict.
        cam = self._cam(install, [_MTOp("Cut", valid=True),
                                  _MTOp("Empty", valid=True, has_toolpath=False)])
        out = _payload(cr.get_machining_time_handler())
        rows = {r["operation"]: r for r in out["setups"][0]["operations"]}
        assert rows["Empty"] == {"operation": "Empty", "empty_toolpath": True}
        assert "machining_time_seconds" not in rows["Empty"]
        assert rows["Cut"]["machining_time_seconds"] == 60.0
        # the doomed call is never made: only the op holding a toolpath was timed per-op
        assert [c[0].name for c in cam.calls if isinstance(c[0], FakeOperation)] == ["Cut"]
        # ...and the empty op still rides in the setup's collection, which times fine
        assert [op.name for op in object_collection[0]] == ["Cut", "Empty"]

    def test_the_empty_rows_count_against_the_cap(self, install, object_collection,
                                                  operation_cast_passthrough, monkeypatch):
        monkeypatch.setattr(cr, "_TIME_OP_CAP", 2)
        self._cam(install, [_MTOp(f"Empty{i}", valid=True, has_toolpath=False) for i in range(4)])
        out = _payload(cr.get_machining_time_handler())
        assert len(out["setups"][0]["operations"]) == 2
        assert out["setups"][0]["operations_truncated"] is True

    def test_per_operation_rows_carry_time_and_cut_distance(self, install, object_collection,
                                                            operation_cast_passthrough):
        self._cam(install, [_MTOp("Cut", valid=True), _MTOp("Stale", valid=False)])
        out = _payload(cr.get_machining_time_handler())
        rows = out["setups"][0]["operations"]
        # only the op carrying a valid toolpath is timed; feedDistance is CM -> mm
        assert [r["operation"] for r in rows] == ["Cut"]
        assert rows[0]["machining_time_seconds"] == 60.0
        assert rows[0]["feed_distance"] == 1000.0 and rows[0]["rapid_distance"] == 250.0

    def test_distances_scale_to_the_requested_unit(self, install, object_collection,
                                                   operation_cast_passthrough):
        self._cam(install, [_MTOp("Cut", valid=True)])
        out = _payload(cr.get_machining_time_handler(units="cm"))
        assert out["units"] == "cm"
        assert out["setups"][0]["feed_distance"] == 1000.0        # 1000 cm stays 1000 cm
        assert out["setups"][0]["operations"][0]["feed_distance"] == 100.0

    def test_the_note_states_the_measured_non_summing_caveat(self, install, object_collection,
                                                             operation_cast_passthrough):
        self._cam(install, [_MTOp("Cut", valid=True)])
        out = _payload(cr.get_machining_time_handler())
        assert "do not sum" in out["note"] and "Suppressed operations are left out" in out["note"]

    # --- CAM-18: BOTH totals ship, and the note says what each covers ---

    def test_both_totals_are_published_so_the_gap_is_visible_on_this_job(
            self, install, object_collection, operation_cast_passthrough):
        # The aggregate is ONE getMachiningTime call over the collection; the sum is the per-op
        # calls added up. They disagree (measured live, and the fake reproduces the shape), so the
        # payload publishes both instead of leaving a caller to add the rows and guess which number
        # is the job.
        self._cam(install, [_MTOp("Cut", valid=True)])
        rec = _payload(cr.get_machining_time_handler())["setups"][0]
        assert rec["machining_time_seconds"] == 120.0
        assert rec["operations_time_sum_seconds"] == 60.0
        assert rec["operations_time_summed"] == 1
        assert rec["operations_time_sum_seconds"] != rec["machining_time_seconds"]

    def test_the_sum_adds_every_timed_row(self, install, object_collection,
                                          operation_cast_passthrough):
        self._cam(install, [_MTOp("Cut", valid=True), _MTOp("Cut2", valid=True),
                            _MTOp("Cut3", valid=True)])
        rec = _payload(cr.get_machining_time_handler())["setups"][0]
        assert rec["operations_time_sum_seconds"] == 180.0
        assert rec["operations_time_summed"] == 3

    def test_an_empty_toolpath_row_contributes_nothing_and_is_not_counted(
            self, install, object_collection, operation_cast_passthrough):
        # an empty-toolpath row carries no time at all, so it may not silently enter the sum as a
        # zero the count then claims was measured.
        self._cam(install, [_MTOp("Cut", valid=True),
                            _MTOp("Empty", valid=True, has_toolpath=False)])
        rec = _payload(cr.get_machining_time_handler())["setups"][0]
        assert len(rec["operations"]) == 2
        assert rec["operations_time_sum_seconds"] == 60.0
        assert rec["operations_time_summed"] == 1

    def test_a_row_whose_own_call_errored_is_left_out_of_the_sum(
            self, install, object_collection, operation_cast_passthrough, monkeypatch):
        cam = self._cam(install, [_MTOp("Cut", valid=True)])
        real = cam.getMachiningTime

        def _boom(obj, *a):
            if isinstance(obj, FakeOperation):
                raise RuntimeError("per-op estimate unavailable")
            return real(obj, *a)
        monkeypatch.setattr(cam, "getMachiningTime", _boom)
        rec = _payload(cr.get_machining_time_handler())["setups"][0]
        assert rec["operations_time_sum_seconds"] == 0.0
        assert rec["operations_time_summed"] == 0

    def test_a_truncated_row_pass_leaves_the_sum_covering_only_the_rows_present(
            self, install, object_collection, operation_cast_passthrough, monkeypatch):
        monkeypatch.setattr(cr, "_TIME_OP_CAP", 2)
        self._cam(install, [_MTOp(f"Op{i}", valid=True) for i in range(5)])
        rec = _payload(cr.get_machining_time_handler())["setups"][0]
        assert rec["operations_truncated"] is True
        assert rec["operations_time_summed"] == 2 and rec["operations_time_sum_seconds"] == 120.0

    def test_the_note_names_both_totals_and_what_each_one_covers(self, install, object_collection,
                                                                 operation_cast_passthrough):
        self._cam(install, [_MTOp("Cut", valid=True)])
        note = _payload(cr.get_machining_time_handler())["note"]
        assert "operations_time_sum_seconds" in note and "operations_time_summed" in note
        assert "machining_time_seconds is ONE call over the whole collection" in note
        assert "do not sum to their setup total" in note

    def test_per_op_rows_are_capped(self, install, object_collection, operation_cast_passthrough,
                                    monkeypatch):
        monkeypatch.setattr(cr, "_TIME_OP_CAP", 3)
        self._cam(install, [_MTOp(f"Op{i}", valid=True) for i in range(5)])
        out = _payload(cr.get_machining_time_handler())
        assert len(out["setups"][0]["operations"]) == 3
        assert out["setups"][0]["operations_truncated"] is True

    def test_a_full_set_of_rows_is_not_flagged_truncated(self, install, object_collection,
                                                         operation_cast_passthrough, monkeypatch):
        monkeypatch.setattr(cr, "_TIME_OP_CAP", 3)
        self._cam(install, [_MTOp(f"Op{i}", valid=True) for i in range(3)])
        out = _payload(cr.get_machining_time_handler())
        assert len(out["setups"][0]["operations"]) == 3
        assert "operations_truncated" not in out["setups"][0]

    def test_a_per_op_estimate_that_raises_becomes_that_rows_error(self, install,
                                                                   object_collection,
                                                                   operation_cast_passthrough,
                                                                   monkeypatch):
        # one op's estimate failing must not cost the setup total, which already came back
        cam = self._cam(install, [_MTOp("Cut", valid=True)])
        real = cam.getMachiningTime

        def _boom(obj, *a):
            if isinstance(obj, FakeOperation):
                raise RuntimeError("per-op estimate unavailable")
            return real(obj, *a)
        monkeypatch.setattr(cam, "getMachiningTime", _boom)
        out = _payload(cr.get_machining_time_handler())
        rec = out["setups"][0]
        assert rec["machining_time_seconds"] == 120.0
        assert rec["operations"][0] == {"operation": "Cut", "error": "per-op estimate unavailable"}

    def test_a_non_operation_node_never_reaches_the_collection(self, install, object_collection,
                                                               monkeypatch):
        # a setup that exposes .operations is walked WITHOUT a cast (the container branch), so a
        # node that is not an Operation reaches _timeable_ops itself - and must be dropped there,
        # never added to a collection getMachiningTime is about to be handed.
        monkeypatch.setattr(adsk.cam.Operation, "cast",
                            lambda x: None if getattr(x, "name", "") == "Folderish" else x)
        cam = _MTCam([FakeSetup("S1", ops=[SimpleNamespace(name="Folderish"),
                                           _MTOp("Cut", valid=True)])])
        install(cam)
        _payload(cr.get_machining_time_handler())
        assert [op.name for op in object_collection[0]] == ["Cut"]

    def test_an_uncreatable_collection_is_an_error_not_a_setup_object_fallback(
            self, install, monkeypatch, operation_cast_passthrough):
        import adsk.core
        monkeypatch.setattr(adsk.core.ObjectCollection, "create", lambda: None)
        cam = self._cam(install, [_MTOp("Cut", valid=True)])
        out = _payload(cr.get_machining_time_handler())
        assert "Could not build the operation collection" in out["setups"][0]["error"]
        assert cam.calls == []          # never falls back to timing the Setup object


# --- the async-generation registry: the handle cam_get_status reads, and what keeps a launch alive ---

class TestRegisterFuture:
    @pytest.fixture
    def registry(self, monkeypatch):
        """A fresh registry + handle sequence, so a handle assertion does not depend on what other
        launches this session already minted."""
        monkeypatch.setattr(cc, "_GENERATIONS", {})
        monkeypatch.setattr(cc, "_HANDLE_SEQ", [0])
        monkeypatch.setattr(cc, "_active_identity", lambda: ("Part.f3d", "urn:adsk:1"))
        monkeypatch.setattr(cc, "document_key", lambda: "urn:adsk:1")
        return cc._GENERATIONS

    def test_each_launch_mints_its_own_handle(self, registry):
        h1, _t1 = cc.register_future(SimpleNamespace(numberOfOperations=3), "whole document",
                                     "document", False)
        h2, _t2 = cc.register_future(SimpleNamespace(numberOfOperations=1), "setup 'S1'",
                                     "setup", True, "S1")
        assert (h1, h2) == ("gen1", "gen2")          # a reused handle would orphan the first launch
        assert set(registry) == {"gen1", "gen2"}

    def test_the_future_itself_stays_referenced(self, registry):
        fut = SimpleNamespace(numberOfOperations=2)
        handle, total = cc.register_future(fut, "setup 'S1'", "setup", False, "S1")
        # Fusion ABANDONS a generation whose Future is garbage-collected - the registry IS what
        # keeps the background work alive between the launch call and the polls.
        assert registry[handle]["future"] is fut
        assert total == 2 and registry[handle]["total"] == 2

    def test_the_entry_binds_the_launch_to_its_document_and_target(self, registry):
        handle, _total = cc.register_future(SimpleNamespace(numberOfOperations=1), "operation",
                                            "operation", False, "  Face1  ")
        entry = registry[handle]
        assert entry["doc_name"] == "Part.f3d" and entry["doc_urn"] == "urn:adsk:1"
        assert entry["doc_key"] == "urn:adsk:1"       # what the status read COMPARES on
        assert entry["target_name"] == "Face1"       # stripped: the status read matches on it
        assert entry["scope"] == "operation" and entry["skip_valid"] is False

    def test_a_never_saved_launch_document_is_still_bound_by_its_key(self, monkeypatch, registry):
        # THE POINT of stamping the key: a never-saved document has no urn, so a urn-only record
        # cannot identify a launch from one, and its status read has only the Future to settle on.
        # The per-instance key identifies it, and doc_name/doc_urn stay as they read (the payload
        # NAMES the generating document from them).
        monkeypatch.setattr(cc, "_active_identity", lambda: ("Untitled", None))
        monkeypatch.setattr(cc, "document_key", lambda: "unsaved:7")
        handle, _total = cc.register_future(SimpleNamespace(numberOfOperations=1), "whole document",
                                            "document", True)
        entry = registry[handle]
        assert entry["doc_key"] == "unsaved:7"
        assert entry["doc_urn"] is None and entry["doc_name"] == "Untitled"

    def test_the_entry_keeps_the_launch_DOCUMENT_beside_its_key(self, monkeypatch, registry):
        # The key is DERIVED from what reads on the document - document_key prefers a data-file id -
        # so it changes under a launch that is still the same open document the moment one becomes
        # readable. The document itself is what the status read compares when that happens.
        doc = SimpleNamespace(name="Untitled")
        monkeypatch.setattr(cc, "app", SimpleNamespace(activeDocument=doc))
        handle, _total = cc.register_future(SimpleNamespace(numberOfOperations=1), "whole document",
                                            "document", True)
        assert registry[handle]["doc"] is doc

    def test_a_launch_whose_active_document_will_not_read_records_no_handle(self, monkeypatch,
                                                                            registry):
        # None, never a raise out of the launch: the entry then compares on its key alone, which is
        # what it did before there was a handle to keep.
        class _NoActiveDocument(FakeApplication):
            @property
            def activeDocument(self):
                raise RuntimeError("no active document")

            @activeDocument.setter
            def activeDocument(self, value):
                pass

        monkeypatch.setattr(cc, "app", _NoActiveDocument())
        handle, _total = cc.register_future(SimpleNamespace(numberOfOperations=1), "whole document",
                                            "document", True)
        assert registry[handle]["doc"] is None

    def test_a_launch_with_no_readable_document_records_no_key(self, monkeypatch, registry):
        # document_key answers None when nothing reads. Recorded as None, so the status read
        # answers "cannot be compared" instead of comparing two absent identities into a match.
        monkeypatch.setattr(cc, "_active_identity", lambda: (None, None))
        monkeypatch.setattr(cc, "document_key", lambda: None)
        handle, _total = cc.register_future(SimpleNamespace(numberOfOperations=1), "whole document",
                                            "document", True)
        assert registry[handle]["doc_key"] is None

    def test_an_unreadable_operation_count_is_null_not_zero(self, registry):
        # 0 would read as "nothing to generate" and settle the handle complete immediately.
        handle, total = cc.register_future(SimpleNamespace(), "whole document", "document", 1)
        assert total is None and registry[handle]["total"] is None
        assert registry[handle]["skip_valid"] is True     # coerced to a real bool
        assert registry[handle]["target_name"] == ""      # a whole-document launch names no target

    def test_a_key_flip_re_stamps_the_launch_it_belongs_to(self, monkeypatch, registry):
        # A launch document that is SAVED mid-generation answers a new key without closing. The
        # stored key is what the status read compares first, so an entry still holding the
        # superseded one reports a mismatch on every later poll and leans on the handle fallback for
        # the rest of the generation instead of only for the call that detected the flip.
        monkeypatch.setattr(cc, "document_key", lambda: "unsaved:7")
        handle, _total = cc.register_future(SimpleNamespace(numberOfOperations=1), "whole document",
                                            "document", True)
        cc._carry_generation_keys("unsaved:7", "urn:lineage:saved")
        assert registry[handle]["doc_key"] == "urn:lineage:saved"

    def test_a_flip_of_a_different_documents_key_leaves_the_launch_alone(self, monkeypatch, registry):
        # The announcement is broadcast to every consumer and names ONE key: re-stamping a launch
        # whose key did not change would bind it to a document it was never launched from.
        monkeypatch.setattr(cc, "document_key", lambda: "unsaved:7")
        handle, _total = cc.register_future(SimpleNamespace(numberOfOperations=1), "whole document",
                                            "document", True)
        cc._carry_generation_keys("unsaved:99", "urn:lineage:elsewhere")
        assert registry[handle]["doc_key"] == "unsaved:7"

    def test_the_re_stamp_is_WIRED_to_the_shared_announcement(self, monkeypatch, registry):
        # The listener existing is not the same as it being registered: without the on_key_renamed
        # call at import, the two tests above still pass and no real key flip ever reaches this
        # registry. Driven through the shared key read itself.
        wg = load_tool("_write_guard")

        class _Handle:
            def __init__(self, doc):
                self._doc = doc
                self.dataFile = SimpleNamespace(id=doc.urn) if doc.urn else None
                self.isValid = True

            def __eq__(self, other):
                return isinstance(other, _Handle) and other._doc is self._doc
            __hash__ = None

        class _Doc:
            urn = None

        class _App:
            def __init__(self, doc):
                self._doc = doc

            @property
            def activeDocument(self):
                return _Handle(self._doc)

        doc = _Doc()
        monkeypatch.setattr(wg, "app", _App(doc))
        monkeypatch.setattr(wg, "_UNSAVED_DOC_SEQ", 0)
        wg._UNSAVED_DOC_KEYS.clear()
        try:
            monkeypatch.setattr(cc, "document_key", wg.document_key)
            handle, _total = cc.register_future(SimpleNamespace(numberOfOperations=1),
                                                "whole document", "document", True)
            assert registry[handle]["doc_key"] == "unsaved:1"
            doc.urn = "urn:lineage:saved"                  # the launch document is saved
            assert wg.document_key() == "urn:lineage:saved"
            assert registry[handle]["doc_key"] == "urn:lineage:saved"
        finally:
            wg._UNSAVED_DOC_KEYS.clear()


# --- the machine library: the label vocabulary, the query, and the catalog ---

def _machine_stub(vendor, model, description=None, capabilities=None, simulation=False):
    """An adsk.cam.Machine's readable surface - note it carries NO .name."""
    return SimpleNamespace(vendor=vendor, model=model, description=description,
                           capabilities=capabilities, hasSimulationModel=simulation)


def _caps(milling=False, turning=False, cutting=False, additive=False):
    return SimpleNamespace(isMillingSupported=milling, isTurningSupported=turning,
                           isCuttingSupported=cutting, isAdditiveSupported=additive)


# The two NON-NETWORK locations _MACHINE_LOCATIONS searches, read from the seeded enum.
_LOC_LOCAL = adsk.cam.LibraryLocations.LocalLibraryLocation
_LOC_F360 = adsk.cam.LibraryLocations.Fusion360LibraryLocation


def _machine_lib(pools, raises=()):
    """A MachineLibrary whose createQuery serves one pool per location (the live query
    prefix-matches the MODEL field); a location named in `raises` fails the way an unreachable
    library location does."""
    def create_query(loc, vendor, model):
        if loc in raises:
            raise RuntimeError("library location unreachable")
        pool = [m for m in pools.get(loc, [])
                if (not vendor or (m.vendor or "").lower() == vendor.lower())
                and (not model or (m.model or "").lower().startswith(model.lower()))]
        return SimpleNamespace(execute=lambda: pool)
    return SimpleNamespace(createQuery=create_query)


@pytest.fixture
def install_library(monkeypatch):
    def _install(lib):
        holder = SimpleNamespace(libraryManager=SimpleNamespace(machineLibrary=lib))
        monkeypatch.setattr(adsk.cam.CAMManager, "get", staticmethod(lambda: holder), raising=False)
    return _install


@pytest.fixture
def drop_local_location(monkeypatch):
    """A build whose LibraryLocations does not carry the Local member (getattr -> None)."""
    monkeypatch.delattr(adsk.cam.LibraryLocations, "LocalLibraryLocation", raising=False)


class TestMachineLabel:
    def test_the_description_is_the_label(self):
        assert cc.machine_label(_machine_stub("Haas", "VF-2", "Haas VF-2 with TRT100")) == \
            "Haas VF-2 with TRT100"

    def test_it_falls_back_to_vendor_model(self):
        assert cc.machine_label(_machine_stub("Haas", "VF-2")) == "Haas VF-2"

    def test_a_machine_with_no_readable_identity_still_labels(self):
        assert cc.machine_label(_machine_stub("", "")) == "(unnamed machine)"

    def test_no_machine_is_none_not_a_placeholder(self):
        assert cc.machine_label(None) is None

    def test_ident_is_label_vendor_model(self):
        assert cc.machine_ident(_machine_stub("Haas", "VF-2", "Haas VF-2 with TRT100")) == \
            ("Haas VF-2 with TRT100", "Haas", "VF-2")


class TestQueryMachines:
    def test_the_first_location_that_yields_wins(self):
        # Local before Fusion360: the user's own machine must not be listed beside a bundled
        # namesake, which would read as an ambiguity that is not one.
        lib = _machine_lib({_LOC_LOCAL: [_machine_stub("Haas", "VF-2", "Haas VF-2")],
                            _LOC_F360: [_machine_stub("Haas", "VF-2", "Haas VF-2 (bundled)")]})
        assert [t[1] for t in cc.query_machines(lib, "Haas", "VF-2")] == ["Haas VF-2"]

    def test_it_falls_through_to_fusion360_when_local_holds_nothing(self):
        lib = _machine_lib({_LOC_LOCAL: [],
                            _LOC_F360: [_machine_stub("Haas", "VF-2", "Haas VF-2")]})
        assert [t[1] for t in cc.query_machines(lib, "Haas", "VF-2")] == ["Haas VF-2"]

    def test_a_failing_location_is_skipped_not_fatal(self):
        lib = _machine_lib({_LOC_F360: [_machine_stub("Haas", "VF-2", "Haas VF-2")]},
                           raises=(_LOC_LOCAL,))
        assert [t[1] for t in cc.query_machines(lib, "Haas", "VF-2")] == ["Haas VF-2"]

    def test_a_location_this_build_does_not_carry_is_skipped(self, drop_local_location):
        lib = _machine_lib({_LOC_LOCAL: [_machine_stub("Haas", "VF-2", "local only")],
                            _LOC_F360: [_machine_stub("Haas", "VF-2", "Haas VF-2")]})
        assert [t[1] for t in cc.query_machines(lib, "Haas", "VF-2")] == ["Haas VF-2"]

    def test_identical_labels_in_one_location_are_listed_once(self):
        lib = _machine_lib({_LOC_LOCAL: [_machine_stub("Haas", "VF-2", "Haas VF-2"),
                                         _machine_stub("Haas", "VF-2", "Haas VF-2")]})
        assert len(cc.query_machines(lib, "Haas", "VF-2")) == 1


class TestMachineCatalog:
    def _pools(self):
        return {_LOC_LOCAL: [_machine_stub("Haas", "VF-2", "Haas VF-2",
                                           capabilities=_caps(milling=True), simulation=True)],
                _LOC_F360: [_machine_stub("Ultimaker", "S5", "Ultimaker S5",
                                          capabilities=_caps(additive=True))]}

    def test_rows_carry_the_location_and_the_capability_kinds(self, install_library):
        install_library(_machine_lib(self._pools()))
        rows, truncated, err = cc.machine_catalog()
        assert err is None and truncated is False
        assert rows[0] == {"name": "Haas VF-2", "vendor": "Haas", "model": "VF-2",
                           "location": "local", "kind": ["milling"], "simulation_ready": True}
        assert rows[1]["location"] == "fusion360" and rows[1]["kind"] == ["additive"]
        assert rows[1]["simulation_ready"] is False

    def test_machine_type_narrows_to_that_kind(self, install_library):
        # the bundled library is dominated by additive printers, so an unfiltered read floods.
        install_library(_machine_lib(self._pools()))
        rows, truncated, err = cc.machine_catalog(machine_type="milling")
        assert err is None and [r["name"] for r in rows] == ["Haas VF-2"]

    def test_an_unknown_machine_type_is_refused_naming_the_valid_ones(self, install_library):
        install_library(_machine_lib(self._pools()))
        rows, truncated, err = cc.machine_catalog(machine_type="welding")
        assert rows is None and "welding" in err
        assert "additive, cutting, milling, turning" in err

    def test_the_row_cap_truncates_while_the_total_stays_honest(self, install_library):
        install_library(_machine_lib(self._pools()))
        rows, truncated, err = cc.machine_catalog(max_results=1)
        assert err is None and len(rows) == 1 and truncated is True

    def test_a_location_this_build_does_not_carry_is_skipped(self, install_library,
                                                             drop_local_location):
        install_library(_machine_lib(self._pools()))
        rows, _truncated, err = cc.machine_catalog()
        assert err is None and [r["name"] for r in rows] == ["Ultimaker S5"]

    def test_a_failing_location_does_not_sink_the_other(self, install_library):
        install_library(_machine_lib(self._pools(), raises=(_LOC_LOCAL,)))
        rows, _truncated, err = cc.machine_catalog()
        assert err is None and [r["name"] for r in rows] == ["Ultimaker S5"]

    def test_a_name_both_libraries_hold_is_marked_on_both_rows(self, install_library):
        # Machine.id is the description, so the two copies read one identity: the name addresses
        # two machines and a setup carrying it does not say which copy that is.
        install_library(_machine_lib({
            _LOC_LOCAL: [_machine_stub("Haas", "VF-2", "Haas VF-2",
                                       capabilities=_caps(milling=True))],
            _LOC_F360: [_machine_stub("Haas", "VF-2", "Haas VF-2",
                                      capabilities=_caps(milling=True))]}))
        rows, _truncated, err = cc.machine_catalog()
        assert err is None and len(rows) == 2
        assert all(r["name_in_both_locations"] is True for r in rows)
        assert {r["location"] for r in rows} == {"local", "fusion360"}

    def test_two_rows_in_ONE_location_sharing_a_name_are_not_a_collision(self, monkeypatch,
                                                                        install_library):
        # The flag is about the SAME name in two libraries; two Local machines sharing a name is a
        # duplicate within one library, which the location says nothing about.
        install_library(_machine_lib({_LOC_LOCAL: [
            _machine_stub("Haas", "VF-2", "Haas VF-2", capabilities=_caps(milling=True)),
            _machine_stub("Haas", "VF-2b", "Haas VF-2", capabilities=_caps(milling=True))]}))
        rows, _truncated, err = cc.machine_catalog()
        assert err is None and len(rows) == 2
        assert not any("name_in_both_locations" in r for r in rows)
        assert {r["location"] for r in rows} == {"local"}

    def test_an_unreachable_library_is_an_error_not_an_empty_catalog(self, monkeypatch):
        def _boom():
            raise RuntimeError("library manager unavailable")
        monkeypatch.setattr(adsk.cam.CAMManager, "get", staticmethod(_boom), raising=False)
        rows, truncated, err = cc.machine_catalog()
        assert rows is None and truncated is False
        assert "Could not access the machine library" in err
        assert "library manager unavailable" in err


class TestResolveMachineLibraryFailure:
    def test_an_unreachable_library_is_reported_not_swallowed(self, monkeypatch):
        # returning "no machine matches" here would send the agent renaming a machine that is
        # actually there, over a library that could not be opened at all.
        def _boom():
            raise RuntimeError("library manager unavailable")
        monkeypatch.setattr(adsk.cam.CAMManager, "get", staticmethod(_boom), raising=False)
        machine, label, err = cc.resolve_machine("Haas VF-2")
        assert machine is None and label is None
        assert "Could not access the machine library" in err
        assert "library manager unavailable" in err


class TestResolveMachineByDescription:
    """The library query keys on the MODEL field, so a machine whose selectable name is carried by
    its DESCRIPTION alone matches no query - the catalog walk reaches it. The pool here is the
    measured shape of the bundled generic 5-axis machines: one model, three descriptions. Every
    name the ambiguity refusal advertises has to resolve on its own."""

    _MODEL = "Generic 5-axis (AC Table-Table)"
    _DESCRIPTIONS = (
        "This machine has AC axis on the Table and XYZ axis on the Head",
        "This machine has AC axis on the Table and YXZ axis on the Head",
        "This machine has YXAC axis on the Table and Z axis on the Head",
    )

    def _pools(self):
        # Local is NOT empty: a description walk that stops at the first location holding anything
        # never reaches the bundled machines.
        return {_LOC_LOCAL: [_machine_stub("Haas", "VF-2", "Haas VF-2")],
                _LOC_F360: [_machine_stub("Autodesk", self._MODEL, d) for d in self._DESCRIPTIONS]}

    def _advertised_lines(self):
        """The lines the ambiguity refusal tells the caller to pass back."""
        _m, _label, err = cc.resolve_machine(self._MODEL)
        listed = err.partition("matches: ")[2].partition(". Pass the full line")[0]
        return [s.strip() for s in listed.split(", ")]

    def test_the_shared_model_still_refuses_listing_the_three(self, install_library):
        install_library(_machine_lib(self._pools()))
        m, label, err = cc.resolve_machine(self._MODEL)
        assert m is None and label is None
        assert "3 matches" in err and "Pass the full line as shown" in err
        assert self._advertised_lines() == [f"{d} [Autodesk|{self._MODEL}]"
                                            for d in self._DESCRIPTIONS]

    def test_every_advertised_line_resolves_to_its_own_machine(self, install_library):
        install_library(_machine_lib(self._pools()))
        lines = self._advertised_lines()
        assert len(lines) == 3
        for line in lines:
            m, label, err = cc.resolve_machine(line)
            assert err is None, line
            assert m.description == label and line.startswith(label)

    def test_the_description_alone_still_resolves_where_it_is_unique(self, install_library):
        install_library(_machine_lib(self._pools()))
        for d in self._DESCRIPTIONS:
            m, label, err = cc.resolve_machine(d)
            assert err is None, d
            assert label == d and m.description == d

    def test_the_vendor_qualified_label_resolves_too(self, install_library):
        install_library(_machine_lib(self._pools()))
        m, label, err = cc.resolve_machine("Autodesk|" + self._DESCRIPTIONS[1])
        assert err is None and label == self._DESCRIPTIONS[1]
        m2, label2, err2 = cc.resolve_machine("Autodesk/" + self._DESCRIPTIONS[1])
        assert err2 is None and label2 == self._DESCRIPTIONS[1]

    def test_a_local_label_and_a_vendor_model_still_resolve(self, install_library):
        # The decoy is a bundled machine DESCRIBED 'VF-2': the (vendor, model) query answers
        # 'Haas|VF-2' with the Local Haas, so a description match may never outrank it.
        pools = self._pools()
        pools[_LOC_F360] = pools[_LOC_F360] + [_machine_stub("Autodesk", "Generic mill", "VF-2")]
        install_library(_machine_lib(pools))
        m, label, err = cc.resolve_machine("Haas VF-2")
        assert err is None and label == "Haas VF-2" and m.model == "VF-2"
        m2, label2, err2 = cc.resolve_machine("Haas|VF-2")
        assert err2 is None and label2 == "Haas VF-2" and m2.vendor == "Haas"

    def test_a_description_no_machine_carries_still_misses(self, install_library):
        install_library(_machine_lib(self._pools()))
        m, label, err = cc.resolve_machine("This machine has BC axis on the Table")
        assert m is None and label is None
        assert "No machine matches" in err
        assert "Use the machine name (its description)" in err

    def test_a_failing_location_does_not_sink_the_description_walk(self, install_library):
        install_library(_machine_lib(self._pools(), raises=(_LOC_LOCAL,)))
        m, label, err = cc.resolve_machine(self._DESCRIPTIONS[0])
        assert err is None and label == self._DESCRIPTIONS[0]

    def test_one_description_in_both_locations_resolves_local_first(self, install_library):
        # Deduped by label, Local before Fusion360 - a user's copy of a bundled machine is one
        # selectable name, not an ambiguity.
        pools = self._pools()
        pools[_LOC_LOCAL] = [_machine_stub("Shop", "Mill-1", self._DESCRIPTIONS[0])]
        install_library(_machine_lib(pools))
        m, label, err = cc.resolve_machine(self._DESCRIPTIONS[0])
        assert err is None and label == self._DESCRIPTIONS[0] and m.vendor == "Shop"

    def test_the_whole_request_outranks_the_half_after_the_separator(self, install_library):
        # Both halves of 'vendor|description' are matched against the label, so both can hit -
        # the label equal to the WHOLE request is the more specific one and wins.
        install_library(_machine_lib({_LOC_LOCAL: [_machine_stub("Shop", "Mill-1", "Sweep")],
                                      _LOC_F360: [_machine_stub("Autodesk", "Generic mill",
                                                                "Autodesk|Sweep")]}))
        m, label, err = cc.resolve_machine("Autodesk|Sweep")
        assert err is None and label == "Autodesk|Sweep" and m.vendor == "Autodesk"

    def test_a_location_this_build_does_not_carry_is_skipped(self, install_library,
                                                             drop_local_location):
        install_library(_machine_lib(self._pools()))
        m, label, err = cc.resolve_machine(self._DESCRIPTIONS[2])
        assert err is None and label == self._DESCRIPTIONS[2]

    def test_case_varied_requests_resolve(self, install_library):
        # The name is matched case-insensitively on BOTH halves - the description and, when one is
        # given, the vendor - so the case a caller retypes a listed name in decides nothing.
        install_library(_machine_lib(self._pools()))
        m, label, err = cc.resolve_machine(self._DESCRIPTIONS[0].upper())
        assert err is None and label == self._DESCRIPTIONS[0]
        m2, label2, err2 = cc.resolve_machine("aUtOdEsK|" + self._DESCRIPTIONS[1].upper())
        assert err2 is None and label2 == self._DESCRIPTIONS[1]

    def test_the_walk_stops_at_the_location_that_carries_the_name(self, install_library):
        # The catalog walk is unfiltered, so it ends at the location that answered every name it
        # was looking for: a Local hit leaves the bundled location unenumerated.
        pools = self._pools()
        pools[_LOC_LOCAL] = [_machine_stub("Shop", "Mill-1", self._DESCRIPTIONS[0])]
        lib, queried = _machine_lib(pools), []
        inner = lib.createQuery
        lib.createQuery = lambda loc, v, mo: (queried.append(loc), inner(loc, v, mo))[1]
        install_library(lib)
        m, _label, err = cc.resolve_machine(self._DESCRIPTIONS[0])
        assert err is None and m.vendor == "Shop"
        assert queried[-1] == _LOC_LOCAL


class TestResolveMachineSharedDescription:
    """MEASURED in the bundled library: SIX machines carry the model 'Generic 3-axis' with
    different descriptions, and the description 'This machine has XYZ axis on the Head' is carried
    by two machines (Generic 3-axis and Generic 3-axis Router). Neither half addresses one machine,
    so the refusal prints the whole identity per row - and that line is a request."""

    _SHARED = "This machine has XYZ axis on the Head"
    _OTHERS = ("This machine has YXZ axis on the Head",
               "This machine has X axis on the Table and YZ axis on the Head",
               "This machine has Y axis on the Table and XZ axis on the Head",
               "This machine has XY axis on the Table and Z axis on the Head",
               "This machine has YX axis on the Table and Z axis on the Head")

    def _family(self):
        """The six 'Generic 3-axis' machines plus the Router that shares one of their names."""
        return ([_machine_stub("Autodesk", "Generic 3-axis", self._SHARED)]
                + [_machine_stub("Autodesk", "Generic 3-axis", d) for d in self._OTHERS]
                + [_machine_stub("Autodesk", "Generic 3-axis Router", self._SHARED)])

    def _listed(self, err):
        return [s.strip() for s in
                err.partition("matches: ")[2].partition(". Pass the full line")[0].split(", ")]

    def test_one_description_on_two_machines_is_refused_not_collapsed(self, install_library):
        install_library(_machine_lib({_LOC_LOCAL: self._family()}))
        m, label, err = cc.resolve_machine(self._SHARED)
        assert m is None and label is None
        assert f"Ambiguous machine '{self._SHARED}' - 2 matches" in err
        assert self._listed(err) == [f"{self._SHARED} [Autodesk|Generic 3-axis]",
                                     f"{self._SHARED} [Autodesk|Generic 3-axis Router]"]

    def test_a_shared_vendor_model_is_refused_with_lines_that_resolve(self, install_library):
        # The bracket alone is NOT a key: six machines answer 'Autodesk|Generic 3-axis'. Every line
        # the refusal prints has to resolve to exactly one machine when handed straight back.
        install_library(_machine_lib({_LOC_LOCAL: self._family()}))
        _m, _label, err = cc.resolve_machine("Autodesk|Generic 3-axis")
        assert "Ambiguous machine 'Autodesk|Generic 3-axis'" in err
        assert "Pass the full line as shown." in err
        listed = self._listed(err)
        assert len(listed) >= 6
        seen = []
        for line in listed:
            m, label, e = cc.resolve_machine(line)
            assert e is None, line
            assert f"{label} [{m.vendor}|{m.model}]" == line
            assert m not in seen                      # one line, one machine - never a repeat pick
            seen.append(m)

    def test_the_line_tells_the_two_shared_description_machines_apart(self, install_library):
        install_library(_machine_lib({_LOC_LOCAL: self._family()}))
        m, _label, err = cc.resolve_machine(f"{self._SHARED} [Autodesk|Generic 3-axis Router]")
        assert err is None and m.model == "Generic 3-axis Router"
        m2, _l2, err2 = cc.resolve_machine(f"{self._SHARED} [Autodesk|Generic 3-axis]")
        assert err2 is None and m2.model == "Generic 3-axis"

    def test_a_line_naming_no_machine_misses_naming_the_description_and_the_pair(
            self, install_library):
        # The line is ONE request: splitting it on the '|' inside its own bracket would report a
        # vendor made of half the description, which names nothing the caller typed.
        install_library(_machine_lib({_LOC_LOCAL: self._family()}))
        m, label, err = cc.resolve_machine(f"{self._SHARED} [Haas|Generic 3-axis]")
        assert m is None and label is None
        assert f"No machine matches description '{self._SHARED}'" in err
        assert "with vendor|model 'Haas|Generic 3-axis'" in err
        assert f"vendor='{self._SHARED} [Haas'" not in err

    def test_a_unique_vendor_model_still_resolves(self, install_library):
        install_library(_machine_lib({_LOC_LOCAL: self._family()
                                      + [_machine_stub("Haas", "VF-2", "Haas VF-2")]}))
        m, label, err = cc.resolve_machine("Haas|VF-2")
        assert err is None and label == "Haas VF-2"

    def test_two_machines_of_one_identity_are_refused_naming_the_reason(self, install_library):
        # Nothing read tells these apart, so no line could address one of them.
        install_library(_machine_lib({_LOC_LOCAL: [_machine_stub("Autodesk", "Gen-5", "Twin"),
                                                   _machine_stub("Autodesk", "Gen-5", "Twin")]}))
        m, label, err = cc.resolve_machine("Twin [Autodesk|Gen-5]")
        assert m is None and label is None
        assert "2 machines carry that exact description, vendor and model" in err
        assert "Nothing read tells them apart" in err

    def test_two_vendors_sharing_a_description_are_told_apart_too(self, install_library):
        install_library(_machine_lib({_LOC_LOCAL: [_machine_stub("Haas", "GX-1", "Shop 5-axis"),
                                                   _machine_stub("DMG", "GY-2", "Shop 5-axis")]}))
        _m, _label, err = cc.resolve_machine("Shop 5-axis")
        assert "Shop 5-axis [Haas|GX-1]" in err and "Shop 5-axis [DMG|GY-2]" in err

    def test_the_same_description_in_both_locations_is_one_machine(self, install_library):
        # Local first, and the bundled namesake collapses onto it - the collapse query_machines
        # makes. MEASURED: 'Haas VF-2' is in the Local library AND the bundled one.
        install_library(_machine_lib({_LOC_LOCAL: [_machine_stub("Shop", "Mill-1", "Shop 5-axis")],
                                      _LOC_F360: [_machine_stub("Autodesk", "Gen-5", "Shop 5-axis")]}))
        m, label, err = cc.resolve_machine("Shop 5-axis")
        assert err is None and label == "Shop 5-axis" and m.vendor == "Shop"

    def test_a_description_local_answered_is_not_matched_again_in_the_bundled_library(
            self, install_library):
        # The cross-location collapse on a walk that has to keep going: the vendor-qualified
        # request leaves its other name unanswered by Local, so the bundled location is read too -
        # and the description Local already answered is not taken there a second time.
        install_library(_machine_lib({_LOC_LOCAL: [_machine_stub("Autodesk", "Mill-1", "Shop 5-axis")],
                                      _LOC_F360: [_machine_stub("Autodesk", "Gen-5", "Shop 5-axis")]}))
        m, label, err = cc.resolve_machine("Autodesk|Shop 5-axis")
        assert err is None and label == "Shop 5-axis" and m.model == "Mill-1"

    def test_the_vendor_half_picks_between_two_machines_of_one_description(self, install_library):
        # 'vendor|description' STATES the vendor, so the machine it resolves to has to carry it -
        # the Local namesake is passed over for the Autodesk machine the request named.
        install_library(_machine_lib({_LOC_LOCAL: [_machine_stub("Shop", "Mill-1", "Shop 5-axis")],
                                      _LOC_F360: [_machine_stub("Autodesk", "Gen-5", "Shop 5-axis")]}))
        m, label, err = cc.resolve_machine("Autodesk|Shop 5-axis")
        assert err is None and label == "Shop 5-axis" and m.vendor == "Autodesk"

    def test_a_vendor_no_machine_of_that_description_carries_is_a_miss(self, install_library):
        install_library(_machine_lib({_LOC_LOCAL: [_machine_stub("Shop", "Mill-1", "Shop 5-axis")]}))
        m, label, err = cc.resolve_machine("Autodesk|Shop 5-axis")
        assert m is None and label is None and "No machine matches" in err


class TestResolveMachineWalkBudget:
    """The description walk enumerates both locations unfiltered, and every request the query
    misses reaches it - a machine create's freeness pre-check included. It runs on a clock, and a
    walk that spends it answers that, because a catalog read that stopped early proves nothing
    about what the catalog holds."""

    def _pools(self):
        return {_LOC_LOCAL: [_machine_stub("Haas", "VF-2", "Haas VF-2")],
                _LOC_F360: [_machine_stub("Autodesk", "Gen-5", "Shop 5-axis")]}

    def test_a_spent_clock_refuses_and_is_not_worded_as_a_miss(self, install_library, monkeypatch):
        # cam_create_machine reads the 'No machine matches' opening as PROOF that a name is free
        # (its _NAME_FREE_PREFIX), so a walk that never finished may not answer with it.
        monkeypatch.setattr(cc, "_MACHINE_WALK_BUDGET_S", -1.0)
        install_library(_machine_lib(self._pools()))
        m, label, err = cc.resolve_machine("Shop 5-axis")
        assert m is None and label is None
        assert "Timed out" in err and "UNKNOWN" in err
        assert not err.startswith("No machine matches")

    def test_the_clock_does_not_reach_a_name_the_query_answers(self, install_library, monkeypatch):
        # The walk runs only where the query missed, so its budget can never refuse a queried name.
        monkeypatch.setattr(cc, "_MACHINE_WALK_BUDGET_S", -1.0)
        install_library(_machine_lib(self._pools()))
        m, label, err = cc.resolve_machine("Haas|VF-2")
        assert err is None and label == "Haas VF-2"


# ── partial reads: a walk that dies mid-iteration is INCOMPLETE, never complete ──

def _dying_after(items):
    """A collection that yields `items` then raises - the platform's mid-iteration failure."""
    def _gen():
        yield from items
        raise RuntimeError("collection died mid-walk")
    return _gen()


class _DyingCollection(_NamedCollection):
    """A count/item collection that promises one more item than item() will hand over - the
    mid-walk failure in the protocol iter_collection actually reads."""

    @property
    def count(self):
        return len(self._items) + 1

    def item(self, i):
        if i >= len(self._items):
            raise RuntimeError("collection died mid-walk")
        return super().item(i)


class TestPartialReadsAreFlaggedIncomplete:
    # _model_names / _references_in take the GETTER, so a property that raises before yielding any
    # collection at all is a THIRD kind of incompleteness beside the cap and the dying walk.
    def test_model_names_from_a_dying_collection_read_truncated(self):
        dying = _dying_after([SimpleNamespace(name="A"), SimpleNamespace(name="B")])
        names, truncated = cr._model_names(lambda: dying)
        assert names == ["A", "B"]                 # the partial read is kept, not blanked
        assert truncated is True                   # incomplete, not a full read

    def test_a_complete_model_walk_stays_untruncated(self):
        names, truncated = cr._model_names(lambda: iter([SimpleNamespace(name="A")]))
        assert names == ["A"] and truncated is False

    def test_operations_from_a_dying_walk_read_truncated(self):
        # the ops walk reads count/item (iter_collection's protocol), so the dying collection here
        # promises one more item than it can hand over - the rows already read must survive.
        ops = [FakeOperation("Op1"), FakeOperation("Op2")]
        setup = SimpleNamespace(allOperations=_DyingCollection(ops))
        summaries, truncated = cr._operations_in(setup)
        assert [s["name"] for s in summaries] == ["Op1", "Op2"]
        assert truncated is True

    def test_references_from_a_dying_walk_read_truncated(self):
        dying = _dying_after([])
        found, truncated = cr._references_in(lambda: dying, "model")
        assert found == [] and truncated is True

    def test_a_raising_property_is_incomplete_in_both_walks(self):
        def _boom():
            raise RuntimeError("3 : input is null")
        assert cr._model_names(_boom) == (None, True)      # null names, never []
        assert cr._references_in(_boom, "model") == ([], True)



# ── parse_parameters: the ONE {name: expression} / 'name=value, ...' request parser ──────────────
#
# cam_edit_operation and cam_edit_setup both validate their 'parameters' request through this, so a
# form one accepts must be the form the other accepts.

class TestParseParameters:
    def test_a_dict_is_normalized_to_string_expressions(self):
        wanted, err = cc.parse_parameters({" tool_feedCutting ": 3000, "maximumStepdown": 1.5})
        assert err is None
        assert wanted == {"tool_feedCutting": "3000", "maximumStepdown": "1.5"}

    def test_a_blank_key_is_dropped_not_kept_as_an_empty_name(self):
        wanted, err = cc.parse_parameters({"  ": "5", "tool_stepover": "1"})
        assert err is None and wanted == {"tool_stepover": "1"}

    def test_a_name_equals_value_string_parses_and_strips(self):
        wanted, err = cc.parse_parameters("tool_spindleSpeed=12000, tool_stepover = 1.5")
        assert err is None
        assert wanted == {"tool_spindleSpeed": "12000", "tool_stepover": "1.5"}

    def test_blank_chunks_from_stray_commas_are_skipped(self):
        wanted, err = cc.parse_parameters("tool_stepover=1.5, , tool_feedCutting=900,")
        assert err is None
        assert wanted == {"tool_stepover": "1.5", "tool_feedCutting": "900"}

    def test_a_chunk_without_an_equals_is_refused_naming_it(self):
        wanted, err = cc.parse_parameters("tool_stepover 1.5")
        assert wanted is None
        assert "tool_stepover 1.5" in err and "name=value" in err

    def test_a_value_containing_an_equals_keeps_its_tail(self):
        # partition, not split: an expression may legitimately carry '=' after the first one
        wanted, err = cc.parse_parameters("expr=a==b")
        assert err is None and wanted == {"expr": "a==b"}

    def test_neither_a_dict_nor_a_string_is_refused(self):
        wanted, err = cc.parse_parameters(42)
        assert wanted is None and "object" in err


# ── the shared CAM library folder walk: library_children / walk_library_folders / library_assets ──
#
# The tool, post and template libraries all nest folders under a LibraryLocations root. One bounded
# traversal serves all three; each site passes its own leaf op.

def _url(text):
    """A fake library URL: .leafName (the segment after the last '/') plus .toString()."""
    return SimpleNamespace(leafName=text.rstrip("/").rsplit("/", 1)[-1], toString=lambda: text)


def _library(tree, kind="childAssetURLs"):
    """A fake library over `tree`: {url string: (child_folder_urls, child_leaf_items)}."""
    return SimpleNamespace(
        childFolderURLs=lambda u: list(tree.get(u.toString(), ([], []))[0]),
        **{kind: lambda u: list(tree.get(u.toString(), ([], []))[1])})


class TestLibraryChildren:
    def test_a_raising_accessor_reads_as_no_children_not_a_crash(self):
        def _boom(_u):
            raise RuntimeError("cloud enumeration failed")
        lib = SimpleNamespace(childFolderURLs=_boom)
        assert cc.library_children(lib, _url("root"), "childFolderURLs") == []

    def test_an_accessor_this_library_kind_does_not_carry_reads_as_none(self):
        # a tool library has no childTemplates - asking for one must answer empty, not AttributeError
        lib = SimpleNamespace(childAssetURLs=lambda u: [_url("L")])
        assert cc.library_children(lib, _url("root"), "childTemplates") == []


class TestWalkLibraryFolders:
    def test_the_root_itself_is_visited_and_every_nested_folder_after_it(self):
        root, sub, deep = _url("root"), _url("root/sub"), _url("root/sub/deep")
        lib = _library({"root": ([sub], []), "root/sub": ([deep], []), "root/sub/deep": ([], [])})
        seen = []
        truncated = cc.walk_library_folders(lib, root, lambda u: seen.append(u.leafName))
        assert seen == ["root", "sub", "deep"]     # the root counts as a folder
        assert truncated is False

    def test_an_absent_root_walks_nothing(self):
        lib = _library({})
        seen = []
        assert cc.walk_library_folders(lib, None, lambda u: seen.append(u)) is False
        assert seen == []

    def test_a_tree_that_never_bottoms_out_stops_at_the_depth_cap(self):
        deep = _url("deep")
        lib = SimpleNamespace(childFolderURLs=lambda u: [deep], childAssetURLs=lambda u: [])
        seen = []
        truncated = cc.walk_library_folders(lib, deep, lambda u: seen.append(u), max_depth=6)
        assert len(seen) == 7                      # depths 0..6, then the guard
        assert truncated is True                   # and the read says it is INCOMPLETE

    def test_the_folder_budget_caps_a_wide_tree_and_reports_truncated(self):
        root = _url("root")
        kids = [_url(f"root/{i}") for i in range(10)]
        lib = _library({"root": (kids, [])})
        seen = []
        truncated = cc.walk_library_folders(lib, root, lambda u: seen.append(u), max_folders=4)
        assert len(seen) == 4 and truncated is True

    def test_a_visit_that_reports_itself_full_stops_the_whole_walk(self):
        root, a, b = _url("root"), _url("root/a"), _url("root/b")
        lib = _library({"root": ([a, b], [])})
        seen = []

        def visit(u):
            seen.append(u.leafName)
            return u.leafName == "a"               # full once 'a' is read
        assert cc.walk_library_folders(lib, root, visit) is True
        assert seen == ["root", "a"]               # 'b' is never reached


class TestLibraryAssets:
    def test_assets_nested_in_folders_are_collected(self):
        # Hub/Cloud NEST their libraries; a walk reading only the root's own assets finds none
        root, sub = _url("root"), _url("root/Team")
        lib = _library({"root": ([sub], []), "root/Team": ([], [_url("root/Team/Team Mill")])})
        assets, truncated = cc.library_assets(lib, root)
        assert [a.leafName for a in assets] == ["Team Mill"] and truncated is False

    def test_the_walk_is_depth_bounded(self):
        deep = _url("deep")
        lib = SimpleNamespace(childFolderURLs=lambda u: [deep],
                              childAssetURLs=lambda u: [_url("L")])
        assets, truncated = cc.library_assets(lib, deep)
        assert len(assets) == 7 and truncated is True     # depths 0..6, then the guard

    def test_an_absent_root_collects_nothing(self):
        lib = SimpleNamespace(childFolderURLs=lambda u: [_url("f")],
                              childAssetURLs=lambda u: [_url("L")])
        assert cc.library_assets(lib, None) == ([], False)

    def test_max_assets_caps_the_collection_and_reports_truncated(self):
        root, sub = _url("root"), _url("root/sub")
        lib = _library({"root": ([sub], [_url("A"), _url("B")]),
                        "root/sub": ([], [_url("C"), _url("D")])})
        assets, truncated = cc.library_assets(lib, root, max_assets=3)
        assert [a.leafName for a in assets] == ["A", "B", "C"] and truncated is True

    def test_a_complete_read_under_the_cap_is_not_flagged_truncated(self):
        root = _url("root")
        lib = _library({"root": ([], [_url("A"), _url("B")])})
        assets, truncated = cc.library_assets(lib, root, max_assets=3)
        assert [a.leafName for a in assets] == ["A", "B"] and truncated is False


# ── addressing ONE asset by name: asset_leaf / asset_key / asset_leaf_keys / assets_named ────────
#
# The matcher every library DELETE resolves its target on (cam_delete_machine, cam_delete_template).
# A stored asset's leafName carries the file extension the object's own name does not, so the stem
# has to answer too - and both comparisons stay EXACT, because a substring match here deletes the
# neighbour whose name merely starts the same.

class TestAssetLeafKeys:
    """The names ONE asset answers to. Pinned directly because one of them is not observable
    through either delete handler: an EMPTY key can only match a wanted name that is empty, and no
    name a handler can reach is - so the guard that keeps '' out of the set is asserted here or
    nowhere."""

    def test_an_extension_less_leaf_answers_to_itself_alone(self):
        assert cc.asset_leaf_keys(_url("root/SweepMach")) == {"sweepmach"}

    def test_a_stored_leaf_answers_to_both_its_name_and_its_stem(self):
        assert cc.asset_leaf_keys(_url("root/SweepMach.mch")) == {"sweepmach.mch", "sweepmach"}

    def test_a_dotted_name_keeps_everything_before_the_LAST_dot(self):
        # 'Mill v1.2' stored as 'Mill v1.2.mch': a FIRST-dot split reads the stem as 'Mill v1' and
        # the asset becomes unreachable by its own name.
        assert cc.asset_leaf_keys(_url("root/Mill v1.2.mch")) == {"mill v1.2.mch", "mill v1.2"}

    def test_a_leafName_that_does_not_read_contributes_no_stem(self):
        # safe() answers None, asset_leaf answers '' - and '' must not become a stem key that any
        # empty wanted-name would match.
        bad = SimpleNamespace(toString=lambda: "root/x")
        assert cc.asset_leaf(bad) == ""
        assert cc.asset_leaf_keys(bad) == {""}


class TestAssetsNamed:
    def test_an_asset_is_reached_by_the_STEM_of_its_stored_leaf(self):
        hits = cc.assets_named([_url("root/GyroTmpl.f3dhsm-template")], {"gyrotmpl"})
        assert [a.leafName for a in hits] == ["GyroTmpl.f3dhsm-template"]

    def test_an_asset_stored_without_an_extension_is_reached_by_its_whole_leaf(self):
        hits = cc.assets_named([_url("root/GyroTmpl")], {"gyrotmpl"})
        assert [a.leafName for a in hits] == ["GyroTmpl"]

    def test_a_neighbour_whose_name_merely_STARTS_the_same_is_not_a_hit(self):
        assets = [_url("root/GyroTmpl Mk2.f3dhsm-template"), _url("root/GyroTmplExtra")]
        assert cc.assets_named(assets, {"gyrotmpl"}) == []

    def test_the_match_is_case_insensitive_on_both_sides(self):
        hits = cc.assets_named([_url("root/GyroTmpl.mch")], {"gyrotmpl"})
        assert len(hits) == 1

    def test_two_urls_for_ONE_asset_read_as_one_candidate(self):
        # de-dup is by url STRING: the same address arriving twice must not read as a duplicate the
        # delete then refuses to guess between.
        same = [_url("root/A.mch"), _url("root/A.mch")]
        assert len(cc.assets_named(same, {"a"})) == 1

    def test_two_assets_of_one_name_in_DIFFERENT_folders_stay_two_candidates(self):
        both = [_url("root/A.mch"), _url("root/Mills/A.mch")]
        assert len(cc.assets_named(both, {"a"})) == 2

    def test_any_of_the_wanted_names_matches(self):
        # the delete searches by the name it was asked for AND the name the object resolved to
        hits = cc.assets_named([_url("root/VF-2.mch")], {"shop mill #3", "vf-2"})
        assert [a.leafName for a in hits] == ["VF-2.mch"]

    def test_asset_key_falls_back_to_the_object_when_the_url_does_not_stringify(self):
        # two urls for one asset must not read as two candidates just because toString raised
        class _Mute:
            leafName = "A.mch"

            def toString(self):
                raise RuntimeError("url is not addressable")
        mute = _Mute()
        assert cc.asset_key(mute) is mute


# ── the machine's own limits: spindle speed + axis travels off its kinematics tree ───────────────
#
# The route under test is the SUPPORTED one: Machine.elements -> the kinematics element by its
# staticTypeId -> parts (a tree). Machine.kinematics reaches the same tree and is flagged "not
# officially supported", so a test below proves it is never read.


class _MachSpindle:
    def __init__(self, max_speed=12000.0, min_speed=0.0):
        self.maxSpeed = max_speed
        self.minSpeed = min_speed


class _MachStation:
    def __init__(self, diameter=0.0, length=0.0):
        self.maxToolDiameter = diameter
        self.maxToolLength = length


class _MachAxis:
    """A MachineAxis. The axis TYPE decides the unit its range carries (cm for linear, radians for
    rotary), so it is read from the same enum production compares against, never hand-typed."""

    _KINDS = {"linear": "LinearMachineAxisType", "rotary": "RotaryMachineAxisType"}

    def __init__(self, name, kind="linear", lo=0.0, hi=0.0, infinite=False, has_limits=True):
        self.name = name
        self.axisType = (getattr(adsk.cam.MachineAxisTypes, self._KINDS[kind])
                         if kind in self._KINDS else object())
        self.hasLimits = has_limits
        self.physicalRange = SimpleNamespace(min=lo, max=hi, isInfinite=infinite)


class _MachPart:
    def __init__(self, axis=None, spindle=None, station=None, children=()):
        self.axis = axis
        self.spindle = spindle
        self.toolStation = station
        self.children = _NamedCollection(list(children))


class _MachElements:
    """MachineElements: the type-filtered accessors a caller reaches the kinematics element by."""

    def __init__(self, parts, has_kinematics=True, default_answers=True):
        self._parts = _NamedCollection(list(parts))
        self._has = has_kinematics
        self._default_answers = default_answers
        self.asked = []

    def _element(self):
        return SimpleNamespace(parts=self._parts)

    def defaultItemByType(self, type_id):
        self.asked.append(type_id)
        return self._element() if (self._has and self._default_answers) else None

    def itemsByType(self, type_id):
        self.asked.append(type_id)
        return [self._element()] if self._has else []


class _Machine(FakeMachine):
    """A machine whose kinematics route - the one the bindings flag "not officially supported" -
    COUNTS every read, so a test can prove it is never taken."""

    def __init__(self, elements, description="Haas with A-axis"):
        super().__init__(description=description, elements=elements)
        self.unsupported_reads = 0

    @property
    def kinematics(self):
        self.unsupported_reads += 1
        return None


def _haas():
    """The measured Haas A-axis shape: a Z axis carrying the spindle head, then Y, X and an
    unbounded A axis nested under each other."""
    head = _MachPart(spindle=_MachSpindle(12000.0), station=_MachStation())
    a_axis = _MachPart(axis=_MachAxis("A", kind="rotary", lo=-math.inf, hi=math.inf, infinite=True,
                                      has_limits=False))
    x_axis = _MachPart(axis=_MachAxis("X", lo=-76.2, hi=0.0), children=[a_axis])
    y_axis = _MachPart(axis=_MachAxis("Y", lo=-40.6, hi=0.0), children=[x_axis])
    z_axis = _MachPart(axis=_MachAxis("Z", lo=-50.8, hi=0.0), children=[head])
    return _Machine(_MachElements([_MachPart(), z_axis, y_axis]))


def _MachineSetup(name, machine=None):
    """A setup whose assigned machine the limits read walks."""
    return FakeSetup(name, machine=machine)


class TestMachineLimits:
    def test_spindle_max_and_axis_travels_read_in_mm(self, install):
        install(FakeCAM([_MachineSetup("Op1", _haas())]))
        out = _payload(cr.get_machine_limits_handler())
        rec = out["setups"][0]
        assert rec["machine"] == "Haas with A-axis"
        assert rec["kinematics_readable"] is True
        assert rec["spindle"]["max_rpm"] == 12000.0
        travels = {a["name"]: a.get("travel") for a in rec["axes"]}
        assert travels["X"] == 762.0 and travels["Y"] == 406.0 and travels["Z"] == 508.0
        assert out["units"] == "mm"

    def test_travels_scale_with_the_units_input(self, install):
        install(FakeCAM([_MachineSetup("Op1", _haas())]))
        out = _payload(cr.get_machine_limits_handler(units="in"))
        travels = {a["name"]: a.get("travel") for a in out["setups"][0]["axes"]}
        assert travels["X"] == 30.0                       # 76.2 cm = 30 in
        assert out["units"] == "in"

    def test_an_infinite_rotary_axis_publishes_no_travel_number(self, install):
        # -inf/+inf is not a travel, and it is not valid JSON for a strict client either.
        install(FakeCAM([_MachineSetup("Op1", _haas())]))
        out = _payload(cr.get_machine_limits_handler())
        a_axis = [a for a in out["setups"][0]["axes"] if a["name"] == "A"][0]
        assert a_axis["is_infinite"] is True and a_axis["has_limits"] is False
        assert "travel" not in a_axis and "travel_deg" not in a_axis

    def test_a_bounded_rotary_axis_reads_in_degrees(self, install):
        rotary = _MachPart(axis=_MachAxis("B", kind="rotary", lo=0.0, hi=math.pi))
        install(FakeCAM([_MachineSetup("Op1", _Machine(_MachElements([rotary])))]))
        out = _payload(cr.get_machine_limits_handler())
        axis = out["setups"][0]["axes"][0]
        assert axis["kind"] == "rotary" and axis["travel_deg"] == 180.0
        assert "travel" not in axis                        # radians are never reported as a length

    def test_an_undecodable_axis_type_publishes_the_raw_range_unconverted(self, install):
        odd = _MachPart(axis=_MachAxis("W", kind="?", lo=-5.0, hi=5.0))
        install(FakeCAM([_MachineSetup("Op1", _Machine(_MachElements([odd])))]))
        axis = _payload(cr.get_machine_limits_handler())["setups"][0]["axes"][0]
        assert axis["kind"] is None and axis["range_raw"] == [-5.0, 5.0]
        assert "travel" not in axis and "travel_deg" not in axis

    def test_zero_tool_station_limits_are_not_published_as_limits(self, install):
        # measured: maxToolDiameter/maxToolLength read 0.0 on a machine whose spindle read 12000 -
        # publishing 0 would read as "no tool wider than nothing fits".
        install(FakeCAM([_MachineSetup("Op1", _haas())]))
        assert "tool_stations" not in _payload(cr.get_machine_limits_handler())["setups"][0]

    def test_a_real_tool_station_limit_is_published(self, install):
        part = _MachPart(station=_MachStation(diameter=5.0, length=10.0))
        install(FakeCAM([_MachineSetup("Op1", _Machine(_MachElements([part])))]))
        station = _payload(cr.get_machine_limits_handler())["setups"][0]["tool_stations"][0]
        assert station == {"max_tool_diameter": 50.0, "max_tool_length": 100.0, "units": "mm"}

    def test_the_unsupported_kinematics_shortcut_is_never_read(self, install):
        machine = _haas()
        install(FakeCAM([_MachineSetup("Op1", machine)]))
        _payload(cr.get_machine_limits_handler())
        assert machine.unsupported_reads == 0
        assert machine.elements.asked[0] == adsk.cam.KinematicsMachineElement.staticTypeId()

    def test_the_filtered_list_answers_when_there_is_no_default_element(self, install):
        machine = _Machine(_MachElements([_MachPart(spindle=_MachSpindle(8000.0))],
                                         default_answers=False))
        install(FakeCAM([_MachineSetup("Op1", machine)]))
        rec = _payload(cr.get_machine_limits_handler())["setups"][0]
        assert rec["spindle"]["max_rpm"] == 8000.0

    def test_a_machine_without_kinematics_says_so(self, install):
        machine = _Machine(_MachElements([], has_kinematics=False))
        install(FakeCAM([_MachineSetup("Op1", machine)]))
        rec = _payload(cr.get_machine_limits_handler())["setups"][0]
        assert rec["kinematics_readable"] is False and rec["axes"] == []

    def test_a_setup_with_no_machine_is_blocked_not_silent(self, install):
        install(FakeCAM([_MachineSetup("Op1", None)]))
        rec = _payload(cr.get_machine_limits_handler())["setups"][0]
        assert rec["machine"] is None and rec["blocked_by"] == ["no_machine_selected"]

    def test_the_setup_filter_scopes_the_read(self, install):
        install(FakeCAM([_MachineSetup("Op1", _haas()), _MachineSetup("Op2", None)]))
        out = _payload(cr.get_machine_limits_handler(setup="op2"))
        assert out["setup_count"] == 1 and out["setups"][0]["setup"] == "Op2"

    def test_a_duplicated_setup_name_is_refused(self, install):
        install(FakeCAM([_MachineSetup("Dup"), _MachineSetup("Dup")]))
        res = cr.get_machine_limits_handler(setup="Dup")
        assert res["isError"] is True and "ambiguous" in res["message"].lower()

    def test_unknown_units_are_refused_by_name(self, install):
        install(FakeCAM([_MachineSetup("Op1", _haas())]))
        res = cr.get_machine_limits_handler(units="cubits")
        assert res["isError"] is True and "cubits" in res["message"]

    def test_the_note_keeps_the_machine_dimension_trap_out_of_the_travels(self, install):
        install(FakeCAM([_MachineSetup("Op1", _haas())]))
        assert "machine_dimension_x/y/z" in _payload(cr.get_machine_limits_handler())["note"]

    def test_the_fastest_spindle_is_the_one_the_comparison_uses(self):
        machine = _Machine(_MachElements([_MachPart(spindle=_MachSpindle(6000.0)),
                                          _MachPart(spindle=_MachSpindle(24000.0))]))
        assert cc.machine_spindle_max(machine) == 24000.0
        limits = cc.machine_limits(machine, 10.0, "mm")
        assert limits["spindle"]["max_rpm"] == 24000.0 and limits["spindle_count"] == 2

    def test_a_zero_maxspeed_is_not_a_limit(self):
        # 0 rpm bounds nothing; reported as a limit it would make every operation over-max.
        machine = _Machine(_MachElements([_MachPart(spindle=_MachSpindle(0.0))]))
        assert cc.machine_spindle_max(machine) is None
        assert cc.machine_limits(machine, 10.0, "mm")["spindle"]["max_rpm"] is None

    def test_no_machine_has_no_maximum(self):
        assert cc.machine_spindle_max(None) is None

    def test_the_parts_walk_is_depth_bounded(self):
        deep = _MachPart()
        deep.children = _NamedCollection([deep])                       # a self-referencing tree
        machine = _Machine(_MachElements([deep]))
        assert len(cc.kinematics_parts(machine)) == cc._MACHINE_PART_DEPTH + 1


# ── the per-operation "does it ask for more than the machine allows" comparison ──────────────────

def _SpindleParams(rpm):
    """The parameters the spindle check reads - rpm None is an operation carrying no
    tool_spindleSpeed at all."""
    return FakeCAMParameters(
        [] if rpm is None else [FakeCAMParameter("tool_spindleSpeed", value=rpm)])


def _SpindleOp(rpm=None, name="Op"):
    """An operation whose requested spindle speed the machine maximum is compared against."""
    op = FakeOperation(name)
    op.parameters = _SpindleParams(rpm)
    return op


class TestSpindleCheck:
    def test_over_the_maximum_is_true_with_both_numbers(self):
        over, requested, marker = cc.spindle_check(_SpindleOp(24999.0), 12000.0)
        assert over is True and requested == 24999.0 and marker is None

    def test_exactly_at_the_maximum_is_not_over(self):
        over, requested, marker = cc.spindle_check(_SpindleOp(12000.0), 12000.0)
        assert over is False and requested == 12000.0 and marker is None

    def test_one_rpm_over_the_maximum_is_over(self):
        assert cc.spindle_check(_SpindleOp(12000.1), 12000.0)[0] is True

    def test_under_the_maximum_is_false(self):
        assert cc.spindle_check(_SpindleOp(8000.0), 12000.0)[0] is False

    def test_an_unreadable_machine_maximum_is_null_with_a_marker(self):
        over, requested, marker = cc.spindle_check(_SpindleOp(24999.0), None)
        assert over is None and requested is None and marker == "machine_max_unavailable"

    def test_an_unreadable_op_speed_is_null_with_its_own_marker(self):
        over, requested, marker = cc.spindle_check(_SpindleOp(None), 12000.0)
        assert over is None and marker == "op_spindle_speed_unreadable"

    def test_an_op_without_parameters_reads_no_speed(self):
        assert cc.op_spindle_speed(SimpleNamespace(name="Op")) is None


# --- strategy entitlement: the read cam_generate's pre-flight and the readiness verdict share ---
#
# A strategy this installation will not generate reads isGenerationAllowed False on the
# document-independent OperationStrategy factory. False is an observed answer; a flag that would not
# read is None, and no exclusion may be made from that.

class TestStrategyGenerationAllowed:
    def test_the_three_answers_stay_apart(self, monkeypatch):
        monkeypatch.setattr(cc, "_create_strategy",
                            strategy_factory({"face": True, "chamfer": False, "swarf": None}))
        assert cc.strategy_generation_allowed("face") is True
        assert cc.strategy_generation_allowed("chamfer") is False
        # the strategy built but the flag would not read: unknown, never a confident False
        assert cc.strategy_generation_allowed("swarf") is None

    def test_an_unknown_strategy_name_reads_null_rather_than_blocked(self, monkeypatch):
        # createFromString RAISES '3 : Unknown strategy' for a renamed one - degrading that to False
        # would exclude a healthy operation from every launch.
        monkeypatch.setattr(cc, "_create_strategy", strategy_factory({"face": True}))
        assert cc.strategy_generation_allowed("renamed_strategy") is None

    def test_an_operation_with_no_readable_strategy_is_never_blocked(self, monkeypatch):
        monkeypatch.setattr(cc, "_create_strategy", strategy_factory({}))
        assert cc.strategy_generation_allowed("") is None
        assert cc.entitlement_flags([SimpleNamespace(name="NoStrategy")]) == [None]

    def test_each_distinct_strategy_is_probed_exactly_once(self, monkeypatch):
        # the whole-document walk asks per OPERATION; a probe per operation would multiply a live
        # API call by the job size for one answer per strategy.
        seen = {}
        monkeypatch.setattr(cc, "_create_strategy",
                            strategy_factory({"face": True, "chamfer": False}, seen))
        ops = [SimpleNamespace(name=f"Op{i}", strategy=s)
               for i, s in enumerate(("face", "face", "chamfer", "face"))]
        assert cc.entitlement_flags(ops) == [True, True, False, True]
        assert seen == {"face": 1, "chamfer": 1}

    def test_create_strategy_calls_the_document_independent_factory(self, monkeypatch):
        made = _Strategy(True)

        class _OS:
            @staticmethod
            def createFromString(name):
                _OS.asked = name
                return made

        monkeypatch.setattr(adsk.cam, "OperationStrategy", _OS)
        assert cc._create_strategy("swarf") is made and _OS.asked == "swarf"

    def test_the_probe_reads_only_measured_OperationStrategy_members(self):
        # a typo in either member reads nothing on a SWIG proxy and every flag publishes null
        # silently; api_surface.py is generated from the installed bindings.
        from api_surface import PROPERTIES
        assert {"createFromString", "isGenerationAllowed"} <= set(PROPERTIES["cam.OperationStrategy"])


class TestUnfinishedVerdictNamesTheBlockedOps:
    """The verdict for a scope with ops left to generate. 'run cam_generate to finish the rest' is
    circular advice over an operation cam_generate EXCLUDES, so those are named instead."""

    def _ops(self):
        # both still OUT OF DATE (state 1): the verdict only names a blocked op the scope is still
        # waiting on, so an under-specified fake would pass this test for the wrong reason.
        ops = [_tally_op("Cham", state=1), _tally_op("Face1", state=1)]
        ops[0].strategy, ops[1].strategy = "chamfer", "face"
        return ops

    def test_no_blocked_op_keeps_the_plain_pointer(self, monkeypatch):
        monkeypatch.setattr(cc, "_create_strategy",
                            strategy_factory({"chamfer": True, "face": True}))
        assert cc.unfinished_verdict("1 of 2 active ops valid", self._ops()) == \
            "1 of 2 active ops valid - run cam_generate to finish the rest."

    def test_a_blocked_op_is_named_and_the_circular_pointer_is_dropped(self, monkeypatch):
        monkeypatch.setattr(cc, "_create_strategy",
                            strategy_factory({"chamfer": False, "face": True}))
        verdict = cc.unfinished_verdict("1 of 2 active ops valid", self._ops())
        assert "Cham" in verdict and "isGenerationAllowed false" in verdict
        assert "run cam_generate to finish the rest" not in verdict

    def test_an_operation_whose_name_did_not_read_is_still_counted(self, monkeypatch):
        monkeypatch.setattr(cc, "_create_strategy", strategy_factory({"chamfer": False}))
        op = SimpleNamespace(strategy="chamfer", operationState=1, hasError=False,  # no .name
                             isGenerating=False, isSuppressed=False)
        assert cc._UNREAD_SEGMENT in cc.unfinished_verdict("0 of 1 active ops valid", [op])

    def test_a_blocked_op_that_already_generated_or_is_parked_is_not_named(self, monkeypatch):
        # the verdict answers "what is this scope waiting on": a blocked op reading VALID (generated
        # while the entitlement was there) or SUPPRESSED is not it, and naming those points the
        # caller away from the one that is actually stuck.
        monkeypatch.setattr(cc, "_create_strategy", strategy_factory({"chamfer": False}))
        done = _tally_op("ChamDone", state=0)
        parked = _tally_op("ChamParked", state=2, suppressed=True)
        stuck = _tally_op("ChamStuck", state=1)
        for op in (done, parked, stuck):
            op.strategy = "chamfer"
        verdict = cc.unfinished_verdict("1 of 3 active ops valid", [done, parked, stuck])
        assert "ChamStuck" in verdict
        assert "ChamDone" not in verdict and "ChamParked" not in verdict
        assert "1 operation(s) in scope" in verdict          # counted from the named ones only

    def test_the_document_readiness_names_them_through_this_verdict(self, monkeypatch,
                                                                    operation_cast_passthrough):
        # the consumer's own branch: live_readiness reaches the verdict only with ops left over.
        monkeypatch.setattr(cc, "_create_strategy",
                            strategy_factory({"chamfer": False, "face": True}))
        ops = [_tally_op("Cham", state=1), _tally_op("Face1")]
        ops[0].strategy, ops[1].strategy = "chamfer", "face"
        cam = SimpleNamespace(setups=_NamedCollection([_machined_setup(ops)]), ncPrograms=_NamedCollection([]))
        monkeypatch.setattr(cc, "get_cam", lambda: (cam, None))
        sig, err = cc.live_readiness()
        assert err is None and sig["out_of_date"] == 1
        assert "Cham" in sig["readiness"] and "EXCLUDES" in sig["readiness"]


class TestFuturesCount:
    """One handle can cover several Futures (a launch split around a blocked op), so its progress is
    the SUM of them - and an unread member on any one is unknown, never a smaller job."""

    def _future(self, ops, done):
        return SimpleNamespace(numberOfOperations=ops, numberOfCompleted=done)

    def test_the_counts_are_summed_over_every_future(self):
        futures = [self._future(2, 1), self._future(3, 3)]
        assert cc.futures_count(futures, "numberOfOperations") == 5
        assert cc.futures_count(futures, "numberOfCompleted") == 4

    def test_one_unread_member_makes_the_whole_count_unknown(self):
        # numberOfOperations raises "Generation not started" until generation spins up; summing the
        # readable ones alone would publish a job smaller than the handle covers.
        class _NoCount:
            numberOfCompleted = 0

            @property
            def numberOfOperations(self):
                raise RuntimeError("Generation not started")

        assert cc.futures_count([self._future(2, 1), _NoCount()], "numberOfOperations") is None

    def test_a_single_future_still_reads_its_own_count(self):
        assert cc.futures_count([self._future(4, 2)], "numberOfOperations") == 4
