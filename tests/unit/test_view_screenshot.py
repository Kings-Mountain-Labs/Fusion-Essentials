"""Unit tests for ``view_screenshot.py`` _isolate_for_fit - the fit_to visibility helper.

The image capture itself needs a live viewport (integration-tested), but the fit_to helper is pure
visibility bookkeeping: find the named occurrence, hide the others, return a restore() that turns
them back on. That's exactly the bug-prone part (matching + restore), so it gets unit coverage.
"""

import base64
import os
from types import SimpleNamespace

import pytest

from conftest import (BRepBody, FakeApplication, FakeOccurrence, MakeComp, MakeDesign, Viewport,
                      _NamedCollection, body_proxy, install, load_tool, make_design,
                      make_occurrence)

gs = load_tool("view_screenshot")


def FakeOcc(path, on=True):
    """One occurrence in the isolation walk: its own visibility bulb, and the shared Component fake
    it places. A real Occurrence always answers `component`; one whose read RAISES is an unresolved
    external reference, which the shared census keeps out of this walk."""
    return make_occurrence(path=path, light_bulb_on=on,
                           component=MakeComp(name=path.split("+")[-1].split(":")[0]))


def _install(occs):
    design = MakeDesign(comp=MakeComp(name="Root", all_occurrences=occs))
    app = FakeApplication(active_product=design)
    gs.app = app
    # The occurrence resolver runs through the shared OccurrenceRef kind, which reads _common.design()
    # -> _common.app. Point that at the same fake app so resolution and the isolate logic agree.
    gs._inputs._common.app = app
    import adsk.fusion
    adsk.fusion.Design.cast = lambda x: x if isinstance(x, MakeDesign) else None
    return design


class TestIsolateForFit:
    def test_hides_others_and_restores(self):
        a, b, c = FakeOcc("A:1"), FakeOcc("B:1"), FakeOcc("C:1")
        _install([a, b, c])
        restore, target, err = gs._isolate_for_fit("B:1")
        assert restore is not None and err is None
        assert target is b                     # the resolved occurrence rides along
        # only B stays on
        assert b.isLightBulbOn is True
        assert a.isLightBulbOn is False and c.isLightBulbOn is False
        restore()
        assert a.isLightBulbOn is True and c.isLightBulbOn is True

    def test_display_folders_hidden_for_the_shot_and_restored(self):
        # vp.fit() frames every VISIBLE entity, so a big construction plane in the fitted
        # component blows the frame to the whole scene (measured) - the shot switches the
        # per-component display folders off and the restore puts back exactly what it moved.
        a = FakeOcc("A:1")
        design = _install([a])
        r = design.rootComponent
        r.entityToken = "root-tok"
        r.isSketchFolderLightBulbOn = True
        r.isConstructionFolderLightBulbOn = True
        r.isOriginFolderLightBulbOn = False              # already off - never touched
        r.isJointsFolderLightBulbOn = True
        restore, _target, err = gs._isolate_for_fit("A:1")
        assert err is None
        assert r.isSketchFolderLightBulbOn is False
        assert r.isConstructionFolderLightBulbOn is False
        assert r.isJointsFolderLightBulbOn is False
        assert r.isOriginFolderLightBulbOn is False      # was off, stays off
        assert restore() == []
        assert r.isSketchFolderLightBulbOn is True
        assert r.isConstructionFolderLightBulbOn is True
        assert r.isJointsFolderLightBulbOn is True
        assert r.isOriginFolderLightBulbOn is False      # not moved, not force-lit

    def test_substring_match(self):
        a = FakeOcc("Bracket:1")
        _install([a, FakeOcc("Other:1")])
        restore, _target, err = gs._isolate_for_fit("bracket")
        assert restore is not None and err is None and a.isLightBulbOn is True

    def test_no_match_returns_none(self):
        _install([FakeOcc("A:1")])
        restore, _target, err = gs._isolate_for_fit("Ghost")
        assert restore is None and err is not None

    def test_ambiguous_name_refused_not_first_match(self):
        # two instances share local name "Bolt:1" under different sub-assemblies - a bare "Bolt"
        # substring must ERROR (naming both fullPathNames), NOT silently isolate the first.
        a, b = FakeOcc("Sub-A:1+Bolt:1"), FakeOcc("Sub-B:1+Bolt:1")
        _install([a, b])
        restore, _target, err = gs._isolate_for_fit("Bolt")
        assert restore is None
        assert "ambiguous" in err.lower()
        assert "Sub-A:1+Bolt:1" in err and "Sub-B:1+Bolt:1" in err

    def test_already_hidden_others_not_restored_on(self):
        # an occurrence that was already OFF should stay off after restore (we only flip ones we hid)
        a, b = FakeOcc("A:1", on=True), FakeOcc("B:1", on=False)
        _install([a, b])
        restore, _target, err = gs._isolate_for_fit("A:1")
        restore()
        assert b.isLightBulbOn is False      # we never turned it on

    def test_a_clean_restore_reports_nothing_stuck(self):
        a, b = FakeOcc("A:1"), FakeOcc("B:1")
        _install([a, b])
        restore, _target, _err = gs._isolate_for_fit("A:1")
        assert restore() == []

    def test_a_bulb_that_will_not_come_back_on_is_named_by_the_restore(self):
        # This tool MUTATES visibility to take its picture. A restore that silently failed leaves
        # a read tool having changed the document, so the failure has to be reportable.
        class OneWay(FakeOccurrence):
            """A bulb that switches OFF and then refuses to come back ON - so the hide takes and
            the restore silently does not, which is the only shape that leaves a read tool
            having changed the document."""

            def __init__(self, path):
                super().__init__(path=path, light_bulb_on=True,
                                 component=MakeComp(name=path.split("+")[-1].split(":")[0]))
                object.__setattr__(self, "_armed", True)

            def __setattr__(self, key, value):
                if key == "isLightBulbOn" and value is True and getattr(self, "_armed", False):
                    return
                object.__setattr__(self, key, value)

        stuck = OneWay("Sub:1+B:1")
        _install([FakeOcc("A:1"), stuck])
        restore, _target, _err = gs._isolate_for_fit("A:1")
        assert stuck.isLightBulbOn is False           # the hide DID take
        assert restore() == ["Sub:1+B:1"]


