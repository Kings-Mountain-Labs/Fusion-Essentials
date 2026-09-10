"""Unit tests for ``joint_create_origin.py``.

Targets: ``_kp_name`` (reverse keypoint-enum -> readable name), ``_vec``
(rounding/None), the input-validation branches of ``_geometry_from_args``
(missing ``sketch_name``, out-of-range entity index) that gate geometry
construction before any Fusion call, and ``handler()`` itself - its own
guards (unknown anchor/target/keypoint/units, no active design), the
coordinate-anchor unit scaling, and the created-origin report (frame_axes,
name override).
"""

from types import SimpleNamespace

import adsk.core
import adsk.fusion
import pytest

from conftest import (BRepBody, BRepEdge, BRepFace, Cylinder, FakeInfiniteLine3D, FakeJointOrigin,
                      FakeJointOriginInput, FakeMatrix3D, FakePoint, FakeVector3D, Line3D,
                      MakeComp, MeshBody, Plane, Sketch, _NamedCollection, _Vertex, install,
                      load_tool, make_bbox, make_design, make_occurrence, payload as _payload)

jo = load_tool("joint_create_origin")


# ── _vec: round to 6, None passthrough ─────────────────────────────────────

class TestVec:
    def test_rounds_components(self):
        assert jo._vec(SimpleNamespace(x=1.0000001, y=2.5, z=-3.0)) == [1.0, 2.5, -3.0]

    def test_none_passthrough(self):
        assert jo._vec(None) is None


# ── _kp_name: reverse lookup against the keypoint table ────────────────────

class TestKpName:
    def test_known_value_maps_to_name(self):
        # Pick any registered keypoint and round-trip it through the reverse map.
        name, value = next(iter(jo._KEYPOINTS.items()))
        assert jo._kp_name(value) == name

    def test_unknown_value_falls_back_to_str(self):
        assert jo._kp_name(123456) == "123456"


# ── _geometry_from_args: validation gates ──────────────────────────────────

def _call(anchor="coordinates", target="at", x=0, y=0, z=0,
          sketch_name="", entity_index=0, keypoint=None, design=None, comp=None,
          geometry_handle=None, bbox_target=None, orient_axis="z", flip=False, meta=None,
          sketch_component=""):
    # Signature: (design, comp, anchor, target, x_cm, y_cm, z_cm,
    #             sketch_name, entity_index, keypoint, geometry_handle,
    #             bbox_target, orient_axis, flip, meta, sketch_component)
    return jo._geometry_from_args(
        design or SimpleNamespace(), comp or SimpleNamespace(),
        anchor, target, x, y, z, sketch_name, entity_index, keypoint, geometry_handle,
        bbox_target, orient_axis, flip, meta, sketch_component,
    )


def _stub_scoped_sketch(monkeypatch, answer):
    """Point the by-name sketch resolve at a fixed (sketch, refusal) answer. It is reached through
    _sketch_detail, the home of the 'sketch_component' scope this anchor narrows by."""
    monkeypatch.setattr(jo._sketch_detail, "scoped_sketch",
                        lambda design, name, component, input_name="component": answer)


class TestGeometryFromArgsValidation:
    def test_sketch_line_without_sketch_name_errors(self):
        g, desc, err = _call(anchor="sketch_line", sketch_name="")
        assert g is None
        assert "sketch_name" in err

    def test_sketch_point_without_sketch_name_errors(self):
        g, desc, err = _call(anchor="sketch_point", sketch_name="")
        assert g is None
        assert "sketch_name" in err

    def test_sketch_line_missing_sketch_errors(self, monkeypatch):
        # sketch_name given, but no such sketch exists -> clear "no sketch named" error.
        _stub_scoped_sketch(monkeypatch, (None, None))
        g, desc, err = _call(anchor="sketch_line", sketch_name="Ghost")
        assert g is None
        assert "Ghost" in err

    def test_a_shared_sketch_name_is_refused_with_its_owners(self, monkeypatch):
        # Several sketches carrying the name is a REFUSAL naming each owning component - calling
        # that "No sketch named 'Shared'" would state the opposite of what the walk read.
        refusal = "2 sketches are named 'Shared' ('Shared' in Root, 'Shared' in Frame)"
        _stub_scoped_sketch(monkeypatch, (None, refusal))
        g, desc, err = _call(anchor="sketch_line", sketch_name="Shared")
        assert g is None
        assert err == refusal and "No sketch named" not in err

    def test_the_sketch_scope_is_declared_on_the_wire_beside_component(self):
        # the schema is strict, so a handler parameter no property declares is unreachable. Both
        # scopes are declared, and each keeps its own meaning.
        sd = load_tool("_sketch_detail")
        props = jo.tool.input_schema["properties"]
        assert props["sketch_component"] == sd.component_scope("sketch_component",
                                                               narrows="sketch_name")[1]
        # this tool carries a SECOND, differently-scoped component input, so the description has to
        # name the reference this one narrows rather than the family's generic wording
        assert "'sketch_name'" in props["sketch_component"]["description"]
        assert props["sketch_component"] != props["component"]

    def test_the_sketch_scope_is_its_own_input_not_the_receiving_component(self, monkeypatch):
        # 'component' names the occurrence RECEIVING the joint origin; the sketch the JO anchors on
        # is narrowed by 'sketch_component'. Borrowing one for the other would resolve the anchor
        # against the wrong component whenever the two differ.
        seen = {}

        def _scoped(design, name, component, input_name="component"):
            seen["component"] = component
            seen["input_name"] = input_name
            return None, None

        monkeypatch.setattr(jo._sketch_detail, "scoped_sketch", _scoped)
        _call(anchor="sketch_line", sketch_name="S", sketch_component="Frame")
        assert seen == {"component": "Frame", "input_name": "sketch_component"}

    def test_sketch_line_index_out_of_range_errors(self, monkeypatch):
        # A sketch exists with 1 line; asking for index 5 must be rejected.
        one_line = SimpleNamespace(
            sketchCurves=SimpleNamespace(
                sketchLines=SimpleNamespace(count=1)
            )
        )
        _stub_scoped_sketch(monkeypatch, (one_line, None))
        g, desc, err = _call(anchor="sketch_line", sketch_name="S", entity_index=5)
        assert g is None
        assert "out of range" in err


