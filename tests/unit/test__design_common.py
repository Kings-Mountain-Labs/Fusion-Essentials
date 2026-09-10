"""Unit tests for ``_design_common.py`` - the mode read and the base-feature scope runner.

Pinned (the definition of done):
  - get_mode_handler reports each designType + the capability `can{}` map (derived from the ONE true
    reader, so report and guards agree).
  - the wrapper finishes-in-a-FINALLY even when the inner op raises (a leaked open scope would
    corrupt later calls), and run_in_base_feature opens no scope at all in a direct design.
"""

import pytest

import live_api_facts as _api_facts
from conftest import (FakeBaseFeature, FakeBaseFeatures, FakeFeatures, MakeComp, install,
                      load_tool, make_design, make_timeline, payload)

dm = load_tool("_design_common")

_DESIGN_TYPES = _api_facts.ENUMS["fusion.DesignTypes"]
_PARAMETRIC = _DESIGN_TYPES["ParametricDesignType"]
_DIRECT = _DESIGN_TYPES["DirectDesignType"]


def _design(design_type=_PARAMETRIC, timeline=None, base_features=None, edit_object=None):
    """A design whose root component carries a baseFeatures collection, plus the designType and
    activeEditObject the mode read and the scope detector branch on."""
    comp = MakeComp("Root")
    comp.features = FakeFeatures(base_features=base_features)
    return make_design(comp=comp, design_type=design_type, timeline=timeline,
                       active_edit_object=edit_object)


def _wire(monkeypatch, design):
    """Point the shared design seams at `design`, with the BaseFeature TYPE the open-scope detector
    isinstance-checks an activeEditObject against."""
    import adsk.fusion
    monkeypatch.setattr(adsk.fusion, "BaseFeature", FakeBaseFeature)
    return install(dm, design)


# ── get_mode_handler (mode slice) ─────────────────────────────────────────────────────────

class TestGetMode:
    def test_no_active_design(self, monkeypatch):
        _wire(monkeypatch, None)
        res = dm.get_mode_handler()
        assert res["isError"] is True and "No active design" in res["message"]

    def test_reports_parametric_and_capabilities(self, monkeypatch):
        _wire(monkeypatch, _design(timeline=make_timeline("A", "B", "C", "D")))
        out = payload(dm.get_mode_handler())
        assert out["design_type"] == "parametric"
        assert out["has_timeline"] is True
        assert out["timeline_feature_count"] == 4
        can = out["can"]
        # parametric: coordinate datums OFF, offset plane / timeline / base-feature / ->direct ON
        assert can["construction_point_by_coordinate"] is False
        assert can["construction_axis_by_line"] is False
        assert can["construction_plane_by_offset"] is True
        assert can["timeline_ops"] is True
        assert can["base_feature_scope"] is True
        assert can["convert_to_direct"] is True
        assert can["convert_to_parametric"] is False

    def test_reports_direct_and_capabilities(self, monkeypatch):
        # direct design: no timeline attribute at all
        _wire(monkeypatch, _design(design_type=_DIRECT))
        out = payload(dm.get_mode_handler())
        assert out["design_type"] == "direct"
        assert out["has_timeline"] is False
        assert out["timeline_feature_count"] is None
        can = out["can"]
        # direct: coordinate datums ON; timeline / base-feature OFF; ->parametric ON
        assert can["construction_point_by_coordinate"] is True
        assert can["construction_axis_by_line"] is True
        assert can["construction_plane_by_offset"] is True
        assert can["timeline_ops"] is False
        assert can["base_feature_scope"] is False
        assert can["convert_to_direct"] is False
        assert can["convert_to_parametric"] is True

    def test_counts_base_features(self, monkeypatch):
        bf = FakeBaseFeatures([FakeBaseFeature("BF1"), FakeBaseFeature("BF2")])
        _wire(monkeypatch, _design(timeline=make_timeline("A", "B"), base_features=bf))
        out = payload(dm.get_mode_handler())
        assert out["base_feature_count"] == 2

    def test_in_base_feature_edit_true_when_editing(self, monkeypatch):
        _wire(monkeypatch, _design(edit_object=FakeBaseFeature()))
        out = payload(dm.get_mode_handler())
        assert out["in_base_feature_edit"] is True


# ── the leak-proof wrapper: finish-in-finally even when the inner op raises ──────────────────────

