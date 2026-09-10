"""Unit tests for ``_joints.py`` - the shared joint substrate: the by-name resolution every joint
tool edits/drives/links through (the list form and the resolve-one that refuses a name several
joints carry), the JointOrigin walk those leaf ops sit on (one JO reached through both root proxies
is ONE JO), the Joint-Origin leaf ops whose root test decides between "already in assembly
context" and "must be proxied into its occurrence", and the motion-link ratio codec that turns a
display-unit ratio into the native pair setMotionData takes.

Two readings of ONE joint or JO are modelled the way the API hands them over: each scope holds its
OWN wrapper object and the two share an entityToken. One object placed in both scopes is collapsed
by any key at all, so it pins no de-dup.
"""

import pytest

import adsk.fusion

from conftest import (FakeJoint, FakeMotionLink, FakeOccurrence, FakeTimelineObject, MakeComp,
                      MakeDesign, _NamedCollection, load_tool, make_design, make_occurrence)

jt = load_tool("_joints")


def _joint(name, token, comp_name):
    """A joint the way the live walk reads one: its own entityToken (the key that collapses the two
    root proxies) and the parentComponent whose name a refusal names."""
    return FakeJoint(name, entity_token=token, parent_component=MakeComp(name=comp_name))


def _comp(joints=(), asbuilt=(), origins=()):
    return MakeComp(name="Comp", joints=joints, as_built_joints=asbuilt, joint_origins=origins)


def _design(root, subs=()):
    # allComponents lives on the DESIGN, is a COUNTED collection, and CARRIES THE ROOT - the live
    # shape the walks read. A bare list models neither, and a collection without the root would let a
    # root-only walk pass here while under-reporting every joint on a live design.
    return make_design(comp=root, all_components=[root] + list(subs))


# ── find_joints_by_name: the list form (never grab the first) ────────────────

class TestFindJointsByName:
    def test_no_hit_is_empty(self):
        des = _design(_comp([_joint("Revolute1", "t1", "Root")]))
        assert jt.find_joints_by_name(des, "Ghost") == []

    def test_one_hit(self):
        j = _joint("Revolute1", "t1", "Root")
        assert jt.find_joints_by_name(_design(_comp([j])), "Revolute1") == [j]

    def test_every_hit_across_components_is_listed(self):
        a = _joint("Revolute1", "t1", "Arm")
        b = _joint("Revolute1", "t2", "Gripper")
        des = _design(_comp([a]), [_comp([b])])
        assert jt.find_joints_by_name(des, "Revolute1") == [a, b]

    def test_an_as_built_joint_counts_as_a_hit(self):
        # asBuiltJoints is a separate collection; a name shared across the two is still shared.
        a = _joint("Fix1", "t1", "Root")
        b = _joint("Fix1", "t2", "Arm")
        des = _design(_comp([a]), [_comp(asbuilt=[b])])
        assert jt.find_joints_by_name(des, "Fix1") == [a, b]

    def test_a_blank_name_matches_nothing(self):
        des = _design(_comp([_joint("Revolute1", "t1", "Root")]))
        assert jt.find_joints_by_name(des, "   ") == []

    def test_the_match_is_exact_not_a_substring(self):
        des = _design(_comp([_joint("Revolute10", "t1", "Root")]))
        assert jt.find_joints_by_name(des, "Revolute1") == []


# ── find_joint: one resolves, several refuse ────────────────────────────────

class TestFindJoint:
    def test_one_hit_resolves(self):
        j = _joint("Revolute1", "t1", "Arm")
        des = _design(_comp([j]), [_comp([_joint("Revolute2", "t2", "Gripper")])])
        assert jt.find_joint(des, "Revolute1") == (j, None)

    def test_no_hit_is_none_with_no_error(self):
        # A miss carries no error text: each caller words its own not-found message.
        des = _design(_comp([_joint("Revolute1", "t1", "Arm")]))
        assert jt.find_joint(des, "Ghost") == (None, None)

    def test_two_components_sharing_a_name_are_refused_naming_both(self):
        # The boundary: at 2 hits the resolver must refuse. Returning either one silently
        # drives/edits an arbitrary sub-assembly's joint.
        des = _design(_comp([_joint("Revolute1", "t1", "Arm")]),
                      [_comp([_joint("Revolute1", "t2", "Gripper")])])
        j, err = jt.find_joint(des, "Revolute1")
        assert j is None
        assert "2 joints" in err and "Arm" in err and "Gripper" in err and "Revolute1" in err

    def test_a_third_hit_is_counted_and_named(self):
        des = _design(_comp([_joint("R1", "t1", "A")]),
                      [_comp([_joint("R1", "t2", "B")]), _comp([_joint("R1", "t3", "C")])])
        _j, err = jt.find_joint(des, "R1")
        assert "3 joints" in err and "'R1' in C" in err

    def test_an_unreadable_owning_component_is_named_as_unreadable(self):
        class _Blind(FakeJoint):
            """A joint whose owning component will not read - the row a refusal still has to name."""
            @property
            def parentComponent(self):
                raise RuntimeError("no component")
        des = _design(_comp([_joint("R1", "t1", "Arm")]),
                      [_comp([_Blind("R1", entity_token="t2")])])
        _j, err = jt.find_joint(des, "R1")
        assert "(unreadable component)" in err and "Arm" in err

    def test_two_readings_of_one_joint_sharing_a_token_are_not_ambiguous(self):
        # The rig: two scopes the walk reaches, each handing back its OWN wrapper of ONE joint, the
        # two sharing an entityToken. Only that token collapses the pair - without the de-dup the
        # joint refuses as a pair of itself.
        native, proxy = _joint("Revolute1", "t1", "Root"), _joint("Revolute1", "t1", "Root")
        des = _design(_comp([native]), [_comp([proxy])])
        assert jt.find_joint(des, "Revolute1") == (native, None)


