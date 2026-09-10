"""Unit tests for sketch_add_3d_line.py - the off-plane end point and the construction flag."""

import pytest
from conftest import load_tool
from _sketch_fakes import FakeSketch, _Curve, _payload, draw_installer, scoped_installer

sk = load_tool("sketch_add_3d_line")
_install_draw = draw_installer(sk)
_install_scoped = scoped_installer(sk)


@pytest.fixture
def shared_name(monkeypatch):
    """'Sketch1' in BOTH components. The two sketches start with DIFFERENT curve counts (Alpha has
    one line already, Beta none), so a call that reached the wrong one is visible in the counts and
    not merely in a name that both sketches share."""
    alpha, beta = FakeSketch("Sketch1"), FakeSketch("Sketch1")
    alpha.sketchLines._land(1)
    design = _install_scoped(monkeypatch, [("Alpha", [alpha]), ("Beta", [beta])])
    return design, alpha, beta


class TestDraw3dLine:

    def _line_sketch(self):
        s = FakeSketch("S3D")

        def _add(p1, p2):
            c = _Curve()
            c.startSketchPoint = type("SP", (), {"geometry": p1})()
            c.endSketchPoint = type("SP", (), {"geometry": p2})()
            return c
        s.sketchLines.addByTwoPoints = _add
        s.originPoint = object()
        return s

    def test_end_off_plane_detected_and_scaled(self, monkeypatch):
        s = self._line_sketch(); _install_draw(monkeypatch, s)
        out = _payload(sk.handler(x1=0, y1=0, z1=0, x2=0, y2=0, z2=10, units="mm"))
        # z 10mm -> end z back in mm = 10; flagged off-plane
        assert out["end"]["z"] == 10.0
        assert out["end_is_off_plane"] is True

    def test_on_plane_end_not_flagged(self, monkeypatch):
        s = self._line_sketch(); _install_draw(monkeypatch, s)
        out = _payload(sk.handler(x1=0, y1=0, z1=0, x2=10, y2=0, z2=0, units="mm"))
        assert out["end_is_off_plane"] is False
        assert out["end"]["x"] == 10.0

    def test_missing_end_point_errors(self, monkeypatch):
        s = self._line_sketch(); _install_draw(monkeypatch, s)
        res = sk.handler(x1=0, y1=0, z1=0, x2=5, y2=5)   # z2 missing
        assert res["isError"] is True and "x2, y2, z2" in res["message"]

    def test_is_construction_marks_the_line_and_reports_it(self, monkeypatch):
        s = self._line_sketch(); _install_draw(monkeypatch, s)
        out = _payload(sk.handler(x2=1, y2=1, z2=1, is_construction=True))
        assert out["is_construction"] is True             # read BACK off the line, not echoed

    def test_default_is_not_construction(self, monkeypatch):
        s = self._line_sketch(); _install_draw(monkeypatch, s)
        out = _payload(sk.handler(x2=1, y2=1, z2=1))
        assert out["is_construction"] is False

    def test_a_stuck_construction_flag_is_published_as_the_line_reads_it(self, monkeypatch):
        # the flag assignment is accepted and changes nothing: the payload is a READ of the line
        # that landed, so it publishes false. Echoing the request would claim construction geometry
        # over a line that is still a profile edge.
        s = self._line_sketch(); _install_draw(monkeypatch, s)

        class _Stuck:
            def __init__(self, p1, p2):
                self.startSketchPoint = type("SP", (), {"geometry": p1})()
                self.endSketchPoint = type("SP", (), {"geometry": p2})()

            @property
            def isConstruction(self):
                return False

            @isConstruction.setter
            def isConstruction(self, v):
                pass                                  # accepted and ignored

        s.sketchLines.addByTwoPoints = lambda p1, p2: _Stuck(p1, p2)
        res = sk.handler(x2=1, y2=1, z2=1, is_construction=True)
        assert res["isError"] is False                # the line WAS drawn - not an error
        out = _payload(res)
        assert out["is_construction"] is False        # as it READS, not as it was asked for

    def test_is_construction_set_failure_is_reported(self, monkeypatch):
        # the API rejecting the flag must surface (naming the drawn-but-unmarked state), not no-op
        s = self._line_sketch(); _install_draw(monkeypatch, s)

        class _Locked:
            def __init__(self, p1, p2):
                self.startSketchPoint = type("SP", (), {"geometry": p1})()
                self.endSketchPoint = type("SP", (), {"geometry": p2})()
            @property
            def isConstruction(self):
                return False
            @isConstruction.setter
            def isConstruction(self, v):
                raise RuntimeError("isConstruction locked")
        s.sketchLines.addByTwoPoints = lambda p1, p2: _Locked(p1, p2)
        res = sk.handler(x2=1, y2=1, z2=1, is_construction=True)
        assert res["isError"] is True and "could not be marked construction" in res["message"]


class TestSharedSketchNameRefused:

    _REFUSAL = "2 sketches are named 'S' ('S' in Root, 'S' in Frame)"

    def test_draw_3d_line_refuses_with_its_owners(self, monkeypatch):
        _install_draw(monkeypatch, FakeSketch("S"))
        monkeypatch.setattr(sk._common, "find_or_recent_sketch",
                            lambda d, n, remedy=None: (None, n, self._REFUSAL))
        res = sk.handler(sketch_name="S", x2=1, y2=1, z2=1)
        assert res["isError"] is True
        assert res["message"] == self._REFUSAL and "No sketch named" not in res["message"]


class TestSketchNameReporting:

    def test_draw_3d_line_blank_name_with_no_sketch_says_nothing_to_draw_on(self, monkeypatch):
        _install_draw(monkeypatch, None)
        res = sk.handler(sketch_name="", x2=1, y2=1, z2=1)
        assert res["isError"] is True
        assert "No sketch to draw on" in res["message"] and "No sketch named" not in res["message"]


class TestDrawComponentScope:

    def test_3d_line_scope_reaches_the_named_components_sketch(self, shared_name):
        _d, alpha, beta = shared_name
        before_alpha = alpha.sketchLines.count
        out = _payload(sk.handler(sketch_name="Sketch1", component="Beta",
                                               x2=1, y2=1, z2=1))
        assert out["sketch_name"] == "Sketch1"
        assert beta.sketchLines.count == 1 and alpha.sketchLines.count == before_alpha

    def test_3d_line_unscoped_shared_name_refuses_and_names_the_scope_input(self, shared_name):
        _d, alpha, beta = shared_name
        res = sk.handler(sketch_name="Sketch1", x2=1, y2=1, z2=1)
        assert res["isError"] is True
        assert "2 sketches are named 'Sketch1'" in res["message"] and "'component'" in res["message"]
        assert beta.sketchLines.count == 0
