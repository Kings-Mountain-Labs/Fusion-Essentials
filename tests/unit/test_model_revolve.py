"""Unit tests for ``model_revolve.py`` — revolve a sketch profile about an axis.

Pinned (no live Fusion): the unknown-operation/zero-angle guards, sketch + profile resolution
(named vs most recent; profile_index bounds), axis resolution (x/y/z origin axis, an edge or
axis-defining FACE handle, OR 'line:<index>'),
the degrees → radians conversion handed to setAngleExtent, and the operation-name mapping.
"""

import json
import math
import types

import adsk.fusion
import pytest

from conftest import (BRepBody, BRepEdge, BRepFace, Cylinder, FakePoint, FakeVector3D, Line3D,
                      MakeComp, Plane, _NamedCollection, _SimpleNamed, assert_no_active_design,
                      entity_proxy, install, load_tool, make_design, make_occurrence, make_sketch,
                      payload)

rv = load_tool("model_revolve")


def _sketch(name, profile_count=1, line_count=2, compute_deferred=False):
    """A sketch holding `profile_count` closed regions and `line_count` axis-candidate lines."""
    return make_sketch(name=name, lines=[("line", i) for i in range(line_count)],
                       profiles=[("profile", i) for i in range(profile_count)],
                       is_compute_deferred=compute_deferred)


class FakeRevInput:
    def __init__(self, profile, axis, operation, participant_error=None):
        self.profile = profile
        self.axis = axis
        self.operation = operation
        self.participant_error = participant_error
        self.angle_extent = None
        self.two_sides = None
        self._participant_bodies = None
        self.participant_set_after_extent = None
    @property
    def participantBodies(self):
        raise AttributeError("participantBodies is write-only")
    @participantBodies.setter
    def participantBodies(self, bodies):
        if self.participant_error is not None:
            raise self.participant_error
        self.participant_set_after_extent = self.angle_extent is not None or self.two_sides is not None
        self._participant_bodies = list(bodies)
    def setAngleExtent(self, isSymmetric, angle):
        self.angle_extent = (isSymmetric, angle)
        return True
    # Real API name (confirmed live). Only the real name is provided - a fake that also accepted
    # a wrong name like `setTwoSidesExtent` would let the test pass against a method that doesn't
    # exist on the real RevolveFeatureInput; here the wrong name raises AttributeError.
    def setTwoSideAngleExtent(self, a, b):
        self.two_sides = (a, b)
        return True


class FakeRevFeature:
    def __init__(self):
        self.name = "Revolve1"
        self.bodies = _NamedCollection([BRepBody("Body1")])


class FakeRevFeatures:
    def __init__(self):
        self.last_input = None
        self.add_calls = 0            # counted separately: a feature can be BUILT on a collection
        self.participant_error = None
        self.participant_bodies_at_add = None
        self.on_add = None
    def createInput(self, profile, axis, operation):      # whose createInput was never called
        self.last_input = FakeRevInput(profile, axis, operation, self.participant_error)
        return self.last_input
    def add(self, inp):
        self.add_calls += 1
        self.participant_bodies_at_add = (None if inp._participant_bodies is None
                                          else list(inp._participant_bodies))
        if self.on_add is not None:
            self.on_add(inp)
        return FakeRevFeature()


def _comp(sketches, rf, name="Comp", token="TOKEN:Comp"):
    """A component owning `sketches`, the revolve feature collection and the three origin axes."""
    # Every live component answers an entityToken, and _common.same_component compares on it: the
    # assembly-context lift REFUSES an owner it cannot identify rather than guess whether a native
    # entity needs proxying. A test that wants that state deletes the attribute.
    comp = MakeComp(name=name, sketches=sketches, entity_token=token,
                    construction_axes=(("axis", "x"), ("axis", "y"), ("axis", "z")))
    comp.features = types.SimpleNamespace(revolveFeatures=rf)
    return comp


def _install(sketches):
    rf = FakeRevFeatures()
    install(rv, make_design(comp=_comp(sketches, rf)))
    import adsk.core
    adsk.core.ValueInput.createByReal = staticmethod(lambda v: ("real", v))
    return rf


