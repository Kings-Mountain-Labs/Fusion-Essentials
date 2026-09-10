"""Unit tests for ``assembly_get.py`` — structured kinematic state of an assembly.

This is the read tool that lets the agent reason about grounding/position/joint-wiring from NUMBERS
instead of a cluttered screenshot. Pinned: units scaling on positions, the joint-type -> friendly +
DOF mapping, per-occurrence ground flags + bbox, joint connection records, and the
occurrence<->joint cross-index.
"""

import json
import math
from types import SimpleNamespace

import pytest

import live_api_facts as _api_facts
from conftest import (BRepBody, CylindricalJointMotion, FakeAsBuiltJoint as _SharedAsBuiltJoint,
                      FakeContactSet, FakeJoint, FakeMatrix3D, FakeMotionLink,
                      FakeOccurrence, FakePoint, FakeRigidGroup, FakeTimeline, FakeTimelineObject,
                      FakeVector3D, MakeComp, MakeDesign, _MotionLimits, _NamedCollection,
                      go_stale, install, load_tool, make_bbox, make_design, make_occurrence)

ap = load_tool("assembly_get")
ad = load_tool("_assembly_detail")      # the row serializers assembly_get's handler publishes

_JOINT_TYPES = _api_facts.ENUMS["fusion.JointTypes"]
_RIGID = _JOINT_TYPES["RigidJointType"]
_REVOLUTE = _JOINT_TYPES["RevoluteJointType"]
_SLIDER = _JOINT_TYPES["SliderJointType"]
_CYLINDRICAL = _JOINT_TYPES["CylindricalJointType"]
_PIN_SLOT = _JOINT_TYPES["PinSlotJointType"]
_PLANAR = _JOINT_TYPES["PlanarJointType"]
_BALL = _JOINT_TYPES["BallJointType"]
# No JointTypes member carries this - an UNMAPPED value is the subject of the tests that use it.
_UNMAPPED = 99

_STATES = _api_facts.ENUMS["fusion.FeatureHealthStates"]
_HEALTHY = _STATES["HealthyFeatureHealthState"]
_WARNING = _STATES["WarningFeatureHealthState"]
_ERROR = _STATES["ErrorFeatureHealthState"]
_SUPPRESSED = _STATES["SuppressedFeatureHealthState"]
_ROLLED_UP = _STATES["RolledBackFeatureHealthState"]


class _BlindPlacement(FakeMatrix3D):
    """A placement whose coordinate system will not read - the axes are omitted, never faked."""

    def getAsCoordinateSystem(self):
        raise RuntimeError("no coordinate system available")


def _occ(name, comp, origin=(0.0, 0.0, 0.0), bbox=None, body_bbox=None, grounded=False,
         ground_to_parent=False, body_count=1, rotation_deg=0.0, full_path=None, children=(),
         broken_children=(), transform2=None):
    """One occurrence: its placement, ground flags, bodies and the two child collections."""
    # component.occurrences is the COMPONENT-LOCAL superset - the only collection an occurrence
    # with an unresolved reference appears in; childOccurrences (assembly context) drops it.
    component = MakeComp(name=comp, occurrences=list(broken_children) + list(children))
    # body_bbox models the bodies-only boundingBox2 read; "empty" = no bodies (None). An occurrence
    # WITHOUT it carries no boundingBox2 at all, so body_aabb falls back to .boundingBox, like a
    # BRepBody - which is why the knob is passed only when the rig asks for it.
    bodies_only = ({} if body_bbox is None else
                   {"bodies_bounding_box": None if body_bbox == "empty" else make_bbox(*body_bbox)})
    # fullPathName is the ONLY thing that tells two nested instances sharing a leaf name apart;
    # a top-level occurrence's path is just its name.
    return make_occurrence(
        path=full_path or name, component=component,
        transform2=FakeMatrix3D(rotation_deg, origin) if transform2 is None else transform2,
        children=list(children), grounded=grounded, ground_to_parent=ground_to_parent,
        bodies=[BRepBody(f"Body{i + 1}") for i in range(body_count)],
        bounding_box=make_bbox(*bbox) if bbox else None, **bodies_only)


UNAVAILABLE = ("3 : The occurrence's referenced component is unavailable (broken or missing "
               "external reference).")


def _broken_occ(name="45740"):
    """An occurrence whose referenced component will not load: only `name` reads, every other
    property RAISES - so occ.component raising is the only signal a walk can gate on."""
    return make_occurrence(path=name, raises=UNAVAILABLE)


def _raising_walk():
    """root.allOccurrences on a design holding an unresolved reference - the walk itself raises,
    which is what turned this slice into an empty list published as fact."""
    return _NamedCollection(raises="2 : InternalValidationError : occ")


def _stateless(entity):
    """An entity that names itself and answers NO compute state - the residual case where nothing
    a row can ask has a verdict to give."""
    go_stale(entity, attrs=("healthState", "errorOrWarningMessage"))
    return entity


class _Motion:
    """A JointMotion carrying ONLY the value attributes its own class exposes: rotationValue
    (radians) on a revolute, slideValue (cm) on a slider, rotationValue + primarySlideValue +
    secondarySlideValue on a planar, none at all on a rigid."""
    def __init__(self, joint_type, **values):
        self.jointType = joint_type
        for key, value in values.items():
            setattr(self, key, value)


class _UnreadableMotion(_Motion):
    """A motion whose value read raises - unknown, which is not a zero."""

    @property
    def rotationValue(self):
        raise RuntimeError("3 : the value cannot be read")


class _UnreadableAxisMotion(_Motion):
    """A motion whose HEADING read raises - the direction is unknown, which is neither a zero vector
    nor an axis to guess."""

    @property
    def rotationAxisVector(self):
        raise RuntimeError("3 : the vector cannot be read")


class _MotionUnreadableJoint(FakeJoint):
    """A joint whose jointMotion READ raises: the object every motion read goes through answers
    nothing at all, so a row's type, driven value and headings all have to survive it. Distinct from
    a joint carrying a motion that answers None to one member - here the chain fails at its first
    link, which is the read that has to be guarded or the whole payload sinks with it."""

    @property
    def jointMotion(self):
        raise RuntimeError("3 : the motion cannot be read")

    @jointMotion.setter
    def jointMotion(self, value):
        pass


class _JointFrame:
    """A joint's geometryOrOriginOne/Two as a JointGeometry: origin (cm) plus primaryAxisVector (the
    frame Z), secondaryAxisVector (X) and thirdAxisVector (Y).

    The two halves sit in DIFFERENT frames, measured on a joint anchored to a face of a component
    turned 30 deg about Z and placed 8 cm out: the ORIGIN reads WORLD (the face centre at
    component-local (0.5, 0.5, 1) read its lifted world point), while the AXES read
    component-LOCAL - the geometry on a face whose local normal is (1,0,0) published exactly that
    while the face's world normal was (0.866, 0.5, 0). The axis vectors are the copy()/transformBy()
    kind an axis lift takes; a JointGeometry carries no parentComponent and no assemblyContext
    (measured), so the placement has to come from the joint's own occurrence on that side."""
    def __init__(self, origin=(0.0, 0.0, 0.0), z=(0, 0, 1), x=(1, 0, 0), y=(0, 1, 0)):
        self.origin = FakePoint(*origin)
        self.primaryAxisVector = FakeVector3D(*z)
        self.secondaryAxisVector = FakeVector3D(*x)
        self.thirdAxisVector = FakeVector3D(*y)


class _OpaqueFrame:
    """A geometry reference that EXISTS but answers nothing - every frame read raises. Distinct from
    a null reference, and it must not shadow a readable second geometry."""

    def _unreadable(self):
        raise RuntimeError("3 : the frame cannot be read")

    origin = property(_unreadable)
    primaryAxisVector = property(_unreadable)
    secondaryAxisVector = property(_unreadable)
    thirdAxisVector = property(_unreadable)


def _joint_occ(occ):
    """A joint's occurrenceOne/Two. A plain NAME is an occurrence a row only has to name; an
    occurrence OBJECT (see _frame_occ) is one whose placement a frame's axes are lifted through."""
    if not occ or not isinstance(occ, str):
        return occ or None
    return make_occurrence(path=occ)


def _joint(name, motion_type, occ1, occ2, health_state=_HEALTHY, message="", motion_values=None,
           motion_cls=_Motion, frame_one=None, frame_two=None, joint_cls=FakeJoint):
    """One joint carrying the motion `motion_type` names. Both geometry references exist and read
    null on an inferred joint - that null is what the frame read falls back over."""
    return joint_cls(name=name, motion=motion_cls(motion_type, **(motion_values or {})),
                     occurrence_one=_joint_occ(occ1), occurrence_two=_joint_occ(occ2),
                     health=health_state, message=message,
                     geometry_one=frame_one, geometry_two=frame_two)


class _AsBuiltGeometry(_JointFrame):
    """The single JointGeometry an as-built joint holds. `context` is what its own entity answers in
    assemblyContext - measured 'BlkB:1' on a joint whose occurrenceOne was 'BlkA:1', so the geometry
    NAMES the instance its component-local axes belong to and the joint's first occurrence is not
    that statement. `owner` stands in for the entity's body.parentComponent, the read that answers
    when the entity carries no context at all."""
    def __init__(self, origin=(0.0, 0.0, 0.0), z=(0, 0, 1), x=(1, 0, 0), y=(0, 1, 0),
                 context=None, owner=None, entity=True):
        super().__init__(origin=origin, z=z, x=x, y=y)
        self.entityOne = None
        if entity:
            self.entityOne = SimpleNamespace(assemblyContext=context)
            if owner is not None:
                self.entityOne.body = SimpleNamespace(parentComponent=owner)


class FakeAsBuiltJoint(_SharedAsBuiltJoint):
    """The shared as-built joint under a single `geometry`. Like the shared one it carries NO
    healthState, errorOrWarningMessage, geometryOrOriginOne or geometryOrOriginTwo AT ALL -
    measured, every one of those raises AttributeError, while `Joint` carries all four and no
    `geometry`."""
    def __init__(self, name, motion_type, occ1, occ2, geometry=None, timeline=None,
                 motion_values=None):
        super().__init__(name=name, motion=_Motion(motion_type, **(motion_values or {})),
                         occurrence_one=_joint_occ(occ1), occurrence_two=_joint_occ(occ2))
        self.geometry = geometry
        if timeline is not None:
            self.timelineObject = timeline


@pytest.fixture
def kin_design():
    """A design of occurrences + joints wired into assembly_get through a fixture, so the patches
    undo themselves. all_occs is what root.allOccurrences reports - the NESTED walk, the only one
    reaching a nested instance - while occs is the top-level root.occurrences."""
    def _build(occs=(), joints=(), all_occs=None, asbuilt=(), timeline=None, root_bodies=(),
               marker=None, constraints=None):
        # The root's own token: the placement ladder asks "is this the root component" by
        # entityToken, and a root with none would answer that on a name collision instead.
        root = MakeComp(name="Root", entity_token="ROOT", bodies=list(root_bodies),
                        occurrences=list(occs),
                        all_occurrences=list(occs) if all_occs is None else list(all_occs),
                        joints=list(joints), as_built_joints=list(asbuilt),
                        assembly_constraints=constraints)
        # marker below count = a rolled-back timeline; left None it sits at the end.
        design = make_design(comp=root,
                             timeline=FakeTimeline(list(timeline or []), marker=marker))
        install(ap, design)
        return design
    return _build


def _payload(result):
    assert result["isError"] is False, result
    return json.loads(result["content"][0]["text"])


class TestGuards:
    def test_unknown_units(self, kin_design):
        kin_design()
        res = ap.handler(units="furlong")
        assert res["isError"] is True and "Unknown units" in res["message"]

    def test_the_include_enum_matches_the_slice_tuple(self):
        # Catches a HAND-EDITED schema drifting from the tuple. It cannot catch a name added to the
        # tuple itself - both sides read it - which is what the dispatch test below covers.
        enum = ap.tool.input_schema["properties"]["include"]["items"]["enum"]
        assert sorted(enum) == sorted(ap._SLICES)

    def test_every_advertised_slice_actually_dispatches(self, kin_design):
        # A name in _SLICES that no `if ... in inc` branch reads returns the default payload
        # unchanged: the schema offers a slice the router never builds.
        kin_design(occs=[_occ("A:1", "A", origin=(1.0, 2.0, 3.0))])
        for name in ap._SLICES:
            out = _payload(ap.handler(include=[name]))
            if name == "poses":
                # poses has no key of its own - it widens each occurrence row instead.
                assert "origin" in out["occurrences"][0], name
            else:
                assert name in out, name


class TestRolledBackTimeline:
    """A rolled-back marker (markerPosition < count) means features after it - downstream joints
    included - are reverted to home while still reading healthy; assembly_get must surface that and
    NOT report is_healthy over an incomplete model (the state a joint_edit that fails to restore
    the marker leaves behind)."""

    def test_rolled_back_marker_is_incomplete_and_unhealthy(self, kin_design):
        kin_design(occs=[_occ("A:1", "A")],
                   joints=[_joint("J1", _REVOLUTE, "A:1", "B:1")],
                   timeline=[FakeTimelineObject("J1"), FakeTimelineObject("Pattern1")], marker=1)
        out = _payload(ap.handler())
        assert out["timeline_rolled_back"] is True
        assert out["is_healthy"] is False              # would be True without the rolled-back guard
        assert "ROLLED BACK" in out["note"]

    def test_marker_at_end_is_not_rolled_back(self, kin_design):
        kin_design(occs=[_occ("A:1", "A")], joints=[_joint("J1", _REVOLUTE, "A:1", "B:1")],
                   timeline=[FakeTimelineObject("J1")], marker=1)
        out = _payload(ap.handler())
        assert out["timeline_rolled_back"] is False
        assert out["is_healthy"] is True


