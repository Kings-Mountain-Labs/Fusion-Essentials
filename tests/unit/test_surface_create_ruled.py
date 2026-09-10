"""Unit tests for ``surface_create_ruled.py`` - a RULED surface swept off an edge chain.

Pinned: the type/direction pairing (a direction entity with a non-direction type is REFUSED naming
both, and ruled_type=direction WITHOUT an entity is refused before any call, because Fusion itself
refuses that input); the MEASURED createInput arity (4 arguments for tangent/normal, the direction
entity as a 5th); cm for the distance and RADIANS for the angle at the API; the read-backs off the
created feature (a type/distance/angle disagreeing with the request is an error, an unreadable one is
named in `unverified` rather than assumed good); the result set (RuledSurfaceFeature.bodies holds the
PARENT body too, so the payload publishes the DIFFERENCE against a pre-mutation census and reads
is_solid off the new sheet alone); that the input's `direction` - which raises when read - is never
touched; the bSet trap (profile AND feature built on the component that OWNS the edges); and the
add()-returns-None path, which succeeds on a feature-free body census only in a direct design and
stays an error in a parametric one.

Fakes are SimpleNamespace factories over conftest's shared BRepBody/BRepEdge/MakeComp/MakeDesign, so
no new Fake* class enters the tree.
"""

import math
import types

import pytest

from conftest import (BRepBody, BRepEdge, Line3D, MakeComp, MakeDesign, _NamedCollection,
                      assert_no_active_design, assert_unknown_units, body_proxy, error_message,
                      install, load_tool, payload)

scr = load_tool("surface_create_ruled")

_TOKEN = "edge-token-1"


def _raises(_self):
    raise RuntimeError("2 : InternalValidationError : res")


# ── fakes ──────────────────────────────────────────────────────────────────────

def _body(name, is_solid=False, solid_readable=True):
    """One BRepBody. entityToken defaults to the name, which is the key a before/after body census
    subtracts on; solid_readable=False is the flag that REFUSES to read."""
    return BRepBody(name, is_solid=is_solid, solid_readable=solid_readable)


def _proxy_of(body):
    """A SECOND wrapper for the SAME body. Live, every read hands back a fresh proxy - the object in
    the component's browser collection is NEVER the object feature.bodies returns, and
    `a is b` between two references to one body is always False. So the census that subtracts
    before-bodies from the feature's result set has to key on the entityToken; a fake that handed the
    same Python object to both collections would let an identity key pass while it silently reported
    the parent body as newly created."""
    return BRepBody(body.name, is_solid=body.isSolid, entity_token=body.entityToken)


def _feature(name="RuledSurface1", parent=None, new=(), ruled_type=None, distance=1.0, angle=0.0,
             type_readable=True):
    """A RuledSurfaceFeature. MEASURED: `bodies` holds the body the edges came from FIRST and the new
    sheet after it (a tangent surface off a solid box read back [the solid box, the new sheet]), so
    the fake never lets a handler treat the feature's result set as the result. distance/angle are
    ModelParameters reading CM and RADIANS.

    type_readable=False drives the handler's `unverified` branch. That branch is DEFENSIVE: the
    feature's own ruledSurfaceType/distance/angle are measured to read back live. The member measured
    to RAISE is RuledSurfaceFeatureInput.direction, pinned by TestInputDirectionIsNeverRead."""
    if type_readable:
        feat = types.SimpleNamespace(ruledSurfaceType=ruled_type)
    else:
        feat = type("Feat", (), {"ruledSurfaceType": property(_raises)})()
    feat.name = name
    feat.bodies = _NamedCollection(([parent] if parent is not None else []) + list(new))
    feat.distance = types.SimpleNamespace(value=distance)
    feat.angle = types.SimpleNamespace(value=angle)
    return feat


def _ruled_input(args, reads):
    """A RuledSurfaceFeatureInput whose `direction` RAISES when read - measured, reading it on an
    input that is not a Direction one raises InternalValidationError. It also RECORDS the read, so a
    handler that reaches for it INSIDE safe() is caught too, not only one that lets the raise out."""
    def _get_direction(_self):
        reads.append("direction")
        raise RuntimeError("2 : InternalValidationError : res")

    inp = type("RuledInput", (), {"direction": property(_get_direction)})()
    inp.args = args
    return inp


