# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""The drawing world: sheets, views, sketches, images, the create input and the exporters."""

import sys
import types

import live_api_facts as _api_facts

from tests.fakes.data_docs import FakeApplication, _ExportOptions
from tests.fakes.scaffold import _NamedCollection, _StrictEnum, fusion_fake


# The eleven document-side types come off a CLASS dump: adsk.core.DocumentTypes carries no drawing
# member and createDrawing mints a cloud file, so the row had no drawing INSTANCE to read.

_DRAWING_FAMILIES = tuple(sorted(k.split(".", 1)[1] for k in _api_facts.ENUMS
                                 if k.startswith("drawing.")))

# family key -> the member NAME a drawing tool spells it with; every value is read from the
# measured family, so only the spelling is written here.
_DRAWING_UNITS = {"mm": "MillimeterDrawingUnitType", "in": "InchDrawingUnitType"}
_DRAWING_STANDARDS = {"iso": "ISODrawingStandardType", "asme": "ASMEDrawingStandardType"}

_STALE_PROXY_READ = "4 : An API Object refers to a deleted Object"
_SHEET_NAME_TAKEN = "3 : A sheet with that name already exists."
# the orientation a sheet's extents are tabulated in; the other one swaps width and height.
_LANDSCAPE = _api_facts.ENUMS["drawing.SheetOrientationTypes"]["LandscapeSheetOrientationType"]


def drawing_enum(family, keep=None):
    """One measured adsk.drawing enum family as a _StrictEnum, so a member outside it raises the way
    the live enum does. `keep` narrows it to the named members - the build-lacks-this-member state a
    resolver has to refuse on rather than assign the None its read answers."""
    members = _api_facts.ENUMS["drawing." + family]
    return _StrictEnum("drawing." + family,
                       {k: v for k, v in members.items() if keep is None or k in keep})


def drawing_value(family, table, key):
    """The measured value of the member `table` spells for `key`. A key the table does not carry is
    handed back UNCHANGED - how a test asks for a setting whose value is outside the family."""
    member = table.get(key)
    return _api_facts.ENUMS["drawing." + family][member] if member else key


def install_drawing(monkeypatch, **families):
    """Install a COMPLETE stand-in adsk.drawing - the module object AND its sys.modules entry, so a
    sibling drawing test file that swapped that module cannot decide this one's result. It carries
    every measured enum family plus a DrawingDocument whose cast answers a FakeDrawingDocument and
    None for any other document. `families` adds or replaces one family."""
    ns = types.ModuleType("adsk.drawing")
    for family in _DRAWING_FAMILIES:
        setattr(ns, family, drawing_enum(family))
    for name, members in families.items():
        setattr(ns, name, members)
    ns.DrawingDocument = types.SimpleNamespace(
        cast=lambda doc: doc if isinstance(doc, FakeDrawingDocument) else None)
    monkeypatch.setattr(sys.modules["adsk"], "drawing", ns, raising=False)
    monkeypatch.setitem(sys.modules, "adsk.drawing", ns)
    return ns


class _IndexedRaiser(_NamedCollection):
    """A counted collection whose item() throws at ONE index - the stale-proxy shape. Not the same
    answer as a collection that will not enumerate at all: the count still reads, so the walk keeps
    every later entry at its own address."""
    def __init__(self, items=(), at=None, message=_STALE_PROXY_READ):
        super().__init__(items)
        self._at = at
        self._message = message

    def item(self, i):
        if self._at is not None and i == self._at:
            raise RuntimeError(self._message)
        return super().item(i)


@fusion_fake(live_type="DocumentSettings", facts=("shape-dump-drawing-world",))
class FakeDocumentSettings:
    """A drawing's own settings: `units` is the DIMENSION display unit and `standard` is what fixes
    the coordinate unit. drawing_create mints the two independently, so they are set independently
    here and a value outside the measured family is an unreadable setting."""
    def __init__(self, units=None, standard=None):
        self.units = units
        self.standard = standard


