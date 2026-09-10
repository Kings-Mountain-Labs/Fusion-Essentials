"""Unit tests for ``model_replace_face.py`` - re-cut a body's face(s) onto another surface.

Pinned: the measured createInput shape (an ObjectCollection of source faces, then the BOOLEAN, then
the single Base target - the stand-in asserts the argument ORDER so a (source, target, flag)
regression goes red), the single-body guard, the two platform refusals surfacing UNADORNED, and the
effect gate - volume OR face count must move on the one owning body, and a call that moves neither
must be an error rather than a false ok.
"""

import types

from conftest import (BRepBody, BRepFace, entity_proxy, load_tool, make_design, install, MakeComp,
                      go_stale, payload, error_message, assert_no_active_design)

rf = load_tool("model_replace_face")

# The two refusals measured live, verbatim - a solid-derived target, and a topology the feature
# cannot compute. Both name themselves, so the handler must pass them through.
SOLID_TARGET_RAISE = "3 : invalid target faces, it should be surface face or body"
TOPOLOGY_RAISE = ("3 : ReplaceFace1 / Compute Failed // ASM_REPL_FACE_FAILED -   The face(s) could "
                  "not be replaced.\n    Try changing the face selections.")


# ── stand-ins for the feature collection conftest does not model ────────────────

def _face_of(body):
    """A face whose owning-body read hands back a DISTINCT wrapper each time, as the live API does -
    so a token-keyed dedupe passes where an identity-keyed one would not."""
    return BRepFace(None, body=entity_proxy(body))


def _shift_face_count(body, delta):
    """Add/remove entries in a body's (counted) faces collection - the topology signal."""
    items = body.faces._items
    if delta >= 0:
        items.extend([None] * delta)
    else:
        del items[delta:]


class ReplaceFaceFeatures:
    """Stands in for adsk.fusion.ReplaceFaceFeatures: createInput(...) -> input -> add(input) ->
    feature or None. add() shifts the body's volume by `volume_delta` and its face count by
    `face_delta`; both at 0 models the silent no-op the API still calls success.

    `return_feature` False models a falsy add(); `raises` makes add() throw a platform message;
    `signals_unreadable` drops both read-backs, leaving the call with no evidence at all."""

    def __init__(self, body, volume_delta=8.0, face_delta=1, return_feature=True, health=0,
                 raises=None, signals_unreadable=False, input_none=False):
        self.body = body
        self.volume_delta = volume_delta
        self.face_delta = face_delta
        self.return_feature = return_feature
        self.health = health
        self.raises = raises
        self.signals_unreadable = signals_unreadable
        self.input_none = input_none
        self.last_input = None

    def createInput(self, source, is_tangent, target):
        # LIVE-VERIFIED order: (sourceFaces: ObjectCollection, isTangentChain: bool, targetFaces:
        # Base). Swapping the last two was measured to RAISE: TypeError("in method
        # 'ReplaceFaceFeatures_createInput', argument 3 of type 'bool'"). Pin all three so that call
        # goes red here too, instead of only at the live boundary.
        assert not isinstance(source, list) and hasattr(source, "add"), \
            "sourceFaces must be an ObjectCollection, not a list"
        assert isinstance(is_tangent, bool), "argument 2 is isTangentChain (a bool), not the target"
        assert not isinstance(target, bool), "argument 3 is the target entity, not the flag"
        if self.input_none:
            return None
        self.last_input = types.SimpleNamespace(source=source, isTangentChain=is_tangent,
                                                targetFaces=target)
        return self.last_input

    def add(self, inp):
        if self.raises:
            raise RuntimeError(self.raises)
        self.body.volume += self.volume_delta
        _shift_face_count(self.body, self.face_delta)
        if self.signals_unreadable:
            go_stale(self.body, attrs=("volume", "faces"))
        go_stale(self.body)          # the body survives the edit; only identity reads go stale
        if not self.return_feature:
            return None
        return types.SimpleNamespace(name="ReplaceFace1", healthState=self.health,
                                     errorOrWarningMessage="the face(s) could not be replaced")


