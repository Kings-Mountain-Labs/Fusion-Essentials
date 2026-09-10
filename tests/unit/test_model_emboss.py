"""Unit tests for ``model_emboss.py`` - stamp a sketch profile onto a body's faces, raised or engraved.

Pinned: the PLAIN-LIST marshalling both collection arguments of EmbossFeatures.createInput need (the
fake asserts the shape, so a regression to an ObjectCollection goes red), the signed depth (zero
refused, negative allowed and reported as an engrave), the volume-DIRECTION gate - an emboss whose
effect disagrees with the sign asked for is an error, not a false ok - the single-body refusal, the
health-error gate, the direct-mode no-feature path, and the ModelParameter depth read-back.
"""

import types

from conftest import (load_tool, make_design, install, MakeComp, BRepBody, BRepFace, Profile,
                      go_stale, make_sketch, payload, error_message, assert_no_active_design,
                      assert_unknown_units, _NamedCollection)

em = load_tool("model_emboss")


# ── fakes: bodies/faces from conftest, plus the embossFeatures collection ───────────────────────

def make_body(name="Body1", volume=12.0):
    """A solid body whose volume the emboss moves in place - the emboss edits the SAME body."""
    return BRepBody(name=name, volume=volume)


def make_face(body):
    """A BRep face resolved from a find_geometry handle; carries its owning body."""
    return BRepFace(surface=None, body=body)


def make_profile(comp):
    """A sketch profile whose sketch is owned by `comp` - the component the feature must be built on."""
    sketch = make_sketch(name="ProfileSketch")
    sketch.parentComponent = comp
    return Profile(parent_sketch=sketch)


def make_value_input(value):
    """A ValueInput stand-in carrying the raw cm value the handler set."""
    return types.SimpleNamespace(value=value)


def make_feature(name="Emboss1", health=0, depth_cm=0.3):
    """An EmbossFeature stand-in. `depth` is a ModelParameter, whose .value is the applied depth in
    internal cm - not the ValueInput that was handed in."""
    return types.SimpleNamespace(name=name, healthState=health,
                                 errorOrWarningMessage="the profile is not fully on the target face",
                                 depth=types.SimpleNamespace(value=depth_cm, unit="cm"))


class FakeEmbossFeatures:
    """createInput/add simulating a stamp: add() shifts the affected body's volume by `volume_delta`
    (0.0 models the silent no-op the API still reports as success).

    `return_feature` False models a direct-mode add() that hands nothing back while the emboss LANDS;
    `volume_unreadable` additionally drops the volume read, leaving no evidence at all."""
    def __init__(self, bodies, volume_delta=3.36, return_feature=True, health=0,
                 volume_unreadable=False, feature_depth_cm=0.3, input_none=False):
        self.bodies = list(bodies)
        self.volume_delta = volume_delta
        self.return_feature = return_feature
        self.health = health
        self.volume_unreadable = volume_unreadable
        self.feature_depth_cm = feature_depth_cm
        self.input_none = input_none
        self.last_input = None

    def createInput(self, profiles, faces, depth):
        # MEASURED: the SWIG binding takes plain Python lists (std::vector) for BOTH collections;
        # an ObjectCollection is the wrong shape. Pin it so a regression goes red.
        assert isinstance(profiles, list), "createInput 'profiles' must be a plain list"
        assert isinstance(faces, list), "createInput 'faces' must be a plain list"
        if self.input_none:
            return None
        self.last_input = types.SimpleNamespace(profiles=profiles, inputFaces=faces, depth=depth)
        return self.last_input

    def add(self, inp):
        per_body = self.volume_delta / len(self.bodies) if self.bodies else 0.0
        for b in self.bodies:
            b.volume += per_body
        if self.volume_unreadable:
            go_stale(*self.bodies, attrs=("volume",))
        # the emboss edits the SAME body in place, so only the identity reads go stale
        go_stale(*self.bodies)
        if not self.return_feature:
            return None
        return make_feature(health=self.health, depth_cm=self.feature_depth_cm)


