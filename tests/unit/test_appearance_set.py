"""Unit tests for ``appearance_set.py`` — set a body/occurrence/component color.

The logic pinned here, no live Fusion: color parsing (#RRGGBB / RRGGBB / r,g,b, with the malformed
cases rejected), target resolution (body name, occurrence, component -> all bodies), the override idiom
(addByCopy the NAMED base appearance out of the named material library, write the Color into its
albedo channel only, assign to .appearance), and the guards (bad color, bad opacity, no design,
missing target, absent library/base appearance, component with no bodies).
Fakes capture the assignments so a regression to a wrong attribute fails here.
"""

import json
from types import SimpleNamespace

import pytest

from conftest import (BRepBody, BRepFace, ColorProperty, FakeAppearances, FakeColor,
                      FakeMaterialLibraries, FakeOccurrence, FakeVector3D, MakeComp, MakeDesign,
                      Plane, load_tool, make_material_library,
                      FakeAppearance as _SharedAppearance, _Properties)

ap = load_tool("appearance_set")

_REAL_TARGET_RESOLVE = ap._TARGET.resolve


@pytest.fixture(autouse=True)
def _restore_target_resolve():
    """The _resolve_to/_resolve_err helpers reassign ap._TARGET.resolve in place; restore it after each
    test so a stub never leaks into the next (file-order independence — the rich-read fixture discipline)."""
    yield
    ap._TARGET.resolve = _REAL_TARGET_RESOLVE


# ── color parsing (pure) ───────────────────────────────────────────────────────

class TestParseColor:
    def test_hex_with_hash(self):
        assert ap._parse_color("#1E8E3E") == ((30, 142, 62), None)

    def test_hex_without_hash(self):
        assert ap._parse_color("FF0000") == ((255, 0, 0), None)

    def test_rgb_triplet(self):
        assert ap._parse_color("0, 128, 255") == ((0, 128, 255), None)

    def test_empty_rejected(self):
        rgb, err = ap._parse_color("")
        assert rgb is None and "color" in err.lower()

    def test_bad_hex_length_rejected(self):
        rgb, err = ap._parse_color("#FFF")
        assert rgb is None and "6-digit" in err

    def test_non_hex_rejected(self):
        rgb, err = ap._parse_color("#GGGGGG")
        assert rgb is None and "valid hex" in err

    def test_out_of_range_rgb_rejected(self):
        rgb, err = ap._parse_color("300,0,0")
        assert rgb is None and "0-255" in err

    def test_wrong_rgb_count_rejected(self):
        rgb, err = ap._parse_color("10,20")
        assert rgb is None and "r,g,b" in err

    def test_non_integer_rgb_rejected(self):
        rgb, err = ap._parse_color("10,x,30")
        assert rgb is None and "non-integer" in err

    def test_negative_component_rejected(self):
        rgb, err = ap._parse_color("-1,0,0")
        # '-1' parses as int -> caught by the 0-255 range check
        assert rgb is None and "0-255" in err


# ── fakes ──────────────────────────────────────────────────────────────────────

# The ColorProperty ids MEASURED on the base appearance the tool copies, in the order the live
# appearance exposes them: the albedo the color belongs in, and the luminance modifier beside it.
BASE_COLOR_IDS = ("opaque_albedo", "opaque_luminance_modifier")


class _OtherProperty:
    """A non-colour entry of an appearance's Properties - the rows the albedo walk must skip. The
    live float/boolean property types carry no shape dump, so this one stays local."""


def FakeAppearance(name, color_props=BASE_COLOR_IDS, appearance_id=None):
    """The shared appearance fake seeded the way the tool's base reads: the MEASURED channel pair
    in order, plus one non-colour property the albedo walk has to step over."""
    return _SharedAppearance(name, color_props=color_props, appearance_id=appearance_id,
                             extra_props=[_OtherProperty()])


_UNREAD = BRepBody._UNSET


def FakeBody(name, fail_appearance=False, inherited_opacity=_UNREAD):
    """A body carrying the appearance/opacity overrides this tool writes. `inherited_opacity` is
    what the renderer shows - the read that ANSWERS, which is the assembly-proxy shape; a plain
    NATIVE body's visibleOpacity declines (measured), and that is the shared fake's default."""
    cls = _RefusingBody if fail_appearance else BRepBody
    return cls(name, visible_opacity=inherited_opacity)


class _RefusingBody(BRepBody):
    """A body the API REFUSES an appearance assignment on, so a multi-body component loop can be
    tested for honest partial-success reporting."""

    def __setattr__(self, key, value):
        if key == "appearance" and getattr(self, "_armed", False):
            raise RuntimeError(f"appearance rejected for {getattr(self, 'name', '?')}")
        object.__setattr__(self, key, value)

    def __init__(self, name, visible_opacity=_UNREAD):
        super().__init__(name, visible_opacity=visible_opacity)
        object.__setattr__(self, "_armed", True)


# The id every appearance this tool mints in these tests carries: the copy keeps the base's id,
# and _install*/_install_mp seed the tool's own base name as the design's only existing appearance.
MINTED_ID = f"asset:{ap._BASE_NAME}"


class FanoutOcc(FakeOccurrence):
    """An occurrence whose .appearance assignment fans onto its bodies the way Fusion's does
    (MEASURED): every body without an override of its own takes the new appearance, a body holding
    a body-level override silently KEEPS it, and the occurrence's own .appearance still reads back
    as the newly assigned one either way.

    A kept override defaults to the SAME-BASE case measured live: the body carries an appearance
    this same tool minted earlier from the same base, so it shares the applied appearance's id and
    differs only by name. `kept_name`/`kept_id` override either axis for the mirror case."""

    def __init__(self, name, full_path=None, bodies=(), component=None, keeps_override=(),
                 kept_name=None, kept_id=None):
        object.__setattr__(self, "_keeps", set(keeps_override))
        super().__init__(path=full_path or name, bodies=list(bodies),
                         component=component or MakeComp(name + "_comp"))
        for b in bodies:
            if b.name in self._keeps:
                b.appearance = FakeAppearance(kept_name or ("OwnColor_" + b.name),
                                              appearance_id=kept_id or MINTED_ID)

    def __setattr__(self, key, value):
        object.__setattr__(self, key, value)
        if key == "appearance" and value is not None:
            for i in range(self.bRepBodies.count):
                b = self.bRepBodies.item(i)
                if b.name not in self._keeps:
                    b.appearance = value


# One occurrence shape here, because FanoutOcc's docstring is what an occurrence write does: the
# assignment fans onto the bodies, reaching each one that holds no override of its own and leaving
# each one that does. An occurrence that fanned onto NOTHING is that same rule with every body
# keeping an override, which keeps_override= is how a test asks for.
FakeOcc = FanoutOcc


def FakeFace():
    """A face carrying a settable .appearance and NO .name. Its surface is a Plane because a live
    face always has one - a colour write reads neither, but a null geometry is not a face state."""
    return BRepFace(Plane(FakeVector3D(0.0, 0.0, 1.0)))


def _started_with(entity):
    """The appearance an entity carries BEFORE the call - live a body/face always has one, so
    'nothing was applied' is this asset still in place, never a null."""
    return entity.appearance


def _root(name="Root", bodies=(), occurrences=()):
    """The design's root component: the body and occurrence collections a target name resolves
    through."""
    return MakeComp(name, bodies=bodies, occurrences=occurrences)


