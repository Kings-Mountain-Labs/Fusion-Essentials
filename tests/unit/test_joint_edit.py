"""Unit tests for ``joint_edit.py`` - edit an EXISTING joint in place (no remaking).

The behaviour pinned, no live Fusion:

  - find the joint by name; error if absent.
  - the API requires the timeline marker be rolled to BEFORE the joint
    (joint.timelineObject.rollTo(True)) before any property edit, and rolled back
    after - the handler must do this around every edit.
  - selective edits: re-select snap inputs (geometryOrOriginOne/Two via the same
    resolver joint_create uses), change motion type/axis (setAs<Type>JointMotion),
    toggle isFlipped, set rotation/linear limits; a rotation_deg drive request is
    refused with a redirect to joint_drive.
  - a no-op call (nothing to change) is reported as an error, not a silent rollTo.

The snap-input resolution lives in _joint_inputs.py; here the fakes accept
opaque tokens so we pin the orchestration (rollTo, which setters fire, value
conversion) deterministically.
"""

import json
import math
from types import SimpleNamespace

import adsk.fusion

from conftest import (FakeJoint, FakeModelParameter, FakeTimeline, FakeTimelineObject, MakeComp,
                      RevoluteJointMotion, SliderJointMotion, _MotionLimits, _NamedCollection,
                      install, load_tool, make_design)
from conftest import payload as _payload

jt = load_tool("joint_edit")

_JD = adsk.fusion.JointDirections
_HEALTH = adsk.fusion.FeatureHealthStates


# ── fakes ───────────────────────────────────────────────────────────────────

def _param(**kw):
    """Joint.offset / Joint.angle: a ModelParameter starting BLANK, whose `expression` assignment
    (in display units) re-evaluates `value` into DATABASE units - cm for a length, radians here."""
    kw.setdefault("expression", None)
    kw.setdefault("value", None)
    return FakeModelParameter(tracks_expression=True, **kw)


def _jo(name):
    """One JointOrigin by name - JointOrigin has no SHAPES dump, so it stays a name-only bag."""
    return SimpleNamespace(name=name)


def _joint(name="BoomPivot", cls=FakeJoint, motion=None, **kw):
    """One editable Joint: a motion, its own offset/angle parameters and a timeline item. Pass
    offset=None / angle=None for the member that is PRESENT and answers null."""
    kw.setdefault("offset", _param())
    kw.setdefault("angle", _param())
    return cls(name=name, motion=RevoluteJointMotion() if motion is None else motion,
               timeline_object=FakeTimelineObject(name=name),
               geometry_one="OLD1", geometry_two="OLD2", **kw)


def _rolls(joint):
    """The rollTo(bool) calls the joint's timeline item recorded."""
    return joint.timelineObject._rolls


def _root(joints=(), as_built=()):
    """The root component an edit reads: both joint collections plus the world construction axes.
    asBuiltJoints is a SEPARATE collection from joints - find_joint searches both."""
    return MakeComp(name="Root", joints=list(joints), as_built_joints=list(as_built),
                    construction_axes=("WAXIS_X", "WAXIS_Y", "WAXIS_Z"))


def _install_joints(joints, timeline_items=None):
    """Wire a design holding these joint objects into the tool module."""
    items = timeline_items or [FakeTimelineObject(name="Joint1", index=0),
                               FakeTimelineObject(name="Pattern1", index=1)]
    # marker=1 is mid-history, so the edit's restore to the timeline END is observable.
    return install(jt, make_design(comp=_root(joints), timeline=FakeTimeline(items, marker=1)))


def _install(joint_names=("BoomPivot",), motion="revolute", timeline_items=None):
    joints = [_joint(name=n, motion=(SliderJointMotion() if motion == "slider"
                                     else RevoluteJointMotion()))
              for n in joint_names]
    return _install_joints(joints, timeline_items), joints[0]


def _install_as_built(monkeypatch, name="Slider_R"):
    """A design whose only joint is an AS-BUILT slider, in the asBuiltJoints collection. It carries
    NO offset/angle attribute at all - api_surface lists neither on fusion.AsBuiltJoint - and
    adsk.fusion.AsBuiltJoint is patched to a real class so is_as_built_joint is a genuine isinstance
    check rather than the degrade-to-False path a Mock type takes."""
    cls = type("AsBuiltJoint", (), {})
    monkeypatch.setattr(adsk.fusion, "AsBuiltJoint", cls)
    joint = cls()
    joint.name = name
    joint.timelineObject = FakeTimelineObject(name=name)
    joint.jointMotion = SliderJointMotion()
    return install(jt, make_design(comp=_root(as_built=[joint]))), joint


# ── find / guards ────────────────────────────────────────────────────────────

class TestFindAndGuards:
    def test_unknown_joint_errors(self):
        _install(["BoomPivot"])
        res = jt.handler(joint_name="Nope", flip=True)
        assert res["isError"] is True and "No joint named 'Nope'" in res["message"]

    def test_no_edits_requested_errors(self):
        _install(["BoomPivot"])
        res = jt.handler(joint_name="BoomPivot")
        assert res["isError"] is True
        assert "nothing to change" in res["message"].lower()

    def test_a_name_two_joints_carry_is_refused_with_both_owners(self):
        # a joint name is only unique within a component, so editing by a shared name would target
        # an arbitrary assembly's joint - the resolver's refusal is returned, naming both owners
        a = _joint(name="Revolute1", parent_component=MakeComp(name="ArmA"))
        b = _joint(name="Revolute1", parent_component=MakeComp(name="ArmB"))
        _install_joints([a, b])
        res = jt.handler(joint_name="Revolute1", flip=True)
        assert res["isError"] is True
        assert "names 2 joints" in res["message"]
        assert "ArmA" in res["message"] and "ArmB" in res["message"]
        assert a.isFlipped is False and b.isFlipped is False


