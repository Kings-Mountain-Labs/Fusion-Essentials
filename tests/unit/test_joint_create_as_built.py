"""Unit tests for ``joint_create_as_built.py`` - jointing two occurrences where they already are.

Pinned here, no live Fusion: the geometry precondition (a non-rigid as-built joint RAISES with a
null geometry, so it is refused first), the motion read-back (a motion that silently comes back
rigid is a wrong result with a healthy feature), the post-creation rename, and the pending-move
refusal every joint CREATE shares.
"""

import pytest

from conftest import (MakeComp, _NamedCollection, install, load_tool, make_design, make_occurrence,
                      payload)

ja = load_tool("joint_create_as_built")
jn = load_tool("_joints")
jc = load_tool("assembly_capture_position")   # the other consumer of the shared pending read


# ── fakes ───────────────────────────────────────────────────────────────────
#
# AsBuiltJointInput has no SHAPES dump, so it stays bespoke here. The members the two joint fakes
# take a position on are the ones api_surface.py records for the live types.

class FakeSnapshots(_NamedCollection):
    """design.snapshots as this tool reads it: the shared captured walk plus the pending-position
    flag, whose read RAISES when `_blind` is set - the flag being unknown, not a False."""

    def __init__(self, pending=False, items=()):
        super().__init__(items)
        self._pending = pending
        self._blind = False

    @property
    def hasPendingSnapshot(self):
        if self._blind:
            raise RuntimeError("pending flag unreadable")
        return self._pending


class FakeAsBuiltInput:
    """An AsBuiltJointInput: its motion setters take the JointInput arity - the axis enum alone, with
    NO JointGeometry argument (an AsBuiltJointInput is not an AsBuiltJoint). A call carrying the extra
    geometry argument raises here, the way the overload does live."""

    def __init__(self):
        self.motion_calls = []
        self.jointMotion = None
        # a geometry IS readable off the input, so a wrong-arity call would have one to pass and
        # would fail for the arity, not for a missing attribute
        self.geometry = "GEOM_ON_INPUT"

    def _set(self, motion_class, args, arity):
        if len(args) != arity:
            raise TypeError(f"wrong number or type of arguments for {motion_class}")
        self.motion_calls.append((motion_class, args))
        self.jointMotion = type(motion_class, (), {})()
        return True

    def setAsRigidJointMotion(self, *a):
        return self._set("RigidJointMotion", a, 0)

    def setAsRevoluteJointMotion(self, *a):
        return self._set("RevoluteJointMotion", a, 1)

    def setAsSliderJointMotion(self, *a):
        return self._set("SliderJointMotion", a, 1)

    def setAsCylindricalJointMotion(self, *a):
        return self._set("CylindricalJointMotion", a, 1)

    def setAsPlanarJointMotion(self, *a):
        return self._set("PlanarJointMotion", a, 1)

    def setAsBallJointMotion(self, *a):
        return self._set("BallJointMotion", a, 2)

    def setAsPinSlotJointMotion(self, *a):
        return self._set("PinSlotJointMotion", a, 2)


class FakeAsBuiltJoints(_NamedCollection):
    """asBuiltJoints: the shared walk plus createInput(occ1, occ2, geometry) + add(input). The
    created joint reports the motion the input carries, unless motion_class forces another (the
    platform-lies case: '' models a joint whose motion cannot be read at all)."""

    def __init__(self, motion_class=None, geometry_readback="ANCHOR", add_returns=True,
                 name_sticks=True, add_raises=None):
        super().__init__()
        # add_raises is Fusion's own refusal text out of add(); the tool words its diagnosis from
        # that text, so a test can hand it the exact sentence the platform uses.
        self._add_raises = add_raises
        self.last = None
        self.last_input = None
        self.added = 0
        self._motion_class = motion_class
        self._geometry_readback = geometry_readback
        self._add_returns = add_returns
        # name_sticks=False models the SWIG accept-and-ignore: the assignment does not raise, the
        # joint keeps the name Fusion gave it, and only a read-back notices.
        self._name_sticks = name_sticks

    def createInput(self, o1, o2, geometry):
        self.last = (o1, o2, geometry)
        self.last_input = FakeAsBuiltInput()
        return self.last_input

    def add(self, inp):
        self.added += 1
        if self._add_raises:
            raise RuntimeError(self._add_raises)
        if not self._add_returns:
            return None
        cls = self._motion_class
        if cls is None:
            cls = type(inp.jointMotion).__name__ if inp.jointMotion is not None else "RigidJointMotion"
        motion = type(cls, (), {})() if cls else None
        attrs = {"name": "AsBuilt1", "jointMotion": motion,
                 "geometry": self._geometry_readback}
        if not self._name_sticks:
            attrs["name"] = property(lambda self: "AsBuilt1", lambda self, value: None)
        return type("J", (), attrs)()


