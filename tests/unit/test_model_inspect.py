"""Tests for `model_inspect` — the measurement rich read (bbox default + include=['mass']; mesh routing).

Same fixture pattern as test_design_get/test_cam_get: stub the slice SEAMS + the TargetRef resolution,
assert the ROUTER's job — default = bbox, include=['mass'] returns mass INSTEAD of the box ('default'
keeps both), a MESH target routes to mesh stats,
the kind tag is surfaced, unknown include + target errors guard. The slice→handler delegation is proven
by live validation.
"""

import json
import math
from types import SimpleNamespace

import adsk.core
import pytest

from conftest import (load_tool, error_message, FakePoint, FakeBoundingBox3D, FakeMatrix3D,
                      FakeOccurrence, FakeVector3D, BRepBody, BRepFace, MakeComp, Plane,
                      body_proxy, make_design, make_occurrence, _NamedCollection)

mi = load_tool("model_inspect")


def _payload(result):
    assert result["isError"] is False, result
    return json.loads(result["content"][0]["text"])


def _resolve_to(monkeypatch, kind):
    """Make TargetRef resolve to (a dummy entity, `kind`) and a design exist."""
    monkeypatch.setattr(mi._common, "design", lambda: object())
    monkeypatch.setattr(mi._TARGET, "resolve", lambda raw: ((object(), kind), None))


def _ok(payload):
    return {"isError": False, "content": [{"type": "text", "text": json.dumps(payload)}]}


@pytest.fixture
def stub_slices(monkeypatch):
    """Stub the inline measure CORES (_bbox / _physical_properties) + the mesh core (imported lazily
    from _mesh_common). The router's job — dispatch + compose — is what these tests pin; the cores'
    own numbers are covered by live validation."""
    import sys
    monkeypatch.setattr(mi, "_bbox", lambda design, ent, desc, frame, units: _ok({"x": 10, "y": 5, "z": 2}))
    monkeypatch.setattr(mi, "_physical_properties",
                        lambda design, ent, desc, units, accuracy, per_body: _ok({"mass_kg": 1.5}))
    stub = type("Mesh", (), {"mesh_measure_of_body": staticmethod(
        lambda mb, units: _ok({"triangle_count": 900, "is_closed": True}))})
    # `from . import _mesh_common` binds the PACKAGE ATTRIBUTE when the real module is already
    # loaded (another tool imports it at top level), and falls back to sys.modules when it is not -
    # stub BOTH seams so the route is pinned regardless of what loaded first.
    monkeypatch.setitem(sys.modules, "mcpServer.tools._mesh_common", stub)
    monkeypatch.setattr(sys.modules["mcpServer.tools"], "_mesh_common", stub, raising=False)


class TestDefaultAndDispatch:
    def test_solid_default_is_bbox(self, monkeypatch, stub_slices):
        _resolve_to(monkeypatch, "body")
        out = _payload(mi.handler(target="Body1"))
        assert out["x"] == 10 and out["kind"] == "body"
        assert "mass" not in out                         # mass is opt-in
        assert "include=" in out["note"]                 # advertises mass

    def test_design_default_is_bbox(self, monkeypatch, stub_slices):
        _resolve_to(monkeypatch, "design")
        out = _payload(mi.handler(target=""))
        assert out["kind"] == "design" and "x" in out

    def test_include_mass_adds_properties(self, monkeypatch, stub_slices):
        _resolve_to(monkeypatch, "body")
        out = _payload(mi.handler(target="Body1", include=["mass"]))
        assert out["mass"]["mass_kg"] == 1.5

    def test_include_mass_omits_the_bounding_box(self, monkeypatch, stub_slices):
        # the deep read returns what was asked for: a mass read that also re-measures and re-sends
        # the box pays for both every call.
        _resolve_to(monkeypatch, "body")
        out = _payload(mi.handler(target="Body1", include=["mass"]))
        assert out["mass"]["mass_kg"] == 1.5
        assert "x" not in out and "lump_count" not in out
        assert out["kind"] == "body"                     # the resolved-target stamp stays

    def test_default_beside_mass_keeps_both(self, monkeypatch, stub_slices):
        _resolve_to(monkeypatch, "body")
        out = _payload(mi.handler(target="Body1", include=["default", "mass"]))
        assert out["x"] == 10 and out["mass"]["mass_kg"] == 1.5

    def test_default_alone_is_the_box(self, monkeypatch, stub_slices):
        _resolve_to(monkeypatch, "body")
        out = _payload(mi.handler(target="Body1", include=["default"]))
        assert out["x"] == 10 and "mass" not in out and "include=" in out["note"]

    def test_a_mass_read_does_not_run_the_box_measurement(self, monkeypatch, stub_slices):
        # the omission has to skip the MEASUREMENT, not just drop its keys - with 'frame' the box
        # costs a getOrientedBoundingBox call a mass-only read has no reason to pay for.
        _resolve_to(monkeypatch, "body")
        calls = []
        monkeypatch.setattr(mi, "_bbox",
                            lambda *a: calls.append(1) or _ok({"x": 10}))
        mi.handler(target="Body1", include=["mass"])
        assert calls == []

    def test_mesh_target_routes_to_mesh_stats(self, monkeypatch, stub_slices):
        _resolve_to(monkeypatch, "mesh")
        out = _payload(mi.handler(target="Mesh1"))
        assert out["triangle_count"] == 900 and out["kind"] == "mesh"
        assert "x" not in out                            # NOT the bbox path
        assert "mesh" in out["note"].lower()             # breadcrumb explains the mesh path

    def test_mesh_is_not_a_valid_include(self, monkeypatch, stub_slices):
        # mesh stats are automatic for a mesh target, NOT a selectable slice — advertising it would lie.
        _resolve_to(monkeypatch, "body")
        res = mi.handler(target="Body1", include=["mesh"])
        assert res["isError"] and "mesh" in error_message(res).lower()


class TestGuards:
    def test_unresolvable_target_errors(self, monkeypatch):
        monkeypatch.setattr(mi._common, "design", lambda: object())
        monkeypatch.setattr(mi._TARGET, "resolve", lambda raw: (None, "no such target 'Ghost'"))
        res = mi.handler(target="Ghost")
        assert "ghost" in error_message(res).lower()

    def test_unknown_include_errors(self, monkeypatch, stub_slices):
        _resolve_to(monkeypatch, "body")
        res = mi.handler(target="Body1", include=["bogus"])
        assert "bogus" in error_message(res).lower() or "unknown" in error_message(res).lower()

    def test_the_refusal_lists_default_beside_mass(self, monkeypatch, stub_slices):
        # the refusal IS the vocabulary a caller that mistyped reads next; naming only 'mass' hides
        # the token that keeps the bounding box beside it.
        _resolve_to(monkeypatch, "body")
        msg = error_message(mi.handler(target="Body1", include=["bogus"]))
        assert "mass" in msg and "default" in msg

    def test_the_include_enum_matches_the_slice_tuple(self):
        # Catches a HAND-EDITED schema drifting from the tuple. It cannot catch a name added to the
        # tuple itself - both sides read it - which is what the dispatch test below covers.
        enum = mi.tool.input_schema["properties"]["include"]["items"]["enum"]
        assert sorted(enum) == sorted(mi._SLICES + mi._DEFAULT_NAMES)

    def test_every_advertised_slice_actually_dispatches(self, monkeypatch, stub_slices):
        # A name the guard admits but no branch reads returns the box again under a token that
        # promised something deeper.
        _resolve_to(monkeypatch, "body")
        for name in mi._SLICES:
            assert name in _payload(mi.handler(target="Body1", include=[name])), name
        for name in mi._DEFAULT_NAMES:
            # the default tokens keep the orientation measurement rather than adding a key
            assert "x" in _payload(mi.handler(target="Body1", include=[name])), name


class TestBodyAabb:
    """The default bbox spans BODIES only, read through the SHARED _geom.body_aabb (its
    boundingBox2/fallback behavior is pinned in test__geom.py) - so an orphaned datum/sketch does
    not inflate it and model_inspect + assembly_get agree on one occurrence's size."""

    def test_default_bbox_reads_via_geom_body_aabb(self, monkeypatch):
        monkeypatch.setattr(mi._common, "design", lambda: object())
        monkeypatch.setattr(mi._TARGET, "resolve", lambda raw: ((object(), "occurrence"), None))
        seen = []
        def fake_aabb(entity):
            seen.append(entity)
            return FakeBoundingBox3D(FakePoint(0, 0, 0), FakePoint(1, 2, 3))   # cm
        monkeypatch.setattr(mi._geom, "body_aabb", fake_aabb)
        out = _payload(mi.handler(target="Occ:1", units="mm"))
        assert len(seen) == 1                    # the box came from the bodies-only helper
        assert out["x"] == 10.0 and out["y"] == 20.0 and out["z"] == 30.0