# ── rollTo orchestration ─────────────────────────────────────────────────────

class TestRollTo:
    def test_rolls_before_then_restores_marker_to_end(self):
        design, joint = _install(["BoomPivot"])
        _payload(jt.handler(joint_name="BoomPivot", flip=True))
        # rolls the marker BEFORE the joint to edit its geometry...
        assert _rolls(joint)[0] is True
        # ...then restores the marker to the TIMELINE END (markerPosition = count), NOT rollTo(False)
        # which stops just past the joint and leaves downstream features (Pattern1) rolled out.
        assert design.timeline.markerPosition == design.timeline.count


# ── flip ─────────────────────────────────────────────────────────────────────

class TestFlip:
    def test_set_flip(self):
        _, joint = _install(["BoomPivot"])
        out = _payload(jt.handler(joint_name="BoomPivot", flip=True))
        assert joint.isFlipped is True
        assert out["flipped"] is True

    def test_unset_flip(self):
        _, joint = _install(["BoomPivot"])
        joint.isFlipped = True
        _payload(jt.handler(joint_name="BoomPivot", flip=False))
        assert joint.isFlipped is False

    def test_a_second_flip_true_leaves_it_flipped_rather_than_toggling_back(self):
        # 'flip' is a SET, so repeating a value is a no-op - a toggle would swing it back and the
        # read-back gate would then call the second call a flip that did not take.
        _, joint = _install(["BoomPivot"])
        _payload(jt.handler(joint_name="BoomPivot", flip=True))
        out = _payload(jt.handler(joint_name="BoomPivot", flip=True))
        assert joint.isFlipped is True and out["flipped"] is True


# ── motion type / axis ───────────────────────────────────────────────────────

class TestMotion:
    def test_change_to_slider_on_axis(self):
        _, joint = _install(["BoomPivot"])
        _payload(jt.handler(joint_name="BoomPivot", joint_type="slider", axis="x"))
        # frame-relative axis (no custom world entity)
        assert ("slider", (0,)) in joint._motion_calls   # XAxisJointDirection == 0

    def test_change_to_rigid(self):
        _, joint = _install(["BoomPivot"])
        _payload(jt.handler(joint_name="BoomPivot", joint_type="rigid"))
        assert ("rigid", ()) in joint._motion_calls

    def test_unknown_joint_type_errors(self):
        _install(["BoomPivot"])
        res = jt.handler(joint_name="BoomPivot", joint_type="weld")
        assert res["isError"] is True and "Unknown joint_type" in res["message"]

    def test_an_omitted_joint_type_re_types_nothing(self):
        # An omitted EDIT input changes nothing: another field's edit must not carry a motion set
        # with it, and neither input advertises a schema default a client could materialize into
        # the call - which is what would re-type this joint to 'rigid' about z.
        _, joint = _install(["BoomPivot"])
        _payload(jt.handler(joint_name="BoomPivot", flip=True))
        assert joint._motion_calls == []
        props = jt.tool.input_schema["properties"]
        assert "default" not in props["joint_type"] and "default" not in props["axis"]


# ── offset / angle (parameter inputs — full parity with the create joint tool) ──

