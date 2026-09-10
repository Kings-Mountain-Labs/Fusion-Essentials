"""Unit tests for surface_extrude.py - the open profile, the sheet result and its depth."""

import types

import adsk.core
import adsk.fusion
import pytest

from conftest import (BRepBody, BRepEdge, FakeFeature as _SharedFeature,
                      FakeFeatures as _SharedFeatures, MakeComp, MakeDesign, install, load_tool,
                      make_sketch, make_sketch_curve, payload)

sc = load_tool("surface_extrude")
surface_revolve = load_tool("surface_revolve")


def _body(name="Surf1", is_solid=False, solid_readable=True):
    """One result body - an open sheet unless a test asks for a solid or an unreadable flag."""
    return BRepBody(name, is_solid=is_solid, solid_readable=solid_readable)


def _edge():
    """One B-Rep edge whose owning body does not read, so the host falls back to the component."""
    edge = BRepEdge(curve=None)
    edge.body = None
    return edge


def _sketch(name="Sketch1", curve_count=2):
    """A sketch holding `curve_count` curves for the open chain."""
    return make_sketch(name=name,
                       lines=[make_sketch_curve(f"{name}:{i}") for i in range(curve_count)])


def _comp(features, sketches=(), name="Root"):
    """A component carrying the feature collections, the two profile factories and the origin
    axes a surface build reads."""
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


class FakeExtrudeInput:
    def __init__(self, profile, op):
        self.profile = profile
        self.operation = op
        self.isSolid = None
        self.distance_extent = None
    def setDistanceExtent(self, sym, dist):
        self.distance_extent = (sym, dist)
        return True


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


class FakeExtrudeFeatures:
    def __init__(self, result_bodies=None, landed_cm=None, extent_readable=True):
        # landed_cm: the depth the created feature's extent parameter reads back, when it differs
        # from the depth handed to setDistanceExtent (live, the two agree). extent_readable=False
        # models a feature whose extent parameter cannot be read at all.
        self.last_input = None
        self.added = False
        self._result = result_bodies
        self._landed_cm = landed_cm
        self._extent_readable = extent_readable
    def createInput(self, profile, op):
        self.last_input = FakeExtrudeInput(profile, op)
        return self.last_input
    def add(self, inp):
        self.added = True
        extent = None
        if self._extent_readable:
            extent = (self._landed_cm if self._landed_cm is not None
                      else inp.distance_extent[1][1])
        return FakeFeature(bodies=self._result, extent_cm=extent)


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
        monkeypatch.setattr(adsk.fusion, "BRepEdge", BRepEdge)
        return design
    return _wire


