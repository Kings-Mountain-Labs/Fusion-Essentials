"""Unit tests for surface_trim.py - the cell selection, the phantom-cell gate and the abort."""

import types

import adsk.core
import adsk.fusion
import pytest

from conftest import (BRepBody, BRepEdge, BRepFace, FakeFeature as _SharedFeature, MakeComp,
                      install, load_tool, make_design, payload)

se = load_tool("surface_trim")
surface_common_mod = load_tool("_surface_common")


@pytest.fixture(autouse=True)
def _adsk_seams(monkeypatch):
    """The adsk types the input kinds isinstance-check, and a ValueInput.createByReal returning the
    ('real', cm) pair a scaled length is read off. FeatureOperations arrives seeded."""
    monkeypatch.setattr(adsk.fusion, "BRepBody", BRepBody, raising=False)
    monkeypatch.setattr(adsk.fusion, "BRepFace", BRepFace, raising=False)
    monkeypatch.setattr(adsk.fusion, "BRepEdge", BRepEdge, raising=False)
    monkeypatch.setattr(adsk.core.ValueInput, "createByReal",
                        staticmethod(lambda v: ("real", v)), raising=False)


def _body(name="Surf1", is_solid=False, area=None, solid_readable=True):
    """One BRep body - the trim target or a result body; `area` left None models one whose area
    will not read, which is the reading the phantom-cell gate stands down on."""
    return BRepBody(name, is_solid=is_solid, area=area, solid_readable=solid_readable)


def _wire(trim_features, handle_map=None):
    """Install a design whose active component carries `trim_features`, with `handle_map` behind
    the geometry handles."""
    bodies = [b for b in (handle_map or {}).values() if isinstance(b, BRepBody)]
    comp = MakeComp(bodies=bodies)
    comp.features = types.SimpleNamespace(trimFeatures=trim_features)
    install(se, make_design(comp=comp, tokens=dict(handle_map or {})))
    return comp


class FakeFeature(_SharedFeature):
    """A TrimFeature: the shared feature under the result bodies the area read-back walks."""
    def __init__(self, name="Feat1", bodies=None):
        super().__init__(name=name,
                         bodies=bodies if bodies is not None else [_body("Body1")])


class FakeBRepCell:
    """A candidate cell the trim tool divided the surface into. isSelected is settable; for a Trim
    feature a SELECTED cell is REMOVED. cellBody.area sizes it."""
    def __init__(self, area):
        self.isSelected = False
        self.cellBody = _body("Cell", area=area)


class FakeBRepCells:
    def __init__(self, areas):
        self._cells = [FakeBRepCell(a) for a in areas]
    @property
    def count(self):
        return len(self._cells)
    def item(self, i):
        return self._cells[i]


class FakeTrimInput:
    def __init__(self, tool, cell_areas):
        self.tool = tool
        self.cancelled = False
        self.bRepCells = FakeBRepCells(cell_areas)
    def cancel(self):
        self.cancelled = True


def _stale_cell_at(cells, index):
    """Make cells.item(index) RAISE - a stale BRepCell proxy, which safe() reads as no cell at all
    while the cells after it keep their own indices."""
    intact = cells.item

    def item(i):
        if i == index:
            raise RuntimeError("4 : An API Object refers to a deleted Object")
        return intact(i)
    cells.item = item


class FakeTrimFeatures:
    """createInput partial-computes and populates input.bRepCells (all isSelected=False). add()
    REPRODUCES the live contract: it RAISES "No cells are selected" when no cell isSelected - so a
    handler that forgets the selection step fails exactly as it did live. raise_on_add / null_feature
    exercise the failure paths where cancel() MUST be called."""
    def __init__(self, result_bodies=None, raise_on_add=False, null_feature=False,
                 cell_areas=(3.0, 9.0, 1.0)):
        self.last_input = None
        self._result = result_bodies
        self._raise = raise_on_add
        self._null = null_feature
        self._cell_areas = list(cell_areas)
    def createInput(self, tool):
        self.last_input = FakeTrimInput(tool, self._cell_areas)
        return self.last_input
    def add(self, inp):
        # live contract: with zero cells selected, add() raises "No cells are selected"
        if not any(inp.bRepCells.item(i).isSelected for i in range(inp.bRepCells.count)):
            raise RuntimeError("3 : No cells are selected.")
        if self._raise:
            raise RuntimeError("trim tool does not intersect")
        if self._null:
            return None
        return FakeFeature(name="Trim1", bodies=self._result)


