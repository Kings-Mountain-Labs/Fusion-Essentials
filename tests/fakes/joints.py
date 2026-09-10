# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""The assembly world: the drivable joint motions, the joints and links, groups and contact sets."""

import math
import types

import live_api_facts as _api_facts

from tests.fakes.scaffold import _NamedCollection, fusion_fake


# ── joint motion world ────────────────────────────────────────────────────
#
# The three drivable motions are named for their live types because the joint tools select on
# type(jointMotion).__name__. Cylindrical carries NO slideDirectionVector - the live type has none.

def _limit_allows(limits, value):
    """Whether an assignment of `value` STORES: strictly beyond an ENABLED bound it is IGNORED
    (BEHAVIOR['joint_limit_out_of_range_ignored']), exactly AT a bound it lands."""
    if limits is None or not _api_facts.BEHAVIOR["joint_limit_out_of_range_ignored"]:
        return True
    if limits.isMinimumValueEnabled and value < limits.minimumValue:
        return False
    return not (limits.isMaximumValueEnabled and value > limits.maximumValue)


def _stored_rotation(radians):
    """A commanded rotation as rotationValue stores it: verbatim - neither normalized into [0,360)
    nor accumulated (BEHAVIOR['joint_revolute_value_stored_verbatim']) - on the store GRID, whose
    step is its OWN measured fact."""
    grid_deg = _api_facts.BEHAVIOR["joint_revolute_store_grid_deg"]
    if not grid_deg:
        return radians
    step = math.radians(grid_deg)
    return round(radians / step) * step


class _MotionLimits:
    """A JointLimits pair on a motion: the enable flag and the value per bound. Named without a Fake
    prefix because JointLimits has no live SHAPES dump to sweep against (test_fake_shapes_exist),
    like _Strategy above. A bound left None reads disabled.

    `disabled_values` = {bound name: number} arms a DISABLED bound with a non-zero value, which is
    the state a reader taking minimumValue/maximumValue/restValue without checking its enable flag
    publishes as a real bound. Bound names are the plain 'minimum'/'maximum'/'rest'."""

    def __init__(self, minimum=None, maximum=None, rest=None, disabled_values=None):
        held = dict(disabled_values or {})
        self.isMinimumValueEnabled = minimum is not None
        self.minimumValue = held.get("minimum", 0.0) if minimum is None else minimum
        self.isMaximumValueEnabled = maximum is not None
        self.maximumValue = held.get("maximum", 0.0) if maximum is None else maximum
        self.isRestValueEnabled = rest is not None
        self.restValue = held.get("rest", 0.0) if rest is None else rest


@fusion_fake(live_type="RigidJointMotion", facts=("shape-dump-assembly-world-2",
                                                 "enum-joint-types"))
class RigidJointMotion:
    """The rigid motion: nothing on it is drivable, so jointType is the whole read - and the NAME
    is what the shared type map keys on."""
    def __init__(self, joint_type=None):
        self.jointType = (_api_facts.ENUMS["fusion.JointTypes"]["RigidJointType"]
                          if joint_type is None else joint_type)


@fusion_fake(live_type="RevoluteJointMotion",
             facts=("shape-dump-assembly-world", "enum-joint-types",
                    "joint-limit-out-of-range-ignored", "joint-revolute-value-stored-verbatim",
                    "joint-revolute-value-tenth-degree-grid"))
class RevoluteJointMotion:
    """The revolute motion: rotationValue in RADIANS, its limits, and the axis vector a drive reads
    its heading off. An assignment beyond an enabled bound is ignored and one that lands is stored
    on the measured 0.1 deg grid. `stores` False takes every assignment and keeps NONE of them -
    the swallowed write only a read-back catches, distinct from a value a limit refused."""
    def __init__(self, value=0.0, limits=None, axis_vector=None, joint_type=None, stores=True):
        self._value = value
        self._stores = stores
        self.rotationLimits = _MotionLimits() if limits is None else limits
        self.rotationAxisVector = axis_vector
        self.jointType = (_api_facts.ENUMS["fusion.JointTypes"]["RevoluteJointType"]
                          if joint_type is None else joint_type)

    @property
    def rotationValue(self):
        return self._value

    @rotationValue.setter
    def rotationValue(self, value):
        if self._stores and _limit_allows(self.rotationLimits, value):
            self._value = _stored_rotation(value)


