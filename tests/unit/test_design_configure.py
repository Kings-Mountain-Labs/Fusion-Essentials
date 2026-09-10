"""Unit tests for ``design_configure`` — the configured-design build+switch tool.

The Fusion configurations API is mocked; what we pin is the tool's OWN logic: the action dispatch and
guards (unknown action, no design, not-yet-configured for column actions), creating a configured
design, adding configuration rows, and the four column kinds (parameter / suppress / visibility /
appearance-theme) including:
  - addressing cells by ROW NAME (getCellByRowName) — the robust path, live-verified,
  - parameter cells take an EXPRESSION string,
  - suppress cells take isSuppressed (bool), visibility cells take isVisible (bool),
  - the appearance-theme ORDERING: the body column must be added before extra theme rows, and each
    config row is linked to a theme row via the top table's parentTableColumn (ConfigurationThemeColumn).

Fakes are NAMED to match the real adsk classes where the handler reads type(x).__name__.
"""

import json
from types import SimpleNamespace

import pytest

import live_api_facts as _api_facts
from conftest import (FakeDataFile, FakeDataFolder, FakeDataProject, FakeFeature, FakeOccurrence,
                      FakeTimeline, FakeTimelineObject, MakeComp, MakeDesign, _NamedCollection,
                      load_tool)

dc = load_tool("design_configure")

_WARNING = _api_facts.ENUMS["fusion.FeatureHealthStates"]["WarningFeatureHealthState"]
_ERROR = _api_facts.ENUMS["fusion.FeatureHealthStates"]["ErrorFeatureHealthState"]


# ── fakes mirroring the configurations object model ─────────────────────────

class _Param:
    def __init__(self, name, expr="80 mm"):
        self.name = name
        self.expression = expr


class ConfigurationParameterCell:
    def __init__(self):
        self.expression = None


class ConfigurationSuppressCell:
    def __init__(self):
        self.isSuppressed = False


class ConfigurationVisibilityCell:
    def __init__(self):
        self.isVisible = True


class ConfigurationAppearanceCell:
    def __init__(self):
        self.appearance = None


class _Col:
    """A configuration column whose cells are addressed by row name. `owner` is the collection that
    lists it, so deleteMe can stop listing it; `delete_lands` False models the accept-and-ignore -
    deleteMe answers true and the column is still there, which only a read-back catches."""
    def __init__(self, cls, kind="param"):
        self.id = "col-" + kind
        self._cls = cls
        self._cells = {}
        self._scratch = {}
        self.cell_factory = cls
        self.owner = None
        self.delete_lands = True

    def deleteMe(self):
        if self.delete_lands and self.owner is not None and self in self.owner.added:
            self.owner.added.remove(self)
        return True
    def _cell(self, rowname):
        if rowname not in self._cells:
            self._cells[rowname] = self.cell_factory()
        return self._cells[rowname]
    def getCellByRowName(self, name):
        return self._cell(name)
    def getCell(self, idx):
        # NOT the addressing the tool should use - the index runs over the COLUMN's own rowCount,
        # so this hands back a scratch cell unrelated to the by-name cells the assertions read.
        self._scratch.setdefault(idx, self.cell_factory())
        return self._scratch[idx]
    def classType(self):
        return self._cls.__name__


class _Row:
    def __init__(self, name, idx, owner=None):
        self.name = name
        self.id = "row-" + name
        self.index = idx
        self.activated = False
        self._owner = owner
    def activate(self):
        self.activated = True
        if self._owner is not None:
            self._owner._active = self      # a real activate() moves the table's active row
        return True


class _Rows(_NamedCollection):
    """ConfigurationRows: the shared counted/iterable walk plus add(name), which ACTIVATES the new
    row and lets the owner copy the row above it into it."""
    def __init__(self, owner=None, on_add=None):
        super().__init__()
        self._owner = owner
        self._on_add = on_add

    def add(self, name):
        prev = self._items[-1] if self._items else None
        r = _Row(name, len(self._items), self._owner)
        self._items.append(r)
        if self._owner is not None:
            self._owner._active = r         # adding a configuration row activates it
        if self._on_add is not None:
            self._on_add(r, prev)           # a new row copies the cell values of the row above it
        return r


class _ThemeCell:
    """The config->theme link cell. Its owner's 'cell_mode', read at assignment time, models the
    assignment landing ('honest'), being silently dropped ('silent'), or reading back a DIFFERENT row
    than the one assigned ('lies')."""
    def __init__(self, owner=None):
        self._owner = owner
        self._row = None

    @property
    def referencedTableRow(self):
        return self._row

    @referencedTableRow.setter
    def referencedTableRow(self, value):
        mode = getattr(self._owner, "cell_mode", "honest")
        if mode == "honest":
            self._row = value
        elif mode == "lies":
            self._row = getattr(self._owner, "substitute", None)
        # 'silent': the link is dropped and the cell keeps reading no row


class _ThemeColumn:
    """Models the live trap: getCell(index) and getCellByRowName(name) address DIFFERENT cells.
    The tool must use getCellByRowName for the config->theme link; a positional getCell() here returns
    a throwaway cell that the assertions never inspect, so an index-based tool would silently mislink."""
    def __init__(self, rows):
        self._rows = rows
        self.by_name = {}
        self._scratch = {}
        self.cell_mode = "honest"
        self.substitute = None
    def getCell(self, i):
        # NOT the addressing the tool should use — hand back a scratch cell unrelated to by_name.
        self._scratch.setdefault(i, _ThemeCell(self))
        return self._scratch[i]
    def getCellByRowName(self, name):
        if name not in self.by_name:
            self.by_name[name] = _ThemeCell(self)
        return self.by_name[name]


class _AppearanceTable:
    def __init__(self):
        self.rows = _Rows()
        self._columns_added = []
        self._theme_col = _ThemeColumn(self.rows)
        self.parentTableColumn = self._theme_col
    @property
    def columns(self):
        return self
    def add(self, body):
        # adding the body column creates the first theme row (the live gotcha). The emptiness check
        # reads the fake's own list, not the counted property, so a test that breaks the count read
        # still gets the platform's seeded row.
        if not self.rows._items:
            self.rows.add("Theme 1")
        col = _Col(ConfigurationAppearanceCell, kind="appearance")
        self._columns_added.append(col)
        return col


class ConfigurationMaterialCell:
    """A material cell. Its table's 'cell_mode', read at assignment time, models the three outcomes
    an assignment can have: it lands ('honest'), it silently does not land ('silent' - the cell still
    reads no material), or the cell reports a DIFFERENT material than the one assigned ('lies')."""
    def __init__(self, owner=None):
        self._owner = owner
        self._material = None

    @property
    def material(self):
        return self._material

    @material.setter
    def material(self, value):
        mode = getattr(self._owner, "cell_mode", "honest")
        if mode == "honest":
            self._material = value
        elif mode == "lies":
            self._material = getattr(self._owner, "substitute", None)
        # 'silent': the assignment is dropped and the cell keeps reading no material


class _MaterialColumn:
    """A material column: cells are addressed by theme ROW NAME, getCell(index) hands back an
    unrelated scratch cell, and the column exposes .title/.id/.entity - a real
    ConfigurationMaterialColumn has NO .name."""
    def __init__(self, entity, title, table=None):
        self.entity = entity
        self.id = "matcol-" + title
        self.title = title
        self._table = table
        self._cells = {}
        self._scratch = {}

    @property
    def cell_mode(self):
        """This column's assignment behaviour: a column named in the table's silent_columns drops
        what it is handed, so one column can refuse while the others assign honestly."""
        if self.title in getattr(self._table, "silent_columns", ()):
            return "silent"
        return getattr(self._table, "cell_mode", "honest")

    @property
    def substitute(self):
        return getattr(self._table, "substitute", None)

    def getCellByRowName(self, name):
        if name not in self._cells:
            self._cells[name] = ConfigurationMaterialCell(self)
        return self._cells[name]

    def getCell(self, idx):
        # NOT the addressing the tool should use - the index runs over the COLUMN's own rowCount,
        # so this hands back a scratch cell unrelated to the by-name cells the assertions read.
        self._scratch.setdefault(idx, ConfigurationMaterialCell(self))
        return self._scratch[idx]

    def material_of(self, row_name):
        """The material name this column holds on a theme row, or None - what a caller sees for the
        configurations linked to that row."""
        m = self._cells.get(row_name)
        return getattr(m.material, "name", None) if m is not None else None


class _MaterialColumns:
    """materialTable.columns: the first non-root add ALSO mints a root-component column ahead of it,
    so the count jumps 0 -> 2 on a single add. A tool asserting count == 1 would be wrong."""
    def __init__(self, table):
        self._table = table
        self._cols = []
        self.added = []                           # the non-root columns, in add order

    @property
    def count(self):
        return len(self._cols)

    def item(self, i):
        return self._cols[i]

    def add(self, entity):
        if self._table.add_returns_null:
            return None
        existing = next((c for c in self._cols if c.entity is entity), None)
        if existing is not None:
            return existing                       # an entity that already has a column keeps it
        if not self._cols:                        # the auto-created root-component column
            self._cols.append(_MaterialColumn("RootComponent", "(Unsaved)", self._table))
        col = _MaterialColumn(entity, getattr(entity, "name", "Body"), self._table)
        self._cols.append(col)
        self.added.append(col)
        if self._table.rows.count == 0:
            # the column add mints the first theme row, and EVERY configuration starts out
            # referencing it (the platform seeds the links, so it bypasses the cell's mode)
            row = self._table.rows.add("Theme 1")
            for name in self._table.config_names():
                self._table.parentTableColumn.getCellByRowName(name)._row = row
        return col


class _MaterialRows(_Rows):
    """materialTable.rows: adding a name an existing row carries returns THAT row and adds nothing,
    and a genuinely new row starts as a COPY of the row above it - which is not the row the
    configuration being moved was referencing."""
    def __init__(self, table):
        super().__init__()
        self._table = table

    def add(self, name):
        taken = next((self.item(i) for i in range(self.count) if self.item(i).name == name), None)
        if taken is not None:
            return taken
        prev = self.item(self.count - 1) if self.count else None
        row = super().add(name)
        if prev is not None:
            for i in range(self._table.columns.count):
                col = self._table.columns.item(i)
                src = col.getCellByRowName(prev.name).material
                if src is not None:
                    # the platform copies, not the tool
                    col.getCellByRowName(row.name)._material = src
        return row


class _MaterialTable:
    def __init__(self, top=None):
        self._top = top
        self.rows = _MaterialRows(self)
        self.columns = _MaterialColumns(self)
        self.parentTableColumn = _ThemeColumn(self.rows)
        self.add_returns_null = False
        self.cell_mode = "honest"
        self.substitute = None
        self.silent_columns = set()        # titles of columns that drop what they are assigned

    def config_names(self):
        top = self._top
        return [top.rows.item(i).name for i in range(top.rows.count)] if top is not None else []