class TestProbe:
    def test_reports_root_bodies_not_just_occurrences(self, kin_design):
        # bodies directly in the root aren't occurrences, so the occurrence loop misses them; the probe
        # must still surface them (they can't be jointed — the user needs to know they exist).
        kin_design(occs=[_occ("Sub:1", "Sub")], root_bodies=["RootBlock"])
        out = _payload(ap.handler())
        assert out["root_bodies"] == ["RootBlock"]
        assert "can't be jointed" in out["note"]

    def test_no_root_bodies_is_empty_and_no_note(self, kin_design):
        kin_design(occs=[_occ("Sub:1", "Sub")])
        out = _payload(ap.handler())
        assert out["root_bodies"] == []
        assert "root_bodies lists geometry" not in out["note"]   # note clause only when non-empty

    def test_the_default_occurrence_row_withholds_the_pose_and_names_the_slice(self, kin_design):
        # the measured flood: ~40 lines of origin/axes/bbox on each of 50 rows, 76 KB in all. The
        # default row is identity + wiring; a withheld half is only safe if the note names it.
        kin_design(occs=[_occ("Block:1", "Block", origin=(2.0, 0.0, 0.0),
                              body_bbox=((-1, -1, -1), (1, 1, 1)))])
        out = _payload(ap.handler(units="mm"))
        o = out["occurrences"][0]
        for key in ("origin", "x_axis", "y_axis", "z_axis", "bbox_center", "bbox_size"):
            assert key not in o
        assert set(o) >= {"name", "component", "grounded", "body_count", "joints"}
        assert "include=['poses']" in out["note"]

    def test_the_default_read_is_a_fraction_of_the_posed_one(self, kin_design):
        # MEASURED on the Airport Seating Primary Assembly sample: the occurrences array is 8.9 KB
        # against 17.5 KB with poses (29.7 -> 38.1 KB whole payload, 53.3 -> 78.7 KB once
        # include=['all_occurrences'] widens it); Bench reads 10.7 against 18.8 KB. Both land just
        # under 2x - the fake rig's rows carry less identity per pose, so it clears the assertion.
        kin_design(occs=[_occ(f"P{i}:1", f"P{i}", origin=(float(i), 1.0, 2.0), rotation_deg=30.0,
                              body_bbox=((0, 0, 0), (2, 1, 1))) for i in range(50)])
        size = lambda p: len(json.dumps(p, separators=(",", ":")))
        light = size(_payload(ap.handler())["occurrences"])
        posed = size(_payload(ap.handler(include=["poses"]))["occurrences"])
        assert light * 2 < posed

    def test_positions_scaled_to_display_units(self, kin_design):
        # origin in cm -> reported in mm
        kin_design(occs=[_occ("Block:1", "Block", origin=(2.0, 0.0, 0.0),
                              bbox=((-1, -1, -1), (1, 1, 1)))])
        out = _payload(ap.handler(units="mm", include=["poses"]))
        o = out["occurrences"][0]
        assert o["origin"] == [20.0, 0.0, 0.0]          # 2cm -> 20mm
        assert o["bbox_center"] == [0.0, 0.0, 0.0]
        assert o["bbox_size"] == [20.0, 20.0, 20.0]

    def test_bbox_is_bodies_only_not_the_sketch_inflated_box(self, kin_design):
        # occ.boundingBox also counts visible sketches/construction datums - live-verified: an
        # orphaned oversized sketch made a 68x10x10 body read 120x120x10 centered off the part. The
        # bbox must come from the bodies-only boundingBox2 read, NEVER the polluted plain box.
        kin_design(occs=[_occ(
            "Shaft:1", "Shaft",
            bbox=((-8, -3, 0), (4, 9, 1)),                 # sketch-polluted 120x120x10 (cm/10)
            body_bbox=((-3.4, -0.5, 0), (3.4, 0.5, 1)))])  # the body's true 68x10x10 mm
        o = _payload(ap.handler(units="mm", include=["poses"]))["occurrences"][0]
        assert o["bbox_size"] == [68.0, 10.0, 10.0]
        assert o["bbox_center"] == [0.0, 0.0, 5.0]

    def test_bbox_omitted_when_occurrence_has_no_body_geometry(self, kin_design):
        # no bodies -> boundingBox2 returns None. The bbox is OMITTED - falling back to the plain
        # .boundingBox here would report a box made purely of sketches/datums.
        kin_design(occs=[_occ("Empty:1", "Empty", bbox=((-6, -6, 0), (6, 6, 0)),
                              body_bbox="empty", body_count=0)])
        o = _payload(ap.handler(include=["poses"]))["occurrences"][0]
        assert "bbox_size" not in o and "bbox_center" not in o
        assert o["origin"] == [0.0, 0.0, 0.0]     # the pose slice DID run - the bbox alone is gone

    def test_ground_flags_and_grounded_list(self, kin_design):
        block = _occ("Block:1", "Block", grounded=True, ground_to_parent=True)
        crank = _occ("Crank:1", "Crank", grounded=False, ground_to_parent=False)
        kin_design(occs=[block, crank])
        out = _payload(ap.handler())
        assert out["grounded_occurrences"] == ["Block:1"]
        bycomp = {o["name"]: o for o in out["occurrences"]}
        assert bycomp["Crank:1"]["ground_to_parent"] is False

    def test_joint_type_and_dof_mapping(self, kin_design):
        kin_design(occs=[_occ("A:1", "A"), _occ("B:1", "B")],
                   joints=[_joint("CrankMain", _REVOLUTE, "A:1", "B:1")])
        out = _payload(ap.handler())
        j = out["joints"][0]
        assert j["type"] == "revolute" and j["dof"] == 1
        assert j["occurrence_one"] == "A:1" and j["occurrence_two"] == "B:1"

    def test_rigid_and_cylindrical_dof(self, kin_design):
        kin_design(joints=[_joint("R", _RIGID, "A:1", "B:1"),
                           _joint("C", _CYLINDRICAL, "A:1", "B:1")])
        out = _payload(ap.handler())
        by = {x["name"]: x for x in out["joints"]}
        assert by["R"]["type"] == "rigid" and by["R"]["dof"] == 0
        assert by["C"]["type"] == "cylindrical" and by["C"]["dof"] == 2

    def test_all_motion_types_and_dof(self, kin_design):
        # pin_slot=2dof, planar=3dof, ball=3dof, slider=1dof — the remaining _MOTION rows.
        kin_design(joints=[_joint("Slide", _SLIDER, "A:1", "B:1"),
                           _joint("PinSlot", _PIN_SLOT, "A:1", "B:1"),
                           _joint("Planar", _PLANAR, "A:1", "B:1"),
                           _joint("Ball", _BALL, "A:1", "B:1")])
        by = {x["name"]: x for x in _payload(ap.handler())["joints"]}
        assert by["Slide"]["type"] == "slider" and by["Slide"]["dof"] == 1
        assert by["PinSlot"]["type"] == "pin_slot" and by["PinSlot"]["dof"] == 2
        assert by["Planar"]["type"] == "planar" and by["Planar"]["dof"] == 3
        assert by["Ball"]["type"] == "ball" and by["Ball"]["dof"] == 3

    def test_unknown_motion_type_is_question_mark_with_null_dof(self, kin_design):
        kin_design(joints=[_joint("Mystery", _UNMAPPED, "A:1", "B:1")])
        j = _payload(ap.handler())["joints"][0]
        assert j["type"] == "?" and j["dof"] is None

    def test_positions_scaled_to_cm_and_inch(self, kin_design):
        # same 2cm origin reported in cm (unchanged) and in inches (2cm / 2.54).
        kin_design(occs=[_occ("Block:1", "Block", origin=(2.54, 0.0, 0.0))])
        cm = _payload(ap.handler(units="cm", include=["poses"]))["occurrences"][0]
        assert cm["origin"] == [2.54, 0.0, 0.0]
        inch = _payload(ap.handler(units="in", include=["poses"]))["occurrences"][0]
        assert inch["origin"] == [1.0, 0.0, 0.0]    # 2.54 cm -> 1 inch

    def test_occurrence_joint_cross_index(self, kin_design):
        kin_design(occs=[_occ("Crank:1", "Crank"), _occ("Block:1", "Block")],
                   joints=[_joint("CrankMain", _REVOLUTE, "Crank:1", "Block:1")])
        out = _payload(ap.handler())
        by = {o["name"]: o for o in out["occurrences"]}
        assert by["Crank:1"]["joints"] == ["CrankMain"]
        assert by["Block:1"]["joints"] == ["CrankMain"]

    def test_include_joints_false_skips(self, kin_design):
        kin_design(occs=[_occ("A:1", "A")], joints=[_joint("J", _REVOLUTE, "A:1", None)])
        out = _payload(ap.handler(include_joints=False))
        assert out["joints"] is None
        assert "joints" not in out["occurrences"][0]

    def test_include_joints_false_still_counts_and_reports_broken(self, kin_design):
        # include_joints gates EMISSION only - joint_count and broken_joints come from the always-run
        # walk (with the walk skipped they read 0/[] while joints existed, live-observed).
        kin_design(occs=[_occ("A:1", "A")],
                   joints=[_joint("Good", _REVOLUTE, "A:1", None),
                           _joint("Bad", _REVOLUTE, "A:1", None, health_state=_ERROR)])
        out = _payload(ap.handler(include_joints=False))
        assert out["joint_count"] == 2
        assert out["broken_joints"] == ["Bad"]
        assert out["is_healthy"] is False

    def test_as_built_joints_are_visible(self, kin_design):
        # as-built joints live in root.asBuiltJoints, a SEPARATE collection from root.joints. The probe
        # must read both, or a script-created as-built joint is invisible (joint_count undercounts and
        # the occurrence cross-index misses it). AsBuiltJoint exposes the same name/jointMotion/
        # occurrenceOne/Two surface, so the shared Joint fake stands in.
        kin_design(occs=[_occ("Ring:1", "Ring"), _occ("Rotor:1", "Rotor")],
                   joints=[_joint("RegularPin", _REVOLUTE, "Ring:1", "Rotor:1")],
                   asbuilt=[_joint("AsBuiltSpin", _REVOLUTE, "Rotor:1", "Ring:1")])
        out = _payload(ap.handler())
        by = {j["name"]: j for j in out["joints"]}
        assert "AsBuiltSpin" in by and by["AsBuiltSpin"]["type"] == "revolute"
        assert out["joint_count"] == 2                       # both collections counted
        occ = {o["name"]: o for o in out["occurrences"]}
        assert "AsBuiltSpin" in occ["Rotor:1"]["joints"]     # cross-indexed like any joint

    def test_broken_as_built_joint_breaks_health(self, kin_design):
        # an as-built joint that failed to compute must drop is_healthy, same as a regular joint.
        kin_design(asbuilt=[_joint("AB", _REVOLUTE, "A:1", "B:1", health_state=_WARNING,
                                   message="conflict")])
        out = _payload(ap.handler())
        assert out["is_healthy"] is False and out["broken_joints"] == ["AB"]


# ── ORIENTATION: the occurrence's rotation as three world basis axes ────────────────────────────
# The per-occurrence record reports rotation as x_axis/y_axis/z_axis unit vectors read from transform2
# via getAsCoordinateSystem. An unrotated occurrence reads identity; axes are omitted (never faked)
# when the coordinate system can't be read.

class TestOrientation:
    def test_identity_rotation_reads_axis_aligned_basis(self, kin_design):
        kin_design(occs=[_occ("Block:1", "Block", rotation_deg=0.0)])
        o = _payload(ap.handler(include=["poses"]))["occurrences"][0]
        assert o["x_axis"] == [1.0, 0.0, 0.0]
        assert o["y_axis"] == [0.0, 1.0, 0.0]
        assert o["z_axis"] == [0.0, 0.0, 1.0]

    def test_90deg_z_rotation_basis(self, kin_design):
        # a +90 deg rotation about Z: x->+Y, y->-X, z unchanged.
        kin_design(occs=[_occ("Crank:1", "Crank", rotation_deg=90.0)])
        o = _payload(ap.handler(include=["poses"]))["occurrences"][0]
        assert o["x_axis"] == [0.0, 1.0, 0.0]
        assert o["y_axis"] == [-1.0, 0.0, 0.0]
        assert o["z_axis"] == [0.0, 0.0, 1.0]

    def test_axes_omitted_when_coordinate_system_unavailable(self, kin_design):
        # getAsCoordinateSystem raises -> axes omitted, origin still reported.
        kin_design(occs=[_occ("Plain:1", "Plain",
                              transform2=_BlindPlacement(0.0, (1.0, 0.0, 0.0)))])
        o = _payload(ap.handler(units="cm", include=["poses"]))["occurrences"][0]
        assert "x_axis" not in o and "y_axis" not in o and "z_axis" not in o
        assert o["origin"] == [1.0, 0.0, 0.0]


# ── HEALTH: the thing a user sees FIRST (Compute Failed) ───────────────────────────────────────
#
# A joint can be created + wired correctly yet FAIL TO COMPUTE (mis-axised -> over-constrained).
# The probe must surface that (is_healthy / broken_joints / per-joint healthy + timeline_problems)
# so it never reports a broken assembly as fine - observed live: a joint reads healthState=1
# (Compute Failed) while every structural read looks correct.

class TestHealth:
    def test_all_healthy(self, kin_design):
        kin_design(joints=[_joint("J1", _REVOLUTE, "A:1", "B:1"),
                           _joint("J2", _SLIDER, "A:1", "B:1")])
        out = _payload(ap.handler())
        assert out["is_healthy"] is True
        assert out["broken_joints"] == []
        assert all(j["healthy"] is True for j in out["joints"])

    def test_broken_joint_surfaced(self, kin_design):
        # one joint failed to compute -> probe must flag it
        kin_design(joints=[
            _joint("Good", _REVOLUTE, "A:1", "B:1"),
            _joint("PistonSlide1", _SLIDER, "P:1", "B:1", health_state=_WARNING,
                   message="Can't resolve some component positions because there are conflicts."),
        ])
        out = _payload(ap.handler())
        assert out["is_healthy"] is False
        assert out["broken_joints"] == ["PistonSlide1"]
        by = {j["name"]: j for j in out["joints"]}
        assert by["PistonSlide1"]["healthy"] is False
        assert "conflicts" in by["PistonSlide1"]["error"]
        assert by["Good"]["healthy"] is True

    def test_suppressed_joint_is_not_broken(self, kin_design):
        # A SUPPRESSED state is an author-parked alternate (e.g. a fixture template's reversed jaw)
        # - it is INTENTIONAL, not a compute failure. Must report healthy=True and NOT drop is_healthy.
        # (Live case: 'Jaw to Y+ Stock REVERSED' suppressed and isValid=True in a CAM template.)
        kin_design(joints=[_joint("Active", _REVOLUTE, "A:1", "B:1"),
                           _joint("Parked REVERSED", _REVOLUTE, "A:1", "B:1",
                                  health_state=_SUPPRESSED)])
        out = _payload(ap.handler())
        assert out["is_healthy"] is True                 # suppression is not breakage
        assert out["broken_joints"] == []
        by = {j["name"]: j for j in out["joints"]}
        assert by["Parked REVERSED"]["healthy"] is True

    def test_unknown_rollup_state_is_not_a_problem(self, kin_design):
        # a collapsed TimelineGroup (Fusion wraps one around an inserted component) reports a rollup
        # healthState that is neither healthy/suppressed nor a real warning/error. It is NOT a
        # compute failure - _common.timeline_health ignores it, so the probe must too, or is_healthy
        # disagrees with design_get on the same design (a false 'compute failed' alarm).
        kin_design(timeline=[FakeTimelineObject("Group1", health=_ROLLED_UP)])
        out = _payload(ap.handler())
        assert out["is_healthy"] is True
        assert out["timeline_problems"] == []

    def test_stale_joint_health_flagged_when_timeline_is_clean(self, kin_design):
        # per-joint healthState LAGS the timeline after an in-place edit. When a joint reads broken but
        # the timeline shows NO errored feature, flag potential staleness + point to design_recompute.
        kin_design(joints=[_joint("Wheel_Spin", _REVOLUTE, "W:1", "A:1", health_state=_ERROR)],
                   timeline=[FakeTimelineObject("Joint1")])   # timeline CLEAN
        out = _payload(ap.handler())
        assert out["broken_joints"] == ["Wheel_Spin"]
        assert out.get("health_may_be_stale") is True
        assert "design_recompute" in out["note"]

    def test_no_stale_flag_when_timeline_also_shows_the_error(self, kin_design):
        # genuine breakage (joint AND timeline agree) -> NOT flagged as stale
        kin_design(joints=[_joint("J", _REVOLUTE, "A:1", "B:1", health_state=_ERROR)],
                   timeline=[FakeTimelineObject("J", health=_WARNING, message="broke")])
        out = _payload(ap.handler())
        assert out.get("health_may_be_stale") is None

    def test_timeline_problem_surfaced(self, kin_design):
        kin_design(timeline=[FakeTimelineObject("Extrude5"),
                             FakeTimelineObject("Fillet1", health=_WARNING,
                                                message="The fillet failed.")])
        out = _payload(ap.handler())
        assert out["is_healthy"] is False
        probs = {p["name"]: p for p in out["timeline_problems"]}
        assert "Fillet1" in probs and "Extrude5" not in probs

    def test_health_message_deduped(self, kin_design):
        # Fusion repeats the message + appends "Compute Failed<name>"; we keep the first chunk.
        msg = ("Can't resolve positions.\n\nInspect relationships.Compute FailedXCan't resolve "
               "positions.Compute FailedX")
        kin_design(joints=[_joint("X", _SLIDER, "A:1", "B:1", health_state=_WARNING, message=msg)])
        out = _payload(ap.handler())
        err = out["joints"][0]["error"]
        assert "Compute Failed" not in err and "Can't resolve positions" in err

    def test_the_published_message_is_the_shared_condensation(self, kin_design):
        # The message crosses the wire through the ONE shared reader, so the embedded newlines are
        # collapsed into a sentence that reads whole - a local first-chunk slice keeps them.
        msg = "Can't resolve positions.\n\nInspect relationships.Compute FailedX"
        kin_design(joints=[_joint("X", _SLIDER, "A:1", "B:1", health_state=_WARNING, message=msg)])
        err = _payload(ap.handler())["joints"][0]["error"]
        assert err == "Can't resolve positions. Inspect relationships."

    def test_a_message_at_the_cap_is_whole_and_one_over_is_marked_cut(self, kin_design):
        # The shared reader's exact boundary: 240 characters cross whole, 241 are cut and marked
        # with a trailing ' ...' so a shortened message never reads as a complete one.
        for length, cut in ((240, False), (241, True)):
            kin_design(joints=[_joint("X", _SLIDER, "A:1", "B:1", health_state=_WARNING,
                                      message="z" * length)])
            err = _payload(ap.handler())["joints"][0]["error"]
            assert err.endswith(" ...") is cut, length
            assert err.count("z") == (240 if cut else length), length


# ── "could not read it" is not "read it, it is fine" ────────────────────────────────────────────
#
# The shared classifier answers None for BOTH a state deliberately not flagged (Healthy, Suppressed,
# a collapsed group's rollup) and an entity carrying no healthState at all - measured, AsBuiltJoint
# raises AttributeError on healthState AND errorOrWarningMessage while its TimelineObject answers
# both. Reading that second None as "no failure" publishes healthy:true from an attribute that was
# never there. Each row reports the state its timeline item answers, and WITHHOLDS the flag where
# nothing answers - without turning a withheld flag into a false alarm.

