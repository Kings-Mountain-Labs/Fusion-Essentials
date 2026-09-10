# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Create a 2D drawing document from the active design's saved cloud DataFile (CreateDrawingInput +
its automationPreferences tree). The automatic generator lays the views out; manual creation needs a
template carrying view-placeholder information. The new drawing is a CLOUD file, returned as file_id
(lineage URN) and NOT opened - doc_open opens it, no Fusion UI step first (measured: a drawing never
reviewed in the UI opens and drives on current builds). WRITES a drawing.
"""

import adsk.core
import adsk.drawing

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import ok, error, safe
from . import _common
from . import _drawing_common
from . import _outputs
from . import _inputs
from ._data_common import _resolve_data_file

app = adsk.core.Application.get()

# What this tool RETURNS (declared once; drives the PRODUCES: prose + the assert-present contract test).
RETURNS = [
    _outputs.ReturnsUrn("file_id", consumers=["drawing_export", "doc_open", "data_get"]),
    _outputs.ReturnsName("drawing_name", of="drawing document"),
]

# sheet_size value -> SheetSizes member, the shape _resolve_members resolves. 'default' is absent:
# it is not a request, so nothing is set and Fusion picks the sheet.
_SHEET_SIZE_MEMBERS = dict(
    {key: member for key, (_std, member) in _drawing_common.SHEET_SIZE_MAP.items()},
    custom="CustomSizeSheetSize")
_CONTENT_MAP = {"full": "FullAssemblyDrawingContentType",
                "visible": "VisibleOnlyDrawingContentType"}
_SHEET_SCOPE_MAP = {"all_levels": "AllLevelsSheetCreationType",
                    "first_level": "FirstLevelOnlySheetCreationType"}
# baseDocumentType is a request only when a template_file resolved; a scratch drawing leaves the
# property alone, so the map carries the template member only.
_BASE_DOCUMENT_MAP = {"template": "FromTemplateBaseDocumentType"}

# DrawingViewStyleTypes carries exactly these four members: the two shaded ones pair shading with
# HIDDEN edges or with VISIBLE edges - there is no plain shaded member.
_VIEW_STYLE_MAP = {
    "visible": "VisibleEdgesDrawingViewStyleType",
    "hidden": "VisibleAndHiddenEdgesDrawingViewStyleType",
    "shaded_hidden": "ShadedAndHiddenEdgesDrawingViewStyleType",
    "shaded_edges": "ShadedVisibleEdgesDrawingViewStyleType",
}
# sheet-type name -> GlobalPreferences toggle property.
_SHEET_TYPE_ATTR = {
    "component": "isComponentSheetGenerated",
    "main_assembly": "isMainAssemblySheetGenerated",
    "sub_assembly": "isSubAssemblySheetGenerated",
    "flat_pattern": "isFlatPatternSheetGenerated",
    "folded_model": "isFoldedModelSheetGenerated",
    "animation": "isAnimationSheetGenerated",
}
# Sheet-type paths whose autoDimensionPreferences (strategy + hole-annotation) this tool sets.
_AUTODIM_PATHS = ("componentPreferences", "mainAssemblyPreferences", "subAssemblyPreferences",
                  "flatPatternPreferences")
# Sheet-type paths whose drawingViewPreferences (style + drafting-display) this tool sets.
_VIEWSTYLE_PATHS = ("componentPreferences", "mainAssemblyPreferences", "subAssemblyPreferences")

_TABLE_LOCATION_MAP = {
    "top_left": "TopLeftTableLocationType", "top_right": "TopRightTableLocationType",
    "bottom_left": "BottomLeftTableLocationType", "bottom_right": "BottomRightTableLocationType",
}
_HOLE_PREF_MAP = {
    "both": "HoleAndThreadNoteHolePreferencesType",
    "hole": "HoleNoteOnlyHolePreferencesType",
    "thread": "ThreadNoteOnlyHolePreferencesType",
    "none": "NoHoleAnnotationsHolePreferencesType",
}
_TANGENT_EDGE_MAP = {
    "off": "OffTangentEdgeDisplayType", "full_length": "FullLengthTangentEdgeDisplayType",
    "shortened": "ShortenedTangentEdgeDisplayType",
}
_STANDARD_MAP = {"iso": "ISODrawingStandardType", "asme": "ASMEDrawingStandardType"}
_UNITS_MAP = {"mm": "MillimeterDrawingUnitType", "inch": "InchDrawingUnitType"}
_ORIENTATION_MAP = {"landscape": "LandscapeSheetOrientationType",
                    "portrait": "PortraitSheetOrientationType"}

# Every adsk.drawing enum member this tool sets: input name -> (family, resolved value -> member name).
# A value the map does not carry ('default', or auto_dimension 'off') is not a request.
_ENUM_INPUTS = (
    ("standard", "DrawingStandardTypes", _STANDARD_MAP),
    ("units", "DrawingUnitTypes", _UNITS_MAP),
    ("content", "DrawingContentTypes", _CONTENT_MAP),
    ("sheet_size", "SheetSizes", _SHEET_SIZE_MEMBERS),
    ("sheet_scope", "SheetCreationTypes", _SHEET_SCOPE_MAP),
    ("base_document", "BaseDocumentTypes", _BASE_DOCUMENT_MAP),
    ("orientation", "SheetOrientationTypes", _ORIENTATION_MAP),
    ("view_style", "DrawingViewStyleTypes", _VIEW_STYLE_MAP),
    ("auto_dimension", "DimensionStrategyTypes", _drawing_common.DIMENSION_STRATEGIES),
    ("hole_annotations", "HolePreferencesTypes", _HOLE_PREF_MAP),
    ("parts_list_location", "TableLocationTypes", _TABLE_LOCATION_MAP),
    ("tangent_edges", "TangentEdgeDisplayTypes", _TANGENT_EDGE_MAP),
)

# center_line / center_mark have no enum to reach: adsk.drawing carries no CenterLineDisplayTypes or
# CenterMarkDisplayTypes family (the namespace holds CenterLineOptions / CenterMarkOptions classes
# instead), so a non-default request is refused rather than dropped by a best-effort setter.
_UNREACHABLE_INPUTS = {"center_line": ("CenterLineDisplayTypes", "CenterLineOptions"),
                       "center_mark": ("CenterMarkDisplayTypes", "CenterMarkOptions")}
_STANDARD = _inputs.Choice("standard", ["iso", "asme"], default="iso")
_UNITS = _inputs.Choice("units", ["mm", "inch"], default="mm",
                        description="Dimension display units.")
_CONTENT = _inputs.Choice("content", ["full", "visible"], default="full")
_SHEET_SIZE = _inputs.Choice("sheet_size",
                             ["default", "a4", "a3", "a2", "a1", "a0", "a", "b", "c", "d", "e", "custom"],
                             default="default")
_ORIENTATION = _inputs.Choice("orientation", ["landscape", "portrait"], default="landscape")
_SHEET_SCOPE = _inputs.Choice("sheet_scope", ["all_levels", "first_level"], default="all_levels")
_AUTO_DIMENSION = _inputs.Choice("auto_dimension",
                                 ["default", "off"] + list(_drawing_common.DIMENSION_STRATEGIES),
                                 default="default",
                                 description="'off' disables it.")
_VIEW_STYLE = _inputs.Choice("view_style",
                             ["default", "visible", "hidden", "shaded_hidden", "shaded_edges"],
                             default="default")
_CREATION_MODE = _inputs.Choice("creation_mode", ["automatic", "manual"], default="automatic")
_PARTS_LIST_LOCATION = _inputs.Choice("parts_list_location",
                             ["default", "top_left", "top_right", "bottom_left", "bottom_right"],
                             default="default")
_HOLE_ANNOTATIONS = _inputs.Choice("hole_annotations",
                             ["default", "both", "hole", "thread", "none"],
                             default="default")
_CENTER_LINE = _inputs.Choice("center_line", ["default", "off", "cylindrical", "holes"], default="default",
                             description="Refused unless 'default': no enum exists.")
_CENTER_MARK = _inputs.Choice("center_mark",
                             ["default", "off", "holes", "fillets", "edges", "punches"],
                             default="default",
                             description="Refused unless 'default': no enum exists.")
_TANGENT_EDGES = _inputs.Choice("tangent_edges", ["default", "off", "full_length", "shortened"],
                             default="default")


def _source_datafile(design):
    """The cloud DataFile the drawing is generated from, or (None, error). Automatic drawing creation
    needs a saved cloud source design; an unsaved design has no DataFile to draw from."""
    doc = safe(lambda: design.parentDocument) or safe(lambda: app.activeDocument)
    df = safe(lambda: doc.dataFile)
    if not df:
        return None, ("The active design has not been saved to the cloud, so it has no DataFile to "
                      "draw from. Automatic drawing creation needs a cloud source design - save it "
                      "first with doc_save_as, then retry.")
    return df, None


# The gate Fusion enforces on manual creation, in its own words. The failure was measured escaping
# an enclosing try/except inside sys_execute_script, so the tool refuses the mode up front rather
# than calling into it.
_MANUAL_GATE = "Manual drawing creation requires a template with view placeholder information."


# The bare sentence Fusion raises while the source design's cloud DataFile is still processing: a
# design saved seconds earlier fails with exactly this, and the identical call succeeds about a
# minute later. The handler never sleeps or retries - it runs on the main thread.
_PROCESSING_LAG_SENTENCE = "Failed to create drawing document"


def _processing_lag_hint(ex):
    """The processing-lag remedy for Fusion's bare create refusal, or '' for any other failure."""
    if _PROCESSING_LAG_SENTENCE not in str(ex):
        return ""
    return (" Nothing was created. A source design saved seconds ago fails with exactly this "
            "sentence while its cloud DataFile is still processing; the same call succeeds about a "
            "minute later. Wait about a minute, then retry this call unchanged.")


