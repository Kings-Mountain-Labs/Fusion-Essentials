"""Unit tests for ``_view_common.py`` - the shared camera-orientation table for the standard
named views. view_screenshot and view_set must produce the SAME camera for a given named
view even though they consume opposite sign conventions (view_screenshot's look_direction is the
negation of view_set's view_direction); this pins that relationship so the two can't silently
desync.
"""

import math
from types import SimpleNamespace

import pytest

import live_api_facts
from conftest import (BRepBody, Camera, FakePoint, MakeComp, Viewport, body_proxy, camera_state,
                      load_tool, make_occurrence, make_source_document)

vc = load_tool("_view_common")

_NAMED_VIEWS = ("front", "back", "top", "bottom", "right", "left",
                "iso-top-right", "iso-top-left", "iso-bottom-right", "iso-bottom-left")


def _unit(v):
    return math.isclose(sum(c * c for c in v) ** 0.5, 1.0, abs_tol=1e-9)


class TestViewDirection:
    def test_known_views_return_unit_vectors(self):
        for name in _NAMED_VIEWS:
            assert _unit(vc.view_direction(name)), name

    def test_unknown_view_returns_none(self):
        assert vc.view_direction("banana") is None
        assert vc.view_direction("current") is None

    def test_front_points_toward_minus_y(self):
        assert vc.view_direction("front") == (0.0, -1.0, 0.0)

    def test_top_points_toward_plus_z(self):
        assert vc.view_direction("top") == (0.0, 0.0, 1.0)

    def test_right_points_toward_plus_x(self):
        assert vc.view_direction("right") == (1.0, 0.0, 0.0)


class TestIsoCornersMirrorAcrossZ:
    """Fusion is Z-up, so an iso-BOTTOM view must put the eye BELOW the model. A positive eye z on an
    iso-bottom-* entry aims the camera down at the TOP face - the same image its iso-top twin gives,
    which makes the two names indistinguishable and a visual bottom-side check worthless."""

    @pytest.mark.parametrize("name", ("iso-bottom-right", "iso-bottom-left"))
    def test_iso_bottom_eye_is_below_the_model(self, name):
        assert vc.view_direction(name)[2] < 0, name

    @pytest.mark.parametrize("name", ("iso-top-right", "iso-top-left"))
    def test_iso_top_eye_is_above_the_model(self, name):
        assert vc.view_direction(name)[2] > 0, name

    @pytest.mark.parametrize("top,bottom", [("iso-top-right", "iso-bottom-right"),
                                            ("iso-top-left", "iso-bottom-left")])
    def test_each_iso_bottom_mirrors_its_top_twin_across_z(self, top, bottom):
        tx, ty, tz = vc.view_direction(top)
        assert vc.view_direction(bottom) == (tx, ty, -tz)

    @pytest.mark.parametrize("right,left", [("iso-top-right", "iso-top-left"),
                                            ("iso-bottom-right", "iso-bottom-left")])
    def test_the_right_and_left_corner_of_a_pair_differ_in_x_only(self, right, left):
        # the -right/-left half of the name is the eye's x sign; agreeing on x would make the two
        # names render the same image, the same way the z sign collapsed top onto bottom.
        rx, ry, rz = vc.view_direction(right)
        assert vc.view_direction(left) == (-rx, ry, rz)

    def test_the_four_iso_corners_are_four_distinct_directions(self):
        corners = {vc.view_direction(n) for n in
                   ("iso-top-right", "iso-top-left", "iso-bottom-right", "iso-bottom-left")}
        assert len(corners) == 4


class TestLookDirection:
    def test_unknown_view_returns_none(self):
        assert vc.look_direction("banana") is None

    def test_front_looks_along_plus_y(self):
        assert vc.look_direction("front") == (0.0, 1.0, 0.0)

    def test_known_views_return_unit_vectors(self):
        for name in _NAMED_VIEWS:
            assert _unit(vc.look_direction(name)), name


class TestSignRelationship:
    """The relationship view_screenshot and view_set both rely on: for every named view,
    view_screenshot's applied look-direction is the exact negation of view_set's applied
    view-direction. Pinned here so a future edit to either side can't silently desync the two."""

    def test_look_direction_is_negated_view_direction_for_every_named_view(self):
        for name in _NAMED_VIEWS:
            look = vc.look_direction(name)
            view = vc.view_direction(name)
            assert look == tuple(-c for c in view), name