class TestBodyLumpCount:
    """A body row discloses its DISCONNECTED-piece count. A multi-lump body is usually a shipped
    defect (a join that fused nothing) and no bbox or mass read can show it - the number has to come
    from the one read that can, _geom.lump_count."""

    def _resolve_body(self, monkeypatch, body, kind="body"):
        monkeypatch.setattr(mi._common, "design", lambda: object())
        monkeypatch.setattr(mi._TARGET, "resolve", lambda raw: ((body, kind), None))

    def test_a_body_target_publishes_its_lump_count(self, monkeypatch, stub_slices):
        body = BRepBody(name="Tensioner")
        body.lumps = type("L", (), {"count": 2})()
        self._resolve_body(monkeypatch, body)
        out = _payload(mi.handler(target="Tensioner"))
        assert out["lump_count"] == 2

    def test_a_single_piece_body_reads_one(self, monkeypatch, stub_slices):
        body = BRepBody(name="Bracket")
        body.lumps = type("L", (), {"count": 1})()
        self._resolve_body(monkeypatch, body)
        assert _payload(mi.handler(target="Bracket"))["lump_count"] == 1

    def test_an_unreadable_lump_count_is_published_as_unknown(self, monkeypatch, stub_slices):
        # None (not 0, not 1) - the caller can tell "one solid piece" from "nobody knows".
        self._resolve_body(monkeypatch, BRepBody(name="Plain"))
        assert _payload(mi.handler(target="Plain"))["lump_count"] is None

    def test_a_non_body_target_does_not_carry_a_lump_count(self, monkeypatch, stub_slices):
        # An occurrence/component/design spans many bodies, so a single lump count would describe
        # nothing - the key belongs to a BODY row only.
        _resolve_to(monkeypatch, "occurrence")
        assert "lump_count" not in _payload(mi.handler(target="Occ:1"))


class TestNormalizeInclude:
    def test_comma_string(self):
        assert mi._normalize_include("mass") == ["mass"]

    def test_none_empty(self):
        assert mi._normalize_include(None) == [] and mi._normalize_include("") == []


# ── _measurable_geometry: getOrientedBoundingBox needs B-Rep, a Component must fall back ───────────

class _Occurrence(FakeOccurrence):
    """An occurrence whose bRepBodies are the assembly PROXY bodies a measurement falls back to."""

    def __init__(self, path, bodies, component=None, context=None):
        FakeOccurrence.__init__(self, path, component, assembly_context=context)
        self.bRepBodies = _NamedCollection(list(bodies))


def _occurrence(path, bodies, component=None, context=None):
    """An Occurrence as _measurable_geometry sees one - it holds bodies, and is not one."""
    return _Occurrence(path, bodies, component, context)


class TestMeasurableGeometry:
    def test_brep_body_passes_through(self):
        b = BRepBody(name="Plate")
        geom, note = mi._measurable_geometry(b)
        assert geom is b and note == ""

    def test_entity_without_bodies_collection_passes_through(self):
        e = SimpleNamespace(name="not a component")     # no bRepBodies -> assumed already B-Rep
        geom, note = mi._measurable_geometry(e)
        assert geom is e and note == ""

    def test_component_with_no_bodies_yields_none(self):
        comp = MakeComp(name="Empty")
        geom, _ = mi._measurable_geometry(comp)
        assert geom is None

    def test_component_single_body_falls_back_to_it_and_names_it(self):
        body = BRepBody(name="Core")
        comp = MakeComp(name="Shell", bodies=[body])
        geom, note = mi._measurable_geometry(comp)
        assert geom is body and "Core" in note

    def test_an_OCCURRENCE_falls_back_to_its_body_and_names_it(self):
        # MEASURED: getOrientedBoundingBox raises "3 : invalid argument geometry" on an Occurrence,
        # so target=<an occurrence or component name> - the shape TargetRef resolves both to - could
        # never be measured in a frame at all. Its bodies are the same fallback a Component takes.
        body = BRepBody(name="Core")
        geom, note = mi._measurable_geometry(_occurrence("Arm:1", [body]))
        assert geom is body and "Core" in note

    def test_an_OCCURRENCE_with_several_bodies_measures_the_largest_and_says_so(self):
        small = BRepBody(name="Pin", bbox=FakeBoundingBox3D(
            FakePoint(0, 0, 0), FakePoint(1, 1, 1)))
        big = BRepBody(name="Block", bbox=FakeBoundingBox3D(
            FakePoint(0, 0, 0), FakePoint(10, 2, 1)))
        geom, note = mi._measurable_geometry(_occurrence("Arm:1", [small, big]))
        assert geom is big and "largest of 2" in note

    def test_an_OCCURRENCE_holding_no_bodies_yields_none(self):
        # a sub-assembly whose own bodies are all nested one level down has nothing to measure here,
        # and the caller's refusal names the target rather than the frame.
        geom, _ = mi._measurable_geometry(_occurrence("Rig:1", []))
        assert geom is None

    def test_multi_body_component_measures_the_largest_by_aabb_volume(self):
        small = BRepBody(name="Pin", bbox=FakeBoundingBox3D(
            FakePoint(0, 0, 0), FakePoint(1, 1, 1)))            # volume 1
        big = BRepBody(name="Block", bbox=FakeBoundingBox3D(
            FakePoint(0, 0, 0), FakePoint(10, 2, 1)))           # volume 20
        comp = MakeComp(name="Rig", bodies=[small, big])
        geom, note = mi._measurable_geometry(comp)
        assert geom is big
        assert "largest of 2" in note and "Block" in note       # the fallback is flagged to the caller


# ── _joint_origin_axes: the JO frame -> bbox axes mapping ──────────────────────────────────────────

def _jo(name, tag="A"):
    return SimpleNamespace(secondaryAxisVector=f"SEC{tag}", thirdAxisVector=f"THIRD{tag}",
                           primaryAxisVector=f"PRIM{tag}", name=name)


class TestJointOriginAxes:
    """The tool's own job: map the resolved frame's axes and word an unreadable one. Which JO a
    reference names - handle, bare, qualified, ambiguous - is the JointOriginRef kind's contract,
    exercised in test_inputs.py (TestJointOriginRef / TestJointOriginRefNativeSelector)."""

    def _resolves_to(self, monkeypatch, jo, err=None):
        monkeypatch.setattr(mi._FRAME, "resolve", lambda raw: (jo, err))

    def test_frame_is_wired_to_the_NATIVE_selector(self):
        # the proxy selector returns a DIFFERENT object for a sub-component JO, so a silent switch
        # here would change which frame every oriented box is measured in.
        assert mi._FRAME.name == "frame" and mi._FRAME.native is True

    def test_axes_map_secondary_third_primary_to_xyz(self, monkeypatch):
        # X=secondaryAxisVector, Y=thirdAxisVector, Z=primaryAxisVector - swapping any of these
        # would silently measure the box in a rotated frame.
        jo = _jo("MachineFrame")
        self._resolves_to(monkeypatch, jo)
        assert mi._joint_origin_axes("MachineFrame") == (
            "SECA", "THIRDA", "PRIMA", jo, "MachineFrame", None)

    def test_the_resolved_origin_travels_out_for_the_lift(self, monkeypatch):
        # the axes come back in the JO's own PART space; expressing them in the target geometry's
        # space needs the object that knows which component's frame that is, so the JO itself has
        # to leave this leaf - re-resolving the name downstream would run the kind's refusals twice.
        jo = _jo("MachineFrame")
        self._resolves_to(monkeypatch, jo)
        assert mi._joint_origin_axes("MachineFrame")[3] is jo

    def test_the_kinds_refusal_travels_out_unchanged(self, monkeypatch):
        # one acceptor, one refusal: the tool adds no second vocabulary of its own, so whatever the
        # kind refuses (an ambiguous name, an instance form it cannot honor) reaches the caller as is.
        self._resolves_to(monkeypatch, None, "'frame': 'Center' is ambiguous - pass 'Arm:1:Center'.")
        assert mi._joint_origin_axes("Center") == (
            None, None, None, None, None, "'frame': 'Center' is ambiguous - pass 'Arm:1:Center'.")

    def test_a_blank_frame_is_refused_by_the_kind_not_resolved_to_something(self):
        # required=True on the kind: this leaf is reached only after the caller asked for a frame,
        # so a blank must refuse rather than hand back the kind's None default as a resolved JO.
        x, y, z, jo, nm, err = mi._joint_origin_axes("")
        assert (x, y, z, jo, nm) == (None, None, None, None, None)
        assert "'frame' is required" in err

    def test_a_resolved_origin_whose_axis_does_not_read_is_not_reported_as_missing(self, monkeypatch):
        # the not-found error would state a cause nothing read: the JO is there, its axis vector is
        # what came back empty, and those two failures have different remedies.
        jo = SimpleNamespace(secondaryAxisVector=None, thirdAxisVector="T",
                             primaryAxisVector="P", name="Frame")
        self._resolves_to(monkeypatch, jo)
        err = mi._joint_origin_axes("Frame")[5]
        assert "no Joint Origin named" not in err.lower()
        assert "Joint Origin 'Frame' resolved" in err and "X (secondary)" in err
        jo.secondaryAxisVector, jo.thirdAxisVector = "S", None
        assert "Y (third)" in mi._joint_origin_axes("Frame")[5]