def _install(root, existing_appearances=(ap._BASE_NAME,), tokens=None):
    apps = FakeAppearances([FakeAppearance(n) for n in existing_appearances])
    design = MakeDesign(comp=root, tokens=tokens, appearances=apps)
    ap._common.design = lambda: design
    import adsk.core, adsk.fusion
    adsk.core.Color.create = staticmethod(lambda r, g, b, o: ("color", r, g, b, o))
    # isinstance checks in the handle path need these bound to the fakes
    adsk.fusion.BRepFace = BRepFace
    adsk.fusion.BRepBody = BRepBody
    # handle_token: the handler calls _inputs.handle_token(name) before findEntityByToken
    ap._inputs.handle_token = lambda s: s
    return design, apps


def _install_libraries(monkeypatch, *libraries):
    """Install the material-library catalog on the seam appearance_set resolves its base through
    (_materials.find_library reads _materials.app.materialLibraries)."""
    monkeypatch.setattr(ap._materials, "app",
                        SimpleNamespace(materialLibraries=FakeMaterialLibraries(libraries)))


def _library(name, *appearances):
    """One loaded material library holding `appearances` - the shape find_library walks and the
    tool then resolves its base out of by exact name."""
    return make_material_library(name, appearances=appearances)


def _payload(result):
    assert result["isError"] is False, result
    return json.loads(result["content"][0]["text"])


def _silent_body(name):
    """A body that ACCEPTS an appearance assignment but whose .appearance read answers None - the
    unverifiable read-back, which is neither a confirmed landing nor a confirmed miss. Built with
    type() rather than a class statement so it stays one scenario shape over the shared body."""
    cls = type("SilentBody", (BRepBody,), {
        "appearance": property(lambda self: None, lambda self, v: None)})
    return cls(name)


def _resolve_to(entity, kind):
    """Stub TargetRef to hand the handler an already-resolved (entity, kind). Target RESOLUTION is
    covered once in test_inputs.TestTargetRef; here we pin appearance_set's own job — copy a base
    appearance, set the color, apply it to the resolved entity (or all a component's bodies)."""
    ap._TARGET.resolve = lambda raw: ((entity, kind), None)


def _resolve_err(msg):
    ap._TARGET.resolve = lambda raw: (None, msg)


# ── apply to a body / occurrence / component ───────────────────────────────────

class TestApply:
    def test_a_body_write_discloses_that_it_shows_on_every_instance(self):
        # LIVE-CONFIRMED: a body-level write lands on the component's NATIVE body, so the color
        # shows on every instance. A caller reading "applied" as per-instance is reading it wrong,
        # so the note has to name the fan-out and point at the occurrence route.
        body = FakeBody("Body1")
        _install(_root(bodies=[body]))
        out = _payload(ap.handler(target="Body1", color="#1E8E3E"))
        assert "EVERY instance" in out["note"] and "OCCURRENCE" in out["note"]

    def test_an_occurrence_write_does_not_carry_the_native_body_disclosure(self):
        # the occurrence route IS the per-instance one - it must not be told it fans out
        occ = FakeOcc("Part:1", bodies=[FakeBody("Body1")])
        _install(_root(occurrences=[occ]))
        _resolve_to(occ, "occurrence")
        out = _payload(ap.handler(target="Part:1", color="#1E8E3E"))
        assert "EVERY instance" not in out["note"]

    def test_color_a_body_by_name(self):
        body = FakeBody("Body1")
        root = _root(bodies=[body])
        design, apps = _install(root)
        out = _payload(ap.handler(target="Body1", color="#1E8E3E"))
        assert out["applied"] is True
        assert out["color_hex"] == "#1E8E3E"
        assert out["kind"] == "body"
        # the body's appearance got the copied, colored appearance
        assert body.appearance is apps._copied[0][2]
        # and the color property was set to the created Color (0-255, opaque)
        cp = body.appearance.appearanceProperties.item(0)
        assert cp.value == ("color", 30, 142, 62, 255)

    def test_stuck_appearance_bites(self):
        # the assignment raises nothing but the body still reads a different appearance -> error
        stuck = FakeAppearance("OldPaint")
        body = FakeBody("Body1")
        body.__class__ = type("StuckBody", (BRepBody,), {
            "appearance": property(lambda self: stuck, lambda self, v: None)})
        root = _root(bodies=[body])
        _install(root)
        res = ap.handler(target="Body1", color="#1E8E3E")
        assert res["isError"] is True
        assert "did not take" in res["message"]

    def test_a_body_left_holding_a_same_base_copy_is_not_a_success(self):
        # The direct-write branches run the SAME two-key comparison as the fan-out: a body that
        # silently kept an earlier copy minted from the same base shares the applied appearance's
        # id, so an id-only read-back would call this stuck write a success.
        stuck = FakeAppearance("AgentColor_FF0000", appearance_id=MINTED_ID)
        body = FakeBody("Body1")
        body.__class__ = type("StuckBody", (BRepBody,), {
            "appearance": property(lambda self: stuck, lambda self, v: None)})
        _install(_root(bodies=[body]))
        res = ap.handler(target="Body1", color="#1E8E3E")
        assert res["isError"] is True
        assert "did not take" in res["message"] and "AgentColor_FF0000" in res["message"]

    def test_a_body_whose_appearance_will_not_read_back_is_still_a_success(self):
        # The comparison answers None here - not False. An unreadable read-back is not evidence
        # the write missed, so the direct-write branch must NOT raise "the override did not take";
        # only a comparison that came back False may. (`is False` is load-bearing: `is not True`
        # turns every unverifiable read into a fabricated failure.)
        body = _silent_body("Body1")
        _install(_root(bodies=[body]))
        out = _payload(ap.handler(target="Body1", color="#1E8E3E"))
        assert out["applied"] is True and out["applied_to"] == ["Body1"]
        assert "failed" not in out

    def test_a_component_body_whose_appearance_will_not_read_back_is_not_marked_failed(self):
        # Same rule inside the component loop: the unverifiable body joins applied_to, and
        # 'failed' stays absent rather than carrying an invented "still reads 'None'" row.
        quiet, good = _silent_body("Quiet"), FakeBody("Good")
        comp = MakeComp("Multi", bodies=[quiet, good])
        _install(_root())
        _resolve_to(comp, "component")
        out = _payload(ap.handler(target="Multi", color="#1E8E3E"))
        assert out["applied_to"] == ["Quiet", "Good"]
        assert "failed" not in out
        assert "failed" not in out["note"]

    def test_a_component_body_left_holding_a_same_base_copy_lands_in_failed(self):
        stuck = FakeAppearance("AgentColor_FF0000", appearance_id=MINTED_ID)
        good, bad = FakeBody("Good"), FakeBody("Bad")
        bad.__class__ = type("StuckBody2", (BRepBody,), {
            "appearance": property(lambda self: stuck, lambda self, v: None)})
        comp = MakeComp("Multi", bodies=[good, bad])
        _install(_root())
        _resolve_to(comp, "component")
        out = _payload(ap.handler(target="Multi", color="#1E8E3E"))
        assert out["applied_to"] == ["Good"]
        assert out["failed"] == [
            {"body": "Bad", "error": "appearance still reads 'AgentColor_FF0000' after the set"}]

    def test_color_a_single_face_by_handle(self):
        # a find_geometry FACE handle colors just that one face (BRepFace.appearance), not the body
        face = FakeFace()
        h = "/v" + "F" * 70
        root = _root(bodies=[FakeBody("Body1")])
        design, apps = _install(root, tokens={h: face})
        out = _payload(ap.handler(target=h, color="#FF6D00"))
        assert out["applied"] is True
        assert out["kind"] == "face"
        # the override landed on the FACE
        assert face.appearance is apps._copied[0][2]
        cp = face.appearance.appearanceProperties.item(0)
        assert cp.value == ("color", 255, 109, 0, 255)
        # applied_to falls back to the description (a face has no .name)
        assert out["applied_to"] and "face" in out["applied_to"][0].lower()

    def test_body_handle_still_colors_the_body(self):
        # the handle path must still resolve a BODY (not only faces)
        body = FakeBody("Body1")
        h = "/v" + "B" * 70
        root = _root(bodies=[body])
        design, apps = _install(root, tokens={h: body})
        out = _payload(ap.handler(target=h, color="#000000"))
        assert out["kind"] == "body"
        assert body.appearance is apps._copied[0][2]

    def test_long_body_name_not_mistaken_for_handle(self):
        # A 60+ char body NAME must not be mis-routed into the handle path: resolution tries
        # _resolve_token_entity first (returns None for a non-token) and falls through to the
        # name lookup - so a long body name resolves by NAME.
        long_name = "Left-Outrigger-Pivot-Bracket-Weldment-Subassembly-Body-Number-Seven"
        assert len(long_name) > 60
        body = FakeBody(long_name)
        root = _root(bodies=[body])
        design, apps = _install(root, tokens={})              # NOT a token
        out = _payload(ap.handler(target=long_name, color="#101010"))
        assert out["kind"] == "body"
        assert body.appearance is apps._copied[0][2]           # the colored appearance landed on it

    def test_color_an_occurrence(self):
        occ = FakeOcc("Wheel:1")
        root = _root(occurrences=[occ])
        design, apps = _install(root)
        out = _payload(ap.handler(target="Wheel:1", color="0,0,0"))
        assert out["kind"] == "occurrence"
        # the occurrence got the copied, colored appearance (identity, not just any object)
        assert occ.appearance is apps._copied[0][2]
        cp = occ.appearance.appearanceProperties.item(0)
        assert cp.value == ("color", 0, 0, 0, 255)

    def test_color_a_component_applies_to_all_bodies(self):
        b1, b2 = FakeBody("B1"), FakeBody("B2")
        comp = MakeComp("Tire", bodies=[b1, b2])
        design, apps = _install(_root())
        _resolve_to(comp, "component")
        out = _payload(ap.handler(target="Tire", color="#FFFFFF"))
        assert out["kind"] == "component"
        # the ONE minted appearance reached both bodies - each body already carried one of its own,
        # so 'not null' would pass over a loop that coloured nothing
        assert b1.appearance is apps._copied[0][2] and b2.appearance is b1.appearance
        assert set(out["applied_to"]) == {"B1", "B2"}

    def test_the_color_is_always_minted_fully_opaque(self):
        # a Fusion appearance's transparency is its Prism material class, never its color alpha, so
        # the alpha written here is a constant - the see-through control is 'opacity', separately.
        body = FakeBody("Body1")
        _install(_root(bodies=[body]))
        out = _payload(ap.handler(target="Body1", color="#102030", opacity=40))
        assert body.appearance.appearanceProperties.item(0).value == ("color", 16, 32, 48, 255)
        assert out["opacity"] == 40

    def test_default_appearance_name_from_color(self):
        body = FakeBody("Body1")
        _install(_root(bodies=[body]))
        out = _payload(ap.handler(target="Body1", color="#1E8E3E"))
        assert out["appearance"] == "AgentColor_1E8E3E"