# ── joint_create_as_built ───────────────────────────────────────────────────────────

@pytest.fixture
def as_built(monkeypatch):
    """Factory: install a design with two occurrences plus a configurable asBuiltJoints collection,
    and stub the shared '<occ>:<snap>'/handle resolver so 'geometry' yields an opaque JointGeometry
    (a real one needs a live session). snapshots=False is the design that exposes none at all.
    Returns the asBuiltJoints fake."""
    def _make(occ_paths=("A:1", "B:1"), pending=False, snapshots=True, **abj_kwargs):
        import adsk.fusion
        abj = FakeAsBuiltJoints(**abj_kwargs)
        occs = [make_occurrence(path=p, component=MakeComp(name=p.split("+")[-1].split(":")[0]))
                for p in occ_paths]
        comp = MakeComp(occurrences=occs)
        comp.asBuiltJoints = abj
        install(ja, make_design(comp=comp,
                                snapshots=FakeSnapshots(pending=pending) if snapshots else None))
        # real classes, so is_as_built_joint / is_joint_origin are genuine isinstance checks rather
        # than the degrade-to-False path a Mock type takes
        monkeypatch.setattr(adsk.fusion, "AsBuiltJoint", type("AsBuiltJoint", (), {}))
        monkeypatch.setattr(adsk.fusion, "JointOrigin", type("JointOrigin", (), {}))
        monkeypatch.setattr(ja, "_resolve_input",
                            lambda d, spec: (f"JG[{spec}]", f"snap:{spec}", None))
        return abj
    return _make


class TestAsBuiltJoint:
    def test_rigid_as_built_passes_null_geometry(self, as_built):
        abj = as_built()
        out = payload(ja.handler(occurrence_one="A:1", occurrence_two="B:1"))
        o1, o2, geom = abj.last
        assert o1.name == "A:1" and o2.name == "B:1"
        assert geom is None                      # rigid as-built = null geometry
        assert out["created"] is True and out["joint_type"] == "rigid"
        # rigid sets no motion at all: null geometry IS the rigid contract
        assert abj.last_input.motion_calls == []

    def test_missing_occurrence_errors(self, as_built):
        as_built(occ_paths=("A:1",))
        res = ja.handler(occurrence_one="A:1", occurrence_two="Ghost")
        assert res["isError"] is True and "Ghost" in res["message"]

    def test_requires_two_distinct(self, as_built):
        as_built(occ_paths=("A:1",))
        res = ja.handler(occurrence_one="A:1", occurrence_two="A:1")
        assert res["isError"] is True and "two distinct" in res["message"].lower()

    def test_same_local_name_different_path_is_allowed(self, as_built):
        # Two DISTINCT instances of the same component share a local .name ("Bolt:1") but
        # differ by fullPathName. The distinctness check must compare fullPathName, not .name - else it
        # false-positives and rejects a legitimate pair. Address each by its unambiguous fullPathName.
        abj = as_built(occ_paths=("SubA:1+Bolt:1", "SubB:1+Bolt:1"))
        assert {o.name for o in ja._common.design().rootComponent.allOccurrences} == {"Bolt:1"}
        out = payload(ja.handler(
            occurrence_one="SubA:1+Bolt:1", occurrence_two="SubB:1+Bolt:1"))
        assert out["created"] is True
        o1, o2, _ = abj.last
        assert o1.fullPathName == "SubA:1+Bolt:1" and o2.fullPathName == "SubB:1+Bolt:1"

    def test_payload_names_each_occurrence_by_full_path(self, as_built):
        # A NESTED child's .name is only the leaf ("Inner:1") - the caller addressed it as
        # "Outer:1+Inner:1", and only the full path names it unambiguously, so that is what the
        # payload publishes.
        as_built(occ_paths=("Base:1", "Outer:1+Inner:1"))
        out = payload(ja.handler(
            occurrence_one="Base:1", occurrence_two="Outer:1+Inner:1"))
        assert out["occurrence_one"] == "Base:1"
        assert out["occurrence_two"] == "Outer:1+Inner:1"


