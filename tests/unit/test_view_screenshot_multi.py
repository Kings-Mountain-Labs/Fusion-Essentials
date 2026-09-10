"""Unit tests for ``view_screenshot_multi.py`` — capture several orthographic/iso views in one call.

view_screenshot captures ONE viewport per call, so reading a model from a single iso
is error-prone. view_screenshot_multi orients to each requested view, captures each as a
separate image, and returns them interleaved with text labels — so an inference
model sees front/top/right/iso together.

Pinned here (no live Fusion): _parse_views (default set, explicit list, aliases,
unknown-view error, dedupe/order), and the handler's composition over the shared
_view_common helpers — the _MAX_VIEWS cap, width/height clamping + the non-numeric
600x500 fallback, per-view orient/capture failure isolation, the all-failed error,
the camera restore in ``finally``, and the ortho-camera-for-faces-only behavior
(via the real apply_named_view). The actual saveAsImageFile pixels are live-only.
"""

from types import SimpleNamespace

import pytest

from conftest import Camera, FakeApplication, FakePoint, Viewport, camera_state, load_tool

cv = load_tool("view_screenshot_multi")


class TestParseViews:
    def test_default_set(self):
        # No argument -> a sensible multi-view default (front/top/right/iso).
        views, err = cv._parse_views("")
        assert err is None
        assert views == ["front", "top", "right", "iso-top-right"]

    def test_explicit_comma_list(self):
        views, err = cv._parse_views("front, top")
        assert err is None and views == ["front", "top"]

    def test_whitespace_and_case_tolerant(self):
        views, err = cv._parse_views("  FRONT , Iso-Top-Right ")
        assert err is None and views == ["front", "iso-top-right"]

    def test_dedupes_preserving_order(self):
        views, err = cv._parse_views("front, top, front")
        assert err is None and views == ["front", "top"]

    def test_unknown_view_errors(self):
        views, err = cv._parse_views("front, sideways")
        assert views is None
        assert "sideways" in err

    def test_all_keyword_expands_to_six_orthos(self):
        views, err = cv._parse_views("all")
        assert err is None
        assert views == ["front", "back", "left", "right", "top", "bottom"]

    def test_only_separators_falls_back_to_default(self):
        # a string of only commas/whitespace yields no real tokens -> the default set, not an empty list
        views, err = cv._parse_views(" , , ")
        assert err is None
        assert views == ["front", "top", "right", "iso-top-right"]

    def test_list_input(self):
        # the schema's native shape (a JSON array), not just the back-compat comma string.
        views, err = cv._parse_views(["front", "top"])
        assert err is None and views == ["front", "top"]

    def test_list_input_all_keyword_expands(self):
        views, err = cv._parse_views(["all"])
        assert err is None
        assert views == ["front", "back", "left", "right", "top", "bottom"]

    def test_empty_list_falls_back_to_default(self):
        views, err = cv._parse_views([])
        assert err is None
        assert views == ["front", "top", "right", "iso-top-right"]


# ── handler fakes ───────────────────────────────────────────────────────────

def _viewport():
    """The rig's viewport, its camera 5 cm out on X carrying a projection no named view uses -
    so a capture that saw the USER's camera type is told apart from one an orient forced."""
    return Viewport(camera=Camera(eye=(5, 0, 0), camera_type="user-camera-type"))


@pytest.fixture
def rig(monkeypatch):
    """Fake viewport + recorded stand-ins for the shared _view_common helpers.

    The handler's own job is COMPOSITION (order views, size images, isolate per-view
    failures, restore the camera); the orient/capture mechanics are pinned in
    test__view_common.py, so they are stubbed here per the router-test idiom."""
    vp = _viewport()
    monkeypatch.setattr(cv, "app", FakeApplication(active_viewport=vp))
    applied, captures = [], []

    def fake_apply(viewport, name):
        applied.append(name)
        viewport.camera = f"cam-{name}"

    switches = []

    def fake_capture(viewport, width, height, prefix="fe_mcp_shot",
                     transparent_background=None, anti_aliased=None):
        captures.append((width, height))
        switches.append((transparent_background, anti_aliased))
        return "B64DATA", None

    monkeypatch.setattr(cv._view_common, "apply_named_view", fake_apply)
    monkeypatch.setattr(cv._view_common, "capture_png_b64", fake_capture)
    return SimpleNamespace(vp=vp, applied=applied, captures=captures, switches=switches)


def _texts(result):
    return [c["text"] for c in result["content"] if c["type"] == "text"]


def _images(result):
    return [c for c in result["content"] if c["type"] == "image"]


class TestViewCap:
    def test_nine_views_in_captures_only_the_first_eight(self, rig):
        nine = ["front", "back", "left", "right", "top", "bottom",
                "iso-top-right", "iso-top-left", "iso-bottom-right"]
        result = cv.handler(views=nine)
        assert result["isError"] is False
        assert rig.applied == nine[:8]          # the ninth view is never oriented
        assert len(_images(result)) == 8
        summary = _texts(result)[0]
        assert "Captured 8 view(s)" in summary
        # the truncation is REPORTED: the summary names the dropped view over the cap.
        assert "Dropped 1 view(s)" in summary
        assert "iso-bottom-right" in summary