# A create that outruns the CLIENT's call timeout still finishes and lands the drawing in the
# project, findable there by name. So a timeout is not a verdict on this call, and a blind retry
# mints a second drawing.
_TIMEOUT_IS_NOT_A_VERDICT = (
    "If a client call TIMES OUT on this tool, that is NOT a failure verdict - the create can still "
    "land. Re-check before retrying: data_get with 'project' + 'file' (the drawing's name) lists "
    "it if it was created, and doc_get reads it once it is open. A blind retry creates a SECOND "
    "drawing.")


# CreateDrawingInput.customSize hands out a DEFAULT CustomSheetSize that takes effect only when it
# is assigned BACK through the setter; its width/height are unitless numbers in the drawing's own
# document unit, and its two zone counts must each be at least 2. False refuses 'custom' outright.
_CUSTOM_SIZE_ENABLED = True
_CUSTOM_ZONES = 2

_CUSTOM_DISABLED_REFUSAL = (
    "sheet_size 'custom' is turned OFF in this tool, so nothing was created - a drawing emitted at "
    "a preset size while this call reported 'custom' is the outcome that refusal prevents. Pass a "
    "preset sheet_size (a4-a0 under ISO, a-e under ASME); a created drawing's sheet can also be "
    "resized afterwards with drawing_edit_sheet action='set_size'.")


