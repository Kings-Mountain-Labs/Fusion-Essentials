"""Unit tests for ``joint_create.py`` pure logic, and the joint substrate it builds on.

Targets: ``_available_joint_origins`` (the collect-names leaf over the shared _joints JO walk),
``_resolve_input`` (handle / snap / JO-name dispatch, routing the JO-name path through the
JointOriginRef kind), ``_apply_motion`` (dispatch by joint type, incl. the unsupported-type
fallthrough), ``_apply_limits`` (the read-back writer create and edit share) and the create
handler end to end. Resolve-one/qualified/ambiguity of a JO by name is the JointOriginRef kind's
contract - tested in test_inputs.py, not here.
"""

import math as _math
from types import SimpleNamespace

import adsk.core
import adsk.fusion

from conftest import (BRepBody, BRepEdge, BRepFace, Circle3D, Cone, CylindricalJointMotion,
                      Cylinder, FakeJoint, FakeJointInput, FakeJoints, FakeOccurrence,
                      FakeTimelineObject, Line3D, MakeComp, Plane, RevoluteJointMotion,
                      SliderJointMotion, _MotionLimits, _NamedCollection, _Vertex, install,
                      load_tool, make_bbox, make_design)
from conftest import payload as _payload

joint = load_tool("joint_create")
jedit = load_tool("joint_edit")
ji = load_tool("_joint_inputs")
jn = load_tool("_joints")

_JD = adsk.fusion.JointDirections
_HEALTH = adsk.fusion.FeatureHealthStates


def _jo(name):
    """One JointOrigin by name - the resolvers read only that (JointOrigin has no SHAPES dump)."""
    return SimpleNamespace(name=name)


def _jos(*names):
    """A component's jointOrigins: count/item (the shared _joints walk) AND itemByName."""
    return _NamedCollection([_jo(n) for n in names])


# ── _apply_motion: dispatch + fallthrough ──────────────────────────────────

class _JointInput(FakeJointInput):
    """The shared input, with `called` this file's read of the setter it recorded."""
    @property
    def called(self):
        return self._motion


class TestApplyMotion:
    def test_rigid(self):
        ji = _JointInput()
        ok, err = joint._apply_motion(ji, "rigid", 2)
        assert ok is True and err is None
        assert ji.called == ("rigid",)

    def test_slider_uses_axis_index(self):
        ji = _JointInput()
        ok, err = joint._apply_motion(ji, "slider", 0)  # X axis
        assert ok is True and err is None
        assert ji.called[0] == "slider"

    def test_unsupported_type_reports_error(self):
        ji = _JointInput()
        ok, err = joint._apply_motion(ji, "warp_drive", 2)
        assert ok is False
        assert "warp_drive" in err
        assert ji.called is None


# ── _apply_motion: pin_slot dispatch (rotation axis + distinct slide direction) ─────────────────────

class TestApplyMotionPinSlot:
    def test_default_slide_is_next_frame_axis(self):
        ji = _JointInput()
        ok, err = joint._apply_motion(ji, "pin_slot", 2)   # rotation = z
        assert ok is True and err is None
        kind, rot, slide, rest = ji.called
        assert kind == "pin_slot"
        assert rot is _JD.ZAxisJointDirection               # rotation = axis_idx 2
        assert slide is _JD.XAxisJointDirection             # slide default = (2+1)%3 = 0 -> x
        assert rest == ()                                   # no custom entity

    def test_explicit_slide_axis_used(self):
        ji = _JointInput()
        ok, err = joint._apply_motion(ji, "pin_slot", 0, slide_axis_idx=2)   # rot x, slide z
        assert ok is True and err is None
        kind, rot, slide, rest = ji.called
        assert rot is _JD.XAxisJointDirection and slide is _JD.ZAxisJointDirection

    def test_slide_equal_to_rotation_refused(self):
        ji = _JointInput()
        ok, err = joint._apply_motion(ji, "pin_slot", 1, slide_axis_idx=1)
        assert ok is False
        assert "differ" in err
        assert ji.called is None                            # setter never reached

    def test_custom_entity_repoints_rotation_slide_stays_frame(self):
        ji = _JointInput()
        sentinel = object()
        ok, err = joint._apply_motion(ji, "pin_slot", 2, custom_entity=sentinel)
        assert ok is True and err is None
        kind, rot, slide, rest = ji.called
        assert rot is _JD.CustomJointDirection              # rotation re-pointed to the custom axis
        assert slide is _JD.XAxisJointDirection             # slide still frame-relative
        assert rest == (sentinel,)


# ── _slide_index / _slide_name: pin_slot slide-axis resolution ──────────────────────────────────────

class TestSlideAxisHelpers:
    def test_blank_defaults_to_none(self):
        assert ji._slide_index("", "z") == (None, None)

    def test_valid_distinct_axis(self):
        assert ji._slide_index("x", "z") == (0, None)

    def test_same_as_rotation_errors(self):
        idx, err = ji._slide_index("z", "z")
        assert idx is None and "differ" in err

    def test_unknown_axis_errors(self):
        idx, err = ji._slide_index("w", "z")
        assert idx is None and "Unknown slide_axis" in err

    def test_slide_name_default_is_perpendicular(self):
        assert ji._slide_name(None, "z") == "x"
        assert ji._slide_name(None, "x") == "y"
        assert ji._slide_name(2, "x") == "z"


# ── handler: pin_slot flows through create and reports the effective slide axis ─────────────────────

def _fake_design_for_create(joint_input, joint_obj):
    """A design whose root joints hand back this JointInput and this Joint."""
    root = MakeComp(name="Root")
    root.joints = FakeJoints(joint_input=joint_input, new_joint=joint_obj)
    return make_design(comp=root)


class TestCreatePinSlot:
    def _wire(self, monkeypatch, joint_input, joint_obj):
        design = install(joint, _fake_design_for_create(joint_input, joint_obj))
        monkeypatch.setattr(joint, "_resolve_input",
                            lambda d, spec: (SimpleNamespace(name=spec), spec, None))
        return design

    def test_create_pin_slot_reports_default_slide_axis(self, monkeypatch):
        ji = _JointInput()
        self._wire(monkeypatch, ji, FakeJoint(name="Joint1"))
        out = _payload(joint.handler(occurrence_one="A", occurrence_two="B",
                                     joint_type="pin_slot", axis="z"))
        assert out["created"] is True
        assert out["joint_type"] == "pin_slot"
        assert out["axis"] == "z"
        assert out["slide_axis"] == "x"                     # default perpendicular, surfaced
        assert ji.called[0] == "pin_slot"

    def test_create_pin_slot_explicit_slide_axis(self, monkeypatch):
        ji = _JointInput()
        self._wire(monkeypatch, ji, FakeJoint(name="Joint1"))
        out = _payload(joint.handler(occurrence_one="A", occurrence_two="B",
                                     joint_type="pin_slot", axis="x", slide_axis="y"))
        assert out["slide_axis"] == "y"

    def test_create_pin_slot_slide_equal_axis_refused_before_build(self, monkeypatch):
        ji = _JointInput()
        self._wire(monkeypatch, ji, FakeJoint(name="Joint1"))
        res = joint.handler(occurrence_one="A", occurrence_two="B",
                            joint_type="pin_slot", axis="z", slide_axis="z")
        assert res["isError"] is True
        assert "differ" in res["message"]
        assert ji.called is None                            # never reached the motion setter



# ── _apply_limits: shared by create + edit; rotation(rad) vs linear(cm) ──────

