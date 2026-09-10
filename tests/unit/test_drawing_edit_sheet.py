"""Unit tests for ``drawing_edit_sheet.py`` - the active drawing's sheet lifecycle.

The shared sheet fake carries the measured contract of the surface, and each clause of it is a way
this tool can report a change that did not happen:
  - a duplicate sheet name RAISES on add and is a SILENT NO-OP on rename (sheet names are
    case-insensitively unique), so the name a sheet reports is the only name worth publishing;
  - a sheet size belonging to the other drawing standard RAISES, as does portrait on the largest
    sheet of a standard - and a raise inside a drawing document is not reliably rolled back, so
    both are refused before anything is set;
  - width and height are read-only and derive from size + orientation, so they are the evidence a
    resize really landed;
  - deleteMe returns true while the sheet collection still reports its pre-delete count - the
    boolean IS the effect, and an unchanged count is never a failure;
  - Sheet.tidyUp is a property whose READ tidies the sheet, and on an already-modified document the
    modified flag cannot confirm it;
  - an ADDED sheet lands directly after the ACTIVE sheet while a COPY lands last, so the 1-based
    export indices the payload publishes shift on an add.
"""

import pytest

import adsk  # the mock package conftest installed at import time
import live_api_facts
from conftest import (FakeDrawingSketch, FakeDrawingSketches, FakeSheet, FakeSheets, drawing_enum,
                      error_message, load_tool, make_drawing, make_drawing_session, payload)

es = load_tool("drawing_edit_sheet")

# The MEASURED SheetSizes / SheetOrientationTypes value spaces, read from the generated facts file.
# Both are numbered from 0, so each has a member whose value is FALSY - Landscape and
# CustomSizeSheetSize - which is why the fake carries their INTS and not readable sentinels: a
# truthiness guard anywhere on these paths refuses a legitimate landscape sheet.
_SIZES = live_api_facts.ENUMS["drawing.SheetSizes"]
_ORIENTATIONS = live_api_facts.ENUMS["drawing.SheetOrientationTypes"]
_CUSTOM = _SIZES["CustomSizeSheetSize"]
_A4, _A3, _A2, _A1, _A0 = (_SIZES[k] for k in
                           ("A4ISOSheetSize", "A3ISOSheetSize", "A2ISOSheetSize",
                            "A1ISOSheetSize", "A0ISOSheetSize"))
_A, _B, _C, _D, _E = (_SIZES[k] for k in
                      ("AASMESheetSize", "BASMESheetSize", "CASMESheetSize",
                       "DASMESheetSize", "EASMESheetSize"))
_LAND = _ORIENTATIONS["LandscapeSheetOrientationType"]
_PORT = _ORIENTATIONS["PortraitSheetOrientationType"]

# sheet size -> the landscape (width, height) a sheet derives, the way Fusion derives its read-only
# width/height from size + orientation. Every pair is in MILLIMETRES, including the ASME ones: an
# ASME B sheet (17 x 11 in) reads 431.8 x 279.4 and an ASME E sheet reads 863.6 x 1117.6 -
# Sheet.width/height are mm on every drawing.
_SIZE_EXTENT = {_CUSTOM: (500.0, 333.0), _A4: (297.0, 210.0), _A3: (420.0, 297.0),
                _A2: (594.0, 420.0), _A0: (1189.0, 841.0), _B: (431.8, 279.4),
                _C: (558.8, 431.8), _E: (863.6, 1117.6)}

_SIZE_REFUSAL = "3 : Sheet size is not valid for the active drawing standard."
_PORTRAIT_REFUSAL = "3 : Portrait orientation is not supported for ISO A0 sheet size."


def sheet(name, size=_A3, orientation=_LAND, sketches_count=0, **knobs):
    """One sheet at this file's defaults, deriving its read-only extent through the measured table."""
    return FakeSheet(name, size=size, orientation=orientation, extents=_SIZE_EXTENT,
                     sketches=FakeDrawingSketches(
                         sketches=[FakeDrawingSketch() for _ in range(sketches_count)]),
                     **knobs)


