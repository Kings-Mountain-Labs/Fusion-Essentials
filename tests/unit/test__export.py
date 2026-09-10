"""Unit tests for ``_export.py`` - the export-to-disk substrate shared by design_export/mesh_export:
filename sanitizing, the component-by-name resolver, the file-landed verifier (and the pre-write
snapshot that makes it THIS call's proof), the bounded doEvents-pumping wait, and the
one-file-per-top-level-occurrence split orchestration.
"""

import os
from types import SimpleNamespace

import adsk
import pytest

from conftest import MakeDesign, load_tool

ex = load_tool("_export")


# ── sanitize ─────────────────────────────────────────────────────────────────

class TestSanitize:
    def test_drops_instance_suffix(self):
        assert ex.sanitize("Loader Arm:1") == "Loader_Arm"

    def test_keeps_safe_chars(self):
        assert ex.sanitize("Part-A_1.v2") == "Part-A_1.v2"

    def test_swaps_illegal_chars(self):
        assert ex.sanitize("A/B\\C:1") == "A_B_C"

    def test_empty_becomes_part(self):
        assert ex.sanitize("") == "part"
        assert ex.sanitize(None) == "part"

    def test_all_illegal_becomes_part(self):
        # base reduces to all-underscore (still non-empty), so it stays underscores, not "part"
        assert ex.sanitize("***") == "___"


class TestStlUnitEnum:
    """The unit key -> DistanceUnits member read both STL writers bake unitType from."""

    def test_every_key_resolves_to_its_distance_units_member(self):
        for key, member in ex.STL_UNIT_MEMBERS.items():
            assert ex.stl_unit_enum(key) is getattr(adsk.fusion.DistanceUnits, member)

    def test_the_map_is_distance_units_not_mesh_units(self):
        # the members are DistanceUnits spellings; MeshUnits' own names would resolve to nothing
        # here, and its mm/cm ints are SWAPPED relative to these - a 10x error on the two
        # commonest units.
        assert all(m.endswith("DistanceUnits") for m in ex.STL_UNIT_MEMBERS.values())

    def test_an_unknown_key_is_none_not_a_guess(self):
        # None is the caller's signal to record a refusal; assigning it would clear the property.
        # This is also the answer on a build carrying no member for a KNOWN key.
        assert ex.stl_unit_enum("parsecs") is None
        assert ex.stl_unit_enum("") is None
        assert ex.stl_unit_enum(None) is None


class TestAppliedPair:
    """The ONE export-options knob writer. Its whole reason for existing is that a bare
    set-then-read-back cannot bite on a property whose factory value already equals the request -
    measured, unitType reads 0 unset and MillimeterDistanceUnits IS 0."""

    class _Opts:
        """An options object with a factory value, optionally deaf to writes on that property."""
        def __init__(self, factory, deaf=False):
            object.__setattr__(self, "knob", factory)
            object.__setattr__(self, "_deaf", deaf)

        def __setattr__(self, k, v):
            if k == "knob" and object.__getattribute__(self, "_deaf"):
                return
            object.__setattr__(self, k, v)

    def test_a_write_that_changes_the_property_is_applied_and_changed(self):
        opts = self._Opts("FACTORY")
        assert ex.applied_pair(opts, "knob", "WANT", "key") == ("key", True)
        assert opts.knob == "WANT"

    def test_a_value_the_property_already_read_is_applied_but_not_changed(self):
        # THE COLLISION: the write is dropped, yet the read-back equals the request because that
        # is the factory value. Landed is honest (the object does read it); changed must be False,
        # because this answers identically whether the assignment took or never happened.
        opts = self._Opts("WANT", deaf=True)
        assert ex.applied_pair(opts, "knob", "WANT", "key") == ("key", False)

    def test_the_same_value_reports_the_same_pair_whether_or_not_the_write_lands(self):
        # The discriminating statement: with the factory value equal to the request, a LANDING
        # write and a DROPPED one are indistinguishable here - so neither may be called verified.
        deaf = ex.applied_pair(self._Opts("WANT", deaf=True), "knob", "WANT", "key")
        live = ex.applied_pair(self._Opts("WANT"), "knob", "WANT", "key")
        assert deaf == live == ("key", False)

    def test_a_dropped_write_the_read_back_can_see_is_not_applied(self):
        opts = self._Opts("OTHER", deaf=True)
        assert ex.applied_pair(opts, "knob", "WANT", "key") == ex.NOT_APPLIED

    def test_a_falsy_requested_value_is_still_a_landed_value(self):
        # MeshRefinementHigh is 0 and stl_binary can be False: a `not val` test here would report
        # a landed knob as refused. The pair keys on the read-back, never on truthiness.
        opts = self._Opts(1)
        assert ex.applied_pair(opts, "knob", 0, "high") == ("high", True)

    def test_an_unreadable_property_is_not_applied(self):
        # every read goes through safe(), so a property that raises is a refusal, not a crash
        class _Raises:
            @property
            def knob(self):
                raise RuntimeError("this build has no such knob")

        assert ex.applied_pair(_Raises(), "knob", "WANT", "key") == ex.NOT_APPLIED

    def test_not_applied_is_a_no_value_no_evidence_pair(self):
        assert ex.NOT_APPLIED == (None, False)


