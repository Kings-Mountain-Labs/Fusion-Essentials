"""Unit tests for ``cam_select_geometry`` — set the machining geometry (and optional heights) on a CAM
operation, then optionally regenerate.

adsk.cam is mocked. What we PIN is the handler's own logic:
  - dispatch by selection kind: the curve family (chain/pocket/face/silhouette/sketch/
    pocket_recognition) via getCurveSelections -> createNew*Selection -> inputGeometry ->
    applyCurveSelections, vs the holes family via holeFaces.value = [faces];
  - each kind takes its geometry from ONE input (handles / bodies / sketches) and refuses the others;
  - the per-kind knobs (chain is_open/reverted, loop_type/side_type, the pocket-recognition filter),
    each confirmed by a read-back, and REFUSED on a kind whose selection class lacks the property;
  - sketch AND body names are resolved design-wide (via _inputs.SketchRefList / BodyRefList), a name
    several of them share is refused naming the ONE 'component' scope, and that scope narrows every
    name in whichever list the selection kind reads;
  - the payload NAMES what the by-name references resolved to, so a wrong pick is visible in it;
  - the post-apply read-back: outputGeometry paths/segments + value entities, and a selection whose
    hasError is set turning the call into an error carrying Fusion's own reason;
  - diameter filtering of cylinder faces (mm), and the empty-after-filter guard;
  - height setting via _mode/_offset (never _value), validated-before-mutate;
  - generation LAUNCH-and-return: the handler never pumps/waits on the future - it registers the
    future in _cam_common._GENERATIONS (keeps it alive) and the note teaches the
    cam_get_status(target=...) read;
  - the guards (bad selection, no CAM, missing/ambiguous op, 0 selections, no faces after filter).

The GeometryHandleList/BodyRefList input kinds have their own tests; here we patch those resolve
seams to hand back fake entities so we exercise the handler, not the resolver. The sketch input is
resolved for real against a fake design, because refusing a duplicated sketch name is this tool's
own promise.
"""

import json
import types

import pytest

from conftest import (load_tool, make_cam, install, make_sketch, MakeComp, MakeDesign, BRepBody,
                      BRepEdge, BRepFace, Cylinder, FakeCAMParameter, FakeCAMParameters,
                      body_proxy, _NamedCollection)
from conftest import FakeSetup as SharedSetup, FakeOperation as SharedOp

cg = load_tool("cam_select_geometry")
_cam = load_tool("_cam_common")


@pytest.fixture(autouse=True)
def _restore_resolver():
    """The tests patch _inputs.GeometryHandleList.resolve / BodyRefList.resolve (shared class
    methods). Restore them after each test so the patch doesn't leak into other modules' tests."""
    orig_handles = cg._inputs.GeometryHandleList.resolve
    orig_bodies = cg._inputs.BodyRefList.resolve
    yield
    cg._inputs.GeometryHandleList.resolve = orig_handles
    cg._inputs.BodyRefList.resolve = orig_bodies


@pytest.fixture(autouse=True)
def _entitled(monkeypatch):
    """Every test runs against an installation reading the operation's strategy as allowed to
    generate; the entitlement tests patch this same seam with their own answer."""
    monkeypatch.setattr(cg, "strategy_generation_allowed", lambda name: True)


@pytest.fixture(autouse=True)
def _clean_generations():
    """A launch registers a future in _cam_common._GENERATIONS (shared, session-lived) - clear it
    around each test so entries never leak into test_cam_generate's registry assertions."""
    _cam._GENERATIONS.clear()
    _cam._HANDLE_SEQ[0] = 0
    yield
    _cam._GENERATIONS.clear()
    _cam._HANDLE_SEQ[0] = 0


# ── fakes ────────────────────────────────────────────────────────────────────

class _Cyl(Cylinder):
    """A cylindrical surface carrying the radius (cm) the diameter filter reads."""
    def __init__(self, radius_cm):
        super().__init__(axis=None)
        self.radius = radius_cm


def _Face(radius_cm=None):
    """A BRep face; a cylinder face carries .geometry.radius (cm), anything else carries none."""
    return BRepFace(_Cyl(radius_cm) if radius_cm is not None else object())


def _Edge():
    """A BRep edge - the geometry a chain/contour selection takes."""
    return BRepEdge(None)


def _Body(name="Body1"):
    """A solid body - what a silhouette/pocket-recognition selection takes."""
    return BRepBody(name)


class _Path:
    """A Curve3DPath: a counted collection of connected Curve3D objects."""
    def __init__(self, count):
        self.count = count


class _CurveSelection:
    """A CurveSelection (chain/pocket/sketch/...). Records what was set - in `writes`, in order - and
    carries the read-back channel every kind inherits from CurveSelection: outputGeometry, value,
    hasError/error, hasWarning/warning - plus the per-kind knobs the appliers set."""
    def __init__(self, kind):
        object.__setattr__(self, "writes", [])
        self.areHolesIncluded = None          # gates minimumHoleDiameter below - set it first
        self._min_hole_dia = None
        self.kind = kind
        self.inputGeometry = None
        self.isOpen = None
        self.isReverted = None
        self.loopType = None
        self.sideType = None
        self.isSetupModelSelected = None
        self.minimumCornerRadius = None
        self.maximumCornerRadius = None
        self.minimumPocketDepth = None
        self.maximumPocketDepth = None
        self.outputGeometry = []
        self.hasError = False
        self.error = ""
        self.hasWarning = False
        self.warning = ""
        self._value = None
        object.__setattr__(self, "writes", [])   # only the handler's own writes are recorded

    def __setattr__(self, name, value):
        self.writes.append(name)
        object.__setattr__(self, name, value)

    @property
    def minimumHoleDiameter(self):
        return self._min_hole_dia

    @minimumHoleDiameter.setter
    def minimumHoleDiameter(self, value):
        # The binding gates this one: "It can only be set if areHoldeIncluded is set to true." Set
        # out of order, the selection keeps its default and the caller's bound never applies.
        if self.areHolesIncluded:
            self._min_hole_dia = value

    @property
    def value(self):
        # CurveSelection.value reports the input selection unless a kind expands it.
        return (self.inputGeometry or []) if self._value is None else self._value


class _DeafHoleDiameter(_CurveSelection):
    """A selection that keeps its default hole-diameter bound whatever is written to it - so the
    payload can only report the true value by READING it back."""
    @property
    def minimumHoleDiameter(self):
        return None

    @minimumHoleDiameter.setter
    def minimumHoleDiameter(self, value):
        pass


class _ClampingDepth(_CurveSelection):
    """A selection that CLAMPS the pocket-depth bound to its own minimum (5.08 cm = 2 in) instead of
    keeping what was written - a read-back that is a wrong NUMBER, not a missing one."""
    @property
    def minimumPocketDepth(self):
        return 5.08

    @minimumPocketDepth.setter
    def minimumPocketDepth(self, value):
        pass


class _UnreadableOpenRail(_CurveSelection):
    """A rail the operation reports back with no readable isOpen - the shape a selection class that
    carries no such property has, and the one a coerced read publishes as 'closed'."""
    @property
    def isOpen(self):
        raise RuntimeError("isOpen is unreadable on this rail")

    @isOpen.setter
    def isOpen(self, _v):
        pass


class _InertLoopType(_CurveSelection):
    """A selection that SWALLOWS a loopType assignment and keeps its default - the SWIG behaviour a
    set-then-read-back exists to catch."""
    @property
    def loopType(self):
        return None
    @loopType.setter
    def loopType(self, value):
        pass


class _CurveSelections(_NamedCollection):
    """A CurveSelections collection: the shared counted walk plus the per-kind createNew* factories
    and the clear() an applier runs first."""
    def __init__(self):
        super().__init__()
        self.cleared = 0
    def clear(self):
        self.cleared += 1
        self._items = []
    def _make(self, kind):
        s = _CurveSelection(kind); self._items.append(s); return s
    def createNewChainSelection(self):       return self._make("chain")
    def createNewPocketSelection(self):      return self._make("pocket")
    def createNewFaceContourSelection(self): return self._make("face")
    def createNewSilhouetteSelection(self):  return self._make("silhouette")
    def createNewSketchSelection(self):      return self._make("sketch")
    def createNewPocketRecognitionSelection(self): return self._make("pocket_recognition")


class _CurveParamValue:
    def __init__(self):
        self._cs = _CurveSelections()
        self.applied = 0
    def getCurveSelections(self):
        return self._cs
    def applyCurveSelections(self, cs):
        self.applied += 1
        self._cs = cs


class _HoleParamValue:
    def __init__(self):
        self.value = []          # set to [faces] by the handler


class _Param(FakeCAMParameter):
    """A CAM parameter whose .value IS the parameter-value object the appliers drive (a
    CadObjectParameterValue), rather than the second-hop payload a plain read takes. It carries no
    name of its own - _OpParams stamps the key it is stored under.

    isEditable is read for the surface sets and the direct object sets, where a False leaves a set
    out (the deprecated checkSurfaceSelection, flat's machiningDirections); the curve params route
    by presence and never consult it. `expression` is the text the parameter already holds - a live
    CAMParameter answers a string here and never None."""
    def __init__(self, value, editable=True, expression="0 mm"):
        super().__init__("", expression=expression, editable=editable)
        self.value = value


class _OpParams(FakeCAMParameters):
    """An operation's parameters, each named by the key it is stored under; `swap`/`drop` put a
    scenario stand-in in one slot, or take a parameter the operation does not carry away."""
    def __init__(self, mapping):
        rows = []
        for name, param in mapping.items():
            param.name = name
            rows.append(param)
        super().__init__(rows)

    def _held(self, name, verb):
        # A miss would leave the operation exactly as built, and the test would pass on the
        # unmodified op instead of on the stand-in it meant to drive.
        assert self.itemByName(name) is not None, f"no parameter named {name!r} to {verb}"

    def swap(self, name, param):
        self._held(name, "swap")
        param.name = name
        self._coll._items = [param if p.name == name else p for p in self._coll._items]

    def drop(self, name):
        self._held(name, "drop")
        self._coll._items = [p for p in self._coll._items if p.name != name]


class _Future:
    def __init__(self, complete=True):
        self.isGenerationCompleted = complete


class _Op(SharedOp):
    """A CAM operation carrying the parameter set its selection kind drives."""
    def __init__(self, name, params, has_tp=True, valid=True, warning="", strategy="contour2d"):
        super().__init__(name, has_toolpath=has_tp, valid=valid, has_warning=bool(warning),
                         warning=warning, strategy=strategy)
        self.parameters = _OpParams(params)


def _Setup(ops):
    """The one setup the resolve walks - these tests never address it by name."""
    return SharedSetup("Setup1", ops=ops)


def _CAM(setups, future=None):
    """A CAM product recording each generateToolpath launch in `generated`."""
    cam = make_cam(*setups)
    cam.generated = []
    fut = future if future is not None else _Future(True)

    def generate_toolpath(op):
        cam.generated.append(op)
        return fut

    cam.generateToolpath = generate_toolpath
    cam.future = fut          # the object a launch has to keep referenced, for a test to compare
    return cam


def _curve_op(name="2D Contour1", **kw):
    return _Op(name, {"contours": _Param(_CurveParamValue()),
                      "topHeight_mode": _Param(None), "topHeight_offset": _Param(None),
                      "bottomHeight_mode": _Param(None), "bottomHeight_offset": _Param(None)}, **kw)


def _drill_op(name="Drill1", **kw):
    return _Op(name, {"holeFaces": _Param(_HoleParamValue())}, **kw)


def _drill_op_with_heights(name="Drill1", **kw):
    # a real drill op carries the height group too - the fake needs it to show what a refusal left
    return _Op(name, {"holeFaces": _Param(_HoleParamValue()),
                      "topHeight_mode": _Param(None), "topHeight_offset": _Param(None),
                      "bottomHeight_mode": _Param(None), "bottomHeight_offset": _Param(None)}, **kw)


def _bore_op(name="Bore1", **kw):
    # bore/circular use 'circularFaces' (no 'holeFaces') — same object-list shape
    return _Op(name, {"circularFaces": _Param(_HoleParamValue())}, **kw)


def _install(monkeypatch, cam, entities):
    monkeypatch.setattr(cg, "get_cam", lambda: (cam, None))
    # patch the geometry-handle resolver to hand back fake entities (resolver has its own tests)
    cg._inputs.GeometryHandleList.resolve = lambda self, raw: (entities, None)
    return cam


def _install_bodies(monkeypatch, cam, bodies):
    # The stub takes the kind's own (raw, component) signature: the handler passes the scope
    # through, so a one-argument stand-in would hide the wiring instead of standing in for it.
    monkeypatch.setattr(cg, "get_cam", lambda: (cam, None))
    cg._inputs.BodyRefList.resolve = lambda self, raw, component="": (list(bodies), None)
    return cam


def _selection_of(op):
    return op.parameters.itemByName("contours").value.getCurveSelections().item(0)


def _payload(result):
    assert result["isError"] is False, result
    return json.loads(result["content"][0]["text"])


# ── guards ───────────────────────────────────────────────────────────────────

class TestGuards:
    def test_bad_selection(self, monkeypatch):
        res = cg.handler(operation="X", selection="nonsense", handles=["h"])
        assert res["isError"] is True and "selection" in res["message"].lower()

    def test_no_cam(self, monkeypatch):
        monkeypatch.setattr(cg, "get_cam", lambda: (None, "no CAM data"))
        res = cg.handler(operation="X", selection="chain", handles=["h"])
        assert res["isError"] is True and "cam" in res["message"].lower()

    def test_pocket_recognition_refuses_before_cam_or_native_resolution(self, monkeypatch):
        touched = []
        monkeypatch.setattr(cg, "get_cam",
                            lambda: touched.append("get_cam") or (None, "unexpected CAM read"))
        monkeypatch.setattr(cg, "_resolve_geometry",
                            lambda *args: touched.append("resolve_geometry") or ([], None))
        res = cg.handler(operation="Adaptive1", selection="pocket_recognition",
                         bodies=["Carrier"], generate=False)
        assert res["isError"] is True
        assert "allow_pocket_recognition=true" in res["message"]
        assert "floor-face" in res["message"] and "edge handles" in res["message"]
        assert touched == []

    def test_op_not_found(self, monkeypatch):
        cam = _CAM([_Setup([_curve_op("A")])])
        _install(monkeypatch, cam, [_Edge()])
        res = cg.handler(operation="Ghost", selection="chain", handles=["h"])
        assert res["isError"] is True and "Ghost" in res["message"]

    def test_duplicate_op_name_across_setups_is_refused(self, monkeypatch):
        # "Drill1" exists in TWO setups - selecting geometry by that name must REFUSE with both
        # setup paths, never silently target whichever setup's op the walk met first.
        cam = make_cam(SharedSetup("Setup1", ops=[SharedOp("Drill1")]),
                       SharedSetup("Setup2", ops=[SharedOp("Drill1")]))
        monkeypatch.setattr(cg, "get_cam", lambda: (cam, None))
        cg._inputs.GeometryHandleList.resolve = lambda self, raw: ([_Face(0.3)], None)
        res = cg.handler(operation="Drill1", selection="holes", handles=["a"])
        assert res["isError"] is True and "ambiguous" in res["message"].lower()
        assert "Setup1 / Drill1" in res["message"] and "Setup2 / Drill1" in res["message"]

    def test_handle_resolve_error_propagates(self, monkeypatch):
        cam = _CAM([_Setup([_curve_op()])])
        monkeypatch.setattr(cg, "get_cam", lambda: (cam, None))
        cg._inputs.GeometryHandleList.resolve = lambda self, raw: (None, "bad handle")
        res = cg.handler(operation="2D Contour1", selection="chain", handles=["h"])
        assert res["isError"] is True and "bad handle" in res["message"]

    def test_unknown_units_refused(self, monkeypatch):
        cam = _CAM([_Setup([_curve_op()])])
        _install(monkeypatch, cam, [_Edge()])
        res = cg.handler(operation="2D Contour1", selection="chain", handles=["h"],
                         units="furlongs", generate=False)
        assert res["isError"] is True and "furlongs" in res["message"]

    def test_handles_on_a_body_selection_is_refused(self, monkeypatch):
        # silhouette's inputGeometry takes BRepBody, so face handles are the wrong input entirely -
        # the refusal must name the input that DOES carry it rather than resolve them as faces.
        cam = _CAM([_Setup([_curve_op()])])
        _install(monkeypatch, cam, [_Face()])
        res = cg.handler(operation="2D Contour1", selection="silhouette", handles=["h"],
                         generate=False)
        assert res["isError"] is True
        assert "'bodies'" in res["message"] and "'handles'" in res["message"]

    def test_bodies_on_a_handle_selection_is_refused(self, monkeypatch):
        cam = _CAM([_Setup([_curve_op()])])
        _install(monkeypatch, cam, [_Edge()])
        res = cg.handler(operation="2D Contour1", selection="chain", bodies=["Carrier"],
                         generate=False)
        assert res["isError"] is True and "'handles'" in res["message"]


# ── curve family (chain/pocket/...) ──────────────────────────────────────────

class TestCurveSelection:
    def test_chain_applies_and_sets_knobs(self, monkeypatch):
        op = _curve_op()
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Edge(), _Edge(), _Edge(), _Edge()])
        out = _payload(cg.handler(operation="2D Contour1", selection="chain",
                                  chain_groups=[["a", "b", "c", "d"]], is_open=True, reverted=True,
                                  generate=False))
        pv = op.parameters.itemByName("contours").value
        sel = pv.getCurveSelections().item(0)
        assert sel.kind == "chain"
        assert len(sel.inputGeometry) == 4
        assert sel.isOpen is True and sel.isReverted is True
        assert pv.applied == 1 and out["selections"] == 1

    def test_pocket_uses_pocket_builder(self, monkeypatch):
        op = _curve_op(name="2D Pocket1")
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Face()])
        cg.handler(operation="2D Pocket1", selection="pocket", handles=["f"], generate=False)
        assert _selection_of(op).kind == "pocket"

    def test_zero_selections_is_error(self, monkeypatch):
        # applyCurveSelections leaves count 0 -> geometry rejected -> hard error
        op = _curve_op()
        pv = op.parameters.itemByName("contours").value
        # make applyCurveSelections drop everything
        pv.applyCurveSelections = lambda cs: setattr(pv, "_cs", _CurveSelections())
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Edge()])
        res = cg.handler(operation="2D Contour1", selection="chain", handles=["h"], generate=False)
        assert res["isError"] is True and "0 selection" in res["message"]