class TestSurfaceTrim:

    def _keep_scene(self):
        tf = FakeTrimFeatures(result_bodies=[_body("Surf1")], cell_areas=(3.0, 9.0, 1.0))
        _wire(tf, handle_map={"S": _body(), "T": BRepFace(None)})
        return tf

    def test_commits_via_add_on_success(self):
        # cells: areas 3, 9, 1 -> default keeps the largest (index 1)
        tf = FakeTrimFeatures(result_bodies=[_body("Surf1")], cell_areas=(3.0, 9.0, 1.0))
        _wire(tf, handle_map={"S": _body(), "T": BRepFace(None)})
        out = payload(se.handler(surface="S", trim_tool="T"))
        assert out["trimmed"] is True
        # success path: the transaction was committed, NOT cancelled
        assert tf.last_input.cancelled is False
        # DEFAULT (keep larger): the single largest cell kept, the rest removed (selected)
        cells = tf.last_input.bRepCells
        assert cells.item(1).isSelected is False          # largest kept
        assert cells.item(0).isSelected is True           # removed
        assert cells.item(2).isSelected is True           # removed
        assert out["cells_kept"] == [1] and out["cells_removed"] == [0, 2]
        assert out["kept_area"] == 9.0

    def test_trim_selects_a_cell_before_add(self):
        # A handler that skips the cell-selection step leaves all cells unselected, so add() raises
        # "No cells are selected". The trim must select a cell first.
        tf = FakeTrimFeatures(cell_areas=(3.0, 9.0, 1.0))
        _wire(tf, handle_map={"S": _body(), "T": BRepFace(None)})
        out = payload(se.handler(surface="S", trim_tool="T"))
        assert out["trimmed"] is True   # passes only because a cell is now selected before add()

    def test_trim_that_removes_no_area_bites(self):
        # committed, cells were marked removed, but the surface area is identical -> error, not ok
        tf = FakeTrimFeatures(result_bodies=[_body("Surf1", area=12.0)],
                              cell_areas=(3.0, 9.0, 1.0))
        _wire(tf, handle_map={"S": _body(area=12.0), "T": BRepFace(None)})
        res = se.handler(surface="S", trim_tool="T")
        assert res["isError"] is True
        assert "did not decrease" in res["message"]

    def test_trim_that_shrinks_area_passes(self):
        # the removed cells' area is gone
        tf = FakeTrimFeatures(result_bodies=[_body("Surf1", area=9.0)],
                              cell_areas=(3.0, 9.0, 1.0))
        _wire(tf, handle_map={"S": _body(area=12.0), "T": BRepFace(None)})
        out = payload(se.handler(surface="S", trim_tool="T"))
        assert out["trimmed"] is True

    def test_keep_smaller_keeps_smallest_cell(self):
        tf = FakeTrimFeatures(result_bodies=[_body("Surf1")], cell_areas=(3.0, 9.0, 1.0))
        _wire(tf, handle_map={"S": _body(), "T": BRepFace(None)})
        out = payload(se.handler(surface="S", trim_tool="T", keep="smaller"))
        cells = tf.last_input.bRepCells
        assert cells.item(2).isSelected is False          # smallest (area 1) kept
        assert cells.item(0).isSelected is True and cells.item(1).isSelected is True
        assert out["cells_kept"] == [2] and out["kept_area"] == 1.0

    def test_keep_by_index_keeps_that_cell(self):
        tf = FakeTrimFeatures(result_bodies=[_body("Surf1")], cell_areas=(3.0, 9.0, 1.0))
        _wire(tf, handle_map={"S": _body(), "T": BRepFace(None)})
        out = payload(se.handler(surface="S", trim_tool="T", keep="0"))
        cells = tf.last_input.bRepCells
        assert cells.item(0).isSelected is False          # kept by index
        assert cells.item(1).isSelected is True and cells.item(2).isSelected is True
        assert out["cells_kept"] == [0]

    def test_keep_list_of_indices(self):
        tf = FakeTrimFeatures(result_bodies=[_body("Surf1")], cell_areas=(3.0, 9.0, 1.0))
        _wire(tf, handle_map={"S": _body(), "T": BRepFace(None)})
        out = payload(se.handler(surface="S", trim_tool="T", keep=[0, 2]))
        cells = tf.last_input.bRepCells
        assert cells.item(0).isSelected is False and cells.item(2).isSelected is False
        assert cells.item(1).isSelected is True           # only the unlisted cell removed
        assert out["cells_kept"] == [0, 2] and out["cells_removed"] == [1]

    def test_keep_int_index_keeps_that_cell(self):
        # keep passed as an actual int (not a string) -> _select_cells int branch
        tf = FakeTrimFeatures(result_bodies=[_body("Surf1")], cell_areas=(3.0, 9.0, 1.0))
        _wire(tf, handle_map={"S": _body(), "T": BRepFace(None)})
        out = payload(se.handler(surface="S", trim_tool="T", keep=2))
        cells = tf.last_input.bRepCells
        assert cells.item(2).isSelected is False          # kept by int index
        assert cells.item(0).isSelected is True and cells.item(1).isSelected is True
        assert out["cells_kept"] == [2]

    def test_keep_out_of_range_index_is_refused_naming_the_value(self):
        # silently falling back to 'largest' trims a DIFFERENT piece than the caller asked to keep,
        # with nothing in the payload saying so - the refusal names the index and the legal range
        tf = self._keep_scene()
        res = se.handler(surface="S", trim_tool="T", keep=[7, 9])
        assert res["isError"] is True
        assert "'keep' index 7 does not exist" in res["message"]
        assert "0..2" in res["message"]
        assert tf.last_input.cancelled is True            # the open transaction was aborted

    def test_keep_index_at_the_last_cell_is_accepted(self):
        # total-1 is IN range: the boundary the out-of-range refusal must not swallow
        tf = self._keep_scene()
        out = payload(se.handler(surface="S", trim_tool="T", keep=2))
        assert out["cells_kept"] == [2]
        assert tf.last_input.cancelled is False

    def test_keep_index_one_past_the_last_cell_is_refused(self):
        # total itself is OUT of range - the > vs >= boundary of the range check
        self._keep_scene()
        res = se.handler(surface="S", trim_tool="T", keep=3)
        assert res["isError"] is True
        assert "'keep' index 3 does not exist" in res["message"]

    def test_negative_keep_index_is_refused_not_read_as_from_the_end(self):
        self._keep_scene()
        res = se.handler(surface="S", trim_tool="T", keep=-1)
        assert res["isError"] is True
        assert "'keep' index -1 does not exist" in res["message"]

    def test_bad_keep_is_refused_naming_the_value(self):
        tf = self._keep_scene()
        res = se.handler(surface="S", trim_tool="T", keep="garbage")
        assert res["isError"] is True
        assert "'garbage' is not one" in res["message"]
        assert "'larger', 'smaller'" in res["message"]
        assert tf.last_input.cancelled is True

    def test_boolean_keep_is_refused_not_taken_as_index_one(self):
        # True is an int in Python - taken as an index it would keep cell 1 silently
        self._keep_scene()
        res = se.handler(surface="S", trim_tool="T", keep=True)
        assert res["isError"] is True
        assert "True is not one" in res["message"]

    def test_a_list_with_one_bad_member_is_refused_whole(self):
        # keeping the parseable members and dropping the rest would trim a piece nobody named
        self._keep_scene()
        res = se.handler(surface="S", trim_tool="T", keep=[0, "x"])
        assert res["isError"] is True
        assert "'x' is not one" in res["message"]

    def test_empty_keep_still_takes_the_larger_default(self):
        # '' / [] are an OMISSION, not a bad value - they must not be refused
        self._keep_scene()
        out = payload(se.handler(surface="S", trim_tool="T", keep=""))
        assert out["cells_kept"] == [1]                   # largest (area 9 at index 1)

    def test_an_unreadable_cell_leaves_the_area_of_every_later_cell_at_its_own_index(self):
        # 'keep' takes a cell INDEX and the kept indices are published, so the areas list is indexed
        # by cell number. A cell that cannot be read measures 0 in ITS slot: compacting the list
        # instead would make the third cell the second, and 'keep larger' would select the stale one.
        inp = FakeTrimInput(BRepFace(None), (3.0, 9.0, 5.0))
        _stale_cell_at(inp.bRepCells, 1)
        kept, kept_area, total, err = se._select_cells(inp, None)
        assert err is None and total == 3
        assert kept == [2] and kept_area == 5.0      # the largest READABLE cell, at its true index

    def test_an_unreadable_cell_does_not_shift_which_cells_are_selected(self):
        # the selection walk writes isSelected per index: cell 2 was asked for, so cell 2 is the one
        # left unselected (KEPT for a trim) and cell 0 is the one selected for removal
        inp = FakeTrimInput(BRepFace(None), (3.0, 9.0, 5.0))
        first, third = inp.bRepCells.item(0), inp.bRepCells.item(2)
        _stale_cell_at(inp.bRepCells, 1)
        kept, _area, total, err = se._select_cells(inp, 2)
        assert err is None and kept == [2] and total == 3
        assert third.isSelected is False             # kept: the cell the caller named
        assert first.isSelected is True              # removed

    def test_phantom_cell_kept_area_exceeds_input_aborts(self):
        # A coincident/overlapping surface injects a cell LARGER than the target's own area (the compute
        # spans every visible surface the tool crosses). 'keep larger' would latch onto it: kept_area >
        # surf.area is impossible for a real subset of the target -> abort BEFORE add(), cancel the txn.
        tf = FakeTrimFeatures(result_bodies=[_body("Surf1")],
                              cell_areas=(4.0, 20.0, 6.0))   # largest cell (20) exceeds the 16 input
        _wire(tf, handle_map={"S": _body(area=16.0), "T": BRepFace(None)})
        res = se.handler(surface="S", trim_tool="T")
        assert res["isError"] is True
        assert "larger than" in res["message"] and "HIDE" in res["message"]
        assert tf.last_input.cancelled is True          # transaction aborted, no feature landed

    def test_kept_area_within_input_still_trims(self):
        # The same scene without the phantom: the kept cell is smaller than the input -> a normal trim.
        tf = FakeTrimFeatures(result_bodies=[_body("Surf1", area=12.0)],
                              cell_areas=(4.0, 12.0, 6.0))
        _wire(tf, handle_map={"S": _body(area=16.0), "T": BRepFace(None)})
        out = payload(se.handler(surface="S", trim_tool="T"))
        assert out["trimmed"] is True and out["kept_area"] == 12.0

    def test_no_cells_cancels_and_reports_no_intersection(self):
        tf = FakeTrimFeatures(cell_areas=())              # createInput divided nothing
        _wire(tf, handle_map={"S": _body(), "T": BRepFace(None)})
        res = se.handler(surface="S", trim_tool="T")
        assert res["isError"] is True
        assert tf.last_input.cancelled is True            # open transaction aborted
        assert "no cells" in res["message"].lower()

    def test_cancels_open_transaction_when_add_raises(self):
        tf = FakeTrimFeatures(raise_on_add=True)
        _wire(tf, handle_map={"S": _body(), "T": BRepFace(None)})
        res = se.handler(surface="S", trim_tool="T")
        assert res["isError"] is True
        # THE HAZARD: cancel() was CALLED (not swallowed) so the open transaction is aborted
        assert tf.last_input.cancelled is True

    def test_cancels_when_add_returns_null_feature(self):
        tf = FakeTrimFeatures(null_feature=True)
        _wire(tf, handle_map={"S": _body(), "T": BRepFace(None)})
        res = se.handler(surface="S", trim_tool="T")
        assert res["isError"] is True
        assert tf.last_input.cancelled is True
        assert "cancelled" in res["message"].lower()

    def test_wrong_kind_surface_gets_redirect_before_any_transaction(self):
        # a SOLID handed where a surface is required -> redirecting error, no createInput called
        tf = FakeTrimFeatures()
        _wire(tf, handle_map={"S": _body("Body1", is_solid=True), "T": BRepFace(None)})
        res = se.handler(surface="S", trim_tool="T")
        assert res["isError"] is True
        assert "OPEN SURFACE body" in res["message"] and "SOLID body" in res["message"]
        assert tf.last_input is None        # never opened a transaction