def make_text_sketch(comp=None, name="Nameplate", texts=1):
    """A sketch holding ONLY sketch texts - the nameplate case: it has no closed profile and never
    will. Each text is tagged '<sketch>#<i>' so a test can tell which one reached createInput. A
    sketch left unparented is adopted by the component _wire builds."""
    sketch = make_sketch(name=name)
    sketch.parentComponent = comp
    sketch.profiles = _NamedCollection()
    sketch.sketchTexts = _NamedCollection(
        [types.SimpleNamespace(tag=f"{name}#{i}", parentSketch=sketch) for i in range(texts)])
    return sketch


def _wire(monkeypatch, feats, faces, design_type=None, profile_comp=None, extra_comps=(),
          sketches=(), stub_profiles=True):
    """Install a component carrying `feats` (features.embossFeatures), stub the two typed kinds to
    hand back canned entities (their own resolution is covered by test_inputs), and model the adsk
    ValueInput factory the handler calls.

    `profile_comp` hosts the profile's sketch (defaults to the active component); `design_type` sets
    the modelling mode current_design_type reads (1 parametric, 0 direct). Each face's body is
    parented to the profile's component unless the test already parented it somewhere else - the
    handler refuses a body and a profile that live in different components.

    `sketches` populates the component's sketch collection; with `stub_profiles` False the handler
    resolves 'profiles' for real against them (the sketch-text route).

    Returns the profile list the handler will receive."""
    import adsk.core
    # Every component answers an entityToken and _common.same_component compares on it: the
    # faces-vs-profile guard refuses a pair it cannot identify, so the fakes carry tokens the way
    # live components do. A test that wants the unreadable state deletes the attribute.
    comp = MakeComp(name="Comp", sketches=sketches, entity_token="TOKEN:Comp")
    comp.features = types.SimpleNamespace(embossFeatures=feats)
    for i, extra in enumerate(extra_comps):
        if not hasattr(extra, "entityToken"):
            extra.entityToken = f"TOKEN:Extra{i}"
    design = make_design(comp=comp, all_components=[comp, *extra_comps])
    if design_type is not None:
        design.designType = design_type
    install(em, design)
    host = profile_comp if profile_comp is not None else comp
    for sk in sketches:
        if getattr(sk, "parentComponent", None) is None:
            sk.parentComponent = host
    for f in faces:
        if getattr(f, "body", None) is not None and f.body.parentComponent is None:
            f.body.parentComponent = host
    profs = [make_profile(profile_comp if profile_comp is not None else comp)]
    if stub_profiles:
        monkeypatch.setattr(em._PROFILES, "resolve", lambda raw, component="": (profs, None))
    monkeypatch.setattr(em._FACES, "resolve", lambda raw: (faces, None))
    adsk.core.ValueInput.createByReal = staticmethod(make_value_input)
    return profs


# ── happy path ────────────────────────────────────────────────────────────────

