"""Unit tests for ``cam_apply_template.py`` - the apply and what the SETUP reads back.

createFromCAMTemplate2 hands back operation objects, and that return is not evidence: an
incompatible template can return them while the setup takes none. The count read either side of
the call is the read the claim rests on, and the per-operation tool status is read off the setup's
own walk.
"""

import json
from types import SimpleNamespace

import pytest

from conftest import (FakeCAMFolder, FakeOperation, FakeSetup, FakeTool, _NamedCollection,
                      load_tool, make_cam)

ct = load_tool("cam_apply_template")


def _payload(result):
    assert result["isError"] is False, result
    return json.loads(result["content"][0]["text"])


class _FakeTemplate:
    def __init__(self, name="T"):
        self.name = name
        self.isValidTemplate = True


class TestApplyTemplateEnumValidation:
    # apply validates its enums up front (before any CAM work): an unrecognized 'generate' must be a
    # hard error, NOT silently coerced to skip (which would create operations but generate no toolpaths).
    def test_unknown_generate_is_rejected_not_silently_skipped(self):
        res = ct.handler(setup="S", template_name="T", generate="bogus")
        assert res["isError"] is True
        assert "generate" in res["message"].lower()

    def test_unknown_location_is_rejected(self):
        res = ct.handler(setup="S", template_name="T", location="atlantis")
        assert res["isError"] is True
        assert "location" in res["message"].lower()



# ── cam_apply_template: the setup's OWN operation count is what confirms the apply ───────────────
#
# createFromCAMTemplate2 hands back operation objects, and that return is not evidence: an
# incompatible template can return them while the setup takes none. The count read either side of
# the call is the read the claim rests on.


class _ApplyOp(FakeOperation):
    """An operation as the setup's walk reads it - `tool` is the description Operation.tool's own
    object answers (a Tool OBJECT is passed through), and None an operation carrying no tool."""

    def __init__(self, name, tool=None, strategy="drill"):
        super().__init__(name, strategy=strategy)
        self.tool = FakeTool(description=tool) if isinstance(tool, str) else tool


class _ApplySetup(FakeSetup):
    """The apply target: `adds` is what the template lands ((name, tool) pairs, or a count of
    tool-less ops - adds=0 models the call returning operations the setup never took), `existing`
    and `folders` what it already held, `phantom` a rise in its own COUNT the walk does not see,
    `count_unreadable` an allOperations that raises, `returns` what the apply call hands back."""

    def __init__(self, name, adds=1, existing=(), folders=(), phantom=0, count_unreadable=False,
                 returns=None):
        super().__init__(name, ops=[_ApplyOp(*row) for row in existing],
                         folders=[FakeCAMFolder(fname, ops=[_ApplyOp(*row) for row in ops])
                                  for fname, ops in folders])
        self.applied = []
        self._adds = ([(f"Op{i + 1}", None) for i in range(adds)]
                      if isinstance(adds, int) else list(adds))
        self._phantom_after = phantom
        self._phantom = 0
        self._count_unreadable = count_unreadable
        self._returns = returns

    @property
    def allOperations(self):
        if self._count_unreadable:
            raise RuntimeError("allOperations is not available")
        flat = list(FakeCAMFolder.allOperations.fget(self))
        return _NamedCollection(flat + [_ApplyOp(f"Phantom{i + 1}")
                                        for i in range(self._phantom)])

    def createFromCAMTemplate2(self, template_input):
        self.applied.append(template_input)
        made = [_ApplyOp(*row) for row in self._adds]
        self.operations._items.extend(made)
        self._phantom = self._phantom_after
        return made if self._returns is None else self._returns


def _wire_apply(monkeypatch, setup):
    monkeypatch.setattr(ct, "get_cam", lambda: (make_cam(setup), None))
    monkeypatch.setattr(ct, "_template_library", lambda: (SimpleNamespace(), None))
    monkeypatch.setattr(ct, "find_setup", lambda cam, name: (setup, [setup.name], None))
    monkeypatch.setattr(ct, "_find_template_by_name",
                        lambda lib, loc, name: (_FakeTemplate(name), None))


