"""Unit tests for ``drawing_create.py`` - create a 2D drawing from the active design.

Covers the creation flow: source-DataFile guard (unsaved design refused), the createDrawingInput/
createDrawing dispatch, creation-mode selection (automatic by default; manual refused without a
template), enum resolution (each map's member spellings, an absent family or member failing the call
before the create transaction opens, and the center_line/center_mark refusal), the full
auto-generator config mapping (standard/units/content/size/orientation/scope, sheet types,
auto-dimensioning, fastener omission, view style, isometric), the size<->standard and portrait
guards, and that a created cloud drawing reports its file_id. No live Fusion.
"""

import types

import pytest

import adsk  # the mock package conftest installed at import time
import live_api_facts
from conftest import (FakeCreateDrawingInput, FakeCustomSheetSize, FakeDataFile,
                      FakeDrawingManager, FakeFusionDocument, MakeDesign, drawing_enum,
                      install_drawing, load_tool)


# ── the automation-preferences tree and the unmeasured enum families ─────────
#
# No shape dump exists for the *Preferences groups, so each node below is a bespoke bag of the
# properties this tool writes, not a stand-in for a measured surface.

def _drawingview_node():
    """A DrawingViewPreferences node: style + the per-view drafting-display properties. No shape
    dump exists for it."""
    return types.SimpleNamespace(style=None, centerLineType=None, centerMarkType=None,
                                 tangentEdgesType=None, isShowInterferenceEdges=None,
                                 isShowThreadEdges=None)


def _autodim_node():
    """An AutoDimensionBasePreferences node: strategy + hole/thread annotation style. No shape dump
    exists for it."""
    return types.SimpleNamespace(dimensionStrategyType=None, holePreferencesType=None)


def _assembly_sheet_node():
    """An AssemblySheetPreferences node (isoViewSheetPreferences/orthogonalViewSheetPreferences):
    parts-list inclusion + placement. No shape dump exists for it."""
    return types.SimpleNamespace(isPartsListIncluded=None, partsListLocationType=None)


def _component_prefs_node():
    """A ComponentPreferences node. No shape dump exists for it."""
    return types.SimpleNamespace(
        drawingViewPreferences=_drawingview_node(),
        autoDimensionPreferences=_autodim_node(),
        sheetViewPreferences=types.SimpleNamespace(isIsometricViewAdded=False,
                                                   isOrthogonalViewAdded=True))


def _assembly_prefs_node():
    """mainAssemblyPreferences/subAssemblyPreferences: view style/auto-dim like component, plus the
    iso/orthogonal assembly-sheet parts-list nodes. No shape dump exists for it."""
    return types.SimpleNamespace(
        drawingViewPreferences=_drawingview_node(),
        autoDimensionPreferences=_autodim_node(),
        isoViewSheetPreferences=_assembly_sheet_node(),
        orthogonalViewSheetPreferences=_assembly_sheet_node())


def _flatpattern_prefs_node():
    """A FlatPatternPreferences node. No shape dump exists for it."""
    return types.SimpleNamespace(
        drawingViewPreferences=_drawingview_node(),
        autoDimensionPreferences=_autodim_node(),
        orthogonalViewSheetPreferences=_assembly_sheet_node())


def _automation_preferences():
    """The whole AutomationDrawingPreferences tree a create is configured through. No shape dump
    exists for any node in it, so it is a bespoke bag of the properties this tool writes."""
    globals_ = types.SimpleNamespace(
        isComponentSheetGenerated=True, isMainAssemblySheetGenerated=True,
        isSubAssemblySheetGenerated=True, isFlatPatternSheetGenerated=True,
        isFoldedModelSheetGenerated=True, isAnimationSheetGenerated=False,
        isAutoDimensionEnabled=True, isDetectAndOmitFasteners=False,
        omitComponentsWithKeywords="Bolt,Screw,Nut,Washer")
    return types.SimpleNamespace(
        globalPreferences=globals_,
        componentPreferences=_component_prefs_node(),
        mainAssemblyPreferences=_assembly_prefs_node(),
        subAssemblyPreferences=_assembly_prefs_node(),
        flatPatternPreferences=_flatpattern_prefs_node())


def _family(suffix, *prefixes):
    """One UNMEASURED adsk.drawing enum family as the probe log states it: the member prefixes in
    value order, each carrying the family's shared suffix, numbered from 0."""
    return types.SimpleNamespace(**{p + suffix: i for i, p in enumerate(prefixes)})


# The member tables measured on 2705 that live_api_facts does not carry as families. Every fake enum
# below is built from THESE literals, so a test comparing the tool's maps to them is a real
# comparison and not the tool checking itself.
_STYLE_FAMILY = _family("DrawingViewStyleType", "VisibleEdges", "VisibleAndHiddenEdges",
                        "ShadedAndHiddenEdges", "ShadedVisibleEdges")
_TANGENT_FAMILY = _family("TangentEdgeDisplayType", "Off", "FullLength", "Shortened")
_HOLE_FAMILY = _family("HolePreferencesType", "HoleAndThreadNote", "HoleNoteOnly", "ThreadNoteOnly",
                       "NoHoleAnnotations")
_TABLE_FAMILY = _family("TableLocationType", "TopLeft", "TopRight", "BottomLeft", "BottomRight")
_CONTENT_FAMILY = types.SimpleNamespace(FullAssemblyDrawingContentType="FULL",
                                        VisibleOnlyDrawingContentType="VIS")
_SHEET_CREATION_FAMILY = types.SimpleNamespace(FirstLevelOnlySheetCreationType="FIRST",
                                               AllLevelsSheetCreationType="ALL")
_BASE_DOCUMENT_FAMILY = types.SimpleNamespace(FromScratchBaseDocumentType="SCRATCH",
                                              FromTemplateBaseDocumentType="TEMPLATE")

# The MEASURED families, which install_drawing seeds from live_api_facts. In SheetSizes the falsy
# member is CustomSizeSheetSize (0) and the presets run A4=1 to E=10; ISO, Inch, Landscape and
# Overall are each 0 in their own family, so a truthiness test on a resolved member drops a real one.
_STANDARD_FAMILY = live_api_facts.ENUMS["drawing.DrawingStandardTypes"]
_UNIT_FAMILY = live_api_facts.ENUMS["drawing.DrawingUnitTypes"]
_ORIENTATION_FAMILY = live_api_facts.ENUMS["drawing.SheetOrientationTypes"]
_STRATEGY_FAMILY = live_api_facts.ENUMS["drawing.DimensionStrategyTypes"]
_SIZE_FAMILY = live_api_facts.ENUMS["drawing.SheetSizes"]
_CREATION_MODES = live_api_facts.ENUMS["drawing.DrawingCreationModes"]