class TestEmboss:
    def test_raise_reports_mode_and_volume_delta(self, monkeypatch):
        body = make_body(name="Plate", volume=12.0)
        feats = FakeEmbossFeatures([body], volume_delta=3.364)
        _wire(monkeypatch, feats, [make_face(body)])
        out = payload(em.handler(profiles=["p"], faces=["h"], depth=3, units="mm"))
        assert out["embossed"] is True
        assert out["mode"] == "raise"
        assert out["feature"] == "Emboss1"
        assert out["body"] == "Plate"
        assert out["profiles_requested"] == 1
        assert out["faces_requested"] == 1
        assert out["volume_delta_cm3"] == 3.364

    def test_engrave_is_a_negative_depth_and_a_falling_volume(self, monkeypatch):
        body = make_body(volume=12.0)
        feats = FakeEmbossFeatures([body], volume_delta=-1.5, feature_depth_cm=-0.2)
        _wire(monkeypatch, feats, [make_face(body)])
        out = payload(em.handler(profiles=["p"], faces=["h"], depth=-2, units="mm"))
        assert out["mode"] == "engrave"
        assert out["depth"] == -2
        assert out["volume_delta_cm3"] == -1.5
        assert round(feats.last_input.depth.value, 6) == -0.2   # -2mm -> -0.2cm

    def test_createinput_gets_plain_lists_and_the_scaled_depth(self, monkeypatch):
        body = make_body(volume=12.0)
        face = make_face(body)
        feats = FakeEmbossFeatures([body])
        profs = _wire(monkeypatch, feats, [face])
        payload(em.handler(profiles=["p"], faces=["h"], depth=3, units="mm"))
        assert feats.last_input.profiles == profs
        assert feats.last_input.inputFaces == [face]
        assert round(feats.last_input.depth.value, 6) == 0.3     # 3mm -> 0.3cm

    def test_cm_units_scale_the_depth(self, monkeypatch):
        body = make_body(volume=12.0)
        feats = FakeEmbossFeatures([body])
        _wire(monkeypatch, feats, [make_face(body)])
        payload(em.handler(profiles=["p"], faces=["h"], depth=2, units="cm"))
        assert feats.last_input.depth.value == 2.0

    def test_applied_depth_is_read_back_off_the_model_parameter(self, monkeypatch):
        # EmbossFeature.depth is a ModelParameter in internal cm; the payload republishes it in the
        # caller's units, so a feature that took a different depth than asked is visible.
        body = make_body(volume=12.0)
        feats = FakeEmbossFeatures([body], feature_depth_cm=0.25)
        _wire(monkeypatch, feats, [make_face(body)])
        out = payload(em.handler(profiles=["p"], faces=["h"], depth=3, units="mm"))
        assert out["applied_depth"] == 2.5      # 0.25 cm read back as 2.5 mm

    def test_several_faces_on_one_body_are_counted(self, monkeypatch):
        body = make_body(name="Barrel", volume=20.0)
        faces = [make_face(body), make_face(body), make_face(body)]
        feats = FakeEmbossFeatures([body], volume_delta=1.0)
        _wire(monkeypatch, feats, faces)
        out = payload(em.handler(profiles=["p"], faces=["a", "b", "c"], depth=1, units="mm"))
        assert out["faces_requested"] == 3
        assert out["body"] == "Barrel"

    def test_body_name_is_captured_before_the_mutation(self, monkeypatch):
        # add() makes the body stop answering .name (a post-mutation proxy may); the payload must
        # still publish the name, which only holds if it was read BEFORE the emboss.
        body = make_body(name="Plate", volume=12.0)
        feats = FakeEmbossFeatures([body], volume_delta=2.0)
        _wire(monkeypatch, feats, [make_face(body)])
        out = payload(em.handler(profiles=["p"], faces=["h"], depth=3, units="mm"))
        assert out["body"] == "Plate"


# ── engraving a sketch TEXT (the nameplate route) ──────────────────────────────────────────────
#
# createInput's profiles array is documented as "Profile and SketchText objects", so a text needs no
# Profile of its own - it goes into the profile slot as itself, addressed by the 'text:<i>' id
# sketch_get publishes.

