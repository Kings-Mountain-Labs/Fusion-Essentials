"""Unit tests for ``construction.py`` — point / axis / plane construction datums.

Pinned: units scaling on coordinates/offset, the kind/axis/plane guards, that a point sets the
scaled Point3D, an axis builds an InfiniteLine with the right direction, a plane offsets from the
named origin plane, and the friendly direct-modeling 'Environment is not supported' error. Also
pins every non-legacy mode (plane/axis/point): mode-vs-kind dispatch, per-mode required-input
guards (exact counts, missing scalars, wrong geometry kind), the setBy*-returned-false path, and
the geometry sanity read-back (normal/direction/origin read off the CREATED datum).
"""

import collections
import io
import json
import math
import re

from conftest import (BRepEdge, BRepFace, Circle3D, Cone, Cylinder, FakePoint, FakeUnitsManager,
                      FakeVector3D, Line3D, MakeComp, Plane, _Vertex, install, load_tool,
                      make_design)

cn = load_tool("model_construction")


def _tool_source():
    return io.open(cn.__file__, encoding="utf-8").read()


class _CollOut:
    """Fakes a constructionPoints/Axes/Planes collection: createInput() returns an Inp that
    captures EVERY setBy* call's raw args, and returns `next_result` (default True, matching the
    live API returning true on success) - a test flips it False to drive the returned-false guard
    path. add() hands back an object whose `.geometry` is `result_geometry` (default None, so
    ``_geometry_readback`` degrades to {} exactly as it does for an un-modelled fake)."""
    def __init__(self):
        self.captured = None
        self.named = None
        self.next_result = True
        self.result_geometry = None
        self.proxy_geometry = None
        self.result_token = None      # the created datum's entityToken; None = an unreadable token
        self.result_definition = None  # the created datum's definition; None = nothing to read back
        self.no_to_object = False     # True models a ConstructionPointInput (no setByPathToObject)
        self.added = 0
    def createInput(self):
        self.captured = {}
        outer = self
        class Inp:
            # every setBy* is documented "Returns true if successful" - the fake answers, so a
            # handler that drops the answer can be caught dropping it
            def setByPoint(self, p):
                outer.captured["point"] = p
                return outer.next_result
            def setByLine(self, line):
                outer.captured["line"] = line
                return outer.next_result
            def setByEdge(self, edge):              # parametric-legal edge-axis path
                outer.captured["edge"] = edge
                return outer.next_result
            def setByOffset(self, base, val):
                outer.captured["offset"] = (base, val)
                return outer.next_result
            def setByAngle(self, linear, angle_val, planar):
                outer.captured["angle"] = (linear, angle_val, planar)
                return outer.next_result
            def setByThreePoints(self, p1, p2, p3):
                outer.captured["three_points"] = (p1, p2, p3)
                return outer.next_result
            def setByTwoPlanes(self, p1, p2):
                outer.captured["two_planes"] = (p1, p2)
                return outer.next_result
            def setByTangentAtPoint(self, face, pt):
                outer.captured["tangent_at_point"] = (face, pt)
                return outer.next_result
            def setByTwoEdges(self, e1, e2):
                outer.captured["two_edges"] = (e1, e2)
                return outer.next_result
            def setByCircularFace(self, face):
                outer.captured["circular_face"] = face
                return outer.next_result
            def setByTwoPoints(self, p1, p2):
                outer.captured["two_points"] = (p1, p2)
                return outer.next_result
            def setByPerpendicularAtPoint(self, face, pt):
                outer.captured["perpendicular_at_point"] = (face, pt)
                return outer.next_result
            def setByCenter(self, edge):
                outer.captured["center"] = edge
                return outer.next_result
            def setByThreePlanes(self, p1, p2, p3):
                outer.captured["three_planes"] = (p1, p2, p3)
                return outer.next_result
            def setByEdgePlane(self, edge, plane):
                outer.captured["edge_plane"] = (edge, plane)
                return outer.next_result
            def setByAngleOnCurvedFace(self, curved, angle_val, planar):
                outer.captured["angle_on_face"] = (curved, angle_val, planar)
                return outer.next_result
            def setByOffsetThroughPoint(self, planar, pt):
                outer.captured["offset_through_point"] = (planar, pt)
                return outer.next_result
            def setByPath(self, path, distance_type, distance_val):
                outer.captured["on_path"] = (path, distance_type, distance_val)
                return outer.next_result
            def setByPathToObject(self, path, to_object, offset_val):
                # PLANE inputs only - ConstructionPointInput carries no such member, and the fake
                # keeps that asymmetry by refusing to answer for a point (see _CollOut.no_to_object)
                if outer.no_to_object:
                    raise AttributeError("setByPathToObject")
                outer.captured["on_path_to_object"] = (path, to_object, offset_val)
                return outer.next_result
        self._inp = Inp()
        return self._inp
    def add(self, inp):
        # `result_geometry` is what plain .geometry reads. `proxy_geometry` is what
        # createForAssemblyContext(occurrence).geometry reads - the measured split: a datum created
        # while an occurrence is active reads component-LOCAL directly and WORLD through the proxy,
        # so a gate that skips the proxy compares two different spaces.
        self.added += 1
        proxy = (type("OProxy", (), {"name": "Datum", "geometry": self.proxy_geometry})()
                 if self.proxy_geometry is not None else None)
        obj = type("O", (), {"name": "Datum", "geometry": self.result_geometry,
                             "entityToken": self.result_token,
                             "definition": self.result_definition,
                             "createForAssemblyContext": lambda _s, _occ, _p=proxy: _p})()
        return obj


# An origin ConstructionPlane carries a .name; the payload publishes THAT (via
# _inputs.surface_ref_label) rather than echoing the request token back. An origin plane resolved
# from an 'xy'/'xz'/'yz' alias reads its name back UPPERCASE. A namedtuple keeps the tuple identity
# the setBy* argument assertions compare against while supplying that name.
_OriginPlane = collections.namedtuple("_OriginPlane", "kind name")


def _datum_component():
    """The component the datums are built on: the three createInput/add collections plus the origin
    planes an 'xy'/'xz'/'yz' alias resolves to."""
    comp = MakeComp(name="Comp", origin_planes=(_OriginPlane("plane", "XY"),
                                                _OriginPlane("plane", "XZ"),
                                                _OriginPlane("plane", "YZ")))
    comp.constructionPoints = _CollOut()
    comp.constructionAxes = _CollOut()
    comp.constructionPlanes = _CollOut()
    return comp


def _install(raise_env=False, design_type=0, active_occurrence=None):
    comp = _datum_component()
    if raise_env:
        def boom():
            raise RuntimeError("3 : Environment is not supported")
        comp.constructionPoints.createInput = boom
    design = make_design(comp=comp)
    # designType: 0 = Direct (setByPoint/setByLine legal), 1 = Parametric (they fail).
    # activeOccurrence is None when the ROOT component is active (the live contract) - set it to
    # drive the created datum's geometry through its assembly-context proxy.
    design.designType = design_type
    design.activeOccurrence = active_occurrence
    install(cn, design)
    import adsk.core
    adsk.core.Point3D.create = staticmethod(lambda x, y, z: ("pt", x, y, z))
    adsk.core.Vector3D.create = staticmethod(lambda x, y, z: ("vec", x, y, z))
    adsk.core.InfiniteLine3D.create = staticmethod(lambda o, d: ("line", o, d))
    adsk.core.ValueInput.createByReal = staticmethod(lambda v: ("real", v))
    adsk.core.ValueInput.createByString = staticmethod(lambda s: ("str", s))
    return comp


def _payload(result):
    assert result["isError"] is False, result
    return json.loads(result["content"][0]["text"])


# ── fakes for the new modes' typed-kind inputs ──────────────────────────────────────────────────
#
# The new modes resolve 'edges'/'points'/'face'/'plane2'/'plane3' through GeometryHandleList /
# GeometryHandle / PlaneRef - already unit-tested in test_inputs.py. So here (matching the file's
# own established shortcut - see TestParametricConstraint.test_edge_axis_...) we monkeypatch each
# shared kind's OWN .resolve() to hand back a controlled fake entity, and pin THIS tool's logic:
# mode dispatch, per-mode count/presence guards, the exact setBy* args, and the geometry read-back.
#
# Entities are built from conftest's SHARED, live-shape-checked fakes (BRepFace/BRepEdge wrapping
# a Plane/Cylinder/Cone/Line3D/Circle3D geometry object; FakePoint/FakeVector3D for coordinates) -
# never a parallel ad-hoc shape. A 'point' entity is a bare FakePoint: this tool never reads
# .geometry off a resolved point/vertex, only passes it straight through to a setBy* call, so a
# BRepVertex-shaped wrapper would add nothing (also matches Point3D's own measured shape).

def _planar_face(normal_xyz=(0, 0, 1), origin_xyz=None):
    origin = FakePoint(*origin_xyz) if origin_xyz else None
    return BRepFace(Plane(FakeVector3D(*normal_xyz), origin))


def _cylinder_face(axis_xyz=(0, 0, 1), origin_xyz=None):
    # Cylinder.origin is the centre of the base - a point ON the axis, which is what the
    # at_angle_on_face containment check projects onto the created plane.
    origin = FakePoint(*origin_xyz) if origin_xyz else None
    return BRepFace(Cylinder(FakeVector3D(*axis_xyz), origin))


def _cone_face(axis_xyz=(0, 0, 1)):
    return BRepFace(Cone(FakeVector3D(*axis_xyz)))


def _straight_edge():
    return BRepEdge(Line3D())


def _circular_edge():
    return BRepEdge(Circle3D(FakeVector3D(0, 0, 1)))


def _stub_resolve(monkeypatch, kind, value):
    """monkeypatch.setattr(cn.<KIND>, 'resolve', lambda v: (value, None)) - auto-restored."""
    monkeypatch.setattr(kind, "resolve", lambda raw: (value, None))


class TestGuards:
    def test_unknown_units(self):
        _install()
        res = cn.handler(kind="point", units="furlong")
        assert res["isError"] is True and "Unknown units" in res["message"]

    def test_unknown_kind(self):
        _install()
        res = cn.handler(kind="blob")
        assert res["isError"] is True and "Unknown kind" in res["message"]

    def test_bad_axis(self):
        # AxisRef owns the error: 'q' is neither a world axis nor a resolvable edge handle
        _install()
        res = cn.handler(kind="axis", axis="q")
        assert res["isError"] is True and "not a world axis" in res["message"]

    def test_bad_plane(self):
        # PlaneRef owns the error
        _install()
        res = cn.handler(kind="plane", plane="qq")
        assert res["isError"] is True and "not an origin alias" in res["message"]

    def test_direct_modeling_env_error_is_friendly(self):
        _install(raise_env=True)
        res = cn.handler(kind="point", x=1)
        assert res["isError"] is True
        assert "DIRECT-modeling" in res["message"] and "Parametric" in res["message"]


