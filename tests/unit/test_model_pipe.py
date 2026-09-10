"""Unit tests for model_pipe - a pipe/tube along a path in one feature.

What is worth pinning without live Fusion: the fraction inputs are RATIOS and refuse 0 / above 1;
the reverse fraction is refused on an open path (Fusion ignores it there) and needs the forward one;
the isHollow/sectionThickness pair is set hollow-FIRST so the requested wall survives the coupling,
and the wall is verified off the created feature (a solid read-back, or a wall other than the one
asked for, is an error); a 'new' op that lands no body - or a body with no volume - is an error;
a cut that moves no volume is an error; direct mode falls through to the body census; and
target_bodies is refused on 'new'.
"""

import types

import adsk.core
import adsk.fusion
import pytest

from conftest import (BRepBody, MakeComp, assert_no_active_design, body_proxy, error_message,
                      install, load_tool, make_design, make_sketch, make_sketch_curve,
                      make_source_document, payload, _NamedCollection)

mp = load_tool("model_pipe")

# The API's own default wall when isHollow is switched on without a thickness (cm).
_DEFAULT_WALL_CM = 0.1


@pytest.fixture(autouse=True)
def _adsk(monkeypatch):
    monkeypatch.setattr(adsk.core.ValueInput, "createByReal",
                        staticmethod(lambda v: ("real", v)), raising=False)


# ── fakes: the pipe feature graph only ──────────────────────────────────────

class _Path:
    def __init__(self, is_closed=False):
        self.isClosed = is_closed


class _UnreadablePath:
    """A Path whose isClosed read RAISES - what a proxy that stops answering looks like. The reverse
    extent cannot be shown to apply against it, so it must be refused, not set."""
    @property
    def isClosed(self):
        raise RuntimeError("isClosed unavailable")


class _Param:
    """A ModelParameter stand-in - the feature side hands back parameters, not raw numbers."""
    def __init__(self, value):
        self.value = value


class _PipeFeature:
    def __init__(self, name, bodies, section_size_cm, thickness_cm, is_hollow, capped=2):
        self.name = name
        self.bodies = _NamedCollection(list(bodies))
        self.sectionSize = _Param(section_size_cm)
        self.sectionThickness = _Param(thickness_cm) if thickness_cm is not None else None
        self.isHollow = is_hollow
        self.startFaces = _NamedCollection([None] if capped >= 1 else [])
        self.endFaces = _NamedCollection([None] if capped >= 2 else [])


class _PipeInput:
    """A PipeFeatureInput stand-in carrying the live COUPLING: isHollow=True resets the thickness to
    the API default, and setting a thickness turns isHollow on. `ignores` names properties whose
    assignment the platform silently drops (what set_verified exists to catch)."""

    _TRACKED = ("sectionType", "sectionSize", "isHollow", "sectionThickness",
                "distanceOne", "distanceTwo")

    def __init__(self, path, operation, ignores=(), thickness_clears_hollow=False):
        object.__setattr__(self, "_ignores", set(ignores))
        object.__setattr__(self, "_thickness_clears_hollow", bool(thickness_clears_hollow))
        object.__setattr__(self, "order", [])
        self.path = path
        self.operation = operation
        self.sectionType = None
        self.sectionSize = None
        self.isHollow = False
        self.sectionThickness = None
        self.distanceOne = None
        self.distanceTwo = None
        self.participantBodies = None
        object.__setattr__(self, "order", [])

    def __setattr__(self, name, value):
        if name in self._ignores:
            return
        object.__setattr__(self, name, value)
        if name in self._TRACKED:
            self.order.append(name)
        if name == "isHollow" and value is True:
            object.__setattr__(self, "sectionThickness", ("real", _DEFAULT_WALL_CM))
        elif name == "sectionThickness" and value is not None:
            object.__setattr__(self, "isHollow", not self._thickness_clears_hollow)


