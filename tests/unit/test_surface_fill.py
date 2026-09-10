"""Unit tests for ``surface_fill.py`` - Boundary Fill: seal the enclosed cell(s) bounded by a set of
surface/solid tools into a solid.

Pinned: the createInput transaction is committed by add() or cancelled on EVERY refusal path (zero
cells, unreadable cells, an unpicked ambiguity, a bad index, a refused selection, a raising/null
add); the selected-cell contract (for a boundary fill a SELECTED cell is KEPT, so every cell is
written explicitly and read back); the disclosing refusal that lists each cell's index and volume,
capped, instead of guessing which one the caller meant; the operation/remove_tools passthrough and
their read-backs; and the honesty gates - the produced volume is MEASURED after add (never the
input's pre-add cell prediction), the pre-image spans the WHOLE design's solids (a fill whose
material lands in another component must not be rolled back), and a feature that lands nothing is an
error with a deleteMe rollback.

The fake add() enforces the measured feature-input contract: boundaryFillFeatures.add() with no cell
selected RAISES "3 : No cells are selected." (measured live on boundaryFillFeatures itself), so no
test can opt out of it.

Fakes are built from SimpleNamespace + conftest's shared BRepBody/_NamedCollection, so no new Fake*
class enters the tree.
"""

import types

from conftest import (BRepBody, MakeComp, MakeDesign, _NamedCollection, assert_no_active_design,
                      assert_unknown_units, error_message, install, load_tool, payload)

sf = load_tool("surface_fill")


# ── fakes: cells, the fill input, and the boundaryFillFeatures collection ───────────────────────

def _cell(volume):
    """One BRepCell: a settable isSelected plus the transient cellBody carrying the cell's volume."""
    return types.SimpleNamespace(isSelected=False, cellBody=BRepBody(name="cell", volume=volume))


def _stubborn_cell(volume):
    """A cell whose isSelected assignment silently does NOT take - the SWIG shape a bare assignment
    cannot detect (nothing raises), so only the read-back catches it."""
    cell = type("Cell", (), {"isSelected": property(lambda s: False, lambda s, v: None)})()
    cell.cellBody = BRepBody(name="cell", volume=volume)
    return cell


def _stale_cell_at(cells, index):
    """Make cells.item(index) RAISE - a stale BRepCell proxy. It is still one of the cells the
    compute found (count is unchanged), so the cells after it keep their own indices."""
    intact = cells.item

    def item(i):
        if i == index:
            raise RuntimeError("4 : An API Object refers to a deleted Object")
        return intact(i)
    cells.item = item
    return cells


def _fill_input(cells, cancel_ok=True, cells_readable=True):
    """A BoundaryFillFeatureInput: bRepCells, a settable isRemoveTools, and a cancel() that answers
    (and records) - the transaction the handler must either commit or abort. cells_readable=False
    models bRepCells itself failing to read, which is not the same as finding zero cells."""
    inp = types.SimpleNamespace(isRemoveTools=False, cancels=[])
    inp.bRepCells = _NamedCollection(list(cells)) if cells_readable else None
    inp.cancel = lambda: (inp.cancels.append(True), cancel_ok)[1]
    return inp


def _feature(name="BoundaryFill1", bodies=(), delete_ok=True):
    """A BoundaryFillFeature. It deliberately carries NO isRemoveTools/tools: live, reading either
    after add() raises "3 : Didn't roll editing feature back.", so a tool that reached for them
    would get None here and a false sense of a read-back."""
    feat = types.SimpleNamespace(name=name, bodies=_NamedCollection(list(bodies)), deletes=[])
    feat.deleteMe = lambda: (feat.deletes.append(True), delete_ok)[1]
    return feat


def _tool_body(name, comp, is_solid=False, is_valid=True):
    """A tool body that knows the component it lives in - the census key remove_tools is judged by.
    The caller puts it in comp.bRepBodies and removes it there to model consumption. isValid
    defaults to True even for a body about to be consumed, because that is what a consumed body's
    proxy reads LIVE - the fake keeps that reading so a verdict built on it cannot pass here."""
    body = BRepBody(name, is_solid=is_solid)
    body.parentComponent = comp
    body.isValid = is_valid
    return body


def _fill_features(inp, feature=None, add_raises=None, on_add=None):
    """The boundaryFillFeatures collection: createInput records (tools, operation) and hands back
    `inp` (None models the documented null return); add records what it was given, REFUSES an input
    with no cell selected the way the live API does, may raise, may mutate the model via `on_add`,
    and returns `feature`."""
    fx = types.SimpleNamespace(tools=None, operation=None, created=0, added=[])

    def _create_input(tools, operation):
        fx.created += 1
        fx.tools, fx.operation = tools, operation
        return inp

    def _add(given):
        fx.added.append(given)
        cells = getattr(given, "bRepCells", None) if given is not None else None
        if cells is not None and not any(getattr(c, "isSelected", False) for c in cells):
            raise RuntimeError("3 : No cells are selected.")
        if add_raises:
            raise RuntimeError(add_raises)
        if on_add:
            on_add()
        return feature

    fx.createInput = _create_input
    fx.add = _add
    return fx