# ── the frame= path of _bbox: oriented box in a joint-origin frame ─────────────────────────────────

class TestBboxFramePath:
    def _axes(self):
        return (FakeVector3D(1, 0, 0), FakeVector3D(0, 1, 0), FakeVector3D(0, 0, 1))

    def _stub_frame(self, monkeypatch, xv, yv, zv, token, name="PartFrame"):
        """Resolve `name` to a JO whose component shares `token` with the body's owner, so that
        body needs no lift and the axes reach getOrientedBoundingBox as read
        (TestOrientedAxesAreLiftedIntoTheGeometrySpace covers the lift). The JO's component is its
        OWN wrapper - live they never share a Python object, and reusing one here would let an
        identity compare stand in for the token compare."""
        jo = SimpleNamespace(name=name, parentComponent=_comp_wrapper("Plate", token))
        monkeypatch.setattr(mi, "_joint_origin_axes", lambda n: (xv, yv, zv, jo, name, None))
        return jo

    def test_oriented_bbox_measured_with_the_frame_axes(self, monkeypatch):
        xv, yv, zv = self._axes()
        self._stub_frame(monkeypatch, xv, yv, zv, "COMP")
        comp = _comp_wrapper("Plate", "COMP")
        obb = SimpleNamespace(length=1.0, width=2.0, height=0.5, centerPoint=FakePoint(1, 2, 3))
        seen = {}
        def gobb(geom, x, y):
            seen["args"] = (geom, x, y)
            return obb
        monkeypatch.setattr(mi, "app",
                            SimpleNamespace(measureManager=SimpleNamespace(getOrientedBoundingBox=gobb)))
        body = BRepBody(name="Plate", parent_component=comp)
        out = _payload(mi._bbox(None, body, "body 'Plate'", "PartFrame", "mm"))
        assert seen["args"] == (body, xv, yv)               # measured with the frame's X/Y axes
        # the frame field names the origin AND whose frame it is: one native JO answers for every
        # instance of its component, so a reader must not take this for the instance's own frame.
        assert out["oriented"] is True
        assert out["frame"] == ("joint origin 'PartFrame' (part space; the frame its owning "
                                "component carries)")
        assert "does not select a different one" in out["note"]
        # length=X, width=Y, height=Z, scaled cm -> mm
        assert (out["x"], out["y"], out["z"]) == (10.0, 20.0, 5.0)
        assert out["center"] == {"x": 10.0, "y": 20.0, "z": 30.0}
        assert out["frame_axes"]["z_axis"] == [0, 0, 1]

    def test_the_frame_refusal_travels_verbatim(self, monkeypatch):
        # every frame failure - unknown name, a name two Joint Origins carry, an axis that did not
        # read - reaches the caller as the refusal that names it, not a generic "no such frame".
        for refusal in ("No Joint Origin named 'Ghost'. Create one with joint_create_origin.",
                        "'Center of Model' names 2 Joint Origins - pass 'Arm:1:Center of Model'."):
            monkeypatch.setattr(mi, "_joint_origin_axes",
                                lambda n, r=refusal: (None, None, None, None, None, r))
            res = mi._bbox(None, object(), "whole design", "Ghost", "mm")
            assert res["isError"] and error_message(res) == refusal

    def test_frame_target_without_brep_body_errors(self, monkeypatch):
        # a Component with no bodies has nothing getOrientedBoundingBox accepts - refuse with a pointer.
        xv, yv, zv = self._axes()
        self._stub_frame(monkeypatch, xv, yv, zv, "C", name="F")
        monkeypatch.setattr(mi, "app", SimpleNamespace(measureManager=object()))
        comp = MakeComp(name="Empty")
        res = mi._bbox(None, comp, "component 'Empty'", "F", "mm")
        assert res["isError"] and "no B-Rep body" in error_message(res)

    def test_oriented_measure_failure_is_an_error(self, monkeypatch):
        xv, yv, zv = self._axes()
        self._stub_frame(monkeypatch, xv, yv, zv, "COMP", name="F")
        def boom(geom, x, y):
            raise RuntimeError("axes not perpendicular")
        monkeypatch.setattr(mi, "app",
                            SimpleNamespace(measureManager=SimpleNamespace(getOrientedBoundingBox=boom)))
        res = mi._bbox(None, BRepBody(name="Plate", parent_component=_comp_wrapper("Plate", "COMP")),
                       "body 'Plate'", "F", "mm")
        assert res["isError"] and "axes not perpendicular" in error_message(res)

    def test_unknown_units_errors(self):
        res = mi._bbox(None, object(), "x", "", "furlong")
        assert res["isError"] and "furlong" in error_message(res)


# ── the oriented box is measured on axes LIFTED into the target geometry's space ───────────────────

class _Axis(FakeVector3D):
    """A Joint Origin's axis vector, plus the rounded read the lifted-axis assertions compare on."""

    def rounded(self):
        return (round(self.x, 4), round(self.y, 4), round(self.z, 4))


def _rot_z(deg, tx=5.0):
    """An occurrence's transform2: a rotation of `deg` about Z, plus a translation in X that must
    never reach a DIRECTION. Live-measured on a component turned 30 deg and moved 5 cm in X - the
    lifted X axis read (0.866, 0.5, 0), not the 5 cm offset."""
    return FakeMatrix3D(deg, (tx, 0.0, 0.0))


class _RotX(FakeMatrix3D):
    """An occurrence transform2 rotating about X - the axis _rot_z does not turn about, because
    rotations about a common axis commute: a fixture built only from _rot_z cannot tell `to_world`
    then `inverse` from `inverse` then `to_world`, and that order is the heart of a two-leg lift."""

    def _apply_vector(self, x, y, z):
        r = math.radians(self._deg)
        c, s = math.cos(r), math.sin(r)
        return (x, c * y - s * z, s * y + c * z)


def _part_frame_jo(comp, name="PartFrame"):
    """A Joint Origin as its owning component carries it: axis vectors in that component's OWN
    space. MEASURED - a JO on a component turned 30 deg about Z still reads (1,0,0) for its
    secondary axis, natively and through an assembly proxy alike."""
    return SimpleNamespace(name=name, parentComponent=comp,
                           secondaryAxisVector=_Axis(1, 0, 0), thirdAxisVector=_Axis(0, 1, 0),
                           primaryAxisVector=_Axis(0, 0, 1))


def _comp_wrapper(name, token):
    """One component as a FRESH wrapper. Live, a component is never identity-stable - rootComponent
    read twice, and body.parentComponent against it, are distinct objects sharing one entityToken -
    so every reference here hands out its own object. A fixture that reused one object would make
    `a is b` and a token compare indistinguishable, and `same_component`'s whole reason to exist is
    that `a is b` is then effectively always False live."""
    return MakeComp(name=name, entity_token=token)


def _rotated_rig(monkeypatch, deg=30.0):
    """One component ROTATED `deg` about Z, carrying the measured body and the frame's Joint Origin.

    The rotation is the whole point: with an identity transform the part-space axes and the world
    axes coincide, so a fixture built that way cannot tell a lifted measurement from an unlifted one.

    The body's owner and the JO's owner are DISTINCT wrappers of that one component (see
    _comp_wrapper), so the same-component early return is pinned to the token compare.
    """
    root = _comp_wrapper("root", "ROOT")
    comp = _comp_wrapper("Slab", "SLAB")
    occ = make_occurrence(path="Slab:1", component=_comp_wrapper("Slab", "SLAB"),
                          transform2=_rot_z(deg))
    root.allOccurrencesByComponent = lambda c: [occ]
    design = make_design(comp=root)
    monkeypatch.setattr(mi._common, "design", lambda: design)
    native = BRepBody(name="Body1", parent_component=_comp_wrapper("Slab", "SLAB"))
    return SimpleNamespace(root=root, comp=comp, occ=occ,
                           jo=_part_frame_jo(_comp_wrapper("Slab", "SLAB")),
                           native=native, proxy=body_proxy(native, occ))