class TestFitToOnABody:
    """A single-body ROOT design places no occurrence at all, so an occurrence-only fit_to has
    nothing to frame there - the subject is the body."""

    def test_a_bulb_SHARED_across_placements_is_a_refusal_not_a_dark_picture(self):
        # MEASURED: a body's bulb is SHARED across its placements - clearing instance 2's turned
        # instance 1's and the native's off too - which body_proxy models by routing every read to
        # the one native. Capturing then returns a picture of nothing as a success (the cardinal
        # sin), so the isolate reads the subject back and refuses.
        native = BRepBody(name="skin", entity_token="SKIN")
        wing1 = make_occurrence(path="Wing:1", component=MakeComp(name="Wing"))
        wing2 = make_occurrence(path="Wing:2", component=MakeComp(name="Wing"))
        one, two = body_proxy(native, wing1), body_proxy(native, wing2)
        wing1.bRepBodies = _NamedCollection([one])
        wing2.bRepBodies = _NamedCollection([two])
        comp = MakeComp(name="Root", all_occurrences=[wing1, wing2])
        comp.bRepBodies = _NamedCollection([])
        install(gs, make_design(comp=comp, tokens={"HANDLE": one}))
        import adsk.fusion
        adsk.fusion.BRepBody = BRepBody
        restore, _target, err = gs._isolate_for_fit("HANDLE")
        assert restore is None and err is not None
        # the cause is READ off prev, not assumed: another hidden body is that same native body
        assert "another placement of the same body was hidden" in err
        assert "fullPathName" in err                  # the route that CAN frame one instance
        assert native.isLightBulbOn is True           # the bulbs went back before returning
        assert "did NOT come back on" not in err      # ...so nothing is reported stuck

    def test_the_refusal_echoes_a_long_handle_as_a_head_not_in_full(self):
        # find_geometry mints a handle of an entityToken plus a locator, and this refusal quotes the
        # value it was given - echoed whole it buries the sentence naming the route that works.
        handle = "TOKEN" + "z" * 205 + "|@face:1.5,2.5,3.5"
        native = BRepBody(name="skin", entity_token="SKIN")
        wing1 = make_occurrence(path="Wing:1", component=MakeComp(name="Wing"))
        wing2 = make_occurrence(path="Wing:2", component=MakeComp(name="Wing"))
        one, two = body_proxy(native, wing1), body_proxy(native, wing2)
        wing1.bRepBodies = _NamedCollection([one])
        wing2.bRepBodies = _NamedCollection([two])
        comp = MakeComp(name="Root", all_occurrences=[wing1, wing2])
        comp.bRepBodies = _NamedCollection([])
        install(gs, make_design(comp=comp, tokens={handle.split("|@")[0]: one}))
        import adsk.fusion
        adsk.fusion.BRepBody = BRepBody
        # the shared-bulb refusal, reached by registering the composite handle's token half
        _restore, _target, shared_err = gs._isolate_for_fit(handle)
        assert "another placement of the same body was hidden" in shared_err
        assert handle not in shared_err                # the whole value never reaches the wire
        assert handle[:20] in shared_err               # ...its head still says which one it was
        assert f"({len(handle)} chars)" in shared_err   # and the cut is disclosed, not silent
        assert "fullPathName" in shared_err            # the route that CAN frame one instance
        # ...and the resolve MISS, the commonest way a stale handle comes back
        stale = "STALE" + "q" * 205 + "|@face:0,0,0"
        _r, _t, miss_err = gs._isolate_for_fit(stale)
        assert stale not in miss_err and f"({len(stale)} chars)" in miss_err

    def test_a_SINGLY_placed_body_reached_natively_is_framed_not_refused(self):
        # The ordinary single-placement shot: the walk reaches the body through its one occurrence
        # while the target resolved NATIVELY. Treating those two wrappers as different bodies hides
        # the subject's own proxy, the shared bulb goes dark, and the guard refuses a working shot.
        native = BRepBody(name="skin", entity_token="SKIN")
        wing = make_occurrence(path="Wing:1", component=MakeComp(name="Wing"))
        wing.bRepBodies = _NamedCollection([body_proxy(native, wing)])
        comp = MakeComp(name="Root", all_occurrences=[wing])
        comp.bRepBodies = _NamedCollection([])
        install(gs, make_design(comp=comp, tokens={"SKIN": native}))
        import adsk.fusion
        adsk.fusion.BRepBody = BRepBody
        restore, target, err = gs._isolate_for_fit("SKIN")
        assert err is None and target is native
        assert native.isLightBulbOn is True          # the subject stayed lit
        assert restore() == []

    def test_a_bulb_that_will_not_relight_on_that_refusal_is_NAMED(self):
        # The refusal puts the bulbs back with a bare assignment, and that assignment can fail -
        # the same failure restore() collects a stuck list for. Claiming the restore without
        # reading it back is how a READ tool leaves the document changed and says it did not.
        class OneWayBody(BRepBody):
            """A body bulb that switches OFF and then refuses to come back ON."""
            def __setattr__(self, key, value):
                if key == "isLightBulbOn" and value is True and getattr(self, "_armed", False):
                    return
                object.__setattr__(self, key, value)

        native = OneWayBody(name="skin", entity_token="SKIN")
        wing1 = make_occurrence(path="Wing:1", component=MakeComp(name="Wing"))
        wing2 = make_occurrence(path="Wing:2", component=MakeComp(name="Wing"))
        one, two = body_proxy(native, wing1), body_proxy(native, wing2)
        wing1.bRepBodies = _NamedCollection([one])
        wing2.bRepBodies = _NamedCollection([two])
        comp = MakeComp(name="Root", all_occurrences=[wing1, wing2])
        comp.bRepBodies = _NamedCollection([])
        install(gs, make_design(comp=comp, tokens={"HANDLE": one}))
        import adsk.fusion
        adsk.fusion.BRepBody = BRepBody
        object.__setattr__(native, "_armed", True)     # armed AFTER the hide is allowed to land
        restore, _target, err = gs._isolate_for_fit("HANDLE")
        assert restore is None and native.isLightBulbOn is False   # the relight really did fail
        assert "1 bulb(s) did NOT come back on: skin" in err
        assert "view_set(action='show'" in err

    def _install_bodies(self, bodies):
        comp = MakeComp(name="Root", all_occurrences=[])
        comp.bRepBodies = _NamedCollection(bodies)
        install(gs, make_design(comp=comp, tokens={b.entityToken: b for b in bodies}))
        import adsk.fusion
        adsk.fusion.BRepBody = BRepBody

    def test_a_body_handle_frames_a_design_with_no_occurrences(self):
        skin = BRepBody(name="skin", entity_token="SKIN")
        self._install_bodies([skin])
        restore, target, err = gs._isolate_for_fit("SKIN")
        assert err is None and target is skin
        assert skin.isLightBulbOn is True
        assert restore() == []

    def test_the_other_bodies_are_hidden_for_the_shot_and_restored(self):
        skin, rib = BRepBody(name="skin", entity_token="SKIN"), BRepBody(name="rib",
                                                                        entity_token="RIB")
        self._install_bodies([skin, rib])
        restore, _target, err = gs._isolate_for_fit("SKIN")
        assert err is None
        assert skin.isLightBulbOn is True and rib.isLightBulbOn is False
        restore()
        assert rib.isLightBulbOn is True

    def test_a_name_matching_nothing_points_at_both_lookups(self):
        self._install_bodies([BRepBody(name="skin", entity_token="SKIN")])
        restore, _target, err = gs._isolate_for_fit("Ghost")
        assert restore is None and "Ghost" in err

    def test_a_resolved_proxy_matches_the_walked_body_by_native_identity(self):
        # live findEntityByToken mints a FRESH proxy, so `is` is never true across the resolve and
        # the visibility walk - a subject matched by object identity alone would be hidden by its
        # own framing call, and the shot would frame everything except the subject.
        walked = BRepBody(name="skin", entity_token="SKIN")
        rib = BRepBody(name="rib", entity_token="RIB")
        proxy = BRepBody(name="skin", entity_token="SKIN")     # what the token map hands back
        comp = MakeComp(name="Root", all_occurrences=[])
        comp.bRepBodies = _NamedCollection([walked, rib])
        install(gs, make_design(comp=comp, tokens={"SKIN": proxy}))
        import adsk.fusion
        adsk.fusion.BRepBody = BRepBody
        restore, target, err = gs._isolate_for_fit("SKIN")
        assert err is None and target is proxy and proxy is not walked
        assert walked.isLightBulbOn is True         # the subject stayed lit
        assert rib.isLightBulbOn is False
        restore()
        assert rib.isLightBulbOn is True

    def test_the_restore_message_names_bodies_not_occurrences(self):
        class OneWayBody(BRepBody):
            """A body bulb that switches OFF and then refuses to come back ON."""
            def __setattr__(self, key, value):
                if key == "isLightBulbOn" and value is True and getattr(self, "_armed", False):
                    return
                object.__setattr__(self, key, value)

        stuck = OneWayBody(name="rib", entity_token="RIB")
        object.__setattr__(stuck, "_armed", True)
        self._install_bodies([BRepBody(name="skin", entity_token="SKIN"), stuck])
        restore, _target, err = gs._isolate_for_fit("SKIN")
        assert err is None and stuck.isLightBulbOn is False
        msg = gs._restore_message(restore)
        assert "hid the other bodies" in msg and "rib" in msg


