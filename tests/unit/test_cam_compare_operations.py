"""Unit tests for ``cam_compare_operations.py`` -- the diff over two CAM operations'
parameters. Covers the diff logic (same vs differing parameters, not-present-on-one-side) and the
bounded-read cap on 'differences'.
"""

import json
from types import SimpleNamespace

import pytest

from conftest import (FakeCAMParameter, FakeCAMParameters, FakeOperation, FakeSetup, FakeTool,
                      load_tool, make_cam, make_cam_parameters)

cc = load_tool("cam_compare_operations")


def _titled(rows, title="Offset"):
    """Parameters that all share one TITLE and differ only by name."""
    return FakeCAMParameters([FakeCAMParameter(n, e, title=title) for n, e in rows])


def _op(name, params, tool_desc="Tool1"):
    """An operation whose parameters are `params` ({name: expression}) and whose tool carries
    `tool_desc`."""
    return FakeOperation(name, parameters=make_cam_parameters(*params.items()),
                         tool=FakeTool(description=tool_desc))


@pytest.fixture
def install(monkeypatch):
    """Wire a set of operations into the tool's get_cam seam; patches undo themselves."""
    def _install(operations):
        cam = make_cam(FakeSetup("Setup1", ops=operations))
        monkeypatch.setattr(cc, "get_cam", lambda: (cam, None))
        return cam
    return _install


def _payload(result):
    assert result["isError"] is False, result
    return json.loads(result["content"][0]["text"])


class TestGuards:
    def test_missing_operation_names_refused(self):
        res = cc.handler(operation_a="", operation_b="")
        assert res["isError"] is True and "operation_a" in res["message"]

    def test_no_cam_data_errors(self, monkeypatch):
        monkeypatch.setattr(cc, "get_cam",
                            lambda: (None, "This document has no CAM (Manufacture) data."))
        res = cc.handler(operation_a="A", operation_b="B")
        assert res["isError"] is True
        assert "no CAM (Manufacture) data" in res["message"]

    def test_operation_not_found_errors(self, install):
        install([_op("Op1", {"p1": "1"})])
        res = cc.handler(operation_a="Op1", operation_b="Ghost")
        assert res["isError"] is True and "Ghost" in res["message"]
        assert "ambiguous" not in res["message"].lower()     # a true miss stays not-found

    def test_a_miss_lists_whole_names_capped_by_count(self, install):
        # the shared resolver's not-found reaches this tool's callers, so every name it prints has
        # to be a spelling this same input takes back: the list is capped by NAME COUNT with the
        # remainder counted, never cut mid-name at a character budget.
        install([_op(f"Operation-{i:02d}-LongEnoughToTruncate", {"p": "1"})
                 for i in range(20)])
        res = cc.handler(operation_a="Ghost", operation_b="Operation-00")
        assert res["isError"] is True
        listed = res["message"].split("Available: ")[1].rstrip(".").split(", ")
        assert listed[:8] == [f"Operation-{i:02d}-LongEnoughToTruncate" for i in range(8)]
        assert listed[8:] == ["... (+12 more not listed)"]

    def test_duplicate_name_is_refused_with_the_ordinal_addresses_this_input_takes(self, monkeypatch):
        # Two setups each holding a 'Drill1' - names collide across parents, never between setups
        # (Fusion refuses a duplicate SETUP name outright). This tool carries no scope input, so the
        # way through it names is the resolver's '<name>#<n>' address, which the SAME input resolves:
        # nothing outside the call has to happen first, which is why an address is preferred wherever
        # one separates the candidates. The resolver does word a rename elsewhere - the two-readings
        # branch, where no address separates the readings at all (_common._RENAME_REMEDY is the same
        # trade) - but no tool here renames a CAM operation, so it is never offered in its place.
        cam = make_cam(FakeSetup("Setup1", ops=[_op("Drill1", {"p": "1"})]),
                       FakeSetup("Setup2", ops=[_op("Drill1", {"p": "2"})]))
        monkeypatch.setattr(cc, "get_cam", lambda: (cam, None))
        res = cc.handler(operation_a="Drill1", operation_b="Drill1")
        assert res["isError"] is True
        assert "ambiguous" in res["message"].lower()
        assert "Drill1#1" in res["message"] and "Drill1#2" in res["message"]
        assert "Rename" not in res["message"]

    def test_an_ordinal_address_resolves_the_operation_it_names(self, monkeypatch):
        # the address the refusal above hands back must actually resolve on this input, or the
        # remedy is decoration: '#2' picks the SECOND setup's Drill1, whose parameter differs.
        cam = make_cam(FakeSetup("Setup1", ops=[_op("Drill1", {"feed": "100"})]),
                       FakeSetup("Setup2", ops=[_op("Drill1", {"feed": "900"})]))
        monkeypatch.setattr(cc, "get_cam", lambda: (cam, None))
        out = _payload(cc.handler(operation_a="Drill1#1", operation_b="Drill1#2"))
        assert out["difference_count"] == 1
        assert out["differences"][0]["operation_a"] == "100"
        assert out["differences"][0]["operation_b"] == "900"


