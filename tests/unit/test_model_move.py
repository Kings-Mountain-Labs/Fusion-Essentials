"""Unit tests for ``model_move.py`` - reposition bodies with a timeline Move feature.

Pinned: the faces refusal, the per-mode input requirements, what reaches each
MoveFeatureInput definer (cm distances, radians for an angle, the resolved axis entity, the vertex
pair), and the honesty gates - a refused definition, a feature that failed to compute, geometry that
did not move, and geometry that cannot be read back all surface as errors rather than a false ok.
"""

import math
import types

import adsk.core
import adsk.fusion

from conftest import (load_tool, make_design, make_occurrence, install, MakeComp, BRepBody,
                      BRepEdge, BRepFace, FakePoint, FakeBoundingBox3D, Line3D, Plane,
                      FakeVector3D, go_stale, payload, error_message, assert_no_active_design,
                      assert_unknown_units, _NamedCollection)

mm = load_tool("model_move")

# The active component's origin construction axes - what a world axis key resolves to.
_X_AXIS, _Y_AXIS, _Z_AXIS = object(), object(), object()


class _VI:
    """A ValueInput stand-in carrying the raw number createByReal was given."""
    def __init__(self, value=None):
        self.value = value


def _shift(target, delta, move_box=True):
    """Displace a fake's sample geometry: bounding-box corners (unless move_box is False) and every
    vertex."""
    points = []
    box = getattr(target, "boundingBox", None)
    if box is not None and move_box:
        points += [box.minPoint, box.maxPoint]
    verts = getattr(target, "vertices", None)
    for i in range(verts.count if verts is not None else 0):
        points.append(verts.item(i).geometry)
    for p in points:
        p.x, p.y, p.z = p.x + delta[0], p.y + delta[1], p.z + delta[2]


class FakeMoveFeature:
    """The created MoveFeature."""
    def __init__(self, name="Move1", health=0):
        self.name = name
        self.healthState = health
        self.errorOrWarningMessage = "the move pulls the faces off the body"


class FakeMoveInput:
    """The MoveFeatureInput: records which definer ran and the arguments it was given."""
    def __init__(self, entities, defined=True):
        self.inputEntities = entities
        self.definition = None
        self._defined = defined

    def _record(self, kind, *args):
        self.definition = (kind,) + args
        return self._defined

    def defineAsTranslateXYZ(self, x_distance, y_distance, z_distance, is_design_space):
        return self._record("translate", x_distance, y_distance, z_distance, is_design_space)

    def defineAsTranslateAlongEntity(self, linear_entity, distance):
        return self._record("along_entity", linear_entity, distance)

    def defineAsRotate(self, axis_entity, angle):
        return self._record("rotate", axis_entity, angle)

    def defineAsPointToPoint(self, origin_point, target_point):
        return self._record("point_to_point", origin_point, target_point)


class FakeMoveFeatures:
    """component.features.moveFeatures: add() displaces the sample geometry of the targets it was
    built for by `shift` (cm) and returns `feature`. `returns_nothing` models the return with NO
    feature object in it - the measured direct-mode shape, where the displacement still lands;
    `defined` False models a definer Fusion refuses."""
    def __init__(self, targets=(), shift=None, feature=None, returns_nothing=False,
                 defined=True, move_box=True):
        self.targets = list(targets)
        self.shift = shift
        self.feature = feature if feature is not None else FakeMoveFeature()
        self.returns_nothing = returns_nothing
        self.defined = defined
        self.move_box = move_box
        self.last_input = None
        self.added = 0

    def createInput2(self, entities):
        self.last_input = FakeMoveInput(entities, self.defined)
        return self.last_input

    def add(self, move_input):
        self.added += 1
        for target in self.targets:
            _shift(target, self.shift or self._definition_shift(), self.move_box)
        # The moved bodies' proxies stop answering their identity reads; boundingBox/vertices stay
        # readable, since those ARE the effect check.
        go_stale(*self.targets)
        return None if self.returns_nothing else self.feature

    def _definition_shift(self):
        """A real move carries the geometry exactly as far as the definition says; a fake that
        moves some other distance would hide a wrong-magnitude error in the handler."""
        d = self.last_input.definition
        if d[0] == "translate":
            return (d[1].value, d[2].value, d[3].value)
        if d[0] == "along_entity":
            return (d[2].value, 0.0, 0.0)
        if d[0] == "point_to_point":
            a, b = d[1].geometry, d[2].geometry
            return (b.x - a.x, b.y - a.y, b.z - a.z)
        return (1.0, 0.0, 0.0)


