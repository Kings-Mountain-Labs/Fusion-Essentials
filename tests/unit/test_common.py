"""Unit tests for the shared tool helpers (tools/_common.py).

This module is the substrate every MCP tool imports — one response shape, one error contract, one
unit convention. If these drift, every tool drifts, so pin the contract explicitly.
"""

import json
import types
from types import SimpleNamespace

import pytest

import live_api_facts
from conftest import (BRepBody, MakeComp, _NamedCollection, body_proxy, entity_proxy, load_tool,
                      make_occurrence, make_source_document)

common = load_tool("_common")


class TestShortRef:
    """The head a wire message quotes a caller's own value back as."""

    def test_a_value_at_the_cap_is_echoed_whole_and_one_over_is_cut(self):
        at = "h" * common._ECHO_CHARS
        assert common.short_ref(at) == at
        assert common.short_ref(at + "h") == at + f"... ({common._ECHO_CHARS + 1} chars)"

    def test_a_geometry_handle_keeps_a_head_that_identifies_it_and_no_value_is_empty(self):
        handle = "TOKEN" + "z" * 205 + "|@face:1.5,2.5,3.5"
        out = common.short_ref(handle)
        assert out.startswith("TOKEN") and handle not in out
        assert f"({len(handle)} chars)" in out
        assert common.short_ref(None) == ""


class TestResponseBuilders:
    def test_ok_wraps_payload_as_json_text(self):
        res = common.ok({"a": 1, "b": "x"})
        assert res["isError"] is False
        assert json.loads(res["content"][0]["text"]) == {"a": 1, "b": "x"}

    def test_error_sets_flag_and_mirrors_message(self):
        res = common.error("boom")
        assert res["isError"] is True
        assert res["message"] == "boom"
        assert res["content"][0]["text"] == "boom"

    def test_underscore_aliases_are_gone(self):
        # the migration-era _ok/_error/_safe aliases were removed (single public spelling now).
        # Pin their ABSENCE so they can't silently creep back in.
        for legacy in ("_ok", "_error", "_safe", "_scale", "_target_component", "_UNIT_TO_CM"):
            assert not hasattr(common, legacy), f"_common should no longer export {legacy}"


class TestSafe:
    def test_returns_value(self):
        assert common.safe(lambda: 42) == 42

    def test_swallows_exception_returns_default(self):
        def boom():
            raise RuntimeError("x")
        assert common.safe(boom) is None
        assert common.safe(boom, "fallback") == "fallback"


class TestReadFlag:
    """A BOOLEAN read: True / False / None. The whole point is that an unreadable flag is NOT a
    False - `safe(read, False)` at a set-then-read-back site lets a swallowed write pass a
    `now != wanted` gate whenever the wanted value is False and publishes that as confirmed."""

    def test_true_and_false_pass_through(self):
        assert common.read_flag(lambda: True) is True
        assert common.read_flag(lambda: False) is False

    def test_a_raising_getter_is_none_not_false(self):
        def boom():
            raise RuntimeError("3 : not available on this build")
        assert common.read_flag(boom) is None

    def test_a_flag_that_reads_none_is_none(self):
        assert common.read_flag(lambda: None) is None

    def test_a_truthy_non_bool_is_normalised_to_a_bool(self):
        # A SWIG getter can answer with an int; the caller compares against a bool, so 1 must not
        # come back as 1 (which `is True` would then reject).
        assert common.read_flag(lambda: 1) is True
        assert common.read_flag(lambda: 0) is False

    def test_the_unreadable_sentinel_never_escapes(self):
        def boom():
            raise RuntimeError("x")
        assert common.read_flag(boom) is not common._UNREADABLE


class TestAllComponentsRootFallback:
    def test_an_unreadable_component_list_still_walks_the_root(self):
        # allComponents reading as an empty/unreadable collection must not shrink a design-wide walk
        # to nothing: the root component is always there to walk, and returning [] would make every
        # by-name lookup built on this miss silently.
        root = MakeComp(name="Root")

        class _Blind:
            rootComponent = root

            @property
            def allComponents(self):
                raise RuntimeError("3 : cannot enumerate")

        assert common.all_components(_Blind()) == [root]

    def test_an_all_none_component_list_still_walks_the_root(self):
        root = MakeComp(name="Root")
        coll = SimpleNamespace(count=2, item=lambda i: None)
        d = SimpleNamespace(rootComponent=root, allComponents=coll)
        assert common.all_components(d) == [root]


class TestScale:
    def test_known_units(self):
        assert common.scale("mm") == 0.1
        assert common.scale("cm") == 1.0
        assert common.scale("in") == 2.54

    def test_default_is_mm(self):
        assert common.scale("") == 0.1
        assert common.scale(None) == 0.1

    def test_unknown_unit_is_none(self):
        assert common.scale("furlong") is None

    def test_case_and_whitespace_insensitive(self):
        assert common.scale("  MM ") == 0.1


class TestTargetComponent:
    def test_returns_active_component_when_set(self):
        active = object()
        d = type("D", (), {"activeComponent": active, "rootComponent": object()})()
        assert common.target_component(d) is active

    def test_falls_back_to_root_when_no_active(self):
        root = object()
        # activeComponent access raises -> safe() returns None -> fall back to root
        class D:
            rootComponent = root
            @property
            def activeComponent(self):
                raise RuntimeError("none active")
        assert common.target_component(D()) is root


class TestCmToUnit:
    def test_is_the_inverse_of_unit_to_cm(self):
        for u, f in common.UNIT_TO_CM.items():
            assert common.CM_TO_UNIT[u] == 1.0 / f

    def test_mm_is_ten_per_cm(self):
        assert common.CM_TO_UNIT["mm"] == 10.0
        assert common.CM_TO_UNIT["cm"] == 1.0


class TestPtxyz:
    """A published POSITION is a measurement, and the tri-state is per POINT: every consumer
    navigates to / measures from the point as a whole, so one component that will not read makes
    the point unknown rather than a coordinate two thirds measured and one third invented."""

    class _Pt:
        def __init__(self, x, y, z):
            self.x, self.y, self.z = x, y, z

    class _BlindZ:
        """A Point3D whose z read RAISES - a proxy that stopped answering one component."""
        x = 1.0
        y = 2.0

        @property
        def z(self):
            raise RuntimeError("point component unavailable")

    def test_scales_and_rounds(self):
        p = self._Pt(1.0, 2.0, 3.0)
        assert common.ptxyz(p, 10.0) == {"x": 10.0, "y": 20.0, "z": 30.0}

    def test_none_point_is_none(self):
        assert common.ptxyz(None, 10.0) is None

    def test_one_unreadable_component_makes_the_whole_point_null(self):
        # safe(read, 0.0) here publishes {x:10, y:20, z:0} - a real-looking coordinate on the XY
        # plane that nothing measured.
        assert common.ptxyz(self._BlindZ(), 10.0) is None

    def test_a_genuine_zero_coordinate_is_still_an_answer(self):
        # the boundary the null must not swallow: the origin is a position, not a failed read.
        assert common.ptxyz(self._Pt(0.0, 0.0, 0.0), 10.0) == {"x": 0.0, "y": 0.0, "z": 0.0}

    def test_a_non_numeric_component_is_null_not_a_crash(self):
        # an unmodelled adsk property hands back a truthy child object; multiplying it by the unit
        # factor would raise inside the read (or worse, publish whatever it multiplies to).
        assert common.ptxyz(self._Pt(1.0, object(), 3.0), 10.0) is None

    def test_a_boolean_component_is_not_a_coordinate(self):
        # bool is an int subclass: True would otherwise scale to a 10.0 mm coordinate.
        assert common.ptxyz(self._Pt(1.0, 2.0, True), 10.0) is None

    def test_scaled_overflow_is_null_for_measure_and_point(self):
        assert common.measured(lambda: 1e308, scale=10.0) is None
        assert common.ptxyz(self._Pt(1e308, 0.0, 0.0), 10.0) is None

    def test_nonfinite_component_is_not_json_measurement(self):
        assert common.ptxyz(self._Pt(float("nan"), 0.0, float("inf")), 10.0) is None


class TestMeasured:
    def test_nonfinite_values_are_unknown_but_zero_is_measured(self):
        assert common.measured(lambda: float("nan")) is None
        assert common.measured(lambda: float("inf")) is None
        assert common.measured(lambda: 0.0) == 0.0


class TestResultBodies:
    def _feature(self, bodies):
        return type("F", (), {"bodies": _NamedCollection(bodies)})()

    def test_empty_feature_bodies(self):
        assert common.result_bodies(self._feature([])) == []

    def test_collects_bodies_in_order(self):
        b1, b2 = type("B", (), {})(), type("B", (), {})()
        assert common.result_bodies(self._feature([b1, b2])) == [b1, b2]

    def test_filters_none_bodies(self):
        b = type("B", (), {})()
        assert common.result_bodies(self._feature([b, None])) == [b]

    def test_none_feature_is_safe(self):
        assert common.result_bodies(None) == []

    def test_unreadable_bodies_is_safe(self):
        class Bad:
            @property
            def bodies(self):
                raise RuntimeError("gone")
        assert common.result_bodies(Bad()) == []


class TestLandedExtentCm:
    """The ONE read of the depth an extrude-family feature reports. Its TYPE GUARD decides what
    counts as readable for every consumer, so both halves are pinned. A BOOLEAN is not a depth:
    isinstance(True, int) is True, so a guard that only excludes non-numbers turns a .value
    answering True into exactly 1.0 cm - which MATCHES a 10 mm request and passes the comparison
    that exists to catch a wrong depth. A whole number is a depth, and answering it as a float
    keeps one definition of readable across every consumer."""

    def _feature(self, value):
        return SimpleNamespace(extentOne=SimpleNamespace(distance=SimpleNamespace(value=value)))

    def test_a_whole_number_depth_reads_as_a_float(self):
        got = common.landed_extent_cm(self._feature(3))
        assert got == 3.0 and isinstance(got, float)

    def test_a_boolean_is_not_a_depth(self):
        assert common.landed_extent_cm(self._feature(True)) is None

    def test_a_feature_carrying_no_extent_reads_nothing(self):
        assert common.landed_extent_cm(SimpleNamespace()) is None


class TestLandedExtent2Cm:
    """The second side's read, for a two-sided extent. It holds the SAME type guard as its first-side
    twin - a boolean is not a depth - and it must read extentTWO: a copy that reads extentOne would
    judge both sides of a two-sided extrude against the first side's number and pass a swap."""

    def _feature(self, one, two):
        return SimpleNamespace(extentOne=SimpleNamespace(distance=SimpleNamespace(value=one)),
                               extentTwo=SimpleNamespace(distance=SimpleNamespace(value=two)))

    def test_it_reads_the_second_side_not_the_first(self):
        assert common.landed_extent2_cm(self._feature(1.0, 0.5)) == 0.5

    def test_a_whole_number_depth_reads_as_a_float(self):
        got = common.landed_extent2_cm(self._feature(3, 2))
        assert got == 2.0 and isinstance(got, float)

    def test_a_boolean_is_not_a_depth(self):
        assert common.landed_extent2_cm(self._feature(1.0, True)) is None

    def test_a_one_sided_feature_reads_nothing_rather_than_raising(self):
        # extentTwo is absent on a one-sided extent - that is None, never a zero depth
        assert common.landed_extent2_cm(
            SimpleNamespace(extentOne=SimpleNamespace(distance=SimpleNamespace(value=1.0)))) is None

    def test_a_null_second_extent_reads_nothing(self):
        assert common.landed_extent2_cm(
            SimpleNamespace(extentTwo=None)) is None


class TestBodyFacts:
    """The per-body {name, is_solid} projection every feature result is published with. is_solid is
    a published FLAG, so it holds the read_flag contract: True / False / None, never a coerced
    False - a body whose flag will not read is not an open surface."""

    class _Body:
        def __init__(self, name, is_solid):
            self.name, self.isSolid = name, is_solid

    class _BlindBody:
        name = "Mystery"

        @property
        def isSolid(self):
            raise RuntimeError("3 : flag unavailable")

    def test_flags_pass_through(self):
        rows = common.body_facts([self._Body("Solid1", True), self._Body("Srf1", False)])
        assert rows == [{"name": "Solid1", "is_solid": True},
                        {"name": "Srf1", "is_solid": False}]

    def test_an_unreadable_flag_is_null_not_false(self):
        rows = common.body_facts([self._BlindBody()])
        assert rows[0]["is_solid"] is None and rows[0]["name"] == "Mystery"

    def test_no_bodies_is_an_empty_list(self):
        assert common.body_facts([]) == []


class TestTargetSketch:
    def test_named_sketch_found(self):
        sk = type("Sk", (), {"name": "S1"})()
        comp = type("C", (), {"sketches": _NamedCollection([sk])})()
        sketch, requested = common.target_sketch(comp, "S1")
        assert sketch is sk and requested == "S1"

    def test_named_sketch_not_found(self):
        comp = type("C", (), {"sketches": _NamedCollection([])})()
        sketch, requested = common.target_sketch(comp, "Nope")
        assert sketch is None and requested == "Nope"

    def test_no_name_returns_most_recent(self):
        sk0 = type("Sk", (), {"name": "S0"})()
        sk1 = type("Sk", (), {"name": "S1"})()
        comp = type("C", (), {"sketches": _NamedCollection([sk0, sk1])})()
        sketch, requested = common.target_sketch(comp, "")
        assert sketch is sk1 and requested == ""

    def test_no_name_no_sketches_is_none(self):
        comp = type("C", (), {"sketches": _NamedCollection([])})()
        sketch, requested = common.target_sketch(comp, "")
        assert sketch is None and requested == ""


_comp_serial = iter(range(1, 10_000))


def _comp_with_sketches(name, sketch_names=()):
    # Like the live Component: NO allComponents attribute (that collection is a Design property),
    # so reading it here raises AttributeError exactly as adsk does - and a DISTINCT entityToken,
    # which every live component has. Two components CAN wear one name (two inserted references each
    # bring their own 'Frame'), and the token is the only thing that tells them apart: a fake without
    # one makes same_component fall back to the name and report those two as ONE component.
    sks = [type("Sk", (), {"name": n})() for n in sketch_names]
    return type("C", (), {"name": name, "sketches": _NamedCollection(sks),
                          "entityToken": f"comp-{name}-{next(_comp_serial)}"})()


def _design_with(root, subs, active=None, occurrences=()):
    # allComponents lives on the DESIGN and is a counted collection (count/item), root included.
    # occurrences hang off root.allOccurrences - the fast path the design-wide occurrence census
    # reads. Attached only when the caller asks for placements, so a design that names none keeps
    # the unreadable-census shape every other test here was written against.
    if occurrences:
        root.allOccurrences = list(occurrences)
    return type("D", (), {"rootComponent": root, "activeComponent": active or root,
                          "allComponents": _NamedCollection([root] + list(subs))})()