@fusion_fake(live_type="SliderJointMotion",
             facts=("shape-dump-assembly-world", "enum-joint-types",
                    "joint-limit-out-of-range-ignored"))
class SliderJointMotion:
    """The slider motion: slideValue in CM, its limits, and the direction vector a drive's SIGN
    follows. An assignment beyond an enabled bound is ignored; no store grid is measured for it, so
    a value that lands is kept verbatim. `stores` False takes every assignment and keeps NONE of
    them - the swallowed write only a read-back catches."""
    def __init__(self, value=0.0, limits=None, direction_vector=None, joint_type=None, stores=True):
        self._value = value
        self._stores = stores
        self.slideLimits = _MotionLimits() if limits is None else limits
        self.slideDirectionVector = direction_vector
        self.jointType = (_api_facts.ENUMS["fusion.JointTypes"]["SliderJointType"]
                          if joint_type is None else joint_type)

    @property
    def slideValue(self):
        return self._value

    @slideValue.setter
    def slideValue(self, value):
        if self._stores and _limit_allows(self.slideLimits, value):
            self._value = value


@fusion_fake(live_type="CylindricalJointMotion",
             facts=("shape-dump-assembly-world", "enum-joint-types",
                    "joint-limit-out-of-range-ignored", "joint-revolute-value-stored-verbatim",
                    "joint-revolute-value-tenth-degree-grid"))
class CylindricalJointMotion:
    """The cylindrical motion: both drivable values, each against its own limits. It exposes
    rotationAxisVector and NO slideDirectionVector, as the live type does, so a slide-heading read
    declines here exactly as it declines live. `stores` False takes every assignment to EITHER value
    and keeps neither - the swallowed write only a read-back catches. `rotation_stores` and
    `slide_stores` narrow that to ONE degree of freedom, which is the split that separates a
    two-value drive reporting per-DOF from one reporting the pair; each defaults to `stores`."""
    def __init__(self, rotation=0.0, slide=0.0, rotation_limits=None, slide_limits=None,
                 axis_vector=None, joint_type=None, stores=True, rotation_stores=None,
                 slide_stores=None):
        self._rotation = rotation
        self._slide = slide
        self._rotation_stores = stores if rotation_stores is None else rotation_stores
        self._slide_stores = stores if slide_stores is None else slide_stores
        self.rotationLimits = _MotionLimits() if rotation_limits is None else rotation_limits
        self.slideLimits = _MotionLimits() if slide_limits is None else slide_limits
        self.rotationAxisVector = axis_vector
        self.jointType = (_api_facts.ENUMS["fusion.JointTypes"]["CylindricalJointType"]
                          if joint_type is None else joint_type)

    @property
    def rotationValue(self):
        return self._rotation

    @rotationValue.setter
    def rotationValue(self, value):
        if self._rotation_stores and _limit_allows(self.rotationLimits, value):
            self._rotation = _stored_rotation(value)

    @property
    def slideValue(self):
        return self._slide

    @slideValue.setter
    def slideValue(self, value):
        if self._slide_stores and _limit_allows(self.slideLimits, value):
            self._slide = value