class _RecordingParameters:
    """A CAM parameter collection that COUNTS every itemByName - the lookup _geometry_facts makes
    per selection parameter. It records rather than raises: safe() swallows a raise, so a raising
    stand-in would let the read happen and still report a refusal."""

    def __init__(self, inner):
        self._inner = inner
        self.lookups = 0

    @property
    def count(self):
        return self._inner.count

    def item(self, i):
        return self._inner.item(i)

    def itemByName(self, name):
        self.lookups += 1
        return self._inner.itemByName(name)


def _unsettled(name, params, tool_desc="Tool1"):
    """An operation MID-GENERATION: the flag raised over a state that has not answered (NoToolpath,
    no toolpath yet) - what op_settled reads as generating still to do."""
    op = FakeOperation(name, parameters=make_cam_parameters(*params.items()),
                       tool=FakeTool(description=tool_desc),
                       has_toolpath=False, operation_state=3)
    op.isGenerating = True
    return op


class TestGeneratingGuard:
    def test_the_guard_refuses_BEFORE_any_selection_parameter_is_read(self, install):
        # ORDER, not just presence. The crash observation is about walking selection objects on a
        # regenerating document, so a guard placed after _geometry_facts would refuse having
        # already made the read it exists to prevent - and this counter is what tells them apart.
        a = _op("A", {"feed": "100"})
        b = _unsettled("B", {"feed": "200"})
        b.parameters = _RecordingParameters(b.parameters)
        install([a, b])
        res = cc.handler(operation_a="A", operation_b="B")
        assert res["isError"] is True
        assert b.parameters.lookups == 0, "the refusal read selection parameters before refusing"

    def test_an_unsettled_operation_refuses_the_compare_naming_the_count(self, install):
        # The geometry half reads the selection objects off both operations; one compare on a
        # document whose operations were still regenerating ended the Fusion process.
        a = _op("A", {"feed": "100"})
        b = _unsettled("B", {"feed": "200"})
        install([a, b])
        res = cc.handler(operation_a="A", operation_b="B")
        assert res["isError"] is True
        assert "1 of the 2 named operations still has generating to do (B)" in res["message"]
        assert "cam_get_status until completed=true" in res["message"]
        b.isGenerating = False
        out = _payload(cc.handler(operation_a="A", operation_b="B"))
        assert out["operation_a"] == "A" and out["operation_b"] == "B"
        assert out["difference_count"] == 1

    def test_the_count_and_its_verb_agree_when_both_are_unsettled(self, install):
        # '1 ... still have' was the wart; the verb is interpolated off the count, so both spellings
        # need a case or only the singular is ever read.
        install([_unsettled("A", {"feed": "100"}), _unsettled("B", {"feed": "200"})])
        res = cc.handler(operation_a="A", operation_b="B")
        assert res["isError"] is True
        assert "2 of the 2 named operations still have generating to do (A, B)" in res["message"]

    def test_a_flag_left_raised_over_a_settled_operation_does_not_refuse(self, monkeypatch, install):
        # MEASURED: isGenerating stays true for ~1.1 s past the Future completing, so the raw flag
        # would refuse a compare cam_get_status already calls completed - and the refusal's own
        # remedy would never come true. Both settle on _cam_common.op_settled.
        a, b = _op("A", {"feed": "100"}), _op("B", {"feed": "200"})
        b.isGenerating = True                     # state 0 with a toolpath: the flag is lagging
        install([a, b])
        out = _payload(cc.handler(operation_a="A", operation_b="B"))
        assert out["operation_a"] == "A" and out["operation_b"] == "B"
        assert out["difference_count"] == 1