@pytest.fixture
def wire(monkeypatch):
    """Install a drawing document as the active document; return its live state."""
    def _install(sheets=None, standard="iso", units="mm", active=0, is_drawing=True,
                 add_result="ok", modified=False, copy_activates=True, unreadable=()):
        objs = list(sheets or [sheet("Sheet1")])
        collection = FakeSheets(objs, add_result=add_result, activates=copy_activates,
                                unreadable=unreadable)
        document = make_drawing(sheets=collection, active=active, standard=standard, units=units,
                               is_modified=modified)
        session = make_drawing_session(monkeypatch, document if is_drawing else object())
        monkeypatch.setattr(es, "app", session)
        return type("State", (), {"drawing": document.drawing, "sheets": collection._items,
                                  "doc": document, "collection": collection})()
    return _install


class TestGuards:
    def test_non_drawing_active_document_is_refused(self, wire):
        wire(is_drawing=False)
        assert "not a drawing" in error_message(es.handler(action="add"))

    def test_unknown_action_lists_the_vocabulary(self, wire):
        wire()
        msg = error_message(es.handler(action="reorder"))
        assert "tidy_up" in msg and "reorder" in msg

    def test_missing_action_is_refused(self, wire):
        wire()
        assert "action" in error_message(es.handler())


class TestSheetTargeting:
    def test_unknown_name_lists_the_drawing_sheets(self, wire):
        wire([sheet("Front"), sheet("Detail")])
        msg = error_message(es.handler(action="tidy_up", sheet="Sction"))
        assert "Front" in msg and "Detail" in msg

    def test_name_matches_case_insensitively_and_exactly(self, wire):
        state = wire([sheet("Front", views=2), sheet("Front Detail", views=3)])
        out = payload(es.handler(action="tidy_up", sheet="front"))
        assert out["sheet"] == "Front" and out["views"] == 2
        assert state.sheets[1]._tidy_reads == 0

    def test_omitted_sheet_acts_on_the_active_sheet(self, wire):
        wire([sheet("Front"), sheet("Detail")], active=1)
        assert payload(es.handler(action="tidy_up"))["sheet"] == "Detail"


class TestAdd:
    def test_new_sheet_inherits_the_active_sheet_and_becomes_active(self, wire):
        state = wire([sheet("Front", size=_A2, orientation=_PORT)])
        out = payload(es.handler(action="add", new_name="Detail"))
        assert out["sheet"] == "Detail"
        assert out["sheet_count_before"] == 1 and out["sheet_count"] == 2
        assert out["facts"]["sheet_size"] == "a2" and out["facts"]["orientation"] == "portrait"
        assert out["facts"]["width"] == 420.0 and out["facts"]["height"] == 594.0
        assert out["sheet_units"] == "mm"
        assert state.drawing.activeSheet.name == "Detail"

    def test_a_duplicate_name_carries_the_platform_refusal(self, wire):
        # measured: Sheets.add with a name a sheet already holds RAISES - there is no dedupe
        state = wire([sheet("Front")])
        msg = error_message(es.handler(action="add", new_name="Front"))
        assert "already exists" in msg
        assert len(state.sheets) == 1

    def test_the_published_name_is_the_one_the_sheet_reports(self, wire):
        # whatever name the created sheet reports is the one a caller can address later, so the
        # payload publishes THAT - never the string the call asked for
        state = wire([sheet("Front")], add_result="renamed")
        out = payload(es.handler(action="add", new_name="Detail"))
        assert out["sheet"] == "Detail (2)" == state.sheets[-1].name
        assert out["requested_name"] == "Detail"
        assert "Detail (2)" in out["name_warning"]

    def test_add_returning_nothing_is_an_error(self, wire):
        wire(add_result="null")
        assert "returned nothing" in error_message(es.handler(action="add"))

    def test_add_that_did_not_grow_the_drawing_is_an_error(self, wire):
        wire(add_result="no_growth")
        assert "did not take" in error_message(es.handler(action="add", new_name="Detail"))