class TestUpVector:
    def test_unknown_view_returns_none(self):
        assert vc.up_vector("banana") is None

    def test_top_and_bottom_use_y_up(self):
        assert vc.up_vector("top") == (0, 1, 0)
        assert vc.up_vector("bottom") == (0, 1, 0)

    def test_faces_and_isos_otherwise_use_z_up(self):
        for name in ("front", "back", "right", "left",
                     "iso-top-right", "iso-top-left", "iso-bottom-right", "iso-bottom-left"):
            assert vc.up_vector(name) == (0, 0, 1), name

    def test_up_vector_is_the_same_for_both_conventions(self):
        # up does not flip sign between view_direction and look_direction consumers.
        for name in _NAMED_VIEWS:
            assert vc.up_vector(name) is not None


class TestOrthoFace:
    def test_six_true_faces_are_ortho(self):
        for name in ("front", "back", "top", "bottom", "right", "left"):
            assert vc.is_ortho_face(name) is True

    def test_iso_corners_and_unknown_are_not_ortho(self):
        for name in ("iso-top-right", "iso-top-left", "iso-bottom-right", "iso-bottom-left",
                     "current", "banana"):
            assert vc.is_ortho_face(name) is False


# ── apply_named_view: the one orient-then-fit both screenshot tools share ────

def _camera():
    """The camera an orient starts from: 5 cm out on X (apply_named_view reads
    eye.distanceTo(target) for the standoff), and a projection name no orient produces, so a
    camera type an orient FORCED is told apart from the one it left alone."""
    return Camera(eye=(5, 0, 0), camera_type="initial")


def _fake_options(filename):
    """A SaveImageFileOptions stand-in at the initial values a freshly created options object
    carries - width/height 0, isBackgroundTransparent false, isAntiAliased true - measured live
    as BEHAVIOR['save_image_options_defaults'], which the tests below gate on."""
    return SimpleNamespace(filename=filename, width=0, height=0,
                           isBackgroundTransparent=False, isAntiAliased=True)


def _viewport(cam=None, save_ok=True, png=b"PNGBYTES"):
    """The shared viewport fake, its camera defaulting to the orient rig's."""
    return Viewport(camera=cam or _camera(), save_ok=save_ok, png=png)


@pytest.fixture
def options_kind(monkeypatch):
    """SaveImageFileOptions.create -> the local stand-in, so the options capture path runs on real
    attribute writes instead of a Mock that swallows every assignment."""
    import adsk.core
    monkeypatch.setattr(adsk.core, "SaveImageFileOptions",
                        SimpleNamespace(create=_fake_options), raising=False)


def _refusing_point(x, y, z):
    """A camera point whose distanceTo declines to answer - the only route to the standoff
    fallback, since a distance that READS is used whatever it is."""
    def _no_read(self, other):
        raise RuntimeError("distanceTo unavailable")

    return type("RefusingPoint", (FakePoint,), {"distanceTo": _no_read})(x, y, z)


class TestViewportHandsBackACopy:
    def test_a_read_camera_moves_nothing_until_it_is_assigned_back(self):
        # BEHAVIOR['viewport_camera_returns_copy'] (measure row camera-returns-copy): every
        # assigning site in this family writes its camera back because a mutation of the read one
        # reaches no viewport. A fake sharing one mutable camera hides a site that forgot.
        assert live_api_facts.BEHAVIOR["viewport_camera_returns_copy"] is True
        vp = _viewport()
        read = vp.camera
        read.viewExtents = 999.0
        assert vp.camera.viewExtents != 999.0
        vp.camera = read
        assert vp.camera.viewExtents == 999.0