@fusion_fake(live_type="View", facts=("shape-dump-drawing-world",))
class FakeView:
    """One drawing View: `type` is the only readable fact it carries - no name, scale or position -
    and viewCurves is populated with items exposing no readable geometry."""
    def __init__(self, view_type=None, curves=()):
        self.type = view_type
        self.viewCurves = _NamedCollection(list(curves))


@fusion_fake(live_type="Views", facts=("shape-dump-drawing-world",))
class FakeViews:
    """A sheet's views: count and item(i), and nothing else - live Views carries no itemByName, so a
    view is addressed by index alone."""
    def __init__(self, views=()):
        self._views = list(views)

    @property
    def count(self):
        return len(self._views)

    def item(self, i):
        return _NamedCollection(self._views).item(i)


@fusion_fake(factory_for="FakeViews")
def make_drawing_views(views):
    """A FakeViews from a count, a sequence of views, or one already built."""
    if isinstance(views, FakeViews):
        return views
    if isinstance(views, int):
        return FakeViews([FakeView() for _ in range(views)])
    return FakeViews(list(views))


class _DrawnCurves:
    """One DrawingSketch curve collection: `count` - the only readable fact the API offers - plus
    the add factory its own kind carries. The sketch owns the recording, so a factory that hands
    back an entity while the count stands still is one knob on the sketch."""
    def __init__(self, sketch, key, count=0):
        self._sketch = sketch
        self._key = key
        self.count = count

    def add(self, *args):
        if self._key == "lines":
            # measured: Lines.add takes a LIST and draws a CHAIN - N points make N-1 entities.
            return self._sketch._draw("lines", max(len(args[0]) - 1, 0), (list(args[0]),))
        return self._sketch._draw(self._key, 1, tuple(args))

    def addTwoPointRectangle(self, a, b):
        return self._sketch._draw(self._key, 1, (a, b))

    def addByThreePoints(self, a, b, c):
        return self._sketch._draw(self._key, 1, (a, b, c))

    def addByCenterRadius(self, centre, radius):
        return self._sketch._draw(self._key, 1, (centre, radius))


@fusion_fake(live_type="DrawingSketch", facts=("shape-dump-drawing-world",))
class FakeDrawingSketch:
    """One sheet sketch. A created entity exposes nothing readable, so each collection's `count` is
    the only read-back there is - and one call hands back ONE entity however many curves it drew.
    `refuses`/`silent`/`nothing`/`raises_after` name the collections whose factory throws before
    drawing, grows nothing while handing an entity back, returns None, and throws AFTER landing."""
    def __init__(self, name="DrwSketch", counts=None, refuses=(), silent=(), nothing=(),
                 raises_after=(), refusal="the sheet is locked"):
        self.name = name
        self._drawn = []
        self._refuses = set(refuses)
        self._silent = set(silent)
        self._nothing = set(nothing)
        self._raises_after = set(raises_after)
        self._refusal = refusal
        held = dict(counts or {})
        self.lines = _DrawnCurves(self, "lines", held.get("lines", 0))
        self.rectangles = _DrawnCurves(self, "rectangles", held.get("rectangles", 0))
        self.arcs = _DrawnCurves(self, "arcs", held.get("arcs", 0))
        self.circles = _DrawnCurves(self, "circles", held.get("circles", 0))
        self.ellipses = _DrawnCurves(self, "ellipses", held.get("ellipses", 0))

    def _draw(self, key, grew, args):
        self._drawn.append((key, args))
        if key in self._refuses:
            raise RuntimeError(self._refusal)
        if key in self._nothing:
            return None
        if key not in self._silent:
            getattr(self, key).count += grew
        if key in self._raises_after:
            raise RuntimeError(self._refusal)
        return types.SimpleNamespace(objectType="adsk::drawing::" + key)

    def deleteMe(self):
        return True