class TestCopy:
    def test_copy_publishes_what_the_copy_carries_and_where_it_landed(self, wire):
        state = wire([sheet("Front", sketches_count=1, custom_tables=2, views=4)])
        out = payload(es.handler(action="copy", sheet="Front", new_name="Front Copy"))
        assert out["copied_from"] == "Front" and out["sheet"] == "Front Copy"
        # the facts are read off the COPY, not off the sheet it was copied from
        assert out["facts"]["name"] == "Front Copy"
        assert out["facts"]["sketches"] == 1 and out["facts"]["custom_tables"] == 2
        assert out["sheet_count"] == 2 and state.drawing.activeSheet.name == "Front Copy"

    def test_a_copy_carries_the_source_sheets_settings_not_the_active_sheets(self, wire):
        # a copy takes the SOURCE sheet's size and orientation - only an ADD inherits the active
        # sheet's, so the payload and its note must not claim otherwise
        wire([sheet("Front", size=_A2, orientation=_PORT), sheet("Detail", size=_A4)], active=1)
        out = payload(es.handler(action="copy", sheet="Front", new_name="Front Copy"))
        assert out["facts"]["sheet_size"] == "a2" and out["facts"]["orientation"] == "portrait"
        assert "SOURCE" in out["note"] and "not the active sheet's" in out["note"]

    def test_the_facts_come_from_the_sheet_copy_returned(self, wire):
        # the object copy() handed back is the one to read - not whichever sheet the drawing
        # happens to report as active afterwards
        wire([sheet("Front", size=_A2, sketches_count=1)], copy_activates=False)
        out = payload(es.handler(action="copy", sheet="Front", new_name="Front Copy"))
        assert out["sheet"] == "Front Copy" and out["facts"]["name"] == "Front Copy"

    def test_the_copy_call_pins_the_measured_argument_shape(self, wire):
        # This pins the call arguments, without inferring a PDF page from collection order.
        state = wire([sheet("Front")])
        payload(es.handler(action="copy", sheet="Front", new_name="Front Copy"))
        assert state.sheets[0]._copy_args == [("Front Copy", False)]

    def test_null_copy_names_the_async_cause(self, wire):
        wire([sheet("Front", copy_result="null")])
        msg = error_message(es.handler(action="copy", sheet="Front"))
        assert "asynchronously" in msg and "returned nothing" in msg

    def test_copy_that_did_not_grow_the_drawing_is_an_error(self, wire):
        wire([sheet("Front", copy_result="no_growth")])
        message = error_message(es.handler(action="copy", sheet="Front"))
        assert "unverified" in message and "before retrying" in message


class TestDelete:
    def test_unchanged_count_is_reported_not_failed(self, wire):
        # measured: deleteMe returns true while the collection still reports its pre-delete count -
        # the delete must NOT be called a failure, and the count must NOT be sold as a verification
        state = wire([sheet("Front"), sheet("Detail")])
        out = payload(es.handler(action="delete", sheet="Detail"))
        assert out["deleted"] is None and out["sheet"] == "Detail"
        assert out["delete_accepted"] is True and out["verification"] == "pending"
        assert out["sheet_count_before"] == 2 and out["sheet_count_still_reads"] == 2
        assert "neither is a verification" in out["note"] and "later call" in out["note"]
        # the sheets it STILL reports include the deleted one - the payload says so rather than
        # publishing a list that looks like a post-delete state
        assert [s["name"] for s in out["sheets_still_read"]] == ["Front", "Detail"]
        assert state.sheets[1]._deleted is True

    def test_refused_delete_is_an_error(self, wire):
        wire([sheet("Front"), sheet("Detail", delete_ok=False)])
        assert "refused" in error_message(es.handler(action="delete", sheet="Detail"))

    def test_the_only_sheet_is_not_deleted(self, wire):
        state = wire([sheet("Front")])
        assert "only sheet" in error_message(es.handler(action="delete", sheet="Front"))
        assert state.sheets[0]._deleted is False


