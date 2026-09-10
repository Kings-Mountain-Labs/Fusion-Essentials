"""Tests for `cam_get` — the CAM rich read (setups default + include= deeper slices).

Same fixture-based pattern as test_design_get.py (CLAUDE.md "Tests"): stub
the _slice_* SEAMS and assert the ROUTER's composition (default = setups orientation only; an include=
returns that slice INSTEAD of the orientation unless 'default'/'setups' is named beside it; the note
advertises the rest; unknown include + no-CAM guards). The slice→source-handler
delegation is proven by live validation, not by mocking 6 handlers' internals.
"""

import json
from types import SimpleNamespace

import pytest

from conftest import (FakeCAMFolder, FakeCAMParameter, FakeCAMParameters, FakeOperation,
                      FakeSetup, _NamedCollection, error_message, load_tool, make_cam)

cg = load_tool("cam_get")


def _payload(result):
    assert result["isError"] is False, result
    return json.loads(result["content"][0]["text"])


def _named_op(name):
    """The Operation a by-name RESOLVE needs: the walk classifies nodes structurally and reads
    only .name."""
    return FakeOperation(name)


@pytest.fixture
def stub_slices(monkeypatch):
    monkeypatch.setattr(cg, "get_cam", lambda: (object(), None))   # a CAM product
    monkeypatch.setattr(cg, "_slice_setups", lambda cam, setup: (
        {"setup_count": 2, "setups": [{"name": "Setup1", "operation_count": 3}]}, None))
    monkeypatch.setattr(cg, "_slice_operations", lambda cam, setup: ({"operations": []}, None))
    monkeypatch.setattr(cg, "_slice_strategies",
                        lambda cam, setup: ({"setup_count": 0, "setups": []}, None))
    monkeypatch.setattr(cg, "_slice_references", lambda cam, setup: ({"references": []}, None))
    monkeypatch.setattr(cg, "_slice_nc_programs", lambda cam: ({"nc_programs": []}, None))
    monkeypatch.setattr(cg, "_slice_time", lambda cam, setup, units: ({"total_minutes": 12}, None))
    monkeypatch.setattr(cg, "_slice_machine",
                        lambda cam, setup, units: ({"setup_count": 0, "setups": []}, None))
    monkeypatch.setattr(cg, "_slice_tools", lambda cam: ({"tools": []}, None))
    monkeypatch.setattr(cg, "_slice_library",
                        lambda cam, scope, library, tool_type: ({"tool_count": 0, "tools": []}, None))
    monkeypatch.setattr(cg, "_slice_library_types", lambda cam: ({"type_count": 0, "types": []}, None))
    monkeypatch.setattr(cg, "_slice_machines",
                        lambda cam, vendor, machine_type: ({"count": 0, "machines": []}, None))
    monkeypatch.setattr(cg, "_slice_templates",
                        lambda cam, loc, url, depth: ({"node_count": 0, "tree": {}}, None))
    monkeypatch.setattr(cg, "_slice_inspection",
                        lambda cam, measure, max_results, units: (
                            {"available": False, "measure_count": 0, "measures": []}, None))


class TestDefaultSlice:
    def test_default_returns_setups_only(self, stub_slices):
        out = _payload(cg.handler())
        assert "setups" in out and "setup_count" in out
        # the heavy slices must be absent by default (anti-flood)
        for k in ("operations", "strategies", "references", "nc_programs", "time", "tools",
                  "library_types", "inspection"):
            assert k not in out

    def test_default_note_advertises_remaining(self, stub_slices):
        out = _payload(cg.handler())
        assert "include=" in out["note"]


class TestIncludeSlices:
    @pytest.mark.parametrize("slice_name,key", [
        ("operations", "operations"),
        ("references", "references"),
        ("nc_programs", "nc_programs"),
        ("time", "time"),
        ("tools", "tools"),
    ])
    def test_include_adds_the_slice(self, stub_slices, slice_name, key):
        out = _payload(cg.handler(include=[slice_name]))
        assert key in out

    def test_setup_filter_passes_through(self, monkeypatch, stub_slices):
        seen = {}
        monkeypatch.setattr(cg, "_slice_operations",
                            lambda cam, setup: (seen.update(setup=setup) or {"operations": []}, None))
        cg.handler(include=["operations"], setup="Setup1")
        assert seen["setup"] == "Setup1"

    def test_multiple_includes(self, stub_slices):
        out = _payload(cg.handler(include=["operations", "time"]))
        assert "operations" in out and "time" in out


class TestDeepReadDropsTheDefaultSlice:
    """A deep include= returns the slice asked for, NOT the orientation slice again - a
    cam_get(include=['tool']) that re-emits every setup pays for the whole job twice. The caller
    keeps the orientation by naming it: 'default', or the slice's own name 'setups'."""

    def test_a_deep_include_omits_the_setups_slice(self, stub_slices):
        out = _payload(cg.handler(include=["operations"]))
        assert "operations" in out
        assert "setups" not in out and "setup_count" not in out

    def test_default_beside_a_deep_slice_keeps_both(self, stub_slices):
        out = _payload(cg.handler(include=["default", "operations"]))
        assert out["setup_count"] == 2 and "operations" in out

    def test_the_slices_own_name_keeps_it_too(self, stub_slices):
        out = _payload(cg.handler(include=["setups", "time"]))
        assert out["setups"][0]["name"] == "Setup1" and "time" in out

    def test_default_alone_is_the_orientation_read(self, stub_slices):
        out = _payload(cg.handler(include=["default"]))
        assert "setups" in out and "operations" not in out
        assert "include=" in out["note"]          # still advertises the slices not pulled

    def test_the_advertising_note_rides_only_on_the_default_read(self, stub_slices):
        assert "include=" not in _payload(cg.handler(include=["operations"])).get("note", "")
        assert "include=" in _payload(cg.handler(include=["default", "operations"]))["note"]

    def test_the_validity_caveat_rides_on_every_read_that_carries_op_state(self, stub_slices):
        for inc in (None, ["operations"], ["default", "operations"]):
            assert "Manufacture" in _payload(cg.handler(include=inc))["note"]
        assert "note" not in _payload(cg.handler(include=["time"]))

    def test_a_deep_read_publishes_no_orientation_pointers(self, monkeypatch):
        # the pointers are read OFF the orientation rows, so a deep read has nothing to point at -
        # and the pointer walk must take the unread slice without raising.
        monkeypatch.setattr(cg, "get_cam", lambda: (object(), None))
        monkeypatch.setattr(cg, "_slice_setups", lambda cam, setup: (
            {"setups": [{"name": "S", "op_states": {"out_of_date": 6}}]}, None))
        monkeypatch.setattr(cg, "_slice_time", lambda cam, setup, units: ({"total_minutes": 5}, None))
        out = _payload(cg.handler(include=["time"]))
        assert "pointers" not in out and "setups" not in out


class TestReferencesCensus:
    """The references slice states WHAT it counted, because 0 without a census reads as a verdict on
    the document.

    Measured live on a two-setup job holding 6 reference links: each setup selects three occurrences
    that read isReferencedComponent False (a model, a fixture and a stock container), the slice
    counted 0, and three referenced components sat one level below them - which
    doc_get(include=['xref_tree']) found. The underlying walk tests the SELECTED entries and does
    not descend, so the note has to say that rather than let 0 stand alone.
    """

    def _siblings(self):
        """The sibling modules _slice_references reaches through its own `from . import` - resolved
        off cam_get's package so the object patched here is the one the slice will import."""
        import importlib
        pkg = cg.__package__
        return (importlib.import_module(pkg + "._cam_read"),
                importlib.import_module(pkg + "._common"))

    def _stub_source(self, monkeypatch, rows):
        """Stub the read core _slice_references delegates to, so these tests cover the slice's own
        census composition and not _cam_read's walk."""
        cr, common = self._siblings()
        monkeypatch.setattr(cr, "get_setup_references_handler",
                            lambda setup="": common.ok({"setup_count": len(rows), "setups": rows}))

    def test_zero_references_still_states_what_was_counted(self, monkeypatch):
        # THE BITE: the live shape - selections present, none of them a referenced component.
        self._stub_source(monkeypatch, [
            {"setup": "Op1", "reference_count": 0, "references": [], "references_truncated": False},
            {"setup": "Op2", "reference_count": 0, "references": [], "references_truncated": False}])
        payload, err = cg._slice_references(object(), "")
        assert err is None
        note = payload["note"]
        assert "Counted 0 referenced component(s)" in note
        assert "2 setup(s)" in note
        assert "selects directly" in payload["counted"]
        # the reader must be told 0 is not a verdict on the document, and where the deeper read is
        assert "not 'this document has no external references'" in note
        assert "doc_get(include=['xref_tree'])" in note

    def test_the_census_names_the_slice_that_carries_the_selection_keys(self, monkeypatch):
        # The note sits on references.note, and references.setups[] carries NONE of
        # selected_models / fixtures / stock_solids - those live on the TOP-LEVEL setups[] slice. A
        # bare 'setups[].selected_models' resolves against the object the note sits in and finds
        # nothing, so the sentence has to name which slice it means.
        self._stub_source(monkeypatch, [
            {"setup": "Op1", "reference_count": 0, "references": [], "references_truncated": False}])
        payload, _ = cg._slice_references(object(), "")
        assert "top-level setups[] slice" in payload["note"]
        assert "setups[].selected_models" not in payload["note"]

    def test_the_count_is_the_sum_across_setups(self, monkeypatch):
        self._stub_source(monkeypatch, [
            {"setup": "Op1", "reference_count": 2, "references": [{}, {}],
             "references_truncated": False},
            {"setup": "Op2", "reference_count": 1, "references": [{}],
             "references_truncated": False}])
        payload, _ = cg._slice_references(object(), "")
        assert "Counted 3 referenced component(s)" in payload["note"]
        assert "2 setup(s)" in payload["note"]

    def test_a_missing_reference_count_is_not_read_as_a_number(self, monkeypatch):
        # a row whose count did not read must not crash the census or silently count as 0 more than
        # it already does - the sum still has to compose.
        self._stub_source(monkeypatch, [
            {"setup": "Op1", "reference_count": None, "references": []},
            {"setup": "Op2", "reference_count": 2, "references": [{}, {}]}])
        payload, _ = cg._slice_references(object(), "")
        assert "Counted 2 referenced component(s)" in payload["note"]

    def test_no_setups_in_scope_counts_zero_setups(self, monkeypatch):
        self._stub_source(monkeypatch, [])
        payload, _ = cg._slice_references(object(), "")
        assert "Counted 0 referenced component(s)" in payload["note"]
        assert "0 setup(s)" in payload["note"]

    def test_a_truncated_selection_list_is_disclosed_beside_the_census(self, monkeypatch):
        # even the selected-entry census is incomplete when a selection list would not read or hit
        # its cap - a census that does not say so overstates what it examined.
        self._stub_source(monkeypatch, [
            {"setup": "Op1", "reference_count": 1, "references": [{}],
             "references_truncated": True}])
        payload, _ = cg._slice_references(object(), "")
        assert "references_truncated" in payload["note"]
        assert "incomplete" in payload["note"]

    def test_one_truncated_setup_among_several_still_discloses_incompleteness(self, monkeypatch):
        # The disclosure is ANY, not ALL: one setup whose selection list would not read makes the
        # whole census incomplete. With a single row any and all agree, so only a job with a
        # truncated setup BESIDE a clean one separates them - and under 'all' the sentence silently
        # drops, leaving the census overstating what it examined.
        self._stub_source(monkeypatch, [
            {"setup": "Op1", "reference_count": 1, "references": [{}],
             "references_truncated": True},
            {"setup": "Op2", "reference_count": 0, "references": [],
             "references_truncated": False}])
        payload, _ = cg._slice_references(object(), "")
        assert "incomplete" in payload["note"]
        assert "references_truncated" in payload["note"]

    def test_an_untruncated_read_does_not_claim_incompleteness(self, monkeypatch):
        self._stub_source(monkeypatch, [
            {"setup": "Op1", "reference_count": 1, "references": [{}],
             "references_truncated": False}])
        payload, _ = cg._slice_references(object(), "")
        assert "incomplete" not in payload["note"]

    def test_a_source_error_passes_through_with_no_census(self, monkeypatch):
        cc, common = self._siblings()
        monkeypatch.setattr(cc, "get_setup_references_handler",
                            lambda setup="": common.error("No CAM product."))
        payload, err = cg._slice_references(object(), "")
        assert payload is None and err is not None      # nothing to state a census over

    def test_the_census_rides_through_the_router(self, monkeypatch):
        # the slice is reached through include=['references'], so the sentence actually crosses the
        # wire rather than living on a helper nobody calls.
        monkeypatch.setattr(cg, "get_cam", lambda: (object(), None))
        monkeypatch.setattr(cg, "_slice_setups", lambda cam, setup: ({"setups": []}, None))
        self._stub_source(monkeypatch, [
            {"setup": "Op1", "reference_count": 0, "references": [], "references_truncated": False}])
        out = _payload(cg.handler(include=["references"]))
        assert "Counted 0 referenced component(s)" in out["references"]["note"]


