"""Tests for `pmi_create` - the guards (one entity per note, faces-only hole notes, the closed
{symbol} vocabulary), the leader-extension pin, and the create-then-read-back composition."""

import json
from types import SimpleNamespace

import pytest

from conftest import (load_tool, error_message, BRepEdge, BRepFace, FakePMIAnnotations,
                      FakePMIHoleThreadNote, FakePMIHoleThreadNoteInput, FakePMIHoleThreadNotes,
                      FakePMILeaderLineNote, FakePMILeaderLineNoteInput, FakePMILeaderLineNotes,
                      MakeComp, MakeDesign)

pc = load_tool("pmi_create")


def _payload(result):
    assert result["isError"] is False, result
    return json.loads(result["content"][0]["text"])


def _face(comp):
    f = BRepFace(None)
    f.body = SimpleNamespace(parentComponent=comp)
    return f


@pytest.fixture
def rig(monkeypatch):
    """A component whose PMI collections are the shared fakes; geometry resolution is stubbed
    per-test. The notes hand out `_input` from createInput, record `_added` and answer `_note`."""
    ann = FakePMILeaderLineNote(name="Note1", text="DEBURR", segments=[])
    notes = FakePMILeaderLineNotes(note=ann, note_input=FakePMILeaderLineNoteInput())
    hole_ann = FakePMIHoleThreadNote(name="Hole Note1", text="QTY", segments=[])
    hole_notes = FakePMIHoleThreadNotes(note=hole_ann,
                                        note_input=FakePMIHoleThreadNoteInput())
    comp = MakeComp("Root")
    comp.pmiAnnotations = FakePMIAnnotations(leader_notes=notes, hole_notes=hole_notes)
    design = MakeDesign(comp=comp)
    monkeypatch.setattr(pc._common, "design", lambda: design)
    monkeypatch.setattr(pc._pmi, "segments_markup", lambda a: None)

    def stub_geometry(ents):
        monkeypatch.setattr(pc._GEOMETRY, "resolve", lambda raw: (ents, None))
    return SimpleNamespace(comp=comp, notes=notes, hole_notes=hole_notes, ann=ann,
                           hole_ann=hole_ann, stub_geometry=stub_geometry)


class TestNoteGuards:
    def test_note_takes_exactly_one_entity(self, rig):
        rig.stub_geometry([_face(rig.comp), _face(rig.comp)])
        msg = error_message(pc.handler(kind="note", geometry=["a", "b"], text="X"))
        assert "exactly ONE" in msg and "2" in msg

    def test_note_refuses_a_non_brep_entity(self, rig):
        rig.stub_geometry([SimpleNamespace()])
        assert "face/edge/vertex" in error_message(
            pc.handler(kind="note", geometry=["a"], text="X"))

    def test_unknown_symbol_token_is_refused_with_the_vocabulary(self, rig):
        rig.stub_geometry([_face(rig.comp)])
        msg = error_message(pc.handler(kind="note", geometry=["a"], text="{bogus}"))
        assert "'{bogus}'" in msg and "flatness" in msg

    def test_no_active_design_is_an_error(self, monkeypatch):
        monkeypatch.setattr(pc._common, "design", lambda: None)
        assert "No active design" in error_message(
            pc.handler(kind="note", geometry=["a"], text="X"))