# ── all_joints: the identity key that runs when entityToken does not read ───

class _TokenlessJoint(FakeJoint):
    """A joint whose entityToken read RAISES - the state a SUPPRESSED joint degrades toward - so the
    walk keys it on (name, objectType, owning component) instead."""

    def __init__(self, name, comp_name, obj_type="adsk::fusion::Joint"):
        super().__init__(name, object_type=obj_type, parent_component=MakeComp(name=comp_name))

    @property
    def entityToken(self):
        raise RuntimeError("no token")


class TestAllJointsFallbackKey:
    """The key that carries identity where entityToken answers nothing. Every part of it is
    load-bearing: two DIFFERENT joints can agree on the parts a narrower key keeps, and collapsing
    them drops one joint out of the walk every rollup and resolver counts over."""

    def test_two_tokenless_joints_sharing_a_name_in_two_components_both_survive(self):
        # A joint name is only component-locally unique, so a key of name ALONE reads these two real
        # joints as one and hides the second from every consumer of the walk.
        arm = _TokenlessJoint("Revolute1", "Arm")
        grip = _TokenlessJoint("Revolute1", "Gripper")
        assert jt.all_joints(_design(_comp([arm]), [_comp([grip])])) == [arm, grip]

    def test_two_tokenless_joints_differing_only_in_objectType_both_survive(self):
        # joints and asBuiltJoints are separate collections on ONE component: same name, same owner,
        # different class. Dropping objectType from the key collapses that pair too.
        joint = _TokenlessJoint("Fix1", "Arm", "adsk::fusion::Joint")
        as_built = _TokenlessJoint("Fix1", "Arm", "adsk::fusion::AsBuiltJoint")
        assert jt.all_joints(_design(_comp([joint], asbuilt=[as_built]))) == [joint, as_built]


# ── motion_link_record: a link's own STATE, not just its partner's name ─────
# joint_drive claims a partner moved, and arms a crash guard, off this record. A link that reads
# SUPPRESSED or compute-failed transmits nothing; a state that does not READ is neither, so every
# field is a tri-state a consumer branches on with `is`.

def _Linked(name, links=()):
    """A joint the record reads: its name plus the motionLinks the platform hands over."""
    return FakeJoint(name, links=links)


def _Blind(name):
    """A joint whose motionLinks membership RAISES - 'could not be asked', which the record must not
    report as 'in no link'."""
    joint = FakeJoint(name)
    joint.motionLinks = _NamedCollection(raises="membership unreadable")
    return joint


def _Link(one, two, name="MotionLink1", suppressed=False,
          health=adsk.fusion.FeatureHealthStates.HealthyFeatureHealthState,
          value_one=1.0, value_two=2.0, reversed_=False, timeline=None, own_health=True):
    """A MotionLink: the two joints it couples, its own suppression and compute state, the two
    ModelParameter values whose ratio IS the coupling, and the reversed flag. `timeline` is the
    TimelineObject beside the link, the second source the record asks for a health state and for a
    suppression an entity's own flag does not carry; `own_health` False DELETES the link's own pair,
    the state where only that second source can answer."""
    link = FakeMotionLink(name=name, joint_one=one, joint_two=two, suppressed=suppressed,
                          health=health, value_one=value_one, value_two=value_two,
                          reversed_link=reversed_, timeline_object=timeline,
                          message=("" if health == adsk.fusion.FeatureHealthStates
                                   .HealthyFeatureHealthState else "linked joints conflict"))
    if not own_health:
        del link.healthState
        del link.errorOrWarningMessage
    return link