class TestApplyLimits:
    def test_rotation_in_radians(self):
        m = RevoluteJointMotion()
        changed, unverified, err = ji._apply_limits(m, min_deg=-45, max_deg=90)
        assert err is None and unverified == []
        assert m.rotationLimits.isMinimumValueEnabled and m.rotationLimits.isMaximumValueEnabled
        assert abs(m.rotationLimits.minimumValue - _math.radians(-45)) < 1e-9
        assert abs(m.rotationLimits.maximumValue - _math.radians(90)) < 1e-9
        assert changed["min_deg"] == -45 and changed["max_deg"] == 90

    def test_linear_in_cm(self):
        m = SliderJointMotion()
        changed, unverified, err = ji._apply_limits(m, min_mm=0, max_mm=300, cm_scale=0.1)
        assert err is None and unverified == []
        assert abs(m.slideLimits.maximumValue - 30.0) < 1e-9   # 300 mm -> 30 cm
        assert changed["max_mm"] == 300

    def test_rest_values(self):
        m = RevoluteJointMotion()
        ji._apply_limits(m, rest_deg=10)
        assert m.rotationLimits.isRestValueEnabled
        assert abs(m.rotationLimits.restValue - _math.radians(10)) < 1e-9

    def test_rotation_on_slider_errors(self):
        m = SliderJointMotion()
        changed, unverified, err = ji._apply_limits(m, min_deg=10)
        assert err is not None and "rotation" in err.lower()

    def test_linear_on_revolute_errors(self):
        m = RevoluteJointMotion()
        changed, unverified, err = ji._apply_limits(m, max_mm=100)
        assert err is not None and ("slide" in err.lower() or "linear" in err.lower())

    def test_inverted_rotation_pair_is_refused_before_any_write(self):
        # min > max is an EMPTY feasible range that silently makes the joint undrivable while every
        # health field reads healthy - refused, and nothing is enabled on the motion.
        m = RevoluteJointMotion()
        changed, unverified, err = ji._apply_limits(m, min_deg=60, max_deg=-60)
        assert err is not None and "INVERTED" in err
        assert changed == {} and m.rotationLimits.isMinimumValueEnabled is False

    def test_inverted_slide_pair_is_refused_before_any_write(self):
        m = SliderJointMotion()
        changed, unverified, err = ji._apply_limits(m, min_mm=50, max_mm=10, cm_scale=0.1)
        assert err is not None and "INVERTED" in err
        assert changed == {} and m.slideLimits.isMaximumValueEnabled is False


class _DeafLim(_MotionLimits):
    """A JointLimits that ACCEPTS every assignment and keeps its own value (and optionally its own
    enabled flag) - the platform shape a request-echoing payload cannot tell from a landed write."""
    def __init__(self, held_value=0.0, hold_flag=None):
        object.__setattr__(self, "_held", held_value)
        object.__setattr__(self, "_hold_flag", hold_flag)
        super().__init__()

    def __setattr__(self, name, value):
        if name in ("minimumValue", "maximumValue", "restValue"):
            return object.__setattr__(self, name, object.__getattribute__(self, "_held"))
        held_flag = object.__getattribute__(self, "_hold_flag")
        if name.startswith("is") and held_flag is not None:
            return object.__setattr__(self, name, held_flag)
        return object.__setattr__(self, name, value)


class _BlindLim(_MotionLimits):
    """A JointLimits whose value read RAISES after the assignment - the unreadable re-read."""
    def __init__(self, blind_flag=False):
        object.__setattr__(self, "_blind_flag", blind_flag)
        super().__init__()

    def __getattribute__(self, name):
        if name in ("minimumValue", "maximumValue", "restValue"):
            raise RuntimeError("limit value unreadable")
        if (name.startswith("is") and name.endswith("Enabled")
                and object.__getattribute__(self, "_blind_flag")):
            raise RuntimeError("limit flag unreadable")
        return object.__getattribute__(self, name)


class _BandLim(_MotionLimits):
    """A JointLimits that lands every value OFF by a fixed amount, given in the caller's own units
    (degrees) so a test can sit either side of the read-back band."""
    def __init__(self, deg_error):
        # Armed only after the base constructor, so the limit STARTS as _MotionLimits describes.
        object.__setattr__(self, "_deg_error", None)
        super().__init__()
        object.__setattr__(self, "_deg_error", deg_error)

    def __setattr__(self, name, value):
        off = object.__getattribute__(self, "_deg_error")
        if off is not None and name in ("minimumValue", "maximumValue", "restValue"):
            return object.__setattr__(self, name, value + _math.radians(off))
        return object.__setattr__(self, name, value)


class TestLimitsAreReadBack:
    """Every limit and its enabled flag is published as the JOINT READS IT BACK, never as the
    request: a JointLimits assignment that the platform keeps at its own value leaves a payload
    built from the request claiming a limit that is not in force."""

    def test_a_landed_limit_publishes_the_read_back_value(self):
        m = RevoluteJointMotion()
        changed, unverified, err = ji._apply_limits(m, min_deg=-45)
        assert err is None and unverified == []
        assert changed["min_deg"] == -45.0

    def test_the_published_number_is_the_read_back_not_the_request(self):
        # the joint lands the limit slightly off (inside the band, so no refusal): the payload must
        # carry what the JOINT holds, or a caller reading it back learns nothing it did not send
        m = RevoluteJointMotion()
        m.rotationLimits = _BandLim(ji._LIMIT_BAND * 0.5)
        changed, unverified, err = ji._apply_limits(m, min_deg=-45)
        assert err is None
        assert changed["min_deg"] == -44.9995        # the read-back, not the requested -45

    def test_a_rotation_limit_that_did_not_take_errors_naming_it(self):
        m = RevoluteJointMotion()
        m.rotationLimits = _DeafLim(held_value=0.0)
        changed, unverified, err = ji._apply_limits(m, min_deg=-45)
        assert err is not None
        assert "min_deg did not take" in err
        assert "-45" in err and "0.0" in err          # requested vs what the joint reads back
        assert changed == {}                          # the failed limit is not published as applied

    def test_a_slide_limit_that_did_not_take_errors_naming_it(self):
        m = SliderJointMotion()
        m.slideLimits = _DeafLim(held_value=0.0)
        changed, unverified, err = ji._apply_limits(m, max_mm=300, cm_scale=0.1)
        assert err is not None and "max_mm did not take" in err
        assert "300" in err

    def test_an_enabled_flag_that_reads_back_false_errors(self):
        # the value can land while the joint keeps the limit DISABLED - then it constrains nothing
        m = RevoluteJointMotion()
        m.rotationLimits = _DeafLim(held_value=_math.radians(-45), hold_flag=False)
        changed, unverified, err = ji._apply_limits(m, min_deg=-45)
        assert err is not None
        assert "min_deg did not take" in err and "isMinimumValueEnabled" in err

    def test_earlier_limits_that_landed_are_kept_in_changed(self):
        # the partial-success disclosure both handlers build reads `changed` - a limit that landed
        # before the failing one must still be named there
        m = RevoluteJointMotion()
        changed, unverified, err = ji._apply_limits(m, min_deg=-45, max_deg=90, rest_deg=10)
        assert err is None and set(changed) == {"min_deg", "max_deg", "rest_deg"}
        m2 = RevoluteJointMotion()
        m2.rotationLimits = _DeafLim(held_value=_math.radians(-45))
        changed2, _unv, err2 = ji._apply_limits(m2, min_deg=-45, max_deg=90)
        assert err2 is not None and "max_deg did not take" in err2
        assert changed2 == {"min_deg": -45.0}         # the one that landed, read back

    def test_an_unreadable_read_back_publishes_null_and_a_marker(self):
        # the value read RAISES: publishing the request would report a write nobody confirmed
        m = RevoluteJointMotion()
        m.rotationLimits = _BlindLim()
        changed, unverified, err = ji._apply_limits(m, min_deg=-45)
        assert err is None
        assert changed == {"min_deg": None} and unverified == ["min_deg"]

    def test_an_unreadable_enabled_flag_also_publishes_null(self):
        # an unreadable FLAG is not a False - it is unknown, so the limit is unverified, not failed
        m = SliderJointMotion()
        m.slideLimits = _BlindLim(blind_flag=True)
        changed, unverified, err = ji._apply_limits(m, min_mm=5, cm_scale=0.1)
        assert err is None
        assert changed == {"min_mm": None} and unverified == ["min_mm"]


class TestLimitReadBackBand:
    """The read-back is compared in the REQUEST'S own units (a limit round-trips deg -> rad -> deg),
    within _LIMIT_BAND. Both sides of that edge are pinned - a units round trip must not error, a
    real miss must not slip under the band - and the edge itself: a difference EQUAL to the band is
    inside it (the comparison is `>`, not `>=`)."""

    def _apply(self, deg_error, wanted=-45):
        m = RevoluteJointMotion()
        m.rotationLimits = _BandLim(deg_error)
        return ji._apply_limits(m, min_deg=wanted)

    def test_a_difference_inside_the_band_lands(self):
        changed, unverified, err = self._apply(ji._LIMIT_BAND * 0.5)
        assert err is None and unverified == []

    def test_a_difference_exactly_at_the_band_lands(self):
        # min_deg=0 is the one request whose round trip lands the difference EXACTLY on the band
        # (at -45 the float error puts it just under), so this is what separates `>` from `>=`.
        changed, unverified, err = self._apply(ji._LIMIT_BAND, wanted=0)
        assert err is None, "a difference equal to the band is inside it"
        assert abs(changed["min_deg"] - 0.0) == ji._LIMIT_BAND, "the boundary case itself moved"

    def test_a_difference_beyond_the_band_errors(self):
        changed, unverified, err = self._apply(ji._LIMIT_BAND * 2.0)
        assert err is not None and "min_deg did not take" in err

    def test_the_band_is_small_enough_to_catch_a_real_miss(self):
        # a limit that landed a whole degree off is a wrong limit, not round-trip noise
        assert self._apply(1.0)[2] is not None