class ConfigurationInsertCell:
    def __init__(self):
        self.row = None        # set to a part ConfigurationRow


class _InsertCol(_Col):
    def __init__(self):
        super().__init__(ConfigurationInsertCell, "insert")
        self.cell_factory = ConfigurationInsertCell
        self.occurrence = None


class _Columns:
    """The table's column collection - countable and indexable, the reads a rollback is proven by."""
    def __init__(self):
        self.added = []

    @property
    def count(self):
        return len(self.added)

    def item(self, i):
        return self.added[i]

    def _own(self, c):
        c.owner = self
        self.added.append(c)
        return c

    def addParameterColumn(self, p):
        c = _Col(ConfigurationParameterCell, "param"); c.param = p; return self._own(c)
    def addSuppressColumn(self, f):
        c = _Col(ConfigurationSuppressCell, "suppress"); c.feature = f; self.added.append(c); return c
    def addVisibilityColumn(self, e):
        c = _Col(ConfigurationVisibilityCell, "visibility"); c.entity = e; self.added.append(c); return c
    def addInsertColumn(self, occ):
        c = _InsertCol(); c.occurrence = occ; self.added.append(c); return c


class ConfigurationTopTable:
    def __init__(self):
        self._active = None
        # rows.add(...) and row.activate() both move _active; a new row also copies the row above
        self.rows = _Rows(owner=self, on_add=self._row_added)
        self.rows.add("Default")           # createConfiguredDesign yields one row
        self.columns = _Columns()
        self.appearanceTable = _AppearanceTable()
        self.materialTable = _MaterialTable(self)
        self.name = "Configurations"
        self.id = "1"

    @property
    def activeRow(self):
        return self._active

    def _row_added(self, row, prev):
        # a new configuration row copies the cell values of the row above it - including WHICH theme
        # row it references, so a configuration added later starts out sharing its neighbour's theme
        if prev is None:
            return
        tc = self.materialTable.parentTableColumn
        tc.getCellByRowName(row.name)._row = tc.getCellByRowName(prev.name).referencedTableRow


class _MaterialCollection(_NamedCollection):
    """design.materials - a count/item(i) collection of named materials (what iter_collection walks)."""
    def __init__(self, names):
        super().__init__(SimpleNamespace(name=n, id="mat-%d" % i) for i, n in enumerate(names))

    def named(self, name):
        return next(m for m in self._items if m.name == name)


class _PartRow:
    def __init__(self, name):
        self.name = name
        self.id = "part-" + name


class _PartTable:
    """Stand-in for the inserted part's configurationTable (rows addressable by name)."""
    def __init__(self, names):
        self._rows = [_PartRow(n) for n in names]
        self.rows = _Rows()
        for n in names:
            self.rows.add(n)
    def row(self, name):
        for i in range(self.rows.count):
            if self.rows.item(i).name == name:
                return self.rows.item(i)
        return None


class _FakeDataFile(FakeDataFile):
    """A configured-design cloud file: its configurationTable carries the rows an insert picks."""
    def __init__(self, name, configs):
        super().__init__(name=name, file_id="urn:" + name)
        self.isConfiguredDesign = True
        self.configurationTable = _PartTable(configs)


class _FakeOccurrence(FakeOccurrence):
    """The instance addFromConfiguration hands back: it carries the configuration row it was
    placed from."""
    def __init__(self, row):
        super().__init__(path="Inserted:1")
        self.isConfiguration = True
        self.configurationRow = row


class _Occurrences(_NamedCollection):
    """comp.occurrences: the shared walk plus addFromConfiguration - the insert seam."""
    def __init__(self):
        super().__init__()
        self.inserted = []
    def addFromConfiguration(self, row, transform):
        occ = _FakeOccurrence(row)
        self.inserted.append((row, transform))
        return occ


def _root():
    """The root component, whose occurrences collection is the insert seam."""
    comp = MakeComp("Root")
    comp.occurrences = _Occurrences()
    return comp


class _Design(MakeDesign):
    """A design that can be CONVERTED to a configured one - createConfiguredDesign mints the top
    table, and `created` records that it ran."""
    def __init__(self, configured=False, params=None, bodies=None, features=None,
                 appearances=None, datafiles=None, materials=()):
        super().__init__(comp=_root(), all_parameters=_NamedCollection(params or []))
        self._top = ConfigurationTopTable() if configured else None
        self.created = None
        self._bodies = bodies or {}
        self._features = features or {}
        self._appearances = appearances or {}
        self._datafiles = datafiles or {}
        self.materials = _MaterialCollection(materials)

    @property
    def configurationTopTable(self):
        return self._top

    def createConfiguredDesign(self):
        self._top = ConfigurationTopTable()
        self.created = self._top
        return self._top


def _install(monkeypatch, design, saved=True):
    monkeypatch.setattr(dc._common, "design", lambda: design)
    monkeypatch.setattr(dc, "_doc_is_saved", lambda: saved)      # default: pretend the doc is saved
    return design


def _payload(result):
    assert result["isError"] is False, result
    return json.loads(result["content"][0]["text"])


# ── guards / dispatch ────────────────────────────────────────────────────────

class TestGuards:
    def test_unknown_action(self, monkeypatch):
        _install(monkeypatch, _Design())
        res = dc.handler(action="frobnicate")
        assert res["isError"] is True and "action" in res["message"].lower()

    def test_no_active_design(self, monkeypatch):
        monkeypatch.setattr(dc._common, "design", lambda: None)
        res = dc.handler(action="create")
        assert res["isError"] is True and "design" in res["message"].lower()

    def test_column_action_requires_configured_design(self, monkeypatch):
        # add_parameter on a non-configured design should error clearly, not crash
        _install(monkeypatch, _Design(configured=False, params=[_Param("plate_len")]))
        res = dc.handler(action="add_parameter", parameter="plate_len", values={"Default": "50 mm"})
        assert res["isError"] is True and "configured" in res["message"].lower()


# ── create ───────────────────────────────────────────────────────────────────

class TestCreate:
    def test_create_converts_design(self, monkeypatch):
        d = _install(monkeypatch, _Design(configured=False))
        out = _payload(dc.handler(action="create"))
        assert d.created is not None
        assert out["configured"] is True

    def test_create_is_idempotent_when_already_configured(self, monkeypatch):
        d = _install(monkeypatch, _Design(configured=True))
        out = _payload(dc.handler(action="create"))
        # already configured -> reports it, does NOT call createConfiguredDesign again
        assert d.created is None and out["configured"] is True

    def test_create_refuses_unsaved_document(self, monkeypatch):
        # the conversion only materializes on save+reopen; converting an unsaved doc is refused
        # (and must NOT auto-save). It must also NOT have called createConfiguredDesign.
        d = _install(monkeypatch, _Design(configured=False), saved=False)
        res = dc.handler(action="create")
        assert res["isError"] is True and "save" in res["message"].lower()
        assert d.created is None      # did not mutate

    def test_create_proceeds_when_saved(self, monkeypatch):
        d = _install(monkeypatch, _Design(configured=False), saved=True)
        out = _payload(dc.handler(action="create"))
        assert d.created is not None and out["created"] is True
        # the success note steers the user to save+reopen to see it in the UI
        assert "reopen" in out["note"].lower()

    def test_the_note_says_the_conversion_leaves_the_old_lineage_behind(self, monkeypatch):
        # MEASURED: the pre-conversion lineage is relocated into a Fusion-managed project and
        # deleting this design does NOT remove it - a caller cleaning up its own scratch files has
        # no way to learn that from any read this tool offers, so the conversion says it here.
        _install(monkeypatch, _Design(configured=False), saved=True)
        note = _payload(dc.handler(action="create"))["note"]
        assert "System Project - CONFIG" in note and "leaves behind" in note


# ── add_configuration (row) ─────────────────────────────────────────────────

class TestAddConfiguration:
    def test_add_row(self, monkeypatch):
        d = _install(monkeypatch, _Design(configured=True))
        out = _payload(dc.handler(action="add_configuration", name="Large"))
        names = [d.configurationTopTable.rows.item(i).name
                 for i in range(d.configurationTopTable.rows.count)]
        assert "Large" in names and out["configuration"] == "Large"

    def test_add_row_discloses_that_it_activated_the_new_configuration(self, monkeypatch):
        # adding a row switches the design to it - a payload that stayed silent would leave the
        # caller believing the configuration active before the call is still what the model shows
        _install(monkeypatch, _Design(configured=True))
        out = _payload(dc.handler(action="add_configuration", name="Large"))
        assert out["active_configuration"] == "Large"
        assert "activated it" in out["note"].lower()

    def test_add_row_requires_name(self, monkeypatch):
        _install(monkeypatch, _Design(configured=True))
        res = dc.handler(action="add_configuration", name="")
        assert res["isError"] is True and "name" in res["message"].lower()


class TestRenameConfiguration:
    def test_rename_changes_row_name(self, monkeypatch):
        d = _install(monkeypatch, _Design(configured=True))
        # default row is "Default" in the fake; rename to Medium
        out = _payload(dc.handler(action="rename_configuration", name="Default", new_name="Medium"))
        names = [d.configurationTopTable.rows.item(i).name
                 for i in range(d.configurationTopTable.rows.count)]
        assert "Medium" in names and "Default" not in names
        assert out["from"] == "Default" and out["to"] == "Medium"

    def test_rename_unknown_row_errors(self, monkeypatch):
        _install(monkeypatch, _Design(configured=True))
        res = dc.handler(action="rename_configuration", name="Ghost", new_name="X")
        assert res["isError"] is True and "Ghost" in res["message"]

    def test_rename_requires_both_names(self, monkeypatch):
        _install(monkeypatch, _Design(configured=True))
        res = dc.handler(action="rename_configuration", name="Default", new_name="")
        assert res["isError"] is True
        assert "'new_name'" in res["message"]

    def test_rename_to_existing_name_errors(self, monkeypatch):
        _install(monkeypatch, _Design(configured=True))
        dc.handler(action="add_configuration", name="Large")
        res = dc.handler(action="rename_configuration", name="Default", new_name="Large")
        assert res["isError"] is True and "exists" in res["message"].lower()


# ── add_parameter ────────────────────────────────────────────────────────────