# ── find_component ────────────────────────────────────────────────────────────

class _Comp:
    def __init__(self, name):
        self.name = name


class _Design(MakeDesign):
    """The shared design over this file's root, whose allComponents is the COUNTED collection the
    census walks. An empty design has no allOccurrences, which is what most tests want."""
    def __init__(self, comps, occurrences=()):
        super().__init__(comp=_Comp("Root"), all_components=list(comps))
        self.rootComponent.allOccurrences = list(occurrences)


def _occ(path, comp):
    """One placement: what the census reads off it is its component and its assembly path."""
    return SimpleNamespace(name=path.split("+")[-1], fullPathName=path, component=comp)


class TestFindComponent:
    def test_finds_matching_component(self):
        a, b = _Comp("A"), _Comp("B")
        assert ex.find_component(_Design([a, b]), "B") == (b, None)

    def test_no_match_is_a_plain_miss_not_a_refusal(self):
        # (None, None): the caller words its own not-found error off its own vocabulary
        assert ex.find_component(_Design([_Comp("A")]), "Nope") == (None, None)

    def test_empty_component_list_returns_none(self):
        # all_components falls back to [root] on an empty collection; "A" still misses
        assert ex.find_component(_Design([]), "A") == (None, None)

    def test_a_name_two_components_carry_is_refused_with_the_count(self):
        # the whole point: neither 'Bracket' is the one asked for, so neither is handed back
        comp, err = ex.find_component(_Design([_Comp("Bracket"), _Comp("Bracket")]), "Bracket")
        assert comp is None
        assert "2 components match 'Bracket'" in err
        # both are spelled exactly as asked, so there is nothing to tell them apart by name
        assert "(named" not in err

    def test_a_third_duplicate_is_counted_not_capped_at_two(self):
        comp, err = ex.find_component(
            _Design([_Comp("Jaw"), _Comp("Jaw"), _Comp("Jaw")]), "Jaw")
        assert comp is None and "3 components match 'Jaw'" in err

    def test_one_hit_beside_other_names_still_resolves(self):
        # exactly-one is the resolving case - a duplicate of a DIFFERENT name must not refuse it
        target = _Comp("Frame")
        des = _Design([_Comp("Bolt"), target, _Comp("Bolt")])
        assert ex.find_component(des, "Frame") == (target, None)

    def test_the_refusal_names_the_instances_that_place_each_hit(self):
        # the candidates ARE the remedy: an occurrence fullPathName identifies one of two
        # same-named components, where the name identifies neither
        a, b = _Comp("Bracket"), _Comp("Bracket")
        des = _Design([a, b], occurrences=[_occ("Sub-A:1+Bracket:1", a),
                                           _occ("Sub-B:1+Bracket:1", b)])
        comp, err = ex.find_component(des, "Bracket")
        assert comp is None
        assert "'Sub-A:1+Bracket:1'" in err and "'Sub-B:1+Bracket:1'" in err

    def test_an_instance_of_a_DIFFERENT_component_is_not_listed(self):
        # the list must be the placements of the AMBIGUOUS components only - naming an unrelated
        # occurrence would hand the caller a spelling that resolves to the wrong part
        a, b, other = _Comp("Bracket"), _Comp("Bracket"), _Comp("Frame")
        des = _Design([a, b, other], occurrences=[_occ("Sub-A:1+Bracket:1", a),
                                                  _occ("Frame:1", other)])
        _comp, err = ex.find_component(des, "Bracket")
        assert "'Sub-A:1+Bracket:1'" in err and "Frame:1" not in err

    def test_the_instance_list_is_capped_and_says_how_many_are_hidden(self):
        hits = [_Comp("Pin") for _ in range(10)]
        des = _Design(hits, occurrences=[_occ(f"Sub-{i}:1+Pin:1", c) for i, c in enumerate(hits)])
        _comp, err = ex.find_component(des, "Pin")
        assert "'Sub-7:1+Pin:1'" in err            # the 8th, the last one listed
        assert "'Sub-8:1+Pin:1'" not in err
        assert "(+2 more not listed)" in err       # named_with_remainder's disclosure

    def test_a_placement_whose_path_and_name_both_fail_to_read_is_not_listed(self):
        # neither read answered, so there is no spelling to offer for that one - listing it as ''
        # would hand the caller a candidate that cannot be passed back
        a, b = _Comp("Bracket"), _Comp("Bracket")
        blind = SimpleNamespace(name=None, fullPathName=None, component=a)
        des = _Design([a, b], occurrences=[blind, _occ("Sub-B:1+Bracket:1", b)])
        _comp, err = ex.find_component(des, "Bracket")
        assert "Instances found: 'Sub-B:1+Bracket:1'." in err
        assert "''" not in err

    def test_no_instances_found_means_no_instance_claim(self):
        # nothing placed them, so the refusal must not offer a spelling that was never read
        _comp, err = ex.find_component(_Design([_Comp("Orphan"), _Comp("Orphan")]), "Orphan")
        assert "2 components match 'Orphan'" in err
        assert "Instances found" not in err

    def test_the_refusal_never_asks_for_a_rename(self):
        # components arriving from an inserted/x-ref'd document duplicate names wholesale, and
        # renaming one of those means editing a different document - an unfollowable instruction
        _comp, err = ex.find_component(_Design([_Comp("Rotor"), _Comp("Rotor")]), "Rotor")
        assert "rename" not in err.lower()

    def test_the_refusal_carries_no_remedy_of_its_own(self):
        # the remedy is the caller's, because only the caller knows which of ITS vocabularies is
        # still open; a remedy here would be advice this resolver cannot check
        _comp, err = ex.find_component(_Design([_Comp("Rotor"), _Comp("Rotor")]), "Rotor")
        for unreachable in ("find_geometry", "handle", "design_get", "retry"):
            assert unreachable not in err.lower()

    def test_the_match_ignores_case_and_surrounding_space(self):
        # ONE comparison repo-wide (_common._component_is_named): a spelling that scopes a read in
        # sketch_get must not miss here, or an agent's name works in one tool and not the next
        beta = _Comp("Beta")
        des = _Design([beta, _Comp("Gamma")])
        assert ex.find_component(des, "beta") == (beta, None)
        assert ex.find_component(des, "  BETA  ") == (beta, None)

    def test_the_exact_spelling_still_resolves_against_a_case_variant(self):
        # the widened match must not COST a resolve: 'Beta' names exactly one component exactly, so
        # the presence of a 'BETA' beside it cannot turn that into an ambiguity
        beta, shout = _Comp("Beta"), _Comp("BETA")
        des = _Design([beta, shout])
        assert ex.find_component(des, "Beta") == (beta, None)
        assert ex.find_component(des, "BETA") == (shout, None)

    def test_narrowing_to_the_exact_spelling_only_happens_when_ONE_survives(self):
        # two components spelled 'Bracket' and one 'BRACKET': the exact spelling still names two, so
        # the narrowing must not fire - collapsing to the exact hits would report 2 and drop the
        # third component that answers to the same name
        des = _Design([_Comp("Bracket"), _Comp("Bracket"), _Comp("BRACKET")])
        comp, err = ex.find_component(des, "Bracket")
        assert comp is None
        assert "3 components match 'Bracket'" in err
        assert "(named 'Bracket', 'BRACKET')" in err

    def test_a_spelling_that_matches_BOTH_case_variants_is_ambiguous(self):
        # 'beta' is the spelling of neither, so it names both - the one true ambiguity here
        _comp, err = ex.find_component(_Design([_Comp("Beta"), _Comp("BETA")]), "beta")
        assert _comp is None and "2 components match 'beta'" in err

    def test_the_refusal_quotes_the_names_read_not_the_query(self):
        # no component is named 'beta'; saying so would assert a name nothing carries, and the real
        # spellings are exactly what the caller needs to retry with
        _comp, err = ex.find_component(_Design([_Comp("Beta"), _Comp("BETA")]), "beta")
        assert "(named 'Beta', 'BETA')" in err
        assert "are named 'beta'" not in err

    def test_the_spelling_list_is_deduplicated(self):
        # ten hits, two spellings: the list is what tells them apart, so it carries each spelling
        # once - one entry per HIT would be nine copies of 'Pin'
        des = _Design([_Comp("Pin") for _ in range(9)] + [_Comp("PIN")])
        _comp, err = ex.find_component(des, "pin")
        assert "(named 'Pin', 'PIN')" in err

    def test_the_spelling_list_is_capped_and_counts_the_rest(self):
        # every list a refusal publishes crosses the wire, so none of them is unbounded
        # ten spellings that all answer to 'pin', none of them spelled 'pin' (an exact hit would
        # resolve instead of refusing)
        variants = ["Pin", "PIN", "PiN", "pIn", "PIn", "piN", "pIN", "Pin ", " PIN", "  pin  "]
        _comp, err = ex.find_component(_Design([_Comp(v) for v in variants]), "pin")
        assert "'Pin', 'PIN', 'PiN', 'pIn', 'PIn', 'piN', 'pIN', 'Pin '" in err
        assert "' PIN'" not in err                # the 9th is not listed
        # The remainder is rendered by _common.named_with_remainder, the one capped-wire-list
        # renderer - it names what it dropped, so a truncated list cannot read as the complete set.
        assert "(+2 more not listed)" in err

    def test_a_blank_name_matches_nothing(self):
        # a component whose name does not READ answers "" to the walk; a blank query must not
        # resolve to it
        class _Unreadable:
            @property
            def name(self):
                raise RuntimeError("name unreadable")

        assert ex.find_component(_Design([_Unreadable()]), "") == (None, None)
        assert ex.find_component(_Design([_Unreadable()]), "   ") == (None, None)


