"""Unit tests for ``assembly_move.py`` - the free reposition.

The logic pinned here, no live Fusion: assembly_move building a Matrix3D translation/rotation and
applying it to occurrence.transform2 (the same property assembly_get's own read path prefers), the
unchanged-transform refusal, and the jointed_warning.
"""

import math

import pytest

from conftest import (BRepEdge, FakeInfiniteLine3D, FakeJoint, FakeMatrix3D, FakeOccurrence,
                      FakePoint, FakeVector3D, Line3D, MakeComp, MakeDesign, _NamedCollection,
                      install, load_tool, make_bbox, payload)

import adsk.core
import adsk.fusion


asm = load_tool("assembly_move")


class _ConstructionAxis:
    """A datum axis: its line reads as an InfiniteLine3D, and `component` is the owner a world lift
    is decided against - the same one means the local geometry already reads world."""
    def __init__(self, geometry, component=None):
        self.geometry = geometry
        self.component = component


def _occurrence(path="Block:1", joints=(), **kw):
    """One occurrence a move can write: two DISTINCT placement matrices, so a write to the legacy
    `transform` instead of `transform2` is visible, plus the joints the move guard names."""
    kw.setdefault("component", MakeComp(name=path.split("+")[-1].split(":")[0]))
    kw.setdefault("transform", FakeMatrix3D())
    kw.setdefault("transform2", FakeMatrix3D())
    return FakeOccurrence(path=path, joints=[FakeJoint(name=n) for n in joints], **kw)


def _basis(matrix):
    """The (x, y, z) axes the matrix's rotation puts the world basis on."""
    _origin, *axes = matrix.getAsCoordinateSystem()
    return [(a.x, a.y, a.z) for a in axes]


def _applied(matrix, point):
    """`point` through the matrix - the 16-cell array's rotation plus its translation column."""
    a = matrix.asArray()
    return tuple(a[i * 4] * point[0] + a[i * 4 + 1] * point[1] + a[i * 4 + 2] * point[2]
                 + a[i * 4 + 3] for i in range(3))


@pytest.fixture(autouse=True)
def _geometry_factories(monkeypatch):
    """Point the adsk geometry factories and the kinds' isinstance targets at the shared fakes."""
    monkeypatch.setattr(adsk.core.Matrix3D, "create", staticmethod(FakeMatrix3D), raising=False)
    monkeypatch.setattr(adsk.core.Vector3D, "create", staticmethod(FakeVector3D), raising=False)
    monkeypatch.setattr(adsk.core.Point3D, "create", staticmethod(FakePoint), raising=False)
    monkeypatch.setattr(adsk.fusion, "BRepEdge", BRepEdge, raising=False)
    monkeypatch.setattr(adsk.fusion, "SketchLine", type("SketchLine", (), {}), raising=False)
    monkeypatch.setattr(adsk.fusion, "ConstructionAxis", _ConstructionAxis, raising=False)


@pytest.fixture
def wire():
    """Build a design placing `occurrences` (resolving `tokens`) and wire both tool seams."""
    def build(*occurrences, tokens=None):
        occs = list(occurrences) or [_occurrence()]
        design = install(asm, MakeDesign(comp=MakeComp(occurrences=occs), tokens=tokens))
        return design, occs
    return build


class _BodyCarryingOccurrence(FakeOccurrence):
    """An instance whose world body box FOLLOWS its placement (or, with carries=False, one whose
    transform moves while the body geometry stays exactly where it was)."""

    def __init__(self, carries=True, **kw):
        super().__init__(**kw)
        self._carries = carries

    @property
    def boundingBox(self):
        t = self.transform2.translation if self._carries else None
        origin = (t.x, t.y, t.z) if t is not None else (0.0, 0.0, 0.0)
        return make_bbox(origin, tuple(c + 1.0 for c in origin))


class TestBodyGeometryCarried:
    """The transform is a CLAIM; the part's own body corner is the EVIDENCE."""

    def test_a_transform_the_body_geometry_did_not_follow_bites(self, wire):
        wire(_BodyCarryingOccurrence(carries=False, path="Block:1",
                                     component=MakeComp(name="Block"),
                                     transform=FakeMatrix3D(), transform2=FakeMatrix3D()))
        res = asm.handler(occurrence="Block:1", dx=10)
        assert res["isError"] is True
        assert "body geometry did NOT move" in res["message"]

    def test_a_body_that_travelled_publishes_how_far_it_went(self, wire):
        wire(_BodyCarryingOccurrence(path="Block:1", component=MakeComp(name="Block"),
                                     transform=FakeMatrix3D(), transform2=FakeMatrix3D()))
        out = payload(asm.handler(occurrence="Block:1", dx=10))
        assert out["geometry_moved_mm"] == 10.0

    def test_a_rotation_only_move_is_not_judged_by_the_box(self, wire):
        # a symmetric part turned about its own axis keeps its world box - no verdict, no refusal
        wire(_BodyCarryingOccurrence(carries=False, path="Block:1",
                                     component=MakeComp(name="Block"),
                                     transform=FakeMatrix3D(), transform2=FakeMatrix3D()))
        out = payload(asm.handler(occurrence="Block:1", rotate_deg=90))
        assert "geometry_moved_mm" not in out

    def test_an_instance_with_no_readable_box_is_not_refused(self, wire):
        wire()                                  # the plain fake carries no bounding box at all
        assert payload(asm.handler(occurrence="Block:1", dx=10))["moved"] is True


