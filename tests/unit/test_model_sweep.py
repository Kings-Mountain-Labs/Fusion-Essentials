"""Unit tests for ``model_sweep.py`` - sweep a profile along a path into a solid/surface.

Pinned: the solid happy path, the open-profile -> surface fallback (and as_surface forcing a surface
off a closed profile), the path builders (a path sketch vs model edges), operation + orientation
handling, target_bodies scoping, the guards (missing profile, bad sketch/path/operation/orientation,
no design), and the honesty contract (an API no-op that creates no body must report isError, and a
swallowed configure/add failure must surface).
"""

import types

import adsk.fusion

from conftest import (load_tool, make_design, install, make_sketch, payload as _payload,
                      error_message, assert_no_active_design, BRepBody, BRepEdge,
                      FakeFeatures as _SharedFeatures, Line3D, Profile, _NamedCollection)

sw = load_tool("model_sweep")


# ── small sweep-shaped fakes ────────────────────────────────────────────────

class FakeOpenProfile:
    pass


def _sketch(name, profiles=(), curves=0, owner=None):
    """A sketch holding `profiles` closed regions and `curves` plain curves. `owner` is the
    parentComponent the OPEN-profile path hosts the feature on; left unset it falls back to the
    active component, which cannot tell two same-named sketches apart."""
    sk = make_sketch(name, lines=[object() for _ in range(curves)], profiles=profiles)
    sk.parentComponent = owner
    return sk


class FakeSweepInput:
    def __init__(self, profile, path, op):
        self.profile = profile
        self.path = path
        self.operation = op
        self.isSolid = True
        self.orientation = None
        self.participantBodies = None


class FakeSweepFeature:
    def __init__(self, bodies_names=("Body1",), is_solid=True):
        self.name = "Sweep1"
        self.isSolid = is_solid
        self.bodies = _NamedCollection([BRepBody(n) for n in bodies_names])


class FakeSweepFeatures:
    def __init__(self, body_names=("Body1",)):
        self.last = None
        self.body_names = tuple(body_names)
        self.create_raises = False
        self.add_returns_none = False
        self.add_raises = False

    def createInput(self, profile, path, op):
        if self.create_raises:
            raise RuntimeError("createInput boom")
        self.last = FakeSweepInput(profile, path, op)
        return self.last

    def add(self, inp):
        if self.add_raises:
            raise RuntimeError("add boom")
        if self.add_returns_none:
            return None
        # Echo the requested isSolid back off the feature, as the live API does.
        return FakeSweepFeature(bodies_names=self.body_names, is_solid=inp.isSolid)


def _built_path(count):
    """A built adsk.fusion.Path: `count` is the number of edges the path ACTUALLY holds, which is
    not derivable from how many handles were passed in."""
    return types.SimpleNamespace(count=count)


class FakeFeatures(_SharedFeatures):
    """comp.features plus the sweep collection and the createPath factory a sweep drives."""
    def __init__(self, sweepfeatures):
        super().__init__()
        self.sweepFeatures = sweepfeatures
        self.path_calls = []
        self.path_returns = _built_path(1)

    def createPath(self, seed, is_chain):
        self.path_calls.append((seed, is_chain))
        return self.path_returns


def _install(*, closed_profiles=1, open_curves=0, body_names=("Body1",), tokens=None,
             extra_sketches=(), bodies=()):
    """Build a root component carrying the sweep surface (sketches + features + createOpenProfile) and
    wire it in via conftest's make_design/install (both seams). `bodies` are the component's existing
    solid bodies - what a cut/intersect samples volumes over. Returns (module-features, design)."""
    from conftest import MakeComp
    sf = FakeSweepFeatures(body_names=body_names)
    comp = MakeComp(name="Root", bodies=list(bodies))
    comp.features = FakeFeatures(sf)
    comp.createOpenProfile = lambda coll, chain: FakeOpenProfile()

    sketches = [
        _sketch("Prof", profiles=[Profile() for _ in range(closed_profiles)],
                curves=open_curves),
        _sketch("PathSketch", curves=3),
    ]
    sketches.extend(extra_sketches)
    comp.sketches = _NamedCollection(sketches)

    design = make_design(comp=comp, tokens=tokens or {})
    install(sw, design)
    return sf, design


