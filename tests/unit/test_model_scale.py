"""Unit tests for ``model_scale.py`` - resize solid bodies about an anchor point.

Pinned: the uniform/per-axis mode split (all three axis factors or none, never both forms at once),
the greater-than-zero factor guard, what reaches ScaleFeatures.createInput/setToNonUniform (the
entities, the anchor point, the factor ValueInputs), the anchor default of the active component's
origin, and the honesty gates - a scale whose measured volume ratio is not the f^3 / x*y*z the
factors imply, one that moved nothing, and one whose geometry cannot be read back all surface as
errors rather than a false ok.
"""

import types

import adsk.core
import adsk.fusion

from conftest import (load_tool, make_design, install, MakeComp, BRepBody, FakePoint,
                      FakeBoundingBox3D, FakeUnitsManager, go_stale, payload, error_message,
                      assert_no_active_design)

ms = load_tool("model_scale")

# The active component's origin construction point - the anchor the handler falls back to.
_ORIGIN = object()


# ── fakes: the ScaleFeatures createInput/add graph (no conftest fake models a feature collection) ──

class _VI:
    """A ValueInput stand-in carrying what the handler set: the raw number for createByReal, or the
    expression text for createByString."""
    def __init__(self, value=None, expression=None):
        self.value = value
        self.expression = expression


class FakeScaleFeature:
    """The created ScaleFeature. `params` maps a factor parameter name (scaleFactor / xScale / ...)
    to the number Fusion resolved it to - what the handler reads back for an expression factor."""
    def __init__(self, name="Scale1", health=0, params=None):
        self.name = name
        self.healthState = health
        self.errorOrWarningMessage = "the scale collapses the body"
        for attr, value in (params or {}).items():
            setattr(self, attr, types.SimpleNamespace(value=value))


class FakeScaleInput:
    """The ScaleFeatureInput: records the createInput arguments and the per-axis switch."""
    def __init__(self, entities, point, scale_factor, non_uniform_result):
        self.inputEntities = entities
        self.point = point
        self.scaleFactor = scale_factor
        self.axis_factors = None
        self._non_uniform_result = non_uniform_result

    def setToNonUniform(self, x_scale, y_scale, z_scale):
        self.axis_factors = (x_scale, y_scale, z_scale)
        return self._non_uniform_result


class FakeScaleFeatures:
    """component.features.scaleFeatures: add() applies the canned effect a real scale would have
    (volume by `volume_ratio` - one number, or a per-body-name map for a partial result - and each
    bounding-box extent by `extent_ratios`) to the bodies it was built for, then returns `feature`.
    `returns_nothing` models the return with NO feature object in it - the measured direct-mode
    shape, where the canned effect still lands."""
    def __init__(self, bodies, volume_ratio=8.0, extent_ratios=(2.0, 2.0, 2.0),
                 feature=None, returns_nothing=False, non_uniform_result=True):
        self.bodies = list(bodies)
        self.volume_ratio = volume_ratio
        self.extent_ratios = extent_ratios
        self.feature = feature if feature is not None else FakeScaleFeature()
        self.returns_nothing = returns_nothing
        self.non_uniform_result = non_uniform_result
        self.last_input = None

    def createInput(self, entities, point, scale_factor):
        self.last_input = FakeScaleInput(entities, point, scale_factor, self.non_uniform_result)
        return self.last_input

    def _ratio_for(self, body):
        if isinstance(self.volume_ratio, dict):
            return self.volume_ratio[body.name]
        return self.volume_ratio

    def add(self, scale_input):
        for body in self.bodies:
            body.volume *= self._ratio_for(body)
            bb = body.boundingBox
            if bb is None:
                continue
            for axis, ratio in zip(("x", "y", "z"), self.extent_ratios):
                low = getattr(bb.minPoint, axis)
                high = getattr(bb.maxPoint, axis)
                setattr(bb.maxPoint, axis, low + (high - low) * ratio)
        # The resized bodies' proxies stop answering their identity reads; volume/boundingBox stay
        # readable, since those ARE the effect check.
        go_stale(*self.bodies)
        return None if self.returns_nothing else self.feature


def _body(name="Block", volume=8.0, span=2.0):
    """A solid body with a readable volume and bounding box - the evidence the handler measures."""
    return BRepBody(name=name, volume=volume, is_solid=True, entity_token=name,
                    bbox=FakeBoundingBox3D(FakePoint(0, 0, 0), FakePoint(span, span, span)))