class TestDiffLogic:
    def test_matching_parameters_are_not_differences(self, install):
        install([_op("A", {"feed": "100", "speed": "5000"}),
                 _op("B", {"feed": "100", "speed": "5000"})])
        out = _payload(cc.handler(operation_a="A", operation_b="B"))
        assert out["difference_count"] == 0
        assert out["same_parameter_count"] == 2
        assert out["differences"] == []

    def test_differing_value_reported_on_both_sides(self, install):
        install([_op("A", {"feed": "100"}), _op("B", {"feed": "200"})])
        out = _payload(cc.handler(operation_a="A", operation_b="B"))
        assert out["difference_count"] == 1
        d = out["differences"][0]
        assert d["parameter"] == "feed" and d["operation_a"] == "100" and d["operation_b"] == "200"

    def test_parameter_only_on_one_side_reported_as_not_present(self, install):
        install([_op("A", {"feed": "100", "onlyA": "x"}),
                 _op("B", {"feed": "100"})])
        out = _payload(cc.handler(operation_a="A", operation_b="B"))
        d = next(d for d in out["differences"] if d["parameter"] == "onlyA")
        assert d["operation_a"] == "x" and d["operation_b"] == "(not present)"

    def test_reports_tool_descriptions(self, install):
        install([_op("A", {}, tool_desc="Ball 6mm"),
                 _op("B", {}, tool_desc="Flat 10mm")])
        out = _payload(cc.handler(operation_a="A", operation_b="B"))
        assert out["tool_a"] == "Ball 6mm" and out["tool_b"] == "Flat 10mm"

    def test_colliding_titles_keyed_by_name_are_not_masked(self, install):
        # two parameters share a TITLE ("Offset") but differ by NAME - keying the diff by title would
        # let one overwrite the other and MASK a real difference. Keyed by name, BOTH surface: the
        # matching topOffset is same, the differing bottomOffset is a difference. Title rides for display.
        cam = install([_op("A", {}), _op("B", {})])
        op_a = cam.setups.item(0).allOperations.item(0)
        op_b = cam.setups.item(0).allOperations.item(1)
        op_a.parameters = _titled([("topOffset", "1"), ("bottomOffset", "2")])
        op_b.parameters = _titled([("topOffset", "1"), ("bottomOffset", "9")])
        out = _payload(cc.handler(operation_a="A", operation_b="B"))
        assert out["same_parameter_count"] == 1        # topOffset matched (not masked by the collision)
        assert out["difference_count"] == 1
        d = out["differences"][0]
        assert d["parameter"] == "bottomOffset"        # keyed by the unique NAME
        assert d["title"] == "Offset"                  # title still reported for display
        assert d["operation_a"] == "2" and d["operation_b"] == "9"


class _UnnamedParam(FakeCAMParameter):
    """A CAM parameter whose NAME will not read. The diff is keyed by name, so there is no key to
    file this one under - and keying it on the unreadable read would collide every such parameter
    onto one row."""

    @property
    def name(self):
        raise RuntimeError("3 : name unavailable")

    @name.setter
    def name(self, value):
        pass