def _body(name="Block", span=2.0, origin=(0.0, 0.0, 0.0), vertices=()):
    """A solid body with the bounding box (and optional vertices) the handler samples."""
    x, y, z = origin
    return BRepBody(name=name, volume=span ** 3, is_solid=True, entity_token=name,
                    bbox=FakeBoundingBox3D(FakePoint(x, y, z),
                                           FakePoint(x + span, y + span, z + span)),
                    vertices=[FakePoint(*v) for v in vertices])


def _face(token="f1"):
    return BRepFace(Plane(FakeVector3D(0, 0, 1)), area=4.0, body_name="Block",
                    entity_token=token)


def _edge(token="e1"):
    return BRepEdge(Line3D(FakePoint(0, 0, 0), FakePoint(10, 0, 0)), entity_token=token)


def _wire(monkeypatch, bodies=(), feats=None, tokens=None, axes=True, design_type=None):
    """Install a design whose active component carries `feats` (features.moveFeatures) and the
    origin construction axes a world axis key resolves to, with the BRep types and ValueInput
    modelled.

    `design_type` sets the modelling mode current_design_type reads (1 parametric, 0 direct); left
    unset the design reports neither, which is the 'unknown' mode."""
    comp = MakeComp(name="Comp", bodies=list(bodies),
                    construction_axes=(_X_AXIS, _Y_AXIS, _Z_AXIS) if axes else None)
    feats = feats if feats is not None else FakeMoveFeatures(bodies)
    comp.features = types.SimpleNamespace(moveFeatures=feats)
    design = make_design(comp=comp, tokens=tokens)
    if design_type is not None:
        design.designType = design_type
    install(mm, design)
    monkeypatch.setattr(adsk.fusion, "BRepBody", BRepBody)
    monkeypatch.setattr(adsk.fusion, "BRepFace", BRepFace)
    monkeypatch.setattr(adsk.fusion, "BRepEdge", BRepEdge)
    # AxisRef's isinstance ladder walks SketchLine before it reaches the face branch.
    monkeypatch.setattr(adsk.fusion, "SketchLine", type("SL", (), {}), raising=False)
    monkeypatch.setattr(adsk.core.ValueInput, "createByReal", staticmethod(lambda v: _VI(value=v)))
    return feats


def _points(monkeypatch, start=(0.0, 0.0, 0.0), end=(4.0, 0.0, 0.0), rides=None):
    """The two BRepVertex stand-ins a point_to_point call resolves. `rides` is the moving body whose
    sample geometry the from_point belongs to - the normal case, since the vertex you move FROM sits
    on the body being moved and therefore travels with it."""
    first = types.SimpleNamespace(geometry=FakePoint(*start))
    second = types.SimpleNamespace(geometry=FakePoint(*end))
    if rides is not None:
        rides.vertices._items.append(types.SimpleNamespace(geometry=first.geometry))
    monkeypatch.setattr(mm._FROM, "resolve", lambda raw: (first, None))
    monkeypatch.setattr(mm._TO, "resolve", lambda raw: (second, None))
    return first, second



# -- a body in a SUB-COMPONENT: the move hosts there, and the axis is proxied ------------------
#
# MEASURED: a move on a sub-component body needs BOTH - hosting the feature on the active component
# raises "object is not in the assembly context of this component" at add(), and a native origin
# axis on the owning component raises "3 : Invalid entity" at defineAs. The occurrence the axis is
# proxied into comes from _inputs.single_placement, so a component placed SEVERAL times is refused
# naming each path instead of resolving to its first instance.

def _sub_component(name="Rail", bodies=(), feats=None, x_axis=_X_AXIS):
    """A sub-component carrying its OWN moveFeatures collection and origin axes - what _host_for
    has to route to when the moved body lives there. `x_axis` is the entity the x key resolves to,
    so a test can hand one that proxies (or one that will not)."""
    comp = MakeComp(name=name, bodies=list(bodies), entity_token="TOKEN:" + name,
                    construction_axes=(x_axis, _Y_AXIS, _Z_AXIS))
    comp.features = types.SimpleNamespace(
        moveFeatures=feats if feats is not None else FakeMoveFeatures(bodies))
    return comp


class _Proxyable:
    """An origin ConstructionAxis that answers createForAssemblyContext, recording which occurrence
    it was lifted into - the read that proves the axis reached the feature in the moved body's
    assembly context rather than natively."""
    def __init__(self):
        self.proxied_into = []

    def createForAssemblyContext(self, occurrence):
        self.proxied_into.append(occurrence.fullPathName)
        return ("PROXY", occurrence.fullPathName)