def _wire(monkeypatch, bodies, feats=None, origin=_ORIGIN, tokens=None, units=None,
          design_type=None):
    """Install a design whose active component carries `feats` (features.scaleFeatures) and an origin
    construction point, plus the units engine an expression factor is gated against, with ValueInput
    modelled and BRepBody wired for the solid-body kind.

    `design_type` sets the modelling mode current_design_type reads (1 parametric, 0 direct); left
    unset the design reports neither, which is the 'unknown' mode."""
    comp = MakeComp(name="Comp", bodies=list(bodies))
    feats = feats if feats is not None else FakeScaleFeatures(bodies)
    comp.features = types.SimpleNamespace(scaleFeatures=feats)
    if origin is not None:
        comp.originConstructionPoint = origin
    design = make_design(comp=comp, tokens=tokens)
    if design_type is not None:
        design.designType = design_type
    design.fusionUnitsManager = units if units is not None else FakeUnitsManager(
        valid=("ShrinkAllowance", "XFactor", "YFactor", "ZFactor", "ShrinkAllowance * 2", "5 mm",
               "TiltAngle"),
        value=2.0, dimensioned=("5 mm",), angle_dimensioned=("TiltAngle",),
        # bare parameter NAMES are strictly unitless: readable with no units, unreadable under any
        strict_unitless=("ShrinkAllowance", "XFactor", "YFactor", "ZFactor"))
    install(ms, design)
    monkeypatch.setattr(adsk.fusion, "BRepBody", BRepBody)
    monkeypatch.setattr(adsk.core.ValueInput, "createByReal", staticmethod(lambda v: _VI(value=v)))
    monkeypatch.setattr(adsk.core.ValueInput, "createByString",
                        staticmethod(lambda s: _VI(expression=s)))
    return feats


# ── uniform ──────────────────────────────────────────────────────────────────

class TestUniform:
    def test_happy_path_verifies_the_cubed_volume_ratio(self, monkeypatch):
        body = _body(volume=8.0)
        feats = _wire(monkeypatch, [body], FakeScaleFeatures([body], volume_ratio=8.0))
        out = payload(ms.handler(bodies=["Block"], factor=2))
        assert out["scaled"] is True
        assert out["feature"] == "Scale1"
        assert out["uniform"] is True
        assert out["bodies"] == ["Block"]
        assert out["factor"] == 2
        assert out["volume_ratio"] == 8.0
        assert out["expected_volume_ratio"] == 8.0
        assert out["scale_check"] == "volume_ratio"
        assert feats.last_input.scaleFactor.value == 2.0
        assert feats.last_input.inputEntities.count == 1

    def test_shrinking_factor_is_verified_against_its_own_cube(self, monkeypatch):
        body = _body(volume=8.0)
        _wire(monkeypatch, [body], FakeScaleFeatures([body], volume_ratio=0.125,
                                                     extent_ratios=(0.5, 0.5, 0.5)))
        out = payload(ms.handler(bodies=["Block"], factor=0.5))
        assert out["volume_ratio"] == 0.125
        assert out["expected_volume_ratio"] == 0.125

    def test_two_bodies_are_both_measured(self, monkeypatch):
        a, b = _body(name="A", volume=8.0), _body(name="B", volume=16.0)
        feats = _wire(monkeypatch, [a, b], FakeScaleFeatures([a, b], volume_ratio=8.0))
        out = payload(ms.handler(bodies=["A", "B"], factor=2))
        assert out["bodies"] == ["A", "B"]
        assert out["volume_ratio"] == 8.0            # (64+128)/(8+16)
        assert feats.last_input.inputEntities.count == 2

    def test_expression_factor_is_applied_as_its_resolved_number(self, monkeypatch):
        # The expression never reaches the feature: a bare parameter reference raises at add(), and
        # an accepted one is baked to a literal anyway, so the resolved number goes in by value.
        body = _body(volume=8.0)
        feature = FakeScaleFeature(params={"scaleFactor": 2.0})
        feats = _wire(monkeypatch, [body],
                      FakeScaleFeatures([body], volume_ratio=8.0, feature=feature))
        out = payload(ms.handler(bodies=["Block"], factor="ShrinkAllowance"))
        assert feats.last_input.scaleFactor.value == 2.0
        assert feats.last_input.scaleFactor.expression is None
        assert out["factor"] == "ShrinkAllowance"     # echoed as written
        assert out["resolved_factor"] == 2.0          # and as the number applied
        assert out["expected_volume_ratio"] == 8.0
        assert out["scale_check"] == "volume_ratio"

    def test_expression_factor_without_a_readable_parameter_uses_the_resolved_number(
            self, monkeypatch):
        body = _body(volume=8.0)
        feature = FakeScaleFeature()                 # no scaleFactor parameter to read back
        _wire(monkeypatch, [body], FakeScaleFeatures([body], volume_ratio=8.0, feature=feature))
        out = payload(ms.handler(bodies=["Block"], factor="ShrinkAllowance"))
        assert out["expected_volume_ratio"] == 8.0   # 2.0 ^ 3, from the gate's resolved number
        assert out["scale_check"] == "volume_ratio"

    def test_one_body_that_did_not_scale_is_named(self, monkeypatch):
        # A scales 8 -> 64, B is left alone: an aggregate ratio would hide B, the per-body loop
        # must name it.
        a, b = _body(name="A", volume=8.0), _body(name="B", volume=16.0)
        _wire(monkeypatch, [a, b], FakeScaleFeatures([a], volume_ratio=8.0))
        res = ms.handler(bodies=["A", "B"], factor=2)
        assert res["isError"] is True
        assert "'B'" in res["message"] and "unchanged" in res["message"]
        assert "'A'" not in res["message"]
        # the message must own the split rather than claim nothing was resized
        assert "PARTIAL" in res["message"] and "design_delete_feature" in res["message"]


