"""Unit tests for ``view_section.py`` — the Section Analysis cutaway tool.

The branches that matter and could silently misbehave: action validation, plane
aliasing (top/front/right -> xy/xz/yz), the ``through``-occurrence path that
converts a bbox center into a section distance along the chosen plane's normal
(mm->cm, per-plane coordinate pick), ``flip``/``show_hatch`` propagation into the
SectionAnalysisInput, and list/clear. The createInput call is captured on a fake
``sectionAnalyses`` so we can assert the distance/flags handed to Fusion without
a live session. ``auto_view`` camera aiming is a viewport side-effect (no pure
logic) — left to live testing; we keep auto_view=false here so handler() returns
without touching a camera.
"""

import json

from conftest import (
    Camera,
    FakeApplication,
    FakeOccurrence,
    FakePoint,
    FakeVector3D,
    MakeComp,
    MakeDesign,
    Viewport,
    load_tool,
    make_bbox,
)

sv = load_tool("view_section")


# ── fakes ───────────────────────────────────────────────────────────────────
#
# The session, design, component and occurrence come from conftest's shared fakes; only the
# section-analysis object graph below is local, SectionAnalysis carrying no shape dump.

def _occ(name, bbox=None, full_path=None, bodies_bbox=FakeOccurrence._UNSET):
    """One occurrence the 'through' path measures: the shared Occurrence fake placing a component
    of the same base name, with its own bounding box. `bodies_bbox` is what boundingBox2 answers -
    left unset the occurrence carries none, the state the plain-box fallback is measured on."""
    return FakeOccurrence(path=full_path or name, component=MakeComp(name=name.split(":")[0]),
                          bounding_box=bbox, bodies_bounding_box=bodies_bbox)


class FakeSectionInput:
    def __init__(self, entity, distance_cm):
        self.entity = entity
        self.distance_cm = distance_cm
        self.flip = False
        self.isHatchShown = True


class FakeSection:
    def __init__(self, name, delete_ok=True):
        self.name = name
        self.isLightBulbOn = True
        self._deleted = False
        self._delete_ok = delete_ok
        self._owner = None          # set when it joins a FakeSectionAnalyses

    def deleteMe(self):
        # Mirror Fusion: a successful delete takes the analysis OUT of the collection; a refused
        # one returns false and leaves the model cut.
        if not self._delete_ok:
            return False
        self._deleted = True
        if self._owner is not None and self in self._owner._items:
            self._owner._items.remove(self)
        return True


class FakeSectionAnalyses:
    def __init__(self, existing=()):
        self._items = list(existing)
        for s in self._items:
            s._owner = self
        self.last_input = None
        self.add_calls = 0

    @property
    def count(self):
        return len(self._items)

    def item(self, i):
        return self._items[i]

    def createInput(self, entity, distance_cm):
        self.last_input = FakeSectionInput(entity, distance_cm)
        return self.last_input

    def add(self, inp):
        self.add_calls += 1
        sec = FakeSection(f"Section{self.add_calls}")
        sec._owner = self
        self._items.append(sec)
        return sec


class FakeAnalyses:
    """design.analyses, holding sectionAnalyses. Bespoke: Analyses has no shape dump, so there is
    no shared fake to stand for it."""
    def __init__(self, sections):
        self.sectionAnalyses = sections


def _install(occurrences=(), existing_sections=()):
    sections = FakeSectionAnalyses(existing_sections)
    root = MakeComp(name="Root", occurrences=occurrences,
                    origin_planes=("PLANE_XY", "PLANE_XZ", "PLANE_YZ"))
    design = MakeDesign(comp=root, analyses=FakeAnalyses(sections))
    sv.app = FakeApplication(
        active_product=design,
        # _aim_at_cut reads eye/target (for distance) and writes eye/upVector/isFitView.
        active_viewport=Viewport(camera=Camera(eye=(10, 0, 0), up=None)))
    sv._common.app = sv.app
    import adsk.fusion
    adsk.fusion.Design.cast = lambda x: x if isinstance(x, MakeDesign) else None
    # the bare-plane path uses PlaneRef, which resolves via _common.design()/target_component()
    # (the app-reference seam) — point them at the fake root so an origin alias resolves.
    sv._inputs._common.design = lambda: design
    sv._inputs._common.target_component = lambda d: root
    return sections