def _wire_sub(monkeypatch, sub, *placements):
    """A root design in which `sub` is placed under the given occurrence fullPathNames."""
    root = MakeComp(name="Root")
    root.entityToken = "TOKEN:Root"
    root.features = types.SimpleNamespace(moveFeatures=FakeMoveFeatures())
    occs = [make_occurrence(path=p) for p in placements]
    root.allOccurrencesByComponent = lambda comp: _NamedCollection(occs)
    design = make_design(comp=root)
    install(mm, design)
    monkeypatch.setattr(adsk.fusion, "BRepBody", BRepBody)
    monkeypatch.setattr(adsk.fusion, "BRepFace", BRepFace)
    monkeypatch.setattr(adsk.fusion, "BRepEdge", BRepEdge)
    monkeypatch.setattr(adsk.fusion, "SketchLine", type("SL", (), {}), raising=False)
    monkeypatch.setattr(adsk.core.ValueInput, "createByReal", staticmethod(lambda v: _VI(value=v)))
    return design


class TestSubComponentHosting:
    def test_the_move_is_hosted_on_the_bodys_own_component_and_the_axis_is_proxied(self, monkeypatch):
        body = _body("Slug")
        sub_feats = FakeMoveFeatures([body])
        axis = _Proxyable()
        sub = _sub_component(bodies=[body], feats=sub_feats, x_axis=axis)
        body.parentComponent = sub
        _wire_sub(monkeypatch, sub, "Assy:1+Rail:1")
        monkeypatch.setattr(mm._BODIES, "resolve", lambda raw: ([body], None))
        out = payload(mm.handler(mode="along_entity", bodies=["Slug"], axis="x", distance=30))
        assert sub_feats.added == 1                      # the SUB-component built the feature
        assert axis.proxied_into == ["Assy:1+Rail:1"]    # into the one occurrence that places it
        assert sub_feats.last_input.definition[1] == ("PROXY", "Assy:1+Rail:1")
        assert out["moved"] is True

    def test_a_component_placed_twice_is_refused_naming_each_path(self, monkeypatch):
        # the first-match pick this replaces would have proxied the axis into Rail:1 and aimed the
        # move somewhere the caller never asked for, with the displacement check still passing
        body = _body("Slug")
        sub_feats = FakeMoveFeatures([body])
        sub = _sub_component(bodies=[body], feats=sub_feats)
        body.parentComponent = sub
        _wire_sub(monkeypatch, sub, "Assy:1+Rail:1", "Assy:1+Rail:2")
        monkeypatch.setattr(mm._BODIES, "resolve", lambda raw: ([body], None))
        res = mm.handler(mode="along_entity", bodies=["Slug"], axis="x", distance=30)
        assert res["isError"] is True
        msg = error_message(res)
        assert "placed 2 times" in msg
        assert "Assy:1+Rail:1" in msg and "Assy:1+Rail:2" in msg
        assert sub_feats.added == 0                      # refused before any feature transaction

    def test_an_axis_that_will_not_proxy_is_refused_not_handed_over_native(self, monkeypatch):
        # The lift exists because the NATIVE origin axis is what defineAs refuses ("3 : Invalid
        # entity") in the moved body's context - so a createForAssemblyContext that hands back
        # nothing has nothing safe to fall back to. Falling back to `ent` passes that very entity on
        # and the call fails inside the API with nothing pointing at why.
        body = _body("Slug")
        sub_feats = FakeMoveFeatures([body])
        # an x axis that answers no createForAssemblyContext
        sub = _sub_component(bodies=[body], feats=sub_feats, x_axis=object())
        body.parentComponent = sub
        _wire_sub(monkeypatch, sub, "Assy:1+Rail:1")
        monkeypatch.setattr(mm._BODIES, "resolve", lambda raw: ([body], None))
        res = mm.handler(mode="along_entity", bodies=["Slug"], axis="x", distance=30)
        assert res["isError"] is True
        msg = error_message(res)
        assert "x origin construction axis" in msg
        assert "Assy:1+Rail:1" in msg                # the occurrence it would not proxy into
        assert sub_feats.added == 0                  # refused before any feature transaction

    def test_an_unplaced_sub_component_is_refused(self, monkeypatch):
        body = _body("Slug")
        sub_feats = FakeMoveFeatures([body])
        sub = _sub_component(bodies=[body], feats=sub_feats)
        body.parentComponent = sub
        _wire_sub(monkeypatch, sub)                       # no occurrences at all
        monkeypatch.setattr(mm._BODIES, "resolve", lambda raw: ([body], None))
        res = mm.handler(mode="along_entity", bodies=["Slug"], axis="x", distance=30)
        assert res["isError"] is True
        assert "not placed in the assembly" in error_message(res)
        assert sub_feats.added == 0

    def test_a_body_that_is_already_a_proxy_uses_its_own_context(self, monkeypatch):
        # a proxy body names its instance already, so no placement lookup runs - and a component
        # placed twice must NOT be refused on a reference that is unambiguous
        body = _body("Slug")
        sub_feats = FakeMoveFeatures([body])
        axis = _Proxyable()
        sub = _sub_component(bodies=[body], feats=sub_feats, x_axis=axis)
        body.parentComponent = sub
        body.assemblyContext = make_occurrence(path="Assy:1+Rail:2")
        _wire_sub(monkeypatch, sub, "Assy:1+Rail:1", "Assy:1+Rail:2")
        monkeypatch.setattr(mm._BODIES, "resolve", lambda raw: ([body], None))
        out = payload(mm.handler(mode="along_entity", bodies=["Slug"], axis="x", distance=30))
        assert axis.proxied_into == ["Assy:1+Rail:2"]
        assert out["moved"] is True