class TestOrientedAxesAreLiftedIntoTheGeometrySpace:
    """getOrientedBoundingBox reads its AXIS ARGUMENTS in the same space as the geometry it is
    handed, and a JointOrigin reports its axes in its owning COMPONENT's space. Feeding part-space
    axes against an assembly-PROXY body's world-space geometry measured a 40x30x10 mm slab in a
    30-deg-rotated component as 49.6 x 46.0 x 10.0 mm - 24% and 53% too large, published under a
    payload that calls them the part-space extents."""

    def _capture(self, monkeypatch):
        seen = {}
        def gobb(geom, x, y):
            seen["geom"], seen["x"], seen["y"] = geom, x, y
            return SimpleNamespace(length=4.0, width=3.0, height=1.0,
                                   centerPoint=FakePoint(0, 0, 0))
        monkeypatch.setattr(mi, "app", SimpleNamespace(
            measureManager=SimpleNamespace(getOrientedBoundingBox=gobb)))
        return seen

    def _measure(self, monkeypatch, rig, geom):
        monkeypatch.setattr(mi._FRAME, "resolve", lambda raw: (rig.jo, None))
        return _payload(mi._bbox(None, geom, "body 'Body1'", "PartFrame", "mm"))

    def test_a_proxy_body_of_a_rotated_component_is_measured_on_the_LIFTED_axes(self, monkeypatch):
        rig = _rotated_rig(monkeypatch)
        seen = self._capture(monkeypatch)
        self._measure(monkeypatch, rig, rig.proxy)
        # inputs (1,0,0)/(0,1,0) -> expectations the component's 30-deg rotation carries them to.
        assert seen["x"].rounded() == (0.866, 0.5, 0.0)
        assert seen["y"].rounded() == (-0.5, 0.866, 0.0)

    def test_the_lift_carries_no_translation(self, monkeypatch):
        # the occurrence is 5 cm out in X; a direction that picked that up would point somewhere
        # nobody measured and hand getOrientedBoundingBox a non-unit axis.
        rig = _rotated_rig(monkeypatch)
        seen = self._capture(monkeypatch)
        self._measure(monkeypatch, rig, rig.proxy)
        assert round(seen["x"].x ** 2 + seen["x"].y ** 2 + seen["x"].z ** 2, 6) == 1.0

    def test_the_published_frame_axes_stay_PART_space(self, monkeypatch):
        # 'frame' is published as "part space" and frame_axes has to agree with that label: the
        # lift belongs to the measurement, not to the payload.
        rig = _rotated_rig(monkeypatch)
        self._capture(monkeypatch)
        out = self._measure(monkeypatch, rig, rig.proxy)
        assert out["frame_axes"] == {"x_axis": [1.0, 0.0, 0.0], "y_axis": [0.0, 1.0, 0.0],
                                     "z_axis": [0.0, 0.0, 1.0]}
        assert "part space" in out["frame"]

    def test_a_NATIVE_body_of_the_frames_own_component_is_measured_unlifted(self, monkeypatch):
        # MEASURED: a native body is read in its own component's frame, so the part-space axes are
        # already the right ones - lifting here is the same defect pointing the other way (it
        # measured the same slab as 49.6 x 46.0 mm).
        rig = _rotated_rig(monkeypatch)
        seen = self._capture(monkeypatch)
        self._measure(monkeypatch, rig, rig.native)
        assert seen["x"].rounded() == (1.0, 0.0, 0.0)
        assert seen["y"].rounded() == (0.0, 1.0, 0.0)

    def test_a_NATIVE_body_of_ANOTHER_component_comes_back_out_of_world_into_its_own(self, monkeypatch):
        # LIVE-MEASURED: a world-aligned 40 x 30 mm root body read in the frame of a component
        # turned 30 deg came back 49.641 x 45.981 mm (40cos30 + 30sin30, 40sin30 + 30cos30) - the
        # frame's axes reach world through the JO's component and then back into the body's own.
        #
        # The two legs turn about DIFFERENT axes (frame about Z, body's component about X) so the
        # composition ORDER is pinned: applying the inverse first instead would read (0.866, 0.5,
        # 0) and (0, 0, -1), and rotations sharing one axis commute, which would hide it.
        rig = _rotated_rig(monkeypatch)
        plate_occ = make_occurrence(path="Plate:1", component=_comp_wrapper("Plate", "PLATE"),
                                    transform2=_RotX(90.0))
        by_comp = {"SLAB": [rig.occ], "PLATE": [plate_occ]}
        rig.root.allOccurrencesByComponent = lambda c: by_comp[c.entityToken]
        seen = self._capture(monkeypatch)
        self._measure(monkeypatch, rig,
                      BRepBody(name="PlateBody", parent_component=_comp_wrapper("Plate", "PLATE")))
        assert seen["x"].rounded() == (0.866, 0.0, -0.5)
        assert seen["y"].rounded() == (-0.5, 0.0, -0.866)

    def test_the_no_lift_return_rests_on_the_TOKEN_not_python_identity(self, monkeypatch):
        # Component wrappers are never identity-stable live ("`a is b` is effectively ALWAYS
        # FALSE" - same_component). Under an identity compare this early return never fires, the
        # body gets lifted, and a 40 x 30 slab reads 49.641 x 45.981 - this row's defect pointing
        # the other way. Here the component has NO placement to lift through, so a missed early
        # return cannot round-trip back to the right answer: it refuses instead.
        rig = _rotated_rig(monkeypatch)
        rig.root.allOccurrencesByComponent = lambda c: []
        assert rig.native.parentComponent is not rig.jo.parentComponent
        assert rig.native.parentComponent.entityToken == rig.jo.parentComponent.entityToken
        seen = self._capture(monkeypatch)
        self._measure(monkeypatch, rig, rig.native)
        assert seen["x"].rounded() == (1.0, 0.0, 0.0)
        assert seen["y"].rounded() == (0.0, 1.0, 0.0)

    def test_a_root_component_frame_leaves_a_root_body_alone(self, monkeypatch):
        # the root frame IS world: an unrotated design is exactly the case that kept this defect
        # invisible, and it must stay a no-op.
        rig = _rotated_rig(monkeypatch)
        monkeypatch.setattr(adsk.core.Matrix3D, "create", staticmethod(lambda: _rot_z(0.0, 0.0)),
                            raising=False)
        rig.jo.parentComponent = _comp_wrapper("root", "ROOT")
        root_body = BRepBody(name="RootBody", parent_component=_comp_wrapper("root", "ROOT"))
        seen = self._capture(monkeypatch)
        self._measure(monkeypatch, rig, root_body)
        assert seen["x"].rounded() == (1.0, 0.0, 0.0)

    def test_the_lift_picks_the_INSTANCE_the_target_was_reached_through(self, monkeypatch):
        # two placements of one component rotate differently; the frame the caller asked for is the
        # one on the instance holding the body, not whichever occurrence the design lists first.
        rig = _rotated_rig(monkeypatch)
        other = make_occurrence(path="Slab:2", component=rig.comp, transform2=_rot_z(90.0))
        rig.root.allOccurrencesByComponent = lambda c: [other, rig.occ]
        seen = self._capture(monkeypatch)
        self._measure(monkeypatch, rig, rig.proxy)        # reached through Slab:1, the 30-deg one
        assert seen["x"].rounded() == (0.866, 0.5, 0.0)

    def test_a_frame_on_an_ANCESTOR_occurrence_lifts_through_that_ancestor(self, monkeypatch):
        # a nested proxy's assemblyContext is the innermost occurrence; the JO's component may sit
        # further up the path, and only walking the chain finds the transform that places it.
        rig = _rotated_rig(monkeypatch)
        inner_comp = _comp_wrapper("Boss", "BOSS")
        inner = make_occurrence(path="Slab:1+Boss:1", component=inner_comp,
                                assembly_context=rig.occ, transform2=_rot_z(0.0, 0.0))
        rig.root.allOccurrencesByComponent = lambda c: []      # only the chain can answer
        deep = body_proxy(BRepBody(name="Boss1", parent_component=inner_comp), inner)
        seen = self._capture(monkeypatch)
        self._measure(monkeypatch, rig, deep)
        assert seen["x"].rounded() == (0.866, 0.5, 0.0)

    def test_a_frame_with_no_single_placement_is_REFUSED_not_measured(self, monkeypatch):
        # a component placed twice has no one orientation, and the target gives no instance to
        # scope it to. Measuring anyway would publish one instance's frame under the other's name.
        rig = _rotated_rig(monkeypatch)
        rig.root.allOccurrencesByComponent = lambda c: [
            rig.occ, make_occurrence(path="Slab:2", component=rig.comp,
                                     transform2=_rot_z(90.0))]
        other_comp = _comp_wrapper("Plate", "PLATE")
        self._capture(monkeypatch)
        monkeypatch.setattr(mi._FRAME, "resolve", lambda raw: (rig.jo, None))
        res = mi._bbox(None, BRepBody(name="Other", parent_component=other_comp),
                       "body 'Other'", "PartFrame", "mm")
        assert res["isError"]
        assert "could not be placed in the same space" in error_message(res)
        assert "PartFrame" in error_message(res)

    def test_a_TARGET_with_no_single_placement_is_refused_NAMING_THE_TARGET(self, monkeypatch):
        # the ambiguity cuts both ways: the frame resolves fine, but the body's own component is
        # placed twice, so which space the axes have to come back into is the open question. The
        # frame-blaming wording sent the caller to re-word the one argument that was not at fault.
        rig = _rotated_rig(monkeypatch)
        twins = [make_occurrence(path=f"Plate:{i}", component=_comp_wrapper("Plate", "PLATE"),
                                 transform2=_rot_z(90.0 * i, 0.0))
                 for i in (1, 2)]
        by_comp = {"SLAB": [rig.occ], "PLATE": twins}
        rig.root.allOccurrencesByComponent = lambda c: by_comp[c.entityToken]
        self._capture(monkeypatch)
        monkeypatch.setattr(mi._FRAME, "resolve", lambda raw: (rig.jo, None))
        res = mi._bbox(None, BRepBody(name="PlateBody",
                                      parent_component=_comp_wrapper("Plate", "PLATE")),
                       "body 'PlateBody'", "PartFrame", "mm")
        msg = error_message(res)
        assert res["isError"] and "body 'PlateBody'" in msg
        assert "No single placement answers" in msg
        assert "'PartFrame' placed fine" in msg
        assert "Joint Origin 'PartFrame' could not be placed" not in msg

    def test_a_FRAME_with_no_single_placement_still_names_the_FRAME(self, monkeypatch):
        # the two refusals must not converge: here it IS the frame's component that is placed twice,
        # and blaming the target would send the caller to re-word the argument that was fine.
        rig = _rotated_rig(monkeypatch)
        rig.root.allOccurrencesByComponent = lambda c: [
            rig.occ, make_occurrence(path="Slab:2", component=rig.comp,
                                     transform2=_rot_z(90.0))] if c.entityToken == "SLAB" else []
        self._capture(monkeypatch)
        monkeypatch.setattr(mi._FRAME, "resolve", lambda raw: (rig.jo, None))
        res = mi._bbox(None, BRepBody(name="Other", parent_component=_comp_wrapper("P", "PLATE")),
                       "body 'Other'", "PartFrame", "mm")
        msg = error_message(res)
        assert res["isError"] and "Joint Origin 'PartFrame' could not be placed" in msg
        assert "No single placement answers" not in msg

    def test_a_placement_that_will_not_invert_says_so_without_blaming_the_frame(self, monkeypatch):
        # a placement that WAS found but refuses to invert is a different failure from one that was
        # never found, and only the second is about how many instances there are.
        rig = _rotated_rig(monkeypatch)
        # a placement the platform declines to invert; copy() keeps the refusal, so it survives
        # the copy the lift takes before inverting
        plate_occ = make_occurrence(
            path="Plate:1", component=_comp_wrapper("Plate", "PLATE"),
            transform2=FakeMatrix3D(0.0, (0.0, 0.0, 0.0), invertible=False))
        by_comp = {"SLAB": [rig.occ], "PLATE": [plate_occ]}
        rig.root.allOccurrencesByComponent = lambda c: by_comp[c.entityToken]
        self._capture(monkeypatch)
        monkeypatch.setattr(mi._FRAME, "resolve", lambda raw: (rig.jo, None))
        res = mi._bbox(None, BRepBody(name="PlateBody",
                                      parent_component=_comp_wrapper("Plate", "PLATE")),
                       "body 'PlateBody'", "PartFrame", "mm")
        msg = error_message(res)
        assert res["isError"] and "did not invert" in msg and "body 'PlateBody'" in msg
        assert "No single placement answers" not in msg

    def test_an_axis_that_reaches_world_but_not_back_is_an_error(self, monkeypatch):
        # the two legs fail separately: an axis lifted into world and left there would be measured
        # against a native body's component-space geometry - the very mismatch this fixes.
        rig = _rotated_rig(monkeypatch)
        plate_occ = make_occurrence(path="Plate:1", component=_comp_wrapper("Plate", "PLATE"),
                                    transform2=_rot_z(90.0, 0.0))
        by_comp = {"SLAB": [rig.occ], "PLATE": [plate_occ]}
        rig.root.allOccurrencesByComponent = lambda c: by_comp[c.entityToken]

        class _OneWay(_Axis):
            """Transforms into world, then refuses the trip back."""
            def transformBy(self, m):
                if getattr(self, "_done", False):
                    return False
                self._done = True
                return FakeVector3D.transformBy(self, m)
        rig.jo.secondaryAxisVector = _OneWay(1, 0, 0)
        self._capture(monkeypatch)
        monkeypatch.setattr(mi._FRAME, "resolve", lambda raw: (rig.jo, None))
        res = mi._bbox(None, BRepBody(name="PlateBody",
                                      parent_component=_comp_wrapper("Plate", "PLATE")),
                       "body 'PlateBody'", "PartFrame", "mm")
        assert res["isError"] and "did not transform" in error_message(res)

    def test_a_root_owned_FACE_is_lifted_like_any_other_world_framed_geometry(self, monkeypatch):
        # LIVE-MEASURED, and the hole a hand-rolled `geom.parentComponent` left: a BRepFace has NO
        # parentComponent attribute (hasattr False) and a root-owned one reads assemblyContext
        # None, so it fell through to the no-lift miss and was measured in the wrong frame - the
        # +X face of a root body read 0.0 x 30.0 x 10.0 mm where the truth is 15.0 x 25.981 x 10.0.
        # find_geometry mints face handles and the tool advertises "body/face/mesh", so this is a
        # target kind on the shipped surface, not a corner.
        rig = _rotated_rig(monkeypatch)
        monkeypatch.setattr(adsk.core.Matrix3D, "create", staticmethod(lambda: _rot_z(0.0, 0.0)),
                            raising=False)
        face = BRepFace(Plane(FakeVector3D(0, 0, 1)),      # a face carries no parentComponent
                        body=BRepBody(name="RootBody",
                                      parent_component=_comp_wrapper("root", "ROOT")))
        assert not hasattr(face, "parentComponent")
        seen = self._capture(monkeypatch)
        self._measure(monkeypatch, rig, face)
        assert seen["x"].rounded() == (0.866, 0.5, 0.0)
        assert seen["y"].rounded() == (-0.5, 0.866, 0.0)

    def test_a_NATIVE_face_of_the_frames_own_component_is_still_unlifted(self, monkeypatch):
        # the owner read has to reach the face's component for BOTH answers, not just the lift.
        rig = _rotated_rig(monkeypatch)
        face = BRepFace(Plane(FakeVector3D(0, 0, 1)),
                        body=BRepBody(name="SlabBody",
                                      parent_component=_comp_wrapper("Slab", "SLAB")))
        seen = self._capture(monkeypatch)
        self._measure(monkeypatch, rig, face)
        assert seen["x"].rounded() == (1.0, 0.0, 0.0)

    def test_an_OCCURRENCE_target_is_measured_on_its_PROXY_body_in_WORLD_axes(self, monkeypatch):
        # target=<occurrence or component name> is the shape TargetRef resolves both spellings to,
        # and getOrientedBoundingBox refuses an Occurrence outright, so the natural call could never
        # be measured in a frame. Its proxy bodies read in WORLD, so the frame's axes are lifted and
        # NOT brought back - the reverse pairing measured a 40 x 30 slab as 49.6 x 46.0.
        rig = _rotated_rig(monkeypatch)
        seen = self._capture(monkeypatch)
        out = self._measure(monkeypatch, rig, _occurrence("Slab:1", [rig.proxy],
                                                          component=rig.comp))
        assert seen["geom"] is rig.proxy                       # not the Occurrence itself
        assert seen["x"].rounded() == (0.866, 0.5, 0.0)
        assert seen["y"].rounded() == (-0.5, 0.866, 0.0)
        assert "Body1" in out["target"]                        # the fallback is disclosed

    def test_a_MULTI_PLACED_occurrence_target_is_measured_not_refused(self, monkeypatch):
        # MEASURED before this: an occurrence of a twice-placed component came back "Joint Origin
        # 'PartFrame' could not be placed in the same space as the target" - blaming the frame for
        # the TARGET's ambiguity, when the occurrence names the instance and settles it. The proxy
        # body carries that instance in assemblyContext, so the placement is never ambiguous.
        rig = _rotated_rig(monkeypatch)
        rig.root.allOccurrencesByComponent = lambda c: [
            rig.occ, make_occurrence(path="Slab:2", component=rig.comp,
                                     transform2=_rot_z(90.0))]
        seen = self._capture(monkeypatch)
        out = self._measure(monkeypatch, rig, _occurrence("Slab:1", [rig.proxy],
                                                          component=rig.comp))
        assert seen["x"].rounded() == (0.866, 0.5, 0.0)        # Slab:1's 30 deg, not Slab:2's 90
        assert out["oriented"] is True

    def test_an_OCCURRENCE_holding_no_body_is_refused_NAMING_THE_TARGET(self, monkeypatch):
        # nothing to measure is the target's problem, and the refusal points at the read that lists
        # what the occurrence holds - not at the frame, which resolved.
        rig = _rotated_rig(monkeypatch)
        self._capture(monkeypatch)
        monkeypatch.setattr(mi._FRAME, "resolve", lambda raw: (rig.jo, None))
        res = mi._bbox(None, _occurrence("Rig:1", []), "occurrence 'Rig:1'", "PartFrame", "mm")
        assert res["isError"] and "occurrence 'Rig:1'" in error_message(res)
        assert "no B-Rep body" in error_message(res)

    def test_a_target_kind_nothing_recognises_keeps_the_APIs_own_refusal(self, monkeypatch):
        # an entity that is neither a body nor holds bodies goes on as-is, and the call's own
        # refusal - which names the target - is what the caller sees.
        rig = _rotated_rig(monkeypatch)
        def boom(geom, x, y):
            raise RuntimeError("3 : invalid argument geometry")
        monkeypatch.setattr(mi, "app", SimpleNamespace(
            measureManager=SimpleNamespace(getOrientedBoundingBox=boom)))
        monkeypatch.setattr(mi._FRAME, "resolve", lambda raw: (rig.jo, None))
        res = mi._bbox(None, SimpleNamespace(name="Slab:1", assemblyContext=None,
                                             component=_comp_wrapper("Slab", "SLAB")),
                       "occurrence 'Slab:1'", "PartFrame", "mm")
        assert res["isError"] and "invalid argument geometry" in error_message(res)
        assert "could not be placed" not in error_message(res)

    def test_a_target_whose_owner_no_read_reaches_keeps_the_APIs_own_refusal(self, monkeypatch):
        # nothing identified the space, so the axes go on as read and the call's own refusal - which
        # names the target - is what the caller sees.
        rig = _rotated_rig(monkeypatch)
        def boom(geom, x, y):
            raise RuntimeError("3 : invalid argument geometry")
        monkeypatch.setattr(mi, "app", SimpleNamespace(
            measureManager=SimpleNamespace(getOrientedBoundingBox=boom)))
        monkeypatch.setattr(mi._FRAME, "resolve", lambda raw: (rig.jo, None))
        res = mi._bbox(None, SimpleNamespace(assemblyContext=None), "mystery", "PartFrame", "mm")
        assert res["isError"] and "invalid argument geometry" in error_message(res)
        assert "could not be placed" not in error_message(res)

    def test_an_axis_vector_that_will_not_transform_is_an_error_not_a_measurement(self, monkeypatch):
        # a half-lifted pair would measure the box against a frame nobody read.
        rig = _rotated_rig(monkeypatch)
        class _Stuck(_Axis):
            def transformBy(self, m):
                return False
        rig.jo.thirdAxisVector = _Stuck(0, 1, 0)
        self._capture(monkeypatch)
        monkeypatch.setattr(mi._FRAME, "resolve", lambda raw: (rig.jo, None))
        res = mi._bbox(None, rig.proxy, "body 'Body1'", "PartFrame", "mm")
        assert res["isError"] and "did not transform" in error_message(res)