class TestHealthWithheldWhenUnread:
    def _unread(self, kin_design, **kw):
        return kin_design(asbuilt=[FakeAsBuiltJoint(
            "Spin", _REVOLUTE, "A:1", "B:1",
            timeline=_stateless(FakeTimelineObject("Spin")), **kw)])

    def test_an_absent_health_state_publishes_NO_healthy_flag(self, kin_design):
        self._unread(kin_design)
        row = _payload(ap.handler())["joints"][0]
        assert "healthy" not in row and row["health_unknown"] is True

    def test_a_state_the_tool_chooses_not_to_flag_still_publishes_healthy(self, kin_design):
        # The discriminator: SUPPRESSED is a state that IS read and deliberately counted healthy.
        # The distinction drawn is absent-vs-read, not unflagged-vs-failed, so this row must keep
        # its healthy:true - a fixture where both cases answer alike proves nothing.
        kin_design(joints=[_joint("Parked", _REVOLUTE, "A:1", "B:1", health_state=_SUPPRESSED)])
        row = _payload(ap.handler())["joints"][0]
        assert row["healthy"] is True and "health_unknown" not in row

    def test_the_timeline_item_answers_where_the_joint_carries_no_state(self, kin_design):
        # An as-built joint's TimelineObject DOES carry healthState (measured), so the row reports a
        # state that was read rather than withholding the flag.
        kin_design(asbuilt=[FakeAsBuiltJoint("Spin", _REVOLUTE, "A:1", "B:1",
                                             timeline=FakeTimelineObject("Spin"))])
        row = _payload(ap.handler())["joints"][0]
        assert row["healthy"] is True and "health_unknown" not in row

    def test_a_failed_as_built_joint_is_read_off_that_timeline_item(self, kin_design):
        kin_design(asbuilt=[FakeAsBuiltJoint(
            "Spin", _REVOLUTE, "A:1", "B:1",
            timeline=FakeTimelineObject("Spin", health=_WARNING,
                                        message="Can't resolve positions."))])
        out = _payload(ap.handler())
        assert out["broken_joints"] == ["Spin"] and out["is_healthy"] is False
        assert out["joints"][0]["healthy"] is False
        assert "Can't resolve positions" in out["joints"][0]["error"]

    def test_a_withheld_flag_is_NOT_counted_broken(self, kin_design):
        # The opposite false alarm: reading the missing key as unhealthy would drop is_healthy on
        # every design holding a joint whose state nothing answered.
        self._unread(kin_design)
        out = _payload(ap.handler())
        assert out["broken_joints"] == [] and out["is_healthy"] is True

    def test_the_note_names_the_joints_that_publish_no_verdict(self, kin_design):
        self._unread(kin_design)
        note = _payload(ap.handler())["note"]
        assert "1 joint(s) publish NO healthy flag (Spin)" in note
        assert "is_healthy makes no claim about them" in note

    def test_no_such_note_when_every_row_carries_a_verdict(self, kin_design):
        kin_design(joints=[_joint("Pin", _REVOLUTE, "A:1", "B:1")])
        assert "publish NO healthy flag" not in _payload(ap.handler())["note"]

    def test_a_timeline_item_that_answers_no_state_is_not_a_problem(self, kin_design):
        # The timeline walk consumes the same tri-state: reading "nothing answered" as a failure
        # would publish a compute failure nobody measured.
        kin_design(timeline=[_stateless(FakeTimelineObject("Group1"))])
        out = _payload(ap.handler())
        assert out["timeline_problems"] == [] and out["is_healthy"] is True


class TestRelationHealthWithheldWhenUnread:
    """The relations rows consume the same tri-state. MotionLink and AssemblyConstraint DO carry
    healthState (measured), so a row here withholds its flag only when the read itself answers
    nothing - and a withheld flag must not reach broken_relations."""

    def test_a_motion_link_that_answers_no_state_withholds_its_flag(self, relations_design):
        relations_design(links=[_stateless(_link("ML1"))])
        out = _payload(ap.handler(include=["relations"]))
        row = out["relations"]["motion_links"][0]
        assert "healthy" not in row and row["health_unknown"] is True
        assert out["broken_relations"] == [] and out["is_healthy"] is True

    def test_a_constraint_that_answers_no_state_withholds_its_flag(self, relations_design):
        relations_design(constraints=[_stateless(_RelConstraint("AC1"))])
        out = _payload(ap.handler(include=["relations"]))
        row = out["relations"]["constraints"][0]
        assert "healthy" not in row and row["health_unknown"] is True
        assert out["broken_relations"] == [] and out["is_healthy"] is True

    def test_a_readable_relation_still_publishes_its_verdict(self, relations_design):
        relations_design(links=[_link("Good")],
                         constraints=[_RelConstraint("Bad", health=_ERROR,
                                                     message="over-constrained")])
        out = _payload(ap.handler(include=["relations"]))
        assert out["relations"]["motion_links"][0]["healthy"] is True
        assert out["relations"]["constraints"][0]["healthy"] is False
        assert [r["name"] for r in out["broken_relations"]] == ["Bad"]


# ── BOUNDED READS: occurrences/joints arrays cap + report truncated (CLAUDE.md "Bound it") ──────

class TestCaps:
    def test_occurrences_under_cap_untruncated_and_unchanged(self, kin_design):
        kin_design(occs=[_occ(f"O{i}:1", f"C{i}") for i in range(5)])
        out = _payload(ap.handler())
        assert out["occurrences_truncated"] is False
        assert len(out["occurrences"]) == 5
        assert out["occurrence_count"] == 5

    def test_occurrences_at_cap_truncates_and_flags(self, kin_design):
        kin_design(occs=[_occ(f"O{i}:1", f"C{i}") for i in range(60)])
        out = _payload(ap.handler(max_occurrences=50))
        assert out["occurrences_truncated"] is True
        assert len(out["occurrences"]) == 50
        # the full count is still honest, even though the array is capped
        assert out["occurrence_count"] == 60

    def test_joints_under_cap_untruncated_and_unchanged(self, kin_design):
        kin_design(joints=[_joint(f"J{i}", _REVOLUTE, "A:1", "B:1") for i in range(5)])
        out = _payload(ap.handler())
        assert out["joints_truncated"] is False
        assert len(out["joints"]) == 5
        assert out["joint_count"] == 5

    def test_joints_at_cap_truncates_and_flags(self, kin_design):
        kin_design(joints=[_joint(f"J{i}", _REVOLUTE, "A:1", "B:1") for i in range(120)])
        out = _payload(ap.handler(max_joints=100))
        assert out["joints_truncated"] is True
        assert len(out["joints"]) == 100
        # the full count (and health rollup) still sees every joint, even beyond the cap
        assert out["joint_count"] == 120

    def test_default_caps_are_generous_enough_for_a_normal_model(self, kin_design):
        # the DEFAULT caps (50 occurrences / 100 joints) must not bite a normal small model.
        kin_design(occs=[_occ(f"O{i}:1", f"C{i}") for i in range(10)],
                   joints=[_joint(f"J{i}", _REVOLUTE, "A:1", "B:1") for i in range(10)])
        out = _payload(ap.handler())
        assert out["occurrences_truncated"] is False
        assert out["joints_truncated"] is False


# ── include=['joint_origins']: each Joint Origin as a referenceable, handle-bearing row ──────────────
#
# The JOINT-ORIGIN SEAM read. The default omits it (and advertises it); include= adds a row per JO
# INSTANCE: name + qualified reference (bare, or '<occ>:<name>'), owning component, world position +
# frame axes, the joints that CONSUME it, and a handle. A sub-component JO is reported per occurrence.

class _StuckAxis(FakeVector3D):
    """ONE axis vector whose lift refuses. _world_axes reads each axis separately - getattr,
    copy, transformBy, axis_vec - so a single axis can come back None while its siblings resolve,
    independently of whether the placement matrix did. The shared FakeVector3D.copy() answers
    type(self), which is what carries this refusal through the copy the lift takes first."""
    def transformBy(self, m):
        return False


def _zrot(deg, tx=5.0):
    """An occurrence's transform2: a rotation of `deg` about Z plus a translation that must never
    reach a DIRECTION. Live-measured on a component turned 30 deg and moved 5 cm in X - the frame's
    lifted X read (0.866, 0.5, 0), not the 5 cm offset - so the translation is CARRIED here and the
    shared matrix drops it on a direction; a rig that left it at zero would let a
    translation-leaking lift pass."""
    return FakeMatrix3D(deg, (tx, 0.0, 0.0))


class _SliceJO:
    """`pos` is the base anchor point the reference reports. `instance_pos` maps an occurrence's
    fullPathName to the point its PROXY reports: measured on a component placed at (5,0,0) and again
    turned 90 deg at (0,10,0), the proxy answers its OWN instance's world point ((6,1,2) and
    (-1,11,2)) while the context-stripped native answers the FIRST placement's for both."""
    def __init__(self, name, pos=(0.0, 0.0, 0.0), offsets=(0.0, 0.0, 0.0), token=None, comp=None,
                 instance_pos=None):
        self.name = name
        # the BASE anchor point (cm, WORLD) - a POINT, so its own shared fake
        self.geometry = SimpleNamespace(origin=FakePoint(*pos))
        # The three axis vectors, in the OWNING COMPONENT's frame - measured: a JO on a component
        # turned 30 deg about Z still reads (1,0,0) here, natively and through a proxy alike.
        self.primaryAxisVector = FakeVector3D(0.0, 0.0, 1.0)     # Z
        self.secondaryAxisVector = FakeVector3D(1.0, 0.0, 0.0)   # X
        self.thirdAxisVector = FakeVector3D(0.0, 1.0, 0.0)       # Y
        # offsetX/Y/Z ModelParameters (cm) - a coordinate-anchored JO carries its position here.
        self.offsetX = SimpleNamespace(value=offsets[0])
        self.offsetY = SimpleNamespace(value=offsets[1])
        self.offsetZ = SimpleNamespace(value=offsets[2])
        self.entityToken = token
        self.parentComponent = comp
        self.assemblyContext = None
        self._pos, self._offsets = pos, offsets
        self._instance_pos = dict(instance_pos or {})

    def createForAssemblyContext(self, occ):
        pos = self._instance_pos.get(getattr(occ, "fullPathName", None), self._pos)
        p = _SliceJO(self.name, pos=pos, offsets=self._offsets, token=self.entityToken,
                     comp=self.parentComponent, instance_pos=self._instance_pos)
        # The proxy reports the SAME axis triple as its native (measured), so each vector is copied
        # as its own type - a per-axis read that refuses must refuse through the proxy too, which is
        # the object every sub-component row is actually built from.
        for attr in ("primaryAxisVector", "secondaryAxisVector", "thirdAxisVector"):
            v = getattr(self, attr)
            setattr(p, attr, type(v)(v.x, v.y, v.z))
        p.context = occ
        p.assemblyContext = occ      # measured: a proxy answers the occurrence it was made for
        return p


def _slice_comp(name, jos=(), token=None):
    """A component carrying joint origins, with its OWN token so a same-component test cannot pass
    on a name collision or on identity."""
    return MakeComp(name=name, joint_origins=list(jos), entity_token=token or f"COMP:{name}")


def _slice_occ(full, comp, transform2=None):
    """An occurrence placing `comp` at assembly path `full`."""
    return make_occurrence(path=full, component=comp, transform2=transform2)


def _frame_occ(name, comp="A", transform2=None):
    """The occurrence one side of a joint is anchored to - the only thing that answers for a
    JointGeometry's frame, since the geometry carries neither parentComponent nor assemblyContext.
    Its component gets its OWN entityToken, so the placement ladder's same-component test cannot
    pass on identity, and transform2 defaults to an unturned frame for a test whose subject is not
    the lift."""
    return _slice_occ(name, _slice_comp(comp), transform2=transform2 or _zrot(0.0, 0.0))


def _slice_joint(name, origin_one=None, origin_two=None):
    """A joint whose geometryOrOriginOne/Two may reference a JointOrigin (drives consumed_by)."""
    return FakeJoint(name=name, geometry_one=origin_one, geometry_two=origin_two,
                     entity_token="J:" + name)


def _slice_root(name="Root", jos=(), joints=(), occ_by_comp=None, token="ROOT"):
    """The root component of the joint-origin rigs: its joint origins, its joints, and the curated
    placement map allOccurrencesByComponent answers from."""
    return MakeComp(name=name, entity_token=token, joint_origins=list(jos), joints=list(joints),
                    as_built_joints=[], occurrences_by_component=occ_by_comp,
                    all_occurrences=[o for lst in (occ_by_comp or {}).values() for o in lst])


def _slice_design(root, subs=()):
    """A design whose allComponents carries the root AND every sub-component."""
    # counted AND iterable alike (measure_api allcomponents-design-only); _common.all_components
    # reads it with .count/.item. The root is IN it - the contract _common.all_components holds -
    # so both joint walks ask this collection alone, neither prepending design.rootComponent.
    return make_design(comp=root, all_components=[root] + list(subs))