@fusion_fake(live_type="DrawingSketches", facts=("shape-dump-drawing-world",))
class FakeDrawingSketches:
    """A sheet's sketches: add() records the name it was ASKED for and hands back `sketch`, whose
    own name is what a caller can address later; `landed_name` is that name when it differs.
    `returns_nothing` is the add that answers None."""
    def __init__(self, sketch=None, sketches=(), landed_name=None, returns_nothing=False):
        self._sketch = sketch
        self._items = list(sketches)
        self._landed_name = landed_name
        self._returns_nothing = returns_nothing
        self._requested = []

    def add(self, name=None):
        self._requested.append(name)
        if self._returns_nothing:
            return None
        made = self._sketch if self._sketch is not None else FakeDrawingSketch()
        made.name = self._landed_name or name or made.name
        self._items.append(made)
        return made

    @property
    def count(self):
        return len(self._items)

    def item(self, i):
        return _NamedCollection(self._items).item(i)

    def itemByName(self, name):
        return _NamedCollection(self._items).itemByName(name)


def _image_insert_input():
    """A fresh ImageInsertInput: the four properties an insert writes, at the defaults they read
    back from. ImageInsertInput carries no shape dump, so this is a bag, not a stand-in."""
    return types.SimpleNamespace(imageFilePath="", position=None, scale=None, rotationAngle=0.0)


@fusion_fake(live_type="Images", facts=("shape-dump-drawing-world",))
class FakeImages:
    """A sheet's images: createInput and insert, and NOTHING else - live Images carries no count,
    item or delete, so a placed image can be neither read back nor removed. `input_factory` builds
    the input each createInput hands out; `insert_ok` is what insert() answers, and `modifies` is
    whether it flips the owning document to modified."""
    def __init__(self, sheet=None, input_factory=None, insert_ok=True, modifies=True):
        self._sheet = sheet
        self._input_factory = input_factory
        self._insert_ok = insert_ok
        self._modifies = modifies
        self._inputs = []
        self._inserts = []

    def createInput(self):
        made = (self._input_factory or _image_insert_input)()
        self._inputs.append(made)
        return made

    def insert(self, image_input):
        self._inserts.append(image_input)
        document = getattr(self._sheet, "_document", None)
        if self._modifies and document is not None:
            document.isModified = True
        return self._insert_ok


def _auto_dimension_input():
    """A fresh AutoDimensionInput: the three properties a dimensioning call writes. AutoDimensionInput
    carries no shape dump, so this is a bag, not a stand-in."""
    return types.SimpleNamespace(view=None, dimensionStrategy=None, datumLocation=None)