@fusion_fake(live_type="Joint", facts=("shape-dump-assembly-world",))
class FakeJoint:
    """One joint: the jointMotion whose SUBCLASS says what it drives, the two occurrences it
    couples, the suppress/flip flags a joint edit writes back, its entityToken and timelineObject,
    the motionLinks it takes part in, and deleteMe answering the bool its caller gates on.

    ``geometryOrOriginOne``/``Two`` are ALWAYS present, reading None where the joint names no
    reference - which is what an inferred joint answers, so a frame read falls over a null rather
    than over an absent member. ``offset``/``angle``/``parentComponent``/``objectType`` are set only
    when given, since a joint whose parameters or owner do not read is its own tested state; passing
    ``offset``/``angle`` an explicit None installs the member ANSWERING null, which is a different
    state from the absent member and the one a safe() read cannot tell from it by accident.

    ``health_readable`` False makes the healthState read RAISE - the state a compute verdict has to
    publish as null rather than read as healthy. ``motion_set_ok`` is what every setAs*JointMotion
    answers, and the calls land in ``_motion_calls``."""

    _UNSET = object()

    def __init__(self, name="Joint1", motion=None, occurrence_one=None, occurrence_two=None,
                 suppressed=False, flipped=False, entity_token=None,
                 timeline_object=None, health=None, message="", links=(), delete_ok=True,
                 geometry_one=None, geometry_two=None, offset=_UNSET, angle=_UNSET,
                 parent_component=None, object_type=None, health_readable=True,
                 motion_set_ok=True):
        self.name = name
        self.jointMotion = motion
        self.occurrenceOne = occurrence_one
        self.occurrenceTwo = occurrence_two
        self.isSuppressed = suppressed
        self.isFlipped = flipped
        self.timelineObject = timeline_object
        self.motionLinks = _NamedCollection(list(links))
        self._health_readable = health_readable
        self.healthState = health
        self.errorOrWarningMessage = message
        self.geometryOrOriginOne = geometry_one
        self.geometryOrOriginTwo = geometry_two
        if offset is not FakeJoint._UNSET:
            self.offset = offset
        if angle is not FakeJoint._UNSET:
            self.angle = angle
        if parent_component is not None:
            self.parentComponent = parent_component
        if object_type is not None:
            self.objectType = object_type
        if entity_token is not None:
            self.entityToken = entity_token
        self._motion_set_ok = motion_set_ok
        self._motion_calls = []
        self._delete_ok = delete_ok
        self._deleted = False

    @property
    def healthState(self):
        if not self._health_readable:
            raise RuntimeError("3 : the health state of this joint is unavailable")
        return self._health

    @healthState.setter
    def healthState(self, value):
        self._health = value

    @healthState.deleter
    def healthState(self):
        self._health_readable = False

    def _set_motion(self, kind, args):
        self._motion_calls.append((kind, args))
        return self._motion_set_ok

    def setAsRigidJointMotion(self):
        return self._set_motion("rigid", ())

    def setAsRevoluteJointMotion(self, *args):
        return self._set_motion("revolute", args)

    def setAsSliderJointMotion(self, *args):
        return self._set_motion("slider", args)

    def setAsCylindricalJointMotion(self, *args):
        return self._set_motion("cylindrical", args)

    def setAsPlanarJointMotion(self, *args):
        return self._set_motion("planar", args)

    def setAsBallJointMotion(self, *args):
        return self._set_motion("ball", args)

    def setAsPinSlotJointMotion(self, *args):
        return self._set_motion("pin_slot", args)

    def deleteMe(self):
        if self._delete_ok:
            self._deleted = True
        return self._delete_ok


@fusion_fake(live_type="Joints", facts=("shape-dump-assembly-world",))
class FakeJoints:
    """component.joints: the counted/by-name walk a joint resolve makes, plus createInput/add.
    createInput answers `joint_input` - None models the input that could not be built, which the
    create tools guard on - and add() appends `new_joint` and hands it back. Left unset `new_joint`
    is a default FakeJoint; an explicit None is the add that ANSWERS NOTHING, appending no joint -
    the create a caller must not report as landed."""

    _UNSET = object()

    def __init__(self, joints=(), new_joint=_UNSET, joint_input=None):
        self._joints = list(joints)
        self._new = new_joint
        self._input = joint_input
        self._calls = []

    def _live(self):
        return [j for j in self._joints if not getattr(j, "_deleted", False)]

    @property
    def count(self):
        return len(self._live())

    def item(self, i):
        return _NamedCollection(self._live()).item(i)

    def itemByName(self, name):
        return _NamedCollection(self._live()).itemByName(name)

    def createInput(self, geometry_one, geometry_two):
        self._calls.append(("createInput", geometry_one, geometry_two))
        return self._input

    def add(self, joint_input):
        self._calls.append(("add", joint_input))
        if self._new is None:
            return None
        joint = FakeJoint() if self._new is FakeJoints._UNSET else self._new
        self._joints.append(joint)
        return joint