class TestJointOriginsSlice:
    @pytest.fixture(autouse=True)
    def _root_frame_is_world(self, monkeypatch):
        # component_world_matrix answers the ROOT leg with adsk.core.Matrix3D.create(); the shared
        # mock hands back a Mock no axis lift can read, so the root frame gets a real identity here.
        import adsk.core
        monkeypatch.setattr(adsk.core.Matrix3D, "create", staticmethod(lambda: _zrot(0.0, 0.0)),
                            raising=False)

    def test_default_omits_slice_and_advertises_it(self, jo_design):
        jo_design(_slice_design(_slice_root(jos=[_SliceJO("Stock_Center", token="T")])))
        out = _payload(ap.handler())                       # no include
        assert "joint_origins" not in out
        assert "include=['joint_origins']" in out["note"]  # a flag is invisible unless advertised

    def test_unknown_include_errors(self, jo_design):
        jo_design(_slice_design(_slice_root()))
        res = ap.handler(include=["bogus"])
        assert res["isError"] is True and "bogus" in res["message"]

    def test_root_jo_row_has_handle_position_axes_and_bare_name(self, jo_design):
        jo = _SliceJO("Stock_Center", pos=(0.0, 0.0, 4.5), token="JO_TOKEN")
        jo_design(_slice_design(_slice_root(jos=[jo])))
        out = _payload(ap.handler(include=["joint_origins"], units="mm"))
        rows = out["joint_origins"]
        assert len(rows) == 1 and out["joint_origin_count"] == 1
        r = rows[0]
        assert r["name"] == "Stock_Center" and r["qualified_name"] == "Stock_Center"  # bare on root
        assert r["world_position"] == [0.0, 0.0, 45.0]     # 4.5 cm -> 45 mm
        assert r["frame"]["z_axis"] == [0.0, 0.0, 1.0] and r["frame"]["x_axis"] == [1.0, 0.0, 0.0]
        assert r["handle"] == "JO_TOKEN"

    def test_coordinate_jo_world_position_adds_offsets_to_the_base(self, jo_design):
        # a coordinate-anchored JO holds its position in offsetX/Y/Z (geometry.origin stays at the base,
        # e.g. the model origin). world_position must ADD the offsets along the frame axes - reading
        # geometry.origin alone reports the base (the live bug this closes: a +45mm-Z JO read [0,0,0]).
        jo = _SliceJO("Stock_Center", pos=(0.0, 0.0, 0.0), offsets=(0.0, 0.0, 4.5), token="T")
        jo_design(_slice_design(_slice_root(jos=[jo])))
        r = _payload(ap.handler(include=["joint_origins"], units="mm"))["joint_origins"][0]
        assert r["world_position"] == [0.0, 0.0, 45.0]     # base (0,0,0) + offsetZ 4.5cm along +Z

    def test_consumed_by_names_the_joints_that_reference_the_jo(self, jo_design):
        # the grip joint names the JO via geometryOrOriginOne -> consumed_by lists it.
        stock = _SliceJO("Stock_Center", token="S")
        vise = _SliceJO("Vise_Center", token="V")
        grip = _slice_joint("Stock_Gripped_By_Vise", origin_one=stock, origin_two=vise)
        jo_design(_slice_design(_slice_root(jos=[stock, vise], joints=[grip])))
        rows = {r["name"]: r for r in _payload(ap.handler(include=["joint_origins"]))["joint_origins"]}
        assert rows["Stock_Center"]["consumed_by"] == ["Stock_Gripped_By_Vise"]
        assert rows["Vise_Center"]["consumed_by"] == ["Stock_Gripped_By_Vise"]

    def test_unconsumed_jo_has_empty_consumed_by(self, jo_design):
        jo_design(_slice_design(_slice_root(jos=[_SliceJO("Lonely", token="L")])))
        r = _payload(ap.handler(include=["joint_origins"]))["joint_origins"][0]
        assert r["consumed_by"] == []

    def test_subcomponent_jo_reported_per_occurrence_with_qualified_name(self, jo_design):
        native = _SliceJO("Center", pos=(1.0, 0.0, 0.0), token="C")
        sub = _slice_comp("Tower", jos=[native])
        occ = _slice_occ("Tower:1", sub)
        root = _slice_root(jos=[], occ_by_comp={"Tower": [occ]})
        jo_design(_slice_design(root, subs=[sub]))
        rows = _payload(ap.handler(include=["joint_origins"]))["joint_origins"]
        assert len(rows) == 1
        assert rows[0]["qualified_name"] == "Tower:1:Center"     # the resolver-accepted form
        assert rows[0]["component"] == "Tower"

    def test_a_subcomponent_named_like_the_root_is_not_treated_as_the_root(self):
        # The reference form turns on "is this JO's owner the ROOT component". Deciding that by NAME
        # calls a sub-component that happens to carry the root's name the root, and hands back a
        # BARE 'Center' - which addresses nothing: this JO lives in an occurrence and needs its path.
        sub = _slice_comp("Root", token="SUB")       # a sub-component carrying the root's name
        root = _slice_root(name="Root", token="ROOT",
                           occ_by_comp={"Root": [_slice_occ("Root:1", sub)]})
        jo = _SliceJO("Center", token="C")
        refs = [ref for ref, _ctx in ad._jo_instances(_slice_design(root, subs=[sub]), jo, sub)]
        assert refs == ["Root:1:Center"]

    def test_a_second_wrapper_of_the_root_component_still_reads_as_the_root(self):
        # `comp is root` is always False between two wrappers of ONE component (component identity
        # is never stable), so an identity test would send every root JO down the occurrence path.
        root = _slice_root(name="Root", token="ROOT",
                           occ_by_comp={"Root": [_slice_occ("Root:1", None)]})
        other = _slice_comp("Root", token="ROOT")    # a second wrapper of the SAME component
        jo = _SliceJO("Center", token="C")
        refs = [ref for ref, _ctx in ad._jo_instances(_slice_design(root), jo, other)]
        assert refs == ["Center"]

    # The row's world heading has to hold for the AXES too. A JointOrigin reports them in its owning
    # COMPONENT's frame, so on a rotated component the unlifted pair contradicts the world_position
    # beside it AND the occurrence row in the same payload - and it corrupts the position, which
    # projects the offsets ALONG those axes. Every fixture below rotates the component: an identity
    # placement cannot tell a lifted axis from an unlifted one.

    def _rotated_tower(self, jo_design, deg=30.0, tx=5.0, offsets=(0.0, 0.0, 0.0),
                       pos=(5.0, 0.0, 0.0), blind_axis=None):
        """A 'Tower' component placed ONCE, turned `deg` about Z and `tx` cm out in X, carrying one
        JO whose part-space axes are the identity triple. `blind_axis` names ONE axis attribute whose
        lift refuses, so the row is built from a MIXED triple - one None beside two readable."""
        sub = _slice_comp("Tower")
        native = _SliceJO("Center", pos=pos, offsets=offsets, token="C", comp=sub)
        if blind_axis:
            v = getattr(native, blind_axis)
            setattr(native, blind_axis, _StuckAxis(v.x, v.y, v.z))
        sub.jointOrigins = _NamedCollection([native])
        occ = _slice_occ("Tower:1", sub, transform2=_zrot(deg, tx))
        root = _slice_root(occ_by_comp={"Tower": [occ]})
        jo_design(_slice_design(root, subs=[sub]))
        return root, sub, occ

    def test_a_rotated_components_jo_axes_are_published_in_WORLD(self, jo_design):
        # The JO reports x_axis [1,0,0] in part space while the occurrence row for the same
        # component reads [0.866, 0.5, 0]; one payload carries one frame, so the row publishes the
        # latter.
        self._rotated_tower(jo_design)
        r = _payload(ap.handler(include=["joint_origins"], units="mm"))["joint_origins"][0]
        assert r["frame"]["x_axis"] == [0.866, 0.5, 0.0]
        assert r["frame"]["y_axis"] == [-0.5, 0.866, 0.0]
        assert r["frame"]["z_axis"] == [0.0, 0.0, 1.0]      # the rotation axis is unmoved

    def test_the_lift_leaves_the_SOURCE_axis_vector_untouched(self):
        # _world_axes copies each axis BEFORE transforming it, and the copy is the whole of that
        # discipline: transformBy moves a Vector3D IN PLACE. Lifting the frame's own vector would
        # rewrite the JointOrigin's stored axis, so the NEXT read of that same frame - the second
        # instance's row, or any later reader - would lift an already-lifted axis and publish a
        # doubly-rotated heading. Only a source read AFTER the lift can see that.
        sub = _slice_comp("Tower")
        jo = _SliceJO("Center", token="C", comp=sub)
        source = jo.secondaryAxisVector
        occ = _slice_occ("Tower:1", sub, transform2=_zrot(30.0, 0.0))
        design = _slice_design(_slice_root(occ_by_comp={"Tower": [occ]}), subs=[sub])
        _z, x, _y = ad._world_axes(design, jo, sub, occ)
        assert x == [0.866, 0.5, 0.0]                              # the lift ran
        assert (source.x, source.y, source.z) == (1.0, 0.0, 0.0)   # and consumed nothing

    def test_the_world_position_projects_the_offsets_along_the_WORLD_axes(self, jo_design):
        # MEASURED live: geometry.origin reads WORLD (5,0,0) cm for a component 50 mm out in X, so a
        # 20 mm offsetX projected on the component-LOCAL (1,0,0) published [70, 0, 0] mm for a frame
        # that actually sits at [67.321, 10.0, 0]. The position and the axes stand or fall together.
        self._rotated_tower(jo_design, offsets=(2.0, 0.0, 0.0))
        r = _payload(ap.handler(include=["joint_origins"], units="mm"))["joint_origins"][0]
        assert r["world_position"] == [67.32, 10.0, 0.0]

    def test_each_instances_row_lifts_through_ITS_OWN_placement(self, jo_design):
        # one component placed twice with different rotations: the rows are per INSTANCE, so each
        # must carry the frame of the occurrence its qualified_name names, not one shared answer.
        sub = _slice_comp("Tower")
        native = _SliceJO("Center", token="C", comp=sub)
        sub.jointOrigins = _NamedCollection([native])
        occs = [_slice_occ("Tower:1", sub, transform2=_zrot(30.0, 0.0)),
                _slice_occ("Tower:2", sub, transform2=_zrot(90.0, 0.0))]
        jo_design(_slice_design(_slice_root(occ_by_comp={"Tower": occs}), subs=[sub]))
        rows = {r["qualified_name"]: r for r in
                _payload(ap.handler(include=["joint_origins"]))["joint_origins"]}
        assert rows["Tower:1:Center"]["frame"]["x_axis"] == [0.866, 0.5, 0.0]
        assert rows["Tower:2:Center"]["frame"]["x_axis"] == [0.0, 1.0, 0.0]

    def _unplaced_jo(self, jo_design, offsets=(0.0, 0.0, 0.0), pos=(5.0, 0.0, 0.0)):
        """A JO on a component the assembly does not place - reachable because all_joint_origins
        walks design.allComponents, which lists a component no occurrence references."""
        sub = _slice_comp("Orphan")
        sub.jointOrigins = _NamedCollection(
            [_SliceJO("Center", pos=pos, offsets=offsets, token="C", comp=sub)])
        jo_design(_slice_design(_slice_root(occ_by_comp={}), subs=[sub]))
        return _payload(ap.handler(include=["joint_origins"], units="mm"))["joint_origins"][0]

    def test_a_jo_with_no_placement_to_lift_through_publishes_no_frame(self, jo_design):
        # a component the assembly does not place has no world frame. Publishing the part-space
        # triple under the world key is the same defect this closes, so the key is dropped instead.
        r = self._unplaced_jo(jo_design)
        assert r["name"] == "Center" and "frame" not in r

    def test_an_unplaceable_jo_publishes_NO_world_position_for_a_nonzero_offset(self, jo_design):
        # The offsets run ALONG the frame axes, so dropping only the 'frame' key leaves the position
        # computed on a substituted world basis - the identical mixed-frame sum, now with nothing in
        # the row to show it: 5 cm base + 2 cm offsetX on (1,0,0) reads [70, 0, 0] mm for a frame
        # whose real direction was never established. Both keys go, or neither is honest.
        r = self._unplaced_jo(jo_design, offsets=(2.0, 0.0, 0.0))
        assert "frame" not in r and "world_position" not in r

    def test_an_unplaceable_jo_with_no_offsets_still_reports_its_base_anchor(self, jo_design):
        # the guard is about the PROJECTION: with every offset zero there is no direction to need,
        # and geometry.origin is a read rather than a computation, so it still travels.
        assert self._unplaced_jo(jo_design)["world_position"] == [50.0, 0.0, 0.0]

    def test_each_offset_is_gated_by_ITS_OWN_axis(self, jo_design):
        """The guard pairs offsetX/Y/Z with the frame's X/Y/Z. A MIXED triple - exactly one axis
        missing - is the only fixture that can hold that pairing: with all three present nothing
        gates, and with all three absent every pairing gates alike.

        The axis order is remapped once on the way here - _jo_row destructures (Z, X, Y) and passes
        (x, y, z) - so a swapped pair reads plausibly at both sites. Mis-paired, offsetX would be
        gated by the readable Y and sail through, X would fall to the zero vector, and its 20 mm
        contribution would VANISH from a published world_position with nothing in the row saying so.
        """
        # (a) the offset is on the MISSING axis: no direction for it to run along, so no position.
        self._rotated_tower(jo_design, offsets=(2.0, 0.0, 0.0), blind_axis="secondaryAxisVector")
        r = _payload(ap.handler(include=["joint_origins"], units="mm"))["joint_origins"][0]
        assert "world_position" not in r
        assert r["frame"]["x_axis"] is None                  # exactly one axis went missing
        assert r["frame"]["y_axis"] == [-0.5, 0.866, 0.0]

        # (b) the same missing axis, but the offset is on a PRESENT one: the position still travels
        # AND carries that offset - base [50,0,0] plus 20 mm along the lifted Y (-0.5, 0.866, 0).
        self._rotated_tower(jo_design, offsets=(0.0, 2.0, 0.0), blind_axis="secondaryAxisVector")
        r = _payload(ap.handler(include=["joint_origins"], units="mm"))["joint_origins"][0]
        assert r["world_position"] == [40.0, 17.32, 0.0]

    def test_an_unplaceable_jo_is_judged_per_AXIS_not_on_any_offset_at_all(self, jo_design):
        # the boundary is "a nonzero offset whose axis is missing", not "any nonzero offset": a
        # placed component resolves all three axes, so its offsets project and the row stands.
        self._rotated_tower(jo_design, offsets=(0.0, 0.0, 2.0))
        r = _payload(ap.handler(include=["joint_origins"], units="mm"))["joint_origins"][0]
        assert r["world_position"] == [50.0, 0.0, 20.0]     # offsetZ along the unmoved world Z

    def test_a_ROOT_jo_is_published_unlifted(self, jo_design):
        # the root component's frame IS world, so its JO's axes are already the answer - lifting
        # them through anything but identity would be this defect pointing the other way.
        jo = _SliceJO("Stock_Center", token="T")
        root = _slice_root(jos=[jo])
        jo.parentComponent = _slice_comp("Root", token="ROOT")    # a second wrapper of the root
        jo_design(_slice_design(root))
        r = _payload(ap.handler(include=["joint_origins"]))["joint_origins"][0]
        assert r["frame"]["x_axis"] == [1.0, 0.0, 0.0]

    def test_joint_origins_cap_and_truncated(self, jo_design):
        jos = [_SliceJO(f"JO{i}", token=f"T{i}") for i in range(5)]
        jo_design(_slice_design(_slice_root(jos=jos)))
        out = _payload(ap.handler(include=["joint_origins"], max_joint_origins=3))
        assert len(out["joint_origins"]) == 3
        assert out["joint_origin_count"] == 5 and out["joint_origins_truncated"] is True


# ── include=['relations']: the maintained relationships that are NOT joints ──────────────────────
#
# Rigid groups, motion links and assembly constraints live in three collections the joint walk never
# sees, so without this slice they are invisible to a reader and un-addressable by
# assembly_edit_relations. The default omits it (and advertises it); include= adds a row per
# relation carrying the name the editor resolves by and the state an edit would change.

def _rigid(name, members=(), suppressed=False, token=None):
    """One rigid group: its membership as occurrences a row reports by full path."""
    return FakeRigidGroup(name=name, occurrences=[make_occurrence(path=m) for m in members],
                          suppressed=suppressed, entity_token=token or f"RG:{name}")


def _link(name, one="CrankAxis", two="Spin", values=(1.0, 2.0), reversed_=False,
          suppressed=False, health=_HEALTHY, message="", token=None):
    """One motion link: the two joints it couples and the ratio pair the coupling means."""
    return FakeMotionLink(name=name, joint_one=FakeJoint(name=one) if one else None,
                          joint_two=FakeJoint(name=two) if two else None,
                          value_one=values[0], value_two=values[1], reversed_link=reversed_,
                          suppressed=suppressed, health=health, message=message,
                          entity_token=token or f"ML:{name}")


class _RelConstraint:
    """An AssemblyConstraint: no SHAPES dump measures the live type, so this stays bespoke."""
    def __init__(self, name, relationships=2, suppressed=False, health=_HEALTHY, message="",
                 token=None):
        self.name = name
        self.entityToken = token or f"AC:{name}"
        self.geometricRelationships = _NamedCollection([object()] * relationships)
        self.isSuppressed = suppressed
        self.healthState = health
        self.errorOrWarningMessage = message


def _rel_comp(name, rigid=(), links=(), constraints=()):
    """A component carrying the three relation collections beside its empty joint ones."""
    return MakeComp(name=name, rigid_groups=list(rigid), motion_links=list(links),
                    assembly_constraints=list(constraints), joint_origins=[], joints=[],
                    as_built_joints=[])


@pytest.fixture
def relations_design():
    """A design whose root carries the given relations (plus optional sub-components), wired into
    assembly_get through a fixture so the patches undo themselves."""
    def _build(rigid=(), links=(), constraints=(), subs=()):
        design = _slice_design(_rel_comp("Root", rigid, links, constraints), subs=subs)
        install(ap, design)
        return design
    return _build