class TestRename:
    def test_rename_reads_the_name_back(self, wire):
        state = wire([sheet("Sheet1")])
        out = payload(es.handler(action="rename", sheet="Sheet1", new_name="Front"))
        assert out["sheet"] == "Front" and out["previous_name"] == "Sheet1"
        assert out["changed"] is True
        assert state.sheets[0].name == "Front"

    def test_a_silently_ignored_rename_is_an_error(self, wire):
        # measured: assigning a name another sheet holds (or a case variant) no-ops without raising
        state = wire([sheet("Front", rename_lands=False), sheet("Detail")])
        msg = error_message(es.handler(action="rename", sheet="Front", new_name="Detail"))
        assert "did not take" in msg and "case-insensitively unique" in msg
        assert state.sheets[0].name == "Front"

    def test_same_name_is_a_no_op(self, wire):
        wire([sheet("Front")])
        out = payload(es.handler(action="rename", sheet="Front", new_name="Front"))
        assert out["changed"] is False

    def test_rename_needs_a_new_name(self, wire):
        wire([sheet("Front")])
        assert "new_name" in error_message(es.handler(action="rename", sheet="Front"))


class TestSetSize:
    def test_size_and_extent_are_read_back(self, wire):
        state = wire([sheet("Front", size=_A3, orientation=_LAND)])
        out = payload(es.handler(action="set_size", sheet="Front", sheet_size="a4"))
        assert out["sheet_size"] == "a4" and out["previous_sheet_size"] == "a3"
        assert out["width"] == 297.0 and out["height"] == 210.0
        assert out["previous_width"] == 420.0 and out["sheet_units"] == "mm"
        assert state.sheets[0].sheetSize == _A4

    def test_a_size_from_the_other_standard_is_refused_before_anything_is_set(self, wire):
        # measured: Fusion RAISES on a mismatched size, and a raise in a drawing document is not
        # reliably rolled back - so the mismatch must never reach the assignment
        state = wire([sheet("Front", size=_A3, size_raises=_SIZE_REFUSAL)], standard="iso")
        msg = error_message(es.handler(action="set_size", sheet="Front", sheet_size="b"))
        # the full phrase is pinned: the refusal reads as English, never "a ASME sheet size"
        assert "The ASME sheet size 'b' is not valid for this drawing" in msg
        assert "Choose one of the ISO sizes." in msg
        assert state.sheets[0].sheetSize == _A3
        assert state.sheets[0]._size_sets == 0       # the assignment was never attempted

    def test_a_platform_refusal_reaches_the_caller_verbatim(self, wire):
        # standard unreadable -> the guard cannot fire, so the raise itself must be carried
        state = wire([sheet("Front", size=_A3, size_raises=_SIZE_REFUSAL)], standard=None)
        msg = error_message(es.handler(action="set_size", sheet="Front", sheet_size="a4"))
        assert "not valid for the active drawing standard" in msg
        assert state.sheets[0].sheetSize == _A3

    def test_a_silently_ignored_size_is_an_error(self, wire):
        wire([sheet("Front", size=_A3, size_ignored=True)])
        assert "did not take" in error_message(es.handler(action="set_size", sheet="Front",
                                                          sheet_size="a4"))

    def test_set_size_needs_a_size(self, wire):
        wire([sheet("Front")])
        assert "sheet_size" in error_message(es.handler(action="set_size", sheet="Front"))

    def test_a_size_this_build_has_no_member_for_is_refused_before_anything_is_set(self, wire,
                                                                                   monkeypatch):
        # the member is read BY NAME and answers None on a build that lacks it - assigning that None
        # would set the sheet to nothing, so the guard names the member and stops
        state = wire([sheet("Front", size=_A3)])
        monkeypatch.setattr(adsk.drawing, "SheetSizes",
                            drawing_enum("SheetSizes", keep=["A3ISOSheetSize"]))
        msg = error_message(es.handler(action="set_size", sheet="Front", sheet_size="a4"))
        assert "A4ISOSheetSize" in msg and "a4" in msg
        assert state.sheets[0]._size_sets == 0
        assert state.sheets[0].sheetSize == _A3