class TestAsBuiltMotion:
    """A non-rigid as-built joint: Fusion REFUSES one whose createInput got a null geometry
    ("Geometry should not be null if joint motion is not rigid"), so the anchor is a precondition
    here, and the motion the joint comes back with is read off the created joint."""

    def _revolute(self, **kw):
        args = {"occurrence_one": "A:1", "occurrence_two": "B:1", "geometry": "A:1:top",
                "joint_type": "revolute"}
        args.update(kw)
        return ja.handler(**args)

    def test_revolute_uses_the_one_arg_joint_input_setter(self, as_built):
        # The bite for the reuse claim: an AsBuiltJointInput takes the JointInput arity (the axis enum
        # ALONE). Routing it through the existing-as-built branch would call the two-arg
        # setter(axis, geometry), which raises in the fake - so this pins the dispatch, not just the type.
        abj = as_built()
        out = payload(self._revolute(axis="x"))
        assert abj.last_input.motion_calls == [("RevoluteJointMotion", (0,))]
        assert out["joint_type"] == "revolute" and out["axis"] == "x"

    def test_resolved_geometry_reaches_create_input(self, as_built):
        abj = as_built()
        payload(self._revolute())
        assert abj.last[2] == "JG[A:1:top]"      # createInput's third argument, not None
        assert payload(self._revolute())["geometry"] == "snap:A:1:top"

    def test_non_rigid_without_geometry_is_refused_before_any_call(self, as_built):
        abj = as_built()
        res = self._revolute(geometry="")
        assert res["isError"] is True
        assert "geometry" in res["message"] and "revolute" in res["message"]
        assert abj.last is None and abj.added == 0

    def test_rigid_with_geometry_is_refused(self, as_built):
        # A rigid as-built joint anchors nowhere, so a supplied geometry would be silently dropped.
        abj = as_built()
        res = ja.handler(occurrence_one="A:1", occurrence_two="B:1",
                                        geometry="A:1:top")
        assert res["isError"] is True
        assert "rigid" in res["message"] and "A:1:top" in res["message"]
        assert abj.added == 0

    def test_joint_origin_geometry_is_refused_naming_joint_create(self, as_built, monkeypatch):
        import adsk.fusion
        abj = as_built()
        monkeypatch.setattr(ja, "_resolve_input",
                            lambda d, spec: (adsk.fusion.JointOrigin(), "handle:joint_origin", None))
        res = self._revolute(geometry="H_JO")
        assert res["isError"] is True
        assert "Joint Origin" in res["message"] and "joint_create" in res["message"]
        assert abj.added == 0

    def test_unresolvable_geometry_error_is_surfaced(self, as_built, monkeypatch):
        abj = as_built()
        monkeypatch.setattr(ja, "_resolve_input",
                            lambda d, spec: (None, spec, "handle did not resolve - stale token"))
        res = self._revolute(geometry="H_DEAD")
        assert res["isError"] is True
        assert "geometry" in res["message"] and "stale token" in res["message"]
        assert abj.added == 0

    def test_motion_readback_mismatch_is_an_error(self, as_built):
        # the joint comes back RIGID when revolute was asked - a wrong result with a healthy feature
        as_built(motion_class="RigidJointMotion")
        res = self._revolute()
        assert res["isError"] is True
        assert "'rigid'" in res["message"] and "'revolute'" in res["message"]

    def test_unreadable_motion_is_an_error_for_a_motion_type(self, as_built):
        as_built(motion_class="")
        res = self._revolute()
        assert res["isError"] is True and "could not be read back" in res["message"]

    def test_rigid_survives_an_unreadable_motion_readback(self, as_built):
        # add() with a null geometry RAISES for any non-rigid motion, so reaching a created joint on
        # the rigid path is itself the proof - an unreadable motion class is not a failure there.
        as_built(motion_class="")
        out = payload(ja.handler(occurrence_one="A:1", occurrence_two="B:1"))
        assert out["created"] is True and out["joint_type"] == "rigid"

    def test_a_rejected_motion_setter_stops_before_add(self, as_built, monkeypatch):
        abj = as_built()
        monkeypatch.setattr(ja, "_apply_motion",
                            lambda *a, **k: (False, "Invalid parameter pitchDirection"))
        res = self._revolute()
        assert res["isError"] is True and "pitchDirection" in res["message"]
        assert abj.added == 0

    def test_null_anchor_geometry_on_a_motion_joint_is_reported(self, as_built):
        # AsBuiltJoint.geometry reads null only for a rigid joint, so a revolute one with no
        # geometry to read is a signal - reported, not swallowed.
        as_built(geometry_readback=None)
        out = payload(self._revolute())
        assert "anchor_warning" in out and "joint_drive" in out["anchor_warning"]

    def test_pin_slot_passes_two_distinct_directions(self, as_built):
        abj = as_built()
        out = payload(self._revolute(joint_type="pin_slot", axis="z", slide_axis="x"))
        assert abj.last_input.motion_calls == [("PinSlotJointMotion", (2, 0))]
        assert out["slide_axis"] == "x"

    def test_pin_slot_slide_axis_must_differ_from_the_rotation_axis(self, as_built):
        abj = as_built()
        res = self._revolute(joint_type="pin_slot", axis="z", slide_axis="z")
        assert res["isError"] is True and "must differ" in res["message"]
        assert abj.added == 0

    def test_unknown_joint_type_is_refused(self, as_built):
        as_built()
        res = self._revolute(joint_type="hinge")
        assert res["isError"] is True and "hinge" in res["message"]

    def test_unknown_axis_is_refused(self, as_built):
        as_built()
        res = self._revolute(axis="w")
        assert res["isError"] is True and "Unknown axis 'w'" in res["message"]

    def test_add_returning_nothing_is_an_error(self, as_built):
        as_built(add_returns=False)
        res = self._revolute()
        assert res["isError"] is True and "returned nothing" in res["message"]