# ── _resolve_input: the geometry-as-values HANDLE path ──────────
#
# A find_geometry handle is now a first-class joint input: it resolves to a JointGeometry AT the real
# geometry, instead of the ':origin' snap that collapses both parts to (0,0,0). Distinguished from a
# JointOrigin NAME by RESOLVING — a token that findEntityByToken yields nothing for falls through to the
# snap/JO-name paths (so existing inputs keep working).

def _install_resolve_seam(monkeypatch, token_map):
    """The JointGeometry recorder, the entity kinds the resolver branches on, and a design whose
    findEntityByToken answers `token_map`."""
    # JointGeometry factory -> a sentinel recorder so we can assert which builder ran.
    rec = type("R", (), {
        "createByPlanarFace": staticmethod(lambda f, e, kp: ("planar", kp)),
        "createByNonPlanarFace": staticmethod(lambda f, kp: ("nonplanar", kp)),
        "createByCurve": staticmethod(lambda c, kp: ("curve", kp)),
        "createByPoint": staticmethod(lambda p: ("point",)),
    })
    monkeypatch.setattr(adsk.fusion, "JointGeometry", rec)
    monkeypatch.setattr(adsk.fusion, "BRepFace", BRepFace)
    monkeypatch.setattr(adsk.fusion, "BRepEdge", BRepEdge)
    monkeypatch.setattr(adsk.fusion, "BRepVertex", _Vertex)
    monkeypatch.setattr(adsk.fusion, "ConstructionPoint", type("CP", (), {}))
    monkeypatch.setattr(adsk.fusion, "SketchPoint", type("SP", (), {}))
    # a real type so is_joint_origin() works
    monkeypatch.setattr(adsk.fusion, "JointOrigin", type("JointOrigin", (), {}))
    # install() points BOTH design seams at one design: the handler reads joint._common, and the
    # JointOriginRef kind (the JO-name path) resolves through joint._inputs._common.
    return install(joint, make_design(comp=MakeComp(name="Root", joint_origins=[]),
                                      tokens=dict(token_map)))


class TestResolveInputHandle:
    def test_handle_resolves_to_joint_geometry_at_real_face(self, monkeypatch):
        face = BRepFace(Plane(None))
        design = _install_resolve_seam(monkeypatch, {"H_FACE": face})
        g, label, err = ji._resolve_input(design, "H_FACE")
        import adsk.fusion
        assert err is None
        # built a JointGeometry from the face, not an origin
        assert g == ("planar", adsk.fusion.JointKeyPointTypes.CenterKeyPoint)
        assert label.startswith("handle:")

    def test_non_token_falls_through_to_jo_name(self, monkeypatch):
        # 'JO_A' is not a resolvable token -> handle path declines, JO-name path (JointOriginRef) resolves it.
        design = _install_resolve_seam(monkeypatch, {})
        target = _jo("JO_A")
        design.rootComponent.jointOrigins = _NamedCollection([target])
        g, label, err = ji._resolve_input(design, "JO_A")
        assert err is None and g is target and label == "JO_A"

    def test_joint_origin_handle_used_directly(self, monkeypatch):
        # A JOINT ORIGIN handle (assembly_get mints these) is a first-class joint input - used directly,
        # NOT run through build_joint_geometry (which would reject it).
        design = _install_resolve_seam(monkeypatch, {})
        jo = adsk.fusion.JointOrigin()
        jo.name = "Stock_Center"
        # the JO is reachable ONLY as a handle - no jointOrigins entry carries it
        design._tokens["H_JO"] = jo
        g, label, err = ji._resolve_input(design, "H_JO")
        assert err is None and g is jo and label == "handle:joint_origin"

    def test_unresolvable_spec_errors_naming_all_paths(self, monkeypatch):
        design = _install_resolve_seam(monkeypatch, {})
        g, label, err = ji._resolve_input(design, "Nope")
        assert g is None
        assert "handle" in err and "Joint Origin" in err and "snap" in err


# The resolve-one / qualified '<occurrence>:<JO name>' / ambiguity contract now lives on the
# JointOriginRef kind - exercised in test_inputs.py (TestJointOriginRef), not here.

# ── resolve-failure error lists the design's Joint Origins (self-correction data) ───────────

class TestResolveErrorListsJointOrigins:
    def _design_with_jos(self, monkeypatch):
        root = MakeComp(name="RootComp", joint_origins=[_jo("Attach Center of Workpiece")])
        sub = MakeComp(name="SculpturalTower", joint_origins=[_jo("Center of Model")])
        _install_resolve_seam(monkeypatch, {})
        return install(joint, make_design(comp=root, all_components=[root, sub]))

    def test_error_names_each_jo_and_owner(self, monkeypatch):
        design = self._design_with_jos(monkeypatch)
        g, label, err = ji._resolve_input(design, "Wrong Name")
        assert g is None
        assert "'Attach Center of Workpiece' (root)" in err
        assert "'Center of Model' (in component 'SculpturalTower')" in err

    def test_listing_is_capped_with_overflow_count(self, monkeypatch):
        design = self._design_with_jos(monkeypatch)
        design.rootComponent.jointOrigins = _jos(*[f"JO_{i}" for i in range(12)])
        listed, more = ji._available_joint_origins(design, limit=8)
        assert len(listed) == 8
        assert more == 5  # 12 root + 1 sub-component JO, 8 listed

    def test_no_jos_keeps_error_unadorned(self, monkeypatch):
        design = _install_resolve_seam(monkeypatch, {})
        g, label, err = ji._resolve_input(design, "Nope")
        assert "Joint Origins in this design" not in err




# ── _jg_from_entity: VALID keypoint per entity kind (the runtime rule) ──────
# Mirrors joint_at_geometry: a planar face -> CenterKeyPoint, a cyl/cone (non-planar) face ->
# MiddleKeyPoint (CenterKeyPoint is invalid there), a circular edge -> center, a line edge ->
# middle, a vertex/point -> createByPoint.

def _jg_seam(monkeypatch):
    """Install the JointGeometry recorder + the entity kinds the resolver branches on."""
    rec = type("R", (), {
        "createByPlanarFace": staticmethod(lambda f, e, kp: ("planar", kp)),
        "createByNonPlanarFace": staticmethod(lambda f, kp: ("nonplanar", kp)),
        "createByCurve": staticmethod(lambda c, kp: ("curve", kp)),
        "createByPoint": staticmethod(lambda p: ("point",)),
    })
    monkeypatch.setattr(adsk.fusion, "JointGeometry", rec)
    monkeypatch.setattr(adsk.fusion, "BRepFace", BRepFace)
    monkeypatch.setattr(adsk.fusion, "BRepEdge", BRepEdge)
    monkeypatch.setattr(adsk.fusion, "BRepVertex", _Vertex)
    monkeypatch.setattr(adsk.fusion, "ConstructionPoint", type("CP", (), {}))
    monkeypatch.setattr(adsk.fusion, "SketchPoint", type("SP", (), {}))


class TestJgFromEntity:
    def test_planar_face_center(self, monkeypatch):
        _jg_seam(monkeypatch)
        g, label, err = ji._jg_from_entity(BRepFace(Plane(None)))
        assert err is None and "planar" in label
        assert g == ("planar", adsk.fusion.JointKeyPointTypes.CenterKeyPoint)

    def test_cylinder_face_middle_not_center(self, monkeypatch):
        _jg_seam(monkeypatch)
        g, label, err = ji._jg_from_entity(BRepFace(Cylinder(None)))
        # CenterKeyPoint invalid on a cylinder
        assert err is None and g == ("nonplanar", adsk.fusion.JointKeyPointTypes.MiddleKeyPoint)

    def test_circular_edge_center(self, monkeypatch):
        _jg_seam(monkeypatch)
        g, label, err = ji._jg_from_entity(BRepEdge(Circle3D(None)))
        assert err is None and label == "edge"
        assert g == ("curve", adsk.fusion.JointKeyPointTypes.CenterKeyPoint)

    def test_line_edge_middle(self, monkeypatch):
        _jg_seam(monkeypatch)
        g, _, err = ji._jg_from_entity(BRepEdge(Line3D()))
        assert err is None and g == ("curve", adsk.fusion.JointKeyPointTypes.MiddleKeyPoint)

    def test_vertex_uses_point(self, monkeypatch):
        _jg_seam(monkeypatch)
        g, label, err = ji._jg_from_entity(_Vertex(None))
        assert err is None and g == ("point",) and label == "point"

    def test_unsupported_entity_errors(self, monkeypatch):
        _jg_seam(monkeypatch)
        g, label, err = ji._jg_from_entity(object())
        assert g is None and err is not None and "not a supported joint geometry" in err