def _placement(path, comp):
    """One occurrence: the census reads its component and its assembly path off the SAME object."""
    return SimpleNamespace(name=path.split("+")[-1], fullPathName=path, component=comp)


# The entityToken two components out of two inserted references are measured to SHARE, byte for
# byte. The measurement itself is recorded once, in _common.component_placements; this is that
# value, and it is the ONLY such literal here - a second spelling would be a second string claiming
# to be one measurement.
_XREF_TOKEN = "/v4BAAEAegEAAAAAAAAAAAAA"


class TestAllComponents:
    def test_reads_the_collection_off_the_design_not_the_root(self):
        # Component has no allComponents in the live API - reading it off the root raises, safe()
        # swallows, and the walk silently degrades to [root], hiding every sub-component from
        # "design-wide" reads (observed live: a 3-component doc summarized as 0 sketches).
        root = _comp_with_sketches("Root")
        sub = _comp_with_sketches("Frame")
        assert common.all_components(_design_with(root, [sub])) == [root, sub]

    def test_falls_back_to_root_when_the_design_lacks_the_collection(self):
        root = _comp_with_sketches("Root")
        d = type("D", (), {"rootComponent": root, "activeComponent": root})()
        assert common.all_components(d) == [root]

    def test_no_root_is_empty(self):
        class D:
            @property
            def rootComponent(self):
                raise RuntimeError("no design")
        assert common.all_components(D()) == []


class TestResolveSketchDesignWide:
    def test_finds_a_sub_component_sketch_while_root_is_active(self):
        root = _comp_with_sketches("Root")
        sub = _comp_with_sketches("Frame", ["FrameSketch"])
        sk = common.resolve_sketch(_design_with(root, [sub]), "FrameSketch")
        assert sk is not None and sk.name == "FrameSketch"

    def test_a_name_two_components_share_resolves_to_nothing(self):
        # Two components each hold a "Profile" sketch. Neither is identified by that name, so the
        # resolver hands back nothing rather than an arbitrary one to draw on or delete from.
        root = _comp_with_sketches("Root", ["Profile"])
        sub = _comp_with_sketches("Frame", ["Profile"])
        assert common.resolve_sketch(_design_with(root, [sub], active=sub), "Profile") is None

    def test_a_unique_name_still_resolves_beside_a_shared_one(self):
        # The other side of the boundary: one hit resolves even while another name is shared.
        root = _comp_with_sketches("Root", ["Profile", "Base"])
        sub = _comp_with_sketches("Frame", ["Profile"])
        d = _design_with(root, [sub], active=sub)
        assert common.resolve_sketch(d, "Base") is root.sketches.item(1)

    def test_a_sketch_in_the_ACTIVE_sub_component_is_reachable(self):
        # The reach that matters: a sketch drawn in an activated sub-component resolves by name.
        # (There is no active-FIRST ordering any more - nothing for an order to decide, since a name
        # several components carry refuses rather than resolving to whichever came first.)
        root = _comp_with_sketches("Root", ["RootOnly"])
        sub = _comp_with_sketches("Frame", ["FrameOnly"])
        d = _design_with(root, [sub], active=sub)
        hits = common.find_sketches_by_name(d, "FrameOnly")
        assert [c.name for _sk, c in hits] == ["Frame"]
        assert common.resolve_sketch(d, "FrameOnly") is sub.sketches.item(0)

    def test_one_sketch_reached_through_the_active_and_root_wrappers_is_not_shared(self):
        # The active component IS the root here, so the same sketch is reached three times (active,
        # root, allComponents). De-duplicated by the sketch itself, that stays ONE hit - otherwise
        # every root sketch would refuse as a name several sketches carry.
        root = _comp_with_sketches("Root", ["Profile"])
        d = _design_with(root, [], active=root)
        assert common.resolve_sketch(d, "Profile") is root.sketches.item(0)


class TestFindSketch:
    def test_a_shared_name_is_refused_naming_each_owning_component(self):
        root = _comp_with_sketches("Root", ["Profile"])
        sub = _comp_with_sketches("Frame", ["Profile"])
        sk, err = common.find_sketch(_design_with(root, [sub]), "Profile")
        assert sk is None
        assert "2 sketches" in err and "Root" in err and "Frame" in err and "Profile" in err

    def test_a_unique_name_resolves_with_no_error(self):
        root = _comp_with_sketches("Root", ["Base"])
        sub = _comp_with_sketches("Frame", ["FrameSketch"])
        d = _design_with(root, [sub])
        assert common.find_sketch(d, "Base") == (root.sketches.item(0), None)

    # The fallback sentence is written out LITERALLY in both tests below, never compared against
    # _RENAME_REMEDY: an assertion built from the same constant that produced the string cannot
    # fail when that constant is rewritten, so it pins the wiring and not the wording.
    _RENAME_SENTENCE = ("Rename one so the name resolves to a single sketch, then retry "
                        "(sketch_get lists the sketches).")

    def test_without_a_remedy_the_refusal_falls_back_to_the_rename_sentence(self):
        # The only way forward a caller with NO scope input of its own can offer. It is the weakest
        # remedy, which is why a caller that HAS one replaces it.
        root = _comp_with_sketches("Root", ["Profile"])
        sub = _comp_with_sketches("Frame", ["Profile"])
        _sk, err = common.find_sketch(_design_with(root, [sub]), "Profile")
        assert err.endswith(self._RENAME_SENTENCE)

    def test_find_or_recent_sketch_ALSO_defaults_to_the_rename_sentence(self):
        # The sibling helper's own default, which six production callers consume - doc_insert_import,
        # model_extrude, model_revolve, the two surface creators and the _inputs sketch kind. It is
        # reachable only by calling find_or_recent_sketch itself: every test above passes a remedy
        # explicitly, so a non-None default here would ship a remedy none of those six asked for.
        root = _comp_with_sketches("Root", ["Profile"])
        sub = _comp_with_sketches("Frame", ["Profile"])
        _sk, requested, err = common.find_or_recent_sketch(_design_with(root, [sub]), "Profile")
        assert requested == "Profile"
        assert err.endswith(self._RENAME_SENTENCE)

    def test_a_remedy_REPLACES_the_rename_sentence_and_keeps_the_census(self):
        # The census and the owner naming stay here - only the sentence naming what to pass back
        # differs, because only the caller knows what it accepts.
        root = _comp_with_sketches("Root", ["Profile"])
        sub = _comp_with_sketches("Frame", ["Profile"])
        _sk, err = common.find_sketch(_design_with(root, [sub]), "Profile",
                                      remedy="Pass 'component'.")
        assert err.endswith("Pass 'component'.")
        assert "Rename one" not in err
        assert "2 sketches are named 'Profile'" in err and "Root" in err and "Frame" in err

    def test_a_remedy_reaches_the_refusal_through_find_or_recent_sketch(self):
        # The name-or-most-recent form hands it straight through; a caller passing a remedy that
        # never arrives would ship the rename sentence it meant to replace.
        root = _comp_with_sketches("Root", ["Profile"])
        sub = _comp_with_sketches("Frame", ["Profile"])
        _sk, requested, err = common.find_or_recent_sketch(_design_with(root, [sub]), "Profile",
                                                           remedy="Pass 'component'.")
        assert requested == "Profile"
        assert err.endswith("Pass 'component'.") and "Rename one" not in err

    def test_a_remedy_changes_nothing_when_the_name_resolves(self):
        root = _comp_with_sketches("Root", ["Base"])
        d = _design_with(root, [])
        assert common.find_sketch(d, "Base", remedy="Pass 'component'.") == (
            root.sketches.item(0), None)
        assert common.find_sketch(d, "Ghost", remedy="Pass 'component'.") == (None, None)

    def test_a_missing_name_carries_no_error_text(self):
        # A miss is (None, None) - each caller words its own not-found error off all_sketch_names.
        root = _comp_with_sketches("Root", ["Base"])
        assert common.find_sketch(_design_with(root, []), "Ghost") == (None, None)

    def test_a_blank_name_matches_nothing(self):
        root = _comp_with_sketches("Root", ["Base"])
        assert common.find_sketches_by_name(_design_with(root, []), "  ") == []

    def test_colliding_token_components_are_BOTH_kept_so_the_name_refuses(self):
        # MEASURED LIVE, the defect this pins: the walk de-duplicated components by entityToken, and
        # two 'Frame' components out of two inserted references read the SAME token - so one was
        # dropped, the walk returned a single hit, and an unscoped sketch_get(sketch_name=
        # "Frame_Ring") RESOLVED with isError false, silently returning one of the two. The refusal
        # this row exists to defend has to hold on the DEFAULT path, not only the scoped one.
        a = _xref_twin("Frame", ["Frame_Ring"])
        b = _xref_twin("Frame", ["Frame_Ring"])
        d = _design_with(_comp_with_sketches("Root"), [a, b])
        hits = common.find_sketches_by_name(d, "Frame_Ring")
        assert len(hits) == 2                       # neither component may be merged away
        assert [c.name for _sk, c in hits] == ["Frame", "Frame"]
        assert {id(sk) for sk, _c in hits} == {id(a.sketches.item(0)), id(b.sketches.item(0))}

    def test_that_collision_makes_the_unscoped_resolver_REFUSE(self):
        a = _xref_twin("Frame", ["Frame_Ring"])
        b = _xref_twin("Frame", ["Frame_Ring"])
        d = _design_with(_comp_with_sketches("Root"), [a, b])
        sk, err = common.find_sketch(d, "Frame_Ring")
        assert sk is None
        assert "2 sketches are named 'Frame_Ring'" in err and err.count("Frame") >= 2
        assert common.resolve_sketch(d, "Frame_Ring") is None

    def test_owners_sharing_a_name_are_listed_by_their_occurrence_paths(self):
        # The degenerate listing this replaces: "'Frame_Ring' in Frame, 'Frame_Ring' in Frame" is
        # the count restated, and a caller reading it has no second call to make. Both owners are
        # named 'Frame', so the refusal names OCCURRENCES that place a component of that name
        # holding the sketch - two DISTINCT addresses, each a spelling a component scope resolves.
        a = _xref_twin("Frame", ["Frame_Ring"], _XREF_TOKEN)
        b = _xref_twin("Frame", ["Frame_Ring"], _XREF_TOKEN)
        d = _design_with(_comp_with_sketches("Root"), [a, b],
                         occurrences=[_placement("Frame:1", a), _placement("Frame:2", b)])
        _sk, err = common.find_sketch(d, "Frame_Ring")
        assert "2 sketches are named 'Frame_Ring'" in err
        assert "2 occurrences place one holding this sketch: Frame:1, Frame:2." in err
        assert "in Frame," not in err and "in Frame)" not in err   # no bare owner name survives

    def test_placements_are_counted_apart_from_the_hits_they_outnumber(self):
        # THE BITE: two components named 'Frame', each holding 'Ring' and each placed TWICE - the
        # ordinary assembly case. Four addresses under a stated count of 2 read as four sketches if
        # they sit inside the hits' own parenthetical, so the placements are counted in a sentence
        # of their own and the parenthetical never grows past the hit count.
        a = _xref_twin("Frame", ["Ring"], _XREF_TOKEN)
        b = _xref_twin("Frame", ["Ring"], _XREF_TOKEN)
        d = _design_with(_comp_with_sketches("Root"), [a, b],
                         occurrences=[_placement("Gimbal:1+Frame:1", a),
                                      _placement("Gimbal:1+Frame:2", b),
                                      _placement("Gimbal:2+Frame:1", a),
                                      _placement("Gimbal:2+Frame:2", b)])
        _sk, err = common.find_sketch(d, "Ring")
        assert "2 sketches are named 'Ring' - " in err          # no parenthetical at all
        assert "4 occurrences place one holding this sketch: " in err
        for path in ("Gimbal:1+Frame:1", "Gimbal:1+Frame:2",
                     "Gimbal:2+Frame:1", "Gimbal:2+Frame:2"):
            assert path in err
        assert err.count("'Ring' in ") == 0     # nothing reads as an enumeration of the 2 hits

    def test_a_placement_of_that_name_holding_no_such_sketch_is_not_listed_as_an_owner(self):
        # The sketch half of the read is what keeps a row true rather than merely plausible: a THIRD
        # 'Frame' is placed but holds nothing of this name, so naming it would offer an owner that
        # owns nothing - and a caller scoping to it gets a miss, not the sketch.
        a = _xref_twin("Frame", ["Frame_Ring"], _XREF_TOKEN)
        b = _xref_twin("Frame", ["Frame_Ring"], _XREF_TOKEN)
        bare = _xref_twin("Frame", ["Something_Else"], _XREF_TOKEN)
        d = _design_with(_comp_with_sketches("Root"), [a, b, bare],
                         occurrences=[_placement("Frame:1", a), _placement("Frame:2", b),
                                      _placement("Frame:3", bare)])
        _sk, err = common.find_sketch(d, "Frame_Ring")
        assert "2 occurrences place one holding this sketch: Frame:1, Frame:2." in err
        assert "Frame:3" not in err

    def test_a_distinctly_named_owner_keeps_its_plain_name_beside_a_shared_one(self):
        # The other side of the substitution: placements where the name says nothing, the NAME where
        # it already identifies one owner - a path there would hand back a longer spelling for free.
        a = _xref_twin("Frame", ["Ring"], _XREF_TOKEN)
        b = _xref_twin("Frame", ["Ring"], _XREF_TOKEN)
        base = _comp_with_sketches("Base", ["Ring"])
        d = _design_with(_comp_with_sketches("Root"), [a, b, base],
                         occurrences=[_placement("Frame:1", a), _placement("Frame:2", b),
                                      _placement("Base:1", base)])
        _sk, err = common.find_sketch(d, "Ring")
        assert "3 sketches are named 'Ring' ('Ring' in Base) - " in err
        assert "2 occurrences place one holding this sketch: Frame:1, Frame:2." in err
        assert "Base:1" not in err

    def test_one_readable_placement_among_shared_owners_is_still_offered(self):
        # The boundary a single row would swallow: only ONE of the two 'Frame's is placed, so the
        # substitution has one address to make. Rendering that lone row as its NAME again would drop
        # the only spelling that resolves - the uncovered hit stays a bare name row beside it.
        a = _xref_twin("Frame", ["Frame_Ring"], _XREF_TOKEN)
        b = _xref_twin("Frame", ["Frame_Ring"], _XREF_TOKEN)
        d = _design_with(_comp_with_sketches("Root"), [a, b],
                         occurrences=[_placement("Frame:1", a)])
        _sk, err = common.find_sketch(d, "Frame_Ring")
        assert "2 sketches are named 'Frame_Ring' ('Frame_Ring' in Frame) - " in err
        assert "1 occurrence places one holding this sketch: Frame:1." in err

    def test_shared_owners_with_no_placement_keep_the_name_and_claim_no_address(self):
        # Nothing places them, so there is no address to offer - the listing repeats the name and
        # the refusal must NOT claim it named occurrences it never read.
        a = _xref_twin("Frame", ["Frame_Ring"], _XREF_TOKEN)
        b = _xref_twin("Frame", ["Frame_Ring"], _XREF_TOKEN)
        d = _design_with(_comp_with_sketches("Root"), [a, b],
                         occurrences=[_placement("Other:1", _comp_with_sketches("Other"))])
        _sk, err = common.find_sketch(d, "Frame_Ring")
        assert err.count("'Frame_Ring' in Frame") == 2
        assert "occurrence" not in err
        assert "Other:1" not in err

    def test_an_unreadable_allComponents_still_reaches_the_ACTIVE_component(self):
        # all_components degrades to [root] when design.allComponents will not read. The fallback
        # asks the active component directly, so a sketch in an activated sub-component stays
        # reachable even then - the normal multi-part workflow must not depend on that collection.
        root = _comp_with_sketches("Root")
        sub = _comp_with_sketches("Frame", ["FrameOnly"])
        blind = type("D", (), {"rootComponent": root, "activeComponent": sub})()   # no allComponents
        hits = common.find_sketches_by_name(blind, "FrameOnly")
        assert [c.name for _sk, c in hits] == ["Frame"]

    def test_the_fallback_does_not_double_count_when_the_active_component_IS_the_root(self):
        # The fallback asks the active component AND the root, and with nothing activated those are
        # ONE component - counting it twice would report a single sketch as a collision and refuse
        # it. Reaching this needs allComponents to READ but not to carry the holder, so the main
        # walk comes back empty and the fallback is what answers.
        root = _comp_with_sketches("Root", ["Base"])
        other = _comp_with_sketches("Elsewhere", ["Unrelated"])
        d = type("D", (), {"rootComponent": root, "activeComponent": root,
                           "allComponents": _NamedCollection([other])})()
        hits = common.find_sketches_by_name(d, "Base")
        assert len(hits) == 1 and hits[0][0] is root.sketches.item(0)

    def test_the_fallback_never_runs_when_the_main_walk_found_something(self):
        # It fires only on an EMPTY result, so it can never append a second copy of a component
        # all_components already asked - which is what would turn one sketch into a false collision.
        root = _comp_with_sketches("Root", ["Base"])
        d = _design_with(root, [], active=root)
        assert len(common.find_sketches_by_name(d, "Base")) == 1

    def test_one_component_is_asked_once_so_its_sketch_is_not_a_false_collision(self):
        # The other side of the no-de-dup rule: all_components lists each component ONCE, so the
        # walk asks a given component once and a single component's sketch can never read as
        # several - which is what makes de-duplicating the hits unnecessary in the first place.
        root = _comp_with_sketches("Root", ["Profile"])
        d = _design_with(root, [], active=root)
        assert len(common.find_sketches_by_name(d, "Profile")) == 1
        assert common.resolve_sketch(d, "Profile") is root.sketches.item(0)