class TestMotionLinkRecord:
    def test_a_joint_in_no_link_reads_linked_false(self):
        rec = jt.motion_link_record(_Linked("Rev1"))
        assert rec["linked"] is False and rec["partner"] is None
        assert rec["suppressed"] is None and rec["broken"] is None

    def test_an_unreadable_membership_reads_linked_none(self):
        # the discriminator against the test above: 'no link' and 'could not be asked' are different
        # answers, and only one of them lets a caller reason from the absence of a link.
        assert jt.motion_link_record(_Blind("Rev1"))["linked"] is None

    def test_a_joint_with_no_readable_name_reads_linked_none(self):
        # without this joint's own name there is no telling which end of the link is the partner.
        assert jt.motion_link_record(_Linked("", []))["linked"] is None

    def test_a_healthy_link_reads_every_field(self):
        me = _Linked("Rack")
        partner = _Linked("Pinion")
        me.motionLinks = [_Link(me, partner, value_one=1.0, value_two=3.5)]
        assert jt.motion_link_record(me) == {
            "linked": True, "partner": "Pinion", "link": "MotionLink1", "suppressed": False,
            "broken": False, "value_self": 1.0, "value_partner": 3.5, "reversed": False}

    def test_a_suppressed_link_reads_suppressed_true(self):
        me, partner = _Linked("Rack"), _Linked("Pinion")
        me.motionLinks = [_Link(me, partner, suppressed=True)]
        rec = jt.motion_link_record(me)
        assert rec["suppressed"] is True and rec["broken"] is False

    def test_a_compute_failed_link_reads_broken_true(self):
        me, partner = _Linked("Rack"), _Linked("Pinion")
        me.motionLinks = [_Link(me, partner,
                                health=adsk.fusion.FeatureHealthStates.ErrorFeatureHealthState)]
        assert jt.motion_link_record(me)["broken"] is True

    def test_health_carried_only_by_the_timeline_item_is_still_read(self):
        # the measured shape for relation-like objects: the entity answers no state at all while the
        # TimelineObject beside it does. Asking the entity alone would report a failed link as fine.
        me, partner = _Linked("Rack"), _Linked("Pinion")
        tl = FakeTimelineObject(health=adsk.fusion.FeatureHealthStates.ErrorFeatureHealthState,
                                message="linked joints conflict")
        me.motionLinks = [_Link(me, partner, own_health=False, timeline=tl)]
        assert jt.motion_link_record(me)["broken"] is True

    def test_a_link_answering_nothing_at_either_source_reads_broken_none(self):
        me, partner = _Linked("Rack"), _Linked("Pinion")
        me.motionLinks = [_Link(me, partner, own_health=False)]
        rec = jt.motion_link_record(me)
        assert rec["broken"] is None and rec["linked"] is True

    def test_a_suppression_set_on_the_TIMELINE_item_reads_suppressed_true(self):
        # the pairing assembly_get makes for a Joint, live-verified there: the entity's own flag
        # keeps reading False while the TIMELINE item carries the suppression. Asking the link
        # alone reports a suppressed link as a working one, which is the reading that lets a
        # receipt claim it moved the partner.
        me, partner = _Linked("Rack"), _Linked("Pinion")
        tl = FakeTimelineObject(suppressed=True)
        me.motionLinks = [_Link(me, partner, suppressed=False, timeline=tl)]
        assert jt.motion_link_record(me)["suppressed"] is True

    def test_a_link_whose_only_suppression_flag_is_the_timeline_items_still_reads_true(self):
        # the second source answers alone, the way it does for the health state of an entity that
        # carries none of its own.
        me, partner = _Linked("Rack"), _Linked("Pinion")
        link = _Link(me, partner, timeline=FakeTimelineObject(suppressed=True))
        del link.isSuppressed
        me.motionLinks = [link]
        assert jt.motion_link_record(me)["suppressed"] is True

    def test_a_timeline_flag_reading_false_answers_for_an_unreadable_own_flag(self):
        # False is an ANSWER: one source reading it settles the state, so a link with a silent own
        # flag is reported unsuppressed rather than unknown.
        me, partner = _Linked("Rack"), _Linked("Pinion")
        link = _Link(me, partner, timeline=FakeTimelineObject(suppressed=False))
        del link.isSuppressed
        me.motionLinks = [link]
        assert jt.motion_link_record(me)["suppressed"] is False

    def test_an_unreadable_suppression_reads_none_not_false(self):
        # NEITHER source answers here - no own flag and no timelineObject at all. safe(read, False)
        # would publish "not suppressed" for a pair that never answered, which is the reading that
        # lets a dead link claim a coupling.
        me, partner = _Linked("Rack"), _Linked("Pinion")
        link = _Link(me, partner)
        del link.isSuppressed
        me.motionLinks = [link]
        assert jt.motion_link_record(me)["suppressed"] is None

    def test_the_values_follow_which_END_of_the_link_this_joint_is(self):
        # the driven joint is jointTWO here: value_self must be valueTwo. Reading valueOne for both
        # ends scales every coupling by its reciprocal.
        partner, me = _Linked("Pinion"), _Linked("Rack")
        me.motionLinks = [_Link(partner, me, value_one=3.5, value_two=1.0)]
        rec = jt.motion_link_record(me)
        assert rec["partner"] == "Pinion"
        assert rec["value_self"] == 1.0 and rec["value_partner"] == 3.5

    def test_a_same_joint_two_dof_link_names_no_partner(self):
        # jointTwo is null for a link between two DOF of ONE joint - there is no partner to report,
        # and the membership DID read, so the answer is False rather than unknown.
        me = _Linked("Cyl")
        me.motionLinks = [_Link(me, None)]
        rec = jt.motion_link_record(me)
        assert rec["linked"] is False and rec["partner"] is None

    def test_a_partnerless_link_does_not_hide_a_later_one(self):
        # the same-joint link is skipped, not returned - a joint can hold both.
        me, partner = _Linked("Cyl"), _Linked("Spindle")
        me.motionLinks = [_Link(me, None), None, _Link(me, partner, name="MotionLink2")]
        rec = jt.motion_link_record(me)
        assert rec["partner"] == "Spindle" and rec["link"] == "MotionLink2"

    def test_an_unnamed_link_still_reports_its_partner(self):
        me, partner = _Linked("Rack"), _Linked("Pinion")
        link = _Link(me, partner)
        del link.name
        me.motionLinks = [link]
        rec = jt.motion_link_record(me)
        assert rec["partner"] == "Pinion" and rec["link"] is None