# ── per-axis ─────────────────────────────────────────────────────────────────

class TestPerAxis:
    def test_happy_path_verifies_the_xyz_product(self, monkeypatch):
        body = _body(volume=8.0)
        feats = _wire(monkeypatch, [body], FakeScaleFeatures([body], volume_ratio=6.0,
                                                             extent_ratios=(3.0, 2.0, 1.0)))
        out = payload(ms.handler(bodies=["Block"], x_factor=3, y_factor=2, z_factor=1))
        assert out["uniform"] is False
        assert out["factors"] == {"x_factor": 3, "y_factor": 2, "z_factor": 1}
        assert out["expected_volume_ratio"] == 6.0   # 3 * 2 * 1
        assert out["volume_ratio"] == 6.0
        # createInput carries the UNIFORM seed; the per-axis factors arrive via setToNonUniform
        assert feats.last_input.scaleFactor.value == 1.0
        assert [vi.value for vi in feats.last_input.axis_factors] == [3.0, 2.0, 1.0]

    def test_expression_axes_are_read_back_off_their_own_parameters(self, monkeypatch):
        # xScale/yScale/zScale carry the applied per-axis factors, so 3 x 2 x 1 gives the exact
        # x6 expectation; reading the wrong parameter name would drop to geometry_changed.
        body = _body(volume=8.0)
        feature = FakeScaleFeature(params={"xScale": 3.0, "yScale": 2.0, "zScale": 1.0})
        feats = _wire(monkeypatch, [body], FakeScaleFeatures([body], volume_ratio=6.0,
                                                             extent_ratios=(3.0, 2.0, 1.0),
                                                             feature=feature))
        out = payload(ms.handler(bodies=["Block"], x_factor="XFactor", y_factor="YFactor",
                                 z_factor="ZFactor"))
        assert out["expected_volume_ratio"] == 6.0
        assert out["scale_check"] == "volume_ratio"
        assert out["volume_ratio"] == 6.0
        assert out["factors"] == {"x_factor": "XFactor", "y_factor": "YFactor",
                                  "z_factor": "ZFactor"}
        # each expression resolved to 2.0 and went in by value; the x6 expectation came from the
        # feature's own parameters, not from those inputs
        assert out["resolved_factors"] == {"x_factor": 2.0, "y_factor": 2.0, "z_factor": 2.0}
        assert [vi.value for vi in feats.last_input.axis_factors] == [2.0, 2.0, 2.0]

    def test_volume_preserving_axes_are_verified_by_the_bounding_box(self, monkeypatch):
        # 2 x 0.5 x 1 leaves the volume alone, so the ratio cannot discriminate - the extents must.
        body = _body(volume=8.0)
        _wire(monkeypatch, [body], FakeScaleFeatures([body], volume_ratio=1.0,
                                                     extent_ratios=(2.0, 0.5, 1.0)))
        out = payload(ms.handler(bodies=["Block"], x_factor=2, y_factor=0.5, z_factor=1))
        assert out["scale_check"] == "geometry_changed"
        assert out["volume_ratio"] == 1.0

    def test_volume_preserving_axes_that_move_nothing_are_an_error(self, monkeypatch):
        body = _body(volume=8.0)
        _wire(monkeypatch, [body], FakeScaleFeatures([body], volume_ratio=1.0,
                                                     extent_ratios=(1.0, 1.0, 1.0)))
        res = ms.handler(bodies=["Block"], x_factor=2, y_factor=0.5, z_factor=1)
        assert res["isError"] is True and "unchanged" in res["message"]

    def test_refused_non_uniform_switch_is_reported(self, monkeypatch):
        body = _body(volume=8.0)
        _wire(monkeypatch, [body], FakeScaleFeatures([body], non_uniform_result=False))
        msg = error_message(ms.handler(bodies=["Block"], x_factor=2, y_factor=2, z_factor=2))
        assert "setToNonUniform" in msg and "nothing was scaled" in msg


