"""Unit tests for ``drawing_insert_image.py`` - place an image file on the active drawing sheet.

Covers: the file-exists refusal BEFORE any Fusion call (a raise inside createInput/insert rolls the
whole script transaction back), the sheet position in the drawing's own length unit (reported from
the drawing's own setting - never a guessed default), the OFF-SHEET refusal (an anchor outside the
sheet inserts successfully and renders nothing, which neither the insert boolean nor the modified
flag can tell from a real placement), the input-did-not-take read-backs, and the
honesty gate - insert returning true while the document stays unmodified is a failure, and an
UNREADABLE modified flag is published as null, never false, since the Images collection exposes no
count, item or delete. No live Fusion.
"""

import json
import math
import types

import pytest

import adsk  # the mock package conftest installed at import time
from conftest import FakeImages, FakeSheet, load_tool, make_drawing, make_drawing_session

ins = load_tool("drawing_insert_image")


class _PathIgnoringInput:
    """An ImageInsertInput whose imageFilePath assignment silently does not take - the SWIG-proxy
    shape the handler's read-back guards against. ImageInsertInput carries no shape dump."""

    imageFilePath = property(lambda self: "", lambda self, value: None)

    def __init__(self):
        self.position = None
        self.scale = None


class _RecordingInput:
    """An ImageInsertInput that RECORDS which properties were assigned; no shape dump exists for it.

    rotationAngle's API default is 0.0, so a payload read alone cannot tell an omitted rotation from
    one written as zero - the assignment set can."""

    def __init__(self):
        object.__setattr__(self, "assigned", set())
        object.__setattr__(self, "imageFilePath", "")
        object.__setattr__(self, "position", None)
        object.__setattr__(self, "scale", None)
        object.__setattr__(self, "rotationAngle", 0.0)

    def __setattr__(self, name, value):
        self.assigned.add(name)
        object.__setattr__(self, name, value)


class _RotationIgnoringInput(_RecordingInput):
    """An input whose rotationAngle assignment silently does not take - the SWIG-proxy shape that
    would otherwise place an UNROTATED image while the payload claims the angle landed."""

    def __setattr__(self, name, value):
        if name == "rotationAngle":
            self.assigned.add(name)
            return
        super().__setattr__(name, value)


def _payload(res):
    assert res["isError"] is False, res
    return json.loads(res["content"][0]["text"])


@pytest.fixture
def env(monkeypatch):
    """An active drawing document with one ISO A3 millimetre sheet and a recording Images
    collection. The whole adsk.drawing surface the tool and _drawing_common read goes in wholesale,
    so this file does not depend on what a sibling drawing test file left behind."""
    state = types.SimpleNamespace()

    def _install(units="mm", standard="iso", width=420.0, height=297.0, modified_raises=None,
                 **sheet_knobs):
        # width/height are MILLIMETRES on every drawing whatever the drawing's own units read -
        # they are what an anchor is bounded against.
        sheet = FakeSheet("Sheet1", images=FakeImages(input_factory=_RecordingInput),
                          width=width, height=height, **sheet_knobs)
        sheet.images._sheet = sheet
        state.document = make_drawing(sheets=[sheet], units=units, standard=standard,
                                      modified_raises=modified_raises)
        state.sheet = sheet
        state.images = sheet.images
        state.settings = state.document.drawing.documentSettings
        make_drawing_session(monkeypatch, state.document)
        monkeypatch.setattr(adsk.core.Point2D, "create", lambda x, y: ("pt", x, y))
        return state.document

    _install()
    state.install = _install
    state.last_input = lambda: state.images._inputs[-1]
    return state


@pytest.fixture
def image_file(tmp_path):
    p = tmp_path / "logo.png"
    p.write_bytes(b"PNG-STUB")
    return str(p)