class TestStrategiesSlice:
    """include=['strategies'] = each setup's compatible strategy vocabulary with its
    isGenerationAllowed entitlement flag, delegated to cam_create_operation.read_strategies (the
    tool whose 'strategy' input that vocabulary types, so both sides take ONE walk).

    Measured on the base license (Fusion 2705.1.4): a milling setup offers 54 strategies, 33
    reading allowed and 21 blocked; a blocked one creates successfully and then never generates.
    """

    # Measured on a milling setup's 54 strategies: is_suppressible read true on every row, the rest
    # false but for one is_cutting (profile2d) and one is_additive (feature_construction).
    _FLAG_DEFAULTS = {"is_suppressible": True}

    def _row(self, name, allowed, **flags):
        row = {"name": name, "title": name.title(), "allowed": allowed}
        for key in ("is_2d", "is_3d", "is_drilling", "is_milling", "is_rotary", "is_turning",
                    "is_finishing", "is_additive", "is_cutting", "is_support", "is_suppressible"):
            row[key] = flags.get(key, self._FLAG_DEFAULTS.get(key, False))
        return row

    def _stub_source(self, monkeypatch, setups):
        """Stub read_strategies so these tests cover the SLICE's own razor/cap/note, not the walk."""
        cco = load_tool("cam_create_operation")
        seen = {}
        monkeypatch.setattr(cco, "read_strategies",
                            lambda setup="": (seen.update(setup=setup) or {
                                "isError": False,
                                "content": [{"type": "text", "text": json.dumps(
                                    {"setup_count": len(setups), "setups": setups})}]}))
        return seen

    def test_router_includes_strategies_and_passes_the_setup_scope(self, monkeypatch, stub_slices):
        seen = {}
        monkeypatch.setattr(cg, "_slice_strategies",
                            lambda cam, setup: (seen.update(setup=setup)
                                                or ({"setup_count": 1, "setups": []}, None)))
        out = _payload(cg.handler(include=["strategies"], setup="Op1"))
        assert out["strategies"]["setup_count"] == 1
        assert seen == {"setup": "Op1"}

    def test_the_slice_delegates_to_cam_create_operations_reader(self, monkeypatch):
        seen = self._stub_source(monkeypatch, [
            {"setup": "Op1", "strategy_count": 1, "allowed_count": 1, "blocked_count": 0,
             "strategies": [self._row("face", True, is_2d=True)]}])
        out, err = cg._slice_strategies(object(), "Op1")
        assert err is None and out["setups"][0]["setup"] == "Op1"
        assert seen == {"setup": "Op1"}          # the scope reaches the reader, not just the router

    def test_a_false_classification_flag_drops_but_a_blocked_entitlement_survives(self,
                                                                                   monkeypatch):
        # THE razor bite: 'allowed' is the read the create refuses on, so a false there must never
        # be collapsed as noise the way is_rotary false is. Dropping it would publish a blocked
        # strategy as indistinguishable from an allowed one.
        self._stub_source(monkeypatch, [
            {"setup": "Op1", "strategy_count": 2, "allowed_count": 1, "blocked_count": 1,
             "strategies": [self._row("face", True, is_2d=True, is_milling=True),
                            self._row("steep_and_shallow", False, is_3d=True, is_finishing=True)]}])
        out, _err = cg._slice_strategies(object(), "")
        by_name = {r["name"]: r for r in out["setups"][0]["strategies"]}
        assert by_name["steep_and_shallow"]["allowed"] is False
        assert by_name["face"]["allowed"] is True
        assert by_name["face"]["is_2d"] is True and by_name["face"]["is_milling"] is True
        assert "is_rotary" not in by_name["face"]            # the quiet answer drops
        assert "is_2d" not in by_name["steep_and_shallow"]

    def test_the_added_classification_flags_go_through_the_same_razor(self, monkeypatch):
        # a flag the razor does not know stays on every row it appears in, which is the flood the
        # razor exists to stop - so each added key needs its own quiet default here.
        self._stub_source(monkeypatch, [
            {"setup": "Op1", "strategy_count": 1, "allowed_count": 1, "blocked_count": 0,
             "strategies": [self._row("lattice", True, is_additive=True, is_support=True)]}])
        out, _err = cg._slice_strategies(object(), "")
        row = out["setups"][0]["strategies"][0]
        assert row["is_additive"] is True and row["is_support"] is True
        assert "is_cutting" not in row and "is_suppressible" not in row

    def test_is_suppressible_collapses_at_TRUE_and_pops_where_it_read_false(self, monkeypatch):
        # its quiet value is the opposite of the others': measured true on all 54 strategies of a
        # milling setup, so a true-only razor would stamp it on every row and the one strategy that
        # cannot be suppressed - the row actually worth seeing - would be the one saying nothing.
        self._stub_source(monkeypatch, [
            {"setup": "Op1", "strategy_count": 2, "allowed_count": 2, "blocked_count": 0,
             "strategies": [self._row("face", True),
                            self._row("locked", True, is_suppressible=False)]}])
        out, _err = cg._slice_strategies(object(), "")
        by_name = {r["name"]: r for r in out["setups"][0]["strategies"]}
        assert "is_suppressible" not in by_name["face"]           # the usual answer drops
        assert by_name["locked"]["is_suppressible"] is False      # the exception pops

    def test_a_classification_flag_that_did_not_read_survives_the_razor_as_null(self, monkeypatch):
        # the razor drops a flag's own USUAL value only, so a flag read_flag could not read stays
        # and pops as null - which is why the note says a flag is omitted at its usual value and
        # cannot say flags show only where true.
        self._stub_source(monkeypatch, [
            {"setup": "Op1", "strategy_count": 1, "allowed_count": 1, "blocked_count": 0,
             "strategies": [self._row("mystery", True, is_2d=None, is_milling=True)]}])
        out, _err = cg._slice_strategies(object(), "")
        row = out["setups"][0]["strategies"][0]
        assert row["is_2d"] is None                  # unreadable, not the quiet default
        assert "is_drilling" not in row              # read its usual false, dropped
        assert "at its usual value is dropped (false; is_suppressible true)" in out["note"]

    def test_an_unreadable_entitlement_survives_the_razor_as_null(self, monkeypatch):
        # null is not the noise default, so it stays and pops - an unknown entitlement must never
        # read as an allowed one.
        self._stub_source(monkeypatch, [
            {"setup": "Op1", "strategy_count": 1, "allowed_count": 0, "blocked_count": 0,
             "unreadable_count": 1, "strategies": [self._row("mystery", None)]}])
        out, _err = cg._slice_strategies(object(), "")
        assert out["setups"][0]["strategies"][0]["allowed"] is None
        assert out["setups"][0]["unreadable_count"] == 1

    def test_rows_are_capped_across_setups_and_the_cap_is_flagged(self, monkeypatch):
        # one over the cap: the cap bites, the flag is set, and the pointer names the scope input.
        rows = [self._row(f"s{i}", True) for i in range(cg._STRATEGY_CAP + 1)]
        self._stub_source(monkeypatch, [
            {"setup": "Op1", "strategy_count": len(rows), "allowed_count": len(rows),
             "blocked_count": 0, "strategies": rows}])
        out, _err = cg._slice_strategies(object(), "")
        assert len(out["setups"][0]["strategies"]) == cg._STRATEGY_CAP
        assert out["truncated"] is True
        assert f"Capped at {cg._STRATEGY_CAP} rows" in out["note"]
        assert "'setup' scopes it" in out["note"]

    def test_exactly_the_cap_is_not_truncated(self, monkeypatch):
        # the other side of `emitted >= _STRATEGY_CAP`: a job that fits exactly may not be reported
        # as cut short, and every row it holds must survive.
        rows = [self._row(f"s{i}", True) for i in range(cg._STRATEGY_CAP)]
        self._stub_source(monkeypatch, [
            {"setup": "Op1", "strategy_count": len(rows), "allowed_count": len(rows),
             "blocked_count": 0, "strategies": rows}])
        out, _err = cg._slice_strategies(object(), "")
        assert len(out["setups"][0]["strategies"]) == cg._STRATEGY_CAP
        assert "truncated" not in out
        assert "Capped at" not in out["note"]      # the cap sentence's own opening words

    def test_the_cap_counts_ACROSS_setups_not_per_setup(self, monkeypatch):
        # a per-setup cap lets N setups each emit the full cap - the flood the bound exists to stop.
        half = cg._STRATEGY_CAP // 2 + 1
        rows = [self._row(f"s{i}", True) for i in range(half)]
        self._stub_source(monkeypatch, [
            {"setup": "Op1", "strategy_count": half, "allowed_count": half, "blocked_count": 0,
             "strategies": list(rows)},
            {"setup": "Op2", "strategy_count": half, "allowed_count": half, "blocked_count": 0,
             "strategies": list(rows)}])
        out, _err = cg._slice_strategies(object(), "")
        emitted = sum(len(s["strategies"]) for s in out["setups"])
        assert emitted == cg._STRATEGY_CAP and out["truncated"] is True

    def test_the_true_per_setup_total_rides_through_a_truncated_read(self, monkeypatch):
        # the capped rows are the flood control; strategy_count is what says how much was cut.
        rows = [self._row(f"s{i}", True) for i in range(cg._STRATEGY_CAP + 7)]
        self._stub_source(monkeypatch, [
            {"setup": "Op1", "strategy_count": len(rows), "allowed_count": len(rows),
             "blocked_count": 0, "strategies": rows}])
        out, _err = cg._slice_strategies(object(), "")
        assert out["setups"][0]["strategy_count"] == cg._STRATEGY_CAP + 7
        assert "strategy_count is the true total" in out["note"]

    def test_the_note_says_what_allowed_means_and_that_null_refuses_nothing(self, monkeypatch):
        self._stub_source(monkeypatch, [
            {"setup": "Op1", "strategy_count": 0, "allowed_count": 0, "blocked_count": 0,
             "strategies": []}])
        out, _err = cg._slice_strategies(object(), "")
        note = out["note"]
        assert "isGenerationAllowed" in note
        assert "cam_create_operation REFUSES" in note
        assert "no refusal follows" in note
        assert "silently" not in note
        # the strategies read is where an agent is CHOOSING, so it is where the recipe that
        # teaches the choice is named - one call, not a second browse of the same list.
        assert f"sys_get_guidance(recipe='{cg.STRATEGY_RECIPE_ID}')" in note
        # what a blocked op then does is taught where an agent meets it - the create's own refusal
        # and cam_generate's entitlement_blocked - not restated on every strategy browse.
        assert "entitlement_blocked" not in note

    def test_a_setup_whose_list_did_not_read_survives_the_slice_and_is_named(self, monkeypatch):
        # the row's empty 'strategies' reads exactly like a setup that offers nothing, so both the
        # KEY and a note clause naming it have to reach the wire - the key alone is a null nobody
        # is looking for, and the note alone cannot say which setup.
        self._stub_source(monkeypatch, [
            {"setup": "Op1", "strategies_read": False, "strategy_count": None,
             "allowed_count": None, "blocked_count": None, "strategies": []},
            {"setup": "Op2", "strategy_count": 1, "allowed_count": 1, "blocked_count": 0,
             "strategies": [self._row("face", True, is_2d=True)]}])
        out, _err = cg._slice_strategies(object(), "")
        assert out["setups"][0]["strategies_read"] is False
        assert out["setups"][0]["strategy_count"] is None
        assert "strategies_read false" in out["note"]

    def test_an_all_readable_read_does_not_mention_the_unreadable_case(self, monkeypatch):
        # the clause is CONDITIONAL: riding every read would make the one payload it describes
        # indistinguishable from the healthy ones.
        self._stub_source(monkeypatch, [
            {"setup": "Op1", "strategy_count": 1, "allowed_count": 1, "blocked_count": 0,
             "strategies": [self._row("face", True)]}])
        out, _err = cg._slice_strategies(object(), "")
        assert "strategies_read" not in out["note"]

    def test_the_worst_composed_note_fits_the_wire_budget(self, monkeypatch):
        # the note is assembled at run time from up to three pieces, so test_prose_budget measures
        # none of the compositions - the cap sentence, the base note and the unreadable clause all
        # ride together when a big job holds one setup that would not report its list.
        rows = [self._row(f"s{i}", True) for i in range(cg._STRATEGY_CAP + 1)]
        self._stub_source(monkeypatch, [
            {"setup": "Op1", "strategies_read": False, "strategy_count": None,
             "allowed_count": None, "blocked_count": None, "strategies": []},
            {"setup": "Op2", "strategy_count": len(rows), "allowed_count": len(rows),
             "blocked_count": 0, "strategies": rows}])
        out, _err = cg._slice_strategies(object(), "")
        assert out["truncated"] is True and "strategies_read false" in out["note"]
        assert len(out["note"]) <= 400, len(out["note"])       # test_prose_budget.NOTE_BUDGET_CHARS

    def test_a_source_error_passes_through_with_no_note(self, monkeypatch):
        cco = load_tool("cam_create_operation")
        monkeypatch.setattr(cco, "read_strategies",
                            lambda setup="": cg.error("No setup named 'Ghost'. Available: Op1."))
        payload, err = cg._slice_strategies(object(), "Ghost")
        assert payload is None
        assert "Ghost" in err["message"]

    def test_the_slice_rides_through_the_router(self, monkeypatch):
        # end to end: the reader's payload reaches the wire under include=['strategies'].
        monkeypatch.setattr(cg, "get_cam", lambda: (object(), None))
        monkeypatch.setattr(cg, "_slice_setups", lambda cam, setup: ({"setups": []}, None))
        self._stub_source(monkeypatch, [
            {"setup": "Op1", "strategy_count": 1, "allowed_count": 0, "blocked_count": 1,
             "strategies": [self._row("steep_and_shallow", False, is_3d=True)]}])
        out = _payload(cg.handler(include=["strategies"]))
        assert out["strategies"]["setups"][0]["strategies"][0]["allowed"] is False

    def test_the_default_note_advertises_the_slice(self, stub_slices):
        # a flag nothing names is invisible - the orientation note is where an agent finds it.
        assert "'strategies'" in _payload(cg.handler())["note"]