class TestHoleNoteGuards:
    def test_hole_note_refuses_non_face_entities(self, rig):
        rig.stub_geometry([BRepEdge(None)])
        assert "FACE handles" in error_message(pc.handler(kind="hole_note", geometry=["a"]))

    def test_hole_note_appends_text_after_the_callout(self, rig):
        rig.stub_geometry([_face(rig.comp)])
        out = _payload(pc.handler(kind="hole_note", geometry=["a"], text=" REAM FINAL"))
        assert out["annotation"] == "Hole Note1"
        assert len(rig.hole_notes._note.segments) == 1     # appended to the (empty) fake callout

    def test_hole_note_refuses_note_only_inputs(self, rig):
        rig.stub_geometry([_face(rig.comp)])
        msg = error_message(pc.handler(kind="hole_note", geometry=["a"], plane="xy"))
        assert "kind='note' only" in msg

    def test_note_refuses_hole_only_inputs(self, rig):
        rig.stub_geometry([_face(rig.comp)])
        msg = error_message(pc.handler(kind="note", geometry=["a"], text="X",
                                       flags={"quantity_note": False}))
        assert "hole_note" in msg

    def test_unknown_flag_is_refused_with_the_vocabulary(self, rig):
        rig.stub_geometry([_face(rig.comp)])
        msg = error_message(pc.handler(kind="hole_note", geometry=["a"],
                                       flags={"bogus_flag": True}))
        assert "bogus_flag" in msg and "quantity_note" in msg

    def test_unknown_value_key_is_refused(self, rig):
        rig.stub_geometry([_face(rig.comp)])
        msg = error_message(pc.handler(kind="hole_note", geometry=["a"],
                                       values={"bogus": 5}))
        assert "bogus" in msg and "diameter" in msg


class TestToleranceSpec:
    def test_missing_type_is_refused(self):
        tol, err = pc._pmi.build_tolerance({}, 0.1)
        assert tol is None and "'type'" in err and "symmetric" in err

    def test_unknown_type_is_refused(self):
        tol, err = pc._pmi.build_tolerance({"type": "wonky"}, 0.1)
        assert tol is None and "wonky" in err

    def test_display_units_vocabulary_is_enforced(self):
        ds, err = pc._pmi.build_display({"units": "furlong"})
        assert ds is None and "must be one of" in err