class TestApplyNamedView:
    def test_named_view_assigns_camera_and_fits(self):
        vp = _viewport()
        before = camera_state(vp._original_camera)
        vc.apply_named_view(vp, "front")
        # a viewport read hands back a copy, so the orient is judged on the camera it PUT BACK
        # carrying the state it read, not on getting the same object returned.
        assert vp._assigned and vp._cam is vp._assigned[-1]
        assert camera_state(vp._cam) != before
        assert vp._fit_calls == 1

    def test_true_face_forces_orthographic_camera(self):
        vp = _viewport()
        vc.apply_named_view(vp, "front")
        assert vp._cam.cameraType != "initial"

    def test_the_standoff_survives_the_orient(self, monkeypatch):
        # The orient re-places the eye along the named view's look direction and must leave it the
        # SAME distance from the target it started at: apply_named_view reads that distance off the
        # pre-orient camera, then rebuilds the eye from the TARGET. The expected distance is
        # computed HERE from the coordinates, never through the same eye.distanceTo the orient
        # reads, so a distanceTo answering a constant fails this instead of agreeing with itself.
        # An iso corner is used because its table entry is un-normalized - iso-top-right is
        # (1, -1, 1), of length sqrt(3) - so the assertion covers the normalization too.
        import adsk.core
        monkeypatch.setattr(adsk.core.Point3D, "create", lambda x, y, z: FakePoint(x, y, z))
        cam = _camera()
        cam.target = FakePoint(2, -3, 4)         # a focus off the origin
        cam.eye = FakePoint(5, 1, 16)            # (3, 4, 12) from it - a standoff of exactly 13
        vp = _viewport(cam=cam)
        vc.apply_named_view(vp, "iso-top-right")
        eye, tgt = vp._cam.eye, vp._cam.target
        assert math.isclose(math.dist((eye.x, eye.y, eye.z), (tgt.x, tgt.y, tgt.z)), 13.0,
                            rel_tol=1e-9)

    def test_iso_corner_keeps_camera_type(self):
        vp = _viewport()
        vc.apply_named_view(vp, "iso-top-right")
        assert vp._cam.cameraType == "initial"

    def test_unknown_or_current_is_a_noop(self):
        for name in ("current", "banana"):
            vp = _viewport()
            vc.apply_named_view(vp, name)
            assert vp._assigned == [] and vp._fit_calls == 0

    def test_a_standoff_that_will_not_read_reports_the_fallback_it_used(self, monkeypatch):
        # The distance read RAISED, so the orient ran on a substituted standoff. Returning None
        # here would leave the caller unable to tell a 100 cm camera from a fabricated one.
        import adsk.core
        monkeypatch.setattr(adsk.core.Point3D, "create", lambda x, y, z: FakePoint(x, y, z))
        cam = _camera()
        cam.eye = _refusing_point(5, 0, 0)
        vp = _viewport(cam=cam)
        used = vc.apply_named_view(vp, "front")
        assert used == vc.STANDOFF_FALLBACK_CM
        eye, tgt = vp._cam.eye, vp._cam.target
        assert math.isclose(math.dist((eye.x, eye.y, eye.z), (tgt.x, tgt.y, tgt.z)),
                            vc.STANDOFF_FALLBACK_CM, rel_tol=1e-9)

    def test_a_standoff_that_reads_zero_falls_back_instead_of_collapsing_the_eye(self, monkeypatch):
        # 0.0 READS, but using it rebuilds the eye ON the target - a camera with no view direction
        # at all. It is unusable, so it takes the fallback and SAYS so; only a positive standoff is
        # used as read.
        import adsk.core
        monkeypatch.setattr(adsk.core.Point3D, "create", lambda x, y, z: FakePoint(x, y, z))
        cam = _camera()
        cam.target = FakePoint(2, -3, 4)
        cam.eye = FakePoint(2, -3, 4)            # exactly on the target - a standoff of 0.0
        vp = _viewport(cam=cam)
        assert vc.apply_named_view(vp, "front") == vc.STANDOFF_FALLBACK_CM
        eye, tgt = vp._cam.eye, vp._cam.target
        assert math.isclose(math.dist((eye.x, eye.y, eye.z), (tgt.x, tgt.y, tgt.z)),
                            vc.STANDOFF_FALLBACK_CM, rel_tol=1e-9)

    def test_a_positive_standoff_is_used_as_read_and_reports_no_fallback(self, monkeypatch):
        # the boundary's other side: 13 cm is usable, so it survives the orient untouched and the
        # fallback channel stays null.
        import adsk.core
        monkeypatch.setattr(adsk.core.Point3D, "create", lambda x, y, z: FakePoint(x, y, z))
        cam = _camera()
        cam.target = FakePoint(2, -3, 4)
        cam.eye = FakePoint(5, 1, 16)            # (3, 4, 12) from it - a standoff of exactly 13
        vp = _viewport(cam=cam)
        assert vc.apply_named_view(vp, "front") is None
        eye, tgt = vp._cam.eye, vp._cam.target
        assert math.isclose(math.dist((eye.x, eye.y, eye.z), (tgt.x, tgt.y, tgt.z)), 13.0,
                            rel_tol=1e-9)