# ── _current_joint_type: JointMotion subclass name -> our keyword ───────────

class TestCurrentJointType:
    def _unmapped(self, class_name):
        """A motion class with no shared fake - rigid/planar/ball have no SHAPES dump."""
        return type(class_name, (), {})()

    def test_maps_each_motion_class(self):
        kind = jn.current_joint_type
        assert kind(FakeJoint(motion=RevoluteJointMotion())) == "revolute"
        assert kind(FakeJoint(motion=SliderJointMotion())) == "slider"
        assert kind(FakeJoint(motion=CylindricalJointMotion())) == "cylindrical"
        assert kind(FakeJoint(motion=self._unmapped("PlanarJointMotion"))) == "planar"
        assert kind(FakeJoint(motion=self._unmapped("RigidJointMotion"))) == "rigid"
        assert kind(FakeJoint(motion=self._unmapped("BallJointMotion"))) == "ball"

    def test_unknown_class_is_empty(self):
        assert jn.current_joint_type(FakeJoint(motion=self._unmapped("MysteryJointMotion"))) == ""

    def test_no_motion_is_empty(self):
        assert jn.current_joint_type(FakeJoint(motion=None)) == ""




# ── _face_extent / _is_planar: bbox projection + planarity gate ─────────────

class TestFaceExtentAndPlanar:
    def test_extent_projects_onto_axis(self):
        f = BRepFace(Plane(None), bounding_box=make_bbox((-3.0, 1.0, 5.0), (7.0, 2.0, 9.0)))
        assert ji._face_extent(f, 0) == (-3.0, 7.0)   # x
        assert ji._face_extent(f, 1) == (1.0, 2.0)    # y
        assert ji._face_extent(f, 2) == (5.0, 9.0)    # z

    def test_no_bbox_returns_zeros(self):
        # a face whose own AABB does not read at all
        assert ji._face_extent(BRepFace(Plane(None)), 2) == (0.0, 0.0)

    def test_is_planar_admits_the_plane_member_and_no_other_surface(self):
        # Each fake surface carries its own MEASURED SurfaceTypes member, so a gate comparing
        # against the wrong one lets a curved face through to createByPlanarFace, which rejects it.
        assert ji._is_planar(BRepFace(Plane(None))) is True
        assert ji._is_planar(BRepFace(Cylinder(None))) is False
        assert ji._is_planar(BRepFace(Cone(None))) is False


# ── create handler: end-to-end logic (motion dispatch, axis field, offset/angle scaling) ──
# The create handler had no handler-level test; pin the validation gates and the computed output
# fields (axis null for non-axis types, offset/angle ValueInput scaling, joint_type echo).

class _CreateJointInput(FakeJointInput):
    """The shared JointInput plus the three fields a create WRITES onto it, and `called` - the
    motion the shared fake records; `ok` False is the setter that REFUSES."""
    def __init__(self, ok=True):
        super().__init__(sets_ok=ok)
        self.offset = None
        self.angle = None
        self.isFlipped = False

    @property
    def called(self):
        return self._motion


def _install_create(monkeypatch, jo_names=("JO_A", "JO_B"), snapshots=None, joint_input=None):
    """A design whose root carries these Joint Origins plus a joints collection answering one
    JointInput and handing back one Joint. Returns (design, that collection)."""
    root = MakeComp(name="Root", joint_origins=[_jo(n) for n in jo_names])
    joints_coll = FakeJoints(joint_input=joint_input if joint_input is not None
                             else _CreateJointInput(), new_joint=FakeJoint(name="Joint1"))
    root.joints = joints_coll
    # install() points both design seams at it: the JO-name inputs resolve through the
    # JointOriginRef kind (joint._inputs._common), the handler through joint._common.
    design = install(joint, make_design(comp=root, snapshots=snapshots))
    monkeypatch.setattr(adsk.core.ValueInput, "createByReal", staticmethod(lambda v: ("real", v)))
    return design, joints_coll


def _added(joints_coll):
    """The JointInput a FakeJoints was asked to add, or None - Joints has no `added` member live,
    so the shared fake records the call instead."""
    for call in joints_coll._calls:
        if call[0] == "add":
            return call[1]
    return None


class TestCreatedJointHealth:
    """A joint can be ADDED yet fail to COMPUTE. joints.add() hands back a truthy Joint while Fusion
    marks it 'Compute Failed' and assembly_get counts it under broken_joints - measured on a joint to
    the child of a ground_to_parent occurrence, which moved NOTHING and still published created:true.
    The create reads the state back, so a broken joint is a refusal, never a plain success."""

    def _with_health(self, monkeypatch, joint_health=None, timeline_health=None, msg=""):
        _d, joints_coll = _install_create(monkeypatch)
        # No joint_health = the state that does not READ at all, which is not a healthy one.
        made = FakeJoint(name="Joint1", message=msg, health=joint_health,
                         health_readable=joint_health is not None)
        if timeline_health is not None:
            made.timelineObject = FakeTimelineObject(name="Joint1", health=timeline_health,
                                                     message=msg)
        joints_coll._new = made
        return joints_coll

    def test_a_joint_that_computed_BROKEN_is_refused_not_created_true(self, monkeypatch):
        # The measured shape: healthState reads WARNING (Fusion's 'Compute Failed'), so the create
        # refuses instead of publishing created:true.
        self._with_health(monkeypatch, joint_health=_HEALTH.WarningFeatureHealthState,
                          msg="Can't resolve some component positions because there are conflicts "
                              "with assembly relationships in the design.Compute FailedJoint1")
        res = joint.handler(occurrence_one="JO_A", occurrence_two="JO_B")
        assert res["isError"] is True
        assert "FAILED to compute" in res["message"]
        assert "conflicts with assembly relationships" in res["message"]
        assert "Compute Failed" not in res["message"]     # the repeating blob is condensed
        assert "design_delete_feature" in res["message"]  # the joint REMAINS - say how to remove it
        assert "ground_to_parent" in res["message"]       # and name the measured cause

    def test_an_ERROR_health_state_is_refused_too(self, monkeypatch):
        self._with_health(monkeypatch, joint_health=_HEALTH.ErrorFeatureHealthState,
                          msg="over-constrained")
        res = joint.handler(occurrence_one="JO_A", occurrence_two="JO_B")
        assert res["isError"] is True
        assert "FAILED to compute" in res["message"]

    def test_a_failure_visible_only_on_the_TIMELINE_item_is_still_caught(self, monkeypatch):
        # The measured failure showed on BOTH the joint and its timeline item; a build where only
        # the timeline item carries it must not slip through.
        self._with_health(monkeypatch, joint_health=_HEALTH.HealthyFeatureHealthState,
                          timeline_health=_HEALTH.ErrorFeatureHealthState, msg="broken")
        res = joint.handler(occurrence_one="JO_A", occurrence_two="JO_B")
        assert res["isError"] is True
        assert "FAILED to compute" in res["message"]

    def test_a_HEALTHY_joint_is_created_and_says_so(self, monkeypatch):
        self._with_health(monkeypatch, joint_health=_HEALTH.HealthyFeatureHealthState)
        out = _payload(joint.handler(occurrence_one="JO_A", occurrence_two="JO_B"))
        assert out["created"] is True
        assert out["healthy"] is True

    def test_an_UNREADABLE_health_state_is_null_not_a_coerced_healthy(self, monkeypatch):
        # Neither the joint nor a timeline item answers healthState: whether it solved is UNKNOWN.
        # A True here would be a clean bill of health nobody read.
        self._with_health(monkeypatch)
        out = _payload(joint.handler(occurrence_one="JO_A", occurrence_two="JO_B"))
        assert out["created"] is True
        assert out["healthy"] is None
        assert "'healthy' is null" in out["note"]