# ── verify_written ─────────────────────────────────────────────────────────────

class TestVerifyWritten:
    def test_existing_nonempty_file_passes(self, tmp_path):
        p = tmp_path / "out.step"
        p.write_text("data")
        size, err = ex.verify_written(str(p))
        assert err is None
        assert size == p.stat().st_size > 0

    def test_missing_file_is_an_error(self, tmp_path):
        size, err = ex.verify_written(str(tmp_path / "missing.step"))
        assert size == 0
        assert "no file was written" in err.lower()
        assert "file_exists=False" in err

    def test_empty_file_is_an_error(self, tmp_path):
        p = tmp_path / "empty.step"
        p.write_text("")
        size, err = ex.verify_written(str(p))
        assert size == 0
        assert "no file was written" in err.lower()
        assert "size_bytes=0" in err


class TestSnapshot:
    def test_an_absent_target_is_the_zero_baseline(self, tmp_path):
        assert ex.snapshot(str(tmp_path / "nothing.step")) == (False, 0, 0)

    def test_an_existing_target_carries_its_size_and_mtime(self, tmp_path):
        p = tmp_path / "there.step"
        p.write_text("data")
        exists, size, mtime = ex.snapshot(str(p))
        assert exists is True
        assert size == 4
        assert mtime == p.stat().st_mtime_ns

    def test_a_stat_that_raises_after_isfile_reads_as_existing_but_unmeasured(self, monkeypatch, tmp_path):
        # the race/permission gap between isfile and stat: the baseline must say the file EXISTS
        # (so a later identical state is not mistaken for a fresh write) with zeroed metrics, not
        # crash and not claim absence.
        p = tmp_path / "flaky.step"
        p.write_text("data")
        # *args/**kwargs: ex.os IS the stdlib module, so this patch is process-wide for the
        # test's duration - pytest's own machinery calls os.stat with keyword arguments.
        monkeypatch.setattr(ex.os, "stat",
                            lambda *a, **k: (_ for _ in ()).throw(OSError("locked")))
        assert ex.snapshot(str(p)) == (True, 0, 0)


