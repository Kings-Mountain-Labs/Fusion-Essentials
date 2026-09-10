"""Unit tests for surface_delete_face - delete faces, optionally healing, with a face-count read-back.

Pins: heal=true routes to deleteFaceFeatures (heal) and heal=false to surfaceDeleteFaceFeatures (no
heal); the body face-count delta is reported; a delete that consumes a whole body is reported (not a
bare success); a heal that raises / returns null is an error naming the heal=false fallback; missing
faces is rejected.
"""

import types

import adsk.fusion
import pytest

from conftest import (load_tool, make_design, install, entity_proxy, go_stale, payload,
                      error_message, MakeComp, BRepBody, BRepFace, FakeFeature)

sdf = load_tool("surface_delete_face")


class _UnreadableFacesBody(BRepBody):
    """Present and named, but its faces collection will not enumerate - the read `safe(..., 0)`
    turns into a confident 'this body has zero faces'."""
    @property
    def faces(self):
        raise RuntimeError("4 : An API Object refers to a deleted Object")

    @faces.setter
    def faces(self, value):
        pass


def _body(name="Srf1", is_solid=False, face_count=0, faces=None, entity_token=None, cls=BRepBody):
    """A body whose entityToken reads, because _geom.owning_bodies dedupes on it and only falls
    back to identity when there is none. Each face gets its OWN proxy of the body - the measured
    shape of face.body - so the token path is what these tests exercise; sharing one object would
    make them pass under either keying."""
    faces = list(faces) if faces is not None else [BRepFace(None) for _ in range(face_count)]
    body = cls(name, is_solid=is_solid, entity_token=entity_token, faces=faces)
    for f in faces:
        if f.body is None:
            f.body = entity_proxy(body)
    return body


class _Feature(FakeFeature):
    """A DeleteFaceFeature - the shared feature under this tool's result name."""
    def __init__(self, name="DeleteFace1", bodies=()):
        super().__init__(name=name, bodies=bodies)


class _DelFeatures:
    def __init__(self, result):
        self._result = result
        self.calls = 0
    def add(self, coll):
        self.calls += 1
        r = self._result
        return r(coll) if callable(r) else r


class _Raises:
    def __init__(self):
        self.calls = 0
    def add(self, coll):
        self.calls += 1
        raise RuntimeError("body cannot be healed")


class _DirectDelFeatures:
    """The MEASURED direct-mode shape: add() returns NO feature object while the delete LANDS - a
    healed fillet face took a box from 7 faces to 6. `faces_removed` drops that many faces off each
    input body; `count_unreadable` additionally kills the face read, leaving no evidence at all."""
    def __init__(self, bodies, faces_removed=1, count_unreadable=False):
        self.bodies = list(bodies)
        self.faces_removed = faces_removed
        self.count_unreadable = count_unreadable
        self.calls = 0

    def add(self, coll):
        self.calls += 1
        for b in self.bodies:
            if self.faces_removed < 0:
                b.faces._items.extend([BRepFace(None, body=b) for _ in range(-self.faces_removed)])
            else:
                del b.faces._items[:self.faces_removed]
        if self.count_unreadable:
            go_stale(*self.bodies, attrs=("faces",))
        go_stale(*self.bodies)      # identity reads go stale; the face count is the effect check
        return None


@pytest.fixture(autouse=True)
def _types(monkeypatch):
    monkeypatch.setattr(adsk.fusion, "BRepFace", BRepFace, raising=False)
    monkeypatch.setattr(adsk.fusion, "BRepBody", BRepBody, raising=False)


def _wire(tokens, delete=None, surface_delete=None, design_type=None):
    """`design_type` sets the modelling mode current_design_type reads (1 parametric, 0 direct);
    left unset the design reports neither, which is the 'unknown' mode."""
    comp = MakeComp()
    comp.features = types.SimpleNamespace(
        deleteFaceFeatures=delete, surfaceDeleteFaceFeatures=surface_delete)
    design = make_design(comp=comp, tokens=tokens)
    if design_type is not None:
        design.designType = design_type
    install(sdf, design)
    return comp.features