def _wire(monkeypatch, fx, tool_bodies=None, comp_bodies=(), other_components=(), comp=None):
    """Install a component carrying `fx` (features.boundaryFillFeatures) plus any pre-existing
    bodies, optionally beside further components (the design-wide volume sample walks them all), and
    stub _TOOLS.resolve (the kind's own resolution is covered by test_inputs). Pass `comp=` when the
    test needs to hold the component itself (to add/remove bodies from its census)."""
    comp = comp if comp is not None else MakeComp(name="Comp")
    for b in comp_bodies:
        comp.bRepBodies._items.append(b if hasattr(b, "name") else BRepBody(b))
    comp.features = types.SimpleNamespace(boundaryFillFeatures=fx)
    install(sf, MakeDesign(comp=comp, all_components=[comp] + list(other_components)))
    bodies = list(tool_bodies) if tool_bodies is not None else [BRepBody("Surf1", is_solid=False)]
    monkeypatch.setattr(sf._TOOLS, "resolve", lambda raw: (bodies, None))
    return comp


# ── cell discovery and selection ───────────────────────────────────────────────────────────────

class TestCells:
    def test_zero_cells_errors_and_cancels_the_transaction(self, monkeypatch):
        inp = _fill_input([])
        fx = _fill_features(inp, feature=_feature())
        _wire(monkeypatch, fx)
        msg = error_message(sf.handler(tools=["h"]))
        assert "no cell" in msg and "do not enclose a volume" in msg
        assert inp.cancels == [True], "the open transaction must be cancelled"
        assert fx.added == [], "add() must not run when there is nothing to fill"

    def test_unreadable_cells_are_not_reported_as_zero_cells(self, monkeypatch):
        inp = _fill_input([], cells_readable=False)
        fx = _fill_features(inp, feature=_feature())
        _wire(monkeypatch, fx)
        msg = error_message(sf.handler(tools=["h"]))
        assert "could not be read" in msg
        assert "do not enclose" not in msg, "a failed READ is not evidence about the geometry"
        assert inp.cancels == [True] and fx.added == []

    def test_several_cells_without_a_pick_disclose_index_and_volume(self, monkeypatch):
        cells = [_cell(1.0), _cell(0.5)]
        inp = _fill_input(cells)
        fx = _fill_features(inp, feature=_feature())
        _wire(monkeypatch, fx)
        msg = error_message(sf.handler(tools=["h"]))
        assert "computed 2 cells" in msg
        # each cell is named by index AND by volume (1 cm3 -> 1000 mm3), so the caller can choose
        assert "[0] 1000.0 mm3" in msg and "[1] 500.0 mm3" in msg
        assert inp.cancels == [True]
        assert fx.added == []
        assert [c.isSelected for c in cells] == [False, False], \
            "a refusal must leave every cell as it found it"

    def test_the_listing_is_capped_and_says_how_many_it_dropped(self, monkeypatch):
        inp = _fill_input([_cell(float(i + 1)) for i in range(22)])
        fx = _fill_features(inp, feature=_feature())
        _wire(monkeypatch, fx)
        msg = error_message(sf.handler(tools=["h"], units="cm"))
        assert "computed 22 cells" in msg
        assert "[19] 20.0 cm3" in msg and "[20]" not in msg, "exactly 20 cells are listed"
        assert "... 2 more" in msg

    def test_single_cell_is_selected_without_being_asked(self, monkeypatch):
        cells = [_cell(2.0)]
        inp = _fill_input(cells)
        fx = _fill_features(inp, feature=_feature(bodies=[BRepBody("Body1", volume=2.0)]))
        _wire(monkeypatch, fx)
        out = payload(sf.handler(tools=["h"]))
        assert out["cells_kept"] == [0] and out["cells_total"] == 1
        assert cells[0].isSelected is True, "for a boundary fill a SELECTED cell is the KEPT cell"
        assert inp.cancels == [], "a committed transaction must not also be cancelled"

    def test_explicit_cells_select_exactly_those(self, monkeypatch):
        cells = [_cell(1.0), _cell(2.0), _cell(3.0)]
        inp = _fill_input(cells)
        fx = _fill_features(inp, feature=_feature(bodies=[BRepBody("Body1", volume=4.0)]))
        _wire(monkeypatch, fx)
        out = payload(sf.handler(tools=["h"], cells=[0, 2]))
        assert [c.isSelected for c in cells] == [True, False, True]
        assert out["cells_kept"] == [0, 2]
        # the prediction is the sum of the PICKED cells only (1 + 3 cm3 -> 4000 mm3)
        assert out["cells_volume_picked"] == 4000.0

    def test_an_unreadable_cell_holds_its_index_in_the_disclosure(self, monkeypatch):
        # the index in this listing is what 'cells' takes back. A cell that will not read keeps its
        # own number and is labelled unreadable - compacting the listing would hand the caller index
        # 1 for the cell the compute calls 2, and the fill would seal the wrong volume.
        inp = _fill_input([_cell(1.0), _cell(9.0), _cell(0.5)])
        _stale_cell_at(inp.bRepCells, 1)
        fx = _fill_features(inp, feature=_feature())
        _wire(monkeypatch, fx)
        msg = error_message(sf.handler(tools=["h"]))
        assert "computed 3 cells" in msg
        assert "[0] 1000.0 mm3" in msg and "[1] volume unreadable" in msg
        assert "[2] 500.0 mm3" in msg
        assert fx.added == [] and inp.cancels == [True]

    def test_an_unreadable_cell_is_refused_by_its_index_not_skipped(self, monkeypatch):
        # the selection walk writes every cell by index; one it cannot read means the fill would run
        # on a different set than the one asked for, so it refuses NAMING the cell rather than
        # sliding cell 2's request onto whatever sits after the gap.
        cells = [_cell(1.0), _cell(9.0), _cell(0.5)]
        inp = _fill_input(cells)
        _stale_cell_at(inp.bRepCells, 1)
        fx = _fill_features(inp, feature=_feature(bodies=[BRepBody("Body1", volume=1.5)]))
        _wire(monkeypatch, fx)
        msg = error_message(sf.handler(tools=["h"], cells=[0, 2]))
        assert "Cell 1" in msg and "could not be read back" in msg
        assert fx.added == [], "a fill must not run on a different cell set than requested"
        assert inp.cancels == [True]
        assert cells[2].isSelected is False, "the walk stopped at the gap - it did not select past it"

    def test_out_of_range_cell_index_is_refused_not_clamped(self, monkeypatch):
        cells = [_cell(1.0), _cell(2.0)]
        inp = _fill_input(cells)
        fx = _fill_features(inp, feature=_feature())
        _wire(monkeypatch, fx)
        msg = error_message(sf.handler(tools=["h"], cells=[5]))
        assert "index 5 does not exist" in msg and "0..1" in msg
        assert fx.added == [] and inp.cancels == [True]
        assert [c.isSelected for c in cells] == [False, False], "no cell may be touched on a refusal"

    def test_cell_index_equal_to_the_cell_count_is_refused(self, monkeypatch):
        # the upper bound is EXCLUSIVE: two cells are 0..1, so 2 is one past the end. An off-by-one
        # here reaches cells.item(2) after the selection loop has already started writing.
        cells = [_cell(1.0), _cell(2.0)]
        inp = _fill_input(cells)
        fx = _fill_features(inp, feature=_feature())
        _wire(monkeypatch, fx)
        msg = error_message(sf.handler(tools=["h"], cells=[2]))
        assert "index 2 does not exist" in msg and "0..1" in msg
        assert fx.added == [] and inp.cancels == [True]
        assert [c.isSelected for c in cells] == [False, False], "no cell may be touched on a refusal"

    def test_non_integer_cell_value_is_refused(self, monkeypatch):
        inp = _fill_input([_cell(1.0), _cell(2.0)])
        fx = _fill_features(inp, feature=_feature())
        _wire(monkeypatch, fx)
        msg = error_message(sf.handler(tools=["h"], cells=["outer"]))
        assert "index numbers" in msg and "outer" in msg
        assert fx.added == [] and inp.cancels == [True]

    def test_a_selection_that_does_not_take_is_refused_before_add(self, monkeypatch):
        inp = _fill_input([_stubborn_cell(1.0)])
        fx = _fill_features(inp, feature=_feature(bodies=[BRepBody("Body1", volume=1.0)]))
        _wire(monkeypatch, fx)
        msg = error_message(sf.handler(tools=["h"], cells=[0]))
        assert "did not take" in msg and "cell 0" in msg
        assert fx.added == [], "a fill must not run on a different cell set than requested"
        assert inp.cancels == [True]