# ── anchor='geometry': BRep face/edge/vertex handle (geometry-as-values) ────────────────────────

def _face(planar):
    """A face on conftest's shared BRepFace fake - the shared surface fakes hold the measured
    surfaceType intrinsically, so a face's kind is named once here."""
    axis = FakeVector3D(0.0, 0.0, 1.0)
    return BRepFace(Plane(axis) if planar else Cylinder(axis))


@pytest.fixture
def install_geom(monkeypatch):
    """Point the adsk entity types at conftest's shared fakes and install a design resolving the
    geometry handles; the returned recorder names which JointGeometry factory ran."""
    def _wire(handle_map):
        calls = {}

        class JG:
            @staticmethod
            def createByPlanarFace(face, edge, kp):
                calls["kind"] = "planar_face"; return ("g", "planar")
            @staticmethod
            def createByNonPlanarFace(face, kp):
                calls["kind"] = "non_planar_face"; return ("g", "nonplanar")
            @staticmethod
            def createByCurve(edge, kp):
                calls["kind"] = "curve"; return ("g", "curve")
            @staticmethod
            def createByPoint(v):
                calls["kind"] = "point"; return ("g", "point")

        monkeypatch.setattr(adsk.fusion, "JointGeometry", JG)
        monkeypatch.setattr(adsk.fusion, "BRepFace", BRepFace)
        monkeypatch.setattr(adsk.fusion, "BRepEdge", BRepEdge)
        monkeypatch.setattr(adsk.fusion, "BRepVertex", _Vertex)
        monkeypatch.setattr(adsk.fusion, "ConstructionPoint", type("CP", (), {}))
        monkeypatch.setattr(adsk.fusion, "SketchPoint", type("SP", (), {}))
        install(jo, make_design(tokens=handle_map))
        return calls
    return _wire


class TestGeometryAnchor:
    def test_planar_face_uses_createByPlanarFace(self, install_geom):
        calls = install_geom({"F": _face(planar=True)})
        g, desc, err = _call(anchor="geometry", geometry_handle="F")
        assert err is None and g is not None
        assert calls["kind"] == "planar_face"
        assert "normal" in desc

    def test_cylinder_face_uses_nonplanar(self, install_geom):
        calls = install_geom({"C": _face(planar=False)})
        g, desc, err = _call(anchor="geometry", geometry_handle="C")
        assert err is None and calls["kind"] == "non_planar_face"

    def test_edge_uses_createByCurve(self, install_geom):
        calls = install_geom({"E": BRepEdge(Line3D())})
        g, desc, err = _call(anchor="geometry", geometry_handle="E", keypoint=1)
        assert err is None and calls["kind"] == "curve"
        assert "edge" in desc.lower()

    def test_vertex_uses_createByPoint(self, install_geom):
        calls = install_geom({"V": _Vertex(None)})
        g, desc, err = _call(anchor="geometry", geometry_handle="V")
        assert err is None and calls["kind"] == "point"

    def test_bad_geometry_handle_errors(self, install_geom):
        install_geom({})   # nothing resolves
        g, desc, err = _call(anchor="geometry", geometry_handle="missing")
        assert g is None and err is not None


# ── handler(): guards, coordinate-anchor scaling, and the created-origin report ─────────────────

class _FakeJointOriginInput(FakeJointOriginInput):
    """The shared input carrying this file's frame - Z primary, X secondary."""
    def __init__(self):
        super().__init__(primary=SimpleNamespace(x=0.0, y=0.0, z=1.0),
                         secondary=SimpleNamespace(x=1.0, y=0.0, z=0.0),
                         third=SimpleNamespace(x=0.0, y=1.0, z=0.0))


class _FakeJointOrigin(FakeJointOrigin):
    """The shared origin with its offsetX/Y/Z ModelParameters at zero - each carries .value in cm
    plus a dNN .name, and add() repopulates them from the input."""
    def __init__(self):
        super().__init__(offsets={"offsetX": SimpleNamespace(value=0.0, name="d5"),
                                  "offsetY": SimpleNamespace(value=0.0, name="d6"),
                                  "offsetZ": SimpleNamespace(value=0.0, name="d7")})


class _FakeJointOrigins(_NamedCollection):
    """comp.jointOrigins: the shared walk plus createInput/add - the create seam."""
    def __init__(self, owner=None):
        super().__init__()
        self.owner = owner          # the component this collection hangs off

    def createInput(self, geom):
        return _FakeJointOriginInput()

    def add(self, jo_input):
        origin = _FakeJointOrigin()
        self._items.append(origin)
        # A created JO belongs to the component owning the collection it was added to: sub-component
        # jointOrigins.add lands a JO whose parentComponent IS that sub-component.
        origin.parentComponent = self.owner
        # Model the offset parameters: the created JO reports back whatever offsets the input carried
        # (createByReal wraps the cm value as ._real). This is what the honesty read-back verifies.
        for ax, dnn in (("offsetX", "d5"), ("offsetY", "d6"), ("offsetZ", "d7")):
            vi = getattr(jo_input, ax, None)
            if vi is not None:
                setattr(origin, ax, SimpleNamespace(value=getattr(vi, "_real", 0.0), name=dnn))
        return origin