def test_plain_delete_reports_face_count_delta():
    body = _body("Srf1", face_count=6)
    target = body.faces.item(0)
    result = _Feature(bodies=[_body("Srf1", face_count=5)])
    feats = _wire({"F1": target}, surface_delete=_DelFeatures(result), delete=_DelFeatures(result))
    out = payload(sdf.delete_face_handler(faces=["F1"], heal=False))
    assert out["heal"] is False
    assert out["faces_before"] == 6 and out["faces_after"] == 5
    assert out["bodies_consumed"] == 0
    assert feats.surfaceDeleteFaceFeatures.calls == 1
    assert feats.deleteFaceFeatures.calls == 0        # non-heal path only


def test_heal_routes_to_deleteFaceFeatures():
    body = _body("Solid1", is_solid=True, face_count=6)
    target = body.faces.item(0)
    result = _Feature(bodies=[_body("Solid1", is_solid=True, face_count=5)])
    feats = _wire({"F1": target}, delete=_DelFeatures(result), surface_delete=_DelFeatures(result))
    out = payload(sdf.delete_face_handler(faces=["F1"], heal=True))
    assert out["heal"] is True
    assert feats.deleteFaceFeatures.calls == 1
    assert feats.surfaceDeleteFaceFeatures.calls == 0
    # `heal` is an input flag, never read back: the note may say it was REQUESTED, not that the
    # opening was closed.
    assert "heal requested" in out["note"]
    assert "and healed the opening" not in out["note"]


def test_consumed_body_is_reported():
    body = _body("Srf1", face_count=1)
    target = body.faces.item(0)
    result = _Feature(bodies=[])            # no result body -> the body vanished
    _wire({"F1": target}, surface_delete=_DelFeatures(result))
    res = sdf.delete_face_handler(faces=["F1"], heal=False)
    assert res["isError"] is False           # reported, not hidden - but surfaced explicitly
    out = payload(res)
    assert out["bodies_consumed"] == 1
    assert "warning" in out and "consumed" in out["warning"]


def test_heal_failure_is_error_pointing_to_no_heal():
    body = _body("Solid1", is_solid=True, face_count=6)
    target = body.faces.item(0)
    _wire({"F1": target}, delete=_Raises())
    res = sdf.delete_face_handler(faces=["F1"], heal=True)
    msg = error_message(res)
    assert "heal=false" in msg


def test_heal_null_feature_is_error():
    body = _body("Solid1", is_solid=True, face_count=6)
    target = body.faces.item(0)
    _wire({"F1": target}, delete=_DelFeatures(lambda coll: None))
    res = sdf.delete_face_handler(faces=["F1"], heal=True)
    assert "heal=false" in error_message(res)


def test_faces_from_two_bodies_tracked():
    b1 = _body("Srf1", face_count=4)
    b2 = _body("Srf2", face_count=3)
    f1, f2 = b1.faces.item(0), b2.faces.item(0)
    result = _Feature(bodies=[_body("Srf1", face_count=3), _body("Srf2", face_count=2)])
    _wire({"F1": f1, "F2": f2}, surface_delete=_DelFeatures(result))
    out = payload(sdf.delete_face_handler(faces=["F1", "F2"], heal=False))
    assert sorted(out["input_bodies"]) == ["Srf1", "Srf2"]
    assert out["bodies_consumed"] == 0
    assert out["faces_requested"] == 2


def test_result_row_is_solid_is_null_when_the_flag_will_not_read():
    # informational, but still a published FLAG: bool(safe(...)) calls a body an open surface off a
    # read that failed, which is exactly what a delete-face caller inspects the row for.
    body = _body("Srf1", face_count=6)
    target = body.faces.item(0)
    survivor = _body("Srf1", face_count=5)
    del survivor.isSolid                              # the flag will not read at all
    result = _Feature(bodies=[survivor])
    _wire({"F1": target}, surface_delete=_DelFeatures(result))
    out = payload(sdf.delete_face_handler(faces=["F1"], heal=False))
    assert out["result_bodies"][0]["is_solid"] is None
    assert out["result_bodies"][0]["faces"] == 5      # the readable fields still publish


def test_result_row_is_solid_passes_a_readable_flag_through():
    # the boundary beside it: a flag that READ false stays false, not null.
    body = _body("Srf1", face_count=6)
    target = body.faces.item(0)
    result = _Feature(bodies=[_body("Srf1", is_solid=False, face_count=5)])
    _wire({"F1": target}, surface_delete=_DelFeatures(result))
    out = payload(sdf.delete_face_handler(faces=["F1"], heal=False))
    assert out["result_bodies"][0]["is_solid"] is False