class TestComponentsInScope:
    """The ONE component-scope match: exact, case-insensitive, and refusing a name the design does
    not carry with the names it does."""

    def test_a_blank_name_scopes_to_every_component(self):
        root = _comp_with_sketches("Root")
        sub = _comp_with_sketches("Frame")
        comps, err = common.components_in_scope(_design_with(root, [sub]), "  ")
        assert err is None and comps == [root, sub]

    def test_a_name_selects_only_that_component(self):
        root = _comp_with_sketches("Root")
        sub = _comp_with_sketches("Frame")
        assert common.components_in_scope(_design_with(root, [sub]), "Frame") == ([sub], None)

    def test_the_match_is_case_insensitive_and_padding_tolerant(self):
        root = _comp_with_sketches("Root")
        sub = _comp_with_sketches("Frame")
        assert common.components_in_scope(_design_with(root, [sub]), "  frame ") == ([sub], None)

    def test_the_match_is_exact_not_a_prefix(self):
        # 'Frame' must not select 'Frame Bracket' - a prefix scope silently reads the wrong part.
        root = _comp_with_sketches("Root")
        sub = _comp_with_sketches("Frame Bracket")
        comps, err = common.components_in_scope(_design_with(root, [sub]), "Frame")
        assert comps is None and "No component named 'Frame'" in err

    def test_an_unknown_name_is_refused_with_the_component_names_that_exist(self):
        root = _comp_with_sketches("Root")
        sub = _comp_with_sketches("Frame")
        comps, err = common.components_in_scope(_design_with(root, [sub]), "Ghost")
        assert comps is None
        assert "No component named 'Ghost'" in err and "Root, Frame" in err

    def test_a_name_two_components_share_selects_both(self):
        # Selecting both is what lets the caller REFUSE the scope instead of picking one.
        root = _comp_with_sketches("Twin")
        sub = _comp_with_sketches("Twin")
        comps, err = common.components_in_scope(_design_with(root, [sub]), "Twin")
        assert err is None and comps == [root, sub]


class TestComponentScopeNarrowing:
    """Case-insensitive matching WIDENS the hit list, and a widened list must not manufacture an
    ambiguity that the design does not have."""

    def _two_spellings(self):
        root = _comp_with_sketches("Root")
        beta = _comp_with_sketches("Beta", ["S"])
        shouty = _comp_with_sketches("BETA", ["S"])
        return beta, shouty, _design_with(root, [beta, shouty])

    def test_the_exact_spelling_asked_for_wins_over_a_case_variant(self):
        # Without the narrowing this REFUSES, and 'Beta' - a name exactly one component carries -
        # becomes unaddressable purely because a case variant exists beside it.
        beta, _shouty, d = self._two_spellings()
        assert common.components_in_scope(d, "Beta") == ([beta], None)

    def test_the_other_spelling_resolves_by_its_own_spelling(self):
        # both directions, or a narrowing that always picked the first hit would still pass above
        _beta, shouty, d = self._two_spellings()
        assert common.components_in_scope(d, "BETA") == ([shouty], None)

    def test_a_query_matching_NEITHER_spelling_stays_ambiguous(self):
        # 'beta' names no component, so it identifies nothing and both hits are handed back
        beta, shouty, d = self._two_spellings()
        assert common.components_in_scope(d, "beta") == ([beta, shouty], None)

    def test_two_hits_spelled_exactly_as_asked_stay_ambiguous(self):
        # The narrowing must not become a first-match: two components literally named 'Frame' are
        # not told apart by the spelling, so both survive.
        root = _comp_with_sketches("Root")
        a = _comp_with_sketches("Frame")
        b = _comp_with_sketches("Frame")
        assert common.components_in_scope(_design_with(root, [a, b]), "Frame") == ([a, b], None)

    def test_three_components_across_two_spellings_stay_ambiguous(self):
        # THE boundary: two hits carry the asked-for spelling and a third is a case variant. Only
        # `len(cased) == 1` narrows - `>= 1` would silently drop the variant and answer with two.
        root = _comp_with_sketches("Root")
        a = _comp_with_sketches("Beta")
        b = _comp_with_sketches("Beta")
        c = _comp_with_sketches("BETA")
        comps, err = common.components_in_scope(_design_with(root, [a, b, c]), "Beta")
        assert err is None and comps == [a, b, c]

    def test_a_padded_component_name_is_matched_but_not_narrowed_to(self):
        # _component_is_named strips both sides, so ' Frame ' matches - but its raw spelling is not
        # what was asked for, so the unpadded one is the exact hit.
        root = _comp_with_sketches("Root")
        padded = _comp_with_sketches(" Frame ")
        plain = _comp_with_sketches("Frame")
        assert common.components_in_scope(_design_with(root, [padded, plain]), "Frame") == \
            ([plain], None)


class TestSpelledAsRead:
    """The honest half of a scope refusal: how the hits are ACTUALLY spelled. "N components are
    named '<query>'" is a claim the case-insensitive match never checked."""

    def test_omitted_when_every_hit_is_spelled_exactly_as_asked(self):
        # echoing the query back adds nothing
        comps = [_comp_with_sketches("Frame"), _comp_with_sketches("Frame")]
        assert common.spelled_as_read(comps, "Frame") == ""

    def test_names_the_distinct_spellings_when_they_differ_from_the_query(self):
        comps = [_comp_with_sketches("Beta"), _comp_with_sketches("BETA")]
        assert common.spelled_as_read(comps, "beta") == " (named 'Beta', 'BETA')"

    def test_repeated_spellings_are_listed_once(self):
        # nine 'Pin' beside one 'PIN' is TWO spellings, not ten
        comps = [_comp_with_sketches("Pin") for _ in range(9)] + [_comp_with_sketches("PIN")]
        assert common.spelled_as_read(comps, "pin") == " (named 'Pin', 'PIN')"

    def test_a_query_matching_one_of_several_spellings_still_names_them_all(self):
        # 'Beta' IS one of the spellings, but the clause must still show the variant it does not name
        comps = [_comp_with_sketches("Beta"), _comp_with_sketches("BETA")]
        assert common.spelled_as_read(comps, "Beta") == " (named 'Beta', 'BETA')"

    def test_the_spelling_list_is_capped_like_every_other_wire_list(self):
        comps = [_comp_with_sketches(f"N{i}") for i in range(12)]
        assert "(+4 more not listed)" in common.spelled_as_read(comps, "q")

    def test_components_whose_names_do_not_read_contribute_nothing(self):
        blind = type("C", (), {"name": property(lambda self: (_ for _ in ()).throw(
            RuntimeError("no name")))})()
        assert common.spelled_as_read([blind], "q") == ""


class TestNamedWithRemainder:
    """The ONE capped wire list. A silently truncated list reads as the complete set, and a caller
    picking its next call out of it never learns the entry it wanted was cut."""

    def test_a_short_list_is_joined_whole(self):
        assert common.named_with_remainder(["a", "b"]) == "a, b"

    def test_exactly_the_cap_is_still_whole(self):
        # the boundary: 8 of 8 named, nothing summarized
        items = [str(i) for i in range(8)]
        out = common.named_with_remainder(items)
        assert out == ", ".join(items) and "more not listed" not in out

    def test_one_over_the_cap_discloses_the_remainder(self):
        items = [str(i) for i in range(9)]
        out = common.named_with_remainder(items)
        assert out.startswith(", ".join(items[:8]))
        assert "(+1 more not listed)" in out and "8" not in out.split("...")[1]

    def test_the_remainder_counts_every_dropped_entry(self):
        assert "(+12 more not listed)" in common.named_with_remainder([str(i) for i in range(20)])

    def test_an_empty_list_is_the_empty_string(self):
        # the callers append their own "(none)"/"(no sketches)" wording onto a falsy result
        assert common.named_with_remainder([]) == ""


class TestToldApart:
    """The ONE substitution a listing makes when a name repeats. A listing that prints one name
    twice has restated its own count and said nothing else."""

    def test_a_unique_name_keeps_its_name_even_with_a_discriminator(self):
        # the plain name is what a caller passes back; a path there is a longer spelling for free
        assert common.told_apart([("Rough", "S1 / Rough"), ("Finish", "S2 / Finish")]) == \
            ["Rough", "Finish"]

    def test_a_repeated_name_renders_as_each_row_discriminator(self):
        assert common.told_apart([("Rough", "S1 / Rough"), ("Rough", "S2 / Rough")]) == \
            ["S1 / Rough", "S2 / Rough"]

    def test_exactly_two_is_the_boundary_one_is_not(self):
        # the substitution turns on at 2 rows of a name, not at 1: a lone row is already an address
        assert common.told_apart([("Rough", "S1 / Rough")]) == ["Rough"]
        assert common.told_apart([("Rough", "S1 / Rough"), ("Rough", "S2 / Rough")])[0] \
            == "S1 / Rough"

    def test_a_repeated_name_with_no_discriminator_keeps_the_name(self):
        # a listing renders what was READ; a blank row addresses nothing at all
        assert common.told_apart([("Rough", None), ("Rough", "")]) == ["Rough", "Rough"]

    def test_one_missing_discriminator_does_not_cost_its_namesake_its_own(self):
        assert common.told_apart([("Rough", None), ("Rough", "S2 / Rough")]) == \
            ["Rough", "S2 / Rough"]

    def test_the_rows_come_back_one_for_one_and_in_order(self):
        # length-preserving on purpose: a payload list is one row per THING, so collapsing two
        # identical renders would drop an operation from a warnings list
        rows = [("a", "x"), ("b", None), ("a", "y"), ("a", "x")]
        assert common.told_apart(rows) == ["x", "b", "y", "x"]

    def test_an_empty_row_list_is_an_empty_render(self):
        assert common.told_apart([]) == []

    def test_names_are_matched_exactly_not_case_folded(self):
        # two spellings ARE two names on a listing - substituting them would hide the very
        # difference that tells them apart
        assert common.told_apart([("Pin", "S1 / Pin"), ("PIN", "S2 / PIN")]) == ["Pin", "PIN"]


class TestPlacementsHoldingSketch:
    """The address that separates two components wearing one name: an occurrence whose OWN
    component answers to the name AND hands back the sketch - both read off one occurrence."""

    def test_only_placements_whose_component_holds_the_sketch_are_named(self):
        a = _comp_with_sketches("Frame", ["Ring"])
        bare = _comp_with_sketches("Frame", ["Other"])
        d = _design_with(_comp_with_sketches("Root"), [a, bare],
                         occurrences=[_placement("Frame:1", a), _placement("Frame:2", bare)])
        assert common.placements_holding_sketch(d, "Frame", "Ring") == ["Frame:1"]

    def test_a_component_of_another_name_is_never_named(self):
        a = _comp_with_sketches("Frame", ["Ring"])
        other = _comp_with_sketches("Base", ["Ring"])
        d = _design_with(_comp_with_sketches("Root"), [a, other],
                         occurrences=[_placement("Frame:1", a), _placement("Base:1", other)])
        assert common.placements_holding_sketch(d, "Frame", "Ring") == ["Frame:1"]

    def test_a_blank_sketch_name_names_nothing_even_when_the_lookup_ANSWERS(self):
        # The guard cannot rest on the collection declining a blank name: what live Fusion's
        # sketches.itemByName('') returns is unverified, so the component here HAS a sketch whose
        # name reads blank and the lookup hands it straight back. Without the guard the blank query
        # matches it and the refusal offers 'Frame:1' as the placement holding a sketch nobody named.
        a = _comp_with_sketches("Frame", ["Ring", ""])
        assert a.sketches.itemByName("") is not None       # the lookup does answer here
        d = _design_with(_comp_with_sketches("Root"), [a],
                         occurrences=[_placement("Frame:1", a)])
        assert common.placements_holding_sketch(d, "Frame", "  ") == []
        assert common.placements_holding_sketch(d, "Frame", "") == []

    def test_a_design_with_no_placements_names_nothing(self):
        a = _comp_with_sketches("Frame", ["Ring"])
        assert common.placements_holding_sketch(_design_with(_comp_with_sketches("Root"), [a]),
                                                "Frame", "Ring") == []