@fusion_fake(live_type="Sheet", facts=("shape-dump-drawing-world",))
class FakeSheet:
    """One sheet. width/height are READ-ONLY millimetres on every drawing, derived from size plus
    orientation through `extents` ({size value: its landscape (w, h)}) or standing as the plain pair
    given; a duplicate name assignment silently no-ops; size/orientation assignments raise the way
    Fusion does; and tidyUp is a property whose READ performs the tidy."""
    def __init__(self, name="Sheet1", size=None, orientation=None, extents=None,
                 width=None, height=None, width_raises=None, views=0, sketches=None,
                 custom_tables=0, images=None, custom_size=None, custom_size_raises=None,
                 rename_lands=True, size_raises=None, size_ignored=False,
                 orientation_raises=None, orientation_ignored=False, delete_ok=True,
                 tidy_ok=True, auto_dimension_ok=True, auto_dimension_input=None,
                 modifies=True, copy_result="ok"):
        self._name = name
        self._size = size
        self._orientation = orientation
        self._extents = dict(extents or {})
        self._width, self._height = width, height
        self._width_raises = width_raises
        self._custom_size = custom_size
        self._custom_size_raises = custom_size_raises
        self._rename_lands = rename_lands
        self._size_raises, self._size_ignored = size_raises, size_ignored
        self._orientation_raises = orientation_raises
        self._orientation_ignored = orientation_ignored
        self._delete_ok, self._tidy_ok = delete_ok, tidy_ok
        self._auto_ok = auto_dimension_ok
        self._auto_factory = auto_dimension_input
        self._modifies = modifies
        self._copy_result = copy_result
        self._sheets = None
        self._document = None
        self._size_sets = self._orientation_sets = self._tidy_reads = 0
        self._deleted = False
        self._copy_args = []
        self._auto_calls = []
        self._auto_input = None
        self.views = make_drawing_views(views)
        self.sketches = FakeDrawingSketches() if sketches is None else sketches
        self.customTables = types.SimpleNamespace(count=custom_tables)
        self.images = FakeImages(self) if images is None else images

    @property
    def name(self):
        return self._name

    @name.setter
    def name(self, value):
        if self._rename_lands:
            self._name = value

    @property
    def sheetSize(self):
        return self._size

    @sheetSize.setter
    def sheetSize(self, value):
        self._size_sets += 1
        if self._size_raises:
            raise RuntimeError(self._size_raises)
        if not self._size_ignored:
            self._size = value

    @property
    def orientation(self):
        return self._orientation

    @orientation.setter
    def orientation(self, value):
        self._orientation_sets += 1
        if self._orientation_raises:
            raise RuntimeError(self._orientation_raises)
        if not self._orientation_ignored:
            self._orientation = value

    def _extent(self):
        if self._size in self._extents:
            wide, tall = self._extents[self._size]
            if self._orientation is not None and self._orientation != _LANDSCAPE:
                return tall, wide
            return wide, tall
        return self._width, self._height

    @property
    def width(self):
        if self._width_raises:
            raise RuntimeError(self._width_raises)
        return self._extent()[0]

    @property
    def height(self):
        return self._extent()[1]

    @property
    def customSize(self):
        if self._custom_size_raises:
            raise RuntimeError(self._custom_size_raises)
        return self._custom_size

    @property
    def tidyUp(self):
        self._tidy_reads += 1
        if self._modifies and self._document is not None:
            self._document.isModified = True
        return self._tidy_ok

    def deleteMe(self):
        # the collection deliberately does NOT shrink: a drawing delete is invisible in its own call
        self._deleted = True
        return self._delete_ok

    def copy(self, name=None, before=False):
        self._copy_args.append((name, before))
        if self._copy_result == "null":
            return None
        made = FakeSheet(name or (self._name + " copy"), size=self._size,
                         orientation=self._orientation, extents=self._extents,
                         width=self._width, height=self._height, views=self.views.count,
                         sketches=FakeDrawingSketches(
                             sketches=[FakeDrawingSketch() for _ in range(self.sketches.count)]),
                         custom_tables=self.customTables.count)
        if self._copy_result != "no_growth" and self._sheets is not None:
            self._sheets._land(made)
        return made

    def createAutoDimensionInput(self):
        self._auto_input = (self._auto_factory or _auto_dimension_input)()
        return self._auto_input

    def autoDimension(self, dimension_input):
        self._auto_calls.append(dimension_input)
        if self._modifies and self._document is not None:
            self._document.isModified = True
        return self._auto_ok