# ── each curve kind takes ONE object type, enforced by the real handle kind ──
#
# ChainSelection.inputGeometry takes B-Rep EDGES and Pocket/FaceContour take BRepFACES: the class
# states one type and Fusion rejects the other. These two run the REAL GeometryHandleList (no
# resolve patch), so the require= the tool wires per kind is what the refusal rests on - handing
# 'chain' a face handle must be refused HERE, not applied and then rejected downstream with the
# operation's previous selection already cleared.

class TestHandleKindRequirement:
    def _resolves_to(self, monkeypatch, entity):
        """The real handle kind, against a design whose one handle resolves to `entity`."""
        import adsk.fusion
        adsk.fusion.BRepFace = BRepFace
        adsk.fusion.BRepEdge = BRepEdge
        monkeypatch.setattr(cg._inputs, "_resolve_token_entity", lambda des, h: entity)
        monkeypatch.setattr(cg._inputs._common, "design", lambda: object())

    def test_chain_refuses_a_face_handle_naming_the_type_it_got(self, monkeypatch):
        op = _curve_op()
        cam = _CAM([_Setup([op])])
        monkeypatch.setattr(cg, "get_cam", lambda: (cam, None))
        self._resolves_to(monkeypatch, _Face())
        res = cg.handler(operation="2D Contour1", selection="chain", handles=["h"], generate=False)
        assert res["isError"] is True
        assert "must be an edge" in res["message"] and "BRepFace" in res["message"]
        assert op.parameters.itemByName("contours").value.applied == 0   # nothing was applied

    def test_pocket_refuses_an_edge_handle_naming_the_type_it_got(self, monkeypatch):
        op = _curve_op(name="2D Pocket1")
        cam = _CAM([_Setup([op])])
        monkeypatch.setattr(cg, "get_cam", lambda: (cam, None))
        self._resolves_to(monkeypatch, _Edge())
        res = cg.handler(operation="2D Pocket1", selection="pocket", handles=["h"], generate=False)
        assert res["isError"] is True
        assert "must be a face" in res["message"] and "BRepEdge" in res["message"]
        assert op.parameters.itemByName("contours").value.applied == 0

    def test_surfaces_refuses_an_edge_handle_naming_the_type_it_got(self, monkeypatch):
        # A surface set takes FACES. Every other surfaces test stubs the resolver seam, so this is
        # the one place the require= that kind is wired with decides anything: an edge resolved
        # through would be assigned into the operation's surface parameter as the wrong type.
        op = _geodesic_op()
        cam = _CAM([_Setup([op])])
        monkeypatch.setattr(cg, "get_cam", lambda: (cam, None))
        self._resolves_to(monkeypatch, _Edge())
        res = cg.handler(operation="Geodesic1", selection="surfaces", handles=["h"], generate=False)
        assert res["isError"] is True
        assert "must be a face" in res["message"] and "BRepEdge" in res["message"]
        assert op.parameters.itemByName("driveSurfaces").value.value == []

    def test_surfaces_takes_a_face_handle_through_the_same_kind(self, monkeypatch):
        # the refusal above must come from the require=, not from this fixture refusing everything.
        op = _geodesic_op()
        cam = _CAM([_Setup([op])])
        monkeypatch.setattr(cg, "get_cam", lambda: (cam, None))
        face = _Face()
        self._resolves_to(monkeypatch, face)
        out = _payload(cg.handler(operation="Geodesic1", selection="surfaces", handles=["h"],
                                  generate=False))
        assert out["selections"] == 1
        assert op.parameters.itemByName("driveSurfaces").value.value == [face]

    def test_the_matching_handle_kind_still_resolves(self, monkeypatch):
        # The two refusals above must come from the require=, not from the real kind refusing
        # everything this fixture hands it.
        op = _curve_op()
        cam = _CAM([_Setup([op])])
        monkeypatch.setattr(cg, "get_cam", lambda: (cam, None))
        self._resolves_to(monkeypatch, _Edge())
        out = _payload(cg.handler(operation="2D Contour1", selection="chain", handles=["h"],
                                  generate=False))
        assert out["selections"] == 1 and _selection_of(op).kind == "chain"


# ── the two body selections (silhouette / pocket_recognition) ────────────────

class TestBodySelections:
    def test_no_bodies_machines_the_setup_models(self, monkeypatch):
        # The zero-body form is a NAMED flag (isSetupModelSelected), not an undocumented default of
        # passing nothing - so it is set explicitly and reported.
        op = _curve_op()
        cam = _CAM([_Setup([op])])
        _install_bodies(monkeypatch, cam, [])
        out = _payload(cg.handler(operation="2D Contour1", selection="silhouette", generate=False))
        sel = _selection_of(op)
        assert sel.kind == "silhouette" and sel.isSetupModelSelected is True
        assert out["setup_models_selected"] is True

    def test_named_bodies_clear_the_setup_model_flag(self, monkeypatch):
        op = _curve_op()
        cam = _CAM([_Setup([op])])
        bodies = [_Body("Carrier")]
        _install_bodies(monkeypatch, cam, bodies)
        out = _payload(cg.handler(operation="2D Contour1", selection="silhouette",
                                  bodies=["Carrier"], generate=False))
        sel = _selection_of(op)
        assert sel.inputGeometry == bodies and sel.isSetupModelSelected is False
        assert out["setup_models_selected"] is False

    def test_pocket_recognition_uses_its_own_builder(self, monkeypatch):
        op = _curve_op(name="Adaptive1")
        cam = _CAM([_Setup([op])])
        _install_bodies(monkeypatch, cam, [_Body("Carrier")])
        cg.handler(operation="Adaptive1", selection="pocket_recognition", bodies=["Carrier"],
                   allow_pocket_recognition=True, generate=False)
        assert _selection_of(op).kind == "pocket_recognition"


def _shared_body_design(name="Body1"):
    """A design where TWO components hold a body of one name - Fusion names every component's first
    body 'Body1', so a machining reference by that name is shared by construction. The sub-component
    is placed once, so the design-wide walk offers the root's native body and the sub's proxy.
    Returns (design, the root's body, the sub's placed proxy)."""
    import adsk.fusion
    adsk.fusion.BRepBody = BRepBody
    sub_body = BRepBody(name, entity_token="TOK-SUB")
    sub = MakeComp("Bracket", bodies=[sub_body], entity_token="TOKEN:Bracket")
    sub_body.parentComponent = sub
    occ = types.SimpleNamespace(name="Bracket:1", fullPathName="Bracket:1", component=sub)
    proxy = body_proxy(sub_body, occ)
    occ.bRepBodies = _NamedCollection([proxy])
    root_body = BRepBody(name, entity_token="TOK-ROOT")
    root = MakeComp("Carrier", bodies=[root_body], occurrences=[occ], entity_token="TOKEN:Carrier")
    root_body.parentComponent = root
    return MakeDesign(comp=root, all_components=[root, sub]), root_body, proxy


class TestBodySelectionComponentScope:
    """The 'bodies' input reads NAMES design-wide, and Fusion auto-names every component's first
    body 'Body1' - the same shared-name trap the sketch input closed, one input over. The ONE
    'component' scope narrows whichever by-name reference the selection kind reads."""

    def _run(self, monkeypatch, **kw):
        op = _curve_op()
        cam = _CAM([_Setup([op])])
        monkeypatch.setattr(cg, "get_cam", lambda: (cam, None))
        res = cg.handler(operation="2D Contour1", selection="silhouette", generate=False, **kw)
        return op, res

    def test_a_shared_body_name_is_refused_naming_the_component_scope(self, monkeypatch):
        # A first-match resolve here machines a body in the wrong component and reports the same
        # count either way, so the name is refused - and the way out has to include this tool's own
        # input, not just the qualified spellings.
        install(cg, _shared_body_design()[0])
        op, res = self._run(monkeypatch, bodies=["Body1"])
        assert res["isError"] is True
        assert "names 2 bodies" in res["message"]
        assert "'Carrier:Body1'" in res["message"] and "'Bracket:1:Body1'" in res["message"]
        assert "'component'" in res["message"]
        assert op.parameters.itemByName("contours").value.applied == 0   # nothing was applied

    def test_the_component_scope_picks_that_components_body(self, monkeypatch):
        # The remedy the refusal names has to WORK, and it must land on the scoped component's own
        # body rather than on whichever the design-wide walk reached first.
        design, _root_body, proxy = _shared_body_design()
        install(cg, design)
        op, res = self._run(monkeypatch, bodies=["Body1"], component="Bracket")
        assert res["isError"] is False
        sel = _selection_of(op)
        assert sel.inputGeometry == [proxy] and sel.isSetupModelSelected is False

    def test_the_scope_reaches_pocket_recognition_as_well(self, monkeypatch):
        # Both body kinds read the same input through the same kind, so a scope that only narrowed
        # silhouette would leave the identical trap open one strategy over.
        design, _root_body, proxy = _shared_body_design()
        install(cg, design)
        op = _curve_op(name="Adaptive1")
        cam = _CAM([_Setup([op])])
        monkeypatch.setattr(cg, "get_cam", lambda: (cam, None))
        res = cg.handler(operation="Adaptive1", selection="pocket_recognition", bodies=["Body1"],
                         component="Bracket", allow_pocket_recognition=True, generate=False)
        assert res["isError"] is False
        assert _selection_of(op).inputGeometry == [proxy]

    def test_a_scope_the_design_does_not_hold_is_refused(self, monkeypatch):
        # An input a caller can get wrong without being told is a trap - and this scope is refused
        # even though a qualified name would have identified the body on its own.
        install(cg, _shared_body_design()[0])
        op, res = self._run(monkeypatch, bodies=["Carrier:Body1"], component="Ghost")
        assert res["isError"] is True and "No component named 'Ghost'" in res["message"]
        assert op.parameters.itemByName("contours").value.applied == 0

    def test_a_scope_with_NO_bodies_is_refused_rather_than_machining_the_setup_models(self, monkeypatch):
        # Omitting 'bodies' is the setup-models form (isSetupModelSelected), which no component
        # scope narrows - so a caller who asked to narrow to one component and would have got the
        # whole setup's models is told, not answered with a success that names no scope at all.
        install(cg, _shared_body_design()[0])
        op, res = self._run(monkeypatch, component="Bracket")
        assert res["isError"] is True
        assert "'component'" in res["message"] and "'bodies'" in res["message"]
        assert "No component named" not in res["message"]   # the scope exists; it applies to nothing
        pv = op.parameters.itemByName("contours").value
        assert pv.applied == 0 and pv.getCurveSelections().count == 0

    def test_a_scope_the_design_does_not_hold_with_NO_bodies_is_refused_too(self, monkeypatch):
        # An unresolvable scope beside an empty list is refused on the scope, not dropped: the
        # empty form selects the setup's own models, which no component narrows, so a dropped
        # scope would answer ok naming neither the unknown component nor what it selected.
        install(cg, _shared_body_design()[0])
        op, res = self._run(monkeypatch, component="Ghost")
        assert res["isError"] is True and "'component'" in res["message"]
        assert op.parameters.itemByName("contours").value.applied == 0

    def test_the_setup_models_form_still_resolves_with_no_scope(self, monkeypatch):
        # Only the scope BESIDE it is refused: omitting both is the form the tool description
        # advertises, and it still selects the setup's own models.
        install(cg, _shared_body_design()[0])
        op, res = self._run(monkeypatch)
        assert res["isError"] is False
        assert _selection_of(op).isSetupModelSelected is True


class TestPocketFilter:
    def _run(self, monkeypatch, flt, units="mm"):
        op = _curve_op(name="Adaptive1")
        cam = _CAM([_Setup([op])])
        _install_bodies(monkeypatch, cam, [_Body("Carrier")])
        res = cg.handler(operation="Adaptive1", selection="pocket_recognition", bodies=["Carrier"],
                         pocket_filter=flt, units=units, allow_pocket_recognition=True,
                         generate=False)
        return op, res

    def test_min_hole_diameter_without_holes_is_refused(self, monkeypatch):
        # minimumHoleDiameter can only be set while areHolesIncluded is true - refuse rather than
        # write a value the API will not keep.
        op, res = self._run(monkeypatch, {"min_hole_diameter": 2.5})
        assert res["isError"] is True and "holes=true" in res["message"]
        assert _selection_of(op).minimumHoleDiameter is None

    def test_holes_is_set_before_the_hole_diameter(self, monkeypatch):
        # areHolesIncluded GATES minimumHoleDiameter, so the flag has to be written first or the
        # bound never lands (the fake keeps its default when the order is wrong).
        op, res = self._run(monkeypatch, {"holes": True, "min_hole_diameter": 2.5})
        sel = _selection_of(op)
        assert res["isError"] is False
        assert sel.areHolesIncluded is True
        assert sel.minimumHoleDiameter == pytest.approx(0.25)      # 2.5 mm -> 0.25 cm internally
        assert sel.writes.index("areHolesIncluded") < sel.writes.index("minimumHoleDiameter")
        # published back in the units the caller spoke, not the internal cm
        assert _payload(res)["pocket_filter_applied"] == {"holes": True, "min_hole_diameter": 2.5}
        assert _payload(res)["pocket_filter_units"] == "mm"

    def test_every_length_lands_scaled_and_is_published(self, monkeypatch):
        op, res = self._run(monkeypatch, {"min_corner_radius": 1.0, "max_corner_radius": 8.0,
                                          "min_depth": 3.0, "max_depth": 40.0})
        sel = _selection_of(op)
        assert res["isError"] is False
        assert (sel.minimumCornerRadius, sel.maximumCornerRadius) == pytest.approx((0.1, 0.8))
        assert (sel.minimumPocketDepth, sel.maximumPocketDepth) == pytest.approx((0.3, 4.0))
        # the properties hold cm; the payload restates each read-back in the caller's mm
        out = _payload(res)
        assert out["pocket_filter_applied"] == {
            "min_corner_radius": 1.0, "max_corner_radius": 8.0,
            "min_depth": 3.0, "max_depth": 40.0}
        assert out["pocket_filter_units"] == "mm"

    def test_inch_units_scale_the_filter(self, monkeypatch):
        # 1 in lands as 2.54 cm on the property and is published back as 1.0 in - a payload that
        # echoed the internal number would tell an inch caller their 1" depth is 2.54".
        op, res = self._run(monkeypatch, {"min_depth": 1.0}, units="in")
        assert res["isError"] is False
        assert _selection_of(op).minimumPocketDepth == pytest.approx(2.54)
        out = _payload(res)
        assert out["pocket_filter_applied"] == {"min_depth": 1.0}
        assert out["pocket_filter_units"] == "in"

    def test_a_filter_value_the_selection_drops_is_an_error(self, monkeypatch):
        # min_hole_diameter is refused when holes is false; when holes is TRUE but the selection
        # still fails to keep the value, the read-back - not the written number - is what tells us.
        op = _curve_op(name="Adaptive1")
        cam = _CAM([_Setup([op])])
        _install_bodies(monkeypatch, cam, [_Body("Carrier")])
        pv = op.parameters.itemByName("contours").value
        def _deaf(kind):
            sel = _DeafHoleDiameter(kind)
            pv._cs._items.append(sel)
            return sel
        pv._cs._make = _deaf
        res = cg.handler(operation="Adaptive1", selection="pocket_recognition", bodies=["Carrier"],
                         pocket_filter={"holes": True, "min_hole_diameter": 2.5},
                         allow_pocket_recognition=True, generate=False)
        assert res["isError"] is True and "min_hole_diameter" in res["message"]
        assert pv.applied == 0

    def test_a_dropped_value_states_the_read_back_in_the_callers_units(self, monkeypatch):
        # the failure path publishes a number too: an inch caller who sent 1.0 and is told the
        # selection "reads back 5.08" is reading Fusion's internal cm, not their own units.
        op = _curve_op(name="Adaptive1")
        cam = _CAM([_Setup([op])])
        _install_bodies(monkeypatch, cam, [_Body("Carrier")])
        pv = op.parameters.itemByName("contours").value
        def _clamping(kind):
            sel = _ClampingDepth(kind)
            pv._cs._items.append(sel)
            return sel
        pv._cs._make = _clamping
        res = cg.handler(operation="Adaptive1", selection="pocket_recognition", bodies=["Carrier"],
                         pocket_filter={"min_depth": 1.0}, units="in",
                         allow_pocket_recognition=True, generate=False)
        assert res["isError"] is True and "min_depth" in res["message"]
        assert "reads back 2.0 in" in res["message"]      # 5.08 cm stated in the caller's inches
        assert "5.08" not in res["message"]
        assert pv.applied == 0

    def test_a_flag_only_filter_carries_no_units_key(self, monkeypatch):
        # 'holes' is a boolean - a units key beside it would state units for a value that has none.
        op, res = self._run(monkeypatch, {"holes": True})
        out = _payload(res)
        assert out["pocket_filter_applied"] == {"holes": True}
        assert "pocket_filter_units" not in out

    def test_unknown_filter_key_is_refused(self, monkeypatch):
        op, res = self._run(monkeypatch, {"min_taper": 3.0})
        assert res["isError"] is True and "min_taper" in res["message"]

    def test_non_numeric_length_is_refused(self, monkeypatch):
        op, res = self._run(monkeypatch, {"min_depth": "deep"})
        assert res["isError"] is True and "min_depth" in res["message"]

    def test_the_schema_advertises_exactly_the_keys_the_handler_reads(self):
        # The schema is generated from the same tables _apply_pocket_filter reads, so a key the
        # wire offers that the handler would refuse cannot exist - the typing replaces a prose
        # description that stated the key list without anything failing when it drifted.
        schema = cg.tool.input_schema["properties"]["pocket_filter"]
        assert tuple(schema["properties"]) == cg._POCKET_FILTER_KEYS
        assert schema["additionalProperties"] is False
        assert schema["properties"]["holes"]["type"] == "boolean"
        assert [schema["properties"][k]["type"] for k, _p in cg._POCKET_FILTER_LENGTHS] == \
            ["number"] * len(cg._POCKET_FILTER_LENGTHS)
        assert "needs holes=true" in schema["properties"]["min_hole_diameter"]["description"]

    def test_every_length_key_the_schema_advertises_is_one_the_handler_accepts(self, monkeypatch):
        # The other half: an advertised key the handler refuses would be a schema that lies.
        for key, _prop in cg._POCKET_FILTER_LENGTHS:
            flt = {"holes": True, key: 2.0} if key == "min_hole_diameter" else {key: 2.0}
            _op, res = self._run(monkeypatch, flt)
            assert res["isError"] is False, (key, res)
            assert key in json.loads(res["content"][0]["text"])["pocket_filter_applied"], (key, res)