def _xref_twin(name, sketch_names, token=_XREF_TOKEN):
    """A component out of an INSERTED reference: its own sketches, and an entityToken it SHARES with
    the same-named component from the other reference (_XREF_TOKEN).

    entityToken is document-LOCAL, so it is not an identity across references. The other fakes here
    give every component a distinct token (which is what a single-document design looks like); this
    one models the collision, and it is the only shape under which a token-keyed grouping
    misbehaves. What the collision IS, and where it was read, is recorded in
    _common.component_placements - this fixture models it and claims no measurement of its own."""
    sks = [type("Sk", (), {"name": n})() for n in sketch_names]
    return type("C", (), {"name": name, "sketches": _NamedCollection(sks), "entityToken": token})()


def _design_with_occs(root, subs, occs):
    d = _design_with(root, subs)
    d.rootComponent = type("R", (), {"name": root.name, "sketches": root.sketches,
                                     "entityToken": root.entityToken,
                                     "allOccurrences": list(occs)})()
    return d


class TestComponentPlacements:
    """The path comes from the WALK: both halves of a placement are read off the SAME occurrence, so
    no comparison of two components is ever made - which is the only way to be right while two
    distinct components can report one entityToken."""

    def test_pairs_each_occurrence_path_with_its_own_components_name(self):
        a = _comp_with_sketches("Frame", ["Frame_Ring"])
        b = _comp_with_sketches("Frame", ["Frame_Ring"])
        root = _comp_with_sketches("Root")
        d = _design_with_occs(root, [a, b],
                              [make_occurrence("GimbalA:1+Frame:1", a),
                               make_occurrence("GimbalB:1+Frame:1", b)])
        assert [(p, c.name) for p, c in common.component_placements(d)] == [
            ("GimbalA:1+Frame:1", "Frame"), ("GimbalB:1+Frame:1", "Frame")]

    def test_each_occurrence_contributes_exactly_one_placement(self):
        a = _comp_with_sketches("Bolt")
        root = _comp_with_sketches("Root")
        d = _design_with_occs(root, [a],
                              [make_occurrence("Bolt:1", a), make_occurrence("Bolt:2", a)])
        assert [p for p, _c in common.component_placements(d)] == ["Bolt:1", "Bolt:2"]

    def test_an_occurrence_missing_either_half_is_omitted(self):
        # a placement without a path, or without a component, identifies nothing
        a = _comp_with_sketches("Frame")
        root = _comp_with_sketches("Root")
        d = _design_with_occs(root, [a], [make_occurrence("", a), make_occurrence("Frame:1", None),
                                          make_occurrence("Frame:2", a)])
        assert [p for p, _c in common.component_placements(d)] == ["Frame:2"]


class TestPlacementPathsNamed:
    """The paths a component NAME answers to. It never claims which component a path holds - only
    that the path places a component of that name, which is exactly what resolves when passed back."""

    def _two_xrefs(self):
        # ONE token, two components - the shape _XREF_TOKEN carries
        a = _xref_twin("Frame", ["Frame_Ring"])
        b = _xref_twin("Frame", ["Frame_Ring"])
        root = _comp_with_sketches("Root")
        return a, b, _design_with_occs(root, [a, b],
                                       [make_occurrence("P2a-Gimbal:1+Frame:1", a),
                                        make_occurrence("P3-Gimbal:1+Frame:1", b)])

    def test_two_colliding_token_components_yield_TWO_paths_not_four(self):
        # THE defect: keyed by entityToken, each component collected BOTH paths and the refusal
        # printed four entries for two components. Enumerating occurrences lists each once.
        _a, _b, d = self._two_xrefs()
        assert common.placement_paths_named(d, "Frame") == [
            "P2a-Gimbal:1+Frame:1", "P3-Gimbal:1+Frame:1"]

    def test_the_colliding_tokens_really_are_equal_so_the_fixture_bites(self):
        # Guards the fixture itself: if these tokens ever differ, this class stops testing the
        # measured shape and every assertion above passes for the wrong reason.
        a, b, _d = self._two_xrefs()
        assert a.entityToken == b.entityToken
        assert common.same_component(a, b) is True      # which is why the walk consults no identity

    def test_a_name_no_component_wears_has_no_placements(self):
        _a, _b, d = self._two_xrefs()
        assert common.placement_paths_named(d, "Ghost") == []

    def test_the_root_component_contributes_no_placement(self):
        # nothing places the root, so there is no path to offer - never a wrong one
        _a, _b, d = self._two_xrefs()
        assert common.placement_paths_named(d, "Root") == []

    def test_the_name_match_is_the_shared_scope_comparison(self):
        # case-insensitive, like every other component scope, so the refusal's paths match the
        # components the scope itself selected
        _a, _b, d = self._two_xrefs()
        assert len(common.placement_paths_named(d, "frame")) == 2


class TestFindSketchIn:
    """The SCOPED resolver over an ALREADY-RESOLVED component. It filters by component IDENTITY, not
    by name: the case it exists for is two components wearing one name."""

    def _shared(self):
        root = _comp_with_sketches("Root", ["Sketch2"])
        sub = _comp_with_sketches("Bracket", ["Sketch2"])
        return root, sub, _design_with(root, [sub])

    def test_the_scope_resolves_that_components_own_sketch(self):
        root, sub, d = self._shared()
        assert common.find_sketch_in(d, "Sketch2", sub, "Bracket") == (sub.sketches.item(0), None)

    def test_the_other_scope_resolves_the_other_components_sketch(self):
        root, sub, d = self._shared()
        assert common.find_sketch_in(d, "Sketch2", root, "Root") == (root.sketches.item(0), None)

    def test_two_components_wearing_ONE_name_still_resolve_apart(self):
        # the measured insert case: a name filter answers the same sketch for both, so this is the
        # test that the resolve does not go by name
        a = _comp_with_sketches("Frame", ["Frame_Ring"])
        b = _comp_with_sketches("Frame", ["Frame_Ring"])
        d = _design_with(a, [b])
        assert common.find_sketch_in(d, "Frame_Ring", a, "p+Frame:1")[0] is a.sketches.item(0)
        assert common.find_sketch_in(d, "Frame_Ring", b, "q+Frame:1")[0] is b.sketches.item(0)

    def test_components_sharing_ONE_TOKEN_resolve_to_their_OWN_sketch(self):
        # The discriminating case: SAME sketch name, SAME component name, SAME entityToken - two
        # x-refs. An identity filter (same_component) answers
        # True for both, so it either refuses or returns whichever hit survived de-duplication; a
        # name filter cannot separate them at all. Asking each component's OWN collection is the
        # only thing that answers correctly, and the two sketches must come back DISTINCT.
        a = _xref_twin("Frame", ["Frame_Ring"])
        b = _xref_twin("Frame", ["Frame_Ring"])
        d = _design_with(_comp_with_sketches("Root"), [a, b])
        first, err_a = common.find_sketch_in(d, "Frame_Ring", a, "P2a-Gimbal:1+Frame:1")
        second, err_b = common.find_sketch_in(d, "Frame_Ring", b, "P3-Gimbal:1+Frame:1")
        assert err_a is None and err_b is None
        assert first is a.sketches.item(0)
        assert second is b.sketches.item(0)
        assert first is not second          # the whole point: two reads, two different sketches

    def test_the_unscoped_resolver_still_refuses_the_same_name(self):
        # The scope is an ADDITION: find_sketch must not have been weakened into a first-match.
        _root, _sub, d = self._shared()
        assert common.resolve_sketch(d, "Sketch2") is None

    def test_a_component_holding_no_such_sketch_is_refused_naming_the_ones_that_do(self):
        root = _comp_with_sketches("Root", ["Sketch2"])
        sub = _comp_with_sketches("Bracket", ["Sketch1"])
        sk, err = common.find_sketch_in(_design_with(root, [sub]), "Sketch2", sub, "Bracket")
        assert sk is None
        assert "Component 'Bracket' holds no sketch named 'Sketch2'" in err and "'Root'" in err

    def test_the_retry_remedy_names_the_scope_input_and_defaults_to_component(self):
        # The remedy has to name an input the CALLING tool accepts. Tools declaring a strict schema
        # spell their scope differently ('boundary_component', 'dxf_component') and carry no
        # 'component' input at all, so a hardcoded name there is a retry the schema rejects. The
        # default is what sketch_get's own scoped read emits and must stay 'component'.
        root = _comp_with_sketches("Root", ["Sketch2"])
        sub = _comp_with_sketches("Bracket", ["Sketch1"])
        d = _design_with(root, [sub])
        _sk, default_err = common.find_sketch_in(d, "Sketch2", sub, "Bracket")
        assert default_err.endswith("Retry with one of those as 'component'.")
        _sk, named_err = common.find_sketch_in(d, "Sketch2", sub, "Bracket", "dxf_component")
        assert named_err.endswith("Retry with one of those as 'dxf_component'.")
        assert "'component'" not in named_err

    def test_a_name_nothing_carries_lists_only_THAT_components_sketches(self):
        # the defect: a design-wide 'Available' list answers a scoped miss with sketches the caller
        # just excluded - here Bracket's - which is a suggestion that will not resolve either
        root = _comp_with_sketches("Root", ["Sketch2", "Base"])
        sub = _comp_with_sketches("Bracket", ["BracketOnly"])
        sk, err = common.find_sketch_in(_design_with(root, [sub]), "Nope", root, "Root")
        assert sk is None
        assert "Component 'Root' holds no sketch named 'Nope'" in err
        assert "Component 'Root' holds: Sketch2, Base." in err
        assert "BracketOnly" not in err

    def test_a_component_with_no_sketches_at_all_says_so(self):
        root = _comp_with_sketches("Root", ["Sketch2"])
        sub = _comp_with_sketches("Empty")
        sk, err = common.find_sketch_in(_design_with(root, [sub]), "Nope", sub, "Empty")
        assert sk is None and "Component 'Empty' holds: (no sketches)." in err

    def test_a_path_scope_names_the_component_as_read_and_echoes_the_scope(self):
        # An occurrence path is NOT a component name, so "Component 'Gimbal:1+Root:1'" would assert
        # something never read. The name as read leads; the scope is echoed as the offending value.
        root = _comp_with_sketches("Root", ["Sketch2"])
        sk, err = common.find_sketch_in(_design_with(root, []), "Nope", root, "Gimbal:1+Root:1")
        assert sk is None
        assert "Component 'Root' (scope 'Gimbal:1+Root:1') holds no sketch named 'Nope'" in err
        assert "Component 'Root' holds: Sketch2." in err

    def test_a_case_variant_scope_names_the_component_as_read(self):
        # The scope match is case-insensitive, so 'root' resolves - but NO component is named 'root'
        # and the refusal must not say one is.
        root = _comp_with_sketches("Root", ["Sketch2"])
        sk, err = common.find_sketch_in(_design_with(root, []), "Nope", root, "root")
        assert sk is None
        assert "Component 'Root' (scope 'root')" in err
        assert "Component 'root' holds" not in err

    def test_the_scope_is_not_echoed_when_it_IS_the_name(self):
        # the ordinary case stays terse - no parenthetical repeating what was just said
        root = _comp_with_sketches("Root", ["Sketch2"])
        sk, err = common.find_sketch_in(_design_with(root, []), "Nope", root, "Root")
        assert sk is None and "(scope" not in err

    def test_a_component_whose_name_will_not_read_is_not_given_one(self):
        root = _comp_with_sketches("Root", ["Sketch2"])
        blind = type("C", (), {"sketches": _NamedCollection([]), "entityToken": "blind",
                               "name": property(lambda self: (_ for _ in ()).throw(
                                   RuntimeError("no name")))})()
        sk, err = common.find_sketch_in(_design_with(root, []), "Nope", blind, "whatever")
        assert sk is None and "the scoped component (scope 'whatever')" in err


class TestFindOrRecentSketch:
    """The name-or-most-recent contract in its REFUSING form: the third value is what lets a caller
    tell "no sketch carries this name" apart from "several do"."""

    def test_a_shared_name_hands_back_the_refusal_not_a_bare_miss(self):
        # The defect this third value removes: a caller seeing (None, name) worded it "No sketch
        # named 'Profile'" while TWO sketches carried the name - the opposite of what was read.
        root = _comp_with_sketches("Root", ["Profile"])
        sub = _comp_with_sketches("Frame", ["Profile"])
        sk, requested, ambiguous = common.find_or_recent_sketch(_design_with(root, [sub]), "Profile")
        assert sk is None and requested == "Profile"
        assert ambiguous and "2 sketches" in ambiguous and "Root" in ambiguous and "Frame" in ambiguous

    def test_a_name_no_sketch_carries_has_no_refusal_text(self):
        root = _comp_with_sketches("Root", ["Base"])
        assert common.find_or_recent_sketch(_design_with(root, []), "Ghost") == (None, "Ghost", None)

    def test_a_unique_name_resolves_with_neither_flag(self):
        root = _comp_with_sketches("Root", ["Base"])
        sub = _comp_with_sketches("Frame", ["Profile"])
        d = _design_with(root, [sub])
        assert common.find_or_recent_sketch(d, "Base") == (root.sketches.item(0), "Base", None)

    def test_a_blank_name_takes_the_most_recent_sketch_in_the_active_component(self):
        root = _comp_with_sketches("Root", ["Base"])
        sub = _comp_with_sketches("Frame", ["First", "Last"])
        d = _design_with(root, [sub], active=sub)
        assert common.find_or_recent_sketch(d, "  ") == (sub.sketches.item(1), None, None)

    def test_a_blank_name_with_no_sketches_is_a_plain_miss(self):
        root = _comp_with_sketches("Root")
        assert common.find_or_recent_sketch(_design_with(root, []), "") == (None, None, None)

    def test_resolve_or_recent_sketch_drops_the_refusal_and_keeps_the_pair(self):
        # The silent form stays the two-value contract its remaining callers (a postcondition
        # fingerprint) read - a shared name answers None there, like a name none carries.
        root = _comp_with_sketches("Root", ["Profile"])
        sub = _comp_with_sketches("Frame", ["Profile"])
        assert common.resolve_or_recent_sketch(_design_with(root, [sub]), "Profile") == (None, "Profile")


