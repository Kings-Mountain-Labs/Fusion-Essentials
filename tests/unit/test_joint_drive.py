"""Unit tests for ``joint_drive`` — the Drive Joints command (set a joint's value).

Pinned (no live Fusion): the type gate (only revolute/slider/cylindrical drivable; rigid/ball refused),
the angle-vs-distance argument matching (a slider rejects angle_deg; a revolute rejects distance), the
degrees->radians and mm->cm conversions onto the API's value setters, the enabled-limit warning, the
value read-back, and the motion-link second-member refusal (driving a joint whose linked partner was
already driven this session is refused before any mutation). The motion classes are NAMED to match the
real adsk classes so the shared _current_joint_type maps them correctly.
"""

import types
import math

import adsk.fusion
import pytest

from conftest import (CylindricalJointMotion, FakeApplication, FakeDataFile, FakeFusionDocument,
                      FakeJoint, FakeMatrix3D, FakeMotionLink, FakeOccurrence, FakeTimelineObject,
                      FakeVector3D, MakeComp, RevoluteJointMotion, RigidJointMotion,
                      SliderJointMotion, _MotionLimits, _NamedCollection, install, load_tool,
                      make_design, make_occurrence, payload)

jd = load_tool("joint_drive")


@pytest.fixture(autouse=True)
def _isolated_document_keys():
    """_write_guard's per-instance key registry is SESSION state shared with every other consumer
    of document_key, and it outlives one call by design. Cleared around every test in this file so
    the minted tokens are deterministic and no fake document is left for another file to scan."""
    jd._write_guard._UNSAVED_DOC_KEYS.clear()
    yield
    jd._write_guard._UNSAVED_DOC_KEYS.clear()


# ── fakes: the shared conftest world, plus a scenario subclass where a state has no knob ────────────
# The shared motions are NAMED for their live types (_current_joint_type keys off
# type(jm).__name__), which is why every scenario subclass below carries that same name - and why
# each reaches its base through the alias here rather than through the name it shadows.
_Revolute, _Slider, _Cylindrical = RevoluteJointMotion, SliderJointMotion, CylindricalJointMotion

# What the FLAT occurrence walk throws with where a census has to take the recursed one - measured:
# allOccurrences raises on a subtree holding an unresolved reference.
_UNREADABLE_WALK = "2 : InternalValidationError : occ"


class _DrivenOccurrence(FakeOccurrence):
    """A joint member the mechanism places from the joint's own value: `place(motion)` is its
    transform2, so a drive that moves the part changes what a placement sample reads."""

    def __init__(self, path, motion, place):
        super().__init__(path)
        self._motion, self._place = motion, place

    @property
    def transform2(self):
        return self._read("transform2", self._place(self._motion))


def _occ(referenced=False, parent=None):
    """A joint occurrence stub: isReferencedComponent (+ an optional assemblyContext parent chain) is
    what the xref-scoped refusal reads to decide plain-vs-xref. `referenced` None is the flag whose
    READ DECLINES, which the live member always carries. It holds no transform2, so its placement is
    unreadable - the shape the 'no moved key' branch answers."""
    if referenced is None:
        return make_occurrence(assembly_context=parent,
                               raises_on={"isReferencedComponent": "3 : read declined"})
    return make_occurrence(referenced=referenced, assembly_context=parent)


def _plain_occ():
    return _occ(referenced=False, parent=None)


def _slides(axis):
    """The placement a slide value gives a member displaced along `axis` (cm per cm of slide)."""
    return lambda m: FakeMatrix3D(t=[a * m.slideValue for a in axis])


def _spins(motion):
    """The placement a rotation value gives a member spun about world z by that angle."""
    return FakeMatrix3D(math.degrees(motion.rotationValue))


def _fixed(motion):
    """The placement of a member the drive never moves - readable, and the same either side of it."""
    return FakeMatrix3D()


def _blind_slider():
    """A slider whose slideValue READ raises - the DOF answering nothing at all, which is neither a
    value nor the number zero."""
    class SliderJointMotion(_Slider):               # the NAME is what current_joint_type keys on
        @property
        def slideValue(self):
            raise RuntimeError("value unreadable")
    return SliderJointMotion()


def _blind_revolute():
    """The rotation twin of _blind_slider: rotationValue raises, so a partner observation has no
    number to publish."""
    class RevoluteJointMotion(_Revolute):           # the NAME is what current_joint_type keys on
        @property
        def rotationValue(self):
            raise RuntimeError("value unreadable")
    return RevoluteJointMotion()


def _cylindrical_frozen_slide():
    """A cylindrical joint whose ROTATION lands while its SLIDE is taken and kept nowhere."""
    return CylindricalJointMotion(slide_stores=False)


def link_pair(j1, j2, blind=False, name="MotionLink1", **kw):
    """Motion-link two joints the way the platform reports it: the SAME MotionLink appears in BOTH
    joints' own motionLinks. `blind` is the link that answers NOTHING about itself - no name, no
    suppression flag and no state source at either end - which a receipt must read as neither a
    working link nor a dead one. Returns the link."""
    ml = FakeMotionLink(name=name, joint_one=j1, joint_two=j2, **kw)
    if blind:
        ml.name = None
        del ml.healthState
        del ml.isSuppressed
    j1.motionLinks = _NamedCollection([ml])
    j2.motionLinks = _NamedCollection([ml])
    return ml


def _motionless_joint(name):
    """A joint whose jointMotion READ RAISES, so its kind reads as nothing at all - the state the
    type gate's last branch answers, and the one a joint with a known-but-undrivable kind cannot
    stand in for."""
    class _Motionless(FakeJoint):
        @property
        def jointMotion(self):
            raise RuntimeError("2 : InternalValidationError : motion")

        @jointMotion.setter
        def jointMotion(self, value):
            pass
    return _Motionless(name)


def _blind_link_joint(name, motion):
    """A joint whose motionLinks membership RAISES. 'Could not be asked' is not the same answer as
    'in no motion link', and only one of the two lets a receipt reason about grounding."""
    joint = FakeJoint(name, motion)
    joint.motionLinks = _NamedCollection(raises="membership unreadable")
    return joint


def _root(joints, asbuilt=()):
    """A root component carrying the two SEPARATE collections a joint walk reads."""
    return MakeComp(name="Root", joints=joints, as_built_joints=asbuilt)


def _design(joints, asbuilt=(), subs=()):
    """A design whose allComponents carries the ROOT - the contract _common.all_components holds; a
    collection without it hides every root joint from a design-wide walk."""
    root = _root(joints, asbuilt)
    return make_design(comp=root, all_components=[root] + list(subs))


def _install(joint, asbuilt=()):
    return install(jd, _design([joint], asbuilt))


# ── guards / type gate ───────────────────────────────────────────────────────

class TestGuards:
    def test_no_value_given_errors(self, monkeypatch):
        _install(FakeJoint("J", RevoluteJointMotion()))
        res = jd.handler(joint_name="J")
        assert res["isError"] is True and "drive" in res["message"].lower()

    def test_unknown_units(self, monkeypatch):
        _install(FakeJoint("J", SliderJointMotion()))
        res = jd.handler(joint_name="J", distance=5, units="furlong")
        assert res["isError"] is True and "unit" in res["message"].lower()

    def test_joint_not_found(self, monkeypatch):
        _install(FakeJoint("J", RevoluteJointMotion()))
        res = jd.handler(joint_name="Ghost", angle_deg=10)
        assert res["isError"] is True and "Ghost" in res["message"]

    def test_as_built_joint_is_drivable_by_name(self, monkeypatch):
        # an as-built REVOLUTE (in root.asBuiltJoints, a separate collection) must be found + driven,
        # not reported "no joint named ...". This is the cold-build case (script-created as-built spin).
        spin = FakeJoint("AsBuiltSpin", RevoluteJointMotion())
        _install(FakeJoint("Regular", RigidJointMotion()), asbuilt=[spin])
        out = payload(jd.handler(joint_name="AsBuiltSpin", angle_deg=90))
        assert out["driven"] is True
        assert abs(spin.jointMotion.rotationValue - math.pi / 2) < 1e-9

    def test_rigid_is_refused(self, monkeypatch):
        _install(FakeJoint("J", RigidJointMotion()))
        res = jd.handler(joint_name="J", angle_deg=10)
        assert res["isError"] is True and "revolute" in res["message"].lower()

    def test_a_joint_whose_motion_does_not_read_is_refused_without_a_kind(self, monkeypatch):
        # The kind reads as '' here, and a refusal naming no kind at all would read as a joint whose
        # type the caller could look up - it has to say the type is what did not answer.
        _install(_motionless_joint("J"))
        res = jd.handler(joint_name="J", angle_deg=10)
        assert res["isError"] is True and "is an unknown type" in res["message"]

    def test_slider_rejects_angle(self, monkeypatch):
        _install(FakeJoint("J", SliderJointMotion()))
        res = jd.handler(joint_name="J", angle_deg=10)
        assert res["isError"] is True and "distance" in res["message"].lower()

    def test_revolute_rejects_distance(self, monkeypatch):
        _install(FakeJoint("J", RevoluteJointMotion()))
        res = jd.handler(joint_name="J", distance=10)
        assert res["isError"] is True and "angle_deg" in res["message"]


# ── revolute drive: degrees -> radians ──────────────────────────────────────

class TestRevoluteDrive:
    def test_angle_set_in_radians(self, monkeypatch):
        j = FakeJoint("Pivot", RevoluteJointMotion())
        _install(j)
        out = payload(jd.handler(joint_name="Pivot", angle_deg=90))
        assert abs(j.jointMotion.rotationValue - math.pi / 2) < 1e-9
        assert out["driven"] is True and out["joint_type"] == "revolute"
        assert out["applied"]["angle_deg"] == 90.0

    def test_value_read_back_in_degrees(self, monkeypatch):
        j = FakeJoint("Pivot", RevoluteJointMotion())
        _install(j)
        out = payload(jd.handler(joint_name="Pivot", angle_deg=45))
        assert abs(out["value_now"]["angle_deg"] - 45.0) < 1e-4

    def test_an_over_limit_command_is_REFUSED_before_assignment(self, monkeypatch):
        # measured live: Fusion IGNORES a beyond-limit drive (the value stays put; it never
        # clamps) - so the command is refused before anything is assigned.
        m = RevoluteJointMotion()
        m.rotationLimits = _MotionLimits(maximum=math.radians(30))   # max 30 deg
        j = FakeJoint("Pivot", m)
        _install(j)
        res = jd.handler(joint_name="Pivot", angle_deg=60)                  # exceeds 30
        assert res["isError"] is True
        assert "above the enabled maximum" in res["message"] and "30.0 deg" in res["message"]
        assert j.jointMotion.rotationValue == 0.0                           # nothing was assigned

    def test_a_command_exactly_at_the_bound_is_driven(self, monkeypatch):
        # measured live: a command exactly AT an enabled bound lands on it - only STRICTLY
        # beyond is refused.
        m = RevoluteJointMotion()
        m.rotationLimits = _MotionLimits(maximum=math.radians(30))
        j = FakeJoint("Pivot", m)
        _install(j)
        out = payload(jd.handler(joint_name="Pivot", angle_deg=30))
        assert abs(out["value_now"]["angle_deg"] - 30.0) < 1e-3


# ── slider drive: mm -> cm ──────────────────────────────────────────────────

class TestSliderDrive:
    def test_distance_set_in_cm(self, monkeypatch):
        j = FakeJoint("Rail", SliderJointMotion())
        _install(j)
        out = payload(jd.handler(joint_name="Rail", distance=50, units="mm"))
        assert abs(j.jointMotion.slideValue - 5.0) < 1e-9       # 50 mm -> 5 cm
        assert out["joint_type"] == "slider"

    def test_distance_read_back_in_mm(self, monkeypatch):
        j = FakeJoint("Rail", SliderJointMotion())
        _install(j)
        out = payload(jd.handler(joint_name="Rail", distance=50, units="mm"))
        assert abs(out["value_now"]["distance_mm"] - 50.0) < 1e-4

    def test_inch_distance(self, monkeypatch):
        j = FakeJoint("Rail", SliderJointMotion())
        _install(j)
        jd.handler(joint_name="Rail", distance=1, units="in")
        assert abs(j.jointMotion.slideValue - 2.54) < 1e-9      # 1 in -> 2.54 cm

    def test_a_below_minimum_command_is_REFUSED_before_assignment(self, monkeypatch):
        # measured live for rotation; the slide path shares the refusal (Fusion ignores an
        # out-of-range drive - nothing would move).
        m = SliderJointMotion()
        m.slideLimits = _MotionLimits(minimum=0.0)        # min 0 cm
        j = FakeJoint("Rail", m)
        _install(j)
        res = jd.handler(joint_name="Rail", distance=-20, units="mm")      # -2 cm < 0
        assert res["isError"] is True
        assert "below the enabled minimum" in res["message"] and "mm" in res["message"]
        assert j.jointMotion.slideValue == 0.0                   # nothing was assigned


# ── cylindrical drive: both angle + distance ────────────────────────────────

class TestCylindricalDrive:
    def test_drives_both_values(self, monkeypatch):
        j = FakeJoint("Cyl", CylindricalJointMotion())
        _install(j)
        out = payload(jd.handler(joint_name="Cyl", angle_deg=30, distance=10, units="mm"))
        assert abs(j.jointMotion.rotationValue - math.radians(30)) < 1e-9
        assert abs(j.jointMotion.slideValue - 1.0) < 1e-9       # 10 mm -> 1 cm
        assert out["applied"] == {"angle_deg": 30.0, "distance": 10.0}
        assert "angle_deg" in out["value_now"] and "distance_mm" in out["value_now"]

    def test_cylindrical_angle_only(self, monkeypatch):
        j = FakeJoint("Cyl", CylindricalJointMotion())
        _install(j)
        out = payload(jd.handler(joint_name="Cyl", angle_deg=15))
        assert abs(j.jointMotion.rotationValue - math.radians(15)) < 1e-9
        assert j.jointMotion.slideValue == 0.0                  # untouched


# ── motion-link second-member refusal ───────────────────────────────────────
# Driving BOTH members of a motion-linked pair kills the Fusion process; once one member is driven,
# driving its partner is refused for the session.

