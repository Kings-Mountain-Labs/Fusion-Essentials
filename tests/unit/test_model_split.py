"""Unit tests for ``model_split.py`` - SplitBody / SplitFace dispatched by 'split'.

Pinned here (no live Fusion): the split dispatch, the exactly-one-cutter guard (neither / both), the
no-active-design guard, resolution-error propagation, and the honesty verifiers - a body split that
yields a SINGLE body is reported as an error (no silent ok), a face split that creates no new faces is
an error, and the resulting body count / net face-count increase are read back and reported.
"""

from conftest import (load_tool, make_design, install, MakeComp, BRepBody, BRepFace, entity_proxy,
                      go_stale, payload, error_message, assert_no_active_design,
                      _NamedCollection)

sp = load_tool("model_split")


# ── body-split fakes ────────────────────────────────────────────────────────

class FakeSplitBodyFeature:
    def __init__(self, result_names=("Body1", "Body2"), health=0):
        self.name = "Split1"
        self.bodies = _NamedCollection([BRepBody(name=n) for n in result_names])
        self.healthState = health
        self.errorOrWarningMessage = "no intersection"


class FakeSplitBodyFeatures:
    """`returns_nothing` models the return with NO feature object in it - the measured direct-mode
    shape, where `pieces_added` new bodies still land in `host` (a MakeComp), which is the only
    evidence a split has when no feature comes back. The target body then goes stale - a WORST CASE
    (a held body was measured still answering parentComponent after a direct split), pinning that
    the handler must not DEPEND on reading identity back after the mutation."""
    def __init__(self, feature=None, returns_nothing=False, host=None, pieces_added=0,
                 census_unreadable=False):
        self.last = None
        self._feature = feature
        self.returns_nothing = returns_nothing
        self.host = host
        self.pieces_added = pieces_added
        self.census_unreadable = census_unreadable

    def createInput(self, body, tool, extend):
        self.last = (body, tool, extend)
        return type("I", (), {})()

    def add(self, inp):
        if self.host is not None:
            for i in range(self.pieces_added):
                self.host.bRepBodies._items.append(BRepBody(name=f"Piece{i}"))
            if self.census_unreadable:
                # the collection stops answering AFTER the split - the after-count is unavailable
                go_stale(self.host, attrs=("bRepBodies",))
        if self.last is not None:
            go_stale(self.last[0])
        if self.returns_nothing:
            return None
        return self._feature if self._feature is not None else FakeSplitBodyFeature()


# ── face-split fakes ────────────────────────────────────────────────────────

class FakeSplitFaceFeature:
    def __init__(self, created=2, health=0):
        self.name = "SplitFace1"
        self.faces = _NamedCollection([None] * created)
        self.healthState = health
        self.errorOrWarningMessage = ""


class FakeSplitFaceFeatures:
    """`returns_nothing` models the return with NO feature object in it - the measured direct-mode
    shape, where the new faces still land on the owning body."""
    def __init__(self, bumps=1, feature=None, returns_nothing=False):
        self.last = None
        self._bumps = bumps        # how many faces the split adds to the owning body
        self._feature = feature
        self._body = None
        self.returns_nothing = returns_nothing

    def bind_body(self, body):
        self._body = body

    def createInput(self, faces, tool, extend):
        self.last = (faces, tool, extend)
        return type("I", (), {})()

    def add(self, inp):
        if self._body is not None:
            # the split grows the owning body's face count - ONCE, however many of its faces were
            # targeted
            self._body.faces._items.extend([None] * self._bumps)
        if self.returns_nothing:
            return None
        return self._feature if self._feature is not None else FakeSplitFaceFeature()


def _faces_on_one_body(count, face_count=6, token="Box1"):
    """(body, [face, ...]) for `count` faces of ONE body, each holding its OWN proxy - the measured
    shape of face.body (see conftest entity_proxy). A fake handing every face the same object
    cannot tell an entityToken dedupe from an id() one, which is how a per-face double-count hides."""
    body = BRepBody(name=token, entity_token=token, face_count=face_count)
    faces = [BRepFace(None, body=entity_proxy(body)) for _ in range(count)]
    return body, faces


def _comp_with(body_feats=None, face_feats=None, bodies=()):
    comp = MakeComp(name="Comp", bodies=list(bodies))
    comp.features = type("F", (), {
        "splitBodyFeatures": body_feats if body_feats is not None else FakeSplitBodyFeatures(),
        "splitFaceFeatures": face_feats if face_feats is not None else FakeSplitFaceFeatures(),
    })()
    return comp