class TestAllSketchNames:
    def test_spans_every_component(self):
        root = _comp_with_sketches("Root", ["Base"])
        sub = _comp_with_sketches("Frame", ["FrameSketch"])
        assert common.all_sketch_names(_design_with(root, [sub])) == ["Base", "FrameSketch"]

    def test_a_repeated_name_is_qualified_by_its_owning_component(self):
        # Bare, the list read "S, S" - one name printed twice, which reads as a duplicate entry
        # rather than as two sketches in two components. The owner is what tells them apart.
        root = _comp_with_sketches("Alpha", ["S"])
        sub = _comp_with_sketches("Beta", ["S"])
        assert common.all_sketch_names(_design_with(root, [sub])) == ["S (Alpha)", "S (Beta)"]

    def test_a_unique_name_stays_bare_beside_a_repeated_one(self):
        # Only the ambiguous name pays the qualifier; qualifying every row would make the common
        # single-component list unreadable.
        root = _comp_with_sketches("Alpha", ["S", "Profile"])
        sub = _comp_with_sketches("Beta", ["S"])
        names = common.all_sketch_names(_design_with(root, [sub]))
        assert names == ["S (Alpha)", "Profile", "S (Beta)"]

    def test_three_sketches_sharing_one_name_are_each_qualified(self):
        root = _comp_with_sketches("Alpha", ["S"])
        b = _comp_with_sketches("Beta", ["S"])
        c = _comp_with_sketches("Gamma", ["S"])
        assert common.all_sketch_names(_design_with(root, [b, c])) == [
            "S (Alpha)", "S (Beta)", "S (Gamma)"]

    def test_a_repeated_name_whose_owner_will_not_read_stays_bare(self):
        # Nothing measured to qualify with - the honest form is the bare name, not "S (None)".
        class _NamelessComp:
            sketches = _NamedCollection([type("Sk", (), {"name": "S"})()])

            @property
            def name(self):
                raise RuntimeError("component name unreadable")

        root = _comp_with_sketches("Alpha", ["S"])
        names = common.all_sketch_names(_design_with(root, [_NamelessComp()]))
        assert names == ["S (Alpha)", "S"]

    def test_no_sketches_is_an_empty_list(self):
        # The empty case every caller renders as "Available: (none)".
        root = _comp_with_sketches("Root")
        assert common.all_sketch_names(_design_with(root, [])) == []


class TestResolveEntityRef:
    class _Curves:
        def __init__(self, lines=(), arcs=(), circles=()):
            self.sketchLines = _NamedCollection(list(lines))
            self.sketchArcs = _NamedCollection(list(arcs))
            self.sketchCircles = _NamedCollection(list(circles))

    def _sketch(self):
        line = type("Line", (), {"name": "L0"})()
        return type("Sk", (), {
            "sketchCurves": self._Curves(lines=[line]),
            "sketchPoints": _NamedCollection([type("Pt", (), {"name": "P0"})()]),
        })()

    def test_resolves_line_by_index(self):
        assert common.resolve_entity_ref(self._sketch(), "line:0").name == "L0"

    def test_resolves_point_by_index(self):
        assert common.resolve_entity_ref(self._sketch(), "point:0").name == "P0"

    def test_bad_type_is_none(self):
        assert common.resolve_entity_ref(self._sketch(), "spline:0") is None

    def test_out_of_range_is_none(self):
        assert common.resolve_entity_ref(self._sketch(), "line:9") is None

    def test_malformed_ref_is_none(self):
        assert common.resolve_entity_ref(self._sketch(), "line") is None


class TestResolveEntityRefs(TestResolveEntityRef):
    """The comma-separated list parser over resolve_entity_ref - the ONE 'entities' selector
    sketch_constrain's list kinds and sketch_move/sketch_copy share."""

    def test_an_empty_selector_yields_no_refs_and_no_error(self):
        ents, refs, err = common.resolve_entity_refs(self._sketch(), "  ")
        assert (ents, refs, err) == ([], [], None)

    def test_one_ref_resolves(self):
        ents, refs, err = common.resolve_entity_refs(self._sketch(), "line:0")
        assert [e.name for e in ents] == ["L0"] and refs == ["line:0"] and err is None

    def test_several_refs_keep_the_order_given_and_tolerate_spacing(self):
        ents, refs, err = common.resolve_entity_refs(self._sketch(), " point:0 , line:0 ")
        assert [e.name for e in ents] == ["P0", "L0"] and refs == ["point:0", "line:0"]
        assert err is None

    def test_the_first_ref_that_misses_is_named_with_the_legal_kinds(self):
        ents, refs, err = common.resolve_entity_refs(self._sketch(), "line:0,arc:7,line:0")
        assert ents is None and "'arc:7'" in err and "line/arc/circle" in err
        assert refs == ["line:0", "arc:7", "line:0"]     # kept, so a caller can echo what it got

    def test_the_field_name_in_the_error_is_the_caller_s(self):
        _e, _r, err = common.resolve_entity_refs(self._sketch(), "arc:7", field="targets")
        assert "in 'targets'" in err


class _GP:
    def __init__(self, x, y, z=0.0):
        self.x, self.y, self.z = x, y, z


class _AnchorPoint:
    def __init__(self, tag, x=0.0, y=0.0, z=0.0):
        self.tag = tag
        self.geometry = _GP(x, y, z)


class _AnchorLine:
    def __init__(self):
        self.startSketchPoint = _AnchorPoint("start", 0.0, 0.0, 0.0)
        self.endSketchPoint = _AnchorPoint("end", 4.0, 0.0, 0.0)


class _AnchorCircle:
    def __init__(self):
        self.centerSketchPoint = _AnchorPoint("center", 1.0, 1.0, 0.0)


class _AnchorArc:
    def __init__(self):
        self.startSketchPoint = _AnchorPoint("astart")
        self.endSketchPoint = _AnchorPoint("aend")
        self.centerSketchPoint = _AnchorPoint("acenter")


class _MidSketch:
    """Records the SketchPoint and the midpoint constraint the 'mid' anchor creates."""
    def __init__(self):
        self.added = []
        self.midpoints = []
        self.sketchPoints = self
        self.geometricConstraints = self
    def add(self, p):
        self.added.append(p)
        return _AnchorPoint("midpoint")
    def addMidPoint(self, pt, line):
        self.midpoints.append((pt, line))
        return True


class TestParseAnchorRef:
    """The optional THIRD segment of a '<type>:<index>' ref - the ONE grammar sketch_dimension and
    sketch_constrain both read, so an anchor form one verb accepts cannot be rejected by the other."""

    def test_a_bare_ref_carries_no_anchor(self):
        assert common.parse_anchor_ref("line:0") == ("line:0", None, None)

    def test_a_line_endpoint_anchor_splits_off(self):
        assert common.parse_anchor_ref("line:0:end") == ("line:0", "end", None)

    def test_a_circle_centre_anchor_splits_off(self):
        assert common.parse_anchor_ref("circle:2:center") == ("circle:2", "center", None)

    def test_the_anchor_segment_is_case_insensitive(self):
        # entity refs resolve case-insensitively; an anchor that did not would turn a shouted ref
        # into an "unknown anchor" refusal for a form the tools accept in lower case
        assert common.parse_anchor_ref("CIRCLE:0:CENTER") == ("CIRCLE:0", "center", None)

    def test_an_unknown_third_segment_errors_naming_the_valid_anchors(self):
        # silently dropping it would dimension/constrain the WRONG point of the entity
        base, anchor, err = common.parse_anchor_ref("line:0:bogus")
        assert base is None and anchor is None
        assert "unknown anchor" in err and "center" in err


class TestAnchorPoint:
    def test_line_end_and_start(self):
        assert common.anchor_point(None, _AnchorLine(), "end")[0].tag == "end"
        assert common.anchor_point(None, _AnchorLine(), "start")[0].tag == "start"

    def test_circle_centre(self):
        assert common.anchor_point(None, _AnchorCircle(), "center")[0].tag == "center"

    def test_start_on_a_circle_is_refused(self):
        pt, err = common.anchor_point(None, _AnchorCircle(), "start")
        assert pt is None and "line or arc" in err

    def test_centre_on_a_line_is_refused(self):
        pt, err = common.anchor_point(None, _AnchorLine(), "center")
        assert pt is None and "circle or arc" in err

    def test_mid_on_a_line_creates_a_parametrically_welded_point(self, monkeypatch):
        import adsk.core
        monkeypatch.setattr(adsk.core.Point3D, "create", lambda x, y, z: ("pt", x, y, z))
        sk = _MidSketch()
        pt, err = common.anchor_point(sk, _AnchorLine(), "mid")
        assert err is None and pt.tag == "midpoint"
        assert sk.added == [("pt", 2.0, 0.0, 0.0)]     # the geometric midpoint of 0..4
        assert len(sk.midpoints) == 1                  # welded with a midpoint constraint

    def test_mid_on_an_arc_is_refused(self):
        # an arc has a centre, so 'mid' (a line-only addMidPoint target) is refused, not mis-applied
        pt, err = common.anchor_point(None, _AnchorArc(), "mid")
        assert pt is None and "LINE" in err


class TestOperations:
    def test_maps_every_verb_to_a_feature_operation_attribute_name(self):
        for key in ("new", "new_body", "join", "cut", "intersect"):
            assert common.OPERATIONS[key].endswith("FeatureOperation")


class TestMinDistance:
    """The one measureMinimumDistance core both measure tools share - a READ, so a failure is an
    error result, never a swallowed None."""

    def _install_mgr(self, monkeypatch, result=None, raises=False):
        class _Mgr:
            def measureMinimumDistance(self, a, b):
                if raises:
                    raise RuntimeError("boom")
                return result
        monkeypatch.setattr(common.app, "measureManager", _Mgr())

    def test_success_returns_result_and_no_error(self, monkeypatch):
        res = type("R", (), {"value": 1.0})()
        self._install_mgr(monkeypatch, result=res)
        mr, err = common.min_distance(object(), object())
        assert err is None and mr is res

    def test_measure_exception_is_surfaced_as_error(self, monkeypatch):
        self._install_mgr(monkeypatch, raises=True)
        mr, err = common.min_distance(object(), object())
        assert mr is None and err["isError"] is True and "failed" in err["message"].lower()

    def test_none_result_is_an_error_not_a_silent_none(self, monkeypatch):
        self._install_mgr(monkeypatch, result=None)
        mr, err = common.min_distance(object(), object())
        assert mr is None and err["isError"] is True


# ── same_component / root_body_advisory: identity NEVER carries the comparison ───────────────

class _FakeDesignWithRoot:
    def __init__(self, root, occurrence_count=0):
        self._root = root
        self._occ = occurrence_count

    @property
    def rootComponent(self):
        # a FRESH wrapper on every read, as the platform hands back (measured: two reads of
        # design.rootComponent are different Python objects sharing one entityToken)
        self._root.occurrences = type("C", (), {"count": self._occ})()
        return entity_proxy(self._root)


def _root(name="Root", token="TOKEN:Root", bodies=0):
    comp = MakeComp(name=name, bodies=["B%d" % i for i in range(bodies)])
    comp.entityToken = token
    return comp


class TestSameComponent:
    def test_distinct_wrappers_of_one_component_are_the_same(self):
        c = _root()
        assert common.same_component(entity_proxy(c), entity_proxy(c)) is True

    def test_identity_still_short_circuits(self):
        c = _root()
        assert common.same_component(c, c) is True

    def test_different_tokens_are_different_components(self):
        assert common.same_component(_root(token="TOKEN:A"), _root(token="TOKEN:B")) is False

    def test_an_unreadable_token_is_UNKNOWN_not_a_name_match(self):
        # Two components can carry one name, so matching on it answers True about a pair nothing
        # identified. None is the only verdict an unreadable token supports.
        a, b = _root(name="Sub", token=None), _root(name="Sub", token=None)
        assert common.same_component(a, b) is None

    def test_an_unreadable_token_is_UNKNOWN_even_when_the_names_differ(self):
        # The other direction of the same fallback: one component answers a different name after a
        # rename, so a name mismatch is not a proof of two components either.
        a, b = _root(name="Sub", token=None), _root(name="Other", token=None)
        assert common.same_component(a, b) is None

    def test_ONE_unreadable_token_is_enough_to_make_it_unknown(self):
        # the boundary: `ta and tb` needs BOTH sides, and a single readable token identifies nothing
        assert common.same_component(_root(token="TOKEN:A"), _root(token=None)) is None
        assert common.same_component(_root(token=None), _root(token="TOKEN:A")) is None

    def test_an_EMPTY_token_reads_as_unreadable(self):
        # '' is what a token that did not mint looks like; comparing two of them equal would call
        # every such component the same one
        assert common.same_component(_root(token=""), _root(token="")) is None

    def test_none_is_UNKNOWN_not_a_different_component(self):
        # A None operand IS the caller's own failed read (safe(lambda: o.component)); calling that
        # "different components" is the fallback this helper exists to refuse.
        assert common.same_component(None, _root()) is None
        assert common.same_component(_root(), None) is None


# ── the physical-body identity: a DOCUMENT-LOCAL token paired with its source document ──────────
#
# The x-ref shape, measured on a host holding two x-refs of one design: 'Frame's body reads ONE
# entityToken through both x-refs while the two source documents' lineage ids differ.

_URN_A = "urn:adsk.wipprod:dm.lineage:K3I2nkywRlaWPHJexysOdA"
_URN_B = "urn:adsk.wipprod:dm.lineage:N_QoPrrrSJmF__f9BZV86A"
_COLLIDING_TOKEN = "/vB+AAEAAwAAAAAAAAAAAAAA"


def _body_in_document(name, token, urn):
    """One body living in the document whose lineage id is `urn`. `urn=None` gives a body in a
    never-saved document, which carries no dataFile to read an id off."""
    return BRepBody(name, entity_token=token,
                    parent_component=MakeComp(name=name,
                                              parent_design=make_source_document(urn)))


def _component_in_document(name, token, urn):
    """One COMPONENT living in the document whose lineage id is `urn` - the same document, reached
    one hop shorter than a body's (a Component answers parentDesign, never parentComponent)."""
    return MakeComp(name=name, entity_token=token, parent_design=make_source_document(urn))


class _ProxyWithUnreadableParent:
    """An occurrence proxy carrying its own token, whose OWN ``parentComponent`` read RAISES.

    conftest.body_proxy delegates every read but three to the native, so a document read taken off
    the WRAPPER is indistinguishable there from one taken off the NATIVE. An unreadable read on the
    wrapper separates them without asserting anything about what a live proxy's parentComponent
    answers: whatever it answers, the identity is not read from it."""

    def __init__(self, native, token):
        self.nativeObject = native
        self.entityToken = token

    @property
    def parentComponent(self):
        raise RuntimeError("4 : An API Object refers to a deleted Object")


class TestTheXrefIdentityFixture:
    def test_the_two_document_fixture_really_models_the_collision(self):
        # Both halves have to be real or the tests below prove nothing: with no token collision the
        # defect this key exists for never fires, and with no native/proxy pair a key that pulls one
        # body apart from its own proxy would look correct.
        a = _body_in_document("Frame", _COLLIDING_TOKEN, _URN_A)
        b = _body_in_document("Frame", _COLLIDING_TOKEN, _URN_B)
        assert a is not b
        assert common.native_token(a) == common.native_token(b) == _COLLIDING_TOKEN
        assert a.parentComponent.parentDesign.parentDocument.dataFile.id == _URN_A
        assert b.parentComponent.parentDesign.parentDocument.dataFile.id == _URN_B
        proxy = body_proxy(a, SimpleNamespace(name="Frame:1", fullPathName="Frame:1"))
        assert proxy.entityToken != a.entityToken          # a real proxy, not another reference
        assert common.native_token(proxy) == common.native_token(a)