class TestCamPointers:
    """`_cam_pointers` - the actionable breadcrumb: a present, actionable CAM state names the tool that
    resolves it (stale/ungenerated toolpaths -> cam_generate; out-of-date machine -> cam_edit_setup)."""

    def test_stale_ops_point_at_cam_generate(self):
        p = cg._cam_pointers([{"op_states": {"out_of_date": 2, "no_toolpath": 3}}])
        assert "cam_generate" in p["toolpaths"] and "5" in p["toolpaths"]   # 2 + 3 summed

    def test_out_of_date_machine_points_at_edit_setup(self):
        p = cg._cam_pointers([{"op_states": {}, "machine_out_of_date": True}])
        assert "cam_edit_setup" in p["machine"]

    def test_clean_setup_gets_no_pointers(self):
        # all valid, machine current -> nothing to point at (no noise).
        p = cg._cam_pointers([{"op_states": {"suppressed": 4}, "machine_out_of_date": False}])
        assert p == {}

    def test_sums_across_setups(self):
        p = cg._cam_pointers([{"op_states": {"out_of_date": 2}}, {"op_states": {"no_toolpath": 1}}])
        assert "3" in p["toolpaths"]

    def test_router_emits_pointers_on_stale_default(self, monkeypatch):
        monkeypatch.setattr(cg, "get_cam", lambda: (object(), None))
        monkeypatch.setattr(cg, "_slice_setups", lambda cam, setup: (
            {"setup_count": 1, "setups": [{"name": "S", "op_states": {"out_of_date": 6},
                                           "machine_out_of_date": True}]}, None))
        out = _payload(cg.handler())
        assert "cam_generate" in out["pointers"]["toolpaths"]
        assert "cam_edit_setup" in out["pointers"]["machine"]


class TestOrientationDedup:
    """Content-aware de-dup on a call that asks for BOTH ('default' beside a deep slice): a fact the
    included slice restates per-op is dropped from the orientation copy, while the context an agent
    needs to act on a cold jump-in (machine, op_states, names) is preserved."""

    @pytest.fixture
    def stub_with_reasons(self, monkeypatch):
        monkeypatch.setattr(cg, "get_cam", lambda: (object(), None))
        monkeypatch.setattr(cg, "_slice_setups", lambda cam, setup: ({"setup_count": 1, "setups": [
            {"name": "Op1", "machine": "Haas", "op_states": {"out_of_date": 2},
             "invalidation_reasons": ["Design changed: WCS origin"]}]}, None))
        monkeypatch.setattr(cg, "_slice_operations", lambda cam, setup: ({"operations": []}, None))
        monkeypatch.setattr(cg, "_slice_time", lambda cam, setup, units: ({"total_minutes": 5}, None))

    def test_default_keeps_setup_invalidation_reasons(self, stub_with_reasons):
        out = _payload(cg.handler())
        assert "invalidation_reasons" in out["setups"][0]

    def test_operations_drops_setup_reasons_keeps_context(self, stub_with_reasons):
        out = _payload(cg.handler(include=["default", "operations"]))
        s = out["setups"][0]
        assert "invalidation_reasons" not in s          # restated per-op -> dropped from orientation
        assert s["machine"] == "Haas" and "op_states" in s and s["name"] == "Op1"   # context preserved

    def test_unrelated_include_keeps_setup_reasons(self, stub_with_reasons):
        # the time slice does NOT restate invalidation reasons, so they stay
        out = _payload(cg.handler(include=["default", "time"]))
        assert "invalidation_reasons" in out["setups"][0]


class TestGuards:
    def test_unknown_include_errors(self, stub_slices):
        res = cg.handler(include=["bogus"])
        assert "bogus" in error_message(res).lower() or "unknown" in error_message(res).lower()

    def test_the_refusal_lists_the_default_tokens_beside_the_slices(self, stub_slices):
        # the refusal IS the vocabulary an agent that mistyped reads next: a list that names only
        # the deep slices hides the one token that keeps the orientation block beside them.
        msg = error_message(cg.handler(include=["bogus"]))
        assert "operations" in msg and "default" in msg and "setups" in msg

    def test_the_include_enum_matches_the_slice_tuple(self, stub_slices):
        # Catches a HAND-EDITED schema drifting from the tuple. It cannot catch a name added to the
        # tuple itself - both sides read it - which is what the dispatch test below covers.
        enum = cg.tool.input_schema["properties"]["include"]["items"]["enum"]
        assert sorted(enum) == sorted(cg._SLICES + cg._DEFAULT_NAMES)

    def test_every_advertised_slice_actually_dispatches(self, monkeypatch, stub_slices):
        # A name in _SLICES that no `if ... in inc` branch reads returns a silent empty ok: the
        # schema offers a slice the router never builds. Each one must put its own key in the payload.
        monkeypatch.setattr(cg, "_slice_parameters",
                            lambda cam, operation, setup, units: ({"sections": {}}, None))
        monkeypatch.setattr(cg, "_slice_tool",
                            lambda cam, operation, preset, setup, units: ({"tool": None}, None))
        for name in cg._SLICES:
            out = _payload(cg.handler(include=[name]))
            assert name in out, name

    def test_no_cam_data_guard(self, monkeypatch):
        monkeypatch.setattr(cg, "get_cam", lambda: (None, "This document has no CAM (Manufacture) data."))
        res = cg.handler()
        assert "cam" in error_message(res).lower()


class TestOperationRazor:
    """Keeping operation rows terse (_common.terse + _OP_NOISE)."""

    def test_healthy_op_collapses(self):
        op = cg.terse({"name": "Face1", "tool": "T", "strategy": "face", "state": "valid",
                       "has_toolpath": True, "toolpath_valid": True, "is_generating": False,
                       "is_suppressed": False, "is_optional": False, "has_warning": False,
                       "has_error": False, "is_out_of_date": False}, cg._OP_NOISE)
        assert op == {"name": "Face1", "tool": "T", "strategy": "face", "state": "valid"}

    def test_abnormal_op_keeps_its_flags(self):
        op = cg.terse({"name": "X", "state": "invalid", "has_error": True, "is_suppressed": False,
                       "is_out_of_date": True}, cg._OP_NOISE)
        assert op["has_error"] is True and op["is_out_of_date"] is True
        assert "is_suppressed" not in op                # the boring false is dropped