# ── the sketch selection (whole sketches, by name) ───────────────────────────

def _sketch_design(*comps):
    root = comps[0]
    return MakeDesign(comp=root, all_components=list(comps))


class TestSketchSelection:
    def test_resolves_a_sketch_by_name(self, monkeypatch):
        sk = make_sketch("Pocket Outline")
        install(cg, _sketch_design(MakeComp("Root", sketches=[sk])))
        op = _curve_op()
        cam = _CAM([_Setup([op])])
        monkeypatch.setattr(cg, "get_cam", lambda: (cam, None))
        out = _payload(cg.handler(operation="2D Contour1", selection="sketch",
                                  sketches=["Pocket Outline"], generate=False))
        sel = _selection_of(op)
        assert sel.kind == "sketch" and sel.inputGeometry == [sk]
        assert out["selections"] == 1

    def test_duplicate_sketch_name_across_components_is_refused(self, monkeypatch):
        # Two components can each hold a "Sketch1", so a first-match resolve would silently machine
        # the wrong one. The refusal counts the SKETCHES it found and names where they live.
        root = MakeComp("Carrier", sketches=[make_sketch("Sketch1")])
        sub = MakeComp("Bracket", sketches=[make_sketch("Sketch1")])
        install(cg, _sketch_design(root, sub))
        op = _curve_op()
        cam = _CAM([_Setup([op])])
        monkeypatch.setattr(cg, "get_cam", lambda: (cam, None))
        res = cg.handler(operation="2D Contour1", selection="sketch", sketches=["Sketch1"],
                         generate=False)
        assert res["isError"] is True
        assert "2 sketches are named 'Sketch1'" in res["message"]
        # the shared refusal (_common.find_sketch) pairs each hit with its owning component
        assert "'Sketch1' in Carrier" in res["message"]
        assert "'Sketch1' in Bracket" in res["message"]
        assert "'sketches'[0]" in res["message"]   # and still names the input slot it came from
        # the way through is this tool's OWN input, not a rename in whichever document owns the
        # other sketch - the scope input the schema declares beside 'sketches'
        assert "'component'" in res["message"]
        assert "rename" not in res["message"].lower()
        assert op.parameters.itemByName("contours").value.applied == 0   # nothing was applied

    def test_the_component_scope_picks_that_components_sketch(self, monkeypatch):
        # The remedy the refusal above names has to WORK: with both components holding a 'Sketch1',
        # the scope decides which one is machined - and it is the scoped component's own object,
        # not whichever the design-wide walk reached first.
        mine = make_sketch("Sketch1")
        theirs = make_sketch("Sketch1")
        root = MakeComp("Carrier", sketches=[theirs])
        sub = MakeComp("Bracket", sketches=[mine])
        install(cg, _sketch_design(root, sub))
        op = _curve_op()
        cam = _CAM([_Setup([op])])
        monkeypatch.setattr(cg, "get_cam", lambda: (cam, None))
        out = _payload(cg.handler(operation="2D Contour1", selection="sketch", sketches=["Sketch1"],
                                  component="Bracket", generate=False))
        assert _selection_of(op).inputGeometry == [mine]
        assert out["selections"] == 1

    def test_the_scope_narrows_EVERY_name_in_the_list(self, monkeypatch):
        # A list resolves one name at a time, so a scope applied to the first element only would
        # machine one component's outline beside another's and report two selections either way.
        a_mine, b_mine = make_sketch("Inner"), make_sketch("Outer")
        root = MakeComp("Carrier", sketches=[make_sketch("Inner"), make_sketch("Outer")])
        sub = MakeComp("Bracket", sketches=[a_mine, b_mine])
        install(cg, _sketch_design(root, sub))
        op = _curve_op()
        cam = _CAM([_Setup([op])])
        monkeypatch.setattr(cg, "get_cam", lambda: (cam, None))
        _payload(cg.handler(operation="2D Contour1", selection="sketch",
                            sketches=["Inner", "Outer"], component="Bracket", generate=False))
        assert _selection_of(op).inputGeometry == [a_mine, b_mine]

    def test_a_scope_the_design_does_not_hold_is_refused(self, monkeypatch):
        # An input a caller can get wrong without being told is a trap: the name here IS unique, so
        # dropping the unusable scope would resolve and machine geometry the caller never scoped.
        install(cg, _sketch_design(MakeComp("Carrier", sketches=[make_sketch("Outline")])))
        op = _curve_op()
        cam = _CAM([_Setup([op])])
        monkeypatch.setattr(cg, "get_cam", lambda: (cam, None))
        res = cg.handler(operation="2D Contour1", selection="sketch", sketches=["Outline"],
                         component="Ghost", generate=False)
        assert res["isError"] is True
        assert "No component named 'Ghost'" in res["message"] and "Carrier" in res["message"]
        assert op.parameters.itemByName("contours").value.applied == 0

    def test_a_name_the_scoped_component_does_not_hold_names_where_it_lives(self, monkeypatch):
        # The scope decides which component answers, so a name it does not hold is a miss even
        # though the design carries exactly one sketch of that name - and the refusal names the
        # component that DOES hold it rather than offering a design-wide list the scope excluded.
        root = MakeComp("Carrier", sketches=[make_sketch("Outline")])
        sub = MakeComp("Bracket", sketches=[make_sketch("Inner")])
        install(cg, _sketch_design(root, sub))
        op = _curve_op()
        cam = _CAM([_Setup([op])])
        monkeypatch.setattr(cg, "get_cam", lambda: (cam, None))
        res = cg.handler(operation="2D Contour1", selection="sketch", sketches=["Outline"],
                         component="Bracket", generate=False)
        assert res["isError"] is True
        assert "Outline" in res["message"] and "Carrier" in res["message"]
        assert op.parameters.itemByName("contours").value.applied == 0

    def test_the_scope_is_refused_on_a_kind_that_reads_no_NAME(self, monkeypatch):
        # 'component' narrows whichever by-NAME list the selection kind reads (sketches or bodies),
        # so on a handle-driven kind it applies to nothing - dropping it would leave the caller
        # believing it scoped the cut. The refusal names every kind the scope DOES reach.
        op = _curve_op()
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Edge()])
        res = cg.handler(operation="2D Contour1", selection="chain", handles=["e"],
                         component="Bracket", generate=False)
        assert res["isError"] is True
        assert "'component'" in res["message"] and "'chain'" in res["message"]
        for kind in ("sketch", "silhouette", "pocket_recognition"):
            assert kind in res["message"]

    def test_unknown_sketch_lists_the_available_names(self, monkeypatch):
        install(cg, _sketch_design(MakeComp("Root", sketches=[make_sketch("Outline")])))
        op = _curve_op()
        cam = _CAM([_Setup([op])])
        monkeypatch.setattr(cg, "get_cam", lambda: (cam, None))
        res = cg.handler(operation="2D Contour1", selection="sketch", sketches=["Ghost"],
                         generate=False)
        assert res["isError"] is True and "Ghost" in res["message"] and "Outline" in res["message"]

    def test_sketch_selection_needs_a_sketch(self, monkeypatch):
        install(cg, _sketch_design(MakeComp("Root", sketches=[make_sketch("Outline")])))
        op = _curve_op()
        cam = _CAM([_Setup([op])])
        monkeypatch.setattr(cg, "get_cam", lambda: (cam, None))
        res = cg.handler(operation="2D Contour1", selection="sketch", generate=False)
        assert res["isError"] is True and "sketches" in res["message"]


# ── WHICH sketch / body the by-name reference resolved to ────────────────────

class TestSelectedIdentityIsPublished:
    """A count says a selection landed; it cannot say WHICH sketch or body it landed on. Both
    inputs address by NAME, so a wrong pick reads exactly like a right one unless the payload names
    what the reference resolved to."""

    def _sketch_run(self, monkeypatch, comps, names):
        install(cg, _sketch_design(*comps))
        op = _curve_op()
        cam = _CAM([_Setup([op])])
        monkeypatch.setattr(cg, "get_cam", lambda: (cam, None))
        return _payload(cg.handler(operation="2D Contour1", selection="sketch", sketches=names,
                                   generate=False))

    def _named_sketches(self, *names):
        """A root component holding a sketch per name, each answering its owning component."""
        sketches = [make_sketch(n) for n in names]
        root = MakeComp("Carrier", sketches=sketches)
        for sk in sketches:
            sk.parentComponent = root
        return root, sketches

    def test_a_selected_sketch_is_named_with_its_owning_component(self, monkeypatch):
        root, _ = self._named_sketches("Pocket Outline")
        out = self._sketch_run(monkeypatch, [root], ["Pocket Outline"])
        assert out["selected"] == "'Pocket Outline' in Carrier"

    def test_the_selected_sketches_are_named_one_per_entry(self, monkeypatch):
        root, _ = self._named_sketches("Inner", "Outer")
        out = self._sketch_run(monkeypatch, [root], ["Inner", "Outer"])
        assert out["selected"] == "'Inner' in Carrier, 'Outer' in Carrier"

    def test_a_selected_body_is_named_in_the_spelling_that_addresses_it_back(self, monkeypatch):
        # The qualified '<occurrence-or-component>:<body>' form is what the ambiguity refusal lists
        # and what this input resolves, so the payload's label is a string the caller can re-send.
        design, _root_body, _proxy = _shared_body_design()
        install(cg, design)
        op = _curve_op()
        cam = _CAM([_Setup([op])])
        monkeypatch.setattr(cg, "get_cam", lambda: (cam, None))
        out = _payload(cg.handler(operation="2D Contour1", selection="silhouette",
                                  bodies=["Body1"], component="Bracket", generate=False))
        assert out["selected"] == "'Bracket:1:Body1'"

    def test_the_setup_models_form_names_no_bodies(self, monkeypatch):
        # Nothing was selected by name there - 'setup_models_selected' is the whole answer, and an
        # empty label list would read as a selection that resolved to nothing.
        op = _curve_op()
        cam = _CAM([_Setup([op])])
        _install_bodies(monkeypatch, cam, [])
        out = _payload(cg.handler(operation="2D Contour1", selection="silhouette", generate=False))
        assert out["setup_models_selected"] is True and "selected" not in out

    def test_a_handle_driven_selection_names_nothing(self, monkeypatch):
        # A handle addresses one entity and the resolver refuses a token that names more than one,
        # so there is no by-name pick to disclose - and a face carries no name to publish.
        op = _curve_op()
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Edge(), _Edge()])
        out = _payload(cg.handler(operation="2D Contour1", selection="chain", chain_groups=[["a", "b"]],
                                  generate=False))
        assert out["selections"] == 1 and "selected" not in out

    def test_more_sketches_than_the_cap_counts_the_ones_it_did_not_name(self, monkeypatch):
        # The label list grows with the selection, so it is bounded by the shared renderer - and a
        # silently cut list reads as the complete set of what was machined.
        cap = cg._inputs._common._MAX_NAMED_CANDIDATES
        root, sketches = self._named_sketches(*[f"S{i:02d}" for i in range(cap + 1)])
        out = self._sketch_run(monkeypatch, [root], [s.name for s in sketches])
        assert out["selected"].count(" in Carrier") == cap
        assert f"'S{cap:02d}'" not in out["selected"]
        assert "(+1 more not listed)" in out["selected"]

    def test_every_sketch_AT_the_cap_is_named_and_nothing_is_counted(self, monkeypatch):
        cap = cg._inputs._common._MAX_NAMED_CANDIDATES
        root, sketches = self._named_sketches(*[f"S{i:02d}" for i in range(cap)])
        out = self._sketch_run(monkeypatch, [root], [s.name for s in sketches])
        assert out["selected"].count(" in Carrier") == cap
        assert f"'S{cap - 1:02d}' in Carrier" in out["selected"]
        assert "not listed" not in out["selected"]


# ── per-kind knobs ───────────────────────────────────────────────────────────

class TestKnobs:
    def test_loop_and_side_type_land_on_a_face_selection(self, monkeypatch):
        op = _curve_op(name="Face1")
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Face()])
        out = _payload(cg.handler(operation="Face1", selection="face", handles=["f"],
                                  loop_type="outside", side_type="always_inside", generate=False))
        sel = _selection_of(op)
        assert sel.loopType is cg.adsk.cam.LoopTypes.OnlyOutsideLoops
        assert sel.sideType is cg.adsk.cam.SideTypes.AlwaysInsideSideType
        assert out["loop_type"] == "outside" and out["side_type"] == "always_inside"

    def test_loop_type_on_holes_is_refused(self, monkeypatch):
        # A drill's object-list selection has no loopType at all - dropping the knob silently would
        # leave the caller believing an option applied.
        op = _drill_op()
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Face(0.3)])
        res = cg.handler(operation="Drill1", selection="holes", handles=["a"], loop_type="outside",
                         generate=False)
        assert res["isError"] is True and "loop_type" in res["message"] and "holes" in res["message"]
        assert op.parameters.itemByName("holeFaces").value.value == []   # refused before any write

    def test_chain_knob_on_a_pocket_selection_is_refused(self, monkeypatch):
        op = _curve_op(name="2D Pocket1")
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Face()])
        res = cg.handler(operation="2D Pocket1", selection="pocket", handles=["f"], is_open=True,
                         generate=False)
        assert res["isError"] is True and "is_open" in res["message"]

    def test_pocket_filter_on_a_silhouette_is_refused(self, monkeypatch):
        op = _curve_op()
        cam = _CAM([_Setup([op])])
        _install_bodies(monkeypatch, cam, [])
        res = cg.handler(operation="2D Contour1", selection="silhouette",
                         pocket_filter={"holes": True}, generate=False)
        assert res["isError"] is True and "pocket_filter" in res["message"]

    def test_bad_loop_type_value_is_refused(self, monkeypatch):
        op = _curve_op(name="Face1")
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Face()])
        res = cg.handler(operation="Face1", selection="face", handles=["f"], loop_type="sideways",
                         generate=False)
        assert res["isError"] is True and "sideways" in res["message"]

    def test_a_knob_that_does_not_stick_is_an_error(self, monkeypatch):
        # A SWIG proxy accepts an assignment to a property it does not define; only the read-back
        # reveals it, so a knob that reads back unchanged must fail the call.
        op = _curve_op(name="Face1")
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Face()])
        pv = op.parameters.itemByName("contours").value
        def _inert(kind):
            sel = _InertLoopType(kind)
            pv._cs._items.append(sel)
            return sel
        pv._cs._make = _inert
        res = cg.handler(operation="Face1", selection="face", handles=["f"],
                         loop_type="outside", generate=False)
        assert res["isError"] is True and "loop_type" in res["message"]
        assert pv.applied == 0                      # never applied on an unset knob


# ── read-back off the applied selection ──────────────────────────────────────

class TestReadBack:
    def _apply(self, monkeypatch, prepare):
        op = _curve_op(name="Face1")
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Face(), _Face()])
        pv = op.parameters.itemByName("contours").value
        real_make = pv._cs._make
        def _prepared(kind):
            sel = real_make(kind)
            prepare(sel)
            return sel
        pv._cs._make = _prepared
        return cg.handler(operation="Face1", selection="face", handles=["a", "b"], generate=False)

    def test_reports_what_fusion_resolved(self, monkeypatch):
        def prep(sel):
            sel.outputGeometry = [_Path(3), _Path(5)]
            sel._value = [object()] * 7        # value can EXCEED the input (same-plane expansion)
        out = _payload(self._apply(monkeypatch, prep))
        assert out["resolved"] == {"curve_paths": 2, "curve_segments": 8, "entities": 7}

    def test_zero_resolved_paths_are_reported_not_hidden(self, monkeypatch):
        out = _payload(self._apply(monkeypatch, lambda sel: None))
        assert out["resolved"]["curve_paths"] == 0 and out["resolved"]["curve_segments"] == 0
        assert out["resolved"]["entities"] == 2          # value mirrors the input geometry

    def test_selection_error_after_apply_is_an_error_carrying_fusions_reason(self, monkeypatch):
        # The selection's own error channel is populated by applyCurveSelections; reporting ok
        # because the COLLECTION count is nonzero would swallow a rejected selection.
        def prep(sel):
            sel.hasError = True
            sel.error = "The selected geometry is not planar."
        res = self._apply(monkeypatch, prep)
        assert res["isError"] is True
        assert "not planar" in res["message"] and "face" in res["message"]
        # The collection was cleared before this selection was built, so the operation is NOT back on
        # what it held before the call - the refusal has to say so.
        assert "cleared" in res["message"] and "select its geometry again" in res["message"]

    def test_selection_warning_is_surfaced_without_failing(self, monkeypatch):
        def prep(sel):
            sel.hasWarning = True
            sel.warning = "Some contours were skipped."
        out = _payload(self._apply(monkeypatch, prep))
        assert out["selection_warning"] == "Some contours were skipped."