class TestTrimVisibleScope:

    def test_foreign_smaller_surface_refuses_before_transaction(self):
        tf = FakeTrimFeatures(cell_areas=(0.2, 0.8, 0.5, 0.5))
        _wire(tf, handle_map={"S": _body(area=1.0), "T": BRepFace(None),
                              "B": _body("Nearby", area=1.0)})
        res = se.handler(surface="S", trim_tool="T")
        assert res["isError"] is True and "Nearby" in res["message"]
        assert "view_set" in res["message"]
        assert tf.last_input is None

    def test_hidden_neighbor_allows_trim_without_changing_its_visibility(self):
        tf = FakeTrimFeatures(result_bodies=[_body(area=0.8)], cell_areas=(0.2, 0.8))
        neighbor = _body("Nearby", area=1.0)
        neighbor.isLightBulbOn = False
        _wire(tf, handle_map={"S": _body(area=1.0), "T": BRepFace(None), "B": neighbor})
        out = payload(se.handler(surface="S", trim_tool="T"))
        assert out["trimmed"] is True
        assert out["surface_scope"] == "no_other_visible_surfaces"
        assert neighbor.isLightBulbOn is False and neighbor.area == 1.0

    def test_incomplete_body_census_refuses_before_transaction(self):
        tf = FakeTrimFeatures()
        comp = _wire(tf, handle_map={"S": _body(), "T": BRepFace(None)})
        def unread(_index):
            raise RuntimeError("stale body")
        comp.bRepBodies.item = unread
        res = se.handler(surface="S", trim_tool="T")
        assert res["isError"] is True and "scope" in res["message"]
        assert tf.last_input is None

    def test_hidden_target_refuses_before_transaction(self):
        tf = FakeTrimFeatures()
        target = _body()
        target.isLightBulbOn = False
        _wire(tf, handle_map={"S": target, "T": BRepFace(None)})
        res = se.handler(surface="S", trim_tool="T")
        assert res["isError"] is True and "target surface must be visible" in res["message"]
        assert tf.last_input is None