class TestConstruction:
    def test_point_scales_coords(self):
        comp = _install()
        out = _payload(cn.handler(kind="point", x=10, y=0, z=20, units="mm"))
        assert out["kind"] == "point"
        # 10mm,20mm -> 1.0cm, 2.0cm
        assert comp.constructionPoints.captured["point"] == ("pt", 1.0, 0.0, 2.0)

    def test_axis_direction_and_origin(self):
        comp = _install()
        out = _payload(cn.handler(kind="axis", x=5, axis="x", units="mm"))
        assert out["kind"] == "axis" and out["axis"] == "x"
        tag, origin, direction = comp.constructionAxes.captured["line"]
        assert origin == ("pt", 0.5, 0.0, 0.0)      # 5mm -> 0.5cm
        assert direction == ("vec", 1, 0, 0)

    def test_plane_offset_from_named_plane(self):
        comp = _install()
        out = _payload(cn.handler(kind="plane", plane="xz", offset=15, units="mm"))
        # 'XZ' - the resolved origin plane's own name, not the 'xz' alias that was asked for
        assert out["kind"] == "plane" and out["offset_from"] == "XZ"
        base, val = comp.constructionPlanes.captured["offset"]
        assert base == ("plane", "XZ") and val == ("real", 1.5)   # 15mm -> 1.5cm

    def test_offset_from_names_the_resolved_plane_not_the_request_token(self, monkeypatch):
        # 'plane' also takes a face handle; the payload reports what the input BECAME, so a
        # lowercased handle string can never reach it
        _install()
        _stub_resolve(monkeypatch, cn._PLANE, _planar_face())
        out = _payload(cn.handler(kind="plane", plane="<FACE-HANDLE-AbC>", offset=5))
        assert out["offset_from"] == "BRepFace"

    def test_point_scales_inches(self):
        comp = _install()
        _payload(cn.handler(kind="point", x=1, y=2, z=0, units="in"))
        # 1in -> 2.54cm, 2in -> 5.08cm
        assert comp.constructionPoints.captured["point"] == ("pt", 2.54, 5.08, 0.0)

    def test_axis_through_field_reports_raw_coords(self):
        _install()
        out = _payload(cn.handler(kind="axis", x=5, y=6, z=7, axis="y", units="mm"))
        # 'through' echoes the RAW (un-scaled) coordinates
        assert out["through"] == {"x": 5.0, "y": 6.0, "z": 7.0}

    def test_point_at_field_reports_raw_coords(self):
        _install()
        out = _payload(cn.handler(kind="point", x=3, y=4, z=5, units="mm"))
        assert out["at"] == {"x": 3.0, "y": 4.0, "z": 5.0}

    def test_custom_name_applied(self):
        comp = _install()
        # The created object names itself "Datum"; a custom name must overwrite it.
        captured = {}
        real_add = comp.constructionPoints.add
        def add(inp):
            obj = type("O", (), {})()
            obj.name = "Datum"
            return obj
        comp.constructionPoints.add = add
        out = _payload(cn.handler(kind="point", x=1, name="CrankPin"))
        assert out["name"] == "CrankPin"

    def test_generic_exception_is_reported(self):
        comp = _install()
        def boom():
            raise RuntimeError("kaboom-unexpected")
        comp.constructionPlanes.createInput = boom
        res = cn.handler(kind="plane", plane="xy", offset=1)
        assert res["isError"] is True
        assert "kaboom-unexpected" in res["message"]


# ── the direct-edit-only constraint ─────────────────────────────────────────────────────────────
#
# setByPoint(Point3D)/setByLine(InfiniteLine3D) FAIL in parametric mode (live API docstrings).
# These pin the contract: in parametric, refuse coordinate point/axis with an actionable message;
# the EDGE-axis path uses parametric-legal setByEdge; an offset plane works in BOTH modes.

class TestSetByRefusals:
    """A setBy* answering false means the definition was NOT accepted. The handler reports that and
    never reaches add() - a datum built from an input Fusion refused is not the one asked for."""

    def test_a_refused_offset_is_an_error(self):
        comp = _install()
        comp.constructionPlanes.next_result = False
        res = cn.handler(kind="plane", mode="offset", plane="xy", offset=10)
        assert res["isError"] is True and "setByOffset returned false" in res["message"]
        assert comp.constructionPlanes.added == 0

    def test_a_refused_point_is_an_error(self):
        comp = _install()
        comp.constructionPoints.next_result = False
        res = cn.handler(kind="point", mode="coordinate", x=1, y=2, z=3)
        assert res["isError"] is True and "setByPoint returned false" in res["message"]
        # the mode it names must be one an agent can actually pass back
        assert "mode='coordinate'" in res["message"]
        assert comp.constructionPoints.added == 0

    def test_a_refused_world_axis_names_the_world_mode(self):
        comp = _install()
        comp.constructionAxes.next_result = False
        res = cn.handler(kind="axis", axis="x", x=1)
        assert res["isError"] is True and "setByLine returned false" in res["message"]
        assert "mode='world'" in res["message"]
        assert comp.constructionAxes.added == 0

    def test_every_refusal_names_a_mode_the_tool_accepts(self):
        # a refusal naming a mode that is not in the vocabulary sends an agent to a dead end
        legal = set(cn._MODE_OPTIONS)
        named = set(re.findall(r"mode='([a-z_]+)'", _tool_source()))
        assert named <= legal, f"errors name modes that do not exist: {sorted(named - legal)}"

class TestParametricConstraint:
    def test_point_at_coord_refused_in_parametric(self):
        comp = _install(design_type=1)            # parametric
        res = cn.handler(kind="point", x=10, y=0, z=20)
        assert res["isError"] is True
        assert "DIRECT" in res["message"] and "sketch" in res["message"].lower()
        # and it must NOT have attempted the doomed setByPoint
        assert comp.constructionPoints.captured is None

    def test_world_axis_at_coord_refused_in_parametric(self):
        comp = _install(design_type=1)
        res = cn.handler(kind="axis", x=5, axis="x")
        assert res["isError"] is True and "DIRECT" in res["message"]
        assert comp.constructionAxes.captured is None

    def test_point_at_coord_works_in_direct(self):
        comp = _install(design_type=0)            # direct
        out = _payload(cn.handler(kind="point", x=10, y=0, z=20, units="mm"))
        assert out["kind"] == "point"
        assert comp.constructionPoints.captured["point"] == ("pt", 1.0, 0.0, 2.0)

    def test_edge_axis_uses_setByEdge_and_works_in_parametric(self):
        # An edge handle is parametric-legal via setByEdge. Patch AxisRef to resolve to an edge.
        comp = _install(design_type=1)            # parametric — should still succeed
        real_resolve = cn._AXIS.resolve
        cn._AXIS.resolve = lambda v: (("edge", "EDGE_HANDLE"), None)
        try:
            out = _payload(cn.handler(kind="axis", axis="<edge-handle>"))
        finally:
            cn._AXIS.resolve = real_resolve
        assert out["kind"] == "axis"
        assert comp.constructionAxes.captured["edge"] == "EDGE_HANDLE"   # setByEdge, not setByLine
        assert "line" not in comp.constructionAxes.captured

    def test_offset_plane_works_in_parametric(self):
        comp = _install(design_type=1)
        out = _payload(cn.handler(kind="plane", plane="xz", offset=15, units="mm"))
        assert out["kind"] == "plane"
        base, val = comp.constructionPlanes.captured["offset"]
        assert val == ("real", 1.5)


# ── mode-vs-kind dispatch guard ─────────────────────────────────────────────────────────────────

class TestModeKindValidation:
    def test_mode_invalid_for_kind_is_refused(self):
        _install()
        res = cn.handler(kind="axis", mode="offset")
        assert res["isError"] is True
        assert "not valid for kind='axis'" in res["message"]
        assert "circular_face" in res["message"]      # lists the LEGAL modes for axis

    def test_unknown_mode_is_refused(self):
        _install()
        res = cn.handler(kind="plane", mode="not_a_real_mode")
        assert res["isError"] is True
        assert "not valid for kind='plane'" in res["message"]

    def test_two_edges_mode_routes_to_the_right_collection_per_kind(self, monkeypatch):
        # 'two_edges' is a legal mode for BOTH plane and point - each call must hit its OWN
        # collection, never the other kind's.
        comp = _install()
        e1, e2 = _straight_edge(), _straight_edge()
        _stub_resolve(monkeypatch, cn._EDGES, [e1, e2])
        _payload(cn.handler(kind="plane", mode="two_edges"))
        assert "two_edges" in comp.constructionPlanes.captured
        assert comp.constructionPoints.captured is None
        _payload(cn.handler(kind="point", mode="two_edges"))
        assert "two_edges" in comp.constructionPoints.captured


# ── plane modes ──────────────────────────────────────────────────────────────────────────────────

class TestPlaneAtAngle:
    def test_calls_setByAngle_with_radians_and_reports_normal_changed(self, monkeypatch):
        comp = _install()
        base_plane = _planar_face((0, 0, 1))
        _stub_resolve(monkeypatch, cn._PLANE, base_plane)
        edge = _straight_edge()
        _stub_resolve(monkeypatch, cn._EDGES, [edge])
        comp.constructionPlanes.result_geometry = Plane(FakeVector3D(1, 0, 0), FakePoint(0, 0, 0))
        out = _payload(cn.handler(kind="plane", mode="at_angle", angle=45))
        assert out["kind"] == "plane" and out["mode"] == "at_angle"
        linear, angle_val, planar = comp.constructionPlanes.captured["angle"]
        assert linear is edge and planar is base_plane
        assert math.isclose(angle_val[1], math.radians(45))    # ValueInput.createByReal(radians)
        assert out["angle_deg"] == 45.0
        # the rotated plane's normal (1,0,0) differs from the base plane's (0,0,1) - proves the
        # rotation actually took, the exact sanity check the write-honesty bar asks for.
        assert out["normal_changed"] is True

    def test_needs_exactly_one_edge(self, monkeypatch):
        _install()
        _stub_resolve(monkeypatch, cn._EDGES, [])
        res = cn.handler(kind="plane", mode="at_angle", angle=10)
        assert res["isError"] is True
        assert "needs exactly 1 'edges'" in res["message"]

    def test_setByAngle_false_is_reported(self, monkeypatch):
        comp = _install()
        _stub_resolve(monkeypatch, cn._EDGES, [_straight_edge()])
        comp.constructionPlanes.next_result = False
        res = cn.handler(kind="plane", mode="at_angle", angle=10)
        assert res["isError"] is True and "setByAngle returned false" in res["message"]