class TestDimensionClamp:
    def test_oversize_dimensions_clamp_to_4096(self, rig):
        cv.handler(views=["front"], width=10000, height=99999)
        assert rig.captures == [(4096, 4096)]

    def test_zero_and_negative_dimensions_clamp_to_1(self, rig):
        cv.handler(views=["front"], width=0, height=-20)
        assert rig.captures == [(1, 1)]

    def test_non_numeric_width_keeps_the_valid_height(self, rig):
        # int('abc') resets ONLY the bad width to its default; the valid height=300 is kept
        # (each dimension is clamped independently).
        cv.handler(views=["front"], width="abc", height=300)
        assert rig.captures == [(600, 300)]

    def test_non_numeric_height_keeps_the_valid_width(self, rig):
        cv.handler(views=["front"], width=800, height="oops")
        assert rig.captures == [(800, 500)]


class TestCaptureSwitchPassThrough:
    """The capture switches reach EVERY view's grab, and stay absent when omitted - an
    omitted-switch call takes the plain capture path."""

    def test_omitting_both_leaves_every_view_on_the_plain_path(self, rig):
        cv.handler(views=["front", "top"])
        assert rig.switches == [(None, None), (None, None)]

    def test_switches_apply_to_every_captured_view(self, rig):
        cv.handler(views=["front", "top", "right"], transparent_background=True, anti_aliased=False)
        assert rig.switches == [(True, False)] * 3

    def test_transparent_background_false_is_forwarded_not_dropped(self, rig):
        cv.handler(views=["front"], transparent_background=False)
        assert rig.switches == [(False, None)]


class TestPerViewFailureIsolation:
    def test_one_orient_failure_reports_that_row_and_keeps_the_rest(self, rig, monkeypatch):
        def flaky_apply(viewport, name):
            if name == "top":
                raise ValueError("boom")
            rig.applied.append(name)

        monkeypatch.setattr(cv._view_common, "apply_named_view", flaky_apply)
        result = cv.handler(views=["front", "top", "right"])
        assert result["isError"] is False
        assert "[top] failed to orient: boom" in _texts(result)
        assert len(_images(result)) == 2
        assert len(rig.captures) == 2           # no capture attempt for the failed view
        assert "Captured 2 view(s): front, right" in _texts(result)[0]

    def test_one_capture_failure_reports_that_row_and_keeps_the_rest(self, rig, monkeypatch):
        calls = {"n": 0}

        def flaky_capture(viewport, width, height, prefix="fe_mcp_shot", **switches):
            calls["n"] += 1
            if calls["n"] == 2:                 # the second view ("top") fails to grab
                return None, "saveAsImageFile returned false"
            return "B64DATA", None

        monkeypatch.setattr(cv._view_common, "capture_png_b64", flaky_capture)
        result = cv.handler(views=["front", "top", "right"])
        assert result["isError"] is False
        assert "[top] capture failed." in _texts(result)
        assert len(_images(result)) == 2
        assert "Captured 2 view(s): front, right" in _texts(result)[0]

    def test_every_view_failing_is_an_error_not_an_empty_ok(self, rig, monkeypatch):
        monkeypatch.setattr(cv._view_common, "capture_png_b64",
                            lambda vp, w, h, prefix="fe_mcp_shot", **switches: (None, "nope"))
        result = cv.handler(views=["front", "top"])
        assert result["isError"] is True
        assert result["message"] == "No views were captured."


class TestCameraRestore:
    def test_original_camera_is_reasserted_after_a_successful_run(self, rig):
        before = camera_state(rig.vp._original_camera)
        cv.handler(views=["front", "top"])
        # apply assigned per-view cameras; the finally must put the SAVED camera back last. A
        # viewport read hands back a copy, so the restore is judged on the STATE put back.
        assert camera_state(rig.vp._assigned[-1]) == before

    def test_original_camera_is_reasserted_even_when_a_capture_raises(self, rig, monkeypatch):
        def exploding_capture(viewport, width, height, prefix="fe_mcp_shot", **switches):
            raise RuntimeError("disk full")

        before = camera_state(rig.vp._original_camera)
        monkeypatch.setattr(cv._view_common, "capture_png_b64", exploding_capture)
        with pytest.raises(RuntimeError):
            cv.handler(views=["front"])
        assert rig.vp._assigned and camera_state(rig.vp._assigned[-1]) == before

    def test_a_camera_restore_the_viewport_refuses_is_named_on_the_summary(self, rig,
                                                                           monkeypatch):
        # write="read": a camera this tool cannot put back leaves the viewport moved BY A READ.
        # Swallowing that in the finally reported a clean multi-shot over a changed document.
        # The per-view orients still land; only putting the ORIGINAL camera back is refused, which
        # is the state that leaves the viewport moved after the call returns.
        # The camera the tool saves is the FIRST copy it reads; refusing that object alone leaves
        # the per-view orients landing, which is the state this test is about.
        reads = []

        def read_a_copy(self):
            reads.append(self._cam._copy())
            return reads[-1]

        def refuse_the_restore(self, value):
            if reads and value is reads[0]:
                raise RuntimeError("viewport busy")
            self._assigned.append(value)
        monkeypatch.setattr(type(rig.vp), "camera", property(read_a_copy, refuse_the_restore))
        result = cv.handler(views=["front", "top"])
        assert result["isError"] is False                 # the images were still captured
        summary = _texts(result)[0]
        assert "could NOT be put back" in summary and "viewport busy" in summary

    def test_a_clean_camera_restore_adds_nothing_to_the_summary(self, rig):
        result = cv.handler(views=["front"])
        assert "could NOT be put back" not in _texts(result)[0]


