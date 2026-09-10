"""Unit tests for ``joint_at_geometry.py`` — joint two parts at geometry handles.

The VALUE of this tool is the baked-in runtime rules, so that's what's pinned: `_joint_geometry_for`
must pick a VALID keypoint by entity kind — a cylinder/cone face uses MiddleKeyPoint (CenterKeyPoint
is invalid on a cylinder/cone face), a sphere/torus face uses CenterKeyPoint (MiddleKeyPoint is the
one the API refuses there), a planar face uses CenterKeyPoint, a circular edge uses center,
a vertex uses createByPoint. Plus the motion mapping and the handle-resolution guards. The geometry
construction is captured on fakes so we assert which JointGeometry factory + keypoint were used,
without a live design.
"""

from types import SimpleNamespace

import adsk.core
import adsk.fusion
import pytest

from conftest import (BRepBody, BRepEdge, BRepFace, Circle3D, Cone, Cylinder, FakeJoint,
                      FakeJointInput, FakeJoints, FakeMatrix3D, FakePoint, FakeTimelineObject,
                      FakeVector3D, Line3D, MakeComp, Plane, Torus, _NamedCollection, _Vertex,
                      install, load_tool, make_design, make_occurrence, payload as _payload)

jg = load_tool("joint_at_geometry")

# Measured enum shorthands (seeded from live_api_facts) - the fakes and assertions speak these.
_ST = adsk.core.SurfaceTypes
_CT = adsk.core.Curve3DTypes
_KP = adsk.fusion.JointKeyPointTypes
_JD = adsk.fusion.JointDirections
_FHS = adsk.fusion.FeatureHealthStates


# ── fakes for the JointGeometry factory + keypoint enum ─────────────────────

class _Recorder:
    """Records which JointGeometry factory was called with which keypoint."""
    def __init__(self):
        self.calls = []
    def createByNonPlanarFace(self, face, kp):
        self.calls.append(("nonplanar", kp)); return ("geo", "nonplanar", kp)
    def createByPlanarFace(self, face, edge, kp):
        self.calls.append(("planar", kp)); return ("geo", "planar", kp)
    def createByCurve(self, edge, kp):
        self.calls.append(("curve", kp)); return ("geo", "curve", kp)
    def createByPoint(self, pt):
        self.calls.append(("point", None)); return ("geo", "point", None)


# The verbatim message createByNonPlanarFace raises when the keypoint is wrong for the face type.
_KEYPOINT_RAISE = "3 : Key point type should be CenterKeyPoint, if the face is sphere and torus face"


class _RaisingRecorder(_Recorder):
    """createByNonPlanarFace raises the way the live API does for a keypoint a face type refuses."""
    def createByNonPlanarFace(self, face, kp):
        self.calls.append(("nonplanar", kp))
        raise RuntimeError(_KEYPOINT_RAISE)


# The rigs place their components with conftest's shared FakeMatrix3D: a rotation of N degrees about
# Z followed by a translation - the composed component-to-world matrix Occurrence.transform2 reads.
# 90 degrees about Z is the rotation the measured nested rig places its parent with.
_ROT90Z_DEG = 90.0


class _OriginRecorder(_Recorder):
    """createByNonPlanarFace hands back a JointGeometry carrying an ORIGIN - the only signal the
    torus base-feature trap gives (the call itself reports success). The origin is WORLD-framed,
    as measured for both a native face and an assembly proxy."""
    def __init__(self, origin):
        super().__init__()
        self._origin = origin
    def createByNonPlanarFace(self, face, kp):
        self.calls.append(("nonplanar", kp))
        return type("JG", (), {"origin": FakePoint(*self._origin)})()


# The entity kinds are conftest's shared fakes - BRepFace, BRepEdge, _Vertex, the surface/curve
# fakes and FakePoint. _install below points the handler's isinstance() checks at them.

def _surface(surface_type, normal):
    """The surface object a face of `surface_type` carries. conftest's shared surface fakes hold the
    measured surfaceType intrinsically, so a face's kind is named once here. A sphere and a NURBS
    surface have no measured shape dump, so no shared fake carries their surfaceType and the type is
    named directly. A planar face's plane holds the same normal its evaluator samples."""
    if surface_type == _ST.PlaneSurfaceType:
        return Plane(FakeVector3D(*normal))
    if surface_type == _ST.CylinderSurfaceType:
        return Cylinder(FakeVector3D(0.0, 0.0, 1.0))
    if surface_type == _ST.ConeSurfaceType:
        return Cone(FakeVector3D(0.0, 0.0, 1.0))
    if surface_type == _ST.TorusSurfaceType:
        return Torus()
    return SimpleNamespace(surfaceType=surface_type)


def _face(surface_type, origin=None, context=None, component=None, normal=(0.0, 0.0, 1.0),
          point_on_face=None):
    """A face on conftest's shared BRepFace fake. `origin` is the surface geometry's centre AS THAT
    FACE REPORTS IT - which frame that is follows `context`, live-measured on a torus centred at
    component-local (0, 0, -1) in a component turned 30 deg and placed 8 cm out: the NATIVE face
    reads (0, 0, -1), the assembly PROXY reads (8, 0, -1). `context` is the occurrence an assembly
    proxy carries (None = a native face); `component` is the owning body's component, which supplies
    a native face's placement. `normal` is what the surface evaluator samples, which only a face
    carrying `point_on_face` is asked for."""
    surface = _surface(surface_type, normal)
    if origin is not None:
        surface.origin = FakePoint(*origin)
    body = BRepBody(parent_component=component) if component is not None else None
    return BRepFace(surface, assembly_context=context, body=body,
                    normal=FakeVector3D(*normal), point_on_face=point_on_face)


def _comp(name, token):
    """One component as a FRESH wrapper. Live, two references to one component are DISTINCT objects
    sharing one entityToken, so a fixture handing the same object to both sides of a same-component
    test cannot tell an identity compare from the token compare the placement ladder makes."""
    return MakeComp(name=name, entity_token=token)


def _placed(matrix, component=None):
    """An occurrence: transform2 is the composed component-to-world matrix, and `component` is what
    it places - the ladder matches an entity's owning component against that, not against the first
    occurrence it meets."""
    return make_occurrence(component=component, transform2=matrix, transform=FakeMatrix3D())


def _proxy_face(surface_type, world_origin, matrix, token="CHILD"):
    """A face reached through an assembly PROXY: assemblyContext names the occurrence, and
    `world_origin` is the centre the proxy's own surface geometry reports - already WORLD. `matrix`
    is the placement that occurrence still carries, so a test can assert it was NOT applied a second
    time on top of a reading that already has it."""
    return _face(surface_type, origin=world_origin,
                 context=_placed(matrix, component=_comp("Child", token)),
                 component=_comp("Child", token))


@pytest.fixture
def world_frames(monkeypatch):
    """Wire the two seams the world-lift reads: the active design's root component (a native ROOT
    face needs no transform) and the identity matrix factory. install() replaces the design seams
    only, so same_component stays the REAL one - component wrappers are never identity-stable live,
    so an `a is b` stand-in here would let an identity compare pass for the token compare the ladder
    actually makes. A test places a sub-component by seeding the root's by-component lookup."""
    import adsk.core
    root_comp = MakeComp(name="Root", entity_token="ROOT")
    install(jg, make_design(comp=root_comp))
    monkeypatch.setattr(adsk.core.Matrix3D, "create", staticmethod(FakeMatrix3D), raising=False)
    return root_comp


def _edge(curve_type):
    """An edge on conftest's shared BRepEdge fake, carrying the shared curve fake for its kind -
    each of which holds the measured curveType intrinsically."""
    curve = (Circle3D(FakeVector3D(0.0, 0.0, 1.0)) if curve_type == _CT.Circle3DCurveType
             else Line3D())
    return BRepEdge(curve)