class TestSolidVerdictOverBodyFacts:

    """trim/extend collapse the shared per-body {name, is_solid} projection into one verdict. That
    projection publishes True/False/None, so the collapse keeps the three apart - any() would fold
    an unreadable flag into a confident 'a surface'."""

    def test_the_verdict_keeps_the_three_states_apart(self):
        assert surface_common_mod._solid_verdict([False, True]) is True        # any solid wins
        assert surface_common_mod._solid_verdict([False, None]) is False       # a flag READ false is an answer
        assert surface_common_mod._solid_verdict([None, None]) is None         # nothing read -> unknown
        assert surface_common_mod._solid_verdict([]) is None                   # no body read -> unknown

    def test_trim_publishes_a_null_is_solid_with_an_unverified_marker(self):
        rb = _body("Surf1", area=12.0, solid_readable=False)
        tf = FakeTrimFeatures(result_bodies=[rb], cell_areas=(4.0, 12.0, 6.0))
        _wire(tf, handle_map={"S": _body(area=16.0), "T": BRepFace(None)})
        out = payload(se.handler(surface="S", trim_tool="T"))
        assert out["is_solid"] is None
        assert out["unverified"] == ["is_solid"]
        assert "Not read back off the feature: is_solid." in out["note"]

    def test_trim_on_a_readable_flag_carries_no_marker(self):
        # the boundary: a flag that READ false is an answer, so the disclosure must not fire on it.
        tf = FakeTrimFeatures(result_bodies=[_body("Surf1", area=12.0)],
                              cell_areas=(4.0, 12.0, 6.0))
        _wire(tf, handle_map={"S": _body(area=16.0), "T": BRepFace(None)})
        out = payload(se.handler(surface="S", trim_tool="T"))
        assert out["is_solid"] is False and "unverified" not in out