class TestPlaneAtAngleOnFace:
    """setByAngleOnCurvedFace rotates the plane about the axis INFERRED from a cylindrical/conical
    face, measured from 'plane'. createByReal takes radians; the created plane contains that axis."""

    def test_calls_setByAngleOnCurvedFace_with_radians_and_reports_containment(self, monkeypatch):
        comp = _install()
        base_plane = _planar_face((0, 1, 0))
        _stub_resolve(monkeypatch, cn._PLANE, base_plane)
        face = _cylinder_face((0, 0, 1), (0, 0, 0))
        _stub_resolve(monkeypatch, cn._FACE, face)
        # a plane whose normal is perpendicular to the +Z axis and whose origin sits on that axis
        comp.constructionPlanes.result_geometry = Plane(FakeVector3D(1, 0, 0), FakePoint(0, 0, 5))
        out = _payload(cn.handler(kind="plane", mode="at_angle_on_face", plane="xz", angle=90))
        curved, angle_val, planar = comp.constructionPlanes.captured["angle_on_face"]
        assert curved is face and planar is base_plane
        assert math.isclose(angle_val[1], math.radians(90))     # createByReal(RADIANS)
        assert out["angle_deg"] == 90.0
        # the zero-angle reference is the planarEntity, so the payload names the RESOLVED plane -
        # a face handle became a BRepFace, and echoing the request token back would say '<face-h>'
        assert out["angle_from"] == "BRepFace"
        assert out["contains_face_axis"] is True

    def test_containment_is_false_when_the_normal_runs_along_the_face_axis(self, monkeypatch):
        # ORIENTATION alone is wrong: the plane's origin sits exactly ON the axis, so only the
        # normal-vs-axis term can reject it
        comp = _install()
        _stub_resolve(monkeypatch, cn._PLANE, _planar_face((0, 1, 0)))
        _stub_resolve(monkeypatch, cn._FACE, _cylinder_face((0, 0, 1), (0, 0, 0)))
        comp.constructionPlanes.result_geometry = Plane(FakeVector3D(0, 0, 1), FakePoint(0, 0, 0))
        out = _payload(cn.handler(kind="plane", mode="at_angle_on_face", angle=90))
        assert out["contains_face_axis"] is False

    def test_containment_is_false_when_the_plane_misses_the_axis_point(self, monkeypatch):
        # POSITION alone is wrong: the axis direction lies in the plane, so only the on-the-axis
        # term can reject it
        comp = _install()
        _stub_resolve(monkeypatch, cn._PLANE, _planar_face((0, 1, 0)))
        _stub_resolve(monkeypatch, cn._FACE, _cylinder_face((0, 0, 1), (0, 0, 0)))
        comp.constructionPlanes.result_geometry = Plane(FakeVector3D(1, 0, 0), FakePoint(3, 0, 5))
        out = _payload(cn.handler(kind="plane", mode="at_angle_on_face", angle=90))
        assert out["contains_face_axis"] is False

    def test_containment_survives_an_off_axis_cylinder_at_a_long_lever_arm(self, monkeypatch):
        # a normal whose components do not round exactly, 50 cm from the axis point: rounding the
        # gate's vectors to display precision reports a miss on a plane that exactly contains the
        # axis. Axis (1,2,3)/|.|, an in-plane normal perpendicular to it, origin 50 cm along the axis.
        comp = _install()
        m = 14.0 ** 0.5
        _stub_resolve(monkeypatch, cn._PLANE, _planar_face((0, 1, 0)))
        _stub_resolve(monkeypatch, cn._FACE, _cylinder_face((1 / m, 2 / m, 3 / m), (0, 0, 0)))
        # (2,-1,0) is perpendicular to (1,2,3); the origin sits 50 cm along the axis itself
        n = 5.0 ** 0.5
        comp.constructionPlanes.result_geometry = Plane(
            FakeVector3D(2 / n, -1 / n, 0.0), FakePoint(50 / m, 100 / m, 150 / m))
        out = _payload(cn.handler(kind="plane", mode="at_angle_on_face", angle=30))
        assert out["contains_face_axis"] is True

    def test_containment_reads_the_datum_through_its_assembly_context(self, monkeypatch):
        # with an occurrence active the datum's own .geometry is component-LOCAL while the resolved
        # face reads WORLD; only the assembly-context proxy puts both in one space
        comp = _install(active_occurrence=("occ", "CompC:1"))
        _stub_resolve(monkeypatch, cn._PLANE, _planar_face((0, 1, 0)))
        _stub_resolve(monkeypatch, cn._FACE, _cylinder_face((0, 0, 1), (0, 0, 0)))
        comp.constructionPlanes.result_geometry = Plane(FakeVector3D(1, 0, 0), FakePoint(3, 0, 5))
        comp.constructionPlanes.proxy_geometry = Plane(FakeVector3D(1, 0, 0), FakePoint(0, 0, 5))
        out = _payload(cn.handler(kind="plane", mode="at_angle_on_face", angle=30))
        assert out["contains_face_axis"] is True

    def test_containment_is_withheld_when_the_context_proxy_cannot_be_built(self, monkeypatch):
        # comparing across two spaces would be a fabricated verdict either way - so the claim is
        # published as an explicit null with the reason, not dropped (a missing key reads as
        # "not applicable" when the truth is "not checked")
        comp = _install(active_occurrence=("occ", "CompC:1"))
        _stub_resolve(monkeypatch, cn._PLANE, _planar_face((0, 1, 0)))
        _stub_resolve(monkeypatch, cn._FACE, _cylinder_face((0, 0, 1), (0, 0, 0)))
        comp.constructionPlanes.result_geometry = Plane(FakeVector3D(1, 0, 0), FakePoint(0, 0, 5))
        out = _payload(cn.handler(kind="plane", mode="at_angle_on_face", angle=30))
        assert out["contains_face_axis"] is None
        assert "NOT measured" in out["note"] and "occurrence is active" in out["note"]
        assert "space_unread" not in out          # the flag drives the note, it is not wire noise

    def test_rejects_planar_face(self, monkeypatch):
        _install()
        _stub_resolve(monkeypatch, cn._FACE, _planar_face())
        res = cn.handler(kind="plane", mode="at_angle_on_face", angle=30)
        assert res["isError"] is True
        assert "CYLINDRICAL or CONICAL" in res["message"] and "planar" in res["message"]

    def test_a_degenerate_reference_plane_surfaces_the_platform_refusal(self, monkeypatch):
        # a reference plane whose normal is parallel to the inferred axis leaves the angle
        # undefined: setByAngleOnCurvedFace returns true and add() raises. The tool carries no
        # pre-guard, so the platform's self-naming message must reach the caller intact.
        comp = _install()
        _stub_resolve(monkeypatch, cn._PLANE, _planar_face((0, 0, 1)))
        _stub_resolve(monkeypatch, cn._FACE, _cylinder_face((0, 0, 1), (0, 0, 0)))
        def boom(inp):
            raise RuntimeError("3 : reference planarEntity must not be perpendicular with axis input")
        comp.constructionPlanes.add = boom
        res = cn.handler(kind="plane", mode="at_angle_on_face", plane="xy", angle=30)
        assert res["isError"] is True
        assert "reference planarEntity must not be perpendicular" in res["message"]

    def test_accepts_conical_face(self, monkeypatch):
        comp = _install()
        _stub_resolve(monkeypatch, cn._FACE, _cone_face())
        out = _payload(cn.handler(kind="plane", mode="at_angle_on_face", angle=30))
        assert out["mode"] == "at_angle_on_face"
        assert "angle_on_face" in comp.constructionPlanes.captured

    def test_needs_face(self):
        _install()
        res = cn.handler(kind="plane", mode="at_angle_on_face", angle=30)
        assert res["isError"] is True and "needs 'face'" in res["message"]

    def test_setByAngleOnCurvedFace_false_is_reported(self, monkeypatch):
        comp = _install()
        _stub_resolve(monkeypatch, cn._FACE, _cylinder_face())
        comp.constructionPlanes.next_result = False
        res = cn.handler(kind="plane", mode="at_angle_on_face", angle=30)
        assert res["isError"] is True
        assert "setByAngleOnCurvedFace returned false" in res["message"]


class TestPlaneOffsetThroughPoint:
    """The point DEFINES the offset, so the created plane passes through it exactly - a plane that
    misses it is a success report over the wrong geometry and must come back as an error."""

    def test_calls_setByOffsetThroughPoint_and_confirms_the_plane_passes_through_it(self, monkeypatch):
        comp = _install()
        v = _Vertex(FakePoint(0, 0, 2))
        _stub_resolve(monkeypatch, cn._POINTS, [v])
        comp.constructionPlanes.result_geometry = Plane(FakeVector3D(0, 0, 1), FakePoint(0, 0, 2))
        out = _payload(cn.handler(kind="plane", mode="offset_through_point", plane="xy"))
        planar, pt = comp.constructionPlanes.captured["offset_through_point"]
        assert planar == ("plane", "XY") and pt is v
        assert out["passes_through_point"] is True

    def test_an_exactly_on_plane_vertex_survives_a_skew_normal_at_a_long_lever_arm(self, monkeypatch):
        # the gate's own precision, not Fusion's: a normal whose components do not round exactly
        # (1,2,3)/|.| plus a lever arm of tens of cm turns display rounding into a phantom miss.
        # The vertex is constructed exactly ON the plane, so the ONLY honest answer is ok.
        comp = _install()
        m = 14.0 ** 0.5
        # (3,6,-5) is perpendicular to (1,2,3) with no zero component, so no pair of rounding
        # errors can cancel: 40 cm along it stays exactly in the plane, and a display-rounded
        # normal reports it 2.4e-5 cm off.
        s = 40.0 / (70.0 ** 0.5)
        origin = FakePoint(1, 2, 3)
        on_plane = FakePoint(1 + 3 * s, 2 + 6 * s, 3 - 5 * s)
        _stub_resolve(monkeypatch, cn._POINTS, [_Vertex(on_plane)])
        comp.constructionPlanes.result_geometry = Plane(FakeVector3D(1 / m, 2 / m, 3 / m), origin)
        out = _payload(cn.handler(kind="plane", mode="offset_through_point", plane="xy"))
        assert out["passes_through_point"] is True

    def test_the_gate_reads_the_datum_through_its_assembly_context(self, monkeypatch):
        # measured: with an occurrence active the plane's own .geometry is component-LOCAL while a
        # proxy-resolved vertex reads WORLD, and the two differ by exactly the occurrence offset -
        # comparing them directly fails every correct call into a transformed component.
        comp = _install(active_occurrence=("occ", "CompC:1"))
        _stub_resolve(monkeypatch, cn._POINTS, [_Vertex(FakePoint(6, 3, 2))])
        comp.constructionPlanes.result_geometry = Plane(FakeVector3D(0, 0, 1), FakePoint(0, 0, -1))
        comp.constructionPlanes.proxy_geometry = Plane(FakeVector3D(0, 0, 1), FakePoint(0, 0, 2))
        out = _payload(cn.handler(kind="plane", mode="offset_through_point", plane="xy"))
        assert out["passes_through_point"] is True

    def test_the_claim_is_withheld_when_the_context_proxy_cannot_be_built(self, monkeypatch):
        comp = _install(active_occurrence=("occ", "CompC:1"))
        _stub_resolve(monkeypatch, cn._POINTS, [_Vertex(FakePoint(0, 0, 2))])
        comp.constructionPlanes.result_geometry = Plane(FakeVector3D(0, 0, 1), FakePoint(0, 0, 2))
        out = _payload(cn.handler(kind="plane", mode="offset_through_point", plane="xy"))
        assert out["passes_through_point"] is None       # null + reason, not a missing key
        assert "NOT measured" in out["note"]

    def test_a_plane_that_misses_the_point_is_an_error(self, monkeypatch):
        comp = _install()
        _stub_resolve(monkeypatch, cn._POINTS, [_Vertex(FakePoint(0, 0, 2))])
        # created 1 cm short of the vertex
        comp.constructionPlanes.result_geometry = Plane(FakeVector3D(0, 0, 1), FakePoint(0, 0, 1))
        res = cn.handler(kind="plane", mode="offset_through_point", plane="xy")
        assert res["isError"] is True
        assert "misses the point by 1.000000 cm" in res["message"]
        assert "still in the design" in res["message"]

    def test_unreadable_point_geometry_omits_the_claim_rather_than_faking_it(self, monkeypatch):
        # a resolved point whose coordinates cannot be read proves nothing either way
        comp = _install()
        _stub_resolve(monkeypatch, cn._POINTS, [FakePoint(0, 0, 2)])
        comp.constructionPlanes.result_geometry = Plane(FakeVector3D(0, 0, 1), FakePoint(0, 0, 9))
        out = _payload(cn.handler(kind="plane", mode="offset_through_point", plane="xy"))
        assert "passes_through_point" not in out

    def test_needs_exactly_one_point(self, monkeypatch):
        _install()
        _stub_resolve(monkeypatch, cn._POINTS, [_Vertex(FakePoint()), _Vertex(FakePoint())])
        res = cn.handler(kind="plane", mode="offset_through_point")
        assert res["isError"] is True and "needs exactly 1 'points'" in res["message"]

    def test_setByOffsetThroughPoint_false_is_reported(self, monkeypatch):
        comp = _install()
        _stub_resolve(monkeypatch, cn._POINTS, [_Vertex(FakePoint())])
        comp.constructionPlanes.next_result = False
        res = cn.handler(kind="plane", mode="offset_through_point")
        assert res["isError"] is True
        assert "setByOffsetThroughPoint returned false" in res["message"]


class TestPlaneThreePoints:
    def test_calls_setByThreePoints(self, monkeypatch):
        comp = _install()
        v1, v2, v3 = FakePoint(), FakePoint(), FakePoint()
        _stub_resolve(monkeypatch, cn._POINTS, [v1, v2, v3])
        out = _payload(cn.handler(kind="plane", mode="three_points"))
        assert out["mode"] == "three_points" and out["point_count"] == 3
        assert comp.constructionPlanes.captured["three_points"] == (v1, v2, v3)

    def test_needs_exactly_three_points(self, monkeypatch):
        _install()
        _stub_resolve(monkeypatch, cn._POINTS, [FakePoint(), FakePoint()])
        res = cn.handler(kind="plane", mode="three_points")
        assert res["isError"] is True
        assert "needs exactly 3 'points'" in res["message"]

    def test_setByThreePoints_false_is_reported(self, monkeypatch):
        comp = _install()
        _stub_resolve(monkeypatch, cn._POINTS, [FakePoint(), FakePoint(), FakePoint()])
        comp.constructionPlanes.next_result = False
        res = cn.handler(kind="plane", mode="three_points")
        assert res["isError"] is True and "setByThreePoints returned false" in res["message"]