class TestVerifyWrittenProvesThisCall:
    """A file EXISTING is not proof this export wrote it - a stale file from an earlier export sits
    at the same path and passes an existence-only check."""

    def test_an_untouched_pre_existing_file_is_refused(self, tmp_path):
        p = tmp_path / "stale.step"
        p.write_text("an export from an earlier call")
        before = ex.snapshot(str(p))
        size, err = ex.verify_written(str(p), before)      # nothing wrote in between
        assert size == 0
        assert "already there before this call" in err
        assert str(p) in err

    def test_a_rewrite_that_changes_the_size_passes(self, tmp_path):
        p = tmp_path / "again.step"
        p.write_text("old")
        before = ex.snapshot(str(p))
        p.write_text("a substantially longer export")
        size, err = ex.verify_written(str(p), before)
        assert err is None
        assert size == p.stat().st_size

    def test_a_same_size_rewrite_passes_on_the_timestamp(self, tmp_path):
        # identical byte count, later modification time - the export DID write, so it must not be
        # refused as stale
        p = tmp_path / "same.step"
        p.write_text("1234")
        before = ex.snapshot(str(p))
        later = before[2] + 1_000_000_000
        os.utime(str(p), ns=(later, later))
        size, err = ex.verify_written(str(p), before)
        assert err is None and size == 4

    def test_a_first_time_write_needs_no_change(self, tmp_path):
        p = tmp_path / "new.step"
        before = ex.snapshot(str(p))
        assert before == (False, 0, 0)
        p.write_text("fresh")
        size, err = ex.verify_written(str(p), before)
        assert err is None and size == 5

    def test_without_a_baseline_the_weaker_existence_check_still_applies(self, tmp_path):
        # a caller with no pre-write state (a redundant re-stat) keeps the exists-and-non-empty gate
        p = tmp_path / "stale2.step"
        p.write_text("old")
        assert ex.verify_written(str(p))[1] is None