# ── the solid happy path ────────────────────────────────────────────────────

class TestSolid:
    def test_solid_sweep_along_path_sketch(self):
        sf, _ = _install()
        out = _payload(sw.handler(profile={"sketch": "Prof", "profile_index": 0},
                                  path="sketch:PathSketch"))
        assert out["swept"] is True
        assert out["is_solid"] is True
        assert out["as_surface"] is False
        assert out["open_profile"] is False
        assert out["path"] == "sketch:PathSketch"
        assert out["result_bodies"] == ["Body1"]
        # A closed profile with the default (solid) sets isSolid True on the input.
        assert sf.last.isSolid is True

    def test_path_sketch_seeds_createpath_with_chain(self):
        sf, design = _install()
        _payload(sw.handler(profile={"sketch": "Prof"}, path="sketch:PathSketch"))
        feats = design.rootComponent.features
        # The path sketch has curves -> createPath is called with (seed, isChain=True).
        assert len(feats.path_calls) == 1 and feats.path_calls[0][1] is True

    def test_multiple_result_bodies_collected(self):
        _install(body_names=("R0", "R1", "R2"))
        out = _payload(sw.handler(profile={"sketch": "Prof"}, path="sketch:PathSketch"))
        assert out["result_bodies"] == ["R0", "R1", "R2"]


# ── surface fallback + forcing ──────────────────────────────────────────────

class TestSurface:
    def test_open_profile_falls_back_to_surface(self):
        # Profile sketch has NO closed region but open curves -> an OPEN profile / SURFACE sweep.
        sf, _ = _install(closed_profiles=0, open_curves=2)
        out = _payload(sw.handler(profile={"sketch": "Prof"}, path="sketch:PathSketch"))
        assert out["open_profile"] is True
        assert out["as_surface"] is True
        assert out["is_solid"] is False
        assert "SURFACE" in out["note"]
        assert sf.last.isSolid is False

    def test_as_surface_forces_surface_off_closed_profile(self):
        # A CLOSED profile, but as_surface=True -> isSolid False (open tube), open_profile stays False.
        sf, _ = _install(closed_profiles=1)
        out = _payload(sw.handler(profile={"sketch": "Prof"}, path="sketch:PathSketch",
                                  as_surface=True))
        assert out["is_solid"] is False
        assert out["as_surface"] is True
        assert out["open_profile"] is False
        assert sf.last.isSolid is False


# ── path from model edges ───────────────────────────────────────────────────

class TestEdgePath:
    def test_single_edge_path_seeds_createpath(self):
        adsk.fusion.BRepEdge = BRepEdge
        edge = BRepEdge(curve=Line3D())
        sf, design = _install(tokens={"EDGE1": edge})
        out = _payload(sw.handler(profile={"sketch": "Prof"}, path=["EDGE1"]))
        assert out["path"] == "1 edge(s) from 1 seed handle"
        feats = design.rootComponent.features
        # A single edge goes to createPath with chaining requested, not to Path.create.
        assert feats.path_calls and feats.path_calls[0][0] is edge

    def test_path_reports_the_edges_the_built_path_holds_not_the_one_passed(self):
        # The seed expanded: the swept path holds 14 edges though ONE handle was named. The payload
        # describes the path Fusion built, so an agent reading 'path' is not told the sweep ran over
        # a single edge.
        adsk.fusion.BRepEdge = BRepEdge
        edge = BRepEdge(curve=Line3D())
        sf, design = _install(tokens={"EDGE1": edge})
        design.rootComponent.features.path_returns = _built_path(14)
        out = _payload(sw.handler(profile={"sketch": "Prof"}, path=["EDGE1"]))
        assert out["path"] == "14 edge(s) from 1 seed handle"

    def test_bad_edge_handle_errors(self):
        adsk.fusion.BRepEdge = BRepEdge
        _install(tokens={})
        res = sw.handler(profile={"sketch": "Prof"}, path=["NOPE"])
        assert res["isError"] is True
        assert "handle did not resolve" in error_message(res)


# ── operation + orientation + target_bodies ─────────────────────────────────

