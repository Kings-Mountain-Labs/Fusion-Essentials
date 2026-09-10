"""Unit tests for ``model_shell.py`` - hollow a solid body into a thin-walled shell.

Pinned: the closed-shell happy path (body only) and the open-shell path (faces removed), thickness +
direction mapping onto inside/outside thickness, the guards (no design, bad units, zero/negative
thickness, missing body, wrong-kind body), and the honesty gate - a shell that the API reports as a
success but that leaves the body's volume unchanged must surface as an error, not a false ok.
"""

import adsk.core
import adsk.fusion

from conftest import (
    load_tool, make_design, install, MakeComp, BRepBody, BRepFace, _NamedCollection,
    payload as _payload, error_message, assert_no_active_design, assert_unknown_units,
)

sh = load_tool("model_shell")


# ── fakes: the ShellFeatures collection ──────────────────────────────────────

class _VI:
    """A ValueInput stand-in carrying the raw cm value the handler set."""
    def __init__(self, value):
        self.value = value


class FakeShellInput:
    def __init__(self, coll, tangent):
        self.coll = coll
        self.isTangentChain = tangent
        self.insideThickness = None
        self.outsideThickness = None


class FakeShellFeature:
    def __init__(self, name, inside_v, outside_v, result_bodies):
        self.name = name
        self.insideThickness = _VI(inside_v)
        self.outsideThickness = _VI(outside_v)
        self.bodies = _NamedCollection(result_bodies)


class FakeShellFeatures:
    """createInput/add that simulate hollowing: add() drops the target body's volume and adds inner
    faces (unless volume_delta/faces_added are 0, which models a silent no-op)."""
    def __init__(self, body, volume_delta=40.0, faces_added=6, return_feature=True):
        self.body = body
        self.volume_delta = volume_delta
        self.faces_added = faces_added
        self.return_feature = return_feature
        self.last_input = None

    def createInput(self, coll, isTangentChain=True):
        self.last_input = FakeShellInput(coll, isTangentChain)
        return self.last_input

    def add(self, inp):
        if not self.return_feature:
            return None
        if self.volume_delta:
            self.body.volume -= self.volume_delta
        self.body.faces._items.extend([None] * self.faces_added)
        inside_v = inp.insideThickness.value if inp.insideThickness else 0.0
        outside_v = inp.outsideThickness.value if inp.outsideThickness else 0.0
        return FakeShellFeature("Shell1", inside_v, outside_v, [self.body])


def _install(body, sf, tokens=None):
    """Wire a component carrying `sf` (features.shellFeatures) and `body` into the tool module via
    conftest.install (both seams + Design.cast + ObjectCollection.create). Also model the adsk types
    the input kinds isinstance-check and the ValueInput factory the handler calls. All reverted by the
    conftest autouse restore, so nothing leaks."""
    comp = MakeComp(name="Comp", bodies=[body])
    comp.features = type("F", (), {"shellFeatures": sf})()
    install(sh, make_design(comp=comp, tokens=tokens))
    adsk.fusion.BRepBody = BRepBody
    adsk.fusion.BRepFace = BRepFace
    adsk.core.ValueInput.createByReal = staticmethod(lambda v: _VI(v))
    return comp


# ── happy paths ──────────────────────────────────────────────────────────────

class TestClosedShell:
    def test_hollows_most_recent_body_into_closed_shell(self):
        body = BRepBody(name="Block", volume=100.0, face_count=6)
        sf = FakeShellFeatures(body, volume_delta=40.0, faces_added=6)
        _install(body, sf)
        out = _payload(sh.handler(thickness=2, units="mm"))
        assert out["shelled"] is True
        assert out["body"] == "Block"
        assert out["removed_faces"] == 0           # closed shell: no faces opened
        assert out["result_bodies"] == ["Block"]
        # the coll handed to createInput held the body itself (closed shell)
        assert sf.last_input.coll.count == 1

    def test_reports_volume_removed_and_face_delta(self):
        body = BRepBody(volume=100.0, face_count=6)
        sf = FakeShellFeatures(body, volume_delta=40.0, faces_added=6)
        _install(body, sf)
        out = _payload(sh.handler(thickness=2, units="mm"))
        assert out["volume_removed_cm3"] == 40.0
        assert out["faces_delta"] == 6

    def test_targets_named_solid_body(self):
        body = BRepBody(name="Housing", volume=50.0)
        sf = FakeShellFeatures(body, volume_delta=10.0)
        _install(body, sf)
        out = _payload(sh.handler(body_name="Housing", thickness=1, units="mm"))
        assert out["body"] == "Housing"