class TestOffsetAngle:
    def test_offset_sets_expression_with_units(self):
        _, joint = _install(["BoomPivot"])
        out = _payload(jt.handler(joint_name="BoomPivot", offset=-200, units="mm"))
        # offset is a ModelParameter -> set via an explicit-units expression
        assert joint.offset.expression == "-200 mm"
        assert out["offset"] == -200

    def test_offset_inch_units(self):
        _, joint = _install(["BoomPivot"])
        _payload(jt.handler(joint_name="BoomPivot", offset=2, units="in"))
        assert joint.offset.expression == "2 in"

    def test_angle_sets_degrees_expression(self):
        _, joint = _install(["BoomPivot"])
        out = _payload(jt.handler(joint_name="BoomPivot", angle=30))
        assert joint.angle.expression == "30 deg"
        assert out["angle"] == 30

    def test_offset_counts_as_an_edit(self):
        # offset alone should NOT trip the "nothing to change" guard.
        _, joint = _install(["BoomPivot"])
        out = _payload(jt.handler(joint_name="BoomPivot", offset=5))
        assert joint.offset.expression == "5 mm"
        assert out["offset"] == 5.0

    def test_unknown_units_errors(self):
        _install(["BoomPivot"])
        res = jt.handler(joint_name="BoomPivot", offset=5, units="furlongs")
        assert res["isError"] is True and "Unknown units" in res["message"]

    def test_as_built_offset_refusal_routes_to_the_regular_joint(self, monkeypatch):
        # An AsBuiltJoint has no offset parameter for ANY motion - a slider included. The generic
        # "rigid/inferred or already 0-DOF" wording sends the agent looking for a DOF it already
        # has; the recovery is to rebuild the pair with joint_create, whose offset IS a parameter.
        _install_as_built(monkeypatch)
        res = jt.handler(joint_name="Slider_R", offset=5)
        assert res["isError"] is True
        assert "AS-BUILT" in res["message"] and "Slider_R" in res["message"]
        assert "joint_create" in res["message"]
        assert "rigid/inferred" not in res["message"]

    def test_a_regular_joint_without_an_offset_keeps_the_generic_refusal(self, monkeypatch):
        # the as-built branch must not swallow the ordinary case: a real Joint whose offset is
        # absent is still "rigid/inferred or already 0-DOF", not an as-built joint
        monkeypatch.setattr(adsk.fusion, "AsBuiltJoint", type("AsBuiltJoint", (), {}))
        _, joint = _install(["BoomPivot"])
        del joint.offset
        res = jt.handler(joint_name="BoomPivot", offset=5)
        assert res["isError"] is True
        assert "rigid/inferred" in res["message"] and "AS-BUILT" not in res["message"]

    def test_an_offset_member_that_reads_null_is_refused_like_an_absent_one(self, monkeypatch):
        # The member is PRESENT and answers None, so a guard written as hasattr() lets the null
        # through into the expression write; the guard has to be on the VALUE.
        monkeypatch.setattr(adsk.fusion, "AsBuiltJoint", type("AsBuiltJoint", (), {}))
        _install_joints([_joint(offset=None)])
        res = jt.handler(joint_name="BoomPivot", offset=5)
        assert res["isError"] is True and "rigid/inferred" in res["message"]

    def test_an_angle_member_that_reads_null_is_refused_like_an_absent_one(self, monkeypatch):
        monkeypatch.setattr(adsk.fusion, "AsBuiltJoint", type("AsBuiltJoint", (), {}))
        _install_joints([_joint(angle=None)])
        res = jt.handler(joint_name="BoomPivot", angle=30)
        assert res["isError"] is True and "no angle parameter" in res["message"]

    def test_as_built_angle_refusal_routes_to_the_regular_joint(self, monkeypatch):
        # the angle arm mirrors the offset arm: an AsBuiltJoint exposes no angle parameter either,
        # and the dead-end "no angle parameter" wording must not be what an as-built joint gets
        _install_as_built(monkeypatch)
        res = jt.handler(joint_name="Slider_R", angle=30)
        assert res["isError"] is True
        assert "AS-BUILT" in res["message"] and "joint_create" in res["message"]
        assert res["message"] != "This joint has no angle parameter."

    def test_a_regular_joint_without_an_angle_keeps_the_generic_refusal(self, monkeypatch):
        monkeypatch.setattr(adsk.fusion, "AsBuiltJoint", type("AsBuiltJoint", (), {}))
        _, joint = _install(["BoomPivot"])
        del joint.angle
        res = jt.handler(joint_name="BoomPivot", angle=30)
        assert res["isError"] is True
        assert "no angle parameter" in res["message"] and "AS-BUILT" not in res["message"]


# ── joint limits: rotation (deg/rad) AND linear (mm/cm) + rest ──────────────

class TestLimits:
    def test_rotation_limits_enable_and_set_radians(self):
        _, joint = _install(["BoomPivot"], motion="revolute")
        out = _payload(jt.handler(joint_name="BoomPivot", min_deg=-45, max_deg=90))
        lim = joint.jointMotion.rotationLimits
        assert lim.isMinimumValueEnabled is True and lim.isMaximumValueEnabled is True
        assert abs(lim.minimumValue - math.radians(-45)) < 1e-9
        assert abs(lim.maximumValue - math.radians(90)) < 1e-9
        assert out["min_deg"] == -45 and out["max_deg"] == 90

    def test_rotation_rest_value(self):
        _, joint = _install(["BoomPivot"], motion="revolute")
        _payload(jt.handler(joint_name="BoomPivot", rest_deg=10))
        lim = joint.jointMotion.rotationLimits
        assert lim.isRestValueEnabled is True
        assert abs(lim.restValue - math.radians(10)) < 1e-9

    def test_linear_limits_on_slider_in_cm(self):
        _, joint = _install(["CableSlide"], motion="slider")
        out = _payload(jt.handler(joint_name="CableSlide", min_mm=0, max_mm=300, units="mm"))
        lim = joint.jointMotion.slideLimits
        assert lim.isMaximumValueEnabled is True
        assert abs(lim.maximumValue - 30.0) < 1e-9     # 300 mm -> 30 cm
        assert out["max_mm"] == 300

    def test_linear_rest_value(self):
        _, joint = _install(["CableSlide"], motion="slider")
        _payload(jt.handler(joint_name="CableSlide", rest_mm=50, units="mm"))
        lim = joint.jointMotion.slideLimits
        assert lim.isRestValueEnabled is True
        assert abs(lim.restValue - 5.0) < 1e-9          # 50 mm -> 5 cm

    def test_rotation_limit_on_slider_errors(self):
        # min_deg on a slider (no rotationLimits) should error, not silently no-op.
        _install(["CableSlide"], motion="slider")
        res = jt.handler(joint_name="CableSlide", min_deg=10)
        assert res["isError"] is True
        assert "rotation" in res["message"].lower()

    def test_linear_limit_on_revolute_errors(self):
        _install(["BoomPivot"], motion="revolute")
        res = jt.handler(joint_name="BoomPivot", max_mm=100)
        assert res["isError"] is True
        assert "slide" in res["message"].lower() or "linear" in res["message"].lower()