class TestAsBuiltResultNote:
    """What the note TELLS the agent after a successful create must match what was actually set -
    the axis the setter used, and a next step that will not refuse the joint."""

    def _make(self, **kw):
        args = {"occurrence_one": "A:1", "occurrence_two": "B:1", "geometry": "A:1:top"}
        args.update(kw)
        return payload(ja.handler(**args))

    def test_ball_note_claims_no_frame_axis(self, as_built):
        # setAsBallJointMotion ignores 'axis' (pitch Z / yaw X is the only pair the API accepts), and
        # the payload publishes axis=None - so the note must not name the requested axis.
        abj = as_built()
        out = self._make(joint_type="ball", axis="y")
        assert abj.last_input.motion_calls == [("BallJointMotion", (2, 0))]   # Z pitch, X yaw
        assert out["axis"] is None
        assert "y axis" not in out["note"]
        assert "pitch Z / yaw X" in out["note"]

    def test_axis_using_types_state_the_dof_the_setter_gave_that_axis(self, as_built):
        as_built()
        assert "rotating about the frame z axis" in self._make(joint_type="revolute")["note"]
        as_built()
        assert "sliding along the frame x axis" in self._make(joint_type="slider", axis="x")["note"]
        as_built()
        # planar's axis is the plane NORMAL - the motion is in the plane, not along the axis
        note = self._make(joint_type="planar", axis="z")["note"]
        assert "sliding in the plane normal to the frame z axis" in note

    def test_pin_slot_note_names_both_directions(self, as_built):
        as_built()
        note = self._make(joint_type="pin_slot", axis="z", slide_axis="x")["note"]
        assert "rotating about the frame z axis" in note
        assert "sliding along the frame x axis" in note

    def test_joint_drive_is_offered_only_for_the_types_it_drives(self, as_built):
        # joint_drive REFUSES ball/planar/pin_slot ("only revolute, slider, and cylindrical joints
        # can be driven by value"), so pointing those at it would hand the agent a dead end.
        for jt in ("revolute", "slider", "cylindrical"):
            as_built()
            assert "Pose it with joint_drive." in self._make(joint_type=jt)["note"]
        for jt in ("planar", "ball", "pin_slot"):
            as_built()
            note = self._make(joint_type=jt)["note"]
            assert "assembly_move" in note, jt
            assert "Pose it with joint_drive." not in note, jt

    def test_anchor_warning_points_at_the_right_tool_for_a_ball_joint(self, as_built):
        as_built(geometry_readback=None)
        warn = self._make(joint_type="ball")["anchor_warning"]
        assert "assembly_move" in warn and "Pose it with joint_drive." not in warn

    def test_rigid_note_is_unchanged(self, as_built):
        as_built()
        out = payload(ja.handler(occurrence_one="A:1", occurrence_two="B:1"))
        assert out["note"] == "Occurrences rigidly joined where they already are."


