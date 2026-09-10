"""Unit tests for the polyline / closed_path sketch kind in sketch_add_geometry.py.

The gap: drawing a custom boundary from independent 'line' calls leaves each segment's endpoints
DISCONNECTED — dragging a vertex tears the shape apart (no coincident constraints). A polyline draws
a chain of connected lines that SHARE endpoints (each segment starts at the previous segment's
endSketchPoint), so the loop is continuous and parametric. 'close' welds the last point back to the
first.

Pinned here (no live Fusion): _draw_polyline chains points by reusing the prior line's
endSketchPoint (so endpoints are shared, not duplicated), and adds the closing segment. The fakes
record which point object each segment started from, so we can assert the chaining.
"""

from conftest import FakeSketchPoint as _SharedSketchPoint, load_tool, make_sketch

sk = load_tool("sketch_add_geometry")


# ── the shared point, plus the SketchLine(s) / GeometricConstraints recorders ──

_pid = [0]


class FakeSketchPoint(_SharedSketchPoint):
    """The shared point holding its (x, y) as geometry, plus `id` - the identity that tells two
    points at the same coordinates apart, which is what proves a chain SHARED one."""
    def __init__(self, x, y):
        super().__init__(geometry=(x, y))
        _pid[0] += 1
        self.id = _pid[0]


class FakeSketchLine:
    def __init__(self, start, end):
        # start/end may be a FakeSketchPoint (shared) or a coordinate tuple (new point -> wrap it)
        self.startSketchPoint = start if isinstance(start, FakeSketchPoint) else FakeSketchPoint(*start)
        self.endSketchPoint = end if isinstance(end, FakeSketchPoint) else FakeSketchPoint(*end)


class FakeSketchLines:
    def __init__(self):
        self.lines = []

    def addByTwoPoints(self, start, end):
        ln = FakeSketchLine(start, end)
        self.lines.append(ln)
        return ln


class FakeConstraints:
    def __init__(self):
        self.coincidents = []

    def addCoincident(self, a, b):
        self.coincidents.append((a, b))
        return ("coin", a, b)


def _sketch():
    """The shared Sketch fake, its sketchLines swapped for the recorder the chaining is read off."""
    s = make_sketch()
    s.sketchCurves.sketchLines = FakeSketchLines()
    s.geometricConstraints = FakeConstraints()
    return s


def _pt(x, y, k):
    # mirror sketches._pt's role for the test: return a coordinate tuple in cm
    return (x * k, y * k)


# ── chaining: consecutive segments SHARE the prior endpoint ─────────────────

import pytest


@pytest.fixture(autouse=True)
def _patch_pt(monkeypatch):
    # _draw_polyline calls sketches._pt (which uses the mocked Point3D.create -> a Mock). Make it
    # return a plain (x,y) coordinate tuple so the chaining logic is what's under test.
    monkeypatch.setattr(sk, "_pt", lambda x, y, k: (x * k, y * k))


class TestPolylineChaining:
    def _draw(self, points):
        s = _sketch()
        return s, sk._draw_polyline(s, points, k=0.1)

    def test_open_polyline_segment_count(self):
        # 4 points, open -> 3 segments
        s, label = self._draw([(0, 0), (10, 0), (10, 10), (0, 10)])
        assert len(s.sketchCurves.sketchLines.lines) == 3

    def test_repeated_first_point_closes_with_n_segments(self):
        # a loop closes by repeating the first point as the last: 4 vertices + repeat -> 4 segments
        s, label = self._draw([(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)])
        assert len(s.sketchCurves.sketchLines.lines) == 4

    def test_consecutive_segments_share_endpoint(self):
        # THE KEY PROPERTY: segment N's start IS segment N-1's end (same point object) →
        # parametric, draggable. Not two separate coincident points. 4 pts open -> 3 segments.
        s, _ = self._draw([(0, 0), (10, 0), (10, 10), (0, 10)])
        lines = s.sketchCurves.sketchLines.lines
        assert lines[1].startSketchPoint is lines[0].endSketchPoint
        assert lines[2].startSketchPoint is lines[1].endSketchPoint

    def test_closure_is_geometric_not_a_constraint(self):
        # the closing segment ends AT the first point's coordinates, and NO explicit closing
        # coincident constraint is added - that constraint is what the sketch solver rejects on
        # many outlines (VCS_SKETCH_SOLVING_FAILED, live-verified); geometric closure forms the
        # profile without it.
        s, _ = self._draw([(0, 0), (10, 0), (10, 10), (0, 0)])
        lines = s.sketchCurves.sketchLines.lines
        first, closing_end = lines[0].startSketchPoint, lines[-1].endSketchPoint
        assert closing_end.geometry == first.geometry
        assert not s.geometricConstraints.coincidents

    def test_needs_at_least_two_points(self):
        s = _sketch()
        res = sk._draw_polyline(s, [(0, 0)], k=0.1)
        assert res is None  # not enough points to draw anything