class TestBaseFeatureWrapper:
    def test_inner_op_runs_inside_scope_and_scope_finishes(self, monkeypatch):
        _wire(monkeypatch, _design())
        bf = FakeBaseFeature("W")
        seen = {}

        def open_scope():
            return bf, None

        def inner(b):
            seen["editing_during_op"] = b._open
            return "result"

        out_bf, result = dm.base_feature_run_wrapper(open_scope, inner)
        assert out_bf is bf and result == "result"
        assert seen["editing_during_op"] is True      # the op saw an OPEN scope
        assert bf._finishes == 1 and bf._open is False  # and it was finished

    def test_scope_finishes_in_finally_when_inner_raises(self, monkeypatch):
        # A raising inner op must still finish the scope (a leaked open base-feature edit corrupts later
        # tool calls), and the error must propagate.
        _wire(monkeypatch, _design())
        bf = FakeBaseFeature("W")

        def open_scope():
            return bf, None

        def inner(b):
            raise RuntimeError("inner op exploded")

        with pytest.raises(RuntimeError, match="inner op exploded"):
            dm.base_feature_run_wrapper(open_scope, inner)
        assert bf._finishes == 1 and bf._open is False   # finished despite the raise

    def test_open_scope_error_short_circuits_before_any_scope(self, monkeypatch):
        _wire(monkeypatch, _design())
        err = dm.error("cannot open")

        def open_scope():
            return None, err

        ran = {"inner": False}

        def inner(b):
            ran["inner"] = True

        out_bf, result = dm.base_feature_run_wrapper(open_scope, inner)
        assert out_bf is None and result is err and ran["inner"] is False

    def test_startEdit_false_in_wrapper_errors_without_running_inner(self, monkeypatch):
        _wire(monkeypatch, _design())
        bf = FakeBaseFeature("W", start_ok=False)
        ran = {"inner": False}

        def open_scope():
            return bf, None

        def inner(b):
            ran["inner"] = True

        out_bf, result = dm.base_feature_run_wrapper(open_scope, lambda b: inner(b))
        assert result["isError"] is True and "startEdit returned false" in result["message"]
        assert ran["inner"] is False


# ── run_in_base_feature: the BLESSED mode-aware helper mesh write tools import ────────────────────

class TestRunInBaseFeature:
    def test_direct_runs_inner_directly_with_no_scope(self, monkeypatch):
        # DIRECT design: inner_op runs directly, gets None, and NO base feature is add()ed.
        des = _wire(monkeypatch, _design(design_type=_DIRECT))
        comp = des.rootComponent
        seen = {}

        def inner(bf):
            seen["bf"] = bf
            return "direct-result"

        result, err = dm.run_in_base_feature(des, comp, inner)
        assert err is None and result == "direct-result"
        assert seen["bf"] is None                                  # inner got None (no scope)
        assert comp.features.baseFeatures.count == 0               # add() was NEVER called

    def test_parametric_runs_inner_inside_atomic_scope(self, monkeypatch):
        # PARAMETRIC: a fresh base feature is add()ed, opened, inner runs inside it, then it finishes.
        des = _wire(monkeypatch, _design())
        comp = des.rootComponent
        seen = {}

        def inner(bf):
            seen["editing_during_op"] = bf._open
            return "param-result"

        result, err = dm.run_in_base_feature(des, comp, inner)
        assert err is None and result == "param-result"
        bf = comp.features.baseFeatures.item(0)
        assert seen["editing_during_op"] is True                  # op saw an OPEN scope
        assert bf._finishes == 1 and bf._open is False            # and it was finished

    def test_parametric_finishes_in_finally_when_inner_raises(self, monkeypatch):
        # The helper must finish the scope even when the inner op raises, and propagate the error.
        des = _wire(monkeypatch, _design())
        comp = des.rootComponent

        def inner(bf):
            raise RuntimeError("mesh import exploded")

        with pytest.raises(RuntimeError, match="mesh import exploded"):
            dm.run_in_base_feature(des, comp, inner)
        bf = comp.features.baseFeatures.item(0)
        assert bf._finishes == 1 and bf._open is False            # finished despite the raise

    def test_parametric_open_failure_returns_error_not_crash(self, monkeypatch):
        # add() returning nothing surfaces as a ready-to-return error, and inner never runs.
        des = _wire(monkeypatch, _design(base_features=FakeBaseFeatures(made=False)))
        comp = des.rootComponent
        ran = {"inner": False}

        def inner(bf):
            ran["inner"] = True

        result, err = dm.run_in_base_feature(des, comp, inner)
        assert result is None and err is not None and err["isError"] is True
        assert ran["inner"] is False
