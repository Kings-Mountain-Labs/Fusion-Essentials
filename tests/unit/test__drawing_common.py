"""Unit tests for ``_drawing_common.py`` - the drawing family's shared substrate.

The contracts every drawing tool leans on:
  - ``sheet_units`` decodes the drawing's OWN documentSettings.units and returns None when it cannot
    be read. A guessed default would put a unit on the wire that no read backs, and the caller
    cannot recover a wrong unit claim - so the None path is the point of the helper. It is the
    DIMENSION display unit; ``SHEET_EXTENT_UNIT`` is the separate, constant unit Sheet.width/height
    come back in (millimetres on every drawing, ISO and ASME alike).
  - ``standard_label`` decodes documentSettings.standard the same way, and is what the sheet-size
    and portrait guards turn on.
  - ``coordinate_unit`` keys the unit sheet GEOMETRY lands in to that standard - mm under ISO, in
    under ASME - never to sheet_units, which the two tools that place geometry publish beside it.
    ISO reads 0, so a truthiness test anywhere on this path nulls the unit on every ISO drawing.
  - ``NO_PORTRAIT`` is the measured (standard, sheet size) table Fusion refuses portrait on, held
    once so the two tools that guard on it cannot disagree.
  - ``SHEET_SIZE_MAP`` and ``DIMENSION_STRATEGIES`` are the two enum-member tables the family
    shares, so the tool that sets a value at creation time and the tool that sets it afterwards
    cannot offer different legal sets. ``enum_value`` reads a member whose value is 0 as a VALUE
    (CustomSizeSheetSize is that member); only None means the build lacks it.
  - ``resolve_sheet`` matches a sheet name case-insensitively and EXACTLY (sheet names are measured
    case-insensitively unique), falls back to the active sheet for an empty name, and lists the
    drawing's sheets on a miss so the caller can re-issue a real name.
"""

import pytest

import adsk  # the mock package conftest installed at import time
import live_api_facts
from conftest import FakeSheet, load_tool, make_drawing, make_drawing_session

dc = load_tool("_drawing_common")

# The MEASURED ISO value. ISO is 0 - a FALSY member - so a truthiness test in the decode drops
# every ISO drawing; Inch is 0 in its own family for the same reason.
_ISO = live_api_facts.ENUMS["drawing.DrawingStandardTypes"]["ISODrawingStandardType"]


@pytest.fixture
def wire(monkeypatch):
    """Install a drawing document as the ACTIVE one over the shared drawing-world fakes; return the
    Drawing the helpers under test take. The whole adsk.drawing stand-in goes in wholesale, so a
    sibling drawing test file that swapped that module cannot decide this file's result."""
    def _install(names=(), active=0, units="mm", standard="iso", settings=True):
        document = make_drawing(sheets=[FakeSheet(n) for n in names], active=active,
                                units=units, standard=standard)
        make_drawing_session(monkeypatch, document)
        if not settings:
            document.drawing.documentSettings = None
        return document.drawing
    return _install


class TestSheetUnits:
    def test_millimetre_and_inch_both_decode(self, wire):
        assert dc.sheet_units(wire(units="mm")) == "mm"
        assert dc.sheet_units(wire(units="in")) == "in"

    def test_unreadable_units_are_none_never_a_default(self, wire):
        # the drawing carries no readable settings: publishing "mm" here would be a unit claim
        # nothing read, and a caller cannot recover from a wrong one
        assert dc.sheet_units(wire(settings=False)) is None
        assert dc.sheet_units(None) is None

    def test_an_unrecognised_units_value_is_none(self, wire):
        assert dc.sheet_units(wire(units=987654)) is None


class TestSheetExtentUnit:
    def test_width_and_height_are_millimetres_whatever_the_dimension_unit_reads(self, wire):
        # measured: an ASME B sheet (17 x 11 in) reads 431.8 x 279.4 from Sheet.width/height while
        # its documentSettings.units reads inches - the two units are separate facts, and labelling
        # the extent with sheet_units publishes millimetres under an inch label
        assert dc.SHEET_EXTENT_UNIT == "mm"
        assert dc.sheet_units(wire(units="in")) == "in"