def _doc(name="DocA", urn=None):
    """One open document: `urn` is the lineage id its dataFile answers, and no urn is the
    never-saved document whose dataFile answers none."""
    return FakeFusionDocument(name=name,
                              data_file=None if urn is None else FakeDataFile(file_id=urn))


def _save_as(doc, urn):
    """The document SAVED without closing: the same handle, which the key registry still matches,
    now answers a data-file id where it answered none."""
    doc.dataFile = FakeDataFile(file_id=urn)


def _app(doc_name="DocA", urn=None):
    """The session seam _write_guard reads the active document through."""
    return FakeApplication(active_document=_doc(doc_name, urn))


class TestDocumentKeyIdentity:
    """What THIS consumer does with the shared document key (_write_guard.document_key).

    The key itself - the lineage urn for a saved document, a per-instance token matched by document
    HANDLE for an unsaved one (measured live, two open never-saved documents both answer
    'Untitled', so a name is not a stand-in), and the backward prune walk - is pinned once in
    test_write_guard.py. Pinned here is joint_drive's own branch on it: the stand-in it words when
    no document reads at all, and the consequence for the driven-joint registry, whose entityToken
    half is DOCUMENT-LOCAL and repeats across documents - so a key that merged two documents would
    refuse a safe drive in one because a DIFFERENT document's joint was driven.
    """

    def test_no_readable_document_is_not_a_document_key(self, monkeypatch):
        # document_key answers None here - nothing read, so nothing to mint for. This consumer's
        # stand-in is a string that is not a document either, so it matches no real one.
        monkeypatch.setattr(jd._write_guard, "app", FakeApplication())   # no active document
        key = jd._doc_key()
        assert key == "<no document>"
        assert not key.startswith("unsaved:")               # and it mints nothing
        assert jd._write_guard._UNSAVED_DOC_KEYS == []

    def test_several_closed_documents_are_all_evicted_in_one_pass(self, monkeypatch):
        # The SHARED eviction, reached through this consumer. It walks the registry BACKWARDS so a
        # deletion cannot slide the next entry past the cursor, and so the index it holds stays
        # inside a list that is shrinking under it. Only a registry holding MORE THAN ONE dead entry
        # tells the two walks apart: a forward walk skips the entry that slid into the freed slot
        # and then indexes past the end, raising IndexError out of _doc_key() and so out of
        # handler(). A --keep-open session full of scratch documents produces exactly this shape.
        app = _app("Untitled")
        docs = [app.activeDocument, _doc("Untitled"), _doc("Untitled")]
        monkeypatch.setattr(jd._write_guard, "app", app)
        keys = []
        for d in docs:
            app.activeDocument = d
            keys.append(jd._doc_key())
        assert len(jd._write_guard._UNSAVED_DOC_KEYS) == 3 and len(set(keys)) == 3
        assert docs[0].close() is True
        assert docs[1].close() is True             # two dead entries, adjacent, at the front
        app.activeDocument = docs[2]
        assert jd._doc_key() == keys[2]       # the survivor keeps its own key
        assert [k for _d, k in jd._write_guard._UNSAVED_DOC_KEYS] == [keys[2]]

    def test_a_drive_in_one_unsaved_document_does_not_refuse_the_partner_in_another(self, monkeypatch):
        # The end-to-end consequence, holding the JOINTS constant and varying only the document:
        # driving JawL in one never-saved document must not refuse JawR in a different one.
        jaw_l = FakeJoint("Slider_JawL", SliderJointMotion())
        jaw_r = FakeJoint("Slider_JawR", SliderJointMotion())
        link_pair(jaw_l, jaw_r)
        design = _design([jaw_l, jaw_r])
        install(jd, design)
        monkeypatch.setattr(jd, "_driven_this_session", set())
        app = _app("Untitled")
        monkeypatch.setattr(jd._write_guard, "app", app)
        assert payload(jd.handler(joint_name="Slider_JawL", distance=16))["driven"] is True
        app.activeDocument = _doc("Untitled")          # a DIFFERENT unsaved document
        out = jd.handler(joint_name="Slider_JawR", distance=-16)
        assert out["isError"] is False, out
        # and the guard is not disarmed generally: back in the FIRST document it still refuses.
        app.activeDocument = jd._write_guard._UNSAVED_DOC_KEYS[0][0]
        assert jd.handler(joint_name="Slider_JawR", distance=-16)["isError"] is True


class TestSecondMemberRefusal:
    @pytest.fixture
    def linked_pair(self, monkeypatch):
        """A JawL/JawR slider pair motion-linked at the root; fresh session registry; DocA active.
        Yields (jaw_l, jaw_r, design)."""
        jaw_l = FakeJoint("Slider_JawL", SliderJointMotion())
        jaw_r = FakeJoint("Slider_JawR", SliderJointMotion())
        link_pair(jaw_l, jaw_r)
        design = _design([jaw_l, jaw_r])
        install(jd, design)
        monkeypatch.setattr(jd._write_guard, "app", _app("DocA"))
        monkeypatch.setattr(jd, "_driven_this_session", set())
        return jaw_l, jaw_r, design

    def test_partner_drive_refused_after_first_member(self, linked_pair):
        jaw_l, jaw_r, _ = linked_pair
        assert payload(jd.handler(joint_name="Slider_JawL", distance=16))["driven"] is True
        res = jd.handler(joint_name="Slider_JawR", distance=-16)
        assert res["isError"] is True
        assert "Slider_JawL" in res["message"]           # names the driven partner
        assert jaw_r.jointMotion.slideValue == 0.0       # refused BEFORE mutating

    def test_refusal_reports_current_value(self, linked_pair):
        # The link (in real Fusion) already moved the refused joint; the refusal reports its current
        # value so the caller needs no extra read.
        jaw_l, jaw_r, _ = linked_pair
        jd.handler(joint_name="Slider_JawL", distance=16)
        jaw_r.jointMotion.slideValue = -1.6              # what the link did (cm)
        res = jd.handler(joint_name="Slider_JawR", distance=-16)
        assert res["isError"] is True and "-16.0 mm" in res["message"]

    def test_first_linked_drive_names_partner_and_warns(self, linked_pair):
        # A successful drive of a LINKED joint reports the partner + the cycle warning; an
        # unlinked drive carries neither.
        out = payload(jd.handler(joint_name="Slider_JawL", distance=16))
        assert out["motion_link_partner"] == "Slider_JawR"
        assert "Slider_JawR" in out["note"] and "offset" in out["note"]

    def test_same_member_redrive_allowed(self, linked_pair):
        # Repeatedly driving the SAME member is the safe, supported pattern.
        _, _, _ = linked_pair
        assert payload(jd.handler(joint_name="Slider_JawL", distance=16))["driven"] is True
        assert payload(jd.handler(joint_name="Slider_JawL", distance=0))["driven"] is True
        assert payload(jd.handler(joint_name="Slider_JawL", distance=-5))["driven"] is True

    def test_unlinked_joints_both_drivable(self, monkeypatch):
        a, b = FakeJoint("A", SliderJointMotion()), FakeJoint("B", SliderJointMotion())
        design = _design([a, b])                          # no motion link
        install(jd, design)
        monkeypatch.setattr(jd._write_guard, "app", _app("DocA"))
        monkeypatch.setattr(jd, "_driven_this_session", set())
        assert payload(jd.handler(joint_name="A", distance=5))["driven"] is True
        assert payload(jd.handler(joint_name="B", distance=5))["driven"] is True

    def test_link_inside_subcomponent_is_found(self, monkeypatch):
        # The xref shape: the pair + link live inside an xref'd sub-assembly, NOT on the root - the
        # partner comes off the JOINT'S OWN motionLinks membership, so a joint resolved through the
        # sub-component walk carries its link with it.
        jaw_l = FakeJoint("Slider_JawL", SliderJointMotion())
        jaw_r = FakeJoint("Slider_JawR", SliderJointMotion())
        link_pair(jaw_l, jaw_r)
        sub = _root([jaw_l, jaw_r])
        design = _design([], subs=[sub])                   # root: no joints
        install(jd, design)
        monkeypatch.setattr(jd._write_guard, "app", _app("DocA"))
        monkeypatch.setattr(jd, "_driven_this_session", set())
        assert payload(jd.handler(joint_name="Slider_JawL", distance=16))["driven"] is True
        res = jd.handler(joint_name="Slider_JawR", distance=-16)
        assert res["isError"] is True and "Slider_JawL" in res["message"]

    def test_registry_is_per_document_identity(self, monkeypatch, linked_pair):
        # A same-named pair in ANOTHER document (distinct lineage URN, same display name) is not
        # poisoned by the first document's drive - identity is the URN, not the name.
        monkeypatch.setattr(jd._write_guard, "app", _app("Doc", urn="urn:lineage:a"))
        jd.handler(joint_name="Slider_JawL", distance=16)
        monkeypatch.setattr(jd._write_guard, "app", _app("Doc", urn="urn:lineage:b"))
        assert payload(jd.handler(joint_name="Slider_JawR", distance=-16))["driven"] is True

    def test_rename_does_not_disarm_guard(self, monkeypatch, linked_pair):
        # The first drive registers under the lineage URN; a document RENAME (name changes,
        # dataFile.id stable) must still refuse the second-member drive.
        jaw_l, jaw_r, _ = linked_pair
        monkeypatch.setattr(jd._write_guard, "app", _app("Original", urn="urn:lineage:1"))
        assert payload(jd.handler(joint_name="Slider_JawL", distance=16))["driven"] is True
        monkeypatch.setattr(jd._write_guard, "app", _app("Renamed", urn="urn:lineage:1"))
        res = jd.handler(joint_name="Slider_JawR", distance=-16)
        assert res["isError"] is True and "Slider_JawL" in res["message"]
        assert jaw_r.jointMotion.slideValue == 0.0       # refused BEFORE mutating

    def test_unsaved_doc_keys_on_a_per_instance_token_and_the_guard_still_functions(self, linked_pair):
        # An unsaved document has no dataFile, so the registry keys on a per-instance token minted
        # for that document - NOT its name. Within the one document the guard is unchanged: the
        # second member is still refused.
        jaw_l, jaw_r, _ = linked_pair                     # fixture's DocA carries no dataFile
        assert payload(jd.handler(joint_name="Slider_JawL", distance=16))["driven"] is True
        keys = [k for k, _ in jd._driven_this_session]
        assert keys and all(k.startswith("unsaved:") for k in keys), keys
        assert "DocA" not in keys                         # the name is not the identity
        res = jd.handler(joint_name="Slider_JawR", distance=-16)
        assert res["isError"] is True and "Slider_JawL" in res["message"]

    def test_guard_survives_the_save_that_re_keys_the_document(self, monkeypatch, linked_pair):
        # THE BITE, and it fails toward the crash: a never-saved document holding referenced
        # components is driven, then SAVED. The document key changes from the minted token to the
        # data-file id without the document closing, so the entry parked under the token stops
        # matching and the partner reads as never driven - the both-members refusal, which exists
        # because driving both members of a motion-linked pair in an xref context has killed the
        # Fusion process, does not fire. The rename announcement carries the entry across.
        jaw_l, jaw_r, _ = linked_pair
        app = _app("Untitled")                          # no dataFile - never saved
        monkeypatch.setattr(jd._write_guard, "app", app)
        assert payload(jd.handler(joint_name="Slider_JawL", distance=16))["driven"] is True
        _save_as(app.activeDocument, "urn:lineage:saved")     # doc_save_as, same open document
        res = jd.handler(joint_name="Slider_JawR", distance=-16)
        assert res["isError"] is True and "Slider_JawL" in res["message"]
        assert jaw_r.jointMotion.slideValue == 0.0          # refused BEFORE mutating

    def test_the_carried_entry_keeps_its_own_joint_token(self, monkeypatch, linked_pair):
        # Only the DOCUMENT half of the key moves. The entity-token half is what makes a
        # delete+recreate of the driven joint clear the block and a rename keep it; carrying an
        # entry that lost it would arm the guard for every joint in the document.
        jaw_l, jaw_r, _ = linked_pair
        jaw_l.entityToken = "tok:jawL"
        app = _app("Untitled")
        monkeypatch.setattr(jd._write_guard, "app", app)
        jd.handler(joint_name="Slider_JawL", distance=16)
        _save_as(app.activeDocument, "urn:lineage:saved")
        jd._doc_key()                                       # the read that detects the flip
        assert jd._driven_this_session == {("urn:lineage:saved", "tok:jawL")}

    def test_a_save_in_one_document_does_not_re_key_anothers_entries(self, monkeypatch, linked_pair):
        # The announcement is broadcast but names ONE key: an entry registered against a different
        # document must keep its own key, or saving document A silently moves document B's
        # driven-joint entries onto A's new id and refuses a safe drive there.
        jd._driven_this_session.add(("unsaved:other", "tok:elsewhere"))
        app = _app("Untitled")
        monkeypatch.setattr(jd._write_guard, "app", app)
        jd.handler(joint_name="Slider_JawL", distance=16)
        _save_as(app.activeDocument, "urn:lineage:saved")
        jd._doc_key()
        assert ("unsaved:other", "tok:elsewhere") in jd._driven_this_session

    def test_partial_drive_still_arms_the_guard(self, monkeypatch):
        # A cylindrical drive whose rotation assignment is accepted and whose slide then raises: the
        # receipt names the accepted assignment and withholds every verdict it did not read, and the
        # session registry arms anyway - the guard fails toward refusal, or the both-members xref
        # refusal fails open on exactly this sequence.
        class CylindricalJointMotion(_Cylindrical):       # name keys the shared type map
            """A cylindrical joint whose SLIDE assignment RAISES while its rotation lands."""
            @property
            def slideValue(self):
                return self._slide

            @slideValue.setter
            def slideValue(self, v):
                raise RuntimeError("slide jammed")
        a = FakeJoint("Cyl_A", CylindricalJointMotion())
        b = FakeJoint("Cyl_B", SliderJointMotion())
        link_pair(a, b)
        design = _design([a, b])
        install(jd, design)
        monkeypatch.setattr(jd._write_guard, "app", _app("DocA"))
        monkeypatch.setattr(jd, "_driven_this_session", set())
        res = jd.handler(joint_name="Cyl_A", angle_deg=30, distance=10, units="mm")
        assert res["isError"] is True
        assert "The assignments made before the failure ({'angle_deg': 30.0}) were accepted" \
            in res["message"]
        # The receipt reads nothing back on this path - not this joint's value, not the partner's -
        # so it claims neither a motion for the joint nor one carried to the linked 'Cyl_B'.
        assert "no value was read back here" in res["message"]
        assert "motion-linked partner" not in res["message"] and "has moved" not in res["message"]
        assert abs(a.jointMotion.rotationValue - math.radians(30)) < 1e-9   # rotation DID land
        res2 = jd.handler(joint_name="Cyl_B", distance=-16)
        assert res2["isError"] is True and "Cyl_A" in res2["message"]       # guard armed