class TestApplyNamedViewEyeSide:
    """WHICH SIDE of the target the eye lands on - the half of the orient the standoff and the
    camera-type assertions above cannot see. apply_named_view rebuilds the eye from the target along
    a direction, and both signs keep the same standoff and the same camera type: negating it (the
    look direction applied where the view direction belongs, or either table entry flipped) renders
    the far side of the model under the near side's name - a 'bottom' shot showing the top face.

    The expected sides are world facts written out here rather than read from the module's own
    table, so a flipped TABLE entry fails this test instead of flipping the expectation with it -
    a flip of the table AND its consumer together cancels inside apply_named_view and is caught by
    TestViewDirection and view_set's front pin instead. Fusion is Z-up:
    'front' views the model from -Y, 'top' from above, and an iso corner's name spells its three
    signs (top/bottom = eye z, right/left = eye x; every iso sits on the -Y viewer side).
    """

    _EYE_SIDE = {
        "front": (0, -1, 0),
        "back": (0, 1, 0),
        "top": (0, 0, 1),
        "bottom": (0, 0, -1),
        "right": (1, 0, 0),
        "left": (-1, 0, 0),
        "iso-top-right": (1, -1, 1),
        "iso-top-left": (-1, -1, 1),
        "iso-bottom-right": (1, -1, -1),
        "iso-bottom-left": (-1, -1, -1),
    }

    @pytest.mark.parametrize("name,side", sorted(_EYE_SIDE.items()))
    def test_the_eye_lands_on_the_named_side_of_the_target(self, name, side, monkeypatch):
        import adsk.core
        monkeypatch.setattr(adsk.core.Point3D, "create", lambda x, y, z: FakePoint(x, y, z))
        cam = _camera()
        # An OFF-ORIGIN focus, and a starting eye whose own offset (3, 4, 12) shares no sign
        # pattern with any named view - so a side that reads correct came from the orient rather
        # than from the camera it started on.
        cam.target = FakePoint(2, -3, 4)
        cam.eye = FakePoint(5, 1, 16)
        vp = _viewport(cam=cam)
        vc.apply_named_view(vp, name)
        eye, tgt = vp._cam.eye, vp._cam.target
        offset = (eye.x - tgt.x, eye.y - tgt.y, eye.z - tgt.z)
        for axis, want in enumerate(side):
            got = offset[axis]
            if want == 0:
                assert math.isclose(got, 0.0, abs_tol=1e-9), (name, axis, got)
            else:
                assert got * want > 0, (name, axis, got)

    def test_the_target_is_left_where_the_camera_had_it(self):
        # The eye moves around the focus; the focus itself is not re-aimed by an orient, or every
        # named view would also recentre the model.
        cam = _camera()
        cam.target = FakePoint(2, -3, 4)
        vp = _viewport(cam=cam)
        vc.apply_named_view(vp, "front")
        tgt = vp._cam.target
        assert (tgt.x, tgt.y, tgt.z) == (2, -3, 4)


class TestCapturePngB64:
    def test_refreshes_before_the_grab(self):
        # the refresh must precede saveAsImageFile - the capture otherwise races an un-refreshed
        # frame (a camera/visibility change that hasn't drawn yet reads as blank).
        vp = _viewport()
        b64, err = vc.capture_png_b64(vp, 100, 80)
        assert err is None
        assert vp._calls.index("refresh") < vp._calls.index("save")

    def test_returns_the_png_as_base64(self):
        import base64
        vp = _viewport(png=b"IMAGEDATA")
        b64, err = vc.capture_png_b64(vp, 100, 80)
        assert err is None and base64.b64decode(b64) == b"IMAGEDATA"

    def test_save_returning_false_is_an_error_not_a_blank_ok(self):
        vp = _viewport(save_ok=False)
        b64, err = vc.capture_png_b64(vp, 100, 80)
        assert b64 is None and "capture failed" in err.lower()
        assert "saveAsImageFile returned false" in err       # the overload that actually answered

    def test_empty_file_is_an_error_not_an_empty_base64_ok(self):
        # mkstemp already created the file, so file-exists proves nothing - a success that wrote no
        # bytes must not come back as an ok with an empty image.
        vp = _viewport(png=b"")
        b64, err = vc.capture_png_b64(vp, 100, 80)
        assert b64 is None and "0-byte" in err