class TestHappyPath:
    def test_places_the_image_at_the_requested_sheet_position(self, env, image_file):
        out = _payload(ins.handler(image_path=image_file, x=60, y=100, scale=0.25))
        assert out["inserted"] is True
        assert out["image_path"] == image_file
        assert out["position"] == [60.0, 100.0]
        assert out["scale"] == 0.25
        assert out["sheet"] == "Sheet1"
        inp = env.last_input()
        assert inp.imageFilePath == image_file
        assert inp.position == ("pt", 60.0, 100.0)      # x/y in sheet order, not swapped
        assert inp.scale == 0.25
        assert env.images._inserts == [inp]

    def test_omitted_scale_leaves_the_api_default_and_reports_null(self, env, image_file):
        out = _payload(ins.handler(image_path=image_file, x=1, y=2))
        assert out["scale"] is None
        assert env.last_input().scale is None           # never assigned - no invented 1.0

    def test_declared_returns_are_present(self, env, image_file):
        out = _payload(ins.handler(image_path=image_file, x=1, y=2))
        for spec in ins.RETURNS:
            assert spec.assert_present(out) == "", spec.assert_present(out)


class TestRotation:
    """ImageInsertInput.rotationAngle is RADIANS about the insert position - a pi assignment renders
    the image a half turn round its anchor, while the same number taken as degrees would leave it
    visually put. The caller works in degrees, so the conversion is the contract this pins."""

    @pytest.mark.parametrize("deg, rad", [(180, math.pi), (90, math.pi / 2), (-45, -math.pi / 4)])
    def test_degrees_are_converted_to_radians_on_the_input(self, env, image_file, deg, rad):
        out = _payload(ins.handler(image_path=image_file, x=1, y=2, rotate_deg=deg))
        assert env.last_input().rotationAngle == pytest.approx(rad)
        assert out["rotate_deg"] == float(deg)              # what the caller passed, back verbatim
        assert out["rotation_radians"] == pytest.approx(rad)

    def test_the_degrees_number_itself_never_reaches_the_input(self, env, image_file):
        # the conversion's DIRECTION: 180 landing as 180.0 would be degrees-into-a-radians property,
        # the exact silent mis-rotation the measurement rules out
        ins.handler(image_path=image_file, x=1, y=2, rotate_deg=180)
        assert env.last_input().rotationAngle != 180.0

    def test_zero_degrees_is_still_written_and_reported(self, env, image_file):
        # 0 is falsy - a truthiness test here would silently drop an explicit no-rotation request
        out = _payload(ins.handler(image_path=image_file, x=1, y=2, rotate_deg=0))
        assert "rotationAngle" in env.last_input().assigned
        assert out["rotate_deg"] == 0.0 and out["rotation_radians"] == 0.0

    def test_omitted_rotation_is_never_assigned_and_reports_null(self, env, image_file):
        out = _payload(ins.handler(image_path=image_file, x=1, y=2))
        assert "rotationAngle" not in env.last_input().assigned   # the API's own default stands
        assert out["rotate_deg"] is None and out["rotation_radians"] is None

    def test_non_numeric_rotation_is_refused_naming_the_value(self, env, image_file):
        res = ins.handler(image_path=image_file, x=1, y=2, rotate_deg="sideways")
        assert res["isError"] is True
        assert "rotate_deg" in res["message"] and "'sideways'" in res["message"]
        assert env.images._inputs == []

    def test_a_rotation_that_does_not_take_is_refused_before_the_insert(self, env, image_file):
        env.images._input_factory = _RotationIgnoringInput
        res = ins.handler(image_path=image_file, x=1, y=2, rotate_deg=90)
        assert res["isError"] is True
        assert "did not take" in res["message"]
        assert env.images._inserts == []      # no unrotated image left on the sheet

    def test_the_rotation_input_states_degrees_about_the_position(self):
        # the caller works in degrees while the API takes radians, and the turn is about the
        # image's own position, not the sheet's origin - both on the input that takes the number
        props = ins.tool.to_dict()["inputSchema"]["properties"]
        assert "Degrees" in props["rotate_deg"]["description"]
        assert "position" in props["rotate_deg"]["description"]