# ── active-component note: a non-root activation dims everything else to ghosts ──
# When a sub-component is activated, the screenshot looks washed-out/translucent. The note names the
# active component so the agent reads that as activation scope, not a lighting/appearance bug.

class TestActiveComponentNote:
    def _design(self, active_is_root=True, active_name="Gimbal:1"):
        # activeOccurrence is None exactly when the root is active (the API contract the note keys
        # on); a non-root activation exposes the activated OCCURRENCE. An identity test of
        # activeComponent against rootComponent can never be true live - each property access mints
        # a new proxy - so the note must never be derived from a component comparison.
        from types import SimpleNamespace
        occ = None if active_is_root else SimpleNamespace(name=active_name)
        return SimpleNamespace(activeOccurrence=occ)

    def test_root_active_no_note(self):
        assert gs._active_component_note(self._design(active_is_root=True)) is None

    def test_sub_component_active_warns_and_names_it(self):
        note = gs._active_component_note(self._design(active_is_root=False, active_name="Rotor:1"))
        assert note is not None
        assert "Rotor:1" in note
        assert "dimmed" in note or "translucent" in note
        assert "lighting" in note            # explicitly rules out the misdiagnosis

    def test_none_design_is_safe(self):
        assert gs._active_component_note(None) is None


# The exact-world-axis camera math the handler orients with is _view_common.apply_named_view,
# pinned in test__view_common.py.