# ── xref-scoping + token keying ─────────────────────────────────────────────
# The second-member refusal is scoped to xref-context pairs (crashes are observed only in xref contexts); a plain
# in-document pair is allowed with a warning. The registry keys on entity token, so rebuilding the
# driven partner clears the block.

class TestXrefScopingAndTokens:
    def _install(self, monkeypatch, jaws, urn=None):
        design = _design(jaws)
        install(jd, design)
        monkeypatch.setattr(jd._write_guard, "app", _app("DocA", urn=urn))
        monkeypatch.setattr(jd, "_driven_this_session", set())
        return design

    def test_plain_pair_second_member_allowed_with_warning(self, monkeypatch):
        # Both joints wholly native -> driving the second member is ALLOWED (a plain in-document
        # pair survives a both-members drive), with a warning; the drive actually takes.
        jaw_l = FakeJoint("L", SliderJointMotion(), occurrence_one=_plain_occ(), occurrence_two=_plain_occ())
        jaw_r = FakeJoint("R", SliderJointMotion(), occurrence_one=_plain_occ(), occurrence_two=_plain_occ())
        link_pair(jaw_l, jaw_r)
        self._install(monkeypatch, [jaw_l, jaw_r])
        assert payload(jd.handler(joint_name="L", distance=16))["driven"] is True
        out = payload(jd.handler(joint_name="R", distance=-16))
        assert out["driven"] is True                       # NOT refused
        assert abs(jaw_r.jointMotion.slideValue - (-1.6)) < 1e-9   # the drive took
        assert "plain" in out["note"].lower()              # the both-members warning

    def test_xref_pair_second_member_refused(self, monkeypatch):
        # A referenced (xref) occurrence anywhere in the pair -> the second-member drive is refused.
        xr = _occ(referenced=True)
        jaw_l = FakeJoint("L", SliderJointMotion(), occurrence_one=xr, occurrence_two=_plain_occ())
        jaw_r = FakeJoint("R", SliderJointMotion(), occurrence_one=xr, occurrence_two=_plain_occ())
        link_pair(jaw_l, jaw_r)
        self._install(monkeypatch, [jaw_l, jaw_r])
        assert payload(jd.handler(joint_name="L", distance=16))["driven"] is True
        res = jd.handler(joint_name="R", distance=-16)
        assert res["isError"] is True and "R" in res["message"]
        assert jaw_r.jointMotion.slideValue == 0.0         # refused BEFORE mutating
        assert "did NOT read as wholly native" in res["message"]
        assert "isReferencedComponent" in res["message"]

    def test_the_refusal_does_not_assert_an_xref_context_it_never_read(self, monkeypatch):
        # _pair_is_plain answers False for an occurrence that would not ANSWER isReferencedComponent
        # just as it does for one that reads true, so the guard arms on both. Wording the refusal as
        # "the pair is in an XREF/referenced context" states a context nothing here established - the
        # sentence has to name the reading that was taken, which is that the pair did not read native.
        blind = _occ(referenced=None)                # isReferencedComponent does not answer
        jaw_l = FakeJoint("L", SliderJointMotion(), occurrence_one=blind, occurrence_two=_plain_occ())
        jaw_r = FakeJoint("R", SliderJointMotion(), occurrence_one=blind, occurrence_two=_plain_occ())
        link_pair(jaw_l, jaw_r)
        self._install(monkeypatch, [jaw_l, jaw_r])
        assert payload(jd.handler(joint_name="L", distance=16))["driven"] is True
        res = jd.handler(joint_name="R", distance=-16)
        assert res["isError"] is True
        assert "did NOT read as wholly native" in res["message"]
        assert "did not answer isReferencedComponent" in res["message"]
        assert "the pair is in an XREF" not in res["message"]
        assert jaw_r.jointMotion.slideValue == 0.0         # still refused, still before mutating

    def test_a_pair_with_NO_occurrences_claims_no_referenced_reading(self, monkeypatch):
        # _pair_is_plain refuses an occurrence that read as None before it reads any flag, so a
        # joint whose occurrenceOne and occurrenceTwo are both None arms the guard with NOTHING
        # having been read off an occurrence. A refusal naming only the referenced/ancestor/unread
        # arms states a reading nothing took here; the fourth arm is the one that applies.
        jaw_l = FakeJoint("L", SliderJointMotion())          # occ_one/occ_two default to None
        jaw_r = FakeJoint("R", SliderJointMotion())
        link_pair(jaw_l, jaw_r)
        self._install(monkeypatch, [jaw_l, jaw_r])
        assert payload(jd.handler(joint_name="L", distance=16))["driven"] is True
        res = jd.handler(joint_name="R", distance=-16)
        assert res["isError"] is True
        assert "did NOT read as wholly native" in res["message"]
        assert "did not read as an occurrence at all" in res["message"]
        # and it claims no COUNT of readings: _pair_is_plain SHORT-CIRCUITS, and this rig is the
        # one-read case - the first occurrence reads None, so the other three are never asked. A
        # refusal saying both joints' occurrenceOne and occurrenceTwo "were read" states three
        # readings that were never taken.
        assert "were read" not in res["message"]
        assert jaw_r.jointMotion.slideValue == 0.0         # still refused, still before mutating

    def test_the_short_circuit_really_stops_at_the_first_non_native_read(self, monkeypatch):
        # What licenses dropping the count clause: the reads are RECORDED here, and the all-None
        # pair takes exactly ONE of the four. A _pair_is_plain that asked all four regardless would
        # make the dropped clause true again - and would be reading occurrences the answer does not
        # need.
        read = []

        class _RecordingJoint(FakeJoint):
            """Records which of its two occurrence reads was taken; both answer None, the shape
            that lets the very FIRST read decide the answer."""

            @property
            def occurrenceOne(self):
                read.append((self.name, "occurrenceOne"))
                return None

            @occurrenceOne.setter
            def occurrenceOne(self, value):
                pass

            @property
            def occurrenceTwo(self):
                read.append((self.name, "occurrenceTwo"))
                return None

            @occurrenceTwo.setter
            def occurrenceTwo(self, value):
                pass

        assert jd._pair_is_plain(_RecordingJoint("L"), _RecordingJoint("R")) is False
        assert read == [("L", "occurrenceOne")]

    def test_xref_via_referenced_ancestor_refused(self, monkeypatch):
        # The pair is native but nested INSIDE a referenced parent -> refused.
        parent = _occ(referenced=True)
        jaw_l = FakeJoint("L", SliderJointMotion(),
                          occurrence_one=_occ(parent=parent), occurrence_two=_occ(parent=parent))
        jaw_r = FakeJoint("R", SliderJointMotion(),
                          occurrence_one=_occ(parent=parent), occurrence_two=_occ(parent=parent))
        link_pair(jaw_l, jaw_r)
        self._install(monkeypatch, [jaw_l, jaw_r])
        assert payload(jd.handler(joint_name="L", distance=16))["driven"] is True
        assert jd.handler(joint_name="R", distance=-16)["isError"] is True

    def test_recreated_partner_new_token_clears_block(self, monkeypatch):
        # xref pair: driving L registers L's token t1. Rebuilding L (new token t2, still linked to R)
        # means driving R does not match the registered token -> allowed. Delete+recreate clears.
        l_old = FakeJoint("L", SliderJointMotion(),
                          occurrence_one=_occ(referenced=True), occurrence_two=_occ(referenced=True),
                          entity_token="t1")
        r = FakeJoint("R", SliderJointMotion(),
                      occurrence_one=_occ(referenced=True), occurrence_two=_occ(referenced=True), entity_token="tr")
        link_pair(l_old, r)
        design = self._install(monkeypatch, [l_old, r])
        assert payload(jd.handler(joint_name="L", distance=16))["driven"] is True
        # rebuild L with a new token; R now links to the rebuilt L
        l_new = FakeJoint("L", SliderJointMotion(),
                          occurrence_one=_occ(referenced=True), occurrence_two=_occ(referenced=True),
                          entity_token="t2")
        link_pair(l_new, r)
        design.rootComponent.joints = _NamedCollection([l_new, r])
        assert payload(jd.handler(joint_name="R", distance=-16))["driven"] is True

    def test_same_token_partner_still_refused(self, monkeypatch):
        # The token is stable across a rename: a still-registered partner token keeps the refusal
        # (xref pair) - the token change is what clears it, not merely re-reading.
        l = FakeJoint("L", SliderJointMotion(),
                      occurrence_one=_occ(referenced=True), occurrence_two=_occ(referenced=True), entity_token="t1")
        r = FakeJoint("R", SliderJointMotion(),
                      occurrence_one=_occ(referenced=True), occurrence_two=_occ(referenced=True), entity_token="tr")
        link_pair(l, r)
        self._install(monkeypatch, [l, r])
        assert payload(jd.handler(joint_name="L", distance=16))["driven"] is True
        assert jd.handler(joint_name="R", distance=-16)["isError"] is True


# ── the link's own STATE gates the coupling claim and the refusal ────────────
# A motion link that reads SUPPRESSED or compute-failed transmits nothing, so the receipt claims no
# moved partner and the second-member refusal is not armed (measured: a suppressed rack/pinion link
# allowed independent drives). A link whose state does not READ is neither state: the claim is
# dropped, the refusal stays.

class TestLinkStateGating:
    def _xref_pair(self, monkeypatch, **link_kw):
        """A JawL/JawR slider pair in an XREF context - the context the second-member refusal exists
        for, so 'armed' and 'not armed' are told apart by one drive. Returns (jaw_l, jaw_r, link)."""
        jaw_l = FakeJoint("Slider_JawL", SliderJointMotion(),
                          occurrence_one=_occ(referenced=True), occurrence_two=_occ(referenced=True))
        jaw_r = FakeJoint("Slider_JawR", SliderJointMotion(),
                          occurrence_one=_occ(referenced=True), occurrence_two=_occ(referenced=True))
        ml = link_pair(jaw_l, jaw_r, **link_kw)
        design = _design([jaw_l, jaw_r])
        install(jd, design)
        monkeypatch.setattr(jd._write_guard, "app", _app("DocA"))
        monkeypatch.setattr(jd, "_driven_this_session", set())
        return jaw_l, jaw_r, ml

    def test_a_healthy_link_claims_the_coupling_and_refuses_the_second_member(self, monkeypatch):
        # the control the two gated cases are read against: only the LINK'S STATE differs below.
        _l, jaw_r, _ml = self._xref_pair(monkeypatch)
        out = payload(jd.handler(joint_name="Slider_JawL", distance=16))
        assert out["motion_link_state"] == {"link": "MotionLink1", "suppressed": False, "broken": False,
                                      "value_self": 1.0, "value_partner": 1.0, "reversed": False}
        assert "the link couples the two joints" in out["note"]
        res = jd.handler(joint_name="Slider_JawR", distance=-16)
        assert res["isError"] is True and "Slider_JawL" in res["message"]
        # The refusal states the two things it READ - the link's clean states, and this joint's
        # current value - and claims no motion for a joint whose pre-drive value it never took.
        assert "The link reads neither suppressed nor compute-failed" in res["message"]
        assert "'Slider_JawR' now reads 0.0 mm" in res["message"]
        assert "whether the partner's drive moved it is not read here" in res["message"]
        assert jaw_r.jointMotion.slideValue == 0.0

    def test_a_suppressed_link_makes_no_moved_partner_claim(self, monkeypatch):
        self._xref_pair(monkeypatch, suppressed=True)
        out = payload(jd.handler(joint_name="Slider_JawL", distance=16))
        assert out["motion_link_state"]["suppressed"] is True
        assert "reads SUPPRESSED" in out["note"]
        assert "makes NO claim that the partner moved" in out["note"]
        assert "the link couples the two joints" not in out["note"]

    def test_a_suppressed_link_arms_no_second_member_refusal(self, monkeypatch):
        _l, jaw_r, _ml = self._xref_pair(monkeypatch, suppressed=True)
        assert payload(jd.handler(joint_name="Slider_JawL", distance=16))["driven"] is True
        out = payload(jd.handler(joint_name="Slider_JawR", distance=-16))
        assert out["driven"] is True
        assert abs(jaw_r.jointMotion.slideValue - (-1.6)) < 1e-9     # the drive actually took
        assert "does not read as one that couples" in out["note"]

    def test_a_link_suppressed_on_its_TIMELINE_item_makes_no_moved_partner_claim(self, monkeypatch):
        # the second suppression route: the link's own flag reads False while the TIMELINE item
        # carries the suppression - the pairing assembly_get makes for a Joint, where it is
        # live-verified. Reading the link's flag alone publishes a coupling for a link that
        # transmits nothing and arms the second-member refusal on it.
        _l, jaw_r, _ml = self._xref_pair(monkeypatch, suppressed=False,
                                         timeline_object=FakeTimelineObject(suppressed=True))
        out = payload(jd.handler(joint_name="Slider_JawL", distance=16))
        assert out["motion_link_state"]["suppressed"] is True
        assert "reads SUPPRESSED" in out["note"]
        assert "makes NO claim that the partner moved" in out["note"]
        assert "the link couples the two joints" not in out["note"]
        # and the refusal is not armed: the second member drives, because a suppressed link is not
        # the both-members-of-a-coupling case the crash guard exists for.
        assert payload(jd.handler(joint_name="Slider_JawR", distance=-16))["driven"] is True

    def test_a_compute_failed_link_is_gated_the_same_way(self, monkeypatch):
        # the OTHER dead state: a link whose compute failed couples nothing either.
        _l, jaw_r, _ml = self._xref_pair(
            monkeypatch, health=adsk.fusion.FeatureHealthStates.ErrorFeatureHealthState)
        out = payload(jd.handler(joint_name="Slider_JawL", distance=16))
        assert out["motion_link_state"]["broken"] is True
        assert "carrying a compute failure" in out["note"]
        assert "makes NO claim that the partner moved" in out["note"]
        assert payload(jd.handler(joint_name="Slider_JawR", distance=-16))["driven"] is True

    def test_a_warning_state_link_counts_as_broken(self, monkeypatch):
        # Fusion marks a warning-state feature 'Compute Failed' too - the shared classifier treats
        # warning and error alike, so a warned link is gated like a failed one.
        self._xref_pair(
            monkeypatch, health=adsk.fusion.FeatureHealthStates.WarningFeatureHealthState)
        out = payload(jd.handler(joint_name="Slider_JawL", distance=16))
        assert out["motion_link_state"]["broken"] is True

    def test_an_unreadable_link_state_withholds_the_claim(self, monkeypatch):
        self._xref_pair(monkeypatch, blind=True)
        out = payload(jd.handler(joint_name="Slider_JawL", distance=16))
        assert out["motion_link_state"]["suppressed"] is None and out["motion_link_state"]["broken"] is None
        assert "not known from this receipt" in out["note"]
        assert "the link couples the two joints" not in out["note"]
        assert "makes NO claim that the partner moved" not in out["note"]

    def test_an_unreadable_link_state_still_arms_the_refusal(self, monkeypatch):
        # THE BITE, and it fails toward the crash: an unread state must not be read as a dead link,
        # or the both-members drive this guard exists to stop goes through unremarked.
        _l, jaw_r, _ml = self._xref_pair(monkeypatch, blind=True)
        assert payload(jd.handler(joint_name="Slider_JawL", distance=16))["driven"] is True
        res = jd.handler(joint_name="Slider_JawR", distance=-16)
        assert res["isError"] is True
        assert "stands on that unread state" in res["message"]
        # the two arms must stay apart: the clean-state clause belongs only to a link that READ
        # clean, so an unread state can never borrow it.
        assert "reads neither suppressed nor compute-failed" not in res["message"]
        assert jaw_r.jointMotion.slideValue == 0.0

    def test_only_the_suppression_reading_none_is_enough_to_withhold(self, monkeypatch):
        # HALF a state is not a state: a link answering its compute state but not its suppression is
        # unknown, not clean.
        _l, _r, ml = self._xref_pair(monkeypatch)
        del ml.isSuppressed
        out = payload(jd.handler(joint_name="Slider_JawL", distance=16))
        assert out["motion_link_state"] == {"link": "MotionLink1", "suppressed": None, "broken": False,
                                      "value_self": 1.0, "value_partner": 1.0, "reversed": False}
        assert "did not answer its suppression" in out["note"]
        assert jd.handler(joint_name="Slider_JawR", distance=-16)["isError"] is True