class TestOpenShell:
    def test_removes_given_faces_and_derives_body(self):
        body = BRepBody(name="Cup", volume=80.0, face_count=6)
        face = BRepFace(None, body=body)
        sf = FakeShellFeatures(body, volume_delta=30.0, faces_added=5)
        _install(body, sf, tokens={"F1": face})
        out = _payload(sh.handler(remove_faces=["F1"], thickness=2, units="mm"))
        assert out["shelled"] is True
        assert out["removed_faces"] == 1
        assert out["body"] == "Cup"                # body derived from the face's owner
        # the coll held the face(s), NOT the body (createInput contract)
        assert sf.last_input.coll.count == 1


# ── thickness + direction mapping ────────────────────────────────────────────

class TestDirection:
    def test_inside_sets_inside_thickness_only(self):
        body = BRepBody(volume=100.0)
        sf = FakeShellFeatures(body)
        _install(body, sf)
        out = _payload(sh.handler(thickness=2, units="mm", direction="inside"))
        assert out["direction"] == "inside"
        assert out["inside_thickness"] == 2.0      # 2mm -> 0.2cm -> reported back as 2.0mm
        assert out["outside_thickness"] == 0.0
        assert sf.last_input.insideThickness.value == 0.2
        assert sf.last_input.outsideThickness.value == 0.0

    def test_outside_sets_outside_thickness_only(self):
        body = BRepBody(volume=100.0)
        sf = FakeShellFeatures(body)
        _install(body, sf)
        out = _payload(sh.handler(thickness=3, units="mm", direction="outside"))
        assert out["outside_thickness"] == 3.0
        assert out["inside_thickness"] == 0.0
        assert sf.last_input.insideThickness.value == 0.0

    def test_both_sets_both_thicknesses(self):
        body = BRepBody(volume=100.0)
        sf = FakeShellFeatures(body)
        _install(body, sf)
        out = _payload(sh.handler(thickness=1, units="mm", direction="both"))
        assert out["inside_thickness"] == 1.0
        assert out["outside_thickness"] == 1.0

    def test_unreadable_inactive_thickness_does_not_refuse(self, monkeypatch):
        body = BRepBody(volume=100.0)
        sf = FakeShellFeatures(body)
        original_add = sf.add
        def add(inp):
            feature = original_add(inp)
            feature.insideThickness = None
            return feature
        monkeypatch.setattr(sf, "add", add)
        _install(body, sf)
        out = _payload(sh.handler(thickness=2, units="mm", direction="outside"))
        assert out["shelled"] is True
        assert "inside_thickness" not in out

    def test_readable_inactive_thickness_mismatch_refuses(self, monkeypatch):
        body = BRepBody(volume=100.0)
        sf = FakeShellFeatures(body)
        original_add = sf.add
        def add(inp):
            feature = original_add(inp)
            feature.insideThickness.value = 0.1
            return feature
        monkeypatch.setattr(sf, "add", add)
        _install(body, sf)
        res = sh.handler(thickness=2, units="mm", direction="outside")
        assert res["isError"] is True and "unverified" in res["message"]

    def test_missing_requested_thickness_refuses_for_both(self, monkeypatch):
        body = BRepBody(volume=100.0)
        sf = FakeShellFeatures(body)
        original_add = sf.add
        def add(inp):
            feature = original_add(inp)
            feature.insideThickness = None
            return feature
        monkeypatch.setattr(sf, "add", add)
        _install(body, sf)
        res = sh.handler(thickness=2, units="mm", direction="both")
        assert res["isError"] is True and "unverified" in res["message"]

    def test_cm_units_scale_thickness(self):
        body = BRepBody(volume=100.0)
        sf = FakeShellFeatures(body)
        _install(body, sf)
        out = _payload(sh.handler(thickness=2, units="cm", direction="inside"))
        assert sf.last_input.insideThickness.value == 2.0   # 2cm -> 2.0cm internal
        assert out["inside_thickness"] == 2.0