class TestSketchText:
    def test_a_text_id_reaches_createinput_as_the_sketch_text(self, monkeypatch):
        body = make_body(name="Plate", volume=12.0)
        feats = FakeEmbossFeatures([body], volume_delta=-0.42, feature_depth_cm=-0.05)
        sketch = make_text_sketch()
        _wire(monkeypatch, feats, [make_face(body)], sketches=[sketch], stub_profiles=False)
        out = payload(em.handler(profiles=["text:0"], faces=["h"], depth=-0.5, units="mm"))
        assert [p.tag for p in feats.last_input.profiles] == ["Nameplate#0"]
        assert out["mode"] == "engrave"
        assert out["volume_delta_cm3"] == -0.42
        assert out["profiles_requested"] == 1

    def test_the_index_picks_that_text(self, monkeypatch):
        body = make_body(volume=12.0)
        feats = FakeEmbossFeatures([body], volume_delta=0.4)
        sketch = make_text_sketch(texts=3)
        _wire(monkeypatch, feats, [make_face(body)], sketches=[sketch], stub_profiles=False)
        payload(em.handler(profiles=["text:2"], faces=["h"], depth=0.5, units="mm"))
        assert [p.tag for p in feats.last_input.profiles] == ["Nameplate#2"]

    def test_several_texts_stamp_in_one_call(self, monkeypatch):
        body = make_body(volume=12.0)
        feats = FakeEmbossFeatures([body], volume_delta=-0.8)
        sketch = make_text_sketch(texts=2)
        _wire(monkeypatch, feats, [make_face(body)], sketches=[sketch], stub_profiles=False)
        out = payload(em.handler(profiles=["text:0", "text:1"], faces=["h"], depth=-0.5, units="mm"))
        assert [p.tag for p in feats.last_input.profiles] == ["Nameplate#0", "Nameplate#1"]
        assert out["profiles_requested"] == 2

    def test_an_engrave_that_added_material_is_still_refused_for_a_text(self, monkeypatch):
        # the volume-direction gate is the tool's verdict and does not care what shape was stamped.
        body = make_body(volume=12.0)
        feats = FakeEmbossFeatures([body], volume_delta=+0.9)
        sketch = make_text_sketch()
        _wire(monkeypatch, feats, [make_face(body)], sketches=[sketch], stub_profiles=False)
        msg = error_message(em.handler(profiles=["text:0"], faces=["h"], depth=-0.5, units="mm"))
        assert "wrong way" in msg and "added" in msg

    def test_a_text_only_sketch_no_longer_dead_ends_on_no_closed_profile(self, monkeypatch):
        # A nameplate sketch never gets a closed region, so "draw one" is a dead end: the refusal
        # names the address that does reach the text.
        body = make_body(volume=12.0)
        feats = FakeEmbossFeatures([body])
        sketch = make_text_sketch(texts=2)
        _wire(monkeypatch, feats, [make_face(body)], sketches=[sketch], stub_profiles=False)
        msg = error_message(em.handler(profiles=[{"sketch": "Nameplate"}], faces=["h"],
                                       depth=-0.5, units="mm"))
        assert "'text:0'..'text:1'" in msg and "Draw a closed region" not in msg
        assert feats.last_input is None      # refused BEFORE any mutation was attempted

    def test_a_sketch_qualified_text_in_a_sub_component_builds_there(self, monkeypatch):
        # the text lives in a SUB-component's sketch, reached design-wide by '<sketch>/text:<i>';
        # host resolution runs off the text's parentSketch, exactly as it does for a Profile.
        body = make_body(volume=12.0)
        other = MakeComp(name="SubComp")
        other_feats = FakeEmbossFeatures([body], volume_delta=-0.5)
        other.features = types.SimpleNamespace(embossFeatures=other_feats)
        sketch = make_text_sketch(other)
        other.sketches = _NamedCollection([sketch])

        def _boom(*a, **k):
            raise AssertionError("the emboss must not be built on the ACTIVE component")

        active_feats = FakeEmbossFeatures([body])
        active_feats.createInput = _boom
        _wire(monkeypatch, active_feats, [make_face(body)], profile_comp=other,
              extra_comps=[other], stub_profiles=False)
        out = payload(em.handler(profiles=["Nameplate/text:0"], faces=["h"], depth=-0.5, units="mm"))
        assert out["embossed"] is True
        assert [p.tag for p in other_feats.last_input.profiles] == ["Nameplate#0"]

    def test_the_tool_advertises_the_text_route(self):
        assert "text" in em.TOOL_DESCRIPTION.lower()
        assert "text:<i>" in em.emboss_tool.input_schema["properties"]["profiles"]["description"]