class TestSurfaceExtrude:

    def test_sets_isSolid_false_and_reports_it(self, wire):
        ef = FakeExtrudeFeatures(result_bodies=[_body("Surf1")])
        wire(_comp(FakeFeatures(ef=ef), sketches=[_sketch("S")]))
        out = payload(sc.handler(sketch_name="S", distance=5, units="mm"))
        assert out["created"] is True
        assert out["is_solid"] is False
        assert ef.last_input.isSolid is False        # the surface switch was actually set
        sym, dist = ef.last_input.distance_extent
        assert dist == ("real", 0.5)                 # 5 mm -> 0.5 cm
        assert ef.last_input.operation == adsk.fusion.FeatureOperations.NewBodyFeatureOperation

    def test_reports_result_is_solid_read_back(self, wire):
        # is_solid is READ BACK from the result body, not assumed. With createOpenProfile + isSolid=False
        # a closed boundary makes an open sheet/tube (is_solid False, verified live), so the tool
        # SUCCEEDS - it doesn't reject; it reports what the body actually is.
        ef = FakeExtrudeFeatures(result_bodies=[_body("Body1")])
        wire(_comp(FakeFeatures(ef=ef), sketches=[_sketch("S")]))
        out = payload(sc.handler(sketch_name="S", distance=5))
        assert out["created"] is True and out["is_solid"] is False

    def test_solid_result_contradicts_the_sheet_note(self, wire):
        # a result that reads back SOLID must not carry the open-surface note - the note reports
        # the observed body, not the intent
        ef = FakeExtrudeFeatures(result_bodies=[_body("Body1", is_solid=True)])
        wire(_comp(FakeFeatures(ef=ef), sketches=[_sketch("S")]))
        out = payload(sc.handler(sketch_name="S", distance=5))
        assert out["is_solid"] is True
        assert "SOLID" in out["note"]
        assert "Open surface body created" not in out["note"]

    def test_zero_distance_guard(self, wire):
        wire(_comp(FakeFeatures(ef=FakeExtrudeFeatures()), sketches=[_sketch("S")]))
        res = sc.handler(sketch_name="S", distance=0)
        assert res["isError"] is True and "non-zero" in res["message"]

    def test_unknown_operation_rejected(self, wire):
        wire(_comp(FakeFeatures(ef=FakeExtrudeFeatures()), sketches=[_sketch("S")]))
        res = sc.handler(sketch_name="S", distance=5, operation="cut")
        assert res["isError"] is True and "new, join" in res["message"]

    def test_from_edge_curves_uses_edge_profile(self, wire):
        e1, e2 = _edge(), _edge()
        ef = FakeExtrudeFeatures(result_bodies=[_body("Surf1")])
        wire(_comp(FakeFeatures(ef=ef)), handle_map={"E1": e1, "E2": e2})
        out = payload(sc.handler(curves=["E1", "E2"], distance=3))
        assert out["is_solid"] is False
        assert out["open_edge_count"] == 2
        # B-Rep edges -> createBRepEdgeProfile path
        assert ef.last_input.profile == ("edge_profile", None)

    def test_no_sketch_no_curves_errors(self, wire):
        wire(_comp(FakeFeatures(ef=FakeExtrudeFeatures()), sketches=[]))
        res = sc.handler(distance=5)
        assert res["isError"] is True
        assert "No sketch or 'curves' to extrude" in res["message"]

    def test_unknown_units_rejected(self, wire):
        wire(_comp(FakeFeatures(ef=FakeExtrudeFeatures()), sketches=[_sketch("S")]))
        res = sc.handler(sketch_name="S", distance=5, units="furlong")
        assert res["isError"] is True
        assert "furlong" in res["message"] and "mm, cm, or in" in res["message"]

    def test_join_op_and_symmetric_passed_through(self, wire):
        # operation=join maps to the JoinFeatureOperation enum; symmetric flows to setDistanceExtent
        ef = FakeExtrudeFeatures(result_bodies=[_body("Surf1")])
        wire(_comp(FakeFeatures(ef=ef), sketches=[_sketch("S")]))
        out = payload(sc.handler(sketch_name="S", distance=5, operation="join",
                                 symmetric=True))
        assert out["operation"] == "join"
        assert out["symmetric"] is True
        assert ef.last_input.operation == adsk.fusion.FeatureOperations.JoinFeatureOperation
        sym, _dist = ef.last_input.distance_extent
        assert sym is True

    def test_sketch_with_no_curves_errors(self, wire):
        # _open_sketch_profile: a sketch present but with zero curves -> honest error, no add()
        wire(_comp(FakeFeatures(ef=FakeExtrudeFeatures()),
                   sketches=[_sketch("Empty", curve_count=0)]))
        res = sc.handler(sketch_name="Empty", distance=5)
        assert res["isError"] is True and "no curves" in res["message"].lower()

    def test_depth_that_reads_back_wrong_is_an_error(self, wire):
        # the extrude landed a depth Fusion took, not the one asked for -> error, never an ok
        # payload echoing the request as if it were the sheet's depth
        ef = FakeExtrudeFeatures(result_bodies=[_body("Surf1")], landed_cm=0.37)
        wire(_comp(FakeFeatures(ef=ef), sketches=[_sketch("S")]))
        res = sc.handler(sketch_name="S", distance=5, units="mm")
        assert res["isError"] is True
        assert "reads back 3.7" in res["message"] and "requested 5.0" in res["message"]
        assert "design_delete_feature" in res["message"]

    def test_depth_read_off_the_feature_is_published(self, wire):
        ef = FakeExtrudeFeatures(result_bodies=[_body("Surf1")])
        wire(_comp(FakeFeatures(ef=ef), sketches=[_sketch("S")]))
        out = payload(sc.handler(sketch_name="S", distance=5, units="mm"))
        assert out["distance"] == 5.0            # the feature's own extent, in the caller's units
        assert "unverified" not in out

    def test_a_flipped_depth_is_an_error_not_a_magnitude_match(self, wire):
        # a -15 mm request that landed +15 mm points the sheet the other way; only a SIGNED
        # comparison catches it (the extent parameter keeps the requested sign, measured)
        ef = FakeExtrudeFeatures(result_bodies=[_body("Surf1")], landed_cm=1.5)
        wire(_comp(FakeFeatures(ef=ef), sketches=[_sketch("S")]))
        res = sc.handler(sketch_name="S", distance=-15, units="mm")
        assert res["isError"] is True
        assert "reads back 15.0" in res["message"] and "requested -15.0" in res["message"]

    def test_unreadable_depth_is_flagged_unverified_not_silently_echoed(self, wire):
        ef = FakeExtrudeFeatures(result_bodies=[_body("Surf1")], extent_readable=False)
        wire(_comp(FakeFeatures(ef=ef), sketches=[_sketch("S")]))
        out = payload(sc.handler(sketch_name="S", distance=5, units="mm"))
        assert out["unverified"] == ["distance"]
        assert "Not read back off the feature: distance." in out["note"]
        assert out["distance"] == 5.0            # the request, published only because it is flagged

    def test_surface_extrude_built_on_the_sketchs_owning_component(self, wire):
        # The named sketch lives in a SUB-component (its parentComponent) while a DIFFERENT component
        # is active. Building the open profile + feature on the active component while the sketch is
        # owned elsewhere raises bSet, so both must be built on the sketch's OWNER. The active
        # comp and the owner carry SEPARATE extrudeFeatures; the test proves the owner's got the call.
        owner_ef = FakeExtrudeFeatures(result_bodies=[_body("Surf1")])
        owned_sketch = _sketch("OwnedSketch")
        owner = _comp(FakeFeatures(ef=owner_ef), sketches=[owned_sketch], name="Owner")
        owned_sketch.parentComponent = owner
        active_ef = FakeExtrudeFeatures(result_bodies=[_body("X")])
        active = _comp(FakeFeatures(ef=active_ef), sketches=[])
        wire(active, sub_components=[owner])
        out = payload(sc.handler(sketch_name="OwnedSketch", distance=5))
        assert out["created"] is True
        assert owner_ef.last_input is not None      # the OWNER built the surface extrude
        assert active_ef.last_input is None         # NOT the active component (the bSet trap)