# -- an axis HANDLE native to another component takes the same lift the body took ---------------

def _foreign_edge(owner, token="e9", proxy=BRepEdge._UNSET):
    """A straight edge whose BODY belongs to `owner` and which is NOT already a proxy; `proxy` is
    what its createForAssemblyContext hands back."""
    edge = BRepEdge(Line3D(FakePoint(0, 0, 0), FakePoint(10, 0, 0)), entity_token=token,
                    assembly_proxy=proxy)
    edge.assemblyContext = None
    edge.body = BRepBody(name="RailBody", parent_component=owner)
    return edge


class TestForeignAxisHandle:
    def _root_design(self, monkeypatch, body, edge, owner, *placements):
        root = MakeComp(name="Root", bodies=[body])
        root.entityToken = "TOKEN:Root"
        feats = FakeMoveFeatures([body])
        root.features = types.SimpleNamespace(moveFeatures=feats)
        occs = [make_occurrence(path=p) for p in placements]
        root.allOccurrencesByComponent = lambda comp: _NamedCollection(occs)
        design = make_design(comp=root, tokens={"e9": edge})
        install(mm, design)
        monkeypatch.setattr(adsk.fusion, "BRepBody", BRepBody)
        monkeypatch.setattr(adsk.fusion, "BRepFace", BRepFace)
        monkeypatch.setattr(adsk.fusion, "BRepEdge", BRepEdge)
        monkeypatch.setattr(adsk.fusion, "SketchLine", type("SL", (), {}), raising=False)
        monkeypatch.setattr(adsk.core.ValueInput, "createByReal",
                            staticmethod(lambda v: _VI(value=v)))
        return feats

    def test_a_native_foreign_axis_handle_is_proxied_into_its_one_occurrence(self, monkeypatch):
        # the entity a move consumes has to be reachable in the hosting component's context,
        # whichever input it arrived on - handing the NATIVE edge through is the measured
        # "3 : Invalid entity" at defineAs
        body, owner = _body("Block"), MakeComp(name="Rail")
        owner.entityToken = "TOKEN:Rail"
        proxy = object()
        edge = _foreign_edge(owner, proxy=proxy)
        feats = self._root_design(monkeypatch, body, edge, owner, "Assy:1+Rail:1")
        out = payload(mm.handler(mode="along_entity", bodies=["Block"], axis="e9", distance=30))
        assert feats.last_input.definition[1] is proxy
        assert out["moved"] is True

    def test_a_foreign_axis_handle_from_a_twice_placed_component_is_refused(self, monkeypatch):
        body, owner = _body("Block"), MakeComp(name="Rail")
        owner.entityToken = "TOKEN:Rail"
        edge = _foreign_edge(owner, proxy=object())
        feats = self._root_design(monkeypatch, body, edge, owner,
                                  "Assy:1+Rail:1", "Assy:1+Rail:2")
        res = mm.handler(mode="along_entity", bodies=["Block"], axis="e9", distance=30)
        assert res["isError"] is True
        msg = error_message(res)
        assert "placed 2 times" in msg and "Assy:1+Rail:2" in msg
        assert feats.added == 0


class TestSelection:
    def test_faces_are_refused_and_point_at_the_tool_that_works(self, monkeypatch):
        # createInput2's binding doc offers BRepFace, but the kernel fails on one
        _wire(monkeypatch, [_body()])
        msg = error_message(mm.handler(faces=["f1"], dz=10))
        assert "cannot move FACES" in msg and "model_offset_face" in msg

    def test_faces_are_refused_even_alongside_bodies(self, monkeypatch):
        _wire(monkeypatch, [_body()])
        msg = error_message(mm.handler(bodies=["Block"], faces=["f1"], dx=5))
        assert "cannot move FACES" in msg

    def test_no_bodies_is_refused(self, monkeypatch):
        _wire(monkeypatch, [_body()])
        assert "'bodies' is required" in error_message(mm.handler(dx=5))

    def test_every_resolved_body_reaches_the_collection(self, monkeypatch):
        a, b = _body(name="A"), _body(name="B", origin=(10, 0, 0))
        feats = _wire(monkeypatch, [a, b], FakeMoveFeatures([a, b]))
        out = payload(mm.handler(bodies=["A", "B"], dx=5))
        assert out["bodies"] == ["A", "B"]
        assert feats.last_input.inputEntities.count == 2

    def test_a_mesh_body_is_refused_by_the_brep_kind(self, monkeypatch):
        mesh = object()
        _wire(monkeypatch, [_body()], tokens={"m1": mesh})
        monkeypatch.setattr(adsk.fusion, "MeshBody", type(mesh))
        msg = error_message(mm.handler(bodies=["m1"], dx=5))
        assert "MESH" in msg and "mesh_to_brep" in msg