class TestNativeToken:
    def test_a_proxy_answers_its_NATIVES_token(self):
        native = _body_in_document("Frame", "TOK-NATIVE", _URN_A)
        proxy = body_proxy(native, SimpleNamespace(name="Frame:1", fullPathName="Frame:1"))
        assert common.native_token(proxy) == "TOK-NATIVE"

    def test_a_wrapper_that_answers_no_nativeObject_keys_on_its_own_token(self):
        b = _body_in_document("Frame", "TOK-ONLY", _URN_A)
        del b.nativeObject
        assert common.native_token(b) == "TOK-ONLY"

    def test_an_unreadable_token_reads_None(self):
        b = _body_in_document("Frame", "TOK", _URN_A)
        del b.entityToken
        assert common.native_token(b) is None


class TestNativeIdentity:
    def test_two_bodies_in_TWO_documents_sharing_one_token_are_not_one_entity(self):
        # The defect the pair exists for: an entityToken is document-local, so keyed on the token
        # alone these two DISTINCT bodies compare equal - a de-dup drops one with no trace.
        a = _body_in_document("Frame", _COLLIDING_TOKEN, _URN_A)
        b = _body_in_document("Frame", _COLLIDING_TOKEN, _URN_B)
        assert common.native_identity(a) != common.native_identity(b)
        assert len({common.native_identity(a), common.native_identity(b)}) == 2

    def test_a_body_and_its_occurrence_proxy_are_ONE_entity(self):
        # A body and its own proxy are ONE entity: the proxy resolves to the same native, so both
        # halves of the key - token and document - are read off that one entity.
        native = _body_in_document("Frame", _COLLIDING_TOKEN, _URN_A)
        proxy = body_proxy(native, SimpleNamespace(name="Frame:1", fullPathName="Frame:1"))
        assert common.native_identity(proxy) == common.native_identity(native)

    def test_BOTH_halves_are_read_off_the_native_not_off_the_wrapper(self):
        # The invariant _inputs._body_key leans on: both halves come from the SAME entity, which is
        # what makes a proxy's key equal to its native's. Read off the wrapper, the two halves would
        # describe two different objects.
        native = _body_in_document("Frame", _COLLIDING_TOKEN, _URN_A)
        proxy = _ProxyWithUnreadableParent(native, "PROXY::Frame:1::" + _COLLIDING_TOKEN)
        assert proxy.entityToken != native.entityToken
        assert (common.native_identity(proxy) == common.native_identity(native)
                == (_COLLIDING_TOKEN, _URN_A))

    def test_the_second_half_is_the_source_documents_lineage_id(self):
        a = _body_in_document("Frame", _COLLIDING_TOKEN, _URN_A)
        assert common.native_identity(a) == (_COLLIDING_TOKEN, _URN_A)

    def test_a_never_saved_documents_body_pairs_its_token_with_None(self):
        # An unsaved document has no dataFile. None is a legitimate half - such a body can only be
        # the host's own, where the token is already unique - and nothing is substituted for it.
        unsaved = _body_in_document("Frame", _COLLIDING_TOKEN, None)
        assert common.native_identity(unsaved) == (_COLLIDING_TOKEN, None)

    def test_a_body_whose_document_cannot_be_reached_at_all_still_has_its_token(self):
        loose = BRepBody("Frame", entity_token=_COLLIDING_TOKEN)
        assert common.native_identity(loose) == (_COLLIDING_TOKEN, None)

    def test_two_bodies_in_ONE_document_are_still_separated_by_their_tokens(self):
        one = _body_in_document("Frame", "TOK-A", _URN_A)
        two = _body_in_document("Lid", "TOK-B", _URN_A)
        assert common.native_identity(one) != common.native_identity(two)

    def test_an_unreadable_token_has_no_identity_at_all(self):
        # No token, no identity: answering (None, urn) would make every unreadable body in one
        # document equal to every other, which is a merge, not an unknown.
        b = _body_in_document("Frame", "TOK", _URN_A)
        del b.entityToken
        assert common.native_identity(b) is None

    def test_an_EMPTY_token_has_no_identity_either(self):
        b = _body_in_document("Frame", "TOK", _URN_A)
        b.entityToken = ""
        assert common.native_identity(b) is None


class TestNativeIdentityOfAComponent:
    """A COMPONENT hangs off the design one hop closer than a body does, so its source document is
    read through parentDesign rather than parentComponent.parentDesign. Both kinds answer the same
    key - a token-only key merges every document's ROOT component, which all read one shared token
    (measured on a CAM job assembled from 7 source documents)."""

    def test_the_two_chains_are_disjoint_on_the_MEASURED_shapes(self):
        # The whole two-chain design rests on this: exactly ONE chain reads per kind, so neither can
        # answer for the other and the second is never a looser guess at the first. If a Fusion
        # build ever gives a body a parentDesign or a component a parentComponent, this goes red and
        # the fallback has to be re-argued rather than silently changing meaning.
        shapes = live_api_facts.SHAPES
        for body_kind in ("BRepBody", "MeshBody"):
            assert "parentComponent" in shapes[body_kind], body_kind
            assert "parentDesign" not in shapes[body_kind], body_kind
        assert "parentDesign" in shapes["Component"]
        assert "parentComponent" not in shapes["Component"]

    def test_only_a_COMPONENT_can_reach_the_second_chain_at_all(self):
        # The blast-radius guard over the whole measured surface, not just the three kinds above: a
        # helper the entire tool surface consumes must answer for exactly the kinds it means to.
        # parentDesign is carried by Component and Timeline only, and no identity is taken for a
        # timeline, so every other kind an identity is taken for (an occurrence, a face, an edge -
        # none of which carry either attribute) reaches neither chain and answers None. A build
        # that adds parentDesign anywhere else reds this.
        carriers = sorted(k for k, v in live_api_facts.SHAPES.items() if "parentDesign" in v)
        assert carriers == ["Component", "Timeline"], carriers
        for silent_kind in ("Occurrence", "BRepFace", "BRepEdge"):
            attrs = live_api_facts.SHAPES[silent_kind]
            assert "parentDesign" not in attrs and "parentComponent" not in attrs, silent_kind

    def test_root_components_of_TWO_documents_sharing_one_token_are_not_one_entity(self):
        a = _component_in_document("Stock", _COLLIDING_TOKEN, _URN_A)
        b = _component_in_document("Vise", _COLLIDING_TOKEN, _URN_B)
        assert common.native_token(a) == common.native_token(b) == _COLLIDING_TOKEN
        assert common.native_identity(a) != common.native_identity(b)

    def test_a_components_urn_half_is_its_own_documents_lineage_id(self):
        c = _component_in_document("Stock", _COLLIDING_TOKEN, _URN_A)
        assert common.native_identity(c) == (_COLLIDING_TOKEN, _URN_A)

    def test_two_components_of_ONE_document_are_still_separated_by_their_tokens(self):
        one = _component_in_document("Stock", "TOK-A", _URN_A)
        two = _component_in_document("Vise", "TOK-B", _URN_A)
        assert common.native_identity(one) != common.native_identity(two)

    def test_a_component_in_a_never_saved_document_pairs_its_token_with_None(self):
        unsaved = _component_in_document("Stock", _COLLIDING_TOKEN, None)
        assert common.native_identity(unsaved) == (_COLLIDING_TOKEN, None)

    def test_reading_a_component_does_not_disturb_the_body_chain(self):
        # The component chain is a FALLBACK: a body whose parentComponent chain reads must still
        # take its urn from there, or the two kinds would answer off different hops.
        b = _body_in_document("Frame", _COLLIDING_TOKEN, _URN_A)
        assert common.native_identity(b) == (_COLLIDING_TOKEN, _URN_A)


class TestSameComponentAcrossDocuments:
    """The same-component verdict runs on native_identity, so a cross-document token collision
    cannot answer True.

    Every document's ROOT COMPONENT reads one shared entityToken, and a component an x-ref brought
    into a host IS that source document's root - so on a token-only compare the host's own root and
    each x-ref'd component read as one component. Every caller's `is True` branch then takes the
    wrong side on exactly the pair it was written to separate: "nothing to lift" for a frame that
    does need lifting, "this Joint Origin is native at the root" for one that must be proxied, "the
    write landed on the component I asked for" for one that landed somewhere else."""

    def test_two_components_in_TWO_documents_sharing_one_token_are_DIFFERENT(self):
        host_root = _component_in_document("Host", _COLLIDING_TOKEN, _URN_A)
        xref = _component_in_document("ProbeFrame", _COLLIDING_TOKEN, _URN_B)
        assert common.native_token(host_root) == common.native_token(xref)   # the collision is real
        assert common.same_component(host_root, xref) is False

    def test_two_wrappers_of_ONE_component_in_a_saved_document_are_the_SAME(self):
        # The other direction, and the read every `is True` branch depends on: the urn half must not
        # split a component from its own second wrapper.
        c = _component_in_document("Stock", _COLLIDING_TOKEN, _URN_A)
        assert common.same_component(entity_proxy(c), entity_proxy(c)) is True

    def test_an_unsaved_hosts_own_root_does_not_merge_with_the_xref_it_holds(self):
        # A never-saved host reads no lineage id at all, while the x-ref it holds comes from a saved
        # document - so this pair separates on the urn half alone, with both tokens identical.
        unsaved_root = _component_in_document("Host", _COLLIDING_TOKEN, None)
        xref = _component_in_document("ProbeFrame", _COLLIDING_TOKEN, _URN_A)
        assert common.same_component(unsaved_root, xref) is False

    def test_two_components_of_ONE_document_are_still_told_apart_by_their_tokens(self):
        one = _component_in_document("Stock", "TOK-A", _URN_A)
        two = _component_in_document("Vise", "TOK-B", _URN_A)
        assert common.same_component(one, two) is False

    def test_two_identity_less_components_in_ONE_document_are_UNKNOWN_not_the_same(self):
        # Both identities read None, and comparing the two values directly would call that pair one
        # component - the merge the None gate refuses. Nothing was read, so None is the verdict.
        a = _component_in_document("Sub", None, _URN_A)
        b = _component_in_document("Sub", None, _URN_A)
        assert common.native_identity(a) is None and common.native_identity(b) is None
        assert common.same_component(a, b) is None


# ── the assembly-context walk + the cycle test the structural edits run on ──────────────────────


def _occ(path, component=None):
    """One occurrence record: the two attributes these helpers read."""
    return SimpleNamespace(fullPathName=path, component=component)


def _design_with_occurrences(*occs, root=None):
    root = root if root is not None else _root()
    root.allOccurrences = list(occs)
    return type("D", (), {"rootComponent": root})()


class TestAllOccurrences:
    def test_reads_the_root_components_assembly_walk(self):
        a, b = _occ("Frame:1"), _occ("Frame:1+Bolt:1")
        assert common.all_occurrences(_design_with_occurrences(a, b)) == [a, b]

    def test_no_design_is_empty(self):
        assert common.all_occurrences(None) == []

    def test_an_unreadable_root_is_empty_not_a_crash(self):
        class D:
            @property
            def rootComponent(self):
                raise RuntimeError("no design")
        assert common.all_occurrences(D()) == []


class TestOccurrencePaths:
    def test_returns_the_set_of_assembly_paths(self):
        d = _design_with_occurrences(_occ("Frame:1"), _occ("Frame:1+Bolt:1"))
        assert common.occurrence_paths(d) == {"Frame:1", "Frame:1+Bolt:1"}

    def test_an_unreadable_path_reads_as_empty_string_not_a_dropped_row(self):
        # a caller filters '' out; silently dropping the row would make a before/after diff report a
        # phantom new path instead.
        bad = SimpleNamespace()
        assert common.occurrence_paths(_design_with_occurrences(_occ("Frame:1"), bad)) == {
            "Frame:1", ""}

    def test_no_occurrences_is_an_empty_set(self):
        assert common.occurrence_paths(_design_with_occurrences()) == set()


class TestComponentContains:
    def test_a_component_contains_ITSELF_through_a_distinct_wrapper(self):
        # the identity trap: two wrappers of one component are different Python objects, so `outer is
        # inner` reads False and a self-nesting call would sail past the guard.
        comp = _root(name="Sub", token="TOKEN:Sub")
        assert common.component_contains(entity_proxy(comp), entity_proxy(comp)) is True

    def test_a_component_INSIDE_it_is_found_through_a_distinct_wrapper(self):
        inner = _root(name="Bolt", token="TOKEN:Bolt")
        outer = _root(name="Sub", token="TOKEN:Sub")
        outer.allOccurrences = [_occ("Sub:1+Bolt:1", component=entity_proxy(inner))]
        assert common.component_contains(outer, entity_proxy(inner)) is True

    def test_an_unrelated_component_is_not_contained(self):
        outer = _root(name="Sub", token="TOKEN:Sub")
        outer.allOccurrences = [_occ("Sub:1+Bolt:1", component=_root(name="Bolt",
                                                                    token="TOKEN:Bolt"))]
        assert common.component_contains(outer, _root(name="Frame", token="TOKEN:Frame")) is False

    def test_an_empty_subtree_contains_nothing(self):
        # Both walks enumerate and find nothing, so False here is a PROVEN "no cycle" - the answer
        # that licenses the edit.
        outer = _root(name="Sub", token="TOKEN:Sub")
        outer.allOccurrences = []
        assert common.component_contains(outer, _root(name="X", token="TOKEN:X")) is False
        del outer.allOccurrences                      # the component.occurrences recursion answers
        assert common.component_contains(outer, _root(name="X", token="TOKEN:X")) is False

    def test_a_subtree_NEITHER_walk_can_read_is_unknown(self):
        # THE row: allOccurrences raises (the measured unresolved-external-reference shape) and the
        # component.occurrences fallback raises too, so nothing was read. False here would be "no
        # cycle" about a subtree nobody looked at - the answer that lets the illegal edit through.
        outer = _root(name="Sub", token="TOKEN:Sub")
        del outer.allOccurrences
        outer.occurrences = _RaisingColl()
        assert common.component_contains(outer, _root(name="X", token="TOKEN:X")) is None

    def test_an_occurrence_whose_component_identity_will_not_read_is_unknown(self):
        # The walk answered, but one row's identity did not - so the subtree was not fully compared
        # and a False would overclaim.
        outer = _root(name="Sub", token="TOKEN:Sub")
        outer.allOccurrences = [_occ("Sub:1+?:1", component=_root(name="Bolt", token=None))]
        assert common.component_contains(outer, _root(name="X", token="TOKEN:X")) is None

    def test_a_HIT_still_answers_True_beside_an_unreadable_row(self):
        # An unknown row never demotes a proven containment: the cycle is real either way.
        inner = _root(name="Bolt", token="TOKEN:Bolt")
        outer = _root(name="Sub", token="TOKEN:Sub")
        outer.allOccurrences = [_occ("Sub:1+?:1", component=_root(name="X", token=None)),
                                _occ("Sub:1+Bolt:1", component=entity_proxy(inner))]
        assert common.component_contains(outer, entity_proxy(inner)) is True

    def test_a_census_holding_an_UNRESOLVED_REFERENCE_cannot_answer_False(self):
        # THE design this guard exists for. A broken row leaves complete=True (the fast path stamps
        # it, and walk_children skips a broken child without clearing it), so gating on complete
        # alone answers False - "this edit is legal" - about a subtree whose unresolved reference
        # was never enumerated. Its component RAISES, so what sits under it is unknown.
        outer = _root(name="Sub", token="TOKEN:Sub")
        outer.allOccurrences = [_occ("Sub:1+Bolt:1", component=_root(name="Bolt",
                                                                     token="TOKEN:Bolt")),
                                _BrokenOcc("45740")]
        walk = common.component_walk(outer)
        assert walk.complete is True and len(walk.broken) == 1   # the shape the reviewer measured
        assert common.component_contains(outer, _root(name="X", token="TOKEN:X")) is None

    def test_the_RECURSED_walk_with_a_broken_row_cannot_answer_False_either(self):
        # Same verdict through the fallback walk, where the broken row is collected by the
        # component-local scan and complete likewise stays True.
        outer = _root(name="Sub", token="TOKEN:Sub")
        del outer.allOccurrences
        good = _plain_occ("Bolt:1")
        good.component.entityToken = "TOKEN:Bolt"
        outer.occurrences = _NamedCollection([_BrokenOcc("45740"), good])
        walk = common.component_walk(outer)
        assert walk.method == "recursed" and walk.complete is True and len(walk.broken) == 1
        assert common.component_contains(outer, _root(name="X", token="TOKEN:X")) is None

    def test_a_HIT_still_answers_True_beside_a_broken_row(self):
        # An unresolved row never demotes a PROVEN containment - the cycle is real either way.
        inner = _root(name="Bolt", token="TOKEN:Bolt")
        outer = _root(name="Sub", token="TOKEN:Sub")
        outer.allOccurrences = [_BrokenOcc("45740"),
                                _occ("Sub:1+Bolt:1", component=entity_proxy(inner))]
        assert common.component_contains(outer, entity_proxy(inner)) is True

    def test_an_INCOMPLETE_walk_cannot_answer_False(self):
        # The recursion stopped short (a child's own collection would not enumerate), so the rows it
        # did compare are not the whole subtree - every identity in it read cleanly.
        outer = _root(name="Sub", token="TOKEN:Sub")
        del outer.allOccurrences
        child = _plain_occ("Mid:1")
        child.component.entityToken = "TOKEN:Mid"
        child.childOccurrences = _RaisingColl()
        outer.occurrences = _NamedCollection([child])
        assert common.component_contains(outer, _root(name="X", token="TOKEN:X")) is None

    def test_none_contains_UNKNOWN(self):
        # No component to walk - not a proof that nothing is inside it.
        assert common.component_contains(None, _root()) is None