# ── the createInput transaction ────────────────────────────────────────────────────────────────

class TestTransaction:
    def test_createinput_returning_none_errors_before_add(self, monkeypatch):
        fx = _fill_features(None, feature=_feature())
        _wire(monkeypatch, fx)
        msg = error_message(sf.handler(tools=["h"]))
        assert "createInput returned nothing" in msg
        assert fx.added == []

    def test_add_returning_none_errors_and_cancels(self, monkeypatch):
        inp = _fill_input([_cell(1.0)])
        fx = _fill_features(inp, feature=None)
        _wire(monkeypatch, fx)
        msg = error_message(sf.handler(tools=["h"]))
        assert "returned no feature" in msg
        assert inp.cancels == [True]

    def test_add_raising_errors_and_cancels(self, monkeypatch):
        inp = _fill_input([_cell(1.0)])
        fx = _fill_features(inp, add_raises="cells are not closed")
        _wire(monkeypatch, fx)
        msg = error_message(sf.handler(tools=["h"]))
        assert "Boundary fill failed" in msg and "cells are not closed" in msg
        assert inp.cancels == [True]

    def test_a_failed_cut_names_the_missing_target_body(self, monkeypatch):
        # live-measured: a cut whose 'tools' hold only the enclosing surface raises at add(); the
        # cells can only be partitioned against a body that is itself among the tools.
        inp = _fill_input([_cell(1.0)])
        fx = _fill_features(inp, add_raises="InternalValidationError")
        _wire(monkeypatch, fx)
        msg = error_message(sf.handler(tools=["h"], operation="cut"))
        assert "include the target body among 'tools'" in msg

    def test_the_target_body_hint_covers_intersect_too(self, monkeypatch):
        inp = _fill_input([_cell(1.0)])
        fx = _fill_features(inp, add_raises="InternalValidationError")
        _wire(monkeypatch, fx)
        msg = error_message(sf.handler(tools=["h"], operation="intersect"))
        assert "cut/join/intersect" in msg and "include the target body among 'tools'" in msg

    def test_a_failed_new_body_fill_does_not_give_the_target_body_hint(self, monkeypatch):
        inp = _fill_input([_cell(1.0)])
        fx = _fill_features(inp, add_raises="InternalValidationError")
        _wire(monkeypatch, fx)
        msg = error_message(sf.handler(tools=["h"]))
        assert "include the target body" not in msg
        assert "check that they enclose the volume you meant" in msg

    def test_a_failed_cancel_is_reported_not_swallowed(self, monkeypatch):
        inp = _fill_input([], cancel_ok=False)
        fx = _fill_features(inp, feature=_feature())
        _wire(monkeypatch, fx)
        msg = error_message(sf.handler(tools=["h"]))
        assert "could NOT be cancelled" in msg and "undo in Fusion" in msg