class _PipeFeatures:
    """Builds a feature that MIRRORS the input it was given, so a test asserts on what the handler
    actually configured. `effect` runs the canned model change (a cut's volume drop, a direct-mode
    body landing) before the feature is returned."""

    def __init__(self, comp, body_names=("Pipe1",), body_volume=5.0, returns_none=False,
                 ignores=(), create_raises=False, input_is_none=False, add_raises=False,
                 force_thickness_cm="mirror", effect=None, thickness_clears_hollow=False):
        self.comp = comp
        self.body_names = tuple(body_names)
        self.body_volume = body_volume
        self.returns_none = returns_none
        self.ignores = ignores
        self.create_raises = create_raises
        self.input_is_none = input_is_none
        self.add_raises = add_raises
        self.force_thickness_cm = force_thickness_cm
        self.effect = effect
        self.thickness_clears_hollow = thickness_clears_hollow
        self.last = None
        self.add_calls = 0

    def createInput(self, path, operation):
        if self.create_raises:
            raise RuntimeError("createInput boom")
        if self.input_is_none:
            return None
        self.last = _PipeInput(path, operation, self.ignores, self.thickness_clears_hollow)
        return self.last

    def add(self, inp):
        self.add_calls += 1
        if self.add_raises:
            raise RuntimeError("add boom")
        if self.effect:
            self.effect(self.comp)
        if self.returns_none:
            return None
        # MEASURED (live sweep): a SOLID pipe's feature reads back isHollow TRUE with a 0.0 wall -
        # the platform reports a solid section as a zero-wall hollow one, so the fake never hands a
        # solid create an isHollow=False read the tool could lean on.
        thickness = (inp.sectionThickness[1] if isinstance(inp.sectionThickness, tuple) else 0.0) \
            if self.force_thickness_cm == "mirror" else self.force_thickness_cm
        size = inp.sectionSize[1] if isinstance(inp.sectionSize, tuple) else 0.0
        bodies = [BRepBody(n, volume=self.body_volume) for n in self.body_names]
        return _PipeFeature("Pipe1", bodies, size, thickness, thickness is not None)


def _wire(bodies=(), path_closed=False, design_type=1, sketch_curves=1, path_unreadable=False,
          tokens=None, all_components=None, **kw):
    """A design whose root component owns the pipe feature collection and the path factory."""
    comp = MakeComp(bodies=bodies,
                    sketches=[make_sketch(name="Spine",
                                          lines=[make_sketch_curve(f"c{i}")
                                                 for i in range(sketch_curves)])])
    pf = _PipeFeatures(comp, **kw)
    made_path = _UnreadablePath() if path_unreadable else _Path(path_closed)
    comp.features = types.SimpleNamespace(
        pipeFeatures=pf, createPath=lambda seed, is_chain=True: made_path)
    design = make_design(comp=comp, tokens=tokens, all_components=all_components)
    design.designType = design_type          # 1 parametric, 0 direct
    install(mp, design)
    return pf


# ── the solid happy path ────────────────────────────────────────────────────

class TestSolidPipe:
    def test_circular_pipe_on_a_path_sketch(self):
        pf = _wire()
        out = payload(mp.handler(path="sketch:Spine", section_size=20))
        assert out["piped"] is True
        assert out["result_bodies"] == ["Pipe1"]
        assert out["section_type"] == "circular"
        # the created feature reads isHollow TRUE with a 0.0 wall (measured) - a zero wall IS solid,
        # so the verdict is False and no wall_thickness is published
        assert out["hollow"] is False
        assert "wall_thickness" not in out
        assert out["path"] == "sketch:Spine"
        assert out["volume_cm3"] == 5.0
        # 20 mm -> 2.0 cm reaches sectionSize as a ValueInput
        assert pf.last.sectionSize == ("real", 2.0)
        assert pf.last.sectionType == adsk.fusion.PipeSectionTypes.CircularPipeSectionType

    def test_square_section_reaches_the_input(self):
        pf = _wire()
        out = payload(mp.handler(path="sketch:Spine", section_size=10, section_type="square"))
        assert out["section_type"] == "square"
        assert pf.last.sectionType == adsk.fusion.PipeSectionTypes.SquarePipeSectionType

    def test_triangular_section_reaches_the_input(self):
        pf = _wire()
        payload(mp.handler(path="sketch:Spine", section_size=10, section_type="triangular"))
        assert pf.last.sectionType == adsk.fusion.PipeSectionTypes.TriangularPipeSectionType

    def test_unknown_section_type_refused(self):
        _wire()
        res = mp.handler(path="sketch:Spine", section_size=10, section_type="hexagonal")
        assert res["isError"] is True and "section_type" in error_message(res)

    def test_section_type_the_platform_drops_is_an_error(self):
        # A SWIG proxy accepts an assignment it then ignores: a square pipe would come out CIRCULAR.
        # set_verified catches it, and the pipe must not be built on the default section.
        pf = _wire(ignores=("sectionType",))
        res = mp.handler(path="sketch:Spine", section_size=10, section_type="square")
        msg = error_message(res)
        assert "section_type=square" in msg and "No pipe was created" in msg
        assert pf.add_calls == 0

    def test_measured_size_comes_from_the_feature_and_no_cap_count_is_claimed(self):
        _wire()
        out = payload(mp.handler(path="sketch:Spine", section_size=20, units="mm"))
        # read back off the feature's own parameter (2.0 cm -> 20 mm), not echoed from the request
        assert out["section_size_measured"] == 20.0
        # MEASURED: startFaces/endFaces/sideFaces all read an EMPTY collection (count 0) on a freshly
        # added pipe, so they cannot tell a capped end from an uncapped one - the payload must not
        # carry a cap count derived from them.
        assert "capped_ends" not in out