class TestModeGuards:
    def test_unknown_mode_lists_the_modes(self, monkeypatch):
        _wire(monkeypatch, [_body()])
        msg = error_message(mm.handler(mode="teleport", bodies=["Block"], dx=5))
        assert "teleport" in msg and "point_to_point" in msg

    def test_translate_with_no_offset_is_refused(self, monkeypatch):
        feats = _wire(monkeypatch, [_body()])
        msg = error_message(mm.handler(bodies=["Block"]))
        assert "'dx'" in msg and "'dy'" in msg and "'dz'" in msg
        assert feats.last_input is None

    def test_translate_offset_that_is_not_a_number_is_refused_by_name(self, monkeypatch):
        _wire(monkeypatch, [_body()])
        msg = error_message(mm.handler(bodies=["Block"], dy="sideways"))
        assert "'dy'" in msg and "must be a number" in msg

    def test_along_entity_without_an_axis_is_refused(self, monkeypatch):
        feats = _wire(monkeypatch, [_body()])
        msg = error_message(mm.handler(mode="along_entity", bodies=["Block"], distance=5))
        assert "'axis'" in msg and "along_entity" in msg
        assert feats.last_input is None

    def test_along_entity_without_a_distance_is_refused(self, monkeypatch):
        _wire(monkeypatch, [_body()])
        msg = error_message(mm.handler(mode="along_entity", bodies=["Block"], axis="x"))
        assert "'distance' is required" in msg

    def test_along_entity_with_a_zero_distance_is_refused(self, monkeypatch):
        _wire(monkeypatch, [_body()])
        msg = error_message(mm.handler(mode="along_entity", bodies=["Block"], axis="x",
                                       distance=0))
        assert "'distance'" in msg and "non-zero" in msg

    def test_rotate_without_an_axis_is_refused(self, monkeypatch):
        _wire(monkeypatch, [_body()])
        msg = error_message(mm.handler(mode="rotate", bodies=["Block"], angle_deg=30))
        assert "'axis'" in msg and "rotate" in msg

    def test_rotate_without_an_angle_is_refused(self, monkeypatch):
        _wire(monkeypatch, [_body()])
        msg = error_message(mm.handler(mode="rotate", bodies=["Block"], axis="z"))
        assert "'angle_deg'" in msg and "degrees" in msg

    def test_rotate_by_zero_degrees_is_refused(self, monkeypatch):
        feats = _wire(monkeypatch, [_body()])
        msg = error_message(mm.handler(mode="rotate", bodies=["Block"], axis="z", angle_deg=0))
        assert "non-zero" in msg
        assert feats.last_input is None

    def test_rotate_with_a_non_numeric_angle_is_refused(self, monkeypatch):
        _wire(monkeypatch, [_body()])
        msg = error_message(mm.handler(mode="rotate", bodies=["Block"], axis="z",
                                       angle_deg="thirty"))
        assert "'angle_deg'" in msg and "thirty" in msg

    def test_point_to_point_names_every_missing_point(self, monkeypatch):
        _wire(monkeypatch, [_body()])
        msg = error_message(mm.handler(mode="point_to_point", bodies=["Block"]))
        assert "from_point" in msg and "to_point" in msg and "find_geometry" in msg

    def test_point_to_point_names_only_the_missing_point(self, monkeypatch):
        _wire(monkeypatch, [_body()])
        _points(monkeypatch)
        msg = error_message(mm.handler(mode="point_to_point", bodies=["Block"], from_point="v1"))
        assert "to_point" in msg and "from_point" not in msg

    def test_unknown_units(self, monkeypatch):
        _wire(monkeypatch, [_body()])
        assert_unknown_units(mm.handler, bodies=["Block"], dx=5)

    def test_no_active_design(self, monkeypatch):
        _wire(monkeypatch, [_body()])
        assert_no_active_design(mm, mm.handler, bodies=["Block"], dx=5)