# ── holes family + diameter filter ───────────────────────────────────────────

class TestHoles:
    def test_holes_sets_holefaces_directly(self, monkeypatch):
        op = _drill_op()
        cam = _CAM([_Setup([op])])
        faces = [_Face(0.3), _Face(0.3), _Face(0.5)]   # Ø6,Ø6,Ø10 (cm radius)
        _install(monkeypatch, cam, faces)
        out = _payload(cg.handler(operation="Drill1", selection="holes", handles=["a","b","c"],
                                  generate=False))
        assert op.parameters.itemByName("holeFaces").value.value == faces
        assert out["selections"] == 3

    def test_diameter_filter_keeps_in_range(self, monkeypatch):
        op = _drill_op()
        cam = _CAM([_Setup([op])])
        faces = [_Face(0.3), _Face(0.3), _Face(0.3), _Face(0.3), _Face(0.5), _Face(0.5)]  # 4×Ø6, 2×Ø10
        _install(monkeypatch, cam, faces)
        out = _payload(cg.handler(operation="Drill1", selection="holes", handles=["a"]*6,
                                  min_diameter=5.5, max_diameter=6.5, generate=False))
        assert len(op.parameters.itemByName("holeFaces").value.value) == 4   # only the Ø6
        assert out["selections"] == 4 and "diameter_filter" in out

    def test_diameter_filter_empty_is_error(self, monkeypatch):
        op = _drill_op()
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Face(0.5), _Face(0.5)])    # both Ø10, filter for Ø6 -> none
        res = cg.handler(operation="Drill1", selection="holes", handles=["a","b"],
                         min_diameter=5.5, max_diameter=6.5, generate=False)
        assert res["isError"] is True and "diameter filter" in res["message"].lower()

    def test_filtered_to_zero_leaves_the_heights_UNTOUCHED(self, monkeypatch):
        # The filter reads the passed faces only, so it decides before anything is written: a call
        # refused here must leave the operation exactly as found. (Measured live: heights written
        # first were RETAINED by this refusal - 0 mm -> 7 mm on a Drill op that returned isError.)
        op = _drill_op_with_heights()
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Face(0.5), _Face(0.5)])    # both Ø10, filter for Ø6 -> none
        res = cg.handler(operation="Drill1", selection="holes", handles=["a", "b"],
                         min_diameter=5.5, max_diameter=6.5, top_offset="7 mm", generate=False)
        assert res["isError"] is True and "diameter filter" in res["message"].lower()
        # both still hold the expression they were found with - no write landed
        assert op.parameters.itemByName("topHeight_offset").expression == "0 mm"
        assert op.parameters.itemByName("topHeight_mode").expression == "0 mm"

    def test_holes_on_nonhole_op_errors(self, monkeypatch):
        op = _curve_op()                # neither holeFaces nor circularFaces
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Face(0.3)])
        res = cg.handler(operation="2D Contour1", selection="holes", handles=["a"], generate=False)
        assert res["isError"] is True
        assert "holeFaces" in res["message"] and "circularFaces" in res["message"]

    def test_holes_on_bore_uses_circularFaces(self, monkeypatch):
        # bore/circular have 'circularFaces' (no 'holeFaces') — the holes mode must drive it
        op = _bore_op()
        cam = _CAM([_Setup([op])])
        faces = [_Face(0.6), _Face(0.6)]
        _install(monkeypatch, cam, faces)
        out = _payload(cg.handler(operation="Bore1", selection="holes", handles=["a", "b"],
                                  generate=False))
        assert op.parameters.itemByName("circularFaces").value.value == faces
        assert out["selections"] == 2

    def test_holes_prefers_holeFaces_when_both_absent_irrelevant(self, monkeypatch):
        # a drill op (only holeFaces) still works — holeFaces is probed first
        op = _drill_op()
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Face(0.4)])
        out = _payload(cg.handler(operation="Drill1", selection="holes", handles=["a"], generate=False))
        assert len(op.parameters.itemByName("holeFaces").value.value) == 1 and out["selections"] == 1

    def test_a_hole_set_that_keeps_fewer_faces_than_assigned_is_an_error(self, monkeypatch):
        # the direct family shares one assign-then-read-back; a set that silently drops faces would
        # otherwise report the drill as selected while it machines a subset of the holes asked for.
        op = _drill_op()
        cam = _CAM([_Setup([op])])
        op.parameters.itemByName("holeFaces").value = _ShortSurfaceParam()
        _install(monkeypatch, cam, [_Face(0.3), _Face(0.3), _Face(0.3)])
        res = cg.handler(operation="Drill1", selection="holes", handles=["a", "b", "c"],
                         generate=False)
        assert res["isError"] is True and "did not take" in res["message"]
        assert "3 face(s) were assigned" in res["message"] and "reads back 1" in res["message"]


# ── the turning object sets (groove positions / thread faces) ────────────────
#
# MEASURED on the hub: 'grooves' takes the circular EDGE bounding the groove and RAISES
# '2 : InternalValidationError : status.isOk()' on that groove's own cylinder face, while
# 'threadFaces' takes the thread's cylinder face. Both read CadObjectParameterValue, so the class
# does not say which type - only the required handle type keeps a caller off the refused one.


def _groove_op(name="Single Groove1", **kw):
    return _Op(name, {"grooves": _Param(_HoleParamValue())}, **kw)


def _turn_thread_op(name="Thread1", **kw):
    return _Op(name, {"threadFaces": _Param(_HoleParamValue())}, **kw)


def _turn_chamfer_op(name="Chamfer1", **kw):
    return _Op(name, {"chamfers": _Param(_HoleParamValue())}, **kw)


class TestTurningObjectSets:
    def test_groove_lands_on_the_grooves_parameter(self, monkeypatch):
        op = _groove_op()
        cam = _CAM([_Setup([op])])
        edges = [_Edge()]
        _install(monkeypatch, cam, edges)
        out = _payload(cg.handler(operation="Single Groove1", selection="groove", handles=["e"],
                                  generate=False))
        assert op.parameters.itemByName("grooves").value.value == edges
        assert out["selections"] == 1 and out["selection_param"] == "grooves"

    def test_thread_lands_on_the_thread_faces_parameter(self, monkeypatch):
        op = _turn_thread_op()
        cam = _CAM([_Setup([op])])
        faces = [_Face(1.4911)]
        _install(monkeypatch, cam, faces)
        out = _payload(cg.handler(operation="Thread1", selection="thread", handles=["f"],
                                  generate=False))
        assert op.parameters.itemByName("threadFaces").value.value == faces
        assert out["selections"] == 1 and out["selection_param"] == "threadFaces"

    def test_chamfer_lands_on_the_chamfers_parameter(self, monkeypatch):
        # MEASURED on the hub's turning setup: the chamfer's bounding circular EDGE on 'chamfers'
        # generated 0.3 s of toolpath, where that chamfer's own cone FACE raised
        # '2 : InternalValidationError : status.isOk()' - the same split 'grooves' reads.
        op = _turn_chamfer_op()
        cam = _CAM([_Setup([op])])
        edges = [_Edge()]
        _install(monkeypatch, cam, edges)
        out = _payload(cg.handler(operation="Chamfer1", selection="chamfer", handles=["e"],
                                  generate=False))
        assert op.parameters.itemByName("chamfers").value.value == edges
        assert out["selections"] == 1 and out["selection_param"] == "chamfers"

    def test_chamfer_refuses_a_face_handle_before_the_parameter_raises(self, monkeypatch):
        op = _turn_chamfer_op()
        cam = _CAM([_Setup([op])])
        monkeypatch.setattr(cg, "get_cam", lambda: (cam, None))
        import adsk.fusion
        adsk.fusion.BRepFace = BRepFace
        adsk.fusion.BRepEdge = BRepEdge
        monkeypatch.setattr(cg._inputs, "_resolve_token_entity", lambda des, h: _Face(2.2))
        monkeypatch.setattr(cg._inputs._common, "design", lambda: object())
        res = cg.handler(operation="Chamfer1", selection="chamfer", handles=["h"], generate=False)
        assert res["isError"] is True and "must be an edge" in res["message"]
        assert op.parameters.itemByName("chamfers").value.value == []

    def test_groove_refuses_a_face_handle_before_the_parameter_raises(self, monkeypatch):
        # the groove parameter itself raises on a face, and a raise there reads as a tool fault
        # rather than a wrong pick - so the required type refuses it while nothing has been written.
        op = _groove_op()
        cam = _CAM([_Setup([op])])
        monkeypatch.setattr(cg, "get_cam", lambda: (cam, None))
        import adsk.fusion
        adsk.fusion.BRepFace = BRepFace
        adsk.fusion.BRepEdge = BRepEdge
        monkeypatch.setattr(cg._inputs, "_resolve_token_entity", lambda des, h: _Face(2.2))
        monkeypatch.setattr(cg._inputs._common, "design", lambda: object())
        res = cg.handler(operation="Single Groove1", selection="groove", handles=["h"],
                         generate=False)
        assert res["isError"] is True and "must be an edge" in res["message"]
        assert op.parameters.itemByName("grooves").value.value == []

    def test_thread_refuses_an_edge_handle(self, monkeypatch):
        op = _turn_thread_op()
        cam = _CAM([_Setup([op])])
        monkeypatch.setattr(cg, "get_cam", lambda: (cam, None))
        import adsk.fusion
        adsk.fusion.BRepFace = BRepFace
        adsk.fusion.BRepEdge = BRepEdge
        monkeypatch.setattr(cg._inputs, "_resolve_token_entity", lambda des, h: _Edge())
        monkeypatch.setattr(cg._inputs._common, "design", lambda: object())
        res = cg.handler(operation="Thread1", selection="thread", handles=["h"], generate=False)
        assert res["isError"] is True and "must be a face" in res["message"]
        assert op.parameters.itemByName("threadFaces").value.value == []

    def test_a_kind_the_operation_carries_no_parameter_for_names_that_parameter(self, monkeypatch):
        # a milling op met by selection='groove' has no groove positions at all, so the refusal
        # names the parameter looked for and the family it belongs to.
        op = _curve_op()
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Edge()])
        res = cg.handler(operation="2D Contour1", selection="groove", handles=["e"], generate=False)
        assert res["isError"] is True
        assert "grooves" in res["message"] and "turning groove strategy" in res["message"]


# ── the probing object set ──────────────────────────────────────────────────
#
# MEASURED: probe and probe_geometry both carry probe_selection, a CadObjectParameterValue that
# takes BRep FACES - the plane, boss, bore or web probingType 'probing-unknown' derives from.


def _probe_op(name="Probe WCS1", **kw):
    return _Op(name, {"probe_selection": _Param(_HoleParamValue())}, **kw)


class TestProbeSelection:
    def test_probe_lands_on_the_probe_selection_parameter(self, monkeypatch):
        op = _probe_op()
        cam = _CAM([_Setup([op])])
        faces = [_Face()]
        _install(monkeypatch, cam, faces)
        out = _payload(cg.handler(operation="Probe WCS1", selection="probe", handles=["f"],
                                  generate=False))
        assert op.parameters.itemByName("probe_selection").value.value == faces
        assert out["selections"] == 1 and out["selection_param"] == "probe_selection"

    def test_a_curve_kind_on_a_probe_op_names_probe_selection_as_the_remedy(self, monkeypatch):
        # a probing op carries no curve parameter, so 'face' reaches the curve refusal - which has
        # to hand back the probe set it read off the operation, not a drilling remedy.
        op = _probe_op()
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Face()])
        res = cg.handler(operation="Probe WCS1", selection="face", handles=["f"], generate=False)
        assert res["isError"] is True
        assert "'probe_selection'" in res["message"] and "selection='probe'" in res["message"]
        assert "holes" not in res["message"]

    def test_probe_on_an_op_without_the_parameter_is_refused(self, monkeypatch):
        op = _curve_op()
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Face()])
        res = cg.handler(operation="2D Contour1", selection="probe", handles=["f"], generate=False)
        assert res["isError"] is True
        assert "probe_selection" in res["message"] and "probe_geometry" in res["message"]


def _orientation_op(name="3+2 Roughing1", mode=None, editable=True, **kw):
    """A three_plus_two op: machiningDirections beside toolAxisMode, the mode that decides whether
    the orientation faces are read at all. A fresh op reads 'manual' and isEditable true (measured);
    `flat` carries the same pair reading isEditable false."""
    return _Op(name, {"machiningDirections": _Param(_HoleParamValue(), editable=editable),
                      "toolAxisMode": mode if mode is not None else _BoundaryModeParam("manual")},
               **kw)


class TestOrientationSelection:
    """three_plus_two takes its tool-axis orientations as FACES on machiningDirections (measured:
    CadObjectParameterValue, isEditable true); the faces are not machined, their normals become the
    tool axes, and they count only while toolAxisMode reads 'manual'."""

    def test_orientation_lands_on_the_machining_directions_parameter(self, monkeypatch):
        op = _orientation_op()
        cam = _CAM([_Setup([op])])
        faces = [_Face()]
        _install(monkeypatch, cam, faces)
        out = _payload(cg.handler(operation="3+2 Roughing1", selection="orientation",
                                  handles=["f"], generate=False))
        assert op.parameters.itemByName("machiningDirections").value.value == faces
        assert out["selections"] == 1 and out["selection_param"] == "machiningDirections"

    def test_orientation_engages_the_tool_axis_mode_and_publishes_it(self, monkeypatch):
        # faces on machiningDirections are INERT unless toolAxisMode reads 'manual', so the call
        # that lands them engages that mode and says so - the same step swarf and the 3D boundary
        # take. Landing them under any other mode would report a selection that changes nothing.
        op = _orientation_op(mode=_BoundaryModeParam("tilt"))
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Face()])
        out = _payload(cg.handler(operation="3+2 Roughing1", selection="orientation",
                                  handles=["f"], generate=False))
        assert cg.unquote_expression(
            op.parameters.itemByName("toolAxisMode").expression) == "manual"
        assert out["tool_axis_engaged"] is True and out["tool_axis_mode"] == "manual"

    def test_a_tool_axis_mode_that_will_not_take_is_an_error(self, monkeypatch):
        # a swallowed toolAxisMode leaves the orientations unread while the assignment succeeded,
        # so the call fails rather than reporting an engage, naming the value still read.
        op = _orientation_op(mode=_DeafBoundaryMode("tilt"))
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Face()])
        res = cg.handler(operation="3+2 Roughing1", selection="orientation", handles=["f"],
                         generate=False)
        assert res["isError"] is True
        assert "toolAxisMode" in res["message"] and "did not take" in res["message"]
        assert "tilt" in res["message"]

    def test_an_op_without_the_mode_parameter_is_disclosed_not_claimed_engaged(self, monkeypatch):
        op = _Op("3+2 Roughing1", {"machiningDirections": _Param(_HoleParamValue())})
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Face()])
        res = cg.handler(operation="3+2 Roughing1", selection="orientation", handles=["f"],
                         generate=False)
        assert res["isError"] is True
        assert "toolAxisMode" in res["message"] and "cannot confirm" in res["message"]

    def test_a_non_editable_orientation_set_is_refused_before_any_assignment(self, monkeypatch):
        # `flat` carries machiningDirections reading isEditable false (measured), and such a set
        # silently DROPS the write - so it is refused up front rather than assigned and reported
        # through a read-back count.
        op = _orientation_op(name="Flat1", editable=False)
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Face()])
        res = cg.handler(operation="Flat1", selection="orientation", handles=["f"], generate=False)
        assert res["isError"] is True
        assert "did not read isEditable true" in res["message"]
        assert op.parameters.itemByName("machiningDirections").value.value == []

    def test_a_non_editable_set_is_not_advertised_as_the_remedy(self, monkeypatch):
        # the curve refusal hands back remedies, so advertising a set the operation would silently
        # drop sends the caller to a call that cannot work. The op carries NO curve parameter, so
        # the refusal that composes those remedies is the one this exercises.
        op = _Op("Flat1", {"machiningDirections": _Param(_HoleParamValue(), editable=False)})
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Edge()])
        res = cg.handler(operation="Flat1", selection="chain", handles=["e"], generate=False)
        assert res["isError"] is True and "no curve-selection parameter" in res["message"]
        assert "selection='orientation'" not in res["message"]
        assert "did not read isEditable true" in res["message"]

    def test_orientation_does_not_land_on_a_surface_set(self, monkeypatch):
        # the orientation faces are directions, not geometry to cut, so an op carrying drive
        # surfaces but no orientations must be refused rather than have them machined as surfaces.
        op = _geodesic_op()
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Face()])
        res = cg.handler(operation="Geodesic1", selection="orientation", handles=["f"],
                         generate=False)
        assert res["isError"] is True and "machiningDirections" in res["message"]
        assert op.parameters.itemByName("driveSurfaces").value.value == []


# ── heights ──────────────────────────────────────────────────────────────────