def test_missing_faces_rejected():
    _wire({}, surface_delete=_DelFeatures(_Feature()))
    res = sdf.delete_face_handler(faces=None)
    assert res["isError"] is True
    assert "needs a list of geometry handles" in res["message"]


# ── PARAMETRIC: the face-count gate the direct path already ran ──────────────────────────────

class TestParametricFaceCountGate:
    """A feature object is not proof a face went. The parametric branch published
    "Deleted N face(s); body face count 26 -> 26" off the REQUEST; these pin the measured delta as
    the verdict, at the 0/-1 boundary."""

    def _run(self, before, after, heal=False, requested=1):
        body = _body("Srf1", face_count=before)
        targets = {"F%d" % i: body.faces.item(i) for i in range(requested)}
        result = _Feature(bodies=[_body("Srf1", face_count=after)])
        _wire(targets, surface_delete=_DelFeatures(result), delete=_DelFeatures(result))
        return sdf.delete_face_handler(faces=list(targets), heal=heal)

    def test_unchanged_face_count_is_an_error(self):
        res = self._run(before=26, after=26)
        assert res["isError"] is True
        msg = error_message(res)
        assert "no input body's face count changed" in msg and "26 -> 26" in msg
        assert "design_delete_feature" in msg        # parametric: there IS a feature to remove

    def test_one_face_fewer_is_the_boundary_that_passes(self):
        # delta -1 is the smallest real delete; the gate must bite at 0 and only at 0
        out = payload(self._run(before=26, after=25))
        assert out["faces_delta"] == -1
        assert out["faces_after"] == 25

    def test_the_note_reports_the_measured_delta_not_the_requested_count(self):
        # 3 faces requested, a heal that nets -1: the note must not read "Deleted 3 face(s)"
        out = payload(self._run(before=9, after=8, heal=True, requested=3))
        assert out["faces_delta"] == -1
        assert "9 -> 8" in out["note"]
        assert "3 face(s) requested" in out["note"]
        assert "Deleted 3 face" not in out["note"]

    def test_a_rising_face_count_is_flagged_not_narrated_as_a_delete(self):
        out = payload(self._run(before=7, after=9))
        assert out["faces_delta"] == 2
        assert "ROSE" in out["warning"]
        assert "The edit landed" in out["note"] and "The delete landed" not in out["note"]

    def test_an_unreadable_before_count_does_not_refuse(self):
        # No input body's face count read (total 0), so the delta is evidence of nothing - the
        # feature object is what this path is graded on, and a 0-vs-0 must not read as a no-op.
        target = BRepFace(None)
        _body("Srf1", faces=[target], cls=_UnreadableFacesBody)   # target.body now owns it
        result = _Feature(bodies=[_body("Srf1", face_count=0)])
        _wire({"F1": target}, surface_delete=_DelFeatures(result))
        out = payload(sdf.delete_face_handler(faces=["F1"], heal=False))
        assert out["faces_before"] == 0 and out["faces_after"] == 0