class TestMotionLinkPartner:
    """The name projection over the record - what a caller that needs only the partner reads."""

    def test_it_hands_back_the_partner_name(self):
        me, partner = _Linked("Rack"), _Linked("Pinion")
        me.motionLinks = [_Link(me, partner)]
        assert jt.motion_link_partner(me) == "Pinion"

    def test_a_suppressed_link_still_names_its_partner(self):
        # the projection reports MEMBERSHIP; it deliberately cannot tell a dead link from a live one,
        # which is why the coupling claim reads the record instead.
        me, partner = _Linked("Rack"), _Linked("Pinion")
        me.motionLinks = [_Link(me, partner, suppressed=True)]
        assert jt.motion_link_partner(me) == "Pinion"

    def test_no_link_and_an_unreadable_membership_both_answer_none(self):
        assert jt.motion_link_partner(_Linked("Rev1")) is None
        assert jt.motion_link_partner(_Blind("Rev1")) is None


# ── link_ratio_values: the display-unit ratio -> native (rad/cm) pair codec ───────

JMT = adsk.fusion.JointMotionTypes
ROTATE_DOF = JMT.RevoluteJointRotateMotionType
SLIDE_DOF = JMT.SliderJointSlideMotionType
CYL_ROTATE_DOF = JMT.CylindricalJointRotateMotionType
CYL_SLIDE_DOF = JMT.CylindricalJointSlideMotionType

# The rack-and-pinion pair the conversion is measured on: 2.8647889757 deg of pinion per mm of rack
# is 0.5 rad per cm, the number Fusion actually couples on.
RIG_DISPLAY_RATIO = 2.8647889757
RIG_NATIVE_VALUE_TWO = 0.5


class TestDofMotionKind:
    def test_every_rotate_and_slide_dof_answers_its_kind(self):
        for dof in (ROTATE_DOF, CYL_ROTATE_DOF, JMT.PinSlotJointRotateMotionType,
                    JMT.PlanarJointRotateMotionType, JMT.BallJointPitchMotionType,
                    JMT.BallJointRollMotionType, JMT.BallJointYawMotionType):
            assert jt.dof_motion_kind(dof) == "rotation"
        for dof in (SLIDE_DOF, CYL_SLIDE_DOF, JMT.PinSlotJointSlideMotionType,
                    JMT.PlanarJointSlideOneMotionType, JMT.PlanarJointSlideTwoMotionType):
            assert jt.dof_motion_kind(dof) == "slide"

    def test_a_cylindrical_joints_two_dof_are_told_apart(self):
        # the one joint kind exposing both - a table keyed on the JOINT rather than the DOF would
        # convert a cylindrical slide as a rotation.
        assert jt.dof_motion_kind(CYL_ROTATE_DOF) == "rotation"
        assert jt.dof_motion_kind(CYL_SLIDE_DOF) == "slide"

    def test_an_unrecognised_or_absent_dof_is_no_kind(self):
        assert jt.dof_motion_kind(object()) is None
        assert jt.dof_motion_kind(None) is None