def _at(pos):
    """A placement matrix at `pos` (cm), the shared numeric Matrix3D - its .translation is the
    origin every moved_by read comes off."""
    return FakeMatrix3D(0.0, pos)


def _moving_occ(name, pos, local=None):
    """An occurrence a test moves across joint creation by ASSIGNING transform2, so the reported
    moved_by delta is a real placement change. `local` holds the separate LOCAL .transform: a nested
    proxy's local matrix leaves its parent's placement out, so the two disagree the moment an
    ancestor is not identity."""
    return make_occurrence(path=name, transform2=_at(pos),
                           transform=_at(local if local is not None else pos))


def _install(monkeypatch, rec=None):
    rec = rec if rec is not None else _Recorder()
    # JointGeometry factory -> our recorder
    monkeypatch.setattr(adsk.fusion, "JointGeometry", rec)
    # make the handler's isinstance checks use conftest's shared fakes
    monkeypatch.setattr(adsk.fusion, "BRepFace", BRepFace)
    monkeypatch.setattr(adsk.fusion, "BRepEdge", BRepEdge)
    monkeypatch.setattr(adsk.fusion, "BRepVertex", _Vertex)
    monkeypatch.setattr(adsk.fusion, "ConstructionPoint", type("CP", (), {}))
    monkeypatch.setattr(adsk.fusion, "SketchPoint", type("SP", (), {}))
    return rec


# ── the runtime-rule logic (the whole point of the tool) ────────────────────