class TestOptions:
    def test_operation_echoed(self):
        _install()
        out = _payload(sw.handler(profile={"sketch": "Prof"}, path="sketch:PathSketch",
                                  operation="join"))
        assert out["operation"] == "join"

    def test_orientation_parallel_applied(self):
        # Pin that the parallel keyword actually reaches the input via the SweepOrientationTypes enum.
        sf, _ = _install()
        out = _payload(sw.handler(profile={"sketch": "Prof"}, path="sketch:PathSketch",
                                  orientation="parallel"))
        assert out["orientation"] == "parallel"
        assert sf.last.orientation == adsk.fusion.SweepOrientationTypes.ParallelOrientationType

    def test_a_join_landing_a_new_body_names_model_combine(self):
        _install(bodies=[BRepBody("Bar")])
        out = _payload(sw.handler(profile={"sketch": "Prof"}, path="sketch:PathSketch",
                                  operation="join"))
        assert "landed a NEW body (Body1)" in out["note"]
        assert "model_combine(join)" in out["note"]

    def test_a_join_that_grew_the_body_already_there_appends_nothing(self):
        # the feature's result body IS the one the component held - the join fused, say nothing
        _install(bodies=[BRepBody("Body1")])
        out = _payload(sw.handler(profile={"sketch": "Prof"}, path="sketch:PathSketch",
                                  operation="join"))
        assert "NEW body" not in out["note"]

    def test_target_bodies_rejected_on_new(self):
        _install()
        res = sw.handler(profile={"sketch": "Prof"}, path="sketch:PathSketch",
                         target_bodies=["Body1"])
        assert res["isError"] is True and "target_bodies" in res["message"]

    def test_target_bodies_unresolved_errors_on_cut(self):
        _install()
        # A cut allows target_bodies, but an unresolvable name is a clean BodyRefList error - the
        # mutation never runs, so a wrong scope can't silently pass through.
        res = sw.handler(profile={"sketch": "Prof"}, path="sketch:PathSketch",
                         operation="cut", target_bodies=["Missing"])
        assert res["isError"] is True and "Missing" in res["message"]


# ── guards ──────────────────────────────────────────────────────────────────

class TestGuards:
    def test_missing_profile(self):
        _install()
        res = sw.handler(profile=None, path="sketch:PathSketch")
        assert res["isError"] is True and "profile" in res["message"].lower()

    def test_unknown_sketch_profile(self):
        _install()
        res = sw.handler(profile={"sketch": "NoSuch"}, path="sketch:PathSketch")
        assert res["isError"] is True and "sketch" in res["message"].lower()

    def test_unknown_path_sketch(self):
        _install()
        res = sw.handler(profile={"sketch": "Prof"}, path="sketch:NoPath")
        assert res["isError"] is True and "NoPath" in res["message"]

    def test_bad_operation(self):
        _install()
        res = sw.handler(profile={"sketch": "Prof"}, path="sketch:PathSketch", operation="weld")
        assert res["isError"] is True and "operation" in res["message"].lower()

    def test_bad_orientation(self):
        _install()
        res = sw.handler(profile={"sketch": "Prof"}, path="sketch:PathSketch",
                         orientation="sideways")
        assert res["isError"] is True and "orientation" in res["message"].lower()

    def test_no_active_design(self):
        _install()
        assert_no_active_design(sw, sw.handler,
                                profile={"sketch": "Prof"}, path="sketch:PathSketch")


# ── honesty contract ────────────────────────────────────────────────────────

class TestHonesty:
    def test_no_body_created_is_error(self):
        # add() returns a feature with ZERO bodies on a 'new' op -> a silent no-op; must be isError.
        _install(body_names=())
        res = sw.handler(profile={"sketch": "Prof"}, path="sketch:PathSketch")
        assert res["isError"] is True and "no body" in res["message"].lower()

    def test_add_returning_none_is_error(self):
        sf, _ = _install()
        sf.add_returns_none = True
        res = sw.handler(profile={"sketch": "Prof"}, path="sketch:PathSketch")
        assert res["isError"] is True and "no feature" in res["message"].lower()

    def test_createinput_failure_surfaces(self):
        sf, _ = _install()
        sf.create_raises = True
        res = sw.handler(profile={"sketch": "Prof"}, path="sketch:PathSketch")
        assert res["isError"] is True and "start sweep" in res["message"].lower()

    def test_add_failure_surfaces(self):
        sf, _ = _install()
        sf.add_raises = True
        res = sw.handler(profile={"sketch": "Prof"}, path="sketch:PathSketch")
        assert res["isError"] is True and "sweep failed" in res["message"].lower()