class TestDefiners:
    def test_translate_passes_cm_offsets_in_design_space(self, monkeypatch):
        body = _body()
        feats = _wire(monkeypatch, [body], FakeMoveFeatures([body]))
        payload(mm.handler(bodies=["Block"], dx=5, dy=-2, dz=0))
        kind, x, y, z, design_space = feats.last_input.definition
        assert kind == "translate"
        assert [x.value, y.value, z.value] == [0.5, -0.2, 0.0]
        assert design_space is True

    def test_translate_scales_by_the_chosen_units(self, monkeypatch):
        body = _body()
        feats = _wire(monkeypatch, [body], FakeMoveFeatures([body]))
        payload(mm.handler(bodies=["Block"], dx=5, units="cm"))
        assert feats.last_input.definition[1].value == 5.0

    def test_along_entity_resolves_a_world_axis_to_its_construction_axis(self, monkeypatch):
        body = _body()
        feats = _wire(monkeypatch, [body], FakeMoveFeatures([body]))
        payload(mm.handler(mode="along_entity", bodies=["Block"], axis="y", distance=30))
        kind, entity, distance = feats.last_input.definition
        assert kind == "along_entity"
        assert entity is _Y_AXIS
        assert distance.value == 3.0

    def test_along_entity_passes_an_edge_handle_straight_through(self, monkeypatch):
        body, edge = _body(), _edge()
        feats = _wire(monkeypatch, [body], FakeMoveFeatures([body]),
                      tokens={"Block": body, "e1": edge})
        payload(mm.handler(mode="along_entity", bodies=["Block"], axis="e1", distance=10))
        assert feats.last_input.definition[1] is edge

    def test_a_face_handle_is_refused_as_an_axis(self, monkeypatch):
        body, face = _body(), _face()
        feats = _wire(monkeypatch, [body], FakeMoveFeatures([body]), tokens={"f1": face})
        msg = error_message(mm.handler(mode="along_entity", bodies=["Block"], axis="f1",
                                       distance=10))
        assert "'axis'" in msg and "linear ENTITY" in msg
        assert feats.last_input is None

    def test_a_missing_construction_axis_is_reported(self, monkeypatch):
        _wire(monkeypatch, [_body()], axes=False)
        msg = error_message(mm.handler(mode="along_entity", bodies=["Block"], axis="x",
                                       distance=10))
        assert "'axis'" in msg and "origin construction axis" in msg

    def test_rotate_converts_degrees_to_radians(self, monkeypatch):
        body = _body(vertices=[(1.0, 0.0, 0.0)])
        feats = _wire(monkeypatch, [body], FakeMoveFeatures([body]))
        payload(mm.handler(mode="rotate", bodies=["Block"], axis="z", angle_deg=90))
        kind, entity, angle = feats.last_input.definition
        assert kind == "rotate"
        assert entity is _Z_AXIS
        assert abs(angle.value - math.radians(90)) < 1e-12

    def test_point_to_point_passes_both_resolved_vertices(self, monkeypatch):
        body = _body()
        feats = _wire(monkeypatch, [body], FakeMoveFeatures([body]))
        first, second = _points(monkeypatch)
        payload(mm.handler(mode="point_to_point", bodies=["Block"], from_point="v1",
                           to_point="v2"))
        assert feats.last_input.definition == ("point_to_point", first, second)



class TestHonesty:
    def test_a_refused_definition_never_reaches_add(self, monkeypatch):
        body = _body()
        feats = _wire(monkeypatch, [body], FakeMoveFeatures([body], defined=False))
        msg = error_message(mm.handler(bodies=["Block"], dx=5))
        assert "refused" in msg and "nothing was moved" in msg
        assert feats.added == 0

    def test_no_feature_returned_is_error(self, monkeypatch):
        body = _body()
        _wire(monkeypatch, [body], FakeMoveFeatures([body], returns_nothing=True))
        res = mm.handler(bodies=["Block"], dx=5)
        assert res["isError"] is True and "no feature" in res["message"].lower()

    def test_health_error_reported_not_false_ok(self, monkeypatch):
        body = _body()
        _wire(monkeypatch, [body], FakeMoveFeatures([body], feature=FakeMoveFeature(health=2)))
        res = mm.handler(bodies=["Block"], dx=5)
        assert res["isError"] is True
        assert "failed to compute" in res["message"]
        assert "pulls the faces off the body" in res["message"]

    def test_add_raising_surfaces_as_error(self, monkeypatch):
        body = _body()
        feats = FakeMoveFeatures([body])
        feats.add = lambda inp: (_ for _ in ()).throw(RuntimeError("the move breaks the solid"))
        _wire(monkeypatch, [body], feats)
        res = mm.handler(bodies=["Block"], dx=5)
        assert res["isError"] is True and "Move failed" in res["message"]
        assert "breaks the solid" in res["message"]


# ── DIRECT mode: moveFeatures.add returns nothing while the translate LANDS (measured) ───────