class TestSketchNameResolution:

    """The name-or-most-recent sketch branch, which surface_extrude and surface_revolve each carry
    (both taken only when 'curves' is empty)."""

    @staticmethod
    def _call(wire, which, sketches, **kwargs):
        if which == "extrude":
            ef = FakeExtrudeFeatures(result_bodies=[_body("Surf1")])
            wire(_comp(FakeFeatures(ef=ef), sketches=sketches))
            return sc.handler(distance=5, **kwargs)
        rf = FakeRevolveFeatures(result_bodies=[_body("Surf1")])
        wire(_comp(FakeFeatures(rf=rf), sketches=sketches))
        return surface_revolve.handler(angle_deg=180, **kwargs)

    @pytest.mark.parametrize("which", ["extrude", "revolve"])
    def test_a_padded_sketch_name_is_reported_stripped(self, which, wire):
        # the walk searches the STRIPPED name, so the miss must name that one - quoting the padded
        # input sends the caller looking for a sketch whose name carries the spaces it typed.
        res = self._call(wire, which, [_sketch("S")], sketch_name="  Ghost  ")
        assert res["isError"] is True
        assert "No sketch named 'Ghost'" in res["message"]
        assert "'  Ghost  '" not in res["message"]

    @pytest.mark.parametrize("which,verb", [("extrude", "extrude"), ("revolve", "revolve")])
    def test_a_blank_sketch_name_with_no_sketch_never_quotes_none(self, which, verb, wire):
        # a blank name leaves the requested name None, so the named-miss wording would print
        # "No sketch named 'None'" - a sketch nobody asked for. The blank branch words its own.
        res = self._call(wire, which, [], sketch_name="")
        assert res["isError"] is True
        assert res["message"] == (f"No sketch or 'curves' to {verb}. Draw an OPEN chain first, or "
                                  "pass curves.")
        assert "'None'" not in res["message"]

    @pytest.mark.parametrize("module_name", ["surface_extrude", "surface_revolve"])
    def test_the_component_scope_is_declared_on_the_wire(self, module_name):
        # both schemas are strict, so a handler parameter no property declares is refused before it
        # reaches the handler - the scope would be unreachable and its refusal would name it anyway.
        sd = load_tool("_sketch_detail")
        schema = load_tool(module_name).tool.input_schema["properties"]
        assert schema["component"] == sd.COMPONENT_SCOPE[1]

    @pytest.mark.parametrize("which", ["extrude", "revolve"])
    def test_the_component_scope_reaches_the_sketch_resolve(self, which, wire, monkeypatch):
        # a sketch name two components carry is Fusion's default state, so each of these two
        # handlers needs its own 'component' to say which one it means - and must hand it to the
        # shared resolver, whose refusals name that same input back.
        seen = {}

        def _scoped(design, name, component, input_name="component"):
            seen.update(component=component, input_name=input_name)
            return None, name, "refused"

        monkeypatch.setattr(sc._sketch_detail, "scoped_or_recent_sketch", _scoped)
        res = self._call(wire, which, [_sketch("S")], sketch_name="S", component="Frame")
        assert res["isError"] is True and res["message"] == "refused"
        assert seen == {"component": "Frame", "input_name": "component"}

    @pytest.mark.parametrize("which", ["extrude", "revolve"])
    def test_a_whitespace_only_sketch_name_uses_the_most_recent_sketch(self, which, wire):
        # ' ' strips to blank, which is the most-recent-sketch request - not a search for a sketch
        # named with a space.
        out = payload(self._call(wire, which, [_sketch("First"), _sketch("Last")],
                                 sketch_name=" "))
        assert out["source"] == "Last"