# ── hollow: the isHollow / sectionThickness coupling ────────────────────────

class TestHollow:
    def test_wall_thickness_survives_the_coupling(self):
        # isHollow must be set BEFORE the thickness: the other order lets isHollow's own default
        # (0.1 cm) overwrite the requested wall, which the fake reproduces.
        pf = _wire()
        out = payload(mp.handler(path="sketch:Spine", section_size=20, wall_thickness=2))
        assert out["hollow"] is True
        assert out["wall_thickness"] == 2.0                 # mm in, mm out
        assert pf.last.sectionThickness == ("real", 0.2)    # 2 mm -> 0.2 cm survived
        assert pf.last.order.index("isHollow") < pf.last.order.index("sectionThickness")

    def test_hollow_without_thickness_reports_the_api_default(self):
        _wire()
        out = payload(mp.handler(path="sketch:Spine", section_size=20, hollow=True, units="cm"))
        assert out["hollow"] is True
        assert out["wall_thickness"] == _DEFAULT_WALL_CM    # measured, not assumed

    def test_hollow_false_with_a_thickness_is_refused(self):
        _wire()
        res = mp.handler(path="sketch:Spine", section_size=20, hollow=False, wall_thickness=2)
        msg = error_message(res)
        assert "hollow" in msg and "wall_thickness" in msg

    def test_wall_that_did_not_take_is_an_error(self):
        # The platform drops the thickness assignment: the pipe comes out with the 0.1 cm default
        # instead of the 0.2 cm asked for - reported, never published as the requested wall.
        _wire(ignores=("sectionThickness",))
        res = mp.handler(path="sketch:Spine", section_size=20, wall_thickness=2)
        msg = error_message(res)
        assert "0.1" in msg and "0.2" in msg

    def test_hollow_requested_but_created_with_a_zero_wall_is_an_error(self):
        # The measured solid signature (isHollow true, wall 0.0) reached a HOLLOW request: the wall
        # did not take, and a zero wall is not a pipe wall.
        _wire(force_thickness_cm=0.0)
        res = mp.handler(path="sketch:Spine", section_size=20, wall_thickness=2)
        assert "SOLID" in error_message(res) and "0.0" in error_message(res)

    def test_hollow_requested_but_thickness_unreadable_is_an_error(self):
        _wire(force_thickness_cm=None)
        res = mp.handler(path="sketch:Spine", section_size=20, wall_thickness=2)
        assert "SOLID" in error_message(res) and "no section thickness" in error_message(res)

    def test_solid_requested_but_created_hollow_is_an_error(self):
        _wire(force_thickness_cm=0.3)
        res = mp.handler(path="sketch:Spine", section_size=20)
        assert "HOLLOW" in error_message(res)


# ── path fractions: ratios, not lengths ─────────────────────────────────────