class TestGuards:
    @pytest.mark.parametrize("operation", ["new", "join"])
    def test_target_bodies_refused_for_non_participating_operations(self, operation):
        _install([_sketch("S")])
        res = rv.handler(sketch_name="S", operation=operation,
                         target_bodies=["Body1"])
        assert res["isError"] is True
        assert "only applies to cut/intersect" in res["message"]

    def test_unknown_operation(self):
        _install([_sketch("S")])
        res = rv.handler(sketch_name="S", operation="weld")
        assert res["isError"] is True and "Unknown operation" in res["message"]

    def test_zero_angle(self):
        _install([_sketch("S")])
        res = rv.handler(sketch_name="S", angle_deg=0)
        assert res["isError"] is True and "non-zero 'angle_deg'" in res["message"]

    def test_no_sketch_named(self):
        _install([_sketch("S")])
        res = rv.handler(sketch_name="Nope")
        assert res["isError"] is True and "No sketch named 'Nope'" in res["message"]

    def test_the_component_scope_is_declared_on_the_wire(self):
        # the schema is strict, so a handler parameter no property declares is refused before it
        # reaches the handler - the scope would be unreachable and its refusal would name it anyway.
        sd = load_tool("_sketch_detail")
        assert rv.revolve_tool.input_schema["properties"]["component"] == sd.COMPONENT_SCOPE[1]

    def test_the_component_scope_reaches_the_sketch_resolve(self, monkeypatch):
        # a sketch name two components carry is refused, and 'component' is the way through - the
        # remedy the refusal names, so it has to be the one the resolver is actually given.
        _install([_sketch("S")])
        seen = {}

        def _scoped(design, name, component, input_name="component"):
            seen.update(component=component, input_name=input_name)
            return None, name, "refused"

        monkeypatch.setattr(rv._sketch_detail, "scoped_or_recent_sketch", _scoped)
        res = rv.handler(sketch_name="S", component="Frame")
        assert res["isError"] is True and res["message"] == "refused"
        assert seen == {"component": "Frame", "input_name": "component"}

    def test_a_padded_name_reports_the_name_the_walk_searched_for(self):
        # the resolver STRIPS the name before searching, so the miss quotes the stripped form -
        # echoing the raw input names a sketch nothing ever looked for.
        _install([_sketch("S")])
        res = rv.handler(sketch_name="  Ghost  ")
        assert res["isError"] is True
        assert "No sketch named 'Ghost'" in res["message"]
        assert "'  Ghost  '" not in res["message"]

    def test_a_blank_name_with_no_sketch_in_the_design_never_quotes_None(self):
        # a blank name asks for the MOST RECENT sketch, so there is no requested name to quote:
        # the named-miss branch would render the absent name as the literal string 'None'.
        _install([])
        res = rv.handler()
        assert res["isError"] is True
        assert "'None'" not in res["message"]
        assert res["message"] == "No sketch to revolve. Create one and draw a closed profile first."

    def test_a_whitespace_only_name_takes_the_most_recent_sketch(self):
        # ' ' strips to blank, which means the most recent sketch - searching for a space instead
        # misses every sketch and reports a name no caller typed.
        _install([_sketch("First"), _sketch("Last")])
        res = rv.handler(sketch_name=" ")
        assert "No sketch named" not in json.dumps(res)
        assert payload(res)["sketch"] == "Last"

    def test_profile_out_of_range(self):
        _install([_sketch("S", profile_count=1)])
        res = rv.handler(sketch_name="S", profile_index=5)
        assert res["isError"] is True and "out of range" in res["message"]

    def test_bad_axis(self):
        _install([_sketch("S")])
        res = rv.handler(sketch_name="S", axis="q")
        assert res["isError"] is True and "Could not resolve axis" in res["message"]

    def test_an_index_into_a_deferred_sketch_is_refused(self):
        # int(profile_index) then profiles.item(idx) - neither reaches ProfileRef, so a deferred
        # sketch would revolve whichever region was first before the deferral.
        _install([_sketch("S", profile_count=3, compute_deferred=True)])
        res = rv.handler(sketch_name="S", profile_index=0)
        assert res["isError"] is True
        assert "isComputeDeferred=true" in res["message"] and "'S'" in res["message"]
        assert "'profile_index'" in res["message"]

    def test_an_index_into_a_sketch_computing_normally_still_revolves(self):
        _install([_sketch("S", profile_count=3)])
        assert payload(rv.handler(sketch_name="S", profile_index=0))["sketch"] == "S"