def _apply_custom_size(di, cfg):
    """Write the requested custom sheet size onto the input and assign it BACK through the setter:
    '' or the refusal text (nothing is created on a refusal). The getter returns a COPY, so mutating
    it alone changes nothing; width/height are unitless numbers in cfg's document unit, and both
    zone counts must be at least 2 at creation."""
    spec = cfg["custom_size"]
    cs = safe(lambda: di.customSize)
    if cs is None:
        return ("This Fusion version's CreateDrawingInput carries no customSize, so a custom sheet "
                "size cannot be applied and no drawing was created. Use a preset sheet_size.")
    for prop in ("width", "height"):
        value = spec[prop]
        serr = _common.set_verified(cs, prop, value, f"custom sheet {prop} {value} {spec['unit']}",
                                    "CustomSheetSize")
        if serr:
            return (f"{serr} It reads {safe(lambda p=prop: getattr(cs, p))!r} after the assignment. "
                    "No drawing was created; use a preset sheet_size.")
    for prop in ("horizontalZones", "verticalZones"):
        current = safe(lambda p=prop: getattr(cs, p))
        # A count the input already carries that meets the minimum is the caller's title-block
        # layout and is left alone, so what the payload reports is READ BACK, never the constant.
        if not (isinstance(current, int) and not isinstance(current, bool)
                and current >= _CUSTOM_ZONES):
            serr = _common.set_verified(cs, prop, _CUSTOM_ZONES, f"custom sheet {prop}",
                                        "CustomSheetSize")
            if serr:
                return f"{serr} No drawing was created; use a preset sheet_size."
    try:
        # THE assignment the size hangs on - a failure here must surface, never be swallowed into a
        # create that emits a differently-sized drawing labelled custom.
        di.customSize = cs
    except Exception as ex:
        return (f"CreateDrawingInput.customSize could not be assigned ({ex}), so the custom size "
                f"{spec['width']} x {spec['height']} {spec['unit']} would not have been applied. "
                "No drawing was created; use a preset sheet_size.")
    spec["width_applied"] = safe(lambda: cs.width)
    spec["height_applied"] = safe(lambda: cs.height)
    spec["horizontal_zones_applied"] = safe(lambda: cs.horizontalZones)
    spec["vertical_zones_applied"] = safe(lambda: cs.verticalZones)
    return ""