class TestPlaneMidplane:
    def test_calls_setByTwoPlanes(self, monkeypatch):
        comp = _install()
        p2 = _planar_face()
        _stub_resolve(monkeypatch, cn._PLANE2, p2)
        out = _payload(cn.handler(kind="plane", mode="midplane", plane="xz"))
        assert out["mode"] == "midplane"
        p1, p2_captured = comp.constructionPlanes.captured["two_planes"]
        assert p1 == ("plane", "XZ") and p2_captured is p2

    def test_needs_plane2(self):
        _install()
        res = cn.handler(kind="plane", mode="midplane")     # plane2 omitted -> resolves to None
        assert res["isError"] is True and "needs 'plane2'" in res["message"]


class TestPlaneTangentAtPoint:
    def test_calls_setByTangentAtPoint(self, monkeypatch):
        comp = _install()
        face = _cylinder_face()
        _stub_resolve(monkeypatch, cn._FACE, face)
        v = FakePoint()
        _stub_resolve(monkeypatch, cn._POINTS, [v])
        out = _payload(cn.handler(kind="plane", mode="tangent_at_point"))
        assert out["mode"] == "tangent_at_point"
        f, pt = comp.constructionPlanes.captured["tangent_at_point"]
        assert f is face and pt is v

    def test_rejects_planar_face(self, monkeypatch):
        _install()
        _stub_resolve(monkeypatch, cn._FACE, _planar_face())
        _stub_resolve(monkeypatch, cn._POINTS, [FakePoint()])
        res = cn.handler(kind="plane", mode="tangent_at_point")
        assert res["isError"] is True
        assert "CYLINDRICAL or CONICAL" in res["message"] and "planar" in res["message"]

    def test_needs_face(self, monkeypatch):
        _install()
        _stub_resolve(monkeypatch, cn._POINTS, [FakePoint()])
        res = cn.handler(kind="plane", mode="tangent_at_point")
        assert res["isError"] is True and "needs 'face'" in res["message"]


class TestPlaneTwoEdges:
    def test_calls_setByTwoEdges(self, monkeypatch):
        comp = _install()
        e1, e2 = _straight_edge(), _straight_edge()
        _stub_resolve(monkeypatch, cn._EDGES, [e1, e2])
        out = _payload(cn.handler(kind="plane", mode="two_edges"))
        assert out["mode"] == "two_edges"
        assert comp.constructionPlanes.captured["two_edges"] == (e1, e2)

    def test_needs_exactly_two_edges(self, monkeypatch):
        # the "too many" side of the exact-count guard (0/1 cases are covered elsewhere).
        _install()
        _stub_resolve(monkeypatch, cn._EDGES, [_straight_edge()] * 3)
        res = cn.handler(kind="plane", mode="two_edges")
        assert res["isError"] is True and "needs exactly 2 'edges'" in res["message"]


# ── axis modes ───────────────────────────────────────────────────────────────────────────────────

class TestAxisCircularFace:
    def test_calls_setByCircularFace_and_reports_alignment(self, monkeypatch):
        comp = _install()
        face = _cylinder_face((0, 0, 1))
        _stub_resolve(monkeypatch, cn._FACE, face)
        comp.constructionAxes.result_geometry = type(
            "G", (), {"direction": FakeVector3D(0, 0, 1), "origin": FakePoint(0, 0, 0)})()
        out = _payload(cn.handler(kind="axis", mode="circular_face"))
        assert out["mode"] == "circular_face"
        assert comp.constructionAxes.captured["circular_face"] is face
        assert out["aligned_to_face_axis"] is True

    def test_rejects_planar_face(self, monkeypatch):
        _install()
        _stub_resolve(monkeypatch, cn._FACE, _planar_face())
        res = cn.handler(kind="axis", mode="circular_face")
        assert res["isError"] is True and "CYLINDRICAL or CONICAL" in res["message"]

    def test_accepts_conical_face(self, monkeypatch):
        comp = _install()
        _stub_resolve(monkeypatch, cn._FACE, _cone_face())
        out = _payload(cn.handler(kind="axis", mode="circular_face"))
        assert out["mode"] == "circular_face"
        assert "circular_face" in comp.constructionAxes.captured

    def test_needs_face(self):
        _install()
        res = cn.handler(kind="axis", mode="circular_face")
        assert res["isError"] is True and "needs 'face'" in res["message"]


class TestAxisTwoPoints:
    def test_calls_setByTwoPoints(self, monkeypatch):
        comp = _install()
        v1, v2 = FakePoint(), FakePoint()
        _stub_resolve(monkeypatch, cn._POINTS, [v1, v2])
        out = _payload(cn.handler(kind="axis", mode="two_points"))
        assert out["mode"] == "two_points"
        assert comp.constructionAxes.captured["two_points"] == (v1, v2)

    def test_needs_exactly_two_points(self, monkeypatch):
        _install()
        _stub_resolve(monkeypatch, cn._POINTS, [FakePoint()])
        res = cn.handler(kind="axis", mode="two_points")
        assert res["isError"] is True and "needs exactly 2 'points'" in res["message"]

    def test_setByTwoPoints_false_is_reported(self, monkeypatch):
        comp = _install()
        _stub_resolve(monkeypatch, cn._POINTS, [FakePoint(), FakePoint()])
        comp.constructionAxes.next_result = False
        res = cn.handler(kind="axis", mode="two_points")
        assert res["isError"] is True and "setByTwoPoints returned false" in res["message"]


class TestAxisTwoPlanes:
    def test_calls_setByTwoPlanes(self, monkeypatch):
        comp = _install()
        p2 = _planar_face()
        _stub_resolve(monkeypatch, cn._PLANE2, p2)
        out = _payload(cn.handler(kind="axis", mode="two_planes", plane="yz"))
        assert out["mode"] == "two_planes"
        p1, p2_captured = comp.constructionAxes.captured["two_planes"]
        assert p1 == ("plane", "YZ") and p2_captured is p2

    def test_needs_plane2(self):
        _install()
        res = cn.handler(kind="axis", mode="two_planes")
        assert res["isError"] is True and "needs 'plane2'" in res["message"]


class TestAxisPerpendicularAtPoint:
    def test_calls_setByPerpendicularAtPoint_and_reports_alignment(self, monkeypatch):
        comp = _install()
        face = _planar_face((0, 0, 1))
        _stub_resolve(monkeypatch, cn._FACE, face)
        v = FakePoint()
        _stub_resolve(monkeypatch, cn._POINTS, [v])
        comp.constructionAxes.result_geometry = type(
            "G", (), {"direction": FakeVector3D(0, 0, 1), "origin": FakePoint(0, 0, 0)})()
        out = _payload(cn.handler(kind="axis", mode="perpendicular_at_point"))
        assert out["mode"] == "perpendicular_at_point"
        f, pt = comp.constructionAxes.captured["perpendicular_at_point"]
        assert f is face and pt is v
        assert out["aligned_to_face_normal"] is True

    def test_needs_exactly_one_point(self, monkeypatch):
        _install()
        _stub_resolve(monkeypatch, cn._FACE, _planar_face())
        _stub_resolve(monkeypatch, cn._POINTS, [])
        res = cn.handler(kind="axis", mode="perpendicular_at_point")
        assert res["isError"] is True and "needs exactly 1 'points'" in res["message"]


# ── point modes ──────────────────────────────────────────────────────────────────────────────────

class TestPointCircleCenter:
    def test_calls_setByCenter(self, monkeypatch):
        comp = _install()
        edge = _circular_edge()
        _stub_resolve(monkeypatch, cn._EDGES, [edge])
        out = _payload(cn.handler(kind="point", mode="circle_center"))
        assert out["mode"] == "circle_center"
        assert comp.constructionPoints.captured["center"] is edge

    def test_rejects_straight_edge(self, monkeypatch):
        _install()
        _stub_resolve(monkeypatch, cn._EDGES, [_straight_edge()])
        res = cn.handler(kind="point", mode="circle_center")
        assert res["isError"] is True
        assert "CIRCULAR" in res["message"] and "straight" in res["message"]


class TestPointTwoEdges:
    def test_calls_setByTwoEdges(self, monkeypatch):
        comp = _install()
        e1, e2 = _straight_edge(), _straight_edge()
        _stub_resolve(monkeypatch, cn._EDGES, [e1, e2])
        out = _payload(cn.handler(kind="point", mode="two_edges"))
        assert out["mode"] == "two_edges"
        assert comp.constructionPoints.captured["two_edges"] == (e1, e2)

    def test_needs_exactly_two_edges(self, monkeypatch):
        _install()
        _stub_resolve(monkeypatch, cn._EDGES, [])
        res = cn.handler(kind="point", mode="two_edges")
        assert res["isError"] is True and "needs exactly 2 'edges'" in res["message"]


class TestPointThreePlanes:
    def test_calls_setByThreePlanes(self, monkeypatch):
        comp = _install()
        p2, p3 = _planar_face(), _planar_face()
        _stub_resolve(monkeypatch, cn._PLANE2, p2)
        _stub_resolve(monkeypatch, cn._PLANE3, p3)
        out = _payload(cn.handler(kind="point", mode="three_planes", plane="xz"))
        assert out["mode"] == "three_planes"
        p1, p2_captured, p3_captured = comp.constructionPoints.captured["three_planes"]
        assert p1 == ("plane", "XZ") and p2_captured is p2 and p3_captured is p3

    def test_needs_plane3(self, monkeypatch):
        _install()
        _stub_resolve(monkeypatch, cn._PLANE2, _planar_face())
        res = cn.handler(kind="point", mode="three_planes")
        assert res["isError"] is True and "needs 'plane3'" in res["message"]


class TestPointEdgePlane:
    def test_calls_setByEdgePlane(self, monkeypatch):
        comp = _install()
        edge = _straight_edge()
        _stub_resolve(monkeypatch, cn._EDGES, [edge])
        out = _payload(cn.handler(kind="point", mode="edge_plane", plane="xz"))
        assert out["mode"] == "edge_plane"
        e, p = comp.constructionPoints.captured["edge_plane"]
        assert e is edge and p == ("plane", "XZ")

    def test_needs_exactly_one_edge(self, monkeypatch):
        _install()
        _stub_resolve(monkeypatch, cn._EDGES, [])
        res = cn.handler(kind="point", mode="edge_plane")
        assert res["isError"] is True and "needs exactly 1 'edges'" in res["message"]


# ── on_path (legal for BOTH plane and point) ────────────────────────────────────────────────────
#
# setByPath takes the shared path resolver's adsk.fusion.Path, a PathDistanceTypes member, and the
# distance. Proportional reads a 0-1 ratio and RAISES outside it (the raise rolls the whole call's
# transaction back, so the range must be refused before the call); absolute reads a length from the
# path start and is NOT clamped at the end. setByPathToObject is a PLANE-only member.

_DEFAULT_PATH = object()


def _stub_path(monkeypatch, path=_DEFAULT_PATH, label="1 edge(s)", err=None):
    """Stub the shared path resolver. The default path is an opaque object with no readable curve
    evaluator - the shape a path takes when its length cannot be measured; pass _measurable_path()
    for one that can."""
    if path is _DEFAULT_PATH:
        path = type("Path", (), {})()
    monkeypatch.setattr(cn._common, "build_path", lambda comp, raw: (path, label, err))
    return path


def _param(name, expression, value=None):
    """A ModelParameter behind an on-path placement: the landed expression plus its dNN name.
    `value` is the parameter's INTERNAL value - cm for an absolute distance, the bare ratio for a
    proportional one - which is what the path-extent comparison reads."""
    return type("MP", (), {"name": name, "expression": expression, "value": value})()


def _raising_param(name, expression):
    """A ModelParameter that is PRESENT but whose .value raises - the shape a stale/deleted proxy
    takes live ('An API Object refers to a deleted Object'). Distinct from a readable None: a read
    that raises must still yield no verdict."""
    def boom(_self):
        raise RuntimeError("4 : An API Object refers to a deleted Object")
    return type("MP", (), {"name": name, "expression": expression, "value": property(boom)})()


