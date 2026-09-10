"""Unit tests for surface_revolve.py - the axis, the angle extent and the sheet result."""

import types

import adsk.core
import adsk.fusion
import pytest

from conftest import (BRepBody, FakeFeature as _SharedFeature, FakeFeatures as _SharedFeatures,
                      MakeComp, MakeDesign, install, load_tool, make_sketch, make_sketch_curve,
                      payload)

sc = load_tool("surface_revolve")


def _body(name="Surf1", is_solid=False, solid_readable=True):
    """One result body - an open sheet unless a test asks for a solid or an unreadable flag."""
    return BRepBody(name, is_solid=is_solid, solid_readable=solid_readable)


def _sketch(name="Sketch1", curve_count=2):
    """A sketch holding `curve_count` curves for the open chain."""
    return make_sketch(name=name,
                       lines=[make_sketch_curve(f"{name}:{i}") for i in range(curve_count)])


def _comp(features, sketches=(), name="Root"):
    """A component carrying the revolve features, the open-profile factory and the origin axes the
    revolve takes its axis entity from."""
    comp = MakeComp(name=name, sketches=list(sketches),
                    construction_axes=(("axis", "x"), ("axis", "y"), ("axis", "z")))
    comp.features = features
    comp.createOpenProfile = lambda curves, chained: ("open_profile", None)
    comp.createBRepEdgeProfile = lambda edges: ("edge_profile", None)
    return comp


class FakeFeature(_SharedFeature):
    """The shared feature plus the extent a depth read-back reads."""
    def __init__(self, name="Surface1", bodies=None, extent_cm=None):
        super().__init__(name=name, bodies=bodies if bodies is not None else [_body()])
        if extent_cm is not None:
            # ExtrudeFeature.extentOne is a DistanceExtentDefinition (a SymmetricExtentDefinition
            # for a symmetric extrude) whose .distance is a ModelParameter reading CM, signed as
            # requested. extent_cm=None gives a feature whose extent cannot be read at all.
            self.extentOne = types.SimpleNamespace(
                distance=types.SimpleNamespace(value=extent_cm))


class FakeRevolveInput:
    def __init__(self, profile, axis, op):
        self.profile = profile
        self.axis = axis
        self.operation = op
        self.isSolid = None
        self.angle_extent = None
    def setAngleExtent(self, sym, ang):
        self.angle_extent = (sym, ang)
        return True


class FakeRevolveFeatures:
    def __init__(self, result_bodies=None):
        self.last_input = None
        self._result = result_bodies
    def createInput(self, profile, axis, op):
        self.last_input = FakeRevolveInput(profile, axis, op)
        return self.last_input
    def add(self, inp):
        return FakeFeature(bodies=self._result)


class FakeFeatures(_SharedFeatures):
    """comp.features plus the three surface-build collections this tool reaches through."""
    def __init__(self, ef=None, rf=None, pf=None):
        super().__init__()
        self.extrudeFeatures = ef
        self.revolveFeatures = rf
        self.patchFeatures = pf


@pytest.fixture
def wire(monkeypatch):
    """Factory: install a design holding `comp` (and any sub-components) into the tool module,
    with the adsk members a surface build reads."""
    def _wire(comp, handle_map=None, sub_components=()):
        design = MakeDesign(comp=comp, tokens=handle_map or {},
                            all_components=[comp] + list(sub_components))
        install(sc, design)
        monkeypatch.setattr(adsk.core.ValueInput, "createByReal",
                            staticmethod(lambda v: ("real", v)))
        return design
    return _wire


