"""Unit tests for model_loft.py - the ordered sections, rails/centerline and the cut evidence."""

import json

import adsk.core
import adsk.fusion
import pytest

from conftest import (load_tool, make_design, install, MakeComp, BRepBody, BRepFace, FakeFeature,
                      FakeFeatures, Profile, make_sketch)

so = load_tool("model_loft")


class _FakeFeature(FakeFeature):
    """The shared feature plus isSolid - the flag loft reads off the FEATURE, not off the body."""
    def __init__(self, name, result_bodies, is_solid=None):
        super().__init__(name=name, bodies=result_bodies)
        if is_solid is not None:
            self.isSolid = is_solid


class _FakeLoftSections:
    def __init__(self):
        self.added = []      # records ORDER of section adds
    def add(self, section):
        self.added.append(section)
        return section


class _FakeCenterLineOrRails:
    def __init__(self):
        self.centerlines = []
        self.rails = []
    def addCenterLine(self, c):
        self.centerlines.append(c)
    def addRail(self, r):
        self.rails.append(r)


class _FakeLoftInput:
    def __init__(self, op):
        self.operation = op
        self.loftSections = _FakeLoftSections()
        self.centerLineOrRails = _FakeCenterLineOrRails()
        self.isSolid = True


class _SwallowingLoftInput(_FakeLoftInput):
    """A LoftFeatureInput that ACCEPTS the isClosed write and keeps its default anyway - the SWIG
    shape set_verified exists to catch (nothing raises, the loft would just run open)."""
    def __setattr__(self, name, value):
        object.__setattr__(self, name, False if name == "isClosed" else value)


class _FakeLoftFeatures:
    def __init__(self, result_is_solid=True, result_bodies=None, input_cls=_FakeLoftInput):
        self.last_input = None
        self._result_is_solid = result_is_solid
        self._input_cls = input_cls
        self._result_bodies = (result_bodies if result_bodies is not None
                               else [BRepBody("Body1", is_solid=True)])
    def createInput(self, op):
        self.last_input = self._input_cls(op)
        return self.last_input
    def add(self, inp):
        return _FakeFeature("Loft1", self._result_bodies, is_solid=self._result_is_solid)


class _FakeFeatures(FakeFeatures):
    """comp.features: a per-kind collection is present only when the test wires one."""
    def __init__(self, loft=None, stitch=None, unstitch=None):
        super().__init__()
        if loft is not None:
            self.loftFeatures = loft
        if stitch is not None:
            self.stitchFeatures = stitch
        if unstitch is not None:
            self.unstitchFeatures = unstitch


@pytest.fixture(autouse=True)
def _types(monkeypatch):
    monkeypatch.setattr(adsk.fusion, "BRepBody", BRepBody, raising=False)
    monkeypatch.setattr(adsk.fusion, "BRepFace", BRepFace, raising=False)
    monkeypatch.setattr(adsk.fusion, "Profile", Profile, raising=False)
    monkeypatch.setattr(adsk.core.ValueInput, "createByReal",
                        staticmethod(lambda v: ("real", v)), raising=False)


def _install(features, bodies_by_name=None, handle_map=None):
    comp = MakeComp(name="Comp", bodies=list((bodies_by_name or {}).values()), mesh_bodies=())
    comp.features = features
    return install(so, make_design(comp=comp, tokens=handle_map))


def _payload(result):
    assert result["isError"] is False, result
    return json.loads(result["content"][0]["text"])