class TestFalsyEnumMembers:
    def test_the_two_families_are_numbered_from_zero(self, wire):
        # every measured adsk.drawing family starts at 0, so each has ONE member a truthiness test
        # cannot tell from an absent one - these two
        wire()
        assert adsk.drawing.SheetSizes.CustomSizeSheetSize == 0
        assert adsk.drawing.SheetOrientationTypes.LandscapeSheetOrientationType == 0
        assert adsk.drawing.SheetSizes.A4ISOSheetSize == 1

    def test_setting_the_falsy_landscape_member_lands_and_reads_back(self, wire):
        # landscape is 0: a member guard testing truthiness instead of None would refuse this
        # legitimate reorientation as "this Fusion build has no sheet orientation", and a label
        # decoder doing the same would report the result as unreadable
        state = wire([sheet("Front", size=_A4, orientation=_PORT)])
        out = payload(es.handler(action="set_orientation", sheet="Front",
                                 orientation="landscape"))
        assert out["orientation"] == "landscape" and out["previous_orientation"] == "portrait"
        assert out["width"] == 297.0 and out["height"] == 210.0
        assert state.sheets[0].orientation == _LAND

    def test_a_sheet_holding_the_falsy_custom_size_is_labelled_null_with_its_extent_kept(self, wire):
        # CustomSizeSheetSize is 0 and is NOT a preset this tool can set, so the label is null -
        # the extent is what a custom sheet reports instead, and it must still be published
        wire([sheet("Front", size=_CUSTOM)])
        out = payload(es.handler(action="add", new_name="Detail"))
        assert out["facts"]["sheet_size"] is None
        assert out["facts"]["width"] == 500.0 and out["facts"]["height"] == 333.0


class TestSheetListing:
    def test_add_publishes_collection_positions_with_unknown_export_indices(self, wire):
        # The collection positions are observations; the PDF mapping is deliberately unknown.
        state = wire([sheet("Front"), sheet("Tail")])
        out = payload(es.handler(action="add", new_name="Detail"))
        assert out["sheets"] == [{"export_index": None, "collection_index": 1, "name": "Front"},
                                 {"export_index": None, "collection_index": 2, "name": "Detail"},
                                 {"export_index": None, "collection_index": 3, "name": "Tail"}]
        assert "export/PDF order is unavailable" in out["note"]
        assert len(state.sheets) == 3

    def test_copy_publishes_the_listing_with_the_copy_last(self, wire):
        wire([sheet("Front"), sheet("Tail")])
        out = payload(es.handler(action="copy", sheet="Front", new_name="Front Copy"))
        assert [s["name"] for s in out["sheets"]] == ["Front", "Tail", "Front Copy"]
        assert out["sheets"][-1]["collection_index"] == 3
        assert all(row["export_index"] is None for row in out["sheets"])

    def test_an_unreadable_sheet_holds_its_collection_position(self, wire):
        # An unreadable sheet keeps its slot instead of changing the later collection positions.
        wire([sheet("Front"), sheet("Middle"), sheet("Tail")], unreadable=["Middle"])
        out = payload(es.handler(action="add", new_name="Detail"))
        assert out["sheets"] == [{"export_index": None, "collection_index": 1, "name": "Front"},
                                 {"export_index": None, "collection_index": 2, "name": "Detail"},
                                 {"export_index": None, "collection_index": 3, "name": None},
                                 {"export_index": None, "collection_index": 4, "name": "Tail"}]


class TestExtentUnits:
    def test_width_and_height_are_labelled_mm_on_an_inch_drawing(self, wire):
        # the defect this closes: an ASME B sheet reads 431.8 x 279.4 MILLIMETRES while the
        # drawing's dimension unit reads inches, so publishing the numbers under sheet_units
        # labelled millimetres as inches
        wire([sheet("Front", size=_B)], standard="asme", units="in")
        out = payload(es.handler(action="add", new_name="Detail"))
        assert out["facts"]["width"] == 431.8 and out["facts"]["height"] == 279.4
        assert out["facts"]["width_height_unit"] == "mm"
        assert out["sheet_units"] == "in"

    def test_a_resize_labels_the_extent_it_publishes(self, wire):
        wire([sheet("Front", size=_B)], standard="asme", units="in")
        out = payload(es.handler(action="set_size", sheet="Front", sheet_size="c"))
        assert out["width"] == 558.8 and out["width_height_unit"] == "mm"
        assert out["sheet_units"] == "in"

    def test_a_reorientation_labels_the_extent_it_publishes(self, wire):
        wire([sheet("Front", size=_B)], standard="asme", units="in")
        out = payload(es.handler(action="set_orientation", sheet="Front", orientation="portrait"))
        assert out["width"] == 279.4 and out["width_height_unit"] == "mm"
        assert out["sheet_units"] == "in"