class TestTheValueClauseWithholdsAnUnreadValue:
    """Every sentence stating a joint's current value goes through _value_clause, whose text answers
    None when the DOF read nothing. Each consumer branches on that itself, so each one is driven
    here: a value that did not read is published as unread, never as a reading."""

    def _xref_pair_with_a_blind_second_member(self, monkeypatch, **link_kw):
        """The JawL/JawR xref pair the second-member refusal exists for, with JawR's slideValue READ
        raising - the joint the refusal states a current value for."""
        jaw_l = FakeJoint("Slider_JawL", SliderJointMotion(),
                          occurrence_one=_occ(referenced=True), occurrence_two=_occ(referenced=True))
        jaw_r = FakeJoint("Slider_JawR", _blind_slider(),
                          occurrence_one=_occ(referenced=True), occurrence_two=_occ(referenced=True))
        link_pair(jaw_l, jaw_r, **link_kw)
        design = _design([jaw_l, jaw_r])
        install(jd, design)
        monkeypatch.setattr(jd._write_guard, "app", _app("DocA"))
        monkeypatch.setattr(jd, "_driven_this_session", set())
        return jaw_l, jaw_r

    def test_the_clean_link_refusal_states_that_the_value_did_not_read(self, monkeypatch):
        self._xref_pair_with_a_blind_second_member(monkeypatch)
        assert payload(jd.handler(joint_name="Slider_JawL", distance=16))["driven"] is True
        res = jd.handler(joint_name="Slider_JawR", distance=-16)
        assert res["isError"] is True
        assert "the current value of 'Slider_JawR' did not read" in res["message"]
        assert "now reads" not in res["message"]
        # the rest of the refusal is unchanged - only the reading it does not have is withheld.
        assert "The link reads neither suppressed nor compute-failed" in res["message"]

    def test_the_unread_link_state_refusal_states_it_too(self, monkeypatch):
        # the second arm of the same refusal, worded separately, so it needs its own read.
        self._xref_pair_with_a_blind_second_member(monkeypatch, blind=True)
        assert payload(jd.handler(joint_name="Slider_JawL", distance=16))["driven"] is True
        res = jd.handler(joint_name="Slider_JawR", distance=-16)
        assert res["isError"] is True
        assert "stands on that unread state" in res["message"]
        assert "the current value of 'Slider_JawR' did not read" in res["message"]
        assert "now reads" not in res["message"]

    def test_a_value_that_DOES_read_still_states_the_reading(self, monkeypatch):
        # the discriminating twin: the clause is withheld only where the read answers nothing.
        jaw_l = FakeJoint("Slider_JawL", SliderJointMotion(),
                          occurrence_one=_occ(referenced=True), occurrence_two=_occ(referenced=True))
        jaw_r = FakeJoint("Slider_JawR", SliderJointMotion(),
                          occurrence_one=_occ(referenced=True), occurrence_two=_occ(referenced=True))
        link_pair(jaw_l, jaw_r)
        design = _design([jaw_l, jaw_r])
        install(jd, design)
        monkeypatch.setattr(jd._write_guard, "app", _app("DocA"))
        monkeypatch.setattr(jd, "_driven_this_session", set())
        assert payload(jd.handler(joint_name="Slider_JawL", distance=16))["driven"] is True
        res = jd.handler(joint_name="Slider_JawR", distance=-16)
        assert "'Slider_JawR' now reads 0.0 mm" in res["message"]
        # the value clause's own negative, verbatim - the sentence the two tests above pin. A bare
        # "did not read" would also match the refusal's occurrence-reading clause, which is about
        # the pair's nativeness and not about this joint's value at all.
        assert "the current value of 'Slider_JawR' did not read" not in res["message"]


class TestLinkCouplesTriState:
    """_link_couples over every combination of the two states - the gate both the claim and the
    refusal read, where False and None must never collapse into each other."""

    def _rec(self, **kw):
        rec = {"linked": True, "partner": "P", "link": "L", "suppressed": False, "broken": False,
               "value_self": 1.0, "value_partner": 1.0, "reversed": False}
        rec.update(kw)
        return rec

    def test_clean_on_both_couples(self):
        assert jd._link_couples(self._rec()) is True

    def test_suppressed_does_not_couple(self):
        assert jd._link_couples(self._rec(suppressed=True)) is False

    def test_broken_does_not_couple(self):
        assert jd._link_couples(self._rec(broken=True)) is False

    def test_an_unread_suppression_is_unknown_not_clean(self):
        assert jd._link_couples(self._rec(suppressed=None)) is None

    def test_an_unread_compute_state_is_unknown_not_clean(self):
        assert jd._link_couples(self._rec(broken=None)) is None

    def test_a_set_state_beats_an_unread_one(self):
        # suppressed True with the compute state unread is still provably dead - the refusal must
        # not stay armed on a link that reads suppressed just because the other half is silent.
        assert jd._link_couples(self._rec(suppressed=True, broken=None)) is False

    def test_no_link_does_not_couple(self):
        assert jd._link_couples(self._rec(linked=False, partner=None)) is False

    def test_an_unreadable_membership_is_unknown(self):
        assert jd._link_couples(self._rec(linked=None, partner=None)) is None


class TestEnabledLimitsText:
    """_limits_text publishes only the bounds a value must actually satisfy."""

    def test_a_disabled_bound_is_not_published(self):
        m = RevoluteJointMotion()
        m.rotationLimits = _MotionLimits(maximum=math.radians(120))
        assert jd._limits_text(m, "revolute") == "max 120.0 deg"

    def test_a_disabled_MAXIMUM_is_not_published_either(self):
        # the mirror of the case above, on the other bound: a readable maximumValue whose enable
        # flag is off constrains no value, so publishing it states a limit nothing enforces.
        m = RevoluteJointMotion()
        m.rotationLimits = _MotionLimits(minimum=0.0)
        assert jd._limits_text(m, "revolute") == "min 0.0 deg"

    def test_both_enabled_bounds_are_published(self):
        m = SliderJointMotion()
        m.slideLimits = _MotionLimits(minimum=0.0, maximum=2.5)
        assert jd._limits_text(m, "slider") == "min 0.0 mm, max 25.0 mm"

    def test_no_enabled_bound_reads_as_none(self):
        assert jd._limits_text(RevoluteJointMotion(), "revolute") is None

    def test_a_cylindrical_reports_both_degrees_of_freedom(self):
        m = CylindricalJointMotion()
        m.rotationLimits = _MotionLimits(maximum=math.radians(45))
        m.slideLimits = _MotionLimits(minimum=-1.0)
        assert jd._limits_text(m, "cylindrical") == "max 45.0 deg, min -10.0 mm"


# ── the value_now vs applied gate ────────────────────────────────────────────

# The census rows below are the shared conftest Occurrence fake (make_occurrence), so the members
# this tool's census reads - component, childOccurrences, fullPathName, isGroundToParent - are the
# ones swept against the live Occurrence shape in test_fake_shapes_exist.py.

def _census_occ(name, locked=False, children=(), path=None):
    """One occurrence as the DESIGN-WIDE census reads it. ``component`` must READ - a raise there is
    the census's unresolved-reference detector - ``children`` is the nested level the walk descends
    into, ``path`` is the unique label a lock is named by, and ``locked`` is the flag the census asks
    each row for."""
    return make_occurrence(path or name, component=MakeComp(name=name.split(":")[0]),
                           ground_to_parent=locked, children=children)


def _silent_lock_occ(name):
    """A census row the walk sees but whose LOCK FLAG raises - the occurrence was enumerated, its
    ground state was not. The fake is handed a FALSE flag it must never let through, so a census
    that read the value behind the raise reads as a free member rather than an unanswered one."""
    return make_occurrence(name, component=MakeComp(name=name), ground_to_parent=False,
                           raises_on={"isGroundToParent": "flag unavailable"})


def _broken_ref_occ(name):
    """An unresolved external reference: reading ``component`` RAISES - the ONE detector - and so
    does every other read but the name (measured). Its ground state is unknowable, never free - and
    the fake is handed a FALSE lock flag it must never let through, so a census that read the value
    behind the raise reads as a free member rather than an unanswered one."""
    return make_occurrence(name, raises="2 : InternalValidationError : occ",
                           ground_to_parent=False)


def _pathless_lock_occ(path):
    """A LOCKED census row whose fullPathName raises while the lock flag answers - the row the
    label's name fallback exists for. Its path and its leaf name DIFFER, so a label taken from the
    path is told apart from the fallback. Its component reads, so it is not an unresolved
    reference."""
    leaf = path.split("+")[-1]
    return make_occurrence(path, component=MakeComp(name=leaf.split(":")[0]),
                           ground_to_parent=True,
                           raises_on={"fullPathName": "path unreadable"})


def _unwalkable_occ(name):
    """A census row the walk reaches but cannot descend INTO: its childOccurrences will not
    enumerate, so whatever sits beneath it was never asked and the walk stops there incomplete. Its
    own component and lock flag read, so it is neither an unresolved reference nor a silent flag."""
    return make_occurrence(name, component=MakeComp(name=name), ground_to_parent=False,
                           raises_on={"childOccurrences": "children unavailable"})


def _census(design, occs):
    """Install `occs` as the root's own occurrence collection and make the FLAT walk decline, which
    is where the shared design-wide census starts descending through childOccurrences instead."""
    design.rootComponent.occurrences = _NamedCollection(occs)
    design.rootComponent.allOccurrences = _NamedCollection(raises=_UNREADABLE_WALK)