class TestAddParameter:
    def test_param_column_and_expressions_by_row_name(self, monkeypatch):
        d = _install(monkeypatch, _Design(configured=True, params=[_Param("plate_len")]))
        # add two rows so the values map onto real rows
        dc.handler(action="add_configuration", name="Small")
        dc.handler(action="add_configuration", name="Large")
        out = _payload(dc.handler(action="add_parameter", parameter="plate_len",
                                  values={"Small": "50 mm", "Large": "120 mm"}))
        col = d.configurationTopTable.columns.added[0]
        assert col.getCellByRowName("Small").expression == "50 mm"
        assert col.getCellByRowName("Large").expression == "120 mm"
        assert out["parameter"] == "plate_len" and out["set"] == 2

    def test_missing_parameter_errors(self, monkeypatch):
        _install(monkeypatch, _Design(configured=True, params=[]))
        res = dc.handler(action="add_parameter", parameter="ghost", values={"Default": "5 mm"})
        assert res["isError"] is True and "ghost" in res["message"]

    def test_value_for_unknown_row_is_reported(self, monkeypatch):
        _install(monkeypatch, _Design(configured=True, params=[_Param("plate_len")]))
        res = dc.handler(action="add_parameter", parameter="plate_len",
                         values={"Nonexistent": "5 mm"})
        # a value naming a row that doesn't exist should surface, not silently pass
        assert res["isError"] is True and "Nonexistent" in res["message"]


# ── suppress / visibility (need a resolvable feature/body) ──────────────────

class TestSuppressVisibility:
    def _stub_feature(self, monkeypatch, d):
        """Stub the typed FeatureRef seam ((entity, timeline name), error) - resolution itself
        (exact match, name@index, the ambiguity refusal) is pinned once in test_inputs."""
        monkeypatch.setattr(dc._FEATURE, "resolve",
                            lambda raw: ((d._features[raw], raw), None) if raw in d._features
                            else (None, f"'feature': no timeline feature named '{raw}'."))

    def test_suppress_sets_is_suppressed(self, monkeypatch):
        feat = FakeFeature("Fillet1")
        d = _install(monkeypatch, _Design(configured=True, features={"Fillet1": feat}))
        self._stub_feature(monkeypatch, d)
        dc.handler(action="add_configuration", name="Small")
        out = _payload(dc.handler(action="add_suppress", feature="Fillet1",
                                  suppressed_in=["Small"]))
        col = d.configurationTopTable.columns.added[0]
        assert col.getCellByRowName("Small").isSuppressed is True
        assert out["feature"] == "Fillet1"

    def test_an_ambiguous_feature_name_is_refused_before_any_column_lands(self, monkeypatch):
        # timeline names are NOT design-wide unique (two components can each hold an 'Extrude1');
        # the typed kind refuses with the name@index candidates instead of suppressing whichever
        # same-named feature a walk finds first - and the refusal must land BEFORE the mutation.
        d = _install(monkeypatch, _Design(configured=True, features={}))
        monkeypatch.setattr(dc._FEATURE, "resolve",
                            lambda raw: (None, "'feature': 'Extrude1' matches 2 timeline objects "
                                              "(Extrude1@3, Extrude1@7) - name one with the "
                                              "'name@index' form."))
        dc.handler(action="add_configuration", name="Small")
        res = dc.handler(action="add_suppress", feature="Extrude1", suppressed_in=["Small"])
        assert res["isError"] is True
        assert "name@index" in res["message"] and "Extrude1@3" in res["message"]
        assert d.configurationTopTable.columns.added == []   # nothing mutated on a refusal

    def test_visibility_sets_is_visible(self, monkeypatch):
        body = FakeFeature("Body1")
        d = _install(monkeypatch, _Design(configured=True, bodies={"Body1": body}))
        monkeypatch.setattr(dc._BODY, "resolve", lambda raw: (d._bodies.get(raw), None) if raw in d._bodies
                            else (None, f"No body named '{raw}'."))
        dc.handler(action="add_configuration", name="Large")
        out = _payload(dc.handler(action="add_visibility", body="Body1",
                                  hidden_in=["Large"]))
        col = d.configurationTopTable.columns.added[0]
        assert col.getCellByRowName("Large").isVisible is False
        assert out["body"] == "Body1"


# ── appearance theme (ordering + linkage) ───────────────────────────────────

class TestAppearanceTheme:
    def test_appearance_adds_column_before_rows_then_links(self, monkeypatch):
        body = FakeFeature("Body1")
        # Appearance stubs carry a readable .name: the read-back gate treats an unreadable
        # appearance name as a failed assignment.
        from types import SimpleNamespace as _NS
        d = _install(monkeypatch, _Design(configured=True, bodies={"Body1": body},
                             appearances={"Red": _NS(name="Red"), "Blue": _NS(name="Blue")}))
        monkeypatch.setattr(dc._BODY, "resolve", lambda raw: (d._bodies.get(raw), None) if raw in d._bodies
                            else (None, f"No body named '{raw}'."))
        monkeypatch.setattr(dc, "_resolve_appearance", lambda design, name: d._appearances.get(name))
        dc.handler(action="add_configuration", name="Small")
        out = _payload(dc.handler(action="set_appearance", body="Body1",
                                  appearances={"Default": "Red", "Small": "Blue"}))
        appt = d.configurationTopTable.appearanceTable
        # the body column was added (which auto-created the first theme row)
        assert len(appt._columns_added) == 1
        assert out["body"] == "Body1" and out["themes"] >= 2
        # CRITICAL: each CONFIG must link to the theme row carrying ITS appearance — addressed by NAME,
        # not positional index (the live bug). The theme cell for 'Default' and 'Small' must each have a
        # referencedTableRow set, and they must be DIFFERENT theme rows.
        theme_col = appt.parentTableColumn
        ref_default = theme_col.getCellByRowName("Default").referencedTableRow
        ref_small = theme_col.getCellByRowName("Small").referencedTableRow
        assert ref_default is not None and ref_small is not None
        assert ref_default is not ref_small      # distinct configs -> distinct theme rows
        # and each linked theme row carries the right appearance
        col = appt._columns_added[0]
        # the cell each ref points at, read by the theme row's NAME - the addressing a caller sees
        def appearance_for(ref_row):
            return col.getCellByRowName(ref_row.name).appearance
        assert appearance_for(ref_default) is d._appearances["Red"]
        assert appearance_for(ref_small) is d._appearances["Blue"]

    def test_exactly_one_theme_row_per_named_configuration_is_minted(self, monkeypatch):
        # The bound stops AT the number wanted, not one past it: a row minted beyond that is an
        # orphan no configuration references, and the call still reports success over it.
        from types import SimpleNamespace as _NS
        d = _install(monkeypatch, _Design(configured=True, bodies={"Body1": FakeFeature("Body1")},
                                          appearances={"Red": _NS(name="Red"),
                                                       "Blue": _NS(name="Blue")}))
        monkeypatch.setattr(dc._BODY, "resolve", lambda raw: (d._bodies.get(raw), None))
        monkeypatch.setattr(dc, "_resolve_appearance", lambda design, name: d._appearances.get(name))
        dc.handler(action="add_configuration", name="Small")
        _payload(dc.handler(action="set_appearance", body="Body1",
                            appearances={"Default": "Red", "Small": "Blue"}))
        appt = d.configurationTopTable.appearanceTable
        names = [appt.rows.item(i).name for i in range(appt.rows.count)]
        assert names == ["Theme 1", "Theme 2"], names

    def test_theme_rows_that_never_read_error_instead_of_adding_forever(self, monkeypatch):
        # The theme rows are added until the table holds one per configuration - so the count is
        # what ENDS it. A count that will not read answers 'no rows' to a guarded read, and this
        # tool is exempt from the server's call timeout, so an unbounded add loop returns to nobody.
        from types import SimpleNamespace as _NS
        d = _install(monkeypatch, _Design(configured=True, bodies={"Body1": FakeFeature("Body1")},
                                          appearances={"Red": _NS(name="Red"),
                                                       "Blue": _NS(name="Blue")}))
        monkeypatch.setattr(dc._BODY, "resolve", lambda raw: (d._bodies.get(raw), None))
        monkeypatch.setattr(dc, "_resolve_appearance", lambda design, name: d._appearances.get(name))
        dc.handler(action="add_configuration", name="Small")
        d.configurationTopTable.appearanceTable.rows._raises = "3 : theme row count unreadable"
        res = dc.handler(action="set_appearance", body="Body1",
                         appearances={"Default": "Red", "Small": "Blue"})
        # it RETURNS, and the refusal names the count it reached against the count it needed
        assert res["isError"] is True
        assert "None theme rows" in res["message"] and "2 this call needs" in res["message"]


# ── add_material: per-configuration physical material (the material theme table) ─────────────

@pytest.fixture
def mat_design(monkeypatch):
    """A configured design with three document materials, two bodies, and the body resolver stubbed."""
    d = _Design(configured=True,
                bodies={"Body1": FakeFeature("Body1"), "Body2": FakeFeature("Body2")},
                materials=("Steel", "ABS Plastic", "Aluminum"))
    _install(monkeypatch, d)
    monkeypatch.setattr(dc._BODY, "resolve",
                        lambda raw: (d._bodies.get(raw), None) if raw in d._bodies
                        else (None, f"No body named '{raw}'."))
    return d


def _mat_table(design):
    return design.configurationTopTable.materialTable


def _material_for(design, config, column_title):
    """What one column holds for one CONFIGURATION: follow the config's theme link to a row, then
    read that column's cell on it - the way a caller experiences the table."""
    mtbl = _mat_table(design)
    row = mtbl.parentTableColumn.getCellByRowName(config).referencedTableRow
    if row is None:
        return None
    col = next(c for c in mtbl.columns.added if c.title == column_title)
    return col.material_of(row.name)