# No CenterLineDisplayTypes / CenterMarkDisplayTypes: adsk.drawing carries no such families, so the
# stand-in namespace carries none either and a getattr for them raises, as the live one does.
_UNMEASURED_FAMILIES = {
    "DrawingContentTypes": _CONTENT_FAMILY,
    "SheetCreationTypes": _SHEET_CREATION_FAMILY,
    "BaseDocumentTypes": _BASE_DOCUMENT_FAMILY,
    "DrawingViewStyleTypes": _STYLE_FAMILY,
    "HolePreferencesTypes": _HOLE_FAMILY,
    "TableLocationTypes": _TABLE_FAMILY,
    "TangentEdgeDisplayTypes": _TANGENT_FAMILY,
}

dc = load_tool("drawing_create")


def _datafile(name="Widget v1", file_id="urn:adsk.wipprod:dm.lineage:WIDGET", ext="f2d"):
    """One cloud file at this file's defaults - the id/versionId/extension a created drawing is
    published from."""
    return FakeDataFile(name=name, file_id=file_id, version_id=file_id + "?version=1",
                        extension=ext)


@pytest.fixture
def install(monkeypatch):
    """Install the stand-in adsk.drawing (measured families plus the unmeasured ones above) with a
    DrawingManager the tool reaches through get(), and a saved design behind it. Answers the
    manager, whose recorded (source, mode, input, created_with) are what a create is judged by."""
    def _install(*, datafile=True, result_df="default", input_raises=None, create_raises=None,
                 **input_knobs):
        namespace = install_drawing(monkeypatch, **_UNMEASURED_FAMILIES)
        design = MakeDesign(parent_document=FakeFusionDocument(
            data_file=_datafile() if datafile else None))
        monkeypatch.setattr(dc._common, "design", lambda: design)
        monkeypatch.setattr(dc, "app", types.SimpleNamespace(activeDocument=object()))
        if result_df == "default":
            result_df = _datafile("Widget Drawing v1")
        manager = FakeDrawingManager(
            create_input=FakeCreateDrawingInput(
                automation_preferences=_automation_preferences(), **input_knobs),
            result=result_df, input_raises=input_raises, create_raises=create_raises)
        namespace.DrawingManager = manager
        return manager
    return _install


def _payload(res):
    import json
    assert res["isError"] is False, res
    return json.loads(res["content"][0]["text"])


def _gp(dm):
    return dm._input.automationPreferences.globalPreferences


# ── happy path ──────────────────────────────────────────────────────────────

class TestHappyPath:
    def test_creates_and_returns_file_id(self, install):
        install()
        out = _payload(dc.handler())
        assert out["created"] is True
        assert out["drawing_name"] == "Widget Drawing v1"
        assert out["file_id"] == "urn:adsk.wipprod:dm.lineage:WIDGET"

    def test_defaults_to_automatic_creation_mode(self, install):
        dm = install()
        dc.handler()
        assert dm._mode == _CREATION_MODES["AutomaticDrawingCreationMode"]
        assert dm._created_with is dm._input

    def test_declared_returns_are_present(self, install):
        install()
        out = _payload(dc.handler())
        for spec in dc.RETURNS:
            assert spec.assert_present(out) == "", spec.assert_present(out)

    def test_settings_requested_echoes_config(self, install):
        install()
        out = _payload(dc.handler(standard="asme", view_style="shaded_edges",
                                  sheet_scope="first_level"))
        s = out["settings_requested"]
        assert s["standard"] == "asme"
        assert s["view_style"] == "shaded_edges"
        assert s["sheet_scope"] == "first_level"
        assert s["creation_mode"] == "automatic"


# ── guards ────────────────────────────────────────────────────────────────────

class TestGuards:
    def test_unsaved_design_refused(self, install):
        install(datafile=False)
        res = dc.handler()
        assert res["isError"] is True
        assert "cloud" in res["message"].lower() or "datafile" in res["message"].lower()

    def test_create_returns_null_is_error(self, install):
        install(result_df=None)
        res = dc.handler()
        assert res["isError"] is True
        assert "null" in res["message"].lower() or "nothing created" in res["message"].lower()

    def test_no_active_design_errors(self, install, monkeypatch):
        install()
        monkeypatch.setattr(dc._common, "design", lambda: None)
        res = dc.handler()
        assert res["isError"] is True
        assert "design" in res["message"].lower()

    def test_create_exception_is_reported_not_swallowed(self, install):
        install(create_raises="boom-create")
        res = dc.handler()
        assert res["isError"] is True
        assert "boom-create" in res["message"]

    def test_missing_file_id_on_created_drawing_errors(self, install):
        # A created drawing whose file_id can't be read can't be located for export.
        df = _datafile("Widget Drawing v1")
        df.id = None
        install(result_df=df)
        res = dc.handler()
        assert res["isError"] is True
        assert "file_id" in res["message"].lower()

    def test_sheet_size_wrong_standard_is_refused(self, install):
        # an ISO size with an ASME standard is silently ignored by the API, so the tool guards it.
        install()
        res = dc.handler(standard="asme", sheet_size="a2")
        assert res["isError"] is True
        assert "a2" in res["message"].lower() and "asme" in res["message"].lower()

    def test_portrait_on_largest_sheet_is_refused(self, install):
        install()
        res = dc.handler(sheet_size="a0", orientation="portrait")
        assert res["isError"] is True
        assert "portrait" in res["message"].lower()

    def test_portrait_on_the_largest_asme_sheet_is_refused(self, install):
        # measured: "3 : Portrait orientation is not supported for ASME E sheet size." - the second
        # pair of the shared table, and the one a table holding only ISO A0 would let through
        dm = install()
        res = dc.handler(standard="asme", sheet_size="e", orientation="portrait")
        assert res["isError"] is True
        assert "portrait" in res["message"].lower() and "e" in res["message"].lower()
        assert dm._created_with is None

    def test_portrait_on_a_smaller_sheet_of_each_standard_is_allowed(self, install):
        # only the measured pairs are refused: a blanket largest-sheet rule would block sizes
        # Fusion accepts in portrait
        for standard, size in (("iso", "a1"), ("asme", "d")):
            install()
            out = _payload(dc.handler(standard=standard, sheet_size=size, orientation="portrait"))
            assert out["created"] is True, (standard, size)

    def test_unknown_sheet_type_is_refused(self, install):
        install()
        res = dc.handler(sheet_types=["component", "bogus"])
        assert res["isError"] is True
        assert "bogus" in res["message"].lower()

    def test_unknown_standard_rejected(self, install):
        install()
        res = dc.handler(standard="mil")
        assert res["isError"] is True
        assert "standard" in res["message"].lower()


