"""Unit tests for sketch_create.py - the plane/face target, the frame and the rename."""

from types import SimpleNamespace
import pytest
from conftest import load_tool
from _sketch_fakes import FakeSketch, _datum, _payload, draw_installer

sk = load_tool("sketch_create")
_install_draw = draw_installer(sk)


class TestOnFacePlaneNameMisuse:

    def test_construction_plane_name_points_at_plane_param(self, monkeypatch):
        s = FakeSketch(); _install_draw(monkeypatch, s, planes=[_datum("MidPlane")])
        monkeypatch.setattr(sk._ON_FACE, "resolve",
                            lambda raw: (None, "stale handle - re-run find_geometry"))
        res = sk.handler(on_face="MidPlane")
        assert res["isError"] is True
        assert "plane='MidPlane'" in res["message"]
        assert "construction PLANE name" in res["message"]

    def test_an_origin_alias_passed_as_on_face_points_at_the_plane_param_too(self, monkeypatch):
        s = FakeSketch(); _install_draw(monkeypatch, s)
        monkeypatch.setattr(sk._ON_FACE, "resolve",
                            lambda raw: (None, "stale handle - re-run find_geometry"))
        res = sk.handler(on_face="xy")
        assert res["isError"] is True and "plane='xy'" in res["message"]

    def test_genuinely_bad_handle_keeps_the_resolver_error(self, monkeypatch):
        s = FakeSketch(); _install_draw(monkeypatch, s)
        monkeypatch.setattr(sk._ON_FACE, "resolve",
                            lambda raw: (None, "stale handle - re-run find_geometry"))
        res = sk.handler(on_face="NOTAPLANE")
        assert res["isError"] is True
        assert "stale handle" in res["message"]