class TestHeights:
    def test_sets_mode_and_offset(self, monkeypatch):
        op = _curve_op()
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Edge()])
        out = _payload(cg.handler(operation="2D Contour1", selection="chain", handles=["h"],
                                  bottom_mode="from contour", bottom_offset="-10 mm", generate=False))
        assert op.parameters.itemByName("bottomHeight_mode").expression == "from contour"
        assert op.parameters.itemByName("bottomHeight_offset").expression == "-10 mm"
        assert any("bottomHeight" in s for s in out["heights_set"])

    def test_heights_set_before_selection(self, monkeypatch):
        # ordering matters live: a height _mode's valid enum is context-dependent and applying the
        # selection can transiently invalidate it. So heights must be set BEFORE the selection applies.
        order = []
        op = _curve_op()
        pv = op.parameters.itemByName("contours").value
        real_apply = pv.applyCurveSelections
        def tracked_apply(cs):
            order.append("selection")
            return real_apply(cs)
        pv.applyCurveSelections = tracked_apply
        mode_param = op.parameters.itemByName("bottomHeight_mode")
        class _Tracking:
            def __init__(self, p): self._p = p
            @property
            def expression(self): return self._p.expression
            @expression.setter
            def expression(self, v):
                order.append("height"); self._p.expression = v
        op.parameters.swap("bottomHeight_mode", _Tracking(mode_param))
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Edge()])
        cg.handler(operation="2D Contour1", selection="chain", handles=["h"],
                   bottom_mode="from contour", generate=False)
        assert order.index("height") < order.index("selection")

    def test_a_failure_after_the_height_write_names_what_it_left_behind(self, monkeypatch):
        # Heights are set while the op is settled and this call never undoes them, so a refusal that
        # can only be decided AFTER that write has to say the heights REMAIN - an isError the caller
        # reads as "nothing happened" is the false part.
        op = _curve_op()
        pv = op.parameters.itemByName("contours").value
        pv.applyCurveSelections = lambda cs: setattr(pv, "_cs", _CurveSelections())   # -> 0 selections
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Edge()])
        res = cg.handler(operation="2D Contour1", selection="chain", handles=["h"],
                         top_offset="7 mm", generate=False)
        assert res["isError"] is True and "0 selection" in res["message"]
        assert "topHeight_offset=7 mm" in res["message"] and "REMAIN" in res["message"]

    def test_a_clean_refusal_before_any_height_write_says_nothing_about_heights(self, monkeypatch):
        # the disclosure is conditional: no height was requested, so no retained-mutation sentence
        op = _curve_op()
        pv = op.parameters.itemByName("contours").value
        pv.applyCurveSelections = lambda cs: setattr(pv, "_cs", _CurveSelections())
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Edge()])
        res = cg.handler(operation="2D Contour1", selection="chain", handles=["h"], generate=False)
        assert res["isError"] is True and "REMAIN" not in res["message"]

    def test_a_half_applied_height_group_names_the_half_that_landed(self, monkeypatch):
        # top lands, bottom's param is absent -> the error names the top write it kept
        op = _curve_op()
        op.parameters.drop("bottomHeight_offset")
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Edge()])
        res = cg.handler(operation="2D Contour1", selection="chain", handles=["h"],
                         top_offset="7 mm", bottom_offset="-10 mm", generate=False)
        assert res["isError"] is True and "bottomHeight_offset" in res["message"]
        assert "topHeight_offset=7 mm" in res["message"] and "REMAIN" in res["message"]

    def test_missing_height_param_errors(self, monkeypatch):
        op = _curve_op()
        op.parameters.drop("topHeight_offset")        # simulate an op without that height
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Edge()])
        res = cg.handler(operation="2D Contour1", selection="chain", handles=["h"],
                         top_offset="0 mm", generate=False)
        assert res["isError"] is True and "topHeight_offset" in res["message"]


# ── heights: what the payload states as SET is the parameter's own re-read ───
#
# An ungated height write is an echo: heights_set built from the request strings states a value
# nothing read. A height that stores but does not evaluate, or that the operation simply keeps out,
# is indistinguishable from an applied one until the parameter is read back.


class _StuckHeightParam(_Param):
    """Accepts the expression assignment and keeps the one it already held."""
    def __init__(self, prior):
        super().__init__(None)
        object.__setattr__(self, "_prior", prior)

    @property
    def expression(self):
        return self._prior

    @expression.setter
    def expression(self, _v):
        pass


class _ThirdValueHeightParam(_Param):
    """Takes the expression and stores one of its OWN - neither the expression it held nor the one
    written. A gate keyed on 'it kept the prior expression' passes this write."""
    def __init__(self):
        super().__init__(None)

    @property
    def expression(self):
        return self.__dict__.get("_expr")

    @expression.setter
    def expression(self, v):
        self.__dict__["_expr"] = f"{v} mm"


class _QuotedStoreHeightParam(_Param):
    """Starts holding nothing and stores the SINGLE-QUOTED form of whatever is written - the shape a
    CAM string parameter's stored expression carries (receipt cam-parameter-expressions reads
    'context' and 'strategy' back starting with a quote). Holding nothing, it takes the request
    unwrapped, so its read-back differs from what was written by the wrapper alone."""
    def __init__(self):
        super().__init__(None)
        self.__dict__["_expr"] = None      # holding nothing yet - not the quoted spelling of None

    @property
    def expression(self):
        return self.__dict__.get("_expr")

    @expression.setter
    def expression(self, v):
        self.__dict__["_expr"] = "'" + str(v) + "'"


class _UnevaluatedHeightParam(_Param):
    """Stores the expression verbatim and reports the failure ONLY through .error - the CAM
    parameter store's shape (see _cam_common.expression_error)."""
    @property
    def error(self):
        return "Failed to evaluate expression."


class _UnreadableHeightParam:
    """Takes the expression; nothing reads back off it afterwards."""
    def __init__(self):
        self._written = False
        self.value = None

    @property
    def expression(self):
        if self._written:
            raise RuntimeError("expression is unreadable")
        return None

    @expression.setter
    def expression(self, _v):
        self._written = True


class TestHeightReadBack:
    def test_a_height_the_operation_keeps_out_is_an_error(self, monkeypatch):
        op = _curve_op()
        op.parameters.swap("bottomHeight_mode", _StuckHeightParam("from stock top"))
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Edge()])
        res = cg.handler(operation="2D Contour1", selection="chain", handles=["h"],
                         bottom_mode="from contour", generate=False)
        assert res["isError"] is True and "did not take" in res["message"]
        assert "bottomHeight_mode" in res["message"] and "from stock top" in res["message"]

    def test_a_height_that_lands_as_a_THIRD_value_is_an_error_naming_it(self, monkeypatch):
        # the gate is "the read-back is the expression written" (live-verified receipt
        # cam-parameter-expressions), so a store keeping neither the prior expression nor the
        # request is caught too - and the error states the value the operation actually reads.
        op = _curve_op()
        op.parameters.swap("bottomHeight_offset", _ThirdValueHeightParam())
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Edge()])
        res = cg.handler(operation="2D Contour1", selection="chain", handles=["h"],
                         bottom_offset="-10", generate=False)
        assert res["isError"] is True and "did not take" in res["message"]
        assert "reads back '-10 mm'" in res["message"]

    def test_heights_set_publishes_what_the_operation_reads_back(self, monkeypatch):
        # the payload key states the height the operation will run on, read off the parameter after
        # the write - the gate above is what makes that the same string as the request.
        op = _curve_op()
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Edge()])
        out = _payload(cg.handler(operation="2D Contour1", selection="chain", handles=["h"],
                                  bottom_offset="-10 mm", generate=False))
        assert out["heights_set"] == ["bottomHeight_offset=-10 mm"]

    def test_a_height_that_does_not_evaluate_is_an_error(self, monkeypatch):
        # the expression is STORED and echoed back - only .error exposes that it resolves to nothing
        op = _curve_op()
        op.parameters.swap("topHeight_offset", _UnevaluatedHeightParam(None))
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Edge()])
        res = cg.handler(operation="2D Contour1", selection="chain", handles=["h"],
                         top_offset="NoSuchParam * 2", generate=False)
        assert res["isError"] is True and "did not evaluate" in res["message"]
        assert "Failed to evaluate expression." in res["message"]

    def test_a_height_that_cannot_be_read_back_is_unconfirmed(self, monkeypatch):
        op = _curve_op()
        op.parameters.swap("topHeight_mode", _UnreadableHeightParam())
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Edge()])
        res = cg.handler(operation="2D Contour1", selection="chain", handles=["h"],
                         top_mode="from stock top", generate=False)
        assert res["isError"] is True and "UNCONFIRMED" in res["message"]

    def test_a_mode_that_lands_before_a_refused_offset_is_named_as_retained(self, monkeypatch):
        # both halves belong to ONE height group: the mode already landed when the offset is
        # refused, and an error that named neither would read as "nothing happened".
        op = _curve_op()
        op.parameters.swap("bottomHeight_offset", _StuckHeightParam("0 mm"))
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Edge()])
        res = cg.handler(operation="2D Contour1", selection="chain", handles=["h"],
                         bottom_mode="from contour", bottom_offset="-10 mm", generate=False)
        assert res["isError"] is True and "did not take" in res["message"]
        assert "bottomHeight_mode=from contour" in res["message"] and "REMAIN" in res["message"]

    def test_a_store_that_quotes_the_request_is_not_called_a_no_take(self, monkeypatch):
        # What the receipt measured is two-sided: a numeric parameter's expression reads back the
        # text written, and a STRING parameter's stored expression is single-quoted. A height _mode
        # is a string parameter written here unquoted, so the compare goes through the shared codec
        # - a byte compare would refuse this whole call and call a landed write a no-take.
        op = _curve_op()
        op.parameters.swap("bottomHeight_mode", _QuotedStoreHeightParam())
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Edge()])
        out = _payload(cg.handler(operation="2D Contour1", selection="chain", handles=["h"],
                                  bottom_mode="from contour", generate=False))
        # heights_set publishes the parameter's OWN read-back, wrapper and all
        assert out["heights_set"] == ["bottomHeight_mode='from contour'"]

    def test_a_quoting_store_that_keeps_its_prior_expression_is_still_an_error(self, monkeypatch):
        # The codec strips the wrapper, not the comparison: a store that quotes AND keeps the
        # expression it already held is the swallowed write, and it stays convicted.
        op = _curve_op()
        op.parameters.swap("bottomHeight_mode", _StuckHeightParam("'from stock top'"))
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Edge()])
        res = cg.handler(operation="2D Contour1", selection="chain", handles=["h"],
                         bottom_mode="from contour", generate=False)
        assert res["isError"] is True and "did not take" in res["message"]
        assert "from stock top" in res["message"]

    def test_a_height_set_to_what_it_already_reads_is_not_called_a_no_take(self, monkeypatch):
        # the gate is "the read-back is the expression written", and a caller re-asserting the value
        # the operation already carries reads it back - the state they asked for.
        op = _curve_op()
        op.parameters.swap("bottomHeight_mode", _StuckHeightParam("from contour"))
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Edge()])
        out = _payload(cg.handler(operation="2D Contour1", selection="chain", handles=["h"],
                                  bottom_mode="from contour", generate=False))
        assert out["heights_set"] == ["bottomHeight_mode=from contour"]


# ── generation: launch-and-return (it runs in the background on its own) ─────

class TestGenerate:
    def test_returns_immediately_without_pumping_while_future_incomplete(self, monkeypatch):
        # The handler LAUNCHES generation and returns - it must never pump adsk.doEvents waiting on
        # the future (that blocks Fusion's UI for the whole compute; cam_get_status owns the pump).
        # An INCOMPLETE future proves it: a wait loop would spin its full time budget here.
        import adsk
        pumps = []
        monkeypatch.setattr(adsk, "doEvents", lambda: pumps.append(1), raising=False)
        op = _curve_op()
        cam = _CAM([_Setup([op])], future=_Future(complete=False))   # never completes
        _install(monkeypatch, cam, [_Edge()])
        out = _payload(cg.handler(operation="2D Contour1", selection="chain", handles=["h"]))
        assert cam.generated == [op]
        assert out["launched"] is True
        assert pumps == []                                # returned with the future still incomplete

    def test_future_registered_in_the_shared_generation_registry(self, monkeypatch):
        # The returned handle keys _cam_common._GENERATIONS and the entry holds THE launched future:
        # a dropped future is garbage-collected and Fusion abandons the generation; the registry is
        # also what cam_get_status's handle path reads and cleans up.
        op = _curve_op()
        cam = _CAM([_Setup([op])], future=_Future(complete=False))
        _install(monkeypatch, cam, [_Edge()])
        out = _payload(cg.handler(operation="2D Contour1", selection="chain", handles=["h"]))
        entry = _cam._GENERATIONS[out["handle"]]
        assert entry["future"] is cam.future
        assert entry["scope"] == "operation" and "2D Contour1" in entry["target"]

    def test_note_teaches_the_cam_get_status_target_read(self, monkeypatch):
        op = _curve_op()
        cam = _CAM([_Setup([op])], future=_Future(complete=False))
        _install(monkeypatch, cam, [_Edge()])
        out = _payload(cg.handler(operation="2D Contour1", selection="chain", handles=["h"]))
        assert "cam_get_status(target='2D Contour1')" in out["note"]
        assert "completed=true" in out["note"]

    def test_launch_failure_reported_with_selection_kept(self, monkeypatch):
        # The selection mutation already took; a failed LAUNCH is reported as generate_error with the
        # cam_generate retry pointer, not a false 'launched' and not an isError that hides the applied
        # selection.
        op = _curve_op()
        cam = _CAM([_Setup([op])])
        def _boom(op):
            raise RuntimeError("launch refused")
        cam.generateToolpath = _boom
        _install(monkeypatch, cam, [_Edge()])
        out = _payload(cg.handler(operation="2D Contour1", selection="chain", handles=["h"]))
        assert out["selections"] == 1
        assert "launch refused" in out["generate_error"]
        assert "launched" not in out and "handle" not in out
        assert "cam_generate" in out["note"]

    def test_generate_false_skips(self, monkeypatch):
        op = _curve_op()
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Edge()])
        out = _payload(cg.handler(operation="2D Contour1", selection="chain", handles=["h"],
                                  generate=False))
        assert "launched" not in out and cam.generated == []
        assert _cam._GENERATIONS == {}


class TestGenerateEntitlement:
    """The inline generate runs the pre-flight cam_generate's launch runs, off the same
    _cam_common.strategy_generation_allowed seam: an operation whose strategy reads
    isGenerationAllowed false is not launched, because such a launch parks with nothing to poll."""

    def _run(self, monkeypatch, allowed, **kw):
        op = _curve_op(strategy="swarf")
        cam = _CAM([_Setup([op])], future=_Future(complete=False))
        _install(monkeypatch, cam, [_Edge()])
        monkeypatch.setattr(cg, "strategy_generation_allowed", lambda name: allowed)
        return cam, cg.handler(operation="2D Contour1", selection="chain", handles=["h"], **kw)

    def test_a_blocked_strategy_keeps_the_selection_and_launches_nothing(self, monkeypatch):
        cam, res = self._run(monkeypatch, False)
        out = _payload(res)
        assert out["selections"] == 1                 # the selection is this tool's own job
        assert cam.generated == [] and _cam._GENERATIONS == {}
        assert out["launched"] is False and "handle" not in out
        assert out["entitlement_blocked"] == {"operation": "2D Contour1", "strategy": "swarf"}
        assert "swarf" in out["note"] and "isGenerationAllowed false" in out["note"]

    def test_an_allowed_strategy_launches_and_publishes_no_entitlement_key(self, monkeypatch):
        cam, res = self._run(monkeypatch, True)
        out = _payload(res)
        assert out["launched"] is True and cam.generated
        assert "entitlement_blocked" not in out and "entitlement_checked" not in out

    def test_an_unread_flag_still_launches_and_says_so(self, monkeypatch):
        # None is no verdict at all, so refusing on it would invent one - the launch happens and the
        # payload states that no pre-flight backed it.
        cam, res = self._run(monkeypatch, None)
        out = _payload(res)
        assert out["launched"] is True and cam.generated
        assert out["entitlement_checked"] is False and "did not read" in out["note"]

    def test_a_blocked_launch_keeps_the_rail_order_and_drops_the_triage_pointer(self, monkeypatch):
        # The triage reads a toolpath that generated VALID but EMPTY; this call launched nothing, so
        # pointing at it would send the caller to a state the operation cannot reach - while the
        # order the rails were taken in is a fact about the selection that landed, and still publishes.
        op = _swarf_op()
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Edge(), _Edge()])
        monkeypatch.setattr(cg, "strategy_generation_allowed", lambda name: False)
        out = _payload(cg.handler(operation="Swarf1", selection="chain", handles=["lo", "hi"]))
        assert out["launched"] is False
        assert "rails_order" in out and "rail_triage" not in out["note"]

    def test_a_selection_only_call_asks_the_seam_nothing(self, monkeypatch):
        # The pre-flight belongs to the LAUNCH: generate=false on a blocked strategy is a complete
        # success, and a verdict published there would read as a refusal of the selection.
        asked = []
        op = _curve_op(strategy="swarf")
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Edge()])
        monkeypatch.setattr(cg, "strategy_generation_allowed",
                            lambda name: asked.append(name) or False)
        out = _payload(cg.handler(operation="2D Contour1", selection="chain", handles=["h"],
                                  generate=False))
        assert asked == [] and out["selections"] == 1
        assert "entitlement_blocked" not in out and "launched" not in out


# ── the 3D machining-boundary engage (boundaryMode 'silhouette' -> 'selection') ───────────────
#
# A selection landing on a 3D op's machiningBoundarySel parameter is INERT while boundaryMode holds
# its default 'silhouette' - the op ignores the selection and machines the silhouette surface set
# (measured live: a 3D parallel op fed a boundary chain read boundaryMode 'silhouette' and machined
# the part's underside). The tool flips boundaryMode to 'selection' in the SAME call and reports it.
# The discriminator is the resolved curve param: a 2D contour/pocket op resolves to 'contours'/
# 'pockets' first and never reaches machiningBoundarySel, and the holes family never builds a curve
# selection at all - so neither engages. boundaryMode is a CAM STRING parameter, stored single-quoted.