def _ruled_features(feature=None, add_raises=None, input_is_none=False):
    """The ruledSurfaceFeatures collection. createInput RECORDS its positional arguments - the arity
    is itself a contract under test - and add() records, may raise, and returns `feature`."""
    fx = types.SimpleNamespace(args=None, added=0, feature=feature, reads=[])

    def _create_input(*args):
        fx.args = args
        return None if input_is_none else _ruled_input(args, fx.reads)

    def _add(_given):
        fx.added += 1
        if add_raises:
            raise RuntimeError(add_raises)
        return fx.feature

    fx.createInput = _create_input
    fx.add = _add
    return fx


def _component(name, ruled, bodies=()):
    """A component carrying features.ruledSurfaceFeatures, the two profile factories (each tagged
    with its own component name, so a profile built on the WRONG component is visible), and the
    origin construction axes a world-axis direction resolves to."""
    comp = MakeComp(name=name, bodies=list(bodies),
                    construction_axes=tuple(types.SimpleNamespace(name=a) for a in "XYZ"))
    comp.features = types.SimpleNamespace(ruledSurfaceFeatures=ruled)
    comp.createBRepEdgeProfile = lambda coll, n=name: ("edge_profile", n)
    comp.createOpenProfile = lambda coll, chained, n=name: ("open_profile", n)
    return comp


def _edge(owner, parent, token=_TOKEN):
    """One BRepEdge on `parent`, whose .body names the owning component - what
    _curve_host_component reads. `parent` is the body the surface will grow from, and it already
    lives in the component, so the pre-mutation census knows it."""
    edge = BRepEdge(curve=None, entity_token=token)
    parent.parentComponent = owner
    edge.body = parent
    return edge


def _wire_adsk(monkeypatch, edge):
    import adsk.fusion
    monkeypatch.setattr(adsk.fusion, "BRepEdge", type(edge))
    # bare isinstance() against a Mock raises TypeError - AxisRef tests both classes in turn.
    monkeypatch.setattr(adsk.fusion, "SketchLine", type("SL", (), {}))
    monkeypatch.setattr(adsk.fusion, "BRepFace", type("BF", (), {}))
    monkeypatch.setattr(scr.adsk.core.ValueInput, "createByReal",
                        staticmethod(lambda v: ("real", v)))


@pytest.fixture
def wired(monkeypatch):
    """A one-component parametric design whose root holds the parent body the seed edge belongs to,
    with the feature collection producing [parent, one new non-solid sheet]."""
    parent = _body("Body1", is_solid=False)
    ruled = _ruled_features(feature=_feature(parent=parent, new=[_body("RuledSurf1")]))
    comp = _component("Root", ruled, bodies=[_proxy_of(parent)])
    edge = _edge(comp, parent)
    design = MakeDesign(comp=comp, tokens={_TOKEN: edge})
    design.designType = 1                        # parametric
    install(scr, design)
    _wire_adsk(monkeypatch, edge)
    return types.SimpleNamespace(design=design, comp=comp, ruled=ruled, edge=edge, parent=parent)


def _call(**kw):
    args = {"edges": [_TOKEN], "distance": 10.0, "units": "mm"}
    args.update(kw)
    return scr.ruled_handler(**args)


def _replace_features(wired, ruled):
    wired.ruled = ruled
    wired.comp.features.ruledSurfaceFeatures = ruled
    return ruled


# ── the type / direction pairing ───────────────────────────────────────────────