class TestEnumValue:
    def test_a_present_member_reads_its_value(self, wire):
        wire()
        assert dc.enum_value("SheetSizes", "A4ISOSheetSize") == 1
        assert dc.enum_value("SheetSizes", "A3ISOSheetSize") == 2

    def test_the_zero_member_is_a_value_not_an_absence(self, wire):
        # CustomSizeSheetSize is 0. A member whose value is 0 is a real member: only None means
        # "this build lacks it", so every consumer tests the read against None, never truthiness -
        # a truthiness test here reports the custom size as unavailable on every build that has it.
        wire()
        assert dc.enum_value("SheetSizes", "CustomSizeSheetSize") == 0
        assert dc.enum_value("SheetSizes", "CustomSizeSheetSize") is not None
        assert not dc.enum_value("SheetSizes", "CustomSizeSheetSize")

    def test_a_member_this_build_lacks_reads_none_instead_of_raising(self, wire):
        wire()
        assert dc.enum_value("SheetSizes", "NoSuchSheetSize") is None

    def test_an_absent_family_reads_none_instead_of_raising(self, wire):
        # the None is the whole contract: a caller guards on it and reports which member is missing,
        # so a raise escaping here would sink the read it was called from
        wire()
        assert dc.enum_value("NoSuchFamily", "A4ISOSheetSize") is None


class TestStandardLabel:
    def test_both_standards_decode(self, wire):
        assert dc.standard_label(wire(standard="iso")) == "iso"
        assert dc.standard_label(wire(standard="asme")) == "asme"

    def test_an_unreadable_standard_is_none(self, wire):
        # None is what the size and portrait guards fall through on, so they must never see a guess
        assert dc.standard_label(wire(settings=False)) is None
        assert dc.standard_label(wire(standard=None)) is None
        assert dc.standard_label(None) is None

    def test_an_unrecognised_standard_value_is_none(self, wire):
        assert dc.standard_label(wire(standard=99)) is None


class TestCoordinateUnit:
    def test_the_standard_alone_fixes_the_coordinate_unit(self, wire):
        # "Coordinates are in drawing length units (millimeters when the drawing standard includes
        # ISO; inches when the standard is ASME without ISO)" - the DrawingSketch geometry
        # docstrings. sheet_units is a different setting and does not appear in that rule.
        assert dc.coordinate_unit(wire(standard="iso")) == "mm"
        assert dc.coordinate_unit(wire(standard="asme")) == "in"

    def test_an_iso_drawing_in_inches_still_takes_millimetre_coordinates(self, wire):
        # the split drawing drawing_create can mint: the two fields must DISAGREE, and a helper
        # that keyed off units would return "in" here and be wrong by 25.4x
        dwg = wire(standard="iso", units="in")
        assert dc.coordinate_unit(dwg) == "mm"
        assert dc.sheet_units(dwg) == "in"

    def test_an_asme_drawing_in_millimetres_still_takes_inch_coordinates(self, wire):
        dwg = wire(standard="asme", units="mm")
        assert dc.coordinate_unit(dwg) == "in"
        assert dc.sheet_units(dwg) == "mm"

    def test_the_falsy_iso_member_still_decodes(self, wire):
        # ISO is 0 (measured). Any truthiness test on the member value - in this helper or in the
        # standard_label it leans on - nulls the coordinate unit on EVERY ISO drawing, which is the
        # majority of them, and the null would then be read as "unknown" rather than "mm".
        assert _ISO == 0
        assert dc.coordinate_unit(wire(standard=0)) == "mm"

    def test_an_unreadable_standard_is_none_never_a_guessed_default(self, wire):
        assert dc.coordinate_unit(wire(settings=False)) is None
        assert dc.coordinate_unit(wire(standard=99)) is None
        assert dc.coordinate_unit(None) is None


class TestNoPortraitTable:
    def test_the_table_is_the_two_measured_pairs(self):
        # Fusion answers "Portrait orientation is not supported for ISO A0 sheet size." and
        # "3 : Portrait orientation is not supported for ASME E sheet size." - and nothing else is
        # measured, so a third pair here would refuse an orientation Fusion accepts
        assert dc.NO_PORTRAIT == {("iso", "a0"), ("asme", "e")}