class TestCreate:
    def test_note_created_and_read_back(self, rig):
        rig.stub_geometry([_face(rig.comp)])
        out = _payload(pc.handler(kind="note", geometry=["a"], text="{flatness}0.05"))
        assert out["annotation"] == "Note1" and out["kind"] == "note"
        assert rig.notes._added is rig.notes._input
        assert len(rig.notes._input.segments) == 2      # symbol + text landed on the input

    def test_low_input_extension_is_pinned_to_the_default(self, rig):
        rig.notes._input.leaderLineExtension = pc._pmi.LEADER_EXT_FLOOR / 2   # under the floor
        rig.stub_geometry([_face(rig.comp)])
        _payload(pc.handler(kind="note", geometry=["a"], text="X"))
        assert rig.notes._input.leaderLineExtension == pc._pmi.LEADER_EXT_DEFAULT

    def test_an_input_at_or_above_the_floor_is_left_alone(self, rig):
        rig.notes._input.leaderLineExtension = pc._pmi.LEADER_EXT_FLOOR
        rig.stub_geometry([_face(rig.comp)])
        _payload(pc.handler(kind="note", geometry=["a"], text="X"))
        assert rig.notes._input.leaderLineExtension == pc._pmi.LEADER_EXT_FLOOR

    def test_an_explicit_leader_extension_is_written_once_and_never_pre_pinned(self, rig):
        # The pin exists for the case the caller said nothing. When leader_extension IS given, the
        # caller's value is the only write - pinning first would put the default on the input for
        # a moment and make the payload's provenance a guess.
        writes = []

        class _Recording:
            segments = None

            def __setattr__(self, key, value):
                if key == "leaderLineExtension":
                    writes.append(value)
                object.__setattr__(self, key, value)

        rec = _Recording()
        object.__setattr__(rec, "leaderLineExtension", pc._pmi.LEADER_EXT_FLOOR / 2)
        rig.notes._input = rec
        rig.stub_geometry([_face(rig.comp)])
        _payload(pc.handler(kind="note", geometry=["a"], text="X",
                            leader_extension=8, units="mm"))          # 8 mm = 0.8 cm
        assert writes == [pytest.approx(0.8)]

    def test_an_explicit_extension_under_the_floor_is_refused_not_silently_pinned(self, rig):
        rig.stub_geometry([_face(rig.comp)])
        msg = error_message(pc.handler(kind="note", geometry=["a"], text="X",
                                       leader_extension=1, units="mm"))   # 1 mm < 2.5 mm
        assert "floor" in msg and rig.notes._added is None

    def test_null_add_is_an_error(self, rig):
        # the add that CREATES NOTHING: the input was taken and no annotation came back
        empty = FakePMILeaderLineNotes(add_result=None)
        rig.comp.pmiAnnotations.leaderLineNotes = empty
        rig.stub_geometry([_face(rig.comp)])
        assert "no annotation was created" in error_message(
            pc.handler(kind="note", geometry=["a"], text="X"))
        assert empty.count == 0 and empty._added is not None

    def test_hole_note_created(self, rig):
        rig.stub_geometry([_face(rig.comp)])
        out = _payload(pc.handler(kind="hole_note", geometry=["a"]))
        assert out["annotation"] == "Hole Note1" and out["kind"] == "hole_note"

    def test_rename_that_does_not_take_is_reported(self, rig):
        # a rename the platform SWALLOWS: the assignment is accepted and the name never moves
        rig.notes._note = FakePMILeaderLineNote(name="Note1", segments=[], swallows=["name"])
        rig.stub_geometry([_face(rig.comp)])
        out = _payload(pc.handler(kind="note", geometry=["a"], text="X", name="MyNote"))
        assert "did not take" in out["rename_warning"]

    def test_rename_applies(self, rig):
        rig.stub_geometry([_face(rig.comp)])
        out = _payload(pc.handler(kind="note", geometry=["a"], text="X", name="MyNote"))
        assert out["annotation"] == "MyNote" and "rename_warning" not in out

    def test_a_declined_set_annotation_plane_is_an_error_not_a_created_note(self, rig):
        # setAnnotationPlane returns a bool; a False that is not gated leaves the note on the
        # PLATFORM's plane while the tool reports the requested one.
        rig.notes._input = FakePMILeaderLineNoteInput(plane_ok=False)
        rig.stub_geometry([_face(rig.comp)])
        msg = error_message(pc.handler(kind="note", geometry=["a"], text="X", plane="xy"))
        assert "declined" in msg and "xy" in msg
        assert rig.notes._added is None                 # nothing was added

    def test_a_raising_set_annotation_plane_names_the_plane_the_cause_and_the_remedy(self, rig):
        rig.notes._input = FakePMILeaderLineNoteInput(plane_raises="needs an adjacent face")
        rig.stub_geometry([_face(rig.comp)])
        msg = error_message(pc.handler(kind="note", geometry=["a"], text="X", plane="face"))
        assert "setAnnotationPlane(face) failed" in msg and "adjacent face" in msg
        assert "plane_face" in msg                      # the remedy the wire no longer carries

    def test_an_accepted_plane_reaches_the_input_with_the_mapped_member(self, rig):
        rig.stub_geometry([_face(rig.comp)])
        _payload(pc.handler(kind="note", geometry=["a"], text="X", plane="xy"))
        import adsk.fusion
        assert rig.notes._input._plane_calls == [
            (adsk.fusion.LeaderLineNotePlaneTypes.PrincipalXYLeaderLineNotePlaneType,)]

    def test_a_hole_note_display_spec_goes_through_the_shared_writer(self, rig, monkeypatch):
        # pmi_create and pmi_edit run the SAME display writer - a second copy here is what drifts.
        seen = []
        rig.stub_geometry([_face(rig.comp)])
        monkeypatch.setattr(pc._pmi, "apply_display",
                            lambda obj, spec: seen.append((obj, spec)))
        _payload(pc.handler(kind="hole_note", geometry=["a"],
                            display={"precision": 2, "secondary": {"precision": 4}}))
        assert seen == [(rig.hole_ann, {"precision": 2, "secondary": {"precision": 4}})]

    def test_a_display_refusal_reports_the_annotation_as_created(self, rig):
        rig.stub_geometry([_face(rig.comp)])
        msg = error_message(pc.handler(kind="hole_note", geometry=["a"],
                                       display={"units": "furlong"}))
        assert "must be one of" in msg and "WAS created" in msg

    def test_bad_text_point_reports_but_names_the_created_annotation(self, rig):
        rig.stub_geometry([_face(rig.comp)])
        msg = error_message(pc.handler(kind="note", geometry=["a"], text="X",
                                       text_point=["x", 0, 0]))
        assert "text_point" in msg and "WAS created" in msg