class _BoundaryModeParam:
    """boundaryMode as a CAM string parameter: starts at 'silhouette' (the inert default) and stores
    whatever expression is written, single-quoted the way a CAM string parameter reads back."""
    def __init__(self, start="silhouette"):
        self._expr = "'" + start + "'"

    @property
    def expression(self):
        return self._expr

    @expression.setter
    def expression(self, v):
        self._expr = v


class _DeafBoundaryMode:
    """Accepts the mode write and keeps the value it started on - the swallowed engage that leaves the
    selection inert. Only the read-back reveals it."""
    def __init__(self, start="silhouette"):
        self._expr = "'" + start + "'"

    @property
    def expression(self):
        return self._expr

    @expression.setter
    def expression(self, _v):
        pass


class _RaisingBoundaryMode:
    """Refuses the boundaryMode assignment outright (the expression setter raises)."""
    @property
    def expression(self):
        return "'silhouette'"

    @expression.setter
    def expression(self, _v):
        raise RuntimeError("boundaryMode is read-only here")


class _UnreadableBoundaryMode:
    """Takes the write; nothing reads back off boundaryMode afterwards."""
    def __init__(self):
        self._written = False

    @property
    def expression(self):
        if self._written:
            raise RuntimeError("boundaryMode is unreadable")
        return "'silhouette'"

    @expression.setter
    def expression(self, _v):
        self._written = True


class _RefusingEnumerationMode(_BoundaryModeParam):
    """Refuses the write with Fusion's own words for a value outside a CAM enumeration's set."""
    @_BoundaryModeParam.expression.setter
    def expression(self, _v):
        raise RuntimeError("3 : Invalid enumeration value.")


class _RefusingChoiceMode(_RefusingEnumerationMode):
    """The same refusal on a parameter whose value ANSWERS getChoices - the set the remedy names."""
    def __init__(self, start="silhouette", choices=("silhouette", "selection")):
        super().__init__(start)
        legal = list(choices)
        self.value = types.SimpleNamespace(getChoices=lambda: (True, list(legal), legal))


class _RefusingEnumerationHeight:
    """A height parameter holding a QUOTED expression that refuses the write the same way."""
    def __init__(self, held):
        self.value = None
        self._held = held

    @property
    def expression(self):
        return self._held

    @expression.setter
    def expression(self, _v):
        raise RuntimeError("3 : Invalid enumeration value.")


class _BareBoundaryMode(_BoundaryModeParam):
    """A mode parameter whose stored expression carries NO quotes - the current spelling that decides
    whether this call wraps the request."""
    def __init__(self, start="silhouette"):
        super().__init__(start)
        self._expr = start


class _EvalErrorBoundaryMode(_BoundaryModeParam):
    """Stores 'selection' but reports the failure through .error - the CAM parameter store's shape for
    an expression that does not evaluate (see _cam_common.expression_error)."""
    @property
    def error(self):
        return "Failed to evaluate expression."


def _boundary_op(name="Parallel1", boundary_mode=None, **kw):
    """A 3D surfacing op: its curve param is 'machiningBoundarySel' (NO contours/pockets, so the
    probe order resolves to it), plus the boundaryMode string knob and the height group."""
    return _Op(name, {"machiningBoundarySel": _Param(_CurveParamValue()),
                      "boundaryMode": boundary_mode if boundary_mode is not None else _BoundaryModeParam(),
                      "topHeight_mode": _Param(None), "topHeight_offset": _Param(None),
                      "bottomHeight_mode": _Param(None), "bottomHeight_offset": _Param(None)}, **kw)


def _boundary_selection_of(op):
    return op.parameters.itemByName("machiningBoundarySel").value.getCurveSelections().item(0)


class TestBoundaryEngage:
    def test_chain_on_a_3d_boundary_engages_boundary_mode(self, monkeypatch):
        op = _boundary_op()
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Edge(), _Edge()])
        out = _payload(cg.handler(operation="Parallel1", selection="chain", chain_groups=[["a", "b"]],
                                  generate=False))
        # boundaryMode is flipped off its inert default in the same call
        assert cg.unquote_expression(op.parameters.itemByName("boundaryMode").expression) == "selection"
        assert out["boundary_engaged"] is True
        assert out["boundary_mode"] == "selection"
        # the selection itself landed on the machining-boundary param
        assert out["selections"] == 1 and len(_boundary_selection_of(op).inputGeometry) == 2

    def test_a_2d_contour_feed_does_not_engage_boundary_mode(self, monkeypatch):
        # the discriminator is the resolved curve param: a 2D op resolves to 'contours', so the
        # engage never fires and no boundary key is published.
        op = _curve_op()
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Edge()])
        out = _payload(cg.handler(operation="2D Contour1", selection="chain", handles=["h"],
                                  generate=False))
        assert "boundary_engaged" not in out and "boundary_mode" not in out

    def test_a_holes_feed_does_not_engage_boundary_mode(self, monkeypatch):
        # the holes family sets holeFaces directly - it never builds a curve selection, so the
        # boundary seam is unreachable from it.
        op = _drill_op()
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Face(0.3)])
        out = _payload(cg.handler(operation="Drill1", selection="holes", handles=["a"],
                                  generate=False))
        assert "boundary_engaged" not in out and "boundary_mode" not in out

    def test_a_boundary_mode_that_will_not_take_is_an_error(self, monkeypatch):
        # a swallowed boundaryMode leaves the exact inert selection this guard prevents, so it fails
        # the call rather than reporting a boundary engaged - and names the value it still reads.
        op = _boundary_op(boundary_mode=_DeafBoundaryMode())
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Edge()])
        res = cg.handler(operation="Parallel1", selection="chain", handles=["h"], generate=False)
        assert res["isError"] is True
        assert "boundaryMode" in res["message"] and "did not take" in res["message"]
        assert "silhouette" in res["message"]

    def test_a_boundary_mode_set_that_raises_is_an_error(self, monkeypatch):
        op = _boundary_op(boundary_mode=_RaisingBoundaryMode())
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Edge()])
        res = cg.handler(operation="Parallel1", selection="chain", handles=["h"], generate=False)
        assert res["isError"] is True
        assert "Could not set boundaryMode" in res["message"] and "read-only here" in res["message"]

    def test_an_op_without_boundary_mode_is_disclosed_not_claimed_engaged(self, monkeypatch):
        # machiningBoundarySel present, boundaryMode absent: the engage cannot be confirmed, so it is
        # disclosed - never a false boundary_engaged claiming an effect no read backs.
        op = _Op("Parallel1", {"machiningBoundarySel": _Param(_CurveParamValue())})
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Edge()])
        res = cg.handler(operation="Parallel1", selection="chain", handles=["h"], generate=False)
        assert res["isError"] is True
        assert "boundaryMode" in res["message"] and "cannot confirm" in res["message"]

    def test_a_boundary_mode_that_cannot_be_read_back_is_unconfirmed(self, monkeypatch):
        op = _boundary_op(boundary_mode=_UnreadableBoundaryMode())
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Edge()])
        res = cg.handler(operation="Parallel1", selection="chain", handles=["h"], generate=False)
        assert res["isError"] is True and "UNCONFIRMED" in res["message"]

    def test_a_boundary_mode_that_does_not_evaluate_is_an_error(self, monkeypatch):
        op = _boundary_op(boundary_mode=_EvalErrorBoundaryMode())
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Edge()])
        res = cg.handler(operation="Parallel1", selection="chain", handles=["h"], generate=False)
        assert res["isError"] is True and "not engaged" in res["message"]
        assert "Failed to evaluate expression." in res["message"]

    def test_zero_selections_on_a_boundary_op_does_not_engage(self, monkeypatch):
        # nothing landed to engage, so boundaryMode is left untouched and the handler's own
        # 0-selections error is what fires - not a phantom boundary flip.
        op = _boundary_op()
        pv = op.parameters.itemByName("machiningBoundarySel").value
        pv.applyCurveSelections = lambda cs: setattr(pv, "_cs", _CurveSelections())   # -> 0 selections
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Edge()])
        res = cg.handler(operation="Parallel1", selection="chain", handles=["h"], generate=False)
        assert res["isError"] is True and "0 selection" in res["message"]
        assert cg.unquote_expression(op.parameters.itemByName("boundaryMode").expression) == "silhouette"


class TestQuotingMatchesWhatTheParameterStores:
    """Every expression this tool writes goes through _cam_common.matched_quoting: the wrap is
    decided by the CURRENT expression, and a wrap this call added is published under 'quoted'."""

    def _engage(self, monkeypatch, boundary_mode):
        op = _boundary_op(boundary_mode=boundary_mode)
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Edge()])
        return op, _payload(cg.handler(operation="Parallel1", selection="chain", handles=["h"],
                                       generate=False))

    def test_a_quoted_mode_takes_the_wrapped_spelling_and_the_wrap_is_published(self, monkeypatch):
        op, out = self._engage(monkeypatch, _BoundaryModeParam())      # holds "'silhouette'"
        assert op.parameters.itemByName("boundaryMode").expression == "'selection'"
        assert out["quoted"] == ["boundaryMode"] and out["boundary_mode"] == "selection"

    def test_a_bare_mode_takes_the_bare_spelling_and_publishes_no_wrap(self, monkeypatch):
        # The other side of the same decision: a parameter storing an unquoted expression is written
        # unquoted, and a call that always wrapped would leave "'selection'" here.
        op, out = self._engage(monkeypatch, _BareBoundaryMode())       # holds "silhouette"
        assert op.parameters.itemByName("boundaryMode").expression == "selection"
        assert "quoted" not in out and out["boundary_mode"] == "selection"

    def test_a_refused_mode_names_the_expression_that_was_WRITTEN(self, monkeypatch):
        # Fusion's enumeration refusal is about the expression this call sent, not the request the
        # caller typed - so a wrapped write has to name the wrapped spelling, or the remedy points
        # at a string the parameter never saw.
        op, res = self._refused_by_enumeration(monkeypatch, _RefusingEnumerationMode())
        assert res["isError"] is True
        assert "the expression written was 'selection'" in res["message"]
        assert cg._PARAM_READ in res["message"]

    def test_a_refused_height_names_the_expression_that_was_WRITTEN(self, monkeypatch):
        op = _curve_op()
        op.parameters.swap("bottomHeight_mode", _RefusingEnumerationHeight("'from stock top'"))
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Edge()])
        res = cg.handler(operation="2D Contour1", selection="chain", handles=["h"],
                         bottom_mode="from contour", generate=False)
        assert res["isError"] is True
        assert "the expression written was 'from contour'" in res["message"]
        assert cg._PARAM_READ in res["message"]

    def test_a_refused_mode_whose_value_answers_getChoices_lists_them(self, monkeypatch):
        # The read pointer costs the caller a turn; where the parameter itself answers the set, the
        # refusal hands it over instead.
        _op, res = self._refused_by_enumeration(monkeypatch, _RefusingChoiceMode())
        assert res["isError"] is True
        assert "This parameter's own values: silhouette, selection" in res["message"]

    def test_a_non_enumeration_refusal_carries_no_remedy(self, monkeypatch):
        # The clause belongs to the refusal that names an enumeration; on any other platform message
        # it would assert a cause nothing read.
        op, res = self._refused_by_enumeration(monkeypatch, _RaisingBoundaryMode())
        assert res["isError"] is True and "read-only here" in res["message"]
        assert "the expression written" not in res["message"]

    def _refused_by_enumeration(self, monkeypatch, mode):
        op = _boundary_op(boundary_mode=mode)
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Edge()])
        return op, cg.handler(operation="Parallel1", selection="chain", handles=["h"],
                              generate=False)

    def test_a_height_mode_is_wrapped_to_match_the_expression_it_holds(self, monkeypatch):
        # A height _mode is a string parameter: on a store already keeping a quoted string, the bare
        # spelling is the enumeration value Fusion refuses, so the request is wrapped to match.
        op = _curve_op()
        held = _Param(None)
        held.expression = "'from stock top'"
        op.parameters.swap("bottomHeight_mode", held)
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Edge()])
        out = _payload(cg.handler(operation="2D Contour1", selection="chain", handles=["h"],
                                  bottom_mode="from contour", generate=False))
        assert held.expression == "'from contour'"
        assert out["heights_set"] == ["bottomHeight_mode='from contour'"]
        assert out["quoted"] == ["bottomHeight_mode"]


# ── the extension strategies' DRIVE parameters (swarf rails, deburr edges) ───────────────────
#
# A chain routes to whichever curve parameter the strategy carries, probed in _CURVE_PARAM_CANDIDATES
# order, and the order IS the routing. Measured per strategy on 2705.1.4 by dumping every CadContours2d
# parameter of one freshly created op each: swarf carries ONLY swarfContours; deburr carries edgeSel
# AND machiningBoundarySel AND edgeExcludeSel; multiaxis_roughing carries machiningBoundarySel AND
# stockContours; no 2D strategy carries swarfContours or edgeSel. So a drive parameter has to be probed
# before machiningBoundarySel (or a deburr feed lands on the boundary and the op is driven by nothing)
# and after contours/pockets (or a 2D feed changes route).
#
# swarf's rails are inert the same way a 3D boundary is: a fresh swarf op reads swarfSelectionMode
# 'surfaces' and generating with rails under that mode reported 'Surfaces: No valid geometry selected.'
# Deburr needs NO engage - measured: edges on edgeSel with edgeDefinitionType left at its default
# 'automatic' and boundaryMode at 'none' generated a valid toolpath in 3.2s.


def _swarf_op(name="Swarf1", mode=None, **kw):
    """A swarf op: swarfContours is its ONLY curve parameter, and swarfSelectionMode - the mode that
    decides whether the rails are read at all - starts at 'surfaces' on a fresh op (measured)."""
    return _Op(name, {"swarfContours": _Param(_CurveParamValue()),
                      "swarfSelectionMode": mode if mode is not None else _BoundaryModeParam("surfaces"),
                      "bottomHeight_mode": _Param(None), "bottomHeight_offset": _Param(None)}, **kw)


def _deburr_op(name="Deburr1", **kw):
    """A deburr op: edgeSel is the DRIVE selection and machiningBoundarySel sits beside it, so this op
    is the one the probe ORDER is decided on (measured - deburr carries both)."""
    return _Op(name, {"edgeSel": _Param(_CurveParamValue()),
                      "machiningBoundarySel": _Param(_CurveParamValue()),
                      "boundaryMode": _BoundaryModeParam("none"),
                      "topHeight_mode": _Param(None), "topHeight_offset": _Param(None)}, **kw)


def _ma_roughing_op(name="Multi-Axis Roughing1", **kw):
    """A multi-axis roughing op: machiningBoundarySel AND stockContours (measured), so stockContours
    staying LAST in the probe order is what routes its chain to the boundary."""
    return _Op(name, {"machiningBoundarySel": _Param(_CurveParamValue()),
                      "stockContours": _Param(_CurveParamValue()),
                      "boundaryMode": _BoundaryModeParam("none")}, **kw)


def _trace_op(name="Trace1", **kw):
    """A trace op: 'curves' is its only curve parameter and it carries no boundary mode, so the
    selection is read as soon as it lands - the shape trace, multi_axis_contour and
    multi_axis_morph share; morph and project carry a machining boundary beside their curves."""
    return _Op(name, {"curves": _Param(_CurveParamValue()),
                      "topHeight_mode": _Param(None), "topHeight_offset": _Param(None)}, **kw)


def _selection_on(op, param):
    return op.parameters.itemByName(param).value.getCurveSelections()


def _turning_trace_op(name="TurnTrace1", **kw):
    """A turning trace op: MEASURED, its drive input is 'modelContour' - a CadContours2dParameterValue,
    the same class 'contours' carries - and it holds none of the other curve parameters."""
    return _Op(name, {"modelContour": _Param(_CurveParamValue()),
                      "frontHeight_ref": _Param(None)}, **kw)