class TestJointGeometryRules:
    def test_cylinder_face_uses_MIDDLE_not_center(self, monkeypatch):
        # The key rule: CenterKeyPoint is invalid on a cylinder face — use MiddleKeyPoint.
        _install(monkeypatch)
        g, label, err = jg._joint_geometry_for(_face(_ST.CylinderSurfaceType))
        assert err is None
        assert g[1] == "nonplanar" and g[2] == _KP.MiddleKeyPoint     # createByNonPlanarFace + MiddleKeyPoint
        assert "cylinder" in label

    def test_cone_face_also_uses_middle(self, monkeypatch):
        _install(monkeypatch)
        g, label, err = jg._joint_geometry_for(_face(_ST.ConeSurfaceType))
        assert err is None and g[2] == _KP.MiddleKeyPoint

    def test_planar_face_uses_CENTER(self, monkeypatch):
        rec = _install(monkeypatch)
        g, label, err = jg._joint_geometry_for(_face(_ST.PlaneSurfaceType))
        assert err is None
        assert g[1] == "planar" and g[2] == _KP.CenterKeyPoint
        # the planar path is the ONLY factory a planar face touches - never createByNonPlanarFace
        assert rec.calls == [("planar", _KP.CenterKeyPoint)]

    def test_sphere_face_uses_CENTER_via_nonplanar(self, monkeypatch):
        # A sphere face accepts ONLY CenterKeyPoint; MiddleKeyPoint raises. Live: CenterKeyPoint
        # returns a JointGeometry at the sphere centre.
        rec = _install(monkeypatch)
        g, label, err = jg._joint_geometry_for(_face(_ST.SphereSurfaceType))
        assert err is None
        assert g[1] == "nonplanar" and g[2] == _KP.CenterKeyPoint
        assert label == "sphere_face@center"
        assert rec.calls == [("nonplanar", _KP.CenterKeyPoint)]

    def test_torus_face_uses_CENTER_via_nonplanar(self, monkeypatch):
        # Measured on a live torus face (surfaceType 4): MiddleKeyPoint raises the same keypoint
        # sentence the sphere raised, CenterKeyPoint returns a JointGeometry at the torus centre.
        _install(monkeypatch)
        g, label, err = jg._joint_geometry_for(_face(_ST.TorusSurfaceType))
        assert err is None
        assert g[1] == "nonplanar" and g[2] == _KP.CenterKeyPoint
        assert label == "torus_face@center"

    # The measured torus rule, three rigs: a PARAMETRIC torus returns its true centre world-framed,
    # while a torus inside a BASE FEATURE returns its owning COMPONENT'S ORIGIN world-framed,
    # whatever the torus centre is. Nothing raises either way, so the returned origin is the only
    # signal - and the only comparison that separates them is against the torus's own centre read
    # in the SAME world frame.

    def test_root_base_feature_torus_is_refused(self, monkeypatch, world_frames):
        # Rig 1: root component, torus centred (25, 0, 2), keypoint comes back (0,0,0) = the root
        # origin. A native ROOT face needs no lift - its frame IS world.
        _install(monkeypatch, _OriginRecorder((0.0, 0.0, 0.0)))
        face = _face(_ST.TorusSurfaceType, origin=(25.0, 0.0, 2.0),
                     component=_comp("Root", "ROOT"))
        g, label, err = jg._joint_geometry_for(face)
        assert g is None and label == "torus_face@center"
        assert "(0.0000, 0.0000, 0.0000) cm in WORLD space" in err
        assert "(25.0000, 0.0000, 2.0000) cm in WORLD space" in err
        assert "BASE FEATURE" in err

    def test_placed_base_feature_torus_offset_from_its_component_origin_is_refused(
            self, monkeypatch, world_frames):
        # Rig 3 - the one a world-origin signature MISSES: the child sits at world (50,6,0) rotated
        # 90deg, the proxy reports the torus centre at world (50,8,0), and the keypoint comes back
        # as the CHILD ORIGIN (50,6,0) - a plausible nonzero point that is still wrong.
        _install(monkeypatch, _OriginRecorder((50.0, 6.0, 0.0)))
        face = _proxy_face(_ST.TorusSurfaceType, (50.0, 8.0, 0.0),
                           FakeMatrix3D(_ROT90Z_DEG, (50.0, 6.0, 0.0)))
        g, _label, err = jg._joint_geometry_for(face)
        assert g is None
        assert "(50.0000, 6.0000, 0.0000) cm in WORLD space" in err       # the keypoint
        assert "(50.0000, 8.0000, 0.0000) cm in WORLD space" in err       # the proxy's own centre
        # the occurrence's placement is NOT applied to a reading that already carries it - that
        # second lift would put the centre at (42, 56, 0), a point on no part of the model.
        assert "(42.0000, 56.0000, 0.0000)" not in err

    def test_placed_base_feature_torus_centred_on_its_component_origin_is_accepted(
            self, monkeypatch, world_frames):
        # Rig 2: the same base-feature bug, but the torus happens to be centred at the child's own
        # origin, so the keypoint is accidentally RIGHT. There is nothing wrong to report.
        _install(monkeypatch, _OriginRecorder((50.0, 6.0, 0.0)))
        face = _proxy_face(_ST.TorusSurfaceType, (50.0, 6.0, 0.0),
                           FakeMatrix3D(_ROT90Z_DEG, (50.0, 6.0, 0.0)))
        g, _label, err = jg._joint_geometry_for(face)
        assert err is None and g is not None

    def test_parametric_torus_under_a_placed_parent_is_accepted(self, monkeypatch, world_frames):
        # The valid parametric case: the proxy reports the centre at world (50, 8, 1) and the
        # keypoint agrees, so nothing is wrong. Lifting the proxy's reading through its occurrence
        # again refuses this torus and tells its owner to rebuild parametrically something that
        # already is.
        _install(monkeypatch, _OriginRecorder((50.0, 8.0, 1.0)))
        face = _proxy_face(_ST.TorusSurfaceType, (50.0, 8.0, 1.0),
                           FakeMatrix3D(_ROT90Z_DEG, (50.0, 6.0, 0.0)))
        g, _label, err = jg._joint_geometry_for(face)
        assert err is None and g is not None

    def test_parametric_root_torus_is_accepted(self, monkeypatch, world_frames):
        _install(monkeypatch, _OriginRecorder((27.0, 0.0, 1.0)))
        face = _face(_ST.TorusSurfaceType, origin=(27.0, 0.0, 1.0),
                     component=_comp("Root", "ROOT"))
        g, _label, err = jg._joint_geometry_for(face)
        assert err is None and g is not None

    # A NATIVE face in a placed sub-component: LIVE-MEASURED, its surface geometry.origin is
    # component-LOCAL (a torus built at local (30, 10, 5) mm read (3, 1, 0.5) cm) while the keypoint
    # comes back WORLD from a native face too ((-1, -5, 0.5) cm for that component placed at
    # (0, -80, 0) mm and turned 90 deg). So the comparison is available exactly when ONE placement
    # answers for the component, and undecidable otherwise.

    def _native_in_child(self, monkeypatch, world_frames, keypoint, local_origin, placements=1):
        _install(monkeypatch, _OriginRecorder(keypoint))
        # Distinct placements with DIFFERENT transforms, so "several" is a real ambiguity and not
        # one occurrence listed twice.
        occs = [_placed(FakeMatrix3D(_ROT90Z_DEG, (50.0 + 20.0 * i, 6.0, 0.0)),
                        component=_comp("Child", "CHILD")) for i in range(placements)]
        world_frames._occurrences_by_component = {"Child": occs}
        face = _face(_ST.TorusSurfaceType, origin=local_origin,
                     component=_comp("Child", "CHILD"))
        return jg._joint_geometry_for(face)

    def test_a_native_face_in_a_SINGLY_placed_component_is_judged_against_that_placement(
            self, monkeypatch, world_frames):
        # local (2,0,0) turned 90 deg and moved to (50,6,0) is world (50,8,0) - the keypoint agrees,
        # so a valid parametric torus must NOT be refused now the placement is resolvable.
        g, _label, err = self._native_in_child(monkeypatch, world_frames, (50.0, 8.0, 0.0),
                                               (2.0, 0.0, 0.0))
        assert err is None and g is not None

    def test_a_native_base_feature_torus_in_a_placed_component_is_refused(self, monkeypatch,
                                                                          world_frames):
        # the same rig with the base-feature keypoint - the CHILD's own origin (50,6,0), plausible
        # and still wrong. Only the resolved placement separates it from the case above.
        g, _label, err = self._native_in_child(monkeypatch, world_frames, (50.0, 6.0, 0.0),
                                               (2.0, 0.0, 0.0))
        assert g is None
        assert "(50.0000, 6.0000, 0.0000) cm in WORLD space" in err       # the keypoint
        assert "(50.0000, 8.0000, 0.0000) cm in WORLD space" in err       # the lifted centre

    def test_the_world_lift_leaves_the_faces_own_centre_reading_untouched(self, monkeypatch,
                                                                          world_frames):
        # The lift transforms a COPY. transformBy moves a Point3D IN PLACE, so lifting the face's
        # OWN geometry.origin would leave that surface reporting the world point as its centre from
        # then on - and the next reader of the same face compares a world point against a world
        # point and lifts it a second time. Nothing in the returned verdict shows it: with the copy
        # dropped, keypoint and centre still agree and this torus is still accepted. The source
        # point's own coordinates are the only reading that separates the two.
        _install(monkeypatch, _OriginRecorder((50.0, 8.0, 0.0)))
        occ = _placed(FakeMatrix3D(_ROT90Z_DEG, (50.0, 6.0, 0.0)),
                      component=_comp("Child", "CHILD"))
        world_frames._occurrences_by_component = {"Child": [occ]}
        face = _face(_ST.TorusSurfaceType, origin=(2.0, 0.0, 0.0),
                     component=_comp("Child", "CHILD"))
        g, _label, err = jg._joint_geometry_for(face)
        assert err is None and g is not None       # local (2,0,0) lifts to world (50,8,0)
        src = face.geometry.origin
        assert (src.x, src.y, src.z) == (2.0, 0.0, 0.0)

    def test_a_native_face_in_a_TWICE_placed_component_makes_no_judgement(self, monkeypatch,
                                                                          world_frames):
        # two placements, two world frames, and nothing in hand says which instance the face is
        # being asked about. No judgement beats a wrong one - a refusal would block a good joint.
        g, _label, err = self._native_in_child(monkeypatch, world_frames, (0.0, 0.0, 0.0),
                                               (25.0, 0.0, 2.0), placements=2)
        assert err is None and g is not None

    def test_a_native_face_in_an_UNPLACED_component_makes_no_judgement(self, monkeypatch,
                                                                       world_frames):
        # the other side of the "exactly one placement" boundary: a component the assembly does not
        # place has no world frame at all, so there is still nothing to compare against.
        g, _label, err = self._native_in_child(monkeypatch, world_frames, (0.0, 0.0, 0.0),
                                               (25.0, 0.0, 2.0), placements=0)
        assert err is None and g is not None

    def test_sphere_keypoint_is_not_cross_checked(self, monkeypatch, world_frames):
        # Measured: the sphere face is correct in BOTH the parametric and the base-feature case, so
        # it carries no cross-check - adding one would refuse valid sphere joints.
        _install(monkeypatch, _OriginRecorder((0.0, 0.0, 0.0)))
        face = _face(_ST.SphereSurfaceType, origin=(9.0, 9.0, 9.0),
                     component=_comp("Root", "ROOT"))
        g, label, err = jg._joint_geometry_for(face)
        assert err is None and g is not None and label == "sphere_face@center"

    def test_unreadable_torus_centre_makes_no_judgement(self, monkeypatch, world_frames):
        # Nothing to compare against is not evidence of a bad keypoint - the geometry is returned.
        _install(monkeypatch, _OriginRecorder((0.0, 0.0, 0.0)))
        face = _face(_ST.TorusSurfaceType,        # the torus surface reports no centre
                     component=_comp("Root", "ROOT"))
        g, _label, err = jg._joint_geometry_for(face)
        assert err is None and g is not None

    def test_other_nonplanar_face_still_uses_MIDDLE(self, monkeypatch):
        # only sphere/torus move to the centre keypoint - a NURBS face keeps the middle fallback
        _install(monkeypatch)
        g, label, err = jg._joint_geometry_for(_face(_ST.NurbsSurfaceType))
        assert err is None and g[2] == _KP.MiddleKeyPoint
        assert label == "nonplanar_face@middle"

    def test_api_raise_text_reaches_the_error(self, monkeypatch):
        # the platform's own sentence names the keypoint the face demands - it must not be flattened
        # to "createByNonPlanarFace failed", which tells the caller nothing actionable.
        _install(monkeypatch, _RaisingRecorder())
        g, label, err = jg._joint_geometry_for(_face(_ST.CylinderSurfaceType))
        assert g is None
        assert "should be CenterKeyPoint" in err and "sphere and torus" in err

    def test_circular_edge_uses_center(self, monkeypatch):
        _install(monkeypatch)
        g, label, err = jg._joint_geometry_for(_edge(_CT.Circle3DCurveType))
        assert err is None and g[1] == "curve" and g[2] == _KP.CenterKeyPoint

    def test_line_edge_uses_middle(self, monkeypatch):
        _install(monkeypatch)
        g, _, err = jg._joint_geometry_for(_edge(_CT.Line3DCurveType))
        assert err is None and g[2] == _KP.MiddleKeyPoint

    def test_vertex_uses_point(self, monkeypatch):
        _install(monkeypatch)
        g, label, err = jg._joint_geometry_for(_Vertex(None))
        assert err is None and g[1] == "point"


# ── handler guards + wiring ─────────────────────────────────────────────────

class _FakeJointInput(FakeJointInput):
    """The shared input, with `motion` this file's read of the setter it recorded."""
    @property
    def motion(self):
        return self._motion