class TestBounding:
    """A large CAM doc must not flood: operation rows capped + flagged; nc post_parameters summarized.
    The slices call read handlers in _cam_read, so patch the handler on that module (a plain
    monkeypatch.setattr - auto-restored, no sys.modules swap) to return the (big) ok() payload."""

    def _fake_cam_read(self, monkeypatch, **handlers):
        crd = load_tool("_cam_read")
        for name, fn in handlers.items():
            monkeypatch.setattr(crd, name, fn)

    def _ok(self, payload):
        return {"isError": False, "content": [{"type": "text", "text": json.dumps(payload)}]}

    def test_operations_capped_and_flagged(self, monkeypatch):
        big = {"setups": [{"setup": "S", "operations": [
            {"name": f"Op{i}", "state": "valid"} for i in range(cg._OPERATIONS_CAP + 50)]}]}
        self._fake_cam_read(monkeypatch,
                            get_cam_operations_handler=lambda setup="": self._ok(big))
        out, err = cg._slice_operations(object(), "")
        assert err is None
        assert len(out["setups"][0]["operations"]) == cg._OPERATIONS_CAP and out["truncated"] is True

    def test_terse_rows_keep_the_spindle_and_preset_signals_that_matter(self, monkeypatch):
        # _OP_NOISE decides which of the new per-op keys survive into the wire rows. The whole
        # point of the spindle check is that an OVER-limit op stands out, so the noise default has
        # to be the quiet answer (false) - a default of true would delete exactly the rows a
        # machinist is looking for.
        rows = [
            {"name": "Over", "state": "valid", "has_toolpath": True, "toolpath_valid": True,
             "is_suppressed": False, "has_error": False, "preset": "Aluminum - Adaptive",
             "spindle_over_machine_max": True, "spindle_rpm": 24999.0,
             "machine_max_rpm": 12000.0},
            {"name": "Under", "state": "valid", "has_toolpath": True, "toolpath_valid": True,
             "is_suppressed": False, "has_error": False, "preset": None,
             "spindle_over_machine_max": False},
            {"name": "Unchecked", "state": "valid", "has_toolpath": True, "toolpath_valid": True,
             "is_suppressed": False, "has_error": False, "preset": None,
             "spindle_over_machine_max": None, "spindle_check": "machine_max_unavailable"},
        ]
        self._fake_cam_read(monkeypatch, get_cam_operations_handler=lambda setup="": self._ok(
            {"setups": [{"setup": "Op1", "machine_spindle_max_rpm": 12000.0, "operations": rows}]}))
        out, err = cg._slice_operations(object(), "")
        assert err is None
        by_name = {r["name"]: r for r in out["setups"][0]["operations"]}
        # the over-limit row keeps the flag AND both numbers
        assert by_name["Over"]["spindle_over_machine_max"] is True
        assert by_name["Over"]["spindle_rpm"] == 24999.0
        assert by_name["Over"]["machine_max_rpm"] == 12000.0
        assert by_name["Over"]["preset"] == "Aluminum - Adaptive"
        # the at-or-under row drops the boring answer, and a null preset drops too
        assert "spindle_over_machine_max" not in by_name["Under"]
        assert "preset" not in by_name["Under"]
        # the unchecked row keeps null + the marker - never collapsed into "fine"
        assert by_name["Unchecked"]["spindle_over_machine_max"] is None
        assert by_name["Unchecked"]["spindle_check"] == "machine_max_unavailable"
        # and the setup's own maximum rides along, so the comparison is checkable
        assert out["setups"][0]["machine_spindle_max_rpm"] == 12000.0

    def test_nc_programs_summarizes_post_parameters(self, monkeypatch):
        ncp = {"nc_programs": [{"name": "Op1", "machine": "M",
                                "post_parameters": [{"name": f"p{i}"} for i in range(65)]}]}
        self._fake_cam_read(monkeypatch, get_nc_programs_handler=lambda: self._ok(ncp))
        out, err = cg._slice_nc_programs(object())
        prog = out["nc_programs"][0]
        assert prog["post_parameter_count"] == 65 and "post_parameters" not in prog


class TestLibrarySlice:
    """include=['library'] = a tool-library catalog (the tools you can ADD), delegated to
    cam_edit_tools.read_library (the READ half of that tool); add/remove/edit stay on cam_edit_tools."""

    def test_router_includes_library_and_passes_scope(self, monkeypatch, stub_slices):
        # the router must route include=['library'] AND forward scope/library/tool_type to the slice.
        seen = {}
        monkeypatch.setattr(cg, "_slice_library",
                            lambda cam, scope, library, tool_type: (
                                seen.update(scope=scope, library=library, tool_type=tool_type)
                                or ({"tool_count": 1, "tools": [{"index": 0}]}, None)))
        out = _payload(cg.handler(include=["library"], scope="cloud", library="Shop", tool_type="ball"))
        assert out["library"]["tool_count"] == 1
        assert seen == {"scope": "cloud", "library": "Shop", "tool_type": "ball"}

    def test_the_scope_enum_matches_the_scopes_read_library_accepts(self):
        # The enum is spelled here and the vocabulary lives in cam_edit_tools, whose read_library
        # this slice calls - a scope missing from the enum is unreachable through cam_get, and one
        # that is not in the vocabulary is refused after the schema let it through.
        ctl = load_tool("cam_edit_tools")
        enum = cg.tool.input_schema["properties"]["scope"]["enum"]
        assert tuple(enum) == ctl._SCOPES

    def test_slice_delegates_to_read_library(self, monkeypatch):
        # _slice_library unwraps cam_edit_tools.read_library's ok() payload (one read implementation).
        ctl = load_tool("cam_edit_tools")
        monkeypatch.setattr(ctl, "read_library",
                            lambda scope, library, tool_type: {
                                "isError": False,
                                "content": [{"type": "text", "text": json.dumps(
                                    {"tool_count": 2, "tools": [{"index": 0}, {"index": 1}]})}]})
        out, err = cg._slice_library(object(), "document", "", "")
        assert err is None and out["tool_count"] == 2

    def test_router_includes_library_types(self, monkeypatch, stub_slices):
        monkeypatch.setattr(cg, "_slice_library_types",
                            lambda cam: ({"type_count": 2, "types": ["ball end mill", "drill"]}, None))
        out = _payload(cg.handler(include=["library_types"]))
        assert out["library_types"]["types"] == ["ball end mill", "drill"]
        assert out["library_types"]["type_count"] == 2

    def test_library_types_slice_runs_cam_edit_tools_list_types(self, monkeypatch):
        # _slice_library_types runs cam_edit_tools' _do_list_types (imported, not copied): the REAL
        # sorted-type payload comes through, driven by that module's own _build_type_map seam.
        ctl = load_tool("cam_edit_tools")
        monkeypatch.setattr(ctl, "_build_type_map",
                            lambda: {"drill": ("u", 0), "ball end mill": ("u", 1),
                                     "chamfer mill": ("u", 2)})
        out, err = cg._slice_library_types(object())
        assert err is None
        assert out["types"] == ["ball end mill", "chamfer mill", "drill"]   # sorted, actual content
        assert out["type_count"] == 3

    def test_library_types_empty_map_surfaces_error(self, monkeypatch):
        ctl = load_tool("cam_edit_tools")
        monkeypatch.setattr(ctl, "_build_type_map", lambda: {})
        out, err = cg._slice_library_types(object())
        assert out is None and err["isError"] is True

    def test_router_includes_machines_and_passes_filters(self, monkeypatch, stub_slices):
        # include=['machines'] must route AND forward the vendor + machine_type filters to the slice.
        seen = {}
        monkeypatch.setattr(cg, "_slice_machines",
                            lambda cam, vendor, machine_type: (
                                seen.update(vendor=vendor, machine_type=machine_type)
                                or ({"count": 2, "machines": []}, None)))
        out = _payload(cg.handler(include=["machines"], vendor="Haas", machine_type="milling"))
        assert out["machines"]["count"] == 2
        assert seen == {"vendor": "Haas", "machine_type": "milling"}

    def test_machines_slice_delegates_to_read_machines(self, monkeypatch):
        # _slice_machines unwraps cam_edit_setup.read_machines' ok() payload (the read lives with the
        # machine resolver, its owner; cam_get is the wire surface).
        ces = load_tool("cam_edit_setup")
        seen = {}
        monkeypatch.setattr(ces, "read_machines",
                            lambda vendor, machine_type: (
                                seen.update(vendor=vendor, machine_type=machine_type)
                                or {"isError": False,
                                    "content": [{"type": "text", "text": json.dumps(
                                        {"count": 1, "machines": [{"name": "Haas VF-2"}]})}]}))
        out, err = cg._slice_machines(object(), "Haas", "milling")
        assert err is None and out["count"] == 1
        assert seen == {"vendor": "Haas", "machine_type": "milling"}

    def test_templates_slice_delegates_with_location(self, monkeypatch):
        # _slice_templates forwards location/url/depth to _cam_templates' list engine and unwraps it.
        seen = {}
        ct = load_tool("_cam_templates")
        monkeypatch.setattr(ct, "list_cam_templates_handler",
                            lambda location, url, max_depth: (
                                seen.update(location=location, url=url, max_depth=max_depth)
                                or {"isError": False, "content": [{"type": "text", "text": json.dumps(
                                    {"node_count": 3, "tree": {"folder": "Root"}})}]}))
        out, err = cg._slice_templates(object(), "local", "", 2)
        assert err is None and out["node_count"] == 3
        assert seen == {"location": "local", "url": "", "max_depth": 2}

    def test_templates_slice_defaults_location_and_depth(self, monkeypatch):
        # empty location/depth default to cloud / 4 (the engine's contract).
        seen = {}
        ct = load_tool("_cam_templates")
        monkeypatch.setattr(ct, "list_cam_templates_handler",
                            lambda location, url, max_depth: (
                                seen.update(location=location, max_depth=max_depth)
                                or {"isError": False, "content": [{"type": "text", "text": "{}"}]}))
        cg._slice_templates(object(), "", "", 0)
        assert seen == {"location": "cloud", "max_depth": 4}


class TestDeepZoom:
    """One operation's detail: include=parameters/tool need 'operation'; params group by section; preset drills."""

    def test_parameters_requires_operation(self, stub_slices):
        res = cg.handler(include=["parameters"])           # no operation=
        assert "operation" in error_message(res).lower()

    def test_tool_requires_operation(self, stub_slices):
        res = cg.handler(include=["tool"])
        assert "operation" in error_message(res).lower()

    def test_grouped_visible_params_sections_and_filters(self):
        # group sentinels open sections; invisible/disabled are dropped.
        coll = FakeCAMParameters([
            FakeCAMParameter("group_feedspeed", title="Feed & Speed", value=True),
            FakeCAMParameter("tool_feedCutting", "5252.1", title="Cutting Feedrate"),
            FakeCAMParameter("tool_spindleSpeed", "12000", title="Spindle Speed"),
            FakeCAMParameter("hidden", "x", title="Hidden", visible=False),   # dropped
            FakeCAMParameter("group_geometry", title="Geometry", value=True),
            FakeCAMParameter("boundaryOffset", "50mm", title="Additional Offset"),
        ])
        g, hidden = cg._grouped_visible_params(coll)
        assert hidden == 1                                   # the invisible row, counted not listed
        assert g["Feed & Speed"] == [
            {"name": "tool_feedCutting", "title": "Cutting Feedrate", "expression": "5252.1"},
            {"name": "tool_spindleSpeed", "title": "Spindle Speed", "expression": "12000"}]
        assert g["Geometry"][0]["name"] == "boundaryOffset"
        assert "hidden" not in str(g)                        # invisible param dropped


def _refuses_with(paths, refusal="the shared resolver's refusal"):
    """A resolve_operation stub: no node, the resolver's refusal, and the available list. Both come
    off ONE walk in the real helper, which is what TestOnePoolForResolveAndRemedy pins."""
    return lambda cam, name, label="operation": (None, refusal, paths)