class TestLinkRatioValues:
    """The write-side codec: a ratio in DISPLAY units (deg / mm) becomes the native pair
    setMotionData takes (rad / cm). value_one is always 1, so value_two carries the coupling."""

    def test_a_rotation_pair_passes_the_ratio_through_untouched(self):
        # BACK-COMPAT: the two display factors cancel, so a rev/rev link sends exactly what the
        # caller asked for - byte-identical to the caller's own number.
        one, two, facts = jt.link_ratio_values(ROTATE_DOF, ROTATE_DOF, 2.0)
        assert (one, two) == (1.0, 2.0)
        assert facts["ratio_units"] == "deg of joint_two per deg of joint_one"
        assert facts["value_units"] == "value_one in rad, value_two in rad"

    def test_a_slide_pair_passes_the_ratio_through_untouched(self):
        one, two, facts = jt.link_ratio_values(SLIDE_DOF, SLIDE_DOF, 3.0)
        assert (one, two) == (1.0, 3.0)
        assert facts["ratio_units"] == "mm of joint_two per mm of joint_one"
        assert facts["value_units"] == "value_one in cm, value_two in cm"

    def test_a_slide_to_rotation_ratio_converts_to_the_measured_native_value(self):
        # THE RIG: 2.8647889757 deg of pinion per mm of rack IS 0.5 rad per cm. Sent raw, the same
        # number couples 5.7x too fast.
        one, two, facts = jt.link_ratio_values(SLIDE_DOF, ROTATE_DOF, RIG_DISPLAY_RATIO)
        assert one == 1.0
        assert two == pytest.approx(RIG_NATIVE_VALUE_TWO, abs=1e-9)
        assert two != pytest.approx(RIG_DISPLAY_RATIO, abs=1e-6)
        assert facts["ratio_units"] == "deg of joint_two per mm of joint_one"
        assert facts["value_units"] == "value_one in cm, value_two in rad"

    def test_a_rotation_to_slide_ratio_converts_the_other_way(self):
        # the reciprocal pair: 2 mm of travel per degree of rotation is 11.4591559026 cm per radian.
        one, two, facts = jt.link_ratio_values(ROTATE_DOF, SLIDE_DOF, 2.0)
        assert one == 1.0
        assert two == pytest.approx(11.4591559026, abs=1e-9)
        assert facts["ratio_units"] == "mm of joint_two per deg of joint_one"
        assert facts["value_units"] == "value_one in rad, value_two in cm"

    def test_the_sign_never_reaches_the_two_values(self):
        # the direction travels as isReversed, which the caller passes itself - a negative ratio
        # sends the same MAGNITUDES as its positive twin.
        pos = jt.link_ratio_values(SLIDE_DOF, ROTATE_DOF, RIG_DISPLAY_RATIO)
        neg = jt.link_ratio_values(SLIDE_DOF, ROTATE_DOF, -RIG_DISPLAY_RATIO)
        assert pos[0] == neg[0] and pos[1] == neg[1]
        assert " sign is carried by isReversed" in neg[2]["interpreted"]
        assert "isReversed" not in pos[2]["interpreted"]

    def test_the_interpreted_sentence_names_both_unit_systems_and_both_numbers(self):
        _one, _two, facts = jt.link_ratio_values(SLIDE_DOF, ROTATE_DOF, RIG_DISPLAY_RATIO)
        assert ("Ratio read in DISPLAY units: 2.8647889757 deg of joint_two per 1 mm of joint_one."
                in facts["interpreted"])
        assert ("Sent to setMotionData as 1 cm for joint_one and 0.500000000008 rad for joint_two"
                in facts["interpreted"])

    def test_the_sent_pair_is_named_per_joint_not_by_the_payloads_two_keys(self):
        # 'value_one'/'value_two' are the link's own parameters READ BACK in both writers' payloads,
        # so a sentence spelling the SENT numbers with those names offers one reader two meanings.
        for facts in (jt.link_ratio_values(SLIDE_DOF, ROTATE_DOF, RIG_DISPLAY_RATIO)[2],
                      jt.link_ratio_values(SLIDE_DOF, object(), RIG_DISPLAY_RATIO)[2]):
            assert "value_one=" not in facts["interpreted"]
            assert "value_two=" not in facts["interpreted"]
            assert "for joint_one" in facts["interpreted"]
            assert "for joint_two" in facts["interpreted"]

    def test_an_unclassified_dof_is_passed_through_unconverted_and_says_so(self):
        # no display unit is established for that side, so scaling would be a guess: the magnitude
        # goes out as given and both unit keys withhold rather than name a unit.
        one, two, facts = jt.link_ratio_values(SLIDE_DOF, object(), RIG_DISPLAY_RATIO)
        assert (one, two) == (1.0, RIG_DISPLAY_RATIO)
        assert facts["ratio_units"] is None and facts["value_units"] is None
        assert "NO unit conversion" in facts["interpreted"]

    def test_an_unclassified_first_dof_is_refused_conversion_too(self):
        one, two, facts = jt.link_ratio_values(None, ROTATE_DOF, 4.0)
        assert (one, two) == (1.0, 4.0) and facts["ratio_units"] is None

    def test_a_string_ratio_that_floats_is_accepted(self):
        # both writers float() their input before this; the codec is not a second parser, but a
        # numeric string must not silently become a wrong magnitude.
        assert jt.link_ratio_values(ROTATE_DOF, ROTATE_DOF, "-2")[1] == 2.0