def _wire_body(monkeypatch, feature=None, target=("body-obj", None), cutter=("plane", None),
               feats=None, design_type=None, active_bodies=()):
    """`design_type` sets the modelling mode current_design_type reads (1 parametric, 0 direct);
    left unset the design reports neither, which is the 'unknown' mode. `active_bodies` stocks the
    ACTIVE component, which a census must not silently fall back onto."""
    feats = feats if feats is not None else FakeSplitBodyFeatures(feature)
    design = make_design(comp=_comp_with(body_feats=feats, bodies=active_bodies))
    if design_type is not None:
        design.designType = design_type
    install(sp, design)
    monkeypatch.setattr(sp._TARGET, "resolve", lambda raw: target)
    monkeypatch.setattr(sp._PLANE, "resolve", lambda raw: cutter)
    monkeypatch.setattr(sp._TOOLBODY, "resolve", lambda raw: cutter)
    return feats


def _wire_face(monkeypatch, feats, faces, cutter=("plane", None), design_type=None):
    design = make_design(comp=_comp_with(face_feats=feats))
    if design_type is not None:
        design.designType = design_type
    install(sp, design)
    monkeypatch.setattr(sp._FACES, "resolve", lambda raw: (faces, None))
    monkeypatch.setattr(sp._PLANE, "resolve", lambda raw: cutter)
    return feats


class TestCutterGuard:
    def test_no_cutter_is_error(self, monkeypatch):
        _wire_body(monkeypatch)
        msg = error_message(sp.handler(split="body", target="Body1"))
        assert "No cutter" in msg

    def test_both_cutters_is_error(self, monkeypatch):
        _wire_body(monkeypatch)
        msg = error_message(sp.handler(split="body", target="Body1",
                                       split_plane="xy", split_tool_body="ToolBody"))
        assert "not both" in msg

    def test_bad_split_kind_is_error(self, monkeypatch):
        _wire_body(monkeypatch)
        msg = error_message(sp.handler(split="chunks", target="Body1", split_plane="xy"))
        assert "split" in msg

    def test_no_active_design(self, monkeypatch):
        _wire_body(monkeypatch)
        assert_no_active_design(sp, sp.handler, split="body", target="Body1", split_plane="xy")


class TestSplitBody:
    def test_two_bodies_is_ok(self, monkeypatch):
        _wire_body(monkeypatch, feature=FakeSplitBodyFeature(("A", "B")))
        out = payload(sp.handler(split="body", target="Body1", split_plane="xy"))
        assert out["split"] == "body"
        assert out["result_count"] == 2
        assert out["result_bodies"] == ["A", "B"]
        assert out["feature"] == "Split1"

    def test_single_body_is_error_not_silent_ok(self, monkeypatch):
        # The cutter did not divide the body: one resulting body -> honesty demands isError.
        _wire_body(monkeypatch, feature=FakeSplitBodyFeature(("OnlyOne",)))
        res = sp.handler(split="body", target="Body1", split_plane="xy")
        assert res["isError"] is True and "did not divide" in res["message"]

    def test_target_resolution_error_propagates(self, monkeypatch):
        _wire_body(monkeypatch, target=(None, "'target': no body named 'X'."))
        msg = error_message(sp.handler(split="body", target="X", split_plane="xy"))
        assert "no body named 'X'" in msg

    def test_cutter_via_tool_body(self, monkeypatch):
        feats = _wire_body(monkeypatch, cutter=("surf-body", None))
        payload(sp.handler(split="body", target="Body1", split_tool_body="Surf"))
        assert feats.last[1] == "surf-body"        # the resolved tool body reached createInput

    def test_health_error_reported(self, monkeypatch):
        _wire_body(monkeypatch, feature=FakeSplitBodyFeature(("A", "B"), health=2))
        res = sp.handler(split="body", target="Body1", split_plane="xy")
        assert res["isError"] is True and "failed to compute" in res["message"]

    def test_declared_outputs_present(self, monkeypatch):
        _wire_body(monkeypatch, feature=FakeSplitBodyFeature(("A", "B")))
        out = payload(sp.handler(split="body", target="Body1", split_plane="xy"))
        for o in sp.RETURNS:
            assert o.assert_present(out) == "", o.key


# ── DIRECT mode: splitBodyFeatures.add returns nothing while the split LANDS (measured) ──────