class TestAddMaterial:
    def test_a_materials_map_arriving_as_json_text_is_parsed_not_char_iterated(self, mat_design):
        # An object-typed input can cross the wire as its JSON TEXT (a stale client schema does
        # this); iterating that string as a map sprays per-character unknown-configuration refusals.
        out = _payload(dc.handler(action="add_material", body="Body1",
                                  materials='{"Default": "Steel"}'))
        assert _material_for(mat_design, "Default", "Body1") == "Steel"
        assert out["materials"] == {"Default": "Steel"}

    def test_unparseable_map_text_is_refused_naming_the_field(self, mat_design):
        res = dc.handler(action="add_material", body="Body1", materials="not json {")
        assert res["isError"] is True
        assert "'materials'" in res["message"] and "JSON object" in res["message"]

    def test_the_material_lands_on_the_cell_addressed_by_theme_row_name(self, mat_design):
        # A column's getCell(index) runs over the COLUMN's own rowCount, not the table's row order,
        # so a positional write lands on a cell no configuration reads - and its own read-back
        # still passes, because that cell does hold the material.
        _payload(dc.handler(action="add_material", body="Body1", materials={"Default": "Steel"}))
        col = _mat_table(mat_design).columns.added[0]
        assert col.material_of("Theme 1") == "Steel"
        assert col._scratch == {}

    def test_links_each_config_to_its_own_theme_row_by_row_name(self, mat_design):
        dc.handler(action="add_configuration", name="Small")
        out = _payload(dc.handler(action="add_material", body="Body1",
                                  materials={"Default": "Steel", "Small": "ABS Plastic"}))
        mtbl = _mat_table(mat_design)
        # one theme row per configuration, no over-provisioning
        assert mtbl.rows.count == 2
        # each CONFIG links a theme row addressed BY NAME - a positional getCell() on the theme
        # column hands back an unrelated scratch cell, so an index-linked tool leaves these unset
        theme_col = mtbl.parentTableColumn
        ref_default = theme_col.getCellByRowName("Default").referencedTableRow
        ref_small = theme_col.getCellByRowName("Small").referencedTableRow
        assert ref_default is not None and ref_small is not None
        assert ref_default is not ref_small
        # and the theme row each config points at carries ITS material
        assert _material_for(mat_design, "Default", "Body1") == "Steel"
        assert _material_for(mat_design, "Small", "Body1") == "ABS Plastic"
        # the payload publishes what the CELLS read back, per configuration
        assert out["materials"] == {"Default": "Steel", "Small": "ABS Plastic"}
        assert out["themes"] == 2 and out["column_title"] == "Body1"
        assert "configurations_unset" not in out

    def test_a_second_body_does_not_re_point_the_first_bodys_configurations(self, mat_design):
        # theme rows and the config->theme link are TABLE-global: allocating rows positionally on the
        # second call silently moves configurations off the rows the first call gave them, changing
        # the first body's materials while every read-back of the second call still passes
        dc.handler(action="add_configuration", name="Small")
        _payload(dc.handler(action="add_material", body="Body1",
                            materials={"Default": "Steel", "Small": "ABS Plastic"}))
        out = _payload(dc.handler(action="add_material", body="Body2",
                                  materials={"Small": "Aluminum"}))
        # the first body keeps exactly what it was configured with
        assert _material_for(mat_design, "Default", "Body1") == "Steel"
        assert _material_for(mat_design, "Small", "Body1") == "ABS Plastic"
        # and the second body is Aluminum in Small ONLY - not in Default too
        assert _material_for(mat_design, "Small", "Body2") == "Aluminum"
        assert _material_for(mat_design, "Default", "Body2") != "Aluminum"
        assert out["materials"] == {"Small": "Aluminum"}
        assert out["configurations_unset"] == ["Default"]

    def test_a_configuration_moved_to_its_own_theme_row_keeps_the_other_bodys_material(self, mat_design):
        # 'Large' is added after the first column and starts out sharing 'Small's theme row, so it
        # shows Body1 as ABS Plastic. Setting Body2 for it has to mint a row and carry that across -
        # a minted row otherwise copies the row ABOVE it and Body1 silently becomes Steel.
        dc.handler(action="add_configuration", name="Small")
        _payload(dc.handler(action="add_material", body="Body1",
                            materials={"Default": "Steel", "Small": "ABS Plastic"}))
        dc.handler(action="add_configuration", name="Large")
        assert _material_for(mat_design, "Large", "Body1") == "ABS Plastic"
        _payload(dc.handler(action="add_material", body="Body2", materials={"Large": "Aluminum"}))
        assert _material_for(mat_design, "Large", "Body1") == "ABS Plastic"
        assert _material_for(mat_design, "Small", "Body1") == "ABS Plastic"
        assert _material_for(mat_design, "Default", "Body1") == "Steel"
        assert _material_for(mat_design, "Large", "Body2") == "Aluminum"

    def test_a_minted_theme_row_never_takes_a_name_the_table_already_carries(self, mat_design):
        # rows.add(<a name an existing row carries>) returns THAT row and adds nothing, so a
        # count-based name that collides would put two configurations on one row
        mtbl = _mat_table(mat_design)
        mtbl.rows.add("Material 2")          # the name the count-based scheme reaches for first
        dc.handler(action="add_configuration", name="Small")
        _payload(dc.handler(action="add_material", body="Body1",
                            materials={"Default": "Steel", "Small": "ABS Plastic"}))
        theme_col = mtbl.parentTableColumn
        ref_default = theme_col.getCellByRowName("Default").referencedTableRow
        ref_small = theme_col.getCellByRowName("Small").referencedTableRow
        assert ref_default is not None and ref_default is not ref_small
        assert _material_for(mat_design, "Default", "Body1") == "Steel"
        assert _material_for(mat_design, "Small", "Body1") == "ABS Plastic"

    def test_a_repeat_call_for_the_same_body_updates_its_existing_column(self, mat_design):
        # columns.add(<a body that already has a column>) returns the EXISTING column, so a second
        # call re-materials that body rather than building a duplicate column
        _payload(dc.handler(action="add_material", body="Body1", materials={"Default": "Steel"}))
        _payload(dc.handler(action="add_material", body="Body1", materials={"Default": "ABS Plastic"}))
        mtbl = _mat_table(mat_design)
        assert mtbl.columns.count == 2 and len(mtbl.columns.added) == 1
        assert _material_for(mat_design, "Default", "Body1") == "ABS Plastic"

    def test_a_carry_that_does_not_take_is_an_error(self, mat_design):
        # moving a configuration to its own theme row must bring every OTHER column's material with
        # it; a column that drops the copy leaves that configuration mis-materialled, so the call
        # fails naming the column instead of reporting success
        dc.handler(action="add_configuration", name="Small")
        _payload(dc.handler(action="add_material", body="Body1",
                            materials={"Default": "Steel", "Small": "ABS Plastic"}))
        dc.handler(action="add_configuration", name="Large")     # inherits Small's theme row
        mtbl = _mat_table(mat_design)
        root = mtbl.columns.item(0)                              # the root-component column
        root.getCellByRowName("Theme 1")._material = mat_design.materials.named("Steel")
        mtbl.silent_columns.add(root.title)
        res = dc.handler(action="add_material", body="Body2", materials={"Large": "Aluminum"})
        assert res["isError"] is True and root.title in res["message"]

    def test_configurations_left_out_of_the_map_are_published(self, mat_design):
        # every configuration starts on the one auto-created theme row, so an unnamed configuration
        # keeps another configuration's material - say so instead of implying it was set
        dc.handler(action="add_configuration", name="Small")
        dc.handler(action="add_configuration", name="Large")
        out = _payload(dc.handler(action="add_material", body="Body1", materials={"Small": "Steel"}))
        assert out["configurations_unset"] == ["Default", "Large"]
        assert "Default" in out["note"] and "Large" in out["note"]

    def test_dropped_theme_link_is_an_error(self, mat_design):
        # two configurations, so 'Default' has to MOVE to a theme row of its own: the link assignment
        # is silently ignored, leaving it on the shared row while the payload claims it was linked
        dc.handler(action="add_configuration", name="Small")
        _mat_table(mat_design).parentTableColumn.cell_mode = "silent"
        res = dc.handler(action="add_material", body="Body1",
                         materials={"Default": "Steel", "Small": "ABS Plastic"})
        assert res["isError"] is True and "theme link did not take" in res["message"]

    def test_theme_link_pointing_at_another_row_is_an_error(self, mat_design):
        theme_col = _mat_table(mat_design).parentTableColumn
        theme_col.cell_mode = "lies"
        theme_col.substitute = SimpleNamespace(name="Theme 9")
        res = dc.handler(action="add_material", body="Body1", materials={"Default": "Steel"})
        assert res["isError"] is True
        # the error names BOTH the row it landed on and the row it should have linked
        assert "Theme 9" in res["message"] and "Theme 1" in res["message"]

    def test_auto_root_column_does_not_fail_the_call(self, mat_design):
        # adding the first non-root column also mints a root-component column: the count lands on 2
        # after ONE add, and the call must not gate on it
        out = _payload(dc.handler(action="add_material", body="Body1", materials={"Default": "Steel"}))
        assert _mat_table(mat_design).columns.count == 2
        assert out["themes"] == 1

    def test_unconfirmed_readback_is_an_error(self, mat_design):
        # the cell silently keeps no material: reporting ok here would publish a configuration
        # carrying the wrong material
        _mat_table(mat_design).cell_mode = "silent"
        res = dc.handler(action="add_material", body="Body1", materials={"Default": "Steel"})
        assert res["isError"] is True and "could not be confirmed" in res["message"]

    def test_readback_of_a_different_material_is_an_error(self, mat_design):
        mtbl = _mat_table(mat_design)
        mtbl.cell_mode = "lies"
        mtbl.substitute = SimpleNamespace(name="Brass")
        res = dc.handler(action="add_material", body="Body1", materials={"Default": "Steel"})
        assert res["isError"] is True
        assert "Brass" in res["message"] and "Steel" in res["message"]

    def test_unknown_configuration_is_refused_naming_it(self, mat_design):
        res = dc.handler(action="add_material", body="Body1", materials={"Nonexistent": "Steel"})
        assert res["isError"] is True and "Nonexistent" in res["message"]
        assert _mat_table(mat_design).columns.count == 0

    def test_material_missing_from_the_design_is_refused_before_mutating(self, mat_design):
        dc.handler(action="add_configuration", name="Small")
        res = dc.handler(action="add_material", body="Body1",
                         materials={"Default": "Steel", "Small": "Unobtanium"})
        assert res["isError"] is True and "Unobtanium" in res["message"]
        # nothing was built: a half-populated theme table is exactly what the up-front resolve prevents
        mtbl = _mat_table(mat_design)
        assert mtbl.columns.count == 0 and mtbl.rows.count == 0

    def test_duplicate_document_material_name_is_refused(self, monkeypatch):
        d = _Design(configured=True, bodies={"Body1": FakeFeature("Body1")},
                    materials=("Steel", "Steel"))
        _install(monkeypatch, d)
        monkeypatch.setattr(dc._BODY, "resolve", lambda raw: (d._bodies.get(raw), None))
        res = dc.handler(action="add_material", body="Body1", materials={"Default": "Steel"})
        assert res["isError"] is True and "refusing to pick one" in res["message"]

    def test_empty_material_map_is_refused(self, mat_design):
        res = dc.handler(action="add_material", body="Body1", materials={})
        assert res["isError"] is True and "materials" in res["message"]
        assert _mat_table(mat_design).columns.count == 0

    def test_null_column_add_is_an_error(self, mat_design):
        _mat_table(mat_design).add_returns_null = True
        res = dc.handler(action="add_material", body="Body1", materials={"Default": "Steel"})
        assert res["isError"] is True and "null" in res["message"]