# ── mode / factor guards ─────────────────────────────────────────────────────

class TestGuards:
    def test_two_of_three_axis_factors_is_refused_naming_the_missing_one(self, monkeypatch):
        _wire(monkeypatch, [_body()])
        msg = error_message(ms.handler(bodies=["Block"], x_factor=2, y_factor=2))
        assert "z_factor" in msg and "all three" in msg

    def test_factor_and_axis_factors_together_are_refused(self, monkeypatch):
        _wire(monkeypatch, [_body()])
        msg = error_message(ms.handler(bodies=["Block"], factor=2, x_factor=2, y_factor=2,
                                       z_factor=2))
        assert "EITHER" in msg and "not both" in msg

    def test_no_factor_at_all_is_refused(self, monkeypatch):
        _wire(monkeypatch, [_body()])
        msg = error_message(ms.handler(bodies=["Block"]))
        assert "'factor' is required" in msg

    def test_zero_factor_is_refused_naming_the_value(self, monkeypatch):
        _wire(monkeypatch, [_body()])
        msg = error_message(ms.handler(bodies=["Block"], factor=0))
        assert "greater than zero" in msg and "0" in msg

    def test_negative_factor_is_refused_and_points_at_mirror(self, monkeypatch):
        _wire(monkeypatch, [_body()])
        msg = error_message(ms.handler(bodies=["Block"], factor=-2))
        assert "greater than zero" in msg and "-2" in msg and "model_mirror" in msg

    def test_zero_axis_factor_is_refused_naming_the_axis(self, monkeypatch):
        _wire(monkeypatch, [_body()])
        msg = error_message(ms.handler(bodies=["Block"], x_factor=2, y_factor=0, z_factor=2))
        assert "y_factor" in msg and "greater than zero" in msg

    def test_unresolvable_expression_names_the_input_and_echoes_it(self, monkeypatch):
        feats = _wire(monkeypatch, [_body()])
        msg = error_message(ms.handler(bodies=["Block"], factor="NoSuchParamXyz * 2"))
        assert "'factor'" in msg and "NoSuchParamXyz * 2" in msg and "param_get" in msg
        assert feats.last_input is None            # refused before anything was created

    def test_unresolvable_expression_names_which_axis_failed(self, monkeypatch):
        _wire(monkeypatch, [_body()])
        msg = error_message(ms.handler(bodies=["Block"], x_factor="XFactor",
                                       y_factor="NoSuchParamXyz", z_factor="ZFactor"))
        assert "'y_factor'" in msg and "NoSuchParamXyz" in msg

    def test_expression_carrying_a_unit_is_refused_by_name(self, monkeypatch):
        # A dimensioned expression evaluates to the same number whatever units it is read with, so
        # it is refused rather than silently taken as its converted cm value.
        feats = _wire(monkeypatch, [_body()])
        msg = error_message(ms.handler(bodies=["Block"], factor="5 mm"))
        assert "'factor'" in msg and "5 mm" in msg and "carries a unit" in msg
        assert feats.last_input is None            # refused before anything was created

    def test_an_angle_parameter_is_refused_by_name(self, monkeypatch):
        # An angle parameter is unreadable as a LENGTH but readable as an angle, and its unitless
        # reading is its radian value - scaling by 0.5236 would look honest. The angle context is
        # what separates it from a strictly unitless parameter.
        feats = _wire(monkeypatch, [_body()])
        msg = error_message(ms.handler(bodies=["Block"], factor="TiltAngle"))
        assert "'factor'" in msg and "TiltAngle" in msg and "carries a unit" in msg
        assert feats.last_input is None            # refused before anything was created

    def test_a_bare_unitless_parameter_is_accepted(self, monkeypatch):
        # A strictly unitless parameter cannot be read in a length context at all - that RAISE is
        # what identifies it, and it must not be mistaken for a unit-carrying expression. It is the
        # shape a feature refuses BY STRING, so the resolved number is what reaches the feature.
        body = _body(volume=8.0)
        feats = _wire(monkeypatch, [body], FakeScaleFeatures([body], volume_ratio=8.0))
        out = payload(ms.handler(bodies=["Block"], factor="ShrinkAllowance"))
        assert out["factor"] == "ShrinkAllowance"
        assert out["resolved_factor"] == 2.0
        assert feats.last_input.scaleFactor.value == 2.0
        assert feats.last_input.scaleFactor.expression is None

    def test_unitless_arithmetic_is_accepted(self, monkeypatch):
        body = _body(volume=8.0)
        feats = _wire(monkeypatch, [body], FakeScaleFeatures([body], volume_ratio=8.0))
        out = payload(ms.handler(bodies=["Block"], factor="ShrinkAllowance * 2"))
        assert out["factor"] == "ShrinkAllowance * 2"
        assert out["resolved_factor"] == 2.0
        assert feats.last_input.scaleFactor.value == 2.0

    def test_expression_resolving_to_a_non_positive_value_is_refused(self, monkeypatch):
        feats = _wire(monkeypatch, [_body()],
                      units=FakeUnitsManager(valid=("Backwards",), value=-2.0))
        msg = error_message(ms.handler(bodies=["Block"], factor="Backwards"))
        assert "greater than zero" in msg
        assert "'factor'" in msg and "expression 'Backwards' is -2.0" in msg
        assert feats.last_input is None            # refused before anything was created

    def test_missing_units_engine_refuses_an_expression(self, monkeypatch):
        _wire(monkeypatch, [_body()])
        monkeypatch.delattr(ms._common.design(), "fusionUnitsManager")
        msg = error_message(ms.handler(bodies=["Block"], factor="ShrinkAllowance"))
        assert "units engine is unavailable" in msg and "ShrinkAllowance" in msg

    def test_non_numeric_non_expression_factor_is_refused(self, monkeypatch):
        _wire(monkeypatch, [_body()])
        msg = error_message(ms.handler(bodies=["Block"], factor=[2]))
        assert "'factor' must be a number or a parameter-expression string" in msg

    def test_a_sketch_handle_never_reaches_a_per_axis_scale(self, monkeypatch):
        # The solid-body input refuses a sketch handle, so the collection a per-axis scale is built
        # from can only ever hold bodies.
        sketch = type("Sketch", (), {})()
        _wire(monkeypatch, [_body()], tokens={"sk": sketch})
        msg = error_message(ms.handler(bodies=["sk"], x_factor=2, y_factor=2, z_factor=2))
        assert "not a body" in msg and "Sketch" in msg

    def test_a_surface_body_is_refused_by_the_solid_kind(self, monkeypatch):
        surface = BRepBody(name="Skin", volume=0.0, is_solid=False, entity_token="skin")
        _wire(monkeypatch, [surface], tokens={"skin": surface})
        msg = error_message(ms.handler(bodies=["skin"], factor=2))
        assert "SOLID" in msg

    def test_no_active_design(self, monkeypatch):
        _wire(monkeypatch, [_body()])
        assert_no_active_design(ms, ms.handler, bodies=["Block"], factor=2)