class TestWorldAlignedExtentsAreMeasurements:
    """The world-aligned branch publishes x/y/z/center under the SAME keys as the oriented branch,
    so it holds the SAME contract: an unreadable corner is null, never a confident 0. A 0 extent is
    an answer ("this plate is flat in Z") and a 0 centre is an answer ("it sits on the origin")."""

    class _BlindPoint(FakePoint):
        """A Point3D whose coordinate reads RAISE - a proxy that stopped answering."""
        def __init__(self):
            pass                    # x/y/z stay unset, so every read falls through below

        def __getattr__(self, name):
            if name in ("x", "y", "z"):
                raise RuntimeError("point unavailable")
            raise AttributeError(name)

    def _entity(self, monkeypatch, box):
        monkeypatch.setattr(mi._geom, "body_aabb", lambda ent: box)
        return object()

    def test_readable_corners_give_the_extents_and_the_centre(self, monkeypatch):
        box = FakeBoundingBox3D(FakePoint(0, 0, 0), FakePoint(1, 2, 3))
        ent = self._entity(monkeypatch, box)
        out = _payload(mi._bbox(None, ent, "body 'Plate'", "", "mm"))
        assert (out["x"], out["y"], out["z"]) == (10.0, 20.0, 30.0)
        assert out["center"] == {"x": 5.0, "y": 10.0, "z": 15.0}
        assert out["oriented"] is False

    def test_an_unreadable_corner_reads_null_not_zero(self, monkeypatch):
        box = FakeBoundingBox3D(FakePoint(0, 0, 0), self._BlindPoint())
        ent = self._entity(monkeypatch, box)
        out = _payload(mi._bbox(None, ent, "body 'Plate'", "", "mm"))
        assert (out["x"], out["y"], out["z"]) == (None, None, None)
        assert out["center"] == {"x": None, "y": None, "z": None}

    def test_the_published_corner_points_hold_the_same_contract(self, monkeypatch):
        # min_point/max_point go through the shared whole-point read: the unreadable corner is
        # null, not the world origin - a caller navigating to {0,0,0} would go to the wrong place.
        box = FakeBoundingBox3D(FakePoint(0, 0, 0), self._BlindPoint())
        ent = self._entity(monkeypatch, box)
        out = _payload(mi._bbox(None, ent, "body 'Plate'", "", "mm"))
        assert out["max_point"] is None
        assert out["min_point"] == {"x": 0.0, "y": 0.0, "z": 0.0}   # a READ origin is an answer

    def test_a_genuinely_flat_axis_still_reports_zero(self, monkeypatch):
        # the null must mean UNREADABLE and nothing else: a real zero extent is still a 0
        box = FakeBoundingBox3D(FakePoint(0, 0, 5), FakePoint(1, 2, 5))
        ent = self._entity(monkeypatch, box)
        out = _payload(mi._bbox(None, ent, "body 'Shim'", "", "mm"))
        assert out["z"] == 0.0 and out["center"]["z"] == 50.0