class TestLimitsAreReadBackOnEdit:
    """joint_edit's limit fields are the joint's own read-back, or they are null with the unverified
    marker - never the request. The edit path carries its own copy of that plumbing (the marker is
    surfaced after the timeline rolls back), so it is pinned here as well as on the create."""

    def test_a_limit_that_did_not_take_errors_naming_it(self):
        # a JointLimits that keeps its own value: nothing raises, so only the read-back catches it
        _, joint = _install(["BoomPivot"], motion="revolute")
        joint.jointMotion.rotationLimits = _DeafLimits()
        res = jt.handler(joint_name="BoomPivot", min_deg=-45)
        assert res["isError"] is True
        assert "min_deg did not take" in res["message"]

    def test_an_unreadable_read_back_publishes_null_and_the_marker(self):
        _, joint = _install(["BoomPivot"], motion="revolute")
        joint.jointMotion.rotationLimits = _BlindLimits()
        out = _payload(jt.handler(joint_name="BoomPivot", min_deg=-45, max_deg=90))
        assert out["min_deg"] is None and out["max_deg"] is None
        assert out["changes"]["min_deg"] is None
        assert out["limits_unverified"] == ["min_deg", "max_deg"]
        assert "Limits published null" in out["note"]

    def test_a_verified_edit_carries_no_marker(self):
        _install(["BoomPivot"], motion="revolute")
        out = _payload(jt.handler(joint_name="BoomPivot", min_deg=-45))
        assert "limits_unverified" not in out and "Limits published null" not in out["note"]


# ── flip / offset / angle: the joint's own read-back, never the request ─────
#
# The limits arm above has always re-read its set; these three carry the same plumbing, so a joint
# that ACCEPTS the assignment and keeps its own value is an error rather than a published success.

class _StuckFlipJoint(FakeJoint):
    """A joint that accepts the isFlipped assignment and keeps False - the swallowed flag write."""
    def __setattr__(self, name, value):
        if name == "isFlipped":
            return object.__setattr__(self, name, False)
        return object.__setattr__(self, name, value)


class _BlindFlipJoint(FakeJoint):
    """A joint whose isFlipped READ raises - the re-read that cannot be taken."""
    def __getattribute__(self, name):
        if name == "isFlipped":
            raise RuntimeError("flip flag unreadable")
        return object.__getattribute__(self, name)


class _StuckParameter(FakeModelParameter):
    """A ModelParameter that accepts the expression assignment and keeps the value it holds -
    nothing raises, so only the read-back catches it."""
    def __init__(self, value=0.0):
        super().__init__(expression=None, value=value, tracks_expression=True)

    @FakeModelParameter.expression.setter
    def expression(self, text):
        pass


class _BlindParameter(FakeModelParameter):
    """A ModelParameter whose value READ raises - the re-read that cannot be taken."""
    def __init__(self):
        super().__init__(expression=None, value=None, tracks_expression=True)

    def __getattribute__(self, name):
        if name == "value":
            raise RuntimeError("parameter value unreadable")
        return object.__getattribute__(self, name)


class _DriftingParameter(FakeModelParameter):
    """A ModelParameter that lands its own value a fixed distance (in DATABASE units) from the one
    the expression asked for."""
    def __init__(self, drift):
        super().__init__(expression=None, value=None, tracks_expression=True)
        self._drift = drift

    @FakeModelParameter.expression.setter
    def expression(self, text):
        FakeModelParameter.expression.fset(self, text)
        self.value += self._drift