class TestDriveTookGate:
    def test_silently_ignored_drive_is_an_ERROR_listing_the_locked_member(self, monkeypatch):
        # a detected no-take is a FAILED drive: isError, never a success wearing a warning. The lock
        # is published as an OBSERVATION and, with no motion link on the joint, as a CANDIDATE cause
        # - never as a settled verdict: on a rig whose drive was held by a linked partner's enabled
        # limit, parent-locked members were present and releasing them changed nothing.
        j = FakeJoint("J", RevoluteJointMotion(stores=False))
        design = _install(j)
        _census(design, [_census_occ("Rotor:1", locked=True)])
        res = jd.handler(joint_name="J", angle_deg=25)
        assert res["isError"] is True
        assert "DID NOT TAKE" in res["message"]
        assert "Observed: ground_to_parent is SET on Rotor:1" in res["message"]
        assert "'J' is in no motion link" in res["message"]
        assert "CANDIDATE cause" in res["message"]
        assert "assembly_ground(ground_to_parent=false)" in res["message"]

    def test_a_lock_on_a_NESTED_occurrence_is_seen(self, monkeypatch):
        # ground_to_parent is not a top-level-only property - the lock that froze this chain sits on
        # an instance INSIDE a sub-assembly. A root-collection-only census reports such a design as
        # carrying no lock at all, which is the one reading that rules grounding out; the design-wide
        # walk reaches it, and the CANDIDATE branch downstream is what a caller acts on.
        j = FakeJoint("J", RevoluteJointMotion(stores=False))
        design = _install(j)
        nested = _census_occ("Rotor:1", locked=True, path="Gearbox:1+Rotor:1")
        _census(design, [_census_occ("Gearbox:1", children=[nested])])
        res = jd.handler(joint_name="J", angle_deg=25)
        assert res["isError"] is True
        assert "ground_to_parent is SET on Gearbox:1+Rotor:1" in res["message"]
        assert "no occurrence in the design reads ground_to_parent set" not in res["message"]
        assert "did not answer ground_to_parent" not in res["message"]   # both rows answered
        assert "CANDIDATE cause" in res["message"]

    def test_a_design_wide_lock_list_is_capped_with_the_remainder_disclosed(self, monkeypatch):
        # The census reaches every depth, so the locked list grows with the assembly - it goes
        # out through the ONE capped renderer, whose remainder is COUNTED. A bare join would put an
        # unbounded array in an error string; a silent slice would read as the complete set.
        j = FakeJoint("J", RevoluteJointMotion(stores=False))
        design = _install(j)
        _census(design, [_census_occ(f"Part{i}:1", locked=True) for i in range(12)])
        res = jd.handler(joint_name="J", angle_deg=25)
        assert res["isError"] is True
        assert "Part7:1, ... (+4 more not listed)" in res["message"]      # 8 named, 4 counted
        assert "Part8:1" not in res["message"]

    def test_an_unresolved_reference_is_counted_as_unanswered_not_free(self, monkeypatch):
        # A broken xref answers nothing - not its component, not its lock flag. Folding it in with
        # the free rows would let the receipt say no member is locked over a row never asked, so it
        # is counted among the unanswered and the census total still holds it.
        j = FakeJoint("J", RevoluteJointMotion(stores=False))
        design = _install(j)
        _census(design, [_census_occ("Ok:1"), _broken_ref_occ("Ghost:1")])
        res = jd.handler(joint_name="J", angle_deg=25)
        assert res["isError"] is True
        assert "1 of 2 occurrences did not answer ground_to_parent" in res["message"]
        assert "ground_to_parent is SET on" not in res["message"]
        assert "CANDIDATE" not in res["message"]

    def test_a_walk_that_stopped_early_publishes_no_design_wide_negative(self, monkeypatch):
        # The census is the walk's answer, not the design's: a node whose collection will not
        # enumerate leaves its whole subtree unasked, and a lock in there was never seen. Publishing
        # "no occurrence in the design reads ground_to_parent set" off that walk is the one reading
        # that rules grounding out, so the scope word follows the walk and the hole is disclosed.
        j = FakeJoint("J", RevoluteJointMotion(stores=False))
        design = _install(j)
        _census(design, [_unwalkable_occ("Gearbox:1")])
        res = jd.handler(joint_name="J", angle_deg=25)
        assert res["isError"] is True
        assert "no occurrence in the design reads ground_to_parent set" not in res["message"]
        assert "no occurrence the walk reached reads ground_to_parent set" in res["message"]
        assert "did not run to the end" in res["message"]
        assert "CANDIDATE" not in res["message"]

    def test_a_complete_walk_discloses_no_hole(self, monkeypatch):
        # The other side of that flag: a walk that ran to the end must NOT hedge its own reading,
        # or the disclosure appears on every receipt and stops meaning anything.
        j = FakeJoint("J", RevoluteJointMotion(stores=False))
        design = _install(j)
        _census(design, [_census_occ("Gearbox:1", children=[_census_occ("Rotor:1")])])
        res = jd.handler(joint_name="J", angle_deg=25)
        assert res["isError"] is True
        assert "no occurrence in the design reads ground_to_parent set" in res["message"]
        assert "did not run to the end" not in res["message"]

    def test_a_locked_row_whose_PATH_does_not_read_is_named_by_its_name(self, monkeypatch):
        # fullPathName is the label because a nested leaf name is shared by every instance of its
        # component - but a row that answers the lock flag and not the path still has to be NAMED,
        # or the one member the caller must go release reads as '(unnamed occurrence)'. The row's
        # path and leaf name differ, so the fallback is told apart from a path that did read.
        j = FakeJoint("J", RevoluteJointMotion(stores=False))
        design = _install(j)
        _census(design, [_pathless_lock_occ("Gearbox:1+Rotor:1")])
        res = jd.handler(joint_name="J", angle_deg=25)
        assert res["isError"] is True
        assert "ground_to_parent is SET on Rotor:1" in res["message"]
        assert "(unnamed occurrence)" not in res["message"]
        assert "CANDIDATE" in res["message"]

    def test_gate_elects_nothing_when_no_member_is_locked(self, monkeypatch):
        j = FakeJoint("J", RevoluteJointMotion(stores=False))
        design = _install(j)
        _census(design, [_census_occ("Rotor:1")])
        res = jd.handler(joint_name="J", angle_deg=25)
        assert res["isError"] is True
        assert "no occurrence in the design reads ground_to_parent set" in res["message"]
        # every row ANSWERED, so there is nothing to disclose beside that sentence
        assert "did not answer ground_to_parent" not in res["message"]
        assert "do not single out a cause" in res["message"]
        assert "CANDIDATE" not in res["message"]

    def test_an_occurrence_census_that_did_not_read_is_not_reported_as_nothing_locked(
            self, monkeypatch):
        # "no member is ground_to_parent set" is a claim about occurrences that were READ. A root
        # component whose collection raises supports no such claim, and publishing one would let a
        # receipt rule grounding out over a census it never took.
        j = FakeJoint("J", RevoluteJointMotion(stores=False))
        design = _install(j)

        design.rootComponent.occurrences = _NamedCollection(raises="occurrences unavailable")
        design.rootComponent.allOccurrences = _NamedCollection(raises=_UNREADABLE_WALK)
        res = jd.handler(joint_name="J", angle_deg=25)
        assert res["isError"] is True
        assert "the design's occurrence census did not read" in res["message"]
        assert "no occurrence in the design reads ground_to_parent set" not in res["message"]
        assert "CANDIDATE" not in res["message"]

    def test_an_occurrence_whose_lock_flag_does_not_answer_is_DISCLOSED(self, monkeypatch):
        # the census read but the flag did not: safe(read, False) would fold that row in with the
        # free ones, so the count of unanswered rows is published beside the verdict.
        j = FakeJoint("J", RevoluteJointMotion(stores=False))
        design = _install(j)
        _census(design, [_silent_lock_occ("Rotor:1")])
        res = jd.handler(joint_name="J", angle_deg=25)
        assert res["isError"] is True
        assert "1 of 1 occurrences did not answer ground_to_parent" in res["message"]
        assert "ground_to_parent is SET on" not in res["message"]
        assert "CANDIDATE" not in res["message"]

    def test_an_unreadable_link_membership_is_not_reported_as_no_link(self, monkeypatch):
        # 'could not be asked' must not license the grounding candidate: the joint may well be in a
        # link whose partner limit is what held the drive.
        j = _blind_link_joint("J", RevoluteJointMotion(stores=False))
        design = _install(j)
        _census(design, [_census_occ("Rotor:1", locked=True)])
        res = jd.handler(joint_name="J", angle_deg=25)
        assert res["isError"] is True
        assert "motion-link membership of 'J' did not read" in res["message"]
        assert "ground_to_parent is SET on Rotor:1" in res["message"]     # still an observation
        assert "CANDIDATE" not in res["message"]                          # but no cause elected

    def test_a_within_limits_no_take_is_still_an_ERROR(self, monkeypatch):
        # enabled limits must not excuse the verify gate: a command INSIDE the limits that the
        # chain silently ignores is a no-take like any other.
        j = FakeJoint("J", RevoluteJointMotion(stores=False))
        j.jointMotion.rotationLimits = _MotionLimits(minimum=math.radians(-50),
                                                     maximum=math.radians(50))
        _install(j)
        res = jd.handler(joint_name="J", angle_deg=45)                     # inside +/-50
        assert res["isError"] is True and "DID NOT TAKE" in res["message"]

    def test_a_cylindrical_partial_drive_names_what_landed(self, monkeypatch):
        # rotation lands, slide is silently ignored: the receipt must say the mechanism MOVED
        # and which value landed - a blanket frozen-chain diagnosis contradicts the observed
        # rotation.
        j = FakeJoint("Cyl", _cylindrical_frozen_slide())
        _install(j)
        res = jd.handler(joint_name="Cyl", angle_deg=30, distance=50, units="mm")
        assert res["isError"] is True
        assert "PARTIAL" in res["message"] and "angle landed at 30.0 deg" in res["message"]
        assert "slide 0.0 mm vs commanded 50.0 mm" in res["message"]
        assert "parent-locked" not in res["message"]

    def test_a_drive_that_took_is_not_flagged(self, monkeypatch):
        j = FakeJoint("J", RevoluteJointMotion())
        _install(j)
        out = payload(jd.handler(joint_name="J", angle_deg=25))
        assert "drive_took" not in out and abs(out["value_now"]["angle_deg"] - 25.0) < 1e-4


# ── a value that MOVED and missed the command vs one that never moved ───────
# A read-back that misses the command has two shapes behind it, and only the joint's own before/after
# pair tells them apart: a mechanism that turned and settled a fraction off the commanded value, and
# one that never moved. "DID NOT TAKE" describes the second; on the first it reads as a frozen chain.

def _settling_revolute(at_deg, step_deg=5.0):
    """A revolute starting at `at_deg` whose MECHANISM settles on the nearest position `step_deg`
    apart - it turns, and the value it settles on misses the command by more than the store grid the
    shared motion already lands on. A settling mechanism, not a second store grid."""
    class RevoluteJointMotion(_Revolute):           # the NAME is what current_joint_type keys on
        @property
        def rotationValue(self):
            return self._value

        @rotationValue.setter
        def rotationValue(self, v):
            self._value = math.radians(round(math.degrees(v) / step_deg) * step_deg)
    return RevoluteJointMotion(math.radians(at_deg))


def _settling_cylindrical_frozen_slide(at_deg=-11.6, step_deg=5.0):
    """A cylindrical joint whose ROTATION settles on a coarse position while its SLIDE lands
    nowhere. Two commanded values, two DIFFERENT answers and NEITHER landing: the shape that makes
    the verdict aggregate across values instead of reading one of them."""
    class CylindricalJointMotion(_Cylindrical):     # the NAME is what current_joint_type keys on
        @property
        def rotationValue(self):
            return self._rotation

        @rotationValue.setter
        def rotationValue(self, v):
            self._rotation = math.radians(round(math.degrees(v) / step_deg) * step_deg)

        @property
        def slideValue(self):
            return self._slide

        @slideValue.setter
        def slideValue(self, v):
            pass                                    # accepted, lands nowhere
    return CylindricalJointMotion(rotation=math.radians(at_deg))


def _revolute_offset_by(offset_deg, at_deg=0.0):
    """A revolute that stores the command displaced by `offset_deg` - the rig for the edge of the
    angle band, where the residual IS the number under test."""
    class RevoluteJointMotion(_Revolute):           # the NAME is what current_joint_type keys on
        @property
        def rotationValue(self):
            return self._value

        @rotationValue.setter
        def rotationValue(self, v):
            self._value = math.radians(round(math.degrees(v) + offset_deg, 4))
    return RevoluteJointMotion(math.radians(at_deg))


class TestTheAngleBandIsHalfTheStoreGrid:
    """rotationValue lands on a 0.1 deg grid, so an off-grid command reads back up to half a step
    away. That is a drive that LANDED, and the note names the grid it landed on."""

    def test_an_off_grid_command_lands_and_the_note_names_the_grid(self, monkeypatch):
        j = FakeJoint("Vane", RevoluteJointMotion())
        _install(j)
        out = payload(jd.handler(joint_name="Vane", angle_deg=-20.103))
        assert out["value_now"]["angle_deg"] == -20.1
        assert "-20.103 deg is off the 0.1 deg grid" in out["note"]
        assert "value_now reads -20.1 deg" in out["note"]

    def test_a_residual_just_inside_the_half_step_still_lands(self, monkeypatch):
        # just inside the widest a 0.1 deg store grid can miss by - the boundary the band is set at.
        j = FakeJoint("Vane", RevoluteJointMotion())
        _install(j)
        out = payload(jd.handler(joint_name="Vane", angle_deg=-20.149))
        assert abs(out["value_now"]["angle_deg"] + 20.149) <= jd._ANGLE_BAND_DEG

    def test_a_command_exactly_on_the_half_step_lands(self, monkeypatch):
        # The worst a 0.1 deg store can do: the command sits between two multiples, and whichever
        # one it takes is half a step away - as close as this grid gets. Which side it takes is not
        # asserted, only the distance, so this pins the band and not a rounding direction.
        j = FakeJoint("Vane", RevoluteJointMotion())
        _install(j)
        out = payload(jd.handler(joint_name="Vane", angle_deg=-20.15))
        assert out["driven"] is True
        assert abs(out["value_now"]["angle_deg"] + 20.15) == pytest.approx(jd._ANGLE_GRID_DEG / 2)

    def test_a_residual_past_the_band_is_still_a_failed_drive(self, monkeypatch):
        # 0.0501 deg is the first residual PAST the half-step: the receipt must still refuse, or the
        # float slack the tie case needs has become a wider band that swallows a genuine no-take.
        j = FakeJoint("Vane", _revolute_offset_by(0.0501))
        _install(j)
        res = jd.handler(joint_name="Vane", angle_deg=25)
        assert res["isError"] is True and "did NOT land the command" in res["message"]

    def test_an_on_grid_command_carries_no_grid_note(self, monkeypatch):
        # the discriminating twin: the grid sentence is for a command that could not be stored
        # verbatim, not for every drive.
        j = FakeJoint("Vane", RevoluteJointMotion())
        _install(j)
        out = payload(jd.handler(joint_name="Vane", angle_deg=-20.1))
        assert "grid" not in out["note"]


class TestValueMove:
    """_value_move over the two published numbers - the tri-state the verdict's wording branches on."""

    def test_a_change_of_one_published_step_is_a_move(self):
        moved, clause = jd._value_move(0.0, 0.0001, "deg")
        assert moved is True
        assert clause == "moved from 0.0 deg to 0.0001 deg (a change of 0.0001 deg)"

    def test_a_value_that_did_not_change_states_both_reads_and_the_unit(self):
        # the boundary's other side, over the pair shape the handler builds: two values already
        # rounded to the 4 decimals the receipt publishes, and equal. The WHOLE clause is pinned
        # rather than its opening words - the numbers and the unit are what a caller reads the
        # stillness off, and a substring assert leaves them free to say anything.
        moved, clause = jd._value_move(0.0, 0.0, "deg")
        assert moved is False
        assert clause == "did not move at all (before and after both read 0.0 deg)"

    def test_an_unread_before_value_is_neither_a_move_nor_a_stillness(self):
        # None is its own answer: it must not collapse into False, which would publish a stillness
        # nothing read.
        moved, clause = jd._value_move(None, 12.0, "mm")
        assert moved is None and "not known" in clause

    def test_a_move_states_both_values_and_the_difference(self):
        moved, clause = jd._value_move(-11.6, -20.1, "deg")
        assert moved is True
        assert clause == "moved from -11.6 deg to -20.1 deg (a change of -8.5 deg)"


