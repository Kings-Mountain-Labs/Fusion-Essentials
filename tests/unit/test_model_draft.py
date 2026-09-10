"""Unit tests for ``model_draft.py`` - taper faces to a pull direction (the Draft feature).

Pinned here (no live Fusion): the angle guards (non-number / zero / out-of-range), that the resolved
faces + pull plane reach DraftFeatures.createInput, that 'symmetric' flows into setSingleAngle, the
drafted-face read-back, the no-active-design guard, resolution-error propagation, and the honesty
verifier that a feature which computes with a health ERROR is reported as failure, not a false ok.
"""

from conftest import (BRepBody, BRepFace, load_tool, make_design, install, MakeComp, payload,
                      error_message, assert_no_active_design, _NamedCollection)

dr = load_tool("model_draft")


# ── fakes: the draftFeatures collection + input + created feature ───────────────────────────────

class FakeDraftInput:
    def __init__(self, faces, plane, tangent):
        self.faces = faces
        self.plane = plane
        self.isTangentChain = tangent
        self.isDirectionFlipped = False
        self.single = None

    def setSingleAngle(self, symmetric, angle):
        self.single = (symmetric, angle)
        return True


class FakeDraftFeature:
    def __init__(self, name="Draft1", n_faces=2, health=0):
        self.name = name
        # DraftFeature.faces - the faces the draft created or modified, which is the count the tool
        # reads back (its `inputFaces` raises on this platform).
        self.faces = _NamedCollection([None] * n_faces)
        self.healthState = health
        self.errorOrWarningMessage = "geometry undercut"


class FakeDraftFeatures:
    def __init__(self, feature=None):
        self.last = None
        self._feature = feature

    def createInput(self, faces, plane, tangent):
        self.last = FakeDraftInput(faces, plane, tangent)
        return self.last

    def add(self, inp):
        return self._feature if self._feature is not None else FakeDraftFeature()


def _wire(monkeypatch, feature=None, faces=None, plane=("plane", "xy")):
    """Install a design whose component carries a fake draftFeatures, and stub the two input kinds so
    the handler gets canned entities (the kinds' own resolution is covered by test_inputs)."""
    feats = FakeDraftFeatures(feature)
    comp = MakeComp(name="Comp")
    comp.features = type("F", (), {"draftFeatures": feats})()
    install(dr, make_design(comp=comp))
    resolved_faces = faces if faces is not None else [object(), object()]
    monkeypatch.setattr(dr._FACES, "resolve", lambda raw: (resolved_faces, None))
    monkeypatch.setattr(dr._PULL, "resolve", lambda raw: (plane, None))
    return feats


class TestGuards:
    def test_angle_not_a_number(self, monkeypatch):
        _wire(monkeypatch)
        msg = error_message(dr.handler(faces=["h"], pull_direction="xy", angle_deg="wide"))
        assert "angle_deg" in msg and "number" in msg

    def test_zero_angle_rejected(self, monkeypatch):
        _wire(monkeypatch)
        msg = error_message(dr.handler(faces=["h"], pull_direction="xy", angle_deg=0))
        assert "non-zero" in msg

    def test_angle_out_of_range_rejected(self, monkeypatch):
        _wire(monkeypatch)
        msg = error_message(dr.handler(faces=["h"], pull_direction="xy", angle_deg=90))
        assert "-90 and 90" in msg

    def test_no_active_design(self, monkeypatch):
        _wire(monkeypatch)
        assert_no_active_design(dr, dr.handler, faces=["h"], pull_direction="xy", angle_deg=3)

    def test_face_resolution_error_propagates(self, monkeypatch):
        _wire(monkeypatch)
        monkeypatch.setattr(dr._FACES, "resolve", lambda raw: (None, "'faces' must be a face, but the handle points at a BRepEdge."))
        msg = error_message(dr.handler(faces=["edge"], pull_direction="xy", angle_deg=3))
        assert "must be a face" in msg

    def test_pull_direction_error_propagates(self, monkeypatch):
        _wire(monkeypatch)
        monkeypatch.setattr(dr._PULL, "resolve", lambda raw: (None, "'pull_direction' is required (a plane alias, name, or handle)."))
        msg = error_message(dr.handler(faces=["h"], pull_direction="", angle_deg=3))
        assert "pull_direction" in msg