class TestLoft:

    def _profiles_design(self, result_is_solid=True, result_bodies=None):
        lf = _FakeLoftFeatures(result_is_solid=result_is_solid, result_bodies=result_bodies)
        # three profile handles -> live Profile entities (order P0, P1, P2)
        p0, p1, p2 = (Profile("0"), Profile("1"), Profile("2"))
        handles = {"H0": p0, "H1": p1, "H2": p2}
        _install(_FakeFeatures(loft=lf), handle_map=handles)
        return lf, (p0, p1, p2)

    def _cut_design(self, bodies, moves=()):
        """A loft over 3 profiles on a component holding `bodies`; `moves` are (body, new_volume)
        pairs the add applies - the material effect a real cut has across the mutation."""
        lf = _FakeLoftFeatures()
        handles = {"H0": Profile("0"), "H1": Profile("1"), "H2": Profile("2")}
        _install(_FakeFeatures(loft=lf), bodies_by_name={b.name: b for b in bodies},
                 handle_map=handles)
        base_add = lf.add

        def _add(inp):
            for body, volume in moves:
                body.volume = volume
            return base_add(inp)
        lf.add = _add
        return lf

    def test_three_profiles_added_in_order(self):
        lf, (p0, p1, p2) = self._profiles_design()
        out = _payload(so.handler(profiles=["H0", "H1", "H2"]))
        assert out["lofted"] is True
        assert out["profiles_count"] == 3
        # ORDER is the whole game: sections added exactly H0,H1,H2.
        assert lf.last_input.loftSections.added == [p0, p1, p2]

    def test_a_join_landing_a_new_body_names_model_combine(self):
        lf = _FakeLoftFeatures()
        _install(_FakeFeatures(loft=lf), bodies_by_name={"Bar": BRepBody("Bar")},
                 handle_map={"H0": Profile("0"), "H1": Profile("1")})
        out = _payload(so.handler(profiles=["H0", "H1"], operation="join"))
        assert "landed a NEW body (Body1)" in out["note"]
        assert "model_combine(join)" in out["note"]

    def test_a_join_that_grew_the_body_already_there_appends_nothing(self):
        # the feature's result body IS the one the component held - the join fused, say nothing
        lf = _FakeLoftFeatures()
        _install(_FakeFeatures(loft=lf), bodies_by_name={"Body1": BRepBody("Body1")},
                 handle_map={"H0": Profile("0"), "H1": Profile("1")})
        out = _payload(so.handler(profiles=["H0", "H1"], operation="join"))
        assert "NEW body" not in out["note"]

    def test_reports_is_solid_read_back(self):
        self._profiles_design(result_is_solid=True)
        out = _payload(so.handler(profiles=["H0", "H1", "H2"]))
        assert out["is_solid"] is True

    def test_surface_loft_reports_not_solid(self):
        self._profiles_design(result_is_solid=False,
                              result_bodies=[BRepBody("Srf1", is_solid=False)])
        out = _payload(so.handler(profiles=["H0", "H1"], as_surface=True))
        assert out["is_solid"] is False
        assert "SURFACE" in out["note"]

    def test_as_surface_sets_isSolid_false_on_input(self):
        lf, _ = self._profiles_design()
        _payload(so.handler(profiles=["H0", "H1"], as_surface=True))
        assert lf.last_input.isSolid is False

    def test_fewer_than_two_rejected(self):
        self._profiles_design()
        res = so.handler(profiles=["H0"])
        assert res["isError"] is True
        assert "at least 2 profiles" in res["message"]

    def test_is_closed_set_on_input_and_reported(self):
        lf, _ = self._profiles_design()
        out = _payload(so.handler(profiles=["H0", "H1", "H2"], is_closed=True))
        assert lf.last_input.isClosed is True
        assert out["is_closed"] is True

    def test_is_closed_omitted_writes_nothing_and_reports_nothing(self):
        # an unwritten isClosed keeps the API default; the payload must not claim a value for it
        lf, _ = self._profiles_design()
        out = _payload(so.handler(profiles=["H0", "H1"]))
        assert not hasattr(lf.last_input, "isClosed")
        assert "is_closed" not in out

    def test_is_closed_false_is_written_not_skipped(self):
        # False is a REQUEST, not an omission - `if is_closed:` would silently drop it
        lf, _ = self._profiles_design()
        out = _payload(so.handler(profiles=["H0", "H1"], is_closed=False))
        assert lf.last_input.isClosed is False
        assert out["is_closed"] is False

    def test_is_closed_accepts_two_sections(self):
        # measured: a TWO-section closed loft builds a real feature - there is no >=3 guard
        self._profiles_design()
        out = _payload(so.handler(profiles=["H0", "H1"], is_closed=True))
        assert out["lofted"] is True and out["profiles_count"] == 2

    def test_is_closed_that_does_not_take_is_refused(self):
        lf = _FakeLoftFeatures(input_cls=_SwallowingLoftInput)
        handles = {"H0": Profile("0"), "H1": Profile("1")}
        _install(_FakeFeatures(loft=lf), handle_map=handles)
        res = so.handler(profiles=["H0", "H1"], is_closed=True)
        assert res["isError"] is True
        assert "is_closed=True" in res["message"] and "reads back unchanged" in res["message"]

    def test_rails_and_centerline_both_rejected(self):
        lf = _FakeLoftFeatures()
        rail_ent = object()
        center_ent = object()
        handles = {"H0": Profile("0"), "H1": Profile("1"),
                   "R": rail_ent, "C": center_ent}
        _install(_FakeFeatures(loft=lf), handle_map=handles)
        res = so.handler(profiles=["H0", "H1"], rails=["R"], centerline="C")
        assert res["isError"] is True
        assert "centerline OR rails" in res["message"] or "not both" in res["message"]

    def test_centerline_set_on_input(self):
        lf = _FakeLoftFeatures()
        center_ent = object()
        handles = {"H0": Profile("0"), "H1": Profile("1"), "C": center_ent}
        _install(_FakeFeatures(loft=lf), handle_map=handles)
        out = _payload(so.handler(profiles=["H0", "H1"], centerline="C"))
        assert lf.last_input.centerLineOrRails.centerlines == [center_ent]
        assert out["has_centerline"] is True

    def test_rails_added_and_counted(self):
        lf = _FakeLoftFeatures()
        r1, r2 = object(), object()
        handles = {"H0": Profile("0"), "H1": Profile("1"), "R1": r1, "R2": r2}
        _install(_FakeFeatures(loft=lf), handle_map=handles)
        out = _payload(so.handler(profiles=["H0", "H1"], rails=["R1", "R2"]))
        assert lf.last_input.centerLineOrRails.rails == [r1, r2]
        assert out["rails_count"] == 2
        assert out["has_centerline"] is False

    def _sketch_rail_design(self, sketch_name="Spine"):
        """A loft design whose component also holds a SKETCH carrying one arc - the spine case:
        at first-loft time no body exists, so find_geometry can mint no handle for it."""
        lf = _FakeLoftFeatures()
        arc = object()
        sk = make_sketch(name=sketch_name, arcs=[arc])
        comp = MakeComp(name="Comp", bodies=(), mesh_bodies=(), sketches=[sk])
        comp.features = _FakeFeatures(loft=lf)
        install(so, make_design(comp=comp,
                                tokens={"H0": Profile("0"), "H1": Profile("1")}))
        return lf, arc

    def test_a_rail_can_be_a_SKETCH_CURVE_ref(self):
        # MEASURED: LoftCenterLineOrRails.addRail(SketchArc) is accepted. find_geometry acquires
        # BRep faces/edges/vertices only, so this second spelling is the only way to hand a loft a
        # spine drawn before any body exists.
        lf, arc = self._sketch_rail_design()
        out = _payload(so.handler(profiles=["H0", "H1"], rails=["Spine/arc:0"]))
        assert lf.last_input.centerLineOrRails.rails == [arc]
        assert out["rails_count"] == 1

    def test_a_centerline_can_be_a_sketch_curve_ref(self):
        lf, arc = self._sketch_rail_design()
        out = _payload(so.handler(profiles=["H0", "H1"], centerline="Spine/arc:0"))
        assert lf.last_input.centerLineOrRails.centerlines == [arc]
        assert out["has_centerline"] is True

    def test_an_unknown_sketch_in_a_rail_ref_is_named(self):
        self._sketch_rail_design()
        res = so.handler(profiles=["H0", "H1"], rails=["NoSuch/arc:0"])
        assert res["isError"] is True
        assert "NoSuch" in res["message"] and "Spine" in res["message"]

    def test_a_curve_the_sketch_does_not_hold_is_named(self):
        self._sketch_rail_design()
        res = so.handler(profiles=["H0", "H1"], rails=["Spine/arc:7"])
        assert res["isError"] is True
        assert "arc:7" in res["message"] and "sketch_get" in res["message"]

    def test_unknown_operation_rejected(self):
        self._profiles_design()
        res = so.handler(profiles=["H0", "H1"], operation="weld")
        assert res["isError"] is True
        assert "new, join, cut, intersect" in res["message"]

    def test_cut_that_moves_no_volume_is_an_error(self):
        self._cut_design([BRepBody("Bar", is_solid=True, volume=12.0)])
        res = so.handler(profiles=["H0", "H1", "H2"], operation="cut")
        assert res["isError"] is True
        assert "changed nothing" in res["message"] and "'Comp'" in res["message"]
        assert "design_delete_feature" in res["message"]

    def test_cut_that_removed_material_publishes_the_delta(self):
        bar = BRepBody("Bar", is_solid=True, volume=12.0)
        self._cut_design([bar], moves=[(bar, 9.5)])
        out = _payload(so.handler(profiles=["H0", "H1", "H2"], operation="cut"))
        assert out["volume_delta_cm3"] == -2.5      # signed: material LEFT the body

    def test_a_consumed_body_is_not_read_as_a_no_op(self):
        eaten = BRepBody("Eaten", is_solid=True, volume=4.0)
        kept = BRepBody("Kept", is_solid=True, volume=8.0)
        self._cut_design([eaten, kept], moves=[(eaten, None)])
        out = _payload(so.handler(profiles=["H0", "H1", "H2"], operation="cut"))
        assert out["lofted"] is True

    def test_a_new_body_loft_is_never_volume_gated(self):
        self._cut_design([BRepBody("Bar", is_solid=True, volume=12.0)])
        out = _payload(so.handler(profiles=["H0", "H1", "H2"]))
        assert out["lofted"] is True and "volume_delta_cm3" not in out

    def test_a_surface_body_is_not_sampled(self):
        # Only SOLIDS carry the volume a cut moves; sampling an open surface body (whose volume does
        # not read) would make the census unreadable and silently drop the gate.
        surf = BRepBody("Skin", is_solid=False, volume=None)
        bar = BRepBody("Bar", is_solid=True, volume=12.0)
        self._cut_design([surf, bar])
        res = so.handler(profiles=["H0", "H1", "H2"], operation="cut")
        assert res["isError"] is True and "changed nothing" in res["message"]

    def test_loft_with_no_result_body_is_an_error(self):
        # 'lofted: true' beside result_bodies [] claims a body the payload cannot show
        self._profiles_design(result_bodies=[])
        res = so.handler(profiles=["H0", "H1"])
        assert res["isError"] is True
        assert "owns no result body" in res["message"]
        assert "design_delete_feature" in res["message"]

    def test_one_result_body_is_the_boundary_that_passes(self):
        self._profiles_design(result_bodies=[BRepBody("Body1", is_solid=True)])
        out = _payload(so.handler(profiles=["H0", "H1"]))
        assert out["result_bodies"] == ["Body1"]

    def test_a_cut_that_consumed_its_target_is_not_refused_for_an_empty_result(self):
        # a cut/intersect that ate the body outright leaves no result body, and the volume gate
        # above has already proven material moved - refusing there would call a real cut a failure
        eaten = BRepBody("Eaten", is_solid=True, volume=4.0)
        lf = self._cut_design([eaten], moves=[(eaten, None)])
        lf._result_bodies = []
        out = _payload(so.handler(profiles=["H0", "H1", "H2"], operation="cut"))
        assert out["result_bodies"] == []

    def test_a_cut_whose_census_never_read_does_not_buy_the_empty_result_carve_out(self):
        # The census was SAMPLED but no volume read at either end, so the no-op gate above stayed
        # silent and nothing about this cut is proven. Keying the carve-out on "a census exists"
        # rather than on measured movement lets an empty result set through as a success.
        blind = BRepBody("Blind", is_solid=True, volume=None)
        lf = self._cut_design([blind])          # no moves: the volume is None at both ends
        lf._result_bodies = []
        res = so.handler(profiles=["H0", "H1", "H2"], operation="cut")
        assert res["isError"] is True
        assert "owns no result body" in res["message"]

    def test_a_measurable_cut_at_the_no_change_band_keeps_the_carve_out(self):
        # delta exactly AT the band is the smallest movement the volume gate does not refuse, so it
        # is the boundary the carve-out must accept - the >= / > edge of `moved`
        band = so._common.NO_VOLUME_CHANGE_CM3
        bar = BRepBody("Bar", is_solid=True, volume=0.0)
        lf = self._cut_design([bar], moves=[(bar, band)])
        lf._result_bodies = []
        out = _payload(so.handler(profiles=["H0", "H1", "H2"], operation="cut"))
        assert out["result_bodies"] == []
        assert out["volume_delta_cm3"] == round(band, 6)

    def test_a_cut_just_inside_the_no_change_band_is_still_refused_as_a_no_op(self):
        # one notch below the band: the volume gate owns this refusal, and the carve-out must not
        # rescue it
        band = so._common.NO_VOLUME_CHANGE_CM3
        bar = BRepBody("Bar", is_solid=True, volume=0.0)
        lf = self._cut_design([bar], moves=[(bar, band / 2)])
        lf._result_bodies = []
        res = so.handler(profiles=["H0", "H1", "H2"], operation="cut")
        assert res["isError"] is True
        assert "changed nothing" in res["message"]

    def test_unreadable_is_solid_is_null_and_narrated_as_unverified(self):
        # feature.isSolid did not read: safe() made it falsy, so the note claimed "Result is a
        # SURFACE" off a flag nobody read
        lf = _FakeLoftFeatures(result_is_solid=None)      # no isSolid attribute at all
        _install(_FakeFeatures(loft=lf),
                 handle_map={"H0": Profile("0"), "H1": Profile("1")})
        out = _payload(so.handler(profiles=["H0", "H1"]))
        assert out["is_solid"] is None
        assert out["unverified"] == ["is_solid"]
        assert "UNVERIFIED" in out["note"]
        assert "Result is a SURFACE" not in out["note"]

    def test_loft_built_on_the_profiles_owning_component(self):
        # The profiles are OWNED by a sub-component while a DIFFERENT component is active. Handing
        # another component's native profile to the active component's features raises bSet live,
        # so the loft feature must be created on the OWNER's features. The active comp carries its own
        # loftFeatures; the owner carries a SEPARATE one - the test proves the owner's got the call.
        owner_lf = _FakeLoftFeatures()
        owner = MakeComp(name="Owner")
        owner.features = _FakeFeatures(loft=owner_lf)
        # each profile's parentSketch.parentComponent points at the owner (the live Profile chain)
        p0 = Profile("0", parent_sketch=make_sketch(name="Sk0", parent_component=owner))
        p1 = Profile("1", parent_sketch=make_sketch(name="Sk1", parent_component=owner))
        active_lf = _FakeLoftFeatures()
        _install(_FakeFeatures(loft=active_lf), handle_map={"H0": p0, "H1": p1})
        out = _payload(so.handler(profiles=["H0", "H1"]))
        assert out["lofted"] is True
        assert owner_lf.last_input is not None     # the OWNER built the loft
        assert active_lf.last_input is None        # NOT the active component (the bSet trap)

    def test_the_component_scope_is_declared_beside_the_kinds_scope(self):
        # SKETCH-6: a {sketch, profile_index} element addresses a sketch by name, and the refusal's
        # way forward may only name an input this strict schema takes - so the kind's scope_input
        # and the declared property ship together.
        sd = load_tool("_sketch_detail")
        assert so._LOFT_PROFILES.scope_input == "component"
        assert so.tool.input_schema["properties"]["component"] == sd.COMPONENT_SCOPE[1]

    def test_the_component_scope_reaches_the_profile_resolve(self, monkeypatch):
        # a declared property the resolve never sees is a remedy the tool then ignores.
        seen = {}

        def _resolve(raw, component=""):
            seen["component"] = component
            return None, "refused"

        _install(_FakeFeatures(loft=_FakeLoftFeatures()), handle_map={})
        monkeypatch.setattr(so._LOFT_PROFILES, "resolve", _resolve)
        so.handler(profiles=["H0", "H1"], component="Frame")
        assert seen == {"component": "Frame"}