class TestFailureDetail:
    def test_names_each_occurrence_and_its_reason(self):
        line = ex.failure_detail([{"occurrence": "A:1", "error": "STL export returned false"},
                                  {"occurrence": "B:1", "error": "disk full"}])
        assert "A:1: STL export returned false" in line
        assert "B:1: disk full" in line

    def test_a_long_list_is_bounded_with_a_remainder_count(self):
        errs = [{"occurrence": f"P{i}:1", "error": "nope"} for i in range(9)]
        line = ex.failure_detail(errs, limit=3)
        assert line.count("nope") == 3
        assert "(+6 more)" in line

    def test_an_unnamed_occurrence_is_still_reported(self):
        assert "(unnamed occurrence)" in ex.failure_detail([{"occurrence": None, "error": "boom"}])


# ── pump_until ────────────────────────────────────────────────────────────────

class _Clock:
    """A deterministic stand-in for the time module: sleep advances the clock instead of blocking,
    so the bound is exercised in exact steps rather than against the wall clock."""

    def __init__(self):
        self.now = 0.0
        self.slept = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


@pytest.fixture
def wait(monkeypatch):
    """pump_until on a fake clock with a counted pump. Returns (call, clock, pumps)."""
    clock = _Clock()
    pumps = []
    monkeypatch.setattr(ex, "time", clock)
    monkeypatch.setattr(adsk, "doEvents", lambda: pumps.append(1), raising=False)
    return (lambda probe, timeout_s=1.0, poll_sleep=0.25:
            ex.pump_until(probe, timeout_s, poll_sleep)), clock, pumps


