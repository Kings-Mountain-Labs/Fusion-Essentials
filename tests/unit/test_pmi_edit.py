"""Tests for `pmi_edit` - per-action read-back gating: a set that does not take is an error, an
imported annotation refuses text edits, and the below-floor leader extension is normalized before
a segment edit."""

import json
import math
from types import SimpleNamespace

import pytest

from conftest import (load_tool, error_message, FakePMIDisplaySettings,
                      FakePMIGeometricValueTolerance, FakePMIHoleThreadNote,
                      FakePMILeaderLineNote, MakeComp, make_pmi_value)

pe = load_tool("pmi_edit")


def _payload(result):
    assert result["isError"] is False, result
    return json.loads(result["content"][0]["text"])


class _FakeAnn(FakePMILeaderLineNote):
    """The leader note pmi_edit works on, seeded over the shared fake. leaderLineExtension starts
    at _pmi.LEADER_EXT_DEFAULT rather than a literal: the floor the tools refuse below is a tool
    constant, and the platform value behind it is UNMEASURED on this build (PMI authoring is
    extension-gated), so a hand-typed number here would encode a fact nothing measured."""

    def __init__(self, name="Note1", suffix="PMILeaderLineNote", out_of_date=False, **kwargs):
        kwargs.setdefault("leader_extension", pe._pmi.LEADER_EXT_DEFAULT)
        super().__init__(name=name, object_type="adsk::fusion::" + suffix,
                         out_of_date=out_of_date, **kwargs)


def _hole_ann(name="Hole Note1", **kwargs):
    """A hole/thread callout - the kind whose flags/values/display pmi_edit writes."""
    kwargs.setdefault("leader_extension", pe._pmi.LEADER_EXT_DEFAULT)
    return FakePMIHoleThreadNote(name=name, **kwargs)


class _RefusingSet(_FakeAnn):
    """An annotation whose named properties raise on every write - the shape the report-the-raise
    guards exist for. Keyword values seed plain attributes before the refusal is armed, so a
    property can start below a floor and still refuse the repair."""

    def __init__(self, raises=(), **values):
        super().__init__()
        for key, val in values.items():
            setattr(self, key, val)
        object.__setattr__(self, "_declines", set(raises))


class _SuppressFlag:
    """A timeline feature's isSuppressed: 'raise_on' names the value whose write raises, 'drop'
    swallows every write. Both are shapes the suppression read-back gates report. Bespoke: the
    shared timeline fake carries no refuse-this-write / swallow-this-write pair."""

    def __init__(self, value=True, raise_on=None, drop=False):
        object.__setattr__(self, "isSuppressed", value)
        object.__setattr__(self, "_raise_on", raise_on)
        object.__setattr__(self, "_drop", drop)

    def __setattr__(self, key, value):
        if key == "isSuppressed":
            if self._raise_on is not None and value is self._raise_on:
                raise RuntimeError("the timeline refused the flag")
            if self._drop:
                return
        object.__setattr__(self, key, value)


def _install_suppressed(rig, item, hits_after):
    """Wire the suppressed-PMI world: `item` is the suppressed timeline feature named Note1, absent
    from the PMI collections, and `hits_after` is what those collections report once it is
    unsuppressed."""
    rig.monkeypatch.setattr(pe._pmi, "suppressed_pmi_features", lambda d: [(item, "Note1")])
    rig.monkeypatch.setattr(pe._pmi, "find_annotation",
                            lambda d, n, c="": (None, None, "No PMI named 'Note1'."))
    rig.monkeypatch.setattr(
        pe._pmi, "annotation_hits",
        lambda d, n, c="": (([] if item.isSuppressed else list(hits_after)), []))
    return item


@pytest.fixture
def rig(monkeypatch):
    ann = _FakeAnn()
    comp = MakeComp("Root")
    monkeypatch.setattr(pe._common, "design", lambda: object())
    monkeypatch.setattr(pe._pmi, "find_annotation", lambda d, n, c="": (ann, comp, None))
    monkeypatch.setattr(pe._pmi, "segments_markup", lambda a: "NEW")
    return SimpleNamespace(ann=ann, comp=comp, monkeypatch=monkeypatch)