# ── input mapping ─────────────────────────────────────────────────────────────

class TestInputMapping:
    def test_isometric_toggle_reaches_the_input(self, install):
        dm = install()
        dc.handler(isometric=True)
        cp = dm._input.automationPreferences.componentPreferences
        assert cp.sheetViewPreferences.isIsometricViewAdded is True
        dm = install()
        dc.handler(isometric=False)
        cp = dm._input.automationPreferences.componentPreferences
        assert cp.sheetViewPreferences.isIsometricViewAdded is False

    def test_standard_units_content_map_to_enums(self, install):
        dm = install()
        dc.handler(standard="asme", units="inch", content="visible")
        assert dm._input.standard == _STANDARD_FAMILY["ASMEDrawingStandardType"]
        assert dm._input.units == _UNIT_FAMILY["InchDrawingUnitType"]
        assert dm._input.content == "VIS"

    def test_the_falsy_default_standard_and_units_still_reach_the_input(self, install):
        # ISO and Inch are 0; a truthiness test anywhere on the resolved member would drop them.
        dm = install()
        dc.handler(standard="iso", units="inch", orientation="landscape")
        assert dm._input.standard == 0
        assert dm._input.units == 0
        assert dm._input.orientationType == 0

    def test_sheet_size_maps_to_enum(self, install):
        dm = install()
        dc.handler(sheet_size="a2")
        assert dm._input.sheetSize == _SIZE_FAMILY["A2ISOSheetSize"] == 3

    def test_orientation_and_scope_map_to_enums(self, install):
        dm = install()
        dc.handler(orientation="portrait", sheet_scope="first_level")
        assert dm._input.orientationType == _ORIENTATION_FAMILY["PortraitSheetOrientationType"]
        assert dm._input.sheetCreationType == "FIRST"

    def test_sheet_types_enables_only_the_listed_kinds(self, install):
        dm = install()
        dc.handler(sheet_types=["component", "main_assembly"])
        gp = _gp(dm)
        assert gp.isComponentSheetGenerated is True
        assert gp.isMainAssemblySheetGenerated is True
        assert gp.isSubAssemblySheetGenerated is False
        assert gp.isFlatPatternSheetGenerated is False
        assert gp.isAnimationSheetGenerated is False

    def test_auto_dimension_off_disables_it(self, install):
        dm = install()
        dc.handler(auto_dimension="off")
        assert _gp(dm).isAutoDimensionEnabled is False

    def test_auto_dimension_strategy_enables_and_sets_strategy(self, install):
        # dimensionStrategyType is set on EVERY sheet type's autoDimensionPreferences, not just
        # componentPreferences: main/sub-assembly and flat-pattern get it too.
        dm = install()
        dc.handler(auto_dimension="baseline")
        assert _gp(dm).isAutoDimensionEnabled is True
        want = _STRATEGY_FAMILY["BaselineDimensionStrategyType"]
        ap = dm._input.automationPreferences
        assert ap.componentPreferences.autoDimensionPreferences.dimensionStrategyType == want
        assert ap.mainAssemblyPreferences.autoDimensionPreferences.dimensionStrategyType == want
        assert ap.subAssemblyPreferences.autoDimensionPreferences.dimensionStrategyType == want
        assert ap.flatPatternPreferences.autoDimensionPreferences.dimensionStrategyType == want

    def test_omit_fasteners_and_keywords_reach_global_prefs(self, install):
        dm = install()
        dc.handler(omit_fasteners=True, fastener_keywords="Rivet,Pin")
        gp = _gp(dm)
        assert gp.isDetectAndOmitFasteners is True
        assert gp.omitComponentsWithKeywords == "Rivet,Pin"