class TestDriveParamRouting:
    def test_a_turning_trace_chain_lands_on_its_model_contour(self, monkeypatch):
        # MEASURED: applying a chain here cleared 'Model Contour: No model contour selected to
        # machine.' Without the route the op meets the has-no-curve-parameter refusal instead.
        op = _turning_trace_op()
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Edge()])
        out = _payload(cg.handler(operation="TurnTrace1", selection="chain", handles=["a"],
                                  generate=False))
        assert _selection_on(op, "modelContour").count == 1
        assert out["selections"] == 1

    def test_model_contour_is_probed_after_the_boundary_so_no_3d_feed_changes_route(self,
                                                                                    monkeypatch):
        # the ORDER is the routing: an op carrying BOTH keeps its boundary, which is what every 3D
        # family's chain is aimed at.
        op = _Op("Both1", {"machiningBoundarySel": _Param(_CurveParamValue()),
                           "modelContour": _Param(_CurveParamValue()),
                           "boundaryMode": _BoundaryModeParam("none")})
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Edge()])
        _payload(cg.handler(operation="Both1", selection="chain", handles=["a"], generate=False))
        assert _selection_on(op, "machiningBoundarySel").count == 1
        assert _selection_on(op, "modelContour").count == 0

    def test_a_swarf_chain_lands_on_swarf_contours_and_engages_the_mode(self, monkeypatch):
        op = _swarf_op()
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Edge(), _Edge()])
        out = _payload(cg.handler(operation="Swarf1", selection="chain", handles=["lower", "upper"],
                                  generate=False))
        cs = _selection_on(op, "swarfContours")
        assert cs.count == 2                    # one selection per rail
        assert cg.unquote_expression(
            op.parameters.itemByName("swarfSelectionMode").expression) == "contours"
        assert out["swarf_engaged"] is True and out["swarf_mode"] == "contours"

    def test_a_deburr_chain_lands_on_the_drive_edges_not_the_machining_boundary(self, monkeypatch):
        # deburr carries BOTH: routing to machiningBoundarySel would leave the op's drive selection
        # empty and the toolpath driven by nothing, while the payload reported a landed selection.
        op = _deburr_op()
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Edge(), _Edge()])
        out = _payload(cg.handler(operation="Deburr1", selection="chain", chain_groups=[["a", "b"]],
                                  generate=False))
        assert _selection_on(op, "edgeSel").count == 1
        assert _selection_on(op, "machiningBoundarySel").count == 0
        assert out["selections"] == 1

    def test_a_deburr_chain_engages_no_mode(self, monkeypatch):
        # measured: edges on edgeSel generate a valid toolpath with boundaryMode left at 'none', so
        # flipping it here would change a setting nothing asked for and claim an engage that is not
        # this parameter's.
        op = _deburr_op()
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Edge()])
        out = _payload(cg.handler(operation="Deburr1", selection="chain", handles=["a"],
                                  generate=False))
        assert cg.unquote_expression(op.parameters.itemByName("boundaryMode").expression) == "none"
        assert "boundary_engaged" not in out and "swarf_engaged" not in out

    def test_stock_contours_stays_last_so_a_roughing_chain_lands_on_the_boundary(self, monkeypatch):
        # multiaxis_roughing carries machiningBoundarySel AND stockContours; stockContours is never a
        # drive, so a chain landing there would be inert and no boundary would be engaged.
        op = _ma_roughing_op()
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Edge()])
        out = _payload(cg.handler(operation="Multi-Axis Roughing1", selection="chain",
                                  handles=["a"], generate=False))
        assert _selection_on(op, "machiningBoundarySel").count == 1
        assert _selection_on(op, "stockContours").count == 0
        assert out["boundary_engaged"] is True

    def test_a_2d_contour_op_resolves_contours_first(self, monkeypatch):
        # the drive params sit AFTER contours/pockets, so a 2D feed resolves contours and engages
        # nothing.
        op = _curve_op()
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Edge()])
        out = _payload(cg.handler(operation="2D Contour1", selection="chain", handles=["h"],
                                  generate=False))
        assert _selection_of(op).kind == "chain"
        assert "swarf_engaged" not in out and "boundary_engaged" not in out

    def test_a_swarf_mode_that_will_not_take_is_an_error(self, monkeypatch):
        # a swallowed swarfSelectionMode leaves the rails unread - the op asks for surfaces instead -
        # so it fails the call rather than reporting an engage, naming the value it still reads.
        op = _swarf_op(mode=_DeafBoundaryMode("surfaces"))
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Edge(), _Edge()])
        res = cg.handler(operation="Swarf1", selection="chain", handles=["a", "b"], generate=False)
        assert res["isError"] is True
        assert "swarfSelectionMode" in res["message"] and "did not take" in res["message"]
        assert "surfaces" in res["message"]

    def test_a_swarf_op_without_the_mode_param_is_disclosed_not_claimed_engaged(self, monkeypatch):
        op = _Op("Swarf1", {"swarfContours": _Param(_CurveParamValue())})
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Edge(), _Edge()])
        res = cg.handler(operation="Swarf1", selection="chain", handles=["a", "b"], generate=False)
        assert res["isError"] is True
        assert "swarfSelectionMode" in res["message"] and "cannot confirm" in res["message"]

    def test_the_curve_param_miss_names_the_drive_params_it_looked_for(self, monkeypatch):
        # the refusal lists the candidates, so a strategy whose drive param is not routed yet is
        # diagnosable from the message alone.
        op = _Op("Ghost1", {})
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Edge()])
        res = cg.handler(operation="Ghost1", selection="chain", handles=["a"], generate=False)
        assert res["isError"] is True
        assert "swarfContours" in res["message"] and "edgeSel" in res["message"]

    def test_a_trace_chain_lands_on_the_curves_parameter(self, monkeypatch):
        # 'curves' is the drive-curve parameter (measured: CadContours2dParameterValue, isEditable
        # true); on trace it is the only selection parameter the operation carries.
        op = _trace_op()
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Edge()])
        out = _payload(cg.handler(operation="Trace1", selection="chain", handles=["a"],
                                  generate=False))
        assert _selection_on(op, "curves").count == 1
        assert out["selections"] == 1

    def test_two_drive_curves_land_as_two_selections(self, monkeypatch):
        # MEASURED on morph: two rim circles fed to ONE CurveSelection walk into a single path and
        # the operation reports 'No passes to link'; one selection EACH cut 77.7 s of toolpath.
        op = _trace_op()
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Edge(), _Edge()])
        out = _payload(cg.handler(operation="Trace1", selection="chain", handles=["a", "b"],
                                  generate=False))
        assert _selection_on(op, "curves").count == 2
        assert out["selections"] == 2

    def test_curves_is_probed_before_the_machining_boundary(self, monkeypatch):
        # morph and project carry BOTH (measured over every allowed strategy): 'curves' is their
        # drive and machiningBoundarySel is containment, so a chain landing on the boundary would
        # leave the strategy driven by nothing while the payload reported a landed selection.
        op = _Op("Morph1", {"curves": _Param(_CurveParamValue()),
                            "machiningBoundarySel": _Param(_CurveParamValue()),
                            "boundaryMode": _BoundaryModeParam("silhouette")})
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Edge()])
        out = _payload(cg.handler(operation="Morph1", selection="chain", handles=["a"],
                                  generate=False))
        assert _selection_on(op, "curves").count == 1
        assert _selection_on(op, "machiningBoundarySel").count == 0
        assert "boundary_engaged" not in out

    def test_the_curve_param_miss_names_the_selection_params_the_op_carries(self, monkeypatch):
        # a drill met by a chain carries holeFaces, so the refusal names THAT parameter and the kind
        # that reaches it - a remedy read off the operation rather than one the strategy might want.
        op = _drill_op_with_heights()
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Edge()])
        res = cg.handler(operation="Drill1", selection="chain", handles=["a"], generate=False)
        assert res["isError"] is True
        assert "'holeFaces'" in res["message"] and "selection='holes'" in res["message"]

    def test_an_op_carrying_both_families_is_handed_both_remedies(self, monkeypatch):
        # one tail per family: an operation carrying a direct set AND a surface set has two ways
        # in, and naming only the first hides the other behind a second failed call.
        op = _Op("Both1", {"probe_selection": _Param(_HoleParamValue()),
                           "driveSurfaces": _Param(_HoleParamValue())})
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Edge()])
        res = cg.handler(operation="Both1", selection="chain", handles=["a"], generate=False)
        assert res["isError"] is True
        assert "selection='probe'" in res["message"]
        assert "selection='surfaces'" in res["message"] and "drive" in res["message"]

    def test_an_op_carrying_no_selection_parameter_names_no_kind_at_all(self, monkeypatch):
        # with nothing read off the operation there is no remedy to name, so the refusal sends the
        # caller to the parameter read instead of guessing a strategy family.
        op = _Op("Ghost1", {})
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Edge()])
        res = cg.handler(operation="Ghost1", selection="chain", handles=["a"], generate=False)
        assert "selection='" not in res["message"]
        assert "include=['parameters']" in res["message"]


# ── swarf's rail PAIR: one CurveSelection per rail ──────────────────────────────────────────
#
# Measured on 2705.1.4, the two failure shapes are distinct. 'Invalid contours.' at generate is the
# STRUCTURAL one: one contour on swarfContours, closed rails, or two rails fed to a single
# CurveSelection - which the chain walker returned as ONE 6-segment path, so one selection cannot
# carry a pair. Wrong ORDER is the SILENT one: the pair applied lower-rail-first produced passes,
# while the same pair applied upper-first produced a valid but EMPTY toolpath on both otherSide
# settings. Hence one selection per rail, open by default, and the order published and triaged.


class TestSwarfRailPair:
    def test_each_rail_gets_its_own_selection(self, monkeypatch):
        op = _swarf_op()
        cam = _CAM([_Setup([op])])
        edges = [_Edge(), _Edge()]
        _install(monkeypatch, cam, edges)
        out = _payload(cg.handler(operation="Swarf1", selection="chain", handles=["lo", "hi"],
                                  generate=False))
        cs = _selection_on(op, "swarfContours")
        assert cs.count == 2
        assert cs.item(0).inputGeometry == [edges[0]] and cs.item(1).inputGeometry == [edges[1]]
        assert out["selections"] == 2

    def test_one_rail_is_refused_with_the_operation_still_holding_what_it_had(self, monkeypatch):
        # the refusal is decided BEFORE the collection is cleared: a call that cannot produce a legal
        # pair must not leave the operation holding a selection its strategy rejects.
        op = _swarf_op()
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Edge()])
        res = cg.handler(operation="Swarf1", selection="chain", handles=["only"], generate=False)
        assert res["isError"] is True
        assert "RAIL PAIR" in res["message"] and "LOWER rail first" in res["message"]
        pv = op.parameters.itemByName("swarfContours").value
        assert pv.applied == 0 and pv._cs.cleared == 0

    def test_a_refused_rail_count_engages_no_mode(self, monkeypatch):
        op = _swarf_op()
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Edge()])
        cg.handler(operation="Swarf1", selection="chain", handles=["only"], generate=False)
        assert cg.unquote_expression(
            op.parameters.itemByName("swarfSelectionMode").expression) == "surfaces"

    def test_a_rail_defaults_to_open(self, monkeypatch):
        # a closed rail pair was refused by the strategy where the same pair open was not, so the
        # tool picks open when the caller says nothing - and publishes which way it went.
        op = _swarf_op()
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Edge(), _Edge()])
        out = _payload(cg.handler(operation="Swarf1", selection="chain", handles=["lo", "hi"],
                                  generate=False))
        cs = _selection_on(op, "swarfContours")
        assert cs.item(0).isOpen is True and cs.item(1).isOpen is True
        assert out["rails_open"] is True

    def test_an_explicit_closed_rail_is_honoured_and_published(self, monkeypatch):
        # the default is a default, not an override: a caller who asks for closed rails gets them,
        # and the payload says so rather than repeating the tool's own preference.
        op = _swarf_op()
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Edge(), _Edge()])
        out = _payload(cg.handler(operation="Swarf1", selection="chain", handles=["lo", "hi"],
                                  is_open=False, generate=False))
        cs = _selection_on(op, "swarfContours")
        assert cs.item(0).isOpen is False and cs.item(1).isOpen is False
        assert out["rails_open"] is False

    def test_a_non_rail_param_keeps_ONE_selection_over_every_handle(self, monkeypatch):
        # the split belongs to the rail-pair parameter alone: a 2D contour fed four edges is one
        # chain, and splitting it would apply four contours where the caller asked for one.
        op = _curve_op()
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Edge(), _Edge(), _Edge(), _Edge()])
        out = _payload(cg.handler(operation="2D Contour1", selection="chain",
                                  chain_groups=[["a", "b", "c", "d"]], generate=False))
        cs = op.parameters.itemByName("contours").value.getCurveSelections()
        assert cs.count == 1 and len(cs.item(0).inputGeometry) == 4
        assert out["selections"] == 1 and "rails_open" not in out

    def test_rails_open_states_what_the_OPERATION_reads_not_what_was_written(self, monkeypatch):
        # applyCurveSelections hands the operation a collection of its own, so the objects written to
        # and the ones it then reports are not the same rails. A payload built from the written value
        # would call these rails open while the operation holds them closed - the shape the strategy
        # refuses, published as the shape it accepts.
        op = _swarf_op()
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Edge(), _Edge()])
        pv = op.parameters.itemByName("swarfContours").value

        def _swap_in_closed_rails(cs):
            swapped = _CurveSelections()
            for i in range(cs.count):
                clone = swapped._make(cs.item(i).kind)
                clone.inputGeometry = cs.item(i).inputGeometry
                clone.isOpen = False
            pv._cs = swapped
        pv.applyCurveSelections = _swap_in_closed_rails
        out = _payload(cg.handler(operation="Swarf1", selection="chain", handles=["lo", "hi"],
                                  generate=False))
        assert out["selections"] == 2
        assert out["rails_open"] is False

    def test_rails_open_is_withheld_where_no_rail_answers_isOpen(self, monkeypatch):
        # A rail whose isOpen will not read answers NOTHING, and 'nothing' is not 'closed': coercing
        # the unread value would publish rails_open false - a verified-state claim - on exactly the
        # selection class that carries no such property.
        op = _swarf_op()
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Edge(), _Edge()])
        pv = op.parameters.itemByName("swarfContours").value

        def _swap_in_unreadable_rails(cs):
            swapped = _CurveSelections()
            for i in range(cs.count):
                rail = _UnreadableOpenRail(cs.item(i).kind)
                rail.inputGeometry = cs.item(i).inputGeometry
                swapped._items.append(rail)
            pv._cs = swapped
        pv.applyCurveSelections = _swap_in_unreadable_rails
        out = _payload(cg.handler(operation="Swarf1", selection="chain", handles=["lo", "hi"],
                                  generate=False))
        assert out["selections"] == 2 and "rails_open" not in out
        assert out["rails_order"] == cg._RAILS_ORDER      # the order still reports

    def test_rails_open_is_withheld_where_the_rails_disagree(self, monkeypatch):
        # one bool cannot state a collection holding one open rail and one closed, and picking
        # either would be a claim about the other - so the key is left off.
        op = _swarf_op()
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Edge(), _Edge()])
        pv = op.parameters.itemByName("swarfContours").value

        def _swap_in_mixed_rails(cs):
            swapped = _CurveSelections()
            for i in range(cs.count):
                clone = swapped._make(cs.item(i).kind)
                clone.inputGeometry = cs.item(i).inputGeometry
                clone.isOpen = (i == 0)
            pv._cs = swapped
        pv.applyCurveSelections = _swap_in_mixed_rails
        out = _payload(cg.handler(operation="Swarf1", selection="chain", handles=["lo", "hi"],
                                  generate=False))
        assert out["selections"] == 2 and "rails_open" not in out
        assert out["rails_order"] == cg._RAILS_ORDER      # the order still reports

    def test_the_rail_order_contract_is_published(self, monkeypatch):
        # The order is the caller's and getting it wrong fails SILENTLY - an upper-first pair
        # generates valid and EMPTY - so the order this call used is stated in the payload instead
        # of being left for the caller to infer from a toolpath that cut nothing.
        op = _swarf_op()
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Edge(), _Edge()])
        out = _payload(cg.handler(operation="Swarf1", selection="chain", handles=["lo", "hi"],
                                  generate=False))
        assert out["rails_order"] == cg._RAILS_ORDER
        assert "LOWER" in out["rails_order"]

    def test_the_launched_note_sends_a_rail_pair_to_the_status_triage(self, monkeypatch):
        # the empty rail toolpath shows up at cam_get_status, not here - this call's note names the
        # key that triages it there instead of carrying 300 chars of triage on every success.
        op = _swarf_op()
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Edge(), _Edge()])
        out = _payload(cg.handler(operation="Swarf1", selection="chain", handles=["lo", "hi"]))
        assert out["launched"] is True and "rails_triage" not in out
        assert "cam_get_status" in out["note"] and "rail_triage" in out["note"]

    def test_the_worst_composed_launched_note_fits_the_wire_budget(self, monkeypatch):
        # the note is assembled at run time from the base, the operation's own NAME, the rail
        # pointer and the unread-entitlement clause, so test_prose_budget measures none of the
        # compositions - all four ride together on a long-named rail op whose flag would not read.
        op = _swarf_op(name="Op 2 - Finishing Ruled Flank Pass")
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Edge(), _Edge()])
        monkeypatch.setattr(cg, "strategy_generation_allowed", lambda name: None)
        out = _payload(cg.handler(operation="Op 2 - Finishing Ruled Flank Pass", selection="chain",
                                  handles=["lo", "hi"]))
        assert out["launched"] is True and out["entitlement_checked"] is False
        assert "rail_triage" in out["note"] and "isGenerationAllowed" in out["note"]
        assert len(out["note"]) <= 400, len(out["note"])   # test_prose_budget.NOTE_BUDGET_CHARS

    def test_a_selection_only_call_points_at_no_triage(self, monkeypatch):
        # nothing was launched, so there is no toolpath to come back empty and nothing to triage.
        op = _swarf_op()
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Edge(), _Edge()])
        out = _payload(cg.handler(operation="Swarf1", selection="chain", handles=["lo", "hi"],
                                  generate=False))
        assert out["rails_order"] == cg._RAILS_ORDER and "rail_triage" not in out["note"]

    def test_a_non_rail_selection_carries_neither_the_key_nor_the_pointer(self, monkeypatch):
        # the pointer is about a rail pair; on a 2D contour it would send the caller to a triage of
        # inputs that operation has not got.
        op = _curve_op()
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Edge()])
        out = _payload(cg.handler(operation="2D Contour1", selection="chain", handles=["h"]))
        assert "rails_order" not in out and "rail_triage" not in out["note"]

    def test_three_rails_are_applied_rather_than_refused(self, monkeypatch):
        # only the measured floor is guarded: one contour was refused by the strategy, more than two
        # was never measured, so the tool applies them and lets the strategy speak for itself.
        op = _swarf_op()
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Edge(), _Edge(), _Edge()])
        out = _payload(cg.handler(operation="Swarf1", selection="chain", handles=["a", "b", "c"],
                                  generate=False))
        assert out["selections"] == 3


# ── the read-back covers EVERY applied selection, not just the first ─────────────────────────