class TestAddMaterialRefusesBeforeAndDuringTheBuild:
    """The material build is a multi-step table mutation, so every step reads its result back.
    A step that answers nothing stops the call NAMING that step - the half-built table is never
    reported as a configured material."""

    def test_a_missing_body_is_refused(self, mat_design):
        res = dc.handler(action="add_material", materials={"Default": "Steel"})
        assert res["isError"] is True and "Provide 'body'" in res["message"]
        assert _mat_table(mat_design).columns.count == 0

    def test_an_unresolvable_body_is_refused_with_the_resolver_reason(self, mat_design):
        res = dc.handler(action="add_material", body="Ghost", materials={"Default": "Steel"})
        assert res["isError"] is True and "No body named 'Ghost'" in res["message"]
        assert _mat_table(mat_design).columns.count == 0

    def test_a_design_with_no_material_table_is_named(self, mat_design):
        mat_design.configurationTopTable.materialTable = None
        res = dc.handler(action="add_material", body="Body1", materials={"Default": "Steel"})
        assert res["isError"] is True and "no material table" in res["message"]

    def test_a_table_with_no_theme_column_is_named(self, mat_design):
        # the column exists from the first call, so this second one reaches the theme-column read
        _payload(dc.handler(action="add_material", body="Body1", materials={"Default": "Steel"}))
        _mat_table(mat_design).parentTableColumn = None
        res = dc.handler(action="add_material", body="Body1", materials={"Default": "Steel"})
        assert res["isError"] is True and "no theme column" in res["message"]

    def test_a_theme_row_that_will_not_add_is_an_error(self, mat_design):
        # 'Small' must be moved off the row it shares with 'Default', so it mints one - and the
        # mint answering null is the failure this names.
        dc.handler(action="add_configuration", name="Small")
        mtbl = _mat_table(mat_design)
        real_add = mtbl.rows.add
        mtbl.rows.add = lambda name: None if name.startswith("Material ") else real_add(name)
        res = dc.handler(action="add_material", body="Body1",
                         materials={"Default": "Steel", "Small": "ABS Plastic"})
        assert res["isError"] is True and "returned null" in res["message"]

    def test_a_theme_row_absent_from_the_table_after_adding_it_is_an_error(self, mat_design):
        # add() answered a row object, but the table never gained it: reporting the material
        # against a row nothing references is exactly the false ok this catches.
        dc.handler(action="add_configuration", name="Small")
        mtbl = _mat_table(mat_design)
        real_add = mtbl.rows.add
        mtbl.rows.add = (lambda name: SimpleNamespace(name=name)
                         if name.startswith("Material ") else real_add(name))
        res = dc.handler(action="add_material", body="Body1",
                         materials={"Default": "Steel", "Small": "ABS Plastic"})
        assert res["isError"] is True and "is not in the material table" in res["message"]

    def test_a_column_with_no_cell_at_the_theme_row_is_an_error(self, mat_design):
        mtbl = _mat_table(mat_design)
        real_add = mtbl.columns.add

        def add_cell_less_column(entity):
            col = real_add(entity)
            col.getCellByRowName = lambda name: None
            return col

        mtbl.columns.add = add_cell_less_column
        res = dc.handler(action="add_material", body="Body1", materials={"Default": "Steel"})
        assert res["isError"] is True and "No material cell on theme row 'Theme 1'" in res["message"]

    def test_a_configuration_with_no_theme_cell_is_an_error(self, mat_design):
        # the column and its theme row already exist; the LINK step is the one with no cell
        _payload(dc.handler(action="add_material", body="Body1", materials={"Default": "Steel"}))
        theme_col = _mat_table(mat_design).parentTableColumn
        real_by_name = theme_col.getCellByRowName
        calls = {"n": 0}

        def gone_by_the_link_step(name):
            calls["n"] += 1
            return None if calls["n"] > 1 else real_by_name(name)

        theme_col.getCellByRowName = gone_by_the_link_step
        res = dc.handler(action="add_material", body="Body1", materials={"Default": "Steel"})
        assert res["isError"] is True and "No theme cell for configuration 'Default'" in res["message"]


class TestMapInputParsing:
    """Every {name: value} input crosses the wire through one parser. A non-map is refused naming
    the field and its type - never iterated as characters, which sprays per-character refusals."""

    def test_a_blank_string_is_read_as_no_map_not_as_a_refusal(self, mat_design):
        # blank 'appearances' is simply absent, so the missing-map refusal is what answers
        res = dc.handler(action="add_material", body="Body1", materials="   ")
        assert res["isError"] is True and "Provide 'materials'" in res["message"]

    def test_json_text_holding_a_list_is_refused_naming_the_type(self, mat_design):
        res = dc.handler(action="add_material", body="Body1", materials='["Default", "Steel"]')
        assert res["isError"] is True
        assert "'materials'" in res["message"] and "got list" in res["message"]

    def test_a_non_string_non_map_value_is_refused_naming_the_type(self, mat_design):
        res = dc.handler(action="add_material", body="Body1", materials=7)
        assert res["isError"] is True
        assert "'materials'" in res["message"] and "got int" in res["message"]


# ── add_insert: nested configuration (insert a configured part, map per assembly config) ─────

class TestAddInsert:
    def _setup(self, monkeypatch):
        # an assembly design with two configs, and a configured part DataFile resolvable by name
        d = _install(monkeypatch, _Design(configured=True,
                             datafiles={"Bracket": _FakeDataFile("Bracket", ["Medium", "Small", "Large"])}))
        monkeypatch.setattr(dc, "_resolve_datafile",
                            lambda design, name: (d._datafiles.get(name), None))
        dc.handler(action="add_configuration", name="HeavyDuty")   # rows: Default, HeavyDuty
        return d

    def test_insert_and_map_each_config_by_name(self, monkeypatch):
        d = self._setup(monkeypatch)
        out = _payload(dc.handler(action="add_insert", insert_part="Bracket",
                                  insert_config="Medium",
                                  insert_map={"Default": "Medium", "HeavyDuty": "Large"}))
        root = d.rootComponent
        # the part was inserted via addFromConfiguration with the 'Medium' part row
        assert len(root.occurrences.inserted) == 1
        inserted_row, _ = root.occurrences.inserted[0]
        assert inserted_row.name == "Medium"
        # an insert column was added and each assembly config's cell .row is the RIGHT part row (by name)
        col = d.configurationTopTable.columns.added[-1]
        assert col.getCellByRowName("Default").row.name == "Medium"
        assert col.getCellByRowName("HeavyDuty").row.name == "Large"
        assert out["inserted_part"] == "Bracket" and out["mapped"] == 2

    def test_unknown_part_errors(self, monkeypatch):
        self._setup(monkeypatch)
        res = dc.handler(action="add_insert", insert_part="Ghost",
                         insert_map={"Default": "Medium"})
        assert res["isError"] is True and "Ghost" in res["message"]

    def test_map_to_unknown_part_config_errors(self, monkeypatch):
        self._setup(monkeypatch)
        res = dc.handler(action="add_insert", insert_part="Bracket",
                         insert_config="Medium",
                         insert_map={"Default": "Gigantic"})
        # a part config that doesn't exist must be reported, naming it
        assert res["isError"] is True and "Gigantic" in res["message"]

    def test_map_to_unknown_assembly_config_errors(self, monkeypatch):
        self._setup(monkeypatch)
        res = dc.handler(action="add_insert", insert_part="Bracket",
                         insert_config="Medium",
                         insert_map={"Nonexistent": "Medium"})
        assert res["isError"] is True and "Nonexistent" in res["message"]

    def test_insert_config_defaults_to_first_part_row(self, monkeypatch):
        d = self._setup(monkeypatch)
        _payload(dc.handler(action="add_insert", insert_part="Bracket",
                            insert_map={"Default": "Medium", "HeavyDuty": "Small"}))
        # no insert_config given -> inserts the part's first row (Medium)
        inserted_row, _ = d.rootComponent.occurrences.inserted[0]
        assert inserted_row.name == "Medium"

    def test_the_insert_names_the_read_that_proves_it_landed(self, monkeypatch):
        # MEASURED: this insert commits past the server's call timeout - the occurrence, the column
        # and both cells landed while the caller was told the handler was still running. The tool is
        # exempt from that timeout, and the note names the read for a caller who never sees it.
        self._setup(monkeypatch)
        out = _payload(dc.handler(action="add_insert", insert_part="Bracket",
                                  insert_config="Medium", insert_map={"Default": "Medium"}))
        assert "design_get(include=['configurations'])" in out["note"]
        assert dc.item.enforce_timeout is False


# ── activate: switch a configuration + surface a rebuild that breaks the timeline ─────────────

class _ActRow:
    def __init__(self, name, table):
        self.name = name
        self.id = "row-" + name
        self._table = table
    def activate(self):
        self._table._active = self          # a real switch updates the table's active row
        return True


class _ActTable:
    def __init__(self, names):
        self.rows = _NamedCollection([_ActRow(n, self) for n in names])
        self._active = self.rows.item(0)
    @property
    def activeRow(self):
        return self._active


def _act_timeline(n_errors, n_warnings=0):
    """A timeline with N features in error and N in warning - what _common.timeline_health reads."""
    rows = [FakeTimelineObject(name="F%d" % i, index=i, health=_ERROR) for i in range(n_errors)]
    rows += [FakeTimelineObject(name="W%d" % i, index=n_errors + i, health=_WARNING)
             for i in range(n_warnings)]
    return FakeTimeline(rows)


class _ActDesign(MakeDesign):
    """A configured design whose rebuild (computeAll) can flip features into error or warning, so the
    activate guard's before/after timeline_health comparison has something to catch."""
    def __init__(self, table, errors_before=0, errors_after=0, warnings_after=0):
        super().__init__()
        self._top = table
        self._before, self._after = errors_before, errors_after
        self._warn_after = warnings_after

    @property
    def configurationTopTable(self):
        return self._top

    @property
    def timeline(self):
        if self._computes:
            return _act_timeline(self._after, self._warn_after)
        return _act_timeline(self._before)