class TestComponentScope:
    """SKETCH-6: both by-name forms this input takes - {sketch, profile_index} and
    '<sketch>/text:<i>' - address a sketch whose name is only unique inside its component. The
    scope's refusal may only name an input the schema declares, so the two ship together."""

    def test_the_kind_carries_the_scope_this_tools_schema_declares(self):
        sd = load_tool("_sketch_detail")
        assert em._PROFILES.scope_input == "component"
        assert em.emboss_tool.input_schema["properties"]["component"] == sd.COMPONENT_SCOPE[1]

    def test_the_scope_value_reaches_the_profile_resolve(self, monkeypatch):
        # the input is inert unless the handler passes it: a declared property the resolve never
        # sees is a remedy the caller can spell and the tool then ignores.
        body = make_body(volume=12.0)
        _wire(monkeypatch, FakeEmbossFeatures([body], volume_delta=2.0), [make_face(body)])
        seen = {}

        def _resolve(raw, component=""):
            seen["component"] = component
            return None, "refused"

        monkeypatch.setattr(em._PROFILES, "resolve", _resolve)
        em.handler(profiles=["p"], faces=["h"], depth=3, units="mm", component="Frame")
        assert seen == {"component": "Frame"}


# ── the feature is built on the profile's OWNING component (bSet avoidance) ────────────────────

class TestHostComponent:
    def test_feature_is_built_on_the_component_owning_the_profile_sketch(self, monkeypatch):
        body = make_body(volume=12.0)
        other_feats = FakeEmbossFeatures([body], volume_delta=2.0)
        other = MakeComp(name="SubComp")
        other.features = types.SimpleNamespace(embossFeatures=other_feats)

        def _boom(*a, **k):
            raise AssertionError("the emboss must not be built on the ACTIVE component")

        active_feats = FakeEmbossFeatures([body])
        active_feats.createInput = _boom
        _wire(monkeypatch, active_feats, [make_face(body)], profile_comp=other, extra_comps=[other])
        out = payload(em.handler(profiles=["p"], faces=["h"], depth=3, units="mm"))
        assert out["embossed"] is True
        assert other_feats.last_input is not None

    def test_profile_with_no_readable_sketch_falls_back_to_the_active_component(self, monkeypatch):
        body = make_body(volume=12.0)
        feats = FakeEmbossFeatures([body], volume_delta=2.0)
        _wire(monkeypatch, feats, [make_face(body)])
        monkeypatch.setattr(em._PROFILES, "resolve",
                            lambda raw, component="": ([Profile()], None))
        out = payload(em.handler(profiles=["p"], faces=["h"], depth=3, units="mm"))
        assert out["embossed"] is True
        assert feats.last_input is not None

    def test_faces_in_another_component_than_the_profile_are_refused_by_name(self, monkeypatch):
        # The feature is built on the PROFILE's component; stamping a body that lives elsewhere
        # needs creationOccurrence, which this tool does not set - so it refuses, naming both.
        other = MakeComp(name="SubComp", entity_token="TOKEN:SubComp")
        other.features = types.SimpleNamespace(embossFeatures=FakeEmbossFeatures([]))
        body = make_body(name="Plate", volume=12.0)
        body.parentComponent = MakeComp(name="ElsewhereComp", entity_token="TOKEN:Elsewhere")
        feats = FakeEmbossFeatures([body], volume_delta=2.0)
        _wire(monkeypatch, feats, [make_face(body)], profile_comp=other, extra_comps=[other])
        msg = error_message(em.handler(profiles=["p"], faces=["h"], depth=3, units="mm"))
        assert "ElsewhereComp" in msg and "SubComp" in msg
        assert feats.last_input is None      # refused BEFORE any mutation was attempted

    def test_same_component_is_compared_by_token_not_identity(self, monkeypatch):
        # Component wrappers are never identity-stable live, so a body whose parentComponent is a
        # DIFFERENT wrapper of the same component must still be accepted.
        body = make_body(volume=12.0)
        feats = FakeEmbossFeatures([body], volume_delta=2.0)
        _wire(monkeypatch, feats, [make_face(body)])
        twin = MakeComp(name="Comp", entity_token="TOKEN:Comp")   # one token, different object
        body.parentComponent = twin
        out = payload(em.handler(profiles=["p"], faces=["h"], depth=3, units="mm"))
        assert out["embossed"] is True

    def test_an_unreadable_component_identity_refuses_instead_of_embossing(self, monkeypatch):
        # same_component answers None when a token will not read. Proceeding would reach Fusion's
        # own 'InternalValidationError : bSet' from inside createInput with nothing naming the
        # cause, so the tool refuses first - and says the comparison failed, not that the two differ.
        body = make_body(volume=12.0)
        feats = FakeEmbossFeatures([body], volume_delta=2.0)
        _wire(monkeypatch, feats, [make_face(body)])
        body.parentComponent = MakeComp(name="Comp")              # no entityToken at all
        msg = error_message(em.handler(profiles=["p"], faces=["h"], depth=3, units="mm"))
        assert "could not be read" in msg
        assert feats.last_input is None      # refused BEFORE any mutation was attempted


