"""Unit tests for ``_holder.py`` - the tool-holder profile reduction behind model_compute_holder:
axis resolution (``get_axis``), the end-datum intersection (``is_valid_axial_datum``), cylindrical
coordinates off a coaxial circular edge (``get_cylindrical_coordinates_edge``), the coincident-z
point collapse (``filter_points``), the body-of-revolution reduction (``get_tool_profile``), and
the CAM library JSON (``build_holder_data``). The geometry runs on conftest's numeric fakes
(FakeInfiniteLine3D / Plane.intersectWithLine), so every assertion is a real number, not a Mock.
"""

import pytest

from conftest import (Circle3D, Cone, Cylinder, FakeInfiniteLine3D, FakePoint, FakeVector3D,
                      Line3D, Plane, load_tool)

hold = load_tool("_holder")


# The axis-carrying entities get_axis / is_valid_axial_datum isinstance-dispatch on. Local marker
# classes (not shared fakes): the branch is the type itself, so each test wires adsk.fusion to
# these names.

class _Face:
    def __init__(self, geometry):
        self.geometry = geometry


class _Edge:
    def __init__(self, geometry):
        self.geometry = geometry


class _CAxis:
    def __init__(self, geometry):
        self.geometry = geometry


class _DatumVtx:
    def __init__(self, geometry):
        self.geometry = geometry


@pytest.fixture
def wire_geometry(monkeypatch):
    """Point adsk at the numeric fakes + marker entity classes for the whole module. Every cast is
    a pass-through patched via monkeypatch (torn down after the test - the shared fake classes are
    never mutated in place)."""
    import adsk.core
    import adsk.fusion
    monkeypatch.setattr(adsk.fusion, "BRepFace", _Face, raising=False)
    monkeypatch.setattr(adsk.fusion, "BRepEdge", _Edge, raising=False)
    monkeypatch.setattr(adsk.fusion, "BRepVertex", _DatumVtx, raising=False)
    monkeypatch.setattr(adsk.fusion, "ConstructionAxis", _CAxis, raising=False)
    monkeypatch.setattr(adsk.core, "InfiniteLine3D", FakeInfiniteLine3D, raising=False)
    for name, fake in (("Plane", Plane), ("Cylinder", Cylinder), ("Cone", Cone),
                       ("Line3D", Line3D), ("Circle3D", Circle3D), ("Point3D", FakePoint)):
        monkeypatch.setattr(fake, "cast", staticmethod(lambda g: g), raising=False)
        monkeypatch.setattr(adsk.core, name, fake, raising=False)


def _z_axis(origin=None):
    return FakeInfiniteLine3D(origin or FakePoint(0, 0, 0), FakeVector3D(0, 0, 1))


# ── get_axis: entity kind -> axis line ───────────────────────────────────────

class TestGetAxis:
    def test_cylinder_face_axis(self, wire_geometry):
        face = _Face(Cylinder(axis=FakeVector3D(0, 0, 1), origin=FakePoint(1, 2, 0)))
        line = hold.get_axis(face)
        assert (line.origin.x, line.origin.y) == (1, 2)
        assert line.direction.isParallelTo(FakeVector3D(0, 0, 1))

    def test_cone_face_axis(self, wire_geometry):
        face = _Face(Cone(axis=FakeVector3D(1, 0, 0), origin=FakePoint(0, 0, 0)))
        line = hold.get_axis(face)
        assert line.direction.isParallelTo(FakeVector3D(1, 0, 0))

    def test_linear_edge_becomes_infinite_line(self, wire_geometry):
        edge = _Edge(Line3D(FakePoint(0, 0, 0), FakePoint(0, 0, 4)))
        line = hold.get_axis(edge)
        assert line.direction.isParallelTo(FakeVector3D(0, 0, 1))

    def test_construction_axis_geometry_passes_through(self, wire_geometry):
        axis = _z_axis()
        assert hold.get_axis(_CAxis(axis)) is axis

    def test_planar_face_defines_no_axis(self, wire_geometry):
        face = _Face(Plane(FakeVector3D(0, 0, 1), FakePoint(0, 0, 0)))
        assert hold.get_axis(face) is None