class TestPumpUntil:
    def test_an_already_settled_probe_costs_no_pump(self, wait):
        call, clock, pumps = wait
        assert call(lambda: (True, "landed")) == (True, "landed")
        assert pumps == [] and clock.slept == []

    def test_the_probe_reruns_after_every_pump_until_it_settles(self, wait):
        call, clock, pumps = wait
        readings = iter([(False, "absent"), (False, "0 bytes"), (True, "720 bytes")])
        assert call(lambda: next(readings)) == (True, "720 bytes")
        assert pumps == [1, 1]                      # one pump between each pair of probes
        assert clock.slept == [0.25, 0.25]

    def test_gives_up_at_the_bound_and_hands_back_the_last_reading(self, wait):
        call, clock, pumps = wait
        seen = []

        def probe():
            seen.append(len(seen))
            return False, f"still growing {len(seen)}"

        settled, reading = call(probe, timeout_s=1.0, poll_sleep=0.25)
        assert settled is False
        assert reading == "still growing 5"         # probes at 0.00 0.25 0.50 0.75 1.00
        assert len(pumps) == 4 and clock.now == 1.0

    def test_a_zero_bound_still_probes_once_before_giving_up(self, wait):
        # the bound is checked BETWEEN the probe and the pump, so a wait with no budget left still
        # reports what is on disk rather than a blind failure.
        call, clock, pumps = wait
        probes = []

        def probe():
            probes.append(1)
            return False, "absent"

        assert call(probe, timeout_s=0.0) == (False, "absent")
        assert probes == [1] and pumps == []

    def test_a_pump_that_raises_does_not_sink_the_wait(self, wait, monkeypatch):
        # doEvents is a live Fusion call; one bad pump must not turn a landing into an exception.
        call, _clock, _pumps = wait

        def boom():
            raise RuntimeError("pump exploded")

        monkeypatch.setattr(adsk, "doEvents", boom, raising=False)
        readings = iter([(False, "absent"), (True, "landed")])
        assert call(lambda: next(readings)) == (True, "landed")


# ── top_level_occurrences ──────────────────────────────────────────────────────

class _Occs:
    def __init__(self, items):
        self._items = list(items)
        self.count = len(self._items)

    def item(self, i):
        return self._items[i]


class _Root:
    def __init__(self, occs):
        self.occurrences = occs


class _OccDesign:
    def __init__(self, occs):
        self.rootComponent = _Root(occs)


class TestTopLevelOccurrences:
    def test_lists_every_occurrence_in_order(self):
        o1, o2 = object(), object()
        occs = ex.top_level_occurrences(_OccDesign(_Occs([o1, o2])))
        assert occs == [o1, o2]

    def test_no_occurrences_is_empty_list(self):
        # A read that SUCCEEDED and found none - the answer a split export refuses on by name.
        assert ex.top_level_occurrences(_OccDesign(_Occs([]))) == []

    def test_unreadable_occurrences_collection_is_none_not_empty(self):
        # [] would say "this design has no components" about a census that never happened, and a
        # split export over [] reports a clean zero-file result.
        class _BadRoot:
            @property
            def occurrences(self):
                raise RuntimeError("boom")
        design = _OccDesign(_Occs([]))
        design.rootComponent = _BadRoot()
        assert ex.top_level_occurrences(design) is None

    def test_an_unreadable_root_is_none(self):
        class _BadDesign:
            @property
            def rootComponent(self):
                raise RuntimeError("no design")
        assert ex.top_level_occurrences(_BadDesign()) is None

    def test_an_unreadable_count_is_none(self):
        class _BadCount:
            @property
            def count(self):
                raise RuntimeError("boom")
            def item(self, i):
                return object()
        assert ex.top_level_occurrences(_OccDesign(_BadCount())) is None

    def test_a_count_that_is_not_a_number_is_none(self):
        # An unmodelled property hands back a truthy object, not a count; range() over it would
        # raise, and treating it as 0 would report an empty design.
        design = _OccDesign(_Occs([]))
        design.rootComponent.occurrences.count = object()
        assert ex.top_level_occurrences(design) is None

    def test_one_unreadable_item_sinks_the_whole_census(self):
        # N-1 occurrences would export as if they were the whole design, with no sign of the hole.
        class _HoleyOccs:
            count = 2
            def item(self, i):
                if i == 1:
                    raise RuntimeError("boom")
                return "o1"
        assert ex.top_level_occurrences(_OccDesign(_HoleyOccs())) is None


# ── split_by_occurrence ────────────────────────────────────────────────────────

class _Occ:
    def __init__(self, name):
        self.name = name