# ── anchor ───────────────────────────────────────────────────────────────────

class TestAnchor:
    def test_default_anchor_is_the_component_origin(self, monkeypatch):
        body = _body(volume=8.0)
        feats = _wire(monkeypatch, [body], FakeScaleFeatures([body]))
        out = payload(ms.handler(bodies=["Block"], factor=2))
        assert feats.last_input.point is _ORIGIN
        assert out["anchor"] == "component origin"

    def test_a_vertex_handle_becomes_the_anchor(self, monkeypatch):
        body = _body(volume=8.0)
        vertex = object()
        feats = _wire(monkeypatch, [body], FakeScaleFeatures([body]))
        monkeypatch.setattr(ms._ANCHOR, "resolve", lambda raw: (vertex, None))
        out = payload(ms.handler(bodies=["Block"], factor=2, anchor="h"))
        assert feats.last_input.point is vertex
        assert out["anchor"] == "vertex"

    def test_anchor_resolution_error_propagates(self, monkeypatch):
        _wire(monkeypatch, [_body()])
        monkeypatch.setattr(ms._ANCHOR, "resolve",
                            lambda raw: (None, "'anchor' must be a vertex, but the handle points at "
                                               "a BRepFace."))
        msg = error_message(ms.handler(bodies=["Block"], factor=2, anchor="face"))
        assert "must be a vertex" in msg

    def test_missing_origin_point_with_no_anchor_is_refused(self, monkeypatch):
        _wire(monkeypatch, [_body()], origin=None)
        msg = error_message(ms.handler(bodies=["Block"], factor=2))
        assert "origin construction point" in msg and "find_geometry" in msg