class TestCreateHandler:
    def test_requires_both_inputs(self, monkeypatch):
        _install_create(monkeypatch)
        res1 = joint.handler(occurrence_one="JO_A")
        assert res1["isError"] is True
        assert "Provide 'occurrence_one' and 'occurrence_two'" in res1["message"]
        res2 = joint.handler(occurrence_two="JO_B")
        assert res2["isError"] is True
        assert "Provide 'occurrence_one' and 'occurrence_two'" in res2["message"]

    def test_unknown_joint_type_errors(self, monkeypatch):
        _install_create(monkeypatch)
        res = joint.handler(occurrence_one="JO_A", occurrence_two="JO_B", joint_type="weld")
        assert res["isError"] is True and "Unknown joint_type" in res["message"]

    def test_unknown_axis_errors(self, monkeypatch):
        _install_create(monkeypatch)
        res = joint.handler(occurrence_one="JO_A", occurrence_two="JO_B",
                            joint_type="revolute", axis="q")
        assert res["isError"] is True and "Unknown axis" in res["message"]

    def test_unknown_units_errors(self, monkeypatch):
        _install_create(monkeypatch)
        res = joint.handler(occurrence_one="JO_A", occurrence_two="JO_B", units="furlong")
        assert res["isError"] is True and "Unknown units" in res["message"]

    def test_revolute_dispatches_axis_and_echoes_axis_field(self, monkeypatch):
        _, coll = _install_create(monkeypatch)
        out = _payload(joint.handler(occurrence_one="JO_A", occurrence_two="JO_B",
                                      joint_type="revolute", axis="y"))
        assert coll._input.called == ("revolute", 1)   # YAxisJointDirection == 1
        assert out["joint_type"] == "revolute" and out["axis"] == "y"

    def test_rigid_has_null_axis_field(self, monkeypatch):
        _, coll = _install_create(monkeypatch)
        out = _payload(joint.handler(occurrence_one="JO_A", occurrence_two="JO_B",
                                      joint_type="rigid"))
        assert coll._input.called == ("rigid",)
        assert out["axis"] is None                         # rigid needs no axis

    def test_ball_has_null_axis_field(self, monkeypatch):
        _, coll = _install_create(monkeypatch)
        out = _payload(joint.handler(occurrence_one="JO_A", occurrence_two="JO_B",
                                      joint_type="ball"))
        assert coll._input.called[0] == "ball"
        assert out["axis"] is None

    def test_ball_uses_valid_pitch_and_yaw_directions(self, monkeypatch):
        # Live API fact this pins: setAsBallJointMotion(pitchDirection, yawDirection) REJECTS XAxis
        # as the pitch direction ("Invalid parameter pitchDirection") - it requires
        # pitch=ZAxisJointDirection and yaw=XAxisJointDirection, and a mock accepts any args, so
        # only pinning the enums here catches a wrong pair before a live document does.
        import adsk.fusion
        JD = adsk.fusion.JointDirections
        _, coll = _install_create(monkeypatch)
        joint.handler(occurrence_one="JO_A", occurrence_two="JO_B", joint_type="ball")
        kind, pitch, yaw = coll._input.called
        assert kind == "ball"
        assert pitch == JD.ZAxisJointDirection, "pitchDirection must be ZAxisJointDirection (not X)"
        assert yaw == JD.XAxisJointDirection, "yawDirection must be XAxisJointDirection"

    def test_offset_scaled_to_cm_on_value_input(self, monkeypatch):
        _, coll = _install_create(monkeypatch)
        out = _payload(joint.handler(occurrence_one="JO_A", occurrence_two="JO_B",
                                      joint_type="rigid", offset=10, units="mm"))
        # 10 mm -> 1.0 cm passed to ValueInput.createByReal
        assert coll._input.offset == ("real", 1.0)
        assert out["offset"] == 10

    def test_offset_inch_scaling(self, monkeypatch):
        _, coll = _install_create(monkeypatch)
        _payload(joint.handler(occurrence_one="JO_A", occurrence_two="JO_B",
                                joint_type="rigid", offset=2, units="in"))
        assert abs(coll._input.offset[1] - 5.08) < 1e-9   # 2 in -> 5.08 cm

    def test_angle_converted_to_radians(self, monkeypatch):
        import math
        _, coll = _install_create(monkeypatch)
        out = _payload(joint.handler(occurrence_one="JO_A", occurrence_two="JO_B",
                                      joint_type="revolute", angle=90))
        assert abs(coll._input.angle[1] - math.radians(90)) < 1e-9
        assert out["angle_deg"] == 90

    def test_flip_sets_is_flipped(self, monkeypatch):
        _, coll = _install_create(monkeypatch)
        out = _payload(joint.handler(occurrence_one="JO_A", occurrence_two="JO_B",
                                      joint_type="rigid", flip=True))
        assert coll._input.isFlipped is True and out["flipped"] is True

    def test_no_offset_angle_reported_as_none(self, monkeypatch):
        _install_create(monkeypatch)
        out = _payload(joint.handler(occurrence_one="JO_A", occurrence_two="JO_B",
                                      joint_type="rigid"))
        assert out["offset"] is None and out["angle_deg"] is None

    def test_add_failure_on_input_paths_hints_the_proxy_fix(self, monkeypatch):
        # Fusion's "Provided input paths for joint are not valid" = an input not in assembly
        # context. The error must carry the remedy (pass JOs by name so the tool proxies them),
        # not just echo Fusion's opaque message.
        _, coll = _install_create(monkeypatch)
        def boom(ji):
            raise RuntimeError("3 : Provided input paths for joint are not valid.")
        coll.add = boom
        res = joint.handler(occurrence_one="JO_A", occurrence_two="JO_B")
        assert res["isError"] is True
        assert "assembly context" in res["message"]
        assert "<occurrence>:<JO name>" in res["message"]

    def test_add_failure_other_errors_unadorned(self, monkeypatch):
        _, coll = _install_create(monkeypatch)
        def boom(ji):
            raise RuntimeError("5 : something else entirely")
        coll.add = boom
        res = joint.handler(occurrence_one="JO_A", occurrence_two="JO_B")
        assert res["isError"] is True
        assert "assembly context" not in res["message"]
        assert "already jointed" not in res["message"]

    def test_a_pair_that_already_holds_a_joint_is_named_as_the_pair(self, monkeypatch):
        # Fusion answers a second joint on one PAIR with the same words whichever inputs are
        # passed, so relaying it bare sends the caller looking for a bad joint origin.
        _, coll = _install_create(monkeypatch)
        def boom(ji):
            raise RuntimeError("3 : A joint in system exists for the provided input. "
                               "System will be over constrained")
        coll.add = boom
        res = joint.handler(occurrence_one="JO_A", occurrence_two="JO_B")
        assert res["isError"] is True
        assert "'JO_A' and 'JO_B' are already jointed to each other" in res["message"]
        assert "joint_edit" in res["message"]
        # the platform text ends mid-sentence, so the clause is terminated onto it
        assert "over constrained. Fusion names" in res["message"]
        # measured on this tool: a second anchor pair on the same pair returns the identical text
        assert "whichever anchor is passed" in res["message"]


class TestAxisIsAdvertisedFrameRelative:
    """'axis' names an axis of the JOINT GEOMETRY's frame, not a world axis (measured: a snap whose
    local Z points along world Y pivots about world Y). Both tools take that same frame-relative
    axis, so both must say so: a description that reads as if it were a world direction sends a
    caller to rebuild the joint instead of re-pointing it with joint_edit(world_axis=...)."""

    def _axis_property(self, t):
        return t.to_dict()["inputSchema"]["properties"]["axis"]["description"]

    def test_create_axis_input_says_frame_relative(self):
        assert "FRAME-relative" in self._axis_property(joint.tool)

    def test_edit_axis_input_says_the_same_thing(self):
        assert "FRAME-relative" in self._axis_property(jedit.tool)

    def test_the_create_axis_input_names_the_frame_and_the_fix(self):
        axis = self._axis_property(joint.tool)
        assert "FRAME-relative" in axis
        assert "joint_edit(world_axis=" in axis      # the tool that re-points it


_UNREADABLE_FLAG = object()


class _BlindSnapshots:
    """Design.snapshots whose pending flag RAISES - the unknown-flag case, not a False."""
    @property
    def hasPendingSnapshot(self):
        raise RuntimeError("pending flag unreadable")