def _comp(name="Comp1", token=None):
    """A component on conftest's shared MakeComp, carrying the jointOrigins collection a create
    lands in. entityToken, because _common.same_component compares on it: the landed-component
    read-back and the active-component disclosure both refuse to claim anything about a pair they
    cannot identify. A test that wants that state deletes the attribute."""
    comp = MakeComp(name=name, entity_token=token if token is not None else f"TOKEN:{name}",
                    origin_construction_point=object(),   # the anchor for anchor=coordinates
                    origin_planes=(object(), object(), object()))
    comp.jointOrigins = _FakeJointOrigins(comp)
    return comp


def _design(occurrences=(), comp=None):
    """A design on conftest's shared MakeDesign whose root holds `occurrences` - the assembly walk
    an occurrence reference resolves through."""
    root = comp if comp is not None else _comp()
    root.allOccurrences = list(occurrences)
    return make_design(comp=root)


def _install_handler(monkeypatch, design=None):
    """Wire a fake design + the adsk seams _geometry_from_args/handler touch for anchor='coordinates'.
    Returns (design, point3d_calls) so a test can assert the exact cm values Point3D.create received.
    """
    # install() patches BOTH design seams: the handler's own _common, and _inputs' _common, which
    # OccurrenceRef resolves 'component' through.
    d = design if design is not None else _design()
    install(jo, d)
    calls = []

    def _create(x, y, z):
        calls.append((x, y, z))
        return SimpleNamespace(x=x, y=y, z=z)

    monkeypatch.setattr(adsk.core.Point3D, "create", staticmethod(_create))
    monkeypatch.setattr(adsk.fusion.JointGeometry, "createByPoint",
                        staticmethod(lambda pt: SimpleNamespace(anchor_point=pt)))
    # createByReal wraps a cm value; the coordinate-anchor path uses it for offsetX/Y/Z.
    monkeypatch.setattr(adsk.core.ValueInput, "createByReal",
                        staticmethod(lambda v: SimpleNamespace(_real=v)))
    return d, calls


def _matrix(deg=0.0, t=(0.0, 0.0, 0.0)):
    """An occurrence placement on conftest's shared numeric Matrix3D - asArray() is the only read
    the placement guard makes."""
    return FakeMatrix3D(deg, t)


class TestHandlerGuards:
    def test_no_active_design_errors(self, monkeypatch):
        monkeypatch.setattr(jo._common, "design", lambda: None)
        res = jo.handler()
        assert res["isError"] is True and "design" in res["message"].lower()

    def test_unknown_anchor_errors(self, monkeypatch):
        _install_handler(monkeypatch)
        res = jo.handler(anchor="wormhole")
        assert res["isError"] is True and "wormhole" in res["message"]

    def test_unknown_target_errors(self, monkeypatch):
        _install_handler(monkeypatch)
        res = jo.handler(target="mars")
        assert res["isError"] is True and "mars" in res["message"]

    def test_unknown_keypoint_errors(self, monkeypatch):
        _install_handler(monkeypatch)
        res = jo.handler(keypoint="nowhere")
        assert res["isError"] is True and "nowhere" in res["message"]

    def test_unknown_units_errors(self, monkeypatch):
        _install_handler(monkeypatch)
        res = jo.handler(units="furlong")
        assert res["isError"] is True and "furlong" in res["message"]


class TestHandlerCoordinateAnchor:
    # anchor='coordinates' anchors on the MODEL ORIGIN and holds the
    # position in REAL parametric offsetX/Y/Z - not an undimensioned point floating in a hidden sketch
    # (which read plausibly while spawning 0.00mm parameters). The reported location is the read-back
    # offsets, so it is verifiable and recompute-robust.

    def test_coordinates_held_in_parametric_offsets_not_a_floating_point(self, monkeypatch):
        _, calls = _install_handler(monkeypatch)
        out = _payload(jo.handler(anchor="coordinates", target="at", x=10, y=0, z=0, units="mm"))
        # NO floating sketch point is created for the anchor (that path is gone).
        assert calls == []
        # location is the READ-BACK offset parameters: 10 mm on X (1.0 cm internal, reported in mm).
        assert out["location"] == {"x": 10.0, "y": 0.0, "z": 0.0, "units": "mm"}
        assert out["held_by"] == "parametric offsetX/Y/Z from the model origin"
        assert out["offset_parameters"] == {"x": 10.0, "y": 0.0, "z": 0.0, "units": "mm"}

    def test_target_origin_reports_zero_offsets(self, monkeypatch):
        _, calls = _install_handler(monkeypatch)
        out = _payload(jo.handler(anchor="coordinates", target="origin",
                                  x=99, y=99, z=99, units="mm"))
        # target=origin ignores x/y/z; the offsets are a genuine 0 (the JO IS at the origin), not fake.
        assert out["location"] == {"x": 0.0, "y": 0.0, "z": 0.0, "units": "mm"}
        assert out["offset_parameters"] == {"x": 0.0, "y": 0.0, "z": 0.0, "units": "mm"}

    def test_each_axis_lands_its_own_coordinate(self, monkeypatch):
        # Three DISTINCT values, so each axis is pinned separately: an offset fed another axis's
        # number places the frame somewhere else entirely, and the read-back that follows compares
        # per axis, not by distance from the origin.
        _install_handler(monkeypatch)
        out = _payload(jo.handler(anchor="coordinates", target="at",
                                  x=10, y=20, z=30, units="mm"))
        assert out["offset_parameters"] == {"x": 10.0, "y": 20.0, "z": 30.0, "units": "mm"}
        assert out["location"] == {"x": 10.0, "y": 20.0, "z": 30.0, "units": "mm"}

    def test_mismatched_offset_readback_errors_and_rolls_back(self, monkeypatch):
        # If the offsets don't stick (the JO reports a different position than asked), that is a
        # mislocated origin - a hard error with a rollback, never a false success.
        d, _ = _install_handler(monkeypatch)
        orig_add = d.rootComponent.jointOrigins.add
        rolled = {"back": False}

        def _bad_add(jo_input):
            origin = orig_add(jo_input)
            origin.offsetX = SimpleNamespace(value=9.9)      # asked 1.0 cm, reports 9.9 cm
            origin.deleteMe = lambda: rolled.__setitem__("back", True) or True
            return origin
        monkeypatch.setattr(d.rootComponent.jointOrigins, "add", _bad_add)
        res = jo.handler(anchor="coordinates", target="at", x=10, y=0, z=0, units="mm")
        assert res["isError"] is True and "did not take" in res["message"]
        assert rolled["back"] is True                        # the mislocated origin was rolled back

    def test_creates_joint_origin_and_reports_frame_axes(self, monkeypatch):
        d, _ = _install_handler(monkeypatch)
        out = _payload(jo.handler(anchor="coordinates"))
        assert out["created"] is True
        assert out["joint_origin_name"] == "JointOrigin1"
        assert out["frame_axes"]["primary_axis_Z"] == [0.0, 0.0, 1.0]
        assert d.rootComponent.jointOrigins.count == 1

    def test_custom_name_is_applied_to_the_new_joint_origin(self, monkeypatch):
        _install_handler(monkeypatch)
        out = _payload(jo.handler(anchor="coordinates", name="Anchor1"))
        assert out["joint_origin_name"] == "Anchor1"

    def test_reports_dnn_names_for_the_offset_parameters(self, monkeypatch):
        # the dNN names are what param_set needs to drive the frame with an expression (creation
        # accepts numeric offsets only) - values alone leave the agent fishing through param_get
        _install_handler(monkeypatch)
        out = _payload(jo.handler(anchor="coordinates", target="at", x=10, y=0, z=0, units="mm"))
        assert out["model_parameters"] == {"offset_x": "d5", "offset_y": "d6", "offset_z": "d7"}
        assert "param_set" in out["note"]