# ── operation / flags / tool marshalling ───────────────────────────────────────────────────────

class TestOperationAndFlags:
    def test_operation_maps_to_the_feature_operations_member(self, monkeypatch):
        import adsk.fusion
        block = BRepBody("Block", volume=50.0)
        inp = _fill_input([_cell(1.0)])
        # a cut removes material from an existing body rather than making a new one
        fx = _fill_features(inp, feature=_feature(bodies=[]),
                            on_add=lambda: setattr(block, "volume", 40.0))
        _wire(monkeypatch, fx, comp_bodies=[block])
        out = payload(sf.handler(tools=["h"], operation="cut"))
        assert fx.operation is adsk.fusion.FeatureOperations.CutFeatureOperation
        assert out["operation"] == "cut"
        # the note must describe a CUT, not the new-body sentence
        assert "cut away from the target body" in out["note"]
        assert "sealed into a new body" not in out["note"]

    def test_default_operation_is_a_new_body(self, monkeypatch):
        import adsk.fusion
        inp = _fill_input([_cell(1.0)])
        fx = _fill_features(inp, feature=_feature(bodies=[BRepBody("Body1", volume=1.0)]))
        _wire(monkeypatch, fx)
        out = payload(sf.handler(tools=["h"]))
        assert fx.operation is adsk.fusion.FeatureOperations.NewBodyFeatureOperation
        assert "sealed into a new body" in out["note"]

    def test_remove_tools_reaches_the_input(self, monkeypatch):
        inp = _fill_input([_cell(1.0)])
        fx = _fill_features(inp, feature=_feature(bodies=[BRepBody("Body1", volume=1.0)]))
        _wire(monkeypatch, fx)
        out = payload(sf.handler(tools=["h"], remove_tools=True))
        assert inp.isRemoveTools is True
        assert out["remove_tools_requested"] is True, "the key names itself as the REQUEST"

    def test_remove_tools_defaults_to_leaving_the_tools_alone(self, monkeypatch):
        inp = _fill_input([_cell(1.0)])
        fx = _fill_features(inp, feature=_feature(bodies=[BRepBody("Body1", volume=1.0)]))
        _wire(monkeypatch, fx)
        out = payload(sf.handler(tools=["h"]))
        assert inp.isRemoveTools is False and out["remove_tools_requested"] is False
        assert out["tools_consumed"] == []

    def test_a_consumed_tool_is_judged_by_the_component_body_census(self, monkeypatch):
        # live: a consumed body's proxy stays readable and cannot convict - isValid still reads True,
        # and its volume read is stale (0.0 for the measured surface tool; a consumed solid keeps
        # reporting its pre-fill 256.0). Only its absence from bRepBodies marks the consumption.
        comp = MakeComp(name="Comp")
        eaten = _tool_body("Surf1", comp)
        comp.bRepBodies._items.append(eaten)
        inp = _fill_input([_cell(1.0)])
        fx = _fill_features(inp, feature=_feature(bodies=[BRepBody("Body1", volume=1.0)]),
                            on_add=lambda: comp.bRepBodies._items.remove(eaten))
        _wire(monkeypatch, fx, tool_bodies=[eaten], comp=comp)
        out = payload(sf.handler(tools=["h"], remove_tools=True))
        assert out["tools_consumed"] == ["Surf1"] and out["tools_kept"] == []
        assert "Tool bodies consumed (gone from their component): Surf1." in out["note"]
        assert eaten.isValid is True, "the still-readable proxy must not be what the verdict rests on"

    def test_a_surviving_tool_is_reported_as_kept_not_consumed(self, monkeypatch):
        comp = MakeComp(name="Comp")
        survivor = _tool_body("Surf1", comp, is_valid=False)   # proxy says dead; the census says no
        comp.bRepBodies._items.append(survivor)
        inp = _fill_input([_cell(1.0)])
        fx = _fill_features(inp, feature=_feature(bodies=[BRepBody("Body1", volume=1.0)]))
        _wire(monkeypatch, fx, tool_bodies=[survivor], comp=comp)
        out = payload(sf.handler(tools=["h"], remove_tools=True))
        assert out["tools_consumed"] == [] and out["tools_kept"] == ["Surf1"]
        assert "still in their component: Surf1" in out["note"]

    def test_a_tool_gone_without_being_asked_for_is_flagged(self, monkeypatch):
        comp = MakeComp(name="Comp")
        gone = _tool_body("Surf1", comp)
        comp.bRepBodies._items.append(gone)
        inp = _fill_input([_cell(1.0)])
        fx = _fill_features(inp, feature=_feature(bodies=[BRepBody("Body1", volume=1.0)]),
                            on_add=lambda: comp.bRepBodies._items.remove(gone))
        _wire(monkeypatch, fx, tool_bodies=[gone], comp=comp)
        out = payload(sf.handler(tools=["h"]))
        assert out["tools_consumed"] == ["Surf1"]
        assert "remove_tools was NOT requested, yet those bodies are gone." in out["note"]

    def test_a_tool_with_no_readable_component_is_claimed_neither_way(self, monkeypatch):
        inp = _fill_input([_cell(1.0)])
        fx = _fill_features(inp, feature=_feature(bodies=[BRepBody("Body1", volume=1.0)]))
        _wire(monkeypatch, fx)      # the default tool body carries no parentComponent
        out = payload(sf.handler(tools=["h"], remove_tools=True))
        assert out["tools_consumed"] == [] and out["tools_kept"] == []
        assert out["tools_unclassified"] == 1, "a short census must be explicit, not silent"

    def test_an_unreadable_body_list_leaves_the_tool_unclassified(self, monkeypatch):
        # the census FAILING is not evidence the body is gone: calling it consumed would raise the
        # 'gone without being asked for' alarm about a body that is still in the model.
        comp = MakeComp(name="Comp")
        survivor = _tool_body("Surf1", comp)
        comp.bRepBodies._items.append(survivor)
        inp = _fill_input([_cell(1.0)])
        fx = _fill_features(inp, feature=_feature(bodies=[BRepBody("Body1", volume=1.0)]),
                            on_add=lambda: delattr(comp, "bRepBodies"))
        _wire(monkeypatch, fx, tool_bodies=[survivor], comp=comp)
        out = payload(sf.handler(tools=["h"]))
        assert out["tools_consumed"] == [] and out["tools_kept"] == []
        assert out["tools_unclassified"] == 1
        # the note names every cause it could have been, not just the one this test triggered
        assert "their name, component, or its body list could not be read" in out["note"]
        assert "yet those bodies are gone" not in out["note"], \
            "a failed read must not trigger the missing-body alarm"

    def test_a_lookup_that_raises_leaves_the_tool_unclassified(self, monkeypatch):
        # the other half of the gate: the collection reads, but the NAME LOOKUP fails. A raised
        # lookup and a lookup that answers null are opposite facts, so they must not collapse.
        comp = MakeComp(name="Comp")
        survivor = _tool_body("Surf1", comp)
        comp.bRepBodies._items.append(survivor)

        def _boom(_name):
            raise RuntimeError("3 : invalid argument name")

        inp = _fill_input([_cell(1.0)])
        fx = _fill_features(inp, feature=_feature(bodies=[BRepBody("Body1", volume=1.0)]),
                            on_add=lambda: setattr(comp.bRepBodies, "itemByName", _boom))
        _wire(monkeypatch, fx, tool_bodies=[survivor], comp=comp)
        out = payload(sf.handler(tools=["h"]))
        assert out["tools_consumed"] == [] and out["tools_unclassified"] == 1
        assert "yet those bodies are gone" not in out["note"]

    def test_a_census_that_answers_null_still_convicts(self, monkeypatch):
        # the mirror of the test above: itemByName RETURNING null is a real answer, so the tool is
        # consumed - the unreadable-collection gate must not swallow that case too.
        comp = MakeComp(name="Comp")
        eaten = _tool_body("Surf1", comp)
        comp.bRepBodies._items.append(eaten)
        inp = _fill_input([_cell(1.0)])
        fx = _fill_features(inp, feature=_feature(bodies=[BRepBody("Body1", volume=1.0)]),
                            on_add=lambda: comp.bRepBodies._items.remove(eaten))
        _wire(monkeypatch, fx, tool_bodies=[eaten], comp=comp)
        out = payload(sf.handler(tools=["h"], remove_tools=True))
        assert out["tools_consumed"] == ["Surf1"] and out["tools_unclassified"] == 0

    def test_tools_reach_createinput_as_a_counted_collection(self, monkeypatch):
        inp = _fill_input([_cell(1.0)])
        fx = _fill_features(inp, feature=_feature(bodies=[BRepBody("Body1", volume=1.0)]))
        a, b = BRepBody("SurfA", is_solid=False), BRepBody("SurfB", is_solid=False)
        _wire(monkeypatch, fx, tool_bodies=[a, b])
        payload(sf.handler(tools=["h1", "h2"]))
        # createInput takes an ObjectCollection (not a plain list) - count/item, both tools in it
        assert fx.tools.count == 2
        assert [fx.tools.item(0), fx.tools.item(1)] == [a, b]