class TestPendingMoveRefusal:
    """A joint CREATE recomputes the assembly, and a recompute REVERTS an uncaptured occurrence
    position: the parts snap back and the new joint freezes the reverted pose. So a create while
    Design.snapshots.hasPendingSnapshot is set is refused, naming capture / discard_pending. An
    UNREADABLE flag is not evidence a move is pending and must not block the create."""

    def _install(self, monkeypatch, pending):
        snapshots = (_BlindSnapshots() if pending is _UNREADABLE_FLAG
                     else SimpleNamespace(hasPendingSnapshot=pending))
        return _install_create(monkeypatch, snapshots=snapshots)

    def test_refuses_the_create_while_a_move_is_pending(self, monkeypatch):
        _, coll = self._install(monkeypatch, True)
        res = joint.handler(occurrence_one="JO_A", occurrence_two="JO_B")
        assert res["isError"] is True
        assert "would silently revert" in res["message"]
        assert _added(coll) is None                       # nothing was created

    def test_the_refusal_names_both_remedies(self, monkeypatch):
        self._install(monkeypatch, True)
        msg = joint.handler(occurrence_one="JO_A", occurrence_two="JO_B")["message"]
        assert "assembly_capture_position(action='capture')" in msg
        assert "action='discard_pending'" in msg

    def test_creates_normally_with_nothing_pending(self, monkeypatch):
        _, coll = self._install(monkeypatch, False)
        out = _payload(joint.handler(occurrence_one="JO_A", occurrence_two="JO_B"))
        assert out["created"] is True and _added(coll) is not None

    def test_an_unreadable_flag_does_not_refuse(self, monkeypatch):
        _, coll = self._install(monkeypatch, _UNREADABLE_FLAG)
        out = _payload(joint.handler(occurrence_one="JO_A", occurrence_two="JO_B"))
        assert out["created"] is True and _added(coll) is not None


class TestSlideValueHasNoParameter:
    """A slider's slide VALUE carries no ModelParameter (measured twice in anger), so the shared
    offset note has to say both halves: there is no slide parameter to set, and parametric TRAVEL
    comes from co-driving the anchor geometry - otherwise the next caller re-derives it by failing."""

    def test_the_offset_note_states_there_is_no_slide_parameter(self):
        note = joint._OFFSET_PARAM_NOTE
        assert "slide" in note and "no parameter" in note

    def test_the_offset_note_names_the_parametric_travel_route(self):
        assert "co-driving the geometry" in joint._OFFSET_PARAM_NOTE

    def test_a_created_joint_carrying_params_appends_the_note(self, monkeypatch):
        _, coll = _install_create(monkeypatch)
        # ModelParameter has no SHAPES dump, so the joint's own params stay a name-only bag.
        coll._new = FakeJoint(name="Slider1", offset=SimpleNamespace(name="d12"),
                              angle=SimpleNamespace(name="d13"))
        out = _payload(joint.handler(occurrence_one="JO_A", occurrence_two="JO_B",
                                      joint_type="slider"))
        assert out["model_parameters"] == {"offset": "d12", "angle": "d13"}
        assert "co-driving the geometry" in out["note"]


class TestCreateFlipHint:
    """Parity with joint_at_geometry: a create that seats two OPPOSING planar faces without flip
    rotates the free part 180 deg - the payload carries the one shared FLIP_HINT."""

    def _wire(self, monkeypatch, n1, n2):
        _, coll = _install_create(monkeypatch)
        face1, face2 = object(), object()
        monkeypatch.setattr(joint, "_resolve_input",
                            lambda d, spec: (SimpleNamespace(name=spec,
                                                             entityOne=face1 if spec == "JO_A"
                                                             else face2),
                                             spec, None))
        normals = {face1: n1, face2: n2}
        monkeypatch.setattr(joint._joints, "planar_outward_normal",
                            lambda e: normals.get(e))
        return coll

    def test_opposing_faces_without_flip_carry_the_hint(self, monkeypatch):
        self._wire(monkeypatch, (0.0, 0.0, 1.0), (0.0, 0.0, -1.0))
        out = _payload(joint.handler(occurrence_one="JO_A", occurrence_two="JO_B"))
        assert "OPPOSE" in out["flip_hint"] and "flip=true" in out["flip_hint"]

    def test_flip_true_suppresses_the_hint(self, monkeypatch):
        self._wire(monkeypatch, (0.0, 0.0, 1.0), (0.0, 0.0, -1.0))
        out = _payload(joint.handler(occurrence_one="JO_A", occurrence_two="JO_B", flip=True))
        assert "flip_hint" not in out

    def test_aligned_faces_carry_no_hint(self, monkeypatch):
        self._wire(monkeypatch, (0.0, 0.0, 1.0), (0.0, 0.0, 1.0))
        out = _payload(joint.handler(occurrence_one="JO_A", occurrence_two="JO_B"))
        assert "flip_hint" not in out

    def test_an_unreadable_normal_carries_no_hint(self, monkeypatch):
        self._wire(monkeypatch, None, (0.0, 0.0, -1.0))
        out = _payload(joint.handler(occurrence_one="JO_A", occurrence_two="JO_B"))
        assert "flip_hint" not in out


class TestCreatePublishesLimitsItRead:
    """joint_create's limit fields are the joint's own read-back or they are null - never the
    request. A payload built from the request reports a limit that may never have been applied."""

    def _with_motion(self, monkeypatch, limits):
        _d, coll = _install_create(monkeypatch)
        coll._new = FakeJoint(name="Joint1", motion=RevoluteJointMotion(limits=limits))
        return coll

    def test_a_landed_limit_is_published_from_the_joint(self, monkeypatch):
        self._with_motion(monkeypatch, _BandLim(ji._LIMIT_BAND * 0.5))
        out = _payload(joint.handler(occurrence_one="JO_A", occurrence_two="JO_B",
                                      joint_type="revolute", min_deg=-45))
        assert out["created"] is True
        assert out["min_deg"] == -44.9995        # what the joint holds, not the -45 requested
        assert "limits_unverified" not in out

    def test_a_limit_that_did_not_take_refuses_the_create_naming_it(self, monkeypatch):
        self._with_motion(monkeypatch, _DeafLim(held_value=0.0))
        res = joint.handler(occurrence_one="JO_A", occurrence_two="JO_B",
                            joint_type="revolute", min_deg=-45)
        assert res["isError"] is True
        assert "min_deg did not take" in res["message"]
        # the joint EXISTS - the partial-success disclosure has to say so and name the removal path
        assert "WAS CREATED" in res["message"] and "design_delete_feature" in res["message"]

    def test_an_unreadable_limit_publishes_null_and_the_marker(self, monkeypatch):
        self._with_motion(monkeypatch, _BlindLim())
        out = _payload(joint.handler(occurrence_one="JO_A", occurrence_two="JO_B",
                                      joint_type="revolute", min_deg=-45, max_deg=90))
        assert out["min_deg"] is None and out["max_deg"] is None
        assert out["limits_unverified"] == ["min_deg", "max_deg"]
        assert "Limits published null" in out["note"] and "not a 'yes'" in out["note"]




# ── the snap path resolves its occurrence through the shared OccurrenceRef resolver ─────────────

class TestSnapOccurrenceResolution:
    def test_delegates_to_the_shared_occurrence_resolver(self, monkeypatch):
        # The name is passed as BOTH the field label and the raw spec, so the resolver's refusal
        # names the input the caller actually typed. A local name walk here would resolve an
        # ambiguous 'Bolt:1' to the first instance instead of refusing it.
        seen = []
        monkeypatch.setattr(ji._inputs, "_resolve_occurrence",
                            lambda name, raw: seen.append((name, raw)) or (None, "refused"))
        assert ji._resolve_snap_entity(SimpleNamespace(), "Boom:1", "top") == (None, None, "refused")
        assert seen == [("Boom:1", "Boom:1")]


class TestAvailableJointOriginsSkipsUnnamed:
    def test_a_jo_with_no_readable_name_is_left_out_of_the_listing(self, monkeypatch):
        # A blank entry would render as "'' (root)" and teach the agent a name that resolves nothing.
        design = _install_resolve_seam(monkeypatch, {})
        design.rootComponent.jointOrigins = _jos("Center of Model", "")
        listed, more = ji._available_joint_origins(design)
        assert listed == ["'Center of Model' (root)"] and more == 0


# ── _resolve_snap_entity: an occurrence's geometry -> one proxied BRep entity ─────────────────────

def _surface(surface_type):
    """The surface for a SurfaceTypes member - a sphere carries no shared fake holding one."""
    shared = {adsk.core.SurfaceTypes.PlaneSurfaceType: Plane,
              adsk.core.SurfaceTypes.CylinderSurfaceType: Cylinder,
              adsk.core.SurfaceTypes.ConeSurfaceType: Cone}
    return (shared[surface_type](None) if surface_type in shared
            else SimpleNamespace(surfaceType=surface_type))


def _snap_face(surface_type, *, area=1.0, proxy=None):
    """One face: its surface type, its area, and what proxying yields. Surface types come from the
    MEASURED enum (adsk.core.SurfaceTypes) - Cylinder is 1 and 3 is Sphere, so a fake built on a
    hand-typed 3 describes a sphere and lets a cylinder-picker that never matched look correct."""
    return BRepFace(_surface(surface_type), area=area, assembly_proxy=proxy)