class TestReuseExistingAppearance:
    """addByCopy refuses a duplicate name by RAISING (measured) and safe() hands the handler None -
    a second same-color call or a retry after a half-made copy must LOOK UP and reuse the existing
    appearance, not fail."""

    def test_second_same_color_call_reuses_one_shared_appearance(self):
        b1, b2 = FakeBody("B1"), FakeBody("B2")
        root = _root(bodies=[b1, b2])
        design, apps = _install(root)
        out1 = _payload(ap.handler(target="B1", color="#1E8E3E"))
        assert out1["appearance_reused"] is False
        out2 = _payload(ap.handler(target="B2", color="#1E8E3E"))
        assert out2["appearance_reused"] is True
        assert len(apps._copied) == 1                     # ONE shared appearance, not a second copy
        assert b1.appearance is b2.appearance

    def test_reuse_completes_a_half_made_appearance(self):
        # the named appearance exists but its color was never set (a raced/aborted first call):
        # the reuse path re-applies the requested color instead of trusting the stale one
        root = _root(bodies=[FakeBody("B1")])
        design, apps = _install(root, existing_appearances=(ap._BASE_NAME, "AgentColor_FF0000"))
        out = _payload(ap.handler(target="B1", color="#FF0000"))
        assert out["appearance_reused"] is True
        assert apps._copied == []                         # no copy attempted
        reused = apps.itemByName("AgentColor_FF0000")
        assert reused.appearanceProperties.item(0).value == ("color", 255, 0, 0, 255)

    def test_a_color_appearance_that_lands_between_the_lookup_and_the_copy_is_reused(self):
        # a parallel call can create the colour name in the gap: addByCopy refuses the duplicate,
        # and the re-check must adopt that one (and re-apply the colour to it) rather than fail
        body = FakeBody("B1")
        design, apps = _install(_root(bodies=[body]))
        raced = FakeAppearance("AgentColor_1E8E3E")

        def land_it(base, name):
            apps._items.append(raced)
            return None
        apps.addByCopy = land_it
        out = _payload(ap.handler(target="B1", color="#1E8E3E"))
        assert out["appearance_reused"] is True
        assert body.appearance is raced
        assert raced.appearanceProperties.item(0).value == ("color", 30, 142, 62, 255)

    def test_a_colour_copy_that_returns_nothing_is_an_honest_failure(self):
        # MEASURED: addByCopy RAISES on a name already taken, so the refusal states the read it
        # took (the name is not in the document) instead of a cause nothing observed.
        _install(_root(bodies=[FakeBody("B1")]))[1].addByCopy = lambda base, name: None
        res = ap.handler(target="B1", color="#1E8E3E")
        assert res["isError"] is True and "addByCopy" in res["message"]
        assert "AgentColor_1E8E3E" in res["message"]
        assert "already exists in document" in res["message"]

    def test_different_color_still_creates_its_own_appearance(self):
        b1, b2 = FakeBody("B1"), FakeBody("B2")
        root = _root(bodies=[b1, b2])
        design, apps = _install(root)
        _payload(ap.handler(target="B1", color="#1E8E3E"))
        out = _payload(ap.handler(target="B2", color="#FF6D00"))
        assert out["appearance_reused"] is False
        assert len(apps._copied) == 2
        assert b1.appearance is not b2.appearance


# ── guards ─────────────────────────────────────────────────────────────────────