# ── honesty ────────────────────────────────────────────────────────────────────────────────────

class TestHonesty:
    def test_landing_nothing_is_an_error_with_a_rollback(self, monkeypatch):
        inp = _fill_input([_cell(1.0)])
        feat = _feature(bodies=[])          # add() succeeded but produced no body
        fx = _fill_features(inp, feature=feat)
        _wire(monkeypatch, fx, comp_bodies=[BRepBody("Block", volume=50.0)])
        msg = error_message(sf.handler(tools=["h"]))
        assert "nothing was sealed" in msg and "rolled back" in msg
        assert feat.deletes == [True], "an empty feature must be deleted, not left in the timeline"

    def test_a_consumed_tool_is_proof_of_effect_and_blocks_the_rollback(self, monkeypatch):
        # the three blindings were measured on join + remove_tools (a combination now refused
        # outright, see TestGuards): feature.bodies came back EMPTY; the consumed SOLID's pre-add
        # proxy kept reporting its pre-fill volume, so its contribution to the delta was
        # stale-minus-stale = 0; and the product was a body never in the pre-image. remove_tools
        # still consumes on operation='new', so the census is still the only signal that can see it.
        comp = MakeComp(name="Comp")
        target = _tool_body("Block", comp, is_solid=True)
        target.volume = 256.0                       # the proxy keeps reporting this after consumption
        comp.bRepBodies._items.append(target)
        inp = _fill_input([_cell(1.0)])
        feat = _feature(bodies=[])
        fx = _fill_features(inp, feature=feat,
                            on_add=lambda: comp.bRepBodies._items.remove(target))
        _wire(monkeypatch, fx, tool_bodies=[target], comp=comp)
        out = payload(sf.handler(tools=["h"], remove_tools=True))
        assert feat.deletes == [], "a fill that consumed a tool must never be rolled back"
        assert out["tools_consumed"] == ["Block"]
        assert out["existing_volume_change"] == 0.0, "the stale proxy really does read a zero delta"

    def test_a_result_body_inheriting_a_tool_name_is_not_called_a_survivor(self, monkeypatch):
        # the inheritance was measured on join + remove_tools (now refused): the RESULT body carried
        # the consumed tool's name. The routing stays for every operation, because a name that also
        # names a body this feature PRODUCED is never proof the tool survived - and calling it
        # "still in their component" invites deleting the fill's own product.
        comp = MakeComp(name="Comp")
        tool = _tool_body("Body2", comp, is_solid=True)
        comp.bRepBodies._items.append(tool)
        product = BRepBody("Body2", volume=9.0)     # the fill's product, wearing the tool's name
        inp = _fill_input([_cell(9.0)])
        feat = _feature(bodies=[product])
        fx = _fill_features(inp, feature=feat)      # the name is still found in the census
        _wire(monkeypatch, fx, tool_bodies=[tool], comp=comp)
        out = payload(sf.handler(tools=["h"], remove_tools=True))
        assert out["tools_kept"] == [], "the fill's own product is not a surviving tool"
        assert out["tools_consumed"] == []
        assert out["tools_unclassified"] == 1
        assert "still in their component" not in out["note"], \
            "the alarm must not point the agent at the body the fill just produced"
        assert "carries the name of tool body Body2" in out["note"]

    def test_a_rollback_that_fails_is_reported(self, monkeypatch):
        inp = _fill_input([_cell(1.0)])
        feat = _feature(bodies=[], delete_ok=False)
        fx = _fill_features(inp, feature=feat)
        _wire(monkeypatch, fx, comp_bodies=[BRepBody("Block", volume=50.0)])
        msg = error_message(sf.handler(tools=["h"]))
        assert "could NOT be deleted" in msg and "design_delete_feature" in msg

    def test_material_moving_in_another_component_is_not_rolled_back(self, monkeypatch):
        # the pre-image must span the whole design: a cut whose target lives in a different
        # component reports empty feature.bodies, so a component-scoped sample would read this
        # successful fill as "changed nothing" and delete it.
        far = BRepBody("FarBlock", volume=50.0)
        other = MakeComp(name="Other", bodies=[far])
        inp = _fill_input([_cell(1.0)])
        feat = _feature(bodies=[])
        fx = _fill_features(inp, feature=feat, on_add=lambda: setattr(far, "volume", 45.0))
        _wire(monkeypatch, fx, other_components=[other])
        out = payload(sf.handler(tools=["h"], operation="cut", units="cm"))
        assert feat.deletes == [], "a fill that moved real volume must not be rolled back"
        assert out["existing_volume_change"] == -5.0

    def test_a_cut_that_moves_a_local_body_is_not_rolled_back(self, monkeypatch):
        block = BRepBody("Block", volume=50.0)
        inp = _fill_input([_cell(1.0)])
        feat = _feature(bodies=[])
        fx = _fill_features(inp, feature=feat, on_add=lambda: setattr(block, "volume", 45.0))
        _wire(monkeypatch, fx, comp_bodies=[block])
        out = payload(sf.handler(tools=["h"], operation="cut", units="cm"))
        assert out["existing_volume_change"] == -5.0
        assert feat.deletes == []

    def test_result_volume_is_measured_after_add_not_predicted_from_the_cells(self, monkeypatch):
        # the cell predicted 1 cm3; what actually landed measures 2 cm3. A payload that echoed the
        # input's prediction as the effect would report 1000 mm3 and flag nothing.
        inp = _fill_input([_cell(1.0)])
        fx = _fill_features(inp, feature=_feature(bodies=[BRepBody("Body1", volume=2.0)]))
        _wire(monkeypatch, fx)
        out = payload(sf.handler(tools=["h"]))
        assert out["cells_volume_picked"] == 1000.0
        assert out["result_volume"] == 2000.0
        assert "measure 2000.0 mm3, not the 1000.0 mm3" in out["note"]

    def test_a_matching_result_volume_raises_no_divergence_note(self, monkeypatch):
        inp = _fill_input([_cell(1.5)])
        fx = _fill_features(inp, feature=_feature(bodies=[BRepBody("Body1", volume=1.5)]))
        _wire(monkeypatch, fx)
        out = payload(sf.handler(tools=["h"]))
        assert out["result_volume"] == out["cells_volume_picked"] == 1500.0
        assert "not the" not in out["note"]

    def test_a_mixed_result_names_the_body_that_is_not_solid(self, monkeypatch):
        inp = _fill_input([_cell(1.0)])
        good = BRepBody("Solid1", volume=1.0, is_solid=True)
        bad = BRepBody("Sheet1", volume=0.0, is_solid=False)
        fx = _fill_features(inp, feature=_feature(bodies=[good, bad]))
        _wire(monkeypatch, fx)
        out = payload(sf.handler(tools=["h"]))
        assert out["all_solid"] is False, "one solid body among several is not 'all solid'"
        assert out["result_bodies"] == [{"name": "Solid1", "is_solid": True},
                                        {"name": "Sheet1", "is_solid": False}]
        assert "PARTIAL: Sheet1 reads isSolid=false" in out["note"]

    def test_happy_path_reports_the_cells_bodies_and_volume(self, monkeypatch):
        inp = _fill_input([_cell(1.5)])
        fx = _fill_features(inp, feature=_feature(bodies=[BRepBody("Body1", volume=1.5)]))
        _wire(monkeypatch, fx)
        out = payload(sf.handler(tools=["h"]))
        assert out["filled"] is True
        assert out["feature"] == "BoundaryFill1"
        assert out["result_bodies"] == [{"name": "Body1", "is_solid": True}]
        assert out["all_solid"] is True
        assert out["cell_volumes"] == [1500.0] and out["result_volume"] == 1500.0
        assert out["units"] == "mm"
        assert out["existing_volume_change"] is None, "no pre-existing solid to compare against"

    def test_units_scale_the_reported_volumes(self, monkeypatch):
        inp = _fill_input([_cell(1.5)])
        fx = _fill_features(inp, feature=_feature(bodies=[BRepBody("Body1", volume=1.5)]))
        _wire(monkeypatch, fx)
        out = payload(sf.handler(tools=["h"], units="cm"))
        assert out["cell_volumes"] == [1.5] and out["result_volume"] == 1.5