# ── anchor='bbox_center': computed center + oriented Z (geometry-building level) ─────────────────

def _body(mn, mx, name="Body1"):
    """A body on conftest's shared BRepBody fake, carrying the world bounding box (cm) a bbox
    anchor's centre is computed from."""
    return BRepBody(name=name, bbox=make_bbox(mn, mx))


class _CapLines(_NamedCollection):
    """sketchCurves.sketchLines, recording the endpoints each addByTwoPoints was handed - the
    orientation line the bbox anchor's frame Z runs along."""
    def __init__(self, store):
        super().__init__()
        self.store = store

    def addByTwoPoints(self, p1, p2):
        self.store.append((p1, p2))
        line = SimpleNamespace(kind="line")
        self._items.append(line)
        return line


class _CapSketches(_NamedCollection):
    """component.sketches: add(plane) mints the helper sketch a computed anchor draws into. Sketches
    has a live shape dump but no shared fake yet, so the add() half lives here."""
    def __init__(self, store):
        super().__init__()
        self.store = store

    def add(self, plane):
        sketch = Sketch(name=None)
        sketch.sketchCurves.sketchLines = _CapLines(self.store)
        self._items.append(sketch)
        return sketch


def _cap_comp(store):
    """A component whose sketches collection captures the orientation line that gets drawn."""
    comp = MakeComp(name="Comp1", origin_planes=(object(), object(), object()))
    comp.sketches = _CapSketches(store)
    return comp


def _install_bbox_target(monkeypatch, body):
    """Wire the adsk types + a design whose findEntityByToken resolves the bbox_target handle to `body`,
    and record every Point3D.create call so a test can read the orientation line's endpoints."""
    # Distinct class identities so TargetRef classifies `body` as a BODY (not a face/mesh).
    monkeypatch.setattr(adsk.fusion, "BRepFace", BRepFace, raising=False)
    monkeypatch.setattr(adsk.fusion, "MeshBody", MeshBody, raising=False)
    monkeypatch.setattr(adsk.fusion, "BRepBody", BRepBody, raising=False)
    install(jo, make_design(tokens={"BODYH": body}))
    monkeypatch.setattr(adsk.core.Point3D, "create",
                        staticmethod(lambda x, y, z: SimpleNamespace(x=x, y=y, z=z)))
    monkeypatch.setattr(adsk.fusion.JointGeometry, "createByCurve",
                        staticmethod(lambda curve, kp: SimpleNamespace(curve=curve, kp=kp)))


class TestBboxCenterGeometry:
    def test_center_is_the_bbox_midpoint(self, monkeypatch):
        body = _body((0, 0, 0), (10, 20, 30))
        _install_bbox_target(monkeypatch, body)
        store, meta = [], {}
        g, desc, err = _call(anchor="bbox_center", comp=_cap_comp(store),
                             bbox_target="BODYH", orient_axis="z", meta=meta)
        assert err is None and g is not None
        # The orientation line STARTS at the bbox center (5,10,15) - that is the frame origin.
        start = store[0][0]
        assert (start.x, start.y, start.z) == (5.0, 10.0, 15.0)
        assert meta["anchor_cm"] == (5.0, 10.0, 15.0)

    def test_z_line_runs_along_requested_world_axis(self, monkeypatch):
        body = _body((0, 0, 0), (10, 20, 30))
        _install_bbox_target(monkeypatch, body)
        store = []
        _call(anchor="bbox_center", comp=_cap_comp(store), bbox_target="BODYH", orient_axis="x")
        start, end = store[0]
        d = (end.x - start.x, end.y - start.y, end.z - start.z)
        assert d == (1.0, 0.0, 0.0)          # Z aligned to world X

    def test_flip_reverses_the_oriented_axis(self, monkeypatch):
        body = _body((0, 0, 0), (10, 20, 30))
        _install_bbox_target(monkeypatch, body)
        store = []
        _call(anchor="bbox_center", comp=_cap_comp(store), bbox_target="BODYH",
              orient_axis="z", flip=True)
        start, end = store[0]
        assert (end.x - start.x, end.y - start.y, end.z - start.z) == (0.0, 0.0, -1.0)

    def test_the_geometry_takes_the_lines_start_keypoint(self, monkeypatch):
        # MiddleKeyPoint would anchor the origin at the orientation line's midpoint instead of the
        # bbox center the anchor just computed.
        body = _body((0, 0, 0), (10, 20, 30))
        _install_bbox_target(monkeypatch, body)
        g, _desc, err = _call(anchor="bbox_center", comp=_cap_comp([]),
                              bbox_target="BODYH", orient_axis="z")
        assert err is None
        assert g.kp == adsk.fusion.JointKeyPointTypes.StartKeyPoint