# ── honesty ──────────────────────────────────────────────────────────────────

class TestHonesty:
    def test_unchanged_volume_reports_error_not_ok(self, monkeypatch):
        body = _body(volume=8.0)
        _wire(monkeypatch, [body], FakeScaleFeatures([body], volume_ratio=1.0,
                                                     extent_ratios=(1.0, 1.0, 1.0)))
        res = ms.handler(bodies=["Block"], factor=2)
        assert res["isError"] is True
        assert "unchanged" in res["message"] and "volume did not move" in res["message"]
        # a single-body call has no other body to have resized - no PARTIAL claim
        assert "PARTIAL" not in res["message"]

    def test_one_body_scaled_by_the_wrong_amount_is_named(self, monkeypatch):
        # A lands on x8 as asked, B only on x2: the message must name B, leave A out of it, and
        # own the split instead of claiming the whole call failed.
        a, b = _body(name="A", volume=8.0), _body(name="B", volume=16.0)
        _wire(monkeypatch, [a, b], FakeScaleFeatures([a, b], volume_ratio={"A": 8.0, "B": 2.0}))
        res = ms.handler(bodies=["A", "B"], factor=2)
        assert res["isError"] is True
        assert "'B'" in res["message"] and "x2.0" in res["message"] and "x8.0" in res["message"]
        assert "'A'" not in res["message"]
        assert "PARTIAL" in res["message"] and "design_delete_feature" in res["message"]

    def test_wrong_volume_ratio_reports_error_not_ok(self, monkeypatch):
        # the body moved, but by x2 - not the x8 a uniform factor of 2 implies.
        body = _body(volume=8.0)
        _wire(monkeypatch, [body], FakeScaleFeatures([body], volume_ratio=2.0))
        res = ms.handler(bodies=["Block"], factor=2)
        assert res["isError"] is True
        assert "x2.0" in res["message"] and "x8.0" in res["message"] and "Block" in res["message"]
        # a single-body call has no other body to have resized - no PARTIAL claim
        assert "PARTIAL" not in res["message"]
        # a parametric design DOES leave a timeline feature, so the remedy names it
        assert "remains in the timeline" in res["message"]
        assert "design_delete_feature" in res["message"]

    def test_unreadable_geometry_fails_closed(self, monkeypatch):
        body = _body(volume=8.0)
        _wire(monkeypatch, [body], FakeScaleFeatures([body]))
        monkeypatch.setattr(ms, "_measure", lambda b: (None, None))
        res = ms.handler(bodies=["Block"], factor=2)
        assert res["isError"] is True
        assert "could not be verified" in res["message"] and "model_inspect" in res["message"]

    def test_health_error_reported_not_false_ok(self, monkeypatch):
        body = _body(volume=8.0)
        feature = FakeScaleFeature(health=2)
        _wire(monkeypatch, [body], FakeScaleFeatures([body], feature=feature))
        res = ms.handler(bodies=["Block"], factor=2)
        assert res["isError"] is True
        assert "failed to compute" in res["message"] and "collapses the body" in res["message"]

    def test_no_feature_returned_is_error(self, monkeypatch):
        body = _body(volume=8.0)
        _wire(monkeypatch, [body], FakeScaleFeatures([body], returns_nothing=True))
        res = ms.handler(bodies=["Block"], factor=2)
        assert res["isError"] is True and "no feature" in res["message"].lower()

    def test_add_raising_surfaces_as_error(self, monkeypatch):
        body = _body(volume=8.0)
        feats = FakeScaleFeatures([body])
        feats.add = lambda inp: (_ for _ in ()).throw(RuntimeError("scale collapses the geometry"))
        _wire(monkeypatch, [body], feats)
        res = ms.handler(bodies=["Block"], factor=200)
        assert res["isError"] is True and "Scale failed" in res["message"]
        assert "collapses the geometry" in res["message"]