class TestCreateSketchPlaneRef:

    """sketch_create hands 'plane' to _inputs.PlaneRef, so the sketch lands on the entity the SHARED
    kind resolved: an origin alias off the ACTIVE component, a construction-plane name resolved
    DESIGN-WIDE (a sub-component's datum proxied into the occurrence that places it), the qualified
    '<occurrence>:<plane>' form - and a name several components share is REFUSED, never resolved to
    whichever component happened to answer first."""

    def test_xy_alias_lands_on_the_active_components_origin_plane(self, monkeypatch):
        s = FakeSketch(); d = _install_draw(monkeypatch, s)
        out = _payload(sk.handler(plane="xy"))
        assert d.rootComponent.sketches.added is d.rootComponent.xYConstructionPlane
        assert out["on"] == "plane 'xy'"

    @pytest.mark.parametrize("given, attr", [
        ("xy", "xYConstructionPlane"), ("xz", "xZConstructionPlane"), ("yz", "yZConstructionPlane"),
        ("top", "xYConstructionPlane"), ("front", "xZConstructionPlane"),
        ("right", "yZConstructionPlane"), ("  XY Plane ", "xYConstructionPlane"),
        ("xzplane", "xZConstructionPlane"), ("YZPlane", "yZConstructionPlane")])
    def test_every_alias_spelling_the_tool_takes_still_lands_on_its_origin_plane(
            self, monkeypatch, given, attr):
        # the '<alias> plane' spellings are the kind's now, not a local fold - the tool must still
        # take every one of them.
        s = FakeSketch(); d = _install_draw(monkeypatch, s)
        _payload(sk.handler(plane=given))
        assert d.rootComponent.sketches.added is getattr(d.rootComponent, attr)

    def test_a_datum_named_mid_plane_still_resolves_by_name(self, monkeypatch):
        # the suffix handling must not swallow a construction plane whose own name ends in 'plane'
        cp = _datum("Mid plane")
        s = FakeSketch(); d = _install_draw(monkeypatch, s, planes=[cp])
        _payload(sk.handler(plane="Mid plane"))
        assert d.rootComponent.sketches.added is cp

    def test_an_empty_plane_defaults_to_xy(self, monkeypatch):
        s = FakeSketch(); d = _install_draw(monkeypatch, s)
        out = _payload(sk.handler(plane=""))
        assert d.rootComponent.sketches.added is d.rootComponent.xYConstructionPlane
        assert out["on"] == "plane 'xy'"      # the label names the default that actually resolved

    def test_a_root_construction_plane_resolves_by_name(self, monkeypatch):
        cp = _datum("Datum1")
        s = FakeSketch(); d = _install_draw(monkeypatch, s, planes=[cp])
        _payload(sk.handler(plane="Datum1"))
        assert d.rootComponent.sketches.added is cp

    def test_a_sub_component_datum_resolves_as_a_proxy_into_its_occurrence(self, monkeypatch):
        # the capability the active-component-only lookup had no reach for: a datum created inside a
        # sub-component. Its NATIVE form is component-local, so it must arrive PROXIED into the one
        # occurrence that places its owner.
        s = FakeSketch()
        d = _install_draw(monkeypatch, s, subs=[("Tower", ["Datum_A"], ["Tower:1"])])
        _payload(sk.handler(plane="Datum_A"))
        landed = d.rootComponent.sketches.added
        native = d.allComponents.itemByName("Tower").constructionPlanes.itemByName("Datum_A")
        assert landed.native is native
        assert landed.context.fullPathName == "Tower:1"

    def test_a_datum_name_two_components_share_is_refused_with_its_candidates(self, monkeypatch):
        s = FakeSketch()
        d = _install_draw(monkeypatch, s,
                          subs=[("A", ["Mid"], ["A:1"]), ("B", ["Mid"], ["B:1"])])
        res = sk.handler(plane="Mid")
        assert res["isError"] is True and "ambiguous" in res["message"]
        assert "A:1:Mid" in res["message"] and "B:1:Mid" in res["message"]
        assert d.rootComponent.sketches.added is None      # refused BEFORE the sketch was created

    def test_the_qualified_occurrence_form_picks_one_instance(self, monkeypatch):
        s = FakeSketch()
        d = _install_draw(monkeypatch, s,
                          subs=[("A", ["Mid"], ["A:1"]), ("B", ["Mid"], ["B:1"])])
        _payload(sk.handler(plane="B:1:Mid"))
        landed = d.rootComponent.sketches.added
        assert landed.native is d.allComponents.itemByName("B").constructionPlanes.itemByName("Mid")
        assert landed.context.fullPathName == "B:1"

    def test_the_active_components_own_datum_name_shadows_another_components(self, monkeypatch):
        # Fusion default-names the FIRST datum of every component 'Plane1', so the active
        # component's own must win rather than the design-wide vote refusing the commonest name.
        s = FakeSketch()
        d = _install_draw(monkeypatch, s, active="A",
                          subs=[("A", ["Plane1"], ["A:1"]), ("B", ["Plane1"], ["B:1"])])
        _payload(sk.handler(plane="Plane1"))
        native = d.allComponents.itemByName("A").constructionPlanes.itemByName("Plane1")
        assert d.activeComponent.sketches.added is native   # native: already the build context

    def test_an_unresolvable_plane_names_the_vocabulary_that_would_work(self, monkeypatch):
        s = FakeSketch(); d = _install_draw(monkeypatch, s)
        res = sk.handler(plane="nonsense")
        assert res["isError"] is True
        assert "not an origin alias" in res["message"] and "find_geometry" in res["message"]
        assert d.rootComponent.sketches.added is None

    def test_the_plane_schema_is_the_kinds_own(self, monkeypatch):
        # the contract an agent reads comes from PlaneRef, so it cannot drift from what resolves
        prop = sk.tool.to_dict()["inputSchema"]["properties"]["plane"]
        assert prop["description"] == sk._PLANE.schema()["description"]
        assert "top/front/right" in prop["description"]     # the aliases the tool has always taken
        assert "handle" in prop["description"]              # plus the handle form the kind adds