class TestNearLandingIsNotAFrozenChain:
    def test_a_quantized_landing_reports_the_move_and_is_not_called_a_no_take(self, monkeypatch):
        # the joint turns 8.4 deg and settles 0.103 deg off the command - past the half-grid band,
        # so a failed drive (the commanded value is not what reads back), but not a mechanism that
        # never moved.
        j = FakeJoint("Vane", _settling_revolute(-11.6))
        _install(j)
        res = jd.handler(joint_name="Vane", angle_deg=-20.103)
        assert res["isError"] is True                       # the command did not land
        assert "DID NOT TAKE" not in res["message"]
        assert "MOVED the joint but did NOT land the command" in res["message"]
        assert "angle -20.0 deg vs commanded -20.103 deg (a residual of 0.103 deg)" in res["message"]
        assert ("the angle moved from -11.6 deg to -20.0 deg (a change of -8.4 deg)"
                in res["message"])

    def test_a_chain_that_never_moved_still_reads_as_a_no_take_and_says_it_stood_still(
            self, monkeypatch):
        # the discriminating twin of the case above: same gate, same error, and the wording that
        # names a frozen chain is kept for the drive that actually produced one.
        j = FakeJoint("J", RevoluteJointMotion(stores=False))
        _install(j)
        res = jd.handler(joint_name="J", angle_deg=25)
        assert res["isError"] is True and "DID NOT TAKE" in res["message"]
        assert "the angle did not move at all (before and after both read 0.0 deg)" in res["message"]

    def test_a_slider_that_moved_and_stopped_short_reports_the_move(self, monkeypatch):
        # the slide half of the same verdict, read off the pre-drive slideValue.
        class SliderJointMotion(_Slider):           # the NAME is what current_joint_type keys on
            """A rack that settles at 8 mm of the 10 mm commanded - it moved, and stopped short."""
            @property
            def slideValue(self):
                return self._value

            @slideValue.setter
            def slideValue(self, v):
                self._value = min(v, 0.8)
        j = FakeJoint("Rail", SliderJointMotion())
        _install(j)
        res = jd.handler(joint_name="Rail", distance=10, units="mm")
        assert res["isError"] is True and "DID NOT TAKE" not in res["message"]
        assert "slide 8.0 mm vs commanded 10.0 mm (a residual of -2.0 mm)" in res["message"]
        assert "the slide moved from 0.0 mm to 8.0 mm (a change of 8.0 mm)" in res["message"]

    def test_an_unreadable_pre_drive_value_leaves_the_move_unknown(self, monkeypatch):
        # the first rotationValue read raises, so there is no before-value to compare: the receipt
        # says the command did not land, and claims neither a move nor a frozen chain.
        class RevoluteJointMotion(_Revolute):       # the NAME is what current_joint_type keys on
            """A frozen revolute whose FIRST value read raises, so no pre-drive value is taken."""
            _reads = 0

            @property
            def rotationValue(self):
                self._reads += 1
                if self._reads == 1:
                    raise RuntimeError("transient read failure")
                return self._value

            @rotationValue.setter
            def rotationValue(self, v):
                pass
        j = FakeJoint("Crank", RevoluteJointMotion())
        _install(j)
        res = jd.handler(joint_name="Crank", angle_deg=25)
        assert res["isError"] is True and "DID NOT TAKE" not in res["message"]
        assert "did NOT land the command" in res["message"]
        assert "the angle has no readable pre-drive value here" in res["message"]

    def test_a_parent_lock_is_not_elected_as_the_cause_when_the_value_moved(self, monkeypatch):
        # the lock is still OBSERVED, but a value that moved across the drive is not the frozen
        # chain that candidate describes - electing it sends the caller to release a lock the
        # observed move already rules out.
        j = FakeJoint("Vane", _settling_revolute(-11.6))
        design = _install(j)
        _census(design, [_census_occ("Rotor:1", locked=True)])
        res = jd.handler(joint_name="Vane", angle_deg=-20.103)
        assert "ground_to_parent is SET on Rotor:1" in res["message"]      # still an observation
        assert "CANDIDATE" not in res["message"]
        assert "do not single out a cause" in res["message"]

    def test_a_partial_drive_states_what_the_missed_value_did(self, monkeypatch):
        # the PARTIAL branch carries the same distinction per value: the slide that missed says it
        # never moved, beside the angle that landed.
        j = FakeJoint("Cyl", _cylindrical_frozen_slide())
        _install(j)
        res = jd.handler(joint_name="Cyl", angle_deg=30, distance=50, units="mm")
        assert res["isError"] is True and "PARTIAL" in res["message"]
        assert "the slide did not move at all (before and after both read 0.0 mm)" in res["message"]
        assert "DID NOT TAKE" not in res["message"]

    def test_one_commanded_value_moving_and_the_other_frozen_is_not_a_no_take(self, monkeypatch):
        # BOTH commanded values miss, so nothing lands and the TOTAL-no-take verdict runs over two
        # answers at once: the angle turned 8.5 deg and stopped short, the slide never moved. One
        # value that moved is enough to rule out the frozen chain 'DID NOT TAKE' describes, and each
        # value publishes its own clause - reading the two the other way round would print a
        # frozen-chain headline beside a clause reporting 8.5 deg of motion.
        j = FakeJoint("Cyl", _settling_cylindrical_frozen_slide())
        _install(j)
        res = jd.handler(joint_name="Cyl", angle_deg=-20.103, distance=50, units="mm")
        assert res["isError"] is True
        assert "MOVED the joint but did NOT land the command" in res["message"]
        assert "DID NOT TAKE" not in res["message"]
        assert ("the angle moved from -11.6 deg to -20.0 deg (a change of -8.4 deg)"
                in res["message"])
        assert "the slide did not move at all (before and after both read 0.0 mm)" in res["message"]

    def test_a_parent_lock_is_not_elected_when_only_ONE_of_two_values_moved(self, monkeypatch):
        # the same two-value rig with a parent-locked member: the frozen-chain candidate is off the
        # table as soon as ANY commanded value moved. Aggregating the two answers the other way
        # would send the caller to release a lock the angle's own 8.5 deg already rules out.
        j = FakeJoint("Cyl", _settling_cylindrical_frozen_slide())
        design = _install(j)
        _census(design, [_census_occ("Rotor:1", locked=True)])
        res = jd.handler(joint_name="Cyl", angle_deg=-20.103, distance=50, units="mm")
        assert res["isError"] is True
        assert "ground_to_parent is SET on Rotor:1" in res["message"]       # still an observation
        assert "CANDIDATE" not in res["message"]
        assert "do not single out a cause" in res["message"]


class TestTheMissedCommandPathClaimsNothingAboutThePartner:
    """A missed-command receipt reports the DRIVEN joint's own value before and after; nothing on
    that path samples the motion-link partner, so neither verdict says the partner moved. A
    SUPPRESSED link is where such a claim reads as the contradiction it always was - the same
    message reports the suppression among its observations."""

    def _linked(self, monkeypatch, motion, suppressed=True):
        """`motion` on a joint named 'Driven', motion-linked to a 'Follower' revolute, in a fresh
        session registry."""
        driven = FakeJoint("Driven", motion)
        follower = FakeJoint("Follower", RevoluteJointMotion())
        link_pair(driven, follower, suppressed=suppressed)
        design = _design([driven, follower])
        install(jd, design)
        monkeypatch.setattr(jd._write_guard, "app", _app("DocA"))
        monkeypatch.setattr(jd, "_driven_this_session", set())

    def test_a_moved_no_take_on_a_suppressed_link_claims_no_partner_motion(self, monkeypatch):
        # the joint turned 8.4 deg and missed, so the MOVED headline runs - and the same message
        # reports the link SUPPRESSED a few clauses later. A headline claiming the partner came
        # along contradicts the observation printed beside it.
        self._linked(monkeypatch, _settling_revolute(-11.6))
        res = jd.handler(joint_name="Driven", angle_deg=-20.103)
        assert res["isError"] is True
        assert "MOVED the joint but did NOT land the command" in res["message"]
        assert "which reads SUPPRESSED" in res["message"]      # the link state the receipt DID read
        assert "motion-linked partner" not in res["message"]   # and no claim about what it carried

    def test_a_PARTIAL_drive_on_a_suppressed_link_claims_no_partner_motion(self, monkeypatch):
        # the PARTIAL branch reads the partner no more than the branch above does, and here there is
        # not even an observation block to hold the link's state - the receipt stays at the two
        # values it measured and the read that settles the rest.
        self._linked(monkeypatch, _cylindrical_frozen_slide())
        res = jd.handler(joint_name="Driven", angle_deg=30, distance=50, units="mm")
        assert res["isError"] is True
        assert "PARTIAL drive of 'Driven'" in res["message"]
        assert "angle landed at 30.0 deg" in res["message"]
        assert "motion-linked partner" not in res["message"]
        assert "Read the pose back with assembly_get" in res["message"]