class TestCaptureOptionsPath:
    """transparent_background/anti_aliased route the grab through SaveImageFileOptions +
    saveAsImageFileWithOptions; with neither given the plain overload stays untouched."""

    def test_neither_switch_uses_the_plain_overload(self, options_kind):
        vp = _viewport()
        b64, err = vc.capture_png_b64(vp, 100, 80)
        assert err is None
        assert "save" in vp._calls and "save_with_options" not in vp._calls
        assert vp._options_used is None

    def test_transparent_background_switches_to_the_options_overload(self, options_kind):
        vp = _viewport()
        b64, err = vc.capture_png_b64(vp, 100, 80, transparent_background=True)
        assert err is None
        assert "save_with_options" in vp._calls and "save" not in vp._calls
        assert vp._options_used.isBackgroundTransparent is True

    def test_anti_aliased_alone_switches_to_the_options_overload(self, options_kind):
        vp = _viewport()
        vc.capture_png_b64(vp, 100, 80, anti_aliased=False)
        assert vp._options_used.isAntiAliased is False
        # the switch NOT given is left at the options object's own measured initial value
        assert live_api_facts.BEHAVIOR["save_image_options_defaults"]
        assert vp._options_used.isBackgroundTransparent is False

    def test_false_is_a_request_not_an_absence(self, options_kind):
        # transparent_background=False must still take the options path (False != unset), otherwise
        # an explicit "opaque, anti-aliased" request silently falls back to the plain capture.
        vp = _viewport()
        vc.capture_png_b64(vp, 100, 80, transparent_background=False)
        assert "save_with_options" in vp._calls
        assert vp._options_used.isBackgroundTransparent is False

    def test_requested_size_is_assigned_onto_the_options(self, options_kind):
        # a fresh options object starts at width/height 0
        # (BEHAVIOR['save_image_options_defaults']), so the requested pixel size is only
        # honoured if the capture assigns it.
        assert live_api_facts.BEHAVIOR["save_image_options_defaults"]
        vp = _viewport()
        vc.capture_png_b64(vp, 1024, 768, anti_aliased=True)
        assert (vp._options_used.width, vp._options_used.height) == (1024, 768)

    def test_options_path_still_refreshes_first(self, options_kind):
        vp = _viewport()
        vc.capture_png_b64(vp, 100, 80, transparent_background=True)
        assert vp._calls.index("refresh") < vp._calls.index("save_with_options")

    def test_options_path_returns_the_png_as_base64(self, options_kind):
        import base64
        vp = _viewport(png=b"TRANSPARENTPNG")
        b64, err = vc.capture_png_b64(vp, 100, 80, transparent_background=True)
        assert err is None and base64.b64decode(b64) == b"TRANSPARENTPNG"

    def test_options_failure_names_the_options_overload(self, options_kind):
        vp = _viewport(save_ok=False)
        b64, err = vc.capture_png_b64(vp, 100, 80, transparent_background=True)
        assert b64 is None and "saveAsImageFileWithOptions returned false" in err


# The x-ref shape for COMPONENTS, measured on a CAM job assembled from 7 source documents: each
# document's ROOT component reads the SAME byte-identical entityToken while the documents' lineage
# ids differ. A token-only key collapses all of them onto one entry.
_ROOT_TOKEN = "/v4BAAEAAwAAAAAAAAAAAAAA"
_JOB_URNS = ("urn:adsk.wipprod:dm.lineage:K3I2nkywRlaWPHJexysOdA",
             "urn:adsk.wipprod:dm.lineage:N_QoPrrrSJmF__f9BZV86A",
             "urn:adsk.wipprod:dm.lineage:Qb7yTHkCTVSp6t9V9YQKuw")


def _root_of_document(name, urn, token=_ROOT_TOKEN):
    """One document's ROOT component: its own entityToken plus the parentDesign hop a component's
    source document is read through (parentDesign -> parentDocument -> dataFile.id)."""
    return MakeComp(name=name, entity_token=token, parent_design=make_source_document(urn))