class TestSwallowedSets:
    def test_a_flip_that_did_not_take_errors_naming_it(self):
        _install_joints([_joint(cls=_StuckFlipJoint)])
        res = jt.handler(joint_name="BoomPivot", flip=True)
        assert res["isError"] is True
        assert "flip did not take" in res["message"]
        assert "reads isFlipped back as False" in res["message"]
        assert "it read False before the set" in res["message"]

    def test_an_unreadable_flip_publishes_null_and_the_marker(self):
        _install_joints([_joint(cls=_BlindFlipJoint)])
        out = _payload(jt.handler(joint_name="BoomPivot", flip=True))
        assert out["flipped"] is None and out["changes"]["flipped"] is None
        assert out["edits_unverified"] == ["flipped"]
        assert "Published null (flipped)" in out["note"]

    def test_a_flip_that_took_publishes_the_joints_own_flag(self):
        _, joint = _install(["BoomPivot"])
        out = _payload(jt.handler(joint_name="BoomPivot", flip=True))
        assert out["flipped"] is True and joint.isFlipped is True
        assert "edits_unverified" not in out and "Published null" not in out["note"]

    def test_an_offset_that_did_not_take_errors_naming_it(self):
        joint = _joint()
        joint.offset = _StuckParameter(value=0.0)
        _install_joints([joint])
        res = jt.handler(joint_name="BoomPivot", offset=-200, units="mm")
        assert res["isError"] is True
        assert "offset did not take" in res["message"]
        assert "-200.0 was requested" in res["message"]
        assert "reads back 0.0 in the same units" in res["message"]

    def test_an_angle_that_did_not_take_errors_naming_it(self):
        joint = _joint()
        joint.angle = _StuckParameter(value=0.0)
        _install_joints([joint])
        res = jt.handler(joint_name="BoomPivot", angle=30)
        assert res["isError"] is True
        assert "angle did not take" in res["message"] and "30.0 was requested" in res["message"]

    def test_a_swallowed_offset_names_the_edits_that_had_landed(self):
        # PARTIAL SUCCESS: the flip before it really took, and a bare refusal would hide that
        joint = _joint()
        joint.offset = _StuckParameter(value=0.0)
        _install_joints([joint])
        res = jt.handler(joint_name="BoomPivot", flip=True, offset=5)
        assert res["isError"] is True
        assert "Edits already applied before the failure: flipped=True" in res["message"]
        assert joint.isFlipped is True

    def test_an_unreadable_offset_publishes_null_and_the_marker(self):
        joint = _joint()
        joint.offset = _BlindParameter()
        _install_joints([joint])
        out = _payload(jt.handler(joint_name="BoomPivot", offset=5, units="mm"))
        assert out["offset"] is None and out["changes"]["offset"] is None
        assert out["edits_unverified"] == ["offset"]
        assert "Published null (offset)" in out["note"]
        # the units the expression was written in are still reported - that is the call's own fact
        assert out["changes"]["units"] == "mm"

    def test_an_unreadable_angle_publishes_null_and_the_marker(self):
        joint = _joint()
        joint.angle = _BlindParameter()
        _install_joints([joint])
        out = _payload(jt.handler(joint_name="BoomPivot", angle=30))
        assert out["angle"] is None and out["changes"]["angle"] is None
        assert out["edits_unverified"] == ["angle"]
        assert "Published null (angle)" in out["note"]

    def test_the_published_offset_is_the_parameters_own_value(self):
        # 0.0009 mm off the request - inside the band, so it lands; what is PUBLISHED is the
        # parameter's own read-back, not the 5 that was asked for.
        joint = _joint()
        joint.offset = _DriftingParameter(9e-5)      # cm, the parameter's own unit
        _install_joints([joint])
        out = _payload(jt.handler(joint_name="BoomPivot", offset=5, units="mm"))
        assert out["offset"] == 5.0009

    def test_a_read_back_past_the_band_does_not_land(self):
        # the other side of that boundary: 0.0011 mm off is past _LIMIT_BAND and is a no-take
        joint = _joint()
        joint.offset = _DriftingParameter(1.1e-4)
        _install_joints([joint])
        res = jt.handler(joint_name="BoomPivot", offset=5, units="mm")
        assert res["isError"] is True and "offset did not take" in res["message"]

    def test_an_inch_offset_is_judged_in_inches(self):
        # the expression is written in inches and the parameter reads cm - the compare happens in
        # the unit the request was made in, so a correct inch offset is not a 2.54x mismatch
        _, joint = _install(["BoomPivot"])
        out = _payload(jt.handler(joint_name="BoomPivot", offset=2, units="in"))
        assert joint.offset.expression == "2 in"
        assert abs(joint.offset.value - 5.08) < 1e-9      # cm, the database unit
        assert out["offset"] == 2.0


class _DeafLimits(_MotionLimits):
    """A JointLimits that accepts every value assignment and keeps 0.0 - the swallowed write."""
    def __setattr__(self, name, value):
        if name in ("minimumValue", "maximumValue", "restValue"):
            return object.__setattr__(self, name, 0.0)
        return object.__setattr__(self, name, value)


class _BlindLimits(_MotionLimits):
    """A JointLimits whose value read RAISES - the re-read that cannot be taken."""
    def __getattribute__(self, name):
        if name in ("minimumValue", "maximumValue", "restValue"):
            raise RuntimeError("limit value unreadable")
        return object.__getattribute__(self, name)


# ── world_axis: re-point a joint's motion to a TRUE world axis ───────────────

class TestWorldAxis:
    def test_world_axis_uses_custom_construction_axis(self):
        _, joint = _install(["BoomPivot"])
        # joint's current motion is revolute, so world_axis=z re-applies revolute with
        # CustomJointDirection + the root's zConstructionAxis ('WAXIS_Z').
        _payload(jt.handler(joint_name="BoomPivot", world_axis="z"))
        # the recorded call carries the custom world axis entity, not a frame-relative enum
        kinds = [c for c in joint._motion_calls if c[0] == "revolute"]
        assert kinds, "revolute motion should have been re-applied"
        assert kinds[-1][1][-1] == "WAXIS_Z"     # custom entity = world Z construction axis

    def test_world_axis_without_type_reuses_current(self):
        _, joint = _install(["BoomPivot"])
        out = _payload(jt.handler(joint_name="BoomPivot", world_axis="y"))
        assert out["world_axis"] == "y"
        assert out["joint_type"] == "revolute"   # reused the joint's current type

    def test_unknown_world_axis_errors(self):
        _install(["BoomPivot"])
        res = jt.handler(joint_name="BoomPivot", world_axis="q")
        assert res["isError"] is True and "Unknown world_axis" in res["message"]


# ── rotation drive is redirected to joint_drive ────────────────────────────

class TestRotationDriveRedirect:
    def test_rotation_deg_redirects_to_joint_drive(self):
        # Posing a joint to a value is joint_drive's job; joint_edit only changes the joint
        # definition, so it redirects instead of driving.
        _, joint = _install(["BoomPivot"])
        res = jt.handler(joint_name="BoomPivot", rotation_deg=90)
        assert res["isError"] is True
        assert "joint_drive" in res["message"]
        # and it must NOT have driven the value
        assert joint.jointMotion.rotationValue == 0.0


# ── re-select snap inputs ────────────────────────────────────────────────────

class TestReselectInputs:
    def test_reselect_joint_origin_name_inputs(self):
        # When input_one/input_two resolve (here as JO names via the JointOriginRef kind, which
        # walks the collection by count/item), they are assigned to geometryOrOriginOne/Two.
        design, joint = _install(["BoomPivot"])
        design.rootComponent.jointOrigins = _NamedCollection([_jo("A"), _jo("B")])
        out = _payload(jt.handler(joint_name="BoomPivot", input_one="A", input_two="B"))
        assert joint.geometryOrOriginOne.name == "A"
        assert joint.geometryOrOriginTwo.name == "B"
        assert out["input_one"] == "A" and out["input_two"] == "B"