class TestFractions:
    def test_half_the_path_reaches_distance_one(self):
        pf = _wire()
        out = payload(mp.handler(path="sketch:Spine", section_size=20, path_fraction=0.5))
        assert out["path_fraction"] == 0.5
        # a RATIO: it is NOT scaled by 'units' the way a length input is
        assert pf.last.distanceOne == ("real", 0.5)

    @pytest.mark.parametrize("bad", [0, -0.2, 1.5])
    def test_out_of_range_fraction_refused(self, bad):
        _wire()
        res = mp.handler(path="sketch:Spine", section_size=20, path_fraction=bad)
        assert "path_fraction" in error_message(res)

    def test_non_numeric_fraction_refused(self):
        _wire()
        res = mp.handler(path="sketch:Spine", section_size=20, path_fraction="half")
        assert "path_fraction" in error_message(res)

    def test_reverse_fraction_on_an_open_path_refused(self):
        pf = _wire(path_closed=False)
        res = mp.handler(path="sketch:Spine", section_size=20, path_fraction=0.4,
                         path_fraction_reverse=0.4)
        assert "OPEN" in error_message(res)
        assert pf.last is None                       # refused before the transaction opened

    def test_reverse_fraction_on_a_closed_path_applied(self):
        pf = _wire(path_closed=True)
        out = payload(mp.handler(path="sketch:Spine", section_size=20, path_fraction=0.4,
                                 path_fraction_reverse=0.4))
        assert out["path_closed"] is True
        assert pf.last.distanceTwo == ("real", 0.4)
        assert pf.last.order.index("distanceOne") < pf.last.order.index("distanceTwo")

    def test_reverse_fraction_refused_when_the_path_cannot_say_if_it_is_closed(self):
        # isClosed unreadable: the reverse extent is silently ignored on an open path, so a set that
        # cannot be shown to apply is refused rather than reported as applied.
        pf = _wire(path_unreadable=True)
        res = mp.handler(path="sketch:Spine", section_size=20, path_fraction=0.4,
                         path_fraction_reverse=0.4)
        assert "did not report whether it is closed" in error_message(res)
        assert pf.last is None                       # refused before the transaction opened

    def test_reverse_fraction_without_the_forward_one_refused(self):
        _wire(path_closed=True)
        res = mp.handler(path="sketch:Spine", section_size=20, path_fraction_reverse=0.4)
        assert "path_fraction" in error_message(res)

    def test_fractions_summing_past_the_whole_path_refused(self):
        _wire(path_closed=True)
        res = mp.handler(path="sketch:Spine", section_size=20, path_fraction=0.7,
                         path_fraction_reverse=0.5)
        assert "1.2" in error_message(res)


# ── honesty: the effect gate ────────────────────────────────────────────────