class TestMeasuredMemberSpellings:
    # Each expected map is written from the measured member table, not read from the tool - a map
    # naming a member the enum does not carry is the defect these compare against.
    def test_view_style_map_names_the_measured_members(self):
        assert dc._VIEW_STYLE_MAP == {
            "visible": "VisibleEdgesDrawingViewStyleType",
            "hidden": "VisibleAndHiddenEdgesDrawingViewStyleType",
            "shaded_hidden": "ShadedAndHiddenEdgesDrawingViewStyleType",
            "shaded_edges": "ShadedVisibleEdgesDrawingViewStyleType"}

    def test_tangent_edge_map_names_the_measured_members(self):
        assert dc._TANGENT_EDGE_MAP == {
            "off": "OffTangentEdgeDisplayType",
            "full_length": "FullLengthTangentEdgeDisplayType",
            "shortened": "ShortenedTangentEdgeDisplayType"}

    def test_hole_preference_map_names_the_measured_members(self):
        assert dc._HOLE_PREF_MAP == {
            "both": "HoleAndThreadNoteHolePreferencesType",
            "hole": "HoleNoteOnlyHolePreferencesType",
            "thread": "ThreadNoteOnlyHolePreferencesType",
            "none": "NoHoleAnnotationsHolePreferencesType"}

    def test_table_location_map_names_the_measured_members(self):
        assert dc._TABLE_LOCATION_MAP == {
            "top_left": "TopLeftTableLocationType", "top_right": "TopRightTableLocationType",
            "bottom_left": "BottomLeftTableLocationType",
            "bottom_right": "BottomRightTableLocationType"}

    def test_flat_setter_maps_name_the_measured_members(self):
        assert dc._STANDARD_MAP == {"iso": "ISODrawingStandardType", "asme": "ASMEDrawingStandardType"}
        assert dc._UNITS_MAP == {"mm": "MillimeterDrawingUnitType", "inch": "InchDrawingUnitType"}
        assert dc._ORIENTATION_MAP == {"landscape": "LandscapeSheetOrientationType",
                                       "portrait": "PortraitSheetOrientationType"}
        assert dc._CONTENT_MAP == {"full": "FullAssemblyDrawingContentType",
                                   "visible": "VisibleOnlyDrawingContentType"}
        assert dc._SHEET_SCOPE_MAP == {"all_levels": "AllLevelsSheetCreationType",
                                       "first_level": "FirstLevelOnlySheetCreationType"}
        assert dc._BASE_DOCUMENT_MAP == {"template": "FromTemplateBaseDocumentType"}

    def test_the_sheet_size_member_map_covers_every_preset_plus_custom(self):
        # 'default' is deliberately absent - it is not a request, so nothing is set
        assert dc._SHEET_SIZE_MEMBERS == {
            "a4": "A4ISOSheetSize", "a3": "A3ISOSheetSize", "a2": "A2ISOSheetSize",
            "a1": "A1ISOSheetSize", "a0": "A0ISOSheetSize", "a": "AASMESheetSize",
            "b": "BASMESheetSize", "c": "CASMESheetSize", "d": "DASMESheetSize",
            "e": "EASMESheetSize", "custom": "CustomSizeSheetSize"}
        assert "default" not in dc._SHEET_SIZE_MEMBERS

    def test_dimension_strategy_map_names_every_measured_member(self):
        # the shared table: this tool sets the strategy the generator runs with, drawing_dimension
        # sets it per view afterwards, and a strategy legal on one and refused by the other would
        # be this family's invention - all eight members the enum carries are offered by both
        assert dc._drawing_common.DIMENSION_STRATEGIES == {
            "overall": "OverallDimensionStrategyType",
            "automatic": "AutomaticDimensionStrategyType",
            "baseline": "BaselineDimensionStrategyType",
            "chain": "ChainDimensionStrategyType",
            "ordinate": "OrdinateDimensionStrategyType",
            "symmetric": "SymmetricDimensionStrategyType",
            "symmetric_with_baseline": "SymmetricWithBaselineDimensionStrategyType",
            "symmetric_with_ordinate": "SymmetricWithOrdinateDimensionStrategyType"}
        assert list(dc._AUTO_DIMENSION.options) == (
            ["default", "off"] + list(dc._drawing_common.DIMENSION_STRATEGIES))

    def test_every_strategy_the_choice_offers_reaches_the_input(self, install):
        # the refusal this closes: 'ordinate' was schema-legal on the per-view tool and refused
        # here, for a strategy the platform carries on both
        for key, member in dc._drawing_common.DIMENSION_STRATEGIES.items():
            dm = install()
            out = _payload(dc.handler(auto_dimension=key))
            ap = dm._input.automationPreferences
            assert ap.componentPreferences.autoDimensionPreferences.dimensionStrategyType == \
                _STRATEGY_FAMILY[member], key
            assert out["settings_requested"]["auto_dimension"] == key

    def test_every_mapped_member_exists_on_the_measured_families(self, install):
        install()
        for input_name, family, member_map in dc._ENUM_INPUTS:
            fam = getattr(adsk.drawing, family)
            for value, member in member_map.items():
                assert hasattr(fam, member), f"{input_name}={value} -> {family}.{member}"


class TestEnumFamilyResolution:
    def test_an_absent_family_fails_the_call_naming_it(self, install, monkeypatch):
        dm = install()
        monkeypatch.delattr(adsk.drawing, "TangentEdgeDisplayTypes")
        res = dc.handler(tangent_edges="shortened")
        assert res["isError"] is True
        assert "adsk.drawing.TangentEdgeDisplayTypes" in res["message"]
        assert "tangent_edges" in res["message"]
        # resolved BEFORE the create transaction opens: createDrawingInput was never called.
        assert dm._mode is None
        assert dm._created_with is None

    def test_an_absent_family_is_ignored_when_the_input_is_not_requested(self, install, monkeypatch):
        install()
        monkeypatch.delattr(adsk.drawing, "TangentEdgeDisplayTypes")
        out = _payload(dc.handler())
        assert out["created"] is True

    def test_an_absent_content_family_fails_the_call_naming_it(self, install, monkeypatch):
        # an absent family swallowed inside safe() would create the drawing with the wrong content
        # while the call reported ok - the resolver names the family and creates nothing instead
        dm = install()
        monkeypatch.delattr(adsk.drawing, "DrawingContentTypes")
        res = dc.handler(content="visible")
        assert res["isError"] is True
        assert "adsk.drawing.DrawingContentTypes" in res["message"]
        assert "content" in res["message"]
        assert dm._mode is None and dm._created_with is None

    def test_an_absent_sheet_creation_family_fails_the_call_naming_it(self, install, monkeypatch):
        dm = install()
        monkeypatch.delattr(adsk.drawing, "SheetCreationTypes")
        res = dc.handler(sheet_scope="first_level")
        assert res["isError"] is True
        assert "adsk.drawing.SheetCreationTypes" in res["message"]
        assert "sheet_scope" in res["message"]
        assert dm._created_with is None

    def test_an_absent_sheet_size_member_fails_the_call_naming_it(self, install, monkeypatch):
        dm = install()
        monkeypatch.setattr(adsk.drawing, "SheetSizes",
                            drawing_enum("SheetSizes", keep=["A4ISOSheetSize"]))
        res = dc.handler(sheet_size="a2")
        assert res["isError"] is True
        assert "SheetSizes.A2ISOSheetSize" in res["message"]
        assert dm._created_with is None

    def test_an_absent_custom_size_member_fails_the_call_naming_it(self, install, monkeypatch):
        dm = install()
        monkeypatch.setattr(adsk.drawing, "SheetSizes",
                            drawing_enum("SheetSizes", keep=["A4ISOSheetSize"]))
        res = dc.handler(sheet_size="custom", custom_width_mm=420, custom_height_mm=297)
        assert res["isError"] is True
        assert "SheetSizes.CustomSizeSheetSize" in res["message"]
        assert dm._created_with is None

    def test_an_absent_base_document_family_fails_a_template_create(self, install, monkeypatch):
        dm = install()
        monkeypatch.setattr(dc, "_resolve_data_file",
                            lambda raw: (_datafile("Shop Template"), raw, [raw]))
        monkeypatch.delattr(adsk.drawing, "BaseDocumentTypes")
        res = dc.handler(template_file="urn:x")
        assert res["isError"] is True
        assert "adsk.drawing.BaseDocumentTypes" in res["message"]
        assert dm._created_with is None

    def test_an_absent_base_document_family_does_not_block_a_scratch_create(self, install,
                                                                           monkeypatch):
        # without a template there is no baseDocumentType request, so the family is never needed
        install()
        monkeypatch.delattr(adsk.drawing, "BaseDocumentTypes")
        out = _payload(dc.handler())
        assert out["created"] is True

    def test_an_absent_member_on_a_present_family_fails_the_call(self, install, monkeypatch):
        dm = install()
        monkeypatch.setattr(adsk.drawing, "HolePreferencesTypes",
                            types.SimpleNamespace(HoleAndThreadNoteHolePreferencesType=0))
        res = dc.handler(hole_annotations="thread")
        assert res["isError"] is True
        assert "HolePreferencesTypes.ThreadNoteOnlyHolePreferencesType" in res["message"]
        assert dm._mode is None
        assert dm._created_with is None