class TestGuards:
    def test_bad_color_errors_before_touching_design(self):
        res = ap.handler(target="Body1", color="nope")
        assert res["isError"] is True and "hex" in res["message"].lower()

    def test_non_integer_opacity_names_the_percent_range(self):
        _install(_root(bodies=[FakeBody("Body1")]))
        res = ap.handler(target="Body1", color="#000000", opacity="translucent")
        assert res["isError"] is True
        assert "percent" in res["message"].lower() and "100" in res["message"]

    def test_out_of_range_opacity_is_refused(self):
        # 255 is the color-alpha number, and reading it as a percent would silently clamp to opaque
        _install(_root(bodies=[FakeBody("Body1")]))
        res = ap.handler(target="Body1", color="#000000", opacity=255)
        assert res["isError"] is True and "255" in res["message"]

    def test_translucent_opacity_lands_on_the_body(self):
        # the write itself; what it RENDERS as is a separate read, covered below
        body = FakeBody("Body1", inherited_opacity=0.4)
        _install(_root(bodies=[body]))
        out = _payload(ap.handler(target="Body1", color="#000000", opacity=40))
        assert abs(body.opacity - 0.4) < 1e-9
        assert out["opacity"] == 40 and out["opacity_rendered"] == 40

    def test_zero_opacity_is_accepted(self):
        # fully invisible is a legal setting of the override, not a request to refuse
        body = FakeBody("Body1")
        _install(_root(bodies=[body]))
        out = _payload(ap.handler(target="Body1", color="#000000", opacity=0))
        assert body.opacity == 0.0 and out["opacity"] == 0

    def test_opacity_without_a_color_is_a_complete_request(self):
        body = FakeBody("Body1")
        held = _started_with(body)
        _install(_root(bodies=[body]))
        out = _payload(ap.handler(target="Body1", opacity=100))
        assert out["opacity"] == 100 and body.appearance is held

    def test_a_color_still_needs_a_color_when_no_opacity_is_asked_for(self):
        _install(_root(bodies=[FakeBody("Body1")]))
        res = ap.handler(target="Body1")
        assert res["isError"] is True and "color" in res["message"].lower()

    def test_inherited_opacity_is_disclosed_not_claimed(self):
        # What renders can differ from what was asked, so the payload publishes the rendered value
        # and the note states that reading - it must not name a cause (an ancestor's override, a
        # swallowed write) that nothing in this call read.
        body = FakeBody("Body1", inherited_opacity=0.25)
        _install(_root(bodies=[body]))
        out = _payload(ap.handler(target="Body1", opacity=80))
        assert out["opacity"] == 80 and out["opacity_rendered"] == 25
        assert "RENDERS at 25%" in out["note"]
        assert "inherited" not in out["note"] and "ancestor" not in out["note"]

    def test_no_active_design_errors(self):
        ap._common.design = lambda: None
        res = ap.handler(target="Body1", color="#000000")
        assert res["isError"] is True and "no active design" in res["message"].lower()

    def test_missing_target_errors(self):
        # TargetRef rejects an unresolvable target; appearance_set surfaces that error.
        _install(_root(bodies=[FakeBody("Body1")]))
        _resolve_err("'target': 'Ghost' did not resolve to a body handle, an occurrence/component/body name.")
        res = ap.handler(target="Ghost", color="#000000")
        assert res["isError"] is True and "ghost" in res["message"].lower()

    def test_the_base_library_being_absent_is_refused_by_name(self, monkeypatch):
        # The design holds no base yet - only the metal that entered it first - and the named
        # library is not loaded. The refusal must name the library it looked in; falling back to
        # the appearance already in the document is what made every override a tinted metal.
        design, apps = _install(_root(bodies=[FakeBody("Body1")]),
                                existing_appearances=("Steel - Satin",))
        _install_libraries(monkeypatch, _library("Fusion Material Library"))
        res = ap.handler(target="Body1", color="#000000")
        assert res["isError"] is True
        assert ap._BASE_LIBRARY in res["message"]
        assert "Fusion Material Library" in res["message"]      # what IS loaded
        assert apps._copied == []

    def test_component_with_no_bodies_errors(self):
        comp = MakeComp("Empty", bodies=[])
        _install(_root())
        _resolve_to(comp, "component")
        res = ap.handler(target="Empty", color="#000000")
        assert res["isError"] is True and "no bodies" in res["message"].lower()

    def test_a_refused_target_mints_NO_appearance(self):
        # The appearance is a persistent design asset. Creating it before the target is validated
        # leaves an orphan behind on every refusal (measured live: an empty root took the design's
        # appearance count 0 -> 1 on a call that returned isError).
        comp = MakeComp("Empty", bodies=[])
        # a base IS available, so nothing but the ordering keeps the copy from being made
        design, apps = _install(_root())
        _resolve_to(comp, "component")
        res = ap.handler(target="Empty", color="#CC2200")
        assert res["isError"] is True
        assert apps._copied == [] and apps.count == 1        # only the pre-existing base
        assert apps.itemByName("AgentColor_CC2200") is None

    def test_no_editable_color_property_errors(self):
        # the COPIED appearance exposes no ColorProperty -> honest failure, not a silent no-op
        body = FakeBody("Body1")
        root = _root(bodies=[body])
        design, apps = _install(root)

        # make addByCopy return a propertyless appearance (no ColorProperty to set)
        def copy_no_color(base, name):
            a = FakeAppearance(name, color_props=())
            a.appearanceProperties = _Properties([_OtherProperty()])
            apps._copied.append((base, name, a))
            return a
        apps.addByCopy = copy_no_color

        res = ap.handler(target="Body1", color="#000000")
        assert res["isError"] is True and "color property" in res["message"].lower()


# ── occurrence fan-out: where an occurrence-level write actually landed ───────

def _install_mp(monkeypatch, root, existing_appearances=(ap._BASE_NAME,), tokens=None):
    """The monkeypatch install (tests/CLAUDE.md canonical pattern): every seam is torn down after
    the test instead of left poked on the module."""
    apps = FakeAppearances([FakeAppearance(n) for n in existing_appearances])
    design = MakeDesign(comp=root, tokens=tokens, appearances=apps)
    import adsk.core, adsk.fusion
    monkeypatch.setattr(ap._common, "design", lambda: design)
    monkeypatch.setattr(adsk.core.Color, "create",
                        staticmethod(lambda r, g, b, o: ("color", r, g, b, o)))
    monkeypatch.setattr(adsk.fusion, "BRepFace", FakeFace)
    monkeypatch.setattr(adsk.fusion, "BRepBody", FakeBody)
    monkeypatch.setattr(ap._inputs, "handle_token", lambda s: s)
    return design, apps