def _measurable_path(*entity_lengths_cm):
    """A Path whose entities carry real curve evaluators, so the tool can measure its total length
    (Path itself has no length member): getParameterExtents answers (True, start, end) and
    getLengthAtParameter answers (True, length) over that span."""
    curves = []
    for length in entity_lengths_cm:
        ev = type("Ev", (), {
            "getParameterExtents": lambda _s: (True, 0.0, 1.0),
            "getLengthAtParameter": lambda _s, a, b, _n=length: (True, _n)})()
        curves.append(type("PE", (), {"curve": type("C", (), {"evaluator": ev})()})())
    return type("Path", (), {"count": len(curves),
                             "item": lambda _s, i, _c=curves: _c[i]})()


def _path_with_one_unreadable_entity(*entity_lengths_cm, unreadable_at=1):
    """A measurable path where ONE entity's item() raises - a stale entity proxy. The other
    segments still measure, so a walk that skips the bad one produces a plausible but SHORT total."""
    full = _measurable_path(*entity_lengths_cm)

    def item(_s, i):
        if i == unreadable_at:
            raise RuntimeError("4 : An API Object refers to a deleted Object")
        return full.item(i)
    return type("Path", (), {"count": len(entity_lengths_cm), "item": item})()


def _path_defn(distance, **offset):
    """A ConstructionPlanePathDefinition / ConstructionPointPathDefinition. Pass offset=... for the
    plane definition's offset member (None unless the plane was built to an object); OMIT it for a
    point definition, which carries no such member at all."""
    return type("Def", (), dict(distance=distance, **offset))()


def _PDT(name):
    return getattr(cn.adsk.fusion.PathDistanceTypes, name)


class TestOnPath:
    def test_plane_proportional_passes_the_ratio_and_the_proportional_type(self, monkeypatch):
        comp = _install()
        path = _stub_path(monkeypatch)
        out = _payload(cn.handler(kind="plane", mode="on_path", path="<edge-handle>", at=0.5))
        p, dtype, val = comp.constructionPlanes.captured["on_path"]
        assert p is path and val == ("real", 0.5)     # createByReal(ratio), NOT a scaled length
        assert dtype is _PDT("ProportionalPathDistanceType")
        # 'at_ratio', not 'at': the coordinate mode's top-level 'at' is a point, so one payload key
        # would otherwise mean a fraction in one mode and a coordinate in another
        assert out["at_ratio"] == 0.5 and out["path"] == "1 edge(s)"
        assert out["distance_type"] == "proportional" and "at" not in out
        # the datum is added to the PLANE collection - a plane input handed to another collection
        # is a different datum kind than the one asked for
        assert comp.constructionPlanes.added == 1 and comp.constructionPoints.added == 0

    def test_point_routes_to_its_own_collection(self, monkeypatch):
        comp = _install()
        path = _stub_path(monkeypatch)
        out = _payload(cn.handler(kind="point", mode="on_path", path="<edge-handle>", at=0.25))
        p, dtype, val = comp.constructionPoints.captured["on_path"]
        assert p is path and val == ("real", 0.25)
        assert dtype is _PDT("ProportionalPathDistanceType")
        assert comp.constructionPlanes.captured is None
        assert comp.constructionPoints.added == 1 and comp.constructionPlanes.added == 0
        assert out["mode"] == "on_path"
        # the POINT kind is where the key collision lives: mode=coordinate publishes a top-level
        # 'at' COORDINATE, so the ratio must never claim that name on the same kind
        assert out["at_ratio"] == 0.25 and "at" not in out

    def test_the_ratio_ignores_units(self, monkeypatch):
        # a proportional 'at' is a fraction of the path, so a units change must not scale it
        comp = _install()
        _stub_path(monkeypatch)
        _payload(cn.handler(kind="plane", mode="on_path", path="<h>", at=0.5, units="in"))
        _p, _dtype, val = comp.constructionPlanes.captured["on_path"]
        assert val == ("real", 0.5)

    def test_endpoints_are_legal(self, monkeypatch):
        comp = _install()
        _stub_path(monkeypatch)
        _payload(cn.handler(kind="plane", mode="on_path", path="<h>", at=0))
        assert comp.constructionPlanes.captured["on_path"][2] == ("real", 0.0)
        _payload(cn.handler(kind="point", mode="on_path", path="<h>", at=1))
        assert comp.constructionPoints.captured["on_path"][2] == ("real", 1.0)

    def test_above_one_is_refused_before_the_mutation(self, monkeypatch):
        # setByPath RAISES on a proportional value outside [0, 1] and the raise rolls the whole
        # transaction back, so nothing may reach the input
        comp = _install()
        _stub_path(monkeypatch)
        res = cn.handler(kind="point", mode="on_path", path="<h>", at=1.5)
        assert res["isError"] is True
        assert "'at' must be between 0 and 1" in res["message"] and "1.5" in res["message"]
        assert "absolute" in res["message"]        # the refusal names the type that takes a length
        assert comp.constructionPoints.captured is None

    def test_below_zero_is_refused(self, monkeypatch):
        _install()
        _stub_path(monkeypatch)
        res = cn.handler(kind="plane", mode="on_path", path="<h>", at=-0.1)
        assert res["isError"] is True and "-0.1" in res["message"]

    def test_missing_at_is_refused_naming_the_mode(self, monkeypatch):
        _install()
        _stub_path(monkeypatch)
        res = cn.handler(kind="plane", mode="on_path", path="<h>")
        assert res["isError"] is True
        assert "mode='on_path' needs 'at'" in res["message"]

    def test_non_numeric_at_is_refused(self, monkeypatch):
        _install()
        _stub_path(monkeypatch)
        res = cn.handler(kind="plane", mode="on_path", path="<h>", at="halfway")
        assert res["isError"] is True and "must be a number" in res["message"]

    def test_a_path_that_cannot_be_built_is_reported(self, monkeypatch):
        _install()
        _stub_path(monkeypatch, path=None, label=None, err="Path build returned nothing.")
        res = cn.handler(kind="plane", mode="on_path", path="<h>", at=0.5)
        assert res["isError"] is True and "Path build returned nothing." in res["message"]

    def test_setByPath_false_is_reported(self, monkeypatch):
        comp = _install()
        _stub_path(monkeypatch)
        comp.constructionPlanes.next_result = False
        res = cn.handler(kind="plane", mode="on_path", path="<h>", at=0.5)
        assert res["isError"] is True and "setByPath returned false" in res["message"]

    def test_on_path_is_not_a_legal_axis_mode(self):
        _install()
        res = cn.handler(kind="axis", mode="on_path", path="<h>", at=0.5)
        assert res["isError"] is True and "not valid for kind='axis'" in res["message"]

    def test_an_unknown_distance_type_is_refused(self, monkeypatch):
        _install()
        _stub_path(monkeypatch)
        res = cn.handler(kind="plane", mode="on_path", path="<h>", at=0.5, distance_type="ratio")
        assert res["isError"] is True and "proportional" in res["message"]

    def test_the_retired_distance_on_path_call_is_gone(self):
        # setByDistanceOnPath is retired on the installed API; setByPath/setByPathToObject replace it
        assert "setByDistanceOnPath" not in _tool_source()


class TestOnPathAbsolute:
    def test_absolute_scales_the_length_and_uses_the_physical_type(self, monkeypatch):
        comp = _install()
        _stub_path(monkeypatch)
        out = _payload(cn.handler(kind="plane", mode="on_path", path="<h>", at=30,
                                  distance_type="absolute", units="mm"))
        p, dtype, val = comp.constructionPlanes.captured["on_path"]
        assert dtype is _PDT("PhysicalPathDistanceType")
        assert val == ("real", 3.0)               # 30 mm -> 3.0 cm, the internal unit
        assert out["at_distance"] == 30.0 and out["distance_type"] == "absolute"
        assert "at_ratio" not in out              # a length is not a ratio

    def test_absolute_takes_a_parameter_expression(self, monkeypatch):
        comp = _install()
        _with_units_mgr(FakeUnitsManager(valid=("85 mm",)))
        _stub_path(monkeypatch)
        out = _payload(cn.handler(kind="point", mode="on_path", path="<h>", at="85 mm",
                                  distance_type="absolute"))
        _p, dtype, val = comp.constructionPoints.captured["on_path"]
        assert dtype is _PDT("PhysicalPathDistanceType")
        assert val == ("str", "85 mm")            # createByString - keeps the parametric link
        assert out["at_distance"] == "85 mm"

    def test_an_unresolvable_expression_is_refused_by_name(self, monkeypatch):
        comp = _install()
        _with_units_mgr()
        _stub_path(monkeypatch)
        res = cn.handler(kind="plane", mode="on_path", path="<h>", at="NoSuchParam/2",
                         distance_type="absolute")
        assert res["isError"] is True and "NoSuchParam/2" in res["message"]
        assert comp.constructionPlanes.captured is None

    def test_a_negative_absolute_distance_places_before_the_path_start(self, monkeypatch):
        # measured: setByPath takes a negative physical distance, places the datum exactly that far
        # BEFORE the path start and reports the feature healthy - a real placement, not an error
        comp = _install()
        _stub_path(monkeypatch)
        out = _payload(cn.handler(kind="point", mode="on_path", path="<h>", at=-5,
                                  distance_type="absolute", units="mm"))
        _p, dtype, val = comp.constructionPoints.captured["on_path"]
        assert dtype is _PDT("PhysicalPathDistanceType")
        assert val == ("real", -0.5)              # -5 mm -> -0.5 cm, passed through unchanged
        assert out["at_distance"] == -5.0

    def test_a_negative_absolute_expression_is_placed_the_same_way(self, monkeypatch):
        # the literal and the expression form must behave identically - a guard that reads only
        # literals would let '-10 mm' through while refusing -10
        comp = _install()
        _with_units_mgr(FakeUnitsManager(valid=("-10 mm",)))
        _stub_path(monkeypatch)
        out = _payload(cn.handler(kind="plane", mode="on_path", path="<h>", at="-10 mm",
                                  distance_type="absolute"))
        _p, _dtype, val = comp.constructionPlanes.captured["on_path"]
        assert val == ("str", "-10 mm") and out["at_distance"] == "-10 mm"

    def test_zero_is_the_path_start_and_is_accepted(self, monkeypatch):
        comp = _install()
        _stub_path(monkeypatch)
        _payload(cn.handler(kind="plane", mode="on_path", path="<h>", at=0,
                            distance_type="absolute"))
        assert comp.constructionPlanes.captured["on_path"][2] == ("real", 0.0)

    def test_a_missing_absolute_at_names_the_units_it_reads(self, monkeypatch):
        _install()
        _stub_path(monkeypatch)
        res = cn.handler(kind="plane", mode="on_path", path="<h>", distance_type="absolute")
        assert res["isError"] is True and "mode='on_path' needs 'at'" in res["message"]
        assert "units" in res["message"]

    def test_the_landed_model_parameter_is_read_back(self, monkeypatch):
        comp = _install()
        _stub_path(monkeypatch)
        comp.constructionPoints.result_definition = _path_defn(_param("d7", "30.00 mm"))
        out = _payload(cn.handler(kind="point", mode="on_path", path="<h>", at=30,
                                  distance_type="absolute", units="mm"))
        # the EXPRESSION Fusion landed, not an echo of the request
        assert out["landed"] == {"distance": "30.00 mm"}
        assert out["model_parameters"] == {"distance": "d7"}
        assert "param_set" in out["note"]

    def test_an_unreadable_expression_is_not_published_as_a_landed_reading(self, monkeypatch):
        # a null is not a reading - but the parameter NAME is still worth publishing
        comp = _install()
        _stub_path(monkeypatch)
        comp.constructionPoints.result_definition = _path_defn(_param("d9", None))
        out = _payload(cn.handler(kind="point", mode="on_path", path="<h>", at=30,
                                  distance_type="absolute", units="mm"))
        assert "landed" not in out
        assert out["model_parameters"] == {"distance": "d9"}

    def test_a_proportional_placement_publishes_its_landed_ratio(self, monkeypatch):
        # a proportional definition reads back as the bare unitless ratio - publishable, and the
        # parameter behind it is drivable
        comp = _install()
        _stub_path(monkeypatch)
        comp.constructionPlanes.result_definition = _path_defn(_param("d7", "0.5", value=0.5))
        out = _payload(cn.handler(kind="plane", mode="on_path", path="<h>", at=0.5))
        assert out["landed"] == {"distance": "0.5"}
        assert out["model_parameters"] == {"distance": "d7"}

    def test_a_proportional_placement_is_never_measured_against_the_path_length(self, monkeypatch):
        # a proportional parameter's value is a RATIO, not cm - comparing it to a length would
        # manufacture a nonsense verdict, so the extent reading is absolute-only
        comp = _install()
        _stub_path(monkeypatch, path=_measurable_path(10.0))
        comp.constructionPlanes.result_definition = _path_defn(_param("d7", "0.5", value=0.5))
        out = _payload(cn.handler(kind="plane", mode="on_path", path="<h>", at=0.5))
        assert "path_length" not in out and "beyond_path" not in out
        assert "not clamped" not in out["note"]

    def test_one_unreadable_entity_withdraws_the_length_instead_of_summing_the_rest(self):
        # every segment must measure or there IS no path length. Skipping the unreadable entity
        # would sum 4+6 = 10 cm and publish it as the length of a 4+5+6 = 15 cm path - a wrong
        # number a placement is then judged against, which is worse than no number at all.
        assert cn._path_length_cm(_measurable_path(4.0, 5.0, 6.0)) == 15.0
        assert cn._path_length_cm(_path_with_one_unreadable_entity(4.0, 5.0, 6.0)) is None

    def test_an_entity_whose_extents_do_not_answer_withdraws_the_whole_length(self, monkeypatch):
        # the sibling of the unreadable-entity case: the entity IS readable, but getParameterExtents
        # answers a failure flag, so its span is unknown. Carrying on to the next entity would sum
        # 4 + 6 and publish 10 cm as the length of a 15 cm path.
        path = _measurable_path(4.0, 5.0, 6.0)
        path.item(1).curve.evaluator.getParameterExtents = lambda: (False, 0.0, 0.0)
        assert cn._path_length_cm(path) is None

        comp = _install()
        _stub_path(monkeypatch, path=path)
        comp.constructionPlanes.result_definition = _path_defn(_param("d7", "120 mm", value=12.0))
        out = _payload(cn.handler(kind="plane", mode="on_path", path="<h>", at=120,
                                  distance_type="absolute"))
        assert "path_length" not in out and "beyond_path" not in out

    def test_an_unreadable_entity_never_puts_a_short_path_length_on_the_wire(self, monkeypatch):
        comp = _install()
        _stub_path(monkeypatch, path=_path_with_one_unreadable_entity(4.0, 5.0, 6.0))
        comp.constructionPlanes.result_definition = _path_defn(_param("d7", "120 mm", value=12.0))
        out = _payload(cn.handler(kind="plane", mode="on_path", path="<h>", at=120,
                                  distance_type="absolute"))
        assert "path_length" not in out            # no number is invented from the readable parts
        assert "not clamped at either end" in out["note"]

    def test_absolute_warns_generically_when_the_path_length_cannot_be_measured(self, monkeypatch):
        comp = _install()
        _stub_path(monkeypatch)                        # a path with no readable evaluator
        out = _payload(cn.handler(kind="plane", mode="on_path", path="<h>", at=500,
                                  distance_type="absolute"))
        assert "not clamped at either end" in out["note"]
        assert "path_extrapolates" not in out          # the flag drives the note, it is not payload
        assert "path_length" not in out                # no number is invented
        plain = _payload(cn.handler(kind="plane", mode="on_path", path="<h>", at=0.5))
        assert "not clamped" not in plain["note"]