class _OpWithUnreadableParameters(FakeOperation):
    """An operation that resolved but whose parameter collection raises."""

    @property
    def parameters(self):
        raise RuntimeError("3 : parameters unavailable")

    @parameters.setter
    def parameters(self, value):
        pass


class _OpWithUnreadableTool(FakeOperation):
    """An operation that resolved and reads its parameters, but whose .tool raises."""

    @property
    def tool(self):
        raise RuntimeError("3 : no tool")

    @tool.setter
    def tool(self, value):
        pass


class TestUnreadableReads:
    """The diff is keyed by parameter NAME, so a parameter whose name will not read has no key to
    stand under. An unreadable collection is a hole in the diff, not a failed call: both operations
    resolved, and everything that DID read is still worth reporting."""

    def test_a_parameter_with_no_readable_name_is_skipped(self, install):
        cam = install([_op("A", {}), _op("B", {})])
        op_a = cam.setups.item(0).allOperations.item(0)
        op_a.parameters = FakeCAMParameters([FakeCAMParameter("feed", "100", title="Feed"),
                                             _UnnamedParam("anon", "7", title="Anon")])
        out = _payload(cc.handler(operation_a="A", operation_b="B"))
        assert [d["parameter"] for d in out["differences"]] == ["feed"]

    def test_an_unreadable_parameter_collection_leaves_that_side_empty(self, install):
        install([_op("A", {"feed": "100"}),
                 _OpWithUnreadableParameters("B", tool=FakeTool(description="Flat 10mm"))])
        out = _payload(cc.handler(operation_a="A", operation_b="B"))
        assert out["difference_count"] == 1
        assert out["differences"][0]["operation_b"] == "(not present)"

    def test_an_unreadable_tool_reports_null_rather_than_failing_the_diff(self, install):
        install([_op("A", {"feed": "100"}),
                 _OpWithUnreadableTool("B", parameters=make_cam_parameters(("feed", "100")))])
        out = _payload(cc.handler(operation_a="A", operation_b="B"))
        assert out["tool_b"] is None
        assert out["same_parameter_count"] == 1


# ── BOUNDED READS: 'differences' is capped (CLAUDE.md "Bound it") ────────────────────────────────

