"""Unit tests for ``cam_edit_operation.py`` — set CAM operation parameters (feeds/speeds/stepdown/...).

This closes the 'feeds/speeds/depths/tool are unreachable' gap: it sets named operation parameters by
expression. Covers param dispatch (dict + 'name=value' string forms), before/after reporting, the
unknown-param guard, the unknown-operation guard, that nothing is set when a value is invalid, and
the evaluation read-back: a stored-but-unevaluated expression (.error set, .value.value a finite 0.0)
rolls back ALL params in the call; .warning fires on valid input and never gates.
Verified against the live adsk.cam API (op.parameters.itemByName(name).expression is settable). No
live Fusion here — fakes mimic CAMParameters.
"""

import json
import types

from conftest import (FakeCAMFolder, FakeCAMParameter, FakeCAMParameters, FakeTool,
                      _NamedCollection, load_tool, make_cam)
from conftest import FakeSetup as SharedSetup, FakeOperation as SharedOp

ce = load_tool("cam_edit_operation")


class FakeParam(FakeCAMParameter):
    """A numeric operation parameter: its evaluated .value follows the expression it stores (the
    platform's own read, a finite 0.0 for an expression that did not evaluate - only .error exposes
    that), and the expression 'BOOM' is the one the platform refuses outright."""

    @FakeCAMParameter.expression.setter
    def expression(self, value):
        if value == "BOOM":
            raise RuntimeError("invalid expression")
        FakeCAMParameter.expression.fset(self, value)

    @property
    def value(self):
        try:
            return types.SimpleNamespace(value=float(self._expression.split()[0]))
        except Exception:
            return types.SimpleNamespace(value=0.0 if self.error else None)

    @value.setter
    def value(self, v):
        pass


def FakeParams(d):
    """An operation's parameters from {name: expression} - a value that is already a parameter
    (a warning-bearing or scenario one) is taken as it is."""
    return FakeCAMParameters([v if isinstance(v, FakeCAMParameter) else FakeParam(k, v)
                              for k, v in d.items()])


class FakePreset:
    """A ToolPreset: the name/id pair Operation.toolPreset reads back."""
    def __init__(self, name, pid=None):
        self.name = name
        self.id = pid if pid is not None else "id-" + name


def FakeLibTool(description, number, presets=()):
    """A library Tool: the description and the tool_number parameter its identity is read from."""
    return FakeTool(description=description, parameters=FakeParams({"tool_number": str(number)}),
                    presets=[FakePreset(p) for p in presets])


class FakeOp(SharedOp):
    # The members this fake (and its scenario subclasses) model as PROPERTIES; while the shared
    # fake's own __init__ runs, a write to one lands on the private state instead, so a scenario
    # setter only ever sees a write the tool under test made.
    _AT_INIT = {"tool": "_tool", "isSuppressed": "_suppressed", "hasToolpath": "_has_toolpath"}
    _built = False

    def __init__(self, name, params, suppressed=False, has_toolpath=True,
                 presets=None, preset=None, is_generating=False, toolpath_valid=True):
        # An operation with no tool has no presets to point at - the default here, so the existing
        # tests keep meeting the tool-less operation they always did.
        super().__init__(name, parameters=FakeParams(params), strategy="adaptive",
                         suppressed=bool(suppressed), has_toolpath=bool(has_toolpath),
                         valid=bool(toolpath_valid),
                         tool=FakeTool(presets=presets) if presets is not None else None)
        self._preset = preset
        self.isGenerating = bool(is_generating)
        self._built = True

    def __setattr__(self, key, value):
        if not self._built and key in self._AT_INIT:
            object.__setattr__(self, self._AT_INIT[key], value)
            return
        object.__setattr__(self, key, value)

    @property
    def tool(self):
        return self._tool

    @tool.setter
    def tool(self, value):
        self._tool = value

    @property
    def toolPreset(self):
        return self._preset

    @toolPreset.setter
    def toolPreset(self, value):
        self._preset = value

    @property
    def isSuppressed(self):
        return self._suppressed

    @isSuppressed.setter
    def isSuppressed(self, value):
        self._suppressed = bool(value)
        # Suppressing DISCARDS the toolpath: hasToolpath reads True before the set and False after,
        # and clearing the flag again leaves hasToolpath False - both measured (ledger row
        # cam-suppress-discards-toolpath) - so this fake leaves it where the suppression put it.
        if self._suppressed:
            self._has_toolpath = False

    @property
    def hasToolpath(self):
        return self._has_toolpath

    @hasToolpath.setter
    def hasToolpath(self, value):
        self._has_toolpath = bool(value)


class DroppedFlagOp(FakeOp):
    """A setter the platform accepts and silently drops - the swallowed no-op the gate exists for."""

    @FakeOp.isSuppressed.setter
    def isSuppressed(self, value):
        pass


class RaisingSetterOp(FakeOp):
    @FakeOp.isSuppressed.setter
    def isSuppressed(self, value):
        raise RuntimeError("operation is locked")


class UnreadableAfterOp(FakeOp):
    """isSuppressed reads until it is written, then stops answering - the UNCONFIRMED case."""

    @property
    def isSuppressed(self):
        if self._suppressed:
            raise RuntimeError("no longer readable")
        return False

    @isSuppressed.setter
    def isSuppressed(self, value):
        self._suppressed = bool(value)


class UnreadableToolpathOp(FakeOp):
    """isSuppressed answers; hasToolpath does not - the flag pair the note is worded from is half
    unreadable, and neither half may be reported as a value the property held."""

    @property
    def hasToolpath(self):
        raise RuntimeError("hasToolpath unreadable")