class TestOccurrenceFanout:
    """An occurrence write is NOT one assignment: it fans onto the bodies, and the occurrence's own
    read-back agrees with what was set even for bodies it never reached. The payload publishes the
    real reach, so a caller is never told a body changed when it did not."""

    def test_applied_to_names_the_occurrence_and_every_body_reached(self, monkeypatch):
        b1, b2 = FakeBody("B1"), FakeBody("B2")
        occ = FanoutOcc("Wheel:1", bodies=[b1, b2])
        _install_mp(monkeypatch, _root(occurrences=[occ]))
        out = _payload(ap.handler(target="Wheel:1", color="#1E8E3E"))
        assert out["applied_to"] == ["Wheel:1", "B1", "B2"]
        assert "bodies_not_reached" not in out and "unverified_bodies" not in out

    def test_body_the_write_did_not_reach_is_disclosed_as_a_partial_not_swallowed(self, monkeypatch):
        # The default kept override is the SAME-BASE case measured live: the body carries an
        # appearance this tool minted earlier from the same base, so it shares the applied
        # appearance's id. An id-only comparison calls this body reached.
        b1, b2 = FakeBody("Kept"), FakeBody("Reached")
        occ = FanoutOcc("Wheel:1", bodies=[b1, b2], keeps_override=["Kept"])
        _install_mp(monkeypatch, _root(occurrences=[occ]))
        out = _payload(ap.handler(target="Wheel:1", color="#1E8E3E"))
        assert b1.appearance.id == MINTED_ID                  # same source asset id...
        assert b1.appearance.name == "OwnColor_Kept"          # ...different appearance
        # a disclosed partial: still applied, but 'Kept' is named as NOT reached
        assert out["applied"] is True
        assert out["applied_to"] == ["Wheel:1", "Reached"]
        assert out["bodies_not_reached"] == [{"body": "Kept", "appearance": "OwnColor_Kept"}]
        assert "PARTIAL" in out["note"] and "Kept" in out["note"]

    def test_a_same_id_kept_appearance_is_not_reached(self, monkeypatch):
        # The live defect, isolated: the kept appearance shares the applied one's id EXACTLY
        # (a copy keeps its source asset's id, and the tool mints every colour from one base).
        # Only the name separates them, so an id-only comparison reports a false reach.
        kept, reached = FakeBody("Kept"), FakeBody("Reached")
        occ = FanoutOcc("Wheel:1", bodies=[kept, reached], keeps_override=["Kept"],
                        kept_name="AgentColor_FF0000", kept_id=MINTED_ID)
        design, apps = _install_mp(monkeypatch, _root(occurrences=[occ]))
        out = _payload(ap.handler(target="Wheel:1", color="#1E8E3E"))
        assert apps._copied[0][2].id == kept.appearance.id     # the two ids are identical
        assert out["applied_to"] == ["Wheel:1", "Reached"]
        assert out["bodies_not_reached"] == [
            {"body": "Kept", "appearance": "AgentColor_FF0000"}]

    def test_a_same_named_kept_appearance_is_still_not_reached(self, monkeypatch):
        # The mirror: names are non-unique live (92 names shared by two or more of 530
        # appearances), so a body that KEPT a DIFFERENT asset carrying the same name must not be
        # classified as reached either. A name-only comparison reports this body as coloured.
        kept, reached = FakeBody("Kept"), FakeBody("Reached")
        occ = FanoutOcc("Wheel:1", bodies=[kept, reached], keeps_override=["Kept"],
                        kept_name="AgentColor_1E8E3E", kept_id="asset:SomeOtherBase")
        _install_mp(monkeypatch, _root(occurrences=[occ]))
        out = _payload(ap.handler(target="Wheel:1", color="#1E8E3E"))
        assert out["appearance"] == "AgentColor_1E8E3E"        # same NAME as the kept one
        assert kept.appearance.id != MINTED_ID                 # different asset
        assert out["applied_to"] == ["Wheel:1", "Reached"]
        assert out["bodies_not_reached"] == [
            {"body": "Kept", "appearance": "AgentColor_1E8E3E"}]

    def test_reaching_no_body_at_all_is_an_error_not_an_applied_true(self, monkeypatch):
        # Every body demonstrably still reads another appearance: the occurrence's own read-back
        # is the only thing that agreed, and it agrees whether or not anything changed. Reporting
        # applied:true here would be a swallowed no-op.
        occ = FanoutOcc("Wheel:1", bodies=[FakeBody("Kept")], keeps_override=["Kept"])
        _install_mp(monkeypatch, _root(occurrences=[occ]))
        res = ap.handler(target="Wheel:1", color="#1E8E3E")
        assert res["isError"] is True
        assert "NONE" in res["message"] and "Kept" in res["message"]

    def test_the_note_and_the_key_report_the_observation_not_an_unread_cause(self, monkeypatch):
        # The tool reads WHICH appearance each body carries; it never reads why
        # (BRepBody.appearanceSourceType is not consulted). So neither the note NOR the payload key
        # may name a body-level override as the cause - a key called 'overridden_bodies' asserts
        # exactly what the note is careful not to.
        occ = FanoutOcc("Wheel:1", bodies=[FakeBody("Kept"), FakeBody("Reached")],
                        keeps_override=["Kept"])
        _install_mp(monkeypatch, _root(occurrences=[occ]))
        out = _payload(ap.handler(target="Wheel:1", color="#1E8E3E"))
        partial = out["note"].split("Appearance override applied")[0]   # the fan-out clause only
        assert "do NOT carry the new appearance" in partial
        assert "override" not in partial.lower()                    # no cause the tool never read
        assert "Kept" in partial
        assert "bodies_not_reached" in out                          # the key states the observation
        assert not [k for k in out if "overridden" in k]

    @pytest.mark.parametrize("applied_id,applied_name", [
        (None, "AgentColor_1E8E3E"),        # the applied appearance's id would not read
        (MINTED_ID, None),                  # ...or its name would not
        (None, None),
    ])
    def test_an_unreadable_applied_key_classifies_nothing_it_could_not_compare(
            self, applied_id, applied_name):
        # With either key missing there is nothing to compare on, so every body is UNVERIFIED.
        # Calling them reached or not-reached would publish a comparison the tool never made.
        b1, b2 = FakeBody("B1"), FakeBody("B2")
        b1.appearance = FakeAppearance("Blue")
        b2.appearance = FakeAppearance("Green")
        occ = FakeOcc("Wheel:1", bodies=[b1, b2])
        reached, not_reached, unverified = ap._occurrence_fanout(occ, applied_id, applied_name)
        assert reached == [] and not_reached == []
        assert unverified == ["B1", "B2"]

    def test_a_body_with_no_readable_id_is_unverified_even_when_the_names_match(self):
        # The shape a name-only fallback would "rescue": the body's appearance answers with a
        # matching NAME but no id. Unverified is the honest answer - a name match alone cannot
        # tell this body from one carrying a same-named stranger.
        b = FakeBody("Quiet")
        b.appearance = FakeAppearance("AgentColor_1E8E3E")
        del b.appearance.id
        occ = FakeOcc("Wheel:1", bodies=[b])
        reached, not_reached, unverified = ap._occurrence_fanout(occ, MINTED_ID,
                                                                 "AgentColor_1E8E3E")
        assert reached == [] and not_reached == [] and unverified == ["Quiet"]

    def test_a_body_with_no_readable_name_is_unverified_even_when_the_ids_match(self):
        # The mirror, and the one an id-only comparison would wrongly rescue: the body's
        # appearance answers with the matching source-asset id but no name, so it cannot be told
        # from a same-base copy the write never reached.
        b = FakeBody("Quiet")
        b.appearance = FakeAppearance("AgentColor_1E8E3E", appearance_id=MINTED_ID)
        del b.appearance.name
        occ = FakeOcc("Wheel:1", bodies=[b])
        reached, not_reached, unverified = ap._occurrence_fanout(occ, MINTED_ID,
                                                                 "AgentColor_1E8E3E")
        assert reached == [] and not_reached == [] and unverified == ["Quiet"]

    def test_body_whose_appearance_does_not_read_back_is_unverified_not_applied(self, monkeypatch):
        b1, b2 = _silent_body("Quiet"), FakeBody("Reached")
        occ = FanoutOcc("Wheel:1", bodies=[b1, b2])
        _install_mp(monkeypatch, _root(occurrences=[occ]))
        out = _payload(ap.handler(target="Wheel:1", color="#1E8E3E"))
        assert out["applied_to"] == ["Wheel:1", "Reached"]     # never counted as applied
        assert out["unverified_bodies"] == ["Quiet"]
        assert "UNCONFIRMED" in out["note"]

    def test_bodyless_occurrence_reports_just_the_occurrence(self, monkeypatch):
        # No body reached and none NOT reached: nothing contradicts the occurrence read-back, so
        # the zero-reach error must not fire on an occurrence that simply holds no bodies.
        occ = FanoutOcc("Empty:1", bodies=[])
        _install_mp(monkeypatch, _root(occurrences=[occ]))
        out = _payload(ap.handler(target="Empty:1", color="#1E8E3E"))
        assert out["applied_to"] == ["Empty:1"]