# ── _full_props / _physical_properties: unit scaling + guards + per-occurrence breakdown ───────────

def _make_pp(**over):
    """A PhysicalProperties fake with concrete cm-based values (the API reports cm)."""
    pp = SimpleNamespace(mass=2.0, volume=4.0, area=6.0, density=0.0078,
                         centerOfMass=FakePoint(1.0, 2.0, 3.0), accuracy=None)
    pp.getXYZMomentsOfInertia = lambda: (True, 1.0, 2.0, 3.0, 0.4, 0.5, 0.6)
    pp.getPrincipalMomentsOfInertia = lambda: (True, 5.0, 6.0, 7.0)
    pp.getPrincipalAxes = lambda: (True, FakeVector3D(1, 0, 0),
                                   FakeVector3D(0, 1, 0), FakeVector3D(0, 0, 1))
    pp.getRadiusOfGyration = lambda: (True, 0.5, 0.6, 0.7)
    pp.getRotationToPrincipal = lambda: (True, 0.1, 0.2, 0.3)
    for k, v in over.items():
        setattr(pp, k, v)
    return pp


_DEFAULT_PP = object()      # "build a default PhysicalProperties", distinct from an explicit None


def _occ_row(path, mass=1.0, children=(), pp=_DEFAULT_PP):
    """One occurrence as the per_body walk reads it: a full path, physical properties (pp=None for
    an unmeasurable one), and its own child occurrences - the collection that says whether its mass
    already aggregates anything."""
    props = (SimpleNamespace(mass=mass, centerOfMass=FakePoint(0.0, 0.0, 0.0))
             if pp is _DEFAULT_PP else pp)
    occ = make_occurrence(path=path, children=list(children))
    occ.getPhysicalProperties = lambda acc: props
    return occ


class TestFullProps:
    def test_mm_scaling_per_quantity(self):
        # k = cm-per-mm = 0.1: lengths x10, areas x100, volumes x1000, inertia x100; mass (kg) and
        # rotation angles never rescale. A single wrong exponent here misreports every mass read.
        out = mi._full_props(_make_pp(), mi._common.scale("mm"))
        assert out["mass_kg"] == 2.0
        assert out["volume"] == 4000.0 and out["area"] == 600.0
        assert out["center_of_mass"] == [10.0, 20.0, 30.0]
        assert out["inertia_world"]["Ixx"] == 100.0 and out["inertia_world"]["Ixz"] == 60.0
        assert out["principal_moments"]["i1"] == 500.0
        assert out["radius_of_gyration"]["kx"] == 5.0
        assert out["rotation_to_principal_rad"]["rx"] == 0.1
        assert out["principal_axes"]["z"] == [0, 0, 1]

    def test_nonfinite_physical_values_are_null_and_strict_json(self):
        pp = _make_pp()
        pp.getXYZMomentsOfInertia = lambda: (True, float("nan"), 0.0, float("inf"), 0.0, 0.0, 0.0)
        out = mi._full_props(pp, 0.1)
        assert out["inertia_world"]["Ixx"] is None
        assert out["inertia_world"]["Iyy"] == 0.0
        assert out["inertia_world"]["Izz"] is None
        json.dumps(out, allow_nan=False)

    def test_failed_sub_reads_omit_their_blocks(self):
        # each get*() returns (retVal, ...) - a False retVal means the read failed, so its block is
        # omitted rather than reporting zeros as if measured.
        pp = _make_pp()
        pp.getXYZMomentsOfInertia = lambda: (False, 0, 0, 0, 0, 0, 0)
        pp.getPrincipalMomentsOfInertia = lambda: None
        out = mi._full_props(pp, 0.1)
        assert "inertia_world" not in out and "principal_moments" not in out