class TestHonesty:
    def test_no_body_created_is_an_error(self):
        _wire(body_names=())
        res = mp.handler(path="sketch:Spine", section_size=20)
        assert "no body" in error_message(res).lower()

    def test_zero_volume_body_is_an_error(self):
        _wire(body_volume=0.0)
        res = mp.handler(path="sketch:Spine", section_size=20)
        assert "no volume" in error_message(res).lower()

    def test_add_returning_none_in_a_parametric_design_is_an_error(self):
        _wire(returns_none=True)
        res = mp.handler(path="sketch:Spine", section_size=20)
        assert "no feature" in error_message(res).lower()

    def test_createinput_returning_none_is_an_error(self):
        _wire(input_is_none=True)
        res = mp.handler(path="sketch:Spine", section_size=20)
        assert "no pipe was created" in error_message(res).lower()

    def test_add_failure_surfaces(self):
        _wire(add_raises=True)
        res = mp.handler(path="sketch:Spine", section_size=20)
        assert "pipe failed" in error_message(res).lower()

    def test_createinput_failure_surfaces(self):
        _wire(create_raises=True)
        res = mp.handler(path="sketch:Spine", section_size=20)
        assert "could not start the pipe" in error_message(res).lower()

    def test_cut_that_moves_no_volume_is_an_error(self):
        _wire(bodies=[BRepBody("Block", volume=12.0)])
        res = mp.handler(path="sketch:Spine", section_size=20, operation="cut",
                         target_bodies=["Block"])
        assert "volume changed" in error_message(res)

    def test_cut_that_removes_material_reports_the_delta(self):
        block = BRepBody("Block", volume=12.0)

        def _shrink(comp):
            block.volume = 9.0

        pf = _wire(bodies=[block], effect=_shrink)
        out = payload(mp.handler(path="sketch:Spine", section_size=20, operation="cut",
                                 target_bodies=["Block"]))
        assert out["volume_delta_cm3"] == -3.0
        assert out["scoped_to_bodies"] == ["Block"]
        assert pf.last.participantBodies == [block]

    def test_the_scope_is_published_as_a_request_not_as_a_verified_effect(self):
        # MEASURED: participantBodies is WRITE-ONLY - the assignment succeeds and reading the
        # property back raises AttributeError - so no read-back exists. Nor does the volume gate
        # stand in for one: it sums ONE total delta over the whole watched set, and a scope the
        # platform dropped moves that total MORE, not less. The key must not read as a confirmation.
        block = BRepBody("Block", volume=12.0)

        def _shrink(comp):
            block.volume = 9.0

        _wire(bodies=[block], effect=_shrink)
        out = payload(mp.handler(path="sketch:Spine", section_size=20, operation="cut",
                                 target_bodies=["Block"]))
        assert "REQUESTED" in out["note"]
        assert "write-only" in out["note"] and "model_inspect" in out["note"]

    def test_an_unscoped_cut_makes_no_scope_claim_at_all(self):
        block = BRepBody("Block", volume=12.0)

        def _shrink(comp):
            block.volume = 9.0

        _wire(bodies=[block], effect=_shrink)
        out = payload(mp.handler(path="sketch:Spine", section_size=20, operation="cut"))
        assert out["scoped_to_bodies"] is None
        assert "write-only" not in out["note"]

    def test_cut_watches_a_participant_in_another_component(self, monkeypatch):
        # The participant lives in a DIFFERENT component from the pipe. A census scoped to the
        # pipe's own component sees only the untouched body, so a successful cut would be reported
        # as "no volume changed" - the census is counted on the TARGET's component instead.
        monkeypatch.setattr(adsk.fusion, "BRepBody", BRepBody, raising=False)
        block = BRepBody("Block", volume=12.0, entity_token="BLOCK")
        other = MakeComp(name="Other", bodies=[block])
        block.parentComponent = other

        def _shrink(_comp):
            block.volume = 9.0

        pf = _wire(bodies=[BRepBody("Untouched", volume=8.0, entity_token="UNTOUCHED")],
                   tokens={"BLOCK": block}, all_components=None, effect=_shrink)
        out = payload(mp.handler(path="sketch:Spine", section_size=20, operation="cut",
                                 target_bodies=["BLOCK"]))
        assert out["volume_delta_cm3"] == -3.0
        assert out["scoped_to_bodies"] == ["Block"]
        assert pf.last.participantBodies == [block]

    def test_target_bodies_refused_on_new(self):
        _wire(bodies=[BRepBody("Block", volume=12.0)])
        res = mp.handler(path="sketch:Spine", section_size=20, target_bodies=["Block"])
        assert "target_bodies" in error_message(res)

    def test_unresolvable_target_body_refused_before_the_transaction(self):
        pf = _wire(bodies=[BRepBody("Block", volume=12.0)])
        res = mp.handler(path="sketch:Spine", section_size=20, operation="cut",
                         target_bodies=["Missing"])
        assert "Missing" in error_message(res)
        assert pf.last is None


# ── the watched-body census keys on the PHYSICAL body, not on a wrapper token ──

# The x-ref shape, measured on a host holding two x-refs of one design: two DISTINCT bodies read one
# byte-identical entityToken while their source documents' lineage ids differ. A body and its own
# occurrence proxy are the other half - one physical body reading two different wrapper tokens.
_URN_XREF = "urn:adsk.wipprod:dm.lineage:K3I2nkywRlaWPHJexysOdA"
_URN_HOST = "urn:adsk.wipprod:dm.lineage:N_QoPrrrSJmF__f9BZV86A"
_SHARED_TOKEN = "/vB+AAEAAwAAAAAAAAAAAAAA"


def _component_in_document(name, urn):
    """A component in the document whose lineage id is `urn` - the chain a body's source document is
    read through (parentComponent -> parentDesign -> parentDocument -> dataFile.id)."""
    return MakeComp(name=name, parent_design=make_source_document(urn))