class TestAsBuiltName:
    """AsBuiltJoints.createInput/add take no name, so a requested name is applied to the CREATED
    joint through AsBuiltJoint.name and confirmed by reading it back."""

    def test_name_is_applied_after_creation_and_published(self, as_built):
        as_built()
        out = payload(ja.handler(occurrence_one="A:1", occurrence_two="B:1",
                                                 name="Slider_R"))
        assert out["joint"] == "Slider_R"

    def test_no_name_leaves_the_joint_named_by_fusion(self, as_built):
        as_built()
        out = payload(ja.handler(occurrence_one="A:1", occurrence_two="B:1"))
        assert out["joint"] == "AsBuilt1"

    def test_blank_name_is_not_a_rename_attempt(self, as_built):
        # "   " is no name at all; treating it as one would rename the joint to whitespace
        as_built(name_sticks=False)
        out = payload(ja.handler(occurrence_one="A:1", occurrence_two="B:1",
                                                 name="   "))
        assert out["joint"] == "AsBuilt1"

    def test_a_name_that_does_not_take_is_refused_and_says_the_joint_exists(self, as_built):
        # the SWIG accept-and-ignore: nothing raises, so only the read-back catches it. Reporting
        # created:true with the requested name would publish a name the browser does not show.
        as_built(name_sticks=False)
        res = ja.handler(occurrence_one="A:1", occurrence_two="B:1", name="Slider_R")
        assert res["isError"] is True
        assert "Slider_R" in res["message"] and "AsBuilt1" in res["message"]
        assert "WAS created" in res["message"]
        assert "design_delete_feature" in res["message"]


class TestAsBuiltDescription:
    def test_description_states_the_no_parameter_fact_and_the_parametric_path(self):
        # fusion.AsBuiltJoint carries no offset/angle ModelParameter at all, so an as-built joint's
        # position can never be driven by an expression - the agent has to know that BEFORE it
        # builds the mechanism, not after joint_edit refuses.
        desc = ja.TOOL_DESCRIPTION
        assert "NO offset/angle ModelParameter" in desc
        assert "use joint_create when it must be parametric" in desc