# ── is_valid_axial_datum: the z=0 pin ────────────────────────────────────────

class TestAxialDatum:
    def test_planar_face_pins_datum_on_the_axis(self, wire_geometry):
        face = _Face(Plane(FakeVector3D(0, 0, 1), FakePoint(5, 5, 3)))
        pt = hold.is_valid_axial_datum(face, _z_axis())
        assert (pt.x, pt.y, pt.z) == (0, 0, 3)

    def test_coaxial_circular_edge_pins_its_center_plane(self, wire_geometry):
        edge = _Edge(Circle3D(FakeVector3D(0, 0, 1), FakePoint(0, 0, 2.5), 1.0))
        pt = hold.is_valid_axial_datum(edge, _z_axis())
        assert pt.z == 2.5

    def test_off_axis_circular_edge_is_rejected(self, wire_geometry):
        edge = _Edge(Circle3D(FakeVector3D(1, 0, 0), FakePoint(0, 0, 2.5), 1.0))
        assert hold.is_valid_axial_datum(edge, _z_axis()) is None

    def test_vertex_projects_onto_the_axis(self, wire_geometry):
        pt = hold.is_valid_axial_datum(_DatumVtx(FakePoint(3, 4, 1.5)), _z_axis())
        assert (pt.x, pt.y, pt.z) == (0, 0, 1.5)

    def test_perpendicular_line_edge_pins_its_plane(self, wire_geometry):
        edge = _Edge(Line3D(FakePoint(2, 0, 1), FakePoint(2, 5, 1)))
        pt = hold.is_valid_axial_datum(edge, _z_axis())
        assert pt.z == 1

    def test_axis_parallel_line_edge_is_rejected(self, wire_geometry):
        edge = _Edge(Line3D(FakePoint(2, 0, 0), FakePoint(2, 0, 5)))
        assert hold.is_valid_axial_datum(edge, _z_axis()) is None


# ── cylindrical coordinates off one coaxial circular edge ────────────────────

class TestCylindricalEdge:
    def _plane(self):
        return Plane(FakeVector3D(0, 0, 1), FakePoint(0, 0, 0))

    def test_coaxial_circle_yields_radius_and_z(self, wire_geometry):
        edge = _Edge(Circle3D(FakeVector3D(0, 0, 1), FakePoint(0, 0, 2), 1.5))
        assert hold.get_cylindrical_coordinates_edge(edge, _z_axis(), self._plane()) == (1.5, 2.0)

    def test_non_coaxial_circle_is_none(self, wire_geometry):
        edge = _Edge(Circle3D(FakeVector3D(0, 0, 1), FakePoint(2, 0, 1), 1.5))
        assert hold.get_cylindrical_coordinates_edge(edge, _z_axis(), self._plane()) is None

    def test_line_edge_is_none(self, wire_geometry):
        edge = _Edge(Line3D(FakePoint(0, 0, 0), FakePoint(1, 0, 0)))
        assert hold.get_cylindrical_coordinates_edge(edge, _z_axis(), self._plane()) is None


# ── filter_points: coincident-z collapse ─────────────────────────────────────

class TestFilterPoints:
    def test_keeps_two_largest_radii_per_z(self):
        pts = [(1.0, 2.0, 0), (0.4, 2.0, 1), (0.9, 2.0, 1), (0.5, 0.0, 1)]
        out = hold.filter_points(pts)
        assert (0.4, 2.0, 1) not in out          # the smallest of three at z=2 dropped
        assert (1.0, 2.0, 0) in out and (0.9, 2.0, 1) in out

    def test_lowest_z_keeps_only_the_largest(self):
        pts = [(0.5, 0.0, 1), (1.0, 0.0, 0), (1.0, 3.0, 0)]
        out = hold.filter_points(pts)
        assert (0.5, 0.0, 1) not in out and (1.0, 0.0, 0) in out


# ── get_tool_profile: the body-of-revolution reduction, end to end ───────────

def _circ_edge(z, r):
    return _Edge(Circle3D(FakeVector3D(0, 0, 1), FakePoint(0, 0, z), r))


def _coaxial_face(geometry, edges):
    f = _Face(geometry)
    f.edges = edges
    return f