def _install_design(monkeypatch, token_map, joint_health=_FHS.HealthyFeatureHealthState,
                    joint_msg="", rec=None, snapshots=None, new_joint=None):
    """Install a design whose root carries the joints collection, and return it. `new_joint` is what
    add() hands back and `joints._input` the JointInput createInput does, with `joints._calls` saying
    whether it was ever asked for one. `snapshots` supplies a Design.snapshots surface (the
    moved-but-uncaptured flag the create gates on); omitted, that flag reads unknown."""
    _install(monkeypatch, rec)
    joint = new_joint if new_joint is not None else FakeJoint(
        name="Joint1", health=joint_health, message=joint_msg,
        occurrence_one=make_occurrence("Rod:1"), occurrence_two=make_occurrence("Crank:1"))
    joints = FakeJoints(new_joint=joint, joint_input=_FakeJointInput())
    root = MakeComp(name="Root")
    root.joints = joints
    install(jg, make_design(comp=root, tokens=token_map, snapshots=snapshots))
    return joints


class TestHandler:
    def test_unknown_motion(self, monkeypatch):
        _install_design(monkeypatch, {})
        res = jg.handler(handle_one="a", handle_two="b", motion="weld")
        assert res["isError"] is True and "Unknown motion" in res["message"]

    def test_unresolved_handle(self, monkeypatch):
        _install_design(monkeypatch, {"a": _face(_ST.CylinderSurfaceType)})   # 'b' not in map
        res = jg.handler(handle_one="a", handle_two="b")
        # The typed GeometryHandle kind names the offending input and flags possible staleness.
        assert res["isError"] is True
        assert "handle_two" in res["message"] and "did not resolve" in res["message"]

    def test_revolute_named_axis_is_frame_relative(self, monkeypatch):
        joints = _install_design(monkeypatch, {"rod": _face(_ST.CylinderSurfaceType), "pin": _face(_ST.CylinderSurfaceType)})
        out = _payload(jg.handler(handle_one="rod", handle_two="pin", motion="revolute", axis="x"))
        assert out["jointed"] is True
        assert out["occurrence_one"] == "Rod:1" and out["occurrence_two"] == "Crank:1"
        # axis='x' passes XAxisJointDirection with NO custom entity - the joint FRAME's X, which is
        # world X only when the picked geometry's frame is world-aligned.
        assert joints._input.motion == ("revolute", _JD.XAxisJointDirection)

    def test_revolute_auto_axis_uses_geometry_axis(self, monkeypatch):
        # axis='auto' (default) on cylinder faces derives the axis FROM the geometry
        # (CustomJointDirection + the cylinder face as the axis entity), not a world axis.
        pin = _face(_ST.CylinderSurfaceType)
        joints = _install_design(monkeypatch, {"rod": _face(_ST.CylinderSurfaceType), "pin": pin})
        out = _payload(jg.handler(handle_one="rod", handle_two="pin", motion="revolute"))
        m = joints._input.motion
        assert m[0] == "revolute" and m[1] == _JD.CustomJointDirection      # CustomJointDirection used
        assert m[2] is not None                              # an axis entity was passed
        assert out["axis"] == "auto(geometry)"

    def test_slider_auto_axis_from_geometry(self, monkeypatch):
        joints = _install_design(monkeypatch, {"pis": _face(_ST.CylinderSurfaceType), "bore": _face(_ST.CylinderSurfaceType)})
        _payload(jg.handler(handle_one="pis", handle_two="bore", motion="slider"))
        m = joints._input.motion
        assert m[0] == "slider" and m[1] == _JD.CustomJointDirection

    def test_reports_health_warning_when_joint_fails_to_compute(self, monkeypatch):
        # a joint can ADD fine yet report a WARNING state (over-constrained / Compute Failed) - the
        # handler must surface that as a health warning, not a false success.
        _install_design(monkeypatch, {"a": _face(_ST.CylinderSurfaceType), "b": _face(_ST.CylinderSurfaceType)},
                        joint_health=_FHS.WarningFeatureHealthState,
                        joint_msg="Can't resolve positions.Compute FailedX")
        out = _payload(jg.handler(handle_one="a", handle_two="b", motion="revolute"))
        assert out["healthy"] is False
        assert "FAILED TO COMPUTE" in out["health_warning"]
        assert "Compute Failed" not in out["health_warning"]   # message trimmed

    def test_the_health_warning_is_one_whole_condensed_sentence(self, monkeypatch):
        # Fusion's errorOrWarningMessage carries embedded NEWLINES and REPEATS its sentence, joined
        # by the marker plus the joint's own name. The shared reader collapses the whitespace, so
        # the wire sentence reads whole rather than as a raw slice of the blob.
        blob = ("Can't resolve positions.\n\nInspect relationships.Compute FailedJoint1"
                "Can't resolve positions.Compute FailedJoint1")
        _install_design(monkeypatch, {"a": _face(_ST.CylinderSurfaceType),
                                      "b": _face(_ST.CylinderSurfaceType)},
                        joint_health=_FHS.WarningFeatureHealthState, joint_msg=blob)
        out = _payload(jg.handler(handle_one="a", handle_two="b", motion="revolute"))
        warning = out["health_warning"]
        assert warning.endswith("Can't resolve positions. Inspect relationships.")
        assert "Compute Failed" not in warning and "\n" not in warning

    def test_a_message_at_the_cap_is_whole_and_one_character_over_is_marked_cut(self, monkeypatch):
        # the exact cap boundary this site passes to the shared reader: 200 characters cross whole,
        # 201 are cut and marked, so a shortened message never reads as a complete one.
        for length, cut in ((200, False), (201, True)):
            _install_design(monkeypatch, {"a": _face(_ST.CylinderSurfaceType),
                                          "b": _face(_ST.CylinderSurfaceType)},
                            joint_health=_FHS.WarningFeatureHealthState, joint_msg="z" * length)
            warning = _payload(jg.handler(handle_one="a", handle_two="b",
                                          motion="revolute"))["health_warning"]
            assert warning.endswith(" ...") is cut, length
            # 'z' appears nowhere in the fixed lead-in, so this counts the MESSAGE's own characters
            assert warning.count("z") == (200 if cut else length), length

    def test_healthy_joint_no_warning(self, monkeypatch):
        _install_design(monkeypatch, {"a": _face(_ST.CylinderSurfaceType), "b": _face(_ST.CylinderSurfaceType)})
        out = _payload(jg.handler(handle_one="a", handle_two="b", motion="revolute"))
        assert out["healthy"] is True and "health_warning" not in out

    def test_rigid_motion_has_null_axis(self, monkeypatch):
        joints = _install_design(monkeypatch, {"a": _face(_ST.CylinderSurfaceType), "b": _face(_ST.CylinderSurfaceType)})
        out = _payload(jg.handler(handle_one="a", handle_two="b", motion="rigid"))
        assert joints._input.motion == ("rigid",)
        assert out["axis"] is None                       # rigid has no motion axis to report

    def test_ball_motion_pins_pitch_z_yaw_x(self, monkeypatch):
        # Live API fact: setAsBallJointMotion(pitchDirection, yawDirection) REJECTS X as the pitch
        # ("Invalid parameter pitchDirection") - the valid pair is pitch=Z, yaw=X. A mock accepts
        # any args, so only pinning the enum pair catches a swap before a live document does.
        joints = _install_design(monkeypatch, {"a": _face(_ST.CylinderSurfaceType), "b": _face(_ST.CylinderSurfaceType)})
        out = _payload(jg.handler(handle_one="a", handle_two="b", motion="ball"))
        assert joints._input.motion == ("ball", _JD.ZAxisJointDirection, _JD.XAxisJointDirection)
        assert out["jointed"] is True

    def test_slider_named_axis_is_frame_relative(self, monkeypatch):
        # axis='z' on cylinder faces takes the frame-relative Z direction (no CustomJointDirection).
        joints = _install_design(monkeypatch, {"a": _face(_ST.CylinderSurfaceType), "b": _face(_ST.CylinderSurfaceType)})
        _payload(jg.handler(handle_one="a", handle_two="b", motion="slider", axis="z"))
        assert joints._input.motion == ("slider", _JD.ZAxisJointDirection)

    def test_cylindrical_named_axis_is_frame_relative(self, monkeypatch):
        joints = _install_design(monkeypatch, {"a": _face(_ST.CylinderSurfaceType), "b": _face(_ST.CylinderSurfaceType)})
        _payload(jg.handler(handle_one="a", handle_two="b", motion="cylindrical", axis="y"))
        assert joints._input.motion == ("cylindrical", _JD.YAxisJointDirection)

    def test_auto_axis_with_no_geometry_axis_falls_back_to_frame_z(self, monkeypatch):
        # PLANAR faces give _axis_entity nothing -> 'auto' can't derive an axis; the motion uses the
        # default frame-relative Z direction and the reported axis is plain 'auto', NOT 'auto(geometry)'.
        joints = _install_design(monkeypatch, {"a": _face(_ST.PlaneSurfaceType), "b": _face(_ST.PlaneSurfaceType)})
        out = _payload(jg.handler(handle_one="a", handle_two="b", motion="revolute"))
        assert joints._input.motion == ("revolute", _JD.ZAxisJointDirection)   # frame Z, not CUSTOM
        assert out["axis"] == "auto"

    def test_unknown_axis_keyword_errors(self, monkeypatch):
        # An unrecognized axis string (not x/y/z, not auto) must be REFUSED, naming the offending
        # value - not silently coerced to the world Z direction.
        _install_design(monkeypatch, {"a": _face(_ST.PlaneSurfaceType), "b": _face(_ST.PlaneSurfaceType)})
        res = jg.handler(handle_one="a", handle_two="b", motion="revolute", axis="diagonal")
        assert res["isError"] is True
        assert "diagonal" in res["message"]

    def test_circular_edge_is_an_axis_entity_for_auto(self, monkeypatch):
        # a circular edge can define the motion axis (auto -> CustomJointDirection + the edge).
        joints = _install_design(monkeypatch, {"a": _edge(_CT.Circle3DCurveType), "b": _edge(_CT.Circle3DCurveType)})
        out = _payload(jg.handler(handle_one="a", handle_two="b", motion="revolute"))
        m = joints._input.motion
        assert m[0] == "revolute" and m[1] == _JD.CustomJointDirection and m[2] is not None
        assert out["axis"] == "auto(geometry)"

    def test_reports_moved_by_when_part_repositioned(self, monkeypatch):
        # joint_at aligns the picked keypoints, repositioning handle_one's occurrence; a real move
        # must surface as moved_by + move_warning, not silently (the teleport defect: a member
        # relocating to the joint without the caller being told).
        moving = _moving_occ("Rod:1", (10.0, 0.0, 0.0))    # cm - the moving occurrence's origin
        face_a = _face(_ST.PlaneSurfaceType); face_a.assemblyContext = moving
        face_b = _face(_ST.PlaneSurfaceType)
        joints = _install_design(monkeypatch, {"a": face_a, "b": face_b})
        orig_add = joints.add
        def moving_add(ji):                                # add() repositions the occurrence
            j = orig_add(ji); moving.transform2 = _at((2.5, 0.5, 0.0)); return j
        joints.add = moving_add
        out = _payload(jg.handler(handle_one="a", handle_two="b", motion="rigid"))
        assert "moved_by" in out
        # delta (-7.5, 0.5, 0) cm -> 75.17 mm; direction points -X
        assert 75.0 < out["moved_by"]["distance_mm"] < 75.3
        assert out["moved_by"]["direction"][0] < 0
        assert "move_warning" in out and "keypoints" in out["move_warning"].lower()

    def test_reports_moved_by_when_the_fixed_side_moves(self, monkeypatch):
        # grounding / an existing joint can make the solver move occurrence_TWO instead of one - both
        # sides are watched, and the mover is named (observed live: the wrong member can be the one that moves).
        moving = _moving_occ("Crank:1", (0.0, 0.0, 0.0))
        face_a = _face(_ST.PlaneSurfaceType)                        # handle_one stays put
        face_b = _face(_ST.PlaneSurfaceType); face_b.assemblyContext = moving
        joints = _install_design(monkeypatch, {"a": face_a, "b": face_b})
        orig_add = joints.add
        def moving_add(ji):
            j = orig_add(ji); moving.transform2 = _at((3.0, 0.0, 0.0)); return j    # 3 cm = 30 mm
        joints.add = moving_add
        out = _payload(jg.handler(handle_one="a", handle_two="b", motion="rigid"))
        assert "moved_by" in out and out["moved_by"]["distance_mm"] == 30.0
        assert "Crank:1" in out["move_warning"]

    def test_move_is_measured_in_world_space_off_transform2(self, monkeypatch):
        # .transform is the occurrence's LOCAL matrix: under a placed parent it leaves the parent's
        # rotation/translation out. moved_by is a WORLD distance the caller acts on, so it comes off
        # .transform2. Here the LOCAL matrix never changes across the joint while the WORLD one moves
        # 30 mm - read off .transform the reposition would be reported as no move at all.
        moving = _moving_occ("Rod:1", (0.0, 0.0, 0.0), local=(1.0, 0.0, 0.0))
        face_a = _face(_ST.PlaneSurfaceType); face_a.assemblyContext = moving
        face_b = _face(_ST.PlaneSurfaceType)
        joints = _install_design(monkeypatch, {"a": face_a, "b": face_b})
        orig_add = joints.add
        def moving_add(ji):
            j = orig_add(ji); moving.transform2 = _at((3.0, 0.0, 0.0)); return j   # world moves 3 cm
        joints.add = moving_add
        out = _payload(jg.handler(handle_one="a", handle_two="b", motion="rigid"))
        assert out["moved_by"]["distance_mm"] == 30.0

    def test_transform_is_the_fallback_when_transform2_will_not_read(self, monkeypatch):
        # An occurrence whose transform2 DECLINES must still be watched off its LOCAL .transform,
        # not silently dropped to "no move".
        moving = make_occurrence("Rod:1", transform=_at((0.0, 0.0, 0.0)),
                                 raises_on={"transform2": "3 : the placement is unavailable"})
        face_a = _face(_ST.PlaneSurfaceType); face_a.assemblyContext = moving
        face_b = _face(_ST.PlaneSurfaceType)
        joints = _install_design(monkeypatch, {"a": face_a, "b": face_b})
        orig_add = joints.add
        def moving_add(ji):
            j = orig_add(ji); moving.transform = _at((3.0, 0.0, 0.0)); return j
        joints.add = moving_add
        out = _payload(jg.handler(handle_one="a", handle_two="b", motion="rigid"))
        assert out["moved_by"]["distance_mm"] == 30.0

    def test_no_moved_by_when_part_stays_put(self, monkeypatch):
        # a well-matched pair whose keypoints already coincide does not move -> no moved_by / warning.
        still = _moving_occ("Rod:1", (4.0, 1.0, 0.0))
        face_a = _face(_ST.PlaneSurfaceType); face_a.assemblyContext = still
        face_b = _face(_ST.PlaneSurfaceType)
        _install_design(monkeypatch, {"a": face_a, "b": face_b})       # add() leaves the position unchanged
        out = _payload(jg.handler(handle_one="a", handle_two="b", motion="rigid"))
        assert "moved_by" not in out and "move_warning" not in out

    def test_motion_setter_failure_reports_error(self, monkeypatch):
        # if the motion setter raises (e.g. incompatible geometry), the handler returns an error
        # naming the motion + the axis hint, NOT a false success.
        joints = _install_design(monkeypatch, {"a": _face(_ST.CylinderSurfaceType), "b": _face(_ST.CylinderSurfaceType)})

        def boom(*a, **k):
            raise RuntimeError("geometry rejected")
        # patch createInput to return a JointInput whose revolute setter raises
        orig = joints.createInput
        def make(g1, g2):
            ji = orig(g1, g2)
            ji.setAsRevoluteJointMotion = boom
            return ji
        joints.createInput = make
        res = jg.handler(handle_one="a", handle_two="b", motion="revolute", axis="x")
        assert res["isError"] is True
        assert "Could not set revolute motion" in res["message"]
        assert "pass axis=x/y/z" in res["message"]      # a motion WITH an axis gets the axis advice

    def test_ball_motion_failure_carries_no_axis_advice(self, monkeypatch):
        # setAsBallJointMotion never reads 'axis', so telling a failed ball caller to pass one
        # contradicts the input's own "ball uses none" and sends them after a knob that does nothing.
        joints = _install_design(monkeypatch, {"a": _face(_ST.CylinderSurfaceType), "b": _face(_ST.CylinderSurfaceType)})
        orig = joints.createInput
        def make(g1, g2):
            ji = orig(g1, g2)
            ji.setAsBallJointMotion = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("rejected"))
            return ji
        joints.createInput = make
        res = jg.handler(handle_one="a", handle_two="b", motion="ball")
        assert res["isError"] is True
        assert "Could not set ball motion" in res["message"]
        assert "axis=" not in res["message"]

    def test_sphere_face_handle_joints_instead_of_being_refused(self, monkeypatch):
        # a sphere face IS supported joint geometry - it just needs CenterKeyPoint; the handler must
        # build it, not refuse the handle.
        _install_design(monkeypatch, {"a": _face(_ST.SphereSurfaceType),
                                      "b": _face(_ST.CylinderSurfaceType)})
        out = _payload(jg.handler(handle_one="a", handle_two="b", motion="ball"))
        assert out["jointed"] is True and out["geometry_one"] == "sphere_face@center"

    def test_keypoint_raise_reaches_the_handler_error(self, monkeypatch):
        # the API's own actionable sentence must survive to the caller, named to the offending input
        _install_design(monkeypatch, {"a": _face(_ST.CylinderSurfaceType),
                                      "b": _face(_ST.CylinderSurfaceType)},
                        rec=_RaisingRecorder())
        res = jg.handler(handle_one="a", handle_two="b", motion="revolute")
        assert res["isError"] is True
        assert "handle_one" in res["message"] and "should be CenterKeyPoint" in res["message"]

    def test_flip_sets_isFlipped_on_the_joint_input(self, monkeypatch):
        # flip=true must reach the JointInput BEFORE add() - a dropped flag silently recreates the
        # 180-deg flush-mate rotation the input exists to prevent.
        joints = _install_design(monkeypatch, {"a": _face(_ST.PlaneSurfaceType),
                                  "b": _face(_ST.PlaneSurfaceType)})
        out = _payload(jg.handler(handle_one="a", handle_two="b", motion="rigid", flip=True))
        assert joints._input.isFlipped is True
        assert out["flipped"] is True

    def test_no_flip_leaves_joint_input_unflipped(self, monkeypatch):
        joints = _install_design(monkeypatch, {"a": _face(_ST.PlaneSurfaceType),
                                  "b": _face(_ST.PlaneSurfaceType)})
        out = _payload(jg.handler(handle_one="a", handle_two="b", motion="rigid"))
        assert not getattr(joints._input, "isFlipped", False)
        assert out["flipped"] is False