# ── the unresolved-reference detector + the census that survives a raising allOccurrences ────────

# The verbatim text the platform raises from occ.component on an occurrence whose source project is
# archived. Held as a constant here because a test asserting the DETAIL must assert the real string.
_UNAVAILABLE = ("3 : The occurrence's referenced component is unavailable (broken or missing "
                "external reference).")
_PATH_INVALID = "2 : InternalValidationError : path.valid()"
_WALK_RAISE = "2 : InternalValidationError : occ"
# The text documentReference throws on an ORDINARY local occurrence - the same one the broken
# specimen gives, which is why that read cannot tell the two apart.
_NOT_EXTERNAL = "3 : Occurrence is not referencing an external component"


def _RaisingColl():
    """A collection whose COUNT itself raises - unreadable, which is not the same as empty."""
    return _NamedCollection(raises=_WALK_RAISE)


class _BrokenOcc:
    """The measured specimen - an occurrence whose source project is archived. Bespoke: `name`
    itself RAISES in one variant, which the shared FakeOccurrence answers as a plain attribute.
    Only `name` reads, and it is the only identity a caller can publish. Every other signal a walk
    might gate on LIES: isReferencedComponent reads False where a LIVE xref reads True;
    documentReference raises the SAME "not referencing an external component" text an ordinary
    local occurrence gives, so it cannot tell the two apart; isValid and isLightBulbOn both read
    True. This class is the home of that measurement, and the tests below hold each signal to it."""
    def __init__(self, name="45740", name_raises=False):
        self._name = name
        self._name_raises = name_raises
        self.isReferencedComponent = False
        self.isValid = True
        self.isLightBulbOn = True

    @property
    def name(self):
        if self._name_raises:
            raise RuntimeError(_PATH_INVALID)
        return self._name

    @property
    def component(self):
        raise RuntimeError(_UNAVAILABLE)

    @property
    def fullPathName(self):
        raise RuntimeError(_PATH_INVALID)

    @property
    def childOccurrences(self):
        raise RuntimeError(_PATH_INVALID)

    @property
    def documentReference(self):
        raise RuntimeError(_NOT_EXTERNAL)


def _plain_occ(name, children=(), broken_children=()):
    """An ORDINARY LOCAL occurrence - the trap. isReferencedComponent is False and
    documentReference raises the same text the broken one gives, so only occ.component tells them
    apart."""
    return make_occurrence(
        path=name, children=children,
        # component.occurrences is the SUPERSET: it holds the unresolved child too, while
        # childOccurrences DROPS one - an unresolved child's assembly path is invalid.
        component=types.SimpleNamespace(
            name=name.split(":")[0],
            occurrences=_NamedCollection(list(broken_children) + list(children))),
        raises_on={"documentReference": _NOT_EXTERNAL})


def _walk_design(top=(), broken_top=(), fast=None):
    """A design whose root.allOccurrences RAISES unless `fast` supplies a list, and whose
    root.occurrences holds the component-local superset."""
    class _Root:
        name = "Root"
        occurrences = _NamedCollection(list(broken_top) + list(top))

        @property
        def allOccurrences(self):
            if fast is None:
                raise RuntimeError(_WALK_RAISE)
            return fast

    return types.SimpleNamespace(rootComponent=_Root())


class TestBrokenReference:
    def test_a_raising_component_is_the_signal_and_the_text_is_verbatim(self):
        is_broken, detail = common.broken_reference(_BrokenOcc())
        assert is_broken is True
        assert detail == _UNAVAILABLE

    def test_an_ordinary_local_occurrence_is_NOT_broken(self):
        # THE trap: this occurrence reads isReferencedComponent False and RAISES the same
        # "not referencing an external component" text from documentReference that the broken one
        # does. A detector keyed on either would call it broken.
        occ = _plain_occ("Root:1")
        assert occ.isReferencedComponent is False
        with pytest.raises(RuntimeError):
            occ.documentReference
        assert common.broken_reference(occ) == (False, None)

    def test_a_live_xref_is_NOT_broken(self):
        live = types.SimpleNamespace(name="48205-125 (1):2", isReferencedComponent=True,
                                     component=types.SimpleNamespace(name="48205-125"))
        assert common.broken_reference(live) == (False, None)

    def test_the_gate_ignores_isReferencedComponent_entirely(self):
        # the broken specimen reads FALSE and must still be caught; flipping the flag changes nothing.
        occ = _BrokenOcc()
        occ.isReferencedComponent = True
        assert common.broken_reference(occ)[0] is True

    def test_the_healthy_looking_flags_read_TRUE_and_still_do_not_gate_it(self):
        # isValid and isLightBulbOn both read True on the measured specimen, so a gate keyed on
        # either calls a broken reference healthy - only occ.component raising catches it.
        occ = _BrokenOcc()
        assert occ.isValid is True and occ.isLightBulbOn is True
        assert common.broken_reference(occ)[0] is True


class TestOccurrenceWalk:
    def test_zero_broken_on_the_fast_path_reports_the_fast_walk_and_no_broken_rows(self):
        a, b = _plain_occ("Frame:1"), _plain_occ("Bolt:1")
        walk = common.occurrence_walk(_walk_design(fast=[a, b]))
        assert walk.method == "allOccurrences"
        assert walk.broken == [] and walk.total == 2
        assert walk.occurrences == [a, b] and walk.complete is True

    def test_a_raising_walk_with_zero_broken_still_reports_the_honest_count(self):
        # the boundary that matters most: the walk RAISED, nothing is broken, and the census must be
        # the real number over the 'recursed' marker - never 0.
        bolt = _plain_occ("Bolt:1")
        frame = _plain_occ("Frame:1", children=[bolt])
        walk = common.occurrence_walk(_walk_design(top=[frame]))
        assert walk.method == "recursed"
        assert walk.total == 2 and walk.broken == []
        assert [o.name for o in walk.occurrences] == ["Frame:1", "Bolt:1"]

    def test_one_broken_child_is_found_named_and_counted(self):
        broken = _BrokenOcc("45740")
        container = _plain_occ("Op1 Workholding Container:1",
                              children=[_plain_occ("48205-125 (1):1")],
                              broken_children=[broken])
        walk = common.occurrence_walk(_walk_design(top=[container]))
        assert walk.method == "recursed"
        assert len(walk.broken) == 1
        row = walk.broken[0]
        assert row["name"] == "45740"
        assert row["parent_path"] == "Op1 Workholding Container:1"
        assert row["detail"] == _UNAVAILABLE
        # counted, and kept OUT of the usable rows (every geometry read on it raises)
        assert walk.total == 3
        assert broken not in walk.occurrences
        assert walk.broken_occurrences == [broken]

    def test_a_broken_TOP_LEVEL_occurrence_is_found_too(self):
        walk = common.occurrence_walk(
            _walk_design(top=[_plain_occ("Frame:1")], broken_top=[_BrokenOcc("45740")]))
        assert walk.names() == ["45740"]
        assert walk.broken[0]["parent_path"] == "Root"
        assert walk.total == 2

    def test_a_broken_occurrence_whose_NAME_raises_is_still_a_row(self):
        walk = common.occurrence_walk(
            _walk_design(broken_top=[_BrokenOcc(name_raises=True)]))
        row = walk.broken[0]
        assert row["name_readable"] is False
        assert row["name"] == "(unreadable name)"
        assert row["parent_path"] == "Root"

    def test_a_nested_broken_row_carries_its_parent_PATH(self):
        broken = _BrokenOcc("45740")
        inner = _plain_occ("Sub:1", broken_children=[broken])
        outer = _plain_occ("Op 2 Workholding:1", children=[inner])
        walk = common.occurrence_walk(_walk_design(top=[outer]))
        assert walk.broken[0]["parent_path"] == "Op 2 Workholding:1+Sub:1"

    def test_BOTH_walks_unreadable_is_null_not_zero(self):
        # the cardinal case: nothing enumerated. total is None so no caller can publish 0 with a
        # truncation flag denying anything was lost.
        class _Root:
            name = "Root"
            occurrences = _RaisingColl()

            @property
            def allOccurrences(self):
                raise RuntimeError(_WALK_RAISE)

        walk = common.occurrence_walk(types.SimpleNamespace(rootComponent=_Root()))
        assert walk.method == "unreadable"
        assert walk.total is None and walk.readable is False
        assert walk.occurrences == [] and walk.broken == []

    def test_an_unreadable_SUBTREE_marks_the_census_incomplete_not_short(self):
        good = _plain_occ("Frame:1")
        good.childOccurrences = _RaisingColl()
        walk = common.occurrence_walk(_walk_design(top=[good]))
        assert walk.method == "recursed"
        assert walk.complete is False        # a count from here is a LOWER BOUND, and says so
        assert walk.total == 1

    def test_no_design_and_no_root_are_unreadable_not_empty(self):
        assert common.occurrence_walk(None).method == "unreadable"
        assert common.occurrence_walk(None).total is None

    def test_cap_bounds_the_rows_but_never_the_census(self):
        occs = [_plain_occ(f"P{i}:1") for i in range(5)]
        walk = common.occurrence_walk(_walk_design(fast=occs), cap=2)
        assert len(walk.occurrences) == 2
        assert walk.total == 5

    def test_all_occurrences_survives_a_raising_walk_instead_of_returning_empty(self):
        # the defect this replaces: safe(root.allOccurrences) or [] published an empty assembly.
        frame = _plain_occ("Frame:1", children=[_plain_occ("Bolt:1")])
        assert [o.name for o in common.all_occurrences(_walk_design(top=[frame]))] == [
            "Frame:1", "Bolt:1"]

    def test_component_walk_runs_over_ANY_component_subtree(self):
        class _Sub:
            name = "Sub"
            occurrences = _NamedCollection([_plain_occ("Bolt:1")])

            @property
            def allOccurrences(self):
                raise RuntimeError(_WALK_RAISE)

        walk = common.component_walk(_Sub())
        assert walk.method == "recursed" and walk.total == 1


class TestRootBodyAdvisory:
    def test_fires_when_the_active_component_is_a_DIFFERENT_WRAPPER_of_the_root(self):
        # THE case an identity test gets wrong: comp and design.rootComponent denote the same
        # component but are different objects, so `comp is not d.rootComponent` reads True and the
        # advisory silently never fires at all.
        root = _root(bodies=1)
        design = _FakeDesignWithRoot(root)
        comp = design.rootComponent            # a wrapper, not the object design holds
        assert comp is not root
        assert "ROOT component" in common.root_body_advisory(design, comp)

    def test_silent_when_a_real_sub_component_is_active(self):
        design = _FakeDesignWithRoot(_root())
        assert common.root_body_advisory(design, _root(name="Sub", token="TOKEN:Sub")) == ""

    def test_silent_once_the_root_holds_several_bodies(self):
        root = _root(bodies=2)
        design = _FakeDesignWithRoot(root)
        assert common.root_body_advisory(design, design.rootComponent) == ""

    def test_silent_once_the_design_has_sub_components(self):
        root = _root(bodies=1)
        design = _FakeDesignWithRoot(root, occurrence_count=1)
        assert common.root_body_advisory(design, design.rootComponent) == ""

    def test_silent_without_a_component(self):
        assert common.root_body_advisory(_FakeDesignWithRoot(_root()), None) == ""


# ── all_meshes: the ONE design-wide mesh walk, reached through COMPONENTS ────

def _comp_with_meshes(name, mesh_names=(), meshes=None):
    """A component whose meshBodies collection holds the named meshes. `meshes` overrides the
    collection outright (a raiser / a collection yielding None) for the degradation tests."""
    coll = _NamedCollection([type("M", (), {"name": n})() for n in mesh_names]) if meshes is None else meshes
    return type("C", (), {"name": name, "meshBodies": coll})()