class TestOnPathExtent:
    """The measured path length vs where the datum actually landed. adsk.fusion.Path has no length
    member, so the length is summed from each entity's curve evaluator."""

    def _install_with_landing(self, at_cm, entity_lengths, monkeypatch, kind="plane"):
        comp = _install()
        _stub_path(monkeypatch, path=_measurable_path(*entity_lengths))
        coll = comp.constructionPlanes if kind == "plane" else comp.constructionPoints
        coll.result_definition = _path_defn(_param("d1", "landed", value=at_cm))
        return comp

    def test_a_placement_inside_the_path_reports_both_numbers_and_no_warning(self, monkeypatch):
        self._install_with_landing(3.0, [10.0], monkeypatch)
        out = _payload(cn.handler(kind="plane", mode="on_path", path="<h>", at=30,
                                  distance_type="absolute", units="mm"))
        assert out["path_length"] == 100.0 and out["along_path"] == 30.0   # cm read out in mm
        assert out["beyond_path"] is False
        assert "not clamped" not in out["note"]

    def test_a_placement_past_the_end_is_flagged_with_the_numbers(self, monkeypatch):
        self._install_with_landing(50.0, [10.0], monkeypatch)
        out = _payload(cn.handler(kind="plane", mode="on_path", path="<h>", at=500,
                                  distance_type="absolute", units="mm"))
        assert out["beyond_path"] is True
        assert out["path_length"] == 100.0 and out["along_path"] == 500.0
        assert "OFF the path" in out["note"]

    def test_a_negative_landing_is_flagged_too(self, monkeypatch):
        # extrapolation is symmetric - before the start is as far off the curve as past the end
        self._install_with_landing(-1.0, [10.0], monkeypatch, kind="point")
        out = _payload(cn.handler(kind="point", mode="on_path", path="<h>", at=-10,
                                  distance_type="absolute", units="mm"))
        assert out["beyond_path"] is True and out["along_path"] == -10.0
        assert "OFF the path" in out["note"]

    def test_the_path_end_itself_is_inside(self, monkeypatch):
        self._install_with_landing(10.0, [10.0], monkeypatch)
        out = _payload(cn.handler(kind="plane", mode="on_path", path="<h>", at=100,
                                  distance_type="absolute", units="mm"))
        assert out["beyond_path"] is False

    def test_a_multi_entity_path_sums_its_entities(self, monkeypatch):
        # a chained path is only as long as ALL its entities - measuring the first one alone would
        # call a legal placement off the path
        self._install_with_landing(7.0, [4.0, 3.5], monkeypatch)
        out = _payload(cn.handler(kind="plane", mode="on_path", path="<h>", at=70,
                                  distance_type="absolute", units="mm"))
        assert out["path_length"] == 75.0 and out["beyond_path"] is False

    def test_an_unreadable_landed_value_publishes_no_verdict(self, monkeypatch):
        # the length is measurable but the landed parameter is not - no verdict may be invented
        comp = _install()
        _stub_path(monkeypatch, path=_measurable_path(10.0))
        comp.constructionPlanes.result_definition = _path_defn(_param("d1", "30.00 mm"))
        out = _payload(cn.handler(kind="plane", mode="on_path", path="<h>", at=30,
                                  distance_type="absolute", units="mm"))
        assert "beyond_path" not in out and "path_length" not in out
        assert "not clamped at either end" in out["note"]

    def test_a_landed_value_that_RAISES_publishes_no_verdict(self, monkeypatch):
        # the dangerous failure: a confident zero for an unreadable value would publish "sits
        # exactly at the path start, on the path" - a measurement nobody took
        comp = _install()
        _stub_path(monkeypatch, path=_measurable_path(10.0))
        comp.constructionPlanes.result_definition = _path_defn(_raising_param("d1", "30.00 mm"))
        out = _payload(cn.handler(kind="plane", mode="on_path", path="<h>", at=30,
                                  distance_type="absolute", units="mm"))
        assert "path_length" not in out and "along_path" not in out and "beyond_path" not in out
        assert "not clamped at either end" in out["note"]

    def test_a_non_numeric_landed_value_publishes_no_verdict(self, monkeypatch):
        comp = _install()
        _stub_path(monkeypatch, path=_measurable_path(10.0))
        comp.constructionPlanes.result_definition = _path_defn(_param("d1", "30.00 mm", value="3"))
        out = _payload(cn.handler(kind="plane", mode="on_path", path="<h>", at=30,
                                  distance_type="absolute", units="mm"))
        assert "along_path" not in out and "beyond_path" not in out

    def test_an_evaluator_that_answers_a_failure_flag_yields_no_length(self, monkeypatch):
        # the evaluator's leading flag is the answer's validity - a false one is not a zero length
        comp = _install()
        path = _measurable_path(10.0)
        path.item(0).curve.evaluator.getLengthAtParameter = lambda a, b: (False, 0.0)
        _stub_path(monkeypatch, path=path)
        comp.constructionPlanes.result_definition = _path_defn(_param("d1", "30.00 mm", value=3.0))
        out = _payload(cn.handler(kind="plane", mode="on_path", path="<h>", at=30,
                                  distance_type="absolute", units="mm"))
        assert "path_length" not in out and "beyond_path" not in out