class TestResolveSheet:
    def test_exact_match_is_case_insensitive(self, wire):
        sheet, err = dc.resolve_sheet(wire(["Front", "Front Detail"]), "front")
        assert err is None and sheet.name == "Front"

    def test_a_longer_name_is_not_matched_by_a_prefix(self, wire):
        sheet, err = dc.resolve_sheet(wire(["Front Detail"]), "Front")
        assert sheet is None and "Front Detail" in err

    def test_a_miss_lists_the_available_sheets(self, wire):
        sheet, err = dc.resolve_sheet(wire(["Front", "Detail"]), "Section")
        assert sheet is None
        assert "Section" in err and "Front" in err and "Detail" in err

    def test_an_empty_name_resolves_to_the_active_sheet(self, wire):
        sheet, err = dc.resolve_sheet(wire(["Front", "Detail"], active=1), "")
        assert err is None and sheet.name == "Detail"

    def test_no_active_sheet_is_an_error_not_a_silent_first_sheet(self, wire):
        dwg = wire(["Front"])
        dwg.activeSheet = None
        sheet, err = dc.resolve_sheet(dwg, "")
        assert sheet is None and "active sheet" in err

    def test_a_duplicated_name_is_refused_not_first_matched(self, wire):
        # The invariant guard: names are case-insensitively unique on this build, but if two ever
        # match, the resolver must refuse rather than hand back whichever came first.
        sheet, err = dc.resolve_sheet(wire(["Front", "front"]), "FRONT")
        assert sheet is None and "matches 2 sheets" in err


class TestLabelNoneGuards:
    def test_size_label_of_an_unreadable_value_is_none(self, wire):
        wire()
        assert dc.size_label(None) is None

    def test_orientation_label_of_an_unreadable_value_is_none(self, wire):
        wire()
        assert dc.orientation_label(None) is None


class TestSharedEnumTables:
    def test_every_sheet_size_member_exists_on_the_family_with_its_standard(self, wire):
        # the table both the create-time and the set_size path resolve through: a member name the
        # family does not carry would make enum_value answer None and refuse a legal size
        wire()
        for key, (standard, member) in dc.SHEET_SIZE_MAP.items():
            assert dc.enum_value("SheetSizes", member) is not None, key
            assert standard in ("iso", "asme"), key
        assert [k for k, (s, _m) in dc.SHEET_SIZE_MAP.items() if s == "iso"] == \
            ["a4", "a3", "a2", "a1", "a0"]
        assert [k for k, (s, _m) in dc.SHEET_SIZE_MAP.items() if s == "asme"] == \
            ["a", "b", "c", "d", "e"]

    def test_the_preset_table_excludes_the_custom_member(self):
        # CustomSizeSheetSize cannot be assigned to Sheet.sheetSize at all, so a preset table
        # carrying it would offer a resize Fusion refuses
        assert "custom" not in dc.SHEET_SIZE_MAP
        assert not any(m == "CustomSizeSheetSize" for _s, m in dc.SHEET_SIZE_MAP.values())

    def test_every_dimension_strategy_member_exists_on_the_family(self, wire):
        # all eight members the family carries - the creation-time generator and the per-view
        # dimensioning call read the SAME table, so neither can offer a strategy the other refuses
        wire()
        assert len(dc.DIMENSION_STRATEGIES) == 8
        for key, member in dc.DIMENSION_STRATEGIES.items():
            assert dc.enum_value("DimensionStrategyTypes", member) is not None, key
        assert dc.enum_value("DimensionStrategyTypes",
                             dc.DIMENSION_STRATEGIES["overall"]) == 0


class TestActiveDrawing:
    def test_a_non_drawing_document_reads_none(self, wire, monkeypatch):
        wire(["Front"])
        monkeypatch.setattr(adsk.core.Application, "get",
                            lambda: type("Session", (), {"activeDocument": object()})())
        assert dc.active_drawing() is None
        assert dc.active_drawing_document() is None

    def test_the_drawing_behind_the_document_is_returned(self, wire):
        dwg = wire(["Front"])
        assert dc.active_drawing() is dwg

    def test_the_document_itself_is_returned_for_the_reference_surface(self, monkeypatch):
        # documentReferences/updateAllReferences hang off the DrawingDocument, not off the Drawing,
        # so the cast stops one level earlier for the caller that needs them
        document = make_drawing(sheets=[FakeSheet("Front")], references=[object(), object()])
        make_drawing_session(monkeypatch, document)
        assert dc.active_drawing_document() is document
        assert dc.active_drawing_document().documentReferences.count == 2
        assert dc.active_drawing() is document.drawing