# ── guards ───────────────────────────────────────────────────────────────────

class TestGuards:
    def test_no_active_design(self):
        body = BRepBody()
        _install(body, FakeShellFeatures(body))
        assert_no_active_design(sh, sh.handler, thickness=1, units="mm")

    def test_bad_units(self):
        body = BRepBody()
        _install(body, FakeShellFeatures(body))
        assert_unknown_units(sh.handler, thickness=1)

    def test_zero_thickness_rejected(self):
        body = BRepBody()
        _install(body, FakeShellFeatures(body))
        msg = error_message(sh.handler(thickness=0, units="mm"))
        assert "thickness" in msg and "non-zero" in msg

    def test_negative_thickness_rejected(self):
        body = BRepBody()
        _install(body, FakeShellFeatures(body))
        msg = error_message(sh.handler(thickness=-1, units="mm"))
        assert "thickness" in msg and "positive" in msg

    def test_missing_named_body_reports_name(self):
        body = BRepBody(name="Real")
        _install(body, FakeShellFeatures(body))
        res = sh.handler(body_name="Ghost", thickness=1, units="mm")
        assert res["isError"] is True and "Ghost" in res["message"]

    def test_wrong_kind_body_redirects(self):
        surf = BRepBody(name="Surf", is_solid=False)
        _install(surf, FakeShellFeatures(surf))
        res = sh.handler(body_name="Surf", thickness=1, units="mm")
        assert res["isError"] is True and "SOLID" in res["message"]


# ── honesty ──────────────────────────────────────────────────────────────────