class TestDirectModeNoFeature:
    def _wire(self, monkeypatch, pieces_added, design_type=0, census_unreadable=False):
        # The target body lives in its OWN component, not the active one - measured: a direct split
        # put the new piece in the target's parentComponent (1 -> 2) while the active root held at
        # 3. The active component is stocked with decoys so a census that falls back to it (or
        # re-derives the host after the mutation) fabricates a piece count loudly.
        host = MakeComp(name="Host", bodies=[f"B{i}" for i in range(7)])
        feats = FakeSplitBodyFeatures(returns_nothing=True, host=host, pieces_added=pieces_added,
                                      census_unreadable=census_unreadable)
        _wire_body(monkeypatch, feats=feats,
                   target=(BRepBody(name="Body1", parent_component=host), None),
                   design_type=design_type, active_bodies=[f"Other{i}" for i in range(20)])
        return feats

    def test_direct_none_counts_the_pieces_from_the_body_census(self, monkeypatch):
        # 7 bodies -> 8: the one target became 2 pieces.
        self._wire(monkeypatch, pieces_added=1)
        out = payload(sp.handler(split="body", target="Body1", split_plane="xy"))
        assert out["result_count"] == 2

    def test_direct_none_publishes_no_feature_name_and_no_empty_body_list(self, monkeypatch):
        self._wire(monkeypatch, pieces_added=1)
        out = payload(sp.handler(split="body", target="Body1", split_plane="xy"))
        assert "feature" not in out
        # result_bodies is read off feature.bodies - with no feature there is nothing to list, so
        # the key must be ABSENT, never an empty list beside result_count 2.
        assert "result_bodies" not in out
        assert out["no_timeline_feature"] is True
        assert "DIRECT mode" in out["note"] and "COUNTED" in out["note"]

    def test_direct_none_names_the_target_captured_before_the_split(self, monkeypatch):
        # The proxy stops answering .name once the split ran; the payload must still name the body.
        self._wire(monkeypatch, pieces_added=1)
        out = payload(sp.handler(split="body", target="Body1", split_plane="xy"))
        assert out["target"] == "Body1"

    def test_declared_outputs_hold_on_the_direct_path(self, monkeypatch):
        self._wire(monkeypatch, pieces_added=1)
        out = payload(sp.handler(split="body", target="Body1", split_plane="xy"))
        for o in sp.RETURNS:
            assert o.assert_present(out) == "", o.key

    def test_direct_none_with_an_unchanged_census_is_an_error(self, monkeypatch):
        # add() handed back nothing AND the body count did not move: the cutter divided nothing.
        self._wire(monkeypatch, pieces_added=0)
        res = sp.handler(split="body", target="Body1", split_plane="xy")
        assert res["isError"] is True and "did not divide 'Body1'" in res["message"]
        # No timeline, no feature - the remedy must not send the caller to design_delete_feature.
        assert "design_delete_feature" not in res["message"]
        assert "undo in Fusion" in res["message"]

    def test_direct_none_with_an_unreadable_census_is_an_error(self, monkeypatch):
        # No feature AND no census: the split is UNVERIFIED, which is not a success.
        self._wire(monkeypatch, pieces_added=1, census_unreadable=True)
        res = sp.handler(split="body", target="Body1", split_plane="xy")
        assert res["isError"] is True and "UNVERIFIED" in res["message"]

    def test_parametric_none_stays_an_error(self, monkeypatch):
        # Even with the census showing new bodies: a None feature in a PARAMETRIC design is
        # unmeasured as a success, so it is refused.
        self._wire(monkeypatch, pieces_added=1, design_type=1)
        res = sp.handler(split="body", target="Body1", split_plane="xy")
        assert res["isError"] is True and "returned no feature" in res["message"]
        assert "DIRECT mode" not in res["message"]