class TestParametricAfterCountMustRead:
    """The AFTER side of the same contract: a result body whose faces will not enumerate is not a
    body with zero faces. Coerced to 0 it drops the after-total by that body's whole count, and the
    verdict then reads the largest possible delete off a number nobody measured."""

    def test_one_unreadable_result_body_among_readable_ones_is_refused(self):
        b1 = _body("Srf1", face_count=13)
        b2 = _body("Srf2", face_count=13)
        result = _Feature(bodies=[_body("Srf1", face_count=12),
                                  _body("Srf2", face_count=12, cls=_UnreadableFacesBody)])
        _wire({"F1": b1.faces.item(0), "F2": b2.faces.item(0)},
              surface_delete=_DelFeatures(result))
        res = sdf.delete_face_handler(faces=["F1", "F2"], heal=False)
        assert res["isError"] is True
        msg = error_message(res)
        assert "1 result body(ies) (Srf2)" in msg
        assert "not a count of zero" in msg
        assert "UNVERIFIED" in msg

    def test_every_result_body_unreadable_is_refused(self):
        body = _body("Srf1", face_count=26)
        result = _Feature(bodies=[_body("Srf1", face_count=26, cls=_UnreadableFacesBody)])
        _wire({"F1": body.faces.item(0)}, surface_delete=_DelFeatures(result))
        res = sdf.delete_face_handler(faces=["F1"], heal=False)
        assert res["isError"] is True
        # the fabricated verdict this refusal replaces
        assert "26 -> 0" not in error_message(res)

    def test_every_result_body_readable_stays_a_success(self):
        # the boundary on the other side: zero unreadable bodies renders the verdict as before
        b1 = _body("Srf1", face_count=13)
        b2 = _body("Srf2", face_count=13)
        result = _Feature(bodies=[_body("Srf1", face_count=12), _body("Srf2", face_count=12)])
        _wire({"F1": b1.faces.item(0), "F2": b2.faces.item(0)},
              surface_delete=_DelFeatures(result))
        out = payload(sdf.delete_face_handler(faces=["F1", "F2"], heal=False))
        assert out["faces_before"] == 26 and out["faces_after"] == 24
        assert out["faces_delta"] == -2

    def test_a_consumed_body_still_reports_with_the_after_count_left_null(self):
        # the consumed branch owns the empty/short result set and keeps its warning; the after-count
        # it cannot measure is published as null rather than as a fabricated total
        b1 = _body("Srf1", face_count=6)
        b2 = _body("Srf2", face_count=6)
        result = _Feature(bodies=[_body("Srf1", face_count=5, cls=_UnreadableFacesBody)])
        _wire({"F1": b1.faces.item(0), "F2": b2.faces.item(0)},
              surface_delete=_DelFeatures(result))
        out = payload(sdf.delete_face_handler(faces=["F1", "F2"], heal=False))
        assert out["bodies_consumed"] == 1
        assert out["faces_after"] is None
        assert "consumed" in out["warning"]


def test_parametric_no_op_remedy_names_the_timeline_feature():
    # the parametric counterpart of the direct-path remedy below
    body = _body("Solid1", is_solid=True, face_count=7)
    target = body.faces.item(0)
    _wire({"F1": target}, delete=_DelFeatures(lambda coll: None))
    msg = error_message(sdf.delete_face_handler(faces=["F1"], heal=True))
    assert "returned no feature" in msg and "DIRECT mode" not in msg


# ── DIRECT mode: deleteFaceFeatures.add returns nothing while the delete LANDS (measured) ────