# ── orient_axis from a PLANAR-FACE handle: AxisRef now sources a direction from a face ───────────
# Once AxisRef accepts a planar-face handle (returning its NORMAL as a ('world', vec) direction),
# orient_axis takes a face with NO code change in joint_create_origin - the frame's Z aligns to that
# normal. This proves the one-call parity insert-into-template's Phase 2 relies on.

def _orient_face(normal):
    """A planar face whose NORMAL is the direction AxisRef sources from it - the shared Plane fake
    carries both that normal and the measured surfaceType."""
    return BRepFace(Plane(FakeVector3D(*normal)))


def _install_face_orient(monkeypatch, body, face, tokens=None):
    """Wire the adsk types + a design whose findEntityByToken resolves BODYH -> body and FACEH -> face,
    so bbox_target resolves a body and orient_axis (AxisRef) resolves a planar face to its normal.
    Real (non-Mock) BRepEdge/SketchLine classes so AxisRef's isinstance ladder reaches the face branch."""
    monkeypatch.setattr(adsk.fusion, "BRepFace", BRepFace, raising=False)
    monkeypatch.setattr(adsk.fusion, "BRepEdge", BRepEdge, raising=False)
    monkeypatch.setattr(adsk.fusion, "SketchLine", type("SL", (), {}), raising=False)
    monkeypatch.setattr(adsk.fusion, "MeshBody", MeshBody, raising=False)
    monkeypatch.setattr(adsk.fusion, "BRepBody", BRepBody, raising=False)
    handles = {"BODYH": body, "FACEH": face} if tokens is None else tokens
    install(jo, make_design(tokens=handles))
    monkeypatch.setattr(adsk.core.Point3D, "create",
                        staticmethod(lambda x, y, z: SimpleNamespace(x=x, y=y, z=z)))
    monkeypatch.setattr(adsk.fusion.JointGeometry, "createByCurve",
                        staticmethod(lambda curve, kp: SimpleNamespace(curve=curve, kp=kp)))


class TestOrientAxisFromFace:
    def test_planar_face_handle_aligns_z_to_the_face_normal(self, monkeypatch):
        body = _body((0, 0, 0), (10, 20, 30))
        face = _orient_face((0, 1, 0))                    # normal along world +Y
        _install_face_orient(monkeypatch, body, face)
        store = []
        g, desc, err = _call(anchor="bbox_center", comp=_cap_comp(store),
                             bbox_target="BODYH", orient_axis="FACEH", meta={})
        assert err is None and g is not None
        start, end = store[0]
        # The orientation line (the frame's Z) runs along the face normal (0,1,0), from the bbox center.
        assert (end.x - start.x, end.y - start.y, end.z - start.z) == (0.0, 1.0, 0.0)
        assert (start.x, start.y, start.z) == (5.0, 10.0, 15.0)

    def test_construction_axis_handle_aligns_z_to_the_datum_direction(self, monkeypatch):
        # A ConstructionAxis's geometry is an InfiniteLine3D - origin/direction, NO start/end points.
        # Reading start/end directly refuses an axis the schema advertises; the shared axis_line_of
        # reads both shapes (and lifts a datum into world space).
        body = _body((0, 0, 0), (10, 20, 30))
        datum = SimpleNamespace(name="Spin", component=None, assemblyContext=None,
                                geometry=FakeInfiniteLine3D(FakePoint(0.0, 0.0, 0.0),
                                                            FakeVector3D(0.0, 0.0, 2.0)))
        # ONE design resolving both tokens: the body for bbox_target, the datum for orient_axis.
        _install_face_orient(monkeypatch, body, _orient_face((0, 1, 0)),
                             tokens={"BODYH": body, "AXISH": datum})
        monkeypatch.setattr(adsk.fusion, "ConstructionAxis", type(datum), raising=False)
        store = []
        g, desc, err = _call(anchor="bbox_center", comp=_cap_comp(store),
                             bbox_target="BODYH", orient_axis="AXISH", meta={})
        assert err is None and g is not None
        start, end = store[0]
        # the datum's direction, normalized - not a refusal
        assert (end.x - start.x, end.y - start.y, end.z - start.z) == (0.0, 0.0, 1.0)


# ── anchor='face_center': planar face guard + normal orientation ────────────────────────────────

class TestFaceCenterGeometry:
    def test_planar_face_builds_via_planar_factory(self, install_geom):
        calls = install_geom({"F": _face(planar=True)})
        g, desc, err = _call(anchor="face_center", geometry_handle="F")
        assert err is None and g is not None
        assert calls["kind"] == "planar_face"
        assert "normal" in desc

    def test_non_planar_face_is_rejected(self, install_geom):
        install_geom({"C": _face(planar=False)})
        g, desc, err = _call(anchor="face_center", geometry_handle="C")
        assert g is None
        assert "PLANAR" in err

    def test_non_face_handle_is_rejected(self, install_geom):
        install_geom({"E": BRepEdge(Line3D())})
        g, desc, err = _call(anchor="face_center", geometry_handle="E")
        assert g is None
        assert "not a face" in err