# ── _keep_visible: fit_to isolate keeps the target + its ancestors + descendants visible, matched ──
# ── by fullPathName (NOT Python `is`, which never matches across fresh occurrence proxies and would ──
# ── hide the target itself -> a BLANK image). Nesting boundary is '+' per level. ──────────────────

class TestCaptureSwitchPassThrough:
    """transparent_background/anti_aliased reach the ONE shared capture seam unchanged, and stay
    absent (None) when the caller omits them - the plain-overload default the seam keys on."""

    @pytest.fixture
    def rig(self, monkeypatch):
        monkeypatch.setattr(gs, "app", SimpleNamespace(activeViewport=SimpleNamespace()))
        monkeypatch.setattr(gs._common, "design", lambda: None)
        calls = []

        def fake_capture(viewport, width, height, prefix="fe_mcp_shot",
                         transparent_background=None, anti_aliased=None):
            calls.append({"width": width, "height": height,
                          "transparent_background": transparent_background,
                          "anti_aliased": anti_aliased})
            return "B64DATA", None

        monkeypatch.setattr(gs._view_common, "capture_png_b64", fake_capture)
        return calls

    def test_omitting_both_leaves_the_capture_on_the_plain_path(self, rig):
        result = gs.handler()
        assert result["isError"] is False
        assert rig == [{"width": 800, "height": 600,
                        "transparent_background": None, "anti_aliased": None}]

    def test_transparent_background_reaches_the_capture(self, rig):
        gs.handler(transparent_background=True)
        assert rig[0]["transparent_background"] is True
        assert rig[0]["anti_aliased"] is None       # the switch not asked for stays absent

    def test_anti_aliased_false_is_forwarded_not_dropped(self, rig):
        # False must survive as False, not collapse to "unset" - otherwise an explicit
        # anti_aliased=False silently renders anti-aliased.
        gs.handler(anti_aliased=False)
        assert rig[0]["anti_aliased"] is False
        assert rig[0]["transparent_background"] is None

    def test_both_switches_forwarded_together(self, rig):
        gs.handler(transparent_background=False, anti_aliased=True)
        assert rig[0]["transparent_background"] is False and rig[0]["anti_aliased"] is True

    def test_image_content_block_still_returned(self, rig):
        result = gs.handler(transparent_background=True)
        assert [c["type"] for c in result["content"]] == ["image"]
        assert result["content"][0]["data"] == "B64DATA"

    def test_capture_failure_is_an_error(self, rig, monkeypatch):
        monkeypatch.setattr(gs._view_common, "capture_png_b64",
                            lambda *a, **k: (None, "Viewport capture failed."))
        result = gs.handler(transparent_background=True)
        assert result["isError"] is True and "capture failed" in result["message"]