class TestAllDisplayComponents:
    """The deduped component walk every folder-bulb toggle runs. The key is the physical-entity
    identity: allComponents holds a root proxy DISTINCT from rootComponent (so Python identity would
    toggle the root twice), while an entityToken is DOCUMENT-LOCAL and shared by every document's
    root (so a token-only key drops every root but one)."""

    def test_roots_of_several_source_documents_are_all_walked(self):
        # Keyed on the bare token these DISTINCT components collapse to one entry and the folder
        # bulbs of the others are never written at all.
        roots = [_root_of_document(f"Doc{i}", urn) for i, urn in enumerate(_JOB_URNS)]
        assert len({c.entityToken for c in roots}) == 1      # the tokens really collide
        design = SimpleNamespace(rootComponent=roots[0], allComponents=roots)
        assert vc.all_display_components(design) == roots

    def test_a_shared_token_inside_ONE_document_still_collapses(self):
        # The other direction: the document half must not split a component from its own proxy, or
        # the root's bulbs would be written twice on every design.
        root = _root_of_document("Doc0", _JOB_URNS[0])
        proxy = _root_of_document("Doc0", _JOB_URNS[0])
        design = SimpleNamespace(rootComponent=root, allComponents=[proxy])
        assert vc.all_display_components(design) == [root]

    def test_a_component_whose_document_reads_is_kept_apart_from_one_whose_does_not(self):
        # A source document that will not read answers None, which is a DIFFERENT urn half from a
        # real lineage id - so the component nothing could be read from is never folded into a
        # component that was identified.
        placed = _root_of_document("Doc0", _JOB_URNS[0])
        loose = SimpleNamespace(name="Loose", entityToken=_ROOT_TOKEN)   # no parentDesign at all
        design = SimpleNamespace(rootComponent=placed, allComponents=[loose])
        assert vc.all_display_components(design) == [placed, loose]

    def test_root_proxy_in_allcomponents_is_collapsed(self):
        from types import SimpleNamespace
        root = SimpleNamespace(name="Root", entityToken="tok-root")
        root_proxy = SimpleNamespace(name="Root", entityToken="tok-root")
        sub = SimpleNamespace(name="Sub", entityToken="tok-sub")
        design = SimpleNamespace(rootComponent=root, allComponents=[root_proxy, sub])
        comps = vc.all_display_components(design)
        assert comps == [root, sub]                       # the proxy never doubles the root

    def test_tokenless_components_fall_back_to_identity(self):
        from types import SimpleNamespace
        a = SimpleNamespace(name="A")
        b = SimpleNamespace(name="B")
        design = SimpleNamespace(rootComponent=a, allComponents=[a, b])
        comps = vc.all_display_components(design)
        assert comps == [a, b]                            # same OBJECT deduped; distinct kept

    def test_an_unreadable_allcomponents_still_yields_the_root(self):
        from types import SimpleNamespace
        root = SimpleNamespace(name="Root", entityToken="tok-root")
        design = SimpleNamespace(rootComponent=root)      # no allComponents at all
        assert vc.all_display_components(design) == [root]

    def test_display_folders_name_the_four_component_folder_bulbs(self):
        assert vc.DISPLAY_FOLDERS == {
            "sketches": "isSketchFolderLightBulbOn",
            "construction": "isConstructionFolderLightBulbOn",
            "origins": "isOriginFolderLightBulbOn",
            "joints": "isJointsFolderLightBulbOn",
        }


class TestSameBody:
    """The key an isolate keeps ONE body lit by. A component placed twice hands both proxies of its
    single native body ONE native_identity, so that half alone answers "same body" for every
    instance; the placing occurrence's path is the half that separates them."""

    def _placed(self, native, path):
        return body_proxy(native, make_occurrence(path=path, component=MakeComp(name="Wing")))

    def test_two_instances_of_one_native_body_are_not_the_same_body(self):
        native = BRepBody(name="skin", entity_token="SKIN")
        assert vc.same_body(self._placed(native, "Wing:1"),
                            self._placed(native, "Wing:2")) is False

    def test_two_fresh_reads_of_ONE_instance_are_the_same_body(self):
        # findEntityByToken mints a FRESH proxy, so the subject reached through a second walk still
        # has to match - matched by object identity it would be hidden by its own framing call.
        native = BRepBody(name="skin", entity_token="SKIN")
        a, b = self._placed(native, "Wing:1"), self._placed(native, "Wing:1")
        assert a is not b and vc.same_body(a, b) is True

    def test_two_reads_of_one_body_in_NO_context_are_the_same_body(self):
        native = BRepBody(name="skin", entity_token="SKIN")
        twin = BRepBody(name="skin", entity_token="SKIN")
        assert vc.same_body(native, twin) is True

    def test_a_native_body_and_its_OWN_proxy_are_the_same_body(self):
        # A native body reads NO placement path and its proxy reads one, but they are one body -
        # split here and an isolate hides a wrapper of its own subject, whose bulb the placement
        # shares, and the shot is refused on a design with a single placement.
        native = BRepBody(name="skin", entity_token="SKIN")
        assert vc.same_body(native, self._placed(native, "Wing:1")) is True
        assert vc.same_body(self._placed(native, "Wing:1"), native) is True

    def test_a_path_that_will_not_read_leaves_the_identity_match_standing(self):
        # Hiding the subject is the worse failure, so a path that declines never splits an identity
        # match into two bodies.
        native = BRepBody(name="skin", entity_token="SKIN")
        blind = make_occurrence(path="Wing:1", component=MakeComp(name="Wing"),
                                raises_on={"fullPathName": "3 : InternalValidationError : path"})
        assert vc.same_body(body_proxy(native, blind), self._placed(native, "Wing:2")) is True

    def test_two_different_bodies_are_never_the_same_body(self):
        assert vc.same_body(BRepBody(name="skin", entity_token="SKIN"),
                            BRepBody(name="rib", entity_token="RIB")) is False