class TestRelationsSlice:
    def test_default_omits_the_slice_and_advertises_it(self, relations_design):
        relations_design(rigid=[_rigid("RigidGroup1", members=["Frame:1", "Carrier:1"])])
        out = _payload(ap.handler())
        assert "relations" not in out and "relation_counts" not in out
        assert "include=['relations']" in out["note"]

    def test_empty_design_reports_three_empty_lists(self, relations_design):
        relations_design()
        out = _payload(ap.handler(include=["relations"]))
        assert out["relations"] == {"rigid_groups": [], "motion_links": [], "constraints": []}
        assert out["relation_counts"] == {"rigid_groups": 0, "motion_links": 0, "constraints": 0}
        assert out["relations_truncated"] is False

    def test_rigid_group_row_carries_members_and_suppression(self, relations_design):
        relations_design(rigid=[_rigid("RigidGroup1", members=["Frame:1", "Carrier:1"],
                                          suppressed=True)])
        row = _payload(ap.handler(include=["relations"]))["relations"]["rigid_groups"][0]
        assert row["name"] == "RigidGroup1" and row["component"] == "Root"
        assert row["occurrences"] == ["Frame:1", "Carrier:1"] and row["occurrence_count"] == 2
        assert row["suppressed"] is True

    def test_rigid_group_members_are_bounded_but_the_count_is_honest(self, relations_design):
        relations_design(rigid=[_rigid("Big", members=[f"P{i}:1" for i in range(30)])])
        out = _payload(ap.handler(include=["relations"]))
        row = out["relations"]["rigid_groups"][0]
        assert len(row["occurrences"]) == 12          # the member preview cap
        assert row["occurrence_count"] == 30          # the truth, uncapped
        # a capped member list must never be SILENT: the row is flagged and the top-level
        # truncation flag counts it, even though the relation LISTS themselves fit the cap.
        assert row["occurrences_truncated"] is True
        assert out["relations_truncated"] is True
        assert "occurrences_truncated" in out["note"]

    def test_an_uncapped_member_list_is_not_flagged(self, relations_design):
        relations_design(rigid=[_rigid("Small", members=["P0:1", "P1:1"])])
        out = _payload(ap.handler(include=["relations"]))
        assert out["relations"]["rigid_groups"][0]["occurrences_truncated"] is False
        assert out["relations_truncated"] is False

    def test_motion_link_row_names_both_joints_and_its_values(self, relations_design):
        relations_design(links=[_link("MotionLink1", one="CrankAxis", two="Spin",
                                         values=(1.0, 2.0), reversed_=True)])
        row = _payload(ap.handler(include=["relations"]))["relations"]["motion_links"][0]
        assert row["joint_one"] == "CrankAxis" and row["joint_two"] == "Spin"
        assert row["value_one"] == 1.0 and row["value_two"] == 2.0
        assert row["reversed"] is True and row["healthy"] is True

    def test_same_joint_link_reports_a_null_second_joint(self, relations_design):
        # jointTwo is null when a link couples two DOF of ONE joint - the row must report null
        # rather than drop the link.
        relations_design(links=[_link("SelfLink", two=None)])
        row = _payload(ap.handler(include=["relations"]))["relations"]["motion_links"][0]
        assert row["joint_one"] == "CrankAxis" and row["joint_two"] is None

    def test_a_broken_relation_reports_unhealthy_with_its_message(self, relations_design):
        relations_design(links=[_link("Bad", health=2, message="Compute Failed")],
                         constraints=[_RelConstraint("AC1", health=1, message="over-constrained")])
        rels = _payload(ap.handler(include=["relations"]))["relations"]
        assert rels["motion_links"][0]["healthy"] is False
        assert rels["constraints"][0]["healthy"] is False
        assert "over-constrained" in rels["constraints"][0]["error"]

    def test_constraint_row_counts_its_relationships(self, relations_design):
        relations_design(constraints=[_RelConstraint("AC1", relationships=3, suppressed=True)])
        row = _payload(ap.handler(include=["relations"]))["relations"]["constraints"][0]
        assert row["relationship_count"] == 3 and row["suppressed"] is True

    def test_one_two_and_many_relations_are_all_listed(self, relations_design):
        for n in (1, 2, 6):
            relations_design(rigid=[_rigid(f"RG{i}") for i in range(n)])
            out = _payload(ap.handler(include=["relations"]))
            assert out["relation_counts"]["rigid_groups"] == n
            assert len(out["relations"]["rigid_groups"]) == n

    def test_subcomponent_relations_are_listed_with_their_component(self, relations_design):
        # a relation created inside a sub-assembly lives on THAT component; a root-only read would
        # report the design as having none.
        relations_design(subs=[_rel_comp("Tower", rigid=[_rigid("SubGroup")])])
        rows = _payload(ap.handler(include=["relations"]))["relations"]["rigid_groups"]
        assert [r["name"] for r in rows] == ["SubGroup"] and rows[0]["component"] == "Tower"

    def test_cap_truncates_each_list_and_flags_it(self, relations_design):
        relations_design(rigid=[_rigid(f"RG{i}") for i in range(5)],
                         links=[_link(f"ML{i}") for i in range(4)])
        out = _payload(ap.handler(include=["relations"], max_relations=2))
        assert len(out["relations"]["rigid_groups"]) == 2
        assert len(out["relations"]["motion_links"]) == 2
        assert out["relation_counts"] == {"rigid_groups": 5, "motion_links": 4, "constraints": 0}
        assert out["relations_truncated"] is True and "max_relations" in out["note"]

    def test_relations_and_joint_origins_compose(self, relations_design):
        # each include= adds exactly its own slice; asking for both must not drop either.
        relations_design(rigid=[_rigid("RG1")])
        out = _payload(ap.handler(include=["relations", "joint_origins"]))
        assert out["relation_counts"]["rigid_groups"] == 1
        assert out["joint_origins"] == [] and out["joint_origin_count"] == 0


# ── include=['contacts']: the design's contact sets + the two contact-analysis flags ─────────────
#
# Contact sets hang off the DESIGN, not a component, so neither the joint walk nor the relations
# walk sees them. The default omits the slice (and advertises it); include= adds a row per set -
# members (previewed), the true member count, suppression - beside the two flags that decide whether
# any of them takes part at all. A ContactSet carries no entityToken and no healthState, so a row
# carries neither a handle nor a healthy key.

class _RawMember:
    """What a BODY member reads back as: an object neither cast accepts, so it counts but has no name."""


def _contact_set(name, members=(), suppressed=False):
    """One set the slice reads: the shared ContactSet fake with its membership written in through
    occurencesAndBodies - ONE 'r', the real property name."""
    row = FakeContactSet(name=name, suppressed=suppressed)
    row.occurencesAndBodies = list(members)
    return row


class _UnreadableMembers(FakeContactSet):
    """A set whose member list cannot be read at all - distinct from a set that holds nothing."""

    @property
    def occurencesAndBodies(self):
        raise RuntimeError("3 : the member list cannot be read")

    @occurencesAndBodies.setter
    def occurencesAndBodies(self, value):
        pass


class _UnreadableScopeDesign(MakeDesign):
    """A design whose isContactSetAnalysis raises - the scope has no answer, which is not all_bodies."""

    @property
    def isContactSetAnalysis(self):
        raise RuntimeError("3 : the flag cannot be read")

    @isContactSetAnalysis.setter
    def isContactSetAnalysis(self, value):
        pass


@pytest.fixture
def contacts_design(monkeypatch):
    """A design carrying contact sets and both analysis flags, wired into assembly_get through a
    fixture so the patches undo themselves. Occurrence/BRepBody casts decide which members can be
    NAMED - a member that casts to neither is the measured body case."""
    def _build(sets=(), enabled=False, use_sets=False, design_cls=MakeDesign):
        design = design_cls(_rel_comp("Root"))
        design.contactSets = _NamedCollection(sets)
        design.isContactAnalysisEnabled = enabled
        design.isContactSetAnalysis = use_sets
        install(ap, design)
        import adsk.fusion
        monkeypatch.setattr(adsk.fusion, "Occurrence",
                            type("Occurrence", (), {"cast": staticmethod(
                                lambda x: x if isinstance(x, FakeOccurrence) else None)}),
                            raising=False)
        monkeypatch.setattr(adsk.fusion, "BRepBody",
                            type("BRepBody", (), {"cast": staticmethod(lambda x: None)}),
                            raising=False)
        return design
    return _build


class TestContactsSlice:
    def test_default_omits_the_slice_and_advertises_it(self, contacts_design):
        contacts_design(sets=[_contact_set("ContactSet1")])
        out = _payload(ap.handler())
        assert "contacts" not in out and "contact_analysis" not in out
        assert "include=['contacts']" in out["note"]

    def test_empty_design_reports_an_empty_list_and_both_flags(self, contacts_design):
        contacts_design()
        out = _payload(ap.handler(include=["contacts"]))
        assert out["contacts"] == [] and out["contact_count"] == 0
        assert out["contacts_truncated"] is False
        assert out["contact_analysis"] == {"enabled": False, "scope": "all_bodies"}

    def test_a_row_carries_members_count_and_suppression(self, contacts_design):
        contacts_design(sets=[_contact_set("Frame_Panel",
                                             members=[make_occurrence("Frame:1"), make_occurrence("Panel:1")],
                                             suppressed=True)])
        row = _payload(ap.handler(include=["contacts"]))["contacts"][0]
        assert row["name"] == "Frame_Panel"
        assert row["members"] == ["Frame:1", "Panel:1"] and row["member_count"] == 2
        assert row["members_truncated"] is False and row["suppressed"] is True

    def test_a_row_carries_no_handle_and_no_health(self, contacts_design):
        # a ContactSet has neither entityToken nor healthState - inventing either key would promise
        # a round-trip and a health verdict that do not exist.
        contacts_design(sets=[_contact_set("ContactSet1", members=[make_occurrence("A:1")])])
        row = _payload(ap.handler(include=["contacts"]))["contacts"][0]
        assert "handle" not in row and "healthy" not in row

    def test_a_body_member_is_counted_but_not_named(self, contacts_design):
        # measured: a body member reads back as a raw object both casts reject. Counting it keeps
        # member_count honest; a names-only row would silently under-report the membership.
        contacts_design(sets=[_contact_set("Mixed", members=[_RawMember(), make_occurrence("A:1")])])
        row = _payload(ap.handler(include=["contacts"]))["contacts"][0]
        assert row["member_count"] == 2
        assert row["members"] == ["A:1"] and row["members_unreadable"] == 1
        # the unnamed member is DISCLOSED, not cut: names plus unreadable already account for the
        # whole membership, so nothing was truncated away - a flag counting only the named ones
        # would report this row as a preview.
        assert row["members_truncated"] is False

    def test_a_fully_readable_membership_omits_the_unreadable_count(self, contacts_design):
        # the key's ABSENCE is what says every member was named. A published 0 would make
        # "members_unreadable" in row true for every readable set, and the same key also carries
        # True for a membership that could not be read at all - so a zero there reads as
        # "unreadable, some amount". No members and two named members are both the 0 side of that
        # boundary; one unnamed member is the 1 side.
        for members in ([], [make_occurrence("Frame:1"), make_occurrence("Panel:1")]):
            contacts_design(sets=[_contact_set("Readable", members=members)])
            row = _payload(ap.handler(include=["contacts"]))["contacts"][0]
            assert "members_unreadable" not in row
            assert row["member_count"] == len(members)

    def test_members_are_previewed_but_the_count_is_honest(self, contacts_design):
        contacts_design(sets=[_contact_set("Big", members=[make_occurrence(f"P{i}:1") for i in range(30)])])
        out = _payload(ap.handler(include=["contacts"]))
        row = out["contacts"][0]
        assert len(row["members"]) == 12              # the member preview cap
        assert row["member_count"] == 30              # the truth, uncapped
        assert row["members_truncated"] is True
        assert out["contacts_truncated"] is True and "members_truncated" in out["note"]

    def test_one_two_and_many_sets_are_all_listed(self, contacts_design):
        for n in (1, 2, 6):
            contacts_design(sets=[_contact_set(f"CS{i}") for i in range(n)])
            out = _payload(ap.handler(include=["contacts"]))
            assert out["contact_count"] == n and len(out["contacts"]) == n

    def test_the_list_cap_truncates_and_flags_it(self, contacts_design):
        contacts_design(sets=[_contact_set(f"CS{i}") for i in range(5)])
        out = _payload(ap.handler(include=["contacts"], max_contacts=2))
        assert len(out["contacts"]) == 2 and out["contact_count"] == 5
        assert out["contacts_truncated"] is True and "max_contacts" in out["note"]

    def test_analysis_on_with_the_sets_is_reported_without_an_inert_warning(self, contacts_design):
        contacts_design(sets=[_contact_set("CS1")], enabled=True, use_sets=True)
        out = _payload(ap.handler(include=["contacts"]))
        assert out["contact_analysis"] == {"enabled": True, "scope": "contact_sets"}
        assert "INERT" not in out["note"]

    def test_analysis_on_but_scoped_to_all_bodies_says_the_sets_are_ignored(self, contacts_design):
        # analysis being ON is not enough: scoped to all bodies, every set listed takes no part, and
        # a list with no disclosure reads as "these are in force".
        contacts_design(sets=[_contact_set("CS1")], enabled=True, use_sets=False)
        out = _payload(ap.handler(include=["contacts"]))
        assert out["contact_analysis"]["scope"] == "all_bodies"
        assert "IGNORED" in out["note"] and "set_analysis_scope" in out["note"]
        assert "INERT" not in out["note"]            # that word is the analysis-OFF case

    def test_an_unreadable_scope_is_null_not_all_bodies(self, contacts_design):
        # all_bodies would be a fabricated answer for a flag that never answered - and it would also
        # trigger the ignored-sets disclosure over a scope nobody read.
        contacts_design(sets=[_contact_set("CS1")], enabled=True,
                        design_cls=_UnreadableScopeDesign)
        out = _payload(ap.handler(include=["contacts"]))
        assert out["contact_analysis"] == {"enabled": True, "scope": None}
        assert "IGNORED" not in out["note"]

    def test_an_unreadable_member_list_is_not_reported_as_an_empty_set(self, contacts_design):
        # member_count 0 would claim the set holds nothing; null plus the flag leaves it unclaimed.
        contacts_design(sets=[_UnreadableMembers("Opaque")])
        row = _payload(ap.handler(include=["contacts"]))["contacts"][0]
        assert row["member_count"] is None and row["members_unreadable"] is True
        assert "members" not in row and "members_truncated" not in row
        assert _payload(ap.handler(include=["contacts"]))["contacts_truncated"] is False

    def test_sets_listed_while_analysis_is_off_are_flagged_inert(self, contacts_design):
        # the list alone would read as "these are in force"; with analysis off none of them acts.
        contacts_design(sets=[_contact_set("CS1")], enabled=False)
        out = _payload(ap.handler(include=["contacts"]))
        assert "INERT" in out["note"] and "enable_analysis" in out["note"]

    def test_no_inert_warning_when_there_are_no_sets(self, contacts_design):
        contacts_design(enabled=False)
        assert "INERT" not in _payload(ap.handler(include=["contacts"]))["note"]

    def test_contacts_composes_with_the_other_slices(self, contacts_design):
        contacts_design(sets=[_contact_set("CS1")])
        out = _payload(ap.handler(include=["contacts", "relations"]))
        assert out["contact_count"] == 1
        assert out["relation_counts"] == {"rigid_groups": 0, "motion_links": 0, "constraints": 0}


class TestSuppressedJointDisclosure:
    def test_suppressed_joint_counted_once_and_disclosed(self, kin_design):
        # A suppressed joint is inert, not broken: healthy stays true, the record carries
        # is_suppressed, and the rollup names it in suppressed_joints (measured defects:
        # a plain-healthy read, and a dead token double-counting one joint).
        j = _joint("Hinge", _REVOLUTE, "A:1", "B:1", health_state=_SUPPRESSED)
        j.isSuppressed = True
        kin_design(joints=[j])
        out = _payload(ap.handler())
        assert out["joint_count"] == 1
        assert out["suppressed_joints"] == ["Hinge"]
        rec = out["joints"][0]
        assert rec["is_suppressed"] is True and rec["healthy"] is True
        assert out["is_healthy"] is True

    def test_active_joint_carries_no_suppression_field(self, kin_design):
        j = _joint("Hinge", _REVOLUTE, "A:1", "B:1")
        j.isSuppressed = False
        kin_design(joints=[j])
        rec = _payload(ap.handler())["joints"][0]
        assert "is_suppressed" not in rec


class TestBrokenRelationInHeadline:
    def test_failed_constraint_drops_is_healthy_without_the_relations_slice(self, kin_design):
        # Measured: a failed assembly constraint left is_healthy true and showed only under
        # include=['relations'] - the headline flag now folds relation health in.
        kin_design(constraints=[_RelConstraint("Constraint 1", health=_ERROR,
                                               message="conflicts with a joint")])
        out = _payload(ap.handler())
        assert out["is_healthy"] is False
        assert out["broken_relations"] == [
            {"kind": "constraint", "name": "Constraint 1", "error": "conflicts with a joint"}]

    def test_healthy_relations_leave_the_headline_alone(self, kin_design):
        kin_design(constraints=[_RelConstraint("Constraint 1")])
        out = _payload(ap.handler())
        assert out["is_healthy"] is True and out["broken_relations"] == []


# ── value_now: the joint's CURRENT driven value, straight off its motion ─────────────────────────
#
# The motion class carries the value (RevoluteJointMotion.rotationValue in radians,
# SliderJointMotion.slideValue in cm, PlanarJointMotion's rotation + both slides). Without it on the
# row a caller has to infer a joint angle from the occurrences' basis vectors. The wire units are the
# ones the row's LIMITS already use: degrees and mm.

class TestJointValueNow:
    def test_revolute_angle_is_converted_from_radians_to_degrees(self, kin_design):
        kin_design(joints=[_joint("Hinge", _REVOLUTE, "A:1", "B:1",
                                     motion_values={"rotationValue": math.radians(30)})])
        rec = _payload(ap.handler())["joints"][0]
        assert rec["value_now"] == {"angle_deg": 30.0}

    def test_slider_value_is_converted_from_cm_to_mm(self, kin_design):
        kin_design(joints=[_joint("Travel", _SLIDER, "A:1", "B:1",
                                     motion_values={"slideValue": 2.5})])
        rec = _payload(ap.handler())["joints"][0]
        assert rec["value_now"] == {"slide_mm": 25.0}

    def test_planar_reports_its_rotation_and_both_slides(self, kin_design):
        kin_design(joints=[_joint("Pad", _PLANAR, "A:1", "B:1",
                                     motion_values={"rotationValue": math.radians(-45),
                                                    "primarySlideValue": 1.2,
                                                    "secondarySlideValue": -0.35})])
        rec = _payload(ap.handler())["joints"][0]
        assert rec["value_now"] == {"angle_deg": -45.0, "slide_primary_mm": 12.0,
                                    "slide_secondary_mm": -3.5}

    def test_rigid_joint_carries_no_value(self, kin_design):
        # a rigid motion exposes no value at all - an invented 0.0 would read as "driven to zero".
        kin_design(joints=[_joint("Locked", _RIGID, "A:1", "B:1")])
        assert "value_now" not in _payload(ap.handler())["joints"][0]

    def test_an_unreadable_value_is_omitted_not_reported_as_zero(self, kin_design):
        kin_design(joints=[_joint("Hinge", _REVOLUTE, "A:1", "B:1", motion_cls=_UnreadableMotion)])
        assert "value_now" not in _payload(ap.handler())["joints"][0]

    def test_value_now_is_the_current_pose_beside_the_limits(self, kin_design):
        # the two are separate reads in the same units: 30 deg driven inside a +/-60 deg limit.
        j = _joint("Swing", _REVOLUTE, "A:1", "B:1", motion_values={"rotationValue": math.radians(30)})
        j.jointMotion.rotationLimits = _MotionLimits(minimum=math.radians(-60),
                                                     maximum=math.radians(60))
        kin_design(joints=[j])
        rec = _payload(ap.handler())["joints"][0]
        assert rec["value_now"] == {"angle_deg": 30.0}
        assert rec["rotation_limits_deg"] == {"min": -60.0, "max": 60.0}


