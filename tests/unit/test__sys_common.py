"""Unit tests for ``_sys_common.py`` - the selection record both selection tools publish.

Vector normalization (``_geom.unit_vector``, including the zero-vector guard) is pinned in
test__geom.py, shared with find_geometry. The bug surface here is the geometry classification:
``_face_direction`` / ``_edge_direction`` (which branch on the runtime surface/curve type name), and
``_classify`` (the big dispatch by ``type(entity).__name__``). None of it needs a live Fusion - only
fakes whose class names match Fusion's type names.
"""

import types

from conftest import (
    BRepBody,
    BRepEdge,
    BRepFace,
    Circle3D,
    Cylinder,
    FakePoint,
    FakeVector3D,
    Line3D,
    Plane,
    Sphere,
    load_tool,
)

sel = load_tool("_sys_common")


# ── _face_direction: branch on surface type ────────────────────────────────

class TestFaceDirection:
    def test_planar_face_returns_normal(self):
        face = BRepFace(Plane(FakeVector3D(0, 0, 2)))
        vec, kind = sel._face_direction(face)
        assert vec == [0.0, 0.0, 1.0]
        assert kind == "face_normal"

    def test_cylindrical_face_returns_axis(self):
        face = BRepFace(Cylinder(FakeVector3D(0, 10, 0)))
        vec, kind = sel._face_direction(face)
        assert vec == [0.0, 1.0, 0.0]
        assert kind == "axis"

    def test_sphere_has_no_direction(self):
        face = BRepFace(Sphere())
        vec, kind = sel._face_direction(face)
        assert vec is None
        assert kind is None


# ── _edge_direction: branch on curve type ──────────────────────────────────

class TestEdgeDirection:
    def test_linear_edge_direction_is_end_minus_start(self):
        edge = BRepEdge(Line3D(), start=FakePoint(1, 0, 0), end=FakePoint(4, 0, 0))
        vec, kind = sel._edge_direction(edge)
        assert vec == [1.0, 0.0, 0.0]   # +X, unit
        assert kind == "edge_direction"

    def test_circular_edge_direction_is_plane_normal(self):
        edge = BRepEdge(Circle3D(FakeVector3D(0, 0, 7)))
        vec, kind = sel._edge_direction(edge)
        assert vec == [0.0, 0.0, 1.0]
        assert kind == "axis"


# ── _classify: dispatch by entity type ─────────────────────────────────────

class TestClassify:
    def test_face_is_classified_with_direction(self):
        out = sel._classify(BRepFace(Plane(FakeVector3D(0, 0, 1))))
        assert out["object_type"] == "BRepFace"
        assert out["kind"] == "face"
        assert out["surface_type"] == "Plane"
        assert out["direction"] == [0.0, 0.0, 1.0]
        assert out["direction_kind"] == "face_normal"

    def test_edge_is_classified_as_edge(self):
        out = sel._classify(BRepEdge(Line3D(), start=FakePoint(0, 0, 0), end=FakePoint(0, 2, 0)))
        assert out["kind"] == "edge"
        assert out["curve_type"] == "Line3D"
        assert out["direction"] == [0.0, 1.0, 0.0]

    def test_unknown_entity_falls_through_to_other(self):
        class Mystery:
            name = "weird"
        out = sel._classify(Mystery())
        assert out["object_type"] == "Mystery"
        assert out["kind"] == "other"


# ── _xyz: a position reads whole, or not at all ────────────────────────────

class _HalfReadablePoint:
    """A point whose .y read raises - the partly-readable case behind every published position."""

    x = 1.0
    z = 3.0

    @property
    def y(self):
        raise RuntimeError("4 : An API Object refers to a deleted Object")


class TestPointReads:
    def test_a_point_whose_component_will_not_read_is_null_not_the_origin(self):
        # 0.0 for the component that failed publishes a coordinate the read never took, and the
        # caller cannot tell it from a part really sitting on that plane.
        assert sel._xyz(_HalfReadablePoint()) is None

    def test_a_non_numeric_component_is_null(self):
        from unittest.mock import Mock
        assert sel._xyz(types.SimpleNamespace(x=Mock(), y=0.0, z=0.0)) is None

    def test_an_absent_point_is_null(self):
        assert sel._xyz(None) is None

    def test_a_genuine_origin_is_still_a_reading(self):
        # 0,0,0 is an ANSWER (a vertex at the world origin), distinguishable from the null above
        assert sel._xyz(FakePoint(0, 0, 0)) == {"x": 0.0, "y": 0.0, "z": 0.0}

    def test_components_are_rounded_to_six_places(self):
        assert sel._xyz(FakePoint(1.23456789, -2.0, 3.5)) == {"x": 1.234568, "y": -2.0, "z": 3.5}

    def test_a_records_picked_point_is_null_rather_than_a_fabricated_origin(self):
        rec = sel._selection_record(types.SimpleNamespace(entity=None, point=_HalfReadablePoint()))
        assert rec["picked_point"] is None


# ── _geometry_handle / _selection_record: find_geometry-style handle minting ────────────────────

class TestGeometryHandle:
    def test_face_handle_uses_centroid_and_token(self):
        face = BRepFace(Plane(FakeVector3D(0, 0, 1)), centroid=FakePoint(1, 2, 3), entity_token="TOK_F")
        h = sel._geometry_handle(face, "face")
        assert h == "TOK_F|@face:1.000000,2.000000,3.000000"

    def test_edge_handle_uses_point_on_edge_and_token(self):
        edge = BRepEdge(Line3D(), point_on_edge=FakePoint(4, 5, 6), entity_token="TOK_E")
        h = sel._geometry_handle(edge, "edge")
        assert h == "TOK_E|@edge:4.000000,5.000000,6.000000"

    def test_vertex_handle_uses_geometry_point(self):
        vertex = types.SimpleNamespace(geometry=FakePoint(7, 8, 9), entityToken="TOK_V")
        h = sel._geometry_handle(vertex, "vertex")
        assert h == "TOK_V|@vertex:7.000000,8.000000,9.000000"

    def test_body_kind_mints_no_handle(self):
        # find_geometry itself mints no handle for a body (only face/edge/vertex) - a picked BODY
        # must not fabricate one either.
        assert sel._geometry_handle(BRepBody(name="Block"), "body") is None

    def test_component_kind_mints_no_handle(self):
        assert sel._geometry_handle(object(), "component") is None

    def test_missing_point_falls_back_to_bare_token(self):
        face = BRepFace(Plane(FakeVector3D(0, 0, 1)), centroid=None, entity_token="TOK_BARE")
        assert sel._geometry_handle(face, "face") == "TOK_BARE"


class TestSelectionRecordHandle:
    def _sel(self, entity, point=None):
        return types.SimpleNamespace(entity=entity, point=point or FakePoint(0, 0, 0))

    def test_face_selection_record_carries_handle(self):
        face = BRepFace(Plane(FakeVector3D(0, 0, 1)), centroid=FakePoint(1, 1, 1), entity_token="TOK1")
        rec = sel._selection_record(self._sel(face))
        assert rec["kind"] == "face"
        assert rec["handle"] == "TOK1|@face:1.000000,1.000000,1.000000"

    def test_body_selection_record_has_no_handle_key(self):
        rec = sel._selection_record(self._sel(BRepBody(name="Block")))
        assert rec["kind"] == "body"
        assert "handle" not in rec