class TestRevolve:
    def test_full_revolve_converts_deg_to_radians(self):
        rf = _install([_sketch("Cup")])
        out = payload(rv.handler(sketch_name="Cup", axis="z", angle_deg=360))
        assert out["revolved"] is True and out["axis"] == "z-axis"
        sym, ang = rf.last_input.angle_extent
        assert ang[0] == "real" and abs(ang[1] - 2 * math.pi) < 1e-9
        assert sym is False

    def test_partial_angle(self):
        rf = _install([_sketch("S")])
        payload(rv.handler(sketch_name="S", angle_deg=90))
        _, ang = rf.last_input.angle_extent
        assert abs(ang[1] - math.pi / 2) < 1e-9

    def test_axis_x_resolves(self):
        rf = _install([_sketch("S")])
        out = payload(rv.handler(sketch_name="S", axis="x"))
        assert rf.last_input.axis == ("axis", "x") and out["axis"] == "x-axis"

    def test_axis_sketch_line(self):
        rf = _install([_sketch("S", line_count=3)])
        out = payload(rv.handler(sketch_name="S", axis="line:1"))
        assert rf.last_input.axis == ("line", 1) and "line:1" in out["axis"]

    def test_operation_cut_mapping(self):
        rf = _install([_sketch("S")])
        payload(rv.handler(sketch_name="S", operation="cut"))
        assert rf.last_input.operation == adsk.fusion.FeatureOperations.CutFeatureOperation

    def test_symmetric_flag(self):
        rf = _install([_sketch("S")])
        payload(rv.handler(sketch_name="S", symmetric=True))
        sym, _ = rf.last_input.angle_extent
        assert sym is True

    def test_two_sided_asymmetric(self):
        import math
        rf = _install([_sketch("S")])
        out = payload(rv.handler(sketch_name="S", angle_deg=90, second_angle_deg=30))
        # setTwoSideAngleExtent used (not setAngleExtent), with both angles in radians
        assert rf.last_input.two_sides is not None
        assert rf.last_input.angle_extent is None
        a, b = rf.last_input.two_sides
        assert abs(a[1] - math.radians(90)) < 1e-9 and abs(b[1] - math.radians(30)) < 1e-9
        assert out["second_angle_deg"] == 30

    def test_fake_rejects_the_nonexistent_method_name(self):
        # The real API method is setTwoSideAngleExtent; the fake must not expose the nonexistent
        # setTwoSidesExtent, so a handler calling it AttributeErrors here instead of silently passing.
        assert not hasattr(FakeRevInput("p", "a", "o"), "setTwoSidesExtent")

    def test_second_angle_ignored_when_symmetric(self):
        rf = _install([_sketch("S")])
        payload(rv.handler(sketch_name="S", angle_deg=90, second_angle_deg=30, symmetric=True))
        assert rf.last_input.two_sides is None     # symmetric wins
        assert rf.last_input.angle_extent is not None


# ── honesty: failed/absent mutation must surface as isError, never a false ok ─
# (the paths test_model_mirror.py / test_model_shell.py treat as mandatory)

class TestHonesty:
    def test_add_returning_none_is_error(self):
        rf = _install([_sketch("S")])
        rf.add = lambda inp: None
        res = rv.handler(sketch_name="S")
        assert res["isError"] is True and "no feature" in res["message"].lower()

    def test_add_raising_surfaces_as_error(self):
        rf = _install([_sketch("S")])

        def _boom(inp):
            raise RuntimeError("axis intersects the profile")

        rf.add = _boom
        res = rv.handler(sketch_name="S")
        assert res["isError"] is True
        assert "Revolve failed" in res["message"] and "axis intersects the profile" in res["message"]
        # the hint may not claim the axis and profile must be COPLANAR: an axis outside the profile's
        # plane is projected onto it, so that is never the cause the caller should chase
        assert "coplanar" not in res["message"].lower()

    def test_no_active_design(self):
        _install([_sketch("S")])
        assert_no_active_design(rv, rv.handler, sketch_name="S")