class TestAsBuiltPendingMoveRefusal:
    """An as-built joint says "joint them where they are" - but its creation recomputes the assembly,
    and a recompute REVERTS an uncaptured occurrence position, so "where they are" becomes the
    reverted pose. The create refuses while the pending flag is set, naming the same remedy
    assembly_capture_position offers, and the flag comes from the ONE shared read."""

    def test_refuses_the_as_built_create_while_a_move_is_pending(self, as_built):
        abj = as_built(pending=True)
        res = ja.handler(occurrence_one="A:1", occurrence_two="B:1")
        assert res["isError"] is True
        assert "would silently revert" in res["message"]
        assert abj.added == 0                       # refused before asBuiltJoints.add

    def test_the_refusal_names_capture_and_discard(self, as_built):
        as_built(pending=True)
        msg = ja.handler(occurrence_one="A:1", occurrence_two="B:1")["message"]
        assert "assembly_capture_position(action='capture')" in msg
        assert "action='discard_pending'" in msg

    def test_creates_normally_with_nothing_pending(self, as_built):
        abj = as_built(pending=False)
        out = payload(ja.handler(occurrence_one="A:1", occurrence_two="B:1"))
        assert out["created"] is True and abj.added == 1

    def test_an_unreadable_pending_flag_does_not_refuse(self, as_built):
        # The flag RAISES - unknown, not pending. An unreadable flag is no evidence of a move, so it
        # must not block a create the way a real True does.
        abj = as_built(pending=False)
        ja._common.design().snapshots._blind = True
        out = payload(ja.handler(occurrence_one="A:1", occurrence_two="B:1"))
        assert out["created"] is True and abj.added == 1

    def test_a_design_whose_snapshots_read_declines_does_not_refuse(self, as_built):
        # A snapshots read that DECLINES leaves the flag UNREADABLE, which is not the answer
        # "nothing is pending" - and only a flag that proved True may refuse a create.
        abj = as_built(snapshots=False)
        assert jn.pending_position(ja._common.design()) is None
        out = payload(ja.handler(occurrence_one="A:1", occurrence_two="B:1"))
        assert out["created"] is True and abj.added == 1

    def test_capture_status_consumes_the_shared_read(self, as_built, monkeypatch):
        # The bite for the ONE-home claim: stub the shared read alone. An inlined
        # snaps.hasPendingSnapshot re-roll in the status handler would ignore this and answer False.
        as_built(pending=False)
        monkeypatch.setattr(jn, "pending_position", lambda design: True)
        out = payload(jc.handler(action="status"))
        assert out["has_pending"] is True

    def test_the_create_guard_consumes_the_same_shared_read(self, as_built, monkeypatch):
        as_built(pending=False)
        monkeypatch.setattr(jn, "pending_position", lambda design: True)
        res = ja.handler(occurrence_one="A:1", occurrence_two="B:1")
        assert res["isError"] is True and "would silently revert" in res["message"]


class TestPairAlreadyJointed:
    """Fusion refuses a second as-built joint on an already-jointed PAIR, so the refusal has to
    point at the two occurrences rather than at the anchor geometry the caller passed."""

    _OVER_CONSTRAINED = ("3 : A joint in system exists for the provided input. "
                         "System will be over constrained")

    def test_the_platform_refusal_names_the_pair(self, as_built):
        as_built(add_raises=self._OVER_CONSTRAINED)
        res = ja.handler(occurrence_one="A:1", occurrence_two="B:1")
        assert res["isError"] is True
        assert "'A:1' and 'B:1' are already jointed to each other" in res["message"]

    def test_another_add_failure_gets_no_pair_diagnosis(self, as_built):
        # The clause is gated on Fusion's OWN words: a different failure must not be handed a
        # cause nothing read.
        as_built(add_raises="3 : Geometry should not be null if joint motion is not rigid")
        res = ja.handler(occurrence_one="A:1", occurrence_two="B:1")
        assert res["isError"] is True and "already jointed" not in res["message"]