class TestDuplicateOperationName:
    """_cam_common.resolve_operation REFUSES a duplicated name, returning each duplicate's
    'Setup / op' path as the available list - the deep-zoom miss error must word that as ambiguity
    naming the paths, never a plain not-found."""

    def test_parameters_duplicate_words_ambiguity_with_paths(self, monkeypatch, stub_slices):
        monkeypatch.setattr(cg, "resolve_operation",
                            _refuses_with(["Setup1 / Drill1", "Setup2 / Drill1"]))
        msg = error_message(cg.handler(include=["parameters"], operation="Drill1"))
        assert "ambiguous" in msg.lower()
        assert "Setup1 / Drill1" in msg and "Setup2 / Drill1" in msg

    def test_tool_duplicate_words_ambiguity_with_paths(self, monkeypatch, stub_slices):
        monkeypatch.setattr(cg, "resolve_operation",
                            _refuses_with(["Setup1 / Drill1", "Setup2 / Drill1"]))
        msg = error_message(cg.handler(include=["tool"], operation="Drill1"))
        assert "ambiguous" in msg.lower() and "Setup2 / Drill1" in msg

    def test_true_miss_stays_not_found_listing_names(self, monkeypatch, stub_slices):
        # a name NO operation carries is worded by the shared resolver over the real tree, so the
        # available list is the walk's own - not a second census this tool keeps.
        cam = make_cam(FakeSetup("Setup1", ops=[_named_op("Face1"), _named_op("Adaptive1")]))
        monkeypatch.setattr(cg, "get_cam", lambda: (cam, None))
        msg = error_message(cg.handler(include=["parameters"], operation="Drill1"))
        assert "ambiguous" not in msg.lower()
        assert "Face1" in msg and "Adaptive1" in msg

    def test_the_refusal_offers_the_setup_values_this_tool_actually_accepts(self, monkeypatch,
                                                                            stub_slices):
        # the refusal must name a remedy in THIS tool's own input vocabulary - "rename the target"
        # is not one, and sharing an operation name across setups is normal shop practice.
        monkeypatch.setattr(cg, "resolve_operation",
                            _refuses_with(["Setup1 / Drill1", "Setup2 / Drill1"]))
        msg = error_message(cg.handler(include=["parameters"], operation="Drill1"))
        assert "setup='Setup1'" in msg and "setup='Setup2'" in msg
        assert "Rename" not in msg

    def test_a_duplicate_inside_one_folder_path_still_offers_the_owning_setup(self, monkeypatch,
                                                                              stub_slices):
        # the breadcrumb can be several segments deep; the SETUP is the first one, never the last
        monkeypatch.setattr(cg, "resolve_operation",
                            _refuses_with(["Top / Roughing / Drill1", "Bottom / Roughing / Drill1"]))
        msg = error_message(cg.handler(include=["parameters"], operation="Drill1"))
        assert "setup='Top'" in msg and "setup='Bottom'" in msg
        assert "setup='Roughing'" not in msg          # the folder is not a setup value

    def test_two_duplicates_in_ONE_setup_offer_the_ordinal_address_not_a_rename(self, monkeypatch,
                                                                                stub_slices):
        # scoping cannot separate two ops of one name inside a SINGLE setup - setup='Setup1' would
        # refuse all over again - so that value is never printed. What IS performable is the shared
        # resolver's '<name>#<n>' address, which this same 'operation' input reads back.
        # The reachable shape: one operation per FOLDER, since names collide across parents.
        cam = make_cam(FakeSetup("Setup1", folders=[
            FakeCAMFolder("Roughing", ops=[_named_op("Drill1")]),
            FakeCAMFolder("Finishing", ops=[_named_op("Drill1")])]))
        monkeypatch.setattr(cg, "get_cam", lambda: (cam, None))
        msg = error_message(cg.handler(include=["parameters"], operation="Drill1"))
        assert "ambiguous" in msg.lower()
        assert "setup=" not in msg
        assert "Rename" not in msg
        assert "Drill1#1" in msg and "Drill1#2" in msg

    def test_a_duplicated_name_is_listed_capped_not_in_full(self, monkeypatch, stub_slices):
        # every candidate rides in an error string, so the list is capped and the remainder COUNTED;
        # an uncapped join reads as the complete set to a caller picking its next call out of it.
        paths = [f"Setup{i} / Drill1" for i in range(1, 13)]
        monkeypatch.setattr(cg, "resolve_operation", _refuses_with(paths))
        msg = error_message(cg.handler(include=["parameters"], operation="Drill1"))
        assert "12 operations share that name" in msg
        assert "(+4 more not listed)" in msg
        assert "Setup12 / Drill1" not in msg

    def test_only_the_setups_holding_ONE_duplicate_are_offered(self, monkeypatch, stub_slices):
        # the mixed case: 'Bottom' holds two of the three, so scoping to it refuses again and it is
        # dropped; 'Top' holds exactly one, so it is the value that resolves and the only one named.
        monkeypatch.setattr(cg, "resolve_operation",
                            _refuses_with(["Top / Drill1", "Bottom / Drill1", "Bottom / Drill1"]))
        msg = error_message(cg.handler(include=["parameters"], operation="Drill1"))
        assert "setup='Top'" in msg
        assert "setup='Bottom'" not in msg

    def test_a_breadcrumb_with_no_setup_segment_hands_back_the_shared_refusal(self):
        # walk_cam_tree builds every operation path setup-first, so a separator-less breadcrumb
        # names no setup to scope by - and the helper then returns the resolver's own refusal
        # VERBATIM rather than wording a second one, which could only offer a rename.
        res = cg._op_miss_error("Drill1", ["Drill1", "Drill1"], "the shared resolver's refusal")
        assert res["isError"] is True
        assert res["message"] == "the shared resolver's refusal"

    def test_a_SINGLE_bare_name_is_not_offered_as_a_setup_value(self):
        # resolve_operation returns the plain NAME list on a miss, and a two-readings miss can leave
        # exactly ONE bare name whose leaf matches: a tree holding two operations named 'Face' plus
        # one literally named 'Face#2' answers names = ['Face', 'Face#2', 'Face'] for 'Face#2'. A
        # bare name's first segment is the OPERATION, not a setup, so counting it as a scope would
        # print setup='Face#2' - a setup the document does not hold. One bare name also slips the
        # "appears exactly once" filter, so the breadcrumb guard is the only thing refusing it.
        refusal = ("'Face#2' reads two ways here: the operation CARRYING that name, and an ordinal "
                   "address over the same-named set. Both exist, so it identifies neither.")
        res = cg._op_miss_error("Face#2", ["Face", "Face#2", "Face"], refusal)
        assert res["isError"] is True
        assert res["message"] == refusal
        assert "setup=" not in res["message"]


class TestOnePoolForResolveAndRemedy:
    """The refusal and the setup= remedy that accompanies it come back from ONE resolve call, off
    one operation pool. Two separate lookups agree only by construction, and nothing asserted it."""

    def _cam(self, monkeypatch):
        cam = make_cam(FakeSetup("Top", ops=[_named_op("Drill1"), _named_op("TopOnly")]),
                       FakeSetup("Bottom", ops=[_named_op("Drill1")]))
        monkeypatch.setattr(cg, "get_cam", lambda: (cam, None))
        return cam

    def test_the_refusal_and_its_setup_values_come_from_one_resolve(self, monkeypatch,
                                                                     stub_slices):
        # end to end through the REAL resolver - no stub stands between the two, so a divergence
        # would have to be built into the shared helper rather than hidden by the fake.
        self._cam(monkeypatch)
        msg = error_message(cg.handler(include=["parameters"], operation="Drill1"))
        assert "Top / Drill1" in msg and "Bottom / Drill1" in msg
        assert "setup='Top'" in msg and "setup='Bottom'" in msg

    def test_the_setups_named_are_only_those_the_resolve_refused_over(self, monkeypatch,
                                                                       stub_slices):
        # 'TopOnly' is in the tree but not in the refusal's candidate set, so its setup may not be
        # offered a second time - the failure mode a separately-built remedy pool has.
        self._cam(monkeypatch)
        msg = error_message(cg.handler(include=["parameters"], operation="Drill1"))
        assert "TopOnly" not in msg

    def test_cam_get_holds_exactly_one_operation_resolving_seam(self):
        # the structural guard: a second by-name operation lookup imported here is a second pool.
        assert hasattr(cg, "resolve_operation")
        assert not hasattr(cg, "find_operation")


class TestOperationScopedBySetup:
    """CAM-7: an operation name is unique only WITHIN a setup (a template routinely carries one
    'Op1' per setup), so 'setup' must scope the deep per-operation reads - not just filter the
    listing. The fixture is two setups each holding a 'Shared' operation with DIFFERENT parameters,
    so a resolver that ignored the scope, or took the first hit, reads the wrong op's values."""

    def _cam(self, monkeypatch):

        def _op(name, feed):
            return type("O", (), {
                "name": name, "strategy": "adaptive",
                "parameters": _SetupParams([FakeCAMParameter("tool_feedCutting", feed, title="Feed")]),
                "tool": type("T", (), {"description": f"{feed} cutter",
                                       "presets": _NamedCollection([])})(),
                "toolPreset": None})()

        top = FakeSetup("Top", ops=[_op("Shared", "3000"), _op("TopOnly", "10")])
        bottom = FakeSetup("Bottom", ops=[_op("Shared", "800")])
        cam = make_cam(top, bottom)
        monkeypatch.setattr(cg, "get_cam", lambda: (cam, None))
        return cam

    def test_unscoped_a_shared_name_is_refused_never_first_matched(self, monkeypatch):
        cam = self._cam(monkeypatch)
        out, err = cg._slice_parameters(cam, "Shared", "")
        assert out is None
        assert "ambiguous" in err["message"].lower()

    def test_the_refusals_own_advice_resolves_the_read_it_refused(self, monkeypatch):
        # THE ROUND TRIP: take the setup= value the refusal printed and pass it straight back.
        cam = self._cam(monkeypatch)
        _out, err = cg._slice_parameters(cam, "Shared", "")
        assert "setup='Bottom'" in err["message"]
        out, err2 = cg._slice_parameters(cam, "Shared", "Bottom")
        assert err2 is None
        assert out["sections"]["General"][0]["expression"] == "800"   # Bottom's op, not Top's

    def test_each_setup_scopes_to_ITS_operation_of_the_shared_name(self, monkeypatch):
        cam = self._cam(monkeypatch)
        top, _e1 = cg._slice_parameters(cam, "Shared", "Top")
        bottom, _e2 = cg._slice_parameters(cam, "Shared", "Bottom")
        assert top["sections"]["General"][0]["expression"] == "3000"
        assert bottom["sections"]["General"][0]["expression"] == "800"

    def test_the_scope_is_case_insensitive_like_every_other_cam_resolve(self, monkeypatch):
        cam = self._cam(monkeypatch)
        out, err = cg._slice_parameters(cam, "shared", "bottom")
        assert err is None and out["operation"] == "Shared"

    def test_the_tool_slice_takes_the_same_scoping(self, monkeypatch):
        # both deep per-operation slices resolve through the one helper, so the setup= the
        # parameters refusal advertises works for include=['tool'] too
        cam = self._cam(monkeypatch)
        out, err = cg._slice_tool(cam, "Shared", "", "Bottom")
        assert err is None and out["tool"] == "800 cutter"

    def test_the_router_passes_setup_through_to_the_tool_slice(self, monkeypatch, stub_slices):
        seen = {}
        monkeypatch.setattr(cg, "_slice_tool",
                            lambda cam, operation, preset, setup="", units="mm": (
                                seen.update(operation=operation, setup=setup, units=units)
                                or ({}, None)))
        cg.handler(include=["tool"], operation="Shared", setup="Bottom", units="in")
        assert seen == {"operation": "Shared", "setup": "Bottom", "units": "in"}

    def test_a_miss_inside_the_scope_lists_THAT_setups_operations(self, monkeypatch):
        # a scoped miss that listed the whole document's operations would offer names this call
        # just excluded - 'TopOnly' is not reachable under setup='Bottom'
        cam = self._cam(monkeypatch)
        out, err = cg._slice_parameters(cam, "TopOnly", "Bottom")
        assert out is None
        msg = err["message"]
        assert "in setup 'Bottom'" in msg and "Shared" in msg
        assert "TopOnly" not in msg.split("Available:")[-1]

    def test_an_unknown_setup_is_refused_by_the_setup_resolver(self, monkeypatch):
        cam = self._cam(monkeypatch)
        out, err = cg._slice_parameters(cam, "Shared", "Ghost")
        assert out is None
        assert "Ghost" in err["message"] and "Top" in err["message"]

    def test_an_unscoped_UNIQUE_name_still_resolves_with_no_setup(self, monkeypatch):
        # the scope stays optional: only a shared name needs it
        cam = self._cam(monkeypatch)
        out, err = cg._slice_parameters(cam, "TopOnly", "")
        assert err is None and out["operation"] == "TopOnly"

    def test_a_name_duplicated_INSIDE_one_setup_offers_no_scope_that_would_refuse(self,
                                                                                  monkeypatch):
        # end to end through the REAL resolvers: both duplicates live in 'Top', so no setup= value
        # can separate them. The refusal must offer none - and the scoped retry it would have
        # advertised does refuse, which is why it may not be printed.

        def _op(feed):
            return type("O", (), {
                "name": "Twin", "strategy": "adaptive",
                "parameters": _SetupParams([FakeCAMParameter("tool_feedCutting", feed, title="Feed")])})()

        cam = make_cam(FakeSetup("Top", ops=[_op("3000"), _op("800")]))
        monkeypatch.setattr(cg, "get_cam", lambda: (cam, None))
        _out, err = cg._slice_parameters(cam, "Twin", "")
        msg = err["message"]
        assert "ambiguous" in msg.lower() and "setup=" not in msg
        # and the value that was withheld would indeed have refused
        _out2, err2 = cg._slice_parameters(cam, "Twin", "Top")
        assert err2 is not None and "ambiguous" in err2["message"].lower()