# -- the axis from GEOMETRY: the entity that DEFINES the axis, not a world key ------------------
#
# RevolveFeatures.createInput's axis takes "a sketch line, construction axis, linear edge or a face
# that defines an axis (cylinder, cone, torus, etc.)" (the installed API's own doc), so the input is
# an AxisRef with face_entity=True. A face mapped to a direction VECTOR and then to a world axis key
# keeps the direction but drops the axis POSITION: a cylinder face at x=30 revolves about the world
# axis through the ORIGIN, which is wrong geometry reported as success.

@pytest.fixture
def wire(monkeypatch):
    """Factory: fake design + handle resolution, installed into the tool at both design seams. The
    entity classes AxisRef isinstance-checks must be REAL classes, since a bare Mock attribute is
    not a type.

    `axes` gives the ACTIVE component construction axes (the by-name axis path); `extra_components`
    puts further components in the design-wide walk (a sketch in a sub-component); `placements` maps
    a component NAME to the occurrences that place it (what the cross-component proxy resolves
    through). Returns the ACTIVE component's revolveFeatures."""
    import adsk.core
    import adsk.fusion

    def _wire(sketches, tokens=None, axes=(), extra_components=(), placements=None):
        rf = FakeRevFeatures()
        root = _comp(sketches, rf)
        root.constructionAxes = _NamedCollection(list(axes))
        root.allOccurrencesByComponent = lambda c, m=dict(placements or {}): _NamedCollection(
            list(m.get(safe_name(c), [])))
        install(rv, make_design(comp=root, tokens=tokens,
                                all_components=[root, *extra_components]))
        monkeypatch.setattr(adsk.fusion, "BRepEdge", BRepEdge)
        monkeypatch.setattr(adsk.fusion, "BRepFace", BRepFace)
        monkeypatch.setattr(adsk.fusion, "SketchLine", type("SL", (), {}))
        monkeypatch.setattr(adsk.core.ValueInput, "createByReal", staticmethod(lambda v: ("real", v)))
        return rf
    return _wire


def safe_name(comp):
    return getattr(comp, "name", None)


def _cylindrical_face(owner=None, proxy=BRepFace._UNSET):
    """A cylinder face whose axis points along +z - the shape that hides a dropped axis POSITION: its
    DIRECTION is a world key, so only the entity reaching createInput proves the position survived.
    `owner` makes it NATIVE to that component (read through the face's body); `proxy` is what its
    createForAssemblyContext hands back, None for the lift that answers nothing."""
    body = BRepBody(parent_component=owner) if owner is not None else None
    return BRepFace(Cylinder(FakeVector3D(0, 0, 1)), body=body, assembly_proxy=proxy)


class TestAxisFromGeometry:
    def test_cylindrical_face_handle_reaches_createinput_as_the_face(self, wire):
        f = _cylindrical_face()
        rf = wire([_sketch("Ring")], tokens={"CYL": f})
        out = payload(rv.handler(sketch_name="Ring", axis="CYL"))
        # the FACE itself, never the component's origin construction axis - the entity is what
        # carries the axis position an off-origin revolve turns about
        assert rf.last_input.axis is f
        assert out["axis"] == "BRepFace"

    def test_planar_face_handle_is_refused(self, wire):
        rf = wire([_sketch("S")], tokens={"F": BRepFace(Plane(normal=FakeVector3D(0, 0, 1)))})
        res = rv.handler(sketch_name="S", axis="F")
        assert res["isError"] is True and "cylindrical" in res["message"]
        assert rf.last_input is None          # refused before any feature transaction opened

    def test_straight_edge_handle_still_reaches_createinput(self, wire):
        e = BRepEdge(curve=Line3D(start=FakePoint(0, 0, 0), end=FakePoint(1, 0, 0)))
        rf = wire([_sketch("S")], tokens={"E": e})
        out = payload(rv.handler(sketch_name="S", axis="E"))
        # no BRepEdge carries a name, so the label falls back to what the entity IS
        assert rf.last_input.axis is e and out["axis"] == "BRepEdge"

    def test_construction_axis_by_name_publishes_the_datums_name(self, wire):
        ax = _SimpleNamed("WheelAxis")
        rf = wire([_sketch("S")], axes=[ax])
        out = payload(rv.handler(sketch_name="S", axis="WheelAxis"))
        assert rf.last_input.axis is ax
        assert out["axis"] == "WheelAxis"       # the datum's own NAME, not the raw input or a type

    def test_ambiguous_construction_axis_name_is_refused(self, wire):
        rf = wire([_sketch("S")], axes=[_SimpleNamed("Hinge"), _SimpleNamed("Hinge")])
        res = rv.handler(sketch_name="S", axis="Hinge")
        assert res["isError"] is True and "names 2 construction axes" in res["message"]
        assert rf.last_input is None            # refused, never resolved to the first hit

    def test_world_key_still_resolves_to_the_origin_construction_axis(self, wire):
        rf = wire([_sketch("S")], tokens={"CYL": _cylindrical_face()})
        out = payload(rv.handler(sketch_name="S", axis="y"))
        assert rf.last_input.axis == ("axis", "y") and out["axis"] == "y-axis"

    def test_line_index_still_resolves_in_the_profiles_own_sketch(self, wire):
        rf = wire([_sketch("S", line_count=3)], tokens={"CYL": _cylindrical_face()})
        out = payload(rv.handler(sketch_name="S", axis="line:2"))
        assert rf.last_input.axis == ("line", 2) and out["axis"] == "sketch line:2"