# ── rotation_axis / slide_direction: the heading the MOTION reports for the joint's DOF ──────────
#
# The frame below carries the OFFSET direction as its z_axis; the motion carries the heading of the
# DOF the joint turns or slides on. They are separate reads - whether the two coincide depends on
# the direction the joint was built on, and is not established here - so each row states the
# headings for the DOF its kind has, and states none it could not read. The note beside them
# carries the two claims a bare vector cannot: which SPACE the numbers are in and how far that is
# measured, and which kinds are not read at all - a planar row publishes three DOF while carrying
# no heading, so an unscoped "the read answered nothing" would report a deliberate silence as a
# failure.

class TestJointMotionAxes:
    _BOTH = {"rotationAxisVector": FakeVector3D(0.0, 1.0, 0.0),
             "slideDirectionVector": FakeVector3D(0.0, 0.0, 1.0)}

    def test_a_revolute_carries_the_rotation_axis_and_no_slide_direction(self, kin_design):
        # The motion carries BOTH vectors, so the absent key is the KIND gate and not an absent
        # member: a revolute has no slide DOF, and a slide_direction on its row is a heading the
        # joint cannot move along.
        kin_design(joints=[_joint("Hinge", _REVOLUTE, "A:1", "B:1", motion_values=dict(self._BOTH))])
        rec = _payload(ap.handler())["joints"][0]
        assert rec["rotation_axis"] == [0.0, 1.0, 0.0]
        assert "slide_direction" not in rec

    def test_a_slider_carries_the_slide_direction_and_no_rotation_axis(self, kin_design):
        kin_design(joints=[_joint("Travel", _SLIDER, "A:1", "B:1", motion_values=dict(self._BOTH))])
        rec = _payload(ap.handler())["joints"][0]
        assert rec["slide_direction"] == [0.0, 0.0, 1.0]
        assert "rotation_axis" not in rec

    def test_a_cylindrical_carries_its_rotation_axis_and_NO_slide_direction(self, kin_design):
        # Read on the MEASURED CylindricalJointMotion shape: it exposes rotationAxisVector and no
        # slideDirectionVector, so the slide DOF has no heading to publish and the row states none.
        motion = CylindricalJointMotion(axis_vector=FakeVector3D(0.0, 1.0, 0.0))
        kin_design(joints=[FakeJoint(name="Spin", motion=motion, health=_HEALTHY,
                                     occurrence_one=_joint_occ("A:1"),
                                     occurrence_two=_joint_occ("B:1"))])
        rec = _payload(ap.handler())["joints"][0]
        assert rec["type"] == "cylindrical" and rec["dof"] == 2
        assert rec["rotation_axis"] == [0.0, 1.0, 0.0]
        assert "slide_direction" not in rec

    def test_a_rigid_joint_carries_neither_heading(self, kin_design):
        # a rigid joint moves along nothing; publishing a vector it exposed would name a DOF it has
        # not got.
        kin_design(joints=[_joint("Locked", _RIGID, "A:1", "B:1", motion_values=dict(self._BOTH))])
        rec = _payload(ap.handler())["joints"][0]
        assert "rotation_axis" not in rec and "slide_direction" not in rec

    def test_a_motion_carrying_no_such_member_publishes_no_key(self, kin_design):
        # the value reads fine and the vector member is simply not there - the key is WITHHELD
        # rather than filled with a zero vector, which would read as an axis pointing nowhere.
        kin_design(joints=[_joint("Hinge", _REVOLUTE, "A:1", "B:1",
                                     motion_values={"rotationValue": math.radians(15)})])
        rec = _payload(ap.handler())["joints"][0]
        assert rec["value_now"] == {"angle_deg": 15.0}
        assert "rotation_axis" not in rec

    def test_an_unreadable_heading_is_omitted_not_reported_as_an_axis(self, kin_design):
        # the member is there and RAISES: no key - and the rest of the row still reads, so one
        # unreadable heading costs its own key and not the call it sits in.
        kin_design(joints=[_joint("Hinge", _REVOLUTE, "A:1", "B:1", motion_cls=_UnreadableAxisMotion)])
        rec = _payload(ap.handler())["joints"][0]
        assert "rotation_axis" not in rec
        assert rec["name"] == "Hinge" and rec["type"] == "revolute"

    def test_a_joint_whose_MOTION_READ_RAISES_still_publishes_a_row_with_no_heading(self,
                                                                                   kin_design):
        # the object every heading read goes through answers nothing at all. The row is still
        # emitted - name and occurrences intact - and carries no direction, which holds only while
        # the read of jointMotion is itself guarded: unguarded, the traceback runs from the read
        # straight out of handler(), so the call returns no payload at all rather than one
        # headingless row.
        kin_design(joints=[_joint("Hinge", _REVOLUTE, "A:1", "B:1",
                                  joint_cls=_MotionUnreadableJoint)])
        rec = _payload(ap.handler())["joints"][0]
        assert rec["name"] == "Hinge" and rec["occurrence_one"] == "A:1"
        assert "rotation_axis" not in rec and "slide_direction" not in rec

    def test_an_UNKNOWN_joint_kind_is_published_with_no_heading_at_all(self, kin_design):
        # the motion carries both vectors but its jointType maps to no kind, so which DOF it has is
        # unknown - the unknown kind takes the withholding branch, never the permissive one.
        kin_design(joints=[_joint("Odd", _UNMAPPED, "A:1", "B:1", motion_values=dict(self._BOTH))])
        rec = _payload(ap.handler())["joints"][0]
        assert rec["type"] == "?"
        assert "rotation_axis" not in rec and "slide_direction" not in rec

    def test_the_headings_are_dimensionless_and_do_NOT_follow_the_units(self, kin_design):
        # a direction is not a length: the same row's frame origin scales with 'units' and these
        # must not, or a caller reading them in inches steers by a vector 25.4x off.
        kin_design(joints=[_joint("Travel", _SLIDER, "A:1", "B:1",
                                     motion_values={"slideDirectionVector": FakeVector3D(0.0, 0.6, 0.8)})])
        for units in ("mm", "cm", "in"):
            rec = _payload(ap.handler(units=units))["joints"][0]
            assert rec["slide_direction"] == [0.0, 0.6, 0.8]

    def test_an_as_built_joint_reports_its_heading_too(self, kin_design):
        # an AsBuiltJoint carries the same jointMotion, and it is the class the as-built rig builds
        # its hinges from - a read wired only to `Joint` would leave those rows headingless.
        kin_design(asbuilt=[FakeAsBuiltJoint("Spin", _REVOLUTE, "A:1", "B:1",
                                             motion_values=dict(self._BOTH))])
        assert _payload(ap.handler())["joints"][0]["rotation_axis"] == [0.0, 1.0, 0.0]

    def test_the_heading_sits_beside_the_frame_and_the_value_without_replacing_either(
            self, kin_design):
        # the three are separate reads of one joint: the motion's axis, the driven value, and the
        # frame whose z_axis is the OFFSET direction - here deliberately a DIFFERENT vector.
        kin_design(joints=[_joint("Hinge", _REVOLUTE, _frame_occ("A:1"), "B:1",
                                     motion_values={"rotationValue": math.radians(90),
                                                    "rotationAxisVector": FakeVector3D(0.0, 1.0, 0.0)},
                                     frame_one=_JointFrame(origin=(1.0, 0.0, 0.0)))])
        rec = _payload(ap.handler(units="cm"))["joints"][0]
        assert rec["rotation_axis"] == [0.0, 1.0, 0.0]
        assert rec["value_now"] == {"angle_deg": 90.0}
        assert rec["frame"]["z_axis"] == [0.0, 0.0, 1.0]
        assert rec["frame"]["origin"] == [1.0, 0.0, 0.0]

    def test_the_note_names_both_headings_and_what_an_ABSENT_key_means(self, kin_design):
        kin_design(joints=[_joint("Hinge", _REVOLUTE, "A:1", "B:1", motion_values=dict(self._BOTH))])
        note = _payload(ap.handler())["note"]
        assert "rotation_axis (revolute/cylindrical)" in note
        # A cylindrical joint SLIDES, so a reader told the key belongs to that kind reads its
        # absence as a failed read; the measured motion carries no such member at all. The scope
        # rides in the heading list itself, where the kinds carrying each heading are named.
        assert ("slide_direction (SLIDER ONLY - a cylindrical motion exposes no slide direction)"
                in note)
        # the clause that gives a MISSING key its meaning. Drop it and the two vectors still cross
        # the wire, with nothing saying whether a row without one was asked and answered nothing.
        assert "an absent key is a read that answered nothing" in note

    def test_the_note_states_the_SPACE_the_headings_are_published_in(self, kin_design):
        # the same row publishes frame in WORLD coordinates. Two bare direction keys beside it
        # invite a comparison against frame.z_axis, and numbers in two frames never disagree out
        # loud - so the note states the space, scoped to the ONE key a read backs. The rest is
        # hedged by name: a heading the note called measured over an unmeasured case is the
        # silent-90-degrees failure the scoping exists to prevent.
        kin_design(joints=[_joint("Hinge", _REVOLUTE, "A:1", "B:1", motion_values=dict(self._BOTH))])
        note = _payload(ap.handler())["note"]
        assert "SPACE: rotation_axis read WORLD on a top-level joint (measured)" in note
        assert ("slide_direction's space is UNMEASURED, as is either heading on a joint reached "
                "through a nested instance") in note
        # the claim and its scope, not the experiment behind it: the two rigs are cited at
        # _assembly_detail._motion_axes, where the code depends on them, and cross no wire.
        assert "turned 90 deg about Z" not in note

    def test_a_PLANAR_row_shows_its_DOF_and_the_note_scopes_the_unread_kinds(self, kin_design):
        # a planar motion is deliberately not read for a heading, and the row itself proves the DOF
        # are there (dof 3, three driven values). An unscoped "the read answered nothing" would
        # describe a read that was never attempted as one that failed.
        kin_design(joints=[_joint("Slide", _PLANAR, "A:1", "B:1",
                                     motion_values=dict(self._BOTH, rotationValue=0.0,
                                                        primarySlideValue=1.0,
                                                        secondarySlideValue=2.0))])
        out = _payload(ap.handler())
        rec = out["joints"][0]
        assert rec["type"] == "planar" and rec["dof"] == 3
        assert rec["value_now"] == {"angle_deg": 0.0, "slide_primary_mm": 10.0,
                                    "slide_secondary_mm": 20.0}
        assert "rotation_axis" not in rec and "slide_direction" not in rec
        assert ("a pin_slot / planar / ball row states none because none is read, not because a "
                "read failed") in out["note"]

    def test_no_heading_teaching_when_joints_are_not_emitted(self, kin_design):
        kin_design(occs=[_occ("A:1", "A")],
                   joints=[_joint("Hinge", _REVOLUTE, "A:1", "B:1", motion_values=dict(self._BOTH))])
        assert "rotation_axis" not in _payload(ap.handler(include_joints=False))["note"]


# ── frame: the joint's own frame in WORLD coordinates ────────────────────────────────────────────
#
# geometryOrOriginOne/Two carry .origin plus primaryAxisVector (the frame Z), secondaryAxisVector (X)
# and thirdAxisVector (Y). The frame Z is the axis a joint OFFSET drives along, which is the fact a
# caller otherwise pays a probe cycle to discover. The two halves sit in different frames - the
# origin is measured WORLD, the axes component-LOCAL - so only the axes are lifted, through the
# occurrence THAT SIDE of the joint is anchored to.