class TestActivate:
    def test_switches_configuration_clean(self, monkeypatch):
        d = _ActDesign(_ActTable(["Default", "Large"]))
        monkeypatch.setattr(dc._common, "design", lambda: d)
        out = _payload(dc.handler(action="activate", name="Large"))
        assert out["activated"] is True and out["now_active"] == "Large"
        assert out["previous"] == "Default"
        assert "timeline_warning" not in out

    def test_a_row_resolves_by_id_when_no_name_matches(self, monkeypatch):
        # add_configuration publishes the row's id beside its name, so an agent can hold that id;
        # a target matching no NAME has to reach the row whose id it is.
        d = _ActDesign(_ActTable(["Default", "Large"]))
        monkeypatch.setattr(dc._common, "design", lambda: d)
        out = _payload(dc.handler(action="activate", name="row-Large"))
        assert out["activated"] is True and out["now_active"] == "Large"

    def test_unknown_configuration_errors(self, monkeypatch):
        d = _ActDesign(_ActTable(["Default", "Large"]))
        monkeypatch.setattr(dc._common, "design", lambda: d)
        res = dc.handler(action="activate", name="Ghost")
        assert res["isError"] is True and "Ghost" in res["message"]

    def test_new_timeline_error_after_switch_is_surfaced(self, monkeypatch):
        # the rebuilt configuration over/under-constrains the model: the switch stands, but the new
        # timeline error must be reported rather than a clean success over a broken model.
        d = _ActDesign(_ActTable(["Default", "Large"]), errors_before=0, errors_after=1)
        monkeypatch.setattr(dc._common, "design", lambda: d)
        out = _payload(dc.handler(action="activate", name="Large"))
        assert out["activated"] is True
        assert "timeline_warning" in out and "new error" in out["timeline_warning"].lower()


# ── cells and rows that keep what they hold: the platform's silent no-op ─────
#
# Every column action assigns a cell value and reads it back. These stand-ins accept the assignment
# and keep their own value, which is the shape the read-back gate exists to catch.

class _StubbornRow:
    """A configuration row that accepts a name assignment and keeps its own name."""
    def __init__(self, name):
        self._name = name
        self.id = "row-" + name

    @property
    def name(self):
        return self._name

    @name.setter
    def name(self, value):
        pass


class _StubbornParamCell:
    def __init__(self):
        self._expr = "9 mm"

    @property
    def expression(self):
        return self._expr

    @expression.setter
    def expression(self, value):
        pass


class _StubbornSuppressCell:
    @property
    def isSuppressed(self):
        return False

    @isSuppressed.setter
    def isSuppressed(self, value):
        pass


class _StubbornVisibilityCell:
    @property
    def isVisible(self):
        return True

    @isVisible.setter
    def isVisible(self, value):
        pass


class _StubbornAppearanceCell:
    _held = SimpleNamespace(name="Chrome")

    @property
    def appearance(self):
        return self._held

    @appearance.setter
    def appearance(self, value):
        pass


class _StubbornInsertCell:
    _held = SimpleNamespace(name="Medium")

    @property
    def row(self):
        return self._held

    @row.setter
    def row(self, value):
        pass


def _null_column(table, adder):
    """Make the named column factory answer nothing, the way the API can."""
    setattr(table.columns, adder, lambda arg: None)


def _cell_less_column(table, adder):
    """Make the next column the named factory builds carry no cell for any row name."""
    real = getattr(table.columns, adder)

    def build(arg):
        col = real(arg)
        col.getCellByRowName = lambda name: None
        return col

    setattr(table.columns, adder, build)


def _cells_from(table, adder, factory):
    """Make the next column the named factory builds hand out `factory` cells."""
    real = getattr(table.columns, adder)

    def build(arg):
        col = real(arg)
        col.cell_factory = factory
        return col

    setattr(table.columns, adder, build)


@pytest.fixture
def col_design(monkeypatch):
    """A configured design with two configurations (Default, Small), a parameter, a body and a
    timeline feature, and the typed body/feature seams stubbed - the rig the column actions'
    refusals are driven through."""
    d = _Design(configured=True,
                params=[_Param("plate_len")],
                bodies={"Body1": FakeFeature("Body1")},
                features={"Fillet1": FakeFeature("Fillet1")})
    _install(monkeypatch, d)
    monkeypatch.setattr(dc._BODY, "resolve",
                        lambda raw: (d._bodies.get(raw), None) if raw in d._bodies
                        else (None, f"No body named '{raw}'."))
    monkeypatch.setattr(dc._FEATURE, "resolve",
                        lambda raw: ((d._features[raw], raw), None) if raw in d._features
                        else (None, f"'feature': no timeline feature named '{raw}'."))
    dc.handler(action="add_configuration", name="Small")
    return d


def _top(design):
    return design.configurationTopTable


# ── create: the refusals around the conversion itself ────────────────────────

class TestCreateRefusals:
    def test_a_conversion_that_yields_no_table_is_an_error(self, monkeypatch):
        # createConfiguredDesign answering nothing is a failed conversion; reporting ok would tell
        # the caller to start adding columns to a table that does not exist
        d = _install(monkeypatch, _Design(configured=False))
        d.createConfiguredDesign = lambda: None
        res = dc.handler(action="create")
        assert res["isError"] is True and "returned no table" in res["message"]

    def test_the_saved_gate_reads_the_active_documents_own_flag(self, monkeypatch):
        # the guard is only worth its wording if it reads isSaved off the active document
        d = _Design(configured=False)
        monkeypatch.setattr(dc._common, "design", lambda: d)
        monkeypatch.setattr(dc, "app",
                            SimpleNamespace(activeDocument=SimpleNamespace(isSaved=False)))
        res = dc.handler(action="create")
        assert res["isError"] is True and "Save the document first" in res["message"]
        assert d.created is None


# ── activate: the refusals a switch can hit ─────────────────────────────────

class TestActivateRefusals:
    def _design(self, monkeypatch, table=None):
        d = _ActDesign(_ActTable(["Default", "Large"]) if table is None else table)
        monkeypatch.setattr(dc._common, "design", lambda: d)
        return d

    def test_activate_without_a_name_is_refused(self, monkeypatch):
        self._design(monkeypatch)
        res = dc.handler(action="activate", name="   ")
        assert res["isError"] is True and "Provide 'name'" in res["message"]

    def test_an_activate_answering_false_is_an_error(self, monkeypatch):
        d = self._design(monkeypatch)
        d.configurationTopTable.rows.item(1).activate = lambda: False
        res = dc.handler(action="activate", name="Large")
        assert res["isError"] is True and "activate() returned false" in res["message"]

    def test_an_active_row_that_did_not_move_is_an_error(self, monkeypatch):
        # activate() answering true is not proof the switch took - the table's own activeRow is
        d = self._design(monkeypatch)
        d.configurationTopTable.rows.item(1).activate = lambda: True
        res = dc.handler(action="activate", name="Large")
        assert res["isError"] is True
        assert "still reads 'Default'" in res["message"] and "expected 'Large'" in res["message"]

    def test_timeline_warnings_after_the_switch_are_published(self, monkeypatch):
        # a rebuild that only WARNS still leaves the switch standing, so the warnings ride along in
        # the success payload rather than being reported as a new error
        d = _ActDesign(_ActTable(["Default", "Large"]), warnings_after=2)
        monkeypatch.setattr(dc._common, "design", lambda: d)
        out = _payload(dc.handler(action="activate", name="Large"))
        assert out["activated"] is True and out["timeline_warnings"] == ["W0", "W1"]
        assert "timeline_warning" not in out

    def test_a_table_with_no_readable_rows_refuses_instead_of_crashing(self, monkeypatch):
        self._design(monkeypatch, table=SimpleNamespace())
        res = dc.handler(action="activate", name="Large")
        assert res["isError"] is True
        assert "No configuration matched 'Large'" in res["message"] and "(none)" in res["message"]

    def test_a_rows_collection_that_will_not_iterate_refuses(self, monkeypatch):
        self._design(monkeypatch, table=SimpleNamespace(rows=object()))
        res = dc.handler(action="activate", name="Large")
        assert res["isError"] is True and "No configuration matched 'Large'" in res["message"]


# ── add_configuration / rename_configuration: the row-level refusals ────────

class TestAddConfigurationRefusals:
    def test_a_duplicate_configuration_name_is_refused(self, monkeypatch):
        # two rows sharing a name would make every by-name cell address ambiguous
        _install(monkeypatch, _Design(configured=True))
        res = dc.handler(action="add_configuration", name="Default")
        assert res["isError"] is True and "already exists" in res["message"]

    def test_a_row_add_that_answers_nothing_is_an_error(self, monkeypatch):
        d = _install(monkeypatch, _Design(configured=True))
        _top(d).rows.add = lambda name: None
        res = dc.handler(action="add_configuration", name="Large")
        assert res["isError"] is True and "Adding configuration 'Large' failed" in res["message"]


class TestRenameConfigurationRefusals:
    def test_a_rename_that_does_not_take_is_an_error(self, monkeypatch):
        # the row keeps its old name: reporting ok would send the caller addressing cells by a name
        # the table does not carry
        d = _install(monkeypatch, _Design(configured=True))
        _top(d).rows._items[0] = _StubbornRow("Default")
        res = dc.handler(action="rename_configuration", name="Default", new_name="Medium")
        assert res["isError"] is True and "did not take" in res["message"]


# ── add_parameter: the refusals around the parameter column ────────────────

class TestAddParameterRefusals:
    def test_a_missing_parameter_name_is_refused(self, col_design):
        res = dc.handler(action="add_parameter", parameter="", values={"Default": "5 mm"})
        assert res["isError"] is True and "Provide 'parameter'" in res["message"]

    def test_a_null_parameter_column_is_an_error(self, col_design):
        _null_column(_top(col_design), "addParameterColumn")
        res = dc.handler(action="add_parameter", parameter="plate_len", values={"Default": "5 mm"})
        assert res["isError"] is True
        assert "addParameterColumn for 'plate_len' returned null" in res["message"]

    def test_a_column_with_no_cell_for_a_configuration_is_an_error(self, col_design):
        _cell_less_column(_top(col_design), "addParameterColumn")
        res = dc.handler(action="add_parameter", parameter="plate_len", values={"Default": "5 mm"})
        assert res["isError"] is True
        assert "No cell for configuration 'Default' in the 'plate_len' column" in res["message"]

    def test_an_expression_that_does_not_take_is_an_error(self, col_design):
        _cells_from(_top(col_design), "addParameterColumn", _StubbornParamCell)
        res = dc.handler(action="add_parameter", parameter="plate_len", values={"Default": "50 mm"})
        assert res["isError"] is True
        assert "reads '9 mm'" in res["message"]
        assert "'50 mm' did not verifiably take" in res["message"]
        assert "param_set after activating the row" in res["message"]

    def test_a_text_parameter_is_refused_before_the_column_lands(self, col_design):
        # MEASURED LIVE: a text parameter reads unit 'Text', and a shipped configured sample varies
        # such a column per row, its cells reading the expression quoted ('40') and the value only
        # on textValue - a form this handler neither sets nor verifies, so it refuses up front.
        table = _top(col_design)
        p = col_design.allParameters.itemByName("plate_len")
        p.unit = "Text"
        res = dc.handler(action="add_parameter", parameter="plate_len",
                         values={"Default": "'50'"})
        assert res["isError"] is True
        assert "is a Text parameter" in res["message"]
        assert "param_set after activating the row" in res["message"]
        assert table.columns.added == []          # nothing was mutated at all

    def test_the_column_is_rolled_back_when_a_cell_will_not_take(self, col_design):
        # the COLUMN is a mutation that already landed; leaving it would give every retry another
        # orphan column on the table
        _cells_from(_top(col_design), "addParameterColumn", _StubbornParamCell)
        res = dc.handler(action="add_parameter", parameter="plate_len", values={"Default": "50 mm"})
        assert res["isError"] is True and "The column has been rolled back" in res["message"]
        assert _top(col_design).columns.added == []

    def test_a_rollback_that_did_not_take_is_disclosed_not_claimed(self, col_design):
        # deleteMe answers true while the table still lists the column - only the read-back after it
        # tells the caller an orphan is there to delete
        table = _top(col_design)
        _cells_from(table, "addParameterColumn", _StubbornParamCell)
        real = table.columns.addParameterColumn

        def build(p):
            col = real(p)
            col.delete_lands = False
            return col

        table.columns.addParameterColumn = build
        res = dc.handler(action="add_parameter", parameter="plate_len", values={"Default": "50 mm"})
        assert res["isError"] is True
        assert "could NOT be auto-removed" in res["message"]
        assert "delete it before retrying" in res["message"]
        assert len(table.columns.added) == 1