# -- an axis owned by ANOTHER component: proxied into its occurrence, or refused ----------------
#
# A face NATIVE to another component (assemblyContext None) kills the revolve call outright; the
# same face proxied into the occurrence that places it (createForAssemblyContext) is accepted and
# turns about the correct off-origin axis. A component placed several times is refused instead:
# each instance holds that axis somewhere else, and a revolve's read-back cannot tell them apart.

class TestCrossComponentAxis:
    def test_native_face_from_another_component_is_proxied_into_its_occurrence(self, wire):
        other = _comp([], FakeRevFeatures(), name="PartB", token="TOKEN:PartB")
        proxied = _cylindrical_face()
        proxied.assemblyContext = make_occurrence("PartB:1")
        f = _cylindrical_face(owner=other, proxy=proxied)
        rf = wire([_sketch("Ring")], tokens={"CYL": f},
                  placements={"PartB": [make_occurrence("PartB:1")]})
        out = payload(rv.handler(sketch_name="Ring", axis="CYL"))
        assert rf.last_input.axis is proxied     # the PROXY, never the native cross-component face
        assert out["axis"] == "BRepFace"

    def test_component_placed_twice_is_refused_with_both_paths(self, wire):
        other = _comp([], FakeRevFeatures(), name="PartB", token="TOKEN:PartB")
        f = _cylindrical_face(owner=other, proxy=_cylindrical_face())
        rf = wire([_sketch("Ring")], tokens={"CYL": f},
                  placements={"PartB": [make_occurrence("PartB:1"), make_occurrence("PartB:2")]})
        res = rv.handler(sketch_name="Ring", axis="CYL")
        assert res["isError"] is True
        assert "placed 2 times" in res["message"]
        assert "PartB:1" in res["message"] and "PartB:2" in res["message"]
        assert rf.last_input is None             # refused before any feature transaction opened

    def test_proxy_that_cannot_be_built_is_refused_not_passed_native(self, wire):
        other = _comp([], FakeRevFeatures(), name="PartB", token="TOKEN:PartB")
        f = _cylindrical_face(owner=other, proxy=None)     # the context could not be built
        rf = wire([_sketch("Ring")], tokens={"CYL": f},
                  placements={"PartB": [make_occurrence("PartB:1")]})
        res = rv.handler(sketch_name="Ring", axis="CYL")
        assert res["isError"] is True and "could not be brought into" in res["message"]
        assert rf.last_input is None and rf.add_calls == 0

    def test_unplaced_component_is_refused(self, wire):
        other = _comp([], FakeRevFeatures(), name="PartB", token="TOKEN:PartB")
        rf = wire([_sketch("Ring")], tokens={"CYL": _cylindrical_face(owner=other)})
        res = rv.handler(sketch_name="Ring", axis="CYL")
        assert res["isError"] is True and "not placed in the assembly" in res["message"]
        assert rf.last_input is None

    def test_face_owned_by_the_revolves_own_component_passes_untouched(self, wire):
        # the owner is a DIFFERENT Python object for the same component (component wrappers are
        # never identity-stable), so an `owner is comp` test would send this face down the
        # cross-component path and refuse a perfectly legal axis
        rf = wire([_sketch("Ring")], tokens={"CYL": None})
        host = rv.app.activeProduct.rootComponent
        f = _cylindrical_face(owner=entity_proxy(host))
        rv.app.activeProduct.findEntityByToken = lambda t, e=f: ([e] if t == "CYL" else [])
        out = payload(rv.handler(sketch_name="Ring", axis="CYL"))
        assert rf.last_input.axis is f and out["axis"] == "BRepFace"