class TestSetOrientation:
    def test_orientation_swaps_the_extent(self, wire):
        state = wire([sheet("Front", size=_A4, orientation=_LAND)])
        out = payload(es.handler(action="set_orientation", sheet="Front", orientation="portrait"))
        assert out["orientation"] == "portrait" and out["previous_orientation"] == "landscape"
        assert out["width"] == 210.0 and out["height"] == 297.0
        assert out["previous_width"] == 297.0
        assert state.sheets[0].orientation == _PORT

    def test_portrait_on_the_largest_sheet_is_refused_before_anything_is_set(self, wire):
        # measured: portrait on an ISO A0 sheet RAISES
        state = wire([sheet("Front", size=_A0, orientation_raises=_PORTRAIT_REFUSAL)],
                     standard="iso")
        msg = error_message(es.handler(action="set_orientation", sheet="Front",
                                       orientation="portrait"))
        # the full phrase is pinned, and the orientation it reports is the one it READ
        assert "portrait orientation on the ISO A0 sheet size" in msg
        assert "keeps its current orientation ('landscape')" in msg
        assert state.sheets[0].orientation == _LAND
        assert state.sheets[0]._orientation_sets == 0  # the assignment was never attempted

    def test_portrait_on_the_largest_asme_sheet_is_refused_before_anything_is_set(self, wire):
        # measured: Fusion answers "3 : Portrait orientation is not supported for ASME E sheet
        # size." - the same refusal as ISO A0, and the shared table carries both pairs
        state = wire([sheet("Front", size=_E, orientation_raises=_PORTRAIT_REFUSAL)],
                     standard="asme")
        msg = error_message(es.handler(action="set_orientation", sheet="Front",
                                       orientation="portrait"))
        assert "portrait orientation on the ASME E sheet size" in msg
        assert state.sheets[0].orientation == _LAND
        assert state.sheets[0]._orientation_sets == 0

    def test_portrait_on_a_smaller_sheet_of_the_same_standard_is_not_pre_refused(self, wire):
        # only the pairs Fusion actually refuses are guarded - a blanket ASME refusal would block
        # an orientation the platform accepts
        state = wire([sheet("Front", size=_B)], standard="asme")
        out = payload(es.handler(action="set_orientation", sheet="Front", orientation="portrait"))
        assert out["orientation"] == "portrait" and state.sheets[0]._orientation_sets == 1

    def test_an_unreadable_current_orientation_is_named_as_such(self, wire):
        # the refusal publishes what it READ - an orientation it cannot decode says so
        wire([sheet("Front", size=_A0, orientation=99)], standard="iso")
        msg = error_message(es.handler(action="set_orientation", sheet="Front",
                                       orientation="portrait"))
        assert "keeps its current orientation ('unreadable')" in msg

    def test_a_platform_refusal_reaches_the_caller_verbatim(self, wire):
        state = wire([sheet("Front", size=_A4, orientation_raises=_PORTRAIT_REFUSAL)],
                     standard=None)
        msg = error_message(es.handler(action="set_orientation", sheet="Front",
                                       orientation="portrait"))
        assert "not supported for ISO A0 sheet size" in msg
        assert state.sheets[0].orientation == _LAND

    def test_a_silently_ignored_orientation_is_an_error(self, wire):
        wire([sheet("Front", size=_A4, orientation_ignored=True)])
        assert "did not take" in error_message(es.handler(action="set_orientation", sheet="Front",
                                                          orientation="portrait"))

    def test_an_orientation_this_build_has_no_member_for_is_refused_before_anything_is_set(
            self, wire, monkeypatch):
        state = wire([sheet("Front", size=_A4)])
        monkeypatch.setattr(adsk.drawing, "SheetOrientationTypes",
                            drawing_enum("SheetOrientationTypes",
                                         keep=["LandscapeSheetOrientationType"]))
        msg = error_message(es.handler(action="set_orientation", sheet="Front",
                                       orientation="portrait"))
        assert "PortraitSheetOrientationType" in msg and "portrait" in msg
        assert state.sheets[0]._orientation_sets == 0
        assert state.sheets[0].orientation == _LAND

    def test_an_unknown_orientation_is_refused(self, wire):
        wire([sheet("Front")])
        assert "orientation" in error_message(es.handler(action="set_orientation", sheet="Front",
                                                         orientation="sideways"))