class TestDirectModeNoFeature:
    def test_direct_none_with_the_measured_displacement_is_ok(self, monkeypatch):
        body = _body()
        _wire(monkeypatch, [body], FakeMoveFeatures([body], returns_nothing=True), design_type=0)
        out = payload(mm.handler(bodies=["Block"], dx=5))
        assert out["moved"] is True and out["displacement"] == 5.0

    def test_direct_none_publishes_no_feature_name(self, monkeypatch):
        # No feature object exists, so no name may be echoed - and the timeline claim goes with it.
        body = _body()
        _wire(monkeypatch, [body], FakeMoveFeatures([body], returns_nothing=True), design_type=0)
        out = payload(mm.handler(bodies=["Block"], dx=5))
        assert "feature" not in out
        assert out["no_timeline_feature"] is True
        assert "DIRECT mode" in out["note"] and "replays on every recompute" not in out["note"]

    def test_direct_none_names_the_bodies_captured_before_the_move(self, monkeypatch):
        # The proxies stop answering .name once the move ran; the payload must still name them.
        body = _body()
        _wire(monkeypatch, [body], FakeMoveFeatures([body], returns_nothing=True), design_type=0)
        out = payload(mm.handler(bodies=["Block"], dx=5))
        assert out["bodies"] == ["Block"]

    def test_declared_outputs_hold_on_the_direct_path(self, monkeypatch):
        body = _body()
        _wire(monkeypatch, [body], FakeMoveFeatures([body], returns_nothing=True), design_type=0)
        out = payload(mm.handler(bodies=["Block"], dx=5))
        for o in mm.RETURNS:
            assert o.assert_present(out) == "", o.key

    def test_direct_none_with_a_failed_effect_check_is_an_error(self, monkeypatch):
        # add() handed back nothing AND no sample point moved: not a success.
        body = _body()
        _wire(monkeypatch, [body], FakeMoveFeatures([body], returns_nothing=True, move_box=False),
              design_type=0)
        res = mm.handler(bodies=["Block"], dx=5)
        assert res["isError"] is True and "sits exactly where it was" in res["message"]
        # There is no timeline feature on this path - the remediation must not name one.
        assert "design_delete_feature" not in res["message"]
        assert "undo in Fusion" in res["message"]

    def test_parametric_none_stays_an_error(self, monkeypatch):
        # Even though the geometry did move: a None feature in a PARAMETRIC design is unmeasured as
        # a success, so it is refused.
        body = _body()
        _wire(monkeypatch, [body], FakeMoveFeatures([body], returns_nothing=True), design_type=1)
        res = mm.handler(bodies=["Block"], dx=5)
        assert res["isError"] is True and "returned no feature" in res["message"]
        assert "DIRECT mode" not in res["message"]

    def test_geometry_that_did_not_move_is_an_error(self, monkeypatch):
        body = _body()
        _wire(monkeypatch, [body], FakeMoveFeatures([body], shift=(0.0, 0.0, 0.0)))
        res = mm.handler(bodies=["Block"], dx=5)
        assert res["isError"] is True
        assert "exactly where it was" in res["message"]
        # a parametric design DOES leave a timeline feature, so the remedy names it
        assert "remains in the timeline" in res["message"]
        assert "design_delete_feature" in res["message"]

    def test_unreadable_geometry_fails_closed(self, monkeypatch):
        body = _body()
        _wire(monkeypatch, [body], FakeMoveFeatures([body]))
        monkeypatch.setattr(mm, "_body_points", lambda b: None)
        res = mm.handler(bodies=["Block"], dx=5)
        assert res["isError"] is True
        assert "could not be verified" in res["message"] and "model_inspect" in res["message"]

    def test_a_rotation_is_verified_by_the_vertices_when_the_box_holds_still(self, monkeypatch):
        # A rotation can map a symmetric body's bounding box onto itself; the vertex samples are
        # what still register the move.
        body = _body(vertices=[(1.0, 0.0, 0.0)])
        _wire(monkeypatch, [body], FakeMoveFeatures([body], shift=(0.0, 1.0, 0.0), move_box=False))
        out = payload(mm.handler(mode="rotate", bodies=["Block"], axis="z", angle_deg=90))
        assert out["displacement"] == 10.0        # 1 cm, reported in the default mm

    def test_displacement_is_reported_in_the_chosen_units(self, monkeypatch):
        body = _body()
        _wire(monkeypatch, [body], FakeMoveFeatures([body]))
        out = payload(mm.handler(bodies=["Block"], dx=2.5, units="cm"))
        assert out["displacement"] == 2.5
        assert out["units"] == "cm"
        assert out["mode"] == "translate"
        assert out["feature"] == "Move1"
        assert out["moved"] is True



class TestOutputContract:
    def test_declared_outputs_are_minted(self, monkeypatch):
        body = _body()
        _wire(monkeypatch, [body], FakeMoveFeatures([body]))
        out = payload(mm.handler(bodies=["Block"], dx=5))
        for o in mm.RETURNS:
            assert o.assert_present(out) == "", o.key