class TestSplitByOccurrence:
    def test_one_record_per_occurrence_named_and_extensioned(self):
        occs = [_Occ("Body:1"), _Occ("Cab:1")]
        calls = []

        def write_one(occ, path):
            calls.append((occ.name, path))
            return 42, None

        files, errors = ex.split_by_occurrence(occs, "C:/out", ".stl", write_one)
        assert errors == []
        assert [f["occurrence"] for f in files] == ["Body:1", "Cab:1"]
        assert all(f["file_path"].endswith(".stl") for f in files)
        assert all(f["size_bytes"] == 42 for f in files)
        assert [c[0] for c in calls] == ["Body:1", "Cab:1"]

    def test_duplicate_stems_disambiguated(self):
        occs = [_Occ("Wheel:1"), _Occ("Wheel:2")]
        files, errors = ex.split_by_occurrence(occs, "C:/out", ".stl",
                                               lambda occ, path: (1, None))
        paths = [f["file_path"].replace("\\", "/") for f in files]
        assert paths[0].endswith("/Wheel.stl")
        assert paths[1].endswith("/Wheel_2.stl")

    def test_failure_lands_in_errors_not_files(self):
        occs = [_Occ("Good:1"), _Occ("Bad:1")]

        def write_one(occ, path):
            if occ.name == "Bad:1":
                return None, "it exploded"
            return 5, None

        files, errors = ex.split_by_occurrence(occs, "C:/out", ".stl", write_one)
        assert [f["occurrence"] for f in files] == ["Good:1"]
        assert errors == [{"occurrence": "Bad:1", "error": "it exploded"}]

    def test_generated_suffix_collision_keeps_each_disk_payload(self, tmp_path):
        payloads = {"A:1": b"first", "A:2": b"second", "A_2:1": b"third"}

        def write_one(occ, path):
            data = payloads[occ.name]
            with open(path, "wb") as handle:
                handle.write(data)
            return len(data), None

        files, errors = ex.split_by_occurrence(
            [_Occ("A:1"), _Occ("A:2"), _Occ("A_2:1")], str(tmp_path), ".stl", write_one
        )

        assert errors == []
        assert [os.path.basename(f["file_path"]) for f in files] == [
            "A.stl", "A_2.stl", "A_2_2.stl"
        ]
        assert (tmp_path / "A.stl").read_bytes() == b"first"
        assert (tmp_path / "A_2.stl").read_bytes() == b"second"
        assert (tmp_path / "A_2_2.stl").read_bytes() == b"third"

    def test_case_variant_paths_are_reserved_case_insensitively(self, tmp_path, monkeypatch):
        original_normcase = ex.os.path.normcase
        monkeypatch.setattr(ex.os.path, "normcase", lambda path: original_normcase(path).lower())

        def write_one(occ, path):
            data = occ.name.encode("utf-8")
            with open(path, "wb") as handle:
                handle.write(data)
            return len(data), None

        files, errors = ex.split_by_occurrence(
            [_Occ("Part:1"), _Occ("part:1")], str(tmp_path), ".stl", write_one
        )

        assert errors == []
        assert [os.path.basename(f["file_path"]) for f in files] == ["Part.stl", "part_2.stl"]
        assert (tmp_path / "Part.stl").read_bytes() == b"Part:1"
        assert (tmp_path / "part_2.stl").read_bytes() == b"part:1"

    def test_failed_writer_attempt_still_reserves_its_path(self, tmp_path):
        calls = []

        def write_one(occ, path):
            calls.append(path)
            if occ.name == "Part:1":
                return None, "write failed"
            data = b"landed"
            with open(path, "wb") as handle:
                handle.write(data)
            return len(data), None

        files, errors = ex.split_by_occurrence(
            [_Occ("Part:1"), _Occ("Part:2")], str(tmp_path), ".stl", write_one
        )

        assert errors == [{"occurrence": "Part:1", "error": "write failed"}]
        assert files[0]["file_path"].endswith("Part_2.stl")
        assert calls[0].endswith("Part.stl")
        assert (tmp_path / "Part_2.stl").read_bytes() == b"landed"

    def test_empty_occurrence_list_yields_nothing(self):
        files, errors = ex.split_by_occurrence([], "C:/out", ".stl", lambda occ, path: (1, None))
        assert files == [] and errors == []