class TestPhysicalProperties:
    def test_unknown_units_errors(self):
        res = mi._physical_properties(None, object(), "x", "parsec", "medium", False)
        assert res["isError"] and "parsec" in error_message(res)

    def test_unknown_accuracy_errors(self):
        res = mi._physical_properties(None, object(), "x", "mm", "extreme", False)
        assert res["isError"] and "extreme" in error_message(res)

    def test_no_measurable_solid_errors_naming_the_target(self):
        e = SimpleNamespace(getPhysicalProperties=lambda acc: None)
        res = mi._physical_properties(None, e, "body 'Shell'", "mm", "medium", False)
        assert res["isError"] and "Shell" in error_message(res)

    def test_reports_mass_and_the_accuracy_actually_used(self):
        # accuracy_used is read BACK off the result (the API may compute at a different accuracy
        # than requested) - echoing the request instead would hide that.
        pp = _make_pp(accuracy=mi._ACCURACY["high"])
        e = SimpleNamespace(getPhysicalProperties=lambda acc: pp)
        out = _payload(mi._physical_properties(None, e, "body 'Plate'", "mm", "medium", False))
        assert out["mass_kg"] == 2.0 and out["accuracy"] == "medium"
        assert out["accuracy_used"] == "high"
        assert "per_occurrence" not in out                  # opt-in via per_body

    def test_per_body_breakdown_skips_unmeasurable_occurrences(self):
        opp = SimpleNamespace(mass=1.25, centerOfMass=FakePoint(0.1, 0.0, 0.0))
        o1 = _occ_row("A:1", pp=opp)
        o2 = _occ_row("B:1", pp=None)                      # surface-only: no physical properties
        e = SimpleNamespace(getPhysicalProperties=lambda acc: _make_pp(),
                            allOccurrences=_NamedCollection([o1, o2]))
        out = _payload(mi._physical_properties(None, e, "whole design", "mm", "medium", True))
        assert out["per_occurrence_count"] == 1
        # a LEAF row keeps the plain shape - no aggregates_children key to reason about
        assert out["per_occurrence"] == [{"occurrence": "A:1", "mass_kg": 1.25,
                                          "center_of_mass": [1.0, 0.0, 0.0]}]


class TestPerOccurrenceReachesEveryDepth:
    """per_body must name EVERY occurrence in the target's subtree. The direct-children collection
    stops one level down, so a body owned by a nested sub-component gets no row of its own and is
    silently folded into its parent's mass - a breakdown that is short by exactly the deep parts."""

    def _design_with_grandchild(self):
        grand = _occ_row("Frame:1+Motor:1", mass=0.5)
        child = _occ_row("Frame:1", mass=2.0, children=[grand])
        # The root exposes BOTH collections, as live: 'occurrences' is the direct children only,
        # 'allOccurrences' the flattened subtree. Walking the former misses the grandchild.
        return SimpleNamespace(getPhysicalProperties=lambda acc: _make_pp(),
                               occurrences=_NamedCollection([child]),
                               allOccurrences=_NamedCollection([child, grand]))

    def _rows(self, entity, target="whole design"):
        out = _payload(mi._physical_properties(None, entity, target, "mm", "medium", True))
        return out, out["per_occurrence"]

    def test_a_grandchild_occurrence_gets_its_own_row(self):
        out, rows = self._rows(self._design_with_grandchild())
        assert [r["occurrence"] for r in rows] == ["Frame:1", "Frame:1+Motor:1"]
        assert out["per_occurrence_count"] == 2
        assert rows[1]["mass_kg"] == 0.5

    def test_rows_are_keyed_by_full_path_not_bare_name(self):
        # two 'Motor:1' under different parents are different parts; the bare name collapses them
        a = _occ_row("Left:1+Motor:1", mass=0.5)
        b = _occ_row("Right:1+Motor:1", mass=0.5)
        e = SimpleNamespace(getPhysicalProperties=lambda acc: _make_pp(),
                            allOccurrences=_NamedCollection([a, b]))
        _, rows = self._rows(e)
        assert [r["occurrence"] for r in rows] == ["Left:1+Motor:1", "Right:1+Motor:1"]

    def test_a_row_with_children_says_it_aggregates_them(self):
        # the parent's mass ALREADY contains the grandchild's row, so summing the rows double-counts
        _, rows = self._rows(self._design_with_grandchild())
        assert rows[0]["aggregates_children"] is True
        assert "aggregates_children" not in rows[1]

    def test_an_unreadable_child_count_publishes_null_not_leaf(self):
        # null says "unknown", which is the only honest answer; a missing key would claim leaf and
        # invite the caller to sum a row that may already include others.
        class _Deaf(FakeOccurrence):
            """An occurrence whose child collection answers no count."""
            @property
            def childOccurrences(self):
                return SimpleNamespace()

        deaf = _Deaf("Frame:1")
        deaf.getPhysicalProperties = lambda acc: SimpleNamespace(
            mass=1.0, centerOfMass=FakePoint(0.0, 0.0, 0.0))
        e = SimpleNamespace(getPhysicalProperties=lambda acc: _make_pp(),
                            allOccurrences=_NamedCollection([deaf]))
        _, rows = self._rows(e)
        assert rows[0]["aggregates_children"] is None

    def test_an_occurrence_target_walks_its_own_subtree(self):
        # an Occurrence carries no flattened allOccurrences - only childOccurrences - so a
        # sub-assembly target still breaks down to its deepest parts.
        grand = _occ_row("Frame:1+Motor:1+Shaft:1", mass=0.25)
        child = _occ_row("Frame:1+Motor:1", mass=0.5, children=[grand])
        occ = SimpleNamespace(getPhysicalProperties=lambda acc: _make_pp(),
                              childOccurrences=_NamedCollection([child]))
        _, rows = self._rows(occ, "occurrence 'Frame:1'")
        assert [r["occurrence"] for r in rows] == ["Frame:1+Motor:1", "Frame:1+Motor:1+Shaft:1"]

    def test_a_target_with_no_occurrences_reports_an_empty_breakdown(self):
        e = SimpleNamespace(getPhysicalProperties=lambda acc: _make_pp())   # a lone body
        out, rows = self._rows(e, "body 'Plate'")
        assert rows == [] and out["per_occurrence_count"] == 0
        assert out["per_occurrence_truncated"] is False

    def test_the_row_list_is_capped_and_says_so(self, monkeypatch):
        monkeypatch.setattr(mi, "_MAX_PER_OCCURRENCE_ROWS", 3)
        many = [_occ_row(f"P{i}:1") for i in range(5)]
        e = SimpleNamespace(getPhysicalProperties=lambda acc: _make_pp(),
                            allOccurrences=_NamedCollection(many))
        out, rows = self._rows(e)
        assert len(rows) == 3 and out["per_occurrence_count"] == 3
        assert out["per_occurrence_truncated"] is True
        assert "per_occurrence_truncated" in out["note"]

    def test_a_list_inside_the_cap_is_not_flagged_truncated(self, monkeypatch):
        monkeypatch.setattr(mi, "_MAX_PER_OCCURRENCE_ROWS", 3)
        e = SimpleNamespace(getPhysicalProperties=lambda acc: _make_pp(),
                            allOccurrences=_NamedCollection([_occ_row(f"P{i}:1") for i in range(3)]))
        out, rows = self._rows(e)
        assert len(rows) == 3 and out["per_occurrence_truncated"] is False

    def test_the_note_discloses_that_parent_rows_aggregate(self):
        out, _ = self._rows(self._design_with_grandchild())
        assert "aggregates_children" in out["note"] and "double-count" in out["note"]

    def test_without_per_body_no_breakdown_and_no_extra_note(self):
        out = _payload(mi._physical_properties(None, self._design_with_grandchild(),
                                               "whole design", "mm", "medium", False))
        assert "per_occurrence" not in out and "per_occurrence_truncated" not in out
        assert "per_body" not in out and "per_body_truncated" not in out
        assert "aggregates_children" not in out["note"]