class TestViewStyle:
    def test_shaded_styles_map_to_the_members_the_enum_carries(self, install):
        for value, member in (
                ("shaded_hidden", _STYLE_FAMILY.ShadedAndHiddenEdgesDrawingViewStyleType),
                ("shaded_edges", _STYLE_FAMILY.ShadedVisibleEdgesDrawingViewStyleType),
                ("visible", _STYLE_FAMILY.VisibleEdgesDrawingViewStyleType),
                ("hidden", _STYLE_FAMILY.VisibleAndHiddenEdgesDrawingViewStyleType)):
            dm = install()
            dc.handler(view_style=value)
            ap = dm._input.automationPreferences
            assert ap.componentPreferences.drawingViewPreferences.style == member, value
            assert ap.mainAssemblyPreferences.drawingViewPreferences.style == member, value
            assert ap.subAssemblyPreferences.drawingViewPreferences.style == member, value

    def test_a_falsy_enum_member_still_reaches_the_input(self, install):
        # An enum member of 0 is a real style; a truthiness test on the resolved member would drop it
        # while the call still reported success.
        dm = install()
        dc.handler(view_style="visible")
        ap = dm._input.automationPreferences
        assert ap.componentPreferences.drawingViewPreferences.style == 0
        assert ap.mainAssemblyPreferences.drawingViewPreferences.style == 0
        assert ap.subAssemblyPreferences.drawingViewPreferences.style == 0

    def test_view_style_default_leaves_the_style_untouched(self, install):
        dm = install()
        dc.handler()
        assert dm._input.automationPreferences.componentPreferences \
            .drawingViewPreferences.style is None

    def test_missing_enum_member_fails_the_call_instead_of_dropping_the_style(self, install,
                                                                              monkeypatch):
        # A style whose enum member this Fusion version does not carry cannot be applied; reporting ok
        # would be a silent drop, so the call fails and nothing is created.
        dm = install()
        monkeypatch.setattr(adsk.drawing, "DrawingViewStyleTypes",
                            types.SimpleNamespace(VisibleEdgesDrawingViewStyleType=0))
        res = dc.handler(view_style="shaded_edges")
        assert res["isError"] is True
        assert "shaded_edges" in res["message"]
        assert "ShadedVisibleEdgesDrawingViewStyleType" in res["message"]
        assert dm._mode is None
        assert dm._created_with is None

    def test_style_whose_member_exists_is_unaffected_by_a_thinner_enum(self, install, monkeypatch):
        dm = install()
        monkeypatch.setattr(adsk.drawing, "DrawingViewStyleTypes",
                            types.SimpleNamespace(VisibleEdgesDrawingViewStyleType=0))
        out = _payload(dc.handler(view_style="visible"))
        assert out["created"] is True
        assert dm._input.automationPreferences.componentPreferences \
            .drawingViewPreferences.style == 0

    def test_retired_shaded_value_is_refused_by_the_enum(self, install):
        # 'shaded' named a member that does not exist; the legal values are the ones that do.
        install()
        res = dc.handler(view_style="shaded")
        assert res["isError"] is True
        assert "shaded_hidden" in res["message"] and "shaded_edges" in res["message"]


class TestCreationMode:
    def test_manual_without_template_is_refused_before_any_create_call(self, install):
        # Fusion's manual-mode refusal escapes try/except and poisons the transaction, so the guard
        # must run before createDrawingInput is ever called.
        dm = install()
        res = dc.handler(creation_mode="manual")
        assert res["isError"] is True
        assert "template_file" in res["message"]
        assert "view placeholder" in res["message"]
        assert dm._mode is None
        assert dm._created_with is None

    def test_manual_with_a_resolved_template_uses_manual_mode(self, install, monkeypatch):
        dm = install()
        template_df = _datafile("Smart Template", file_id="urn:adsk.wipprod:dm.lineage:TPL")
        monkeypatch.setattr(dc, "_resolve_data_file", lambda raw: (template_df, raw, [raw]))
        out = _payload(dc.handler(creation_mode="manual", template_file="urn:x"))
        assert dm._mode == _CREATION_MODES["ManualDrawingCreationMode"]
        assert dm._input.templateFile is template_df
        assert out["settings_requested"]["creation_mode"] == "manual"

    def test_manual_note_states_the_template_gate(self, install, monkeypatch):
        install()
        monkeypatch.setattr(dc, "_resolve_data_file",
                            lambda raw: (_datafile("Smart Template"), raw, [raw]))
        out = _payload(dc.handler(creation_mode="manual", template_file="urn:x"))
        assert "view placeholder" in out["note"]

    def test_automatic_note_does_not_mention_the_manual_gate(self, install):
        install()
        out = _payload(dc.handler())
        assert "view placeholder" not in out["note"]

    def test_the_description_carries_the_timeout_fact_the_note_cannot_reach(self):
        # a caller whose call TIMED OUT never receives the ok() note, so the fact it needs most -
        # that the create can still have landed - has to be on the surface it read beforehand
        desc = dc.tool.to_dict()["description"]
        assert "TIMEOUT is not a verdict" in desc
        assert "data_get" in desc
        # the server-side "waits rather than timing out falsely" sentence said the opposite thing
        # to a caller staring at a client timeout, so it is not what this description promises
        assert "timing out falsely" not in desc

    def test_the_note_denies_that_a_client_timeout_is_a_failure_verdict(self, install):
        # a create that outran the client's call timeout has been found landed afterwards, so the
        # note names the re-check instead of leaving a blind retry as the obvious move
        install()
        out = _payload(dc.handler())
        assert "TIMES OUT" in out["note"] and "NOT a failure verdict" in out["note"]
        assert "data_get" in out["note"] and "doc_get" in out["note"]
        assert "SECOND drawing" in out["note"]

    def test_the_timeout_wording_rides_on_a_custom_size_create_too(self, install):
        # the custom-size branch appends its own extents sentence - the timeout fact must not be
        # the thing it displaces
        install()
        out = _payload(dc.handler(sheet_size="custom", custom_width_mm=500, custom_height_mm=333))
        assert "NOT a failure verdict" in out["note"] and "500.0 x 333.0 mm" in out["note"]

    def test_unknown_creation_mode_is_refused(self, install):
        dm = install()
        res = dc.handler(creation_mode="semi")
        assert res["isError"] is True
        assert "creation_mode" in res["message"]
        assert dm._mode is None