# ── partial success: one body of a component fails, others still get colored ──

class TestPartialFailureComponentBodies:
    def test_one_body_fails_others_still_colored_and_reported(self):
        # A failure on one body must not swallow the bodies that already got colored earlier in the
        # loop: the bodies that succeeded are reported (applied_to) alongside the ones that failed
        # (failed), not folded into a single error() for the whole call.
        good = FakeBody("Good")
        bad = FakeBody("Bad", fail_appearance=True)
        held = _started_with(bad)
        comp = MakeComp("Multi", bodies=[good, bad])
        design, apps = _install(_root())
        _resolve_to(comp, "component")
        out = _payload(ap.handler(target="Multi", color="#123456"))
        assert out["applied_to"] == ["Good"]
        assert out["failed"] == [{"body": "Bad", "error": "appearance rejected for Bad"}]
        assert good.appearance is apps._copied[0][2]
        assert bad.appearance is held
        assert "1 of 2" in out["note"] and "failed" in out["note"]

    def test_all_bodies_fail_returns_error(self):
        bad1 = FakeBody("B1", fail_appearance=True)
        bad2 = FakeBody("B2", fail_appearance=True)
        comp = MakeComp("AllBad", bodies=[bad1, bad2])
        _install(_root())
        _resolve_to(comp, "component")
        res = ap.handler(target="AllBad", color="#123456")
        assert res["isError"] is True
        assert "AllBad" in res["message"]


# ── whole-design / component target applies to all bodies ─────────────────────

class TestResolveExtra:
    def test_component_target_applies_to_its_bodies(self):
        comp = MakeComp("Assembly", bodies=[FakeBody("B")])
        _install(_root())
        _resolve_to(comp, "component")
        out = _payload(ap.handler(target="Assembly", color="#010203"))
        assert out["kind"] == "component"
        assert out["applied_to"] == ["B"]

    def test_empty_target_is_whole_design(self):
        # TargetRef classifies '' as the whole design (a Component); appearance_set treats it as
        # 'component' and colors every body.
        root = _root(name="Root", bodies=[FakeBody("B")])
        _install(root)
        _resolve_to(root, "design")
        out = _payload(ap.handler(target="", color="#010203"))
        assert out["kind"] == "component"
        assert out["applied_to"] == ["B"]


class TestTheColorBase:
    """Every override is copied from ONE named appearance out of ONE named library, kept in the
    document under a fixed name. design.appearances.item(0) - whichever appearance entered the
    document first, measured 'Steel - Satin', an interior_model=1 METAL with no opaque_albedo
    channel - is never the base, so an override is never a tinted metal."""

    def _fresh(self, monkeypatch, *lib_appearances):
        """A design holding no base yet - just the metal that entered it first, which item(0) hands
        back - plus the named library carrying `lib_appearances`."""
        design, apps = _install_mp(monkeypatch, _root(bodies=[FakeBody("Body1")]),
                                   existing_appearances=("Steel - Satin",))
        _install_libraries(monkeypatch, _library(ap._BASE_LIBRARY, *lib_appearances))
        return design, apps

    def test_the_named_library_appearance_is_copied_in_under_the_base_name(self, monkeypatch):
        # the library lists the metal first and the matte alternative last: the base is picked by
        # EXACT name, not by position
        source = FakeAppearance(ap._BASE_SOURCE)
        design, apps = self._fresh(monkeypatch, FakeAppearance("Steel - Satin"), source,
                                   FakeAppearance("Plastic - Matte (White)"))
        out = _payload(ap.handler(target="Body1", color="#1E8E3E"))
        # two copies: the base out of the library, then the color override off that base
        assert [(c[0], c[1]) for c in apps._copied] == [
            (source, ap._BASE_NAME), (apps.itemByName(ap._BASE_NAME), "AgentColor_1E8E3E")]
        assert out["base_appearance"] == ap._BASE_NAME and out["base_reused"] is False
        assert ap._BASE_SOURCE in out["note"] and ap._BASE_LIBRARY in out["note"]

    def test_item_zero_is_never_the_base(self, monkeypatch):
        # The design's FIRST appearance is a metal carrying no albedo channel. Copying it (the
        # insertion-order pick) would mint a tinted metal and the albedo write would find nothing.
        metal = FakeAppearance("Steel - Satin", color_props=("metal_f0",))
        source = FakeAppearance(ap._BASE_SOURCE)
        design, apps = _install_mp(monkeypatch, _root(bodies=[FakeBody("Body1")]),
                                   existing_appearances=())
        design.appearances = FakeAppearances([metal])
        _install_libraries(monkeypatch, _library(ap._BASE_LIBRARY, source))
        out = _payload(ap.handler(target="Body1", color="#1E8E3E"))
        assert design.appearances._copied[0][0] is source        # NOT item(0)
        assert design.appearances.item(0) is metal              # ...which is still sitting there
        assert metal.appearanceProperties.item(0).value is None  # and was not written to
        assert out["base_appearance"] == ap._BASE_NAME

    def test_a_second_call_reuses_the_base_already_in_the_document(self, monkeypatch):
        design, apps = self._fresh(monkeypatch, FakeAppearance(ap._BASE_SOURCE))
        first = _payload(ap.handler(target="Body1", color="#1E8E3E"))
        second = _payload(ap.handler(target="Body1", color="#FF6D00"))
        assert first["base_reused"] is False and second["base_reused"] is True
        # the base was copied in ONCE; the second call only minted its own colour off it
        assert [c[1] for c in apps._copied] == [ap._BASE_NAME, "AgentColor_1E8E3E",
                                               "AgentColor_FF6D00"]
        assert ap._BASE_SOURCE not in second["note"]

    def test_the_base_is_looked_up_before_the_library_is_walked(self, monkeypatch):
        # The steady state: the base is already in the document, so no library read happens at all
        # (an unreadable catalog would refuse if it did).
        design, apps = _install_mp(monkeypatch, _root(bodies=[FakeBody("Body1")]))
        monkeypatch.setattr(ap._materials, "app", SimpleNamespace())   # no materialLibraries
        out = _payload(ap.handler(target="Body1", color="#1E8E3E"))
        assert out["base_reused"] is True and out["base_appearance"] == ap._BASE_NAME
        assert [c[1] for c in apps._copied] == ["AgentColor_1E8E3E"]

    def test_the_base_appearance_being_absent_from_the_library_is_refused_by_name(self, monkeypatch):
        # the library IS loaded but carries no appearance of that name -> name what was looked for
        design, apps = self._fresh(monkeypatch, FakeAppearance("Plastic - Matte (White)"))
        res = ap.handler(target="Body1", color="#1E8E3E")
        assert res["isError"] is True
        assert ap._BASE_SOURCE in res["message"] and ap._BASE_LIBRARY in res["message"]
        assert apps._copied == []                # nothing minted from a substitute

    def test_a_duplicated_library_name_is_refused_not_first_matched(self, monkeypatch):
        _install_mp(monkeypatch, _root(bodies=[FakeBody("Body1")]), existing_appearances=())
        _install_libraries(monkeypatch,
                           _library(ap._BASE_LIBRARY, FakeAppearance(ap._BASE_SOURCE)),
                           _library(ap._BASE_LIBRARY, FakeAppearance(ap._BASE_SOURCE)))
        res = ap.handler(target="Body1", color="#1E8E3E")
        assert res["isError"] is True and "refusing to pick one" in res["message"]

    def test_a_copy_that_returns_nothing_is_an_honest_failure(self, monkeypatch):
        design, apps = self._fresh(monkeypatch, FakeAppearance(ap._BASE_SOURCE))
        monkeypatch.setattr(apps, "addByCopy", lambda base, name: None)
        res = ap.handler(target="Body1", color="#1E8E3E")
        assert res["isError"] is True
        assert ap._BASE_NAME in res["message"] and "addByCopy" in res["message"]
        assert "already exists in document" in res["message"]   # the measured raise, not a guess

    def test_a_base_that_lands_between_the_lookup_and_the_copy_is_reused(self, monkeypatch):
        # a parallel call can create the base name in the gap: addByCopy refuses the duplicate, and
        # the re-check must find that one rather than report a failure
        design, apps = self._fresh(monkeypatch, FakeAppearance(ap._BASE_SOURCE))
        raced = FakeAppearance(ap._BASE_NAME)

        def land_it(base, name):
            if name == ap._BASE_NAME:
                apps._items.append(raced)
                return None
            return FakeAppearances.addByCopy(apps, base, name)
        monkeypatch.setattr(apps, "addByCopy", land_it)
        out = _payload(ap.handler(target="Body1", color="#1E8E3E"))
        assert out["base_reused"] is True
        assert apps._copied[0][0] is raced       # the colour was minted off the raced-in base

    def test_an_unreadable_appearances_collection_is_refused(self, monkeypatch):
        design, apps = self._fresh(monkeypatch, FakeAppearance(ap._BASE_SOURCE))
        del design.appearances
        res = ap.handler(target="Body1", color="#1E8E3E")
        assert res["isError"] is True and "could not be read" in res["message"]

    def test_a_reused_colour_appearance_publishes_no_base_it_did_not_read(self, monkeypatch):
        # the named appearance already exists, so no base was consulted - the payload may not claim
        # which base that appearance rides on
        design, apps = _install_mp(monkeypatch, _root(bodies=[FakeBody("Body1")]))
        _payload(ap.handler(target="Body1", color="#1E8E3E"))
        out = _payload(ap.handler(target="Body1", color="#1E8E3E"))
        assert out["appearance_reused"] is True
        assert "base_appearance" not in out and "base_reused" not in out