def _resolve_members(cfg):
    """Every enum member this tool sets, as {input name: member}, or (None, error). Resolved BEFORE
    the create transaction: a family or member this Fusion version does not carry is reported as a
    failure naming the input, never a setting dropped inside safe() while the call reports success."""
    members = {}
    for input_name, family, member_map in _ENUM_INPUTS:
        value = cfg[input_name]
        name = member_map.get(value)
        if not name:
            continue
        # The family is probed separately from the member: the two are different failures on the
        # wire (an absent family names adsk.drawing.<family>, an absent member names <family>.<name>).
        fam = safe(lambda f=family: getattr(adsk.drawing, f))
        member = None if fam is None else _drawing_common.enum_value(family, name)
        if member is None:
            missing = f"adsk.drawing.{family}" if fam is None else f"{family}.{name}"
            return None, (f"{input_name} '{value}' needs {missing}, which is not available on this "
                          f"Fusion version - the setting could not be applied, so no drawing was "
                          f"created. Retry with a different {input_name}.")
        members[input_name] = member
    return members, None


def _apply_input_settings(di, cfg, members, template_data_file=None):
    """Best-effort configuration of the CreateDrawingInput and its automationPreferences tree: '' or
    the ONE refusal that is not best-effort, the custom sheet size. Every other setter is wrapped in
    safe() and echoed as 'settings_requested' rather than read back. 'members' and
    'template_data_file' stay out of cfg - neither is JSON-safe."""
    safe(lambda: setattr(di, "standard", members["standard"]))
    safe(lambda: setattr(di, "units", members["units"]))
    safe(lambda: setattr(di, "content", members["content"]))
    # A resolved enum value can be 0, so every member is tested against None, never truthiness.
    size_member = members.get("sheet_size")
    if size_member is not None:
        safe(lambda m=size_member: setattr(di, "sheetSize", m))
    if cfg["sheet_size"] == "custom":
        cerr = _apply_custom_size(di, cfg)
        if cerr:
            return cerr
    safe(lambda: setattr(di, "orientationType", members["orientation"]))
    safe(lambda: setattr(di, "sheetCreationType", members["sheet_scope"]))

    # baseDocumentType and templateFile move together, and only when a template_file resolved: the
    # base_document member exists exactly when cfg carries 'template', so this ONE gate holds both.
    base_member = members.get("base_document")
    if base_member is not None:
        safe(lambda m=base_member: setattr(di, "baseDocumentType", m))
        safe(lambda: setattr(di, "templateFile", template_data_file))

    gp = safe(lambda: di.automationPreferences.globalPreferences)
    if gp is not None:
        if cfg["sheet_types"] is not None:
            want = set(cfg["sheet_types"])
            for key, attr in _SHEET_TYPE_ATTR.items():
                safe(lambda a=attr, k=key: setattr(gp, a, k in want))
        if cfg["auto_dimension"] == "off":
            safe(lambda: setattr(gp, "isAutoDimensionEnabled", False))
        elif cfg["auto_dimension"] != "default":
            safe(lambda: setattr(gp, "isAutoDimensionEnabled", True))
        safe(lambda: setattr(gp, "isDetectAndOmitFasteners", bool(cfg["omit_fasteners"])))
        if cfg["fastener_keywords"]:
            safe(lambda: setattr(gp, "omitComponentsWithKeywords", cfg["fastener_keywords"]))

    # Apply the requested strategy/hole-annotation to every sheet type's autoDimensionPreferences, not
    # just componentPreferences.
    strat_member = members.get("auto_dimension")
    hole_member = members.get("hole_annotations")
    if strat_member is not None or hole_member is not None:
        for path in _AUTODIM_PATHS:
            prefs = safe(lambda p=path: getattr(di.automationPreferences, p))
            node = safe(lambda pr=prefs: pr.autoDimensionPreferences) if prefs is not None else None
            if node is None:
                continue
            if strat_member is not None:
                safe(lambda n=node, m=strat_member: setattr(n, "dimensionStrategyType", m))
            if hole_member is not None:
                safe(lambda n=node, m=hole_member: setattr(n, "holePreferencesType", m))

    # Parts-list (BOM) inclusion/placement on both main- and sub-assembly sheet prefs (iso + orthogonal).
    loc_member = members.get("parts_list_location")
    if cfg["parts_list"] is not None or loc_member is not None:
        for path in ("mainAssemblyPreferences", "subAssemblyPreferences"):
            prefs = safe(lambda p=path: getattr(di.automationPreferences, p))
            if prefs is None:
                continue
            for sheet_kind in ("isoViewSheetPreferences", "orthogonalViewSheetPreferences"):
                node = safe(lambda pr=prefs, sk=sheet_kind: getattr(pr, sk))
                if node is None:
                    continue
                if cfg["parts_list"] is not None:
                    safe(lambda n=node: setattr(n, "isPartsListIncluded", cfg["parts_list"]))
                if loc_member is not None:
                    safe(lambda n=node, m=loc_member: setattr(n, "partsListLocationType", m))

    # Per-view drafting display, on the same objects already reached for .style. A resolved enum value
    # can be falsy, so every member is tested against None.
    style_member = members.get("view_style")
    te_member = members.get("tangent_edges")
    if (style_member is not None or te_member is not None
            or cfg["show_interference_edges"] is not None or cfg["show_thread_edges"] is not None):
        for path in _VIEWSTYLE_PATHS:
            node = safe(lambda p=path: getattr(di.automationPreferences, p).drawingViewPreferences)
            if node is None:
                continue
            if style_member is not None:
                safe(lambda n=node, m=style_member: setattr(n, "style", m))
            if te_member is not None:
                safe(lambda n=node, m=te_member: setattr(n, "tangentEdgesType", m))
            if cfg["show_interference_edges"] is not None:
                safe(lambda n=node: setattr(n, "isShowInterferenceEdges", cfg["show_interference_edges"]))
            if cfg["show_thread_edges"] is not None:
                safe(lambda n=node: setattr(n, "isShowThreadEdges", cfg["show_thread_edges"]))

    # Optional isometric view alongside the orthographic set on each component sheet.
    safe(lambda: setattr(
        di.automationPreferences.componentPreferences.sheetViewPreferences,
        "isIsometricViewAdded", bool(cfg["isometric"])))
    return ""