class TestSetText:
    def test_replaces_segments_and_reports_the_markup(self, rig):
        out = _payload(pe.handler(action="set_text", annotation="Note1", text="NEW"))
        assert out["markup"] == "NEW" and rig.ann.segments is not None

    def test_imported_pmi_is_refused(self, rig):
        rig.ann.objectType = "adsk::fusion::PMIImportedNote"
        msg = error_message(pe.handler(action="set_text", annotation="Note1", text="X"))
        assert "read-only" in msg and "convert_imported" in msg

    def test_below_floor_extension_is_normalized_first(self, rig):
        rig.ann.leaderLineExtension = pe._pmi.LEADER_EXT_FLOOR / 2
        # the rig's markup read-back reports "NEW", so the request has to be the same string for
        # the landed-text comparison to pass and leave the extension as the thing under test
        _payload(pe.handler(action="set_text", annotation="Note1", text="NEW"))
        assert rig.ann.leaderLineExtension == pe._pmi.LEADER_EXT_DEFAULT

    def test_bad_token_is_refused(self, rig):
        assert "'{bogus}'" in error_message(
            pe.handler(action="set_text", annotation="Note1", text="{bogus}"))

    def test_an_unrepairable_below_floor_extension_stops_the_text_edit(self, rig):
        ann = _RefusingSet(raises=("leaderLineExtension",),
                           leaderLineExtension=pe._pmi.LEADER_EXT_FLOOR / 2)
        rig.monkeypatch.setattr(pe._pmi, "find_annotation",
                                lambda d, n, c="": (ann, rig.comp, None))
        msg = error_message(pe.handler(action="set_text", annotation="Note1", text="X"))
        assert "recreate" in msg and ann.segments is None

    def test_a_segments_write_that_raises_is_an_error(self, rig):
        ann = _RefusingSet(raises=("segments",))
        rig.monkeypatch.setattr(pe._pmi, "find_annotation",
                                lambda d, n, c="": (ann, rig.comp, None))
        assert "Setting the note text failed" in error_message(
            pe.handler(action="set_text", annotation="Note1", text="X"))

    def test_unreadable_segments_after_the_write_is_an_error(self, rig):
        rig.monkeypatch.setattr(pe._pmi, "segments_markup", lambda a: None)
        assert "did not take" in error_message(
            pe.handler(action="set_text", annotation="Note1", text="X"))

    def test_landed_text_that_differs_from_the_request_is_an_error(self, rig):
        # the write is accepted and the segments READ - they just spell something else. Without the
        # comparison the payload republishes that other string as the edit's success.
        msg = error_message(pe.handler(action="set_text", annotation="Note1", text="DEBURR"))
        assert "'NEW'" in msg and "'DEBURR'" in msg and "did not take" in msg

    def test_landed_text_equal_to_the_request_is_reported_as_the_markup(self, rig):
        # the exact-equality boundary from the other side: same string, no error
        out = _payload(pe.handler(action="set_text", annotation="Note1", text="NEW"))
        assert out["markup"] == "NEW"

    def test_a_symbol_markup_round_trip_is_compared_on_the_encoded_form(self, rig):
        # build_segments/segments_markup round-trip {symbol} tokens exactly, so the comparison is
        # against the markup the caller wrote, not a stripped plain-text version of it
        rig.monkeypatch.setattr(pe._pmi, "segments_markup", lambda a: "{flatness}0.05")
        out = _payload(pe.handler(action="set_text", annotation="Note1", text="{flatness}0.05"))
        assert out["markup"] == "{flatness}0.05"
        assert "0.05" in error_message(
            pe.handler(action="set_text", annotation="Note1", text="{flatness}0.06"))


class TestRename:
    def test_rename_reads_back(self, rig):
        out = _payload(pe.handler(action="rename", annotation="Note1", new_name="Flatness"))
        assert out["name"] == "Flatness"

    def test_rename_that_does_not_take_is_an_error(self, rig):
        class Stubborn(_FakeAnn):
            @property
            def name(self):
                return "Note1"

            @name.setter
            def name(self, v):
                pass
        stubborn = Stubborn()
        rig.monkeypatch.setattr(pe._pmi, "find_annotation",
                                lambda d, n, c="": (stubborn, rig.comp, None))
        assert "did not take" in error_message(
            pe.handler(action="rename", annotation="Note1", new_name="X"))

    def test_missing_new_name_is_an_error(self, rig):
        assert "new_name" in error_message(pe.handler(action="rename", annotation="Note1"))

    def test_a_name_write_that_raises_is_an_error(self, rig):
        ann = _RefusingSet(raises=("name",))
        rig.monkeypatch.setattr(pe._pmi, "find_annotation",
                                lambda d, n, c="": (ann, rig.comp, None))
        assert "Rename failed" in error_message(
            pe.handler(action="rename", annotation="Note1", new_name="X"))


class TestVisibility:
    def test_hide_gates_on_the_reread(self, rig):
        out = _payload(pe.handler(action="hide", annotation="Note1"))
        assert out["light_bulb_on"] is False and rig.ann.isLightBulbOn is False

    def test_a_toggle_that_does_not_take_is_an_error(self, rig):
        class Frozen(_FakeAnn):
            @property
            def isLightBulbOn(self):
                return True

            @isLightBulbOn.setter
            def isLightBulbOn(self, v):
                pass
        rig.monkeypatch.setattr(pe._pmi, "find_annotation",
                                lambda d, n, c="": (Frozen(), rig.comp, None))
        assert "did not take" in error_message(pe.handler(action="hide", annotation="Note1"))

    def test_a_bulb_write_that_raises_is_an_error(self, rig):
        ann = _RefusingSet(raises=("isLightBulbOn",))
        rig.monkeypatch.setattr(pe._pmi, "find_annotation",
                                lambda d, n, c="": (ann, rig.comp, None))
        assert "Visibility toggle failed" in error_message(
            pe.handler(action="hide", annotation="Note1"))

    def test_show_with_a_parent_bulb_off_reports_the_cause(self, rig):
        rig.ann.isLightBulbOn = False
        rig.ann.isVisible = False
        out = _payload(pe.handler(action="show", annotation="Note1"))
        assert "light bulb" in out["note"].lower()