# ── handler(): bbox_center guards + honesty read-back ───────────────────────────────────────────

class _JOInputBbox:
    primaryAxisVector = SimpleNamespace(x=0.0, y=0.0, z=1.0)
    secondaryAxisVector = SimpleNamespace(x=1.0, y=0.0, z=0.0)
    thirdAxisVector = SimpleNamespace(x=0.0, y=1.0, z=0.0)


class _JOWithOrigin:
    def __init__(self, origin_cm):
        self.name = "JointOrigin1"
        self.geometry = SimpleNamespace(
            origin=SimpleNamespace(x=origin_cm[0], y=origin_cm[1], z=origin_cm[2]))
        self.deleted = False

    def deleteMe(self):
        self.deleted = True
        return True


class _JointOriginsBbox:
    def __init__(self, jo_obj):
        self.count = 0
        self._jo = jo_obj

    def createInput(self, geom):
        return _JOInputBbox()

    def add(self, ji):
        self.count += 1
        return self._jo


def _install_bbox_handler(monkeypatch, origin_cm, bbox=((0, 0, 0), (10, 20, 30))):
    """Full handler wiring for anchor='bbox_center': a body at `bbox`, and a created joint origin whose
    geometry.origin reads back at `origin_cm` (cm) so a test can exercise the landing-point check."""
    body = _body(bbox[0], bbox[1])
    jo_obj = _JOWithOrigin(origin_cm)
    comp = _cap_comp([])
    comp.jointOrigins = _JointOriginsBbox(jo_obj)
    monkeypatch.setattr(adsk.fusion, "BRepFace", BRepFace, raising=False)
    monkeypatch.setattr(adsk.fusion, "MeshBody", MeshBody, raising=False)
    monkeypatch.setattr(adsk.fusion, "BRepBody", BRepBody, raising=False)
    install(jo, make_design(comp=comp, tokens={"BODYH": body}))
    monkeypatch.setattr(adsk.core.Point3D, "create",
                        staticmethod(lambda x, y, z: SimpleNamespace(x=x, y=y, z=z)))
    monkeypatch.setattr(adsk.fusion.JointGeometry, "createByCurve",
                        staticmethod(lambda curve, kp: SimpleNamespace(curve=curve, kp=kp)))
    return jo_obj


class TestBboxCenterHandler:
    def test_reports_computed_anchor_and_readback_in_display_units(self, monkeypatch):
        # JO lands on the computed center (5,10,15) cm -> 50,100,150 mm.
        _install_bbox_handler(monkeypatch, origin_cm=(5.0, 10.0, 15.0))
        out = _payload(jo.handler(anchor="bbox_center", bbox_target="BODYH",
                                  orient_axis="z", units="mm"))
        assert out["computed_anchor"]["x"] == 50.0
        assert out["computed_anchor"]["y"] == 100.0
        assert out["computed_anchor"]["z"] == 150.0
        assert out["origin_readback"]["z"] == 150.0
        assert out["frame_axes"]["primary_axis_Z"] == [0.0, 0.0, 1.0]

    def test_wrong_landing_point_errors_and_rolls_back(self, monkeypatch):
        # JO lands far from the computed center -> honesty failure: error + deleteMe().
        jo_obj = _install_bbox_handler(monkeypatch, origin_cm=(99.0, 99.0, 99.0))
        res = jo.handler(anchor="bbox_center", bbox_target="BODYH", units="mm")
        assert res["isError"] is True
        assert "computed anchor" in res["message"]
        assert jo_obj.deleted is True

    def test_missing_target_errors(self, monkeypatch):
        _install_bbox_handler(monkeypatch, origin_cm=(5.0, 10.0, 15.0))
        res = jo.handler(anchor="bbox_center", bbox_target="")
        assert res["isError"] is True
        assert "bbox_target" in res["message"]


class TestBboxCenterAmbiguousTarget:
    def test_ambiguous_name_is_refused(self, monkeypatch):
        # component: a real Occurrence always answers it, and the shared census reads it to tell an
        # ordinary occurrence from one whose external reference will not resolve.
        occ1 = make_occurrence(path="root+Bolt:1", component=MakeComp(name="Bolt"))
        occ2 = make_occurrence(path="root+Bolt:2", component=MakeComp(name="Bolt"))
        install(jo, make_design(comp=MakeComp(name="Root", occurrences=[occ1, occ2])))
        res = jo.handler(anchor="bbox_center", bbox_target="Bolt")
        assert res["isError"] is True
        assert "ambiguous" in res["message"].lower()


# ── 'component': which component's jointOrigins collection receives the origin ───────────────────
# The platform accepts a JO on a sub-component (its parentComponent then IS that component), which is
# what lets the frame serve as that component's side of a joint. Omitted keeps the root landing.
# A joint origin is positioned in ITS component's space, so a WORLD coordinate (an x,y,z, a world
# bounding-box center) is only the same number there while the component sits at the world origin
# unrotated - anything else is refused rather than placed somewhere else.

def _moved(dx_cm):
    """A placement translated dx_cm along X - the shared matrix puts it in element 3 of asArray()."""
    return _matrix(t=(dx_cm, 0.0, 0.0))


def _occurrence(comp, path="Sub:1", transform=None, context=None):
    return make_occurrence(path=path, component=comp,
                           transform2=_matrix() if transform is None else transform,
                           assembly_context=context)