def handler(standard: str = "iso", units: str = "mm", content: str = "full", isometric: bool = True,
            sheet_size: str = "default", orientation: str = "landscape", sheet_scope: str = "all_levels",
            sheet_types=None, auto_dimension: str = "default", omit_fasteners: bool = False,
            fastener_keywords: str = "", view_style: str = "default", parts_list: bool = None,
            parts_list_location: str = "default", template_file: str = "",
            custom_width_mm: float = None, custom_height_mm: float = None,
            hole_annotations: str = "default", center_line: str = "default",
            center_mark: str = "default", tangent_edges: str = "default",
            show_interference_edges: bool = None, show_thread_edges: bool = None,
            creation_mode: str = "automatic") -> dict:
    """See TOOL_DESCRIPTION."""
    std, e = _STANDARD.resolve(standard)
    if e:
        return error(e)
    units_v, e = _UNITS.resolve(units)
    if e:
        return error(e)
    content_v, e = _CONTENT.resolve(content)
    if e:
        return error(e)
    size_v, e = _SHEET_SIZE.resolve(sheet_size)
    if e:
        return error(e)
    orient_v, e = _ORIENTATION.resolve(orientation)
    if e:
        return error(e)
    scope_v, e = _SHEET_SCOPE.resolve(sheet_scope)
    if e:
        return error(e)
    dim_v, e = _AUTO_DIMENSION.resolve(auto_dimension)
    if e:
        return error(e)
    style_v, e = _VIEW_STYLE.resolve(view_style)
    if e:
        return error(e)
    loc_v, e = _PARTS_LIST_LOCATION.resolve(parts_list_location)
    if e:
        return error(e)
    hole_v, e = _HOLE_ANNOTATIONS.resolve(hole_annotations)
    if e:
        return error(e)
    cl_v, e = _CENTER_LINE.resolve(center_line)
    if e:
        return error(e)
    cmk_v, e = _CENTER_MARK.resolve(center_mark)
    if e:
        return error(e)
    te_v, e = _TANGENT_EDGES.resolve(tangent_edges)
    if e:
        return error(e)
    mode_v, e = _CREATION_MODE.resolve(creation_mode)
    if e:
        return error(e)

    # Guard the two real constraints the API silently ignores rather than reports.
    if size_v not in ("default", "custom"):
        need_std = _drawing_common.SHEET_SIZE_MAP[size_v][0]
        if need_std != std:
            fam = [k for k, v in _drawing_common.SHEET_SIZE_MAP.items() if v[0] == std]
            return error(f"sheet_size '{size_v}' is a {need_std.upper()} size but standard is '{std}'. "
                         f"Use an {std.upper()} size ({', '.join(fam)}) or switch the standard.")
        if orient_v == "portrait" and (std, size_v) in _drawing_common.NO_PORTRAIT:
            return error(f"portrait orientation is not supported for the largest {std.upper()} sheet "
                         f"('{size_v}'); use landscape or a smaller sheet.")

    custom_size = None
    if size_v == "custom":
        if not _CUSTOM_SIZE_ENABLED:
            return error(_CUSTOM_DISABLED_REFUSAL)
        if custom_width_mm is None or custom_height_mm is None:
            return error("sheet_size 'custom' requires both custom_width_mm and custom_height_mm.")
        try:
            w_mm, h_mm = float(custom_width_mm), float(custom_height_mm)
        except (TypeError, ValueError):
            return error(f"custom_width_mm and custom_height_mm must be numbers (got "
                         f"{custom_width_mm!r} / {custom_height_mm!r}).")
        if w_mm <= 0 or h_mm <= 0:
            return error(f"custom_width_mm and custom_height_mm must be positive (got {w_mm} / {h_mm}).")
        # The inputs are millimetres; CustomSheetSize takes the DOCUMENT unit, which the standard
        # fixes through the one shared table.
        unit = _drawing_common.DOCUMENT_UNIT[std]
        per_unit = _common.scale("mm") / _common.scale(unit)
        custom_size = {"width": round(w_mm * per_unit, 6), "height": round(h_mm * per_unit, 6),
                       "unit": unit, "zone_minimum": _CUSTOM_ZONES}
    elif custom_width_mm is not None or custom_height_mm is not None:
        return error("custom_width_mm/custom_height_mm only apply when sheet_size='custom'.")

    template_df = None
    tf_raw = (template_file or "").strip()
    if tf_raw:
        template_df, _resolved_tf, tried = _resolve_data_file(tf_raw)
        if not template_df:
            tried_s = ", ".join(tried) if tried else tf_raw
            return error(f"template_file '{tf_raw}' could not be resolved to a file. Tried: {tried_s}. "
                         "Pass a DataFile id/versionId or a fusionWebURL from data_get / "
                         "design_get(include=['tree']).")

    if mode_v == "manual" and template_df is None:
        return error(f"creation_mode 'manual' requires template_file, which was empty. {_MANUAL_GATE} "
                     "This call stops here without creating anything. Pass the template's DataFile "
                     "id/URL as template_file, or use creation_mode 'automatic'.")

    types_v = None
    if sheet_types is not None:
        if not isinstance(sheet_types, (list, tuple)):
            return error("sheet_types must be a list of sheet-type names (e.g. ['component', "
                         "'main_assembly']).")
        bad = [t for t in sheet_types if t not in _SHEET_TYPE_ATTR]
        if bad:
            return error(f"sheet_types has unknown value(s) {bad}. Allowed: "
                         f"{', '.join(_SHEET_TYPE_ATTR)}.")
        types_v = list(sheet_types)

    for input_name, value in (("center_line", cl_v), ("center_mark", cmk_v)):
        if value != "default":
            family, present = _UNREACHABLE_INPUTS[input_name]
            return error(f"{input_name} '{value}' cannot be applied: adsk.drawing has no {family} enum "
                         f"on this Fusion version (the namespace carries {present} classes instead), so "
                         f"the setting has no API to reach and no drawing was created. Leave "
                         f"{input_name} at 'default'.")

    cfg = {
        "creation_mode": mode_v,
        "standard": std, "units": units_v, "content": content_v, "isometric": bool(isometric),
        "sheet_size": size_v, "orientation": orient_v, "sheet_scope": scope_v,
        "sheet_types": types_v, "auto_dimension": dim_v, "omit_fasteners": bool(omit_fasteners),
        "fastener_keywords": (fastener_keywords or "").strip(), "view_style": style_v,
        "parts_list": (bool(parts_list) if parts_list is not None else None),
        "parts_list_location": loc_v,
        "template_file": tf_raw,
        "base_document": "template" if template_df is not None else None,
        "custom_width_mm": custom_width_mm, "custom_height_mm": custom_height_mm,
        "custom_size": custom_size,
        "hole_annotations": hole_v, "center_line": cl_v, "center_mark": cmk_v, "tangent_edges": te_v,
        "show_interference_edges": (bool(show_interference_edges) if show_interference_edges is not None
                                     else None),
        "show_thread_edges": (bool(show_thread_edges) if show_thread_edges is not None else None),
    }

    design = _common.design()
    if not design:
        return error("No active design to draw. Open or create a design first (see doc_new), then retry.")

    src, serr = _source_datafile(design)
    if serr:
        return error(serr)

    dm = safe(lambda: adsk.drawing.DrawingManager.get())
    if not dm:
        return error("DrawingManager is unavailable in this Fusion session - cannot create a drawing.")

    members, e = _resolve_members(cfg)
    if e:
        return error(e)

    modes = adsk.drawing.DrawingCreationModes
    mode_member = (modes.ManualDrawingCreationMode if mode_v == "manual"
                   else modes.AutomaticDrawingCreationMode)
    try:
        di = dm.createDrawingInput(src, mode_member)
    except Exception as ex:
        return error(f"createDrawingInput failed: {ex}")
    if not di:
        return error("createDrawingInput returned null - Fusion could not start a drawing from this design.")

    aerr = _apply_input_settings(di, cfg, members, template_data_file=template_df)
    if aerr:
        return error(aerr)

    try:
        df = dm.createDrawing(di)
    except Exception as ex:
        return error(f"createDrawing failed: {ex}{_processing_lag_hint(ex)}")
    if not df:
        return error("createDrawing returned null - Fusion did not generate a drawing (nothing created).")

    file_id = safe(lambda: df.id)
    if not file_id:
        return error("createDrawing returned a drawing DataFile but no file_id could be read from it, "
                     "so the created drawing cannot be located for export. Treating this as a failure.")

    note = ("Drawing created as a CLOUD file (NOT opened). To reach it: doc_open(file_id, "
            "force_api_open=true), then drawing_export for the PDF - measured on 2705.0.87, a "
            "drawing never reviewed in the Fusion UI opens and drives that way, so no manual step "
            "is needed up front. If that open instead fails or hangs, opening the document once in "
            "the Fusion UI is the known workaround from earlier builds. settings_requested were "
            "applied best-effort to the input (they configure creation and are not read back).")
    if mode_v == "automatic":
        note += (" Manual dimensions/annotations and custom title blocks beyond the automatic "
                 "layout are not placed by this tool.")
    else:
        note += f" creation_mode was 'manual', which Fusion gates on the template: {_MANUAL_GATE}"
    if custom_size is not None:
        note += (f" Custom sheet size: {custom_size['width_applied']} x "
                 f"{custom_size['height_applied']} {custom_size['unit']} (the document unit), "
                 f"{custom_size['horizontal_zones_applied']} x "
                 f"{custom_size['vertical_zones_applied']} zones - every one of those four numbers "
                 "read back off the input before the create, and a zone count the input already "
                 f"carried at or above the {custom_size['zone_minimum']} the API takes was kept. "
                 "The created SHEET's own width/height are not readable from here (this call does "
                 "not open the drawing), so open it and read them with drawing_edit_sheet to "
                 "confirm the sheet Fusion built.")
    note += " " + _TIMEOUT_IS_NOT_A_VERDICT
    return ok({
        "created": True,
        "drawing_name": safe(lambda: df.name),
        "file_id": file_id,
        "version_id": safe(lambda: df.versionId),
        "file_extension": safe(lambda: df.fileExtension),
        "settings_requested": cfg,
        "note": note,
    })