class TestAllMeshes:
    def test_walks_every_component_not_just_the_root(self):
        # A mesh imported into a sub-component is design-wide reachable: the walk goes through
        # all_components, so no occurrence needs to exist for the mesh to be found.
        root = _comp_with_meshes("Root")
        sub = _comp_with_meshes("Scan", ["ScanMesh"])
        pairs = common.all_meshes(_design_with(root, [sub]))
        assert [(c.name, m.name) for c, m in pairs] == [("Scan", "ScanMesh")]

    def test_each_mesh_is_paired_with_its_owning_component(self):
        root = _comp_with_meshes("Root", ["A"])
        sub = _comp_with_meshes("Scan", ["B", "C"])
        pairs = common.all_meshes(_design_with(root, [sub]))
        assert [(c.name, m.name) for c, m in pairs] == [
            ("Root", "A"), ("Scan", "B"), ("Scan", "C")]

    def test_a_component_with_an_unreadable_collection_does_not_sink_the_walk(self):
        # A single bad component must not cost every other component's meshes - the survivor check
        # mesh_delete runs on this walk would otherwise report a deleted mesh as still present.
        class _Raiser:
            @property
            def count(self):
                raise RuntimeError("meshBodies unreadable")

        bad = _comp_with_meshes("Broken", meshes=_Raiser())
        good = _comp_with_meshes("Scan", ["ScanMesh"])
        pairs = common.all_meshes(_design_with(bad, [good]))
        assert [m.name for _c, m in pairs] == ["ScanMesh"]

    def test_a_none_item_is_skipped(self):
        root = _comp_with_meshes("Root", meshes=_NamedCollection([None, type("M", (), {"name": "Real"})()]))
        assert [m.name for _c, m in common.all_meshes(_design_with(root, []))] == ["Real"]

    def test_no_meshes_anywhere_is_empty(self):
        assert common.all_meshes(_design_with(_comp_with_meshes("Root"), [])) == []


# ── build_path: the label describes the path BUILT, not the handles passed ───

class _BuiltPath:
    """An adsk.fusion.Path stand-in: `count` is the number of edges the built path holds."""

    def __init__(self, count):
        self.count = count


class _UnreadablePath:
    @property
    def count(self):
        raise RuntimeError("count unreadable")


class TestBuildPathLabel:
    """The 'path' string model_sweep / model_pipe / model_pattern_path publish. What one seed handle
    yields is not predictable from the request - chaining follows tangent continuity, so a seed
    expands to whatever stays tangent (a closed tangent loop chains fully) and stops at a sharp
    corner - so the count must come off the built Path, never off the input."""

    def _stub_handles(self, monkeypatch, n):
        edges = [type("E", (), {})() for _ in range(n)]
        inputs = load_tool("_inputs")
        monkeypatch.setattr(inputs, "GeometryHandleList",
                            lambda *a, **kw: SimpleNamespace(
                                resolve=lambda handles: (edges, None)))
        return edges

    def _comp(self, built):
        return SimpleNamespace(features=SimpleNamespace(
            createPath=lambda seed, is_chain: built))

    def test_a_seed_that_expanded_reports_the_built_count(self, monkeypatch):
        self._stub_handles(monkeypatch, 1)
        _p, label, err = common.build_path(self._comp(_BuiltPath(14)), "EDGE1")
        assert err is None
        assert label == "14 edge(s) from 1 seed handle"

    def test_a_seed_that_did_not_expand_reports_one(self, monkeypatch):
        self._stub_handles(monkeypatch, 1)
        _p, label, err = common.build_path(self._comp(_BuiltPath(1)), "EDGE1")
        assert err is None and label == "1 edge(s) from 1 seed handle"

    def test_unreadable_count_says_so_instead_of_echoing_the_input(self, monkeypatch):
        self._stub_handles(monkeypatch, 1)
        _p, label, err = common.build_path(self._comp(_UnreadablePath()), "EDGE1")
        assert err is None
        assert label == "from 1 seed handle; edge count unreadable"

    def test_several_handles_are_used_exactly(self, monkeypatch):
        import adsk.fusion
        self._stub_handles(monkeypatch, 3)
        monkeypatch.setattr(adsk.fusion.Path, "create",
                            staticmethod(lambda coll, opts: _BuiltPath(3)))
        _p, label, err = common.build_path(self._comp(None), ["E1", "E2", "E3"])
        assert err is None and label == "3 edge(s) from 3 handles, used exactly"

    def test_the_docstring_states_the_tangent_continuity_rule(self):
        # build_path's own docstring is what an author reads before wiring it (the helper-map
        # catalog line points here): it must promise neither unconditional chaining nor its
        # opposite (a tangent-continuous CLOSED loop chained all 8 edges from one seed) - only
        # tangent continuity, and the built count as the answer.
        doc = common.build_path.__doc__
        assert "TANGENT connections" in doc
        assert "sharp corner stops the chain" in doc
        assert "count is the truth" in doc
        assert "auto-chain" not in doc.lower()
        assert "closed loop" not in doc.lower() and "seed edge alone" not in doc


class TestApplyRename:
    """apply_rename - the ONE create-flow rename-with-disclosure: a declined or deduped rename is
    returned as a warning beside the ACTUAL name, never swallowed and never an error."""

    def test_a_clean_rename_returns_the_new_name_and_no_warning(self):
        ent = SimpleNamespace(name="Sketch1")
        final, warning = common.apply_rename(ent, "Pocket Outline")
        assert final == "Pocket Outline" and warning is None
        assert ent.name == "Pocket Outline"

    def test_the_name_it_already_reads_is_never_written_again(self):
        # Writing the name an entity ALREADY carries makes the platform dedupe it against ITSELF
        # (a CAM operation auto-named 'Face1' renamed to 'Face1' lands 'Face11', measured), so the
        # write is skipped entirely - and it is a clean landing, not a warned dedupe.
        class Deduping:
            def __init__(self):
                self._n = "Face1"
                self.writes = 0

            @property
            def name(self):
                return self._n

            @name.setter
            def name(self, v):
                self.writes += 1
                self._n = str(v) + "1"

        ent = Deduping()
        final, warning = common.apply_rename(ent, "Face1")
        assert final == "Face1" and warning is None
        assert ent.writes == 0 and ent.name == "Face1"

    def test_a_different_name_is_still_written(self):
        # the other side of the skip: it may only fire on the name already read, or renaming stops
        ent = SimpleNamespace(name="Face1")
        final, warning = common.apply_rename(ent, "Rough Pocket")
        assert final == "Rough Pocket" and warning is None and ent.name == "Rough Pocket"

    def test_an_empty_request_renames_nothing_and_warns_nothing(self):
        ent = SimpleNamespace(name="Joint1")
        for req in ("", "   ", None):
            final, warning = common.apply_rename(ent, req)
            assert final == "Joint1" and warning is None

    def test_a_raising_rename_is_disclosed_with_the_kept_name(self):
        class Stubborn:
            @property
            def name(self):
                return "Joint1"

            @name.setter
            def name(self, v):
                raise RuntimeError("3 : name is read-only here")

        final, warning = common.apply_rename(Stubborn(), "Hinge")
        assert final == "Joint1"
        assert "Hinge" in warning and "Joint1" in warning and "failed" in warning

    def test_a_silently_swallowed_rename_is_disclosed_not_reported_as_taken(self):
        # the FR-13 shape: entity.name = x raises nothing and changes nothing - only the
        # read-back catches it, and the warning names both the request and what the entity holds
        class Swallowing:
            @property
            def name(self):
                return "Sketch1"

            @name.setter
            def name(self, v):
                pass

        final, warning = common.apply_rename(Swallowing(), "Outline")
        assert final == "Sketch1"
        assert "Outline" in warning and "did not take" in warning

    def test_a_deduped_landing_reports_the_variant_that_landed(self):
        # the platform dedupes a colliding name ('Foo' -> 'Foo(1)'): the payload's name is the
        # read-back, and the warning says the requested name is not what it holds
        class Deduping:
            def __init__(self):
                self._n = "Body1"

            @property
            def name(self):
                return self._n

            @name.setter
            def name(self, v):
                self._n = v + "(1)"

        final, warning = common.apply_rename(Deduping(), "Bracket")
        assert final == "Bracket(1)"
        assert "Bracket" in warning and "Bracket(1)" in warning


# ── timeline_health: the delta is keyed on identity, not on the repeated name ────────────────────

def _row(name, health, token=None):
    """One timeline row: the three properties timeline_health reads. A row with no token models a
    TimelineGroup / a feature class with no public-API entity, both of which answer entity as None.
    """
    entity = SimpleNamespace(entityToken=token) if token is not None else None
    return SimpleNamespace(name=name, healthState=health, entity=entity)


def _timeline(*rows):
    return type("D", (), {"timeline": _NamedCollection(rows)})()


class TestTimelineHealth:
    """timeline_health hands back NAMES, but timeline names repeat across components (two 'Sketch1',
    two 'Extrude1' in a two-component design), so the delta the guards compute over those lists is
    keyed on the row's entity token instead."""

    def test_health_states_split_into_errors_and_warnings(self):
        errors, warnings, total = common.timeline_health(_timeline(
            _row("Sketch1", 0, "T:a"), _row("Extrude1", 2, "T:b"), _row("Fillet1", 1, "T:c")))
        assert list(errors) == ["Extrude1"] and list(warnings) == ["Fillet1"] and total == 3

    def test_a_design_with_no_timeline_reports_nothing(self):
        assert common.timeline_health(type("D", (), {"timeline": None})()) == ([], [], 0)

    def test_limit_bounds_the_walk_to_the_first_rows(self):
        tl = _timeline(_row("Extrude1", 2, "T:a"), _row("Constraint1", 2, "T:b"))
        errors, _w, total = common.timeline_health(tl, limit=1)
        assert list(errors) == ["Extrude1"] and total == 1

    def test_damage_to_a_twin_is_seen_past_an_already_broken_namesake(self):
        # two components each hold an 'Extrude1'; one is already broken, then the OTHER breaks.
        # Keyed on the name, the delta sees a name already present and reports nothing.
        before, _w, _t = common.timeline_health(_timeline(
            _row("Extrude1", 2, "T:a"), _row("Extrude1", 0, "T:b")))
        after, _w, _t = common.timeline_health(_timeline(
            _row("Extrude1", 2, "T:a"), _row("Extrude1", 2, "T:b")))
        assert [n for n in after if n not in before] == ["Extrude1"]
        assert sorted(set(after) - set(before)) == ["Extrude1"]

    def test_a_feature_broken_before_the_edit_is_not_reported_as_new_damage(self):
        # the same row read twice reports the same token, so the guard stays quiet
        rows = (_row("Extrude1", 2, "T:a"), _row("Extrude1", 0, "T:b"))
        before, _w, _t = common.timeline_health(_timeline(*rows))
        after, _w, _t = common.timeline_health(_timeline(*rows))
        assert [n for n in after if n not in before] == []
        assert set(after) - set(before) == set()

    def test_one_twin_healing_while_the_other_breaks_names_the_broken_one(self):
        # the reverse miss: the name is in both lists yet a DIFFERENT feature carries it
        before, _w, _t = common.timeline_health(_timeline(
            _row("Sketch1", 2, "T:a"), _row("Sketch1", 0, "T:b")))
        after, _w, _t = common.timeline_health(_timeline(
            _row("Sketch1", 0, "T:a"), _row("Sketch1", 2, "T:b")))
        assert [n for n in after if n not in before] == ["Sketch1"]

    def test_rows_with_no_readable_token_key_on_their_name(self):
        before, _w, _t = common.timeline_health(_timeline(_row("Group1", 2)))
        after, _w, _t = common.timeline_health(_timeline(_row("Group1", 2), _row("Group2", 2)))
        assert [n for n in after if n not in before] == ["Group2"]

    def test_a_tokenless_row_compares_equal_to_the_plain_name_it_reports(self):
        errors, _w, _t = common.timeline_health(_timeline(_row("Extrude1", 2)))
        assert errors == ["Extrude1"] and set(errors) == {"Extrude1"}

    def test_an_unreadable_name_falls_back_to_the_row_index(self):
        class Nameless:
            healthState = 2
            entity = None

            @property
            def name(self):
                raise RuntimeError("3 : unreadable")

        errors, _w, _t = common.timeline_health(_timeline(Nameless()))
        assert list(errors) == ["#0"]

    def test_the_names_still_cross_the_wire_as_plain_strings(self):
        errors, warnings, _t = common.timeline_health(_timeline(
            _row("Extrude1", 2, "T:a"), _row("Fillet1", 1, "T:b")))
        payload = json.loads(common.ok({"e": errors, "w": warnings})["content"][0]["text"])
        assert payload == {"e": ["Extrude1"], "w": ["Fillet1"]}
        assert ", ".join(errors) == "Extrude1"


# ── the null-feature note WORDING, pinned by literals ────────────────────────────────────────────

class TestNullFeatureNoteWording:
    """The sentences a payload appends when a write's add() came back with no feature object.

    Around twenty tool modules append one of these, and the tests at those sites assert against
    DIRECT_FEATURE_NOTE / null_feature_note() themselves, which pins the WIRING: an assertion built
    from the same constant that produced the string cannot fail when that constant is rewritten.
    So the WORDING is pinned here, once, with literals - a sentence rewritten to claim the opposite
    (the feature was created; the operation was rolled back) is caught here. For the DIRECT and the
    in-scope sentence this class is the only place it is caught; a consumer's routing test happens
    to cover the no-scope one.

    Each sentence carries two loads for the agent reading it: WHY 'feature' is null, and that the
    numbers beside it are a read-back of the model rather than the request that was sent.

    The scope name and the operation word below are deliberately NOT the ones any caller passes:
    the two interpolated arguments are what the seven call sites vary ('combine',
    'face-group generation', 'plane cut', 'reduce', 'remesh', 'conversion', 'repair'), so an
    argument that coincides with a plausible hardcode cannot tell whether the code used the
    argument at all. The two tests also disagree on the operation word, so no single hardcode
    satisfies both.
    """

    _DIRECT = ("This design is in DIRECT mode, where this operation creates no timeline feature "
               "object to name - what is reported here is read back off the model.")
    _IN_SCOPE = ("'feature' is null: this combine ran inside the base-feature edit scope "
                 "'BaseFeature7' that a parametric mesh write requires, and the add returned no "
                 "feature there - the result is read back off the model.")
    _NO_SCOPE = ("'feature' is null and no base-feature scope was opened - the remesh result "
                 "is read back off the model.")

    def test_the_direct_mode_sentence_is_worded_as_pinned(self):
        assert common.DIRECT_FEATURE_NOTE == self._DIRECT

    def test_a_direct_design_gets_that_exact_sentence(self):
        assert common.null_feature_note(SimpleNamespace(designType=0), None, None, "cut") \
            == self._DIRECT

    def test_a_non_direct_design_names_the_scope_that_suppressed_the_feature(self):
        # not direct, so the base-feature scope - not the design's mode - is what the note names.
        note = common.null_feature_note(SimpleNamespace(designType=1), None, "BaseFeature7",
                                        "combine")
        assert note == self._IN_SCOPE
        assert "'BaseFeature7'" in note and "this combine ran" in note
        assert "DIRECT mode" not in note

    def test_a_non_direct_design_with_no_scope_says_so_without_naming_one(self):
        note = common.null_feature_note(SimpleNamespace(designType=1), None, "", "remesh")
        assert note == self._NO_SCOPE
        assert "the remesh result" in note
        assert "BaseFeature" not in note