class TestStandoffFallbackDisclosure:
    """apply_named_view answers the standoff it fell back to when the camera's eye-target distance
    did not read as a positive number: the eye is placed at that standoff before the fit. A sheet
    where only some views did that must name WHICH, since silence means the camera's own distance."""

    def test_only_the_views_framed_from_the_fallback_are_named(self, rig, monkeypatch):
        def apply(viewport, name):
            viewport.camera = f"cam-{name}"
            return 100.0 if name == "top" else None

        monkeypatch.setattr(cv._view_common, "apply_named_view", apply)
        summary = _texts(cv.handler(views=["front", "top"]))[0]
        assert "standoff_fallback_cm=100 on top:" in summary
        # ONE rendering of the number, in both places it is spelled
        assert "100 cm from the target" in summary and "100.0" not in summary
        assert "front" not in summary.split("standoff_fallback_cm")[1]

    def test_a_sheet_the_camera_framed_itself_adds_nothing(self, rig):
        assert "standoff_fallback_cm" not in _texts(cv.handler(views=["front", "top"]))[0]

    def test_a_view_that_fell_back_but_never_captured_is_not_claimed(self, rig, monkeypatch):
        # 'top' orients off the fallback and then fails to grab - naming it would report a
        # standoff for an image the sheet does not carry
        monkeypatch.setattr(cv._view_common, "apply_named_view", lambda vp, name: 100.0)

        calls = {"n": 0}

        def flaky_capture(vp, w, h, prefix="fe_mcp_shot", **switches):
            calls["n"] += 1
            return ("B64DATA", None) if calls["n"] == 1 else (None, "saveAsImageFile returned false")

        monkeypatch.setattr(cv._view_common, "capture_png_b64", flaky_capture)
        summary = _texts(cv.handler(views=["front", "top"]))[0]
        assert "standoff_fallback_cm=100 on front:" in summary
        assert "top" not in summary.split("standoff_fallback_cm")[1]


@pytest.fixture
def rig_real_orient(monkeypatch):
    """Fake viewport but the REAL _view_common.apply_named_view, so the handler-to-helper
    composition that decides camera TYPE per view is exercised end to end. Point3D/Vector3D
    construction is redirected to plain fakes so the camera math runs on real floats."""
    import adsk.core
    vp = _viewport()
    monkeypatch.setattr(cv, "app", FakeApplication(active_viewport=vp))
    monkeypatch.setattr(adsk.core.Point3D, "create", lambda x, y, z: FakePoint(x, y, z))
    monkeypatch.setattr(adsk.core.Vector3D, "create", lambda x, y, z: (x, y, z))
    cam_types = []

    def fake_capture(viewport, width, height, prefix="fe_mcp_shot", **switches):
        cam_types.append(viewport.camera.cameraType)   # camera type at the capture moment
        return "B64DATA", None

    monkeypatch.setattr(cv._view_common, "capture_png_b64", fake_capture)
    return SimpleNamespace(vp=vp, cam_types=cam_types)


class TestOrthoCameraType:
    def test_all_six_face_views_capture_with_an_orthographic_camera(self, rig_real_orient):
        import adsk.core
        result = cv.handler(views="all")
        assert result["isError"] is False
        assert len(rig_real_orient.cam_types) == 6
        assert all(t is adsk.core.CameraTypes.OrthographicCameraType
                   for t in rig_real_orient.cam_types)

    def test_iso_view_keeps_the_user_camera_type(self, rig_real_orient):
        import adsk.core
        # iso first (before any face view has forced ortho): the iso capture must see the
        # user's own camera type untouched; the face view after it gets ortho.
        cv.handler(views=["iso-top-right", "front"])
        assert rig_real_orient.cam_types[0] == "user-camera-type"
        assert rig_real_orient.cam_types[1] is adsk.core.CameraTypes.OrthographicCameraType