def _value_input_number(value_input):
    """The real number a ValueInput carries (its realValue, or a bare number handed straight in), or
    None where it does not read as one - which is the state a value that cannot land models."""
    for candidate in (value_input, getattr(value_input, "realValue", None)):
        if isinstance(candidate, (int, float)) and not isinstance(candidate, bool):
            return candidate
    return None


@fusion_fake(live_type="MotionLink", facts=("shape-dump-assembly-world",))
class FakeMotionLink:
    """One motion link: the two joints it couples, the DOF pair motionOne/motionTwo a re-value reads
    back and passes through, the ratio parameters valueOne/valueTwo (whose OWN .value is the number,
    as a ModelParameter's is), isReversed, isSuppressed, and deleteMe - the rollback a link whose
    ratio would not apply is undone with. motionOne/motionTwo are set only when given: a link that
    reports NEITHER is the state a re-value refuses on rather than guessing the coupling.

    The health pair and ``timelineObject`` are PLAIN attributes, so a test expresses the link that
    answers no compute state at all by DELETING them (go_stale) - which a property would refuse.
    ``health`` left None reads HEALTHY, since a link that answers a state is the ordinary case and a
    default of "unread" would let a withheld-flag test pass without deleting anything."""
    def __init__(self, name="Link1", joint_one=None, joint_two=None, motion_one=None,
                 motion_two=None, value_one=1.0, value_two=1.0, reversed_link=False,
                 suppressed=False, set_motion_ok=True, delete_ok=True, health=None, message="",
                 timeline_object=None, entity_token=None):
        self.name = name
        self.jointOne = joint_one
        self.jointTwo = joint_two
        if motion_one is not None:
            self.motionOne = motion_one
        if motion_two is not None:
            self.motionTwo = motion_two
        self.valueOne = types.SimpleNamespace(value=value_one)
        self.valueTwo = types.SimpleNamespace(value=value_two)
        self.isReversed = reversed_link
        self.isSuppressed = suppressed
        self.healthState = (_api_facts.ENUMS["fusion.FeatureHealthStates"][
            "HealthyFeatureHealthState"] if health is None else health)
        self.errorOrWarningMessage = message
        self.timelineObject = timeline_object
        if entity_token is not None:
            self.entityToken = entity_token
        self._set_motion_ok = set_motion_ok
        self._motion_data = []
        self._delete_ok = delete_ok
        self._deleted = False

    def setMotionData(self, motion_one, value_one, motion_two, value_two, is_reversed=False):
        """The live five-argument coupling write: a DOF and a ValueInput per side, plus the
        direction. On success it LANDS all five, so a caller's read-back sees what it sent."""
        self._motion_data.append((motion_one, value_one, motion_two, value_two, bool(is_reversed)))
        if not self._set_motion_ok:
            return False
        self.motionOne, self.motionTwo = motion_one, motion_two
        for member, given in (("valueOne", value_one), ("valueTwo", value_two)):
            landed = _value_input_number(given)
            if landed is not None:
                setattr(self, member, types.SimpleNamespace(value=landed))
        self.isReversed = bool(is_reversed)
        return True

    def deleteMe(self):
        if self._delete_ok:
            self._deleted = True
        return self._delete_ok


@fusion_fake(live_type="MotionLinks", facts=("shape-dump-assembly-world",))
class FakeMotionLinks:
    """component.motionLinks: the counted/by-name walk plus createInput/add. add() answers the new
    link and a link whose deleteMe() succeeded leaves every read here, so a rolled-back create sees
    the walk it left behind."""
    def __init__(self, links=(), new_link=None, link_input=None):
        self._links = list(links)
        self._new = new_link
        self._input = link_input
        self._calls = []

    def _live(self):
        return [link for link in self._links if not getattr(link, "_deleted", False)]

    @property
    def count(self):
        return len(self._live())

    def item(self, i):
        return _NamedCollection(self._live()).item(i)

    def itemByName(self, name):
        return _NamedCollection(self._live()).itemByName(name)

    def createInput(self, joint_one, joint_two):
        self._calls.append(("createInput", joint_one, joint_two))
        return self._input

    def add(self, link_input):
        self._calls.append(("add", link_input))
        link = self._new if self._new is not None else FakeMotionLink()
        self._links.append(link)
        return link