# ── a cut/intersect must PROVE it moved material ────────────────────────────
#
# A sweep whose path runs past the body still hands back a healthy feature with result bodies, so
# the feature object cannot tell a real cut from a no-op. Only the volumes of the bodies it could
# act on - the participants when target_bodies scopes it, otherwise the host's solids - can.

def _add_moving_volume(sf, *changes, rolled_back=None):
    """Make sweepFeatures.add apply (body, new_volume) pairs - the material effect a real
    cut/intersect has between the pre- and post-mutation reads. `rolled_back` collects the
    feature's deleteMe() calls, which is how the scoped no-op path reports its rollback."""
    def _add(inp):
        for body, volume in changes:
            body.volume = volume
        feature = FakeSweepFeature(bodies_names=sf.body_names, is_solid=inp.isSolid)
        if rolled_back is not None:
            feature.deleteMe = lambda: rolled_back.append(True) or True
        return feature
    sf.add = _add


class TestCutMovesMaterial:
    def test_unscoped_cut_that_moves_no_volume_is_an_error(self):
        _install(bodies=[BRepBody("Bar", volume=12.0)])
        res = sw.handler(profile={"sketch": "Prof"}, path="sketch:PathSketch", operation="cut")
        assert res["isError"] is True
        assert "changed nothing" in res["message"] and "'Root'" in res["message"]
        # unscoped: the cut could have reached a co-located component this sample never read, so the
        # feature is LEFT and named, never silently rolled back
        assert "design_delete_feature" in res["message"]

    def test_scoped_cut_that_moves_no_volume_rolls_the_feature_back(self):
        bar = BRepBody("Bar", volume=12.0)
        sf, _ = _install(bodies=[bar])
        rolled = []
        _add_moving_volume(sf, rolled_back=rolled)
        res = sw.handler(profile={"sketch": "Prof"}, path="sketch:PathSketch", operation="cut",
                         target_bodies=["Bar"])
        assert res["isError"] is True
        assert "changed nothing" in res["message"] and "Bar" in res["message"]
        # only a participant can be affected, so nothing landed anywhere - safe to remove
        assert rolled == [True] and "rolled back" in res["message"]

    def test_scoped_cut_samples_only_the_participants(self):
        # A volume that moved on a body OUTSIDE 'target_bodies' is not this cut's effect - sampling
        # the whole component instead of the participants would pass this no-op as a success.
        bar, other = BRepBody("Bar", volume=12.0), BRepBody("Other", volume=5.0)
        sf, _ = _install(bodies=[bar, other])
        _add_moving_volume(sf, (other, 1.0))
        res = sw.handler(profile={"sketch": "Prof"}, path="sketch:PathSketch", operation="cut",
                         target_bodies=["Bar"])
        assert res["isError"] is True and "changed nothing" in res["message"]

    def test_cut_that_removed_material_publishes_the_delta(self):
        bar = BRepBody("Bar", volume=12.0)
        sf, _ = _install(bodies=[bar])
        _add_moving_volume(sf, (bar, 9.5))
        out = _payload(sw.handler(profile={"sketch": "Prof"}, path="sketch:PathSketch",
                                  operation="cut"))
        assert out["volume_delta_cm3"] == -2.5      # signed: material LEFT the body

    def test_a_consumed_body_is_not_read_as_a_no_op(self):
        # A body the cut consumed whole stops reporting a volume, so it contributes no delta - the
        # untouched second body's 0 must not become "nothing happened".
        eaten, kept = BRepBody("Eaten", volume=4.0), BRepBody("Kept", volume=8.0)
        sf, _ = _install(bodies=[eaten, kept])
        _add_moving_volume(sf, (eaten, None))
        out = _payload(sw.handler(profile={"sketch": "Prof"}, path="sketch:PathSketch",
                                  operation="cut"))
        assert out["swept"] is True

    def test_a_new_body_sweep_is_never_volume_gated(self):
        _install(bodies=[BRepBody("Bar", volume=12.0)])
        out = _payload(sw.handler(profile={"sketch": "Prof"}, path="sketch:PathSketch"))
        assert out["swept"] is True and "volume_delta_cm3" not in out

    def test_unreadable_volumes_neither_error_nor_publish_a_delta(self):
        _install(bodies=[BRepBody("Bar", volume=None)])
        out = _payload(sw.handler(profile={"sketch": "Prof"}, path="sketch:PathSketch",
                                  operation="cut"))
        assert out["swept"] is True and "volume_delta_cm3" not in out