class TestEmptyResultSetIsAnError:

    """'created: true' beside result_bodies [] claims a sheet the payload cannot show - and leaves
    is_solid with nothing to read off, so the sheet/solid note is narrated from nothing."""

    def test_extrude_with_no_result_body_is_an_error(self, wire):
        ef = FakeExtrudeFeatures(result_bodies=[])
        wire(_comp(FakeFeatures(ef=ef), sketches=[_sketch("S")]))
        res = sc.handler(sketch_name="S", distance=5)
        assert res["isError"] is True
        assert "owns no result body" in res["message"]
        assert "design_delete_feature" in res["message"]

    def test_one_extrude_body_is_the_boundary_that_passes(self, wire):
        ef = FakeExtrudeFeatures(result_bodies=[_body("Surf1")])
        wire(_comp(FakeFeatures(ef=ef), sketches=[_sketch("S")]))
        out = payload(sc.handler(sketch_name="S", distance=5))
        assert out["result_bodies"] == ["Surf1"]

    def test_extrude_unreadable_is_solid_is_null_and_unverified(self, wire):
        ef = FakeExtrudeFeatures(result_bodies=[_body("Surf1", solid_readable=False)])
        wire(_comp(FakeFeatures(ef=ef), sketches=[_sketch("S")]))
        out = payload(sc.handler(sketch_name="S", distance=5))
        assert out["is_solid"] is None
        assert out["unverified"] == ["is_solid"]
        assert "UNVERIFIED" in out["note"]
        assert "isSolid=false" not in out["note"]