class TestDirectModeNoFeature:
    def _wire_direct(self, faces_removed=1, count_unreadable=False, design_type=0, face_count=7):
        body = _body("Box1", is_solid=True, face_count=face_count)
        target = body.faces.item(face_count - 1)   # keep index 0 deletable by the fake
        feats = _DirectDelFeatures([body], faces_removed=faces_removed,
                                   count_unreadable=count_unreadable)
        _wire({"F1": target}, delete=feats, surface_delete=feats, design_type=design_type)
        return body

    def test_direct_none_with_a_moved_face_count_is_ok(self):
        # the measured shape: a healed fillet face took the box from 7 faces to 6, add() -> None
        self._wire_direct()
        out = payload(sdf.delete_face_handler(faces=["F1"], heal=True))
        assert out["deleted"] is True
        assert out["faces_before"] == 7 and out["faces_after"] == 6
        assert out["faces_delta"] == -1

    def test_direct_none_publishes_no_feature_and_no_feature_derived_keys(self):
        self._wire_direct()
        out = payload(sdf.delete_face_handler(faces=["F1"], heal=True))
        assert "feature" not in out
        # BOTH of these are read off the feature's result bodies - neither may be fabricated
        assert "result_bodies" not in out
        assert "bodies_consumed" not in out
        assert out["no_timeline_feature"] is True
        assert "DIRECT mode" in out["note"]

    def test_direct_none_names_the_bodies_captured_before_the_delete(self):
        # a delete can consume the body outright, so the names belong to the pre-mutation capture
        self._wire_direct()
        out = payload(sdf.delete_face_handler(faces=["F1"], heal=True))
        assert out["input_bodies"] == ["Box1"]

    def test_declared_outputs_hold_on_the_direct_path(self):
        self._wire_direct()
        out = payload(sdf.delete_face_handler(faces=["F1"], heal=True))
        for o in sdf.RETURNS:
            assert o.assert_present(out) == "", o.key

    def test_direct_none_with_an_unchanged_face_count_is_an_error(self):
        # add() handed back nothing AND no face went: not a success.
        self._wire_direct(faces_removed=0)
        res = sdf.delete_face_handler(faces=["F1"], heal=True)
        assert res["isError"] is True and "nothing was deleted" in res["message"]
        # no timeline feature exists on this path - the remedy must not name one
        assert "design_delete_feature" not in res["message"]
        assert "undo in Fusion" in res["message"]

    def test_direct_none_with_an_unreadable_face_count_is_unverified(self):
        # No feature AND no face count: UNVERIFIED, not success. In parametric the feature object is
        # itself evidence, so an unreadable count may pass there - here it is the only evidence.
        self._wire_direct(count_unreadable=True)
        res = sdf.delete_face_handler(faces=["F1"], heal=True)
        assert res["isError"] is True and "UNVERIFIED" in res["message"]
        # a fully-consumed body reads the same way - the message must name that, not guess
        assert "fully consumed" in res["message"]

    def test_faces_delta_is_the_true_delta_however_many_faces_share_a_body(self):
        # faces_delta is the body's OWN change, however many of its faces were targeted: the owning
        # body is deduped by entityToken, so it is sampled once. Keyed by identity it would be
        # counted once per face and its delta summed that many times (3 faces, delta -3 -> -9).
        body = _body("Box1", is_solid=True, face_count=9)
        targets = {"F%d" % i: body.faces.item(i) for i in range(3)}
        feats = _DirectDelFeatures([body], faces_removed=3)
        _wire(targets, delete=feats, surface_delete=feats, design_type=0)
        out = payload(sdf.delete_face_handler(faces=list(targets), heal=True))
        assert out["faces_before"] == 9
        assert out["faces_delta"] == -3        # NOT -9 - one body, sampled once
        assert out["faces_after"] == 6

    def test_the_note_reports_the_measured_delta_not_the_requested_count(self):
        # 3 faces requested, a heal that nets -1: the note must not read "Deleted 3 face(s) ... 9 ->
        # 8" beside faces_delta -1. Only the measured movement is claimed; the request is labelled
        # as a request.
        body = _body("Box1", is_solid=True, face_count=9)
        targets = {"F%d" % i: body.faces.item(i) for i in range(3)}
        feats = _DirectDelFeatures([body], faces_removed=1)      # heal re-merged the rest
        _wire(targets, delete=feats, surface_delete=feats, design_type=0)
        out = payload(sdf.delete_face_handler(faces=list(targets), heal=True))
        assert out["faces_delta"] == -1 and out["faces_after"] == 8
        assert "9 -> 8" in out["note"]
        assert "3 face(s) requested" in out["note"]
        assert "Deleted 3 face" not in out["note"]
        # `heal` is an input flag, never read back - the note may only say it was requested
        assert "heal requested" in out["note"]
        assert "and healed the opening" not in out["note"]

    def test_a_rising_face_count_is_flagged_not_narrated_flatly(self):
        # A delete that RAISES the count is unmeasured territory: the count moved, so the edit did
        # land - but "7 -> 9" must not read as a normal delete. Not refused either: refusing would
        # assert a delete can only ever lower the count, which nobody has measured.
        body = _body("Box1", is_solid=True, face_count=7)
        target = body.faces.item(6)
        feats = _DirectDelFeatures([body], faces_removed=-2)   # negative removal = faces ADDED
        _wire({"F1": target}, delete=feats, surface_delete=feats, design_type=0)
        out = payload(sdf.delete_face_handler(faces=["F1"], heal=True))
        assert out["faces_delta"] == 2
        assert "ROSE" in out["warning"] and "unexpected" in out["warning"]
        # the note must not also call this a delete - a moving count proves an edit landed, not
        # that the requested face was the one removed
        assert "The edit landed" in out["note"]
        assert "The delete landed" not in out["note"]

    def test_parametric_none_stays_an_error(self):
        # Even with the face count moved: a None feature in a PARAMETRIC design is unmeasured as a
        # success, so it is refused.
        self._wire_direct(design_type=1)
        res = sdf.delete_face_handler(faces=["F1"], heal=True)
        assert res["isError"] is True and "returned no feature" in res["message"]
        assert "DIRECT mode" not in res["message"]