# -- a cut/intersect must PROVE it moved material ----------------------------------------------
#
# A revolve whose profile sweeps through empty air still hands back a healthy feature with a result
# body, so the feature object cannot tell a real cut from a no-op. Only the host component's solid
# volumes, sampled either side of the add, can.

def _host_bodies(*bodies):
    """Put these solid bodies in the active component, the collection the cut check samples."""
    rv.app.activeProduct.rootComponent.bRepBodies = _NamedCollection(list(bodies))


def _add_moving_volume(rf, *changes):
    """Make revolveFeatures.add apply (body, new_volume) pairs - the material effect a real
    cut/intersect has between the pre- and post-mutation reads."""
    def _move(_inp):
        for body, volume in changes:
            body.volume = volume
    rf.on_add = _move


class TestScopedParticipants:
    def test_scoped_cut_watches_the_cross_component_target_and_leaves_host_unchanged(self, wire,
                                                                                     monkeypatch):
        rf = wire([_sketch("S")])
        host = rv.app.activeProduct.rootComponent
        host_body = BRepBody("Host", volume=12.0, parent_component=host)
        _host_bodies(host_body)
        target_owner = _comp([], FakeRevFeatures(), name="TargetPart", token="TOKEN:TargetPart")
        target = BRepBody("CutMe", volume=8.0, parent_component=target_owner)
        monkeypatch.setattr(rv._TARGET_BODIES, "resolve", lambda _raw: ([target], None))
        _add_moving_volume(rf, (target, 6.5))

        out = payload(rv.handler(sketch_name="S", operation="cut",
                                 target_bodies=["TargetPart:CutMe"]))

        assert rf.participant_bodies_at_add == [target]
        assert rf.last_input.participant_set_after_extent is True
        assert target.volume == 6.5 and host_body.volume == 12.0
        assert out["volume_delta_cm3"] == -1.5
        assert out["scoped_to_bodies"] == ["TargetPart:CutMe"]
        assert "target_bodies" not in out

    def test_participant_setter_failure_never_adds_a_revolve(self, wire, monkeypatch):
        rf = wire([_sketch("S")])
        target = BRepBody("CutMe", parent_component=_comp([], FakeRevFeatures(), name="TargetPart"))
        monkeypatch.setattr(rv._TARGET_BODIES, "resolve", lambda _raw: ([target], None))
        rf.participant_error = RuntimeError("participant assignment rejected")

        res = rv.handler(sketch_name="S", operation="cut", target_bodies=["TargetPart:CutMe"])

        assert res["isError"] is True and "participant assignment rejected" in res["message"]
        assert rf.add_calls == 0