class _Preset:
    """A ToolPreset - the name a drill resolves and the parameter expressions it carries.
    ToolPreset has no live SHAPES dump, so it has no shared fake to land on."""

    def __init__(self, name, exprs):
        self.name = name
        self.parameters = FakeCAMParameters(
            [FakeCAMParameter(k, v) for k, v in exprs.items()])


class TestToolSlicePresets:
    """include=['tool'] publishes the tool's preset NAMES + count, and 'preset' drills ONE preset's
    expressions. The names come off a count/item walk, so a tool holding several presets must
    report every one - and a miss must name what IS available."""

    def _wire(self, monkeypatch, presets, active=None):
        """A real one-setup CAM tree holding the operation - the resolve runs over the shared walk,
        so a stubbed resolve_operation would no longer be the seam that answers. Returns the cam."""
        tool = type("T", (), {"description": "6mm flat", "presets": _NamedCollection(presets)})()
        op = type("O", (), {"name": "Adaptive1", "tool": tool, "toolPreset": active})()
        return make_cam(FakeSetup("Setup1", ops=[op]))

    def test_the_preset_the_operation_actually_uses_is_named_with_its_id(self, monkeypatch):
        # a tool can hold 20 presets; WHICH one this op runs is what decides its feeds and speeds
        chosen = type("P", (), {"name": "Aluminum - Adaptive",
                                "id": "79273b38-74c8-454f-9645-719dfe7dfd68"})()
        cam = self._wire(monkeypatch, [_Preset("Aluminum - Adaptive", {}), _Preset("Steel", {})],
                         active=chosen)
        out, err = cg._slice_tool(cam, "Adaptive1", "")
        assert err is None
        assert out["active_preset"] == {"name": "Aluminum - Adaptive",
                                        "id": "79273b38-74c8-454f-9645-719dfe7dfd68"}

    def test_an_operation_with_no_preset_reads_null_not_the_first_one(self, monkeypatch):
        cam = self._wire(monkeypatch, [_Preset("Aluminum - Adaptive", {}), _Preset("Steel", {})])
        out, err = cg._slice_tool(cam, "Adaptive1", "")
        assert err is None and out["active_preset"] is None
        assert out["preset_names"] == ["Aluminum - Adaptive", "Steel"]   # the catalog still lists

    def test_every_preset_name_is_published_with_its_count(self, monkeypatch):
        cam = self._wire(monkeypatch, [_Preset("Alu roughing", {"tool_feedCutting": "3000 mm/min"}),
                                       _Preset("Steel finishing", {"tool_feedCutting": "800 mm/min"}),
                                       _Preset("Brass", {"tool_spindleSpeed": "14000"})])
        out, err = cg._slice_tool(cam, "Adaptive1", "")
        assert err is None
        assert out["preset_names"] == ["Alu roughing", "Steel finishing", "Brass"]
        assert out["preset_count"] == 3
        assert out["tool"] == "6mm flat"

    def test_preset_drill_returns_that_presets_expressions(self, monkeypatch):
        cam = self._wire(monkeypatch, [_Preset("Alu roughing", {"tool_feedCutting": "3000 mm/min"}),
                                       _Preset("Steel finishing", {"tool_feedCutting": "800 mm/min",
                                                                   "tool_spindleSpeed": "4500"})])
        out, err = cg._slice_tool(cam, "Adaptive1", "Steel finishing")
        assert err is None
        assert out["preset"] == {"name": "Steel finishing",
                                 "expressions": {"tool_feedCutting": "800 mm/min",
                                                 "tool_spindleSpeed": "4500"}}

    def test_preset_miss_names_the_available_presets(self, monkeypatch):
        cam = self._wire(monkeypatch, [_Preset("Alu roughing", {}), _Preset("Steel finishing", {})])
        out, err = cg._slice_tool(cam, "Adaptive1", "Titanium")
        assert out is None
        msg = err["message"]
        assert "Titanium" in msg and "Alu roughing" in msg and "Steel finishing" in msg

    def test_a_tool_with_no_presets_reports_an_empty_list_not_a_miss(self, monkeypatch):
        cam = self._wire(monkeypatch, [])
        out, err = cg._slice_tool(cam, "Adaptive1", "")
        assert err is None and out["preset_names"] == [] and out["preset_count"] == 0

    def test_a_case_differing_spelling_resolves_to_the_presets_own_name(self, monkeypatch):
        # cam_edit_tools resolves a preset case-insensitively, so it accepts 'alu rough'; a read
        # matching byte-for-byte missed the very spelling the write path had taken.
        cam = self._wire(monkeypatch, [_Preset("Alu Rough", {"tool_feedCutting": "3000 mm/min"}),
                                       _Preset("Steel", {})])
        out, err = cg._slice_tool(cam, "Adaptive1", "alu rough")
        assert err is None
        assert out["preset"] == {"name": "Alu Rough",
                                 "expressions": {"tool_feedCutting": "3000 mm/min"}}

    def test_two_presets_sharing_a_name_are_both_returned_keyed_by_index(self, monkeypatch):
        # two recipes answer to one name; publishing the first as 'preset' would present one
        # tool's feeds as though the name identified them. This is a READ - it discloses both.
        cam = self._wire(monkeypatch, [_Preset("Alu Rough", {"tool_feedCutting": "3000 mm/min"}),
                                       _Preset("Alu Rough", {"tool_feedCutting": "800 mm/min"})])
        out, err = cg._slice_tool(cam, "Adaptive1", "Alu Rough")
        assert err is None
        rows = out["presets_sharing_name"]
        assert [r["index"] for r in rows] == [0, 1]
        assert [r["expressions"]["tool_feedCutting"] for r in rows] == ["3000 mm/min", "800 mm/min"]
        assert "preset" not in out              # neither was picked as THE recipe
        assert "2 presets" in out["note"] and "preset_names" in out["note"]

    def test_a_preset_asked_of_a_tool_with_no_presets_names_none(self, monkeypatch):
        # an empty candidate list rendered as 'Available: .' - a sentence with no answer in it
        cam = self._wire(monkeypatch, [])
        out, err = cg._slice_tool(cam, "Adaptive1", "Alu Rough")
        assert out is None
        msg = error_message(err)
        assert "no presets at all" in msg and "Available: ." not in msg
        assert "add_preset" in msg

    def test_presets_whose_names_do_not_read_are_counted_not_denied(self, monkeypatch):
        # the SLOTS are there and only the names would not read. Reporting "no presets at all" and
        # pointing at add_preset would have the caller author a fourth on a tool holding three.
        cam = self._wire(monkeypatch, [_Preset(None, {}), _Preset(None, {}), _Preset(None, {})])
        out, err = cg._slice_tool(cam, "Adaptive1", "Alu Rough")
        assert out is None
        msg = error_message(err)
        assert "3 preset(s)" in msg and "none of their names read" in msg
        assert "no presets at all" not in msg and "add_preset" not in msg


class TestToolSliceDimensions:
    """include=['tool'] publishes the operation's own tool geometry off op.tool.parameters. The
    values are Fusion's internal cm, so the slice scales them into the unit it names."""

    def _wire(self, params):
        """A one-setup CAM tree whose operation carries a tool with 'params' (a _SetupParams)."""
        tool = type("T", (), {"description": "6mm flat", "presets": None, "parameters": params})()
        op = type("O", (), {"name": "Adaptive1", "tool": tool, "toolPreset": None})()
        return make_cam(FakeSetup("Setup1", ops=[op]))

    def _full(self):
        return _SetupParams([FakeCAMParameter("tool_diameter", value=0.6),
                             FakeCAMParameter("tool_fluteLength", value=2.5),
                             FakeCAMParameter("tool_cornerRadius", value=0.05),
                             FakeCAMParameter("tool_overallLength", value=5.0)])

    def test_the_four_dimensions_land_scaled_into_the_named_unit(self):
        out, err = cg._slice_tool(self._wire(self._full()), "Adaptive1", "")
        assert err is None
        assert out["dimensions"] == {"diameter": 6.0, "flute_length": 25.0, "corner_radius": 0.5,
                                     "overall_length": 50.0, "units": "mm"}

    def test_the_unit_the_caller_asked_for_scales_the_values(self):
        # a handler that reported cm regardless would pass the mm test above and fail this one
        out, err = cg._slice_tool(self._wire(self._full()), "Adaptive1", "", "", "in")
        assert err is None
        assert out["dimensions"]["diameter"] == round(0.6 / 2.54, 6)
        assert out["dimensions"]["units"] == "in"

    def test_an_absent_parameter_reads_null_not_zero(self):
        # a square-ended tool carries no corner radius; publishing 0 would read as a measured
        # sharp corner rather than "this tool does not carry the parameter".
        out, err = cg._slice_tool(
            self._wire(_SetupParams([FakeCAMParameter("tool_diameter", value=0.6)])), "Adaptive1", "")
        assert err is None
        assert out["dimensions"]["diameter"] == 6.0
        assert out["dimensions"]["corner_radius"] is None
        assert out["dimensions"]["flute_length"] is None
        assert out["dimensions"]["overall_length"] is None

    def test_a_tool_exposing_no_parameters_answers_null_rather_than_raising(self):
        out, err = cg._slice_tool(self._wire(None), "Adaptive1", "")
        assert err is None
        assert out["dimensions"] == {"diameter": None, "flute_length": None, "corner_radius": None,
                                     "overall_length": None, "units": "mm"}

    def test_the_note_says_the_dimensions_are_this_operations_own_copy(self):
        # MEASURED: an op created before a document-tool edit keeps the values it was made with, so
        # a caller reading these as the library tool's current geometry would be reading stale ones.
        out, _err = cg._slice_tool(self._wire(self._full()), "Adaptive1", "")
        assert "own copy" in out["note"] and "created before" in out["note"]

    def test_an_unknown_unit_is_refused_before_anything_is_read(self):
        out, err = cg._slice_tool(self._wire(self._full()), "Adaptive1", "", "", "parsecs")
        assert out is None and "parsecs" in err["message"]

    def test_the_shared_preset_disclosure_rides_beside_the_copy_note(self):
        # both facts describe the same payload, so the ambiguous-preset sentence rides BESIDE the
        # copy note rather than replacing it - and the composition stays inside the wire budget.
        params = self._full()
        tool = type("T", (), {"description": "6mm flat", "parameters": params,
                              "presets": _NamedCollection([_Preset("Alu Rough", {}),
                                                      _Preset("Alu Rough", {})])})()
        op = type("O", (), {"name": "Adaptive1", "tool": tool, "toolPreset": None})()
        out, err = cg._slice_tool(make_cam(FakeSetup("Setup1", ops=[op])), "Adaptive1", "Alu Rough")
        assert err is None
        assert "own copy" in out["note"] and "2 presets" in out["note"]
        assert len(out["note"]) <= 400, len(out["note"])   # test_prose_budget.NOTE_BUDGET_CHARS