def _snap_occurrence(*, origin_point=None, has_body=True, faces=()):
    """An occurrence exposing exactly what _resolve_snap_entity reads off it. A component built
    WITHOUT an origin point carries no originConstructionPoint at all, which is the read that has to
    be guarded before the proxy is asked for."""
    point = {} if origin_point is None else {"origin_construction_point": origin_point}
    comp = MakeComp(name="Boom", bodies=[BRepBody("Body1", faces=faces)] if has_body else [],
                    **point)
    return FakeOccurrence(path="Boom:1", component=comp)


class TestResolveSnapEntity:
    def _wire(self, monkeypatch, occ, err=None):
        monkeypatch.setattr(ji._inputs, "_resolve_occurrence", lambda name, raw: (occ, err))

    def test_an_unresolved_occurrence_error_is_passed_through(self, monkeypatch):
        self._wire(monkeypatch, None, "no occurrence 'Boom:1'")
        assert ji._resolve_snap_entity(None, "Boom:1", "top") == (None, None, "no occurrence 'Boom:1'")

    def test_origin_snap_returns_the_assembly_context_proxy(self, monkeypatch):
        op = SimpleNamespace(createForAssemblyContext=lambda occ: "PROXY_PT")
        self._wire(monkeypatch, _snap_occurrence(origin_point=op))
        assert ji._resolve_snap_entity(None, "Boom:1", "origin") == ("PROXY_PT", "point", None)

    def test_origin_snap_falls_back_to_the_native_point(self, monkeypatch):
        # A root-component point has no proxy to make; the native entity is the usable form.
        op = SimpleNamespace(createForAssemblyContext=lambda occ: None)
        self._wire(monkeypatch, _snap_occurrence(origin_point=op))
        ent, kind, err = ji._resolve_snap_entity(None, "Boom:1", "origin")
        assert ent is op and kind == "point" and err is None

    def test_origin_snap_without_an_origin_point_names_the_occurrence(self, monkeypatch):
        self._wire(monkeypatch, _snap_occurrence(origin_point=None))
        ent, kind, err = ji._resolve_snap_entity(None, "Boom:1", "origin")
        assert ent is None and err == "'Boom:1' has no origin construction point."

    def test_an_occurrence_with_no_body_names_the_occurrence(self, monkeypatch):
        self._wire(monkeypatch, _snap_occurrence(has_body=False))
        ent, kind, err = ji._resolve_snap_entity(None, "Boom:1", "top")
        assert ent is None and err == "'Boom:1' has no body to snap to."

    def test_a_body_with_zero_faces_is_refused(self, monkeypatch):
        self._wire(monkeypatch, _snap_occurrence(faces=[]))
        ent, kind, err = ji._resolve_snap_entity(None, "Boom:1", "top")
        assert ent is None and err == "'Boom:1' body has no faces."

    def test_cylinder_snap_picks_the_cylindrical_face_and_proxies_it(self, monkeypatch):
        st = adsk.core.SurfaceTypes
        self._wire(monkeypatch, _snap_occurrence(faces=[
            _snap_face(st.PlaneSurfaceType, proxy="FLAT"),
            _snap_face(st.CylinderSurfaceType, proxy="CYL")]))
        assert ji._resolve_snap_entity(None, "Boom:1", "cylinder") == ("CYL", "cylinder", None)

    def test_cylinder_snap_takes_a_cone_too(self, monkeypatch):
        # a tapered pin is round and seats the same way - the pair joint_at_geometry accepts.
        st = adsk.core.SurfaceTypes
        self._wire(monkeypatch, _snap_occurrence(faces=[
            _snap_face(st.PlaneSurfaceType, proxy="FLAT"),
            _snap_face(st.ConeSurfaceType, proxy="CONE")]))
        assert ji._resolve_snap_entity(None, "Boom:1", "cylinder") == ("CONE", "cylinder", None)

    def test_cylinder_snap_does_not_match_a_sphere(self, monkeypatch):
        # SphereSurfaceType is 3; a picker hand-typed against that integer matches spheres and
        # reports every real cylinder as having no cylindrical face.
        st = adsk.core.SurfaceTypes
        self._wire(monkeypatch, _snap_occurrence(faces=[
            _snap_face(st.SphereSurfaceType, proxy="BALL")]))
        ent, kind, err = ji._resolve_snap_entity(None, "Boom:1", "cylinder")
        assert ent is None and err == "'Boom:1' has no cylindrical face to snap to."

    def test_cylinder_snap_on_a_body_with_no_cylinder_is_refused(self, monkeypatch):
        self._wire(monkeypatch, _snap_occurrence(
            faces=[_snap_face(adsk.core.SurfaceTypes.PlaneSurfaceType)]))
        ent, kind, err = ji._resolve_snap_entity(None, "Boom:1", "cylinder")
        assert ent is None and err == "'Boom:1' has no cylindrical face to snap to."

    def test_a_planar_snap_returns_the_picked_face_proxied(self, monkeypatch):
        plane = adsk.core.SurfaceTypes.PlaneSurfaceType
        self._wire(monkeypatch, _snap_occurrence(faces=[
            _snap_face(plane, area=1.0, proxy="SMALL"), _snap_face(plane, area=9.0, proxy="BIG")]))
        # 'center' = the largest planar face
        assert ji._resolve_snap_entity(None, "Boom:1", "center") == ("BIG", "planar", None)

    def test_a_planar_snap_with_no_planar_face_names_the_snap(self, monkeypatch):
        self._wire(monkeypatch, _snap_occurrence(
            faces=[_snap_face(adsk.core.SurfaceTypes.SphereSurfaceType)]))
        ent, kind, err = ji._resolve_snap_entity(None, "Boom:1", "top")
        assert ent is None and err == "Could not pick a 'top' face on 'Boom:1'."


class TestResolveSnapInput:
    def test_builds_a_joint_geometry_from_the_snapped_entity(self, monkeypatch):
        _jg_seam(monkeypatch)
        face = BRepFace(Plane(None))
        monkeypatch.setattr(ji, "_resolve_snap_entity", lambda d, n, s: (face, "planar", None))
        g, err = ji._resolve_snap_input(None, "Boom:1", "top")
        assert err is None and g == ("planar", adsk.fusion.JointKeyPointTypes.CenterKeyPoint)

    def test_an_entity_failure_is_passed_through_untouched(self, monkeypatch):
        monkeypatch.setattr(ji, "_resolve_snap_entity", lambda d, n, s: (None, None, "no body"))
        assert ji._resolve_snap_input(None, "Boom:1", "top") == (None, "no body")

    def test_a_geometry_build_failure_is_reported_not_swallowed(self, monkeypatch):
        _jg_seam(monkeypatch)
        monkeypatch.setattr(ji, "_resolve_snap_entity", lambda d, n, s: (object(), "planar", None))
        g, err = ji._resolve_snap_input(None, "Boom:1", "top")
        assert g is None and "not a supported joint geometry" in err


class TestResolveInputSnapPath:
    def test_a_snap_spec_is_labelled_occurrence_and_snap(self, monkeypatch):
        design = _install_resolve_seam(monkeypatch, {})
        monkeypatch.setattr(ji, "_resolve_snap_input", lambda d, occ, snap: ("G", None))
        assert ji._resolve_input(design, "Boom:1:top") == ("G", "Boom:1:top", None)

    def test_a_snap_failure_keeps_the_snap_label_and_its_error(self, monkeypatch):
        # The snap path OWNS the spec once it parses - it must not fall through to the JO-name
        # form-guide, which would hide "has no body to snap to" behind "not a Joint Origin".
        design = _install_resolve_seam(monkeypatch, {})
        monkeypatch.setattr(ji, "_resolve_snap_input", lambda d, occ, snap: (None, "no body"))
        assert ji._resolve_input(design, "Boom:1:cylinder") == (None, "Boom:1:cylinder", "no body")


class TestResolveInputAmbiguousJointOrigin:
    def test_an_ambiguous_jo_name_is_surfaced_verbatim(self, monkeypatch):
        # Two components carrying the same JO name: the kind's refusal (with the qualified
        # candidates) is what the caller needs - not the generic "not a handle/JO/snap" guide.
        _install_resolve_seam(monkeypatch, {})
        shared = "Center of Model"
        root = MakeComp(name="Root", joint_origins=[_jo(shared)])
        sub = MakeComp(name="Tower", joint_origins=[_jo(shared)])
        design = install(joint, make_design(comp=root, all_components=[root, sub]))
        g, label, err = ji._resolve_input(design, shared)
        assert g is None and label == shared
        assert "ambiguous" in err and "2 Joint Origins share that name" in err
        assert "is not a find_geometry handle" not in err