class TestOnPathToObject:
    def _point_handle(self, monkeypatch):
        pt = FakePoint(7, 0, 0)
        _stub_resolve(monkeypatch, cn._TO_OBJECT, pt)
        return pt

    def test_plane_calls_setByPathToObject_with_the_scaled_offset(self, monkeypatch):
        comp = _install()
        path = _stub_path(monkeypatch)
        pt = self._point_handle(monkeypatch)
        out = _payload(cn.handler(kind="plane", mode="on_path", path="<h>", to_object="<pt>",
                                  offset=10, units="mm"))
        p, to_obj, val = comp.constructionPlanes.captured["on_path_to_object"]
        assert p is path and to_obj is pt and val == ("real", 1.0)   # 10 mm -> 1.0 cm
        assert out["to_object"] is True and out["offset"] == 10.0
        assert "on_path" not in (comp.constructionPlanes.captured or {})   # not the setByPath route
        assert comp.constructionPlanes.added == 1

    def test_the_offset_takes_a_parameter_expression(self, monkeypatch):
        comp = _install()
        _with_units_mgr()
        _stub_path(monkeypatch)
        self._point_handle(monkeypatch)
        out = _payload(cn.handler(kind="plane", mode="on_path", path="<h>", to_object="<pt>",
                                  offset="25 mm"))
        _p, _to, val = comp.constructionPlanes.captured["on_path_to_object"]
        assert val == ("str", "25 mm") and out["offset"] == "25 mm"

    def test_both_landed_parameters_are_read_back(self, monkeypatch):
        # a to-object plane carries the along-path distance and the offset as SEPARATE parameters
        comp = _install()
        _stub_path(monkeypatch)
        self._point_handle(monkeypatch)
        comp.constructionPlanes.result_definition = _path_defn(
            _param("d3", "70.00 mm"), offset=_param("d4", "10.00 mm"))
        out = _payload(cn.handler(kind="plane", mode="on_path", path="<h>", to_object="<pt>",
                                  offset=10, units="mm"))
        assert out["landed"] == {"distance": "70.00 mm", "offset": "10.00 mm"}
        assert out["model_parameters"] == {"distance": "d3", "offset": "d4"}

    def test_an_on_path_plane_is_not_offset_compared(self, monkeypatch):
        # The offset read-back compare is scoped to mode='offset'. An on_path plane stores its
        # placement as a 'distance' parameter with the offset as a SEPARATE one, and no measurement
        # backs judging those against this request - so an offset parameter reading 99 mm for a
        # 10 mm request is PUBLISHED, not refused.
        comp = _install()
        _stub_path(monkeypatch)
        self._point_handle(monkeypatch)
        comp.constructionPlanes.result_definition = _path_defn(
            _param("d3", "70.00 mm", value=7.0), offset=_param("d4", "99.00 mm", value=9.9))
        out = _payload(cn.handler(kind="plane", mode="on_path", path="<h>", to_object="<pt>",
                                  offset=10, units="mm"))
        assert out["landed"]["offset"] == "99.00 mm"

    def test_a_null_offset_parameter_is_not_published(self, monkeypatch):
        # a plane built by distance carries definition.offset = None - a null must not be reported
        # as a landed reading
        comp = _install()
        _stub_path(monkeypatch)
        comp.constructionPlanes.result_definition = _path_defn(_param("d3", "30.00 mm"),
                                                                    offset=None)
        out = _payload(cn.handler(kind="plane", mode="on_path", path="<h>", at=30,
                                  distance_type="absolute", units="mm"))
        assert out["landed"] == {"distance": "30.00 mm"}
        assert out["model_parameters"] == {"distance": "d3"}

    def test_the_extent_is_the_target_distance_PLUS_the_offset(self, monkeypatch):
        # measured: a 40 mm path, a vertex at its end and a 5 mm offset land the plane 5 mm PAST
        # the end - healthy, and silent unless the extent is reported
        comp = _install()
        _stub_path(monkeypatch, path=_measurable_path(4.0))
        self._point_handle(monkeypatch)
        comp.constructionPlanes.result_definition = _path_defn(
            _param("d3", "40.00 mm", value=4.0), offset=_param("d4", "5.00 mm", value=0.5))
        out = _payload(cn.handler(kind="plane", mode="on_path", path="<h>", to_object="<pt>",
                                  offset=5, units="mm"))
        assert out["path_length"] == 40.0 and out["along_path"] == 45.0
        assert out["beyond_path"] is True and "OFF the path" in out["note"]

    def test_a_to_object_plane_inside_the_path_carries_no_warning(self, monkeypatch):
        comp = _install()
        _stub_path(monkeypatch, path=_measurable_path(4.0))
        self._point_handle(monkeypatch)
        comp.constructionPlanes.result_definition = _path_defn(
            _param("d3", "20.00 mm", value=2.0), offset=_param("d4", "5.00 mm", value=0.5))
        out = _payload(cn.handler(kind="plane", mode="on_path", path="<h>", to_object="<pt>",
                                  offset=5, units="mm"))
        assert out["along_path"] == 25.0 and out["beyond_path"] is False
        assert "OFF the path" not in out["note"] and "not clamped" not in out["note"]

    def test_a_to_object_extent_needs_both_parameters(self, monkeypatch):
        # the offset is half the position - without it there is no landed position to judge
        comp = _install()
        _stub_path(monkeypatch, path=_measurable_path(4.0))
        self._point_handle(monkeypatch)
        comp.constructionPlanes.result_definition = _path_defn(
            _param("d3", "40.00 mm", value=4.0), offset=_param("d4", "5.00 mm"))
        out = _payload(cn.handler(kind="plane", mode="on_path", path="<h>", to_object="<pt>",
                                  offset=5, units="mm"))
        assert "along_path" not in out and "beyond_path" not in out
        assert "not clamped at either end" in out["note"]

    def test_setByPathToObject_false_is_reported(self, monkeypatch):
        comp = _install()
        _stub_path(monkeypatch)
        self._point_handle(monkeypatch)
        comp.constructionPlanes.next_result = False
        res = cn.handler(kind="plane", mode="on_path", path="<h>", to_object="<pt>")
        assert res["isError"] is True and "setByPathToObject returned false" in res["message"]
        assert comp.constructionPlanes.added == 0

    def test_to_object_with_at_is_refused(self, monkeypatch):
        comp = _install()
        _stub_path(monkeypatch)
        self._point_handle(monkeypatch)
        res = cn.handler(kind="plane", mode="on_path", path="<h>", to_object="<pt>", at=0.5)
        assert res["isError"] is True and "'at'" in res["message"]
        assert comp.constructionPlanes.captured is None

    def test_to_object_with_an_explicit_distance_type_is_refused(self, monkeypatch):
        comp = _install()
        _stub_path(monkeypatch)
        self._point_handle(monkeypatch)
        res = cn.handler(kind="plane", mode="on_path", path="<h>", to_object="<pt>",
                         distance_type="absolute")
        assert res["isError"] is True and "distance_type" in res["message"]
        assert comp.constructionPlanes.captured is None

    def test_the_point_kind_refuses_to_object_naming_the_api_fact(self, monkeypatch):
        # ConstructionPointInput has no setByPathToObject - the refusal must say so rather than
        # silently dropping the input and placing the point somewhere else
        comp = _install()
        comp.constructionPoints.no_to_object = True
        _stub_path(monkeypatch)
        self._point_handle(monkeypatch)
        res = cn.handler(kind="point", mode="on_path", path="<h>", to_object="<pt>")
        assert res["isError"] is True
        assert "setByPathToObject" in res["message"] and "kind='plane'" in res["message"]
        assert comp.constructionPoints.captured is None

    def test_an_unresolvable_to_object_handle_is_reported(self, monkeypatch):
        comp = _install()
        _stub_path(monkeypatch)
        monkeypatch.setattr(cn._TO_OBJECT, "resolve", lambda raw: (None, "no vertex for '<pt>'."))
        res = cn.handler(kind="plane", mode="on_path", path="<h>", to_object="<pt>")
        assert res["isError"] is True and "no vertex for '<pt>'." in res["message"]
        assert comp.constructionPlanes.captured is None


# ── the geometry sanity read-back (universal across modes) ─────────────────────────────────────

class TestGeometryReadback:
    def test_reports_geometry_when_available(self):
        comp = _install()
        comp.constructionPlanes.result_geometry = Plane(FakeVector3D(0, 0, 1), FakePoint(1, 2, 3))
        out = _payload(cn.handler(kind="plane", plane="xy", offset=5))
        assert out["geometry"]["normal"] == [0.0, 0.0, 1.0]
        # origin is internally cm; mm display units -> *10.
        assert out["geometry"]["origin"] == {"x": 10.0, "y": 20.0, "z": 30.0}

    def test_missing_geometry_degrades_to_empty_dict(self):
        _install()
        out = _payload(cn.handler(kind="point", x=1, y=2, z=3))
        assert out["geometry"] == {}


class TestPlaneSketchOriginNote:
    def test_a_plane_note_points_at_the_sketch_origin_it_does_not_publish(self):
        # 'geometry.origin' is the PLANE's own origin; a sketch built on it starts at the world
        # origin's projection instead, so the note has to name where that number is read.
        comp = _install()
        comp.constructionPlanes.result_geometry = Plane(FakeVector3D(0, 0, 1), FakePoint(1, 2, 3))
        out = _payload(cn.handler(kind="plane", plane="xy", offset=5))
        assert "world origin's projection" in out["note"]
        assert "frame.origin_mm" in out["note"]

    def test_an_axis_datum_does_not_carry_it(self):
        _install()
        out = _payload(cn.handler(kind="axis", axis="z"))
        assert "world origin's projection" not in out["note"]


# ── the payload's coordinates come from the same space the gates verify in ──────────────────────
#
# Measured: a datum created while an occurrence is active reads component-LOCAL off .geometry, and
# createForAssemblyContext(activeOccurrence) restores WORLD - the space a proxy-resolved
# handle/vertex reads in. A payload built from the plain read publishes LOCAL coordinates beside a
# gate that passed in WORLD, so an agent measuring off 'geometry' is a whole occurrence offset out.
# The local/world pairs below are the measured ones (local (1,1,1) under an occurrence at (5,2,1)
# reads world (6,3,2)).

_OCC = ("occ", "CompC:1")


def _axis_geometry(direction, origin):
    return type("G", (), {"direction": FakeVector3D(*direction), "origin": FakePoint(*origin)})()


class TestPayloadSpaceUnderAnActiveOccurrence:
    def test_plane_origin_and_normal_are_the_world_read(self):
        comp = _install(active_occurrence=_OCC)
        comp.constructionPlanes.result_geometry = Plane(FakeVector3D(1, 0, 0), FakePoint(0, 0, -1))
        comp.constructionPlanes.proxy_geometry = Plane(FakeVector3D(0, 0, 1), FakePoint(0, 0, 2))
        out = _payload(cn.handler(kind="plane", plane="xy", offset=5, units="mm"))
        assert out["geometry"]["normal"] == [0.0, 0.0, 1.0]
        assert out["geometry"]["origin"] == {"x": 0.0, "y": 0.0, "z": 20.0}   # 2 cm -> 20 mm

    def test_point_coordinates_are_the_world_read(self, monkeypatch):
        comp = _install(active_occurrence=_OCC)
        _stub_resolve(monkeypatch, cn._EDGES, [_circular_edge()])
        comp.constructionPoints.result_geometry = FakePoint(1, 1, 1)          # component-LOCAL
        comp.constructionPoints.proxy_geometry = FakePoint(6, 3, 2)           # WORLD
        out = _payload(cn.handler(kind="point", mode="circle_center", units="mm"))
        assert out["geometry"]["at"] == {"x": 60.0, "y": 30.0, "z": 20.0}

    def test_axis_payload_and_alignment_both_read_the_world_direction(self, monkeypatch):
        comp = _install(active_occurrence=_OCC)
        _stub_resolve(monkeypatch, cn._FACE, _cylinder_face((0, 0, 1), (0, 0, 0)))
        comp.constructionAxes.result_geometry = _axis_geometry((1, 0, 0), (0, 0, 0))
        comp.constructionAxes.proxy_geometry = _axis_geometry((0, 0, 1), (0, 0, 3))
        out = _payload(cn.handler(kind="axis", mode="circular_face", units="mm"))
        assert out["geometry"]["direction"] == [0.0, 0.0, 1.0]
        assert out["geometry"]["origin"] == {"x": 0.0, "y": 0.0, "z": 30.0}
        # the face is proxy-resolved (WORLD); against the LOCAL direction this reads as misaligned
        assert out["aligned_to_face_axis"] is True

    def test_perpendicular_axis_alignment_reads_the_world_direction(self, monkeypatch):
        comp = _install(active_occurrence=_OCC)
        _stub_resolve(monkeypatch, cn._FACE, _planar_face((0, 0, 1)))
        _stub_resolve(monkeypatch, cn._POINTS, [FakePoint()])
        comp.constructionAxes.result_geometry = _axis_geometry((1, 0, 0), (0, 0, 0))
        comp.constructionAxes.proxy_geometry = _axis_geometry((0, 0, 1), (0, 0, 0))
        out = _payload(cn.handler(kind="axis", mode="perpendicular_at_point"))
        assert out["aligned_to_face_normal"] is True

    def test_geometry_is_withheld_when_the_context_proxy_cannot_be_built(self):
        # publishing the LOCAL read here would hand back coordinates in a space the payload cannot
        # name; the empty read-back is disclosed in the note rather than left to be guessed at
        comp = _install(active_occurrence=_OCC)
        comp.constructionPlanes.result_geometry = Plane(FakeVector3D(0, 0, 1), FakePoint(0, 0, -1))
        out = _payload(cn.handler(kind="plane", plane="xy", offset=5))
        assert out["geometry"] == {}
        assert "NOT measured" in out["note"] and "occurrence is active" in out["note"]

    def test_at_angle_claims_the_rotation_when_both_operands_land_in_one_space(self, monkeypatch):
        # the real shape of this mode under an occurrence: 'plane' resolved to a PROXY face, which
        # already reads WORLD and must not be re-contexted, while the created plane is native and
        # lifts. Both then sit in world: base (0,0,1) vs created (1,0,0) = the rotation took.
        comp = _install(active_occurrence=_OCC)
        base = _planar_face((0, 0, 1))
        base.assemblyContext = _OCC              # measured: a proxy already reads WORLD
        _stub_resolve(monkeypatch, cn._PLANE, base)
        _stub_resolve(monkeypatch, cn._EDGES, [_straight_edge()])
        comp.constructionPlanes.result_geometry = Plane(FakeVector3D(0, 0, 1), FakePoint(0, 0, 0))
        comp.constructionPlanes.proxy_geometry = Plane(FakeVector3D(1, 0, 0), FakePoint(0, 0, 0))
        out = _payload(cn.handler(kind="plane", mode="at_angle", angle=90))
        # against the datum's LOCAL read this same pair says the plane never rotated
        assert out["normal_changed"] is True
        assert "NOT measured" not in out["note"]

    def test_at_angle_discloses_the_claim_when_the_base_cannot_be_lifted(self, monkeypatch):
        # a NATIVE base whose lift is refused: its space is unknown, so the verdict is published as
        # null with the reason rather than computed across two spaces
        comp = _install(active_occurrence=_OCC)
        _stub_resolve(monkeypatch, cn._PLANE, _planar_face((0, 0, 1)))
        _stub_resolve(monkeypatch, cn._EDGES, [_straight_edge()])
        comp.constructionPlanes.result_geometry = Plane(FakeVector3D(0, 0, 1), FakePoint(0, 0, 0))
        comp.constructionPlanes.proxy_geometry = Plane(FakeVector3D(1, 0, 0), FakePoint(0, 0, 0))
        out = _payload(cn.handler(kind="plane", mode="at_angle", angle=90))
        assert out["normal_changed"] is None
        assert "NOT measured" in out["note"]
        # the payload's own geometry still reports the world read - only the base was unreadable
        assert out["geometry"]["normal"] == [1.0, 0.0, 0.0]