class TestMachineSlice:
    """include=['machine'] = the ASSIGNED machine's own limits (spindle speed, axis travels),
    delegated to _cam_read's get_machine_limits_handler. Distinct from 'machines', the catalog of
    machines that can be assigned."""

    def test_router_includes_machine_and_passes_setup_and_units(self, monkeypatch, stub_slices):
        seen = {}
        monkeypatch.setattr(cg, "_slice_machine",
                            lambda cam, setup, units: (
                                seen.update(setup=setup, units=units)
                                or ({"setup_count": 1, "setups": [{"setup": "Op1"}]}, None)))
        out = _payload(cg.handler(include=["machine"], setup="Op1", units="in"))
        assert out["machine"]["setup_count"] == 1
        assert seen == {"setup": "Op1", "units": "in"}

    def test_machine_and_machines_are_different_slices(self, stub_slices):
        # a caller asking for the catalog must not get the limits, and vice versa
        assert "machine" in _payload(cg.handler(include=["machine"]))
        assert "machines" not in _payload(cg.handler(include=["machine"]))
        assert "machine" not in _payload(cg.handler(include=["machines"]))

    def test_slice_delegates_to_the_cam_read_handler(self, monkeypatch):
        crd = load_tool("_cam_read")
        seen = {}
        monkeypatch.setattr(crd, "get_machine_limits_handler",
                            lambda setup, units: (
                                seen.update(setup=setup, units=units)
                                or {"isError": False, "content": [{"type": "text", "text": json.dumps(
                                    {"setup_count": 1, "setups": [{"spindle": {"max_rpm": 12000.0}}]})}]}))
        out, err = cg._slice_machine(object(), "Op1", "mm")
        assert err is None and out["setups"][0]["spindle"]["max_rpm"] == 12000.0
        assert seen == {"setup": "Op1", "units": "mm"}

    def test_time_slice_forwards_units(self, monkeypatch):
        crd = load_tool("_cam_read")
        seen = {}
        monkeypatch.setattr(crd, "get_machining_time_handler",
                            lambda setup, units: (
                                seen.update(setup=setup, units=units)
                                or {"isError": False, "content": [{"type": "text",
                                                                   "text": json.dumps({"units": units})}]}))
        out, err = cg._slice_time(object(), "Op1", "in")
        assert err is None and out["units"] == "in"
        assert seen == {"setup": "Op1", "units": "in"}


class _SetupParams(FakeCAMParameters):
    """Setup.parameters: the visible rows the grouping walks, plus the computed extents that read
    isVisible false and are reachable only by name."""

    def __init__(self, visible=(), hidden=None):
        super().__init__(visible)
        self._hidden = dict(hidden or {})

    def itemByName(self, name):
        hit = super().itemByName(name)
        return self._hidden.get(name) if hit is None else hit


class _UnreadableEditableParam(FakeCAMParameter):
    """isEditable does not answer at all - the read that may not publish a verdict."""

    @property
    def isEditable(self):
        raise RuntimeError("isEditable unreadable")

    @isEditable.setter
    def isEditable(self, value):
        pass


class TestSetupParameterSlice:
    """include=['parameters'] with 'setup' and no 'operation' - the SETUP's own parameters (the
    read-back side of cam_edit_setup's writes), including the computed stock extents."""

    def _wire(self, monkeypatch, params):
        setup = type("S", (), {"name": "Op1", "parameters": params})()
        monkeypatch.setattr(cg, "find_setup", lambda cam, name: (setup, ["Op1"], None))
        return setup

    def _params(self):
        return _SetupParams(
            visible=[FakeCAMParameter("group_job", title="Job", value=True),
                     FakeCAMParameter("job_stockMode", "'solid'", title="Mode"),
                     FakeCAMParameter("job_stockInfoDimensionX", "stockXHigh - stockXLow",
                            title="Stock Width (X)")],
            # value = Fusion's internal CM, expression = the document's mm display text (measured)
            hidden={"stockXLow": FakeCAMParameter("stockXLow", expression="-176.3", visible=False,
                                        value=-17.63),
                    "stockXHigh": FakeCAMParameter("stockXHigh", expression="-62.0", visible=False,
                                         value=-6.2)})

    def test_setup_alone_reads_that_setups_parameters(self, monkeypatch):
        self._wire(monkeypatch, self._params())
        out, err = cg._slice_parameters(object(), "", "Op1")
        assert err is None and out["setup"] == "Op1"
        assert out["sections"]["Job"][0]["name"] == "job_stockMode"
        assert out["parameter_count"] == 2

    def test_the_computed_stock_extents_ride_along(self, monkeypatch):
        # they read isVisible false, so the visible grouping drops them - a stock check needs them
        self._wire(monkeypatch, self._params())
        out, _err = cg._slice_parameters(object(), "", "Op1")
        assert out["stock_extents"]["stockXLow"] == {"value": -176.3, "expression": "-176.3"}
        assert "stockYLow" not in out["stock_extents"]        # absent parameter -> absent key

    def test_the_extent_value_is_scaled_out_of_cm_and_the_expression_is_not(self, monkeypatch):
        # measured on a millimetre document: ONE parameter reports -17.63 through .value.value
        # (Fusion's internal cm) and "-176.3" through .expression (the display unit). Publishing
        # them as one unit understates the stock tenfold.
        self._wire(monkeypatch, _SetupParams(hidden={
            "stockXLow": FakeCAMParameter("stockXLow", expression="-176.3", visible=False, value=-17.63)}))
        out, _err = cg._slice_parameters(object(), "", "Op1")
        row = out["stock_extents"]["stockXLow"]
        assert row["value"] == -176.3                      # -17.63 cm -> mm
        assert row["expression"] == "-176.3"               # authored text, untouched
        assert out["stock_extents"]["units"] == "mm"

    def test_the_extent_value_follows_the_units_input(self, monkeypatch):
        self._wire(monkeypatch, _SetupParams(hidden={
            "stockXLow": FakeCAMParameter("stockXLow", expression="-176.3", visible=False, value=-17.63)}))
        out, _err = cg._slice_parameters(object(), "", "Op1", "in")
        assert out["stock_extents"]["stockXLow"]["value"] == -6.940945    # -17.63 cm in inches
        assert out["stock_extents"]["stockXLow"]["expression"] == "-176.3"
        assert out["stock_extents"]["units"] == "in"

    def test_an_expression_that_is_not_a_number_is_never_converted(self, monkeypatch):
        # a computed extent's expression can be an EXPRESSION; scaling it would be nonsense
        self._wire(monkeypatch, _SetupParams(hidden={
            "stockZLow": FakeCAMParameter("stockZLow", expression="stockZHigh - 38.1", visible=False,
                                value=-1.905)}))
        out, _err = cg._slice_parameters(object(), "", "Op1")
        assert out["stock_extents"]["stockZLow"] == {"value": -19.05,
                                                     "expression": "stockZHigh - 38.1"}

    def test_unknown_units_are_refused_by_name(self, monkeypatch):
        self._wire(monkeypatch, self._params())
        out, err = cg._slice_parameters(object(), "", "Op1", "furlongs")
        assert out is None and "furlongs" in err["message"]

    def test_the_note_tells_the_two_units_apart(self, monkeypatch):
        self._wire(monkeypatch, self._params())
        out, _err = cg._slice_parameters(object(), "", "Op1")
        assert "DIFFERENT units" in out["note"] and "display unit" in out["note"]

    def test_an_operation_still_takes_the_operation_path(self, monkeypatch):
        # with an operation named, the OPERATION is the target - 'setup' scopes WHICH operation of
        # that name is read (see TestOperationScopedBySetup), never the setup-parameters payload.
        op = type("O", (), {"name": "Adaptive1", "strategy": "adaptive",
                            "parameters": _SetupParams(
                                [FakeCAMParameter("tool_feedCutting", "3000", title="Feed")])})()
        setup = type("S", (), {"name": "Op1"})()
        monkeypatch.setattr(cg, "find_setup", lambda cam, name: (setup, ["Op1"], None))
        monkeypatch.setattr(cg, "resolve_cam_node",
                            lambda cam, name, kinds=(), setup=None, label=None:
                            (SimpleNamespace(obj=op), None))
        out, err = cg._slice_parameters(object(), "Adaptive1", "Op1")
        assert err is None and out["operation"] == "Adaptive1" and "setup" not in out

    def test_neither_target_names_both_ways_out(self, stub_slices):
        msg = error_message(cg.handler(include=["parameters"]))
        assert "operation" in msg.lower() and "setup" in msg.lower()

    def test_a_setup_miss_returns_the_resolvers_refusal(self, monkeypatch):
        monkeypatch.setattr(cg, "find_setup",
                            lambda cam, name: (None, ["Op1"], "No setup named 'Ghost'. Available: Op1."))
        out, err = cg._slice_parameters(object(), "", "Ghost")
        assert out is None and "Ghost" in err["message"] and "Op1" in err["message"]

    def test_a_setup_without_readable_parameters_says_so(self, monkeypatch):
        self._wire(monkeypatch, None)
        out, err = cg._slice_parameters(object(), "", "Op1")
        assert out is None and "no readable parameters" in err["message"]

    def test_an_extent_that_reads_nothing_is_not_published_as_a_null_row(self, monkeypatch):
        # a parameter that EXISTS but whose value and expression both fail to read says nothing
        # about the stock; a {value: null, expression: null} row would read as a measured absence.
        class _Unreadable:
            name = "stockXLow"

            @property
            def value(self):
                raise RuntimeError("value cannot be read")

            @property
            def expression(self):
                raise RuntimeError("expression cannot be read")

        self._wire(monkeypatch, _SetupParams(hidden={"stockXLow": _Unreadable(),
                                                     "stockXHigh": FakeCAMParameter("stockXHigh",
                                                                          expression="-62.0",
                                                                          visible=False,
                                                                          value=-6.2)}))
        out, err = cg._slice_parameters(object(), "", "Op1")
        assert err is None
        assert "stockXLow" not in out["stock_extents"]      # nothing read -> nothing claimed
        assert out["stock_extents"]["stockXHigh"]["value"] == -62.0

    def test_an_extent_with_only_an_expression_still_publishes(self, monkeypatch):
        # a computed extent whose numeric value does not read but whose expression does is a real
        # answer - the row carries the half that read.
        self._wire(monkeypatch, _SetupParams(hidden={
            "stockZLow": FakeCAMParameter("stockZLow", expression="stockZHigh - 38.1", visible=False)}))
        out, _err = cg._slice_parameters(object(), "", "Op1")
        assert out["stock_extents"]["stockZLow"] == {"value": None,
                                                     "expression": "stockZHigh - 38.1"}