# ── the route from the created file to a PDF ────────────────────────────────────────────────────────

class TestTheRouteToTheDrawing:
    """A drawing never reviewed in the Fusion UI opens and drives through the API - measured on
    2705.0.87 across a full open/edit/dimension/export cycle. So neither surface may park the agent
    on a human up front; the UI open survives only as failure-time teaching in the note."""

    def _description(self):
        return dc.tool.to_dict()["description"]

    def test_the_description_names_the_api_route_and_asks_for_no_ui_step(self):
        desc = self._description()
        assert "doc_open" in desc and "drawing_export" in desc
        assert "no UI step" in desc
        # the retired instruction: a human opening it before the agent may proceed
        assert "ONCE in the Fusion UI" not in desc
        assert "unreviewed" not in desc

    def test_the_note_routes_through_doc_open_without_a_human_step(self, install):
        install()
        note = _payload(dc.handler())["note"]
        assert "doc_open(file_id, force_api_open=true)" in note
        assert "no manual step is needed up front" in note
        assert "open it ONCE in the Fusion UI" not in note

    def test_the_note_keeps_the_ui_open_as_failure_time_teaching_only(self, install):
        # the fallback is still worth carrying (earlier builds did block), but it is conditional on
        # the open actually failing - not an instruction the agent follows before trying
        install()
        note = _payload(dc.handler())["note"]
        head, _, tail = note.partition("If that open instead fails or hangs")
        assert tail, note
        assert "workaround" not in head            # nothing to work around until the open fails
        assert "never reviewed in the Fusion UI opens and drives that way" in head
        assert "opening the document once in the Fusion UI is the known workaround" in tail


# ── parts list, template, custom size, hole annotations, per-view drafting display ──────────────────

class TestPartsList:
    def test_parts_list_and_location_reach_main_and_sub_assembly_iso_and_orthogonal(self, install):
        dm = install()
        dc.handler(parts_list=True, parts_list_location="bottom_right")
        ap = dm._input.automationPreferences
        for path in (ap.mainAssemblyPreferences, ap.subAssemblyPreferences):
            for sheet in (path.isoViewSheetPreferences, path.orthogonalViewSheetPreferences):
                assert sheet.isPartsListIncluded is True
                assert sheet.partsListLocationType == _TABLE_FAMILY.BottomRightTableLocationType

    def test_parts_list_false_reaches_the_same_nodes(self, install):
        dm = install()
        dc.handler(parts_list=False)
        ap = dm._input.automationPreferences
        assert ap.mainAssemblyPreferences.isoViewSheetPreferences.isPartsListIncluded is False
        assert ap.subAssemblyPreferences.orthogonalViewSheetPreferences.isPartsListIncluded is False

    def test_parts_list_omitted_leaves_the_nodes_untouched(self, install):
        dm = install()
        dc.handler()
        ap = dm._input.automationPreferences
        assert ap.mainAssemblyPreferences.isoViewSheetPreferences.isPartsListIncluded is None
        assert ap.mainAssemblyPreferences.isoViewSheetPreferences.partsListLocationType is None

    def test_unknown_parts_list_location_is_refused(self, install):
        install()
        res = dc.handler(parts_list_location="center")
        assert res["isError"] is True
        assert "parts_list_location" in res["message"] and "center" in res["message"]


class TestTemplateFile:
    # monkeypatch (not a bare `dc._resolve_data_file = ...`) - the autouse seam-restore fixture only
    # resets `design`/`target_component`/`_design`/`app`/`_data`, so an imperative assignment here would
    # leak into later tests; monkeypatch undoes itself automatically.
    def test_template_file_resolves_and_sets_base_document_type(self, install, monkeypatch):
        dm = install()
        template_df = _datafile("Shop Template", file_id="urn:adsk.wipprod:dm.lineage:TEMPLATE")
        monkeypatch.setattr(dc, "_resolve_data_file", lambda raw: (template_df, raw, [raw]))
        out = _payload(dc.handler(template_file="urn:adsk.wipprod:dm.lineage:TEMPLATE"))
        assert dm._input.baseDocumentType == "TEMPLATE"
        assert dm._input.templateFile is template_df
        assert out["settings_requested"]["base_document"] == "template"

    def test_no_template_file_leaves_base_document_type_untouched(self, install):
        dm = install()
        dc.handler()
        assert dm._input.baseDocumentType is None
        assert dm._input.templateFile is None

    def test_unresolvable_template_file_is_refused_and_nothing_is_created(self, install,
                                                                         monkeypatch):
        dm = install()
        monkeypatch.setattr(dc, "_resolve_data_file", lambda raw: (None, None, [raw]))
        res = dc.handler(template_file="urn:adsk.wipprod:dm.lineage:NOPE")
        assert res["isError"] is True
        assert "template_file" in res["message"].lower()
        assert dm._created_with is None