def _install_with_sub(monkeypatch, transform=None, sub_name="Sub", path="Sub:1"):
    """A design whose root holds ONE sub-component occurrence, with the coordinate-anchor adsk seams
    wired. Returns (design, sub_component, occurrence)."""
    sub = _comp(sub_name)
    occ = _occurrence(sub, path=path, transform=transform)
    design = _design(occurrences=[occ])
    _install_handler(monkeypatch, design=design)
    return design, sub, occ


class TestPlacementIdentity:
    def test_identity_matrix_reads_true(self):
        assert jo._is_identity(_matrix()) is True

    def test_a_rotation_is_not_the_identity(self):
        assert jo._is_identity(_matrix(deg=90.0)) is False   # 90 deg about Z, no translation

    def test_an_unreadable_matrix_answers_none_not_false(self):
        # None and False are different verdicts: one refuses because it cannot tell, the other
        # because it can. Coercing either to False would let an unknown placement through as "moved".
        # A short array is no shape the shared matrix can build, so it is stood in for inline.
        assert jo._is_identity(SimpleNamespace()) is None
        assert jo._is_identity(SimpleNamespace(asArray=lambda: [1, 0, 0])) is None


class TestLandingComponent:
    def test_omitted_component_lands_on_root(self, monkeypatch):
        design, sub, _ = _install_with_sub(monkeypatch)
        out = _payload(jo.handler(anchor="coordinates", target="origin"))
        assert design.rootComponent.jointOrigins.count == 1
        assert sub.jointOrigins.count == 0
        assert out["component"] == "Comp1"

    def test_named_component_receives_the_joint_origin(self, monkeypatch):
        design, sub, _ = _install_with_sub(monkeypatch)
        out = _payload(jo.handler(anchor="coordinates", target="origin", component="Sub:1"))
        assert sub.jointOrigins.count == 1
        assert design.rootComponent.jointOrigins.count == 0
        assert out["component"] == "Sub"            # read off the LANDED jo.parentComponent
        assert out["component_verified"] is True

    def test_coordinates_in_a_world_origin_component_keep_their_numbers(self, monkeypatch):
        _, sub, _ = _install_with_sub(monkeypatch)
        out = _payload(jo.handler(anchor="coordinates", x=10, y=0, z=0, units="mm",
                                  component="Sub:1"))
        assert out["offset_parameters"] == {"x": 10.0, "y": 0.0, "z": 0.0, "units": "mm"}
        assert out["held_by"] == "parametric offsetX/Y/Z from the component's origin"
        assert sub.jointOrigins.count == 1

    def test_world_coordinates_into_a_moved_component_are_refused(self, monkeypatch):
        _, sub, _ = _install_with_sub(monkeypatch, transform=_moved(5.0))
        res = jo.handler(anchor="coordinates", x=10, y=0, z=0, units="mm", component="Sub:1")
        assert res["isError"] is True
        assert "not the identity" in res["message"] and "Sub:1" in res["message"]
        assert sub.jointOrigins.count == 0          # refused before anything was created

    def test_bbox_center_into_a_moved_component_is_refused(self, monkeypatch):
        # the bounding-box center is a WORLD point too - the same wrong landing as a raw x,y,z.
        _, sub, _ = _install_with_sub(monkeypatch, transform=_moved(5.0))
        res = jo.handler(anchor="bbox_center", bbox_target="BODYH", component="Sub:1")
        assert res["isError"] is True and "not the identity" in res["message"]
        assert sub.jointOrigins.count == 0

    def test_component_origin_is_allowed_in_a_moved_component(self, monkeypatch):
        # target='origin' carries no world coordinate: it sits at the component's OWN origin, which
        # is unambiguous wherever that component is placed.
        _, sub, _ = _install_with_sub(monkeypatch, transform=_moved(5.0))
        out = _payload(jo.handler(anchor="coordinates", target="origin", component="Sub:1"))
        assert sub.jointOrigins.count == 1
        assert out["anchored_on"] == "component origin"

    def test_a_moved_ancestor_refuses_even_at_an_identity_leaf(self, monkeypatch):
        # a transform is relative to the PARENT component, so an identity leaf inside a moved parent
        # is still displaced in world space - the whole chain decides, not the leaf.
        parent_comp = _comp("Parent")
        parent = _occurrence(parent_comp, path="Parent:1", transform=_moved(7.0))
        sub = _comp("Sub")
        leaf = _occurrence(sub, path="Parent:1+Sub:1", context=parent)
        _install_handler(monkeypatch, design=_design(occurrences=[parent, leaf]))
        res = jo.handler(anchor="coordinates", x=10, component="Parent:1+Sub:1")
        assert res["isError"] is True and "not the identity" in res["message"]
        assert sub.jointOrigins.count == 0

    def test_unreadable_placement_is_refused_rather_than_assumed(self, monkeypatch):
        # NEITHER placement matrix reads, so nothing says where the component sits
        sub = _comp("Sub")
        unreadable = "3 : the placement of this occurrence is unavailable"
        occ = make_occurrence(path="Sub:1", component=sub,
                              raises_on={"transform2": unreadable, "transform": unreadable})
        _install_handler(monkeypatch, design=_design(occurrences=[occ]))
        res = jo.handler(anchor="coordinates", x=10, component="Sub:1")
        assert res["isError"] is True and "could not be read" in res["message"]
        assert sub.jointOrigins.count == 0