def _readable_colors(monkeypatch):
    """Point Color.create at the shared FakeColor - whose components READ, unlike the tuple stand-in
    _install binds - AFTER _install has bound that stand-in. Only this rig reaches the re-read
    comparison."""
    import adsk.core
    monkeypatch.setattr(adsk.core.Color, "create", staticmethod(FakeColor.create))


def _write_only_color(prop_id):
    """A ColorProperty that ACCEPTS an assignment and whose value GETTER declines to answer."""
    return ColorProperty(prop_id, read_raises="this ColorProperty's value cannot be read here")


class TestTheColorLandsOnTheAlbedoOnly:
    """The base exposes two ColorProperties. Writing both puts the requested colour into a channel
    the caller never named; the write is scoped to the albedo."""

    def test_only_the_albedo_channel_is_written(self):
        body = FakeBody("Body1")
        _install(_root(bodies=[body]))
        _payload(ap.handler(target="Body1", color="#1E8E3E"))
        props = body.appearance.appearanceProperties
        assert props.item(0).id == "opaque_albedo"
        assert props.item(0).value == ("color", 30, 142, 62, 255)
        assert props.item(1).id == "opaque_luminance_modifier"
        assert props.item(1).value is None

    def test_an_appearance_with_no_albedo_channel_is_refused(self):
        # a document appearance carrying the requested name but only a metal colour channel: there
        # is nothing to write the colour into, and a tinted metal is not what was asked for
        root = _root(bodies=[FakeBody("Body1")])
        design, apps = _install(root, existing_appearances=(ap._BASE_NAME,))
        apps._items.append(FakeAppearance("AgentColor_1E8E3E", color_props=("metal_f0",)))
        res = ap.handler(target="Body1", color="#1E8E3E")
        assert res["isError"] is True
        assert "metal_f0" in res["message"] and "opaque_albedo" in res["message"]
        assert "different 'name'" in res["message"]

    def test_a_read_only_albedo_channel_is_refused_not_reported_as_applied(self):
        # the channel is there but declines the write - the colour did not land, so the call must
        # not come back applied
        body = FakeBody("Body1")
        held = _started_with(body)
        design, apps = _install(_root(bodies=[body]))
        apps._items.append(FakeAppearance(
            "AgentColor_1E8E3E", color_props=[ColorProperty("opaque_albedo", read_only=True)]))
        res = ap.handler(target="Body1", color="#1E8E3E")
        assert res["isError"] is True and "opaque_albedo" in res["message"]
        assert body.appearance is held

    def test_a_channel_that_stores_nothing_is_refused_not_reported_as_applied(self, monkeypatch):
        # The assignment is ACCEPTED and the channel reads its prior colour back, so nothing but
        # the re-read tells this apart from a landed colour.
        body = FakeBody("Body1")
        held = _started_with(body)
        design, apps = _install(_root(bodies=[body]))
        _readable_colors(monkeypatch)
        apps._items.append(FakeAppearance("AgentColor_1E8E3E", color_props=[
            ColorProperty("opaque_albedo", value=FakeColor(200, 120, 48), swallows=True)]))
        res = ap.handler(target="Body1", color="#1E8E3E")
        assert res["isError"] is True
        assert "opaque_albedo" in res["message"] and "different one back" in res["message"]
        assert body.appearance is held

    def test_a_channel_that_stores_the_colour_is_still_applied(self, monkeypatch):
        # The mirror of the gate above on the same readable-colour rig: a channel that KEEPS what
        # it was given comes back applied with NO unconfirmed qualifier, or the re-read would
        # refuse - or hedge - every good write.
        body = FakeBody("Body1")
        _install(_root(bodies=[body]))
        _readable_colors(monkeypatch)
        out = _payload(ap.handler(target="Body1", color="#1E8E3E"))
        assert out["applied"] is True and out["applied_to"] == ["Body1"]
        assert "color_unconfirmed_channels" not in out
        assert "UNCONFIRMED" not in out["note"]

    def test_a_channel_that_will_not_read_back_is_applied_but_says_it_is_unconfirmed(self):
        # The fail-open direction: nothing refutes the write, so it stands - but the payload must
        # not read as a confirmed colour, the way the opacity path discloses the same condition.
        body = FakeBody("Body1")
        _install(_root(bodies=[body]))         # the tuple Color stand-in: no comparison possible
        out = _payload(ap.handler(target="Body1", color="#1E8E3E"))
        assert out["applied"] is True
        assert out["color_unconfirmed_channels"] == ["opaque_albedo"]
        assert "UNCONFIRMED" in out["note"]

    def test_the_written_colour_reads_but_the_channel_does_not_answer(self, monkeypatch):
        # The LIVE shape of the same disclosure: a real Color always answers red/green/blue, so
        # the colour WRITTEN reads and the only route left is a channel whose own read-back
        # declines. Without this the guard's second half is never exercised.
        body = FakeBody("Body1")
        design, apps = _install(_root(bodies=[body]))
        _readable_colors(monkeypatch)
        blind = FakeAppearance("AgentColor_1E8E3E", color_props=())
        blind.appearanceProperties = _Properties([_write_only_color("opaque_albedo")])
        apps._items.append(blind)
        out = _payload(ap.handler(target="Body1", color="#1E8E3E"))
        assert out["applied"] is True
        assert out["color_unconfirmed_channels"] == ["opaque_albedo"]
        assert "UNCONFIRMED" in out["note"]