def _payload(result):
    assert result["isError"] is False, result
    return json.loads(result["content"][0]["text"])


# ── guards / validation ─────────────────────────────────────────────────────

class TestGuards:
    def test_unknown_action(self):
        _install()
        res = sv.handler(action="slice")
        assert res["isError"] is True and "Unknown action" in res["message"]

    def test_no_design(self):
        sv.app = FakeApplication(active_product=None)
        sv._common.app = sv.app
        import adsk.fusion
        adsk.fusion.Design.cast = lambda x: None
        res = sv.handler(action="cut", plane="xy")
        assert res["isError"] is True and "No active design" in res["message"]

    def test_cut_requires_plane_or_through(self):
        _install()
        res = sv.handler(action="cut")
        assert res["isError"] is True
        assert "Provide 'plane'" in res["message"]

    def test_through_unknown_occurrence(self):
        _install(occurrences=[_occ("Vise")])
        res = sv.handler(action="cut", through="Nonexistent")
        assert res["isError"] is True
        assert "no occurrence matching" in res["message"].lower()


class TestActionEnum:
    """The schema's action vocabulary is _ACTIONS itself, and every member reaches a branch."""

    def test_the_action_enum_matches_the_action_tuple(self):
        # Catches a HAND-EDITED schema drifting from the tuple the handler guards on: a value the
        # enum offers and the guard refuses is unreachable, and one it omits is unofferable.
        assert sv.tool.input_schema["properties"]["action"]["enum"] == list(sv._ACTIONS)

    def test_every_advertised_action_dispatches(self):
        # A member no `if action ==` branch reads would fall through to the cut path, so the schema
        # would advertise an action that cuts the model instead of doing what it names.
        extra = {"cut": {"plane": "xy", "auto_view": False}}
        for name in sv._ACTIONS:
            _install()
            out = _payload(sv.handler(action=name, **extra.get(name, {})))
            assert out["action"] == name, name


# ── plain plane cut ─────────────────────────────────────────────────────────

class TestPlaneCut:
    def test_xy_plane_uses_xy_construction_plane_at_zero(self):
        sections = _install()
        out = _payload(sv.handler(action="cut", plane="xy", auto_view=False))
        assert out["action"] == "cut"
        assert sections.last_input.entity == "PLANE_XY"
        assert sections.last_input.distance_cm == 0.0

    def test_alias_front_maps_to_xz(self):
        sections = _install()
        _payload(sv.handler(action="cut", plane="front", auto_view=False))
        assert sections.last_input.entity == "PLANE_XZ"

    def test_offset_mm_converted_to_cm(self):
        sections = _install()
        _payload(sv.handler(action="cut", plane="xy", offset=10.0, auto_view=False))
        assert sections.last_input.distance_cm == 1.0   # 10 mm -> 1 cm

    def test_flip_and_hatch_propagate(self):
        sections = _install()
        out = _payload(sv.handler(action="cut", plane="yz", flip=True,
                                  show_hatch=False, auto_view=False))
        assert sections.last_input.flip is True
        assert sections.last_input.isHatchShown is False
        assert out["flipped"] is True

    # REGRESSION: the DEFAULT call view_section(cut, plane='xy') runs with auto_view=True; the
    # bare-plane branch must not reference an unbound `pkey` (that raises NameError AFTER the section
    # is created - an error on the happy path). auto_view=False would mask it.
    def test_default_auto_view_bare_plane_does_not_raise(self):
        sections = _install()
        out = _payload(sv.handler(action="cut", plane="xy"))   # auto_view defaults True
        assert out["action"] == "cut"
        assert out["auto_viewed"] is True                       # origin alias -> camera aimed
        assert sections.last_input.entity == "PLANE_XY"

    def test_auto_view_skipped_for_non_origin_plane_handle(self):
        # A construction-plane name / face handle has no fixed world normal -> auto-aim is skipped
        # gracefully (not a NameError). PlaneRef resolves the handle; we feed it through the seam.
        sections = _install()
        real_resolve = sv._PLANE.resolve
        sv._PLANE.resolve = lambda v, component=None: ("FACE_HANDLE", None)
        try:
            out = _payload(sv.handler(action="cut", plane="Plane3"))   # auto_view default True
        finally:
            sv._PLANE.resolve = real_resolve
        assert out["action"] == "cut"
        assert out["auto_viewed"] is False