class TestCreateFrameParity:

    """sketch_create and sketch_get publish the SAME frame block from the one helper, so a caller
    places geometry against the numbers it later verifies against."""

    @staticmethod
    def _framed(z_cm=1.5):
        s = FakeSketch()
        s.origin = SimpleNamespace(x=0.0, y=0.0, z=z_cm)
        s.xDirection = SimpleNamespace(x=1.0, y=0.0, z=0.0)
        s.yDirection = SimpleNamespace(x=0.0, y=0.0, z=-1.0)
        # Root-owned: local IS world. A sketch whose component is instanced several times gets the
        # component-local frame instead (test__sketch_detail's TestFrameSpace). The token is the
        # design root's own - two wrappers of one root component measured share one entityToken,
        # and that is what same_component compares.
        root = SimpleNamespace(name="Root", entityToken="TOKEN:Root")
        root.parentDesign = SimpleNamespace(rootComponent=root)
        s.parentComponent = root
        return s

    def test_created_sketch_publishes_origin_axes_and_normal(self, monkeypatch):
        # origin 1.5 cm up world Z reads 15 mm; the normal is x cross y = (1,0,0) x (0,0,-1).
        _install_draw(monkeypatch, self._framed())
        out = _payload(sk.handler(plane="xz"))
        assert out["frame"] == {"origin_mm": [0.0, 0.0, 15.0],
                                "x_world": [1.0, 0.0, 0.0],
                                "y_world": [0.0, 0.0, -1.0],
                                "normal": [0.0, 1.0, 0.0],
                                "space": "world"}

    def test_frame_comes_from_the_shared_helper(self):
        # one definition, imported - a second local copy is how create and read start disagreeing.
        detail = load_tool("_sketch_detail")
        assert sk.sketch_world_frame is detail.sketch_world_frame

    def test_a_sketch_with_no_readable_plane_reports_frame_null(self, monkeypatch):
        _install_draw(monkeypatch, FakeSketch())      # no origin/xDirection/yDirection
        out = _payload(sk.handler(plane="xy"))
        assert out["frame"] is None
        assert out["created"] is True                 # the create still succeeded


class TestCreateFrameNote:

    def test_the_note_states_this_frame_and_does_not_restate_the_xz_mapping(self, monkeypatch):
        # The note names the space this result's frame numbers are in, and stops there. The
        # per-plane axis mapping is the frame block's own y_world, which reads [0,0,-1] on an xz
        # sketch.
        _install_draw(monkeypatch, TestCreateFrameParity._framed())
        out = _payload(sk.handler(plane="xz"))
        assert "frame.space='world'" in out["note"]
        assert "world -Z" not in out["note"]
        assert "sketch_get" not in out["note"]        # sketch_get's own description carries that.


class TestCreateRenameDisclosure:

    def test_a_swallowed_rename_is_disclosed_beside_the_actual_name(self, monkeypatch):
        # the platform can accept the name assignment and keep its existing name: the payload's
        # sketch_name is the read-back, and the declined rename is DISCLOSED - never swallowed.
        class StubbornSketch(FakeSketch):
            @property
            def name(self):
                return "Sketch1"

            @name.setter
            def name(self, v):
                pass

        s = StubbornSketch(); _install_draw(monkeypatch, s)
        out = _payload(sk.handler(plane="xy", name="Pocket Outline"))
        assert out["sketch_name"] == "Sketch1"
        assert "Pocket Outline" in out["rename_warning"]
        assert "did not take" in out["rename_warning"]

    def test_a_clean_rename_carries_no_warning(self, monkeypatch):
        s = FakeSketch(); _install_draw(monkeypatch, s)
        out = _payload(sk.handler(plane="xy", name="Pocket Outline"))
        assert out["sketch_name"] == "Pocket Outline"
        assert "rename_warning" not in out