class TestJointFrame:
    def test_frame_reports_the_world_origin_and_the_LIFTED_axes_of_the_first_geometry(
            self, kin_design):
        # The component is turned 30 deg about Z, which is what separates the two possible answers:
        # its local X (1,0,0) stands at (0.866, 0.5, 0) in world.
        f1 = _JointFrame(origin=(6.3027, 0.0, 1.6), z=(0, 0, 1), x=(1, 0, 0), y=(0, 1, 0))
        kin_design(joints=[_joint("Grip", _RIGID, _frame_occ("A:1", transform2=_zrot(30.0)), "B:1",
                                     frame_one=f1)])
        frame = _payload(ap.handler(units="mm"))["joints"][0]["frame"]
        assert frame["origin"] == [63.027, 0.0, 16.0]     # 6.3027 cm -> 63.027 mm, already world
        assert frame["x_axis"] == [0.866, 0.5, 0.0]       # secondaryAxisVector, lifted
        assert frame["y_axis"] == [-0.5, 0.866, 0.0]      # thirdAxisVector, lifted
        assert frame["z_axis"] == [0.0, 0.0, 1.0]         # the rotation axis is unmoved

    def test_the_origin_is_NOT_lifted_with_the_axes(self, kin_design):
        # The origin already carries the placement; running it through the same matrix as the axes
        # would turn a real point into one on no part of the model - (10, 0, 0) cm would read
        # (8.66, 5.0, 0). The pairing is per-half, not per-frame.
        f1 = _JointFrame(origin=(10.0, 0.0, 0.0), z=(0, 0, 1), x=(1, 0, 0), y=(0, 1, 0))
        kin_design(joints=[_joint("Grip", _REVOLUTE, _frame_occ("A:1", transform2=_zrot(30.0)), "B:1",
                                     frame_one=f1)])
        frame = _payload(ap.handler(units="cm"))["joints"][0]["frame"]
        assert frame["origin"] == [10.0, 0.0, 0.0]
        assert frame["x_axis"] == [0.866, 0.5, 0.0]       # the axes DID move, so the matrix applied

    def test_frame_falls_back_to_the_second_geometry_when_the_first_is_null(self, kin_design):
        # an inferred joint reads null on geometryOrOriginOne; the frame is still readable off Two.
        f2 = _JointFrame(origin=(0.0, 4.0, 0.0), z=(0, 1, 0), x=(1, 0, 0), y=(0, 0, -1))
        kin_design(joints=[_joint("Inferred", _REVOLUTE, "A:1",
                                     _frame_occ("B:1", comp="B"), frame_one=None, frame_two=f2)])
        frame = _payload(ap.handler(units="mm"))["joints"][0]["frame"]
        assert frame["origin"] == [0.0, 40.0, 0.0]
        assert frame["z_axis"] == [0.0, 1.0, 0.0]

    def test_each_half_is_lifted_through_ITS_OWN_occurrence(self, kin_design):
        # Measured on one joint: side one's axes read Probe-local and side two's read Anchor-local,
        # and the two describe the SAME world direction only once each is lifted through its own
        # occurrence. Here both components are turned, by DIFFERENT angles, and the fallback half is
        # the one that answers - so pairing the geometry with occurrenceOne would publish
        # (0.866, 0.5, 0) instead of the 60-deg answer below.
        f2 = _JointFrame(origin=(0.0, 0.0, 0.0), z=(0, 0, 1), x=(1, 0, 0), y=(0, 1, 0))
        kin_design(joints=[_joint("Pin", _REVOLUTE,
                                     _frame_occ("A:1", comp="A", transform2=_zrot(30.0)),
                                     _frame_occ("B:1", comp="B", transform2=_zrot(60.0)),
                                     frame_one=None, frame_two=f2)])
        frame = _payload(ap.handler(units="mm"))["joints"][0]["frame"]
        assert frame["x_axis"] == [0.5, 0.866, 0.0]       # 60 deg - occurrenceTwo's placement
        assert frame["y_axis"] == [-0.866, 0.5, 0.0]

    def test_the_first_geometry_wins_when_both_are_present(self, kin_design):
        one = _JointFrame(origin=(1.0, 0.0, 0.0), z=(1, 0, 0))
        two = _JointFrame(origin=(0.0, 9.0, 0.0), z=(0, 0, 1))
        kin_design(joints=[_joint("Pin", _REVOLUTE, _frame_occ("A:1"), _frame_occ("B:1", comp="B"),
                                     frame_one=one, frame_two=two)])
        frame = _payload(ap.handler(units="mm"))["joints"][0]["frame"]
        assert frame["origin"] == [10.0, 0.0, 0.0] and frame["z_axis"] == [1.0, 0.0, 0.0]

    def test_no_frame_key_when_both_geometries_are_null(self, kin_design):
        kin_design(joints=[_joint("Bare", _REVOLUTE, "A:1", "B:1")])
        assert "frame" not in _payload(ap.handler())["joints"][0]

    def test_no_frame_key_when_the_geometry_answers_nothing(self, kin_design):
        # a frame of four nulls claims a frame that was never read.
        kin_design(joints=[_joint("Opaque", _REVOLUTE, _frame_occ("A:1"), _frame_occ("B:1", comp="B"),
                                     frame_one=_OpaqueFrame(), frame_two=_OpaqueFrame())])
        assert "frame" not in _payload(ap.handler())["joints"][0]

    def test_a_geometry_that_answers_nothing_does_not_shadow_the_second(self, kin_design):
        good = _JointFrame(origin=(0.0, 0.0, 5.0), z=(0, 0, 1))
        kin_design(joints=[_joint("Pin", _REVOLUTE, _frame_occ("A:1"), _frame_occ("B:1", comp="B"),
                                     frame_one=_OpaqueFrame(), frame_two=good)])
        frame = _payload(ap.handler(units="mm"))["joints"][0]["frame"]
        assert frame["origin"] == [0.0, 0.0, 50.0] and frame["z_axis"] == [0.0, 0.0, 1.0]

    def test_frame_origin_follows_the_units_but_the_axes_do_not(self, kin_design):
        # a direction is dimensionless - scaling it by the unit factor would corrupt it.
        f1 = _JointFrame(origin=(2.54, 0.0, 0.0), z=(0, 0, 1))
        kin_design(joints=[_joint("Grip", _REVOLUTE, _frame_occ("A:1", transform2=_zrot(30.0)), "B:1",
                                     frame_one=f1)])
        frame = _payload(ap.handler(units="in"))["joints"][0]["frame"]
        assert frame["origin"] == [1.0, 0.0, 0.0]         # 2.54 cm -> 1 inch
        assert frame["z_axis"] == [0.0, 0.0, 1.0]         # dimensionless, and on the rotation axis
        assert frame["x_axis"] == [0.866, 0.5, 0.0]       # lifted, and still a unit vector

    def test_a_root_anchored_geometry_publishes_its_axes_unchanged(self, monkeypatch, kin_design):
        # A joint half anchored to root-owned geometry names no occurrence; the root component's
        # frame IS world, so there is nothing to apply.
        import adsk.core
        monkeypatch.setattr(adsk.core.Matrix3D, "create", staticmethod(lambda: _zrot(0.0, 0.0)),
                            raising=False)
        f1 = _JointFrame(origin=(1.0, 2.0, 3.0), z=(0, 0, 1), x=(1, 0, 0), y=(0, 1, 0))
        kin_design(joints=[_joint("Grip", _REVOLUTE, None, "B:1", frame_one=f1)])
        frame = _payload(ap.handler(units="cm"))["joints"][0]["frame"]
        assert frame["origin"] == [1.0, 2.0, 3.0]
        assert frame["x_axis"] == [1.0, 0.0, 0.0] and frame["y_axis"] == [0.0, 1.0, 0.0]

    def test_axes_are_dropped_when_no_placement_answers_for_that_half(self, kin_design):
        # The occurrence reads but its component does not, so no matrix answers. Publishing the
        # part-space axes anyway is the defect - the row keeps the origin it did read and states no
        # axes at all.
        occ = _slice_occ("A:1", None, transform2=_zrot(30.0))
        f1 = _JointFrame(origin=(1.0, 2.0, 3.0), z=(0, 0, 1), x=(1, 0, 0), y=(0, 1, 0))
        kin_design(joints=[_joint("Grip", _REVOLUTE, occ, "B:1", frame_one=f1)])
        frame = _payload(ap.handler(units="cm"))["joints"][0]["frame"]
        assert frame["origin"] == [1.0, 2.0, 3.0]
        assert frame["x_axis"] is None and frame["y_axis"] is None and frame["z_axis"] is None

    def _jo_joint(self, monkeypatch, kin_design, jo, occs=(), occ_one="A:1"):
        """A joint whose geometryOrOriginOne IS a JointOrigin, with the root frame answering as
        world (component_world_matrix takes the root leg through adsk.core.Matrix3D.create, and the
        shared mock's Mock is unreadable to an axis lift). `occ_one` is the occurrence the joint
        names on that half - a bare name where the half's instance is not the subject."""
        import adsk.core, adsk.fusion
        monkeypatch.setattr(adsk.fusion, "JointOrigin", _SliceJO, raising=False)
        monkeypatch.setattr(adsk.core.Matrix3D, "create", staticmethod(lambda: _zrot(0.0, 0.0)),
                            raising=False)
        design = kin_design(joints=[_joint("Grip", _RIGID, occ_one, "B:1", frame_one=jo)])
        if jo.parentComponent is None:
            jo.parentComponent = design.rootComponent
        design.rootComponent.allOccurrencesByComponent = lambda c, o=list(occs): o
        return _payload(ap.handler(units="mm"))["joints"][0]["frame"]

    # A joint's STORED JointOrigin reference reads assemblyContext None even when the joint was
    # built from a createForAssemblyContext proxy, and that context-stripped native answers the
    # FIRST placement's world point. The occurrence the joint names is the only thing that puts the
    # instance back, so the JO half resolves through it exactly as the JointGeometry half does.

    def _twice_placed_pin(self):
        """'Pin' placed at (5,0,0) and again turned 90 deg at (0,10,0), carrying one JO. The
        occurrences' component is a SEPARATE wrapper sharing the token, so the placement ladder's
        same-component test runs on the token and cannot pass on identity."""
        owner = _slice_comp("Pin")
        jo = _SliceJO("PinCenter", pos=(6.0, 1.0, 2.0), token="C", comp=owner,
                      instance_pos={"Pin:2": (-1.0, 11.0, 2.0)})
        owner.jointOrigins = _NamedCollection([jo])
        occs = [_slice_occ("Pin:1", _slice_comp("Pin"), transform2=_zrot(0.0, 0.0)),
                _slice_occ("Pin:2", _slice_comp("Pin"), transform2=_zrot(90.0, 0.0))]
        return jo, occs

    def test_a_joint_origin_half_reports_the_INSTANCE_THE_JOINT_NAMES(self, monkeypatch,
                                                                       kin_design):
        # The joint is on Pin:2, so its frame is Pin:2's: origin (-1, 11, 2) cm and the JO's part
        # space X (1,0,0) standing at (0,1,0). Reading the stored reference as it comes states
        # Pin:1's (6, 1, 2) instead - a point 12.2 cm from the joint - beside three null axes,
        # because two placements answer the owning component and neither is picked.
        jo, occs = self._twice_placed_pin()
        frame = self._jo_joint(monkeypatch, kin_design, jo, occs=occs, occ_one=occs[1])
        assert frame["origin"] == [-10.0, 110.0, 20.0]      # cm -> mm
        assert frame["x_axis"] == [0.0, 1.0, 0.0]
        assert frame["y_axis"] == [-1.0, 0.0, 0.0]

    def test_a_joint_origin_half_with_no_instance_publishes_NEITHER_origin_nor_axes(
            self, monkeypatch, kin_design):
        # The same twice-placed JO on a half that names no occurrence: nothing says which instance,
        # and a JO's position is an instance read. Publishing it beside null axes would state one
        # instance's point under a frame the row declines to describe, so both go together.
        jo, occs = self._twice_placed_pin()
        import adsk.core, adsk.fusion
        monkeypatch.setattr(adsk.fusion, "JointOrigin", _SliceJO, raising=False)
        monkeypatch.setattr(adsk.core.Matrix3D, "create", staticmethod(lambda: _zrot(0.0, 0.0)),
                            raising=False)
        design = kin_design(joints=[_joint("Grip", _RIGID, None, None, frame_one=jo)])
        design.rootComponent.allOccurrencesByComponent = lambda c, o=list(occs): o
        assert "frame" not in _payload(ap.handler(units="mm"))["joints"][0]

    def test_a_joint_origin_half_whose_proxy_REFUSES_falls_through_to_the_other_half(
            self, monkeypatch, kin_design):
        # createForAssemblyContext answering nothing leaves the instance unestablished, so that half
        # states nothing at all and the second half - a readable JointGeometry - answers instead.
        jo, occs = self._twice_placed_pin()
        jo.createForAssemblyContext = lambda occ: None
        import adsk.core, adsk.fusion
        monkeypatch.setattr(adsk.fusion, "JointOrigin", _SliceJO, raising=False)
        monkeypatch.setattr(adsk.core.Matrix3D, "create", staticmethod(lambda: _zrot(0.0, 0.0)),
                            raising=False)
        good = _JointFrame(origin=(0.0, 0.0, 5.0), z=(0, 0, 1), x=(1, 0, 0), y=(0, 1, 0))
        design = kin_design(joints=[_joint("Grip", _REVOLUTE, occs[1], _frame_occ("Slab:1", comp="Slab"),
                                              frame_one=jo, frame_two=good)])
        design.rootComponent.allOccurrencesByComponent = lambda c, o=list(occs): o
        frame = _payload(ap.handler(units="mm"))["joints"][0]["frame"]
        assert frame["origin"] == [0.0, 0.0, 50.0] and frame["x_axis"] == [1.0, 0.0, 0.0]

    def test_a_joint_origin_reference_reports_its_offset_world_position(self, monkeypatch,
                                                                        kin_design):
        # a JointOrigin exposes the three axis vectors but no origin of its own: its position is the
        # base anchor PLUS offsetX/Y/Z along the frame axes. A plain .origin read reports null here.
        jo = _SliceJO("Stock_Center", pos=(0.0, 0.0, 0.0), offsets=(0.0, 0.0, 4.5), token="T")
        frame = self._jo_joint(monkeypatch, kin_design, jo)
        assert frame["origin"] == [0.0, 0.0, 45.0]      # base (0,0,0) + offsetZ 4.5 cm along +Z
        assert frame["z_axis"] == [0.0, 0.0, 1.0]

    def test_a_joint_origin_on_a_ROTATED_component_reports_WORLD_axes(self, monkeypatch,
                                                                      kin_design):
        # The same JointOrigin reaches the joint_origins slice, which publishes its axes in world;
        # reading them raw here would describe one frame two ways inside a single payload. The
        # component's 30-deg turn is what separates the two answers.
        sub = _slice_comp("Tower")
        jo = _SliceJO("Center", pos=(5.0, 0.0, 0.0), offsets=(2.0, 0.0, 0.0), token="C", comp=sub)
        occ = _slice_occ("Tower:1", sub, transform2=_zrot(30.0, 5.0))
        frame = self._jo_joint(monkeypatch, kin_design, jo, occs=[occ])
        assert frame["x_axis"] == [0.866, 0.5, 0.0]
        assert frame["y_axis"] == [-0.5, 0.866, 0.0]
        assert frame["origin"] == [67.32, 10.0, 0.0]   # the offset runs along the WORLD frame X

    def _as_built(self, kin_design, geometry, occ_one=None, occ_two="BlkB:1"):
        """One as-built joint, timeline-healthy, wired into a design. occ_one defaults to an
        UNTURNED instance, so a lift that ran through it publishes the unlifted axes and a lift
        through the geometry's own instance does not."""
        joint = FakeAsBuiltJoint("Spin", _REVOLUTE, occ_one or _frame_occ("BlkA:1"), occ_two,
                                 geometry=geometry, timeline=FakeTimelineObject("Spin"))
        return kin_design(asbuilt=[joint])

    def test_an_as_built_joint_publishes_the_frame_of_its_single_geometry(self, kin_design):
        # An AsBuiltJoint carries `geometry` and NEITHER geometryOrOriginOne nor Two, so the
        # two-halves walk published no frame key at all for one.
        occ_b = _slice_occ("BlkB:1", _slice_comp("BlkB"), transform2=_zrot(90.0, 0.0))
        self._as_built(kin_design, _AsBuiltGeometry(origin=(4.0, 2.0, 1.0), z=(1, 0, 0),
                                                    x=(0, 0, 1), y=(0, -1, 0), context=occ_b),
                       occ_two=occ_b)
        frame = _payload(ap.handler(units="cm"))["joints"][0]["frame"]
        assert frame["origin"] == [4.0, 2.0, 1.0]      # the stored reference is already world
        assert frame["z_axis"] == [0.0, 1.0, 0.0]      # local (1,0,0) through BlkB:1's 90 deg
        assert frame["x_axis"] == [0.0, 0.0, 1.0]
        assert frame["y_axis"] == [1.0, 0.0, 0.0]

    def test_the_axes_are_lifted_through_the_GEOMETRYS_instance_not_occurrenceOne(self, kin_design):
        # MEASURED: on a joint between BlkA:1 (identity) and BlkB:1 (turned 90 deg about Z), the
        # geometry's entityOne named BlkB:1 and lifting through it published (0,1,0) - the face's
        # own world normal, and the vector jointMotion.rotationAxisVector reports. occurrenceOne is
        # BlkA:1, whose identity placement publishes the unlifted (1,0,0), 90 deg wrong.
        occ_b = _slice_occ("BlkB:1", _slice_comp("BlkB"), transform2=_zrot(90.0, 0.0))
        occ_a = _slice_occ("BlkA:1", _slice_comp("BlkA"), transform2=_zrot(0.0, 0.0))
        self._as_built(kin_design, _AsBuiltGeometry(z=(1, 0, 0), context=occ_b),
                       occ_one=occ_a, occ_two=occ_b)
        assert _payload(ap.handler())["joints"][0]["frame"]["z_axis"] == [0.0, 1.0, 0.0]

    def test_an_uncontexted_entity_resolves_through_its_owners_SINGLE_placement(self, kin_design):
        # The entity names no instance, so the owning component's own placement answers - the same
        # ladder every other axis lift resolves through.
        owner = _slice_comp("BlkB")
        design = self._as_built(kin_design,
                                _AsBuiltGeometry(z=(1, 0, 0), context=None, owner=owner))
        design.rootComponent.allOccurrencesByComponent = (
            lambda c, o=[_slice_occ("BlkB:1", owner, transform2=_zrot(90.0, 0.0))]: o)
        assert _payload(ap.handler())["joints"][0]["frame"]["z_axis"] == [0.0, 1.0, 0.0]

    def test_axes_are_dropped_when_the_owning_component_is_placed_TWICE(self, kin_design):
        # Two placements each put the geometry's frame somewhere different and nothing names one,
        # so the row keeps the origin it read and states NO axes rather than an arbitrary instance's.
        owner = _slice_comp("BlkB")
        design = self._as_built(kin_design, _AsBuiltGeometry(origin=(4.0, 2.0, 1.0), z=(1, 0, 0),
                                                             context=None, owner=owner))
        design.rootComponent.allOccurrencesByComponent = (
            lambda c, o=[_slice_occ("BlkB:1", owner, transform2=_zrot(0.0, 0.0)),
                         _slice_occ("BlkB:2", owner, transform2=_zrot(90.0, 0.0))]: o)
        frame = _payload(ap.handler(units="cm"))["joints"][0]["frame"]
        assert frame["origin"] == [4.0, 2.0, 1.0]
        assert frame["z_axis"] is None and frame["x_axis"] is None and frame["y_axis"] is None

    def test_no_frame_key_when_the_as_built_joint_carries_no_geometry(self, kin_design):
        self._as_built(kin_design, None)
        assert "frame" not in _payload(ap.handler())["joints"][0]

    def test_a_regular_joint_still_reads_its_two_halves(self, kin_design):
        # The as-built branch is taken on the presence of `geometry`, which a Joint does not carry -
        # a branch that swallowed regular joints would drop every frame in the payload.
        kin_design(joints=[_joint("Pin", _REVOLUTE, _frame_occ("A:1", transform2=_zrot(30.0)), "B:1",
                                     frame_one=_JointFrame(origin=(1.0, 0.0, 0.0)))])
        frame = _payload(ap.handler(units="cm"))["joints"][0]["frame"]
        assert frame["origin"] == [1.0, 0.0, 0.0] and frame["x_axis"] == [0.866, 0.5, 0.0]

    def test_the_note_teaches_that_the_frame_z_is_the_offset_axis(self, kin_design):
        kin_design(joints=[_joint("Grip", _REVOLUTE, "A:1", "B:1", frame_one=_JointFrame())])
        note = _payload(ap.handler())["note"]
        assert "z_axis is the direction a joint OFFSET drives along" in note
        assert "value_now" in note

    def test_no_joint_teaching_when_joints_are_not_emitted(self, kin_design):
        kin_design(occs=[_occ("A:1", "A")],
                   joints=[_joint("Grip", _REVOLUTE, "A:1", "B:1", frame_one=_JointFrame())])
        assert "OFFSET drives along" not in _payload(ap.handler(include_joints=False))["note"]