def _body(faces):
    b = _Face(None)                                 # any attribute bag; only .edges/.faces are read
    b.faces = faces
    all_edges = [e for f in faces for e in f.edges]
    for e in all_edges:
        e.startVertex = _DatumVtx(e.geometry.center)
        e.endVertex = _DatumVtx(e.geometry.center)
    b.edges = all_edges
    return b


class TestGetToolProfile:
    def test_cylinder_plus_cone_reduce_to_two_segments(self, wire_geometry):
        # A straight shank (r=1cm, z 0..2) under a taper (r 1 -> 0.5, z 2..3), both coaxial with
        # z: the reduction must express them as [z0, z1, r0, r1] segments in order.
        cyl = _coaxial_face(Cylinder(FakeVector3D(0, 0, 1), FakePoint(0, 0, 0)),
                            [_circ_edge(0, 1.0), _circ_edge(2, 1.0)])
        cone = _coaxial_face(Cone(FakeVector3D(0, 0, 1), FakePoint(0, 0, 2)),
                             [_circ_edge(2, 1.0), _circ_edge(3, 0.5)])
        profile = hold.get_tool_profile(_body([cyl, cone]), _z_axis(), FakePoint(0, 0, 0))
        assert profile == [[0.0, 2.0, 1.0, 1.0], [2.0, 3.0, 1.0, 0.5]]

    def test_duplicate_coaxial_face_collapses(self, wire_geometry):
        # Two identical cylinder faces (a split face pair) must not double a segment.
        mk = lambda: _coaxial_face(Cylinder(FakeVector3D(0, 0, 1), FakePoint(0, 0, 0)),
                                   [_circ_edge(0, 1.0), _circ_edge(2, 1.0)])
        profile = hold.get_tool_profile(_body([mk(), mk()]), _z_axis(), FakePoint(0, 0, 0))
        assert profile == [[0.0, 2.0, 1.0, 1.0]]

    def test_off_axis_face_is_ignored(self, wire_geometry):
        cyl = _coaxial_face(Cylinder(FakeVector3D(0, 0, 1), FakePoint(0, 0, 0)),
                            [_circ_edge(0, 1.0), _circ_edge(2, 1.0)])
        off = _coaxial_face(Cylinder(FakeVector3D(0, 0, 1), FakePoint(5, 0, 0)),
                            [_Edge(Circle3D(FakeVector3D(0, 0, 1), FakePoint(5, 0, 1), 0.2))])
        profile = hold.get_tool_profile(_body([cyl, off]), _z_axis(), FakePoint(0, 0, 0))
        assert profile == [[0.0, 2.0, 1.0, 1.0]]

    def test_planar_faces_alone_yield_no_profile(self, wire_geometry):
        cap = _coaxial_face(Plane(FakeVector3D(0, 0, 1), FakePoint(0, 0, 0)), [])
        assert hold.get_tool_profile(_body([cap]), _z_axis(), FakePoint(0, 0, 0)) == []


# ── build_holder_data: the CAM library JSON ──────────────────────────────────

class TestBuildHolderData:
    def test_segments_convert_cm_to_mm_heights_and_diameters(self):
        # Profile lengths arrive in cm (the API unit); the JSON is millimeters: height = (z1-z0)
        # x10, diameters = r x10 x2.
        data = hold.build_holder_data([[0.0, 2.0, 1.0, 1.0], [2.0, 3.0, 1.0, 0.5]], "T1 holder")
        assert data["unit"] == "millimeters" and data["type"] == "holder"
        assert data["segments"] == [
            {"height": 20.0, "lower-diameter": 20.0, "upper-diameter": 20.0},
            {"height": 10.0, "lower-diameter": 20.0, "upper-diameter": 10.0},
        ]

    def test_identity_fields(self):
        data = hold.build_holder_data([], "desc", prodid="PID", prodlink="L", prodvendor="V")
        assert data["guid"].startswith("00000000-0000-0000-0000-")
        assert data["reference_guid"] == data["guid"]
        assert (data["product-id"], data["product-link"], data["vendor"]) == ("PID", "L", "V")
        assert data["description"] == "desc"