class TestWatchedBodiesKeyOnPhysicalIdentity:
    def test_two_participants_sharing_a_document_local_token_are_both_watched(self, monkeypatch):
        # An entityToken is DOCUMENT-LOCAL, so keyed on it the second participant is dropped from
        # the watched set and the cut that removed ITS material is reported as "no volume changed".
        monkeypatch.setattr(adsk.fusion, "BRepBody", BRepBody, raising=False)
        standing = BRepBody("Rail", volume=8.0, entity_token=_SHARED_TOKEN,
                            parent_component=_component_in_document("Host", _URN_HOST))
        moved = BRepBody("Frame", volume=12.0, entity_token=_SHARED_TOKEN,
                         parent_component=_component_in_document("Xref", _URN_XREF))
        assert standing.entityToken == moved.entityToken      # the tokens really collide

        def _cut(_comp):
            moved.volume = 9.0

        _wire(tokens={"RAIL": standing, "FRAME": moved}, effect=_cut)
        out = payload(mp.handler(path="sketch:Spine", section_size=20, operation="cut",
                                 target_bodies=["RAIL", "FRAME"]))
        assert out["volume_delta_cm3"] == -3.0
        assert out["scoped_to_bodies"] == ["Rail", "Frame"]

    def test_one_body_reached_natively_and_through_its_proxy_is_watched_once(self, monkeypatch):
        # The other direction: the host's own collection and the participant list reach ONE physical
        # body, whose two wrappers carry different tokens. Watched twice, its volume change is
        # counted twice and the published delta is double what the cut actually removed.
        monkeypatch.setattr(adsk.fusion, "BRepBody", BRepBody, raising=False)
        host = _component_in_document("Host", _URN_HOST)
        block = BRepBody("Block", volume=12.0, entity_token=_SHARED_TOKEN, parent_component=host)
        host.bRepBodies = _NamedCollection([block])
        proxy = body_proxy(block, types.SimpleNamespace(name="Block:1"))
        assert proxy.entityToken != block.entityToken         # the wrappers really differ

        def _cut(_comp):
            block.volume = 9.0

        _wire(tokens={"PROXY": proxy}, effect=_cut)
        out = payload(mp.handler(path="sketch:Spine", section_size=20, operation="cut",
                                 target_bodies=["PROXY"]))
        assert out["volume_delta_cm3"] == -3.0

    def test_two_bodies_with_no_readable_identity_are_watched_separately(self, monkeypatch):
        # The `or id(b)` last resort: two bodies nothing can be identified from must over-count
        # rather than merge, or the one that moved drops out of the census entirely.
        monkeypatch.setattr(adsk.fusion, "BRepBody", BRepBody, raising=False)
        standing = BRepBody("Rail", volume=8.0)
        moved = BRepBody("Frame", volume=12.0)
        for b in (standing, moved):
            del b.entityToken

        def _cut(_comp):
            moved.volume = 9.0

        _wire(tokens={"RAIL": standing, "FRAME": moved}, effect=_cut)
        out = payload(mp.handler(path="sketch:Spine", section_size=20, operation="cut",
                                 target_bodies=["RAIL", "FRAME"]))
        assert out["volume_delta_cm3"] == -3.0


# ── direct mode: no feature object, so the body census decides ──────────────

class TestDirectMode:
    def test_landed_body_is_reported_without_a_feature_name(self):
        def _land(comp):
            comp.bRepBodies._items.append(BRepBody("Pipe1", volume=5.0))

        _wire(design_type=0, returns_none=True, effect=_land)
        out = payload(mp.handler(path="sketch:Spine", section_size=20, wall_thickness=2))
        assert out["no_timeline_feature"] is True
        assert out["result_bodies"] == ["Pipe1"]
        assert out["volume_cm3"] == 5.0
        assert "feature" not in out
        # the wall cannot be read back off a non-parametric feature - say so, never claim it
        assert out["hollow_requested"] is True and "UNVERIFIED" in out["note"]

    def test_no_body_landed_is_an_error(self):
        _wire(design_type=0, returns_none=True)
        res = mp.handler(path="sketch:Spine", section_size=20)
        assert "body count did not rise" in error_message(res)


# -- the isHollow <-> sectionThickness overwrite defenses --------------------
#
# The two input properties SET EACH OTHER: isHollow=true resets the thickness to the API's own
# default, and writing a thickness turns isHollow on. Both directions of that coupling can fail
# silently - a SWIG proxy accepts an assignment it then drops - and the pipe would come out with the
# wrong wall while every other read-back looks clean. These are the two guards that stop it.