class TestImageFormats:
    """bmp, jpg, jpeg, png, tif and tiff are the extensions measured to both insert AND render on a
    sheet - the two aliases land exactly as their four-letter forms do - so they are what the guard
    takes, and nothing wider is measured. A file the guard accepts but no decoder can read inserts
    true and renders nothing, which is why the note hands that responsibility to the caller."""

    MEASURED = (".bmp", ".jpg", ".jpeg", ".png", ".tif", ".tiff")

    @pytest.mark.parametrize("ext", [".bmp", ".jpg", ".jpeg", ".png", ".tif", ".tiff"])
    def test_each_measured_extension_is_accepted(self, env, tmp_path, ext):
        p = tmp_path / ("logo" + ext)
        p.write_bytes(b"IMG-STUB")
        out = _payload(ins.handler(image_path=str(p), x=1, y=2))
        assert out["inserted"] is True

    def test_an_unmeasured_extension_is_refused_naming_the_measured_set(self, env, tmp_path):
        p = tmp_path / "logo.gif"
        p.write_bytes(b"GIF-STUB")
        res = ins.handler(image_path=str(p), x=1, y=2)
        assert res["isError"] is True
        assert "'.gif'" in res["message"]
        for ext in self.MEASURED:
            assert ext in res["message"]
        assert env.images._inputs == []       # refused before Fusion is touched

    def test_the_extension_check_is_case_insensitive(self, env, tmp_path):
        p = tmp_path / "LOGO.PNG"
        p.write_bytes(b"PNG-STUB")
        out = _payload(ins.handler(image_path=str(p), x=1, y=2))
        assert out["inserted"] is True

    def test_an_extensionless_path_is_refused_naming_the_path(self, env, tmp_path):
        p = tmp_path / "logo"
        p.write_bytes(b"PNG-STUB")
        res = ins.handler(image_path=str(p), x=1, y=2)
        assert res["isError"] is True
        assert str(p) in res["message"]

    def test_no_wire_string_promises_an_extension_the_guard_refuses(self):
        # the measured set is named in the refusal a wrong extension meets
        # (test_an_unmeasured_extension_is_refused_naming_the_measured_set), so the input must not
        # carry a second, driftable copy of it
        desc = ins.tool.to_dict()["inputSchema"]["properties"]["image_path"]["description"]
        for ext in self.MEASURED:
            assert ext not in desc


class TestSheetUnits:
    def test_units_follow_the_drawings_own_setting(self, env, image_file):
        out = _payload(ins.handler(image_path=image_file, x=1, y=2))
        assert out["sheet_units"] == "mm"
        env.install(units="in")
        out = _payload(ins.handler(image_path=image_file, x=1, y=2))
        assert out["sheet_units"] == "in"

    def test_unreadable_units_are_published_as_null_not_guessed_mm(self, env, image_file):
        env.document.drawing.documentSettings = None
        out = _payload(ins.handler(image_path=image_file, x=1, y=2))
        assert out["sheet_units"] is None

    def test_unrecognised_unit_value_is_published_as_null(self, env, image_file):
        env.settings.units = 99
        out = _payload(ins.handler(image_path=image_file, x=1, y=2))
        assert out["sheet_units"] is None


class TestCoordinateUnit:
    def test_a_split_drawing_keys_the_position_to_the_standard_not_the_dimension_unit(
            self, env, image_file):
        # standard='iso' with units='inch' is mintable by drawing_create's own split Choices, and
        # sheet coordinates are drawing length units - millimetres when the standard includes ISO.
        # Labelling x/y with the dimension unit is wrong by 25.4x on this very drawing.
        env.install(units="in", standard="iso")
        out = _payload(ins.handler(image_path=image_file, x=60, y=100))
        assert out["coordinate_unit"] == "mm"
        assert out["sheet_units"] == "in"

    def test_an_asme_drawing_places_in_inches(self, env, image_file):
        env.install(standard="asme")
        out = _payload(ins.handler(image_path=image_file, x=1, y=2))
        assert out["coordinate_unit"] == "in"
        assert out["sheet_units"] == "mm"

    def test_an_unreadable_standard_is_published_as_null_not_guessed(self, env, image_file):
        env.settings.standard = 99
        out = _payload(ins.handler(image_path=image_file, x=1, y=2))
        assert out["coordinate_unit"] is None
        assert out["sheet_units"] == "mm"

    def test_no_wire_string_asserts_a_unit_rule_for_the_position(self):
        # An image position is standard-keyed - millimetres under ISO, inches under ASME, from a
        # corner origin - and the tool holds that rule in its bound, not on the wire: the caller
        # meets it at the failure moment, in an off-sheet refusal that names the unit, the
        # converted figure and the sheet's extent (pinned by TestOffSheetPosition::
        # test_an_asme_anchor_is_bounded_in_inches_against_the_millimetre_extent).
        desc = ins.tool.to_dict()["description"]
        assert "mm under ISO" not in desc and "under ASME" not in desc
        # the frame the numbers are in rides on the inputs that take them, naming no one unit
        props = ins.tool.to_dict()["inputSchema"]["properties"]
        assert "coordinate unit" in props["x"]["description"]

    def test_neither_axis_input_asserts_a_unit_for_the_position(self):
        props = ins.tool.to_dict()["inputSchema"]["properties"]
        for axis in ("x", "y"):
            desc = props[axis]["description"]
            assert "coordinate unit" in desc
            assert "ISO" not in desc and "ASME" not in desc and "mm" not in desc