class TestUpToDateAndConvert:
    def test_already_up_to_date_is_a_no_op_note(self, rig):
        out = _payload(pe.handler(action="mark_up_to_date", annotation="Note1"))
        assert "Already up to date" in out["note"]

    def test_a_decline_is_an_error(self, rig):
        stubborn = _FakeAnn(out_of_date=True, up_to_date_ok=False)
        rig.monkeypatch.setattr(pe._pmi, "find_annotation",
                                lambda d, n, c="": (stubborn, rig.comp, None))
        assert "declined" in error_message(pe.handler(action="mark_up_to_date", annotation="Note1"))
        assert stubborn.isOutOfDate is True             # the flag never cleared either

    def test_dismisses_and_rereads(self, rig):
        rig.ann.isOutOfDate = True
        out = _payload(pe.handler(action="mark_up_to_date", annotation="Note1"))
        assert "out_of_date" not in out

    def test_convert_on_fusion_authored_is_refused(self, rig):
        assert "already Fusion-authored" in error_message(
            pe.handler(action="convert_imported", annotation="Note1"))

    def test_convert_decline_is_an_error(self, rig):
        rig.ann.objectType = "adsk::fusion::PMIImportedDimension"
        rig.ann.convertImportedToFusionPMI = lambda: None
        assert "declined" in error_message(
            pe.handler(action="convert_imported", annotation="Note1"))

    def test_convert_reports_the_new_kind(self, rig):
        rig.ann.objectType = "adsk::fusion::PMIImportedDimension"
        rig.ann.convertImportedToFusionPMI = lambda: _hole_ann(name="Note1")
        out = _payload(pe.handler(action="convert_imported", annotation="Note1"))
        assert out["converted_to"] == "hole_note"

    def test_a_markuptodate_raise_is_an_error(self, rig):
        def _boom():
            raise RuntimeError("the warnings are attached to missing geometry")
        rig.ann.isOutOfDate = True
        rig.ann.markUpToDate = _boom
        assert "markUpToDate() failed" in error_message(
            pe.handler(action="mark_up_to_date", annotation="Note1"))

    def test_a_conversion_raise_is_an_error(self, rig):
        def _boom():
            raise RuntimeError("this reference geometry has no Fusion counterpart")
        rig.ann.objectType = "adsk::fusion::PMIImportedNote"
        rig.ann.convertImportedToFusionPMI = _boom
        assert "Conversion failed" in error_message(
            pe.handler(action="convert_imported", annotation="Note1"))