def _wire(monkeypatch, feats, faces, target=None, target_kind="body", design_type=None):
    """Install a component carrying `feats` (features.replaceFaceFeatures) and stub the two typed
    kinds to hand back canned entities (their own resolution is covered by test_inputs).

    `design_type` sets the modelling mode current_design_type reads (1 parametric, 0 direct); left
    unset the design reports neither, which is the 'unknown' mode."""
    comp = MakeComp(name="Comp")
    comp.features = type("F", (), {"replaceFaceFeatures": feats})()
    design = make_design(comp=comp)
    if design_type is not None:
        design.designType = design_type
    install(rf, design)
    target = target if target is not None else BRepBody(name="Patch1", is_solid=False)
    monkeypatch.setattr(rf._FACES, "resolve", lambda raw: (faces, None))
    monkeypatch.setattr(rf._TARGET, "resolve", lambda raw: ((target, target_kind), None))
    return target


# ── happy path ──────────────────────────────────────────────────────────────────

class TestReplace:
    def test_happy_path_reports_both_deltas(self, monkeypatch):
        body = BRepBody(name="Block", volume=100.0, face_count=6, entity_token="tok-a")
        feats = ReplaceFaceFeatures(body, volume_delta=-4.0, face_delta=2)
        _wire(monkeypatch, feats, [_face_of(body)],
              target=BRepBody(name="Patch1", is_solid=False))
        out = payload(rf.handler(faces=["h"], target="Patch1"))
        assert out["replaced"] is True
        assert out["feature"] == "ReplaceFace1"
        assert out["faces_requested"] == 1
        assert out["body"] == "Block"
        assert out["target"] == "Patch1" and out["target_kind"] == "body"
        assert out["volume_delta_cm3"] == -4.0
        assert out["face_count_delta"] == 2

    def test_face_count_move_alone_is_a_landed_replace(self, monkeypatch):
        # Either signal is sufficient evidence, so a face-count-only move must NOT be the no-effect
        # error. The measured clean replace moved the counterpart - volume 9.0 -> 14.4 with the face
        # count unchanged (6 -> 6) - which is why the gate is an OR rather than an AND.
        body = BRepBody(name="Block", volume=100.0, face_count=6, entity_token="tok-a")
        feats = ReplaceFaceFeatures(body, volume_delta=0.0, face_delta=1)
        _wire(monkeypatch, feats, [_face_of(body)])
        out = payload(rf.handler(faces=["h"], target="Patch1"))
        assert out["replaced"] is True
        assert out["volume_delta_cm3"] == 0.0 and out["face_count_delta"] == 1

    def test_volume_move_alone_is_a_landed_replace(self, monkeypatch):
        # The MEASURED happy path: a clean box top replaced onto an open patch above read
        # healthState 0, volume 9.0 -> 14.4, faces 6 -> 6.
        body = BRepBody(name="Block", volume=100.0, face_count=6, entity_token="tok-a")
        feats = ReplaceFaceFeatures(body, volume_delta=12.5, face_delta=0)
        _wire(monkeypatch, feats, [_face_of(body)])
        out = payload(rf.handler(faces=["h"], target="Patch1"))
        assert out["volume_delta_cm3"] == 12.5 and out["face_count_delta"] == 0

    def test_createinput_gets_the_collection_the_flag_and_the_target(self, monkeypatch):
        body = BRepBody(name="Block", volume=100.0, face_count=6, entity_token="tok-a")
        f1, f2 = _face_of(body), _face_of(body)
        feats = ReplaceFaceFeatures(body)
        target = _wire(monkeypatch, feats, [f1, f2])
        payload(rf.handler(faces=["h1", "h2"], target="Patch1"))
        assert list(feats.last_input.source) == [f1, f2]
        assert feats.last_input.isTangentChain is True           # binding default
        assert feats.last_input.targetFaces is target            # the RESOLVED entity, not the raw name

    def test_tangent_chain_false_is_passed_through(self, monkeypatch):
        body = BRepBody(name="Block", volume=100.0, face_count=6, entity_token="tok-a")
        feats = ReplaceFaceFeatures(body)
        _wire(monkeypatch, feats, [_face_of(body)])
        out = payload(rf.handler(faces=["h"], target="Patch1", tangent_chain=False))
        assert feats.last_input.isTangentChain is False
        assert out["tangent_chain"] is False

    def test_face_target_publishes_its_kind(self, monkeypatch):
        body = BRepBody(name="Block", volume=100.0, face_count=6, entity_token="tok-a")
        feats = ReplaceFaceFeatures(body)
        patch = BRepBody(name="Patch1", is_solid=False)
        _wire(monkeypatch, feats, [_face_of(body)], target=BRepFace(None, body=patch),
              target_kind="face")
        out = payload(rf.handler(faces=["h"], target="face-handle"))
        assert out["target_kind"] == "face"