# ── DIRECT mode: scaleFeatures.add returns nothing while the resize LANDS (measured) ─────────

class TestDirectModeNoFeature:
    def test_direct_none_with_the_measured_ratio_is_ok(self, monkeypatch):
        body = _body(volume=8.0)
        _wire(monkeypatch, [body], FakeScaleFeatures([body], volume_ratio=8.0,
                                                     returns_nothing=True), design_type=0)
        out = payload(ms.handler(bodies=["Block"], factor=2))
        assert out["scaled"] is True and out["volume_ratio"] == 8.0

    def test_direct_none_publishes_no_feature_name(self, monkeypatch):
        # No feature object exists, so no name may be echoed - the note says so instead.
        body = _body(volume=8.0)
        _wire(monkeypatch, [body], FakeScaleFeatures([body], volume_ratio=8.0,
                                                     returns_nothing=True), design_type=0)
        out = payload(ms.handler(bodies=["Block"], factor=2))
        assert "feature" not in out
        assert out["no_timeline_feature"] is True
        assert "DIRECT mode" in out["note"]

    def test_direct_none_names_the_bodies_captured_before_the_scale(self, monkeypatch):
        # The proxies stop answering .name once the scale ran; the payload must still name them.
        body = _body(volume=8.0)
        _wire(monkeypatch, [body], FakeScaleFeatures([body], volume_ratio=8.0,
                                                     returns_nothing=True), design_type=0)
        out = payload(ms.handler(bodies=["Block"], factor=2))
        assert out["bodies"] == ["Block"]

    def test_declared_outputs_hold_on_the_direct_path(self, monkeypatch):
        body = _body(volume=8.0)
        _wire(monkeypatch, [body], FakeScaleFeatures([body], volume_ratio=8.0,
                                                     returns_nothing=True), design_type=0)
        out = payload(ms.handler(bodies=["Block"], factor=2))
        for o in ms.RETURNS:
            assert o.assert_present(out) == "", o.key

    def test_direct_none_with_a_failed_effect_check_is_an_error(self, monkeypatch):
        # add() returned nothing AND the volume did not move: the fall-through must not turn that
        # into a success.
        body = _body(volume=8.0)
        _wire(monkeypatch, [body], FakeScaleFeatures([body], volume_ratio=1.0, extent_ratios=(1.0,)*3,
                                                     returns_nothing=True), design_type=0)
        res = ms.handler(bodies=["Block"], factor=2)
        assert res["isError"] is True and "unchanged" in res["message"]
        # There is no timeline feature on this path - the remediation must not name one.
        assert "design_delete_feature" not in res["message"]
        assert "undo in Fusion" in res["message"]

    def test_direct_none_wrong_ratio_names_the_body_and_points_at_undo(self, monkeypatch):
        # the body moved, but by x2 - not the x8 a factor of 2 implies; the message must still name
        # the body (captured pre-mutation) and offer a remedy that exists in direct mode.
        body = _body(volume=8.0)
        _wire(monkeypatch, [body], FakeScaleFeatures([body], volume_ratio=2.0,
                                                     returns_nothing=True), design_type=0)
        res = ms.handler(bodies=["Block"], factor=2)
        assert res["isError"] is True and "'Block'" in res["message"]
        assert "design_delete_feature" not in res["message"]
        assert "undo in Fusion" in res["message"]

    def test_parametric_none_stays_an_error(self, monkeypatch):
        # Even with the volume ratio the factors imply: a None feature in a PARAMETRIC design is
        # unmeasured as a success, so it is refused.
        body = _body(volume=8.0)
        _wire(monkeypatch, [body], FakeScaleFeatures([body], volume_ratio=8.0,
                                                     returns_nothing=True), design_type=1)
        res = ms.handler(bodies=["Block"], factor=2)
        assert res["isError"] is True and "returned no feature" in res["message"]
        assert "DIRECT mode" not in res["message"]


# ── declared output contract ─────────────────────────────────────────────────