class TestNewActions:
    def test_set_flags_on_a_leader_note_is_refused(self, rig):
        assert "hole/thread callouts only" in error_message(
            pe.handler(action="set_flags", annotation="Note1", flags={"quantity_note": False}))

    def test_set_values_on_a_leader_note_is_refused(self, rig):
        assert "hole/thread callouts only" in error_message(
            pe.handler(action="set_values", annotation="Note1", values={"diameter": 6}))

    def test_set_extension_below_the_floor_is_refused(self, rig):
        msg = error_message(pe.handler(action="set_extension", annotation="Note1",
                                       leader_extension=1))     # 1 mm < 2.5 mm floor
        assert "floor" in msg

    def test_set_extension_reads_back(self, rig):
        out = _payload(pe.handler(action="set_extension", annotation="Note1",
                                  leader_extension=6))
        assert out["leader_extension"] == 6.0
        assert rig.ann.leaderLineExtension == pytest.approx(0.6)

    def test_suppress_without_a_timeline_feature_is_refused(self, rig):
        rig.ann.timelineObject = None
        assert "timeline" in error_message(pe.handler(action="suppress", annotation="Note1"))

    def test_suppress_gates_on_the_reread(self, rig):
        tl = SimpleNamespace(isSuppressed=False)
        rig.ann.timelineObject = tl
        out = _payload(pe.handler(action="suppress", annotation="Note1"))
        assert out["suppressed"] is True and tl.isSuppressed is True

    def _suppressed(self, rig, hits_after):
        """A suppressed timeline feature named Note1; `hits_after` is what the PMI collections
        report once the feature is unsuppressed."""
        return _install_suppressed(rig, SimpleNamespace(isSuppressed=True), hits_after)

    def test_unsuppress_reaches_a_suppressed_pmi_through_the_timeline(self, rig):
        # suppressed PMI leaves the collections; only the suppressed timeline feature's name
        # survives - unsuppress flips it and verifies the annotation reappears.
        item = self._suppressed(rig, [(rig.ann, rig.comp)])
        out = _payload(pe.handler(action="unsuppress", annotation="Note1"))
        assert out["suppressed"] is False and item.isSuppressed is False

    def test_unsuppress_rolls_back_a_wrong_same_named_feature(self, rig):
        item = self._suppressed(rig, [])
        msg = error_message(pe.handler(action="unsuppress", annotation="Note1"))
        assert "not a PMI" in msg and item.isSuppressed is True

    def test_unsuppress_does_not_re_suppress_a_pmi_that_came_back_ambiguously(self, rig):
        # The PMI DID reappear - in two components. That is the opposite of "no PMI reappeared",
        # so it must not be reported as a non-PMI feature and must not be flipped back off.
        other = SimpleNamespace(name="Sub")
        item = self._suppressed(rig, [(rig.ann, rig.comp), (rig.ann, other)])
        msg = error_message(pe.handler(action="unsuppress", annotation="Note1"))
        assert "2 components" in msg and "Root" in msg and "Sub" in msg
        assert "component=" in msg
        assert "not a PMI" not in msg
        assert item.isSuppressed is False              # left unsuppressed - the PMI is back

    def test_set_leader_point_on_a_hole_note_is_refused(self, rig):
        rig.ann.objectType = "adsk::fusion::PMIHoleThreadNote"
        assert "leader notes only" in error_message(
            pe.handler(action="set_leader_point", annotation="Note1", leader_point=[1, 2, 3]))

    def test_set_leader_point_gates_on_the_platform_bool(self, rig):
        declining = _FakeAnn(target_ok=False)
        rig.monkeypatch.setattr(pe._pmi, "find_annotation",
                                lambda d, n, c="": (declining, rig.comp, None))
        assert "declined" in error_message(
            pe.handler(action="set_leader_point", annotation="Note1", leader_point=[1, 2, 3]))
        assert declining.annotationTargetPoint is None     # the refused point never landed

    def test_set_alignment_needs_at_least_one_knob(self, rig):
        assert "needs" in error_message(pe.handler(action="set_alignment", annotation="Note1"))

    def test_set_plane_refuses_an_unsupported_type(self, rig):
        rig.ann.supportedAnnotationPlaneTypes = [99]
        assert "not supported" in error_message(
            pe.handler(action="set_plane", annotation="Note1", plane="xy"))

    def test_set_display_on_a_leader_note_is_refused(self, rig):
        assert "hole/thread callouts only" in error_message(
            pe.handler(action="set_display", annotation="Note1", display={"precision": 2}))

    def test_set_extension_that_lands_off_target_is_an_error_not_a_rounded_ok(self, rig):
        # The read-back gate is a 1e-6 cm tolerance, not equality: a platform that quantised the
        # write to a visibly different length must be reported, and a bit of float noise must not.
        class Quantising(_FakeAnn):
            @property
            def leaderLineExtension(self):
                return 0.5                      # every write settles back here

            @leaderLineExtension.setter
            def leaderLineExtension(self, v):
                pass
        rig.monkeypatch.setattr(pe._pmi, "find_annotation",
                                lambda d, n, c="": (Quantising(), rig.comp, None))
        assert "did not take" in error_message(
            pe.handler(action="set_extension", annotation="Note1", leader_extension=6))

    def test_set_extension_tolerates_float_noise_within_a_micron(self, rig):
        class Noisy(_FakeAnn):
            def __init__(self):
                super().__init__()
                self._ext = pe._pmi.LEADER_EXT_DEFAULT

            @property
            def leaderLineExtension(self):
                return self._ext

            @leaderLineExtension.setter
            def leaderLineExtension(self, v):
                self._ext = v + 5e-7            # sub-micron settle, not a dropped write
        rig.monkeypatch.setattr(pe._pmi, "find_annotation",
                                lambda d, n, c="": (Noisy(), rig.comp, None))
        out = _payload(pe.handler(action="set_extension", annotation="Note1",
                                  leader_extension=6))
        assert out["leader_extension"] == pytest.approx(6.0, abs=1e-4)

    def test_set_extension_needs_a_number(self, rig):
        assert "must be a number" in error_message(
            pe.handler(action="set_extension", annotation="Note1", leader_extension="six"))

    def test_set_extension_without_a_length_is_refused(self, rig):
        assert "needs 'leader_extension'" in error_message(
            pe.handler(action="set_extension", annotation="Note1"))

    def test_set_extension_on_imported_pmi_is_refused(self, rig):
        rig.ann.objectType = "adsk::fusion::PMIImportedNote"
        assert "read-only" in error_message(
            pe.handler(action="set_extension", annotation="Note1", leader_extension=6))

    def test_set_extension_rereads_the_length_itself(self, rig):
        # the handler's own read-back, on top of the writer's: a writer reporting success while
        # the length never moved is still an error.
        rig.monkeypatch.setattr(pe._pmi, "apply_note_format",
                                lambda ann, a, v, p, ext, units="cm": None)
        msg = error_message(pe.handler(action="set_extension", annotation="Note1",
                                       leader_extension=6))
        assert "did not take" in msg and "re-read" in msg

    def test_set_alignment_on_imported_pmi_is_refused(self, rig):
        rig.ann.objectType = "adsk::fusion::PMIImportedNote"
        msg = error_message(pe.handler(action="set_alignment", annotation="Note1", align="left"))
        assert "read-only" in msg and "convert_imported" in msg

    def test_set_alignment_publishes_the_perpendicular_it_read_back(self, rig):
        out = _payload(pe.handler(action="set_alignment", annotation="Note1",
                                  align="left", perpendicular=True))
        assert out["align"] == "left" and out["perpendicular"] is True
        assert rig.ann.isPerpendicularLine is True

    def test_set_alignment_reports_a_perpendicular_that_did_not_take(self, rig):
        class Frozen(_FakeAnn):
            @property
            def isPerpendicularLine(self):
                return False

            @isPerpendicularLine.setter
            def isPerpendicularLine(self, v):
                pass
        rig.monkeypatch.setattr(pe._pmi, "find_annotation",
                                lambda d, n, c="": (Frozen(), rig.comp, None))
        assert "'perpendicular'=True" in error_message(
            pe.handler(action="set_alignment", annotation="Note1", perpendicular=True))

    def test_set_display_writes_both_settings_through_the_shared_writer(self, rig):
        hole = _hole_ann()
        hole.primaryDisplaySettings = None
        hole.secondaryDisplaySettings = None
        hole.hasSecondaryDisplaySettings = False
        rig.monkeypatch.setattr(pe._pmi, "find_annotation",
                                lambda d, n, c="": (hole, rig.comp, None))
        seen = []

        def _apply(obj, spec):
            seen.append((obj, spec))
            return None
        rig.monkeypatch.setattr(pe._pmi, "apply_display", _apply)
        _payload(pe.handler(action="set_display", annotation="Hole Note1",
                            display={"precision": 2, "secondary": {"precision": 4}}))
        assert seen == [(hole, {"precision": 2, "secondary": {"precision": 4}})]

    def test_set_display_surfaces_the_writers_refusal(self, rig):
        hole = _hole_ann()
        rig.monkeypatch.setattr(pe._pmi, "find_annotation",
                                lambda d, n, c="": (hole, rig.comp, None))
        rig.monkeypatch.setattr(pe._pmi, "apply_display",
                                lambda obj, spec: "display.secondary: bad unit")
        assert "bad unit" in error_message(
            pe.handler(action="set_display", annotation="Hole Note1",
                       display={"secondary": {"units": "furlong"}}))