# ── add_suppress: the refusals around the suppress column ──────────────────

class TestAddSuppressRefusals:
    def test_a_missing_feature_name_is_refused(self, col_design):
        res = dc.handler(action="add_suppress", feature="", suppressed_in=["Small"])
        assert res["isError"] is True and "Provide 'feature'" in res["message"]

    def test_suppressed_in_naming_an_unknown_configuration_is_refused(self, col_design):
        res = dc.handler(action="add_suppress", feature="Fillet1", suppressed_in=["Ghost"])
        assert res["isError"] is True
        assert "suppressed_in names unknown configurations: Ghost" in res["message"]
        assert _top(col_design).columns.added == []      # nothing mutated on a refusal

    def test_a_null_suppress_column_is_an_error(self, col_design):
        _null_column(_top(col_design), "addSuppressColumn")
        res = dc.handler(action="add_suppress", feature="Fillet1", suppressed_in=["Small"])
        assert res["isError"] is True
        assert "addSuppressColumn for 'Fillet1' returned null" in res["message"]

    def test_a_column_with_no_suppress_cell_is_an_error(self, col_design):
        _cell_less_column(_top(col_design), "addSuppressColumn")
        res = dc.handler(action="add_suppress", feature="Fillet1", suppressed_in=["Small"])
        assert res["isError"] is True
        assert "No suppress cell for configuration 'Small'" in res["message"]

    def test_a_suppression_that_does_not_take_is_an_error(self, col_design):
        _cells_from(_top(col_design), "addSuppressColumn", _StubbornSuppressCell)
        res = dc.handler(action="add_suppress", feature="Fillet1", suppressed_in=["Small"])
        assert res["isError"] is True and "does not read suppressed" in res["message"]


# ── add_visibility: the refusals around the visibility column ──────────────

class TestAddVisibilityRefusals:
    def test_a_missing_body_is_refused(self, col_design):
        res = dc.handler(action="add_visibility", hidden_in=["Small"])
        assert res["isError"] is True and "Provide 'body'" in res["message"]

    def test_an_unresolvable_body_is_refused_with_the_resolver_reason(self, col_design):
        res = dc.handler(action="add_visibility", body="Ghost", hidden_in=["Small"])
        assert res["isError"] is True and "No body named 'Ghost'" in res["message"]
        assert _top(col_design).columns.added == []

    def test_hidden_in_naming_an_unknown_configuration_is_refused(self, col_design):
        res = dc.handler(action="add_visibility", body="Body1", hidden_in=["Ghost"])
        assert res["isError"] is True
        assert "hidden_in names unknown configurations: Ghost" in res["message"]
        assert _top(col_design).columns.added == []

    def test_a_null_visibility_column_is_an_error(self, col_design):
        _null_column(_top(col_design), "addVisibilityColumn")
        res = dc.handler(action="add_visibility", body="Body1", hidden_in=["Small"])
        assert res["isError"] is True
        assert "addVisibilityColumn for 'Body1' returned null" in res["message"]

    def test_a_column_with_no_visibility_cell_is_an_error(self, col_design):
        _cell_less_column(_top(col_design), "addVisibilityColumn")
        res = dc.handler(action="add_visibility", body="Body1", hidden_in=["Small"])
        assert res["isError"] is True
        assert "No visibility cell for configuration 'Small'" in res["message"]

    def test_a_hide_that_does_not_take_is_an_error(self, col_design):
        _cells_from(_top(col_design), "addVisibilityColumn", _StubbornVisibilityCell)
        res = dc.handler(action="add_visibility", body="Body1", hidden_in=["Small"])
        assert res["isError"] is True and "does not read hidden" in res["message"]


# ── set_appearance: the refusals along the theme build ─────────────────────

class TestSetAppearanceRefusals:
    def _named(self, monkeypatch, **named):
        monkeypatch.setattr(dc, "_resolve_appearance",
                            lambda design, name: named.get(name))

    def test_a_missing_body_is_refused(self, col_design):
        res = dc.handler(action="set_appearance", appearances={"Default": "Red"})
        assert res["isError"] is True and "Provide 'body'" in res["message"]

    def test_an_unresolvable_body_is_refused_with_the_resolver_reason(self, col_design):
        res = dc.handler(action="set_appearance", body="Ghost", appearances={"Default": "Red"})
        assert res["isError"] is True and "No body named 'Ghost'" in res["message"]

    def test_an_appearance_map_naming_an_unknown_configuration_is_refused(self, col_design):
        res = dc.handler(action="set_appearance", body="Body1", appearances={"Ghost": "Red"})
        assert res["isError"] is True
        assert "Appearance map references unknown configurations: Ghost" in res["message"]
        assert _top(col_design).appearanceTable._columns_added == []

    def test_an_appearance_absent_from_the_design_is_refused_before_mutating(self, col_design):
        # the design's OWN appearances collection is what the resolver reads - a library appearance
        # has to be copied in first, so a name it does not hold is refused
        col_design.appearances = SimpleNamespace(itemByName=lambda n: None)
        res = dc.handler(action="set_appearance", body="Body1", appearances={"Default": "Ghost"})
        assert res["isError"] is True and "No appearance named 'Ghost'" in res["message"]
        assert _top(col_design).appearanceTable._columns_added == []

    def test_the_missing_appearance_error_names_a_base_the_caller_can_reach(self, col_design):
        # The example base has to be one this server actually produces: appearance_set copies the
        # Fusion Appearance Library's 'Paint - Enamel Glossy (White)' into the document as
        # 'MCP Neutral Base'. Naming a base the caller cannot find sends it looking for nothing.
        col_design.appearances = SimpleNamespace(itemByName=lambda n: None)
        msg = dc.handler(action="set_appearance", body="Body1",
                         appearances={"Default": "Ghost"})["message"]
        assert "Powder" not in msg
        assert "Paint - Enamel Glossy (White)" in msg and "MCP Neutral Base" in msg

    def test_an_appearance_present_in_the_design_resolves(self, col_design):
        held = SimpleNamespace(name="Red")
        col_design.appearances = SimpleNamespace(
            itemByName=lambda n: held if n == "Red" else None)
        assert dc._resolve_appearance(col_design, "Red") is held

    def test_a_design_with_no_appearance_table_is_named(self, col_design, monkeypatch):
        self._named(monkeypatch, Red=SimpleNamespace(name="Red"))
        _top(col_design).appearanceTable = None
        res = dc.handler(action="set_appearance", body="Body1", appearances={"Default": "Red"})
        assert res["isError"] is True and "no appearance table" in res["message"]

    def test_a_null_appearance_column_is_an_error(self, col_design, monkeypatch):
        self._named(monkeypatch, Red=SimpleNamespace(name="Red"))
        _top(col_design).appearanceTable.add = lambda body: None
        res = dc.handler(action="set_appearance", body="Body1", appearances={"Default": "Red"})
        assert res["isError"] is True
        assert "appearanceTable.columns.add for 'Body1' returned null" in res["message"]

    def test_an_appearance_table_with_no_theme_column_is_named(self, col_design, monkeypatch):
        self._named(monkeypatch, Red=SimpleNamespace(name="Red"))
        _top(col_design).appearanceTable.parentTableColumn = None
        res = dc.handler(action="set_appearance", body="Body1", appearances={"Default": "Red"})
        assert res["isError"] is True and "no theme column" in res["message"]

    def test_a_column_with_no_appearance_cell_is_an_error(self, col_design, monkeypatch):
        self._named(monkeypatch, Red=SimpleNamespace(name="Red"))
        appt = _top(col_design).appearanceTable
        real_add = appt.add

        def cell_less(body):
            col = real_add(body)
            col.getCellByRowName = lambda name: None
            return col

        appt.add = cell_less
        res = dc.handler(action="set_appearance", body="Body1", appearances={"Default": "Red"})
        assert res["isError"] is True
        assert "No appearance cell/row on theme row 'Theme 1'" in res["message"]

    def test_a_cell_reading_back_another_appearance_is_an_error(self, col_design, monkeypatch):
        self._named(monkeypatch, Red=SimpleNamespace(name="Red"))
        appt = _top(col_design).appearanceTable
        real_add = appt.add

        def stubborn(body):
            col = real_add(body)
            col.cell_factory = _StubbornAppearanceCell
            return col

        appt.add = stubborn
        res = dc.handler(action="set_appearance", body="Body1", appearances={"Default": "Red"})
        assert res["isError"] is True and "reads 'Chrome'" in res["message"]

    def test_a_configuration_with_no_theme_cell_is_an_error(self, col_design, monkeypatch):
        self._named(monkeypatch, Red=SimpleNamespace(name="Red"))
        _top(col_design).appearanceTable.parentTableColumn.getCellByRowName = lambda name: None
        res = dc.handler(action="set_appearance", body="Body1", appearances={"Default": "Red"})
        assert res["isError"] is True
        assert "No theme cell for configuration 'Default'" in res["message"]

    def test_a_theme_link_pointing_at_another_row_is_an_error(self, col_design, monkeypatch):
        self._named(monkeypatch, Red=SimpleNamespace(name="Red"))
        theme_col = _top(col_design).appearanceTable.parentTableColumn
        theme_col.cell_mode = "lies"
        theme_col.substitute = SimpleNamespace(name="Theme 9")
        res = dc.handler(action="set_appearance", body="Body1", appearances={"Default": "Red"})
        assert res["isError"] is True and "links theme 'Theme 9'" in res["message"]