class TestApplyTemplateUrlAgainstName:
    """A url published by folder POSITION is only as good as that pairing, so a caller who sends
    both a url and the name it believes is there gets the disagreement, not an apply."""

    def _wire_url(self, monkeypatch, setup, at_url_name):
        monkeypatch.setattr(ct, "get_cam", lambda: (make_cam(setup), None))
        monkeypatch.setattr(ct, "_template_library",
                            lambda: (SimpleNamespace(templateAtURL=lambda u:
                                                     _FakeTemplate(at_url_name)), None))
        monkeypatch.setattr(ct, "find_setup", lambda cam, name: (setup, [setup.name], None))
        monkeypatch.setattr(ct.adsk.core.URL, "create", lambda s: SimpleNamespace(url=s),
                            raising=False)

    def test_a_url_loading_another_name_is_refused_and_applies_nothing(self, monkeypatch):
        setup = _ApplySetup("Setup1", adds=1)
        self._wire_url(monkeypatch, setup, at_url_name="Drill & Tap Countersink Hole")
        res = ct.handler(setup="Setup1", template_url="lib://root/a",
                                                 template_name="Bore")
        assert res["isError"] is True
        assert "Drill & Tap Countersink Hole" in res["message"] and "'Bore'" in res["message"]
        assert setup.applied == []               # nothing was applied on a disagreement

    def test_a_url_whose_name_agrees_applies(self, monkeypatch):
        # the boundary: same two inputs, matching (case-insensitively) - the apply proceeds.
        setup = _ApplySetup("Setup1", adds=1)
        self._wire_url(monkeypatch, setup, at_url_name="Bore")
        out = _payload(ct.handler(setup="Setup1",
                                                          template_url="lib://root/a",
                                                          template_name="bore"))
        assert out["applied"] is True and setup.applied


class TestApplyTemplateEffect:
    def test_a_template_that_adds_no_operation_is_an_error(self, monkeypatch):
        # The lying return: operations handed back while the setup's count stands still.
        setup = _ApplySetup("Setup1", adds=0)
        _wire_apply(monkeypatch, setup)
        res = ct.handler(setup="Setup1", template_name="T")
        assert res["isError"] is True
        assert "did not increase" in res["message"]
        assert setup.applied, "the template WAS applied - the count is what convicts the result"

    def test_one_added_operation_clears_the_growth_gate(self, monkeypatch):
        # The other side of `ops_after <= ops_before`: growth by exactly one is a landed apply.
        setup = _ApplySetup("Setup1", adds=1)
        _wire_apply(monkeypatch, setup)
        out = _payload(ct.handler(setup="Setup1", template_name="T"))
        assert out["applied"] is True
        assert out["operations_added"] == 1 and out["created_count"] == 1


# ── the applied operations' TOOL status, read off the setup ─────────────────────────────────────
#
# A template lands operations carrying no tool: they read Operation.tool null and generate nothing,
# while 'applied' and a count say only that they arrived. The rows come from the setup's own walk.


def _apply(monkeypatch, setup, **kwargs):
    """Apply a template to `setup` through the by-name path and return the payload."""
    _wire_apply(monkeypatch, setup)
    return _payload(ct.handler(setup=setup.name, template_name="T",
                                                       **kwargs))