class TestAnchorPoints:
    """set_text_point / set_leader_point: the kind guard, the helper's refusal, and the record the
    handler builds back out of the point it read."""

    def test_set_text_point_on_imported_pmi_is_refused(self, rig):
        rig.ann.objectType = "adsk::fusion::PMIImportedNote"
        msg = error_message(pe.handler(action="set_text_point", annotation="Note1",
                                       text_point=[1, 2, 3]))
        assert "read-only" in msg and "convert_imported" in msg

    def test_set_text_point_surfaces_a_malformed_point(self, rig):
        assert "[x, y, z]" in error_message(
            pe.handler(action="set_text_point", annotation="Note1", text_point=[1, 2]))

    def test_set_text_point_reports_the_anchor_in_display_units(self, rig):
        # the platform reads back in cm; the payload speaks the caller's 'units'
        rig.monkeypatch.setattr(pe._pmi, "set_text_point",
                                lambda a, xyz, f: (SimpleNamespace(x=0.1, y=0.2, z=0.3), None))
        out = _payload(pe.handler(action="set_text_point", annotation="Note1",
                                  text_point=[1, 2, 3], units="mm"))
        assert out["text_point"] == {"x": 1.0, "y": 2.0, "z": 3.0}

    def test_set_leader_point_reports_the_target_in_display_units(self, rig):
        rig.monkeypatch.setattr(pe._pmi, "set_leader_target",
                                lambda a, xyz, f: (SimpleNamespace(x=0.1, y=0.2, z=0.3), None))
        out = _payload(pe.handler(action="set_leader_point", annotation="Note1",
                                  leader_point=[1, 2, 3], units="mm"))
        assert out["leader_point"] == {"x": 1.0, "y": 2.0, "z": 3.0}

    def test_an_unrepairable_below_floor_extension_stops_the_leader_move(self, rig):
        ann = _RefusingSet(raises=("leaderLineExtension",),
                           leaderLineExtension=pe._pmi.LEADER_EXT_FLOOR / 2)
        rig.monkeypatch.setattr(pe._pmi, "find_annotation",
                                lambda d, n, c="": (ann, rig.comp, None))
        assert "recreate" in error_message(
            pe.handler(action="set_leader_point", annotation="Note1", leader_point=[1, 2, 3]))