class TestInputGuards:
    def test_missing_file_is_refused_before_any_fusion_call(self, env, tmp_path):
        res = ins.handler(image_path=str(tmp_path / "nope.png"), x=1, y=2)
        assert res["isError"] is True
        assert "not found" in res["message"]
        assert env.images._inputs == []                # nothing entered the Fusion transaction

    def test_empty_path_is_refused(self, env):
        res = ins.handler(x=1, y=2)
        assert res["isError"] is True
        assert "image_path" in res["message"]

    def test_missing_position_is_refused(self, env, image_file):
        res = ins.handler(image_path=image_file, x=1)
        assert res["isError"] is True
        assert "'x' and 'y'" in res["message"]
        assert env.images._inputs == []

    def test_non_numeric_position_is_refused(self, env, image_file):
        res = ins.handler(image_path=image_file, x="left", y=2)
        assert res["isError"] is True
        assert "must be numbers" in res["message"]

    def test_non_positive_scale_is_refused_naming_the_value(self, env, image_file):
        res = ins.handler(image_path=image_file, x=1, y=2, scale=0)
        assert res["isError"] is True
        assert "greater than 0" in res["message"]
        assert env.images._inputs == []

    def test_image_path_that_does_not_take_is_refused(self, env, image_file):
        env.images._input_factory = _PathIgnoringInput
        res = ins.handler(image_path=image_file, x=1, y=2)
        assert res["isError"] is True
        assert "did not take" in res["message"]
        assert env.images._inserts == []

    def test_active_document_that_is_not_a_drawing_is_refused(self, env, image_file, monkeypatch):
        make_drawing_session(monkeypatch, object())
        res = ins.handler(image_path=image_file, x=1, y=2)
        assert res["isError"] is True
        assert "not a drawing" in res["message"]