# ── the plane is resolved in ROOT context, not the active component's ───────

class TestRootContextPlane:
    """A Section Analysis is a DOCUMENT-level view: whatever component is active, the plane it cuts
    on is the root's. Resolved against the active component instead, a sub-component's own native
    plane reaches sectionAnalyses.add, which refuses it ('object is not in the assembly context of
    this component') - the model is then not cut at all."""

    def _sub_active(self, monkeypatch):
        """Install the rig with a SUB-COMPONENT active, carrying origin planes of its own so an
        alias resolved against it would succeed - and land on the wrong plane."""
        sections = _install()
        sub = type("Comp", (), {"name": "Inner", "xYConstructionPlane": "SUB_XY",
                                "xZConstructionPlane": "SUB_XZ", "yZConstructionPlane": "SUB_YZ",
                                "constructionPlanes": None})()
        monkeypatch.setattr(sv._common, "target_component", lambda d: sub)
        monkeypatch.setattr(sv._inputs._common, "target_component", lambda d: sub)
        return sections

    def test_an_alias_cuts_on_the_ROOTs_plane_while_a_sub_component_is_active(self, monkeypatch):
        sections = self._sub_active(monkeypatch)
        out = _payload(sv.handler(action="cut", plane="xz", auto_view=False))
        assert out["action"] == "cut"
        assert sections.last_input.entity == "PLANE_XZ"       # the ROOT's, never "SUB_XZ"

    def test_every_alias_reaches_the_root_not_the_active_sub_component(self, monkeypatch):
        # One alias could be right by accident (a shared attribute name); all three cannot.
        for alias, want in (("xy", "PLANE_XY"), ("front", "PLANE_XZ"), ("right", "PLANE_YZ")):
            sections = self._sub_active(monkeypatch)
            _payload(sv.handler(action="cut", plane=alias, auto_view=False))
            assert sections.last_input.entity == want, alias


# ── through-occurrence center math ──────────────────────────────────────────