class TestLinkRatioMismatch:
    """The read-back gate both motion-link writers apply after setMotionData: the platform answers a
    boolean, so the link's own valueOne/valueTwo are the only thing that says which coupling landed."""

    def test_a_pair_expressing_the_sent_value_is_no_mismatch(self):
        assert jt.link_ratio_mismatch(RIG_NATIVE_VALUE_TWO, 1.0, RIG_NATIVE_VALUE_TWO) is None

    def test_the_RATIO_is_compared_not_the_two_numbers_apart(self):
        # a platform storing the coupling scaled (2:1 for a sent 1:0.5) still expresses it -
        # comparing the numbers themselves would reject a correct link.
        assert jt.link_ratio_mismatch(0.5, 2.0, 1.0) is None

    def test_a_pair_holding_a_different_coupling_names_both_numbers(self):
        why = jt.link_ratio_mismatch(RIG_NATIVE_VALUE_TWO, 1.0, RIG_DISPLAY_RATIO)
        assert "1.0:2.8647889757" in why and "0.5" in why and "did not take" in why

    def test_a_pair_off_by_EXACTLY_the_tolerance_is_inside_the_band(self):
        # the > boundary, on numbers with no float slack: t and 2t are both exact doubles and
        # 2t - t is exactly t, so this pair sits ON the band. At the band it passes; past it it bites.
        t = jt.RATIO_READ_TOLERANCE
        assert (2 * t) - t == t
        assert jt.link_ratio_mismatch(t, 1.0, 2 * t) is None
        assert jt.link_ratio_mismatch(t, 1.0, 3 * t) is not None

    def test_the_band_scales_with_the_value_it_guards(self):
        # 1.5e-6 off a value of 2 is past the FLAT tolerance and inside the relative one: a large
        # converted value (11.4591559026 cm per rad) carries proportionally more float tail than 0.5.
        assert jt.link_ratio_mismatch(2.0, 1.0, 2.0 + 1.5e-6) is None
        assert jt.link_ratio_mismatch(1.0, 1.0, 1.0 + 1.5e-6) is not None

    def test_a_zero_first_value_is_no_coupling_at_all(self):
        assert "no coupling at all" in jt.link_ratio_mismatch(2.0, 0.0, 2.0)

    def test_a_pair_that_did_not_read_as_two_numbers_is_no_evidence(self):
        # None on either side, a non-number, and a BOOL (which int-tests true) are all "not asked",
        # never "the ratio failed" - each caller says what an unconfirmed pair means for its write.
        for pair in ((None, 2.0), (1.0, None), (1.0, object()), (True, 2.0)):
            assert jt.link_ratio_mismatch(2.0, *pair) is None


# ── DRIVES_ANY: the union both drive-side surfaces gate on ──────────────────

class TestDrivesAny:
    """The kinds carrying a drivable value AT ALL. joint_drive REFUSES anything outside this set and
    joint_create_as_built's pose pointer sends anything outside it to assembly_move, so the refusal
    and the pointer stand on ONE membership test."""

    def test_it_names_exactly_the_kinds_that_turn_or_slide(self):
        # spelled as either half alone it drops a kind that drives: the angle half has no slider,
        # the slide half no revolute.
        assert jt.DRIVES_ANY == {"revolute", "slider", "cylindrical"}

    def test_a_kind_with_no_single_drivable_value_is_outside_it(self):
        # rigid carries no value; ball/planar/pin_slot carry several, so none is driven by value.
        for kind in ("rigid", "ball", "planar", "pin_slot"):
            assert kind not in jt.DRIVES_ANY


# ── all_joint_origins: the ONE JO walk, de-duplicated across the two root proxies ─

class _JO:
    """A JointOrigin the way a walk reads one: a name, the entityToken that collapses the two root
    proxies, and the assembly-context factory the leaf ops proxy through."""

    def __init__(self, name="Frame", token=None):
        self.name = name
        self.entityToken = token
        self.context = None

    def createForAssemblyContext(self, occ):
        self.context = occ
        return ("proxy", occ)


class TestAllJointOrigins:
    def test_one_jo_reached_through_both_root_proxies_is_listed_once(self):
        # ONE JO read through two scopes arrives as two wrapper objects sharing nothing but their
        # entityToken - the rig hands the second wrapper back from another component in the
        # collection. A key those two wrappers do not share double-lists every such JO. The rig puts
        # the root first in allComponents, so its own wrapper is the one kept.
        native, proxy = _JO(token="JO1"), _JO(token="JO1")
        root = _comp(origins=[native])
        assert jt.all_joint_origins(_design(root, [_comp(origins=[proxy])])) == [(native, root)]

    def test_two_JOs_with_distinct_tokens_are_both_listed(self):
        # the de-dup collapses ONE JO's two wrappers, never two JOs: a name is only
        # component-locally unique, so two components each carrying a 'Frame' are two real frames -
        # dropping one hides it from the JO list and from every resolve over the walk.
        a, b = _JO("Frame", token="JO1"), _JO("Frame", token="JO2")
        root, sub = _comp(origins=[a]), _comp(origins=[b])
        assert jt.all_joint_origins(_design(root, [sub])) == [(a, root), (b, sub)]

    def test_two_JOs_whose_token_does_not_read_stay_apart(self):
        # entityToken is the key; where it reads None the identity fallback keeps two DIFFERENT JOs
        # apart, which a fallback key they happen to share would not.
        a, b = _JO("Frame"), _JO("Base")
        root, sub = _comp(origins=[a]), _comp(origins=[b])
        assert jt.all_joint_origins(_design(root, [sub])) == [(a, root), (b, sub)]

    def test_the_walk_asks_the_component_collection_and_never_a_prepended_root(self):
        # allComponents already CARRIES the root, so prepending design.rootComponent reads every
        # root JO twice, as two distinct wrappers - and the entityToken de-dup cannot collapse that
        # pair: a JO whose token does not read keys on id(), which two wrappers never share. Asking
        # only the collection is what keeps the row single.
        root, mirror = _comp(origins=[_JO()]), _comp(origins=[_JO()])
        des = MakeDesign(comp=root, all_components=[mirror])
        assert len(jt.all_joint_origins(des)) == 1

    def test_an_unreadable_component_collection_still_reaches_the_root_s_JOs(self):
        # the shared walk degrades to [root] when design.allComponents will not read, and this walk
        # inherits that: a design whose collection is silent still lists what the root carries,
        # instead of answering with nothing.
        jo = _JO("Frame", token="JO1")
        root = _comp(origins=[jo])

        class _BlindComponents(MakeDesign):
            """A design whose allComponents will not read at all."""
            @property
            def allComponents(self):
                raise RuntimeError("collection unavailable")
        assert jt.all_joint_origins(_BlindComponents(comp=root)) == [(jo, root)]

    def test_the_resolve_one_over_the_walk_sees_ONE_hit_through_both_root_proxies(self):
        # the consumer of this walk (the JointOriginRef kind) REFUSES at two hits, so a root JO the
        # walk listed twice is unaddressable by the name it carries.
        native, proxy = _JO("Center of Model", token="JO1"), _JO("Center of Model", token="JO1")
        root = _comp(origins=[native])
        des = _design(root, [_comp(origins=[proxy])])
        assert jt.find_joint_origins_by_name(des, "Center of Model") == [(native, root)]