# ── guards ──────────────────────────────────────────────────────────────────────

class TestGuards:
    def test_no_active_design(self, monkeypatch):
        body = BRepBody(name="Block", volume=100.0, face_count=6, entity_token="tok-a")
        _wire(monkeypatch, ReplaceFaceFeatures(body), [_face_of(body)])
        assert_no_active_design(rf, rf.handler, faces=["h"], target="Patch1")

    def test_faces_on_two_bodies_are_refused_by_name(self, monkeypatch):
        a = BRepBody(name="A", volume=100.0, face_count=6, entity_token="tok-a")
        b = BRepBody(name="B", volume=50.0, face_count=6, entity_token="tok-b")
        feats = ReplaceFaceFeatures(a)
        _wire(monkeypatch, feats, [_face_of(a), _face_of(b)])
        msg = error_message(rf.handler(faces=["h1", "h2"], target="Patch1"))
        assert "ONE body" in msg and "'A'" in msg and "'B'" in msg
        assert feats.last_input is None      # refused BEFORE any mutation ran

    def test_several_faces_of_one_body_are_allowed(self, monkeypatch):
        # owning_bodies dedupes by entityToken, and each face hands back its OWN body wrapper; an
        # identity-keyed dedupe would count this legal selection as three bodies and refuse it.
        body = BRepBody(name="Solo", volume=100.0, face_count=6, entity_token="tok-a")
        feats = ReplaceFaceFeatures(body)
        _wire(monkeypatch, feats, [_face_of(body), _face_of(body), _face_of(body)])
        out = payload(rf.handler(faces=["h1", "h2", "h3"], target="Patch1"))
        assert out["faces_requested"] == 3 and out["body"] == "Solo"

    def test_face_with_no_readable_body_is_error(self, monkeypatch):
        body = BRepBody(name="Block", volume=100.0, face_count=6, entity_token="tok-a")
        _wire(monkeypatch, ReplaceFaceFeatures(body), [BRepFace(None, body=None)])
        assert "owning body" in error_message(rf.handler(faces=["h"], target="Patch1"))

    def test_face_resolution_error_propagates(self, monkeypatch):
        body = BRepBody(name="Block", volume=100.0, face_count=6, entity_token="tok-a")
        _wire(monkeypatch, ReplaceFaceFeatures(body), [_face_of(body)])
        monkeypatch.setattr(rf._FACES, "resolve",
                            lambda raw: (None, "'faces' must be a face, but the handle points at a BRepEdge."))
        assert "must be a face" in error_message(rf.handler(faces=["edge"], target="Patch1"))

    def test_target_kind_refusal_propagates(self, monkeypatch):
        body = BRepBody(name="Block", volume=100.0, face_count=6, entity_token="tok-a")
        feats = ReplaceFaceFeatures(body)
        _wire(monkeypatch, feats, [_face_of(body)])
        monkeypatch.setattr(rf._TARGET, "resolve",
                            lambda raw: (None, "'target': that target is a mesh, but this needs one of: body, face."))
        msg = error_message(rf.handler(faces=["h"], target="MeshBody1"))
        assert "needs one of: body, face" in msg
        assert feats.last_input is None      # refused BEFORE any mutation ran


# ── honesty ─────────────────────────────────────────────────────────────────────