# ── auto-recompute after the edit (downstream features settle) ──
# Editing a joint rolls the timeline marker, which can leave downstream features compute-failed until
# a full recompute. joint_edit runs computeAll itself + reports health, so the caller does not have to
# remember design_recompute.

class TestAutoRecompute:
    def test_edit_runs_computeAll(self):
        design, _ = _install(["BoomPivot"])
        out = _payload(jt.handler(joint_name="BoomPivot", flip=True))
        assert design._computes == 1
        assert out["recomputed"] is True
        assert "timeline_errors_after" not in out      # healthy timeline -> no error list

    def test_reports_downstream_errors_after_recompute(self):
        # a downstream feature ends up errored -> surfaced, not silently hidden
        design, _ = _install(
            ["BoomPivot"],
            timeline_items=[FakeTimelineObject(name="Joint1", index=0),
                            FakeTimelineObject(name="Pattern1", index=1,
                                               health=_HEALTH.ErrorFeatureHealthState)])
        out = _payload(jt.handler(joint_name="BoomPivot", offset=5, units="mm"))
        assert out["recomputed"] is True
        assert out["timeline_errors_after"] == ["Pattern1"]
        assert "over-constrain" in out["note"]


class TestEditIsNotPendingGuarded:
    """The pending-move refusal is scoped to joint CREATION, deliberately. A create adds a NEW joint
    that would freeze whatever pose the revert produced; an edit changes an EXISTING joint's
    definition and is the very call an agent reaches for to repair one. Blocking it behind a captured
    position would strand a caller whose only route back is the tool being refused - so joint_edit
    lands with the flag set, and only the create tools refuse."""

    def _pending(self, design):
        design.snapshots = SimpleNamespace(hasPendingSnapshot=True)
        return design

    def test_flip_still_lands_while_a_move_is_pending(self):
        design, joint = _install(["BoomPivot"])
        self._pending(design)
        out = _payload(jt.handler(joint_name="BoomPivot", flip=True))
        assert out["flipped"] is True and joint.isFlipped is True

    def test_a_motion_change_still_lands_while_a_move_is_pending(self):
        design, joint = _install(["BoomPivot"])
        self._pending(design)
        out = _payload(jt.handler(joint_name="BoomPivot", joint_type="slider", axis="x"))
        assert out["joint_type"] == "slider" and out["axis"] == "x"

    def test_the_edit_refusal_vocabulary_never_reaches_this_handler(self):
        design, _ = _install(["BoomPivot"])
        self._pending(design)
        res = jt.handler(joint_name="BoomPivot", flip=True)
        assert res["isError"] is False
        assert "would silently revert" not in json.dumps(res)


# ── edit_handler: posing is joint_drive's job ──────────────────────────────

class TestEditRotationRedirect:
    def test_rotation_deg_redirects_to_joint_drive(self, monkeypatch):
        monkeypatch.setattr(jt._common, "design", lambda: SimpleNamespace())
        # find_joint answers (jt, ambiguity_error_or_None) - a name several joints share refuses.
        monkeypatch.setattr(jt, "_find_joint", lambda design, name: (FakeJoint(name="J"), None))
        out = jt.handler(joint_name="J", rotation_deg=45)
        assert out["isError"] is True
        msg = out["content"][0]["text"]
        assert "joint_drive" in msg
        assert "assembly_move" not in msg


# ── _fmt_num: parameter-expression number formatting ────────────────────────
# offset/angle expressions feed straight into a Fusion ModelParameter ("{n} mm"); a trailing ".0"
# is undesirable. _fmt_num drops it for whole numbers but keeps real fractions.

class TestFmtNum:
    def test_whole_number_drops_trailing_zero(self):
        assert jt._fmt_num(200) == "200"
        assert jt._fmt_num(200.0) == "200"
        assert jt._fmt_num(-200.0) == "-200"
        assert jt._fmt_num(0) == "0"

    def test_fractional_kept(self):
        assert jt._fmt_num(2.5) == "2.5"
        assert jt._fmt_num(-0.125) == "-0.125"


# ── _world_axis_entity: pick the right root construction axis ────────────────

class TestWorldAxisEntity:
    def test_picks_axis_by_index(self):
        design = make_design(comp=_root())
        assert jt._world_axis_entity(design, 0) == "WAXIS_X"
        assert jt._world_axis_entity(design, 1) == "WAXIS_Y"
        assert jt._world_axis_entity(design, 2) == "WAXIS_Z"


class TestSuppressedEditDisclosure:
    def _rig(self, monkeypatch, suppressed):
        j = FakeJoint(name="J", motion=None, suppressed=suppressed)
        _edit_rig(monkeypatch, j)
        return j

    def test_suppressed_joint_edit_is_disclosed_as_inert(self, monkeypatch):
        # The edit is real (the write lands) but a suppressed jt positions nothing (measured:
        # the part sat 47mm from its jointed placement with no mention) - disclosed, not silent.
        j = self._rig(monkeypatch, suppressed=True)
        out = _payload(jt.handler(joint_name="J", flip=True))
        assert j.isFlipped is True                       # the write itself landed
        assert out["suppressed"] is True and "INERT" in out["note"]

    def test_active_joint_edit_carries_no_suppression_note(self, monkeypatch):
        self._rig(monkeypatch, suppressed=False)
        out = _payload(jt.handler(joint_name="J", flip=True))
        assert "suppressed" not in out and "INERT" not in out["note"]