# ── create handler: the failure paths that must report, never return a false ok ───────────────────

def _raise_refusal(*_a, **_k):
    """A platform call that refuses - injected where the handler must report, not swallow."""
    raise RuntimeError("7 : the platform refused")


class TestCreateHandlerFailurePaths:
    def test_no_active_design(self, monkeypatch):
        monkeypatch.setattr(joint._common, "design", lambda: None)
        res = joint.handler(occurrence_one="JO_A", occurrence_two="JO_B")
        assert res["isError"] is True and "No active design" in res["message"]

    def test_an_unresolvable_first_input_carries_the_resolver_error(self, monkeypatch):
        _install_create(monkeypatch)
        res = joint.handler(occurrence_one="Nope", occurrence_two="JO_B")
        assert res["isError"] is True and "'Nope' is not a find_geometry handle" in res["message"]

    def test_an_unresolvable_second_input_carries_the_resolver_error(self, monkeypatch):
        _install_create(monkeypatch)
        res = joint.handler(occurrence_one="JO_A", occurrence_two="Nope")
        assert res["isError"] is True and "'Nope' is not a find_geometry handle" in res["message"]

    def test_a_silent_first_input_failure_still_names_the_input(self, monkeypatch):
        # A resolver that declines without a reason must not produce a bare/blank refusal.
        _install_create(monkeypatch)
        monkeypatch.setattr(joint, "_resolve_input", lambda d, spec: (None, spec, None))
        res = joint.handler(occurrence_one="JO_A", occurrence_two="JO_B")
        assert "Could not resolve joint input 'JO_A'" in res["message"]

    def test_a_silent_second_input_failure_names_the_second_input(self, monkeypatch):
        _install_create(monkeypatch)
        monkeypatch.setattr(
            joint, "_resolve_input",
            lambda d, spec: (SimpleNamespace(name=spec), spec, None) if spec == "JO_A"
            else (None, spec, None))
        res = joint.handler(occurrence_one="JO_A", occurrence_two="JO_B")
        assert "Could not resolve joint input 'JO_B'" in res["message"]

    def test_a_raising_create_input_is_reported(self, monkeypatch):
        _, coll = _install_create(monkeypatch)
        coll.createInput = _raise_refusal
        res = joint.handler(occurrence_one="JO_A", occurrence_two="JO_B")
        assert res["isError"] is True and "Could not create joint input" in res["message"]
        assert "the platform refused" in res["message"]

    def test_a_create_input_returning_nothing_is_reported(self, monkeypatch):
        _, coll = _install_create(monkeypatch)
        coll._input = None                               # the input that could not be built
        res = joint.handler(occurrence_one="JO_A", occurrence_two="JO_B")
        assert res["isError"] is True and "createInput returned nothing" in res["message"]

    def test_a_motion_setter_returning_false_is_not_a_success(self, monkeypatch):
        # The setter answered False and said nothing else, so the refusal falls back to naming the
        # bool it read - the only thing a caller can act on when the platform gives no reason.
        _, coll = _install_create(monkeypatch)
        coll._input = _JointInput(sets_ok=False)
        res = joint.handler(occurrence_one="JO_A", occurrence_two="JO_B")
        assert res["isError"] is True
        assert "Could not set rigid motion: setter returned false" in res["message"]
        assert _added(coll) is None                      # never reached joints.add

    def test_an_offset_that_cannot_be_valued_is_reported(self, monkeypatch):
        import adsk.core
        _, coll = _install_create(monkeypatch)
        monkeypatch.setattr(adsk.core.ValueInput, "createByReal", staticmethod(_raise_refusal))
        res = joint.handler(occurrence_one="JO_A", occurrence_two="JO_B", offset=10)
        assert res["isError"] is True and "Could not apply offset/angle/flip" in res["message"]
        assert _added(coll) is None

    def test_an_add_returning_nothing_is_reported(self, monkeypatch):
        design, coll = _install_create(monkeypatch)
        design.rootComponent.joints = FakeJoints(joint_input=coll._input, new_joint=None)
        res = joint.handler(occurrence_one="JO_A", occurrence_two="JO_B")
        assert res["isError"] is True and "joints.add returned nothing" in res["message"]


class TestCreateLimits:
    def _with_motion(self, monkeypatch, motion):
        _, coll = _install_create(monkeypatch)
        coll._new = FakeJoint(name="Pivot", motion=motion)
        return coll

    def test_limits_on_a_joint_with_no_motion_are_refused(self, monkeypatch):
        _install_create(monkeypatch)                   # the default add() yields jointMotion None
        res = joint.handler(occurrence_one="JO_A", occurrence_two="JO_B", min_deg=-45)
        assert res["isError"] is True and "no motion to limit" in res["message"]

    def test_rotation_limits_land_on_the_new_joints_motion(self, monkeypatch):
        motion = RevoluteJointMotion()
        self._with_motion(monkeypatch, motion)
        out = _payload(joint.handler(occurrence_one="JO_A", occurrence_two="JO_B",
                                      joint_type="revolute", min_deg=-45, max_deg=90))
        assert abs(motion.rotationLimits.maximumValue - _math.radians(90)) < 1e-9
        assert out["min_deg"] == -45 and out["max_deg"] == 90

    def test_a_limit_kind_the_motion_lacks_is_refused(self, monkeypatch):
        self._with_motion(monkeypatch, RevoluteJointMotion())
        res = joint.handler(occurrence_one="JO_A", occurrence_two="JO_B",
                            joint_type="revolute", max_mm=100)
        assert res["isError"] is True and "LINEAR/slide limits" in res["message"]

    def test_create_limit_failure_names_the_created_joint_and_the_landed_limits(self, monkeypatch):
        # The joint EXISTS and min_deg landed before max_mm failed - a bare error would invite a
        # duplicate re-create; the message names it, what landed, and the two ways forward.
        motion = RevoluteJointMotion()
        self._with_motion(monkeypatch, motion)
        res = joint.handler(occurrence_one="JO_A", occurrence_two="JO_B",
                            joint_type="revolute", min_deg=-45, max_mm=100)
        assert res["isError"] is True
        msg = res["content"][0]["text"]
        assert "'Pivot' WAS CREATED" in msg
        assert "min_deg=-45" in msg
        assert "do NOT re-create" in msg
        assert abs(motion.rotationLimits.minimumValue - _math.radians(-45)) < 1e-9

    def test_slide_limits_scale_by_the_requested_units(self, monkeypatch):
        motion = SliderJointMotion()
        self._with_motion(monkeypatch, motion)
        out = _payload(joint.handler(occurrence_one="JO_A", occurrence_two="JO_B",
                                      joint_type="slider", max_mm=2, units="in"))
        assert abs(motion.slideLimits.maximumValue - 5.08) < 1e-9    # 2 in -> 5.08 cm
        assert out["max_mm"] == 2

    def test_a_rest_limit_appends_the_it_does_not_pose_disclaimer(self, monkeypatch):
        self._with_motion(monkeypatch, RevoluteJointMotion())
        out = _payload(joint.handler(occurrence_one="JO_A", occurrence_two="JO_B",
                                      joint_type="revolute", rest_deg=10))
        assert out["rest_deg"] == 10
        assert "does NOT reposition the static model" in out["note"]
        assert "joint_drive" in out["note"]

    def test_ordinary_limits_carry_no_rest_disclaimer(self, monkeypatch):
        self._with_motion(monkeypatch, RevoluteJointMotion())
        out = _payload(joint.handler(occurrence_one="JO_A", occurrence_two="JO_B",
                                      joint_type="revolute", max_deg=90))
        assert "does NOT reposition the static model" not in out["note"]


class _UnrenamableJoint(FakeJoint):
    """A joint that ACCEPTS the rename assignment and keeps the name it holds."""
    @property
    def name(self):
        return "Joint1"

    @name.setter
    def name(self, value):
        pass


class TestCreateRenameDisclosure:
    def test_a_rename_that_does_not_take_is_disclosed_not_swallowed(self, monkeypatch):
        # The create succeeded, so a declined rename is a disclosure, never an error - but the
        # payload must publish the name the joint ACTUALLY holds plus the warning.
        _, coll = _install_create(monkeypatch)
        coll._new = _UnrenamableJoint()
        out = _payload(joint.handler(occurrence_one="JO_A", occurrence_two="JO_B",
                                      name="BoomPivot"))
        assert out["joint_name"] == "Joint1"
        assert "BoomPivot" in out["rename_warning"] and "did not take" in out["rename_warning"]