class UnreadableBeforeOp(FakeOp):
    """The PRIOR flag cannot be read; every read after the set answers normally."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._reads = 0

    @property
    def isSuppressed(self):
        self._reads += 1
        if self._reads == 1:
            raise RuntimeError("not readable yet")
        return self._suppressed

    @isSuppressed.setter
    def isSuppressed(self, value):
        self._suppressed = bool(value)


class DroppedPresetOp(FakeOp):
    """A toolPreset setter the platform accepts and silently drops - the operation keeps the preset
    it already ran, which is the swallowed no-op the read-back gate exists for."""

    @FakeOp.toolPreset.setter
    def toolPreset(self, value):
        pass


class PresetReplacesParameterCollectionOp(FakeOp):
    """A preset assignment replaces the native parameter collection and its values."""
    @FakeOp.toolPreset.setter
    def toolPreset(self, value):
        self._preset = value
        self.parameters = FakeParams({"tool_stepover": "9."})


class PresetLosesParametersOp(FakeOp):
    """A preset assignment leaves the operation parameter collection unreadable."""
    def __init__(self, *args, **kwargs):
        self._parameters = None
        self._parameters_unavailable = False
        super().__init__(*args, **kwargs)

    @property
    def parameters(self):
        if self._parameters_unavailable:
            raise RuntimeError("parameters unavailable after preset")
        return self._parameters

    @parameters.setter
    def parameters(self, value):
        self._parameters = value

    @FakeOp.toolPreset.setter
    def toolPreset(self, value):
        self._preset = value
        self._parameters_unavailable = True


class NullPresetOp(FakeOp):
    """The assignment is accepted and the property then reads NOTHING - the other swallow shape."""

    @FakeOp.toolPreset.setter
    def toolPreset(self, value):
        self._preset = None


class RaisingPresetSetterOp(FakeOp):
    @FakeOp.toolPreset.setter
    def toolPreset(self, value):
        raise RuntimeError("operation is locked")


def FakeSetup(ops):
    """The setup the operations sit in - one only, since every test here addresses by name."""
    return SharedSetup("Setup1", ops=ops)


class _AllOperationsOnlySetup(SharedSetup):
    """A setup whose .operations reads None, so only allOperations can answer it."""

    def __init__(self, ops):
        super().__init__("Setup1", ops=ops)
        self._ops = list(ops)

    @property
    def operations(self):
        return None

    @operations.setter
    def operations(self, value):
        pass

    @property
    def allOperations(self):
        return _NamedCollection(self._ops)


def FakeCAM(ops, doc_tools=()):
    """A CAM product holding one setup of `ops`, plus the document tool library the document-scope
    tool reference reads by index."""
    cam = make_cam(FakeSetup(ops))
    cam.documentToolLibrary = _NamedCollection(list(doc_tools))
    return cam


def _install(monkeypatch, op_name="Adaptive1", params=None):
    params = params if params is not None else {
        "tool_feedCutting": "5210.23", "tool_spindleSpeed": "14006.",
        "maximumStepdown": "2.0483", "tool_stepover": "2.",
    }
    op = FakeOp(op_name, params)
    cam = FakeCAM([op])
    monkeypatch.setattr(ce, "get_cam", lambda: (cam, None))
    return op


def _install_op(monkeypatch, op, doc_tools=()):
    """Install ONE prepared operation (a flag-behaviour variant) behind get_cam."""
    monkeypatch.setattr(ce, "get_cam", lambda: (FakeCAM([op], doc_tools=doc_tools), None))
    return op


def _payload(res):
    assert res["isError"] is False, res
    return json.loads(res["content"][0]["text"])


class TestEditOperation:
    def test_sets_param_dict(self, monkeypatch):
        op = _install(monkeypatch)
        out = _payload(ce.handler(operation="Adaptive1",
                                  parameters={"tool_feedCutting": "3000", "maximumStepdown": "1.5"}))
        assert op.parameters.itemByName("tool_feedCutting").expression == "3000"
        assert op.parameters.itemByName("maximumStepdown").expression == "1.5"
        assert out["updated_count"] == 2
        # before/after captured
        changed = {c["name"]: c for c in out["changed"]}
        assert changed["tool_feedCutting"]["before"] == "5210.23"
        assert changed["tool_feedCutting"]["after"] == "3000"

    def test_accepts_name_equals_value_strings(self, monkeypatch):
        op = _install(monkeypatch)
        out = _payload(ce.handler(operation="Adaptive1",
                                  parameters="tool_spindleSpeed=12000, tool_stepover=1.5"))
        assert op.parameters.itemByName("tool_spindleSpeed").expression == "12000"
        assert op.parameters.itemByName("tool_stepover").expression == "1.5"
        assert out["updated_count"] == 2

    def test_unknown_param_reported(self, monkeypatch):
        _install(monkeypatch)
        res = ce.handler(operation="Adaptive1", parameters={"nope_param": "5"})
        assert res["isError"] is True and "nope_param" in res["message"]

    def test_unknown_operation(self, monkeypatch):
        _install(monkeypatch)
        res = ce.handler(operation="Ghost", parameters={"tool_stepover": "1"})
        assert res["isError"] is True and "Ghost" in res["message"]

    def test_invalid_value_reports_and_does_not_partially_apply(self, monkeypatch):
        op = _install(monkeypatch)
        # first value raises on set; the tool reports the failure and the op is left as found -
        # read BOTH params back: a handler that swallowed the raise and kept applying would
        # leave tool_stepover changed.
        res = ce.handler(operation="Adaptive1",
                         parameters={"maximumStepdown": "BOOM", "tool_stepover": "1.0"})
        assert res["isError"] is True and "maximumStepdown" in res["message"]
        assert op.parameters.itemByName("maximumStepdown").expression == "2.0483"
        assert op.parameters.itemByName("tool_stepover").expression == "2."

    def test_no_parameters_errors(self, monkeypatch):
        _install(monkeypatch)
        res = ce.handler(operation="Adaptive1", parameters={})
        assert res["isError"] is True and "parameters" in res["message"]

    def test_no_operation_name_errors(self, monkeypatch):
        _install(monkeypatch)
        res = ce.handler(operation="   ", parameters={"tool_stepover": "1"})
        assert res["isError"] is True and "operation" in res["message"]

    def test_broken_expression_rolls_back_all_params_in_call(self, monkeypatch):
        # The platform STORES a non-evaluating expression silently (expression echoes it, value.value
        # reads a finite 0.0) - only .error reveals it. The tool must error naming the offending value
        # and Fusion's reason, and roll back EVERY param set in the call, not just the broken one.
        op = _install(monkeypatch)
        res = ce.handler(operation="Adaptive1",
                         parameters={"tool_stepover": "1.5",
                                     "maximumStepdown": "NoSuchParamXyz * 2"})
        assert res["isError"] is True
        assert "maximumStepdown" in res["message"]
        assert "NoSuchParamXyz * 2" in res["message"]
        assert "Failed to evaluate" in res["message"]
        assert "Rolled back" in res["message"]
        # ALL-or-nothing: the valid first param is rolled back too, the op left exactly as found.
        assert op.parameters.itemByName("tool_stepover").expression == "2."
        assert op.parameters.itemByName("maximumStepdown").expression == "2.0483"

    def test_warning_on_valid_expression_never_gates(self, monkeypatch):
        # .warning fires on VALID input too (live fact) - it must be reported, never turned into an
        # error/rollback.
        _install(monkeypatch, params={"tool_feedCutting": FakeParam("tool_feedCutting", "1000.",
                                                                    warning="feed near limit")})
        out = _payload(ce.handler(operation="Adaptive1",
                                  parameters={"tool_feedCutting": "3000"}))
        assert out["updated_count"] == 1
        assert out["changed"][0]["warning"] == "feed near limit"
        assert out["changed"][0]["after"] == "3000"

    def test_changed_records_evaluated_value(self, monkeypatch):
        # changed[].value is the EVALUATED number (FakeParam.value parses the expr),
        # distinct from the .after expression string.
        _install(monkeypatch)
        out = _payload(ce.handler(operation="Adaptive1",
                                  parameters={"tool_feedCutting": "3000"}))
        c = out["changed"][0]
        assert c["after"] == "3000"          # the expression text
        assert c["value"] == 3000.0          # the evaluated value


class StuckParam(FakeParam):
    """Accepts the expression assignment and keeps the one it holds - the platform swallow."""
    @FakeParam.expression.setter
    def expression(self, v):
        pass


class QuotingParam(FakeParam):
    """A STRING parameter: the value it is written is STORED single-quoted, so a request and its own
    read-back differ by the wrapper alone (receipt cam-parameter-expressions)."""
    @FakeParam.expression.setter
    def expression(self, v):
        s = str(v)
        self._expression = s if len(s) >= 2 and s[0] == s[-1] == "'" else f"'{s}'"


class UnreadableAfterParam(FakeParam):
    """The expression reads until it is written, then stops answering - a write nothing can confirm
    (the shape cam_edit_setup's arm calls UNCONFIRMED)."""

    _written = False

    @property
    def expression(self):
        if self._written:
            raise RuntimeError("expression no longer readable")
        return self._expression

    @expression.setter
    def expression(self, v):
        self._expression = v
        self._written = True


class NumericNormalizingParam(FakeParam):
    """The store keeps a number in its OWN spelling: '25' is written and '25.' comes back."""
    @FakeParam.expression.setter
    def expression(self, v):
        s = str(v)
        self._expression = s if "." in s else s + "."


class UnitAppendingParam(FakeParam):
    """The store re-spells what it is given, so the read-back MOVES even when the request matched
    the expression the parameter already held."""
    @FakeParam.expression.setter
    def expression(self, v):
        s = str(v)
        self._expression = s if s.endswith(" mm") else s + " mm"


class NeverReadableParam(FakeParam):
    """The expression does not read on EITHER side of the set - the pair of non-answers that must
    not compare equal into a 'nothing moved' verdict."""
    @property
    def expression(self):
        raise RuntimeError("expression unreadable")

    @expression.setter
    def expression(self, v):
        self._expression = v


class UnreadableEditableParam(FakeParam):
    """isEditable does not answer at all - the read that may not mint a refusal."""
    @property
    def isEditable(self):
        raise RuntimeError("isEditable unreadable")

    @isEditable.setter
    def isEditable(self, value):
        pass


class TestStuckParameter:
    """A parameter that takes the assignment and keeps the expression it held is a write the platform
    dropped: the operation is left as found and the call must say so, never report edited:true over a
    read-back that never moved."""

    def test_a_stuck_parameter_is_an_error_not_a_reported_success(self, monkeypatch):
        op = _install(monkeypatch,
                      params={"tool_feedCutting": StuckParam("tool_feedCutting", "1000."),
                              "tool_stepover": "2."})
        res = ce.handler(operation="Adaptive1",
                         parameters={"tool_stepover": "1.5", "tool_feedCutting": "3000"})
        assert res["isError"] is True
        assert "did not take" in res["message"]
        # the message states what was READ, not a cause it never looked at
        assert "'tool_feedCutting' = '3000' (it still reads '1000.')" in res["message"]
        assert "Rolled back all 2 parameter(s)" in res["message"]
        # all-before-any: the parameter that DID land is put back, so the op is exactly as found
        assert op.parameters.itemByName("tool_stepover").expression == "2."
        assert op.parameters.itemByName("tool_feedCutting").expression == "1000."

    def test_the_no_take_refusal_words_the_tool_remedy_by_creation_order(self, monkeypatch):
        # MEASURED: a document tool edited 25 -> 40 mm flute lands at 40 in an op created AFTER the
        # edit, while an op created before keeps its stale copy. So a cam_edit_tools edit on its own
        # is not a remedy for THIS operation - the re-assign is, and the sentence has to say which.
        _install(monkeypatch,
                 params={"tool_fluteLength": StuckParam("tool_fluteLength", "25.")})
        res = ce.handler(operation="Adaptive1", parameters={"tool_fluteLength": "32"})
        assert res["isError"] is True and "did not take" in res["message"]
        assert "reaches only operations created AFTER it" in res["message"]
        assert "cam_edit_operation(tool_scope, tool_index)" in res["message"]
        assert "must reference existing parameters" not in res["message"]

    def test_an_expression_that_will_not_read_back_is_UNCONFIRMED_not_edited(self, monkeypatch):
        # A write whose read-back does not answer confirms nothing, so edited:true over it would
        # publish a success this call never observed. It is checked BEFORE the movement gate - two
        # unreadable reads compare equal, and "nothing moved" is a verdict neither read supports.
        op = _install(monkeypatch,
                      params={"tool_stepover": "2.",
                              "tolerance": UnreadableAfterParam("tolerance", "0.01")})
        res = ce.handler(operation="Adaptive1",
                         parameters={"tool_stepover": "1.5", "tolerance": "0.025"})
        assert res["isError"] is True
        assert "UNCONFIRMED" in res["message"]
        assert "the expression cannot be read back" in res["message"]
        assert "'tolerance' = '0.025'" in res["message"]
        # not reported as a dropped write, and not carrying the tool-library remedy
        assert "did not take" not in res["message"]
        assert "cam_edit_tools" not in res["message"]
        assert "cam_get(include=['operations'])" in res["message"]
        # rolled back exactly like the other two failure paths
        assert "Rolled back all 2 parameter(s)" in res["message"]
        assert op.parameters.itemByName("tool_stepover").expression == "2."

    def test_two_unreadable_reads_are_UNCONFIRMED_never_a_nothing_moved_verdict(self, monkeypatch):
        # before and after both fail to read, so they compare EQUAL - and "the assignment did not
        # take" off two reads that never happened is a verdict invented from nothing. The
        # unreadable check runs first precisely so this case cannot reach the movement gate.
        _install(monkeypatch, params={"tolerance": NeverReadableParam("tolerance", "0.01")})
        res = ce.handler(operation="Adaptive1", parameters={"tolerance": "0.025"})
        assert res["isError"] is True
        assert "UNCONFIRMED" in res["message"]
        assert "did not take" not in res["message"]

    def test_a_request_the_parameter_already_holds_is_an_ok_no_op(self, monkeypatch):
        # Nothing moved, but nothing was asked to move: an idempotent set is a success, disclosed
        # in the record rather than reported as a landed change.
        _install(monkeypatch, params={"tool_stepover": "2."})
        out = _payload(ce.handler(operation="Adaptive1", parameters={"tool_stepover": "2."}))
        c = out["changed"][0]
        assert c["unchanged"] is True and c["before"] == "2." and c["after"] == "2."
        assert "changed[].unchanged marks a parameter the request already matched" in out["note"]

    def test_the_same_number_spelled_differently_is_an_ok_no_op(self, monkeypatch):
        # The store may keep a number in its own spelling, so a REPEAT of a request already in
        # force reads back as '25.' against a request of '25'. Refusing that as a dropped write
        # would fail a call on a value the operation already holds. The two are compared as
        # NUMBERS - which is a read of both sides, not a guess at what the platform does to text.
        _install(monkeypatch,
                 params={"tool_fluteLength": NumericNormalizingParam("tool_fluteLength", "25.")})
        out = _payload(ce.handler(operation="Adaptive1", parameters={"tool_fluteLength": "25"}))
        assert out["changed"][0]["unchanged"] is True
        assert out["changed"][0]["after"] == "25."

    def test_a_different_number_still_reaches_the_movement_gate(self, monkeypatch):
        # The other side of the number route: it may only excuse an unmoved read-back when the two
        # ARE the same number. A different one that did not move is still a dropped write, and the
        # message quotes the expression the parameter still reads.
        _install(monkeypatch,
                 params={"tool_fluteLength": StuckParam("tool_fluteLength", "32.")})
        res = ce.handler(operation="Adaptive1", parameters={"tool_fluteLength": "25"})
        assert res["isError"] is True and "did not take" in res["message"]
        assert "'tool_fluteLength' = '25' (it still reads '32.')" in res["message"]

    def test_a_matching_request_whose_readback_MOVED_is_an_ordinary_edit(self, monkeypatch):
        # 'unchanged' claims the expression reads the same, so it is set only where the read-back
        # actually held still. A request equal to the held expression whose store re-spelled it
        # DID move - that is a landed edit, and marking it unchanged would state a read-back this
        # call never saw.
        _install(monkeypatch,
                 params={"tool_stepover": UnitAppendingParam("tool_stepover", "2.")})
        out = _payload(ce.handler(operation="Adaptive1", parameters={"tool_stepover": "2."}))
        c = out["changed"][0]
        assert "unchanged" not in c
        assert c["before"] == "2." and c["after"] == "2. mm"
        assert "changed[].unchanged" not in out["note"]

    def test_a_string_parameters_own_quoting_is_not_a_dropped_write(self, monkeypatch):
        # The store wraps a string value in single quotes, so 'flip' read back as "'flip'" is the
        # SAME value - compared through the shared codec, or an idempotent set reads as a stuck one.
        _install(monkeypatch, params={"swarfSelectionMode": QuotingParam("swarfSelectionMode",
                                                                        "'flip'")})
        out = _payload(ce.handler(operation="Adaptive1", parameters={"swarfSelectionMode": "flip"}))
        assert out["changed"][0]["unchanged"] is True
        assert out["changed"][0]["after"] == "'flip'"

    def test_a_string_parameter_that_really_changes_is_a_landed_edit(self, monkeypatch):
        # The other side of the same codec: a DIFFERENT string moves the read-back and carries no
        # 'unchanged' marker, so the idempotent branch cannot be swallowing real edits.
        _install(monkeypatch, params={"swarfSelectionMode": QuotingParam("swarfSelectionMode",
                                                                        "'flip'")})
        out = _payload(ce.handler(operation="Adaptive1", parameters={"swarfSelectionMode": "keep"}))
        c = out["changed"][0]
        assert "unchanged" not in c and c["after"] == "'keep'"
        assert "changed[].unchanged" not in out["note"]


class TestMissingParameter:
    """A name the operation does not carry is refused before any write. The refusal is the one place
    a caller learns which names exist, so it names the read that lists them - and that read lists
    the SHOWN rows only, a row behind a switch landing in the call that sets the switch."""

    def test_the_refusal_names_the_read_that_lists_the_parameter_names(self, monkeypatch):
        op = _install(monkeypatch,
                      params={"tool_diameter": FakeParam("tool_diameter", "10.")})
        res = ce.handler(operation="Adaptive1", parameters={"tool_dia": "12"})
        assert res["isError"] is True
        assert "has no parameter(s): tool_dia" in res["message"]
        assert "cam_get(include=['parameters'], operation=...)" in res["message"]
        assert op.parameters.itemByName("tool_diameter").expression == "10."

    def test_the_refusal_does_not_call_that_read_the_whole_settable_set(self, monkeypatch):
        _install(monkeypatch, params={"tool_diameter": FakeParam("tool_diameter", "10.")})
        message = ce.handler(operation="Adaptive1", parameters={"tool_dia": "12"})["message"]
        assert "only a name it lists can be set" not in message
        assert "hidden_count" in message and "SAME call" in message


class TestNotEditable:
    """308 of a swarf operation's 375 parameters read isEditable False (measured), and assigning to
    one changes nothing while invalidating the toolpath - so the refusal comes BEFORE any assignment,
    names every offending parameter, and is never minted from a flag that did not answer."""

    def test_a_non_editable_parameter_is_refused_and_nothing_is_assigned(self, monkeypatch):
        op = _install(monkeypatch,
                      params={"tool_diameter": FakeParam("tool_diameter", "10.", editable=False)})
        res = ce.handler(operation="Adaptive1", parameters={"tool_diameter": "12"})
        assert res["isError"] is True
        assert "does not accept a write to: tool_diameter" in res["message"]
        assert "isEditable reads False" in res["message"]
        assert "Nothing was applied" in res["message"]
        assert op.parameters.itemByName("tool_diameter").expression == "10."

    def test_the_locked_tool_dimension_remedy_is_worded_by_creation_order(self, monkeypatch):
        # MEASURED: a document tool edited 25 -> 40 mm flute lands at 40 in an op created AFTER the
        # edit, while an op created before keeps its stale copy. So a cam_edit_tools edit is NOT a
        # remedy for THIS operation on its own - the re-assign is.
        _install(monkeypatch,
                 params={"tool_fluteLength": FakeParam("tool_fluteLength", "25.", editable=False)})
        msg = ce.handler(operation="Adaptive1", parameters={"tool_fluteLength": "40"})["message"]
        assert "reaches only operations created AFTER it" in msg
        assert "cam_edit_operation(tool_scope, tool_index)" in msg

    def test_every_non_editable_parameter_is_named_in_one_error(self, monkeypatch):
        _install(monkeypatch,
                 params={"tool_diameter": FakeParam("tool_diameter", "10.", editable=False),
                         "tool_stepover": FakeParam("tool_stepover", "2.", editable=False)})
        res = ce.handler(operation="Adaptive1",
                         parameters={"tool_diameter": "12", "tool_stepover": "1.5"})
        assert res["isError"] is True
        assert "tool_diameter, tool_stepover" in res["message"]

    def test_one_locked_parameter_is_written_then_restored_and_the_refusal_says_so(self,
                                                                                   monkeypatch):
        # The editable row may be the SWITCH that unlocks the locked one, so it is written and the
        # flag re-read. When it was not, every written expression goes back and is re-read - and the
        # refusal states the write rather than claiming the operation was never touched, since
        # putting an expression back is not putting the operation's toolpath state back.
        op = _install(monkeypatch,
                      params={"tool_feedCutting": "5210.23",
                              "tool_diameter": FakeParam("tool_diameter", "10.", editable=False)})
        res = ce.handler(operation="Adaptive1",
                         parameters={"tool_feedCutting": "3000", "tool_diameter": "12"})
        assert res["isError"] is True
        assert "tool_diameter" in res["message"] and "tool_feedCutting" not in res["message"]
        assert "WROTE 1 parameter(s) on the operation and then restored each one" in res["message"]
        assert "every restored expression reads back what it held" in res["message"]
        assert "No other state was read" in res["message"]        # no claim it is as it was found
        assert op.parameters.itemByName("tool_feedCutting").expression == "5210.23"
        assert op.parameters.itemByName("tool_diameter").expression == "10."

    def test_an_unreadable_isEditable_is_not_a_refusal(self, monkeypatch):
        # A flag that did not answer is not a False: refusing on it would invent a verdict from a
        # read that never happened, and lock the caller out of a parameter that does take the write.
        op = _install(monkeypatch,
                      params={"tolerance": UnreadableEditableParam("tolerance", "0.01")})
        out = _payload(ce.handler(operation="Adaptive1", parameters={"tolerance": "0.025"}))
        assert out["updated_count"] == 1 and out["changed"][0]["after"] == "0.025"
        assert op.parameters.itemByName("tolerance").expression == "0.025"


def _preset_op(cls=FakeOp, presets=("Default preset", "Probe Alu"), preset="Default preset",
               name="Adaptive1"):
    """An operation carrying a tool with `presets` and currently running `preset` (by name)."""
    objs = [FakePreset(p) for p in presets]
    current = next((p for p in objs if p.name == preset), None)
    return cls(name, {"tool_stepover": "2."}, presets=objs, preset=current)


class TestPreset:
    """The WRITE side of Operation.toolPreset: resolve a preset ON THIS OPERATION'S OWN TOOL, assign
    it, and read the property back. Every claim in the payload is a read - the name the property
    answers after the set, and the name it answered before."""

    def test_points_the_operation_at_a_preset_and_reads_it_back(self, monkeypatch):
        op = _install_op(monkeypatch, _preset_op())
        out = _payload(ce.handler(operation="Adaptive1", preset="Probe Alu"))
        assert op.toolPreset.name == "Probe Alu"
        assert out["preset"] == "Probe Alu" and out["preset_index"] == 1
        assert out["preset_id"] == "id-Probe Alu"
        assert out["was_preset"] == "Default preset"
        assert "toolPreset now reads 'Probe Alu' on 'Adaptive1'" in out["note"]
        # the note points at the read that carries the cutting data
        assert "cam_get(include=['tool'], operation='Adaptive1', preset='Probe Alu')" in out["note"]
        # ...and carries the measured consequence: assigning a preset INVALIDATES a valid op's
        # toolpath (operationState 0 -> 1, isToolpathValid True -> False), so the remedy is the
        # one a parameter edit gets, worded the same way.
        assert "The toolpath is now OUT OF DATE - regenerate it with cam_generate." in out["note"]
        assert out["updated_count"] == 0 and out["changed"] == []

    def test_the_match_is_case_insensitive_and_publishes_the_stored_spelling(self, monkeypatch):
        op = _install_op(monkeypatch, _preset_op())
        out = _payload(ce.handler(operation="Adaptive1", preset="  probe ALU "))
        assert op.toolPreset.name == "Probe Alu"
        # the payload names the preset as the TOOL spells it, not as the caller typed it
        assert out["preset"] == "Probe Alu"

    def test_a_missing_preset_lists_the_tools_own_presets(self, monkeypatch):
        op = _install_op(monkeypatch, _preset_op())
        res = ce.handler(operation="Adaptive1", preset="Steel Rough")
        assert res["isError"] is True
        assert "no preset named 'Steel Rough'" in res["message"]
        assert "Default preset" in res["message"] and "Probe Alu" in res["message"]
        assert op.toolPreset.name == "Default preset"        # nothing was assigned

    def test_a_long_preset_list_is_capped_with_the_remainder_counted(self, monkeypatch):
        # A cloned mill ships a material-by-operation matrix of presets, so the miss list is capped
        # and the cut is DISCLOSED - a silently truncated list reads as the complete set.
        names = [f"Preset{i}" for i in range(25)]
        _install_op(monkeypatch, _preset_op(presets=names, preset="Preset0"))
        res = ce.handler(operation="Adaptive1", preset="Nope")
        assert res["isError"] is True
        assert "Preset0" in res["message"] and "Preset7" in res["message"]
        assert "Preset8" not in res["message"]
        assert "(+17 more not listed)" in res["message"]
        assert "cam_get(include=['tool'], operation='Adaptive1')" in res["message"]

    def test_a_tool_with_no_presets_points_at_the_authoring_tool(self, monkeypatch):
        _install_op(monkeypatch, _preset_op(presets=(), preset=None))
        res = ce.handler(operation="Adaptive1", preset="Probe Alu")
        assert res["isError"] is True
        assert "carries no presets at all" in res["message"]
        assert "cam_edit_tools(action='add_preset')" in res["message"]

    def test_an_operation_with_no_tool_is_refused(self, monkeypatch):
        _install_op(monkeypatch, FakeOp("Adaptive1", {"tool_stepover": "2."}))
        res = ce.handler(operation="Adaptive1", preset="Probe Alu")
        assert res["isError"] is True
        assert "carries no tool" in res["message"]

    def test_a_shared_preset_name_is_refused_not_picked(self, monkeypatch):
        op = _install_op(monkeypatch,
                         _preset_op(presets=("Alu", "Alu", "Steel"), preset="Steel"))
        res = ce.handler(operation="Adaptive1", preset="Alu")
        assert res["isError"] is True
        assert "names 2 presets" in res["message"] and "0, 1" in res["message"]
        assert "refused rather than picking one" in res["message"]
        assert op.toolPreset.name == "Steel"

    def test_a_dropped_preset_write_is_an_error_not_a_false_ok(self, monkeypatch):
        op = _install_op(monkeypatch, _preset_op(cls=DroppedPresetOp))
        res = ce.handler(operation="Adaptive1", preset="Probe Alu")
        assert res["isError"] is True
        assert "did not take" in res["message"]
        assert "reads back 'Default preset'" in res["message"]
        assert op.toolPreset.name == "Default preset"

    def test_a_preset_that_reads_back_null_is_an_error(self, monkeypatch):
        _install_op(monkeypatch, _preset_op(cls=NullPresetOp))
        res = ce.handler(operation="Adaptive1", preset="Probe Alu")
        assert res["isError"] is True
        assert "reads back null" in res["message"] and "did not take" in res["message"]

    def test_a_raising_preset_setter_is_reported(self, monkeypatch):
        _install_op(monkeypatch, _preset_op(cls=RaisingPresetSetterOp))
        res = ce.handler(operation="Adaptive1", preset="Probe Alu")
        assert res["isError"] is True
        assert "Could not set toolPreset" in res["message"]
        assert "operation is locked" in res["message"]

    def test_was_preset_is_null_when_the_operation_ran_none(self, monkeypatch):
        # false/'' would assert the operation had been running a preset; it was running nothing.
        _install_op(monkeypatch, _preset_op(preset=None))
        out = _payload(ce.handler(operation="Adaptive1", preset="Probe Alu"))
        assert out["was_preset"] is None and out["preset"] == "Probe Alu"

    def test_preset_alone_needs_no_parameters(self, monkeypatch):
        _install_op(monkeypatch, _preset_op())
        out = _payload(ce.handler(operation="Adaptive1", preset="Probe Alu"))
        assert out["edited"] is True and out["operation"] == "Adaptive1"

    def test_the_empty_call_names_all_three_inputs(self, monkeypatch):
        _install_op(monkeypatch, _preset_op())
        res = ce.handler(operation="Adaptive1")
        assert res["isError"] is True
        for word in ("parameters", "preset", "suppressed"):
            assert word in res["message"]

    def test_parameters_preset_and_suppression_in_one_call(self, monkeypatch):
        op = _install_op(monkeypatch, _preset_op())
        out = _payload(ce.handler(operation="Adaptive1", parameters={"tool_stepover": "1.5"},
                                  preset="Probe Alu", suppressed=True))
        assert op.parameters.itemByName("tool_stepover").expression == "1.5"
        assert op.toolPreset.name == "Probe Alu" and op.isSuppressed is True
        assert out["updated_count"] == 1 and out["preset"] == "Probe Alu"
        assert "Parameters set." in out["note"] and "toolPreset now reads" in out["note"]
        assert "isSuppressed now reads True" in out["note"]

    def test_a_rolled_back_parameter_never_reaches_the_preset(self, monkeypatch):
        # The preset must not be assigned to an operation the call left exactly as it found it.
        op = _install_op(monkeypatch, _preset_op())
        res = ce.handler(operation="Adaptive1",
                         parameters={"tool_stepover": "NoSuchParamXyz * 2"}, preset="Probe Alu")
        assert res["isError"] is True and "Rolled back" in res["message"]
        assert op.toolPreset.name == "Probe Alu"
        assert "toolPreset already set to 'Probe Alu'" in res["message"]

    def test_preset_failure_precedes_parameter_writes(self, monkeypatch):
        # The preset arm runs first, so a failed preset leaves parameters untouched.
        op = _install_op(monkeypatch, _preset_op(cls=DroppedPresetOp))
        res = ce.handler(operation="Adaptive1", parameters={"tool_stepover": "1.5"},
                         preset="Probe Alu")
        assert res["isError"] is True and "did not take" in res["message"]
        assert "Parameters already applied" not in res["message"]
        assert op.parameters.itemByName("tool_stepover").expression == "2."

    def test_unreadable_parameters_after_preset_are_an_error(self, monkeypatch):
        op = _install_op(monkeypatch, _preset_op(cls=PresetLosesParametersOp))
        res = ce.handler(operation="Adaptive1", parameters={"tool_stepover": "1.5"},
                         preset="Probe Alu")
        assert res["isError"] is True
        assert "cannot be read after tool/preset assignment" in res["message"]
        assert "no explicit parameter write was attempted" in res["message"]
        assert "toolPreset already set to 'Probe Alu'" in res["message"]

    def test_parameters_are_applied_after_preset_overwrite(self, monkeypatch):
        # The final parameter write must win when a preset setter overwrites that row.
        op = _install_op(monkeypatch, _preset_op(cls=PresetReplacesParameterCollectionOp))
        out = _payload(ce.handler(operation="Adaptive1", parameters={"tool_stepover": "1.5"},
                                  preset="Probe Alu"))
        assert op.parameters.itemByName("tool_stepover").expression == "1.5"
        assert out["changed"][0]["after"] == "1.5"
        assert out["changed"][0]["before"] == "9."

    def test_a_failed_suppression_names_the_preset_that_did_land(self, monkeypatch):
        # The other half of the same disclosure: an arm that failed LAST must name every earlier
        # arm that landed, or the caller cannot tell what state the operation is in.
        op = _install_op(monkeypatch, _preset_op(cls=DroppedFlagOp))
        res = ce.handler(operation="Adaptive1", parameters={"tool_stepover": "1.5"},
                         preset="Probe Alu", suppressed=True)
        assert res["isError"] is True and "did not take" in res["message"]
        assert "Parameters already applied: tool_stepover" in res["message"]
        assert "toolPreset already set to 'Probe Alu'" in res["message"]
        assert op.toolPreset.name == "Probe Alu"


class TestSuppression:
    """The WRITE side of Operation.isSuppressed: set, read back, and report what the set cost.

    Every claim in the payload is a read: the flag after the set, the flag before it, and
    hasToolpath on BOTH sides - suppressing DISCARDS the toolpath rather than hiding it, so the
    before/after pair is what makes the cost visible at the moment of use.
    """

    def test_suppressing_reports_the_flag_and_the_discarded_toolpath(self, monkeypatch):
        op = _install_op(monkeypatch, FakeOp("Drill1", {"tool_stepover": "2."}, has_toolpath=True))
        out = _payload(ce.handler(operation="Drill1", suppressed=True))
        assert op.isSuppressed is True
        assert out["is_suppressed"] is True and out["was_suppressed"] is False
        assert out["had_toolpath"] is True and out["has_toolpath"] is False
        assert "DISCARDED the toolpath" in out["note"]
        # the measured consequence, not a claim about what the post does: the op carries no
        # toolpath until it is regenerated (clearing isSuppressed does not bring it back).
        assert "the operation carries none until it is regenerated" in out["note"]
        assert out["updated_count"] == 0 and out["changed"] == []

    def test_suppressing_an_op_with_no_toolpath_claims_no_discard(self, monkeypatch):
        # The DISCARD sentence carries the measured consequence, so it may only appear where THIS
        # call read the transition: hasToolpath True before and False after. An op that held no
        # toolpath to begin with lost nothing, and the note must say what it read instead.
        op = _install_op(monkeypatch,
                         FakeOp("Drill1", {"tool_stepover": "2."}, has_toolpath=False))
        out = _payload(ce.handler(operation="Drill1", suppressed=True))
        assert op.isSuppressed is True
        assert out["had_toolpath"] is False and out["has_toolpath"] is False
        assert "DISCARDED" not in out["note"]
        assert "hasToolpath read False before the set and False after" in out["note"]

    def test_an_unreadable_toolpath_read_reaches_the_note_as_unreadable(self, monkeypatch):
        # null on both sides: neither the discard claim nor a fabricated False may be published.
        _install_op(monkeypatch, UnreadableToolpathOp("Drill1", {"tool_stepover": "2."}))
        out = _payload(ce.handler(operation="Drill1", suppressed=True))
        assert out["had_toolpath"] is None and out["has_toolpath"] is None
        assert "DISCARDED" not in out["note"]
        assert "hasToolpath read unreadable before the set and unreadable after" in out["note"]

    def test_unsuppressing_reports_the_toolpath_this_call_read(self, monkeypatch):
        # The note never claims the toolpath came back: it reports the hasToolpath THIS call read.
        op = _install_op(monkeypatch,
                         FakeOp("Drill1", {"tool_stepover": "2."}, suppressed=True,
                                has_toolpath=False))
        out = _payload(ce.handler(operation="Drill1", suppressed=False))
        assert op.isSuppressed is False
        assert out["is_suppressed"] is False and out["was_suppressed"] is True
        assert out["has_toolpath"] is False
        assert "the operation carries no toolpath" in out["note"]
        assert "DISCARDED" not in out["note"]

    def test_unsuppressing_an_op_that_still_reads_a_toolpath_says_so(self, monkeypatch):
        # The other side of the unsuppress branch: the note reports the read, and the regenerate
        # remedy belongs only to the op whose hasToolpath read False.
        _install_op(monkeypatch,
                    FakeOp("Drill1", {"tool_stepover": "2."}, suppressed=True, has_toolpath=True))
        out = _payload(ce.handler(operation="Drill1", suppressed=False))
        assert out["is_suppressed"] is False and out["has_toolpath"] is True
        assert "hasToolpath reads True." in out["note"]
        assert "carries no toolpath" not in out["note"]

    def test_a_dropped_flag_write_is_an_error_not_a_false_ok(self, monkeypatch):
        _install_op(monkeypatch, DroppedFlagOp("Drill1", {"tool_stepover": "2."}))
        res = ce.handler(operation="Drill1", suppressed=True)
        assert res["isError"] is True
        assert "did not take" in res["message"] and "Drill1" in res["message"]

    def test_an_unreadable_flag_after_the_set_is_unconfirmed(self, monkeypatch):
        _install_op(monkeypatch, UnreadableAfterOp("Drill1", {"tool_stepover": "2."}))
        res = ce.handler(operation="Drill1", suppressed=True)
        assert res["isError"] is True
        assert "UNCONFIRMED" in res["message"]

    def test_a_raising_setter_is_reported(self, monkeypatch):
        _install_op(monkeypatch, RaisingSetterOp("Drill1", {"tool_stepover": "2."}))
        res = ce.handler(operation="Drill1", suppressed=True)
        assert res["isError"] is True
        assert "Could not set isSuppressed" in res["message"]
        assert "operation is locked" in res["message"]

    def test_an_unreadable_PRIOR_flag_publishes_null_not_false(self, monkeypatch):
        # was_suppressed=false would assert the operation had been active; the read did not answer.
        _install_op(monkeypatch, UnreadableBeforeOp("Drill1", {"tool_stepover": "2."}))
        out = _payload(ce.handler(operation="Drill1", suppressed=True))
        assert out["is_suppressed"] is True
        assert out["was_suppressed"] is None

    def test_suppressed_alone_needs_no_parameters(self, monkeypatch):
        _install_op(monkeypatch, FakeOp("Drill1", {"tool_stepover": "2."}))
        out = _payload(ce.handler(operation="Drill1", suppressed=True))
        assert out["edited"] is True and out["operation"] == "Drill1"

    def test_neither_parameters_nor_suppressed_is_refused(self, monkeypatch):
        _install(monkeypatch)
        res = ce.handler(operation="Adaptive1")
        assert res["isError"] is True
        assert "parameters" in res["message"] and "suppressed" in res["message"]

    def test_parameters_and_suppression_in_one_call(self, monkeypatch):
        op = _install_op(monkeypatch, FakeOp("Drill1", {"tool_stepover": "2."}))
        out = _payload(ce.handler(operation="Drill1", parameters={"tool_stepover": "1.5"},
                                  suppressed=True))
        assert op.parameters.itemByName("tool_stepover").expression == "1.5"
        assert out["updated_count"] == 1 and out["is_suppressed"] is True
        assert "Parameters set." in out["note"] and "isSuppressed now reads True" in out["note"]

    def test_a_rolled_back_parameter_never_reaches_the_suppression(self, monkeypatch):
        # The flag must not be flipped on an operation the call left exactly as it found it.
        op = _install_op(monkeypatch, FakeOp("Drill1", {"tool_stepover": "2."}))
        res = ce.handler(operation="Drill1",
                         parameters={"tool_stepover": "NoSuchParamXyz * 2"}, suppressed=True)
        assert res["isError"] is True and "Rolled back" in res["message"]
        assert op.isSuppressed is False
        assert op.hasToolpath is True

    def test_a_failed_suppression_names_the_parameters_already_applied(self, monkeypatch):
        # Partial success is stated, never swallowed: the params landed, the flag did not.
        op = _install_op(monkeypatch, DroppedFlagOp("Drill1", {"tool_stepover": "2."}))
        res = ce.handler(operation="Drill1", parameters={"tool_stepover": "1.5"}, suppressed=True)
        assert res["isError"] is True
        assert "did not take" in res["message"]
        assert "Parameters already applied: tool_stepover" in res["message"]
        assert op.parameters.itemByName("tool_stepover").expression == "1.5"


class TestParseParameters:
    def test_string_without_equals_errors(self, monkeypatch):
        _install(monkeypatch)
        res = ce.handler(operation="Adaptive1", parameters="tool_stepover 1.5")
        assert res["isError"] is True
        assert "name=value" in res["message"]

    def test_string_skips_blank_chunks(self, monkeypatch):
        # trailing/double commas produce empty chunks that must be ignored, not errored.
        op = _install(monkeypatch)
        out = _payload(ce.handler(operation="Adaptive1",
                                  parameters="tool_stepover=1.5, , tool_feedCutting=900,"))
        assert out["updated_count"] == 2
        assert op.parameters.itemByName("tool_stepover").expression == "1.5"

    def test_non_dict_non_string_errors(self, monkeypatch):
        _install(monkeypatch)
        res = ce.handler(operation="Adaptive1", parameters=42)
        assert res["isError"] is True
        assert "object" in res["message"] or "name=value" in res["message"]


class TestFindOperation:
    def test_falls_back_to_allOperations_when_operations_missing(self, monkeypatch):
        # A setup that exposes only allOperations (operations is None) must still resolve.
        op = FakeOp("OnlyAll", {"tool_stepover": "2."})
        cam = make_cam(_AllOperationsOnlySetup([op]))    # forces the `or allOperations` fallback
        monkeypatch.setattr(ce, "get_cam", lambda: (cam, None))
        out = _payload(ce.handler(operation="OnlyAll", parameters={"tool_stepover": "1"}))
        assert out["operation"] == "OnlyAll"
        assert op.parameters.itemByName("tool_stepover").expression == "1"

    def test_unknown_operation_lists_available_names(self, monkeypatch):
        _install(monkeypatch, op_name="RealOp")
        res = ce.handler(operation="Ghost", parameters={"tool_stepover": "1"})
        assert res["isError"] is True
        assert "RealOp" in res["message"]      # available names surfaced

    def test_duplicate_op_name_across_setups_is_refused(self, monkeypatch):
        # "Drill1" exists in TWO setups - editing by that name must REFUSE with both setup paths,
        # never silently edit whichever setup's op the walk met first.
        cam = make_cam(SharedSetup("Setup1", ops=[SharedOp("Drill1")]),
                       SharedSetup("Setup2", ops=[SharedOp("Drill1")]))
        monkeypatch.setattr(ce, "get_cam", lambda: (cam, None))
        res = ce.handler(operation="Drill1", parameters={"tool_stepover": "1"})
        assert res["isError"] is True and "ambiguous" in res["message"].lower()
        assert "Setup1 / Drill1" in res["message"] and "Setup2 / Drill1" in res["message"]

    def test_operation_nested_in_a_folder_resolves(self, monkeypatch):
        # a folder-nested operation must resolve too - .operations only lists what's directly in
        # the setup, so the lookup must recurse into .folders (and .patterns) to reach it.
        nested_op = FakeOp("Drill1", {"tool_stepover": "2."})
        folder = FakeCAMFolder("Holes", ops=[nested_op])
        cam = make_cam(SharedSetup("Setup1", folders=[folder]))
        monkeypatch.setattr(ce, "get_cam", lambda: (cam, None))
        out = _payload(ce.handler(operation="Drill1", parameters={"tool_stepover": "1"}))
        assert out["operation"] == "Drill1"
        assert nested_op.parameters.itemByName("tool_stepover").expression == "1"


class DroppedToolOp(FakeOp):
    """A tool setter the platform accepts and silently drops - the swallowed no-op the read-back
    gate exists for."""

    @FakeOp.tool.setter
    def tool(self, value):
        pass


def NumberlessLibTool(description):
    """A library tool carrying no tool_number parameter at all - _read_tool_number answers None."""
    return FakeTool(description=description)


def _PrefixedTool(tool, number):
    """The operation's own COPY of a library tool: its description carries the '#<number> - ' prefix
    measured live, which the library tool's own description does not."""
    copy = FakeTool(description=f"#{number} - {tool.description}", parameters=tool.parameters)
    copy.presets = tool.presets
    return copy


class PrefixingToolOp(FakeOp):
    """Assigning a library tool stores that prefixed copy, so Operation.tool never reads back the
    library description verbatim."""

    @FakeOp.tool.setter
    def tool(self, value):
        self._tool = _PrefixedTool(value, 1)


def TextNumberLibTool(description, number_text):
    """A library tool whose tool_number hands its evaluated value back as TEXT rather than a number -
    the read has to answer an int or None, never the string."""
    number = FakeCAMParameter("tool_number", number_text, value=number_text)
    return FakeTool(description=description, parameters=FakeCAMParameters([number]))


class _DescriptionlessTool(FakeTool):
    """A library tool whose description does not read - the identity comparison cannot be made, and
    no value may be printed for it."""

    @property
    def description(self):
        raise RuntimeError("description unreadable")

    @description.setter
    def description(self, value):
        pass


def DescriptionlessLibTool(number):
    return _DescriptionlessTool(parameters=FakeParams({"tool_number": str(number)}))


class ThirdToolOp(FakeOp):
    """The assignment lands, but on a tool that is neither the one requested nor the one already
    carried - a write that TOOK, which must not be reported as one that did not."""

    @FakeOp.tool.setter
    def tool(self, value):
        self._tool = FakeLibTool("#9 - 20mm Face Mill", 9)


class InvalidatingToolOp(FakeOp):
    """A store that clears isToolpathValid when the tool changes. The note is worded from the two
    reads THIS call made, never from an assumption about what the platform does to a toolpath."""

    @FakeOp.tool.setter
    def tool(self, value):
        self._tool = value
        self.isToolpathValid = False


class TestToolChange:
    """The WRITE side of Operation.tool: resolve a library tool by the addressing
    cam_create_operation takes, assign it, and compare what Operation.tool READS back - two fetches
    of one tool are different objects, so the description and tool number are the comparison."""

    def test_assigns_a_document_library_tool_and_reads_its_identity_back(self, monkeypatch):
        op = FakeOp("Adaptive1", {"tool_stepover": "2."})       # a template op arriving with no tool
        _install_op(monkeypatch, op, doc_tools=[FakeLibTool("12mm Flat Endmill", 7)])
        out = _payload(ce.handler(operation="Adaptive1", tool_scope="document", tool_index=0))
        assert op.tool.description == "12mm Flat Endmill"
        assert out["tool"] == "12mm Flat Endmill" and out["tool_number"] == 7
        assert out["tool_index"] == 0
        # null, not '': the operation ran no tool at all, which is the case this arm exists for
        assert out["was_tool"] is None
        assert "Operation.tool now reads '12mm Flat Endmill' (tool number 7)" in out["note"]

    def test_the_operations_own_number_prefixed_copy_is_the_tool_that_landed(self, monkeypatch):
        # Measured live: the operation's copy of a library tool reads back with a '#<number> - '
        # prefix the library description does not carry. A description EQUALITY against the library
        # tool therefore always fails - it would call every successful assignment a dropped write.
        op = PrefixingToolOp("Adaptive1", {"tool_stepover": "2."})
        _install_op(monkeypatch, op, doc_tools=[FakeLibTool("12mm Flat Endmill", 7)])
        out = _payload(ce.handler(operation="Adaptive1", tool_scope="document", tool_index=0))
        assert op.tool.description == "#1 - 12mm Flat Endmill"
        assert out["tool"] == "#1 - 12mm Flat Endmill"      # the read-back, as it reads
        assert out["library_tool_number"] == 7               # the library tool the caller addressed
        assert "tool_identity_checked" not in out            # the comparison was made and passed

    def test_a_tool_number_that_does_not_read_is_left_out_of_the_note(self, monkeypatch):
        # _read_tool_number answers None for an absent parameter; '(tool number None)' would read
        # as a number the tool held.
        op = FakeOp("Adaptive1", {"tool_stepover": "2."})
        _install_op(monkeypatch, op, doc_tools=[NumberlessLibTool("12mm Flat Endmill")])
        out = _payload(ce.handler(operation="Adaptive1", tool_scope="document", tool_index=0))
        assert out["tool_number"] is None and out["library_tool_number"] is None
        assert "None" not in out["note"] and "tool number" not in out["note"]
        assert "Operation.tool now reads '12mm Flat Endmill' on 'Adaptive1'" in out["note"]

    def test_a_dropped_tool_write_is_an_error_not_a_false_ok(self, monkeypatch):
        op = DroppedToolOp("Adaptive1", {"tool_stepover": "2."})
        op._tool = FakeLibTool("6mm Ball Endmill", 3)
        _install_op(monkeypatch, op, doc_tools=[FakeLibTool("12mm Flat Endmill", 7)])
        res = ce.handler(operation="Adaptive1", tool_scope="document", tool_index=0)
        assert res["isError"] is True and "did not take" in res["message"]
        assert "still reads the tool it already carried, '6mm Ball Endmill'" in res["message"]
        # the verdict is the DESCRIPTION not naming the library tool; a number is never the reason
        assert "number" not in res["message"]
        assert op.tool.description == "6mm Ball Endmill"

    def test_a_kept_tool_whose_description_ENDS_WITH_the_requested_one_is_still_dropped(self, monkeypatch):
        # The operation's copy differs from the library description by the '#<n> - ' prefix and
        # nothing else, so the comparison is EQUALITY after that strip. A suffix test would confirm
        # a dropped write here: '#1 - 16mm Flat Endmill' ends with '6mm Flat Endmill'.
        op = DroppedToolOp("Adaptive1", {"tool_stepover": "2."})
        op._tool = FakeLibTool("#1 - 16mm Flat Endmill", 1)
        _install_op(monkeypatch, op, doc_tools=[FakeLibTool("6mm Flat Endmill", 2)])
        res = ce.handler(operation="Adaptive1", tool_scope="document", tool_index=0)
        assert res["isError"] is True and "did not take" in res["message"]
        assert "still reads the tool it already carried, '#1 - 16mm Flat Endmill'" in res["message"]
        assert op.tool.description == "#1 - 16mm Flat Endmill"

    def test_a_library_description_that_ALREADY_carries_the_prefix_is_not_a_false_refusal(self, monkeypatch):
        # Measured: a DOCUMENT-library tool's own description already carries '#<number> - ' (the
        # add assigns the number), while a shared sample library's does not. Stripping the prefix
        # from the read-back ONLY made two identical strings compare unequal, and a landed
        # assignment was refused as "does not name it".
        # the operation's copy reads the SAME string the document library already carries
        op = FakeOp("Face1", {"tool_stepover": "2."})
        _install_op(monkeypatch, op,
                    doc_tools=[FakeLibTool("#1 - 12mm flat (12mm Flat Endmill)", 1)])
        out = _payload(ce.handler(operation="Face1", tool_scope="document", tool_index=0))
        assert out["tool"] == "#1 - 12mm flat (12mm Flat Endmill)"
        assert out["library_tool_number"] == 1
        assert "tool_identity_checked" not in out          # the comparison was made and passed

    def test_a_write_that_LANDED_on_another_tool_is_not_reported_as_not_taken(self, monkeypatch):
        # The refusal fired after a real write, so 'did not take' may only be claimed where the
        # read-back is the tool the operation ALREADY carried. A read-back that MOVED says the
        # assignment landed - on something else - and the caller has to be told that, not the
        # opposite.
        op = ThirdToolOp("Adaptive1", {"tool_stepover": "2."})
        op._tool = FakeLibTool("6mm Ball Endmill", 3)
        _install_op(monkeypatch, op, doc_tools=[FakeLibTool("12mm Flat Endmill", 7)])
        res = ce.handler(operation="Adaptive1", tool_scope="document", tool_index=0)
        assert res["isError"] is True
        assert "carries a tool this call did not ask for" in res["message"]
        assert "did not take" not in res["message"]
        assert "'#9 - 20mm Face Mill'" in res["message"]   # what it actually reads now

    def test_a_read_back_identical_to_the_tool_already_carried_is_DISCLOSED(self, monkeypatch):
        # The strip-both comparison passes on two tools sharing a base description, so an operation
        # on '#2 - 12mm Flat Endmill' pointed at library '#1 - 12mm Flat Endmill' reads back
        # unmoved. Nothing this call read separates a re-assignment from a dropped write, so it is
        # disclosed rather than claimed as a change - and never refused, since the write may have
        # landed. The two numbers are what a caller checks it against.
        op = DroppedToolOp("Adaptive1", {"tool_stepover": "2."})
        op._tool = FakeLibTool("#2 - 12mm Flat Endmill", 2)
        _install_op(monkeypatch, op, doc_tools=[FakeLibTool("#1 - 12mm Flat Endmill", 1)])
        out = _payload(ce.handler(operation="Adaptive1", tool_scope="document", tool_index=0))
        assert out["tool_unchanged"] is True
        assert out["tool_number"] == 2 and out["library_tool_number"] == 1
        assert "cannot tell a re-assignment from a dropped write" in out["note"]
        assert "Operation.tool reads tool number 2 and the library tool is number 1." in out["note"]
        assert "now reads" not in out["note"]              # no change is claimed

    def test_a_landed_tool_change_still_says_now_reads(self, monkeypatch):
        # the other side: a read-back that MOVED is a change this call did observe
        op = FakeOp("Adaptive1", {"tool_stepover": "2."})
        op._tool = FakeLibTool("6mm Ball Endmill", 3)
        _install_op(monkeypatch, op, doc_tools=[FakeLibTool("12mm Flat Endmill", 7)])
        out = _payload(ce.handler(operation="Adaptive1", tool_scope="document", tool_index=0))
        assert "tool_unchanged" not in out
        assert "Operation.tool now reads '12mm Flat Endmill'" in out["note"]
        assert "cannot tell" not in out["note"]

    def test_a_tool_less_op_and_an_unreadable_description_is_not_read_as_unchanged(self, monkeypatch):
        # The operation carried NO tool and the library description does not read, so both sides of
        # the sameness test are null - and null == null would publish 'the SAME tool it already
        # carried' about an operation that carried none. The prior tool having read is the guard.
        op = FakeOp("Adaptive1", {"tool_stepover": "2."})
        _install_op(monkeypatch, op, doc_tools=[DescriptionlessLibTool(7)])
        out = _payload(ce.handler(operation="Adaptive1", tool_scope="document", tool_index=0))
        assert out["was_tool"] is None and out["tool"] is None
        assert "tool_unchanged" not in out
        assert "SAME tool" not in out["note"] and "cannot tell" not in out["note"]

    def test_an_unreadable_description_leaves_no_None_in_the_applied_clause(self, monkeypatch):
        # The identity comparison cannot be made when a description does not read, so the call
        # proceeds and DISCLOSES that - but a later arm's failure may not print 'None' as the tool.
        _install_op(monkeypatch, DroppedFlagOp("Adaptive1", {"tool_stepover": "2."}),
                    doc_tools=[DescriptionlessLibTool(7)])
        res = ce.handler(operation="Adaptive1", tool_scope="document", tool_index=0,
                         suppressed=True)
        assert res["isError"] is True                       # the suppression is the arm that failed
        assert "the cutting tool was already assigned" in res["message"]
        assert "None" not in res["message"]

    def test_a_tool_that_reads_back_null_is_an_error(self, monkeypatch):
        op = DroppedToolOp("Adaptive1", {"tool_stepover": "2."})
        _install_op(monkeypatch, op, doc_tools=[FakeLibTool("12mm Flat Endmill", 7)])
        res = ce.handler(operation="Adaptive1", tool_scope="document", tool_index=0)
        assert res["isError"] is True
        assert "reads back null" in res["message"] and "did not take" in res["message"]

    def test_a_generating_operation_is_refused_before_the_assignment(self, monkeypatch):
        op = FakeOp("Adaptive1", {"tool_stepover": "2."}, is_generating=True)
        _install_op(monkeypatch, op, doc_tools=[FakeLibTool("12mm Flat Endmill", 7)])
        res = ce.handler(operation="Adaptive1", tool_scope="document", tool_index=0)
        assert res["isError"] is True and "GENERATING" in res["message"]
        assert "cam_get_status" in res["message"]
        assert op.tool is None                       # nothing was assigned

    def test_the_note_states_the_toolpath_flag_on_both_sides(self, monkeypatch):
        op = InvalidatingToolOp("Adaptive1", {"tool_stepover": "2."}, toolpath_valid=True)
        _install_op(monkeypatch, op, doc_tools=[FakeLibTool("12mm Flat Endmill", 7)])
        out = _payload(ce.handler(operation="Adaptive1", tool_scope="document", tool_index=0))
        assert out["was_toolpath_valid"] is True and out["is_toolpath_valid"] is False
        assert "isToolpathValid read True before the assignment and False after" in out["note"]
        assert "regenerate the toolpath with cam_generate" in out["note"]

    def test_a_toolpath_flag_that_held_is_reported_not_claimed_invalid(self, monkeypatch):
        # The note may only state what this call READ. A flag still reading True is reported as
        # read - claiming the toolpath went out of date would be a consequence nothing observed.
        op = FakeOp("Adaptive1", {"tool_stepover": "2."}, toolpath_valid=True)
        _install_op(monkeypatch, op, doc_tools=[FakeLibTool("12mm Flat Endmill", 7)])
        out = _payload(ce.handler(operation="Adaptive1", tool_scope="document", tool_index=0))
        assert "isToolpathValid reads True." in out["note"]
        assert "regenerate" not in out["note"].lower()

    def test_a_tool_index_delivered_as_TEXT_resolves_instead_of_raising(self, monkeypatch):
        # Live: tool_index arriving as text raised "'<' not supported between instances of 'str'
        # and 'int'" out of the handler - an unhandled exception, not a worded refusal. The index
        # is read to an int before any ORDER comparison touches it.
        op = FakeOp("Adaptive1", {"tool_stepover": "2."})
        _install_op(monkeypatch, op, doc_tools=[FakeLibTool("12mm Flat Endmill", 7)])
        out = _payload(ce.handler(operation="Adaptive1", tool_scope="document", tool_index="0"))
        assert out["tool"] == "12mm Flat Endmill" and out["tool_index"] == 0

    def test_a_tool_index_that_is_not_a_number_is_refused_NAMING_the_value(self, monkeypatch):
        # 'Provide tool_index ...' reads as a bug report to a caller who provided one, so a value
        # that is present but unusable is named instead of asked for again.
        _install_op(monkeypatch, FakeOp("Adaptive1", {"tool_stepover": "2."}),
                    doc_tools=[FakeLibTool("12mm Flat Endmill", 7)])
        res = ce.handler(operation="Adaptive1", tool_scope="document", tool_index="first")
        assert res["isError"] is True
        assert "'first'" in res["message"] and "not a whole number" in res["message"]
        assert "Provide 'tool_index'" not in res["message"]

    def test_a_boolean_tool_index_is_refused_not_read_as_an_index(self, monkeypatch):
        # bool is an int in Python, so True would sail through a plain isinstance check and select
        # the SECOND tool in the library - a silent wrong tool, which is the worst outcome here.
        op = FakeOp("Adaptive1", {"tool_stepover": "2."})
        _install_op(monkeypatch, op, doc_tools=[FakeLibTool("12mm Flat Endmill", 7),
                                                FakeLibTool("6mm Ball Endmill", 3)])
        res = ce.handler(operation="Adaptive1", tool_scope="document", tool_index=True)
        assert res["isError"] is True and "not a whole number" in res["message"]
        assert op.tool is None                              # no tool was assigned

    def test_an_absent_tool_index_still_asks_for_one(self, monkeypatch):
        # the other side of the split: nothing was provided, so the refusal asks rather than names
        _install_op(monkeypatch, FakeOp("Adaptive1", {"tool_stepover": "2."}),
                    doc_tools=[FakeLibTool("12mm Flat Endmill", 7)])
        res = ce.handler(operation="Adaptive1", tool_scope="document")
        assert res["isError"] is True and "Provide 'tool_index'" in res["message"]
        assert "not a whole number" not in res["message"]

    def test_a_tool_number_stored_as_TEXT_still_reads_as_a_number(self, monkeypatch):
        # A library tool's tool_number parameter can hand back its value as text; the read is
        # int-or-None, so the payload never publishes a string where a number is promised.
        op = FakeOp("Adaptive1", {"tool_stepover": "2."})
        _install_op(monkeypatch, op, doc_tools=[TextNumberLibTool("12mm Flat Endmill", "7")])
        out = _payload(ce.handler(operation="Adaptive1", tool_scope="document", tool_index=0))
        assert out["tool_number"] == 7 and out["library_tool_number"] == 7
        assert "(tool number 7)" in out["note"]

    def test_a_tool_index_with_no_library_names_both_addressings(self, monkeypatch):
        _install_op(monkeypatch, FakeOp("Adaptive1", {"tool_stepover": "2."}))
        res = ce.handler(operation="Adaptive1", tool_index=0)
        assert res["isError"] is True
        assert "tool_scope=document" in res["message"] and "tool_library_url" in res["message"]

    def test_a_document_scope_with_no_index_asks_for_one(self, monkeypatch):
        _install_op(monkeypatch, FakeOp("Adaptive1", {"tool_stepover": "2."}),
                    doc_tools=[FakeLibTool("12mm Flat Endmill", 7)])
        res = ce.handler(operation="Adaptive1", tool_scope="document")
        assert res["isError"] is True and "tool_index" in res["message"]

    def test_the_new_tools_preset_is_reachable_in_the_same_call(self, monkeypatch):
        # The tool arm runs BEFORE the preset arm: a preset resolves on the operation's OWN tool,
        # so this call fails 'carries no tool' the moment the two swap order.
        op = FakeOp("Adaptive1", {"tool_stepover": "2."})
        _install_op(monkeypatch, op,
                    doc_tools=[FakeLibTool("12mm Flat Endmill", 7, presets=("Alu Rough",))])
        out = _payload(ce.handler(operation="Adaptive1", tool_scope="document", tool_index=0,
                                  preset="Alu Rough"))
        assert out["tool"] == "12mm Flat Endmill" and out["preset"] == "Alu Rough"

    def test_a_failed_preset_names_the_tool_that_did_land(self, monkeypatch):
        op = DroppedPresetOp("Adaptive1", {"tool_stepover": "2."})
        _install_op(monkeypatch, op,
                    doc_tools=[FakeLibTool("12mm Flat Endmill", 7, presets=("Alu Rough",))])
        res = ce.handler(operation="Adaptive1", tool_scope="document", tool_index=0,
                         preset="Alu Rough")
        assert res["isError"] is True
        assert "tool already set to '12mm Flat Endmill'" in res["message"]


class StubbornNameOp(FakeOp):
    """Operation.name takes the assignment and keeps the name it holds - a rename the platform
    declined, which an EDIT must report rather than publish the requested name over."""

    @property
    def name(self):
        return self._name

    @name.setter
    def name(self, value):
        if not hasattr(self, "_name"):
            self._name = value


class DedupingNameOp(FakeOp):
    """Operation.name DEDUPES rather than refusing: 'Face1' with one already taken lands as
    'Face11', with no separator (measured live)."""

    @property
    def name(self):
        return self._name

    @name.setter
    def name(self, value):
        self._name = str(value) if not hasattr(self, "_name") else str(value) + "1"


class UnreadableNameOp(FakeOp):
    """Operation.name takes the assignment and then stops answering - a rename nothing can confirm,
    which is neither a decline nor a dedupe."""

    @property
    def name(self):
        if self._writes > 1:
            raise RuntimeError("name is no longer readable")
        return self._name

    @name.setter
    def name(self, value):
        object.__setattr__(self, "_name", value)
        object.__setattr__(self, "_writes", getattr(self, "_writes", 0) + 1)


class DedupingDroppedFlagOp(DedupingNameOp):
    """The deduping rename plus a suppression setter the platform drops, so the LAST arm fails and
    has to name the address the rename actually landed under."""

    @FakeOp.isSuppressed.setter
    def isSuppressed(self, value):
        pass


class TestRename:
    """Renaming is what every other CAM tool addresses this operation by, so a name another
    operation already carries is refused BEFORE any write, and the new name is read back."""

    def test_renames_the_operation_and_publishes_both_names(self, monkeypatch):
        op = _install_op(monkeypatch, FakeOp("Adaptive1", {"tool_stepover": "2."}))
        out = _payload(ce.handler(operation="Adaptive1", rename="Rough Pocket"))
        assert op.name == "Rough Pocket"
        assert out["operation"] == "Rough Pocket" and out["was_operation"] == "Adaptive1"
        assert out["renamed"] is True
        assert "Operation.name now reads 'Rough Pocket' (was 'Adaptive1')." in out["note"]

    def test_a_name_another_operation_carries_is_refused_before_any_write(self, monkeypatch):
        other = FakeOp("Drill1", {"tool_stepover": "2."})
        target = FakeOp("Face1", {"tool_stepover": "2."})
        monkeypatch.setattr(ce, "get_cam", lambda: (FakeCAM([other, target]), None))
        res = ce.handler(operation="Face1", rename="Drill1",
                         parameters={"tool_stepover": "1.5"})
        assert res["isError"] is True
        assert "already answer to 'Drill1'" in res["message"]
        assert "dedupes rather than refusing" in res["message"]
        assert target.name == "Face1"
        # the clash is a PRE-flight: the parameter in the same call was never applied
        assert target.parameters.itemByName("tool_stepover").expression == "2."

    def test_renaming_onto_the_name_it_already_reads_writes_nothing(self, monkeypatch):
        # A rename onto the current name is a NO-OP, not a dedupe: writing it again makes the
        # platform dedupe the operation against itself ('Face1' -> 'Face11', measured).
        op = _install_op(monkeypatch, DedupingNameOp("Face1", {"tool_stepover": "2."}))
        out = _payload(ce.handler(operation="Face1", rename="Face1"))
        assert op.name == "Face1"                       # untouched, not 'Face11'
        assert out["operation"] == "Face1" and out["renamed"] is False
        assert out["name_unchanged"] is True
        assert "Operation.name already reads 'Face1' - nothing was written." in out["note"]

    def test_a_case_only_rename_is_not_read_as_a_clash(self, monkeypatch):
        # The operation itself carries the name case-insensitively, so the clash check has to
        # exclude it - or no operation could ever be re-spelled.
        op = _install_op(monkeypatch, FakeOp("face1", {"tool_stepover": "2."}))
        out = _payload(ce.handler(operation="face1", rename="Face1"))
        assert op.name == "Face1"
        assert out["operation"] == "Face1" and out["was_operation"] == "face1"

    def test_a_declined_rename_is_an_error_not_a_reported_success(self, monkeypatch):
        # DECLINED means the name did not move at all - the one case that is a failure.
        op = _install_op(monkeypatch, StubbornNameOp("Drill1", {"tool_stepover": "2."}))
        res = ce.handler(operation="Drill1", rename="Rough Pocket")
        assert res["isError"] is True and "did not take" in res["message"]
        assert "Operation.name still reads 'Drill1'" in res["message"]
        assert op.name == "Drill1"

    def test_a_deduped_name_is_published_as_the_new_address_not_a_failure(self, monkeypatch):
        # Operation.name DEDUPES ('Face1' taken -> 'Face11', measured), so a landed name matching
        # neither the request nor the name it held is the operation's new address. Reporting it as
        # a failed rename would leave the caller addressing a name nothing carries.
        op = _install_op(monkeypatch, DedupingNameOp("Drill1", {"tool_stepover": "2."}))
        out = _payload(ce.handler(operation="Drill1", rename="Face1"))
        assert op.name == "Face11"
        assert out["operation"] == "Face11" and out["was_operation"] == "Drill1"
        assert out["renamed"] is True and out["name_deduped"] is True
        assert "Operation.name now reads 'Face11' (was 'Drill1')." in out["note"]
        assert "Address the operation as 'Face11' from here." in out["note"]

    def test_a_name_that_does_not_read_back_is_UNCONFIRMED_not_a_deduped_rename(self, monkeypatch):
        # An unread name is neither the declined case nor the deduped one. Taking the dedupe branch
        # would publish 'None' as the operation's new address over an isError:false payload.
        _install_op(monkeypatch, UnreadableNameOp("Drill1", {"tool_stepover": "2."}))
        res = ce.handler(operation="Drill1", rename="Rough Pocket")
        assert res["isError"] is True and "UNCONFIRMED" in res["message"]
        assert "does not read back" in res["message"]
        assert "cam_get(include=['operations'])" in res["message"]
        assert "None" not in res["message"]
        assert "did not take" not in res["message"]        # not worded as the declined case

    def test_a_later_arms_failure_names_the_name_it_LANDED_under(self, monkeypatch):
        # The applied clause has to carry the deduped name: that is the address the caller needs to
        # reach the operation after this call, and the requested one reaches nothing.
        _install_op(monkeypatch, DedupingDroppedFlagOp("Drill1", {"tool_stepover": "2."}))
        res = ce.handler(operation="Drill1", rename="Face1", suppressed=True)
        assert res["isError"] is True
        assert "already renamed to 'Face11'" in res["message"]

    def test_a_failed_rename_names_the_parameters_already_applied(self, monkeypatch):
        op = _install_op(monkeypatch, StubbornNameOp("Drill1", {"tool_stepover": "2."}))
        res = ce.handler(operation="Drill1", rename="Rough Pocket",
                         parameters={"tool_stepover": "1.5"})
        assert res["isError"] is True
        assert "Parameters already applied: tool_stepover" in res["message"]

    def test_the_note_says_a_rename_generates_the_operation(self, monkeypatch):
        # MEASURED: a 'face' op renamed with no cam_generate call went no_toolpath -> valid. A note
        # reporting only the new name leaves the caller unaware a generation was provoked.
        _install_op(monkeypatch, FakeOp("Adaptive1", {"tool_stepover": "2."}))
        out = _payload(ce.handler(operation="Adaptive1", rename="Rough Pocket"))
        assert "generates the operation where its strategy can" in out["note"]
        assert "cam_get_status" in out["note"]

    def test_a_rename_that_wrote_nothing_claims_no_generation(self, monkeypatch):
        # A rename onto the name already read writes nothing, so nothing generated - saying it did
        # would be a claim about a write this call never made.
        _install_op(monkeypatch, DedupingNameOp("Face1", {"tool_stepover": "2."}))
        out = _payload(ce.handler(operation="Face1", rename="Face1"))
        assert "generates the operation" not in out["note"]

    def test_rename_alone_needs_no_other_input(self, monkeypatch):
        _install_op(monkeypatch, FakeOp("Drill1", {"tool_stepover": "2."}))
        out = _payload(ce.handler(operation="Drill1", rename="Bore1"))
        assert out["edited"] is True and out["updated_count"] == 0


class HoleFacesParam(FakeParam):
    """holeFaces / circularFaces - a CadObjectParameterValue whose .value.value is the face LIST,
    not a number, which is what the hole-selection read counts."""

    def __init__(self, name, faces=()):
        super().__init__(name, "")
        self._faces = list(faces)

    @property
    def value(self):
        return types.SimpleNamespace(value=list(self._faces))

    @value.setter
    def value(self, v):
        pass


class TestHoleRenameGuard:
    """Setting Operation.name on a hole/thread operation makes the platform generate it, so the
    rename is refused while that operation's own face selection reads empty."""

    def _op(self, strategy, params):
        op = FakeOp("Bore1", params)
        op.strategy = strategy
        return op

    def test_a_bore_with_no_faces_selected_is_refused_before_the_write(self, monkeypatch):
        op = _install_op(monkeypatch, self._op(
            "bore", {"circularFaces": HoleFacesParam("circularFaces")}))
        res = ce.handler(operation="Bore1", rename="Bore Deep Holes")
        assert res["isError"] is True
        assert "'circularFaces' selection reads 0 faces" in res["message"]
        assert "Failed to generate toolpath." in res["message"]
        assert "cam_select_geometry(operation='Bore1', selection='holes'" in res["message"]
        assert op.name == "Bore1"                       # refused BEFORE the write

    def test_a_thread_is_gated_on_the_hole_spelling_it_carries(self, monkeypatch):
        # MEASURED: the milling thread op picks into circularFaces - it reports "Circular Face
        # Selections: No faces selected" - so the gate reaches it through the 'holes' spelling.
        op = _install_op(monkeypatch, self._op(
            "thread", {"circularFaces": HoleFacesParam("circularFaces")}))
        res = ce.handler(operation="Bore1", rename="Thread M30")
        assert res["isError"] is True
        assert "'circularFaces' selection reads 0 faces" in res["message"]
        assert "selection='holes'" in res["message"]
        assert op.name == "Bore1"

    def test_a_thread_holding_faces_renames(self, monkeypatch):
        op = _install_op(monkeypatch, self._op(
            "thread", {"circularFaces": HoleFacesParam("circularFaces", [object()])}))
        out = _payload(ce.handler(operation="Bore1", rename="Thread M30"))
        assert op.name == "Thread M30" and out["renamed"] is True

    def test_renaming_a_face_less_bore_onto_its_own_name_writes_nothing(self, monkeypatch):
        # the no-op rename never reaches Operation.name (see _rename), so there is no generation
        # to gate - refusing it would block a call that changes nothing.
        op = _install_op(monkeypatch, self._op(
            "bore", {"circularFaces": HoleFacesParam("circularFaces")}))
        out = _payload(ce.handler(operation="Bore1", rename="Bore1"))
        assert out["name_unchanged"] is True and out["renamed"] is False
        assert op.name == "Bore1"

    def test_a_bore_whose_selection_holds_faces_renames(self, monkeypatch):
        # THE BOUNDARY: one face is enough - the guard is about an EMPTY selection, not about bores.
        op = _install_op(monkeypatch, self._op(
            "bore", {"circularFaces": HoleFacesParam("circularFaces", [object()])}))
        out = _payload(ce.handler(operation="Bore1", rename="Bore Deep Holes"))
        assert op.name == "Bore Deep Holes" and out["renamed"] is True

    def test_a_non_hole_strategy_carrying_no_hole_parameter_renames(self, monkeypatch):
        op = _install_op(monkeypatch, self._op("adaptive", {"tool_stepover": "2."}))
        out = _payload(ce.handler(operation="Bore1", rename="Rough Pocket"))
        assert op.name == "Rough Pocket" and out["renamed"] is True

    def test_a_drill_with_an_empty_selection_is_outside_the_guard(self, monkeypatch):
        # The parked dialog was measured on a bore/circular/thread; drill carries the same empty
        # holeFaces shape and is deliberately not refused, so the guard cannot creep to every
        # hole strategy on its own.
        op = _install_op(monkeypatch, self._op(
            "drill", {"holeFaces": HoleFacesParam("holeFaces")}))
        out = _payload(ce.handler(operation="Bore1", rename="Drill Holes"))
        assert op.name == "Drill Holes" and out["renamed"] is True


class EnumRefusingParam(FakeParam):
    """The platform's refusal for a value outside a string parameter's enumeration - Fusion words
    it '3 : Invalid enumeration value.'"""
    @FakeParam.expression.setter
    def expression(self, v):
        raise RuntimeError("3 : Invalid enumeration value.")


class TestQuotedStringParameter:
    """A CAM parameter already holding a QUOTED expression stores a string, and Fusion refuses the
    bare spelling - so a bare request is wrapped to match what the parameter reads, and the record
    says the wrapping happened."""

    def test_a_bare_value_for_a_quoted_parameter_is_written_quoted_and_marked(self, monkeypatch):
        op = _install(monkeypatch, params={"boundaryMode": "'silhouette'"})
        out = _payload(ce.handler(operation="Adaptive1",
                                  parameters={"boundaryMode": "selection"}))
        assert op.parameters.itemByName("boundaryMode").expression == "'selection'"
        assert out["changed"][0]["quoted"] is True and out["changed"][0]["after"] == "'selection'"

    def test_a_parameter_holding_an_unquoted_expression_is_written_as_sent(self, monkeypatch):
        op = _install(monkeypatch, params={"tool_stepover": "2."})
        out = _payload(ce.handler(operation="Adaptive1", parameters={"tool_stepover": "1.5"}))
        assert op.parameters.itemByName("tool_stepover").expression == "1.5"
        assert "quoted" not in out["changed"][0]

    def test_a_request_already_quoted_is_not_wrapped_twice(self, monkeypatch):
        op = _install(monkeypatch, params={"boundaryMode": "'silhouette'"})
        out = _payload(ce.handler(operation="Adaptive1",
                                  parameters={"boundaryMode": "'selection'"}))
        assert op.parameters.itemByName("boundaryMode").expression == "'selection'"
        assert "quoted" not in out["changed"][0]

    def test_the_enumeration_refusal_names_the_expression_written(self, monkeypatch):
        _install(monkeypatch,
                 params={"boundaryMode": EnumRefusingParam("boundaryMode", "'silhouette'")})
        res = ce.handler(operation="Adaptive1", parameters={"boundaryMode": "sillhouette"})
        assert res["isError"] is True
        assert "Invalid enumeration value" in res["message"]      # the platform's own text, relayed
        assert "the expression written was 'sillhouette'" in res["message"]
        # names the read that shows the parameter's own expression, like every sibling refusal
        assert "cam_get(include=['parameters'], operation=...)" in res["message"]

    def test_an_unrelated_setter_failure_carries_no_enumeration_clause(self, monkeypatch):
        # The clause is minted from the platform's own message, so a failure that never named an
        # enumeration must not collect the remedy for one.
        _install(monkeypatch)
        res = ce.handler(operation="Adaptive1", parameters={"maximumStepdown": "BOOM"})
        assert res["isError"] is True and "ENUMERATION" not in res["message"]


class EnumChoiceParam(FakeCAMParameter):
    """A CHOICE parameter: it refuses a value outside its set the way Fusion does, and its value
    answers getChoices() with that set."""
    @FakeCAMParameter.expression.setter
    def expression(self, v):
        raise RuntimeError("3 : Invalid enumeration value.")


class TestEnumerationRefusalNamesTheChoices:
    """A refusal that only points at a read costs the caller another turn - and a guessed token
    costs several. Where the parameter's own getChoices answers, the refusal lists what it takes."""

    def test_the_refusal_lists_the_values_the_parameter_takes(self, monkeypatch):
        _install(monkeypatch, params={"multiAxisMachiningType": EnumChoiceParam(
            "multiAxisMachiningType", "three_axis", value="three_axis",
            choices=["three_axis", "five_axis_simultaneous"])})
        res = ce.handler(operation="Adaptive1",
                         parameters={"multiAxisMachiningType": "five_axis"})
        assert res["isError"] is True
        assert "This parameter's own values: three_axis, five_axis_simultaneous" in res["message"]

    def test_a_parameter_with_no_choice_set_still_gets_the_read_pointer(self, monkeypatch):
        # No getChoices to read means no list to print, and the refusal falls back to the read that
        # shows what the parameter holds rather than printing an empty set.
        _install(monkeypatch,
                 params={"boundaryMode": EnumRefusingParam("boundaryMode", "'silhouette'")})
        res = ce.handler(operation="Adaptive1", parameters={"boundaryMode": "sillhouette"})
        assert "own values:" not in res["message"]
        assert "cam_get(include=['parameters'], operation=...)" in res["message"]


class _StuckAfterFirstWrite(FakeParam):
    """Takes one write and swallows every later one - the parameter whose RESTORE does not land, so
    the operation is left holding the value this call wrote."""

    @FakeParam.expression.setter
    def expression(self, value):
        if getattr(self, "_written_once", False):
            return
        self._written_once = True
        FakeParam.expression.fset(self, value)


class GatedParam(FakeParam):
    """A parameter another one UNLOCKS: isEditable follows the gate parameter's own expression,
    which is the shape numberOfStepovers reads behind doMultiplePasses."""

    def __init__(self, name, expression="", gate=None, **kw):
        super().__init__(name, expression, **kw)
        self._gate = gate

    @property
    def isEditable(self):
        return str(self._gate.expression).strip().lower() == "true"

    @isEditable.setter
    def isEditable(self, value):
        pass


class TestASwitchInTheSameCall:
    """A row whose isEditable is gated by ANOTHER parameter of the same request: the locked rows go
    LAST and their flag is re-read once the switch has landed, so one call carrying both works."""

    def _deburr(self, monkeypatch):
        gate = FakeParam("doMultiplePasses", "false")
        return _install(monkeypatch, params={
            "tool_feedCutting": "5210.23", "doMultiplePasses": gate,
            "numberOfStepovers": GatedParam("numberOfStepovers", "1", gate=gate)})

    def test_the_gated_row_is_written_after_the_switch_that_unlocks_it(self, monkeypatch):
        # The gated row is asked for FIRST, so a handler applying the request in its own order would
        # meet the locked flag and refuse.
        op = self._deburr(monkeypatch)
        out = _payload(ce.handler(operation="Adaptive1",
                                  parameters={"numberOfStepovers": "3",
                                              "doMultiplePasses": "true"}))
        assert op.parameters.itemByName("doMultiplePasses").expression == "true"
        assert op.parameters.itemByName("numberOfStepovers").expression == "3"
        rows = {c["name"]: c for c in out["changed"]}
        assert rows["numberOfStepovers"]["unlocked_here"] is True
        assert "unlocked_here" not in rows["doMultiplePasses"]
        assert "read isEditable false at the start of this call" in out["note"]

    def test_a_row_nothing_in_the_call_unlocks_is_written_around_and_restored(self, monkeypatch):
        op = self._deburr(monkeypatch)
        res = ce.handler(operation="Adaptive1",
                         parameters={"tool_feedCutting": "3000", "numberOfStepovers": "3"})
        assert res["isError"] is True
        assert "does not accept a write to: numberOfStepovers" in res["message"]
        assert "after the other 1 parameter(s) in this call were applied" in res["message"]
        assert "WROTE 1 parameter(s) on the operation and then restored each one" in res["message"]
        assert op.parameters.itemByName("tool_feedCutting").expression == "5210.23"
        assert op.parameters.itemByName("numberOfStepovers").expression == "1"

    def test_a_restore_that_did_not_come_back_is_named_not_claimed_restored(self, monkeypatch):
        # THE BITE: the refusal may only claim what the RE-READ saw. A parameter that swallows the
        # restore leaves the operation holding this call's value, and saying otherwise hands the
        # caller a state nothing read.
        gate = FakeParam("doMultiplePasses", "false")
        _install(monkeypatch, params={
            "tool_feedCutting": _StuckAfterFirstWrite("tool_feedCutting", "5210.23"),
            "doMultiplePasses": gate,
            "numberOfStepovers": GatedParam("numberOfStepovers", "1", gate=gate)})
        res = ce.handler(operation="Adaptive1",
                         parameters={"tool_feedCutting": "3000", "numberOfStepovers": "3"})
        assert res["isError"] is True
        assert "restored each one, but 1 did NOT come back" in res["message"]
        assert "'tool_feedCutting' reads '3000', held '5210.23'" in res["message"]
        assert "set each one back by hand" in res["message"]
        # and NOT the clean-restore sentence, which would contradict the line beside it
        assert "every restored expression reads back what it held" not in res["message"]