class _SilentDropCell:
    """A cell that accepts any write and reads back nothing - the platform's silently-dropped
    assignment. An unreadable read-back is a FAILURE (the material path's stated rule, shared by
    every configuration writer)."""
    def __setattr__(self, name, value):
        pass

    def __getattr__(self, name):
        return None


class TestSilentDropRefusals:
    def test_a_dropped_parameter_expression_is_an_error(self, col_design):
        _cells_from(_top(col_design), "addParameterColumn", _SilentDropCell)
        res = dc.handler(action="add_parameter", parameter="plate_len", values={"Default": "50 mm"})
        assert res["isError"] is True and "did not verifiably take" in res["message"]

    def test_a_dropped_suppression_is_an_error(self, col_design):
        _cells_from(_top(col_design), "addSuppressColumn", _SilentDropCell)
        res = dc.handler(action="add_suppress", feature="Fillet1", suppressed_in=["Small"])
        assert res["isError"] is True and "does not read suppressed" in res["message"]

    def test_a_dropped_hide_is_an_error(self, col_design):
        _cells_from(_top(col_design), "addVisibilityColumn", _SilentDropCell)
        res = dc.handler(action="add_visibility", body="Body1", hidden_in=["Small"])
        assert res["isError"] is True and "does not read hidden" in res["message"]

    def test_a_dropped_appearance_assignment_is_an_error(self, col_design, monkeypatch):
        monkeypatch.setattr(dc, "_resolve_appearance",
                            lambda design, name: SimpleNamespace(name="Red"))
        appt = _top(col_design).appearanceTable
        real_add = appt.add

        def dropping(body):
            col = real_add(body)
            col.cell_factory = _SilentDropCell
            return col

        appt.add = dropping
        res = dc.handler(action="set_appearance", body="Body1", appearances={"Default": "Red"})
        assert res["isError"] is True and "reads nothing" in res["message"]

    def test_a_dropped_theme_link_is_an_error(self, col_design, monkeypatch):
        monkeypatch.setattr(dc, "_resolve_appearance",
                            lambda design, name: SimpleNamespace(name="Red"))
        _top(col_design).appearanceTable.parentTableColumn.cell_mode = "silent"
        res = dc.handler(action="set_appearance", body="Body1", appearances={"Default": "Red"})
        assert res["isError"] is True and "did not verifiably take" in res["message"]


# ── add_material: the two name-resolution refusals ─────────────────────────

class TestMaterialNameResolution:
    def test_a_blank_material_name_is_refused_naming_the_field(self, mat_design):
        res = dc.handler(action="add_material", body="Body1", materials={"Default": ""})
        assert res["isError"] is True
        assert "Provide a material name for every configuration in 'materials'" in res["message"]
        assert _mat_table(mat_design).columns.count == 0

    def test_a_nameless_document_material_is_not_offered_as_a_candidate(self, monkeypatch):
        # a material whose name is unreadable is skipped, so the miss message lists real names
        # instead of an empty quote pair the caller cannot ask for
        d = _Design(configured=True, bodies={"Body1": FakeFeature("Body1")},
                    materials=("", "Steel"))
        _install(monkeypatch, d)
        monkeypatch.setattr(dc._BODY, "resolve", lambda raw: (d._bodies.get(raw), None))
        res = dc.handler(action="add_material", body="Body1", materials={"Default": "Unobtanium"})
        assert res["isError"] is True and "'Steel'" in res["message"]
        assert "''" not in res["message"]


# ── add_insert: the refusals along the nested-configuration build ──────────

class TestAddInsertRefusals:
    def _setup(self, monkeypatch, datafile=None):
        part = datafile if datafile is not None else _FakeDataFile("Bracket", ["Medium", "Large"])
        d = _install(monkeypatch, _Design(configured=True, datafiles={"Bracket": part}))
        monkeypatch.setattr(dc, "_resolve_datafile",
                            lambda design, name: (d._datafiles.get(name), None))
        return d

    def test_a_missing_insert_part_is_refused(self, monkeypatch):
        self._setup(monkeypatch)
        res = dc.handler(action="add_insert", insert_part="")
        assert res["isError"] is True and "Provide 'insert_part'" in res["message"]

    def test_a_part_that_is_not_a_configured_design_is_refused(self, monkeypatch):
        part = _FakeDataFile("Bracket", ["Medium"])
        part.isConfiguredDesign = False
        d = self._setup(monkeypatch, part)
        res = dc.handler(action="add_insert", insert_part="Bracket")
        assert res["isError"] is True and "is not a configured design" in res["message"]
        assert d.rootComponent.occurrences.inserted == []

    def test_a_part_exposing_no_configuration_rows_is_refused(self, monkeypatch):
        part = _FakeDataFile("Bracket", ["Medium"])
        part.configurationTable = None
        self._setup(monkeypatch, part)
        res = dc.handler(action="add_insert", insert_part="Bracket")
        assert res["isError"] is True and "exposes no configuration rows" in res["message"]

    def test_an_insert_config_the_part_does_not_carry_is_refused(self, monkeypatch):
        d = self._setup(monkeypatch)
        res = dc.handler(action="add_insert", insert_part="Bracket", insert_config="Gigantic")
        assert res["isError"] is True
        assert "insert_config 'Gigantic' is not a configuration of 'Bracket'" in res["message"]
        assert d.rootComponent.occurrences.inserted == []

    def test_an_insert_that_yields_no_occurrence_is_an_error(self, monkeypatch):
        d = self._setup(monkeypatch)
        d.rootComponent.occurrences.addFromConfiguration = lambda row, transform: None
        res = dc.handler(action="add_insert", insert_part="Bracket")
        assert res["isError"] is True and "returned no occurrence" in res["message"]

    def test_a_null_insert_column_is_an_error(self, monkeypatch):
        d = self._setup(monkeypatch)
        _null_column(_top(d), "addInsertColumn")
        res = dc.handler(action="add_insert", insert_part="Bracket",
                         insert_map={"Default": "Medium"})
        assert res["isError"] is True and "addInsertColumn returned null" in res["message"]

    def test_a_column_with_no_insert_cell_is_an_error(self, monkeypatch):
        d = self._setup(monkeypatch)
        _cell_less_column(_top(d), "addInsertColumn")
        res = dc.handler(action="add_insert", insert_part="Bracket",
                         insert_map={"Default": "Medium"})
        assert res["isError"] is True
        assert "No insert cell for assembly configuration 'Default'" in res["message"]

    def test_a_mapping_that_does_not_take_is_an_error(self, monkeypatch):
        # the cell keeps the part configuration it already selected, so the nested mapping the
        # payload would claim is not the one the table holds
        d = self._setup(monkeypatch)
        _cells_from(_top(d), "addInsertColumn", _StubbornInsertCell)
        res = dc.handler(action="add_insert", insert_part="Bracket",
                         insert_map={"Default": "Large"})
        assert res["isError"] is True
        assert "still selects part configuration 'Medium'" in res["message"]


# ── _resolve_datafile: urn first, then a name in the active document's project ────────

class TestDataFileResolution:
    """Data.activeProject RAISES on this build, so a by-name insert resolves through the active
    document's own project - and says which read failed when it cannot."""

    def _no_urn(self, monkeypatch):
        monkeypatch.setattr(dc._data_common, "_resolve_data_file", lambda ref: (None, None, []))

    def _project_holding(self, monkeypatch, *files, problem=None):
        root = FakeDataFolder("Root", is_root=True, files=list(files))
        proj = FakeDataProject("Home", project_id="p-1", root_folder=root)
        monkeypatch.setattr(dc._data_common, "active_project",
                            lambda: ((None, problem) if problem else (proj, None)))
        return proj

    def test_a_urn_resolves_through_the_shared_decoder(self, monkeypatch):
        # the shared decoder also base64url-decodes the lineage segment of a pasted share URL, so
        # the urn path never re-rolls a startswith('urn:') test
        wanted = _FakeDataFile("Bracket", ["Medium"])
        monkeypatch.setattr(dc._data_common, "_resolve_data_file",
                            lambda ref: (wanted, ref, [ref]))
        assert dc._resolve_datafile(_Design(), "urn:adsk.wipprod:dm.lineage:abc") == (wanted, None)

    def test_a_name_resolves_in_the_active_documents_own_project(self, monkeypatch):
        self._no_urn(monkeypatch)
        wanted = _FakeDataFile("Bracket", ["Medium"])
        self._project_holding(monkeypatch, _FakeDataFile("Plate", ["Medium"]), wanted)
        assert dc._resolve_datafile(_Design(), "Bracket") == (wanted, None)
        assert dc._resolve_datafile(_Design(), "Missing") == (None, None)

    def test_a_project_that_cannot_be_found_is_the_reported_reason(self, monkeypatch):
        self._no_urn(monkeypatch)
        self._project_holding(monkeypatch, problem="this hub lists no project named 'Home'.")
        df, problem = dc._resolve_datafile(_Design(), "Bracket")
        assert df is None and problem == "this hub lists no project named 'Home'."

    def test_a_name_two_files_in_the_project_share_is_refused(self, monkeypatch):
        # two saveAs calls under one name make two DISTINCT lineages, so the shared resolver
        # refuses rather than inserting whichever comes first.
        self._no_urn(monkeypatch)
        self._project_holding(monkeypatch, _FakeDataFile("Bracket", ["Medium"]),
                              _FakeDataFile("Bracket", ["Large"]))
        df, problem = dc._resolve_datafile(_Design(), "Bracket")
        assert df is None and "names 2 files" in problem

    def test_the_reason_reaches_the_add_insert_refusal(self, monkeypatch):
        _install(monkeypatch, _Design(configured=True))
        self._no_urn(monkeypatch)
        self._project_holding(monkeypatch, problem="the active document reports no cloud project.")
        res = dc.handler(action="add_insert", insert_part="Bracket")
        assert res["isError"] is True
        assert "the active document reports no cloud project." in res["message"]
        assert "Could not find a configured part 'Bracket'" in res["message"]

    def test_an_unreadable_sibling_name_reaches_the_handler_refusal(self, monkeypatch):
        class _BlindName(_FakeDataFile):
            @property
            def name(self):
                raise RuntimeError("3 : file name unreadable")

        design = _Design(configured=True)
        _install(monkeypatch, design)
        self._no_urn(monkeypatch)
        project = self._project_holding(monkeypatch, _FakeDataFile("Bracket", ["Medium"]))
        project.rootFolder._files.append(_BlindName("Hidden", ["Large"]))
        before = design.rootComponent.occurrences.count
        res = dc.handler(action="add_insert", insert_part="Bracket")
        assert res["isError"] is True and "name(s) were unreadable" in res["message"]
        assert design.rootComponent.occurrences.count == before