class TestOffSheetPosition:
    # The one failure BOTH halves of this tool's verification budget miss: an insert anchored off
    # the sheet returns true and flips the document to modified while rendering nothing, and the
    # Images collection cannot be read back to notice. So the anchor is bounded before the call.
    def test_a_position_past_the_sheet_width_is_refused_before_any_fusion_call(self, env,
                                                                               image_file):
        res = ins.handler(image_path=image_file, x=500, y=100)
        assert res["isError"] is True
        assert "off sheet 'Sheet1'" in res["message"] and "0 to 420.0" in res["message"]
        # an ISO position IS millimetres, so the converted figure is the number the caller gave
        assert "(500.0, 100.0) mm is 500.0 x 100.0 mm" in res["message"]
        assert env.images._inputs == []
        assert env.images._inserts == []

    def test_a_negative_position_is_refused(self, env, image_file):
        res = ins.handler(image_path=image_file, x=10, y=-1)
        assert res["isError"] is True
        assert "off sheet" in res["message"]

    def test_a_position_on_the_sheet_edge_is_allowed(self, env, image_file):
        out = _payload(ins.handler(image_path=image_file, x=420, y=297))
        assert out["inserted"] is True

    def test_an_asme_anchor_is_bounded_in_inches_against_the_millimetre_extent(self, env,
                                                                               image_file):
        # measured: on a 508 x 254 mm ASME sheet an image anchored at (5, 3) rendered at the 5in =
        # 127mm spot and one at (100, 50) rendered nothing - the position is INCHES while the
        # extent stays millimetres, so comparing the raw numbers would pass an anchor 2540 mm out
        env.install(standard="asme", width=508.0, height=254.0)
        out = _payload(ins.handler(image_path=image_file, x=5, y=3))
        assert out["inserted"] is True and out["position_bounds_checked"] is True
        res = ins.handler(image_path=image_file, x=100, y=50)
        assert res["isError"] is True
        assert "(100.0, 50.0) in is 2540.0 x 1270.0 mm" in res["message"]
        assert "0 to 508.0 x 0 to 254.0 mm" in res["message"]

    def test_an_unreadable_standard_with_a_readable_sheet_still_says_unchecked(self, env,
                                                                              image_file):
        # the silent-skip this closes: the extent reads fine, the unit does not, and the insert
        # went through claiming nothing at all about the position it never checked
        env.settings.standard = 99
        out = _payload(ins.handler(image_path=image_file, x=99999, y=99999))
        assert out["inserted"] is True
        assert out["position_bounds_checked"] is False
        assert "NOT bounds-checked" in out["note"] and "standard is unreadable" in out["note"]

    def test_an_unmeasurable_sheet_says_the_position_was_not_checked(self, env, image_file):
        # nothing is guessed when the extent cannot be read - the caller is told the one thing it
        # then has to check itself
        env.install(width_raises="the sheet proxy will not report a width")
        out = _payload(ins.handler(image_path=image_file, x=9999, y=9999))
        assert out["sheet_extent"] == [None, 297.0]
        assert out["position_bounds_checked"] is False
        assert "NOT bounds-checked" in out["note"] and "width and a height" in out["note"]

    def test_the_payload_publishes_the_sheet_extent_it_bounded_against(self, env, image_file):
        out = _payload(ins.handler(image_path=image_file, x=60, y=100))
        assert out["sheet_extent"] == [420.0, 297.0]
        assert out["sheet_extent_unit"] == "mm"
        assert out["position_bounds_checked"] is True

    def test_an_off_sheet_position_is_named_even_when_the_file_is_missing(self, env, tmp_path):
        # the bound is INDEPENDENT of the file: a caller whose file is also missing must still be
        # able to see that the position is off the sheet, and the extent it was measured against
        res = ins.handler(image_path=str(tmp_path / "nope.png"), x=500, y=100)
        assert res["isError"] is True
        assert "off sheet 'Sheet1'" in res["message"]
        assert "0 to 420.0 x 0 to 297.0 mm" in res["message"]

    def test_a_missing_file_and_an_off_sheet_position_name_both_facts(self, env, tmp_path):
        # asserted by EQUALITY, like the single-failure tests: the two refusals are the two
        # sentences each states alone, joined by ONE space - a run-together message is a
        # different (unreadable) promise
        missing = str(tmp_path / "nope.png")
        res = ins.handler(image_path=missing, x=500, y=100)
        assert res["isError"] is True
        assert res["message"] == (
            f"Image file not found: {missing}. Pass a local path that exists (a cloud file must "
            "be downloaded first - see data_download_file). "
            "position (500.0, 100.0) mm is 500.0 x 100.0 mm, off sheet 'Sheet1', which spans 0 to "
            "420.0 x 0 to 297.0 mm. An off-sheet insert returns success and renders nothing, and "
            "an image cannot be read back or moved afterwards, so nothing was placed. Pass a "
            "position inside the sheet.")
        assert env.images._inputs == [] and env.images._inserts == []

    def test_a_missing_file_with_a_good_position_still_reads_as_the_file_refusal_alone(
            self, env, tmp_path):
        missing = str(tmp_path / "nope.png")
        res = ins.handler(image_path=missing, x=60, y=100)
        assert res["message"] == (f"Image file not found: {missing}. Pass a local path that exists "
                                  "(a cloud file must be downloaded first - see data_download_file).")

    def test_an_off_sheet_position_with_a_present_file_still_reads_as_the_bounds_refusal_alone(
            self, env, image_file):
        res = ins.handler(image_path=image_file, x=500, y=100)
        assert "not found" not in res["message"]
        assert res["message"].startswith("position (500.0, 100.0) mm")

    def test_a_missing_file_and_a_non_numeric_position_name_both_facts(self, env, tmp_path):
        missing = str(tmp_path / "nope.png")
        res = ins.handler(image_path=missing, x="left", y=2)
        assert res["isError"] is True
        assert "Image file not found" in res["message"] and "must be numbers" in res["message"]

    def test_the_payload_states_the_refusal_the_guard_enforces(self, env, image_file):
        # no wire string may sell an unconditional refusal the ASME/unreadable paths do not
        # deliver: the payload names which way THIS call went, and the note says so when it could
        # not bound the anchor at all
        assert "REFUSED" not in ins.tool.to_dict()["description"]
        assert _payload(ins.handler(image_path=image_file, x=60, y=100))[
            "position_bounds_checked"] is True
        env.install(width_raises="the sheet proxy will not report a width")
        out = _payload(ins.handler(image_path=image_file, x=60, y=100))
        assert out["position_bounds_checked"] is False and "NOT bounds-checked" in out["note"]