class TestRailReadBack:
    def _rails(self, monkeypatch, prepare):
        op = _swarf_op()
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Edge(), _Edge()])
        pv = op.parameters.itemByName("swarfContours").value
        real_make = pv._cs._make
        made = []

        def _prepared(kind):
            sel = real_make(kind)
            made.append(sel)
            prepare(len(made) - 1, sel)
            return sel
        pv._cs._make = _prepared
        return cg.handler(operation="Swarf1", selection="chain", handles=["lo", "hi"],
                          generate=False)

    def test_the_resolved_counts_sum_over_every_rail(self, monkeypatch):
        def prep(i, sel):
            sel.outputGeometry = [_Path(2 + i)]
            sel._value = [object()] * (3 + i)
        out = _payload(self._rails(monkeypatch, prep))
        assert out["resolved"] == {"curve_paths": 2, "curve_segments": 5, "entities": 7}

    def test_an_error_on_the_SECOND_rail_fails_the_call(self, monkeypatch):
        # a read-back that only inspected selection 0 would publish a clean apply while Fusion had
        # rejected the second rail, leaving the operation holding a pair it refused half of.
        def prep(i, sel):
            if i == 1:
                sel.hasError = True
                sel.error = "The selected contour is not on a wall."
        res = self._rails(monkeypatch, prep)
        assert res["isError"] is True and "not on a wall" in res["message"]

    def test_a_warning_on_the_SECOND_rail_is_surfaced(self, monkeypatch):
        def prep(i, sel):
            if i == 1:
                sel.hasWarning = True
                sel.warning = "Rail was extended."
        out = _payload(self._rails(monkeypatch, prep))
        assert out["selection_warning"] == "Rail was extended."

    def test_two_warnings_are_both_named(self, monkeypatch):
        def prep(i, sel):
            sel.hasWarning = True
            sel.warning = f"Rail {i} was extended."
        out = _payload(self._rails(monkeypatch, prep))
        assert out["selection_warning"] == "Rail 0 was extended., Rail 1 was extended."


# ── the 'surfaces' selection: a strategy's surface SET (a CadObject parameter) ───────────────
#
# The surface-driven strategies hold their drive/floor/wall/ceiling/check surfaces in the same
# CadObjectParameterValue shape the hole faces use - assign a list of faces to .value. One op can carry
# SEVERAL at once (measured 2705.1.4: multiaxis_finishing floor AND wall, multiaxis_roughing floor AND
# ceiling, geodesic drive AND check), so which set the faces are is an INPUT, and a set the op does not
# carry is refused naming the ones it does rather than guessed at.
#
# The op's 'model' parameter is the same class and is deliberately NOT a target: assigning a face list
# to it raises '3 : Parameter is not available through the API.' (measured), so the ops faked below
# carry one and no vocabulary or refusal listing may name it.
#
# The assignment itself proves nothing - the read-back COUNT off the parameter is the gate.


class _ShortSurfaceParam:
    """Accepts the assignment and keeps only the first face - a read-back that is a wrong COUNT, the
    swallow a bare 'the assignment did not raise' gate reports as applied."""
    def __init__(self):
        self._v = []

    @property
    def value(self):
        return self._v

    @value.setter
    def value(self, v):
        self._v = list(v)[:1]


class _UnreadableSurfaceParam:
    """Takes the assignment; nothing reads back off the parameter afterwards."""
    def __init__(self):
        self._written = False

    @property
    def value(self):
        if self._written:
            raise RuntimeError("the surface set is unreadable")
        return []

    @value.setter
    def value(self, _v):
        self._written = True


class _RefusingSurfaceParam:
    """Refuses the assignment outright."""
    @property
    def value(self):
        return []

    @value.setter
    def value(self, _v):
        raise RuntimeError("the surface set is read-only here")


def _geodesic_op(name="Geodesic1", drive=None, **kw):
    """A geodesic op: driveSurfaces + checkSurfaceSelection + model are its surface sets and
    machiningBoundarySel its curve one (measured)."""
    return _Op(name, {"machiningBoundarySel": _Param(_CurveParamValue()),
                      "boundaryMode": _BoundaryModeParam("automatic"),
                      "driveSurfaces": _Param(drive if drive is not None else _HoleParamValue()),
                      "checkSurfaceSelection": _Param(_HoleParamValue()),
                      "model": _Param(_HoleParamValue())}, **kw)


def _chamfer_op(name="3D Chamfer1", **kw):
    """A 3D chamfer op: its one surface set is the DEPRECATED checkSurfaceSelection, which reads
    isEditable False and raises '3 : Parameter is deprecated' on assignment (measured)."""
    return _Op(name, {"checkSurfaceSelection": _Param(_HoleParamValue(), editable=False)}, **kw)


def _advanced_swarf_op(name="Advanced Swarf1", **kw):
    """A fresh advanced_swarf op: advancedSwarfSurfaces is its ONE surface set and reads editable,
    its swarf contour parameters read isEditable False (inert - never probed), and no name in the
    curve-parameter probe order, 'curves' included, resolves on it
    (cam-advanced-swarf-surface-set-editable)."""
    return _Op(name, {"advancedSwarfSurfaces": _Param(_HoleParamValue()),
                      "swarfUpperContour": _Param(_CurveParamValue(), editable=False),
                      "swarfLowerContour": _Param(_CurveParamValue(), editable=False)}, **kw)


def _ma_finishing_op(name="Multi-Axis Finishing1", **kw):
    """A multi-axis finishing op: floorSurfaces + wallSurfaces + model, and NO driveSurfaces
    (measured) - the shape an omitted surface_target has nothing to default to on."""
    return _Op(name, {"machiningBoundarySel": _Param(_CurveParamValue()),
                      "boundaryMode": _BoundaryModeParam("none"),
                      "floorSurfaces": _Param(_HoleParamValue()),
                      "wallSurfaces": _Param(_HoleParamValue()),
                      "model": _Param(_HoleParamValue())}, **kw)


class TestSurfaceSelection:
    def test_faces_land_on_drive_surfaces_by_default(self, monkeypatch):
        op = _geodesic_op()
        cam = _CAM([_Setup([op])])
        faces = [_Face(), _Face(), _Face()]
        _install(monkeypatch, cam, faces)
        out = _payload(cg.handler(operation="Geodesic1", selection="surfaces",
                                  handles=["a", "b", "c"], generate=False))
        assert op.parameters.itemByName("driveSurfaces").value.value == faces
        assert out["selections"] == 3
        assert out["surface_target"] == "drive" and out["surface_param"] == "driveSurfaces"

    def test_the_target_routes_the_faces_to_that_set(self, monkeypatch):
        op = _ma_finishing_op()
        cam = _CAM([_Setup([op])])
        faces = [_Face(), _Face()]
        _install(monkeypatch, cam, faces)
        out = _payload(cg.handler(operation="Multi-Axis Finishing1", selection="surfaces",
                                  handles=["a", "b"], surface_target="wall", generate=False))
        assert op.parameters.itemByName("wallSurfaces").value.value == faces
        assert op.parameters.itemByName("floorSurfaces").value.value == []
        assert out["surface_param"] == "wallSurfaces"

    def test_a_read_back_count_short_of_the_assignment_is_an_error(self, monkeypatch):
        # the assignment not raising is not evidence: a parameter that keeps one of three faces
        # would otherwise be published as three surfaces selected.
        op = _geodesic_op(drive=_ShortSurfaceParam())
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Face(), _Face(), _Face()])
        res = cg.handler(operation="Geodesic1", selection="surfaces", handles=["a", "b", "c"],
                         generate=False)
        assert res["isError"] is True and "did not take" in res["message"]
        assert "3 face(s) were assigned" in res["message"] and "reads back 1" in res["message"]

    def test_a_surface_set_that_cannot_be_read_back_is_unconfirmed(self, monkeypatch):
        op = _geodesic_op(drive=_UnreadableSurfaceParam())
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Face()])
        res = cg.handler(operation="Geodesic1", selection="surfaces", handles=["a"], generate=False)
        assert res["isError"] is True and "UNCONFIRMED" in res["message"]
        assert "driveSurfaces" in res["message"]

    def test_an_assignment_that_raises_is_an_error(self, monkeypatch):
        op = _geodesic_op(drive=_RefusingSurfaceParam())
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Face()])
        res = cg.handler(operation="Geodesic1", selection="surfaces", handles=["a"], generate=False)
        assert res["isError"] is True
        assert "Could not set driveSurfaces" in res["message"] and "read-only here" in res["message"]

    def test_an_omitted_target_is_refused_where_there_is_no_drive_set_to_default_to(self, monkeypatch):
        # multi-axis finishing carries floor AND wall: defaulting to either would silently machine
        # the wrong one, and both read back the same count.
        op = _ma_finishing_op()
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Face()])
        res = cg.handler(operation="Multi-Axis Finishing1", selection="surfaces", handles=["a"],
                         generate=False)
        assert res["isError"] is True and "surface_target" in res["message"]
        # The listing is compared WHOLE, not for the presence of the two it should name: a listing
        # built from the vocabulary instead of the sets this op carries contains those two as well,
        # and would send the caller at drive/ceiling/check parameters the op has not got. The op HAS
        # a 'model' parameter, and the platform refuses a face-list write to it, so that one is out
        # of the vocabulary and cannot appear either.
        assert "It carries floor, wall - pass" in res["message"]
        assert "model" not in res["message"]
        assert op.parameters.itemByName("floorSurfaces").value.value == []
        assert op.parameters.itemByName("wallSurfaces").value.value == []

    def test_a_target_the_operation_does_not_carry_is_refused_naming_what_it_does(self, monkeypatch):
        op = _ma_finishing_op()
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Face()])
        res = cg.handler(operation="Multi-Axis Finishing1", selection="surfaces", handles=["a"],
                         surface_target="ceiling", generate=False)
        assert res["isError"] is True
        assert "ceilingSurfaces" in res["message"] and "ceiling" in res["message"]
        # whole-listing compare, for the same reason as the refusal above: the sets this op carries,
        # not the vocabulary it was asked from.
        assert res["message"].endswith("It carries floor, wall.")
        assert "model" not in res["message"]

    def test_the_model_parameter_is_not_an_offered_target(self, monkeypatch):
        # Every milling op carries a 'model' CadObject parameter, but assigning a face list to it
        # raises '3 : Parameter is not available through the API.' - so it is outside the vocabulary
        # and the Choice refuses it, rather than the handler reaching a write that cannot land.
        op = _geodesic_op()
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Face()])
        res = cg.handler(operation="Geodesic1", selection="surfaces", handles=["a"],
                         surface_target="model", generate=False)
        assert res["isError"] is True and "model" in res["message"]
        assert op.parameters.itemByName("model").value.value == []

    def test_an_operation_with_no_surface_sets_at_all_is_refused(self, monkeypatch):
        op = _curve_op()
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Face()])
        res = cg.handler(operation="2D Contour1", selection="surfaces", handles=["a"],
                         generate=False)
        assert res["isError"] is True and "none of the surface sets" in res["message"]

    def test_a_surface_set_that_is_not_editable_is_not_offered(self, monkeypatch):
        # offering a set whose parameter is not editable sends the caller at an assignment that
        # raises - so the listing names only what a face list can actually land on.
        op = _Op("Geodesic1", {"driveSurfaces": _Param(_HoleParamValue()),
                               "checkSurfaceSelection": _Param(_HoleParamValue(), editable=False)})
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Face()])
        res = cg.handler(operation="Geodesic1", selection="surfaces", handles=["a"],
                         surface_target="check", generate=False)
        assert res["isError"] is True
        # not "has no 'checkSurfaceSelection' parameter": it has one, and cannot take faces
        assert "carries 'checkSurfaceSelection' but it did not read isEditable true" in res["message"]
        assert res["message"].endswith("It carries drive.")
        assert op.parameters.itemByName("checkSurfaceSelection").value.value == []

    def test_an_operation_whose_only_surface_set_is_not_editable_carries_none(self, monkeypatch):
        op = _chamfer_op()
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Face()])
        res = cg.handler(operation="3D Chamfer1", selection="surfaces", handles=["a"],
                         generate=False)
        assert res["isError"] is True and "no SETTABLE surface set" in res["message"]
        assert "checkSurfaceSelection did not read isEditable true" in res["message"]
        assert op.parameters.itemByName("checkSurfaceSelection").value.value == []

    def test_the_swarf_target_routes_the_faces_to_the_advanced_swarf_set(self, monkeypatch):
        op = _advanced_swarf_op()
        cam = _CAM([_Setup([op])])
        faces = [_Face(), _Face()]
        _install(monkeypatch, cam, faces)
        out = _payload(cg.handler(operation="Advanced Swarf1", selection="surfaces",
                                  handles=["a", "b"], surface_target="swarf", generate=False))
        assert op.parameters.itemByName("advancedSwarfSurfaces").value.value == faces
        assert out["surface_param"] == "advancedSwarfSurfaces" and out["selections"] == 2

    def test_an_omitted_target_on_the_swarf_op_names_the_one_set_it_can_take(self, monkeypatch):
        # its non-editable floor/check sets stay out of the listing, so the caller is sent at the
        # only set an assignment lands on.
        op = _advanced_swarf_op()
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Face()])
        res = cg.handler(operation="Advanced Swarf1", selection="surfaces", handles=["a"],
                         generate=False)
        assert res["isError"] is True and "It carries swarf - pass" in res["message"]
        assert "floor" not in res["message"] and "check" not in res["message"]

    def test_a_curve_selection_on_a_surface_driven_operation_names_its_surface_sets(self, monkeypatch):
        # the dead end an agent otherwise meets: no curve parameter AND no pointer to the kind that
        # does reach this strategy's geometry.
        op = _advanced_swarf_op()
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Edge()])
        res = cg.handler(operation="Advanced Swarf1", selection="chain", handles=["a"],
                         generate=False)
        assert res["isError"] is True and "no curve-selection parameter" in res["message"]
        assert "It carries the surface set(s) swarf - pass selection='surfaces'" in res["message"]

    def test_a_curve_selection_on_an_operation_whose_surface_sets_are_blocked_says_so(
            self, monkeypatch):
        # the full dead end: no curve parameter AND a surface set that cannot take faces - the
        # refusal names the read that closed the door instead of sending the caller at drilling.
        op = _chamfer_op()
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Edge()])
        res = cg.handler(operation="3D Chamfer1", selection="chain", handles=["a"],
                         generate=False)
        assert res["isError"] is True and "no curve-selection parameter" in res["message"]
        assert "checkSurfaceSelection did not read isEditable true" in res["message"]
        assert "drilling" not in res["message"]

    def test_a_bad_target_value_is_refused_by_the_choice(self, monkeypatch):
        op = _geodesic_op()
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Face()])
        res = cg.handler(operation="Geodesic1", selection="surfaces", handles=["a"],
                         surface_target="rooftop", generate=False)
        assert res["isError"] is True and "rooftop" in res["message"]

    def test_surface_target_on_another_selection_kind_is_refused(self, monkeypatch):
        # a knob silently dropped leaves the caller believing they routed the faces somewhere.
        op = _curve_op()
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Edge()])
        res = cg.handler(operation="2D Contour1", selection="chain", handles=["a"],
                         surface_target="drive", generate=False)
        assert res["isError"] is True
        assert "surface_target" in res["message"] and "'chain'" in res["message"]

    def test_the_surfaces_kind_takes_its_geometry_from_handles(self, monkeypatch):
        op = _geodesic_op()
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Face()])
        res = cg.handler(operation="Geodesic1", selection="surfaces", bodies=["Carrier"],
                         generate=False)
        assert res["isError"] is True and "'handles'" in res["message"]

    def test_a_surfaces_feed_engages_no_curve_mode(self, monkeypatch):
        # geodesic also carries machiningBoundarySel; writing a surface set is not a boundary feed,
        # so boundaryMode must be left exactly as found.
        op = _geodesic_op()
        cam = _CAM([_Setup([op])])
        _install(monkeypatch, cam, [_Face()])
        out = _payload(cg.handler(operation="Geodesic1", selection="surfaces", handles=["a"],
                                  generate=False))
        assert cg.unquote_expression(
            op.parameters.itemByName("boundaryMode").expression) == "automatic"
        assert "boundary_engaged" not in out


class TestExplicitChainGroups:
    def test_separate_contours_reach_apply_as_distinct_selections(self, monkeypatch):
        op = _curve_op()
        edges = [_Edge(), _Edge(), _Edge()]
        _install(monkeypatch, _CAM([_Setup([op])]), edges)
        pv = op.parameters.itemByName("contours").value
        applied = []
        original = pv.applyCurveSelections
        def apply(cs):
            applied.extend([list(cs.item(i).inputGeometry) for i in range(cs.count)])
            original(cs)
        monkeypatch.setattr(pv, "applyCurveSelections", apply)
        out = _payload(cg.handler(operation="2D Contour1", selection="chain",
                                  chain_groups=[["a", "b"], ["c"]], generate=False))
        assert applied == [edges[:2], edges[2:]]
        assert out["chain_groups_read"] == 2

    def test_flat_multiple_edges_refuse_before_height_or_selection_changes(self, monkeypatch):
        op = _curve_op()
        _install(monkeypatch, _CAM([_Setup([op])]), [_Edge(), _Edge()])
        def forbidden(*_args):
            raise AssertionError("height mutation")
        monkeypatch.setattr(cg, "_set_height", forbidden)
        res = cg.handler(operation="2D Contour1", selection="chain", handles=["a", "b"],
                         bottom_offset="99 mm", generate=False)
        assert res["isError"] is True and "chain_groups" in res["message"]
        assert op.parameters.itemByName("contours").value.applied == 0

    def test_lost_group_cannot_report_success(self, monkeypatch):
        op = _curve_op()
        _install(monkeypatch, _CAM([_Setup([op])]), [_Edge(), _Edge()])
        pv = op.parameters.itemByName("contours").value
        def lose_group(cs):
            cs._items.pop()
            pv._cs = cs
        monkeypatch.setattr(pv, "applyCurveSelections", lose_group)
        res = cg.handler(operation="2D Contour1", selection="chain",
                         chain_groups=[["a"], ["b"]], generate=False)
        assert res["isError"] is True and "read back 1" in res["message"]
        assert "changes remain" in res["message"]

    @pytest.mark.parametrize("groups", [[], [[]], ["a"], [[None]]])
    def test_invalid_groups_refuse(self, groups):
        res = cg.handler(operation="X", selection="chain", chain_groups=groups)
        assert res["isError"] is True and "chain_groups" in res["message"]