class TestTidyUp:
    def test_tidy_up_reads_the_property_exactly_once(self, wire):
        state = wire([sheet("Front", views=4)])
        out = payload(es.handler(action="tidy_up", sheet="Front"))
        assert out["tidied"] is True and out["views"] == 4
        assert out["document_modified"] is True and out["modified_confirmed"] is True
        assert state.doc.isModified is True
        assert state.sheets[0]._tidy_reads == 1

    def test_a_tidy_that_left_the_document_clean_is_an_error(self, wire):
        # tidyUp returns true but dirties nothing
        wire([sheet("Front", modifies=False)])
        assert "still unmodified" in error_message(es.handler(action="tidy_up", sheet="Front"))

    def test_an_already_modified_document_cannot_confirm_the_tidy(self, wire):
        # measured: tidying an already-modified document returns true with isModified already true
        wire([sheet("Front")], modified=True)
        out = payload(es.handler(action="tidy_up", sheet="Front"))
        assert out["tidied"] is True and out["modified_confirmed"] is False
        assert "cannot confirm" in out["note"]

    def test_a_false_tidy_up_is_an_error(self, wire):
        wire([sheet("Front", tidy_ok=False)])
        assert "did not tidy" in error_message(es.handler(action="tidy_up", sheet="Front"))

    @pytest.mark.parametrize("kwargs", [
        {"action": "add", "new_name": "Detail"},
        {"action": "rename", "sheet": "Front", "new_name": "Cover"},
        {"action": "set_size", "sheet": "Front", "sheet_size": "a4"},
        {"action": "set_orientation", "sheet": "Front", "orientation": "portrait"},
        {"action": "copy", "sheet": "Front"},
        {"action": "delete", "sheet": "Front"},
    ])
    def test_no_other_action_touches_the_mutating_property(self, wire, kwargs):
        # reading Sheet.tidyUp TIDIES the sheet - every other path must leave it untouched
        state = wire([sheet("Front", views=2), sheet("Spare")])
        es.handler(**kwargs)
        assert [s._tidy_reads for s in state.sheets] == [0] * len(state.sheets)


class TestDescriptionClaims:
    def test_the_add_vs_copy_inheritance_split_is_pinned(self, wire):
        # Both halves are measured: an added sheet inherits the ACTIVE sheet's settings, a copy
        # carries the SOURCE sheet's. A swap is a wrong wire claim on every add or copy - each
        # note is where the caller meets its own half.
        wire([sheet("Sheet1"), sheet("Spare")])
        added = payload(es.handler(action="add"))["note"]
        assert "inheriting its size and orientation" in added and "ACTIVE sheet" in added
        copied = payload(es.handler(action="copy", sheet="Spare"))["note"]
        assert "SOURCE sheet's size, orientation" in copied

    def test_the_two_units_are_told_apart_on_the_wire(self, wire):
        # width/height are millimetres on EVERY drawing; sheet_units is the dimension unit. A
        # payload that published one label for both would be the wire half of the same wrong claim.
        wire([sheet("Sheet1", size=_A3)], units="in")
        out = payload(es.handler(action="set_size", sheet_size="a4"))
        assert out["width_height_unit"] == "mm" and out["sheet_units"] == "in"