class TestHonesty:
    def test_solid_target_refusal_surfaces_unadorned(self, monkeypatch):
        body = BRepBody(name="Block", volume=100.0, face_count=6, entity_token="tok-a")
        feats = ReplaceFaceFeatures(body, raises=SOLID_TARGET_RAISE)
        _wire(monkeypatch, feats, [_face_of(body)])
        msg = error_message(rf.handler(faces=["h"], target="SolidFace"))
        assert SOLID_TARGET_RAISE in msg      # the platform names the cause; nothing is reworded

    def test_compute_failure_text_surfaces_unadorned(self, monkeypatch):
        body = BRepBody(name="Block", volume=100.0, face_count=6, entity_token="tok-a")
        feats = ReplaceFaceFeatures(body, raises=TOPOLOGY_RAISE)
        _wire(monkeypatch, feats, [_face_of(body)])
        msg = error_message(rf.handler(faces=["h"], target="Patch1"))
        assert "ASM_REPL_FACE_FAILED" in msg and "Try changing the face selections." in msg

    def test_createinput_returning_none_is_error(self, monkeypatch):
        body = BRepBody(name="Block", volume=100.0, face_count=6, entity_token="tok-a")
        feats = ReplaceFaceFeatures(body, input_none=True)
        _wire(monkeypatch, feats, [_face_of(body)])
        res = rf.handler(faces=["h"], target="Patch1")
        assert res["isError"] is True and "createInput returned nothing" in res["message"]
        assert body.volume == 100.0           # nothing ran

    def test_health_error_reported_not_false_ok(self, monkeypatch):
        body = BRepBody(name="Block", volume=100.0, face_count=6, entity_token="tok-a")
        feats = ReplaceFaceFeatures(body, health=2)
        _wire(monkeypatch, feats, [_face_of(body)])
        res = rf.handler(faces=["h"], target="Patch1")
        assert res["isError"] is True
        assert "failed to compute" in res["message"] and "could not be replaced" in res["message"]

    def test_neither_signal_moving_is_an_error_not_a_false_ok(self, monkeypatch):
        body = BRepBody(name="Block", volume=100.0, face_count=6, entity_token="tok-a")
        feats = ReplaceFaceFeatures(body, volume_delta=0.0, face_delta=0)
        _wire(monkeypatch, feats, [_face_of(body)])
        res = rf.handler(faces=["h"], target="Patch1")
        assert res["isError"] is True
        assert "'Block' did not change" in res["message"]
        assert "volume is unchanged" in res["message"] and "face count is unchanged" in res["message"]
        # a parametric design leaves a timeline entry, so the remedy names it
        assert "design_delete_feature" in res["message"]

    def test_unreadable_signals_with_a_feature_omit_the_delta_keys(self, monkeypatch):
        # In parametric the feature object is itself evidence, so an unreadable body is not an
        # error - but the payload must not publish a delta it could not measure.
        body = BRepBody(name="Block", volume=100.0, face_count=6, entity_token="tok-a")
        feats = ReplaceFaceFeatures(body, signals_unreadable=True)
        _wire(monkeypatch, feats, [_face_of(body)])
        out = payload(rf.handler(faces=["h"], target="Patch1"))
        assert "volume_delta_cm3" not in out and "face_count_delta" not in out
        assert out["replaced"] is True

    def test_unreadable_signals_with_a_feature_declare_the_check_did_not_run(self, monkeypatch):
        # The docstring, the description and the note all promise a GEOMETRIC verification. On this
        # path neither signal could be read, so that check never ran - and a payload that only drops
        # the deltas leaves the caller to infer the gap from two absent keys.
        body = BRepBody(name="Block", volume=100.0, face_count=6, entity_token="tok-a")
        feats = ReplaceFaceFeatures(body, signals_unreadable=True)
        _wire(monkeypatch, feats, [_face_of(body)])
        out = payload(rf.handler(faces=["h"], target="Patch1"))
        assert out["effect_unverified"] is True
        assert "no geometric proof" in out["note"]
        assert "model_inspect" in out["note"]

    def test_a_measured_replace_carries_no_unverified_flag(self, monkeypatch):
        body = BRepBody(name="Block", volume=100.0, face_count=6, entity_token="tok-a")
        _wire(monkeypatch, ReplaceFaceFeatures(body), [_face_of(body)])
        out = payload(rf.handler(faces=["h"], target="Patch1"))
        assert "effect_unverified" not in out
        assert "measured change on the body" in out["note"]