@fusion_fake(live_type="Sheets", facts=("shape-dump-drawing-world",))
class FakeSheets:
    """The drawing's sheets: the POSITIONAL walk whose 1-based index is a sheet's export address,
    plus createInput/add/itemByName. An ADD lands DIRECTLY AFTER the active sheet and a COPY lands
    LAST, so the export indices shift on an add, and a name a sheet already holds RAISES.
    `unreadable` names the sheets whose item() read throws; `add_result` is 'null' for an add that
    returns nothing, 'no_growth' for one that never joins, 'renamed' for one reporting its own."""
    def __init__(self, sheets=(), add_result="ok", activates=True, unreadable=()):
        self._items = []
        self._drawing = None
        self._document = None
        self._add_result = add_result
        self._activates = activates
        self._unreadable = set(unreadable)
        for sheet in sheets:
            self._land(sheet, activate=False)

    def _land(self, sheet, at=None, activate=True):
        self._items.insert(len(self._items) if at is None else at, sheet)
        sheet._sheets = self
        sheet._document = self._document
        if activate and self._drawing is not None:
            self._drawing.activeSheet = sheet

    @property
    def count(self):
        return len(self._items)

    def item(self, i):
        sheet = _NamedCollection(self._items).item(i)
        if sheet is not None and sheet.name in self._unreadable:
            raise RuntimeError(_STALE_PROXY_READ)
        return sheet

    def itemByName(self, name):
        return _NamedCollection(self._items).itemByName(name)

    def createInput(self):
        # SheetInput carries no shape dump; the name is the one property an add reads off it.
        return types.SimpleNamespace(name="")

    def add(self, sheet_input):
        if self._add_result == "null":
            return None
        wanted = getattr(sheet_input, "name", "") or ""
        if any(s.name == wanted for s in self._items):
            raise RuntimeError(_SHEET_NAME_TAKEN)
        active = getattr(self._drawing, "activeSheet", None)
        made = FakeSheet(wanted or "Sheet%d" % (len(self._items) + 1),
                         size=getattr(active, "_size", None),
                         orientation=getattr(active, "_orientation", None),
                         extents=getattr(active, "_extents", None))
        if self._add_result == "renamed":
            made._name = wanted + " (2)"
        if self._add_result == "no_growth":
            made._document = self._document
            return made
        at = self._items.index(active) + 1 if active in self._items else None
        self._land(made, at=at, activate=self._activates)
        return made


@fusion_fake(live_type="DrawingExportManager", facts=("shape-dump-drawing-world",))
class FakeDrawingExportManager:
    """drawing.exportManager: the PDF/DXF/DWG option factories - its OWN, not the fusion
    ExportManager's - and the execute() every drawing export goes through. execute() writes a stub
    file at the options' path unless `writes` is False, then answers `execute_ok`; the bool and the
    file are separate knobs. `drops`/`seeded` reach the options bag, which is where an assignment
    that is swallowed while the read-back keeps the factory default is modelled."""
    def __init__(self, execute_ok=True, writes=True, raises=None, drops=(), seeded=None):
        self._execute_ok = execute_ok
        self._writes = writes
        self._raises = raises
        self._drops = tuple(drops)
        self._seeded = dict(seeded or {})
        self._calls = []
        self._factory_used = None
        self._opts = None

    def _opt(self, kind, path, factory):
        self._factory_used = factory
        if self._raises:
            raise self._raises
        self._opts = _ExportOptions(kind, path, drops=self._drops, seeded=self._seeded)
        self._calls.append(self._opts)
        return self._opts

    def createPDFExportOptions(self, path):
        return self._opt("pdf", path, "createPDFExportOptions")

    def createDXFExportOptions(self, path):
        return self._opt("dxf", path, "createDXFExportOptions")

    def createDWGExportOptions(self, path):
        return self._opt("dwg", path, "createDWGExportOptions")

    def execute(self, opts):
        if self._writes:
            with open(opts.path, "w") as fh:
                fh.write("DRAWING-STUB")
        return self._execute_ok