# ── the root test behind the JO leaf ops (same_component, not a name compare) ─

def _sub(name="Arm", token="ARM"):
    """A sub-component: the NAME a wrong root test compares and the entityToken the right one does."""
    return MakeComp(name=name, entity_token=token)


def _root_comp(by_component=None, occurrences=None):
    """The ROOT component the leaf ops test against: its entityToken is what same_component compares,
    and the by-component lookup answers from `by_component` (keyed by component name) or by walking
    `occurrences` for the ones placing that component."""
    return MakeComp(name="Root", entity_token="ROOT", occurrences_by_component=by_component,
                    all_occurrences=occurrences)


class TestJointOriginRootTest:
    def test_a_second_root_wrapper_sharing_the_token_is_the_root(self):
        # Component wrappers are never identity-stable: design.rootComponent and the component a JO
        # reports are two objects sharing ONE entityToken. The JO is already in assembly context, so
        # it is returned as-is - no occurrence proxy, no refusal.
        des = MakeDesign(comp=_root_comp())
        jo = _JO()
        assert jt.jo_assembly_proxy(des, jo, _root_comp()) == (jo, None)
        assert jo.context is None
        assert jt.jo_reference_names(des, jo, _root_comp()) == ["Frame"]

    def test_a_sub_component_carrying_the_root_s_NAME_is_not_the_root(self):
        # A component named like the document's root component (renaming a part after the document
        # is the everyday way to get one) shares the NAME but not the entityToken. Comparing names
        # calls it the root and hands back the NATIVE JO, which Fusion answers with "Provided input
        # paths for joint are not valid"; comparing tokens proxies it into its occurrence.
        occ = make_occurrence("Root:1")
        des = MakeDesign(comp=_root_comp({"Root": [occ]}))
        twin = _sub(name="Root", token="TWIN")
        jo = _JO()
        assert jt.jo_assembly_proxy(des, jo, twin) == (("proxy", occ), None)
        assert jt.jo_reference_names(des, jo, twin) == ["Root:1:Frame"]

    def test_a_sub_component_jo_is_proxied_into_its_single_occurrence(self):
        # A DIFFERENT token means a different component: the native JO is refused by Fusion, so it
        # must be proxied into the occurrence that carries the frame.
        occ = make_occurrence("Arm:1")
        des = MakeDesign(comp=_root_comp({"Arm": [occ]}))
        jo = _JO()
        assert jt.jo_assembly_proxy(des, jo, _sub()) == (("proxy", occ), None)
        assert jt.jo_reference_names(des, jo, _sub()) == ["Arm:1:Frame"]

    def test_a_component_instanced_twice_is_refused_with_both_qualified_names(self):
        occs = [make_occurrence("Arm:1"), make_occurrence("Arm:2")]
        des = MakeDesign(comp=_root_comp({"Arm": occs}))
        obj, err = jt.jo_assembly_proxy(des, _JO(), _sub())
        assert obj is None and "instanced 2 times" in err
        assert jt.jo_reference_names(des, _JO(), _sub()) == ["Arm:1:Frame", "Arm:2:Frame"]


# ── component_world_matrix: which transform carries a component's own frame into world ─────────────

def _placed(path, comp, matrix, context=None):
    """An occurrence placing `comp`: transform2 (the COMPOSED component-to-world matrix) and the
    assemblyContext that makes a nested one reachable from its parent."""
    return make_occurrence(path, component=comp, transform2=matrix, assembly_context=context)