# ── DIRECT mode: a falsy add() with the edit still landing ──────────────────────

class TestDirectModeNoFeature:
    def test_direct_none_with_a_moved_body_is_ok(self, monkeypatch):
        body = BRepBody(name="Block", volume=100.0, face_count=6, entity_token="tok-a")
        feats = ReplaceFaceFeatures(body, volume_delta=-3.0, face_delta=1, return_feature=False)
        _wire(monkeypatch, feats, [_face_of(body)], design_type=0)
        out = payload(rf.handler(faces=["h"], target="Patch1"))
        assert out["replaced"] is True and out["volume_delta_cm3"] == -3.0
        assert "feature" not in out
        assert out["no_timeline_feature"] is True
        assert "DIRECT mode" in out["note"]

    def test_direct_none_with_an_unmoved_body_is_an_error(self, monkeypatch):
        body = BRepBody(name="Block", volume=100.0, face_count=6, entity_token="tok-a")
        feats = ReplaceFaceFeatures(body, volume_delta=0.0, face_delta=0, return_feature=False)
        _wire(monkeypatch, feats, [_face_of(body)], design_type=0)
        res = rf.handler(faces=["h"], target="Patch1")
        assert res["isError"] is True and "did not change" in res["message"]
        # no timeline feature exists on this path - the remedy must not name one
        assert "design_delete_feature" not in res["message"]
        assert "undo in Fusion" in res["message"]

    def test_direct_none_with_unreadable_signals_is_unverified(self, monkeypatch):
        body = BRepBody(name="Block", volume=100.0, face_count=6, entity_token="tok-a")
        feats = ReplaceFaceFeatures(body, return_feature=False, signals_unreadable=True)
        _wire(monkeypatch, feats, [_face_of(body)], design_type=0)
        res = rf.handler(faces=["h"], target="Patch1")
        assert res["isError"] is True and "UNVERIFIED" in res["message"]

    def test_parametric_none_stays_an_error(self, monkeypatch):
        body = BRepBody(name="Block", volume=100.0, face_count=6, entity_token="tok-a")
        feats = ReplaceFaceFeatures(body, return_feature=False)
        _wire(monkeypatch, feats, [_face_of(body)], design_type=1)
        res = rf.handler(faces=["h"], target="Patch1")
        assert res["isError"] is True and "returned no feature" in res["message"]
        assert "DIRECT mode" not in res["message"]

    def test_direct_payload_names_the_body_captured_before_the_edit(self, monkeypatch):
        body = BRepBody(name="Cap", volume=100.0, face_count=6, entity_token="tok-a")
        feats = ReplaceFaceFeatures(body, return_feature=False)
        _wire(monkeypatch, feats, [_face_of(body)], design_type=0)
        out = payload(rf.handler(faces=["h"], target="Patch1"))
        assert out["body"] == "Cap"


# ── declared output contract ────────────────────────────────────────────────────

class TestOutputContract:
    def test_feature_output_is_minted(self, monkeypatch):
        body = BRepBody(name="Block", volume=100.0, face_count=6, entity_token="tok-a")
        feats = ReplaceFaceFeatures(body)
        _wire(monkeypatch, feats, [_face_of(body)])
        out = payload(rf.handler(faces=["h"], target="Patch1"))
        for o in rf.RETURNS:
            assert o.assert_present(out) == "", o.key

    def test_declared_outputs_hold_on_the_direct_path(self, monkeypatch):
        body = BRepBody(name="Block", volume=100.0, face_count=6, entity_token="tok-a")
        feats = ReplaceFaceFeatures(body, return_feature=False)
        _wire(monkeypatch, feats, [_face_of(body)], design_type=0)
        out = payload(rf.handler(faces=["h"], target="Patch1"))
        for o in rf.RETURNS:
            assert o.assert_present(out) == "", o.key