# ── edit handler: guards, rewiring order, and the failure wordings ───────────────────────────────

def _edit_joint(tl_index=None, cls=FakeJoint, motion=None, **kw):
    """A minimal editable joint: a timeline item, and no motion or parameters unless given."""
    return cls(name="J", motion=motion,
               timeline_object=FakeTimelineObject(name="J", index=tl_index), **kw)


class _RefusingJoint(FakeJoint):
    """A joint whose ATTR assignment raises - the shape of a platform refusal mid-edit."""
    def __init__(self, attr, exc, **kw):
        object.__setattr__(self, "_refusal", (None, None))
        super().__init__(**kw)
        object.__setattr__(self, "_refusal", (attr, exc))

    def __setattr__(self, name, value):
        attr, exc = object.__getattribute__(self, "_refusal")
        if name == attr:
            raise exc
        object.__setattr__(self, name, value)


def _joint_raising_on(attr, exc, tl_index=None):
    """A joint whose ATTR assignment raises - the shape of a platform refusal mid-edit."""
    return _edit_joint(tl_index=tl_index, cls=_RefusingJoint, attr=attr, exc=exc)


def _edit_rig(monkeypatch, j, design=None):
    """Point BOTH design seams at one design and hand the edit handler `j` as the named jt."""
    d = make_design(comp=_root()) if design is None else design
    install(jt, d)
    monkeypatch.setattr(jt, "_find_joint", lambda des, n: (j, None))
    return d


class TestEditGuards:
    def test_no_active_design(self, monkeypatch):
        monkeypatch.setattr(jt._common, "design", lambda: None)
        res = jt.handler(joint_name="J", flip=True)
        assert res["isError"] is True and "No active design" in res["message"]

    def test_world_axis_on_a_joint_with_no_axis_based_motion_is_refused(self, monkeypatch):
        # world_axis re-applies the CURRENT motion type; rigid/ball (and an unreadable motion)
        # have no single axis to re-point, so there is nothing to re-apply.
        j = _edit_joint()
        _edit_rig(monkeypatch, j)
        res = jt.handler(joint_name="J", world_axis="z")
        assert res["isError"] is True and "not axis-based" in res["message"]
        assert _rolls(j) == []                          # refused before the timeline moved

    def test_an_unknown_axis_is_refused_before_the_timeline_moves(self, monkeypatch):
        j = _edit_joint()
        _edit_rig(monkeypatch, j)
        res = jt.handler(joint_name="J", joint_type="revolute", axis="q")
        assert res["isError"] is True and "Unknown axis 'q'" in res["message"]
        assert _rolls(j) == []

    def test_pin_slot_slide_axis_equal_to_the_rotation_axis_is_refused(self, monkeypatch):
        j = _edit_joint()
        _edit_rig(monkeypatch, j)
        res = jt.handler(joint_name="J", joint_type="pin_slot", axis="y", slide_axis="y")
        assert res["isError"] is True and "differ" in res["message"]
        assert _rolls(j) == []


class TestEditPinSlot:
    def test_pin_slot_reports_the_effective_slide_axis(self, monkeypatch):
        j = _edit_joint()
        _edit_rig(monkeypatch, j)
        out = _payload(jt.handler(joint_name="J", joint_type="pin_slot", axis="z"))
        assert out["joint_type"] == "pin_slot" and out["axis"] == "z"
        assert out["slide_axis"] == "x"                # default = the next frame axis
        assert j._motion_calls == [("pin_slot", (_JD.ZAxisJointDirection,
                                                 _JD.XAxisJointDirection))]


class TestEditInputResolution:
    def test_a_bad_input_one_fails_before_the_timeline_moves(self, monkeypatch):
        j = _edit_joint()
        _edit_rig(monkeypatch, j)
        monkeypatch.setattr(jt, "_resolve_input",
                            lambda d, spec: (None, spec, f"no '{spec}' here"))
        res = jt.handler(joint_name="J", input_one="Ghost")
        assert res["isError"] is True and "no 'Ghost' here" in res["message"]
        assert _rolls(j) == []

    def test_a_bad_input_two_fails_before_the_timeline_moves(self, monkeypatch):
        j = _edit_joint()
        _edit_rig(monkeypatch, j)
        monkeypatch.setattr(
            jt, "_resolve_input",
            lambda d, spec: (SimpleNamespace(name=spec), spec, None) if spec == "A"
            else (None, spec, f"no '{spec}' here"))
        res = jt.handler(joint_name="J", input_one="A", input_two="Ghost")
        assert res["isError"] is True and "no 'Ghost' here" in res["message"]
        assert _rolls(j) == []