class TestApplyTemplateToolStatus:
    def test_an_operation_with_no_tool_is_named_with_its_remedy(self, monkeypatch):
        setup = _ApplySetup("Setup1", adds=[("Spot Drill1", None), ("Drill1", None),
                                            ("Bore1", None)])
        out = _apply(monkeypatch, setup)
        assert out["operations_added"] == 3
        assert out["tool_unselected"] == ["Spot Drill1", "Drill1", "Bore1"]
        assert [r["tool"] for r in out["operations"]] == [None, None, None]
        assert out["ready"] is False
        assert "cam_edit_operation" in out["note"] and "tool_scope='document'" in out["note"]

    def test_an_operation_carrying_a_tool_is_not_listed_unselected(self, monkeypatch):
        setup = _ApplySetup("Setup1", adds=[("Face1", "#1 - 16mm Flat Endmill"), ("Drill1", None)])
        out = _apply(monkeypatch, setup)
        rows = {r["name"]: r for r in out["operations"]}
        assert rows["Face1"]["tool"] == "#1 - 16mm Flat Endmill"
        assert rows["Face1"]["strategy"] == "drill"
        assert out["tool_unselected"] == ["Drill1"] and out["ready"] is False

    def test_a_fully_tooled_apply_reads_ready(self, monkeypatch):
        setup = _ApplySetup("Setup1", adds=[("Face1", "#1 - 16mm Flat Endmill")])
        out = _apply(monkeypatch, setup)
        assert out["tool_unselected"] == [] and out["ready"] is True
        assert "cam_generate" in out["note"]

    def test_an_operation_the_setup_already_held_is_not_reported(self, monkeypatch):
        # The delta is the SETUP's own: a tool-less operation that was already there is not one
        # this apply landed, and naming it would send the caller after another op's tool.
        setup = _ApplySetup("Setup1", existing=[("OldFace", None)],
                            adds=[("Drill1", "#2 - 6mm Drill")])
        out = _apply(monkeypatch, setup)
        assert [r["name"] for r in out["operations"]] == ["Drill1"]
        assert out["tool_unselected"] == [] and out["ready"] is True

    def test_a_held_operation_in_a_folder_does_not_absorb_a_new_one_of_that_name(self, monkeypatch):
        # The boundary of the census credit, and why its key is the PATH: the walk visits the
        # setup's own operations BEFORE its folders, so a name-keyed census hands the held folder
        # op's credit to the new arrival and reports the HELD operation - tool and all.
        setup = _ApplySetup("Setup1", adds=[("Drill1", None)],
                            folders=[("Holes", [("Drill1", "#2 - 6mm Drill")])])
        out = _apply(monkeypatch, setup)
        assert [r["name"] for r in out["operations"]] == ["Drill1"]
        assert out["operations"][0]["tool"] is None
        assert out["tool_unselected"] == ["Drill1"] and out["ready"] is False

    def test_two_of_one_name_in_one_container_withholds_ready(self, monkeypatch):
        # The residual the breadcrumb cannot separate: both operations sit at the same path, so
        # which one this apply landed is not established and no order is allowed to decide it.
        setup = _ApplySetup("Setup1", existing=[("Drill1", "#2 - 6mm Drill")],
                            adds=[("Drill1", None)])
        out = _apply(monkeypatch, setup)
        assert out["ready"] is False
        assert "Drill1" in out["note"] and "not established" in out["note"]
        assert "cam_get(include=['operations'], setup=...)" in out["note"]

    def test_a_fully_tooled_collision_is_withheld_not_captioned(self, monkeypatch):
        # Both operations carry a tool, so nothing else withholds here: the pair is left OUT of
        # 'operations' and named in 'collisions', rather than handed back as an order-picked row.
        setup = _ApplySetup("Setup1", existing=[("Drill1", "#2 - 6mm Drill")],
                            adds=[("Drill1", "#7 - 3mm Drill")])
        out = _apply(monkeypatch, setup)
        assert out["ready"] is False and out["tool_unselected"] == []
        assert out["collisions"] == ["Drill1"] and out["operations"] == []

    def test_a_collision_alone_withholds_ready(self, monkeypatch):
        # The clause's OWN case: a tooled operation landed at a clean breadcrumb and the count
        # cannot contradict the rows, so the collision is the only thing left to withhold 'ready'.
        setup = _ApplySetup("Setup1", existing=[("Drill1", "#2 - 6mm Drill")],
                            adds=[("Face1", "#1 - 16mm Flat Endmill"), ("Drill1", "#7 - 3mm Drill")],
                            count_unreadable=True)
        out = _apply(monkeypatch, setup)
        assert [r["name"] for r in out["operations"]] == ["Face1"]
        assert out["tool_unselected"] == [] and out["collisions"] == ["Drill1"]
        assert out["ready"] is False and "left out of" in out["note"]

    def test_an_unreadable_count_with_no_new_operation_is_not_ready(self, monkeypatch):
        # allOperations raises, so the growth gate is skipped and operations_added reads null: an
        # EMPTY per-op read must not pass for readiness on the strength of nothing blocking.
        setup = _ApplySetup("Setup1", adds=[], count_unreadable=True)
        out = _apply(monkeypatch, setup)
        assert out["operations_added"] is None and out["operations"] == []
        assert out["ready"] is False and "does not account for operations_added" in out["note"]

    def test_the_rows_are_the_setups_own_not_what_the_apply_returned(self, monkeypatch):
        # The lying return again: it hands back operations the setup never took, so the tool status
        # is read off the setup's walk and not off those objects.
        setup = _ApplySetup("Setup1", adds=[("Drill1", None)],
                            returns=[SimpleNamespace(name="Ghost1"),
                                     SimpleNamespace(name="Ghost2")])
        out = _apply(monkeypatch, setup)
        assert out["created_operations"] == ["Ghost1", "Ghost2"] and out["created_count"] == 2
        assert [r["name"] for r in out["operations"]] == ["Drill1"]
        assert out["operations_added"] == 1 and out["tool_unselected"] == ["Drill1"]

    def test_a_count_the_per_op_read_cannot_account_for_withholds_ready(self, monkeypatch):
        # The setup's own count moved by two while one operation reads as new, so the per-op list
        # is incomplete and 'ready' is not claimed off it.
        setup = _ApplySetup("Setup1", adds=[("Drill1", "#2 - 6mm Drill")], phantom=1)
        out = _apply(monkeypatch, setup)
        assert out["operations_added"] == 2 and len(out["operations"]) == 1
        assert out["ready"] is False
        assert "does not account for operations_added" in out["note"]

    def test_a_tool_whose_description_did_not_read_is_not_called_unselected(self, monkeypatch):
        # Operation.tool answered an object: only its description is missing, and reporting that as
        # 'no tool' would send the caller to assign a tool the operation already carries.
        setup = _ApplySetup("Setup1", adds=[("Drill1", SimpleNamespace(description=None))])
        out = _apply(monkeypatch, setup)
        assert out["operations"][0]["tool"] is None
        assert out["operations"][0]["tool_description_unread"] is True
        assert out["tool_unselected"] == [] and out["ready"] is True