class TestComponentWorldMatrix:
    """The placement ladder ``jo_assembly_proxy`` walks, answering with a MATRIX. Its consumer
    (model_inspect's oriented box) hands those axes to getOrientedBoundingBox, which reads them in
    the same space as the geometry it is given - so an instance picked at random here measures a
    part against another instance's orientation."""

    def _des(self, root):
        return MakeDesign(comp=root)

    def test_the_root_components_frame_is_world(self, monkeypatch):
        # identity, not None: the root frame IS world, so a root-owned frame needs no lift and must
        # not be mistaken for an unplaceable one.
        import adsk.core
        monkeypatch.setattr(adsk.core.Matrix3D, "create", staticmethod(lambda: "IDENTITY"),
                            raising=False)
        assert jt.component_world_matrix(self._des(_root_comp()), _root_comp()) == "IDENTITY"

    def test_a_component_placed_once_answers_with_that_occurrences_transform(self):
        sub = _sub()
        root = _root_comp(occurrences=[_placed("Arm:1", sub, "M1")])
        assert jt.component_world_matrix(self._des(root), sub) == "M1"

    def test_a_component_placed_twice_answers_None_with_no_context(self):
        # two placements, two orientations, and nothing in hand says which one is being measured -
        # picking either would publish one instance's frame under the other's name.
        sub = _sub()
        root = _root_comp(occurrences=[_placed("Arm:1", sub, "M1"), _placed("Arm:2", sub, "M2")])
        assert jt.component_world_matrix(self._des(root), sub) is None

    def test_a_context_occurrence_picks_ITS_instance_out_of_several(self):
        sub = _sub()
        first, second = _placed("Arm:1", sub, "M1"), _placed("Arm:2", sub, "M2")
        root = _root_comp(occurrences=[first, second])
        assert jt.component_world_matrix(self._des(root), sub, second) == "M2"

    def test_the_context_walk_climbs_to_an_ANCESTOR_occurrence(self):
        # a nested proxy's assemblyContext is the INNERMOST occurrence; the component whose frame is
        # wanted may sit further up the path, and only the chain reaches it.
        arm, boss = _sub(), _sub(name="Boss", token="BOSS")
        outer = _placed("Arm:1", arm, "M1")
        inner = _placed("Arm:1+Boss:1", boss, "M2", context=outer)
        root = _root_comp()                           # the by-component lookup answers nothing
        assert jt.component_world_matrix(self._des(root), arm, inner) == "M1"

    def test_a_context_that_places_a_different_component_does_not_answer_for_it(self):
        # the chain is a lookup, not a fallback: an unrelated occurrence's transform would lift the
        # axes into a frame nothing in the request named.
        arm, other = _sub(), _sub(name="Plate", token="PLATE")
        root = _root_comp(occurrences=[_placed("Arm:1", arm, "M1"), _placed("Arm:2", arm, "M2")])
        assert jt.component_world_matrix(self._des(root), arm, _placed("Plate:1", other, "MX")) is None

    def test_a_component_not_placed_at_all_answers_None(self):
        assert jt.component_world_matrix(self._des(_root_comp()), _sub()) is None

    def test_a_missing_component_or_design_answers_None(self):
        assert jt.component_world_matrix(self._des(_root_comp()), None) is None
        assert jt.component_world_matrix(MakeDesign(comp=None), _sub()) is None

    def test_the_COMPOSED_transform2_is_the_matrix_read_not_the_LOCAL_one(self):
        # transform2 is the composed component-to-world matrix; transform is the LOCAL one and
        # composes no parent. They agree only while every ancestor is identity - exactly the case
        # a fixture defining just one of them cannot tell apart - and the nested-occurrence lift
        # this helper feeds is where they differ.
        sub = _sub()
        occ = make_occurrence("Arm:1", component=sub, transform2="COMPOSED", transform="LOCAL")
        assert jt.component_world_matrix(self._des(_root_comp(occurrences=[occ])), sub) == "COMPOSED"
        assert jt.component_world_matrix(self._des(_root_comp()), sub, occ) == "COMPOSED"

    def test_an_UNREADABLE_transform2_answers_None_and_does_not_fall_back_to_the_local_matrix(self):
        # The contract is "no single placement answers" -> None, and the caller reads None as
        # "refuse" or "make no judgement". transform composes no parent, so on a nested occurrence
        # it names a DIFFERENT frame: handing it back answers with a matrix this function's own
        # docstring calls wrong, in a slot a caller trusts as world. Both legs must hold it.
        sub = _sub()
        occ = make_occurrence("Arm:1", component=sub, transform="LOCAL",
                              raises_on={"transform2": "transform2 unavailable"})
        assert jt.component_world_matrix(self._des(_root_comp(occurrences=[occ])), sub) is None
        assert jt.component_world_matrix(self._des(_root_comp()), sub, occ) is None

    def test_a_context_chain_that_loops_still_terminates(self):
        # assemblyContext is read off a live proxy; a cycle there would hang the read that every
        # oriented measurement makes.
        class _SelfNesting(FakeOccurrence):
            """An occurrence whose assemblyContext is ITSELF - the cycle the walk must terminate on."""
            @property
            def assemblyContext(self):
                return self
        assert jt.component_world_matrix(self._des(_root_comp()), _sub(),
                                         _SelfNesting("L")) is None