class TestHealthVerdict:
    """The created joint's state - read off the joint AND its timeline item, joint first - decides
    the payload's authoritative flag. Only the ERROR and WARNING states are a failed compute;
    SUPPRESSED / ROLLED BACK are states published by NAME with no failure claim; a state that
    answers but carries no name here is still not a failure, so 'healthy' is true with a null
    state name. 'healthy' is null only where NEITHER source answered a state at all. The enum
    members come from adsk.fusion.FeatureHealthStates, never a hand-typed int."""

    _FHS = adsk.fusion.FeatureHealthStates

    def _cyl_pair(self, monkeypatch, **kw):
        return _install_design(monkeypatch, {"a": _face(_ST.CylinderSurfaceType),
                                             "b": _face(_ST.CylinderSurfaceType)}, **kw)

    def _out(self, monkeypatch, **kw):
        self._cyl_pair(monkeypatch, **kw)
        return _payload(jg.handler(handle_one="a", handle_two="b", motion="revolute"))

    def _joint(self, **kw):
        """The joint add() hands back, named and coupled the way the payload reports it."""
        return FakeJoint(name="Joint1", occurrence_one=make_occurrence("Rod:1"),
                         occurrence_two=make_occurrence("Crank:1"), **kw)

    def test_a_healthy_joint_names_its_state(self, monkeypatch):
        out = self._out(monkeypatch, joint_health=self._FHS.HealthyFeatureHealthState)
        assert out["healthy"] is True and out["health_state"] == "healthy"
        assert "health_warning" not in out

    def test_an_error_state_is_the_failure_verdict(self, monkeypatch):
        out = self._out(monkeypatch, joint_health=self._FHS.ErrorFeatureHealthState,
                        joint_msg="Can't resolve positions.")
        assert out["healthy"] is False and out["health_state"] == "error"
        assert "FAILED TO COMPUTE" in out["health_warning"]

    def test_a_warning_state_is_also_a_failure_verdict(self, monkeypatch):
        # Fusion marks a WARNING-state feature 'Compute Failed' too, and assembly_get's
        # broken_joints classes the two alike - so this tool must not split them either.
        out = self._out(monkeypatch, joint_health=self._FHS.WarningFeatureHealthState,
                        joint_msg="Conflicting relationships.")
        assert out["healthy"] is False and out["health_state"] == "warning"
        assert "FAILED TO COMPUTE" in out["health_warning"]

    def test_a_suppressed_joint_is_not_published_as_a_failed_compute(self, monkeypatch):
        out = self._out(monkeypatch, joint_health=self._FHS.SuppressedFeatureHealthState)
        assert out["healthy"] is True and out["health_state"] == "suppressed"
        assert "health_warning" not in out
        assert "'suppressed'" in out["note"] and "FAILED TO COMPUTE" not in out["note"]

    def test_a_rolled_back_joint_is_not_published_as_a_failed_compute(self, monkeypatch):
        out = self._out(monkeypatch, joint_health=self._FHS.RolledBackFeatureHealthState)
        assert out["healthy"] is True and out["health_state"] == "rolled_back"
        assert "health_warning" not in out
        assert "'rolled_back'" in out["note"] and "FAILED TO COMPUTE" not in out["note"]

    def test_a_state_with_no_name_here_is_still_not_a_failed_compute(self, monkeypatch):
        # A state that ANSWERED and is not ERROR/WARNING is not a failure - the same classification
        # joint_create and assembly_get publish, so two create tools cannot disagree on one design.
        # No name in this tool's table matches it, so the state name is null while 'healthy' reports
        # what was read.
        out = self._out(monkeypatch, joint_health=self._FHS.UnknownFeatureHealthState)
        assert out["healthy"] is True and out["health_state"] is None
        assert "health_warning" not in out
        assert "no name for" in out["note"] and "FAILED TO COMPUTE" not in out["note"]

    def test_a_timeline_items_failure_decides_past_a_healthy_joint(self, monkeypatch):
        # The Joint object and its TimelineObject each carry healthState and they can disagree: a
        # joint reading HEALTHY whose timeline item reports a WARNING is a failed compute, and
        # reading the joint alone publishes healthy=true for it.
        item = FakeTimelineObject(health=self._FHS.WarningFeatureHealthState,
                                  message="Conflicts with assembly relationships.")
        self._cyl_pair(monkeypatch, new_joint=self._joint(
            health=self._FHS.HealthyFeatureHealthState, timeline_object=item))
        out = _payload(jg.handler(handle_one="a", handle_two="b", motion="revolute"))
        assert out["healthy"] is False and out["health_state"] == "warning"
        assert "Conflicts with assembly relationships." in out["health_warning"]

    def test_a_joint_answering_no_state_is_read_off_its_timeline_item(self, monkeypatch):
        # The joint itself answers nothing; its timeline item answers HEALTHY. Reading the joint
        # alone would publish 'healthy' null for a state that was in fact read.
        item = FakeTimelineObject(health=self._FHS.HealthyFeatureHealthState, message="")
        self._cyl_pair(monkeypatch,
                       new_joint=self._joint(health_readable=False, timeline_object=item))
        out = _payload(jg.handler(handle_one="a", handle_two="b", motion="revolute"))
        assert out["healthy"] is True and out["health_state"] == "healthy"
        assert "health_warning" not in out

    def test_a_state_that_will_not_read_leaves_the_verdict_null(self, monkeypatch):
        # An unreadable state is not a clean compute either - reading it as healthy publishes
        # healthy=true for a joint nothing verified.
        self._cyl_pair(monkeypatch, new_joint=self._joint(health_readable=False))
        out = _payload(jg.handler(handle_one="a", handle_two="b", motion="revolute"))
        assert out["healthy"] is None and out["health_state"] is None
        # The warning names BOTH sources that stayed silent - the condition this branch fires on. A
        # warning that blames the state's NAME describes the healthy=true/health_state=null case
        # instead, which reaches a different branch entirely.
        assert "Neither this joint nor its timeline item" in out["health_warning"]
        assert "UNVERIFIED" in out["health_warning"]

    def test_an_unread_state_stays_null_even_when_no_enum_member_reads(self, monkeypatch):
        # A build carrying the FeatureHealthStates family but NONE of its members: every member read
        # answers None - and so does the joint's own unreadable healthState. A bare
        # `hs == <member>` walk would match None against None and publish 'healthy' for a joint
        # whose state nothing read. The verdict is gated on the state having been READ instead.
        # The empty family is SET here, never delattr'd off the shared adsk mock: a deleted Mock
        # member does not reliably come back and leaks into unrelated tests.
        self._cyl_pair(monkeypatch, new_joint=self._joint(health_readable=False))
        monkeypatch.setattr(jg.adsk.fusion, "FeatureHealthStates", type("FHS", (), {})())
        out = _payload(jg.handler(handle_one="a", handle_two="b", motion="revolute"))
        assert out["healthy"] is None and out["health_state"] is None
        assert "Neither this joint nor its timeline item" in out["health_warning"]
        assert "UNVERIFIED" in out["health_warning"]

    def test_the_wire_advertises_the_null_verdict(self, monkeypatch):
        # 'healthy' is the flag the description calls authoritative, so a null it can return has to
        # be on the wire - a caller treating null as falsey would read a rolled-back joint as broken.
        assert "null" in jg.TOOL_DESCRIPTION and "healthy" in jg.TOOL_DESCRIPTION