class TestCaps:
    def test_under_cap_untruncated_and_unchanged(self, install):
        params_a = {f"p{i}": "a" for i in range(5)}
        params_b = {f"p{i}": "b" for i in range(5)}
        install([_op("A", params_a), _op("B", params_b)])
        out = _payload(cc.handler(operation_a="A", operation_b="B"))
        assert out["truncated"] is False
        assert len(out["differences"]) == 5
        assert out["difference_count"] == 5

    def test_at_cap_truncates_and_flags(self, install):
        n = cc._DIFFERENCES_CAP + 30
        params_a = {f"p{i}": "a" for i in range(n)}
        params_b = {f"p{i}": "b" for i in range(n)}
        install([_op("A", params_a), _op("B", params_b)])
        out = _payload(cc.handler(operation_a="A", operation_b="B",
                                                       max_results=cc._DIFFERENCES_CAP))
        assert out["truncated"] is True
        assert len(out["differences"]) == cc._DIFFERENCES_CAP
        # the full count is still honest, even though the array is capped
        assert out["difference_count"] == n

    def test_a_caller_cannot_lift_the_cap_past_the_ceiling(self, install):
        # every row crosses the wire, so max_results is clamped into 1.._DIFFERENCES_CEILING -
        # an oversized request is held at the ceiling, not honoured.
        n = cc._DIFFERENCES_CEILING + 25
        params_a = {f"p{i:04d}": "a" for i in range(n)}
        params_b = {f"p{i:04d}": "b" for i in range(n)}
        install([_op("A", params_a), _op("B", params_b)])
        out = _payload(cc.handler(operation_a="A", operation_b="B",
                                                     max_results=999999))
        assert len(out["differences"]) == cc._DIFFERENCES_CEILING
        assert out["truncated"] is True and out["difference_count"] == n

    def test_each_side_publishes_the_strategy_id_and_the_create_name_apart(self, install):
        # MEASURED: the 'strategy' PARAMETER reads the internal id ('parallel_new') while
        # Operation.strategy reads the create vocabulary ('parallel'). The diff row below carries
        # only the id, so a caller comparing two strategies would carry a spelling
        # cam_create_operation raises on ('Unknown strategy').
        a = FakeOperation("A", parameters=make_cam_parameters(("strategy", "'parallel_new'")),
                          strategy="parallel", tool=FakeTool(description="T"))
        b = FakeOperation("B", parameters=make_cam_parameters(("strategy", "'scallop_new'")),
                          strategy="scallop", tool=FakeTool(description="T"))
        install([a, b])
        out = _payload(cc.handler(operation_a="A", operation_b="B"))
        assert out["strategy_a"] == "parallel_new" and out["strategy_name_a"] == "parallel"
        assert out["strategy_b"] == "scallop_new" and out["strategy_name_b"] == "scallop"
        # the id is still the diff row's value, which is the vocabulary the note tells them apart by
        row = next(d for d in out["differences"] if d["parameter"] == "strategy")
        assert row["operation_a"] == "'parallel_new'"
        assert "take strategy_name" in out["note"]

    def test_a_non_numeric_max_results_falls_back_to_the_default(self, install):
        # the wire types it integer, but the clamp must not raise on a junk value either
        params_a = {f"p{i}": "a" for i in range(3)}
        params_b = {f"p{i}": "b" for i in range(3)}
        install([_op("A", params_a), _op("B", params_b)])
        out = _payload(cc.handler(operation_a="A", operation_b="B",
                                                     max_results="lots"))
        assert len(out["differences"]) == 3 and out["truncated"] is False


# ── the GEOMETRY half: what is SELECTED, which no parameter expression carries ────────────────────

class _CurveSelection:
    """One CAM curve selection: the read-back channel (outputGeometry, value) plus the per-kind
    properties this one answers - an unset name raises, the way a class not carrying it does."""

    def __init__(self, segments=(), entities=0, **props):
        self.outputGeometry = [SimpleNamespace(count=n) for n in segments]
        self.value = list(range(entities))
        for key, value in props.items():
            setattr(self, key, value)


class _CurveSelections:
    """A CurveSelections collection: count + item, the bounded walk the compare reads."""

    def __init__(self, items=()):
        self._items = list(items)

    @property
    def count(self):
        return len(self._items)

    def item(self, index):
        return self._items[index]


class _CurveParam(FakeCAMParameter):
    """A curve-selection parameter, whose .value answers getCurveSelections()."""

    def __init__(self, name, selections=()):
        super().__init__(name)
        self.value = SimpleNamespace(getCurveSelections=lambda: _CurveSelections(selections))


class _ObjectSetParam(FakeCAMParameter):
    """A direct/surface set parameter, whose .value.value is the CAD-object list."""

    def __init__(self, name, entities=0):
        super().__init__(name)
        self.value = SimpleNamespace(value=list(range(entities)))


def _geo_op(name, params):
    """An operation carrying selection parameters and nothing else."""
    return FakeOperation(name, parameters=FakeCAMParameters(list(params)),
                         tool=FakeTool(description="T"))