class TestSetPlane:
    """set_plane: the kind guard, the refusals decided before the write, and the bool + read-back
    gate on the write itself."""

    def _armed(self, rig, accepts=True):
        """Record every setAnnotationPlane call; the plane type moves either way, so `accepts` -
        what the platform returns - is the only thing the bool gate can be reading. Returns the
        call list."""
        seen = []

        def _set(*args):
            seen.append(args)
            rig.ann.annotationPlaneType = args[0]
            return accepts
        rig.ann.setAnnotationPlane = _set
        return seen

    def test_set_plane_on_a_hole_note_is_refused(self, rig):
        rig.ann.objectType = "adsk::fusion::PMIHoleThreadNote"
        assert "leader notes only" in error_message(
            pe.handler(action="set_plane", annotation="Note1", plane="xy"))

    def test_set_plane_without_a_plane_is_refused(self, rig):
        assert "needs 'plane'" in error_message(pe.handler(action="set_plane", annotation="Note1"))

    def test_set_plane_surfaces_the_face_kinds_refusal(self, rig):
        rig.monkeypatch.setattr(pe._PLANE_FACE, "resolve",
                                lambda raw: (None, "'plane_face': handle did not resolve"))
        seen = self._armed(rig)
        assert "did not resolve" in error_message(
            pe.handler(action="set_plane", annotation="Note1", plane="custom_face",
                       plane_face="stale-handle"))
        assert seen == []                    # refused before the plane write

    def test_an_unrepairable_below_floor_extension_stops_the_plane_set(self, rig):
        ann = _RefusingSet(raises=("leaderLineExtension",),
                           leaderLineExtension=pe._pmi.LEADER_EXT_FLOOR / 2)
        rig.monkeypatch.setattr(pe._pmi, "find_annotation",
                                lambda d, n, c="": (ann, rig.comp, None))
        assert "recreate" in error_message(
            pe.handler(action="set_plane", annotation="Note1", plane="xy"))

    def test_a_faceless_plane_is_written_with_one_argument(self, rig):
        seen = self._armed(rig)
        out = _payload(pe.handler(action="set_plane", annotation="Note1", plane="xy"))
        assert seen == [
            (pe.adsk.fusion.LeaderLineNotePlaneTypes.PrincipalXYLeaderLineNotePlaneType,)]
        assert out["plane"] == "xy"

    def test_the_resolved_face_reaches_the_plane_write(self, rig):
        face = object()
        rig.monkeypatch.setattr(pe._PLANE_FACE, "resolve", lambda raw: (face, None))
        seen = self._armed(rig)
        _payload(pe.handler(action="set_plane", annotation="Note1", plane="custom_face",
                            plane_face="handle"))
        assert seen[0][1] is face

    def test_set_plane_gates_on_the_platform_bool(self, rig):
        declining = _FakeAnn(plane_ok=False)
        rig.monkeypatch.setattr(pe._pmi, "find_annotation",
                                lambda d, n, c="": (declining, rig.comp, None))
        assert "did not take" in error_message(
            pe.handler(action="set_plane", annotation="Note1", plane="xy"))
        # the call DID reach the platform - the bool it answered is what refused the set
        assert declining._plane_calls == [
            (pe.adsk.fusion.LeaderLineNotePlaneTypes.PrincipalXYLeaderLineNotePlaneType,)]

    def test_set_plane_gates_on_the_reread_even_when_the_call_returns_true(self, rig):
        types = pe.adsk.fusion.LeaderLineNotePlaneTypes
        rig.ann.annotationPlaneType = types.PrincipalYZLeaderLineNotePlaneType
        rig.ann.setAnnotationPlane = lambda *args: True      # accepted, but nothing moved
        assert "did not take" in error_message(
            pe.handler(action="set_plane", annotation="Note1", plane="xy"))

    def test_a_plane_write_that_raises_names_the_face_input(self, rig):
        def _boom(*args):
            raise RuntimeError("the note is not adjacent to that face")
        rig.ann.setAnnotationPlane = _boom
        msg = error_message(pe.handler(action="set_plane", annotation="Note1", plane="face"))
        assert "setAnnotationPlane failed" in msg and "plane_face" in msg


class TestSuppression:
    """The timeline flag behind suppress/unsuppress: every write is read back, and the by-name
    unsuppress path reaches a PMI that has left the collections."""

    def test_a_suppression_write_that_raises_is_an_error(self, rig):
        rig.ann.timelineObject = _SuppressFlag(value=False, raise_on=True)
        assert "Timeline suppression toggle failed" in error_message(
            pe.handler(action="suppress", annotation="Note1"))

    def test_a_dropped_suppression_write_is_an_error(self, rig):
        rig.ann.timelineObject = _SuppressFlag(value=False, drop=True)
        assert "did not take" in error_message(pe.handler(action="suppress", annotation="Note1"))

    def test_unsuppress_of_a_findable_pmi_clears_the_timeline_flag(self, rig):
        tl = SimpleNamespace(isSuppressed=True)
        rig.ann.timelineObject = tl
        out = _payload(pe.handler(action="unsuppress", annotation="Note1"))
        assert out["suppressed"] is False and tl.isSuppressed is False

    def test_unsuppress_with_no_suppressed_feature_surfaces_the_resolver_error(self, rig):
        rig.monkeypatch.setattr(pe._pmi, "find_annotation",
                                lambda d, n, c="": (None, None, "No PMI named 'Ghost'."))
        rig.monkeypatch.setattr(pe._pmi, "suppressed_pmi_features", lambda d: [])
        assert "No PMI named 'Ghost'." in error_message(
            pe.handler(action="unsuppress", annotation="Ghost"))

    def test_unsuppress_refuses_two_suppressed_features_of_one_name(self, rig):
        first, second = _SuppressFlag(), _SuppressFlag()
        rig.monkeypatch.setattr(pe._pmi, "suppressed_pmi_features",
                                lambda d: [(first, "Note1"), (second, "Note1")])
        rig.monkeypatch.setattr(pe._pmi, "find_annotation",
                                lambda d, n, c="": (None, None, "No PMI named 'Note1'."))
        msg = error_message(pe.handler(action="unsuppress", annotation="Note1"))
        assert "2 suppressed timeline features" in msg
        assert first.isSuppressed is True and second.isSuppressed is True

    def test_an_unsuppress_write_that_raises_is_an_error(self, rig):
        item = _install_suppressed(rig, _SuppressFlag(raise_on=False), [])
        assert "Timeline unsuppress failed" in error_message(
            pe.handler(action="unsuppress", annotation="Note1"))
        assert item.isSuppressed is True

    def test_a_failed_rollback_after_a_non_pmi_unsuppress_is_reported(self, rig):
        # no PMI reappeared AND the re-suppress raised: the caller is told both, and the timeline
        # is left with the feature unsuppressed
        item = _install_suppressed(rig, _SuppressFlag(raise_on=True), [])
        msg = error_message(pe.handler(action="unsuppress", annotation="Note1"))
        assert "not a PMI annotation" in msg and "re-suppressing it failed" in msg
        assert item.isSuppressed is False