class TestPerBodyRows:
    """One row per solid BODY beside the occurrence rows. An occurrence row covers a whole
    component, so a part built as several bodies is one row there - and a LEAF occurrence, having no
    subtree, earns no occurrence row at all and would otherwise report an empty breakdown."""

    def _body(self, name, mass=1.0, volume=2.0, computes=True, is_solid=True):
        b = BRepBody(name=name, volume=volume, is_solid=is_solid)
        props = SimpleNamespace(mass=mass, volume=volume, centerOfMass=FakePoint(0.0, 0.0, 0.0))
        b.getPhysicalProperties = lambda acc, p=(props if computes else None): p
        return b

    def _rows(self, entity, target="whole design"):
        out = _payload(mi._physical_properties(None, entity, target, "mm", "medium", True))
        return out, out["per_body"]

    def test_a_leaf_occurrence_holding_three_bodies_gets_three_rows(self):
        occ = SimpleNamespace(getPhysicalProperties=lambda acc: _make_pp(),
                              bRepBodies=_NamedCollection([self._body("Pin1", mass=0.1),
                                                           self._body("Pin2", mass=0.2),
                                                           self._body("Web", mass=0.7)]))
        out, rows = self._rows(occ, "occurrence 'Carrier:1'")
        assert out["per_occurrence"] == []            # no subtree, so nothing to break down there
        assert [r["body"] for r in rows] == ["Pin1", "Pin2", "Web"]
        assert out["per_body_count"] == 3
        assert rows[0]["mass_kg"] == 0.1
        assert rows[0]["volume"] == 2000.0            # cm3 -> mm3
        assert all(r["occurrence"] is None for r in rows)

    def test_a_subtree_occurrences_bodies_are_named_by_its_path(self):
        child = _occ_row("Frame:1+Motor:1", mass=0.5)
        child.bRepBodies = _NamedCollection([self._body("Rotor")])
        e = SimpleNamespace(getPhysicalProperties=lambda acc: _make_pp(),
                            bRepBodies=_NamedCollection([self._body("Plate")]),
                            allOccurrences=_NamedCollection([child]))
        _out, rows = self._rows(e)
        assert [(r["body"], r["occurrence"]) for r in rows] == [
            ("Plate", None), ("Rotor", "Frame:1+Motor:1")]

    def test_an_open_surface_body_is_a_row_flagged_not_solid(self):
        # the walk is bRepBodies, which carries open surfaces too - the flag is what tells a surface
        # body's empty mass from a solid whose properties would not compute.
        e = SimpleNamespace(getPhysicalProperties=lambda acc: _make_pp(),
                            bRepBodies=_NamedCollection([self._body("Plate"),
                                                         self._body("Skin", is_solid=False)]))
        _out, rows = self._rows(e)
        assert [(r["body"], r["is_solid"]) for r in rows] == [("Plate", True), ("Skin", False)]

    def test_a_body_whose_properties_do_not_compute_still_gets_its_row(self):
        e = SimpleNamespace(getPhysicalProperties=lambda acc: _make_pp(),
                            bRepBodies=_NamedCollection([self._body("Ghost", computes=False)]))
        _out, rows = self._rows(e)
        assert rows[0]["body"] == "Ghost"
        assert rows[0]["mass_kg"] is None and rows[0]["volume"] is None

    def test_the_body_rows_are_capped_and_say_so(self, monkeypatch):
        monkeypatch.setattr(mi, "_MAX_PER_BODY_ROWS", 2)
        e = SimpleNamespace(getPhysicalProperties=lambda acc: _make_pp(),
                            bRepBodies=_NamedCollection([self._body(f"B{i}") for i in range(4)]))
        out, rows = self._rows(e)
        assert len(rows) == 2 and out["per_body_count"] == 2
        assert out["per_body_truncated"] is True and "per_body_truncated" in out["note"]

    def test_exactly_the_cap_is_not_flagged_truncated(self, monkeypatch):
        monkeypatch.setattr(mi, "_MAX_PER_BODY_ROWS", 2)
        e = SimpleNamespace(getPhysicalProperties=lambda acc: _make_pp(),
                            bRepBodies=_NamedCollection([self._body(f"B{i}") for i in range(2)]))
        out, rows = self._rows(e)
        assert len(rows) == 2 and out["per_body_truncated"] is False


def _walkable_occ(path):
    """An occurrence the shared census can classify and descend: `component` READS (a real
    Occurrence always answers it - one that raises is an unresolved external reference) and both
    child collections are empty."""
    return make_occurrence(path=path, component=MakeComp(name=path.split(":")[0]))


class _RaisingSubtree(MakeComp):
    """A COMPONENT whose allOccurrences RAISES - one unresolved reference anywhere in the subtree
    takes the whole flattened walk out - and which has no childOccurrences at all, as a Component
    does not. Its own `occurrences` collection is what the census rebuilds from."""
    def __init__(self, kids):
        MakeComp.__init__(self, name="Sub", occurrences=kids)

    @property
    def allOccurrences(self):
        raise RuntimeError("2 : InternalValidationError : occ")

    @allOccurrences.setter
    def allOccurrences(self, value):
        pass                    # the base class assigns it; the read above is what this models


class TestSubtreeOccurrences:
    def test_a_deep_occurrence_chain_is_walked_to_the_bottom(self):
        deep = _occ_row("A:1+B:1+C:1")
        mid = _occ_row("A:1+B:1", children=[deep])
        top = _occ_row("A:1", children=[mid])
        entity = make_occurrence(path="A:0", children=[top])
        assert [o.fullPathName for o in mi._subtree_occurrences(entity, 10)] == [
            "A:1", "A:1+B:1", "A:1+B:1+C:1"]

    def test_the_walk_stops_one_past_the_limit(self):
        # one extra item is what lets the caller flag truncation without counting a total it
        # never walked; an unbounded walk would enumerate a whole assembly to publish 200 rows.
        chain = [_occ_row(f"A{i}:1") for i in range(10)]
        entity = make_occurrence(path="Rig:1", children=chain)
        assert len(mi._subtree_occurrences(entity, 4)) == 5

    def test_an_entity_with_neither_collection_walks_nothing(self):
        assert mi._subtree_occurrences(SimpleNamespace(), 10) == []

    def test_a_COMPONENT_whose_flattened_walk_RAISES_is_rebuilt_from_its_own_occurrences(self):
        # allOccurrences RAISES on a component whose subtree holds an unresolved external reference,
        # and a Component carries no childOccurrences to fall back to - so a swallowed raise reported
        # the component as holding NOTHING, and every per-occurrence measurement went missing.
        kids = [_walkable_occ(f"P{i}:1") for i in range(3)]
        assert [o.fullPathName for o in mi._subtree_occurrences(_RaisingSubtree(kids), 10)] == [
            "P0:1", "P1:1", "P2:1"]

    def test_the_rebuilt_subtree_still_stops_one_past_the_limit(self):
        # the cap is the caller's truncation evidence and must survive the fallback path too.
        kids = [_walkable_occ(f"P{i}:1") for i in range(10)]
        assert len(mi._subtree_occurrences(_RaisingSubtree(kids), 4)) == 5


class TestRouterErrorPropagation:
    def test_mass_slice_error_fails_the_read(self, monkeypatch, stub_slices):
        _resolve_to(monkeypatch, "body")
        monkeypatch.setattr(mi, "_physical_properties",
                            lambda *a: mi.error("no measurable solid"))
        res = mi.handler(target="Body1", include=["mass"])
        assert res["isError"] and "no measurable solid" in error_message(res)

    def test_bbox_error_fails_the_read(self, monkeypatch, stub_slices):
        _resolve_to(monkeypatch, "body")
        monkeypatch.setattr(mi, "_bbox", lambda *a: mi.error("no bounding box"))
        res = mi.handler(target="Body1")
        assert res["isError"] and "no bounding box" in error_message(res)


class TestVecHelpers:
    def test_none_vectors_stay_none(self):
        assert mi._vec(None) is None

    def test_vec_scales_components(self):
        assert mi._vec(FakeVector3D(1.0, 2.0, 3.0), 10.0) == [10.0, 20.0, 30.0]

    def test_a_component_that_will_not_read_makes_the_whole_vector_null(self):
        # a 0.0 stand-in for one component publishes a DIFFERENT direction/position as if measured
        # (a CoM at [1, 2, 0], a frame axis pointing somewhere nobody read).
        class _Blind(FakeVector3D):
            def __init__(self):
                self.x, self.y = 1.0, 2.0

            @property
            def z(self):
                raise RuntimeError("vector component unavailable")
        assert mi._vec(_Blind(), 10.0) is None

    def test_a_real_zero_component_still_reads_zero(self):
        # the null must mean UNREADABLE only: an axis-aligned vector's other components are 0.
        assert mi._vec(FakeVector3D(0.0, 0.0, 1.0), 10.0) == [0.0, 0.0, 10.0]