class TestCustomSheetSize:
    def test_custom_size_is_assigned_back_through_the_setter_in_document_units(self, install):
        # BOTH halves of the dead path: CustomSheetSize.width/height are unitless numbers in the
        # DOCUMENT unit (millimetres under ISO), and the object the getter hands out only takes
        # effect when it is assigned BACK - a tool that mutates the copy alone emits a default sheet
        # while reporting the size it asked for.
        dm = install()
        out = _payload(dc.handler(sheet_size="custom", custom_width_mm=500, custom_height_mm=333))
        assert dm._input._custom_assignments, "customSize was never assigned back"
        landed = dm._input.customSize
        assert landed is dm._input._custom_assignments[-1]
        assert landed.width == pytest.approx(500.0)     # millimetres, NOT the 50.0 of centimetres
        assert landed.height == pytest.approx(333.0)
        applied = out["settings_requested"]["custom_size"]
        assert (applied["width"], applied["height"], applied["unit"]) == (500.0, 333.0, "mm")
        assert (applied["width_applied"], applied["height_applied"]) == (500.0, 333.0)
        assert (applied["horizontal_zones_applied"], applied["vertical_zones_applied"]) == (2, 2)

    def test_the_falsy_custom_sheet_size_member_still_reaches_the_input(self, install):
        # CustomSizeSheetSize is 0: a truthiness test on the resolved member would skip
        # di.sheetSize entirely while settings_requested still reported 'custom'.
        dm = install()
        out = _payload(dc.handler(sheet_size="custom", custom_width_mm=500, custom_height_mm=333))
        assert _SIZE_FAMILY["CustomSizeSheetSize"] == 0
        assert dm._input.sheetSize == 0
        assert out["settings_requested"]["sheet_size"] == "custom"

    def test_an_asme_custom_size_is_written_in_inches(self, install):
        # the document unit follows the STANDARD: inches under ASME, so 508 mm is 20 in
        dm = install()
        out = _payload(dc.handler(standard="asme", sheet_size="custom",
                                  custom_width_mm=508, custom_height_mm=254))
        assert dm._input.customSize.width == pytest.approx(20.0)
        assert dm._input.customSize.height == pytest.approx(10.0)
        assert out["settings_requested"]["custom_size"]["unit"] == "in"

    def test_both_zone_counts_are_raised_to_the_minimum_the_api_takes(self, install):
        # a CustomSheetSize created with fewer than 2 zones each way is refused at creation, so an
        # input already carrying zero zones has to be lifted before the create
        dm = install()
        dm._input.customSize = FakeCustomSheetSize(0.0, 0.0, 0, 0)
        dc.handler(sheet_size="custom", custom_width_mm=500, custom_height_mm=333)
        assert dm._input.customSize.horizontalZones == 2
        assert dm._input.customSize.verticalZones == 2

    def test_a_zone_count_already_at_the_minimum_is_left_alone(self, install):
        # the exact boundary: a count EQUAL to the minimum already satisfies the API, so nothing is
        # written to it - a '>' comparison here would rewrite a count that was already legal
        dm = install()
        dm._input.customSize = FakeCustomSheetSize(0.0, 0.0, 2, 2)
        _payload(dc.handler(sheet_size="custom", custom_width_mm=500, custom_height_mm=333))
        written = [name for name, _value in dm._input.customSize._writes]
        assert "horizontalZones" not in written and "verticalZones" not in written
        assert "width" in written and "height" in written   # the extents ARE written

    def test_a_zone_count_the_input_already_carries_is_left_alone_and_reported_as_it_reads(self,
                                                                                          install):
        # the payload and the note report the counts READ BACK, so a title block the input already
        # carries is published as the 6 x 4 it is - never as the 2 x 2 minimum the code would have
        # written had it needed to
        dm = install()
        dm._input.customSize = FakeCustomSheetSize(0.0, 0.0, 6, 4)
        out = _payload(dc.handler(sheet_size="custom", custom_width_mm=500, custom_height_mm=333))
        assert (dm._input.customSize.horizontalZones,
                dm._input.customSize.verticalZones) == (6, 4)
        applied = out["settings_requested"]["custom_size"]
        assert (applied["horizontal_zones_applied"], applied["vertical_zones_applied"]) == (6, 4)
        assert "6 x 4 zones" in out["note"]
        assert "2 x 2 zones" not in out["note"]

    def test_a_width_that_does_not_take_refuses_instead_of_creating_a_wrong_sheet(self, install):
        # the whole point of the read-back: a drawing emitted at some other size while the payload
        # says 'custom' is worse than no drawing
        dm = install(custom_ignores=True)
        res = dc.handler(sheet_size="custom", custom_width_mm=500, custom_height_mm=333)
        assert res["isError"] is True
        assert "custom sheet width" in res["message"] and "No drawing was created" in res["message"]
        assert dm._created_with is None

    def test_an_input_without_customsize_refuses_instead_of_creating(self, install):
        dm = install(custom_absent=True)
        res = dc.handler(sheet_size="custom", custom_width_mm=500, custom_height_mm=333)
        assert res["isError"] is True
        assert "no customSize" in res["message"]
        assert dm._created_with is None

    def test_an_unassignable_customsize_refuses_naming_the_size(self, install):
        dm = install(custom_raises="customSize is read-only")
        res = dc.handler(sheet_size="custom", custom_width_mm=500, custom_height_mm=333)
        assert res["isError"] is True
        assert "read-only" in res["message"] and "500.0 x 333.0 mm" in res["message"]
        assert dm._created_with is None

    def test_the_note_publishes_the_read_back_extents_and_denies_reading_the_created_sheet(self,
                                                                                          install):
        install()
        out = _payload(dc.handler(sheet_size="custom", custom_width_mm=500, custom_height_mm=333))
        assert "500.0 x 333.0 mm" in out["note"]
        assert "2 x 2 zones" in out["note"]
        assert "not readable from here" in out["note"]

    def test_the_off_switch_refuses_custom_and_creates_nothing(self, install, monkeypatch):
        # the one constant to throw if a live create stops landing the requested extents: custom
        # REFUSES rather than emitting a preset-sized drawing labelled custom
        dm = install()
        monkeypatch.setattr(dc, "_CUSTOM_SIZE_ENABLED", False)
        res = dc.handler(sheet_size="custom", custom_width_mm=500, custom_height_mm=333)
        assert res["isError"] is True
        assert "turned OFF" in res["message"]
        assert dm._mode is None and dm._created_with is None

    def test_the_off_switch_leaves_preset_sizes_alone(self, install, monkeypatch):
        dm = install()
        monkeypatch.setattr(dc, "_CUSTOM_SIZE_ENABLED", False)
        out = _payload(dc.handler(sheet_size="a3"))
        assert out["created"] is True
        assert dm._input.sheetSize == _SIZE_FAMILY["A3ISOSheetSize"]

    def test_custom_size_missing_height_is_refused(self, install):
        install()
        res = dc.handler(sheet_size="custom", custom_width_mm=420)
        assert res["isError"] is True
        assert "custom_height_mm" in res["message"]

    def test_custom_size_non_positive_is_refused(self, install):
        install()
        res = dc.handler(sheet_size="custom", custom_width_mm=0, custom_height_mm=297)
        assert res["isError"] is True
        assert "custom_width_mm" in res["message"] and "0" in res["message"]

    def test_custom_width_without_custom_sheet_size_is_refused(self, install):
        install()
        res = dc.handler(sheet_size="a4", custom_width_mm=420, custom_height_mm=297)
        assert res["isError"] is True
        assert "custom" in res["message"].lower()