# ── guards ───────────────────────────────────────────────────────────────────

class TestGuards:
    def test_no_active_design(self, monkeypatch):
        body = make_body()
        _wire(monkeypatch, FakeEmbossFeatures([body]), [make_face(body)])
        assert_no_active_design(em, em.handler, profiles=["p"], faces=["h"], depth=1, units="mm")

    def test_bad_units(self, monkeypatch):
        body = make_body()
        _wire(monkeypatch, FakeEmbossFeatures([body]), [make_face(body)])
        assert_unknown_units(em.handler, profiles=["p"], faces=["h"], depth=1)

    def test_zero_depth_rejected(self, monkeypatch):
        body = make_body()
        _wire(monkeypatch, FakeEmbossFeatures([body]), [make_face(body)])
        msg = error_message(em.handler(profiles=["p"], faces=["h"], depth=0, units="mm"))
        assert "depth" in msg and "non-zero" in msg

    def test_profile_resolution_error_propagates(self, monkeypatch):
        body = make_body()
        _wire(monkeypatch, FakeEmbossFeatures([body]), [make_face(body)])
        monkeypatch.setattr(em._PROFILES, "resolve",
                            lambda raw, component="": (None, "'profiles' needs at least one profile (handle or selector)."))
        msg = error_message(em.handler(profiles=[], faces=["h"], depth=1, units="mm"))
        assert "needs at least one profile" in msg

    def test_face_resolution_error_propagates(self, monkeypatch):
        body = make_body()
        _wire(monkeypatch, FakeEmbossFeatures([body]), [make_face(body)])
        monkeypatch.setattr(em._FACES, "resolve",
                            lambda raw: (None, "'faces' must be a face, but the handle points at a BRepEdge."))
        msg = error_message(em.handler(profiles=["p"], faces=["edge"], depth=1, units="mm"))
        assert "must be a face" in msg

    def test_faces_on_two_bodies_are_refused_by_name(self, monkeypatch):
        # EmbossFeatureInput.inputFaces must all be on ONE body - the refusal names what was given.
        a, b = make_body(name="Left", volume=10.0), make_body(name="Right", volume=10.0)
        feats = FakeEmbossFeatures([a, b], volume_delta=2.0)
        _wire(monkeypatch, feats, [make_face(a), make_face(b)])
        msg = error_message(em.handler(profiles=["p"], faces=["h1", "h2"], depth=1, units="mm"))
        assert "spans 2 bodies" in msg and "Left" in msg and "Right" in msg
        assert feats.last_input is None      # refused BEFORE any mutation was attempted

    def test_face_with_no_readable_body_is_error(self, monkeypatch):
        face = make_face(None)
        _wire(monkeypatch, FakeEmbossFeatures([]), [face])
        msg = error_message(em.handler(profiles=["p"], faces=["h"], depth=1, units="mm"))
        assert "owning body" in msg


# ── honesty ──────────────────────────────────────────────────────────────────