class TestExpectedMagnitude:
    def test_a_move_that_travelled_the_wrong_distance_is_error(self, monkeypatch):
        # a wrong-magnitude or unit error travels a different distance while the API still reports
        # success. A sign or frame error travels the SAME distance and this gate cannot see it.
        body = _body()
        _wire(monkeypatch, [body], FakeMoveFeatures([body], shift=(7.0, 0.0, 0.0)))
        res = mm.handler(bodies=["Block"], dx=30, units="mm")
        assert res["isError"] is True
        assert "not the 30.0 mm requested" in res["message"]

    def test_along_entity_checks_its_distance(self, monkeypatch):
        body = _body()
        _wire(monkeypatch, [body], FakeMoveFeatures([body], shift=(0.25, 0.0, 0.0)))
        res = mm.handler(mode="along_entity", bodies=["Block"], axis="x", distance=10, units="mm")
        assert res["isError"] is True and "not the 10.0 mm requested" in res["message"]

    def test_point_to_point_checks_the_vertex_separation(self, monkeypatch):
        body = _body()
        _wire(monkeypatch, [body], FakeMoveFeatures([body], shift=(1.0, 0.0, 0.0)))
        _points(monkeypatch, start=(0.0, 0.0, 0.0), end=(4.0, 0.0, 0.0))
        res = mm.handler(mode="point_to_point", bodies=["Block"], from_point="v1", to_point="v2")
        assert res["isError"] is True and "not the 40.0 mm requested" in res["message"]

    def test_a_rotation_has_no_expected_magnitude_to_check(self, monkeypatch):
        body = _body(vertices=[(0.0, 0.0, 0.0)])
        _wire(monkeypatch, [body], FakeMoveFeatures([body], shift=(3.0, 0.0, 0.0)))
        out = payload(mm.handler(mode="rotate", bodies=["Block"], axis="z", angle_deg=90))
        assert out["displacement"] == 30.0

    def test_a_sub_epsilon_shift_is_still_nothing_moved(self, monkeypatch):
        # pins _MOVE_EPS_CM from BELOW: dropping it to 0 would let this pass as a real move
        body = _body()
        _wire(monkeypatch, [body], FakeMoveFeatures([body], shift=(5e-8, 0.0, 0.0)))
        res = mm.handler(bodies=["Block"], dx=5e-8, units="cm")
        assert res["isError"] is True and "exactly where it was" in res["message"]


class TestPointToPointExpectedIsCapturedBeforeTheMove:
    def test_a_from_point_that_rides_the_body_still_verifies(self, monkeypatch):
        # the from_point vertex sits ON the moving body, so after add() it has closed on the
        # stationary to_point; the expected travel has to come from the pre-move separation
        body = _body(vertices=[(0.0, 0.0, 0.0)])
        _wire(monkeypatch, [body], FakeMoveFeatures([body]))
        _points(monkeypatch, start=(0.0, 0.0, 0.0), end=(4.0, 0.0, 0.0), rides=body)
        out = payload(mm.handler(mode="point_to_point", bodies=["Block"],
                                 from_point="v1", to_point="v2"))
        assert out["displacement"] == 40.0

    def test_unreadable_vertices_fall_back_to_the_displacement_check(self, monkeypatch):
        # no expected magnitude is knowable, so the move is judged only on having moved - not
        # failed for missing a target that could not be computed
        body = _body()
        _wire(monkeypatch, [body], FakeMoveFeatures([body], shift=(1.0, 0.0, 0.0)))
        blind = types.SimpleNamespace(geometry=None)
        monkeypatch.setattr(mm._FROM, "resolve", lambda raw: (blind, None))
        monkeypatch.setattr(mm._TO, "resolve", lambda raw: (blind, None))
        out = payload(mm.handler(mode="point_to_point", bodies=["Block"],
                                 from_point="v1", to_point="v2"))
        assert out["displacement"] == 10.0


class TestMoveToleranceIsPinned:
    def test_a_miss_just_over_the_tolerance_errors(self, monkeypatch):
        body = _body()
        over = 3.0 + mm._MOVE_TOL_CM * 2
        _wire(monkeypatch, [body], FakeMoveFeatures([body], shift=(over, 0.0, 0.0)))
        res = mm.handler(bodies=["Block"], dx=30, units="mm")
        assert res["isError"] is True and "not the 30.0 mm requested" in res["message"]

    def test_a_miss_just_under_the_tolerance_passes(self, monkeypatch):
        body = _body()
        under = 3.0 + mm._MOVE_TOL_CM / 2
        _wire(monkeypatch, [body], FakeMoveFeatures([body], shift=(under, 0.0, 0.0)))
        assert payload(mm.handler(bodies=["Block"], dx=30, units="mm"))["moved"] is True