@fusion_fake(live_type="RigidGroup", facts=("shape-dump-assembly-world",))
class FakeRigidGroup:
    """One rigid group: its name, the occurrences it holds, the suppression flag a relation edit
    writes back, and setOccurrences - which answers the bool its caller gates on and only then
    changes the membership a read-back sees."""
    def __init__(self, name="RigidGroup1", occurrences=(), suppressed=False,
                 entity_token=None, set_ok=True, delete_ok=True):
        self.name = name
        self.occurrences = _NamedCollection(list(occurrences))
        self.isSuppressed = suppressed
        if entity_token is not None:
            self.entityToken = entity_token
        self._set_ok, self._delete_ok = set_ok, delete_ok
        self._sets = []
        self._deleted = False

    def setOccurrences(self, occurrences, include_children):
        """setOccurrences(occurrences, includeChildren) -> bool - BOTH arguments are required live,
        and the call is refused unless the timeline marker sits just before the group."""
        self._sets.append((occurrences, bool(include_children)))
        if not self._set_ok:
            return False
        self.occurrences = _NamedCollection(list(occurrences))
        return True

    def deleteMe(self):
        if self._delete_ok:
            self._deleted = True
        return self._delete_ok


@fusion_fake(live_type="RigidGroups", facts=("shape-dump-assembly-world",))
class FakeRigidGroups:
    """component.rigidGroups: the counted/by-name walk plus add(occurrences, include_children),
    which answers the new group holding exactly the occurrences it was handed."""
    def __init__(self, groups=(), new_group=None):
        self._groups = list(groups)
        self._new = new_group
        self._added = []

    def _live(self):
        return [g for g in self._groups if not getattr(g, "_deleted", False)]

    @property
    def count(self):
        return len(self._live())

    def item(self, i):
        return _NamedCollection(self._live()).item(i)

    def itemByName(self, name):
        return _NamedCollection(self._live()).itemByName(name)

    def add(self, occurrences, include_children=False):
        self._added.append((occurrences, bool(include_children)))
        group = (self._new if self._new is not None
                 else FakeRigidGroup(occurrences=list(occurrences)))
        self._groups.append(group)
        return group


@fusion_fake(live_type="JointInput", facts=("shape-dump-assembly-world-2",))
class FakeJointInput:
    """The input a joint build fills before add(). Every setAs*JointMotion answers the bool its
    caller gates on and records (kind, *args) privately in _motion - which is how a test reads
    back WHICH motion was set and against what axis; `sets_ok` False is the setter that refuses."""
    def __init__(self, sets_ok=True):
        self._motion = None
        self._sets_ok = sets_ok

    def _record(self, kind, *args):
        if not self._sets_ok:
            return False
        self._motion = (kind,) + args
        return True

    def setAsRigidJointMotion(self):
        return self._record("rigid")

    def setAsRevoluteJointMotion(self, axis, *rest):
        return self._record("revolute", axis, *rest)

    def setAsSliderJointMotion(self, axis, *rest):
        return self._record("slider", axis, *rest)

    def setAsCylindricalJointMotion(self, axis, *rest):
        return self._record("cylindrical", axis, *rest)

    def setAsPlanarJointMotion(self, axis, *rest):
        return self._record("planar", axis, *rest)

    def setAsBallJointMotion(self, pitch, yaw):
        return self._record("ball", pitch, yaw)

    def setAsPinSlotJointMotion(self, rotation, slide, *rest):
        # rest = the custom rotation/slide entities, kept GROUPED: which of the two a build passed
        # is what separates a custom axis from the frame axes.
        return self._record("pin_slot", rotation, slide, rest)


@fusion_fake(live_type="JointOriginInput", facts=("shape-dump-assembly-world-2",))
class FakeJointOriginInput:
    """The input a joint origin is created from: the three axis vectors it reports and the
    offsetX/Y/Z a coordinate anchor writes onto it (each None until the build sets one)."""
    def __init__(self, primary=None, secondary=None, third=None):
        self.primaryAxisVector = primary
        self.secondaryAxisVector = secondary
        self.thirdAxisVector = third
        self.offsetX = self.offsetY = self.offsetZ = None