class TestPathReadBack:
    def test_a_path_that_reads_back_different_is_refused(self, env, tmp_path):
        # imageFilePath reads back EXACTLY the string assigned - separators are not normalised - so
        # a read-back that differs means the assignment did not land, not that Fusion tidied it
        forward = str(tmp_path / "logo.png").replace("\\", "/")
        (tmp_path / "logo.png").write_bytes(b"PNG-STUB")

        normalising = type("NormalisingInput", (), {
            "imageFilePath": property(lambda self: getattr(self, "_p", ""),
                                      lambda self, v: object.__setattr__(
                                          self, "_p", v.replace("/", "\\"))),
            "position": None, "scale": None})
        env.images._input_factory = normalising
        res = ins.handler(image_path=forward, x=1, y=2)
        assert res["isError"] is True
        assert "did not take" in res["message"]
        assert env.images._inserts == []

    def test_a_forward_slash_path_that_reads_back_verbatim_is_accepted(self, env, tmp_path):
        p = tmp_path / "logo.png"
        p.write_bytes(b"PNG-STUB")
        forward = str(p).replace("\\", "/")
        out = _payload(ins.handler(image_path=forward, x=1, y=2))
        assert out["image_path"] == forward
        assert env.last_input().imageFilePath == forward


class TestEffectHonesty:
    def test_insert_false_is_a_failure(self, env, image_file):
        env.images._insert_ok = False
        res = ins.handler(image_path=image_file, x=1, y=2)
        assert res["isError"] is True
        assert "returned false" in res["message"]

    def test_success_that_leaves_the_document_unmodified_is_a_failure(self, env, image_file):
        env.images._modifies = False           # the API says true and changes nothing
        res = ins.handler(image_path=image_file, x=1, y=2)
        assert res["isError"] is True
        assert "still unmodified" in res["message"]

    def test_already_modified_document_is_reported_as_inconclusive(self, env, image_file):
        env.document.isModified = True
        out = _payload(ins.handler(image_path=image_file, x=1, y=2))
        assert out["document_modified_before"] is True
        assert "ALREADY modified" in out["note"]

    def test_unreadable_modified_flag_is_published_as_null_not_false(self, env, image_file):
        env.install(modified_raises="isModified unavailable")
        out = _payload(ins.handler(image_path=image_file, x=1, y=2))
        assert out["document_modified"] is None
        assert out["document_modified_before"] is None
        assert "could not be read" in out["note"]

    def test_note_states_the_image_cannot_be_read_back_or_removed(self, env, image_file):
        out = _payload(ins.handler(image_path=image_file, x=1, y=2))
        assert "no count, item or delete" in out["note"]

    def test_note_hands_file_decodability_to_the_caller(self, env, image_file):
        # the false-success class the extension guard CANNOT close: a file with an accepted
        # extension that no decoder can read inserts true and renders nothing, and no read-back
        # exists to notice - so the caller is told, since nothing here can check it
        out = _payload(ins.handler(image_path=image_file, x=1, y=2))
        assert "no decoder can read inserts successfully and renders nothing" in out["note"]
        assert "caller's responsibility" in out["note"]