class TestHonesty:
    def test_unchanged_body_reports_error_not_ok(self):
        # add() returns a feature but leaves volume/faces identical - a silent no-op the API still
        # calls success. The handler must catch that and return isError, never a false 'shelled'.
        body = BRepBody(volume=100.0, face_count=6)
        sf = FakeShellFeatures(body, volume_delta=0.0, faces_added=0)
        _install(body, sf)
        res = sh.handler(thickness=2, units="mm")
        assert res["isError"] is True
        assert "volume 100 -> 100 cm3" in res["message"]
        assert "Shell1" in res["message"] and "Nothing was rolled back" in res["message"]

    def test_the_no_op_error_states_the_read_and_not_a_guessed_cause(self):
        # Nothing in the handler reads the thickness against the geometry, so naming the thickness
        # as the reason is a claim no read backs; the numbers it did read, and the reads that would
        # show what happened, are what the message can stand behind.
        body = BRepBody(volume=100.0, face_count=6)
        _install(body, FakeShellFeatures(body, volume_delta=0.0, faces_added=0))
        msg = sh.handler(thickness=2, units="mm")["message"]
        assert "likely" not in msg and "too large" not in msg
        assert "model_inspect" in msg and "view_section" in msg

    def test_an_unreadable_volume_convicts_on_the_face_count_it_did_read(self):
        # Only ONE of the two reads runs: with no volume to compare, the face count is what
        # convicts, so claiming "volume and face count identical" would name a read that never
        # happened.
        class _NoVolumeBody(BRepBody):
            @property
            def volume(self):
                raise RuntimeError("volume unreadable")

            @volume.setter
            def volume(self, _value):
                pass

        body = _NoVolumeBody(face_count=6)
        _install(body, FakeShellFeatures(body, volume_delta=0.0, faces_added=0))
        res = sh.handler(thickness=2, units="mm")
        assert res["isError"] is True
        assert "faces 6 -> 6" in res["message"]
        assert "volume" not in res["message"]

    def test_outward_shell_accepts_the_recorded_volume_increase(self):
        body = BRepBody(name="Cube10mm", volume=1.0, face_count=6)
        _install(body, FakeShellFeatures(body, volume_delta=-0.744, faces_added=6))
        out = _payload(sh.handler(thickness=2, direction="outside"))
        assert out["shelled"] is True and body.volume == 1.744
        assert out["outside_thickness"] == 2.0 and out["volume_removed_cm3"] == -0.744

    def test_equal_volume_outward_shell_requires_another_geometry_change(self):
        body = BRepBody(volume=1.0, face_count=6)
        _install(body, FakeShellFeatures(body, volume_delta=0.0, faces_added=6))
        assert _payload(sh.handler(thickness=1, direction="outside"))["faces_delta"] == 6
        body = BRepBody(volume=1.0, face_count=6)
        _install(body, FakeShellFeatures(body, volume_delta=0.0, faces_added=0))
        assert sh.handler(thickness=1, direction="outside")["isError"] is True

    def test_inside_volume_increase_is_not_rescued_by_more_faces(self):
        body = BRepBody(volume=1.0, face_count=6)
        _install(body, FakeShellFeatures(body, volume_delta=-0.744, faces_added=6))
        res = sh.handler(thickness=2, direction="inside")
        assert res["isError"] is True
        assert "effect is unverified" in res["message"] and "faces 6 -> 12" in res["message"]

    def test_submicron_thickness_rounding_does_not_refuse_a_matching_feature(self):
        body = BRepBody(volume=1.0, face_count=6)
        _install(body, FakeShellFeatures(body, volume_delta=0.2))
        assert _payload(sh.handler(thickness=1.23456789))["shelled"] is True

    def test_changed_geometry_with_wrong_thickness_is_unverified(self, monkeypatch):
        body = BRepBody(volume=1.0, face_count=6)
        features = FakeShellFeatures(body, volume_delta=-0.744)
        original_add = features.add
        def add(inp):
            feature = original_add(inp)
            feature.outsideThickness.value = 0.1
            return feature
        monkeypatch.setattr(features, "add", add)
        _install(body, features)
        result = sh.handler(thickness=2, direction="outside")
        assert result["isError"] and "unverified" in result["message"]
        assert body.volume == 1.744 and "Nothing was rolled back" in result["message"]

    def test_no_feature_returned_is_error(self):
        body = BRepBody()
        sf = FakeShellFeatures(body, return_feature=False)
        _install(body, sf)
        res = sh.handler(thickness=1, units="mm")
        assert res["isError"] is True and "no feature" in res["message"].lower()

    def test_add_raising_surfaces_as_error(self):
        body = BRepBody()
        sf = FakeShellFeatures(body)
        sf.add = lambda inp: (_ for _ in ()).throw(RuntimeError("thickness too large"))
        _install(body, sf)
        res = sh.handler(thickness=999, units="mm")
        assert res["isError"] is True and "Shell failed" in res["message"]

    def test_the_raise_is_reported_with_the_platforms_own_cause_and_no_guessed_one(self):
        # Nothing here measures the thickness against the geometry or which bodies the removed
        # faces span, so naming either as the reason is a claim no read backs. The raise text is
        # the one cause that was read, and it is the whole message.
        body = BRepBody()
        sf = FakeShellFeatures(body)
        sf.add = lambda inp: (_ for _ in ()).throw(RuntimeError("3-4 : InternalValidationError"))
        _install(body, sf)
        msg = sh.handler(thickness=999, units="mm")["message"]
        assert msg == "Shell failed: 3-4 : InternalValidationError"
        assert "may be" not in msg and "try a smaller" not in msg


# ── declared output contract ─────────────────────────────────────────────────

class TestOutputContract:
    def test_feature_output_is_minted(self):
        body = BRepBody(name="Block")
        _install(body, FakeShellFeatures(body))
        out = _payload(sh.handler(thickness=2, units="mm"))
        assert sh.RETURNS[0].assert_present(out) == ""