class TestAxisNote:
    """The note must state what the motion axis ACTUALLY is. A named x/y/z takes the frame-relative
    path: measured on a 120-deg-rotated frame, axis='y' drove about the frame's Y, (0,-0.5,0.866) -
    120 deg off world Y. Claiming a world axis there would be a false claim on the wire. A ball joint
    takes NO axis at all, so it gets no axis claim in either the note or the payload."""

    def _cyl_pair(self, monkeypatch):
        return _install_design(monkeypatch, {"a": _face(_ST.CylinderSurfaceType),
                                             "b": _face(_ST.CylinderSurfaceType)})

    def test_named_axis_note_says_frame_not_world(self, monkeypatch):
        self._cyl_pair(monkeypatch)
        out = _payload(jg.handler(handle_one="a", handle_two="b", motion="revolute", axis="y"))
        assert "FRAME's y axis, NOT world y" in out["note"]
        assert "joint_edit(world_axis=" in out["note"]

    def test_auto_geometry_axis_note_credits_the_geometry(self, monkeypatch):
        self._cyl_pair(monkeypatch)
        out = _payload(jg.handler(handle_one="a", handle_two="b", motion="revolute"))
        assert "derived the motion axis from the geometry" in out["note"]
        assert "NOT world" not in out["note"]

    def test_auto_without_a_geometry_axis_still_warns_frame_relative(self, monkeypatch):
        # planar faces -> no derivable axis, so the frame-relative default Z is what was used
        _install_design(monkeypatch, {"a": _face(_ST.PlaneSurfaceType),
                                      "b": _face(_ST.PlaneSurfaceType)})
        out = _payload(jg.handler(handle_one="a", handle_two="b", motion="revolute"))
        assert "FRAME's z axis, NOT world z" in out["note"]

    def test_rigid_note_makes_no_axis_claim(self, monkeypatch):
        self._cyl_pair(monkeypatch)
        out = _payload(jg.handler(handle_one="a", handle_two="b", motion="rigid"))
        assert "axis" not in out["note"]

    def test_ball_note_makes_no_axis_claim_even_with_a_named_axis(self, monkeypatch):
        # setAsBallJointMotion hard-codes pitch=Z / yaw=X and reads NEITHER the axis keyword nor a
        # custom entity - a ball landed with axis='x' is identical to one landed with 'auto'. Any
        # axis sentence here (frame-relative OR geometry-derived) would be a measured-false claim.
        self._cyl_pair(monkeypatch)
        out = _payload(jg.handler(handle_one="a", handle_two="b", motion="ball", axis="x"))
        assert "FRAME's" not in out["note"]
        assert "world_axis=" not in out["note"]
        assert "derived the motion axis" not in out["note"]

    def test_ball_payload_axis_is_null(self, monkeypatch):
        # cylinder faces make _axis_entity fire, so the un-carved payload would report
        # 'auto(geometry)' - false: the ball motion consumed no axis entity at all.
        self._cyl_pair(monkeypatch)
        out = _payload(jg.handler(handle_one="a", handle_two="b", motion="ball"))
        assert out["axis"] is None

    def test_ball_payload_axis_is_null_with_a_named_axis(self, monkeypatch):
        self._cyl_pair(monkeypatch)
        out = _payload(jg.handler(handle_one="a", handle_two="b", motion="ball", axis="x"))
        assert out["axis"] is None