class TestHoleCallouts:
    """set_flags / set_display on a hole callout: what the payload republishes after the write."""

    @pytest.fixture
    def hole(self, rig):
        ann = _hole_ann()
        rig.monkeypatch.setattr(pe._pmi, "find_annotation",
                                lambda d, n, c="": (ann, rig.comp, None))
        return ann

    def test_set_flags_reports_each_flag_it_read_back(self, hole):
        out = _payload(pe.handler(action="set_flags", annotation="Hole Note1",
                                  flags={"through": True, "threaded": False}))
        assert out["flags"] == {"through": True, "threaded": False}
        assert hole.isThrough is True and hole.isThreaded is False

    def test_an_empty_or_missing_flags_spec_is_refused_naming_the_action(self, hole):
        """An empty spec applies nothing, so ok would be a false success."""
        for spec in ({}, None):
            msg = error_message(pe.handler(action="set_flags", annotation="Hole Note1",
                                           flags=spec))
            assert "action='set_flags'" in msg and "non-empty 'flags'" in msg
        assert not hasattr(hole, "isThrough")

    def test_an_unknown_flag_is_refused(self, hole):
        assert "Unknown flag 'bogus'" in error_message(
            pe.handler(action="set_flags", annotation="Hole Note1", flags={"bogus": True}))

    def test_set_display_without_a_display_object_is_refused(self, hole):
        assert "needs 'display'" in error_message(
            pe.handler(action="set_display", annotation="Hole Note1"))

    def test_an_unknown_display_key_reaches_the_wire_as_a_refusal(self, hole):
        # the real writer, not a stub: the key the tool cannot honour must refuse before the
        # settings object is assigned, the way an unknown flag does
        hole.primaryDisplaySettings = None
        msg = error_message(pe.handler(action="set_display", annotation="Hole Note1",
                                       display={"decimals": 4}))
        assert "Unknown display key 'decimals'" in msg and "precision" in msg
        assert hole.primaryDisplaySettings is None

    def test_set_display_republishes_the_secondary_settings_a_note_carries(self, hole, rig):
        mm = pe.adsk.fusion.PMIUnitTypes.MillimetersPMIUnitType
        hole.primaryDisplaySettings = FakePMIDisplaySettings(
            precision=2, unit_type=mm, leading_zeros=True, trailing_zeros=False,
            unit_abbreviation=True)
        hole.secondaryDisplaySettings = FakePMIDisplaySettings(
            precision=4, unit_type=mm, leading_zeros=True, trailing_zeros=False,
            unit_abbreviation=False)
        hole.hasSecondaryDisplaySettings = True
        rig.monkeypatch.setattr(pe._pmi, "apply_display", lambda obj, spec: None)
        out = _payload(pe.handler(action="set_display", annotation="Hole Note1",
                                  display={"precision": 2, "secondary": {"precision": 4}}))
        assert out["display"]["precision"] == 2
        assert out["display_secondary"]["precision"] == 4


class _RecordingTolerance(FakePMIGeometricValueTolerance):
    """The shared bounds fake with setSymmetric READING BACK what it was handed, so a test can pin
    exactly what the tool sent (the shared one records the call without storing a bound)."""

    def __init__(self):
        super().__init__(upper=0.0)

    def setSymmetric(self, value):
        self.symmetric = value
        self.upperTolerance = value
        return True