class TestHonesty:
    def test_unchanged_volume_reports_error_not_ok(self, monkeypatch):
        body = make_body(volume=12.0)
        feats = FakeEmbossFeatures([body], volume_delta=0.0)
        _wire(monkeypatch, feats, [make_face(body)])
        msg = error_message(em.handler(profiles=["p"], faces=["h"], depth=3, units="mm"))
        assert "unchanged" in msg
        # a parametric design DOES leave a timeline feature, so the remedy names it
        assert "remains in the timeline" in msg and "design_delete_feature" in msg

    def test_raise_that_removes_material_is_refused(self, monkeypatch):
        # The sign IS the request: a positive depth whose body LOST volume did the opposite of what
        # was asked, and a false ok would hide it.
        body = make_body(volume=12.0)
        feats = FakeEmbossFeatures([body], volume_delta=-3.0)
        _wire(monkeypatch, feats, [make_face(body)])
        msg = error_message(em.handler(profiles=["p"], faces=["h"], depth=3, units="mm"))
        assert "wrong way" in msg and "raise" in msg and "removed" in msg

    def test_engrave_that_adds_material_is_refused(self, monkeypatch):
        body = make_body(volume=12.0)
        feats = FakeEmbossFeatures([body], volume_delta=3.0, feature_depth_cm=-0.3)
        _wire(monkeypatch, feats, [make_face(body)])
        msg = error_message(em.handler(profiles=["p"], faces=["h"], depth=-3, units="mm"))
        assert "wrong way" in msg and "engrave" in msg and "added" in msg

    def test_health_error_reported_not_false_ok(self, monkeypatch):
        body = make_body(volume=12.0)
        feats = FakeEmbossFeatures([body], volume_delta=3.0, health=2)
        _wire(monkeypatch, feats, [make_face(body)])
        msg = error_message(em.handler(profiles=["p"], faces=["h"], depth=3, units="mm"))
        assert "failed to compute" in msg and "not fully on the target face" in msg

    def test_createinput_returning_nothing_is_error(self, monkeypatch):
        body = make_body(volume=12.0)
        feats = FakeEmbossFeatures([body], input_none=True)
        _wire(monkeypatch, feats, [make_face(body)])
        msg = error_message(em.handler(profiles=["p"], faces=["h"], depth=3, units="mm"))
        assert "createInput returned nothing" in msg

    def test_createinput_raising_surfaces_as_error(self, monkeypatch):
        body = make_body(volume=12.0)
        feats = FakeEmbossFeatures([body])
        _wire(monkeypatch, feats, [make_face(body)])
        feats.createInput = lambda p, f, d: (_ for _ in ()).throw(RuntimeError("profiles not co-planar"))
        msg = error_message(em.handler(profiles=["p"], faces=["h"], depth=3, units="mm"))
        assert "Could not start the emboss" in msg and "co-planar" in msg

    def test_add_raising_surfaces_as_error(self, monkeypatch):
        body = make_body(volume=12.0)
        feats = FakeEmbossFeatures([body])
        _wire(monkeypatch, feats, [make_face(body)])
        feats.add = lambda inp: (_ for _ in ()).throw(RuntimeError("depth exceeds the wall"))
        msg = error_message(em.handler(profiles=["p"], faces=["h"], depth=99, units="mm"))
        assert "Emboss failed" in msg and "exceeds the wall" in msg

    def test_truthy_feature_with_an_unreadable_volume_is_unverified(self, monkeypatch):
        # The volume delta is the ONLY evidence of which way the material went - a feature object
        # does not carry it - so an unreadable volume is a failure even when add() handed one back,
        # and the result must not publish a delta it could not measure.
        import json
        body = make_body(volume=12.0)
        feats = FakeEmbossFeatures([body], volume_delta=3.0, volume_unreadable=True)
        _wire(monkeypatch, feats, [make_face(body)])
        res = em.handler(profiles=["p"], faces=["h"], depth=3, units="mm")
        assert res["isError"] is True and "UNVERIFIED" in res["message"]
        assert "volume_delta_cm3" not in json.dumps(res)

    def test_no_feature_in_a_parametric_design_is_error(self, monkeypatch):
        body = make_body(volume=12.0)
        feats = FakeEmbossFeatures([body], return_feature=False)
        _wire(monkeypatch, feats, [make_face(body)], design_type=1)
        msg = error_message(em.handler(profiles=["p"], faces=["h"], depth=3, units="mm"))
        assert "returned no feature" in msg and "DIRECT mode" not in msg