class TestDirectionPairing:
    def test_direction_with_a_non_direction_type_is_refused_naming_both(self, wired):
        msg = error_message(_call(ruled_type="tangent", direction="z"))
        assert "tangent" in msg and "'z'" in msg
        assert wired.ruled.args is None, "no input may be built once the pairing is refused"

    def test_direction_type_without_a_direction_is_refused_before_the_call(self, wired):
        msg = error_message(_call(ruled_type="direction"))
        assert "invalid argument direction" in msg
        assert wired.ruled.args is None

    def test_direction_type_passes_the_axis_entity_as_the_fifth_argument(self, wired):
        body = payload(_call(ruled_type="direction", direction="z"))
        assert len(wired.ruled.args) == 5
        assert wired.ruled.args[4] is wired.comp.zConstructionAxis
        assert body["direction"] == "z-axis"

    def test_tangent_calls_create_input_with_four_arguments(self, wired):
        # MEASURED arity: tangent/normal build from 4 arguments. A 5th None would be a guess.
        payload(_call(ruled_type="tangent"))
        assert len(wired.ruled.args) == 4

    def test_the_requested_type_is_the_one_handed_to_create_input(self, wired):
        import adsk.fusion
        payload(_call(ruled_type="normal"))
        assert wired.ruled.args[3] is adsk.fusion.RuledSurfaceTypes.NormalRuledSurfaceType

    def test_a_face_handle_direction_is_refused_as_a_vector_not_an_entity(self, wired):
        import adsk.fusion
        face = adsk.fusion.BRepFace()
        face.entityToken = "face-token"
        wired.design._tokens["face-token"] = face
        msg = error_message(_call(ruled_type="direction", direction="face-token"))
        assert "ENTITY" in msg
        assert wired.ruled.args is None


# ── values at the API boundary ─────────────────────────────────────────────────

class TestValuesAtTheApi:
    def test_distance_is_scaled_to_centimetres(self, wired):
        payload(_call(distance=10.0, units="mm"))
        assert wired.ruled.args[1] == ("real", 1.0)

    def test_angle_is_converted_to_radians(self, wired):
        wired.ruled.feature.angle.value = math.radians(30.0)     # so the read-back agrees
        payload(_call(angle_deg=30.0))
        assert wired.ruled.args[2] == ("real", pytest.approx(math.radians(30.0)))

    def test_a_zero_angle_is_still_passed(self, wired):
        payload(_call())
        assert wired.ruled.args[2] == ("real", 0.0)

    def test_a_non_numeric_angle_is_refused_naming_the_value(self, wired):
        assert "wobbly" in error_message(_call(angle_deg="wobbly"))

    def test_a_zero_distance_is_refused(self, wired):
        assert "non-zero" in error_message(_call(distance=0))

    def test_unknown_units_are_refused(self, wired):
        assert_unknown_units(scr.ruled_handler, edges=[_TOKEN], distance=10.0)

    def test_an_unknown_ruled_type_is_refused_listing_the_options(self, wired):
        msg = error_message(_call(ruled_type="helical"))
        assert "tangent" in msg and "normal" in msg and "direction" in msg


# ── read-backs off the created feature ─────────────────────────────────────────

class TestFeatureReadback:
    def test_a_type_that_reads_back_wrong_is_an_error(self, wired):
        import adsk.fusion
        wired.ruled.feature = _feature(
            parent=wired.parent, new=[_body("S1")],
            ruled_type=adsk.fusion.RuledSurfaceTypes.NormalRuledSurfaceType)
        msg = error_message(_call(ruled_type="tangent"))
        assert "reads back" in msg and "tangent" in msg
        assert "design_delete_feature" in msg, "a wrong-effect error names the remedy that exists"

    def test_a_type_that_reads_back_right_is_published_verified(self, wired):
        import adsk.fusion
        wired.ruled.feature = _feature(
            parent=wired.parent, new=[_body("S1")],
            ruled_type=adsk.fusion.RuledSurfaceTypes.TangentRuledSurfaceType)
        body = payload(_call(ruled_type="tangent"))
        assert body["ruled_type"] == "tangent"
        assert "unverified" not in body

    def test_an_unreadable_type_is_named_unverified_not_assumed_good(self, wired):
        wired.ruled.feature = _feature(parent=wired.parent, new=[_body("S1")], type_readable=False)
        body = payload(_call(ruled_type="tangent"))
        assert body["unverified"] == ["ruled_type"]
        assert "ruled_type" in body["note"]

    def test_a_distance_that_reads_back_wrong_is_an_error(self, wired):
        import adsk.fusion
        wired.ruled.feature = _feature(
            parent=wired.parent, new=[_body("S1")], distance=3.7,
            ruled_type=adsk.fusion.RuledSurfaceTypes.TangentRuledSurfaceType)
        msg = error_message(_call(distance=10.0, units="mm"))
        assert "37.0" in msg and "10.0" in msg

    def test_the_angle_is_published_back_in_degrees(self, wired):
        import adsk.fusion
        wired.ruled.feature = _feature(
            parent=wired.parent, new=[_body("S1")], angle=math.radians(30.0),
            ruled_type=adsk.fusion.RuledSurfaceTypes.TangentRuledSurfaceType)
        body = payload(_call(angle_deg=30.0))
        assert body["angle_deg"] == pytest.approx(30.0)

    def test_an_angle_that_reads_back_wrong_is_an_error(self, wired):
        import adsk.fusion
        wired.ruled.feature = _feature(
            parent=wired.parent, new=[_body("S1")], angle=math.radians(12.0),
            ruled_type=adsk.fusion.RuledSurfaceTypes.TangentRuledSurfaceType)
        assert "12.0 deg" in error_message(_call(angle_deg=30.0))