class TestThroughCenter:
    def test_xy_uses_z_center(self):
        # bbox z spans 2..4 cm -> center cz = 3 cm; xy normal is Z. The x and y centers (5 and 4)
        # differ from it and from each other, so only the Z read lands on 3.
        occ = _occ("Part", bbox=make_bbox((0, 0, 2), (10, 8, 4)))
        sections = _install(occurrences=[occ])
        _payload(sv.handler(action="cut", through="Part", plane="xy", auto_view=False))
        assert sections.last_input.distance_cm == 3.0

    def test_front_uses_y_center(self):
        # y spans 1..5 -> cy = 3; front/xz normal is Y. x centers on 4 and z on 2, so 3 can only
        # have come from the Y read.
        occ = _occ("Part", bbox=make_bbox((0, 1, 0), (8, 5, 4)))
        sections = _install(occurrences=[occ])
        _payload(sv.handler(action="cut", through="Part", plane="front", auto_view=False))
        assert sections.last_input.distance_cm == 3.0

    def test_through_adds_explicit_offset_on_top_of_center(self):
        # cy = 3 cm, plus 20 mm (=2 cm) offset -> 5 cm.
        occ = _occ("Part", bbox=make_bbox((0, 1, 0), (8, 5, 4)))
        sections = _install(occurrences=[occ])
        _payload(sv.handler(action="cut", through="Part", plane="front",
                            offset=20.0, auto_view=False))
        assert sections.last_input.distance_cm == 5.0

    def test_through_defaults_to_xz_when_no_plane(self):
        occ = _occ("Part", bbox=make_bbox((0, 1, 0), (6, 5, 4)))
        sections = _install(occurrences=[occ])
        out = _payload(sv.handler(action="cut", through="Part", auto_view=False))
        assert sections.last_input.entity == "PLANE_XZ"
        assert "xz plane" in out["where"]

    def test_a_shown_construction_rectangle_does_not_carry_the_cut_off_the_body(self):
        # The plain box spans z 0..40 because a construction rectangle is visible; the bodies-only
        # box spans 2..4. Centred on the plain box the cut lands at z 20, clear of the body.
        occ = _occ("Part", bbox=make_bbox((0, 0, 0), (10, 8, 40)),
                   bodies_bbox=make_bbox((0, 0, 2), (10, 8, 4)))
        sections = _install(occurrences=[occ])
        out = _payload(sv.handler(action="cut", through="Part", plane="xy", auto_view=False))
        assert sections.last_input.distance_cm == 3.0
        assert out["centered_on"] == "solids"

    def test_an_occurrence_placing_no_body_falls_back_to_its_whole_box(self):
        occ = _occ("Sketchy", bbox=make_bbox((0, 0, 2), (10, 8, 4)), bodies_bbox=None)
        sections = _install(occurrences=[occ])
        out = _payload(sv.handler(action="cut", through="Sketchy", plane="xy", auto_view=False))
        assert sections.last_input.distance_cm == 3.0
        assert out["centered_on"] == "all_geometry"

    def test_an_occurrence_with_no_readable_box_is_refused_rather_than_cut_at_the_origin(self):
        # neither box reads, so there is no centre: cutting anyway puts the plane at the bare
        # origin and publishes a 'centered_on' for a measurement that never happened.
        occ = _occ("Opaque", bbox=None, bodies_bbox=None)
        sections = _install(occurrences=[occ])
        res = sv.handler(action="cut", through="Opaque", plane="xy", auto_view=False)
        assert res["isError"] is True
        assert "no readable bounding box" in res["message"] and "Opaque" in res["message"]
        assert sections.last_input is None            # nothing was cut

    def test_through_substring_match(self):
        occ = _occ("Carrier Body:1", bbox=make_bbox((0, 0, 0), (2, 2, 2)))
        sections = _install(occurrences=[occ])
        out = _payload(sv.handler(action="cut", through="carrier", plane="xy", auto_view=False))
        assert "Carrier Body:1" in out["where"]


# ── auto-view camera aiming (_aim_at_cut math) ───────────────────────────────

class TestAutoViewAim:
    """_aim_at_cut places the camera on the +normal side (flip reverses it) and picks +Z up unless
    the cut normal IS Z (top/bottom), where it uses +Y. The default test camera is eye (10,0,0)
    target (0,0,0) -> distance 10, so eye lands at target + normal*10."""

    def _patch_create(self):
        import adsk.core
        adsk.core.Point3D.create = staticmethod(lambda x, y, z: FakePoint(x, y, z))
        adsk.core.Vector3D.create = staticmethod(lambda x, y, z: FakeVector3D(x, y, z))

    def test_yz_cut_aims_camera_down_plus_x_with_z_up(self):
        self._patch_create()
        _install()
        sv.handler(action="cut", plane="yz")   # yz normal = +X, auto_view defaults True
        cam = sv.app.activeViewport.camera
        assert (cam.eye.x, cam.eye.y, cam.eye.z) == (10, 0, 0)   # +X side at distance 10
        assert (cam.upVector.x, cam.upVector.y, cam.upVector.z) == (0, 0, 1)  # side cut -> Z up

    def test_flip_reverses_the_revealing_side(self):
        self._patch_create()
        _install()
        sv.handler(action="cut", plane="yz", flip=True)   # +X normal reversed -> camera on -X
        cam = sv.app.activeViewport.camera
        assert (cam.eye.x, cam.eye.y, cam.eye.z) == (-10, 0, 0)

    def test_top_cut_uses_y_up_because_normal_is_z(self):
        self._patch_create()
        _install()
        sv.handler(action="cut", plane="xy")   # xy/top normal = +Z -> up must switch to +Y
        cam = sv.app.activeViewport.camera
        assert (cam.eye.x, cam.eye.y, cam.eye.z) == (0, 0, 10)
        assert (cam.upVector.x, cam.upVector.y, cam.upVector.z) == (0, 1, 0)