class TestHoleAnnotations:
    def test_hole_annotations_reach_every_autodim_sheet_type(self, install):
        dm = install()
        dc.handler(hole_annotations="hole")
        want = _HOLE_FAMILY.HoleNoteOnlyHolePreferencesType
        ap = dm._input.automationPreferences
        assert ap.componentPreferences.autoDimensionPreferences.holePreferencesType == want
        assert ap.mainAssemblyPreferences.autoDimensionPreferences.holePreferencesType == want
        assert ap.subAssemblyPreferences.autoDimensionPreferences.holePreferencesType == want
        assert ap.flatPatternPreferences.autoDimensionPreferences.holePreferencesType == want

    def test_hole_annotations_default_leaves_it_untouched(self, install):
        dm = install()
        dc.handler()
        cp = dm._input.automationPreferences.componentPreferences
        assert cp.autoDimensionPreferences.holePreferencesType is None

    def test_unknown_hole_annotations_is_refused(self, install):
        install()
        res = dc.handler(hole_annotations="bogus")
        assert res["isError"] is True
        assert "hole_annotations" in res["message"] and "bogus" in res["message"]


class TestPerViewDraftingDisplay:
    def test_tangent_edges_and_show_flags_reach_component_and_assembly_sheets_not_flat_pattern(
            self, install):
        dm = install()
        dc.handler(tangent_edges="shortened", show_interference_edges=True, show_thread_edges=False)
        ap = dm._input.automationPreferences
        for node in (ap.componentPreferences.drawingViewPreferences,
                     ap.mainAssemblyPreferences.drawingViewPreferences,
                     ap.subAssemblyPreferences.drawingViewPreferences):
            assert node.tangentEdgesType == _TANGENT_FAMILY.ShortenedTangentEdgeDisplayType
            assert node.isShowInterferenceEdges is True
            assert node.isShowThreadEdges is False
        # flatPatternPreferences.drawingViewPreferences is NOT in the touched set (matches view_style).
        flat = ap.flatPatternPreferences.drawingViewPreferences
        assert flat.tangentEdgesType is None
        assert flat.isShowInterferenceEdges is None

    def test_the_falsy_tangent_edge_member_still_reaches_the_input(self, install):
        dm = install()
        dc.handler(tangent_edges="off")
        node = dm._input.automationPreferences.componentPreferences.drawingViewPreferences
        assert node.tangentEdgesType == 0

    def test_drafting_display_omitted_leaves_it_untouched(self, install):
        dm = install()
        dc.handler()
        node = dm._input.automationPreferences.componentPreferences.drawingViewPreferences
        assert node.tangentEdgesType is None
        assert node.isShowInterferenceEdges is None
        assert node.isShowThreadEdges is None

    def test_unknown_center_line_is_refused(self, install):
        install()
        res = dc.handler(center_line="bogus")
        assert res["isError"] is True
        assert "center_line" in res["message"] and "bogus" in res["message"]

    def test_unknown_center_mark_is_refused(self, install):
        install()
        res = dc.handler(center_mark="bogus")
        assert res["isError"] is True
        assert "center_mark" in res["message"] and "bogus" in res["message"]

    def test_unknown_tangent_edges_is_refused(self, install):
        install()
        res = dc.handler(tangent_edges="bogus")
        assert res["isError"] is True
        assert "tangent_edges" in res["message"] and "bogus" in res["message"]

    def test_center_line_request_is_refused_naming_the_absent_family(self, install):
        # adsk.drawing carries no CenterLineDisplayTypes, so the setting has no API to reach; a
        # request must fail rather than pass through a best-effort setter that drops it.
        dm = install()
        res = dc.handler(center_line="holes")
        assert res["isError"] is True
        assert "center_line" in res["message"] and "CenterLineDisplayTypes" in res["message"]
        assert dm._mode is None
        assert dm._created_with is None

    def test_center_mark_request_is_refused_naming_the_absent_family(self, install):
        dm = install()
        res = dc.handler(center_mark="fillets")
        assert res["isError"] is True
        assert "center_mark" in res["message"] and "CenterMarkDisplayTypes" in res["message"]
        assert dm._mode is None
        assert dm._created_with is None

    def test_center_line_and_mark_at_default_do_not_block_creation(self, install):
        install()
        out = _payload(dc.handler(center_line="default", center_mark="default"))
        assert out["created"] is True

    def test_the_two_input_descriptions_state_the_refusal_their_resolver_enforces(self):
        # the schema advertises values the handler refuses outright; a description that sells them
        # as a capability is the only place an agent could learn otherwise before it calls
        for kind in (dc._CENTER_LINE, dc._CENTER_MARK):
            desc = kind.as_property()[1]["description"]
            assert "Refused unless 'default'" in desc, kind.name
            assert "no enum exists" in desc, kind.name


class TestJustSavedLag:
    # measured: a design saved seconds earlier fails with exactly "3 : Failed to create drawing
    # document" while its cloud DataFile is still processing, and the identical call succeeds about
    # a minute later. The handler never sleeps or retries - the error is the one place to teach it.
    _SENTENCE = "3 : Failed to create drawing document"

    def test_the_bare_create_refusal_teaches_the_cloud_processing_lag(self, install):
        install(create_raises=self._SENTENCE)
        res = dc.handler()
        assert res["isError"] is True
        assert self._SENTENCE in res["message"]
        assert "still processing" in res["message"]
        assert "minute" in res["message"] and "retry" in res["message"]

    def test_a_sibling_platform_sentence_carries_no_lag_claim(self, install):
        # the lag is measured for exactly one sentence. A Fusion failure that SHARES its shape and
        # prefix is a different failure - sending the caller away to wait it out would waste a
        # minute and then fail again identically.
        sibling = "3 : Failed to create drawing view"
        install(create_raises=sibling)
        res = dc.handler()
        assert res["isError"] is True
        assert sibling in res["message"]          # the platform sentence is carried verbatim
        for claim in ("still processing", "minute", "retry"):
            assert claim not in res["message"], claim

    def test_any_other_create_failure_carries_no_lag_claim(self, install):
        # the lag is a claim about ONE platform sentence; attaching it to every failure would
        # send a caller to wait out a failure waiting cannot fix
        install(create_raises="boom-create")
        res = dc.handler()
        assert res["isError"] is True
        assert "boom-create" in res["message"]
        assert "minute" not in res["message"]

    def test_a_null_create_is_not_reported_as_the_lag(self, install):
        install(result_df=None)
        res = dc.handler()
        assert res["isError"] is True
        assert "minute" not in res["message"]