class TestIsolateForFit:
    """The frame-on-one-occurrence walk view_screenshot's fit_to and view_set's focus= both fit
    through. Its errors are worded by the CALLER's own input kind, so neither tool reports a
    refusal naming the other one's parameter."""

    def test_no_active_design_names_the_callers_own_input(self, monkeypatch):
        monkeypatch.setattr(vc._common, "design", lambda: None)
        ref = SimpleNamespace(name="focus")
        restore, target, err = vc.isolate_for_fit("Part:1", ref)
        assert restore is None and target is None
        assert err.startswith("focus:") and "Part:1" in err

    def test_a_display_folder_that_will_not_relight_is_named(self, monkeypatch):
        class Comp:
            """A component whose sketch folder accepts the hide and refuses to come back on."""

            def __init__(self):
                self.name = "Blocky"
                self.entityToken = "tok"
                self.isSketchFolderLightBulbOn = True
                self.isConstructionFolderLightBulbOn = False
                self.isOriginFolderLightBulbOn = False
                self.isJointsFolderLightBulbOn = False

            def __setattr__(self, key, value):
                if key == "isSketchFolderLightBulbOn" and value is True                         and getattr(self, "_darkened", False):
                    return
                if key == "isSketchFolderLightBulbOn" and value is False:
                    object.__setattr__(self, "_darkened", True)
                object.__setattr__(self, key, value)

        comp = Comp()
        occ = SimpleNamespace(fullPathName="Part:1", name="Part:1", isLightBulbOn=True)
        design = SimpleNamespace(rootComponent=comp, allComponents=[comp])
        monkeypatch.setattr(vc._common, "design", lambda: design)
        monkeypatch.setattr(vc._common, "all_occurrences", lambda d: [occ])
        ref = SimpleNamespace(name="fit_to", resolve=lambda raw: ((occ, "occurrence"), None))
        restore, target, err = vc.isolate_for_fit("Part:1", ref)
        assert err is None and target is occ
        assert comp.isSketchFolderLightBulbOn is False    # the fit really did clear the clutter
        # the folder stays dark, and the caller is told WHICH one - not left with a silent change
        assert restore() == ["Blocky:isSketchFolderLightBulbOn"]


class TestRestoreMessage:
    def test_a_clean_restore_says_nothing(self):
        assert vc.restore_message(lambda: [], "fit_to", "for this shot") is None

    def test_no_restore_at_all_says_nothing(self):
        assert vc.restore_message(None, "fit_to", "for this shot") is None

    def test_the_message_carries_the_callers_label_and_purpose(self):
        msg = vc.restore_message(lambda: ["A:1", "B:1"], "'focus'", "to frame the view")
        assert msg.startswith("'focus' hid the other occurrences to frame the view")
        assert "2 of them" in msg and "A:1" in msg and "B:1" in msg

    def test_a_raising_restore_is_reported_not_swallowed(self):
        def boom():
            raise RuntimeError("bulb bus offline")

        msg = vc.restore_message(boom, "fit_to", "for this shot")
        assert "bulb bus offline" in msg

    def test_only_the_first_five_stuck_names_are_listed(self):
        msg = vc.restore_message(lambda: [f"O{i}:1" for i in range(9)], "fit_to", "for this shot")
        assert "9 of them" in msg                       # the COUNT is complete...
        assert "O4:1" in msg and "O5:1" not in msg      # ...while the listing stays bounded