# ── list / clear ─────────────────────────────────────────────────────────────

class TestListClear:
    def test_list_reports_sections(self):
        _install(existing_sections=[FakeSection("Section1"), FakeSection("Section2")])
        out = _payload(sv.handler(action="list"))
        assert out["count"] == 2
        assert {s["name"] for s in out["sections"]} == {"Section1", "Section2"}

    def test_clear_removes_all(self):
        s1, s2 = FakeSection("Section1"), FakeSection("Section2")
        _install(existing_sections=[s1, s2])
        out = _payload(sv.handler(action="clear"))
        assert out["removed_count"] == 2
        assert s1._deleted and s2._deleted
        # the claim "all removed" is gated on the count read BACK, not on the delete calls made
        assert out["sections_before"] == 2 and out["sections_after"] == 0
        assert "no longer cut" in out["note"]

    def test_a_refused_delete_is_an_error_not_a_partial_success(self):
        # deleteMe() returning FALSE leaves the model cut. Counting only the successful deletes and
        # then asserting "All section analyses removed" is the false-ok this gate exists to stop.
        s1 = FakeSection("Section1")
        s2 = FakeSection("Stubborn", delete_ok=False)
        _install(existing_sections=[s1, s2])
        res = sv.handler(action="clear")
        assert res["isError"] is True
        assert "Stubborn" in res["message"]
        assert "STILL cut" in res["message"]
        assert s1._deleted is True and s2._deleted is False   # the one that could go, went

    def test_a_surviving_section_after_every_delete_returned_true_is_an_error(self):
        # Every deleteMe() answers true but the collection does not shrink - the platform reported a
        # success it did not perform, so the count read back is what decides.
        s1 = FakeSection("Section1")
        s1._owner = None                       # deleteMe() returns True but never leaves the list
        secs = _install(existing_sections=[])
        secs._items.append(s1)
        res = sv.handler(action="clear")
        assert res["isError"] is True
        assert "still reads 1" in res["message"]

    def test_an_unreadable_section_count_refuses_instead_of_claiming_an_empty_sweep(self):
        secs = _install(existing_sections=[])

        class _Unreadable:
            @property
            def count(self):
                raise RuntimeError("nope")
            def item(self, i):
                raise AssertionError("must not be walked")
        sv.app.activeProduct.analyses.sectionAnalyses = _Unreadable()
        res = sv.handler(action="clear")
        assert res["isError"] is True
        assert "how many section analyses exist" in res["message"]
        assert secs is not None

    def test_clear_removes_every_section_from_a_collection_that_shrinks_as_it_deletes(self):
        # deleteMe() takes the section OUT of sectionAnalyses, so the indices shift under the walk.
        # A forward walk visits 0,1,2 over a list that is 3,2,1 long and leaves half the sections
        # cut - the model stays sectioned while the payload reports them all removed.
        secs = _install(existing_sections=[])
        made = [FakeSection("Section%d" % i) for i in (1, 2, 3, 4)]
        for s in made:
            s.deleteMe = (lambda s=s: (secs._items.remove(s), setattr(s, "_deleted", True), True)[-1])
        secs._items.extend(made)
        out = _payload(sv.handler(action="clear"))
        assert out["removed_count"] == 4
        assert sorted(out["removed"]) == ["Section1", "Section2", "Section3", "Section4"]
        assert all(s._deleted for s in made)
        assert secs._items == []          # nothing left cutting the model