class TestFitToRestoreDisclosure:
    """fit_to hides the other occurrences to frame one - a mutation a READ tool must undo. A
    restore that did not take is surfaced on the result, never swallowed in a finally."""

    @pytest.fixture
    def rig(self, monkeypatch):
        vp = SimpleNamespace(camera=SimpleNamespace(viewExtents=1.0), fit=lambda: None)
        monkeypatch.setattr(gs, "app", SimpleNamespace(activeViewport=vp))
        monkeypatch.setattr(gs._common, "design", lambda: None)
        monkeypatch.setattr(gs._view_common, "capture_png_b64",
                            lambda *a, **k: ("B64DATA", None))
        monkeypatch.setattr(gs._view_common, "apply_named_view", lambda v, name: None)
        return monkeypatch

    def _stub_isolate(self, monkeypatch, stuck):
        monkeypatch.setattr(gs, "_isolate_for_fit",
                            lambda name: (lambda: list(stuck), object(), None))

    def test_an_unresolved_fit_to_is_an_error_before_anything_is_hidden(self, rig):
        # the shared isolate hands back its own refusal (a miss, or an ambiguous name naming the
        # candidates); the shot must not proceed on a frame nobody chose
        rig.setattr(gs, "_isolate_for_fit",
                    lambda name: (None, None, "fit_to: 'Ghost' matched no occurrence."))
        result = gs.handler(fit_to="Ghost")
        assert result["isError"] is True and "Ghost" in result["message"]

    def test_a_clean_restore_leaves_the_image_alone(self, rig):
        self._stub_isolate(rig, [])
        result = gs.handler(fit_to="Bracket:1")
        assert result["isError"] is False
        assert [c["type"] for c in result["content"]] == ["image"]

    def test_a_failed_restore_rides_on_the_successful_shot(self, rig):
        self._stub_isolate(rig, ["Sub:1+Gear:1"])
        result = gs.handler(fit_to="Bracket:1")
        assert result["isError"] is False              # the picture WAS taken
        text = result["content"][0]["text"]
        assert "Sub:1+Gear:1" in text and "view_set" in text
        assert [c["type"] for c in result["content"]] == ["text", "image"]

    def test_a_restore_that_raises_is_reported_not_swallowed(self, rig):
        def boom():
            raise RuntimeError("occurrence went invalid")
        rig.setattr(gs, "_isolate_for_fit", lambda name: (boom, object(), None))
        result = gs.handler(fit_to="Bracket:1")
        assert "occurrence went invalid" in result["content"][0]["text"]

    def test_a_failed_orient_still_names_the_bulb_it_could_not_restore(self, rig):
        # The orient blows up AFTER fit_to hid the others. This exit returns before the capture
        # block, so without its own restore disclosure a stuck bulb is named nowhere at all while
        # the document is left with occurrences hidden.
        self._stub_isolate(rig, ["Sub:1+Gear:1"])
        rig.setattr(gs._view_common, "apply_named_view",
                    lambda v, name: (_ for _ in ()).throw(RuntimeError("camera is busy")))
        result = gs.handler(view="top", fit_to="Bracket:1")
        assert result["isError"] is True
        assert "Failed to set view 'top'" in result["message"]
        assert "Sub:1+Gear:1" in result["message"] and "view_set" in result["message"]

    def test_a_failed_orient_with_a_clean_restore_says_nothing_extra(self, rig):
        self._stub_isolate(rig, [])
        rig.setattr(gs._view_common, "apply_named_view",
                    lambda v, name: (_ for _ in ()).throw(RuntimeError("camera is busy")))
        result = gs.handler(view="top", fit_to="Bracket:1")
        assert result["isError"] is True and "could NOT turn" not in result["message"]

    def test_a_failed_restore_is_appended_to_a_capture_error_too(self, rig):
        self._stub_isolate(rig, ["Sub:1+Gear:1"])
        rig.setattr(gs._view_common, "capture_png_b64",
                    lambda *a, **k: (None, "Viewport capture failed."))
        result = gs.handler(fit_to="Bracket:1")
        assert result["isError"] is True
        assert "capture failed" in result["message"] and "Sub:1+Gear:1" in result["message"]

    def test_the_description_discloses_the_hide_and_restore(self):
        # disclosed on 'fit_to' itself - the input whose value triggers the visibility change
        desc = gs.tool.to_dict()["inputSchema"]["properties"]["fit_to"]["description"]
        assert "Hides the rest" in desc
        assert "restores them" in desc