class TestSplitFace:
    def test_face_delta_reported(self, monkeypatch):
        # TWO faces of ONE body, each holding its own proxy (the measured shape of face.body). The
        # owning body is counted ONCE: an id()-keyed dedupe keeps it twice and doubles the delta.
        body, faces = _faces_on_one_body(2, face_count=6)
        feats = FakeSplitFaceFeatures(bumps=1, feature=FakeSplitFaceFeature(created=2))
        feats.bind_body(body)
        _wire_face(monkeypatch, feats, faces)
        out = payload(sp.handler(split="face", faces=["a", "b"], split_plane="xy"))
        assert out["split"] == "face"
        assert out["faces_targeted"] == 2
        assert out["faces_created"] == 2
        assert out["result_count"] == 1        # 6 -> 7 net increase on the owning body, counted once

    def test_result_count_is_the_true_delta_however_many_faces_share_a_body(self, monkeypatch):
        # result_count is the body's OWN face-count increase, however many of its faces were
        # targeted. face.body hands back a fresh proxy per read, so the owning body is deduped by
        # entityToken: keyed by identity it would be counted once per face and its delta summed
        # that many times (3 faces, delta 3 -> 9).
        body, faces = _faces_on_one_body(3, face_count=6)
        feats = FakeSplitFaceFeatures(bumps=3, feature=FakeSplitFaceFeature(created=6))
        feats.bind_body(body)
        _wire_face(monkeypatch, feats, faces)
        out = payload(sp.handler(split="face", faces=["a", "b", "c"], split_plane="xy"))
        assert out["result_count"] == 3        # NOT 9 - one body, counted once

    def test_no_new_faces_is_error(self, monkeypatch):
        body, faces = _faces_on_one_body(1, face_count=6)
        feats = FakeSplitFaceFeatures(bumps=0, feature=FakeSplitFaceFeature(created=0))
        feats.bind_body(body)
        _wire_face(monkeypatch, feats, faces)
        res = sp.handler(split="face", faces=["a"], split_plane="xy")
        assert res["isError"] is True and "no new faces" in res["message"]

    def test_missing_faces_is_error(self, monkeypatch):
        feats = FakeSplitFaceFeatures()
        _wire_face(monkeypatch, feats, [])
        msg = error_message(sp.handler(split="face", faces=[], split_plane="xy"))
        assert "'faces' is required" in msg

    def test_direct_none_counts_the_new_faces_and_names_no_feature(self, monkeypatch):
        # The before/after face census on the owning body needs no feature object, so a direct
        # design's None return still yields an honest verdict - without a fabricated feature name.
        body, faces = _faces_on_one_body(2, face_count=6)
        feats = FakeSplitFaceFeatures(bumps=3, returns_nothing=True)
        feats.bind_body(body)
        _wire_face(monkeypatch, feats, faces, design_type=0)
        out = payload(sp.handler(split="face", faces=["a", "b"], split_plane="xy"))
        # the direct branch has NO feature.faces fallback, so this census IS the published number
        assert out["result_count"] == 3 and "feature" not in out
        # faces_created is read off feature.faces - with no feature it must be ABSENT, never a 0
        # sitting beside result_count 3.
        assert "faces_created" not in out
        assert out["no_timeline_feature"] is True
        assert "DIRECT mode" in out["note"]
        for o in sp.RETURNS:
            assert o.assert_present(out) == "", o.key

    def test_direct_none_with_an_unreadable_face_census_is_unverified_not_diagnosed(self, monkeypatch):
        # Nothing could be read, so the cutter's behaviour is UNKNOWN - claiming "did not cross the
        # faces" would diagnose something never measured.
        # a deliberately bare stub: no .body attribute at all, so no owning body resolves and the
        # face census has nothing to read. Not a fake body - the absence IS the fixture.
        faces = [type("Fc", (), {})()]
        feats = FakeSplitFaceFeatures(bumps=0, returns_nothing=True)
        _wire_face(monkeypatch, feats, faces, design_type=0)
        res = sp.handler(split="face", faces=["a"], split_plane="xy")
        assert res["isError"] is True and "UNVERIFIED" in res["message"]
        assert "did not cross" not in res["message"]

    def test_direct_none_no_new_faces_points_at_undo_not_the_timeline(self, monkeypatch):
        body, faces = _faces_on_one_body(1, face_count=6)
        feats = FakeSplitFaceFeatures(bumps=0, returns_nothing=True)
        feats.bind_body(body)
        _wire_face(monkeypatch, feats, faces, design_type=0)
        res = sp.handler(split="face", faces=["a"], split_plane="xy")
        assert res["isError"] is True and "no new faces" in res["message"]
        assert "design_delete_feature" not in res["message"]
        assert "undo in Fusion" in res["message"]

    def test_faces_reach_createinput_as_collection(self, monkeypatch):
        body, faces = _faces_on_one_body(1, face_count=4)
        feats = FakeSplitFaceFeatures(bumps=1)
        feats.bind_body(body)
        _wire_face(monkeypatch, feats, faces)
        payload(sp.handler(split="face", faces=["a"], split_plane="xy"))
        assert feats.last[0].count == 1        # the ObjectCollection carries the one face