@fusion_fake(live_type="CustomSheetSize", facts=("shape-dump-drawing-world",))
class FakeCustomSheetSize:
    """A custom sheet's extents: width/height as unitless numbers in the DOCUMENT unit, plus the two
    zone counts. The width and height are any positive pair and the zones start at the measured
    minimum of 2 - shape-dump-drawing-world records that PREDICATE of a fresh CreateDrawingInput's
    customSize (positive extents, at least two zones each way), not these particular numbers.
    `ignores` is the SWIG proxy that ACCEPTS an assignment and keeps its own value, the shape only a
    read-back catches; `_writes` records every assignment attempted AFTER construction, which
    separates a value left alone from one rewritten to the number it already held."""
    def __init__(self, width=420.0, height=297.0, horizontal_zones=2, vertical_zones=2,
                 ignores=False):
        object.__setattr__(self, "_ignores", False)
        object.__setattr__(self, "_writes", [])
        self.width = float(width)
        self.height = float(height)
        self.horizontalZones = int(horizontal_zones)
        self.verticalZones = int(vertical_zones)
        object.__setattr__(self, "_ignores", bool(ignores))
        self.__dict__["_writes"].clear()

    def __setattr__(self, name, value):
        self.__dict__["_writes"].append((name, value))
        if self.__dict__.get("_ignores"):
            return
        object.__setattr__(self, name, value)


@fusion_fake(live_type="CreateDrawingInput", facts=("shape-dump-drawing-world",))
class FakeCreateDrawingInput:
    """What createDrawingInput answers: the flat settings a create is configured through, plus the
    automationPreferences node (no shape dump - a caller supplies it). customSize hands out a FRESH
    CustomSheetSize on every READ, so a mutation never assigned BACK leaves the input carrying the
    default; what was assigned is what a later read returns. `custom_absent` is the input answering
    no customSize at all and `custom_raises` the message its assignment throws with."""
    def __init__(self, automation_preferences=None, custom_ignores=False, custom_absent=False,
                 custom_raises=None):
        self.automationPreferences = automation_preferences
        self.standard = None
        self.units = None
        self.content = None
        self.sheetSize = None
        self.orientationType = None
        self.sheetCreationType = None
        self.baseDocumentType = None
        self.templateFile = None
        self._custom_ignores = custom_ignores
        self._custom_absent = custom_absent
        self._custom_raises = custom_raises
        self._custom = None
        self._custom_assignments = []

    @property
    def customSize(self):
        if self._custom_absent:
            return None
        if self._custom is not None:
            return self._custom
        return FakeCustomSheetSize(ignores=self._custom_ignores)

    @customSize.setter
    def customSize(self, value):
        if self._custom_raises:
            raise RuntimeError(self._custom_raises)
        self._custom_assignments.append(value)
        self._custom = value


@fusion_fake(live_type="DrawingManager", facts=("shape-dump-drawing-world",))
class FakeDrawingManager:
    """The adsk.drawing.DrawingManager singleton: get() answers the manager itself, so the fake goes
    onto the stand-in module as DrawingManager. createDrawingInput records the (source file, mode)
    it was called with and hands back `create_input`; createDrawing records the input and answers
    `result` - the DataFile of the drawing it made, None for a create that made nothing."""
    def __init__(self, create_input=None, result=None, input_raises=None, create_raises=None):
        self._input = FakeCreateDrawingInput() if create_input is None else create_input
        self._result = result
        self._input_raises = input_raises
        self._create_raises = create_raises
        self._source = None
        self._mode = None
        self._created_with = None

    def get(self):
        return self

    def createDrawingInput(self, data_file, mode):
        if self._input_raises:
            raise RuntimeError(self._input_raises)
        self._source, self._mode = data_file, mode
        return self._input

    def createDrawing(self, drawing_input):
        if self._create_raises:
            raise RuntimeError(self._create_raises)
        self._created_with = drawing_input
        return self._result


@fusion_fake(live_type="Drawing", facts=("shape-dump-drawing-world",))
class FakeDrawing:
    """The Drawing product a drawing document answers: its sheets and the activeSheet an omitted
    sheet name resolves to, documentSettings, and the exportManager the three file formats come
    off."""
    def __init__(self, sheets=None, active=0, settings=None, export_manager=None):
        self.sheets = FakeSheets() if sheets is None else sheets
        self.sheets._drawing = self
        items = self.sheets._items
        self.activeSheet = items[active] if items and active is not None else None
        self.documentSettings = settings
        self.exportManager = export_manager
        self.parentDocument = None