# ── cross-component hosting: the feature lands on the profile's OWNER ────────────────────────────

def _owned_profile(owner):
    """A closed profile whose parentSketch.parentComponent names its OWNING component - the chain
    profile_host_component reads to decide where the feature is built."""
    return Profile(parent_sketch=_sketch("Prof", owner=owner))


def _install_two_component(body_names=("Body1",)):
    """Root is the ACTIVE component; a SUB-component 'Frame' owns the profile + the path sketch. Each
    component carries its OWN features surface, so the test can tell WHICH one the sweep was built on.
    Returns (root_features, sub_features, design)."""
    from conftest import MakeComp
    root_sf = FakeSweepFeatures(body_names=body_names)
    root = MakeComp(name="Root", bodies=())
    root.features = FakeFeatures(root_sf)
    root.createOpenProfile = lambda coll, chain: FakeOpenProfile()
    root.sketches = _NamedCollection([])

    sub_sf = FakeSweepFeatures(body_names=body_names)
    sub = MakeComp(name="Frame", bodies=())
    sub.features = FakeFeatures(sub_sf)
    sub.createOpenProfile = lambda coll, chain: FakeOpenProfile()
    # The profile is OWNED by the sub-component; the path sketch lives there too.
    prof_sketch = _sketch("Prof", profiles=[_owned_profile(sub)])
    sub.sketches = _NamedCollection([prof_sketch, _sketch("PathSketch", curves=3)])

    design = make_design(comp=root, all_components=[root, sub])
    install(sw, design)
    return root_sf, sub_sf, design


class TestCrossComponentHost:
    def test_sweep_is_built_on_the_profiles_owning_component(self):
        # The profile is owned by sub-component 'Frame' while ROOT is active. Handing another
        # component's profile to the ACTIVE component's features raises bSet live, so the feature
        # must be created on the OWNER's features - proven here by which features object got the call.
        root_sf, sub_sf, _ = _install_two_component()
        out = _payload(sw.handler(profile={"sketch": "Prof", "profile_index": 0},
                                  path="sketch:PathSketch"))
        assert out["swept"] is True
        assert sub_sf.last is not None       # the OWNER built the sweep
        assert root_sf.last is None          # NOT the active/root component (would be the bSet trap)


def _install_same_name_in_two(sketch_name="Prof"):
    """Two components BOTH holding a sketch of one name - what Fusion produces by default, since it
    numbers sketches per component from 1. Each carries its own features surface, so which component
    the scope selected is readable. Returns (alpha_features, beta_features, design)."""
    from conftest import MakeComp
    made = []
    for name in ("Alpha", "Beta"):
        sf = FakeSweepFeatures(body_names=("Body1",))
        comp = MakeComp(name=name, bodies=())
        comp.features = FakeFeatures(sf)
        comp.createOpenProfile = lambda coll, chain: FakeOpenProfile()
        comp.sketches = _NamedCollection([_sketch(sketch_name, profiles=[_owned_profile(comp)]),
                                          _sketch("PathSketch", curves=3)])
        made.append((sf, comp))
    (alpha_sf, alpha), (beta_sf, beta) = made
    design = make_design(comp=alpha, all_components=[alpha, beta])
    install(sw, design)
    return alpha_sf, beta_sf, design