# ── guards ─────────────────────────────────────────────────────────────────────────────────────

class TestGuards:
    def test_no_active_design(self, monkeypatch):
        _wire(monkeypatch, _fill_features(_fill_input([_cell(1.0)])))
        assert_no_active_design(sf, sf.handler, tools=["h"])

    def test_bad_units(self, monkeypatch):
        inp = _fill_input([_cell(1.0)])
        fx = _fill_features(inp, feature=_feature(bodies=[BRepBody("Body1", volume=1.0)]))
        _wire(monkeypatch, fx)
        assert_unknown_units(sf.handler, tools=["h"])
        assert fx.created == 0, "a bad unit must be refused before the transaction is opened"

    def test_unknown_operation_is_refused(self, monkeypatch):
        fx = _fill_features(_fill_input([_cell(1.0)]))
        _wire(monkeypatch, fx)
        msg = error_message(sf.handler(tools=["h"], operation="weld"))
        assert "operation" in msg and "weld" in msg
        assert fx.created == 0

    def test_remove_tools_is_refused_for_a_join(self, monkeypatch):
        # measured: join + remove_tools + the target among 'tools' (which join requires) left a
        # HEALTHY feature and a design with ZERO bodies - the merge landed in the target tool body
        # and remove_tools then ate the merged result along with it.
        fx = _fill_features(_fill_input([_cell(1.0)]))
        _wire(monkeypatch, fx)
        msg = error_message(sf.handler(tools=["h"], operation="join", remove_tools=True))
        assert "remove_tools=true is not supported with operation='join'" in msg
        assert "EMPTY model" in msg and "operation='new'" in msg
        assert fx.created == 0, "the refusal must land before the transaction is opened"

    def test_remove_tools_is_refused_for_cut_and_intersect(self, monkeypatch):
        for op in ("cut", "intersect"):
            fx = _fill_features(_fill_input([_cell(1.0)]))
            _wire(monkeypatch, fx)
            msg = error_message(sf.handler(tools=["h"], operation=op, remove_tools=True))
            assert f"not supported with operation='{op}'" in msg
            assert fx.created == 0

    def test_remove_tools_is_still_allowed_for_a_new_body_fill(self, monkeypatch):
        inp = _fill_input([_cell(1.0)])
        fx = _fill_features(inp, feature=_feature(bodies=[BRepBody("Body1", volume=1.0)]))
        _wire(monkeypatch, fx)
        out = payload(sf.handler(tools=["h"], operation="new", remove_tools=True))
        assert out["remove_tools_requested"] is True and inp.isRemoveTools is True

    def test_the_cell_listing_warns_that_indices_are_per_call(self, monkeypatch):
        # measured: the same tools enumerated their cells in a different order on a later compute,
        # so a cached index seals the wrong cell.
        inp = _fill_input([_cell(1.0), _cell(0.5)])
        fx = _fill_features(inp, feature=_feature())
        _wire(monkeypatch, fx)
        msg = error_message(sf.handler(tools=["h"]))
        assert "valid for the NEXT call only" in msg
        assert "pick by the volume listed here" in msg

    def test_tools_resolution_error_propagates(self, monkeypatch):
        fx = _fill_features(_fill_input([_cell(1.0)]))
        _wire(monkeypatch, fx)
        monkeypatch.setattr(sf._TOOLS, "resolve",
                            lambda raw: (None, "'tools'[0] must be a SOLID or SURFACE (BRep, "
                                               "non-mesh) body, but that handle points at a MESH body."))
        msg = error_message(sf.handler(tools=["mesh"]))
        assert "MESH body" in msg
        assert fx.created == 0


# ── declared output contract ───────────────────────────────────────────────────────────────────

class TestOutputContract:
    def test_declared_outputs_are_minted(self, monkeypatch):
        inp = _fill_input([_cell(1.0)])
        fx = _fill_features(inp, feature=_feature(bodies=[BRepBody("Body1", volume=1.0)]))
        _wire(monkeypatch, fx)
        out = payload(sf.handler(tools=["h"]))
        for o in sf.RETURNS:
            assert o.assert_present(out) == "", o.key