class TestCameraRestore:
    """The camera this tool moves to take its picture is the USER's. Every exit puts it back, and a
    restore (or a zoom) the viewport refuses is NAMED, never swallowed."""

    class _Viewport(Viewport):
        """A viewport whose camera READ or camera assignment can be made to raise, modelling a
        platform that refuses the snapshot, or refuses the restore after accepting the move."""

        class _StuckExtents:
            """A camera whose viewExtents will not be written - the zoom the platform drops."""
            @property
            def viewExtents(self):
                return 1.0

            @viewExtents.setter
            def viewExtents(self, value):
                raise RuntimeError("extents locked")

        def __init__(self, refuse_assign=False, zoom_raises=False, camera_unreadable=False):
            super().__init__(camera=(self._StuckExtents() if zoom_raises
                                     else SimpleNamespace(viewExtents=1.0)))
            self._refuse = refuse_assign
            self._unreadable = camera_unreadable

        @property
        def camera(self):
            if self._unreadable:
                raise RuntimeError("camera unavailable")
            return self._cam

        @camera.setter
        def camera(self, value):
            if self._refuse:
                raise RuntimeError("viewport busy")
            self._assigned.append(value)
            self._cam = value

    @pytest.fixture
    def rig(self, monkeypatch):
        monkeypatch.setattr(gs._common, "design", lambda: None)
        monkeypatch.setattr(gs._view_common, "capture_png_b64",
                            lambda *a, **k: ("B64DATA", None))
        return monkeypatch

    def test_a_failed_orient_puts_the_camera_back(self, rig):
        # apply_named_view moves the camera and THEN fails; returning without restoring leaves the
        # user's viewport somewhere they never asked for, with no way back.
        vp = self._Viewport()
        original = vp.camera
        rig.setattr(gs, "app", SimpleNamespace(activeViewport=vp))

        def move_then_fail(viewport, name):
            viewport.camera = SimpleNamespace(viewExtents=99.0)   # the camera HAS moved
            raise RuntimeError("camera is busy")
        rig.setattr(gs._view_common, "apply_named_view", move_then_fail)
        result = gs.handler(view="top")
        assert result["isError"] is True and "Failed to set view 'top'" in result["message"]
        assert vp.camera is original                              # put back on the failure exit

    def test_a_camera_restore_the_viewport_refuses_is_named_on_the_shot(self, rig):
        vp = self._Viewport(refuse_assign=True)
        rig.setattr(gs, "app", SimpleNamespace(activeViewport=vp))
        rig.setattr(gs._view_common, "apply_named_view", lambda v, name: None)
        result = gs.handler(view="top")
        assert result["isError"] is False                         # the picture WAS taken
        text = result["content"][0]["text"]
        assert "could NOT be put back" in text and "viewport busy" in text
        assert "view_set(orient)" in text

    def test_a_clean_restore_says_nothing_extra(self, rig):
        vp = self._Viewport()
        rig.setattr(gs, "app", SimpleNamespace(activeViewport=vp))
        rig.setattr(gs._view_common, "apply_named_view", lambda v, name: None)
        result = gs.handler(view="top")
        assert [c["type"] for c in result["content"]] == ["image"]

    def test_a_camera_that_will_not_snapshot_says_the_view_is_LEFT_where_the_shot_put_it(self, rig):
        # The snapshot is the only thing that makes the move undoable. When the camera cannot be
        # READ the orient still happens, so the payload owes the caller the fact that the viewport
        # is parked at the capture view - silence reads as "your view came back".
        vp = self._Viewport(camera_unreadable=True)
        rig.setattr(gs, "app", SimpleNamespace(activeViewport=vp))
        oriented = []
        rig.setattr(gs._view_common, "apply_named_view", lambda v, name: oriented.append(name))
        result = gs.handler(view="front")
        assert result["isError"] is False and oriented == ["front"]   # the camera DID move
        assert [c["type"] for c in result["content"]] == ["text", "image"]
        text = result["content"][0]["text"]
        assert "LEFT at the capture view" in text
        assert "view_set(orient)" in text

    def test_a_zoom_the_camera_refuses_is_disclosed_not_silently_dropped(self, rig):
        # The caller asked for zoom=0.5; a swallowed failure returns the FITTED frame as if the
        # zoom had applied, and the image is read as the requested framing.
        vp = self._Viewport(zoom_raises=True)
        rig.setattr(gs, "app", SimpleNamespace(activeViewport=vp))
        rig.setattr(gs._view_common, "apply_named_view", lambda v, name: None)
        result = gs.handler(view="top", zoom=0.5)
        assert result["isError"] is False
        text = result["content"][0]["text"]
        assert "zoom=0.5 could NOT be applied" in text and "extents locked" in text