class TestComponentScope:
    """SKETCH-6: the {sketch, profile_index} selector resolves a sketch BY NAME, so a name two
    components carry identifies nothing. The scope input is what makes the refusal performable - and
    it has to SELECT, not just reword the message."""

    def test_the_scope_selects_that_components_own_sketch(self):
        # asserted on WHICH component built the feature: both hold a 'Prof', so a scope that were
        # ignored (or first-matched) would build on Alpha whatever the caller asked for.
        alpha_sf, beta_sf, _ = _install_same_name_in_two()
        out = _payload(sw.handler(profile={"sketch": "Prof", "profile_index": 0},
                                  path="sketch:PathSketch", component="Beta"))
        assert out["swept"] is True
        assert beta_sf.last is not None and alpha_sf.last is None

    def test_the_other_spelling_selects_the_other_component(self):
        alpha_sf, beta_sf, _ = _install_same_name_in_two()
        _payload(sw.handler(profile={"sketch": "Prof", "profile_index": 0},
                            path="sketch:PathSketch", component="Alpha"))
        assert alpha_sf.last is not None and beta_sf.last is None

    def test_no_scope_refuses_the_shared_name_naming_this_tools_input(self):
        _install_same_name_in_two()
        res = sw.handler(profile={"sketch": "Prof", "profile_index": 0}, path="sketch:PathSketch")
        assert res["isError"] is True
        assert "2 sketches are named 'Prof'" in res["message"]
        assert "as 'component'" in res["message"] and "Rename" not in res["message"]

    def test_the_component_scope_is_declared_on_the_wire(self):
        # the schema is strict, so a remedy naming an input no property declares would be a call the
        # tool's own schema rejects - the input and the kind's scope ship together or not at all.
        sd = load_tool("_sketch_detail")
        assert sw.sweep_tool.input_schema["properties"]["component"] == sd.COMPONENT_SCOPE[1]

    def test_the_open_curve_fallback_is_scoped_too(self):
        # the open-profile fallback resolves through the SAME scoped walk as the closed path: an
        # active-component-only lookup there builds from a different component's same-named sketch.
        from conftest import MakeComp
        made = []
        for name in ("Alpha", "Beta"):
            sf = FakeSweepFeatures(body_names=("Body1",))
            comp = MakeComp(name=name, bodies=())
            comp.features = FakeFeatures(sf)
            comp.createOpenProfile = lambda coll, chain: FakeOpenProfile()
            comp.sketches = _NamedCollection([_sketch("Prof", profiles=[], curves=2, owner=comp),
                                              _sketch("PathSketch", curves=3, owner=comp)])
            made.append((sf, comp))
        (alpha_sf, alpha), (beta_sf, beta) = made
        install(sw, make_design(comp=alpha, all_components=[alpha, beta]))
        out = _payload(sw.handler(profile={"sketch": "Prof"}, path="sketch:PathSketch",
                                  component="Beta"))
        assert out["open_profile"] is True
        assert beta_sf.last is not None and alpha_sf.last is None


# ── declared outputs ────────────────────────────────────────────────────────

def test_declared_returns_present_in_payload():
    _install()
    out = _payload(sw.handler(profile={"sketch": "Prof"}, path="sketch:PathSketch"))
    for spec in sw.RETURNS:
        assert spec.assert_present(out) == "", spec.key


def test_the_path_description_states_the_tangent_continuity_rule():
    # measured: one seed chains by TANGENT CONTINUITY - a sharp corner stops it, open vs closed
    # decides nothing (a tangent-continuous closed loop chained all 8 edges from one seed). So the
    # wire may not promise chaining unconditionally, nor claim a closed loop refuses to chain; what
    # a seed actually reached is only knowable from the reported count.
    desc = sw.sweep_tool.to_dict()["inputSchema"]["properties"]["path"]["description"]
    assert "TANGENT connections only" in desc
    assert "'path' count is the truth" in desc
    assert "auto-chain" not in desc.lower()
    assert "closed loop" not in desc.lower() and "seed edge alone" not in desc


# ── path_curves: what the built path HOLDS, beside what the request named ───────────────────────
#
# Chaining follows tangent continuity, so a 'sketch:<name>' path can chain a single curve out of a
# three-curve sketch and sweep a stub of the intended run. Both counts ride on the payload so the
# shortfall is visible without measuring the body.