class TestActiveComponentDisclosure:
    # Omitting 'component' lands the origin on the ROOT even while another component is the active
    # edit target - unlike sketch/extrude, which build into the active one. The divergence is
    # disclosed in the payload, naming both components.

    def test_active_sub_component_landing_on_root_is_disclosed(self, monkeypatch):
        design, sub, _ = _install_with_sub(monkeypatch)
        design.activeComponent = sub                # the edit target is the sub-component
        out = _payload(jo.handler(anchor="coordinates", target="origin"))
        assert design.rootComponent.jointOrigins.count == 1 and sub.jointOrigins.count == 0
        assert out["active_component"] == "Sub"
        assert "'Comp1'" in out["note"] and "'Sub' is the active edit target" in out["note"]

    def test_no_disclosure_when_the_root_is_the_active_component(self, monkeypatch):
        design, sub, _ = _install_with_sub(monkeypatch)
        design.activeComponent = design.rootComponent
        out = _payload(jo.handler(anchor="coordinates", target="origin"))
        assert "active_component" not in out
        assert "active edit target" not in out["note"]

    def test_no_disclosure_when_the_origin_landed_where_asked(self, monkeypatch):
        # 'component' was given, so there is no divergence to report even with a sub active.
        design, sub, _ = _install_with_sub(monkeypatch)
        design.activeComponent = sub
        out = _payload(jo.handler(anchor="coordinates", target="origin", component="Sub:1"))
        assert "active_component" not in out
        assert "active edit target" not in out["note"]

    def test_an_entity_anchor_is_not_gated_by_the_placement_transform(self, monkeypatch):
        # the guard covers coordinates the tool computes; a sketch/geometry anchor carries none, so a
        # moved component still gets its joint origin (the platform decides a cross-component anchor).
        _, sub, _ = _install_with_sub(monkeypatch, transform=_moved(5.0))
        _stub_scoped_sketch(monkeypatch, (SimpleNamespace(
            sketchPoints=SimpleNamespace(count=1, item=lambda i: "PT")), None))
        out = _payload(jo.handler(anchor="sketch_point", sketch_name="S", entity_index=0,
                                  component="Sub:1"))
        assert sub.jointOrigins.count == 1
        assert out["component"] == "Sub"

    def test_ambiguous_component_name_is_refused(self, monkeypatch):
        # two sub-assemblies each holding a "Bolt:1": no string tells them apart, so the call is
        # refused with both paths instead of one being picked.
        a, b = _comp("BoltA"), _comp("BoltB")
        design = _design(occurrences=[_occurrence(a, path="SubA:1+Bolt:1"),
                                          _occurrence(b, path="SubB:1+Bolt:1")])
        _install_handler(monkeypatch, design=design)
        res = jo.handler(anchor="coordinates", target="origin", component="Bolt:1")
        assert res["isError"] is True
        assert "SubA:1+Bolt:1" in res["message"] and "SubB:1+Bolt:1" in res["message"]
        assert a.jointOrigins.count == 0 and b.jointOrigins.count == 0
        assert design.rootComponent.jointOrigins.count == 0   # nor did it fall back to root

    def test_unknown_component_name_is_refused_with_the_available_paths(self, monkeypatch):
        design, sub, _ = _install_with_sub(monkeypatch)
        res = jo.handler(anchor="coordinates", target="origin", component="Ghost")
        assert res["isError"] is True and "Sub:1" in res["message"]
        assert design.rootComponent.jointOrigins.count == 0 and sub.jointOrigins.count == 0

    def test_landing_on_another_component_errors_and_rolls_back(self, monkeypatch):
        # the reported component is READ BACK off the created origin - a JO that reports a different
        # parent than the collection it was added to is a failure, not a quietly renamed success.
        design, sub, _ = _install_with_sub(monkeypatch)
        rolled = {"back": False}
        real_add = sub.jointOrigins.add

        def _lying_add(jo_input):
            origin = real_add(jo_input)
            origin.parentComponent = design.rootComponent      # landed somewhere else
            origin.deleteMe = lambda: rolled.__setitem__("back", True) or True
            return origin
        monkeypatch.setattr(sub.jointOrigins, "add", _lying_add)
        res = jo.handler(anchor="coordinates", target="origin", component="Sub:1")
        assert res["isError"] is True
        assert "landed on component 'Comp1'" in res["message"]
        assert rolled["back"] is True

    def test_an_unverifiable_landing_is_disclosed_not_claimed(self, monkeypatch):
        # parentComponent unreadable - the WEAKER of the two unknowns, because 'component' then
        # repeats the caller's own ask. The payload must say so in words, not just in a flag: a
        # reader taking 'component' at face value is reading back the request.
        _, sub, _ = _install_with_sub(monkeypatch)
        sub.jointOrigins.owner = None               # the created JO reports no parentComponent
        out = _payload(jo.handler(anchor="coordinates", target="origin", component="Sub:1"))
        assert out["component"] == "Sub"
        assert out["component_verified"] is False
        assert "UNVERIFIED" in out["note"]
        assert "did not read at all" in out["note"]      # names WHICH read failed
        assert "repeats the requested 'Sub'" in out["note"]

    def test_a_landing_that_cannot_be_MATCHED_is_disclosed_and_not_rolled_back(self, monkeypatch):
        # The JO's parentComponent READS, but same_component cannot tell it from the requested
        # component (no token). Rolling back here would delete a created origin over an unreadable
        # token, so it stands - with component_verified false and a note saying which read failed.
        _design, sub, _ = _install_with_sub(monkeypatch)
        rolled = {"back": False}
        real_add = sub.jointOrigins.add

        def _tokenless_add(jo_input):
            origin = real_add(jo_input)
            origin.parentComponent = _comp("Sub", token=None)
            del origin.parentComponent.entityToken       # nothing identifies it
            origin.deleteMe = lambda: rolled.__setitem__("back", True) or True
            return origin
        monkeypatch.setattr(sub.jointOrigins, "add", _tokenless_add)
        out = _payload(jo.handler(anchor="coordinates", target="origin", component="Sub:1"))
        assert out["component_verified"] is False
        assert "UNVERIFIED" in out["note"]
        assert rolled["back"] is False               # the created origin was kept