class TestGeometryDiff:
    """Two operations can carry byte-identical parameter expressions and cut different material:
    what is selected lives on the selection objects, not in any expression."""

    def test_the_same_parameters_with_a_different_chain_knob_are_not_identical(self, install):
        install([_geo_op("A", [_CurveParam("contours", [_CurveSelection([4], 4, isOpen=False)])]),
                 _geo_op("B", [_CurveParam("contours", [_CurveSelection([4], 4, isOpen=True)])])])
        out = _payload(cc.handler(operation_a="A", operation_b="B"))
        assert out["difference_count"] == 0            # nothing in the expressions moved
        assert out["geometry_difference_count"] == 1
        row = out["geometry_differences"][0]
        assert row["parameter"] == "contours"
        assert row["operation_a"]["properties"][0]["isOpen"] is False
        assert row["operation_b"]["properties"][0]["isOpen"] is True

    def test_a_tangential_extension_on_one_chain_is_a_difference(self, install):
        # The ledger's own case: two 2D contours on the same chain, one extended tangentially, read
        # 0 of 462 parameter differences - the extension lives on the ChainSelection.
        import adsk.cam
        distance = getattr(adsk.cam.ExtensionTypes, "DistanceExtensionType")
        boundary = getattr(adsk.cam.ExtensionTypes, "BoundaryExtensionType")
        install([_geo_op("A", [_CurveParam("contours", [
                    _CurveSelection([4], 4, extensionType=boundary, startExtensionLength=0.0)])]),
                 _geo_op("B", [_CurveParam("contours", [
                    _CurveSelection([4], 4, extensionType=distance, startExtensionLength=0.5)])])])
        out = _payload(cc.handler(operation_a="A", operation_b="B"))
        assert out["difference_count"] == 0
        assert out["geometry_difference_count"] == 1
        row = out["geometry_differences"][0]["operation_b"]["properties"][0]
        # the enum decodes to its member spelling, not the ordinal a caller cannot read
        assert row["extensionType"] == "distance"
        # ...and the length is Fusion's internal CM scaled into the requested units: 0.5 cm = 5 mm
        assert row["startExtensionLength"] == 5.0
        assert out["geometry_units"] == "mm"

    def test_a_selection_length_is_scaled_into_the_requested_units(self, install):
        # 0.5 cm reads 5 mm, 0.5 cm, and 0.19685 in - the raw cm would misreport every one of them.
        for units, want in (("mm", 5.0), ("cm", 0.5), ("in", 0.196850)):
            install([_geo_op("A", [_CurveParam("contours", [
                        _CurveSelection([4], 4, minimumCornerRadius=0.5)])]),
                     _geo_op("B", [_CurveParam("contours", [
                        _CurveSelection([4], 4, minimumCornerRadius=0.0)])])])
            out = _payload(cc.handler(operation_a="A", operation_b="B", units=units))
            row = out["geometry_differences"][0]["operation_a"]["properties"][0]
            assert row["minimumCornerRadius"] == want, units
            assert out["geometry_units"] == units

    def test_an_unknown_units_key_is_refused_naming_the_valid_ones(self, install):
        install([_geo_op("A", []), _geo_op("B", [])])
        res = cc.handler(operation_a="A", operation_b="B", units="furlong")
        assert res["isError"] is True and "furlong" in res["message"] and "mm" in res["message"]

    def test_a_loop_type_decodes_to_the_spelling_cam_select_geometry_takes(self, install):
        # the enum int crossing raw would hand back a number that input does not accept
        import adsk.cam
        inside = getattr(adsk.cam.LoopTypes, "OnlyInsideLoops")
        install([_geo_op("A", [_CurveParam("contours", [_CurveSelection([1], 1, loopType=inside)])]),
                 _geo_op("B", [_CurveParam("contours", [_CurveSelection([1], 1)])])])
        out = _payload(cc.handler(operation_a="A", operation_b="B"))
        row = out["geometry_differences"][0]["operation_a"]["properties"][0]
        assert row["loopType"] == "inside"

    def test_a_differing_entity_count_on_a_direct_set_is_a_difference(self, install):
        install([_geo_op("A", [_ObjectSetParam("holeFaces", 3)]),
                 _geo_op("B", [_ObjectSetParam("holeFaces", 7)])])
        out = _payload(cc.handler(operation_a="A", operation_b="B"))
        assert out["geometry_difference_count"] == 1
        row = out["geometry_differences"][0]
        assert row["operation_a"] == {"entities": 3} and row["operation_b"] == {"entities": 7}

    def test_a_set_only_one_side_carries_reads_not_present(self, install):
        install([_geo_op("A", [_ObjectSetParam("driveSurfaces", 2)]), _geo_op("B", [])])
        out = _payload(cc.handler(operation_a="A", operation_b="B"))
        row = next(r for r in out["geometry_differences"] if r["parameter"] == "driveSurfaces")
        assert row["operation_a"] == {"entities": 2} and row["operation_b"] == "(not present)"

    def test_matching_selections_are_counted_same_not_reported(self, install):
        install([_geo_op("A", [_ObjectSetParam("holeFaces", 3)]),
                 _geo_op("B", [_ObjectSetParam("holeFaces", 3)])])
        out = _payload(cc.handler(operation_a="A", operation_b="B"))
        assert out["same_geometry_count"] == 1 and out["geometry_differences"] == []

    def test_a_zero_zero_answer_names_what_it_did_not_compare(self, install):
        install([_geo_op("A", [_ObjectSetParam("holeFaces", 3)]),
                 _geo_op("B", [_ObjectSetParam("holeFaces", 3)])])
        out = _payload(cc.handler(operation_a="A", operation_b="B"))
        assert "not read here" in out["note"] and "open both operations in Fusion" in out["note"]
        assert out["geometry_properties_read"] == list(cc._SELECTION_PROPS)

    def test_no_selection_set_answering_is_a_different_zero(self, install):
        # neither op carries a selection parameter: the geometry counts are absent evidence, and
        # publishing geometry_properties_read would claim reads that never happened.
        install([_geo_op("A", []), _geo_op("B", [])])
        out = _payload(cc.handler(operation_a="A", operation_b="B"))
        assert out["geometry_difference_count"] == 0 and out["same_geometry_count"] == 0
        assert "NO selection set answered" in out["note"]
        assert "geometry_properties_read" not in out and "geometry_units" not in out

    def test_a_parameter_difference_does_not_silence_the_no_geometry_disclosure(self, install):
        # two ops differing only in feed and carrying NO selection parameter: the diff is non-zero,
        # so gating the disclosure on it published a silent 0/0 the description promises to explain.
        a = _op("A", {"tool_feedCutting": "100"})
        b = _op("B", {"tool_feedCutting": "900"})
        install([a, b])
        out = _payload(cc.handler(operation_a="A", operation_b="B"))
        assert out["difference_count"] == 1 and out["geometry_difference_count"] == 0
        assert "NO selection set answered" in out["note"]

    def test_one_difference_anywhere_drops_that_disclosure(self, install):
        install([_geo_op("A", [_ObjectSetParam("holeFaces", 3)]),
                 _geo_op("B", [_ObjectSetParam("holeFaces", 4)])])
        out = _payload(cc.handler(operation_a="A", operation_b="B"))
        assert "not read here" not in out["note"] and "geometry_properties_read" not in out

    def test_the_selection_rows_are_capped_one_over_and_not_at_the_cap(self, install):
        cap = cc._SELECTION_ROWS_CAP
        at = [_CurveSelection([1], 1) for _ in range(cap)]
        over = [_CurveSelection([1], 1) for _ in range(cap + 1)]
        install([_geo_op("A", [_CurveParam("contours", at)]),
                 _geo_op("B", [_CurveParam("contours", over)])])
        out = _payload(cc.handler(operation_a="A", operation_b="B"))
        row = out["geometry_differences"][0]
        assert row["operation_a"]["selections"] == cap
        assert len(row["operation_a"]["properties"]) == cap
        assert "properties_truncated" not in row["operation_a"]
        assert row["operation_b"]["selections"] == cap + 1
        assert len(row["operation_b"]["properties"]) == cap
        assert row["operation_b"]["properties_truncated"] is True