class TestMove:
    def test_move_that_does_not_take_bites(self, wire):
        # the transform assignment is accepted but the pose reads unchanged -> error, not ok
        class FrozenMatrix(FakeMatrix3D):
            """A placement no assignment reaches: every read is the same pose."""
            def asArray(self):
                return [1.0] * 16

        wire(_occurrence(transform2=FrozenMatrix()))
        res = asm.handler(occurrence="Block:1", dx=10)
        assert res["isError"] is True
        assert "did not move" in res["message"]

    # An UNREADABLE transform is not a confirmation. The compare that proves the move took needs a
    # reading on BOTH sides, so a missing reading on either one is a refusal - publishing moved:true
    # with a null position beside it would assert an effect no read took. Each side is pinned
    # separately so a guard covering only one of them still goes red.
    def test_an_unreadable_pose_AFTER_the_write_is_refused_not_moved_true(self, wire):
        class BlindAfter(FakeMatrix3D):
            """A placement whose BEFORE read answers and whose AFTER read declines."""
            def __init__(self):
                super().__init__()
                self.reads = 0

            def asArray(self):
                self.reads += 1
                if self.reads > 1:
                    raise RuntimeError("transform unreadable")
                return super().asArray()

        wire(_occurrence(transform2=BlindAfter()))
        res = asm.handler(occurrence="Block:1", dx=10)
        assert res["isError"] is True
        assert "UNCONFIRMED" in res["message"]
        assert "after the change" in res["message"]

    def test_an_unreadable_pose_BEFORE_the_write_is_refused_not_moved_true(self, wire):
        class BlindBefore(FakeMatrix3D):
            """A placement whose FIRST read declines and whose later reads answer."""
            def __init__(self):
                super().__init__()
                self.reads = 0

            def asArray(self):
                self.reads += 1
                if self.reads == 1:
                    raise RuntimeError("transform unreadable")
                return super().asArray()

        wire(_occurrence(transform2=BlindBefore()))
        res = asm.handler(occurrence="Block:1", dx=10)
        assert res["isError"] is True
        assert "UNCONFIRMED" in res["message"]
        assert "before the change" in res["message"]

    def test_a_wholly_unreadable_pose_names_both_sides(self, wire):
        class Blind(FakeMatrix3D):
            """A placement that never reads back at all."""
            def asArray(self):
                raise RuntimeError("transform unreadable")

        wire(_occurrence(transform2=Blind()))
        res = asm.handler(occurrence="Block:1", dx=10)
        assert res["isError"] is True
        assert "before and after the change" in res["message"]

    def test_a_readable_pose_on_both_sides_still_moves(self, wire):
        # the refusal is scoped to the unreadable case - a normal move is untouched by it
        wire()
        out = payload(asm.handler(occurrence="Block:1", dx=10))
        assert out["moved"] is True
        assert "UNCONFIRMED" not in out.get("note", "")

    def test_translate_sets_transform(self, wire):
        _design, occs = wire()
        out = payload(asm.handler(occurrence="Block:1", dx=10, dy=0, dz=5, units="mm"))
        # a transform2 matrix was written back (transform2, not transform - matches assembly_get's
        # own read path)
        assert occs[0].transform2 is not None
        assert out["moved"] is True
        assert out["translation"] == {"x": 10, "y": 0, "z": 5}
        # 'position' is the pose READ BACK, and translation.* answers in Fusion's internal cm: it
        # has to arrive in the caller's units, or a caller steers the next move off a 10x figure.
        assert out["position"] == {"x": 10.0, "y": 0.0, "z": 5.0}

    def test_writes_transform2_not_transform(self, wire):
        # the move path writes Occurrence.transform2 - the property assembly_get's read path
        # prefers - not the legacy Occurrence.transform, which must stay untouched by the move.
        _design, occs = wire()
        original_transform = occs[0].transform
        payload(asm.handler(occurrence="Block:1", dx=10, units="mm"))
        assert occs[0].transform2 is not original_transform
        assert occs[0].transform is original_transform

    def test_translation_scaled_to_cm(self, wire):
        _design, occs = wire()
        payload(asm.handler(occurrence="Block:1", dx=10, units="mm"))
        # the Vector3D used for translation should be in cm (10mm -> 1cm)
        vec = occs[0].transform2.translation
        assert vec is not None and abs(vec.x - 1.0) < 1e-9

    def test_missing_occurrence_errors(self, wire):
        wire()
        res = asm.handler(occurrence="Ghost", dx=5)
        assert res["isError"] is True and "no occurrence matching" in res["message"].lower()

    def test_zero_move_errors(self, wire):
        wire()
        res = asm.handler(occurrence="Block:1")
        assert res["isError"] is True and "no movement" in res["message"].lower()

    def test_rotate_world_axis(self, wire):
        _design, occs = wire()
        out = payload(asm.handler(occurrence="Block:1", rotate_deg=90, rotate_axis="y"))
        # setToRotation takes RADIANS: 90 deg in must reach the API as pi/2, about world Y - which
        # swings the world +X axis onto -Z. Degrees-for-radians lands it nowhere near.
        assert _basis(occs[0].transform2)[0] == pytest.approx((0.0, 0.0, -1.0), abs=1e-9)
        assert out["rotate_axis"] == "y"

    def test_multi_axis_rotation(self, wire):
        _design, occs = wire()
        out = payload(asm.handler(occurrence="Block:1", rotate_x=90, rotate_z=45))
        # The handler builds ONE working matrix (per-axis rotations composed onto it), then composes
        # that onto the occurrence's original transform2 - so the working matrix is transform2's one
        # composition, and the per-axis rotations are ITS: X then Z, each angle in RADIANS.
        assert len(occs[0].transform2._composed) == 1
        working = occs[0].transform2._composed[0]
        assert len(working._composed) == 2          # the zero Y axis contributes no rotation
        first, second = working._composed
        diag = math.cos(math.radians(45))
        assert _basis(first)[1] == pytest.approx((0.0, 0.0, 1.0), abs=1e-9)   # +90 X swings +Y to +Z
        assert _basis(second)[0] == pytest.approx((diag, diag, 0.0), abs=1e-9)
        assert out["rotate_axis"] == "multi"
        assert out["rotate_xyz"] == {"x": 90, "y": 0, "z": 45}

    def test_single_and_multi_rejected_together(self, wire):
        wire()
        res = asm.handler(occurrence="Block:1", rotate_deg=30, rotate_x=10)
        assert res["isError"] is True and "not both" in res["message"]

    # -- jointed-occurrence warning (posing a jointed part is allowed but transient) --
    # Moving a jointed occurrence poses it along its DOF (the sanctioned path - joint_edit redirects
    # here), but the pose is transient and a move that fights the joints over-constrains the solve. So
    # the move PROCEEDS and warns to capture_position + probe.
    # (NOT a refusal: refusing would dead-end the only safe pose path, since driving
    # jointMotion.rotationValue crashes the connection - see joint_edit.py.)

    def test_move_jointed_occurrence_proceeds_with_warning(self, wire):
        _design, occs = wire(_occurrence(joints=["Flywheel_Spin", "Rigid3"]))
        out = payload(asm.handler(occurrence="Block:1", rotate_deg=180, rotate_axis="x"))
        # it MOVED (pose path is allowed)
        assert out["moved"] is True
        assert _basis(occs[0].transform2)[1] == pytest.approx((0.0, -1.0, 0.0), abs=1e-9)
        # and it WARNED, naming the joints + the capture/health-check next step
        assert "Flywheel_Spin" in out["jointed_joints"]
        assert "capture_position" in out["jointed_warning"]
        assert "assembly_get" in out["jointed_warning"]

    def test_quiet_suppresses_the_jointed_warning(self, wire):
        wire(_occurrence(joints=["Flywheel_Spin"]))
        out = payload(asm.handler(occurrence="Block:1", dx=5, quiet=True))
        assert out["moved"] is True
        assert "jointed_warning" not in out

    def test_unjointed_move_has_no_warning(self, wire):
        wire()  # default: no joints
        out = payload(asm.handler(occurrence="Block:1", dx=5))
        assert out["moved"] is True
        assert "jointed_warning" not in out

    def test_rotate_about_edge_handle(self, wire):
        # AxisRef edge path: rotate about a straight EDGE's line (hinge), not the occ origin. The
        # edge runs along X but sits OFF the world X axis, so rotating about the edge and rotating
        # about the world axis are different transforms and the pivot is readable.
        edge = BRepEdge(Line3D(FakePoint(5.0, 2.0, 0.0), FakePoint(9.0, 2.0, 0.0)))
        handle = "/v" + "E" * 70
        _design, occs = wire(tokens={handle: edge})
        out = payload(asm.handler(occurrence="Block:1", rotate_deg=45, rotate_axis=handle))
        # a point ON the edge is held fixed and one 1 cm above it swings 45 deg: the derived
        # direction, the pivot and the degrees-to-radians conversion in one reading.
        placed, diag = occs[0].transform2, math.sin(math.radians(45))
        assert _applied(placed, (5.0, 2.0, 0.0)) == pytest.approx((5.0, 2.0, 0.0), abs=1e-9)
        assert _applied(placed, (5.0, 2.0, 1.0)) == pytest.approx((5.0, 2.0 - diag, diag), abs=1e-9)
        assert out["rotate_axis"] == "edge"

    def test_rotate_about_construction_axis_infinite_line(self, wire):
        # A ConstructionAxis carries its line as an InfiniteLine3D - origin/direction read straight
        # off it, the fallback with no start/end derivation. AxisRef routes a datum by TYPE, so the
        # curveType a bounded edge is screened on is never read here.
        axis = _ConstructionAxis(FakeInfiniteLine3D(FakePoint(2.0, 2.0, 0.0),
                                                    FakeVector3D(0.0, 0.0, 1.0)))
        handle = "/v" + "A" * 70
        design, occs = wire(tokens={handle: axis})
        axis.component = design.rootComponent      # root-owned: the local line already reads world
        out = payload(asm.handler(occurrence="Block:1", rotate_deg=30, rotate_axis=handle))
        # the axis origin (2,2,0) is the pivot - a rotation about the world Z axis would move it
        placed = occs[0].transform2
        turned = (2.0 + math.cos(math.radians(30)), 2.0 + math.sin(math.radians(30)), 0.0)
        assert _applied(placed, (2.0, 2.0, 0.0)) == pytest.approx((2.0, 2.0, 0.0), abs=1e-9)
        assert _applied(placed, (3.0, 2.0, 0.0)) == pytest.approx(turned, abs=1e-9)
        assert out["rotate_axis"] == "edge"

    def test_unreadable_edge_geometry_errors(self, wire):
        # An edge whose line geometry exposes neither start/end points nor origin/direction
        # must error, never rotate about a guessed axis.
        handle = "/v" + "E" * 70
        wire(tokens={handle: BRepEdge(Line3D())})
        res = asm.handler(occurrence="Block:1", rotate_deg=45, rotate_axis=handle)
        assert res["isError"] is True
        assert "line geometry" in res["message"]

    def test_combined_rotate_and_translate_preserves_pivot(self, wire, monkeypatch):
        # A SINGLE call doing rotation-about-a-pivot AND translation must not assign
        # `mat.translation = vec` on the rotation matrix (that overwrites the pivot column, so the part
        # rotates about the WORLD origin). The translation is composed as its OWN matrix. Use the
        # edge-rotate path (a real non-origin pivot) + a translation in the same call.
        edge = BRepEdge(Line3D(FakePoint(5.0, 2.0, 0.0), FakePoint(9.0, 2.0, 0.0)))
        handle = "/v" + "E" * 70
        wire(tokens={handle: edge})
        created = []
        monkeypatch.setattr(adsk.core.Matrix3D, "create",
                            staticmethod(lambda: created.append(FakeMatrix3D()) or created[-1]),
                            raising=False)
        out = payload(asm.handler(occurrence="Block:1", rotate_deg=90, rotate_axis=handle, dx=10))
        assert out["moved"] is True
        # the matrix that holds the rotation must NOT have had .translation assigned directly.
        rot_mats = [m for m in created if m._rotated]
        assert rot_mats, "expected a rotation matrix to be created"
        for m in rot_mats:
            assert m._direct_translation is False, (
                "translation was assigned directly onto the rotation matrix - clobbers the pivot column")
        # and the 10 mm (1 cm) translation was applied by COMPOSITION (transformBy), not assignment.
        composed = [c.translation for m in created for c in m._composed]
        assert any(abs(t.x - 1.0) < 1e-9 for t in composed), (
            "the translation should be composed via transformBy")


class TestMoveNote:
    def test_jointed_move_note_differs_from_free_move(self, wire):
        # the result 'note' reflects whether the part was posed (jointed) vs a plain free move.
        _design, occs = wire()
        free = payload(asm.handler(occurrence="Block:1", dx=5))
        assert "free move" in free["note"]
        occs[0].joints = _NamedCollection([FakeJoint(name="Spin1")])
        posed = payload(asm.handler(occurrence="Block:1", dx=5))
        assert "jointed" in posed["note"].lower()