class TestDraft:
    def test_happy_path_reports_read_back_count(self, monkeypatch):
        feats = _wire(monkeypatch, feature=FakeDraftFeature(n_faces=3),
                      faces=[object(), object()])
        out = payload(dr.handler(faces=["a", "b"], pull_direction="xy", angle_deg=5))
        assert out["drafted"] is True
        assert out["feature"] == "Draft1"
        assert out["faces_requested"] == 2      # two handles resolved
        assert out["faces_drafted"] == 3        # read off the feature's own faces, not the request
        assert out["angle_deg"] == 5

    def test_createinput_gets_faces_and_plane(self, monkeypatch):
        resolved = [object(), object()]
        feats = _wire(monkeypatch, faces=resolved, plane=("plane", "xy"))
        payload(dr.handler(faces=["a", "b"], pull_direction="xy", angle_deg=5))
        assert feats.last.faces == resolved         # a Python list, per the live signature
        assert feats.last.plane == ("plane", "xy")

    def test_symmetric_flows_into_set_single_angle(self, monkeypatch):
        feats = _wire(monkeypatch)
        payload(dr.handler(faces=["a"], pull_direction="xy", angle_deg=5, symmetric=True))
        assert feats.last.single is not None and feats.last.single[0] is True

    def test_default_not_symmetric(self, monkeypatch):
        feats = _wire(monkeypatch)
        payload(dr.handler(faces=["a"], pull_direction="xy", angle_deg=5))
        assert feats.last.single[0] is False

    def test_flip_sets_direction_flipped(self, monkeypatch):
        feats = _wire(monkeypatch)
        payload(dr.handler(faces=["a"], pull_direction="xy", angle_deg=5, flip=True))
        assert feats.last.isDirectionFlipped is True

    def test_health_error_reported_not_false_ok(self, monkeypatch):
        # The feature was ADDED but computes with a health ERROR (2) - honesty demands isError, not ok.
        _wire(monkeypatch, feature=FakeDraftFeature(health=2))
        res = dr.handler(faces=["a"], pull_direction="xy", angle_deg=5)
        assert res["isError"] is True
        assert "failed to compute" in res["message"] and "geometry undercut" in res["message"]

    def test_add_returns_none_is_error(self, monkeypatch):
        feats = _wire(monkeypatch)
        feats.add = lambda inp: None
        res = dr.handler(faces=["a"], pull_direction="xy", angle_deg=5)
        assert res["isError"] is True and "no feature" in res["message"]

    def test_declared_outputs_present(self, monkeypatch):
        _wire(monkeypatch)
        out = payload(dr.handler(faces=["a"], pull_direction="xy", angle_deg=5))
        for o in dr.RETURNS:
            assert o.assert_present(out) == "", o.key


# ── the taper must MOVE material, and the face count is READ, never echoed ──────────────────────
#
# A draft can compute cleanly and taper nothing (a face already parallel to the pull direction, a
# flipped direction that lands back on itself). The feature object reads identically either way, so
# the owning bodies' volumes on both sides of the add are the only evidence.

def _face_on(body):
    """A face whose owning body is `body` - the chain _geom.owning_bodies walks to find what the
    draft must move."""
    return BRepFace(surface=None, body=body)


def _add_moving_volume(feats, feature, *changes):
    """Make draftFeatures.add apply (body, new_volume) pairs - the taper's material effect between
    the pre- and post-mutation reads."""
    def _add(inp):
        for body, volume in changes:
            body.volume = volume
        return feature
    feats.add = _add


class TestTaperMovesMaterial:
    def test_draft_that_moves_no_volume_is_an_error(self, monkeypatch):
        bar = BRepBody("Bar", volume=20.0)
        _wire(monkeypatch, faces=[_face_on(bar)])
        res = dr.handler(faces=["a"], pull_direction="xy", angle_deg=5)
        assert res["isError"] is True
        assert "tapered nothing" in res["message"] and "Bar" in res["message"]
        assert "design_delete_feature" in res["message"]

    def test_draft_that_moved_material_publishes_the_delta(self, monkeypatch):
        bar = BRepBody("Bar", volume=20.0)
        feature = FakeDraftFeature(n_faces=1)
        feats = _wire(monkeypatch, feature=feature, faces=[_face_on(bar)])
        _add_moving_volume(feats, feature, (bar, 18.5))
        out = payload(dr.handler(faces=["a"], pull_direction="xy", angle_deg=5))
        assert out["drafted"] is True and out["volume_delta_cm3"] == -1.5

    def test_symmetric_draft_is_never_volume_gated(self, monkeypatch):
        # A symmetric draft tapers both sides of the pull plane in OPPOSITE directions, so the two
        # wedges can cancel on a taper that really happened - the delta cannot carry a no-op verdict.
        bar = BRepBody("Bar", volume=20.0)
        _wire(monkeypatch, faces=[_face_on(bar)])
        out = payload(dr.handler(faces=["a"], pull_direction="xy", angle_deg=5, symmetric=True))
        assert out["drafted"] is True and out["volume_delta_cm3"] == 0.0

    def test_faces_with_no_readable_body_neither_error_nor_publish_a_delta(self, monkeypatch):
        # Cannot measure is not "measured the same": no verdict, and no null delta that would read
        # as a measured zero.
        _wire(monkeypatch, faces=[object()])
        out = payload(dr.handler(faces=["a"], pull_direction="xy", angle_deg=5))
        assert out["drafted"] is True and "volume_delta_cm3" not in out


class TestDraftedCountIsRead:
    def test_unreadable_count_is_null_not_the_request(self, monkeypatch):
        # The bug this pins: falling back to len(faces) publishes the REQUEST as though the feature
        # had confirmed it, so a tangent-chain expansion (or a draft that took nothing) is invisible.
        feature = FakeDraftFeature(n_faces=1)
        feature.faces = None
        _wire(monkeypatch, feature=feature, faces=[object(), object()])
        out = payload(dr.handler(faces=["a", "b"], pull_direction="xy", angle_deg=5))
        assert out["faces_drafted"] is None
        assert out["faces_requested"] == 2
        assert "'faces_drafted' is null" in out["note"] and "2 face(s) were requested" in out["note"]