class TestEditRewireTimelineOrder:
    """Editing rolls the marker to just before the jt, where a LATER feature does not exist -
    the platform answers a bare findObjectPath there, so the order is checked before rolling."""

    def _rig(self, monkeypatch, jo_index, joint_index):
        monkeypatch.setattr(adsk.fusion, "JointOrigin", type("JointOrigin", (), {}))
        jo = adsk.fusion.JointOrigin()
        jo.timelineObject = FakeTimelineObject(name="JO", index=jo_index)
        j = _edit_joint(tl_index=joint_index)
        _edit_rig(monkeypatch, j)
        monkeypatch.setattr(jt, "_resolve_input", lambda d, spec: (jo, spec, None))
        return j

    def test_a_later_joint_origin_is_refused_naming_both_positions(self, monkeypatch):
        j = self._rig(monkeypatch, jo_index=9, joint_index=4)
        res = jt.handler(joint_name="J", input_one="LateJO")
        assert res["isError"] is True
        assert "position 9" in res["message"] and "position 4" in res["message"]
        assert "joint_create" in res["message"]
        assert _rolls(j) == []                         # refused BEFORE the timeline is rolled

    def test_a_joint_origin_at_the_joints_own_position_is_refused(self, monkeypatch):
        # Equal index is still "not yet built" at the rolled-back marker, so the guard is >=.
        self._rig(monkeypatch, jo_index=4, joint_index=4)
        res = jt.handler(joint_name="J", input_one="SameSlotJO")
        assert res["isError"] is True and "position 4" in res["message"]

    def test_an_earlier_joint_origin_is_rewired_normally(self, monkeypatch):
        j = self._rig(monkeypatch, jo_index=1, joint_index=4)
        out = _payload(jt.handler(joint_name="J", input_one="EarlyJO"))
        assert out["input_one"] == "EarlyJO"
        assert _rolls(j)[0] is True                    # the edit did roll the marker


class TestEditMotionFailure:
    def test_a_setter_returning_false_is_reported_not_claimed_as_edited(self, monkeypatch):
        j = _edit_joint(motion_set_ok=False)
        _edit_rig(monkeypatch, j)
        res = jt.handler(joint_name="J", joint_type="revolute", axis="z")
        assert res["isError"] is True and "Could not set revolute motion" in res["message"]


class TestEditLimitsGuard:
    def test_limits_on_a_joint_with_no_motion_are_refused(self, monkeypatch):
        j = _edit_joint()
        _edit_rig(monkeypatch, j)
        res = jt.handler(joint_name="J", min_deg=10)
        assert res["isError"] is True and "no editable motion" in res["message"]


class TestEditFailureReporting:
    def test_a_platform_refusal_is_reported_as_an_error(self, monkeypatch):
        j = _joint_raising_on("isFlipped", RuntimeError("3 : the flip was refused"))
        _edit_rig(monkeypatch, j)
        res = jt.handler(joint_name="J", flip=True)
        assert res["isError"] is True
        assert "Edit failed: 3 : the flip was refused" in res["message"]
        assert "LATER in the timeline" not in res["message"]

    def test_an_object_path_failure_names_the_timeline_cause(self, monkeypatch):
        j = _joint_raising_on("geometryOrOriginOne",
                              RuntimeError("2 : InternalValidationError : findObjectPath"))
        _edit_rig(monkeypatch, j)
        monkeypatch.setattr(jt, "_resolve_input", lambda d, spec: ("G", spec, None))
        res = jt.handler(joint_name="J", input_one="SomeJO")
        assert res["isError"] is True
        assert "LATER in the timeline" in res["message"] and "joint_create" in res["message"]

    def test_the_timeline_marker_is_restored_even_when_the_edit_fails(self, monkeypatch):
        j = _joint_raising_on("isFlipped", RuntimeError("refused"))
        _edit_rig(monkeypatch, j)
        jt.handler(joint_name="J", flip=True)
        assert _rolls(j) == [True, False]              # rolled before the jt, then back


class TestEditRecomputeFailure:
    def _raising(self):
        """A design whose full recompute RAISES, and whose timeline does not read."""
        return make_design(comp=_root(), compute_raises="7 : the platform refused")

    def test_a_failing_recompute_does_not_sink_the_edit(self, monkeypatch):
        j = _edit_joint()
        _edit_rig(monkeypatch, j, design=self._raising())
        out = _payload(jt.handler(joint_name="J", flip=True))
        assert out["edited"] is True and out["flipped"] is True and j.isFlipped is True
        assert "timeline_errors_after" not in out

    def test_a_failing_recompute_is_not_reported_as_recomputed(self, monkeypatch):
        # computeAll raising means the model was NOT settled - the payload says so instead of
        # claiming a recompute that never ran.
        j = _edit_joint()
        _edit_rig(monkeypatch, j, design=self._raising())
        out = _payload(jt.handler(joint_name="J", flip=True))
        assert out["recomputed"] is False
        assert "recompute RAISED" in out["note"] and "design_recompute" in out["note"]

    def test_a_clean_recompute_still_reads_recomputed_true(self, monkeypatch):
        j = _edit_joint()
        _edit_rig(monkeypatch, j)
        out = _payload(jt.handler(joint_name="J", flip=True))
        assert out["recomputed"] is True and "full recompute" in out["note"]


class TestLimitsPartialSuccessDisclosure:
    def test_edit_limit_failure_names_the_edits_that_landed(self, monkeypatch):
        j = _edit_joint(motion=RevoluteJointMotion())
        _edit_rig(monkeypatch, j)
        res = jt.handler(joint_name="J", flip=True, min_deg=-30, max_mm=50)
        assert res["isError"] is True
        msg = res["content"][0]["text"]
        assert "Edits already applied before the failure" in msg
        assert "flipped=True" in msg and "min_deg=-30" in msg


class TestEditModelParameters:
    def test_the_joints_own_parameters_are_published_with_the_offset_note(self, monkeypatch):
        j = _edit_joint(offset=SimpleNamespace(name="d12"), angle=SimpleNamespace(name="d13"))
        _edit_rig(monkeypatch, j)
        out = _payload(jt.handler(joint_name="J", flip=True))
        assert out["model_parameters"] == {"offset": "d12", "angle": "d13"}
        assert "co-driving the geometry" in out["note"]