@fusion_fake(live_type="DrawingDocument", facts=("shape-dump-drawing-world",))
class FakeDrawingDocument:
    """A drawing DOCUMENT. Its member list is a CLASS dump - DocumentTypes carries no drawing member
    and createDrawing would mint a cloud file - so no measured instance stands behind this surface.
    documentReferences serves `references` until updateAllReferences has run and `references_after`
    after it; `settle_reads` is how many reads past the refresh still serve the stale rows."""
    def __init__(self, name="Widget Drawing", drawing=None, is_modified=False,
                 modified_raises=None, is_up_to_date=True, data_file=None, references=(),
                 references_after=None, settle_reads=0, unreadable_at=None,
                 references_raise=None, update_ok=True, update_raises=None):
        self._name = name
        self.drawing = drawing
        self.dataFile = data_file
        self.isUpToDate = is_up_to_date
        self._modified = is_modified
        self._modified_raises = modified_raises
        self._before = list(references)
        self._after = list(references) if references_after is None else list(references_after)
        self._settle_reads = settle_reads
        self._unreadable_at = unreadable_at
        self._references_raise = references_raise
        self._update_ok = update_ok
        self._update_raises = update_raises
        self._updated = False
        self._reads_after_update = 0
        self._update_calls = 0
        sheets = getattr(drawing, "sheets", None)
        if drawing is not None:
            drawing.parentDocument = self
        if sheets is not None:
            sheets._document = self
            for sheet in sheets._items:
                sheet._document = self

    @property
    def name(self):
        return self._name

    @property
    def isModified(self):
        if self._modified_raises:
            raise RuntimeError(self._modified_raises)
        return self._modified

    @isModified.setter
    def isModified(self, value):
        self._modified = value

    @property
    def documentReferences(self):
        if self._references_raise:
            raise RuntimeError(self._references_raise)
        if not self._updated:
            return _IndexedRaiser(self._before, at=self._unreadable_at)
        self._reads_after_update += 1
        settled = self._reads_after_update > self._settle_reads
        return _IndexedRaiser(self._after if settled else self._before, at=self._unreadable_at)

    def updateAllReferences(self):
        self._update_calls += 1
        if self._update_raises:
            raise self._update_raises
        self._updated = True
        return self._update_ok


@fusion_fake(factory_for="FakeDrawingDocument")
def make_drawing(sheets=(), active=0, standard="iso", units="mm", name="Widget Drawing",
                 export_manager=None, **document):
    """An active drawing DOCUMENT over `sheets`, its dimension display unit and its standard named
    by the family's own keys - drawing_create mints the two independently, so a key outside the
    tables lands as the unreadable setting it is. `document` reaches FakeDrawingDocument."""
    settings = FakeDocumentSettings(
        units=drawing_value("DrawingUnitTypes", _DRAWING_UNITS, units),
        standard=drawing_value("DrawingStandardTypes", _DRAWING_STANDARDS, standard))
    collection = sheets if isinstance(sheets, FakeSheets) else FakeSheets(sheets)
    drawing = FakeDrawing(sheets=collection, active=active, settings=settings,
                          export_manager=export_manager)
    return FakeDrawingDocument(name=name, drawing=drawing, **document)


@fusion_fake(factory_for="FakeApplication")
def make_drawing_session(monkeypatch, document, **families):
    """The whole seam a drawing tool reads through: a stand-in adsk.drawing installed wholesale, and
    `document` as the ACTIVE document on adsk.core.Application.get - the ONE read the family's cast
    goes through. Answers the Application, so a test can swap the active document mid-test."""
    install_drawing(monkeypatch, **families)
    session = FakeApplication(active_document=document)
    monkeypatch.setattr(sys.modules["adsk"].core.Application, "get", lambda: session)
    return session