class TestNoTakeCauseElection:
    """A total no-take publishes what it READ and names a cause only where those reads prove one.
    The rack/pinion shape: a 10 mm rack command that does not land while the pinion it is linked to
    would have to pass its own enabled limit - releasing the chassis lock changes nothing there, so
    the lock is an observation, and the partner's limit is published as what the link's RECORDED
    ratio implies rather than as a coupling the receipt watched happen."""

    def _rack_and_pinion(self, monkeypatch, per_cm_rad, pinion_max_deg=120.0, pinion_at_deg=0.0,
                         reversed_link=False, locked="Chassis:1", rack_at_cm=0.0):
        rack = FakeJoint("Rack", SliderJointMotion(rack_at_cm, stores=False))
        pinion_motion = RevoluteJointMotion(math.radians(pinion_at_deg),
                                            _MotionLimits(maximum=math.radians(pinion_max_deg)))
        pinion = FakeJoint("Pinion", pinion_motion)
        # the link's own ModelParameter values, in Fusion internal units: 1 cm of rack per
        # per_cm_rad radians of pinion, the ratio joint_motion_link writes as valueOne/valueTwo.
        link_pair(rack, pinion, value_one=1.0, value_two=per_cm_rad,
                  reversed_link=reversed_link)
        design = _design([rack, pinion])
        install(jd, design)
        monkeypatch.setattr(jd._write_guard, "app", _app("DocA"))
        monkeypatch.setattr(jd, "_driven_this_session", set())
        if locked:
            _census(design, [_census_occ(locked, locked=True)])
        return rack, pinion

    def test_the_partner_limit_is_named_and_grounding_is_not_blamed(self, monkeypatch):
        self._rack_and_pinion(monkeypatch, math.radians(200.0))     # 1 cm -> 200 deg
        res = jd.handler(joint_name="Rack", distance=10, units="mm")
        assert res["isError"] is True and "DID NOT TAKE" in res["message"]
        assert "200.0 deg is above the enabled maximum 120.0 deg" in res["message"]
        # the lock is an observation here, not the elected cause
        assert "ground_to_parent is SET on Chassis:1" in res["message"]
        assert "CANDIDATE" not in res["message"]

    def test_the_named_limit_is_stated_as_ARITHMETIC_not_as_an_observed_cause(self, monkeypatch):
        # nothing in this call reads what the link did to the partner: the implied value is computed
        # from the link's own recorded values. Stating it as the cause would send the agent to widen
        # a limit on strength of a prediction the receipt never measured, which is the parent-lock
        # verdict's defect wearing a new scapegoat - so the sentence names the arithmetic, says the
        # coupling was not observed, and points at the read that settles it.
        self._rack_and_pinion(monkeypatch, math.radians(200.0))
        msg = jd.handler(joint_name="Rack", distance=10, units="mm")["message"]
        assert "whose recorded values are 1.0 : " in msg
        assert "Applying that RATIO to this command implies a value" in msg
        assert "arithmetic on the values the link records, not a coupling this receipt observed" in msg
        assert "what the link did to 'Pinion' was not read here" in msg
        assert "Read 'Pinion' back with assembly_get to check it" in msg

    def test_the_partner_observation_carries_its_value_and_enabled_limits(self, monkeypatch):
        self._rack_and_pinion(monkeypatch, math.radians(200.0), pinion_at_deg=15.0)
        res = jd.handler(joint_name="Rack", distance=10, units="mm")
        assert "'Pinion' reads 15.0 deg, enabled limits max 120.0 deg" in res["message"]

    def test_a_partner_whose_value_does_not_read_publishes_no_reading(self, monkeypatch):
        # the third sentence that states a joint's current value. A partner DOF answering nothing
        # publishes no number - and the implied-value clause drops with it, since the arithmetic has
        # no starting value to add the scaled command to.
        rack = FakeJoint("Rack", SliderJointMotion(stores=False))
        pinion = FakeJoint("Pinion", _blind_revolute())
        link_pair(rack, pinion, value_one=1.0, value_two=math.radians(200.0))
        design = _design([rack, pinion])
        install(jd, design)
        monkeypatch.setattr(jd._write_guard, "app", _app("DocA"))
        monkeypatch.setattr(jd, "_driven_this_session", set())
        res = jd.handler(joint_name="Rack", distance=10, units="mm")
        assert res["isError"] is True and "DID NOT TAKE" in res["message"]
        assert "the current value of 'Pinion' did not read, with no enabled limits" in res["message"]
        assert "'Pinion' reads" not in res["message"]
        assert "do not single out a cause" in res["message"]

    def test_a_reversed_link_scales_the_other_way(self, monkeypatch):
        # reversed sends the pinion the other way, so the same command lands below a MINIMUM instead
        # - the sign is read off the link, never assumed.
        rack = FakeJoint("Rack", SliderJointMotion(stores=False))
        pm = RevoluteJointMotion()
        pm.rotationLimits = _MotionLimits(minimum=math.radians(-30.0))
        pinion = FakeJoint("Pinion", pm)
        link_pair(rack, pinion, value_one=1.0, value_two=math.radians(200.0), reversed_link=True)
        design = _design([rack, pinion])
        install(jd, design)
        monkeypatch.setattr(jd._write_guard, "app", _app("DocA"))
        monkeypatch.setattr(jd, "_driven_this_session", set())
        res = jd.handler(joint_name="Rack", distance=10, units="mm")
        assert "-200.0 deg is below the enabled minimum -30.0 deg" in res["message"]

    def test_an_implied_value_exactly_AT_the_bound_names_no_limit(self, monkeypatch):
        # the boundary: only a value STRICTLY beyond an enabled bound is one the limits exclude, the
        # same rule a directly commanded value is judged by.
        rack = FakeJoint("Rack", SliderJointMotion(stores=False))
        pm = RevoluteJointMotion()
        pm.rotationLimits = _MotionLimits(maximum=2.0)       # radians, exactly
        pinion = FakeJoint("Pinion", pm)
        link_pair(rack, pinion, value_one=1.0, value_two=2.0)       # 1 cm -> exactly 2.0 rad
        design = _design([rack, pinion])
        install(jd, design)
        monkeypatch.setattr(jd._write_guard, "app", _app("DocA"))
        monkeypatch.setattr(jd, "_driven_this_session", set())
        res = jd.handler(joint_name="Rack", distance=10, units="mm")
        assert res["isError"] is True and "DID NOT TAKE" in res["message"]
        assert "enabled maximum" not in res["message"]
        assert "do not single out a cause" in res["message"]

    def test_an_implied_value_one_step_past_the_bound_names_the_limit(self, monkeypatch):
        # the twin of the boundary above, one representable step out.
        rack = FakeJoint("Rack", SliderJointMotion(stores=False))
        pm = RevoluteJointMotion()
        pm.rotationLimits = _MotionLimits(maximum=2.0)
        pinion = FakeJoint("Pinion", pm)
        link_pair(rack, pinion, value_one=1.0, value_two=2.000001)
        design = _design([rack, pinion])
        install(jd, design)
        monkeypatch.setattr(jd._write_guard, "app", _app("DocA"))
        monkeypatch.setattr(jd, "_driven_this_session", set())
        res = jd.handler(joint_name="Rack", distance=10, units="mm")
        assert "is above the enabled maximum" in res["message"]

    def test_the_commanded_CHANGE_is_what_is_scaled_not_the_absolute_value(self, monkeypatch):
        # the rack already sits at 9 mm, so the command asks for 1 mm of travel - 20 deg of pinion,
        # well inside its 120 deg. Scaling the command's ABSOLUTE value instead would imply 200 deg
        # and name a limit the command never asks for.
        self._rack_and_pinion(monkeypatch, math.radians(200.0), rack_at_cm=0.9)
        res = jd.handler(joint_name="Rack", distance=10, units="mm")
        assert res["isError"] is True and "DID NOT TAKE" in res["message"]
        assert "enabled maximum" not in res["message"]
        assert "do not single out a cause" in res["message"]

    def test_a_change_from_that_same_pose_that_DOES_cross_the_bound_names_the_limit(self, monkeypatch):
        # the discriminating twin: same 1 mm of travel, a pinion with only 15 deg to give. The
        # verdict turns on the CHANGE, so the same starting pose answers both ways.
        self._rack_and_pinion(monkeypatch, math.radians(200.0), pinion_max_deg=15.0,
                              rack_at_cm=0.9)
        res = jd.handler(joint_name="Rack", distance=10, units="mm")
        assert "20.0 deg is above the enabled maximum 15.0 deg" in res["message"]

    def test_the_partners_OWN_current_value_is_part_of_the_implied_value(self, monkeypatch):
        # the pinion sits at 110 deg of its 120: the command's own 12 deg of coupled travel fits
        # inside the limit, and only the value the partner ALREADY holds carries the total past it.
        self._rack_and_pinion(monkeypatch, math.radians(200.0), pinion_at_deg=110.0)
        res = jd.handler(joint_name="Rack", distance=0.6, units="mm")
        assert "122.0 deg is above the enabled maximum 120.0 deg" in res["message"]

    def test_the_same_command_against_a_partner_at_zero_names_no_limit(self, monkeypatch):
        # the twin of the test above: only the partner's starting value differs, so it alone is
        # what decides. An implied value computed from the command alone reports both the same way.
        self._rack_and_pinion(monkeypatch, math.radians(200.0), pinion_at_deg=0.0)
        res = jd.handler(joint_name="Rack", distance=0.6, units="mm")
        assert "enabled maximum" not in res["message"]
        assert "do not single out a cause" in res["message"]

    def test_a_link_whose_own_end_reads_a_ZERO_value_names_no_limit_and_does_not_raise(
            self, monkeypatch):
        # a zero on this joint's end of the link is no ratio at all, and dividing by it raises out
        # of a WRITE tool that has already attempted its mutation. The guard withholds the
        # arithmetic; the receipt stays at what it read.
        rack, _pinion = self._rack_and_pinion(monkeypatch, math.radians(200.0))
        rack.motionLinks.item(0).valueOne = types.SimpleNamespace(value=0.0)
        res = jd.handler(joint_name="Rack", distance=10, units="mm")
        assert res["isError"] is True and "DID NOT TAKE" in res["message"]
        assert "enabled maximum" not in res["message"]
        assert "do not single out a cause" in res["message"]

    def test_a_suppressed_link_names_no_partner_limit(self, monkeypatch):
        # the same numbers that name the limit above, on a link that couples nothing: a state that
        # transmits no motion cannot be what held the drive.
        rack = FakeJoint("Rack", SliderJointMotion(stores=False))
        pm = RevoluteJointMotion()
        pm.rotationLimits = _MotionLimits(maximum=math.radians(120.0))
        pinion = FakeJoint("Pinion", pm)
        link_pair(rack, pinion, value_one=1.0, value_two=math.radians(200.0), suppressed=True)
        design = _design([rack, pinion])
        install(jd, design)
        monkeypatch.setattr(jd._write_guard, "app", _app("DocA"))
        monkeypatch.setattr(jd, "_driven_this_session", set())
        res = jd.handler(joint_name="Rack", distance=10, units="mm")
        assert "enabled maximum" not in res["message"]
        assert "which reads SUPPRESSED" in res["message"]
        assert "do not single out a cause" in res["message"]

    def test_an_unreadable_link_ratio_names_no_partner_limit(self, monkeypatch):
        # the arithmetic needs every number: a link whose values do not read proves nothing, so the
        # receipt stays at observations rather than scaling by a ratio it does not have.
        rack, _p = self._rack_and_pinion(monkeypatch, math.radians(200.0))
        del rack.motionLinks.item(0).valueTwo
        res = jd.handler(joint_name="Rack", distance=10, units="mm")
        assert "enabled maximum" not in res["message"]
        assert "do not single out a cause" in res["message"]

    def test_a_cylindrical_partner_gets_no_scaled_claim(self, monkeypatch):
        # two DOF on the partner: which one the link couples is not established by its type, so no
        # value is scaled onto it.
        rack = FakeJoint("Rack", SliderJointMotion(stores=False))
        cyl = CylindricalJointMotion()
        cyl.rotationLimits = _MotionLimits(maximum=math.radians(120.0))
        partner = FakeJoint("Spindle", cyl)
        link_pair(rack, partner, value_one=1.0, value_two=math.radians(200.0))
        design = _design([rack, partner])
        install(jd, design)
        monkeypatch.setattr(jd._write_guard, "app", _app("DocA"))
        monkeypatch.setattr(jd, "_driven_this_session", set())
        res = jd.handler(joint_name="Rack", distance=10, units="mm")
        assert "enabled maximum" not in res["message"]
        assert "'Spindle' reads 0.0 deg, 0.0 mm" in res["message"]     # still observed

    def test_the_partner_side_of_the_link_is_read_from_the_right_end(self, monkeypatch):
        # the driven joint is jointTWO here, so value_self is valueTwo and value_partner valueOne -
        # swapping them would scale by the reciprocal and name the wrong verdict.
        pm = RevoluteJointMotion()
        pm.rotationLimits = _MotionLimits(maximum=math.radians(120.0))
        pinion = FakeJoint("Pinion", pm)
        rack = FakeJoint("Rack", SliderJointMotion(stores=False))
        link_pair(pinion, rack, value_one=math.radians(200.0), value_two=1.0)   # rack is jointTwo
        design = _design([pinion, rack])
        install(jd, design)
        monkeypatch.setattr(jd._write_guard, "app", _app("DocA"))
        monkeypatch.setattr(jd, "_driven_this_session", set())
        res = jd.handler(joint_name="Rack", distance=10, units="mm")
        assert "200.0 deg is above the enabled maximum 120.0 deg" in res["message"]


# ── equivalent-pose semantics: a stored angle can carry full turns the command does not ──
# A joint whose stored value reads 720 deg, commanded to 0 deg, is ALREADY at the commanded physical
# pose. That is an equivalent pose, never a failed drive.

def _frozen_revolute_at(deg):
    """A revolute pinned at `deg` that keeps NO assignment, so the read-back keeps a full-turn count
    the command did not carry."""
    return RevoluteJointMotion(math.radians(deg), stores=False)


class TestEquivalentPose:
    def test_zero_command_at_720_is_equivalent_not_failed(self, monkeypatch):
        j = FakeJoint("Crank", _frozen_revolute_at(720))
        _install(j)
        out = payload(jd.handler(joint_name="Crank", angle_deg=0))
        assert out["equivalent_pose"] is True
        assert "drive_took" not in out                       # NOT a failed drive
        assert "DID NOT TAKE" not in out["note"]             # no grounded-chain blame either
        assert "modulo 360" in out["note"]
        assert out["value_now"]["angle_deg"] == 720.0        # the full-turn value, reported honestly
        assert out["value_now"]["angle_deg_normalized"] == 0.0

    def test_full_turn_command_at_zero_is_equivalent(self, monkeypatch):
        # the wrap-around direction: at 0 deg, commanding 360 deg is the same pose (d == 360).
        j = FakeJoint("Crank", _frozen_revolute_at(0))
        _install(j)
        out = payload(jd.handler(joint_name="Crank", angle_deg=360))
        assert out["equivalent_pose"] is True and "drive_took" not in out
        # the note carries the way forward, and claims no turn count: value_now reads 0.0 deg here
        assert "not a multiple of 360 deg" in out["note"]
        assert "full-turn count" not in out["note"]

    def test_float_drift_just_under_a_full_turn_is_equivalent(self, monkeypatch):
        # 719.9995 deg commanded to 0: the raw difference is 359.9995, so only the 360-d half of
        # min(d, 360-d) recognizes the equivalence - a plain d <= tol test would call this pose a
        # failed drive, on exactly the value float drift produces.
        j = FakeJoint("Crank", _frozen_revolute_at(719.9995))
        _install(j)
        out = payload(jd.handler(joint_name="Crank", angle_deg=0))
        assert out["equivalent_pose"] is True
        assert "drive_took" not in out and "DID NOT TAKE" not in out["note"]

    def test_genuine_no_take_is_still_flagged(self, monkeypatch):
        # 25 deg commanded against a chain frozen at 0 is NOT equivalent - the failure diagnosis
        # must survive the modulo test, and a no-take is an ERROR.
        j = FakeJoint("Crank", _frozen_revolute_at(0))
        _install(j)
        res = jd.handler(joint_name="Crank", angle_deg=25)
        assert res["isError"] is True and "DID NOT TAKE" in res["message"]

    def test_a_multi_turn_readback_carries_its_normalized_twin(self, monkeypatch):
        # a drive that TOOK to 450 deg reads back both forms: 450 as stored, 90 normalized.
        j = FakeJoint("Crank", RevoluteJointMotion())
        _install(j)
        out = payload(jd.handler(joint_name="Crank", angle_deg=450))
        assert out["value_now"]["angle_deg"] == 450.0
        assert out["value_now"]["angle_deg_normalized"] == 90.0
        assert "equivalent_pose" not in out                  # it moved; nothing to explain

    def test_negative_angle_normalizes_into_0_360(self, monkeypatch):
        j = FakeJoint("Crank", RevoluteJointMotion())
        _install(j)
        out = payload(jd.handler(joint_name="Crank", angle_deg=-30))
        assert out["value_now"]["angle_deg"] == -30.0
        assert out["value_now"]["angle_deg_normalized"] == 330.0

    def test_in_range_readback_has_no_normalized_twin(self, monkeypatch):
        # 25 deg IS its own normalized form - the twin would be noise.
        j = FakeJoint("Crank", RevoluteJointMotion())
        _install(j)
        out = payload(jd.handler(joint_name="Crank", angle_deg=25))
        assert "angle_deg_normalized" not in out["value_now"]

    def test_a_moving_drive_that_lands_pose_equivalent_reports_the_move(self, monkeypatch):
        # a joint that answers a command by turning BY the delta reads back a value that changed
        # (2160 -> 2250) while matching the command modulo 360: the receipt must report the move,
        # never "the pose already matches", which claims nothing moved.
        class RevoluteJointMotion(_Revolute):       # the NAME is what current_joint_type keys on
            """A revolute that answers a command by turning BY the normalized delta."""
            @property
            def rotationValue(self):
                return self._value

            @rotationValue.setter
            def rotationValue(self, v):
                self._value += (v - self._value) % (2 * math.pi)
        j = FakeJoint("Crank", RevoluteJointMotion(math.radians(2160)))
        _install(j)
        out = payload(jd.handler(joint_name="Crank", angle_deg=90))
        assert "equivalent_pose" not in out and "drive_took" not in out
        assert out["value_now"]["angle_deg"] == 2250.0
        assert out["value_now"]["angle_deg_normalized"] == 90.0
        assert "moved the mechanism" in out["note"]

    def test_unreadable_before_value_keeps_the_claim_hedged(self, monkeypatch):
        # the pre-drive read failing means moved-vs-not is unknowable: the receipt says the pose
        # is equivalent AND that whether the mechanism moved is not known - never the confident
        # "nothing needed to move".
        class RevoluteJointMotion(_Revolute):       # the NAME is what current_joint_type keys on
            """A revolute pinned at 720 deg whose FIRST value read raises."""
            _reads = 0

            @property
            def rotationValue(self):
                self._reads += 1
                if self._reads == 1:
                    raise RuntimeError("transient read failure")
                return self._value

            @rotationValue.setter
            def rotationValue(self, v):
                pass
        j = FakeJoint("Crank", RevoluteJointMotion(math.radians(720)))
        _install(j)
        out = payload(jd.handler(joint_name="Crank", angle_deg=0))
        assert out["equivalent_pose"] is True
        assert "not known from this receipt" in out["note"]
        assert "nothing needed to move" not in out["note"]