class TestStandoffFallbackDisclosure:
    """apply_named_view answers the standoff it fell back to when the camera's eye-target distance
    did not read as a positive number: the eye is placed at that standoff before the fit. The shot
    says so, since silence means the camera's own distance was the one used."""

    @pytest.fixture
    def rig(self, monkeypatch):
        vp = SimpleNamespace(camera=SimpleNamespace(viewExtents=1.0), fit=lambda: None)
        monkeypatch.setattr(gs, "app", SimpleNamespace(activeViewport=vp))
        monkeypatch.setattr(gs._common, "design", lambda: None)
        monkeypatch.setattr(gs._view_common, "capture_png_b64", lambda *a, **k: ("B64DATA", None))
        return monkeypatch

    def test_a_used_fallback_rides_on_the_shot(self, rig):
        rig.setattr(gs._view_common, "apply_named_view", lambda v, name: 100.0)
        result = gs.handler(view="top")
        assert result["isError"] is False
        text = result["content"][0]["text"]
        # ONE rendering of the number, in both places it is spelled
        assert "standoff_fallback_cm=100:" in text and "100 cm from the target" in text
        assert "100.0" not in text
        assert [c["type"] for c in result["content"]] == ["text", "image"]

    def test_a_capture_that_failed_claims_no_eye_placement(self, rig):
        # the orient placed an eye, but there is no picture the placement produced - saying so on
        # the failure describes a shot that does not exist
        rig.setattr(gs._view_common, "apply_named_view", lambda v, name: 100.0)
        rig.setattr(gs._view_common, "capture_png_b64",
                    lambda *a, **k: (None, "Viewport capture failed."))
        result = gs.handler(view="top")
        assert result["isError"] is True
        assert "standoff_fallback_cm" not in result["message"]

    def test_a_camera_that_framed_the_shot_itself_publishes_nothing(self, rig):
        rig.setattr(gs._view_common, "apply_named_view", lambda v, name: None)
        result = gs.handler(view="top")
        assert [c["type"] for c in result["content"]] == ["image"]

    def test_the_current_view_never_claims_a_fallback(self, rig):
        # view='current' does not orient at all, so no eye was placed at a fallback standoff
        rig.setattr(gs._view_common, "apply_named_view", lambda v, name: 100.0)
        result = gs.handler(view="current")
        assert all("standoff_fallback_cm" not in c.get("text", "") for c in result["content"])


class TestFilePathWrite:
    """'file_path' writes the captured PNG to local disk - the raster file drawing_insert_image
    needs. The inline image is returned either way; a file that does not land is a failure, since
    the caller asked for a path to hand on."""

    PNG = b"\x89PNG\r\n\x1a\nSTUB-BYTES"

    @pytest.fixture
    def rig(self, monkeypatch):
        vp = SimpleNamespace(camera=SimpleNamespace(viewExtents=1.0), fit=lambda: None)
        monkeypatch.setattr(gs, "app", SimpleNamespace(activeViewport=vp))
        monkeypatch.setattr(gs._common, "design", lambda: None)
        oriented = []
        monkeypatch.setattr(gs._view_common, "apply_named_view",
                            lambda v, name: oriented.append(name))
        monkeypatch.setattr(gs._view_common, "capture_png_b64",
                            lambda *a, **k: (base64.b64encode(self.PNG).decode("ascii"), None))
        return SimpleNamespace(monkeypatch=monkeypatch, oriented=oriented)

    def _text(self, result):
        return " ".join(c["text"] for c in result["content"] if c["type"] == "text")

    def test_the_captured_png_bytes_land_on_disk(self, rig, tmp_path):
        out = str(tmp_path / "shot.png")
        result = gs.handler(file_path=out)
        assert result["isError"] is False
        # the file carries the CAPTURED bytes, not the base64 text of them
        assert open(out, "rb").read() == self.PNG

    def test_the_path_and_size_are_published_beside_the_image(self, rig, tmp_path):
        out = str(tmp_path / "shot.png")
        result = gs.handler(file_path=out)
        text = self._text(result)
        assert f"file_path={out}" in text
        assert f"size_bytes={len(self.PNG)}" in text
        assert [c["type"] for c in result["content"]] == ["text", "image"]

    def test_omitting_file_path_writes_nothing_and_returns_the_image_alone(self, rig, tmp_path):
        result = gs.handler()
        assert [c["type"] for c in result["content"]] == ["image"]
        assert list(tmp_path.iterdir()) == []

    def test_a_missing_png_extension_is_appended(self, rig, tmp_path):
        # the capture IS a PNG whatever the path says - the fleet's export idiom appends rather
        # than leaving PNG bytes under another format's name
        result = gs.handler(file_path=str(tmp_path / "shot.jpg"))
        assert (tmp_path / "shot.jpg.png").read_bytes() == self.PNG
        assert "shot.jpg.png" in self._text(result)

    def test_an_existing_png_extension_is_not_doubled(self, rig, tmp_path):
        gs.handler(file_path=str(tmp_path / "SHOT.PNG"))
        assert (tmp_path / "SHOT.PNG").exists()
        assert not (tmp_path / "SHOT.PNG.png").exists()

    def test_a_missing_output_directory_is_created(self, rig, tmp_path):
        out = str(tmp_path / "renders" / "deep" / "shot.png")
        result = gs.handler(file_path=out)
        assert result["isError"] is False and os.path.isfile(out)

    def test_an_uncreatable_directory_refuses_before_the_camera_moves(self, rig, tmp_path):
        # the refusal costs the caller nothing: a read tool that reoriented the view and then
        # failed would have moved the user's camera for no picture at all
        rig.monkeypatch.setattr(gs._export.os, "makedirs",
                                lambda *a, **k: (_ for _ in ()).throw(OSError("read-only volume")))
        result = gs.handler(view="top", file_path=str(tmp_path / "nope" / "shot.png"))
        assert result["isError"] is True
        assert "read-only volume" in result["message"] and "nope" in result["message"]
        assert rig.oriented == []

    def test_a_zero_byte_write_is_a_failure_not_an_ok_carrying_the_image(self, rig, tmp_path):
        # a captured-but-empty file is the false success this gate exists for: the caller would be
        # sent to a path holding nothing
        rig.monkeypatch.setattr(gs._view_common, "capture_png_b64", lambda *a, **k: ("", None))
        out = str(tmp_path / "shot.png")
        result = gs.handler(file_path=out)
        assert result["isError"] is True
        assert "size_bytes=0" in result["message"] and out in result["message"]

    def test_an_undecodable_capture_is_reported_naming_the_path(self, rig, tmp_path):
        rig.monkeypatch.setattr(gs._view_common, "capture_png_b64", lambda *a, **k: ("ABC", None))
        out = str(tmp_path / "shot.png")
        result = gs.handler(file_path=out)
        assert result["isError"] is True
        assert "could not be decoded" in result["message"] and out in result["message"]

    def test_a_capture_failure_writes_no_file(self, rig, tmp_path):
        # the capture's OWN error must survive to the caller: attempting the decode/write on a
        # failed capture would relabel it as a decode failure and point at the wrong cause
        rig.monkeypatch.setattr(gs._view_common, "capture_png_b64",
                                lambda *a, **k: (None, "Viewport capture failed."))
        out = str(tmp_path / "shot.png")
        result = gs.handler(file_path=out)
        assert result["isError"] is True
        assert "capture failed" in result["message"]
        assert "could not be decoded" not in result["message"]
        assert not os.path.exists(out)

    def test_the_users_camera_is_restored_after_a_reoriented_shot(self, rig, tmp_path):
        # the shot reorients the camera; a read the user sees as a moved view is a side effect
        # this tool undoes - the snapshot goes back whether or not a file was requested
        vp = gs.app.activeViewport
        snapshot = vp.camera

        def orient(viewport, name):
            viewport.camera = SimpleNamespace(viewExtents=9.0)    # the orient moves the camera

        rig.monkeypatch.setattr(gs._view_common, "apply_named_view", orient)
        gs.handler(view="top", file_path=str(tmp_path / "shot.png"))
        assert vp.camera is snapshot

    def test_the_surface_offers_the_path_and_says_the_image_still_returns(self):
        props = gs.tool.to_dict()["inputSchema"]["properties"]
        assert props["file_path"]["type"] == "string"
        assert "PNG" in props["file_path"]["description"]
        assert "file_path" in gs.TOOL_DESCRIPTION and "inline" in gs.TOOL_DESCRIPTION