class TestSetValues:
    """A hole callout's values: a length writes value and tolerance in cm, an angle writes its
    value in radians and REFUSES a tolerance."""

    @pytest.fixture
    def hole(self, rig):
        ann = _hole_ann(values={key: make_pmi_value()
                                for key in ("countersink_angle_deg", "diameter", "depth")})
        made = []

        def _create():
            made.append(_RecordingTolerance())
            return made[-1]

        rig.monkeypatch.setattr(pe._pmi, "find_annotation", lambda d, n, c="": (ann, rig.comp, None))
        rig.monkeypatch.setattr(pe._pmi.adsk.fusion, "PMIGeometricValueTolerance",
                                SimpleNamespace(create=_create))
        return SimpleNamespace(ann=ann, made=made)

    def _set_angle_with_tolerance(self, hole):
        return pe.handler(action="set_values", annotation="Hole Note1", units="mm",
                          values={"countersink_angle_deg":
                                  {"value": 90, "tolerance": {"type": "symmetric", "value": 1}}})

    def test_an_angle_tolerance_is_refused_naming_the_field(self, hole):
        msg = error_message(self._set_angle_with_tolerance(hole))
        assert "countersink_angle_deg" in msg and "not written" in msg
        assert hole.made == []                       # nothing was handed to the platform
        assert hole.ann.countersinkAngle.value == 0.0        # the refused call wrote nothing
        assert hole.ann.countersinkAngle.tolerance is None

    def test_a_refused_angle_tolerance_leaves_the_other_values_untouched(self, hole):
        """The refusal is decided BEFORE the first write, so a good key listed ahead of the
        refused one is not left half-applied."""
        msg = error_message(pe.handler(
            action="set_values", annotation="Hole Note1", units="mm",
            values={"diameter": {"value": 6, "tolerance": {"type": "symmetric", "value": 0.5}},
                    "countersink_angle_deg": {"value": 90,
                                              "tolerance": {"type": "symmetric", "value": 1}}}))
        assert "countersink_angle_deg" in msg
        assert hole.ann.diameter.value == 0.0 and hole.ann.diameter.tolerance is None
        assert hole.ann.countersinkAngle.value == 0.0

    def test_a_malformed_tolerance_refuses_before_any_key_is_written(self, hole):
        """The tolerance objects are built in the pre-pass, so a bad spec on the SECOND key
        refuses with the first key still unwritten."""
        msg = error_message(pe.handler(
            action="set_values", annotation="Hole Note1", units="mm",
            values={"diameter": {"value": 6, "tolerance": {"type": "symmetric", "value": 0.5}},
                    "depth": {"value": 3, "tolerance": {"type": "wonky"}}}))
        assert "depth" in msg and "wonky" in msg
        assert hole.ann.diameter.value == 0.0 and hole.ann.diameter.tolerance is None
        assert hole.ann.depth.value == 0.0

    def test_a_tolerance_only_length_spec_writes_the_bound_and_keeps_the_value(self, hole):
        out = _payload(pe.handler(
            action="set_values", annotation="Hole Note1", units="mm",
            values={"diameter": {"tolerance": {"type": "symmetric", "value": 0.5}}}))
        assert hole.ann.diameter.value == 0.0            # no value sent, none written
        assert hole.made[0].symmetric == pytest.approx(0.05)
        assert out["values"]["diameter"]["tolerance"]["upper"] == 0.5

    def test_a_tolerance_only_angle_spec_refuses_and_writes_no_value(self, hole):
        msg = error_message(pe.handler(
            action="set_values", annotation="Hole Note1", units="mm",
            values={"countersink_angle_deg": {"tolerance": {"type": "symmetric", "value": 1}}}))
        assert "countersink_angle_deg" in msg
        assert hole.ann.countersinkAngle.value == 0.0

    def test_an_angle_value_without_a_tolerance_still_writes(self, hole):
        out = _payload(pe.handler(action="set_values", annotation="Hole Note1", units="mm",
                                  values={"countersink_angle_deg": 90}))
        assert hole.ann.countersinkAngle.value == pytest.approx(math.radians(90))
        assert out["values"]["countersink_angle_deg"]["value"] == 90.0

    def test_an_angle_key_in_any_case_converts_its_value_to_radians(self, hole):
        out = _payload(pe.handler(action="set_values", annotation="Hole Note1", units="mm",
                                  values={"Countersink_Angle_Deg": 90}))
        assert hole.ann.countersinkAngle.value == pytest.approx(math.radians(90))
        assert out["values"]["Countersink_Angle_Deg"]["value"] == 90.0

    def test_length_tolerance_is_written_in_cm_like_its_value(self, hole):
        out = _payload(pe.handler(action="set_values", annotation="Hole Note1", units="mm",
                                  values={"diameter": {"value": 6,
                                                       "tolerance": {"type": "symmetric",
                                                                     "value": 0.5}}}))
        assert hole.ann.diameter.value == pytest.approx(0.6)
        assert hole.made[0].symmetric == pytest.approx(0.05)
        applied = out["values"]["diameter"]
        assert applied["value"] == 6.0 and applied["tolerance"]["upper"] == 0.5

    def test_an_empty_or_missing_values_spec_is_refused_naming_the_action(self, hole):
        """An empty spec applies nothing, so ok would be a false success."""
        for spec in ({}, None):
            msg = error_message(pe.handler(action="set_values", annotation="Hole Note1",
                                           values=spec))
            assert "action='set_values'" in msg and "non-empty 'values'" in msg
        assert hole.ann.diameter.value == 0.0 and hole.ann.diameter.isOverriddenValue is False

    def test_an_unreadable_value_property_is_refused(self, hole):
        hole.ann.depth = None
        assert "not applicable" in error_message(
            pe.handler(action="set_values", annotation="Hole Note1", values={"depth": 3}))


class TestGuards:
    def test_resolver_error_surfaces(self, rig):
        rig.monkeypatch.setattr(pe._pmi, "find_annotation",
                                lambda d, n, c="": (None, None, "No PMI named 'X'."))
        assert "No PMI named" in error_message(pe.handler(action="rename", annotation="X",
                                                          new_name="Y"))

    def test_no_active_design_is_an_error(self, rig):
        rig.monkeypatch.setattr(pe._common, "design", lambda: None)
        assert "No active design" in error_message(pe.handler(action="hide", annotation="N"))

    def test_an_unknown_action_is_refused_listing_the_vocabulary(self, rig):
        msg = error_message(pe.handler(action="set_colour", annotation="Note1"))
        assert "must be one of" in msg and "set_text" in msg

    def test_unknown_units_are_refused(self, rig):
        assert "Unknown units 'furlong'" in error_message(
            pe.handler(action="hide", annotation="Note1", units="furlong"))