# ── the sheet discriminator ────────────────────────────────────────────────────

class TestResultBody:
    def test_the_open_sheet_is_reported_with_its_body_name(self, wired):
        body = payload(_call())
        assert body["result_bodies"] == ["RuledSurf1"]
        assert body["is_solid"] is False
        assert body["edge_count"] == 1
        assert body["feature"] == "RuledSurface1"

    def test_the_parent_body_is_not_reported_as_created(self, wired):
        # feature.bodies holds [parent, new sheet] live, so publishing it raw would claim the body
        # the edges came from was created by this call.
        body = payload(_call())
        assert wired.parent.name not in body["result_bodies"]
        assert len(body["result_bodies"]) == 1

    def test_a_result_set_holding_only_the_parent_is_an_error(self, wired):
        # the census is the gate: a feature whose result set adds no body to the component made no
        # surface, however healthy the feature object reads.
        wired.ruled.feature = _feature(parent=wired.parent, new=[])
        res = _call()
        assert res["isError"] is True
        assert "added nothing" in res["message"]

    def test_the_parent_reached_as_an_OCCURRENCE_PROXY_is_still_not_created(self, wired):
        # The two sides of the diff are two DIFFERENT collections - the component's own body census
        # before, feature.bodies after - and each mints its own wrapper for the same body. A proxy's
        # entityToken DIFFERS from its native's (measured), so keyed on the wrapper's token the
        # parent body reads as newly created and the payload claims a second sheet nothing made.
        wired.comp.bRepBodies = _NamedCollection([wired.parent])          # the census: the NATIVE
        proxy = body_proxy(wired.parent,
                           types.SimpleNamespace(name="Root:1", fullPathName="Root:1"))
        assert proxy.entityToken != wired.parent.entityToken              # a real proxy, not a copy
        wired.ruled.feature = _feature(parent=proxy, new=[_body("RuledSurf1")])
        body = payload(_call())
        assert body["result_bodies"] == ["RuledSurf1"]

    def test_an_UNTOKENED_parent_and_its_proxy_still_key_together(self, wired):
        # With no token there is no identity, and the fallback carries the pair on its own. It is the
        # bare NAME for that reason: a proxy delegates its name to its native, while the wrapper's
        # SCOPE reads the occurrence path off the proxy and the component name off the native - two
        # keys for one body, and the parent published as a sheet this call created.
        wired.parent.entityToken = None
        wired.comp.bRepBodies = _NamedCollection([wired.parent])          # the census: the NATIVE
        proxy = body_proxy(wired.parent,
                           types.SimpleNamespace(name="Root:1", fullPathName="Root:1"))
        wired.ruled.feature = _feature(parent=proxy, new=[_body("RuledSurf1")])
        body = payload(_call())
        assert body["result_bodies"] == ["RuledSurf1"]

    def test_a_solid_parent_does_not_make_the_sheet_read_solid(self, wired):
        # the draft-check case: ruling off a SOLID box edge. is_solid must come from the NEW sheet,
        # not from the solid parent sitting in the same feature.bodies collection.
        wired.parent.isSolid = True
        body = payload(_call())
        assert body["is_solid"] is False
        assert "SOLID" not in body["note"]
        assert body["result_bodies"] == ["RuledSurf1"]

    def test_a_feature_carrying_only_the_parent_is_an_error(self, wired):
        wired.ruled.feature = _feature(parent=wired.parent)
        msg = error_message(_call())
        assert "added nothing" in msg and "'Root'" in msg
        assert "design_delete_feature" in msg

    def test_a_feature_carrying_no_bodies_at_all_is_the_same_honest_error(self, wired):
        # the error may not claim the feature "holds only the parent" - an empty body set reaches
        # this branch too, and the sentence has to be true of both.
        wired.ruled.feature = _feature()
        msg = error_message(_call())
        assert "added nothing" in msg
        assert "only the body" not in msg

    def test_an_unreadable_solid_flag_is_null_not_an_open_sheet(self, wired):
        # body_facts publishes each flag True/False/None; any() folds the None into False, and this
        # payload's False is the claim "a new OPEN SURFACE body" - about a flag nobody read.
        wired.ruled.feature = _feature(parent=wired.parent,
                                       new=[_body("Blind1", solid_readable=False)])
        body = payload(_call())
        assert body["is_solid"] is None
        assert "is_solid" in body["unverified"]
        assert "UNVERIFIED" in body["note"]
        assert "isSolid=false" not in body["note"]

    def test_a_readable_false_flag_is_still_an_open_sheet(self, wired):
        # the boundary beside the null: a flag that READ false is an answer, so the tri-state must
        # not turn the ordinary sheet result into an unverified one.
        body = payload(_call())
        assert body["is_solid"] is False
        assert "isSolid=false" in body["note"] and "UNVERIFIED" not in body["note"]
        assert "is_solid" not in body.get("unverified", [])

    def test_one_solid_body_still_wins_over_an_unreadable_sibling(self, wired):
        # any-True beats an unknown: a result that DID read solid is the verdict, unread or not.
        wired.ruled.feature = _feature(parent=wired.parent,
                                       new=[_body("Blind1", solid_readable=False),
                                            _body("S1", is_solid=True)])
        body = payload(_call())
        assert body["is_solid"] is True and "SOLID" in body["note"]

    def test_direct_mode_publishes_a_null_solid_flag_as_unverified(self, wired):
        landed = _body("RuledSurf1", solid_readable=False)
        wired.ruled.feature = None
        wired.ruled.add = lambda _i: wired.comp.bRepBodies._items.append(landed)
        wired.design.designType = 0                                   # direct
        body = payload(_call())
        assert body["created"] is True and body["is_solid"] is None
        assert "is_solid" in body["unverified"]

    def test_a_solid_result_is_reported_honestly(self, wired):
        wired.ruled.feature = _feature(parent=wired.parent, new=[_body("S1", is_solid=True)])
        body = payload(_call())
        assert body["is_solid"] is True
        assert "SOLID" in body["note"]

    def test_a_raising_add_is_an_error_quoting_the_platform_message(self, wired):
        _replace_features(wired, _ruled_features(add_raises="3 : nothing to rule from"))
        assert "nothing to rule from" in error_message(_call())

    def test_an_input_that_never_built_is_an_error_and_never_added(self, wired):
        ruled = _replace_features(wired, _ruled_features(input_is_none=True))
        assert "no ruled-surface input" in error_message(_call())
        assert ruled.added == 0