class TestKeepVisible:
    def test_target_itself_kept(self):
        assert gs._keep_visible("Frame:1", "Frame:1") is True

    def test_ancestor_kept(self):
        # hiding Frame:1 would hide its nested child - the exact nested-target BLANK cause
        assert gs._keep_visible("Frame:1", "Frame:1+Pedestal:1") is True

    def test_descendant_kept(self):
        assert gs._keep_visible("Frame:1+Pedestal:1", "Frame:1") is True

    def test_sibling_hidden(self):
        assert gs._keep_visible("Gear:1", "Frame:1") is False

    def test_string_prefix_is_not_a_path_boundary(self):
        # 'Frame:10' is a different instance, NOT an ancestor of 'Frame:1' - the '+' boundary matters
        assert gs._keep_visible("Frame:10", "Frame:1") is False
        assert gs._keep_visible("Frame:1", "Frame:10") is False

    def test_missing_path_is_hidden(self):
        assert gs._keep_visible(None, "Frame:1") is False
        assert gs._keep_visible("Frame:1", None) is False


class TestHandlerInputGuards:
    """The two guards ahead of any camera move: an unknown view name, and pixel dimensions that
    are not numbers at all (which must fall back to the documented default, not raise)."""

    @pytest.fixture
    def rig(self, monkeypatch):
        vp = SimpleNamespace(camera=SimpleNamespace(viewExtents=1.0), fit=lambda: None)
        monkeypatch.setattr(gs, "app", SimpleNamespace(activeViewport=vp))
        monkeypatch.setattr(gs._common, "design", lambda: None)
        monkeypatch.setattr(gs._view_common, "apply_named_view", lambda v, name: None)
        return monkeypatch

    def test_unknown_view_is_refused_and_lists_the_valid_ones(self, rig):
        result = gs.handler(view="sideways")
        assert result["isError"] is True
        assert "sideways" in result["message"] and "iso-top-right" in result["message"]

    def test_unusable_dimensions_fall_back_to_the_default_size(self, rig):
        seen = {}

        def capture(vp, width, height, **kw):
            seen["size"] = (width, height)
            return "B64DATA", None

        rig.setattr(gs._view_common, "capture_png_b64", capture)
        result = gs.handler(width="wide", height=None)
        assert result["isError"] is False
        assert seen["size"] == (800, 600)

    def test_no_active_viewport_is_an_error(self, monkeypatch):
        monkeypatch.setattr(gs, "app", SimpleNamespace(activeViewport=None))
        result = gs.handler()
        assert result["isError"] is True and "viewport" in result["message"]