class TestPathCurves:
    def test_sketch_path_publishes_both_counts(self):
        sf, design = _install()
        design.rootComponent.features.path_returns = _built_path(3)
        out = _payload(sw.handler(profile={"sketch": "Prof"}, path="sketch:PathSketch"))
        assert out["path_curves"] == 3
        assert out["path_sketch_curves"] == 3

    def test_a_chain_that_stopped_short_warns_naming_both_counts(self):
        # The bail case: 1 of the sketch's 3 curves chained, and every other signal (a feature, a
        # body, is_solid) reports a clean sweep.
        sf, design = _install()
        design.rootComponent.features.path_returns = _built_path(1)
        out = _payload(sw.handler(profile={"sketch": "Prof"}, path="sketch:PathSketch"))
        assert out["path_curves"] == 1 and out["path_sketch_curves"] == 3
        assert "chained 1 of the sketch's 3 curves" in out["note"]
        assert "tangent continuity" in out["note"]

    def test_a_full_chain_does_not_warn(self):
        sf, design = _install()
        design.rootComponent.features.path_returns = _built_path(3)
        out = _payload(sw.handler(profile={"sketch": "Prof"}, path="sketch:PathSketch"))
        assert "WARNING" not in out["note"]

    def test_an_edge_path_publishes_the_count_with_no_sketch_to_compare(self):
        adsk.fusion.BRepEdge = BRepEdge
        sf, design = _install(tokens={"EDGE1": BRepEdge(curve=Line3D())})
        design.rootComponent.features.path_returns = _built_path(14)
        out = _payload(sw.handler(profile={"sketch": "Prof"}, path=["EDGE1"]))
        assert out["path_curves"] == 14
        # no source sketch exists for an edge path - a null here would read as an unreadable sketch
        assert "path_sketch_curves" not in out
        assert "WARNING" not in out["note"]

    def test_an_unreadable_path_count_is_published_as_unknown(self):
        # The Path would not answer .count: path_curves is None (unknown), and nothing claims a
        # shortfall it could not measure.
        sf, design = _install()
        design.rootComponent.features.path_returns = types.SimpleNamespace()
        out = _payload(sw.handler(profile={"sketch": "Prof"}, path="sketch:PathSketch"))
        assert out["path_curves"] is None
        assert "WARNING" not in out["note"]

    def test_a_longer_chain_than_the_sketch_carries_does_not_warn(self):
        # A path holding at least the sketch's curves is not a shortfall - only fewer is.
        sf, design = _install()
        design.rootComponent.features.path_returns = _built_path(4)
        out = _payload(sw.handler(profile={"sketch": "Prof"}, path="sketch:PathSketch"))
        assert "WARNING" not in out["note"]


class TestConstructionCurvesAreNotPathCurves:
    def _path_sketch(self, design):
        return design.rootComponent.sketches.itemByName("PathSketch")

    def test_a_construction_line_is_not_counted_as_a_path_curve(self):
        # sketchCurves counts CONSTRUCTION geometry too, and construction is not part of any path.
        # Counting it reports a shortfall on a path that chained everything there was to chain, and
        # blames tangency for it - a false warning with the wrong cause on a perfectly good sweep.
        sf, design = _install()
        self._path_sketch(design).sketchCurves._items.append(
            types.SimpleNamespace(isConstruction=True))
        design.rootComponent.features.path_returns = _built_path(3)
        out = _payload(sw.handler(profile={"sketch": "Prof"}, path="sketch:PathSketch"))
        assert out["path_sketch_curves"] == 3        # 4 curves, one of them construction
        assert "WARNING" not in out["note"]

    def test_a_real_shortfall_still_warns_when_construction_is_present(self):
        # The construction filter must not silence a genuine short chain.
        sf, design = _install()
        self._path_sketch(design).sketchCurves._items.append(
            types.SimpleNamespace(isConstruction=True))
        design.rootComponent.features.path_returns = _built_path(1)
        out = _payload(sw.handler(profile={"sketch": "Prof"}, path="sketch:PathSketch"))
        assert out["path_sketch_curves"] == 3
        assert "chained 1 of the sketch's 3 curves" in out["note"]

    def test_a_curve_whose_construction_flag_will_not_read_counts_as_real(self):
        # The conservative side: an unreadable flag can only shrink a warning, never invent one.
        sf, design = _install()
        out = _payload(sw.handler(profile={"sketch": "Prof"}, path="sketch:PathSketch"))
        assert out["path_sketch_curves"] == 3        # the plain fixture curves read no flag at all