class TestHollowCouplingDefenses:
    def test_an_isHollow_the_platform_drops_refuses_before_any_pipe_is_built(self):
        # isHollow is set FIRST and through set_verified: dropped, the wall would land on a SOLID
        # input and the pipe would be built without one.
        pf = _wire(ignores=("isHollow",))
        res = mp.handler(path="sketch:Spine", section_size=20, wall_thickness=2)
        msg = error_message(res)
        assert "hollow=true" in msg and "No pipe was created" in msg
        assert pf.add_calls == 0

    def test_a_thickness_that_switches_the_input_back_to_solid_is_refused(self):
        # The other direction of the coupling: writing sectionThickness clears isHollow. Nothing
        # downstream would catch it - the feature would read back a solid pipe the caller asked to
        # be hollow - so the input is re-read after the write and the call refuses.
        pf = _wire(thickness_clears_hollow=True)
        res = mp.handler(path="sketch:Spine", section_size=20, wall_thickness=2)
        msg = error_message(res)
        assert "back to SOLID" in msg and "No pipe was created" in msg
        assert pf.add_calls == 0


# -- DIRECT mode, cut/join/intersect: no feature AND no readable volume ------

class TestDirectModeBooleanIsUnverified:
    def test_a_direct_cut_whose_volumes_cannot_be_read_is_unverified_not_ok(self):
        # A direct design returns no feature object, so the volume census is the ONLY evidence a
        # cut/join/intersect changed anything. With no body's volume readable at both ends there is
        # nothing to judge on, and an ok() here would report an unmeasured edit as a success.
        blind = BRepBody("Rail", volume=None)
        _wire(bodies=[blind], design_type=0, returns_none=True)
        res = mp.handler(path="sketch:Spine", section_size=20, operation="cut")
        msg = error_message(res)
        assert "UNVERIFIED" in msg and "DIRECT" in msg

    def test_a_direct_cut_whose_volume_moved_is_reported_with_its_delta(self):
        # the same path with a READABLE census: the delta carries the verdict, no feature needed
        moved = BRepBody("Rail", volume=10.0)

        def _cut(_comp):
            moved.volume = 6.0

        _wire(bodies=[moved], design_type=0, returns_none=True, effect=_cut)
        out = payload(mp.handler(path="sketch:Spine", section_size=20, operation="cut"))
        assert out["volume_delta_cm3"] == -4.0
        assert out["no_timeline_feature"] is True


# ── guards ──────────────────────────────────────────────────────────────────

class TestGuards:
    def test_section_size_is_required(self):
        _wire()
        res = mp.handler(path="sketch:Spine")
        assert "section_size" in error_message(res)

    def test_zero_section_size_refused(self):
        _wire()
        res = mp.handler(path="sketch:Spine", section_size=0)
        assert "section_size" in error_message(res)

    def test_unknown_units_refused(self):
        _wire()
        res = mp.handler(path="sketch:Spine", section_size=20, units="furlong")
        assert "units" in error_message(res).lower()

    def test_unknown_operation_refused(self):
        _wire()
        res = mp.handler(path="sketch:Spine", section_size=20, operation="weld")
        assert "operation" in error_message(res).lower()

    def test_missing_path_refused(self):
        _wire()
        res = mp.handler(section_size=20)
        assert "path" in error_message(res).lower()

    def test_unknown_path_sketch_refused(self):
        _wire()
        res = mp.handler(path="sketch:NoSuch", section_size=20)
        assert "NoSuch" in error_message(res)

    def test_empty_path_sketch_refused(self):
        _wire(sketch_curves=0)
        res = mp.handler(path="sketch:Spine", section_size=20)
        assert "no curves" in error_message(res).lower()

    def test_no_active_design(self):
        _wire()
        assert_no_active_design(mp, mp.handler, path="sketch:Spine", section_size=20)


# ── declared outputs ────────────────────────────────────────────────────────

def test_declared_returns_present_in_payload():
    _wire()
    out = payload(mp.handler(path="sketch:Spine", section_size=20))
    for spec in mp.RETURNS:
        assert spec.assert_present(out) == "", spec.key


def test_the_path_description_states_the_tangent_continuity_rule():
    # measured through THIS tool: a tangent-continuous closed loop chained all 8 edges from one
    # seed, while a fillet patch that breaks tangency at the junction stops the chain. Chaining
    # follows tangent continuity, not open-vs-closed, and only the reported count says what ran.
    desc = mp.pipe_tool.to_dict()["inputSchema"]["properties"]["path"]["description"]
    assert "TANGENT connections only" in desc
    assert "'path' count is the truth" in desc
    assert "auto-chain" not in desc.lower()
    assert "closed loop" not in desc.lower() and "seed edge alone" not in desc