# ── DIRECT mode: a falsy add() carries no information, so the volume is the only evidence ─────

class TestDirectModeNoFeature:
    def test_direct_none_with_a_moved_volume_is_ok(self, monkeypatch):
        body = make_body(name="Plate", volume=12.0)
        feats = FakeEmbossFeatures([body], volume_delta=3.364, return_feature=False)
        _wire(monkeypatch, feats, [make_face(body)], design_type=0)
        out = payload(em.handler(profiles=["p"], faces=["h"], depth=3, units="mm"))
        assert out["embossed"] is True and out["volume_delta_cm3"] == 3.364
        assert out["mode"] == "raise"

    def test_direct_none_publishes_no_feature_name(self, monkeypatch):
        body = make_body(name="Plate", volume=12.0)
        feats = FakeEmbossFeatures([body], volume_delta=3.364, return_feature=False)
        _wire(monkeypatch, feats, [make_face(body)], design_type=0)
        out = payload(em.handler(profiles=["p"], faces=["h"], depth=3, units="mm"))
        assert "feature" not in out
        assert "applied_depth" not in out       # no feature object to read the parameter off
        assert out["no_timeline_feature"] is True
        assert "DIRECT mode" in out["note"]

    def test_direct_none_with_an_unmoved_volume_is_an_error(self, monkeypatch):
        body = make_body(volume=12.0)
        feats = FakeEmbossFeatures([body], volume_delta=0.0, return_feature=False)
        _wire(monkeypatch, feats, [make_face(body)], design_type=0)
        msg = error_message(em.handler(profiles=["p"], faces=["h"], depth=3, units="mm"))
        assert "unchanged" in msg
        # no timeline feature exists on this path - the remedy must not name one
        assert "design_delete_feature" not in msg and "undo in Fusion" in msg

    def test_direct_none_with_an_unreadable_volume_is_unverified(self, monkeypatch):
        body = make_body(volume=12.0)
        feats = FakeEmbossFeatures([body], volume_delta=3.364, return_feature=False,
                                   volume_unreadable=True)
        _wire(monkeypatch, feats, [make_face(body)], design_type=0)
        msg = error_message(em.handler(profiles=["p"], faces=["h"], depth=3, units="mm"))
        assert "UNVERIFIED" in msg

    def test_direct_wrong_direction_is_still_refused(self, monkeypatch):
        body = make_body(volume=12.0)
        feats = FakeEmbossFeatures([body], volume_delta=-3.0, return_feature=False)
        _wire(monkeypatch, feats, [make_face(body)], design_type=0)
        msg = error_message(em.handler(profiles=["p"], faces=["h"], depth=3, units="mm"))
        assert "wrong way" in msg and "undo in Fusion" in msg


# ── declared output contract ─────────────────────────────────────────────────

class TestOutputContract:
    def test_declared_outputs_are_minted(self, monkeypatch):
        body = make_body(volume=12.0)
        feats = FakeEmbossFeatures([body], volume_delta=3.0)
        _wire(monkeypatch, feats, [make_face(body)])
        out = payload(em.handler(profiles=["p"], faces=["h"], depth=3, units="mm"))
        for o in em.RETURNS:
            assert o.assert_present(out) == "", o.key

    def test_declared_outputs_hold_on_the_direct_path(self, monkeypatch):
        body = make_body(volume=12.0)
        feats = FakeEmbossFeatures([body], volume_delta=3.0, return_feature=False)
        _wire(monkeypatch, feats, [make_face(body)], design_type=0)
        out = payload(em.handler(profiles=["p"], faces=["h"], depth=3, units="mm"))
        for o in em.RETURNS:
            assert o.assert_present(out) == "", o.key