# ── the opacity override: where it lands, what it renders, and how it fails ────

def _opacity_refusing_body(name):
    """A body that takes an appearance but whose opacity override the API REJECTS. Built as a
    scenario subclass so the shared body's own surface stays untouched."""
    def _guarded(self, key, value):
        if key == "opacity" and getattr(self, "_refuse_opacity", False):
            raise RuntimeError("opacity is read-only on this body")
        object.__setattr__(self, key, value)

    body = type("OpacityRefusingBody", (BRepBody,), {"__setattr__": _guarded})(name)
    body._refuse_opacity = True
    return body


class TestOpacityOverride:
    """Opacity is a second, independent override with its own holder rules: a face has none, an
    occurrence has none of its own (its COMPONENT carries it), and what RENDERS is inherited, so it
    is read back off the object rather than echoed. Every failure here aborts the whole call before
    an appearance is minted - a half-applied write would leave an orphan asset in the design."""

    def test_an_occurrence_opacity_is_written_to_its_component_and_the_note_says_so(self,
                                                                                    monkeypatch):
        # An Occurrence carries no settable opacity - the write goes to the Component, so it reaches
        # EVERY instance of that component, not the one the caller named. The caller is told, because
        # nothing in the request says the effect is per-instance.
        comp = MakeComp("Part", bodies=[FakeBody("B1", inherited_opacity=0.4)])
        occ = FakeOcc("Part:1", bodies=[FakeBody("B1")], component=comp)
        _install_mp(monkeypatch, _root(occurrences=[occ]))
        _resolve_to(occ, "occurrence")
        out = _payload(ap.handler(target="Part:1", opacity=40))
        assert abs(comp.opacity - 0.4) < 1e-9              # the COMPONENT took it
        assert getattr(occ, "opacity", None) is None       # the occurrence itself was never written
        assert "COMPONENT" in out["note"]
        assert "every instance of that component" in out["note"]

    def test_an_occurrence_whose_component_cannot_be_reached_is_refused_and_mints_nothing(
            self, monkeypatch):
        # The component is the only holder an occurrence write has. Unreachable means the opacity
        # was NOT set, so the call fails - and it fails before the appearance is copied, or a
        # refusal leaves a persistent orphan appearance in the design.
        orphan = type("OrphanOcc", (FakeOcc,), {
            "component": property(lambda self: None, lambda self, v: None)})("Part:1")
        design, apps = _install_mp(monkeypatch, _root(occurrences=[orphan]))
        _resolve_to(orphan, "occurrence")
        res = ap.handler(target="Part:1", color="#CC2200", opacity=50)
        assert res["isError"] is True
        assert "component behind this occurrence" in res["message"]
        assert apps._copied == [] and apps.itemByName("AgentColor_CC2200") is None
        assert orphan.appearance is None

    def test_a_face_has_no_opacity_and_is_told_which_target_does(self, monkeypatch):
        # A BRepFace takes a colour but has no opacity of its own; the refusal names the two targets
        # that do, so the caller can act on it instead of guessing.
        face = FakeFace()
        held = _started_with(face)
        design, apps = _install_mp(monkeypatch, _root())
        _resolve_to(face, "face")
        res = ap.handler(target="face:1", color="#CC2200", opacity=50)
        assert res["isError"] is True
        assert "FACE" in res["message"]
        assert "body" in res["message"] and "occurrence" in res["message"]
        assert apps._copied == [] and face.appearance is held

    def test_an_opacity_the_api_rejects_is_an_error_not_a_reported_setting(self, monkeypatch):
        # The API said no. Reporting the asked percent anyway is the cardinal sin - a write that
        # did not happen must come back isError, carrying what the API said.
        body = _opacity_refusing_body("Body1")
        held = _started_with(body)
        design, apps = _install_mp(monkeypatch, _root(bodies=[body]))
        res = ap.handler(target="Body1", color="#CC2200", opacity=50)
        assert res["isError"] is True
        assert "Could not set opacity" in res["message"]
        assert "read-only on this body" in res["message"]   # what the API actually said
        assert apps._copied == [] and body.appearance is held

    def test_an_unreadable_rendered_opacity_is_unconfirmed_not_the_asked_percent(self, monkeypatch):
        # The write landed; the read-back did not. opacity_rendered must stay None rather than be
        # filled in from the request, and the note must say the render is unconfirmed. MEASURED: a
        # NATIVE body's visibleOpacity RAISES, which is why the shared body declines it by default.
        body = FakeBody("Body1")
        _install_mp(monkeypatch, _root(bodies=[body]))
        out = _payload(ap.handler(target="Body1", opacity=40))
        assert abs(body.opacity - 0.4) < 1e-9              # the write itself did land
        assert out["opacity"] == 40
        assert out["opacity_rendered"] is None
        assert "UNCONFIRMED" in out["note"]
        # ...and the note names the ONE read that answers: the body's occurrence proxy. Without it
        # "unconfirmed" leaves the caller no route to the rendered value.
        assert "occurrence PROXY" in out["note"] and "target the occurrence" in out["note"]

    def test_a_component_opacity_is_read_back_off_a_body_not_off_the_component(self, monkeypatch):
        # A Component renders nothing itself, so its own opacity would read back the number just
        # written whether or not anything changed on screen. The rendered value comes off one of its
        # bodies - here 25%, which matches neither the asked 80% nor the 0.8 written on the holder.
        comp = MakeComp("Housing", bodies=[FakeBody("B1", inherited_opacity=0.25)])
        _install_mp(monkeypatch, _root())
        _resolve_to(comp, "component")
        out = _payload(ap.handler(target="Housing", opacity=80))
        assert abs(comp.opacity - 0.8) < 1e-9
        assert out["opacity"] == 80 and out["opacity_rendered"] == 25
        assert "RENDERS at 25%" in out["note"]


class TestDirectAppearanceAssignmentFailure:
    def test_a_body_whose_appearance_assignment_raises_is_an_error_naming_what_the_api_said(
            self, monkeypatch):
        # The single-entity branch (body/face/occurrence) has no per-body partial-success story to
        # tell: the one assignment raised, nothing was coloured, and the call must say so with the
        # API's own message rather than come back applied.
        body = FakeBody("Body1", fail_appearance=True)
        held = _started_with(body)
        _install_mp(monkeypatch, _root(bodies=[body]))
        res = ap.handler(target="Body1", color="#123456")
        assert res["isError"] is True
        assert "Body1" in res["message"]
        assert "appearance rejected for Body1" in res["message"]
        assert body.appearance is held