class TestFlipHint:
    """Two planar faces whose OUTWARD normals oppose (the flush face-to-face pick) rotate the free
    part 180 deg unless flip is passed - the payload must flag exactly that case."""

    def _faces(self, n1, n2):
        # the normal comes off the shared fake's own surface evaluator, and pointOnFace is present,
        # so the real planar_outward_normal path runs whole: isinstance, surfaceType, then the
        # _geom sample the hint is decided on.
        return (_face(_ST.PlaneSurfaceType, normal=n1, point_on_face=FakePoint(0.0, 0.0, 0.0)),
                _face(_ST.PlaneSurfaceType, normal=n2, point_on_face=FakePoint(0.0, 0.0, 0.0)))

    def test_opposing_normals_without_flip_flag_the_hint(self, monkeypatch):
        fa, fb = self._faces([0.0, 0.0, -1.0], [0.0, 0.0, 1.0])
        _install_design(monkeypatch, {"a": fa, "b": fb})
        out = _payload(jg.handler(handle_one="a", handle_two="b", motion="rigid"))
        assert "flip_hint" in out and "180" in out["flip_hint"] and "flip=true" in out["flip_hint"]

    def test_opposing_normals_with_flip_no_hint(self, monkeypatch):
        fa, fb = self._faces([0.0, 0.0, -1.0], [0.0, 0.0, 1.0])
        _install_design(monkeypatch, {"a": fa, "b": fb})
        out = _payload(jg.handler(handle_one="a", handle_two="b", motion="rigid", flip=True))
        assert "flip_hint" not in out

    def test_agreeing_normals_no_hint(self, monkeypatch):
        fa, fb = self._faces([0.0, 0.0, 1.0], [0.0, 0.0, 1.0])
        _install_design(monkeypatch, {"a": fa, "b": fb})
        out = _payload(jg.handler(handle_one="a", handle_two="b", motion="rigid"))
        assert "flip_hint" not in out