class TestCutMovesMaterial:
    def test_cut_that_moves_no_volume_is_an_error(self, wire):
        wire([_sketch("S")])
        _host_bodies(BRepBody("Bar", volume=12.0))
        res = rv.handler(sketch_name="S", operation="cut")
        assert res["isError"] is True
        assert "changed nothing" in res["message"] and "'Comp'" in res["message"]
        assert "design_delete_feature" in res["message"]

    def test_intersect_that_moves_no_volume_is_an_error(self, wire):
        wire([_sketch("S")])
        _host_bodies(BRepBody("Bar", volume=12.0))
        res = rv.handler(sketch_name="S", operation="intersect")
        assert res["isError"] is True and "this intersect changed nothing" in res["message"]

    def test_cut_that_removed_material_publishes_the_delta(self, wire):
        rf = wire([_sketch("S")])
        bar = BRepBody("Bar", volume=12.0)
        _host_bodies(bar)
        _add_moving_volume(rf, (bar, 9.5))
        out = payload(rv.handler(sketch_name="S", operation="cut"))
        assert out["volume_delta_cm3"] == -2.5      # signed: material LEFT the body

    def test_a_consumed_body_is_not_read_as_a_no_op(self, wire):
        # A body the cut consumed whole stops reporting a volume, so it contributes no delta - the
        # untouched second body's 0 must not become "nothing happened".
        rf = wire([_sketch("S")])
        eaten, kept = BRepBody("Eaten", volume=4.0), BRepBody("Kept", volume=8.0)
        _host_bodies(eaten, kept)
        _add_moving_volume(rf, (eaten, None))
        out = payload(rv.handler(sketch_name="S", operation="cut"))
        assert out["revolved"] is True

    def test_a_new_body_revolve_is_never_volume_gated(self, wire):
        # 'new' adds a body rather than moving material in an existing one; gating it on an unmoved
        # volume would fail every legitimate revolve in a component that already holds a body.
        wire([_sketch("S")])
        _host_bodies(BRepBody("Bar", volume=12.0))
        out = payload(rv.handler(sketch_name="S", operation="new"))
        assert out["revolved"] is True and "volume_delta_cm3" not in out

    def test_unreadable_volumes_neither_error_nor_publish_a_delta(self, wire):
        # Cannot measure is not "measured the same": no verdict, and no null delta that would read
        # as a measured zero.
        wire([_sketch("S")])
        _host_bodies(BRepBody("Bar", volume=None))
        out = payload(rv.handler(sketch_name="S", operation="cut"))
        assert out["revolved"] is True and "volume_delta_cm3" not in out

    def test_a_split_body_suppresses_the_incomplete_held_body_delta(self, wire):
        rf = wire([_sketch("S")])
        host = rv.app.activeProduct.rootComponent
        original = BRepBody("Body1", volume=1.0, parent_component=host)
        _host_bodies(original)

        def _split(_inp):
            original.volume = 0.874336294
            host.bRepBodies._items.append(
                BRepBody("Body2", volume=0.031415927, parent_component=host))

        rf.on_add = _split
        out = payload(rv.handler(sketch_name="S", operation="cut"))
        assert out["revolved"] is True
        assert "volume_delta_cm3" not in out
        assert "body count changed" in out["note"]


# -- a join whose result body is not one the component already held ------------------------------

class TestJoinLandedANewBody:
    def test_a_result_body_the_component_did_not_hold_before_names_model_combine(self, wire):
        wire([_sketch("S")])
        _host_bodies(BRepBody("Bar", volume=12.0))
        out = payload(rv.handler(sketch_name="S", operation="join"))
        assert "landed a NEW body (Body1)" in out["note"]
        assert "model_combine(join)" in out["note"]

    def test_a_join_that_grew_the_body_already_there_appends_nothing(self, wire):
        # the feature's result body IS the one the component held - the join fused, say nothing
        wire([_sketch("S")])
        _host_bodies(BRepBody("Body1", volume=12.0))
        out = payload(rv.handler(sketch_name="S", operation="join"))
        assert "NEW body" not in out["note"]


# -- the feature is built on the sketch's OWNING component, not the active one ------------------

class TestHostComponent:
    def test_feature_and_axis_come_from_the_sketchs_owning_component(self, wire):
        owner_rf = FakeRevFeatures()
        sketch = _sketch("Rim")
        owner = _comp([sketch], owner_rf, name="Hub")
        for key in ("x", "y", "z"):
            setattr(owner, f"{key}ConstructionAxis", ("axis", key, "Hub"))
        sketch.parentComponent = owner
        active_rf = wire([], extra_components=[owner])
        payload(rv.handler(sketch_name="Rim", axis="z"))
        # a profile handed to ANOTHER component's features collection raises 'InternalValidationError
        # : bSet', so both the feature and its origin axis must come from the sketch's owner
        assert active_rf.last_input is None and active_rf.add_calls == 0
        assert owner_rf.last_input is not None and owner_rf.add_calls == 1
        assert owner_rf.last_input.axis == ("axis", "z", "Hub")