class _TemplateInputDouble:
    """CreateFromCAMTemplateInput has no measured shared fake; mode failures are injected."""

    def __init__(self, initial, fault):
        self.camTemplate = None
        self._mode = initial
        self._fault = fault

    @property
    def mode(self):
        if self._fault == "unreadable":
            raise RuntimeError("mode read failed")
        if self._fault == "missing":
            raise AttributeError("mode")
        return self._mode

    @mode.setter
    def mode(self, value):
        if self._fault == "setter":
            raise RuntimeError("mode setter failed")
        if self._fault != "mismatch":
            self._mode = value


@pytest.fixture
def mode_apply(monkeypatch):
    def apply(generate="generate", missing_member=False, fault=None):
        setup = _ApplySetup("Setup1", adds=[("Face1", "#1 - 16mm Flat Endmill")])
        modes = ct.adsk.cam.AutomaticGenerationModes
        requested = getattr(modes, ct._GEN_MODES[generate])
        alternate = getattr(modes, ct._GEN_MODES["skip" if generate == "generate" else "generate"])
        input_obj = _TemplateInputDouble(alternate, fault)
        _wire_apply(monkeypatch, setup)
        if missing_member:
            monkeypatch.setattr(ct.adsk.cam, "AutomaticGenerationModes", SimpleNamespace())
        monkeypatch.setattr(ct.adsk.cam, "CreateFromCAMTemplateInput",
                            SimpleNamespace(create=lambda: input_obj))
        result = ct.handler(setup="Setup1", template_name="T", generate=generate)
        return setup, input_obj, result, requested
    return apply


class TestApplyTemplateGenerationMode:
    @pytest.mark.parametrize("generate,member", [("skip", "SkipGeneration"),
                                               ("generate", "ForceGeneration")])
    def test_missing_selected_mode_is_rejected_before_apply(self, mode_apply, generate, member):
        setup, _input, result, _mode = mode_apply(generate, missing_member=True)
        assert result["isError"] is True and member in result["message"]
        assert setup.applied == [] and setup.allOperations.count == 0

    def test_existing_falsy_mode_is_assigned_and_apply_still_reads_ready(self, mode_apply):
        setup, input_obj, result, mode = mode_apply()
        out = _payload(result)
        assert not mode
        assert len(setup.applied) == 1 and setup.applied[0] is input_obj
        assert input_obj.mode == mode
        assert out["generation_mode"] == "generate"
        assert out["ready"] is True and out["operations_added"] == 1

    @pytest.mark.parametrize("fault,needle", [
        ("mismatch", "did not take"), ("missing", "did not take"),
        ("unreadable", "did not take"), ("setter", "Could not set"),
    ])
    def test_mode_assignment_or_readback_failure_is_rejected_before_apply(self, mode_apply,
                                                                        fault, needle):
        setup, _input, result, _mode = mode_apply(fault=fault)
        assert result["isError"] is True and needle in result["message"]
        assert "AutomaticGenerationModes.ForceGeneration" in result["message"]
        assert setup.applied == [] and setup.allOperations.count == 0