class TestAxisSchema:
    def test_axis_reaches_the_wire_as_a_validated_enum(self):
        # The legal values must be carried by the SCHEMA, where they are machine-validated, not
        # asserted in prose that drifts. Read off the BUILT tool, so unwiring the kind fails too.
        props = jg.joint_at_tool.to_dict()["inputSchema"]["properties"]
        assert props["axis"]["enum"] == ["auto", "x", "y", "z"]
        assert jg._AXIS.default == "auto"
        # the one axis fact no enum can carry: a ball joint reads no axis at all
        assert "ball uses none" in props["axis"]["description"]


class TestAsBuiltRigidRefusal:
    """A rigid as-built joint carries NO joint geometry ("Geometry should not be null if joint motion
    is not rigid"), so the API cannot redefine it as a motion joint. The refusal must point at the
    tool that CAN build one - joint_create_as_built, which takes the 'geometry' the motion anchors on
    - rather than fail bare, and it must not attempt the setter first."""

    def _as_built(self, monkeypatch, geometry):
        class FakeAsBuilt:
            def __init__(self):
                self.geometry = geometry
                self.calls = []
            def setAsRevoluteJointMotion(self, *args):
                self.calls.append(args); return True
        monkeypatch.setattr(adsk.fusion, "AsBuiltJoint", FakeAsBuilt)
        return FakeAsBuilt()

    def test_refusal_points_at_the_tool_that_can_build_the_motion_joint(self, monkeypatch):
        ji = self._as_built(monkeypatch, None)
        did, err = jg.apply_motion(ji, "revolute", 2)
        assert did is False
        # the pointer must name the tool AND the input that makes it work, or it is not actionable
        assert "joint_create_as_built" in err and "'geometry'" in err
        assert "revolute" in err                      # names the motion that was refused
        assert ji.calls == []                         # nothing attempted on the joint

    def test_as_built_with_geometry_takes_the_extra_arity_setter(self, monkeypatch):
        # the refusal is scoped to the no-geometry case: an as-built joint that HAS an anchor is
        # redefined through the setter's as-built arity (direction, geometry).
        geom = object()
        ji = self._as_built(monkeypatch, geom)
        did, err = jg.apply_motion(ji, "revolute", 1)
        assert did is True and err is None
        assert ji.calls == [(_JD.YAxisJointDirection, geom)]


class TestModelParameters:
    def test_payload_names_the_joints_own_dnn_params(self, monkeypatch):
        # the created joint's offset/angle ModelParameter names must reach the payload (with the
        # shared offset-is-frame-Z teaching) so an agent can param_set the right dNN.
        _install_design(monkeypatch, {"a": _face(_ST.CylinderSurfaceType),
                                      "b": _face(_ST.CylinderSurfaceType)},
                        new_joint=FakeJoint(name="Joint1",
                                            occurrence_one=make_occurrence("Rod:1"),
                                            occurrence_two=make_occurrence("Crank:1"),
                                            offset=SimpleNamespace(name="d8"),
                                            angle=SimpleNamespace(name="d5")))
        out = _payload(jg.handler(handle_one="a", handle_two="b", motion="revolute"))
        assert out["model_parameters"] == {"offset": "d8", "angle": "d5"}
        assert "FRAME'S Z" in out["note"]

    def test_motion_param_names_omits_absent_params(self):
        # A joint that ANSWERS both members as null - the shared FakeJoint sets offset/angle only
        # when given, so it can only model them missing, which is a shape live Joint never has.
        j = type("J", (), {"offset": None, "angle": None})()
        assert jg.motion_param_names(j) == {}

    def test_motion_param_names_reads_both(self):
        j = FakeJoint(offset=SimpleNamespace(name="d12"), angle=SimpleNamespace(name="d11"))
        assert jg.motion_param_names(j) == {"offset": "d12", "angle": "d11"}


class _Snapshots(_NamedCollection):
    """Design.snapshots: the shared walk plus the moved-but-uncaptured position flag. `blind` makes
    the flag read RAISE, which is the flag being UNKNOWN - not a False."""
    def __init__(self, pending=False, blind=False):
        super().__init__()
        self._pending = pending
        self._blind = blind

    @property
    def hasPendingSnapshot(self):
        if self._blind:
            raise RuntimeError("pending flag unreadable")
        return self._pending


class TestPendingMoveRefusal:
    """joints.add() recomputes the assembly, and a recompute REVERTS an uncaptured occurrence
    position - so jointing AT geometry while a move is pending would silently move the parts back and
    anchor the joint on the reverted pose. Refuse before touching the joint collection."""

    _HANDLES = {"rod": None, "pin": None}

    def _faces(self):
        return {"rod": _face(_ST.CylinderSurfaceType),
                "pin": _face(_ST.CylinderSurfaceType)}

    def test_refuses_while_a_move_is_pending(self, monkeypatch):
        joints = _install_design(monkeypatch, self._faces(), snapshots=_Snapshots(pending=True))
        res = jg.handler(handle_one="rod", handle_two="pin")
        assert res["isError"] is True
        assert "would silently revert" in res["message"]
        assert "assembly_capture_position(action='capture')" in res["message"]
        assert joints._calls == []                       # no joint input was ever built

    def test_joints_normally_with_nothing_pending(self, monkeypatch):
        _install_design(monkeypatch, self._faces(), snapshots=_Snapshots(pending=False))
        out = _payload(jg.handler(handle_one="rod", handle_two="pin"))
        assert out["jointed"] is True

    def test_an_unreadable_flag_does_not_refuse(self, monkeypatch):
        _install_design(monkeypatch, self._faces(), snapshots=_Snapshots(blind=True))
        out = _payload(jg.handler(handle_one="rod", handle_two="pin"))
        assert out["jointed"] is True


class TestCreateGuards:
    def test_no_active_design_is_refused(self, monkeypatch):
        monkeypatch.setattr(jg._common, "design", lambda: None)
        res = jg.handler(handle_one="a", handle_two="b")
        assert res["isError"] is True and "No active design" in res["message"]

    def test_a_handle_resolver_error_surfaces_verbatim(self, monkeypatch):
        _install_design(monkeypatch, {})
        monkeypatch.setattr(jg._HANDLE_ONE, "resolve",
                            lambda raw: (None, "'handle_one': stale handle - re-run find_geometry."))
        res = jg.handler(handle_one="dead", handle_two="b")
        assert res["isError"] is True and "stale handle" in res["message"]

    def test_a_raising_createInput_is_reported(self, monkeypatch):
        f = _face(_ST.PlaneSurfaceType, point_on_face=FakePoint(0.0, 0.0, 0.0))
        joints = _install_design(monkeypatch, {"a": f, "b": f})
        def _boom(g1, g2):
            raise RuntimeError("geometry rejected")
        joints.createInput = _boom
        res = jg.handler(handle_one="a", handle_two="b")
        assert res["isError"] is True
        assert "Could not create joint input" in res["message"]
        assert "geometry rejected" in res["message"]

    def test_a_flip_the_input_refuses_is_reported(self, monkeypatch):
        f = _face(_ST.PlaneSurfaceType, point_on_face=FakePoint(0.0, 0.0, 0.0))
        joints = _install_design(monkeypatch, {"a": f, "b": f})
        class _NoFlip(_FakeJointInput):
            @property
            def isFlipped(self):
                return False
            @isFlipped.setter
            def isFlipped(self, v):
                raise RuntimeError("flip unavailable on this input")
        joints.createInput = lambda g1, g2: _NoFlip()
        res = jg.handler(handle_one="a", handle_two="b", flip=True)
        assert res["isError"] is True and "Could not apply flip" in res["message"]