# ── the input member that must never be read ───────────────────────────────────

class TestInputDirectionIsNeverRead:
    """RuledSurfaceFeatureInput.direction RAISES when it is read on an input that is not a Direction
    one (measured), so no verification may reach for it - the direction the payload reports is the
    entity the handler RESOLVED, never a read-back."""

    def test_tangent_completes_without_touching_the_inputs_direction(self, wired):
        payload(_call(ruled_type="tangent"))
        assert wired.ruled.reads == []

    def test_normal_completes_without_touching_the_inputs_direction(self, wired):
        payload(_call(ruled_type="normal"))
        assert wired.ruled.reads == []

    def test_the_direction_type_reports_the_resolved_entity_not_a_read_back(self, wired):
        body = payload(_call(ruled_type="direction", direction="z"))
        assert body["direction"] == "z-axis"
        assert wired.ruled.reads == []

    def test_a_linear_edge_direction_is_labelled_by_its_TYPE(self, wired):
        # measured: neither a BRepEdge nor a SketchLine carries a `name`, so a name read here would
        # be dead code and any fallback string a fabrication.
        line_edge = BRepEdge(curve=Line3D(), entity_token="dir-edge")
        wired.design._tokens["dir-edge"] = line_edge
        body = payload(_call(ruled_type="direction", direction="dir-edge"))
        assert body["direction"] == "BRepEdge"
        assert wired.ruled.args[4] is line_edge

    def test_tangent_and_normal_payloads_carry_no_direction_key(self, wired):
        assert "direction" not in payload(_call(ruled_type="tangent"))
        assert "direction" not in payload(_call(ruled_type="normal"))