class TestEmptyResultSetIsAnError:

    """'created: true' beside result_bodies [] claims a sheet the payload cannot show - and leaves
    is_solid with nothing to read off, so the sheet/solid note is narrated from nothing."""

    def test_revolve_with_no_result_body_is_an_error(self, wire):
        rf = FakeRevolveFeatures(result_bodies=[])
        wire(_comp(FakeFeatures(rf=rf), sketches=[_sketch("S")]))
        res = sc.handler(sketch_name="S", angle_deg=180)
        assert res["isError"] is True
        assert "owns no result body" in res["message"]

    def test_one_revolve_body_is_the_boundary_that_passes(self, wire):
        rf = FakeRevolveFeatures(result_bodies=[_body("Surf1")])
        wire(_comp(FakeFeatures(rf=rf), sketches=[_sketch("S")]))
        out = payload(sc.handler(sketch_name="S", angle_deg=180))
        assert out["result_bodies"] == ["Surf1"]

    def test_revolve_unreadable_is_solid_is_null_and_unverified(self, wire):
        rf = FakeRevolveFeatures(result_bodies=[_body("Surf1", solid_readable=False)])
        wire(_comp(FakeFeatures(rf=rf), sketches=[_sketch("S")]))
        out = payload(sc.handler(sketch_name="S", angle_deg=180))
        assert out["is_solid"] is None
        assert out["unverified"] == ["is_solid"]
        assert "isSolid=false" not in out["note"]


class TestSurfaceRevolve:

    def test_sets_isSolid_false(self, wire):
        rf = FakeRevolveFeatures(result_bodies=[_body("Surf1")])
        wire(_comp(FakeFeatures(rf=rf), sketches=[_sketch("S")]))
        out = payload(sc.handler(sketch_name="S", axis="y", angle_deg=180))
        assert out["is_solid"] is False
        assert out["axis"] == "y-axis"
        # the LABEL agreeing is not the entity agreeing: pin which origin axis reached createInput
        assert rf.last_input.axis == ("axis", "y")
        assert rf.last_input.isSolid is False

    def test_reports_result_is_solid_read_back(self, wire):
        # is_solid is read back from the body, not assumed; a sheet revolve makes an open shell
        # (is_solid False, verified live) and SUCCEEDS rather than rejecting.
        rf = FakeRevolveFeatures(result_bodies=[_body("Body1")])
        wire(_comp(FakeFeatures(rf=rf), sketches=[_sketch("S")]))
        out = payload(sc.handler(sketch_name="S", angle_deg=360))
        assert out["created"] is True and out["is_solid"] is False

    def test_zero_angle_guard(self, wire):
        wire(_comp(FakeFeatures(rf=FakeRevolveFeatures()), sketches=[_sketch("S")]))
        res = sc.handler(sketch_name="S", angle_deg=0)
        assert res["isError"] is True and "non-zero" in res["message"]

    def test_non_numeric_angle_rejected(self, wire):
        wire(_comp(FakeFeatures(rf=FakeRevolveFeatures()), sketches=[_sketch("S")]))
        res = sc.handler(sketch_name="S", angle_deg="lots")
        assert res["isError"] is True and "number" in res["message"]

    def test_unknown_axis_rejected(self, wire):
        wire(_comp(FakeFeatures(rf=FakeRevolveFeatures()), sketches=[_sketch("S")]))
        res = sc.handler(sketch_name="S", angle_deg=90, axis="w")
        assert res["isError"] is True and "x, y, or z" in res["message"]

    def test_surface_revolve_built_on_the_sketchs_owning_component(self, wire):
        # Named sketch owned by a SUB-component while a different component is active. Both the profile
        # and the origin axis must come from the OWNER (an axis from the wrong component mixes contexts,
        # and the profile-consuming feature raises bSet on the active component). Proven by which
        # revolveFeatures object got the call.
        owner_rf = FakeRevolveFeatures(result_bodies=[_body("Surf1")])
        owned_sketch = _sketch("OwnedSketch")
        owner = _comp(FakeFeatures(rf=owner_rf), sketches=[owned_sketch], name="Owner")
        owned_sketch.parentComponent = owner
        active_rf = FakeRevolveFeatures(result_bodies=[_body("X")])
        active = _comp(FakeFeatures(rf=active_rf), sketches=[])
        wire(active, sub_components=[owner])
        out = payload(sc.handler(sketch_name="OwnedSketch", angle_deg=180))
        assert out["created"] is True
        assert owner_rf.last_input is not None      # the OWNER built the surface revolve
        assert active_rf.last_input is None         # NOT the active component (the bSet trap)