# ── offset-plane parameter EXPRESSIONS + the model-parameter (dNN) read-back ────────────────────
#
# mode=offset accepts a parameter EXPRESSION string ('StockZ/2', '25 mm') routed through
# ValueInput.createByString (ties the plane's offset to a live parameter), validated via the units
# engine so an unresolvable one is refused BY NAME. And an offset plane NAMES the model parameter
# (dNN) it created so the plane is retargetable via param_set - the same shape as the landed
# model_extrude distance fix.

def _with_units_mgr(mgr=None):
    """Attach the shared fake units engine (conftest.FakeUnitsManager) to the installed design so
    string expressions can evaluate."""
    cn.app.activeProduct.unitsManager = mgr or FakeUnitsManager()


def _plane_with_offset_param(comp, dname="d5"):
    """Make constructionPlanes.add() return a plane exposing the offset ModelParameter read-back
    path (obj.definition.offset.name = dname), so _offset_parameter surfaces the dNN."""
    param = type("MP", (), {"name": dname})()
    defn = type("Def", (), {"offset": param})()
    comp.constructionPlanes.add = lambda inp: type(
        "O", (), {"name": "Datum", "geometry": None, "definition": defn})()


class TestOffsetExpression:
    def test_string_expression_uses_createByString_not_scaled_real(self):
        comp = _install()
        _with_units_mgr()
        out = _payload(cn.handler(kind="plane", plane="xy", offset="25 mm"))
        base, val = comp.constructionPlanes.captured["offset"]
        assert val == ("str", "25 mm")        # createByString - NOT a scaled createByReal
        assert out["offset"] == "25 mm"        # echoed as the expression, not a rounded number

    def test_expression_references_a_parameter(self):
        comp = _install()
        _with_units_mgr()
        _payload(cn.handler(kind="plane", plane="xz", offset="StockZ/2"))
        _base, val = comp.constructionPlanes.captured["offset"]
        assert val == ("str", "StockZ/2")

    def test_numeric_string_is_a_literal_scaled_via_createByReal(self):
        # a PLAIN numeric string is a literal, not an expression - it still scales through createByReal.
        comp = _install()
        out = _payload(cn.handler(kind="plane", plane="xy", offset="15", units="mm"))
        _base, val = comp.constructionPlanes.captured["offset"]
        assert val == ("real", 1.5)           # "15" mm -> 1.5 cm, the literal path
        assert out["offset"] == 15.0

    def test_unresolvable_expression_refused_by_name(self):
        _install()
        _with_units_mgr()                      # only the known set evaluates; this one does not
        res = cn.handler(kind="plane", plane="xy", offset="NoSuchParam * 2")
        assert res["isError"] is True
        assert "NoSuchParam * 2" in res["message"]


class TestOffsetModelParameter:
    def test_names_the_offset_model_parameter(self):
        comp = _install()
        _plane_with_offset_param(comp, "d7")
        out = _payload(cn.handler(kind="plane", plane="xy", offset=5))
        assert out["model_parameters"]["offset"] == "d7"

    def test_note_advertises_the_model_parameter(self):
        comp = _install()
        _plane_with_offset_param(comp)
        out = _payload(cn.handler(kind="plane", plane="xy", offset=5))
        assert "model_parameters" in out["note"] and "param_set" in out["note"]

    def test_absent_when_no_offset_parameter_exists(self):
        # the default fake plane exposes no .definition -> the key is simply omitted, never a crash.
        _install()
        out = _payload(cn.handler(kind="plane", plane="xy", offset=5))
        assert "model_parameters" not in out


def _plane_with_offset_value(comp, value_cm, dname="d5"):
    """Make constructionPlanes.add() return a plane whose ConstructionPlaneOffsetDefinition reports
    `value_cm` on its offset ModelParameter - the SIGNED internal-cm read the compare gates on."""
    param = type("MP", (), {"name": dname, "value": value_cm})()
    defn = type("Def", (), {"offset": param})()
    comp.constructionPlanes.add = lambda inp: type(
        "O", (), {"name": "Datum", "geometry": None, "definition": defn})()


class TestOffsetReadBack:
    """The offset the CREATED plane REPORTS, against the number the units engine evaluated the
    request to. Fusion hands back a plane object either way, so the definition's own offset
    ModelParameter is the only thing that can contradict a datum that landed at a depth nobody asked
    for - and it reads SIGNED internal cm (measured: a -12 mm request reads -1.2), so the sign is
    part of the comparison rather than a magnitude match."""

    def test_a_matching_read_back_passes_silently(self):
        comp = _install()
        _plane_with_offset_value(comp, 1.5)
        out = _payload(cn.handler(kind="plane", plane="xy", offset=15, units="mm"))
        assert out["created"] is True and out["offset"] == 15.0

    def test_a_mismatched_read_back_errors_naming_both_values(self):
        comp = _install()
        _plane_with_offset_value(comp, 0.75)               # asked 15 mm, landed 7.5 mm
        res = cn.handler(kind="plane", plane="xy", offset=15, units="mm")
        assert res["isError"] is True
        msg = res["message"]
        assert "7.5 mm" in msg and "15.0 mm" in msg
        assert "'Datum'" in msg and "still in the design" in msg

    def test_the_sign_is_part_of_the_comparison(self):
        # the parameter keeps the requested SIGN, so a plane reading +1.2 cm for a -12 mm request
        # sits on the wrong side of its base - a magnitude-only compare would pass it
        comp = _install()
        _plane_with_offset_value(comp, 1.2)
        res = cn.handler(kind="plane", plane="xy", offset=-12, units="mm")
        assert res["isError"] is True and "-12.0 mm" in res["message"]

    def test_a_matching_negative_read_back_passes(self):
        comp = _install()
        _plane_with_offset_value(comp, -1.2)
        out = _payload(cn.handler(kind="plane", plane="xy", offset=-12, units="mm"))
        assert out["created"] is True

    def test_an_expression_is_compared_against_what_it_evaluates_to(self):
        comp = _install()
        _with_units_mgr()                       # 'StockZ/2' evaluates to 0.25 cm under mm
        _plane_with_offset_value(comp, 0.5)     # the plane landed at 5 mm instead
        res = cn.handler(kind="plane", plane="xy", offset="StockZ/2", units="mm")
        assert res["isError"] is True
        msg = res["message"]
        assert "StockZ/2" in msg and "5.0 mm" in msg and "2.5 mm" in msg

    def test_a_plane_reporting_no_offset_number_withholds_the_compare(self):
        # the definition carries the parameter's NAME but no readable value - nothing to judge the
        # datum by, and treating that as zero would refuse an offset plane that landed correctly
        comp = _install()
        _plane_with_offset_param(comp)
        out = _payload(cn.handler(kind="plane", plane="xy", offset=15, units="mm"))
        assert out["created"] is True and out["model_parameters"]["offset"] == "d5"

    def test_a_boolean_offset_is_not_a_distance(self):
        # isinstance(True, int) is True, so a guard that only excludes non-numbers turns an
        # offset.value answering True into exactly 1.0 cm - which MATCHES a 10 mm request and passes
        # the very comparison that exists to catch a wrong offset. The twin both siblings pin.
        comp = _install()
        _plane_with_offset_value(comp, True)
        out = _payload(cn.handler(kind="plane", plane="xy", offset=10, units="mm"))
        assert out["created"] is True                      # compare withheld, not read as 1.0 cm
        assert cn._offset_value_cm(comp.constructionPlanes.add(None)) is None

    def test_an_evaluation_that_answers_no_number_withholds_the_compare(self):
        # the units engine answered something that is not a length, so nothing here can judge the
        # datum's offset - the other half of the same gate
        comp = _install()
        _with_units_mgr(type("M", (), {"evaluateExpression": lambda self, e, u=None: "eleven",
                                       "defaultLengthUnits": "mm"})())
        _plane_with_offset_value(comp, 9.9)
        out = _payload(cn.handler(kind="plane", plane="xy", offset="StockZ/2", units="mm"))
        assert out["created"] is True

    def test_a_difference_at_the_tolerance_passes_and_one_past_it_errors(self):
        # The exact boundary of the 1e-6 cm band. 2e-6 - 1e-6 is EXACT in binary floating point, so
        # the equal case really sits on the boundary rather than rounding under it.
        for landed_cm, is_error in ((2e-6, False), (3e-6, True)):
            comp = _install()
            _plane_with_offset_value(comp, landed_cm)
            res = cn.handler(kind="plane", plane="xy", offset=1e-6, units="cm")
            assert res["isError"] is is_error, landed_cm


# ── the created datum's HANDLE ──────────────────────────────────────────────────────────────────
#
# A datum nothing can point at is unreachable: the payload publishes the created object's
# entityToken so the next call can consume the axis/plane directly (AxisRef and PlaneRef both
# resolve one), instead of the caller having to re-find it.

class TestCreatedDatumHandle:
    def test_axis_handle_is_the_created_objects_token(self):
        comp = _install()
        comp.constructionAxes.result_token = "AXIS-TOKEN"
        out = _payload(cn.handler(kind="axis", axis="x"))
        assert out["handle"] == "AXIS-TOKEN"

    def test_plane_handle_is_the_created_objects_token(self):
        comp = _install()
        comp.constructionPlanes.result_token = "PLANE-TOKEN"
        out = _payload(cn.handler(kind="plane", plane="xy", offset=5))
        assert out["handle"] == "PLANE-TOKEN"

    def test_point_handle_is_the_created_objects_token(self):
        comp = _install()
        comp.constructionPoints.result_token = "POINT-TOKEN"
        out = _payload(cn.handler(kind="point", x=1))
        assert out["handle"] == "POINT-TOKEN"

    def test_axis_note_names_where_the_handle_can_be_spent(self):
        comp = _install()
        comp.constructionAxes.result_token = "AXIS-TOKEN"
        out = _payload(cn.handler(kind="axis", axis="x"))
        assert "handle" in out["note"] and "model_pattern_circular" in out["note"]

    def test_an_unreadable_token_publishes_null_and_promises_nothing(self):
        # the fake's default object carries no entityToken: the key is present but null, and the
        # note must NOT advertise a handle the payload does not have.
        _install()
        out = _payload(cn.handler(kind="axis", axis="x"))
        assert out["handle"] is None
        assert "handle" not in out["note"]


def test_the_path_description_states_the_tangent_continuity_rule():
    # measured: one seed chains by TANGENT CONTINUITY - a sharp corner stops it, open vs closed
    # decides nothing (a tangent-continuous closed loop chained all 8 edges from one seed). So the
    # wire may not promise chaining unconditionally, nor claim a closed loop refuses to chain; what
    # a seed actually reached is only knowable from the reported count.
    desc = cn.construction_tool.to_dict()["inputSchema"]["properties"]["path"]["description"]
    assert "TANGENT connections only" in desc
    assert "'path' count is the truth" in desc
    assert "auto-chain" not in desc.lower()
    assert "closed loop" not in desc.lower() and "seed edge alone" not in desc