# ── the bSet trap: build where the edges live ──────────────────────────────────

class TestOwningComponent:
    def test_the_profile_and_feature_are_built_on_the_component_that_owns_the_edges(self, monkeypatch):
        parent = _body("SubBody")
        sub_ruled = _ruled_features(feature=_feature(parent=parent, new=[_body("S1")]))
        sub = _component("Sub", sub_ruled, bodies=[_proxy_of(parent)])
        active_ruled = _ruled_features(feature=_feature(new=[_body("Wrong")]))
        active = _component("Root", active_ruled)
        edge = _edge(sub, parent)
        design = MakeDesign(comp=active, tokens={_TOKEN: edge}, all_components=[active, sub])
        design.designType = 1
        install(scr, design)
        _wire_adsk(monkeypatch, edge)

        body = payload(_call())
        assert sub_ruled.added == 1, "the feature must be added on the edges' OWN component"
        assert active_ruled.added == 0
        assert sub_ruled.args[0] == ("edge_profile", "Sub")
        assert body["result_bodies"] == ["S1"]

    def test_brep_edges_route_through_create_brep_edge_profile(self, wired):
        payload(_call())
        assert wired.ruled.args[0] == ("edge_profile", "Root")


# ── add() returning nothing ────────────────────────────────────────────────────

class TestNoFeatureReturned:
    def _landing_add(self, wired):
        """An add() that returns NOTHING while a new surface body lands. Measured for six OTHER
        feature classes in direct designs, not for this one - so the handler must decide on the
        model, which is the only evidence a no-feature return leaves either way."""
        landed = _body("RuledSurf1")
        wired.ruled.feature = None
        wired.ruled.add = lambda _i: wired.comp.bRepBodies._items.append(landed)

    def test_direct_mode_succeeds_on_the_body_census_when_a_surface_landed(self, wired):
        self._landing_add(wired)
        wired.design.designType = 0                                   # direct
        body = payload(_call())
        assert body["created"] is True
        assert body["result_bodies"] == ["RuledSurf1"]
        assert body["bodies_added"] == 1
        assert body.get("feature") is None, "a direct design has no feature name to publish"
        assert "DIRECT" in body["note"]

    def test_direct_mode_with_no_new_body_is_an_error(self, wired):
        wired.ruled.feature = None
        wired.design.designType = 0
        assert "returned no feature" in error_message(_call())

    def test_a_parametric_design_never_treats_a_missing_feature_as_success(self, wired):
        self._landing_add(wired)
        wired.design.designType = 1
        assert "returned no feature" in error_message(_call())


# ── shared guards ──────────────────────────────────────────────────────────────

class TestGuards:
    def test_no_active_design_is_a_clean_error(self, wired):
        assert_no_active_design(scr, scr.ruled_handler, edges=[_TOKEN], distance=10.0)

    def test_an_unresolvable_edge_handle_is_refused(self, wired):
        assert "handle" in error_message(_call(edges=["not-a-token"]))

    def test_missing_edges_are_refused(self, wired):
        assert "edges" in error_message(_call(edges=[]))