# ── include=['all_occurrences']: the NESTED occurrences, each with its full path ─────────────────
#
# The 'occurrences' array walks root.occurrences - top-level only - so a part inside a sub-assembly
# appears nowhere in it and its position is unreadable. This slice repeats the same record over
# root.allOccurrences, the only walk that reaches nested instances and reports true full paths.

class TestAllOccurrencesSlice:
    def test_default_omits_the_slice_and_advertises_it(self, kin_design):
        kin_design(occs=[_occ("Tower:1", "Tower")])
        out = _payload(ap.handler())
        assert "all_occurrences" not in out and "all_occurrence_count" not in out
        assert "include=['all_occurrences']" in out["note"]

    def test_a_nested_occurrence_is_listed_with_its_full_path_and_position(self, kin_design):
        # the drift case: the child moved inside its parent, and the top-level rows cannot show it.
        top = _occ("Tower:1", "Tower")
        nested = _occ("Bolt:1", "Bolt", origin=(1.0, 2.0, 3.0),
                         full_path="Tower:1+Bolt:1")
        kin_design(occs=[top], all_occs=[top, nested])
        out = _payload(ap.handler(include=["all_occurrences", "poses"], units="mm"))
        rows = {r["full_path"]: r for r in out["all_occurrences"]}
        assert set(rows) == {"Tower:1", "Tower:1+Bolt:1"}
        assert rows["Tower:1+Bolt:1"]["origin"] == [10.0, 20.0, 30.0]
        assert rows["Tower:1+Bolt:1"]["component"] == "Bolt"
        # the top-level array and its count stay top-level - the slice ADDS a view, never edits one
        assert out["occurrence_count"] == 1 and len(out["occurrences"]) == 1

    def test_the_slice_row_is_the_same_record_as_a_top_level_row(self, kin_design):
        occ = _occ("Tower:1", "Tower", origin=(1.0, 0.0, 0.0), body_bbox=((0, 0, 0), (2, 1, 1)),
                   grounded=True, ground_to_parent=True, body_count=3, rotation_deg=0.0)
        kin_design(occs=[occ], joints=[_joint("J1", _REVOLUTE, "Tower:1", None)])
        out = _payload(ap.handler(include=["all_occurrences", "poses"], units="mm"))
        top, nested = out["occurrences"][0], out["all_occurrences"][0]
        assert set(nested) - set(top) == {"full_path"}       # the ONE key the slice adds
        assert {k: v for k, v in nested.items() if k != "full_path"} == top
        assert nested["grounded"] is True and nested["ground_to_parent"] is True
        assert nested["body_count"] == 3 and nested["joints"] == ["J1"]
        assert nested["bbox_size"] == [20.0, 10.0, 10.0] and nested["z_axis"] == [0.0, 0.0, 1.0]

    def test_poses_reaches_the_nested_rows_too(self, kin_design):
        # the two arrays are built by two calls: a poses flag wired into only one of them leaves
        # the nested rows - the ones a caller opens this slice FOR - poseless.
        top = _occ("Tower:1", "Tower")
        nested = _occ("Bolt:1", "Bolt", origin=(1.0, 2.0, 3.0), full_path="Tower:1+Bolt:1")
        kin_design(occs=[top], all_occs=[top, nested])
        rows = {r["full_path"]: r for r in
                _payload(ap.handler(include=["all_occurrences"], units="mm"))["all_occurrences"]}
        assert "origin" not in rows["Tower:1+Bolt:1"]
        rows = {r["full_path"]: r for r in
                _payload(ap.handler(include=["all_occurrences", "poses"],
                                    units="mm"))["all_occurrences"]}
        assert rows["Tower:1+Bolt:1"]["origin"] == [10.0, 20.0, 30.0]

    def test_the_slice_walks_all_occurrences_not_the_top_level_collection(self, kin_design):
        # root.occurrences holds ONE occurrence while the design holds three - a slice built on the
        # top-level collection would report the two nested ones as not existing.
        top = _occ("Tower:1", "Tower")
        kids = [_occ(f"Bolt:{i}", "Bolt", full_path=f"Tower:1+Bolt:{i}") for i in (1, 2)]
        kin_design(occs=[top], all_occs=[top] + kids)
        out = _payload(ap.handler(include=["all_occurrences"]))
        assert out["all_occurrence_count"] == 3 and len(out["all_occurrences"]) == 3
        assert out["all_occurrences_truncated"] is False

    def test_two_nested_instances_sharing_a_leaf_name_are_told_apart_by_path(self, kin_design):
        # 'Bolt:1' under two different parents is the same leaf name twice; only the path separates
        # them, so a name-keyed row would collapse the pair.
        a = _occ("Bolt:1", "Bolt", full_path="Left:1+Bolt:1")
        b = _occ("Bolt:1", "Bolt", full_path="Right:1+Bolt:1")
        kin_design(occs=[], all_occs=[a, b])
        rows = _payload(ap.handler(include=["all_occurrences"]))["all_occurrences"]
        assert sorted(r["full_path"] for r in rows) == ["Left:1+Bolt:1", "Right:1+Bolt:1"]

    def test_the_cap_truncates_the_list_and_flags_it(self, kin_design):
        kids = [_occ(f"P{i}:1", "P", full_path=f"Tower:1+P{i}:1") for i in range(9)]
        kin_design(occs=[], all_occs=kids)
        out = _payload(ap.handler(include=["all_occurrences"], max_all_occurrences=4))
        assert len(out["all_occurrences"]) == 4
        assert out["all_occurrence_count"] == 9          # the truth, uncapped
        assert out["all_occurrences_truncated"] is True
        assert "max_all_occurrences" in out["note"]

    def test_an_uncapped_list_is_not_flagged(self, kin_design):
        kin_design(occs=[], all_occs=[_occ("A:1", "A")])
        out = _payload(ap.handler(include=["all_occurrences"]))
        assert out["all_occurrences_truncated"] is False and "max_all_occurrences" not in out["note"]

    def test_an_empty_design_reports_an_empty_list(self, kin_design):
        kin_design()
        out = _payload(ap.handler(include=["all_occurrences"]))
        assert out["all_occurrences"] == [] and out["all_occurrence_count"] == 0

    def test_include_joints_false_drops_the_joints_key_from_the_slice_rows(self, kin_design):
        kin_design(occs=[_occ("A:1", "A")], joints=[_joint("J", _REVOLUTE, "A:1", None)])
        out = _payload(ap.handler(include=["all_occurrences"], include_joints=False))
        assert "joints" not in out["all_occurrences"][0]


class TestUnresolvedReferences:
    """The measured defect: on a 55-occurrence assembly whose root.allOccurrences RAISED, this tool
    published all_occurrence_count 0 with all_occurrences_truncated false and is_healthy true -
    under a note telling the agent to check is_healthy FIRST."""

    def _broken_design(self, kin_design, walk_raises=True):
        container = _occ("Op1 Workholding Container:1", "Op1",
                            children=[_occ("48205-125 (1):1", "48205-125",
                                              full_path="Op1 Workholding Container:1+48205-125 (1):1")],
                            broken_children=[_broken_occ("45740")])
        kin_design(occs=[container])
        if walk_raises:
            ap.app.activeProduct.rootComponent.allOccurrences = _raising_walk()
        return container

    def test_zero_unresolved_keeps_the_healthy_verdict_and_the_fast_walk(self, kin_design):
        kin_design(occs=[_occ("Tower:1", "Tower")])
        out = _payload(ap.handler())
        assert out["is_healthy"] is True
        assert out["unresolved_references"] == []
        assert "UNRESOLVED" not in out["note"]

    def test_one_unresolved_reference_makes_is_healthy_false_and_names_it(self, kin_design):
        self._broken_design(kin_design)
        out = _payload(ap.handler())
        assert out["is_healthy"] is False
        assert [u["name"] for u in out["unresolved_references"]] == ["45740"]
        assert out["unresolved_references"][0]["detail"] == UNAVAILABLE
        assert "45740" in out["note"] and "UNRESOLVED" in out["note"]
        # the note's own promise must now hold: unresolved_references is one of the named fields
        assert "unresolved_references" in out["note"]

    def test_a_raising_walk_publishes_the_real_count_not_an_empty_list(self, kin_design):
        self._broken_design(kin_design)
        out = _payload(ap.handler(include=["all_occurrences"]))
        assert out["occurrences_walk"] == "recursed"
        assert out["all_occurrence_count"] == 3        # container + its child + the unresolved one
        assert out["all_occurrences"] != []
        assert out["all_occurrences_truncated"] is False
        rows = {r["name"]: r for r in out["all_occurrences"]}
        assert rows["45740"]["unresolved"] is True
        assert rows["45740"]["parent_path"] == "Op1 Workholding Container:1"
        # the unresolved row claims NO placement or body count - every one of those reads raises
        assert "origin" not in rows["45740"] and "body_count" not in rows["45740"]

    def test_an_unreadable_census_publishes_null_and_never_truncated_false_over_a_zero(self, kin_design):
        # the cardinal shape: 0 + truncated:false is an active claim that nothing was lost.
        kin_design(occs=[])
        root = ap.app.activeProduct.rootComponent
        root.allOccurrences = _raising_walk()
        root.occurrences = _raising_walk()
        out = _payload(ap.handler(include=["all_occurrences"]))
        assert out["all_occurrence_count"] is None
        assert out["occurrences_walk"] == "unreadable"
        assert out["all_occurrences_truncated"] is False
        assert "UNKNOWN, not" in out["note"]

    def test_a_broken_TOP_LEVEL_occurrence_gets_a_flagged_row_not_a_blank_record(self, kin_design):
        kin_design(occs=[_broken_occ("45740"), _occ("Stock:1", "Stock")])
        rows = {r["name"]: r for r in _payload(ap.handler())["occurrences"]}
        assert rows["45740"]["unresolved"] is True
        assert "grounded" not in rows["45740"]        # never a swallowed False about an unreadable flag
        assert rows["Stock:1"]["grounded"] is False   # a real read still publishes a real value


class TestJointLimitsRead:
    def test_enabled_limits_are_published_in_the_record(self, kin_design):
        # Limits were WRITE-ONLY on this surface (measured) - the record now reads them back.
        j = _joint("Swing", _REVOLUTE, "A:1", "B:1")
        j.jointMotion.rotationLimits = _MotionLimits(minimum=math.radians(-60),
                                                     maximum=math.radians(60))
        kin_design(joints=[j])
        rec = _payload(ap.handler())["joints"][0]
        assert rec["rotation_limits_deg"] == {"min": -60.0, "max": 60.0}
        assert "slide_limits_mm" not in rec

    def test_disabled_limits_stay_off_the_wire(self, kin_design):
        j = _joint("Swing", _REVOLUTE, "A:1", "B:1")
        kin_design(joints=[j])
        rec = _payload(ap.handler())["joints"][0]
        assert "rotation_limits_deg" not in rec and "slide_limits_mm" not in rec

    def test_a_disabled_bound_carrying_a_value_stays_off_the_wire(self, kin_design):
        # The ENABLE FLAG is the gate, not the number beside it: a reader keying on the value would
        # pass every zero-valued disabled bound above and publish this -60 as a real limit.
        j = _joint("Swing", _REVOLUTE, "A:1", "B:1")
        j.jointMotion.rotationLimits = _MotionLimits(
            disabled_values={"minimum": math.radians(-60), "rest": math.radians(5)})
        kin_design(joints=[j])
        rec = _payload(ap.handler())["joints"][0]
        assert "rotation_limits_deg" not in rec


# ── the DEFAULT cap of every bounded array, driven through the payload ───────────────────────────
#
# Each max_* input's description states a default, and the handler applies one when the caller names
# none. Nothing forces those to be the same number: a description reading its constant while the
# signature carries a bare literal of its own leaves the two free to diverge, and every existing cap
# test passes the cap EXPLICITLY, so none of them exercises the default at all. These call the
# handler with no cap and assert the list length that comes back - the number the agent actually
# gets - against the number the wire promises.

@pytest.fixture
def jo_design(monkeypatch):
    """A JointOrigin-carrying design wired into assembly_get through a fixture, so every patch undoes
    itself - adsk.fusion.JointOrigin included, which is the class is_joint_origin casts against and
    is shared with every other test in the session."""
    def _build(design):
        import adsk.fusion
        monkeypatch.setattr(adsk.fusion, "JointOrigin", _SliceJO, raising=False)
        install(ap, design)
        return design
    return _build


def _promised_default(input_name):
    """The default one max_* input's WIRE SCHEMA carries, as an int."""
    prop = ap.tool.to_dict()["inputSchema"]["properties"][input_name]
    assert "default" in prop, f"{input_name} carries no schema default: {prop!r}"
    return int(prop["default"])


class TestDefaultCapsReachThePayload:
    def test_occurrences_default_caps_the_array_at_50(self, kin_design):
        kin_design(occs=[_occ(f"O{i}:1", f"C{i}") for i in range(51)])
        out = _payload(ap.handler())
        assert len(out["occurrences"]) == 50
        assert out["occurrence_count"] == 51 and out["occurrences_truncated"] is True

    def test_joints_default_caps_the_array_at_100(self, kin_design):
        kin_design(joints=[_joint(f"J{i}", _REVOLUTE, "A:1", "B:1") for i in range(101)])
        out = _payload(ap.handler())
        assert len(out["joints"]) == 100
        assert out["joint_count"] == 101 and out["joints_truncated"] is True

    def test_all_occurrences_default_caps_the_list_at_100(self, kin_design):
        kin_design(occs=[], all_occs=[_occ(f"P{i}:1", "P", full_path=f"T:1+P{i}:1")
                                      for i in range(101)])
        out = _payload(ap.handler(include=["all_occurrences"]))
        assert len(out["all_occurrences"]) == 100
        assert out["all_occurrence_count"] == 101 and out["all_occurrences_truncated"] is True

    def test_joint_origins_default_caps_the_list_at_50(self, jo_design):
        jos = [_SliceJO(f"JO{i}", token=f"T{i}") for i in range(51)]
        jo_design(_slice_design(_slice_root(jos=jos)))
        out = _payload(ap.handler(include=["joint_origins"]))
        assert len(out["joint_origins"]) == 50
        assert out["joint_origin_count"] == 51 and out["joint_origins_truncated"] is True

    def test_relations_default_caps_each_list_at_50(self, relations_design):
        relations_design(rigid=[_rigid(f"RG{i}") for i in range(51)])
        out = _payload(ap.handler(include=["relations"]))
        assert len(out["relations"]["rigid_groups"]) == 50
        assert out["relation_counts"]["rigid_groups"] == 51
        assert out["relations_truncated"] is True

    def test_contacts_default_caps_the_list_at_50(self, contacts_design):
        contacts_design(sets=[_contact_set(f"CS{i}") for i in range(51)])
        out = _payload(ap.handler(include=["contacts"]))
        assert len(out["contacts"]) == 50
        assert out["contact_count"] == 51 and out["contacts_truncated"] is True

    def test_every_cap_the_wire_promises_is_the_cap_the_payload_applies(
            self, kin_design, jo_design, relations_design, contacts_design):
        """The two legs read the same number. Each max_* input's description PROMISES a default and
        the handler APPLIES one; a signature carrying its own literal lets the promise and the
        applied cap drift apart while every explicit-cap test above stays green."""
        over = 200                                   # more rows than any default admits

        kin_design(occs=[_occ(f"O{i}:1", f"C{i}") for i in range(over)],
                   joints=[_joint(f"J{i}", _REVOLUTE, "A:1", "B:1") for i in range(over)],
                   all_occs=[_occ(f"P{i}:1", "P", full_path=f"T:1+P{i}:1")
                             for i in range(over)])
        out = _payload(ap.handler(include=["all_occurrences"]))
        assert len(out["occurrences"]) == _promised_default("max_occurrences")
        assert len(out["joints"]) == _promised_default("max_joints")
        assert len(out["all_occurrences"]) == _promised_default("max_all_occurrences")

        jo_design(_slice_design(_slice_root(jos=[_SliceJO(f"JO{i}", token=f"T{i}")
                                              for i in range(over)])))
        out = _payload(ap.handler(include=["joint_origins"]))
        assert len(out["joint_origins"]) == _promised_default("max_joint_origins")

        relations_design(rigid=[_rigid(f"RG{i}") for i in range(over)])
        out = _payload(ap.handler(include=["relations"]))
        assert len(out["relations"]["rigid_groups"]) == _promised_default("max_relations")

        contacts_design(sets=[_contact_set(f"CS{i}") for i in range(over)])
        out = _payload(ap.handler(include=["contacts"]))
        assert len(out["contacts"]) == _promised_default("max_contacts")