class TestParameterEditableFlag:
    """isEditable is the flag cam_edit_operation and cam_edit_setup refuse a write on (measured:
    273 of a face op's 327 parameters and 274 of a setup's 304 read False), so the read that lists
    the parameters is where an agent learns which rows refuse - not the refusal it gets afterwards."""

    def test_a_row_that_refuses_a_write_is_marked_editable_false(self):
        g, _hidden = cg._grouped_visible_params(
            _SetupParams([FakeCAMParameter("tool_diameter", "10.", title="Diameter", editable=False)]))
        assert g["General"][0]["editable"] is False

    def test_a_writable_row_carries_no_editable_key(self):
        # the quiet default: most rows an agent reads are settable, and a key on every one of them
        # is 300 rows of noise.
        g, _hidden = cg._grouped_visible_params(_SetupParams([FakeCAMParameter("tolerance", "0.01", title="Tolerance")]))
        assert g["General"][0] == {"name": "tolerance", "title": "Tolerance",
                                   "expression": "0.01"}

    def test_an_unreadable_flag_publishes_null_and_never_false(self):
        # a flag that did not answer is not a refusal - false here would name a row the write takes.
        g, _hidden = cg._grouped_visible_params(
            _SetupParams([_UnreadableEditableParam("tolerance", "0.01", title="Tolerance")]))
        assert g["General"][0]["editable"] is None

    def test_the_operation_slice_note_says_which_rows_refuse_a_write(self, monkeypatch):
        op = type("O", (), {"name": "Adaptive1", "strategy": "adaptive", "parameters": _SetupParams(
            [FakeCAMParameter("tool_diameter", "10.", title="Diameter", editable=False)])})()
        monkeypatch.setattr(cg, "resolve_operation",
                            lambda cam, name, label="operation": (SimpleNamespace(obj=op), None, []))
        out, err = cg._slice_parameters(object(), "Adaptive1", "")
        assert err is None and out["sections"]["General"][0]["editable"] is False
        assert "editable false refuses a write" in out["note"]

    def test_the_setup_slice_note_carries_it_beside_the_units_sentence(self, monkeypatch):
        setup = type("S", (), {"name": "Op1", "parameters": _SetupParams(
            [FakeCAMParameter("surfaceZHigh", "0.0", title="Top", editable=False)])})()
        monkeypatch.setattr(cg, "find_setup", lambda cam, name: (setup, ["Op1"], None))
        out, _err = cg._slice_parameters(object(), "", "Op1")
        assert out["sections"]["General"][0]["editable"] is False
        assert "editable false refuses a write" in out["note"] and "DIFFERENT units" in out["note"]


class TestGatedRowsAreCounted:
    """MEASURED on a contour2d: a row behind a switch reads isVisible TRUE with isEnabled FALSE, so
    the visible+enabled filter drops it; the switch landing flips it to enabled+editable and the
    listing grows by it. The listed count is therefore not the operation's whole set."""

    def test_a_hidden_row_is_counted_and_the_note_says_a_switch_lands_it(self, monkeypatch):
        op = type("O", (), {"name": "Contour1", "strategy": "contour2d",
                            "parameters": _SetupParams([
                                FakeCAMParameter("useStockToLeave", "false", title="Stock to leave"),
                                FakeCAMParameter("stockToLeave", "0.1mm", title="Stock to Leave",
                                                 enabled=False, editable=False)])})()
        monkeypatch.setattr(cg, "resolve_operation",
                            lambda cam, name, label="operation": (SimpleNamespace(obj=op), None, []))
        out, err = cg._slice_parameters(object(), "Contour1", "")
        assert err is None and out["parameter_count"] == 1 and out["hidden_count"] == 1
        assert "hidden_count" in out["note"] and "SAME call" in out["note"]
        assert "isEnabled FALSE" in out["note"]

    def test_a_setup_counts_its_hidden_rows_too(self, monkeypatch):
        # MEASURED on a fresh milling setup: 304 parameters, 42 listed, 262 hidden - the 12 computed
        # extents are a small part of that, so the extents alone do not account for the silence.
        setup = type("S", (), {"name": "Setup1", "parameters": _SetupParams([
            FakeCAMParameter("job_stockMode", "'default'", title="Stock Mode"),
            FakeCAMParameter("wcs_orientation_axisZ", "0", title="Z axis", visible=False)])})()
        monkeypatch.setattr(cg, "find_setup", lambda cam, name: (setup, ["Setup1"], None))
        out, _err = cg._slice_parameters(object(), "", "Setup1")
        assert out["parameter_count"] == 1 and out["hidden_count"] == 1
        assert "stock_extents publishes" in out["note"]
        # cam_edit_setup REFUSES a locked row outright (no switch-first ordering), so the setup note
        # must not borrow cam_edit_operation's same-call promise
        assert "REFUSES a row reading editable false" in out["note"]
        assert "lands it in the SAME call" not in out["note"]

    def test_an_operation_hiding_nothing_carries_no_hidden_key_or_sentence(self, monkeypatch):
        op = type("O", (), {"name": "Face1", "strategy": "face", "parameters": _SetupParams(
            [FakeCAMParameter("tolerance", "0.01", title="Tolerance")])})()
        monkeypatch.setattr(cg, "resolve_operation",
                            lambda cam, name, label="operation": (SimpleNamespace(obj=op), None, []))
        out, _err = cg._slice_parameters(object(), "Face1", "")
        assert "hidden_count" not in out and "hidden_count" not in out["note"]


class TestParameterChoices:
    """A CHOICE parameter's own values, published on the row: without them neither this read nor the
    refusal a wrong token earns names a single legal spelling, and the caller guesses."""

    def _op(self, monkeypatch, params):
        op = type("O", (), {"name": "MA1", "strategy": "multi_axis_contour",
                            "parameters": _SetupParams(params)})()
        monkeypatch.setattr(cg, "resolve_operation",
                            lambda cam, name, label="operation": (SimpleNamespace(obj=op), None, []))

    def test_a_choice_row_publishes_the_values_it_takes(self, monkeypatch):
        self._op(monkeypatch, [FakeCAMParameter("multiAxisMachiningType", "three_axis",
                                                title="Type", value="three_axis",
                                                choices=["three_axis", "five_axis"])])
        out, err = cg._slice_parameters(object(), "MA1", "")
        assert err is None
        assert out["sections"]["General"][0]["choices"] == ["three_axis", "five_axis"]
        assert "getChoices()" in out["note"]

    def test_a_row_with_no_choice_set_carries_no_key_and_no_note(self, monkeypatch):
        # the quiet default: nearly every parameter is a number or a string, and a 'choices' key on
        # each of them - or a sentence about a key nothing carries - is noise.
        self._op(monkeypatch, [FakeCAMParameter("tolerance", "0.01", title="Tolerance")])
        out, _err = cg._slice_parameters(object(), "MA1", "")
        assert "choices" not in out["sections"]["General"][0]
        assert "getChoices()" not in out["note"]

    def test_the_setup_slice_publishes_them_too(self, monkeypatch):
        setup = type("S", (), {"name": "Turn1", "parameters": _SetupParams(
            [FakeCAMParameter("wcs_origin_turning", "'model front'", title="Origin",
                              choices=["model front", "model back"])])})()
        monkeypatch.setattr(cg, "find_setup", lambda cam, name: (setup, ["Turn1"], None))
        out, _err = cg._slice_parameters(object(), "", "Turn1")
        assert out["sections"]["General"][0]["choices"] == ["model front", "model back"]
        assert "getChoices()" in out["note"]


class TestStrategyIdAndName:
    """MEASURED: the 'strategy' PARAMETER reads the platform's internal id ('parallel_new') while
    Operation.strategy reads the create vocabulary ('parallel') - and createInput takes only the
    latter. One key each, so an agent cannot carry the id back into a create that raises on it."""

    def test_the_slice_publishes_the_id_and_the_create_name_apart(self, monkeypatch):
        op = type("O", (), {"name": "Parallel1", "strategy": "parallel", "parameters": _SetupParams(
            [FakeCAMParameter("strategy", "'parallel_new'", title="Strategy"),
             FakeCAMParameter("tolerance", "0.01", title="Tolerance")])})()
        monkeypatch.setattr(cg, "resolve_operation",
                            lambda cam, name, label="operation": (SimpleNamespace(obj=op), None, []))
        out, err = cg._slice_parameters(object(), "Parallel1", "")
        assert err is None
        assert out["strategy"] == "parallel_new"        # the parameter's own id, unquoted
        assert out["strategy_name"] == "parallel"       # what cam_create_operation takes
        assert "cam_create_operation and cam_get(include=['strategies']) take strategy_name" \
            in out["note"]

    def test_an_operation_with_no_strategy_parameter_reads_null_not_the_name(self, monkeypatch):
        # the two are separate reads: falling back to Operation.strategy here would publish the
        # create name under the id key and hide that the parameter never answered.
        op = type("O", (), {"name": "Face1", "strategy": "face",
                            "parameters": _SetupParams([FakeCAMParameter("tolerance", "0.01")])})()
        monkeypatch.setattr(cg, "resolve_operation",
                            lambda cam, name, label="operation": (SimpleNamespace(obj=op), None, []))
        out, _err = cg._slice_parameters(object(), "Face1", "")
        assert out["strategy"] is None and out["strategy_name"] == "face"


class TestInspectionSlice:
    """include=['inspection'] = the recorded probing results, delegated to _cam_read's
    get_inspection_results_handler; 'measure'/'max_results'/'units' scope and bound it."""

    def test_router_includes_inspection_and_passes_the_scope(self, monkeypatch, stub_slices):
        seen = {}
        monkeypatch.setattr(cg, "_slice_inspection",
                            lambda cam, measure, max_results, units: (
                                seen.update(measure=measure, max_results=max_results, units=units)
                                or ({"available": True, "measure_count": 1}, None)))
        out = _payload(cg.handler(include=["inspection"], measure="0/1", max_results=25,
                                  units="in"))
        assert out["inspection"]["measure_count"] == 1
        assert seen == {"measure": "0/1", "max_results": 25, "units": "in"}

    def test_slice_delegates_to_the_cam_read_handler(self, monkeypatch):
        crd = load_tool("_cam_read")
        seen = {}
        monkeypatch.setattr(crd, "get_inspection_results_handler",
                            lambda measure, max_results, units: (
                                seen.update(measure=measure, max_results=max_results, units=units)
                                or {"isError": False, "content": [{"type": "text", "text": json.dumps(
                                    {"available": False, "measure_count": 0})}]}))
        out, err = cg._slice_inspection(object(), "", 0, "mm")
        assert err is None and out["available"] is False
        assert seen == {"measure": "", "max_results": 0, "units": "mm"}

    def test_slice_error_surfaces_from_the_router(self, monkeypatch, stub_slices):
        monkeypatch.setattr(cg, "_slice_inspection",
                            lambda cam, measure, max_results, units: (
                                None, cg.error("measure index 5 is out of range")))
        assert "out of range" in error_message(cg.handler(include=["inspection"], measure="5"))


class TestNormalizeInclude:
    def test_comma_string(self):
        assert cg._normalize_include("operations, time") == ["operations", "time"]

    def test_list_lowercased(self):
        assert cg._normalize_include(["Operations", "TIME"]) == ["operations", "time"]

    def test_none_empty(self):
        assert cg._normalize_include(None) == [] and cg._normalize_include("") == []