# ── which member the drive displaced ────────────────────────────────────────
# The drive reports the member whose placement actually changed, sampled either side of the
# assignment - an observation, never a prediction from the joint's member ordering.

class TestMovedMember:
    def test_the_first_member_is_named_when_it_is_the_one_that_moves(self, monkeypatch):
        motion = SliderJointMotion()
        arm = _DrivenOccurrence("Arm:1", motion, _slides((1.0, 0.0, 0.0)))
        base = _DrivenOccurrence("Base:1", motion, _fixed)
        j = FakeJoint("Rail", motion, occurrence_one=arm, occurrence_two=base)
        _install(j)
        out = payload(jd.handler(joint_name="Rail", distance=50, units="mm"))
        assert out["moved"]["occurrence"] == "Arm:1"
        assert out["moved"]["delta_mm"] == [50.0, 0.0, 0.0]
        assert "also_moved" not in out

    def test_the_second_member_is_named_when_it_is_the_one_that_moves(self, monkeypatch):
        # the anchored-first-side case: the same joint, the same command, the OTHER part displaced -
        # naming occurrenceOne from the member ordering alone would report the wrong part here.
        motion = SliderJointMotion()
        arm = _DrivenOccurrence("Arm:1", motion, _fixed)
        base = _DrivenOccurrence("Base:1", motion, _slides((-1.0, 0.0, 0.0)))
        j = FakeJoint("Rail", motion, occurrence_one=arm, occurrence_two=base)
        _install(j)
        out = payload(jd.handler(joint_name="Rail", distance=50, units="mm"))
        assert out["moved"]["occurrence"] == "Base:1"
        assert out["moved"]["delta_mm"] == [-50.0, 0.0, 0.0]

    def test_both_members_moving_publishes_the_larger_first(self, monkeypatch):
        motion = SliderJointMotion()
        arm = _DrivenOccurrence("Arm:1", motion, _slides((0.2, 0.0, 0.0)))
        base = _DrivenOccurrence("Base:1", motion, _slides((-1.0, 0.0, 0.0)))
        j = FakeJoint("Rail", motion, occurrence_one=arm, occurrence_two=base)
        _install(j)
        out = payload(jd.handler(joint_name="Rail", distance=10, units="mm"))
        assert out["moved"]["occurrence"] == "Base:1"          # -10 mm, the larger move
        assert out["also_moved"]["occurrence"] == "Arm:1"      # +2 mm
        assert out["also_moved"]["delta_mm"] == [2.0, 0.0, 0.0]

    def test_a_rotation_is_reported_in_degrees(self, monkeypatch):
        motion = RevoluteJointMotion()
        rotor = _DrivenOccurrence("Rotor:1", motion, _spins)
        base = _DrivenOccurrence("Base:1", motion, _fixed)
        j = FakeJoint("Pivot", motion, occurrence_one=rotor, occurrence_two=base)
        _install(j)
        out = payload(jd.handler(joint_name="Pivot", angle_deg=90))
        assert out["moved"]["occurrence"] == "Rotor:1"
        assert out["moved"]["delta_deg"] == 90.0
        assert out["moved"]["delta_mm"] == [0.0, 0.0, 0.0]     # a spin in place moves no origin

    def test_a_drive_that_displaced_nothing_publishes_moved_null(self, monkeypatch):
        # both placements READ and both unchanged: the honest answer is "nothing moved", which is a
        # different statement from "the placement could not be measured".
        motion = SliderJointMotion()
        arm = _DrivenOccurrence("Arm:1", motion, _fixed)
        base = _DrivenOccurrence("Base:1", motion, _fixed)
        j = FakeJoint("Rail", motion, occurrence_one=arm, occurrence_two=base)
        _install(j)
        out = payload(jd.handler(joint_name="Rail", distance=50, units="mm"))
        assert out["moved"] is None
        assert "neither member's placement changed" in out["note"]

    def test_an_unreadable_placement_publishes_no_moved_key_at_all(self, monkeypatch):
        # a null 'moved' would claim nothing moved; these occurrences carry no transform2 at all.
        j = FakeJoint("Rail", SliderJointMotion(), occurrence_one=_plain_occ(), occurrence_two=_plain_occ())
        _install(j)
        out = payload(jd.handler(joint_name="Rail", distance=50, units="mm"))
        assert "moved" not in out
        assert "neither member's placement could be read" in out["note"]

    def test_a_move_exactly_at_the_band_is_not_a_move(self, monkeypatch):
        # 0.001 mm is the band itself - only a move BEYOND it counts, or solver noise reads as motion.
        motion = SliderJointMotion()
        arm = _DrivenOccurrence("Arm:1", motion, _slides((1.0, 0.0, 0.0)))
        base = _DrivenOccurrence("Base:1", motion, _fixed)
        j = FakeJoint("Rail", motion, occurrence_one=arm, occurrence_two=base)
        _install(j)
        out = payload(jd.handler(joint_name="Rail", distance=0.001, units="mm"))
        assert out["moved"] is None

    def test_a_move_past_the_band_is_reported(self, monkeypatch):
        motion = SliderJointMotion()
        arm = _DrivenOccurrence("Arm:1", motion, _slides((1.0, 0.0, 0.0)))
        base = _DrivenOccurrence("Base:1", motion, _fixed)
        j = FakeJoint("Rail", motion, occurrence_one=arm, occurrence_two=base)
        _install(j)
        out = payload(jd.handler(joint_name="Rail", distance=0.002, units="mm"))
        assert out["moved"]["occurrence"] == "Arm:1"
        assert out["moved"]["delta_mm"] == [0.002, 0.0, 0.0]


class TestBodyCornerEvidence:
    """The placement rows are the transform's CLAIM; a member's own body corner is the EVIDENCE."""

    @staticmethod
    def _boxed(occ, corner_x):
        """Give `occ` a bodies-only box whose min corner sits at corner_x() cm along x."""
        occ.boundingBox2 = lambda _types: types.SimpleNamespace(
            minPoint=types.SimpleNamespace(x=corner_x(), y=0.0, z=0.0))
        return occ

    def test_a_slide_whose_placement_moved_but_body_stayed_is_an_error(self):
        motion = SliderJointMotion()
        arm = self._boxed(_DrivenOccurrence("Arm:1", motion, _slides((1.0, 0.0, 0.0))), lambda: 0.0)
        base = _DrivenOccurrence("Base:1", motion, _fixed)
        _install(FakeJoint("Rail", motion, occurrence_one=arm, occurrence_two=base))
        res = jd.handler(joint_name="Rail", distance=50, units="mm")
        assert res["isError"] is True
        assert "Arm:1" in res["message"] and "body geometry did not move" in res["message"]

    def test_a_slide_that_carried_its_body_publishes_how_far(self):
        motion = SliderJointMotion()
        arm = self._boxed(_DrivenOccurrence("Arm:1", motion, _slides((1.0, 0.0, 0.0))),
                          lambda: motion.slideValue)
        base = _DrivenOccurrence("Base:1", motion, _fixed)
        _install(FakeJoint("Rail", motion, occurrence_one=arm, occurrence_two=base))
        out = payload(jd.handler(joint_name="Rail", distance=50, units="mm"))
        assert out["moved"]["geometry_moved_mm"] == 50.0

    def test_a_spin_over_a_still_corner_is_evidence_not_an_error(self):
        # A body turning about its own axis keeps its box, so a rotation is never convicted on it.
        motion = RevoluteJointMotion()
        rotor = self._boxed(_DrivenOccurrence("Rotor:1", motion, _spins), lambda: 0.0)
        base = _DrivenOccurrence("Base:1", motion, _fixed)
        _install(FakeJoint("Pivot", motion, occurrence_one=rotor, occurrence_two=base))
        out = payload(jd.handler(joint_name="Pivot", angle_deg=90))
        assert out["moved"]["geometry_moved_mm"] == 0.0

    def test_a_member_whose_corner_does_not_read_carries_no_geometry_key(self):
        motion = SliderJointMotion()
        arm = _DrivenOccurrence("Arm:1", motion, _slides((1.0, 0.0, 0.0)))
        base = _DrivenOccurrence("Base:1", motion, _fixed)
        _install(FakeJoint("Rail", motion, occurrence_one=arm, occurrence_two=base))
        out = payload(jd.handler(joint_name="Rail", distance=50, units="mm"))
        assert "geometry_moved_mm" not in out["moved"]


class TestMovedBandBoundary:
    """The band gate itself, over crafted samples - a basis rounded the way the placement record
    rounds it cannot express a rotation this small, so the degree edge is exercised directly."""
    def _samples(self, monkeypatch, deg):
        c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
        before = {"origin": [0.0, 0.0, 0.0], "x_axis": [1.0, 0.0, 0.0],
                  "y_axis": [0.0, 1.0, 0.0], "z_axis": [0.0, 0.0, 1.0]}
        after = {"origin": [0.0, 0.0, 0.0], "x_axis": [c, s, 0.0],
                 "y_axis": [-s, c, 0.0], "z_axis": [0.0, 0.0, 1.0]}
        monkeypatch.setattr(jd, "_placement", lambda occ: after)
        return [(make_occurrence("Rotor:1"), before, None)]

    def test_a_rotation_exactly_at_the_band_is_not_a_move(self, monkeypatch):
        rows, readable = jd._moved_rows(self._samples(monkeypatch, 0.01))
        assert rows == [] and readable is True

    def test_a_rotation_past_the_band_is_a_move(self, monkeypatch):
        rows, readable = jd._moved_rows(self._samples(monkeypatch, 0.011))
        assert readable is True
        assert rows[0]["occurrence"] == "Rotor:1" and rows[0]["delta_deg"] == 0.011

    def test_a_rounded_basis_that_did_not_turn_reports_no_rotation(self, monkeypatch):
        # the axes the placement record publishes are rounded to 4 decimals and so fall short of unit
        # length; comparing them as-is turns that missing length into rotation (measured live: a part
        # that had not turned reported 0.5375 deg, which would name a static member as the mover).
        rounded = {"origin": [0.0, 0.0, 0.0], "x_axis": [0.866, 0.5, 0.0],
                   "y_axis": [-0.5, 0.866, 0.0], "z_axis": [0.0, 0.0, 1.0]}
        assert jd._delta_deg(rounded, rounded) == 0.0
        monkeypatch.setattr(jd, "_placement", lambda occ: dict(rounded))
        rows, readable = jd._moved_rows([(make_occurrence("Rotor:1"), rounded, None)])
        assert rows == [] and readable is True


class TestDriveDirection:
    def test_the_slide_direction_is_the_vector_read_BEFORE_the_drive(self, monkeypatch):
        # the drive itself can re-aim the vector; a receipt that re-read it afterwards would publish
        # a direction the reported delta was never measured against.
        class SliderJointMotion(_Slider):              # the NAME is what current_joint_type keys on
            """A slider that re-aims its own direction vector ON the drive."""
            @property
            def slideValue(self):
                return self._value

            @slideValue.setter
            def slideValue(self, v):
                _Slider.slideValue.fset(self, v)
                self.slideDirectionVector = FakeVector3D(0.0, 1.0, 0.0)
        motion = SliderJointMotion(direction_vector=FakeVector3D(1.0, 0.0, 0.0))
        arm = _DrivenOccurrence("Arm:1", motion, _slides((1.0, 0.0, 0.0)))
        base = _DrivenOccurrence("Base:1", motion, _fixed)
        j = FakeJoint("Rail", motion, occurrence_one=arm, occurrence_two=base)
        _install(j)
        out = payload(jd.handler(joint_name="Rail", distance=50, units="mm"))
        assert out["slide_direction"] == [1.0, 0.0, 0.0]
        assert "rotation_axis" not in out                  # no angle was commanded

    def test_an_angle_drive_publishes_the_rotation_axis(self, monkeypatch):
        motion = RevoluteJointMotion(axis_vector=FakeVector3D(0.0, 0.0, 1.0))
        rotor = _DrivenOccurrence("Rotor:1", motion, _spins)
        base = _DrivenOccurrence("Base:1", motion, _fixed)
        j = FakeJoint("Pivot", motion, occurrence_one=rotor, occurrence_two=base)
        _install(j)
        out = payload(jd.handler(joint_name="Pivot", angle_deg=30))
        assert out["rotation_axis"] == [0.0, 0.0, 1.0]
        assert "slide_direction" not in out

    def test_a_cylindrical_drive_publishes_the_rotation_axis_and_NO_slide_direction(self,
                                                                                   monkeypatch):
        # a cylindrical motion carries rotationAxisVector and NO slideDirectionVector at all, so the
        # slide half of a two-value drive has no heading to publish - reading one off this joint
        # states a direction the platform never reported.
        m = CylindricalJointMotion(axis_vector=FakeVector3D(0.0, 0.0, 1.0))
        assert not hasattr(m, "slideDirectionVector")
        j = FakeJoint("Cyl", m)
        _install(j)
        out = payload(jd.handler(joint_name="Cyl", angle_deg=30, distance=10, units="mm"))
        assert out["rotation_axis"] == [0.0, 0.0, 1.0]
        assert "slide_direction" not in out

    def test_an_unreadable_vector_publishes_no_direction(self, monkeypatch):
        # the stock SliderJointMotion fake carries no slideDirectionVector - an unread vector is
        # absent, never substituted with an axis the joint never reported.
        j = FakeJoint("Rail", SliderJointMotion())
        _install(j)
        out = payload(jd.handler(joint_name="Rail", distance=50, units="mm"))
        assert "slide_direction" not in out
        assert "motion vector" not in out["note"]


# ── the description makes no motion-link claim: the receipt answers it ───────

class TestTheDescriptionGatesItsMotionLinkClaims:
    """BOTH motion-link claims are conditional in the handler: the coupling claim is withheld for a
    link reading suppressed or compute-failed (_link_couples False), and the second-member refusal
    is armed only where that state is not False. The description is read before every call and
    cannot carry either condition, so it states neither - the receipt answers the coupling and the
    refusal names the partner, both pinned by TestLinkStateGating above."""

    def test_the_wire_makes_no_motion_link_claim_at_all(self):
        # a wire sentence of the "the link moves it" shape asserts the coupling for every link,
        # which is what the receipt refuses to say for a suppressed or compute-failed one.
        low = jd.TOOL_DESCRIPTION.lower()
        for claim in ("the link moves it", "motion link", "motion-link", "couple", "partner",
                      "second member"):
            assert claim not in low, claim