@fusion_fake(live_type="JointOrigin", facts=("shape-dump-assembly-world-2",))
class FakeJointOrigin:
    """A created joint origin: its name, the parentComponent it belongs to - the component owning
    the collection it was added to - and the offsetX/Y/Z ModelParameters a read-back reports."""
    def __init__(self, name="JointOrigin1", parent_component=None, offsets=None):
        self.name = name
        self.parentComponent = parent_component
        for axis, param in (offsets or {}).items():
            setattr(self, axis, param)


@fusion_fake(live_type="Snapshot", facts=("shape-dump-assembly-world-2",))
class FakeSnapshot:
    """One captured position: its name, the timelineObject that carries its index, and deleteMe -
    which answers the bool its caller gates on and only then leaves the collection."""
    def __init__(self, name="Snapshot1", timeline_object=None, delete_ok=True):
        self.name = name
        self.timelineObject = timeline_object
        self._delete_ok = delete_ok
        self._deleted = False

    def deleteMe(self):
        if self._delete_ok:
            self._deleted = True
        return self._delete_ok


@fusion_fake(live_type="AsBuiltJoint", facts=("shape-dump-assembly-world-2",
                                              "asbuilt-rigidgroup-health-via-timeline"))
class FakeAsBuiltJoint:
    """A joint built in place: the two occurrences it couples, the jointMotion whose SUBCLASS says
    what it drives, the suppress flag and deleteMe. It carries NO healthState - the measured
    absence a compute verdict has to publish as null rather than read as healthy."""
    def __init__(self, name="AsBuilt1", motion=None, occurrence_one=None, occurrence_two=None,
                 suppressed=False, delete_ok=True):
        self.name = name
        self.jointMotion = motion
        self.occurrenceOne = occurrence_one
        self.occurrenceTwo = occurrence_two
        self.isSuppressed = suppressed
        self._delete_ok = delete_ok
        self._deleted = False

    def deleteMe(self):
        if self._delete_ok:
            self._deleted = True
        return self._delete_ok


@fusion_fake(live_type="ContactSet", facts=("shape-dump-assembly-world-2",))
class FakeContactSet:
    """One contact set: its name, the suppress flag, the members read back off
    `occurencesAndBodies` - ONE 'r', the real property spelling - and deleteMe answering the bool
    its caller gates on. It starts EMPTY: a set's members are written through that property, which
    is the write a caller has to read back."""
    def __init__(self, name="Contacts1", suppressed=False, delete_ok=True):
        self.name = name
        self.occurencesAndBodies = []
        self.isSuppressed = suppressed
        self._delete_ok = delete_ok
        self._deleted = False

    def deleteMe(self):
        if self._delete_ok:
            self._deleted = True
        return self._delete_ok


@fusion_fake(factory_for="FakeJoint")
def make_joint(name="Joint1", kind="revolute", rotation=0.0, slide=0.0, rotation_limits=None,
               slide_limits=None, occurrence_one=None, occurrence_two=None, axis_vector=None,
               stores=True, rotation_stores=None, slide_stores=None, **joint_kwargs):
    """A joint carrying the motion `kind` names - 'revolute', 'slider' or 'cylindrical', the three
    a drive can move. Limits are _MotionLimits, so a drive past an enabled bound is refused the
    measured way; `stores` False makes the motion swallow every write, and on a cylindrical
    `rotation_stores`/`slide_stores` narrow that to one degree of freedom. Further keywords reach
    FakeJoint itself."""
    if kind == "revolute":
        motion = RevoluteJointMotion(rotation, rotation_limits, axis_vector, stores=stores)
    elif kind == "slider":
        motion = SliderJointMotion(slide, slide_limits, axis_vector, stores=stores)
    elif kind == "cylindrical":
        motion = CylindricalJointMotion(rotation, slide, rotation_limits, slide_limits, axis_vector,
                                        stores=stores, rotation_stores=rotation_stores,
                                        slide_stores=slide_stores)
    else:
        raise ValueError("make_joint kind is revolute, slider or cylindrical, not %r" % (kind,))
    return FakeJoint(name=name, motion=motion, occurrence_one=occurrence_one,
                     occurrence_two=occurrence_two, **joint_kwargs)