TOOL_DESCRIPTION = (
    "Create a 2D drawing from the active design via Fusion's automatic generator. The result is a "
    "CLOUD file, NOT opened - doc_open the file_id, then drawing_export for the PDF, no UI step. A "
    "client TIMEOUT is not a verdict: the create can still land, so re-check with data_get before "
    "retrying or a retry mints a second drawing."
)

FULL_DESCRIPTION = TOOL_DESCRIPTION + "\n" + _outputs.produces_block(RETURNS)

tool = (
    Tool.create_simple(name="drawing_create", description=FULL_DESCRIPTION)
    .add_input_property(*_STANDARD.as_property())
    .add_input_property(*_UNITS.as_property())
    .add_input_property(*_CONTENT.as_property())
    .add_input_property("isometric", {"type": "boolean"})
    .add_input_property(*_SHEET_SIZE.as_property())
    .add_input_property(*_ORIENTATION.as_property())
    .add_input_property(*_SHEET_SCOPE.as_property())
    .add_input_property("sheet_types", {"type": "array",
            "items": {"type": "string", "enum": list(_SHEET_TYPE_ATTR)},
            "description": "Kinds to enable; others off."})
    .add_input_property(*_AUTO_DIMENSION.as_property())
    .add_input_property("omit_fasteners", {"type": "boolean"})
    .add_input_property("fastener_keywords", {"type": "string"})
    .add_input_property(*_VIEW_STYLE.as_property())
    .add_input_property("parts_list", {"type": "boolean",
            "description": "On assembly sheets."})
    .add_input_property(*_PARTS_LIST_LOCATION.as_property())
    .add_input_property(*_CREATION_MODE.as_property())
    .add_input_property("template_file", {"type": "string",
            "description": "DataFile id/URL; empty = scratch."})
    .add_input_property("custom_width_mm", {"type": "number", "description": "In mm."})
    .add_input_property("custom_height_mm", {"type": "number", "description": "In mm."})
    .add_input_property(*_HOLE_ANNOTATIONS.as_property())
    .add_input_property(*_CENTER_LINE.as_property())
    .add_input_property(*_CENTER_MARK.as_property())
    .add_input_property(*_TANGENT_EDGES.as_property())
    .add_input_property("show_interference_edges", {"type": "boolean"})
    .add_input_property("show_thread_edges", {"type": "boolean"})
    .strict_schema()
)

# enforce_timeout=False: createDrawing is a blocking, uninterruptible main-thread call that can run
# past the server's call timeout even for a small design, and timing it out would report a false
# failure for a drawing that WAS created.
item = Item.create_tool_item(
    tool=tool, write="write", handler=handler, run_on_main_thread=True, enforce_timeout=False,
    # drawing_name / file_id / version_id / file_extension are read off the DataFile createDrawing
    # returned, and a create that hands back nothing or no readable id is an error. settings_requested
    # is the request, published under its own key and named as not read back in the note.
    verification=Verification(
        kind="effect", rung="value",
        evidence_test="tests/unit/test_drawing_create.py::TestGuards"
                      "::test_missing_file_id_on_created_drawing_errors"),
    deferred_capable=True)


def register_tool():
    register(item)