class TestVolumePreservingMix:
    """A per-axis mix whose factors multiply to 1 (2 x 0.5 x 1) expects NO volume change, so the
    ratio check cannot discriminate and the verdict falls to "did the geometry move at all". Volume
    is one of the two signals that answers that, and it is the ONLY one left when the bounding box
    cannot be read."""

    def test_a_volume_only_move_still_proves_the_scale_took(self, monkeypatch):
        # bbox unreadable, volume readable and MOVED: without the volume half of the moved test this
        # comes back as "nothing was resized" on a scale that plainly landed
        body = _body(volume=8.0)
        feats = FakeScaleFeatures([body], volume_ratio=2.0, extent_ratios=(1.0, 1.0, 1.0))
        _wire(monkeypatch, [body], feats)
        monkeypatch.setattr(ms, "_measure",
                            lambda b, seen=[]: (seen.append(b) or None) or
                            ((8.0, None) if len(seen) == 1 else (16.0, None)))
        out = payload(ms.handler(bodies=["Block"], x_factor=2, y_factor=0.5, z_factor=1))
        assert out["scaled"] is True
        assert out["scale_check"] == "geometry_changed"
        assert out["volume_ratio"] == 2.0

    def test_neither_signal_moving_is_an_error_not_a_false_ok(self, monkeypatch):
        # volume identical AND every extent identical: the scale did nothing, and the expectation of
        # 1.0 means the ratio branch cannot catch it - only this gate can
        body = _body(volume=8.0)
        feats = FakeScaleFeatures([body], volume_ratio=1.0, extent_ratios=(1.0, 1.0, 1.0))
        _wire(monkeypatch, [body], feats)
        res = ms.handler(bodies=["Block"], x_factor=2, y_factor=0.5, z_factor=1)
        assert res["isError"] is True
        assert "nothing was resized" in res["message"]


class TestVolumeCheckThatCouldNotRun:
    def test_a_bbox_only_verdict_declares_the_volume_check_did_not_run(self, monkeypatch):
        # No body offers a before/after volume pair, so no volume_ratio exists to publish. The
        # DECLARED key may only be absent when the payload states the condition itself, so the flag
        # plus the note is what keeps the promised volume check from reading as one that ran.
        body = _body(volume=8.0)
        feats = FakeScaleFeatures([body])
        _wire(monkeypatch, [body], feats)
        boxes = [((0.0, 0.0, 0.0)), ((2.0, 2.0, 2.0))]
        monkeypatch.setattr(ms, "_measure",
                            lambda b, seen=[]: (seen.append(b) or None) or
                            (None, boxes[0] if len(seen) == 1 else boxes[1]))
        out = payload(ms.handler(bodies=["Block"], factor=2))
        assert out["scaled"] is True
        assert out["scale_check"] == "geometry_changed"
        assert out["volume_check_skipped"] is True
        assert "volume_ratio" not in out
        # the sentence names what the branch ACTUALLY tests - the floor, not readability: it fires
        # for a volume that read fine but sits below _MIN_VOLUME_CM3 too
        assert "no body offered a before/after volume pair above the floor" in out["note"].lower()
        assert "volume check did NOT run" in out["note"]

    def test_the_declared_outputs_still_pass_on_that_path(self, monkeypatch):
        # volume_ratio is declared absent_when='volume_check_skipped', so the contract holds only
        # BECAUSE the payload states the condition itself
        body = _body(volume=8.0)
        _wire(monkeypatch, [body], FakeScaleFeatures([body]))
        boxes = [((0.0, 0.0, 0.0)), ((2.0, 2.0, 2.0))]
        monkeypatch.setattr(ms, "_measure",
                            lambda b, seen=[]: (seen.append(b) or None) or
                            (None, boxes[0] if len(seen) == 1 else boxes[1]))
        out = payload(ms.handler(bodies=["Block"], factor=2))
        for spec in ms.RETURNS:
            assert spec.assert_present(out) == "", spec.key

    def test_a_measured_volume_publishes_the_ratio_and_no_skip_flag(self, monkeypatch):
        body = _body(volume=8.0)
        _wire(monkeypatch, [body], FakeScaleFeatures([body]))
        out = payload(ms.handler(bodies=["Block"], factor=2))
        assert out["volume_ratio"] == 8.0
        assert "volume_check_skipped" not in out


class TestOutputContract:
    def test_declared_outputs_are_minted(self, monkeypatch):
        body = _body(volume=8.0)
        _wire(monkeypatch, [body], FakeScaleFeatures([body], volume_ratio=8.0))
        out = payload(ms.handler(bodies=["Block"], factor=2))
        for o in ms.RETURNS:
            assert o.assert_present(out) == "", o.key
