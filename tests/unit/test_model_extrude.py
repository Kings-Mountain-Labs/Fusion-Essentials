"""Unit tests for ``extrude.py`` — turn a sketch profile into a solid.

The logic worth pinning (no live Fusion): units → cm scaling, the zero-distance
and unknown-operation/units guards, sketch + profile resolution (named vs. most
recent; profile_index bounds), the operation-name → FeatureOperations mapping,
and that the distance handed to the API is scaled. The actual feature creation is
captured on a fake ExtrudeFeatures so we can assert the profile/operation/extent
passed in, without a real design.
"""

import json
import types

import adsk.fusion

from conftest import (
    BRepBody, BRepFace, FakeFeature as _SharedFeature, FakeUnitsManager, MakeComp, Profile,
    _NamedCollection, install, load_tool, make_bbox, make_design, make_sketch, payload as _payload,
)

ex = load_tool("model_extrude")


# ── fakes ───────────────────────────────────────────────────────────────────

def _sketch(name, profile_count=1, curve_count=0, profiles=None, text_count=0,
            compute_deferred=False):
    """A sketch whose profiles are opaque tokens unless a test hands in ones carrying geometry.

    sketchTexts is the 'text:<i>' address space; each text is tagged '<sketch>#<i>' so a test can
    tell WHICH one resolved.
    """
    regions = (list(profiles) if profiles is not None
               else [("profile", i) for i in range(profile_count)])
    sk = make_sketch(name=name, profiles=regions, is_compute_deferred=compute_deferred,
                     lines=[("curve", i) for i in range(curve_count)])
    sk.sketchTexts = _NamedCollection([types.SimpleNamespace(tag=f"{name}#{i}")
                                       for i in range(text_count)])
    return sk


class FakeExtrudeInput:
    def __init__(self, profile, operation):
        self.profile = profile
        self.operation = operation
        self.distance_extent = None     # (isSymmetric, ValueInput) captured
        self.one_side = None
        self.symmetric_extent = None    # (distance, isFullLength, taper) captured
        self.all_extent = None          # direction captured (the RETIRED setAllExtent)
        self.two_sides_extent = None    # (sideOne, sideTwo, taperOne, taperTwo) captured
        self.two_sides_distance = None  # (distanceOne, distanceTwo) captured (extent=two_side)
        self.participantBodies = None
        self.isSolid = True             # default solid; surface path sets this False
        # EVERY extent setter is documented "Returns true if successful" - the fake returns the
        # bool as live does, so a handler that ignores it has something to be caught ignoring.
        self.next_result = True

    def setDistanceExtent(self, isSymmetric, distance):
        self.distance_extent = (isSymmetric, distance)
        return self.next_result

    def setOneSideExtent(self, extent, direction, taper=None):
        self.one_side = (extent, direction, taper)
        return self.next_result

    def setSymmetricExtent(self, distance, isFullLength, taper=None):
        self.symmetric_extent = (distance, isFullLength, taper)
        return self.next_result

    def setAllExtent(self, direction):
        # The RETIRED through-all setter: modelled so a test can pin that it is never called.
        self.all_extent = direction
        return self.next_result

    def setTwoSidesExtent(self, sideOne, sideTwo, taperOne=None, taperTwo=None):
        self.two_sides_extent = (sideOne, sideTwo, taperOne, taperTwo)
        return self.next_result

    def setTwoSidesDistanceExtent(self, distanceOne, distanceTwo):
        self.two_sides_distance = (distanceOne, distanceTwo)
        return self.next_result


class FakeFeature(_SharedFeature):
    """The shared feature plus isSolid - the solid/surface mode the read-back reports."""
    def __init__(self, name="Extrude1", is_solid=True):
        super().__init__(name=name, bodies=[BRepBody("Body1", is_solid=is_solid)])
        self.isSolid = is_solid


class FakeExtrudeFeatures:
    def __init__(self):
        self.last_input = None
        self.added = False
        self.next_result = True   # propagated onto every extent setter of each new input
        self.on_add = None        # optional callable(inp) - simulates a live body mutation from add()

    def createInput(self, profile, operation):
        self.last_input = FakeExtrudeInput(profile, operation)
        self.last_input.next_result = self.next_result
        return self.last_input

    def add(self, inp):
        self.added = True
        if self.on_add:
            self.on_add(inp)
        # mirror the input's solid/surface mode onto the resulting feature (read back as is_solid)
        return FakeFeature(is_solid=getattr(inp, "isSolid", True))


def _component(name, sketches=(), bodies=(), ef=None):
    """A component holding `sketches`, plus the extrudeFeatures collection and the open-profile
    factory when it is the one the handler builds in."""
    comp = MakeComp(name=name, sketches=list(sketches), bodies=list(bodies))
    if ef is not None:
        comp.features = types.SimpleNamespace(extrudeFeatures=ef)
        comp.createOpenProfile = lambda curves, chain: ("open_profile", curves, chain)
    return comp


def _wire_adsk():
    """Model the geometry factories the handler reaches for - FeatureOperations arrives seeded."""
    import adsk.fusion, adsk.core
    adsk.core.ValueInput.createByReal = staticmethod(lambda v: ("real", v))
    adsk.core.ValueInput.createByString = staticmethod(lambda s: ("str", s))
    adsk.fusion.ToEntityExtentDefinition.create = staticmethod(lambda face, chained: ("to", face, chained))
    # A FRESH definition object per call, so a test can tell one shared object from two real ones.
    adsk.fusion.ThroughAllExtentDefinition.create = staticmethod(lambda: ["through_all"])


def _install(sketches):
    ef = FakeExtrudeFeatures()
    install(ex, make_design(comp=_component("Root", sketches=sketches, ef=ef)))
    _wire_adsk()
    return ef


# ── guards ───────────────────────────────────────────────────────────────────

class TestGuards:
    def test_unknown_units(self):
        _install([_sketch("S")])
        res = ex.handler(sketch_name="S", distance=5, units="furlongs")
        assert res["isError"] is True and "Unknown units" in res["message"]

    def test_zero_distance(self):
        _install([_sketch("S")])
        res = ex.handler(sketch_name="S", distance=0)
        assert res["isError"] is True and "non-zero 'distance'" in res["message"]

    def test_unknown_operation(self):
        _install([_sketch("S")])
        res = ex.handler(sketch_name="S", distance=5, operation="weld")
        assert res["isError"] is True and "Unknown operation" in res["message"]

    def test_no_sketch_named(self):
        _install([_sketch("S")])
        res = ex.handler(sketch_name="Nope", distance=5)
        assert res["isError"] is True and "No sketch named 'Nope'" in res["message"]

    def test_a_padded_name_reports_the_name_the_walk_searched_for(self):
        # the resolver STRIPS the name before searching, so the miss quotes the stripped form -
        # echoing the raw input names a sketch nothing ever looked for.
        _install([_sketch("S")])
        res = ex.handler(sketch_name="  Ghost  ", distance=5)
        assert res["isError"] is True
        assert "No sketch named 'Ghost'" in res["message"]
        assert "'  Ghost  '" not in res["message"]

    def test_a_blank_name_with_no_sketch_in_the_design_never_quotes_None(self):
        # a blank name asks for the MOST RECENT sketch, so there is no requested name to quote:
        # the named-miss branch would render the absent name as the literal string 'None'.
        _install([])
        res = ex.handler(distance=5)
        assert res["isError"] is True
        assert "'None'" not in res["message"]
        assert res["message"] == "No sketch to extrude. Create one and draw a closed profile first."

    def test_a_whitespace_only_name_takes_the_most_recent_sketch(self):
        # ' ' strips to blank, which means the most recent sketch - searching for a space instead
        # misses every sketch and reports a name no caller typed.
        _install([_sketch("First"), _sketch("Last")])
        res = ex.handler(sketch_name=" ", distance=5)
        assert "No sketch named" not in json.dumps(res)
        assert _payload(res)["sketch"] == "Last"

    def test_a_shared_sketch_name_is_refused_with_its_owners(self, monkeypatch):
        # Two components can each hold an "S". The refusal names them; calling that
        # "No sketch named 'S'" states the opposite of what the design-wide walk read.
        _install([_sketch("S")])
        refusal = "2 sketches are named 'S' ('S' in Root, 'S' in Frame)"
        monkeypatch.setattr(ex._sketch_detail, "scoped_or_recent_sketch",
                            lambda d, n, c, input_name="component": (None, n, refusal))
        res = ex.handler(sketch_name="S", distance=5)
        assert res["isError"] is True
        assert res["message"] == refusal and "No sketch named" not in res["message"]

    def test_the_component_scope_is_declared_on_the_wire(self, monkeypatch):
        # the schema is strict, so a handler parameter no property declares is refused before it
        # reaches the handler - the scope would be unreachable and its refusal would name it anyway.
        sd = load_tool("_sketch_detail")
        assert ex.extrude_tool.input_schema["properties"]["component"] == sd.COMPONENT_SCOPE[1]

    def test_the_component_scope_reaches_the_sketch_resolve(self, monkeypatch):
        _install([_sketch("S")])
        seen = {}

        def _scoped(d, n, c, input_name="component"):
            seen.update(component=c, input_name=input_name)
            return None, n, "refused"

        monkeypatch.setattr(ex._sketch_detail, "scoped_or_recent_sketch", _scoped)
        ex.handler(sketch_name="S", distance=5, component="Frame")
        assert seen == {"component": "Frame", "input_name": "component"}

    def test_profile_index_out_of_range(self):
        _install([_sketch("S", profile_count=1)])
        res = ex.handler(sketch_name="S", distance=5, profile_index=3)
        assert res["isError"] is True and "out of range" in res["message"]

    def test_no_profile_in_sketch(self):
        # No closed profile AND no curves -> an error that points at the surface path, not a
        # flat "no closed profile" dead-end.
        _install([_sketch("S", profile_count=0)])
        res = ex.handler(sketch_name="S", distance=5)
        assert res["isError"] is True and "no curves" in res["message"]

    def test_an_index_into_a_deferred_sketch_is_refused(self):
        # The bare-integer path indexes sketch.profiles directly and never reaches ProfileRef, so
        # the refusal has to be raised here or the cut lands on a pre-deferral region in silence.
        _install([_sketch("S", profile_count=3, compute_deferred=True)])
        res = ex.handler(sketch_name="S", distance=5, profile_index=0)
        assert res["isError"] is True
        assert "isComputeDeferred=true" in res["message"] and "'S'" in res["message"]
        assert "'profile_index'" in res["message"]
        assert "sketch_add_geometry" in res["message"]

    def test_an_index_into_a_sketch_computing_normally_still_extrudes(self):
        _install([_sketch("S", profile_count=3)])
        out = _payload(ex.handler(sketch_name="S", distance=5, profile_index=0))
        assert out["sketch"] == "S"

    def test_a_deferred_sketch_reading_zero_profiles_is_refused_not_auto_surfaced(self):
        # The AUTO surface clause picks the FEATURE TYPE off pcount, and a sketch deferred while
        # empty then drawn into reads 0 with curves present - so the caller would get an open
        # SURFACE where a region was asked for, reported as a clean ok.
        _install([_sketch("S", profile_count=0, curve_count=2, compute_deferred=True)])
        res = ex.handler(sketch_name="S", distance=5, profile_index=0)
        assert res["isError"] is True
        assert "isComputeDeferred=true" in res["message"] and "'S'" in res["message"]
        assert "surface" not in json.dumps(res).lower()

    def test_a_forced_as_surface_still_runs_on_a_deferred_sketch(self):
        # The other side: as_surface builds from the sketch's CURVES, which a deferral leaves
        # current - so the explicit request is honoured rather than refused off the profile flag.
        _install([_sketch("S", profile_count=0, curve_count=2, compute_deferred=True)])
        out = _payload(ex.handler(sketch_name="S", distance=5, as_surface=True))
        assert out["as_surface"] is True and out["is_solid"] is False


# ── multi-profile selection (one extrude over N profiles, not N calls) ──
# One call with profile_index='all' (or a list) extrudes N profiles together, instead of N calls.

class TestProfileIndexResolution:
    def test_single_int(self):
        assert ex._resolve_profile_indices(2, 5) == ([2], None)

    def test_default_zero(self):
        assert ex._resolve_profile_indices(0, 1) == ([0], None)

    def test_all_keyword(self):
        assert ex._resolve_profile_indices("all", 6) == ([0, 1, 2, 3, 4, 5], None)

    def test_list(self):
        assert ex._resolve_profile_indices([3, 1, 1], 6) == ([1, 3], None)   # sorted + de-duped

    def test_comma_string(self):
        assert ex._resolve_profile_indices("0,2,4", 6) == ([0, 2, 4], None)

    def test_out_of_range_in_list_reports(self):
        idxs, err = ex._resolve_profile_indices([0, 9], 6)
        assert idxs is None and "out of range" in err and "9" in err

    def test_garbage_string(self):
        idxs, err = ex._resolve_profile_indices("xyz", 6)
        assert idxs is None and "not an int" in err


class TestProfileHandle:
    """profile_index may carry a profile HANDLE (entityToken from sketch_get) — _looks_like_handle
    routes it to ProfileRef instead of the index path. (The on-face disambiguation, done right.)"""

    def test_composite_handle_is_a_handle(self):
        assert ex._looks_like_handle("sometoken|@profile:0.4,0.2,0.0") is True

    def test_long_bare_token_is_a_handle(self):
        assert ex._looks_like_handle("/v4BAAAARlJLZXkAH4sIAAAA" + "x" * 40) is True

    def test_index_selectors_are_not_handles(self):
        assert ex._looks_like_handle(0) is False
        assert ex._looks_like_handle("0,2,3") is False
        assert ex._looks_like_handle("all") is False
        assert ex._looks_like_handle([0, 1]) is False

    def test_handle_uses_its_profile_before_the_newest_open_sketch(self, monkeypatch):
        import adsk.fusion
        monkeypatch.setattr(adsk.fusion, "Profile", Profile, raising=False)
        token = "PROFILE_TOKEN_" + "x" * 40
        profile = Profile("selected", entity_token=token)
        older = _sketch("Selected", profiles=[profile])
        profile.parentSketch = older
        newer = _sketch("Newest", profile_count=0, curve_count=1)
        ef = FakeExtrudeFeatures()
        install(ex, make_design(comp=_component("Root", sketches=[older, newer], ef=ef),
                                tokens={token: profile}))
        _wire_adsk()

        out = _payload(ex.handler(profile_index=token + "|@profile:0,0,0",
                                  sketch_name="Newest", distance=5))
        bare = _payload(ex.handler(profile_index=token, distance=5))

        assert ef.last_input.profile is profile
        assert out["sketch"] == "Selected" and out["as_surface"] is False
        assert bare["sketch"] == "Selected" and bare["as_surface"] is False


class TestAllIncludesEnclosedRegions:
    """'all' takes every closed region with no containment analysis, so a region ENCLOSED by another
    selected one is extruded too - measured: a frame sketch's 5 bays between the members filled in and
    the frame came out a solid plate, reported as plain success. The result cannot show that, so the
    note discloses it and points at the per-region selection."""

    def test_all_over_several_profiles_discloses_the_enclosed_regions(self):
        _install([_sketch("S", profile_count=6)])
        out = _payload(ex.handler(sketch_name="S", distance=5, profile_index="all"))
        note = out["note"]
        assert "'all' selected every closed region in this sketch (6)" in note
        assert "INCLUDING any region enclosed by another selected one" in note
        assert "this new acted on them as well" in note
        assert "sketch_get" in note and "handle" in note and "index list" in note

    def test_the_disclosure_never_claims_what_a_cut_did_to_those_regions(self):
        # a bay that FILLS on a 'new' extrude is material REMOVED on a cut - the sentence names the
        # operation that ran instead of asserting the new-body outcome for all four
        _install([_sketch("S", profile_count=6)])
        out = _payload(ex.handler(sketch_name="S", distance=5, profile_index="all", operation="cut"))
        disclosure = out["note"].split("'all' selected")[1]
        assert "this cut acted on them as well" in disclosure
        assert "solid" not in disclosure and "fill" not in disclosure

    def test_a_single_profile_sketch_is_not_lectured(self):
        # one region cannot enclose another - the sentence would be noise on every simple extrude
        _install([_sketch("S", profile_count=1)])
        out = _payload(ex.handler(sketch_name="S", distance=5, profile_index="all"))
        assert "enclosed" not in out["note"]

    def test_an_explicit_index_list_is_not_lectured(self):
        # the caller named the regions one by one - it is 'all' that selects sight-unseen
        _install([_sketch("S", profile_count=6)])
        out = _payload(ex.handler(sketch_name="S", distance=5, profile_index=[0, 2, 4]))
        assert "enclosed" not in out["note"]

    def test_the_star_spelling_the_resolver_accepts_is_disclosed_too(self):
        _install([_sketch("S", profile_count=3)])
        out = _payload(ex.handler(sketch_name="S", distance=5, profile_index="*"))
        assert "enclosed" in out["note"] and out["profiles_extruded"] == 3


def _box(x0, y0, x1, y1, z0=0.0, z1=0.0):
    """A region's bounding box in the (x, y) plane, or in (x, z) via _vbox."""
    return make_bbox((x0, y0, z0), (x1, y1, z1))


def _vbox(x0, z0, x1, z1):
    """A box on a VERTICAL sketch plane (XZ): every region in such a sketch shares one constant y,
    so containment there is decided by x and z."""
    return _box(x0, 0.0, x1, 0.0, z0, z1)


class _Loop:
    """A ProfileLoop: isOuter plus the curves whose boxes give the loop its extent (a loop carries
    no bounding box of its own, so several curves each cover part of it)."""
    def __init__(self, *boxes, is_outer=False):
        self.isOuter = is_outer
        self.profileCurves = _NamedCollection(
            [type("PC", (), {"boundingBox": b})() for b in boxes])


def _profile(box, loops=()):
    """A sketch region at `box`, bounded by `loops`."""
    return Profile(bbox=box, loops=loops)


def _frame(*bays):
    """A frame outline whose material profile carries ONE inner loop per bay, plus the bay profiles
    themselves - the sketch topology a frame drawn with double lines produces. Profile 0 is the
    frame; profiles 1..N are the bays, in the order given."""
    frame = _profile(_box(0, 0, 100, 50), [_Loop(_box(0, 0, 100, 50), is_outer=True)]
                     + [_Loop(_box(*b)) for b in bays])
    return [frame] + [_profile(_box(*b), [_Loop(_box(*b), is_outer=True)]) for b in bays]


def _vframe(*bays):
    """The same frame drawn on a VERTICAL plane: constant y, the regions spread over x and z. Each
    bay is given as (x0, z0, x1, z1)."""
    frame = _profile(_vbox(0, 0, 100, 50), [_Loop(_vbox(0, 0, 100, 50), is_outer=True)]
                     + [_Loop(_vbox(*b)) for b in bays])
    return [frame] + [_profile(_vbox(*b), [_Loop(_vbox(*b), is_outer=True)]) for b in bays]


class TestEnclosedRegionCount:
    """The 'all' disclosure COUNTS the regions that fill a hole of another selected region, so a
    frame extruded solid says how many bays it swallowed instead of only that it might have."""

    def test_the_note_counts_and_names_the_regions_that_filled_a_hole(self):
        _install([_sketch("S", profiles=_frame((10, 10, 40, 40), (60, 10, 90, 40)))])
        out = _payload(ex.handler(sketch_name="S", distance=5, profile_index="all"))
        assert "INCLUDING 2 region(s) enclosed by another selected one (profile index 1, 2)" in out["note"]
        assert out["enclosed_profile_indices"] == [1, 2]

    def test_regions_that_enclose_nothing_are_told_apart_from_an_unknown_count(self):
        # three side-by-side regions, no holes anywhere: the honest answer is 'none', not a warning
        side_by_side = [_profile(_box(x, 0, x + 5, 5), [_Loop(_box(x, 0, x + 5, 5), is_outer=True)])
                        for x in (0, 10, 20)]
        _install([_sketch("S", profiles=side_by_side)])
        out = _payload(ex.handler(sketch_name="S", distance=5, profile_index="all"))
        assert "none of them sits inside another selected region" in out["note"]
        assert "INCLUDING" not in out["note"] and "enclosed_profile_indices" not in out

    def test_an_outer_loop_is_not_a_hole(self):
        # the frame's OUTER loop spans every bay; counting it as an opening would call each bay
        # enclosed even for a sketch with no openings at all
        no_holes = [_profile(_box(0, 0, 100, 50), [_Loop(_box(0, 0, 100, 50), is_outer=True)]),
                    _profile(_box(10, 10, 40, 40), [_Loop(_box(10, 10, 40, 40), is_outer=True)])]
        _install([_sketch("S", profiles=no_holes)])
        out = _payload(ex.handler(sketch_name="S", distance=5, profile_index="all"))
        assert "none of them sits inside another selected region" in out["note"]

    def test_a_region_that_overhangs_the_opening_is_not_counted(self):
        # each overhang leaves the opening on ONE side: a containment test missing that axis (or
        # that end of it) would report a region enclosed that sticks out of the frame
        for box in ((10, 10, 60, 40), (10, 10, 40, 45), (5, 10, 40, 40), (10, 5, 40, 40)):
            overhang = _frame((10, 10, 40, 40))
            overhang[1] = _profile(_box(*box), [_Loop(_box(*box), is_outer=True)])
            _install([_sketch("S", profiles=overhang)])
            out = _payload(ex.handler(sketch_name="S", distance=5, profile_index="all"))
            assert "none of them sits inside another selected region" in out["note"], box

    def test_an_opening_drawn_as_several_curves_takes_their_whole_extent(self):
        # a loop is a chain of curves, each covering part of the opening - reading one of them as
        # the hole's extent shrinks the opening and loses the bay that fills it
        split_loop = _profile(_box(0, 0, 100, 50),
                              [_Loop(_box(0, 0, 100, 50), is_outer=True),
                               _Loop(_box(10, 10, 25, 40), _box(25, 10, 40, 40))])
        bay = _profile(_box(10, 10, 40, 40), [_Loop(_box(10, 10, 40, 40), is_outer=True)])
        _install([_sketch("S", profiles=[split_loop, bay])])
        out = _payload(ex.handler(sketch_name="S", distance=5, profile_index="all"))
        assert "INCLUDING 1 region(s) enclosed by another selected one (profile index 1)" in out["note"]

    def test_a_bay_matching_its_opening_exactly_still_counts(self):
        # a bay's extent and the hole it fills are the SAME curves - an exclusive comparison would
        # count zero on every real frame
        same = (10.0, 10.0, 0.0, 40.0, 40.0, 0.0)
        assert ex._within(same, same) is True

    def test_a_profile_whose_geometry_cannot_be_read_yields_no_count(self):
        # opaque profiles: the note must fall back to what it could not rule out, never to 'none'
        _install([_sketch("S", profile_count=6)])
        out = _payload(ex.handler(sketch_name="S", distance=5, profile_index="all"))
        assert "INCLUDING any region enclosed by another selected one" in out["note"]
        assert "none of them sits inside" not in out["note"]

    def test_one_unreadable_profile_withdraws_the_verdict_for_the_whole_sketch(self):
        mixed = _frame((10, 10, 40, 40))
        mixed.append(("profile", 2))            # a region whose extent cannot be read
        _install([_sketch("S", profiles=mixed)])
        out = _payload(ex.handler(sketch_name="S", distance=5, profile_index="all"))
        assert "INCLUDING any region enclosed by another selected one" in out["note"]

    def test_a_vertical_plane_sketch_is_judged_on_all_three_axes(self):
        # every region of a vertical-plane sketch shares one constant coordinate, so a containment
        # test that skips an axis matches on the remaining interval alone: the tab parked far above
        # the frame shares the bay's x span and would be called enclosed
        vertical = _vframe((10, 10, 40, 40))
        vertical.append(_profile(_vbox(10, 150, 40, 180),
                                 [_Loop(_vbox(10, 150, 40, 180), is_outer=True)]))
        _install([_sketch("S", profiles=vertical)])
        out = _payload(ex.handler(sketch_name="S", distance=5, profile_index="all"))
        assert "INCLUDING 1 region(s) enclosed by another selected one (profile index 1)" in out["note"]
        assert out["enclosed_profile_indices"] == [1]

    def test_an_unreadable_is_outer_withdraws_the_verdict(self):
        # isOuter decides whether a loop IS an opening: read it as 'outer' on a raise and the hole
        # vanishes, so the payload would claim nothing is enclosed while a bay sits in one
        class _RaisingOuter:
            profileCurves = _NamedCollection(
                [type("PC", (), {"boundingBox": _box(10, 10, 40, 40)})()])

            @property
            def isOuter(self):
                raise RuntimeError("3 : bad index parameter")

        frame = _profile(_box(0, 0, 100, 50), [_Loop(_box(0, 0, 100, 50), is_outer=True),
                                               _RaisingOuter()])
        bay = _profile(_box(10, 10, 40, 40), [_Loop(_box(10, 10, 40, 40), is_outer=True)])
        _install([_sketch("S", profiles=[frame, bay])])
        out = _payload(ex.handler(sketch_name="S", distance=5, profile_index="all"))
        assert "INCLUDING any region enclosed by another selected one" in out["note"]
        assert "none of them sits inside" not in out["note"]
        assert "enclosed_profile_indices" not in out

    def test_a_loop_the_collection_will_not_hand_over_withdraws_the_verdict(self):
        # the loop that raises IS the opening: skipping it drops the hole, and the payload would
        # tell a caller nothing is enclosed while the bay sits inside that very loop
        class _RaisingLoops:
            def __init__(self, items):
                self._items = list(items)

            @property
            def count(self):
                return len(self._items)

            def item(self, i):
                if i == 1:
                    raise RuntimeError("3 : bad index parameter")
                return self._items[i]

        frame = _profile(_box(0, 0, 100, 50))
        frame.profileLoops = _RaisingLoops([_Loop(_box(0, 0, 100, 50), is_outer=True),
                                            _Loop(_box(10, 10, 40, 40))])
        bay = _profile(_box(10, 10, 40, 40), [_Loop(_box(10, 10, 40, 40), is_outer=True)])
        _install([_sketch("S", profiles=[frame, bay])])
        out = _payload(ex.handler(sketch_name="S", distance=5, profile_index="all"))
        assert "INCLUDING any region enclosed by another selected one" in out["note"]
        assert "none of them sits inside" not in out["note"]
        assert "enclosed_profile_indices" not in out

    def test_an_unreadable_curve_box_withdraws_the_verdict(self):
        # a hole measured from the curves that happened to read is a SHRUNK hole - it would report
        # the bay that fills it as enclosed by nothing
        class _RaisingCurve:
            @property
            def boundingBox(self):
                raise RuntimeError("3 : bad index parameter")

        hole = _Loop(_box(10, 10, 25, 40))
        hole.profileCurves = _NamedCollection(
            [type("PC", (), {"boundingBox": _box(10, 10, 25, 40)})(), _RaisingCurve()])
        frame = _profile(_box(0, 0, 100, 50), [_Loop(_box(0, 0, 100, 50), is_outer=True), hole])
        bay = _profile(_box(10, 10, 40, 40), [_Loop(_box(10, 10, 40, 40), is_outer=True)])
        _install([_sketch("S", profiles=[frame, bay])])
        out = _payload(ex.handler(sketch_name="S", distance=5, profile_index="all"))
        assert "INCLUDING any region enclosed by another selected one" in out["note"]
        assert "none of them sits inside" not in out["note"]

    def test_a_hole_whose_curves_do_not_read_withdraws_the_verdict(self):
        # no curve of the loop answers, so the opening has no measurable extent at all - dropping
        # the hole here is the same false 'nothing is enclosed' as shrinking it
        blind_hole = _Loop(_box(10, 10, 40, 40))
        blind_hole.profileCurves = _NamedCollection([])
        frame = _profile(_box(0, 0, 100, 50),
                         [_Loop(_box(0, 0, 100, 50), is_outer=True), blind_hole])
        bay = _profile(_box(10, 10, 40, 40), [_Loop(_box(10, 10, 40, 40), is_outer=True)])
        _install([_sketch("S", profiles=[frame, bay])])
        out = _payload(ex.handler(sketch_name="S", distance=5, profile_index="all"))
        assert "INCLUDING any region enclosed by another selected one" in out["note"]
        assert "none of them sits inside" not in out["note"]

    def test_a_coordinate_that_is_not_a_number_withdraws_the_verdict(self):
        # an unmodelled/absent coordinate reads as something that is not a number; treating it as a
        # zero would place the region at the origin and invent a containment answer
        bad = _box(10, 10, 40, 40)
        bad.minPoint = type("P", (), {"x": "10", "y": 10.0, "z": 0.0})()
        profiles = _frame((10, 10, 40, 40))
        profiles[1] = _profile(bad, [_Loop(_box(10, 10, 40, 40), is_outer=True)])
        _install([_sketch("S", profiles=profiles)])
        out = _payload(ex.handler(sketch_name="S", distance=5, profile_index="all"))
        assert "INCLUDING any region enclosed by another selected one" in out["note"]
        assert "enclosed_profile_indices" not in out

    def test_a_hole_read_that_raises_cannot_sink_the_extrude(self):
        class _Exploding:
            @property
            def boundingBox(self):
                raise RuntimeError("3 : bad index parameter")

            @property
            def profileLoops(self):
                raise RuntimeError("3 : bad index parameter")

        _install([_sketch("S", profiles=[_Exploding(), _Exploding()])])
        out = _payload(ex.handler(sketch_name="S", distance=5, profile_index="all"))
        assert out["extruded"] is True and out["profiles_extruded"] == 2

    def test_the_count_is_over_the_selection_not_the_whole_sketch(self):
        # the two bays alone enclose nothing - the profile whose holes they fill was NOT selected,
        # so a walk over every profile in the sketch would over-report them
        sketch = _sketch("S", profiles=_frame((10, 10, 40, 40), (60, 10, 90, 40)))
        _install([sketch])
        assert ex._enclosed_regions(sketch.profiles, [1, 2]) == []
        assert ex._enclosed_regions(sketch.profiles, [0, 1, 2]) == [1, 2]


class TestMultiProfileExtrude:
    def test_all_profiles_extruded_in_one_call(self):
        _install([_sketch("S", profile_count=4)])
        out = _payload(ex.handler(sketch_name="S", distance=5, profile_index="all"))
        assert out["profiles_extruded"] == 4
        assert out["profile_index"] == [0, 1, 2, 3]

    def test_list_of_profiles(self):
        _install([_sketch("S", profile_count=6)])
        out = _payload(ex.handler(sketch_name="S", distance=5, profile_index=[1, 3, 5]))
        assert out["profiles_extruded"] == 3 and out["profile_index"] == [1, 3, 5]

    def test_single_still_reports_scalar(self):
        _install([_sketch("S", profile_count=3)])
        out = _payload(ex.handler(sketch_name="S", distance=5, profile_index=2))
        assert out["profiles_extruded"] == 1 and out["profile_index"] == 2


# ── behaviour ────────────────────────────────────────────────────────────────

class TestExtrude:
    def test_basic_new_body_scales_distance_to_cm(self):
        ef = _install([_sketch("Base")])
        out = _payload(ex.handler(sketch_name="Base", distance=6, units="mm", operation="new"))
        assert out["extruded"] is True
        assert out["operation"] == "new"
        assert out["result_bodies"] == ["Body1"]
        # 6 mm -> 0.6 cm handed to the API
        sym, dist = ef.last_input.distance_extent
        assert dist[0] == "real" and abs(dist[1] - 0.6) < 1e-9
        assert sym is False
        assert ef.last_input.operation == adsk.fusion.FeatureOperations.NewBodyFeatureOperation

    def test_inch_scaling(self):
        ef = _install([_sketch("Base")])
        _payload(ex.handler(sketch_name="Base", distance=1, units="in"))
        _, dist = ef.last_input.distance_extent
        assert dist == ("real", 2.54)

    def test_most_recent_sketch_when_unnamed(self):
        ef = _install([_sketch("First"), _sketch("Last")])
        out = _payload(ex.handler(distance=5))
        assert out["sketch"] == "Last"     # most recent

    def test_operation_mapping_cut(self):
        ef = _install([_sketch("S")])
        _payload(ex.handler(sketch_name="S", distance=5, operation="cut"))
        assert ef.last_input.operation == adsk.fusion.FeatureOperations.CutFeatureOperation

    def test_symmetric_flag_passed(self):
        ef = _install([_sketch("S")])
        _payload(ex.handler(sketch_name="S", distance=5, symmetric=True))
        sym, _ = ef.last_input.distance_extent
        assert sym is True

    def test_negative_distance_allowed(self):
        ef = _install([_sketch("S")])
        out = _payload(ex.handler(sketch_name="S", distance=-4, units="mm"))
        _, dist = ef.last_input.distance_extent
        assert dist[0] == "real" and abs(dist[1] - (-0.4)) < 1e-9
        assert out["distance"] == -4


# ── taper (draft angle) ─────────────────────────────────────────────────────────────────────────
# A one-sided extrude with taper_deg builds a DistanceExtentDefinition + a 'N deg' taper ValueInput and
# calls setOneSideExtent(extent, dir, taper). A SYMMETRIC extrude with taper needs setSymmetricExtent
# (which carries a taper) - setDistanceExtent cannot, so the taper would otherwise be silently dropped.
# Pinned: the one-sided taper path, the distance still scaled onto the DistanceExtentDefinition, and that
# symmetric+taper applies the taper via setSymmetricExtent instead of dropping it.

class TestTaper:
    def test_taper_uses_one_side_extent_with_deg_string(self):
        ef = _install([_sketch("S")])
        import adsk.fusion
        adsk.fusion.DistanceExtentDefinition.create = staticmethod(lambda v: ("dist_ext", v))
        out = _payload(ex.handler(sketch_name="S", distance=6, units="mm", taper_deg=3))
        # taper path: one_side set, NOT the plain distance extent
        assert ef.last_input.one_side is not None
        assert ef.last_input.distance_extent is None
        extent, direction, taper = ef.last_input.one_side
        # distance still scaled to cm onto the DistanceExtentDefinition (6mm -> 0.6cm)
        assert extent[0] == "dist_ext" and extent[1][0] == "real" and abs(extent[1][1] - 0.6) < 1e-9
        # taper passed as a 'N deg' string ValueInput
        assert taper[0] == "str" and "3" in taper[1] and "deg" in taper[1]
        assert out["taper_deg"] == 3.0

    def test_symmetric_with_taper_uses_symmetric_extent(self):
        # symmetric + taper: setDistanceExtent carries no taper, so the handler must use
        # setSymmetricExtent (which does) - not drop the taper onto a plain distance extent.
        ef = _install([_sketch("S")])
        out = _payload(ex.handler(sketch_name="S", distance=5, units="mm", taper_deg=10, symmetric=True))
        assert ef.last_input.distance_extent is None      # NOT the taper-dropping path
        dist, is_full, taper = ef.last_input.symmetric_extent
        assert dist[0] == "real" and abs(dist[1] - 0.5) < 1e-9   # 5 mm -> 0.5 cm (per-side half-length)
        assert is_full is False                                  # 'distance' is per-side, matching setDistanceExtent
        assert taper[0] == "str" and "10" in taper[1] and "deg" in taper[1]
        assert out["taper_deg"] == 10.0                   # the reported taper is the one applied

    def test_zero_taper_is_plain_distance(self):
        ef = _install([_sketch("S")])
        _payload(ex.handler(sketch_name="S", distance=5, taper_deg=0))
        assert ef.last_input.distance_extent is not None
        assert ef.last_input.one_side is None


# ── as_surface (open-profile extrude into a surface wall) ───────────────────
# The additive surface path: as_surface=True (or an open path with no closed profile) builds via
# createOpenProfile + ExtrudeFeatureInput.isSolid=False, and EVERY result reports is_solid.

class TestAsSurface:
    def test_default_extrude_is_solid_unchanged(self):
        # as_surface defaults False -> today's behavior: closed profile -> solid, is_solid True.
        ef = _install([_sketch("S", profile_count=1)])
        out = _payload(ex.handler(sketch_name="S", distance=5))
        assert out["as_surface"] is False
        assert out["is_solid"] is True
        assert ef.last_input.isSolid is True            # never touched the surface path
        assert ef.last_input.distance_extent is not None

    def test_as_surface_true_sets_isSolid_false(self):
        ef = _install([_sketch("S", profile_count=1, curve_count=4)])
        out = _payload(ex.handler(sketch_name="S", distance=5, as_surface=True))
        assert out["as_surface"] is True
        assert out["is_solid"] is False
        assert ef.last_input.isSolid is False
        assert "SURFACE" in out["note"]

    def test_open_path_auto_surface_when_no_closed_profile(self):
        # No closed profile but open curves exist -> auto surface, not a dead-end error.
        ef = _install([_sketch("S", profile_count=0, curve_count=2)])
        out = _payload(ex.handler(sketch_name="S", distance=5))
        assert out["as_surface"] is True
        assert out["is_solid"] is False
        assert ef.last_input.isSolid is False

    def test_no_profile_and_no_curves_points_at_surface_path(self):
        # No closed profile AND no curves -> error that mentions the surface path / curves.
        _install([_sketch("S", profile_count=0, curve_count=0)])
        res = ex.handler(sketch_name="S", distance=5)
        assert res["isError"] is True
        assert "surface" in res["message"].lower() or "open path" in res["message"].lower()


# ── a sketch TEXT as the profile ('text:<i>') ───────────────────────────────
# ExtrudeFeatures.createInput takes a SketchText in its profile slot, and a nameplate sketch holds
# no closed profile at all - so the address must route to ProfileRef(allow_text) BEFORE the
# zero-profile surface branch, which has no curves to build an open profile from.

class TestSketchTextProfile:
    def test_a_text_only_sketch_extrudes_the_text_as_a_solid(self):
        ef = _install([_sketch("Nameplate", profile_count=0, text_count=1)])
        out = _payload(ex.handler(sketch_name="Nameplate", distance=2, profile_index="text:0"))
        assert ef.last_input.profile.tag == "Nameplate#0"    # the SketchText itself, not a profile
        assert out["profile_index"] == "text" and out["profiles_extruded"] == 1
        assert out["as_surface"] is False and ef.last_input.isSolid is True
        assert out["is_solid"] is True

    def test_a_text_address_never_reaches_the_surface_branch(self):
        # the measured dead-end: pcount == 0 routed the call into the open-profile path, which
        # refuses a sketch with no curves - so a text-only sketch could not be extruded at all.
        _install([_sketch("Nameplate", profile_count=0, text_count=1)])
        res = ex.handler(sketch_name="Nameplate", distance=2, profile_index="text:0")
        assert res["isError"] is False, res
        assert "no curves to extrude as a surface" not in json.dumps(res)

    def test_a_text_address_is_not_read_as_an_index(self):
        # 'text:0' is short and non-numeric, so the handle predicate says no - without the text
        # predicate the index resolver answers "not an int, list, 'all', or '0,1,2'".
        _install([_sketch("Plate", profile_count=2, text_count=1)])
        out = _payload(ex.handler(sketch_name="Plate", distance=2, profile_index="text:0"))
        assert out["profile_index"] == "text"

    def test_the_named_sketch_owns_a_bare_text_address(self):
        # blank-sketch resolution takes the MOST RECENT sketch, so an unqualified address handed
        # straight to ProfileRef would extrude the wrong sketch's text - silently.
        ef = _install([_sketch("Nameplate", profile_count=0, text_count=1),
                       _sketch("Later", profile_count=0, text_count=1)])
        out = _payload(ex.handler(sketch_name="Nameplate", distance=2, profile_index="text:0"))
        assert ef.last_input.profile.tag == "Nameplate#0"
        assert out["sketch"] == "Nameplate"

    def test_a_sketch_qualified_address_is_taken_as_given(self):
        ef = _install([_sketch("Nameplate", profile_count=0, text_count=1),
                       _sketch("Later", profile_count=0, text_count=2)])
        _payload(ex.handler(distance=2, profile_index="Nameplate/text:0"))
        assert ef.last_input.profile.tag == "Nameplate#0"

    def test_an_out_of_range_text_is_refused_by_the_kind(self):
        _install([_sketch("Nameplate", profile_count=0, text_count=1)])
        res = ex.handler(sketch_name="Nameplate", distance=2, profile_index="text:5")
        assert res["isError"] is True
        assert "out of range" in res["message"] and "1 sketch text(s)" in res["message"]

    def test_the_note_points_at_the_stamp_alternative(self):
        _install([_sketch("Nameplate", profile_count=0, text_count=1)])
        out = _payload(ex.handler(sketch_name="Nameplate", distance=2, profile_index="text:0"))
        assert "Sketch text extruded into a solid" in out["note"]
        assert "model_emboss" in out["note"]

    def test_a_text_mixed_into_a_list_selector_is_refused(self):
        # the list/'all' forms address CLOSED profiles by index; a text carries no index there, so
        # a mixed selector must not silently drop it.
        _install([_sketch("Plate", profile_count=2, text_count=1)])
        res = ex.handler(sketch_name="Plate", distance=2, profile_index=["text:0", 1])
        assert res["isError"] is True
        assert "text:0" in res["message"] and "on its own" in res["message"]

    def test_a_text_mixed_into_a_comma_selector_is_refused(self):
        _install([_sketch("Plate", profile_count=2, text_count=1)])
        res = ex.handler(sketch_name="Plate", distance=2, profile_index="text:0,1")
        assert res["isError"] is True and "text:0" in res["message"]

    def test_a_text_mixed_with_all_is_refused(self):
        _install([_sketch("Plate", profile_count=2, text_count=1)])
        res = ex.handler(sketch_name="Plate", distance=2, profile_index=["text:0", "all"])
        assert res["isError"] is True and "text:0" in res["message"]

    def test_as_surface_with_a_text_is_refused_rather_than_ignored(self):
        _install([_sketch("Nameplate", profile_count=0, text_count=1)])
        res = ex.handler(sketch_name="Nameplate", distance=2, profile_index="text:0",
                         as_surface=True)
        assert res["isError"] is True and "as_surface" in res["message"]

    def test_a_text_extrude_states_the_solid_it_promises(self):
        # as_surface is REFUSED with a text, so the solid is set and read back here rather than
        # inherited from an ExtrudeFeatureInput default this code never reads.
        ef = _install([_sketch("Nameplate", profile_count=0, text_count=1)])
        prior = ef.createInput

        def _surface_default(profile, operation):
            inp = prior(profile, operation)
            inp.isSolid = False
            return inp
        ef.createInput = _surface_default
        out = _payload(ex.handler(sketch_name="Nameplate", distance=2, profile_index="text:0"))
        assert ef.last_input.isSolid is True and out["is_solid"] is True

    def test_an_isSolid_assignment_the_input_drops_refuses_before_add(self):
        # The SWIG trap set_verified exists for: the assignment lands on a dead Python attribute
        # while the object keeps its default, and only the read-back tells.
        ef = _install([_sketch("Nameplate", profile_count=0, text_count=1)])

        class _Swallows(FakeExtrudeInput):
            @property
            def isSolid(self):
                return False

            @isSolid.setter
            def isSolid(self, value):
                pass

        def _make(profile, operation):
            ef.last_input = _Swallows(profile, operation)
            return ef.last_input
        ef.createInput = _make
        res = ex.handler(sketch_name="Nameplate", distance=2, profile_index="text:0")
        assert res["isError"] is True and "isSolid" in res["message"]
        assert ef.added is False          # refused BEFORE the feature was added

    def test_a_plain_index_selector_is_untouched_by_the_text_route(self):
        ef = _install([_sketch("Plate", profile_count=2, text_count=1)])
        out = _payload(ex.handler(sketch_name="Plate", distance=2, profile_index=1))
        assert out["profile_index"] == 1
        assert ef.last_input.profile == ("profile", 1)


# ── to_object extent (extrude up to a face handle) ──────────────────────────

def _install_geom(faces=None, bodies=None):
    """Install + wire the design so to_object faces / target_bodies resolve.

    The handler resolves its design via _common.design() - the SAME seam _inputs uses for handle/body
    resolution - so ONE design fake serves both (conftest.install patches the pair). The design
    _install built is EXTENDED here with findEntityByToken and a body-by-name lookup."""
    ef = _install([_sketch("S")])
    import adsk.fusion
    adsk.fusion.BRepFace = BRepFace
    adsk.fusion.BRepBody = BRepBody
    faces = faces or {}
    bodies = bodies or {}
    handle_map = dict(faces); handle_map.update(bodies)
    design = ex.app.activeProduct
    design.rootComponent.bRepBodies = _NamedCollection(list(bodies.values()))
    design.findEntityByToken = lambda t, hm=handle_map: ([hm[t]] if t in hm else [])

    def _cut_removes(inp):
        # A cut takes material out of every body it was scoped to - the drop the design-wide
        # snapshot diffs. Override ef.on_add for a cut that reaches nothing.
        if inp.operation == adsk.fusion.FeatureOperations.CutFeatureOperation:
            for b in (inp.participantBodies or ()):
                b.volume = max(b.volume - 10.0, 0.0)
    ef.on_add = _cut_removes
    return ef


class TestToObject:
    def test_extrude_to_face_uses_to_entity_extent(self):
        face = BRepFace(None)
        ef = _install_geom(faces={"F": face})
        out = _payload(ex.handler(sketch_name="S", to_object="F"))
        assert out["extent"] == "to_object"
        assert out["distance"] is None
        # a ToEntityExtentDefinition was used (one_side set, distance_extent not)
        assert ef.last_input.one_side is not None
        assert ef.last_input.distance_extent is None

    def test_to_object_overrides_distance(self):
        face = BRepFace(None)
        ef = _install_geom(faces={"F": face})
        out = _payload(ex.handler(sketch_name="S", distance=999, to_object="F"))
        assert out["extent"] == "to_object" and ef.last_input.distance_extent is None

    def test_bad_to_object_handle_errors(self):
        _install_geom(faces={})
        res = ex.handler(sketch_name="S", to_object="missing")
        assert res["isError"] is True
        assert "handle did not resolve" in res["message"]


# ── target_bodies cut scoping (prevents bleed-through) ──────────────────────

class TestTargetBodies:
    def test_cut_scoped_to_bodies(self):
        b = BRepBody("KeepMe", volume=100.0)
        ef = _install_geom(bodies={"KeepMe": b})
        out = _payload(ex.handler(sketch_name="S", distance=5, operation="cut", target_bodies="KeepMe"))
        assert ef.last_input.participantBodies == [b]
        assert out["scoped_to_bodies"] == ["KeepMe"]

    def test_target_bodies_by_handle(self):
        h = "/v" + "B" * 70
        b = BRepBody("FromHandle")
        ef = _install_geom(bodies={h: b})
        out = _payload(ex.handler(sketch_name="S", distance=5, operation="join", target_bodies=h))
        assert ef.last_input.participantBodies == [b]

    def test_target_bodies_rejected_on_new(self):
        b = BRepBody("X")
        _install_geom(bodies={"X": b})
        res = ex.handler(sketch_name="S", distance=5, operation="new", target_bodies="X")
        assert res["isError"] is True and "cut/join/intersect" in res["message"]

    def test_bad_target_body_errors(self):
        _install_geom(bodies={"X": BRepBody("X")})
        res = ex.handler(sketch_name="S", distance=5, operation="cut", target_bodies="Nope")
        assert res["isError"] is True and "Nope" in res["message"]

    def test_the_documented_occurrence_qualified_slash_form_resolves(self):
        # target_bodies' description tells the caller to scope a shared body name as
        # '<occurrence>/<body>' ("RingOuter:1/Body1"). That claim is only worth making if something
        # fails when it stops being true: this drives the exact advertised spelling through the kind
        # the tool wires, so dropping '/' from the qualified-reference resolver goes red here.
        import types
        ef = _install_geom()
        design = ex.app.activeProduct

        def _placed(occ_name, comp_name, body_name):
            body = BRepBody(body_name)
            body.isSolid = True
            body.entityToken = f"tok-{occ_name}"
            body.parentComponent = types.SimpleNamespace(name=comp_name)
            occ = types.SimpleNamespace(
                name=occ_name, fullPathName=occ_name, component=body.parentComponent,
                bRepBodies=type("BB", (), {
                    "itemByName": staticmethod(lambda n, b=body: b if n == b.name else None),
                    "count": 1, "item": staticmethod(lambda i, b=body: b)})())
            body.assemblyContext = occ
            return occ, body

        outer_occ, outer_body = _placed("RingOuter:1", "RingOuter", "Body1")
        inner_occ, inner_body = _placed("RingInner:1", "RingInner", "Body1")
        design.rootComponent.allOccurrences = [outer_occ, inner_occ]
        design.rootComponent.bRepBodies = type("BB", (), {
            "itemByName": staticmethod(lambda n: None), "count": 0,
            "item": staticmethod(lambda i: None)})()

        got, err = ex._TARGET_BODIES.resolve(["RingOuter:1/Body1"])
        assert err is None, err
        assert got == [outer_body]                  # the RIGHT component's body, not the other one

        # and the bare name both components answer to is REFUSED, not silently resolved to one
        _bare, bare_err = ex._TARGET_BODIES.resolve(["Body1"])
        assert bare_err is not None
        assert "RingOuter:1" in bare_err and "RingInner:1" in bare_err

    def test_scoped_echo_qualifies_same_named_bodies_by_owning_component(self):
        # Fusion auto-names every body 'Body1' by default, so two DIFFERENT target bodies that
        # happen to share that name must still read as distinguishable entries in
        # 'scoped_to_bodies' - otherwise a cut mis-targeted onto the wrong body's component is
        # invisible in the echo (the live defect this pins).
        h1, h2 = "/v" + "A" * 70, "/v" + "B" * 70
        carrier = BRepBody("Body1", volume=100.0)
        carrier.parentComponent = type("C", (), {"name": "Carrier"})()
        inner_ring = BRepBody("Body1", volume=100.0)
        inner_ring.parentComponent = type("C", (), {"name": "Inner_Ring"})()
        _install_geom(bodies={h1: carrier, h2: inner_ring})
        out = _payload(ex.handler(sketch_name="S", distance=5, operation="cut",
                                  target_bodies=[h1, h2]))
        assert out["scoped_to_bodies"] == ["Carrier/Body1", "Inner_Ring/Body1"]
        assert out["scoped_to_bodies"][0] != out["scoped_to_bodies"][1]


# ── extent selector guards (cross-extent conflicts) ──────────────────────────
# 'extent' picks the depth style; these pin the cross-extent validation that must fire BEFORE any
# ExtrudeFeatureInput setter runs - an unknown value, 'to_object' paired with an extent that doesn't
# use it, and 'to_face' missing its required 'to_object'.

class TestExtentSetterRefusals:
    """Each extent setter answers "did it take". A false answer left on the floor means add() builds
    the feature on its DEFAULT extent while the payload reports the requested one."""

    def test_a_refused_distance_extent_is_an_error_and_nothing_is_added(self):
        ef = _install([_sketch("S")])
        ef.next_result = False
        res = ex.handler(sketch_name="S", profile_index=0, distance=10)
        assert res["isError"] is True and "distance extent" in res["message"]
        assert ef.added is False

    def test_a_refused_symmetric_tapered_extent_is_an_error(self):
        ef = _install([_sketch("S")])
        ef.next_result = False
        res = ex.handler(sketch_name="S", profile_index=0, distance=10, symmetric=True, taper_deg=3)
        assert res["isError"] is True and "symmetric tapered extent" in res["message"]
        assert ef.added is False

    def test_a_refused_one_sided_tapered_extent_is_an_error(self):
        ef = _install([_sketch("S")])
        ef.next_result = False
        res = ex.handler(sketch_name="S", profile_index=0, distance=10, taper_deg=3)
        assert res["isError"] is True and "one-sided tapered extent" in res["message"]
        assert ef.added is False


class TestExtentGuards:
    def test_unknown_extent_value(self):
        _install([_sketch("S")])
        res = ex.handler(sketch_name="S", distance=5, extent="bogus")
        assert res["isError"] is True and "Unknown extent" in res["message"]

    def test_to_object_rejected_with_through_all(self):
        _install_geom(faces={"F": BRepFace(None)})
        res = ex.handler(sketch_name="S", extent="through_all", to_object="F")
        assert res["isError"] is True
        assert "not used with extent='through_all'" in res["message"]

    def test_to_object_rejected_with_two_side(self):
        _install_geom(faces={"F": BRepFace(None)})
        res = ex.handler(sketch_name="S", extent="two_side", distance=1, distance2=1, to_object="F")
        assert res["isError"] is True
        assert "not used with extent='two_side'" in res["message"]

    def test_to_face_without_to_object_errors(self):
        _install([_sketch("S")])
        res = ex.handler(sketch_name="S", extent="to_face")
        assert res["isError"] is True and "to_face' needs 'to_object'" in res["message"]

    def test_to_face_alias_behaves_like_to_object(self):
        # extent='to_face' is the explicit spelling of the legacy to_object-alone shorthand - same
        # ToEntityExtentDefinition path, same reported 'extent' value (back-compat).
        face = BRepFace(None)
        ef = _install_geom(faces={"F": face})
        out = _payload(ex.handler(sketch_name="S", extent="to_face", to_object="F"))
        assert out["extent"] == "to_object"
        assert ef.last_input.one_side is not None


# ── through_all extent (ThroughAllExtentDefinition) ──────────────────────────
# 'distance' carries no magnitude for through_all - only its SIGN (direction hint); symmetric=true
# goes both ways. The extent is built from ThroughAllExtentDefinition: one side plus a direction for
# a one-sided cut, BOTH sides for a symmetric one. The retired setAllExtent(SymmetricExtentDirection)
# answers true while cutting a single direction (measured live - half the expected material), so it
# is never called. Pinned: the setter per direction, the no-taper guard, and the returned-false paths.

class TestThroughAll:
    def test_default_direction_is_positive(self):
        import adsk.fusion
        ef = _install([_sketch("S")])
        out = _payload(ex.handler(sketch_name="S", extent="through_all"))
        extent, direction, taper = ef.last_input.one_side
        assert extent == ["through_all"]
        assert direction == adsk.fusion.ExtentDirections.PositiveExtentDirection
        assert taper is None
        assert ef.last_input.all_extent is None       # the retired setter is never called
        assert out["extent"] == "through_all" and out["direction"] == "positive"
        assert out["distance"] is None

    def test_negative_distance_picks_negative_direction(self):
        import adsk.fusion
        ef = _install([_sketch("S")])
        out = _payload(ex.handler(sketch_name="S", extent="through_all", distance=-5))
        extent, direction, _taper = ef.last_input.one_side
        assert extent == ["through_all"]
        assert direction == adsk.fusion.ExtentDirections.NegativeExtentDirection
        assert ef.last_input.all_extent is None
        assert out["direction"] == "negative"

    def test_positive_distance_picks_positive_direction(self):
        import adsk.fusion
        ef = _install([_sketch("S")])
        _payload(ex.handler(sketch_name="S", extent="through_all", distance=5))
        _extent, direction, _taper = ef.last_input.one_side
        assert direction == adsk.fusion.ExtentDirections.PositiveExtentDirection
        assert ef.last_input.all_extent is None

    def test_symmetric_sets_both_sides_and_never_the_retired_setter(self):
        # setAllExtent(SymmetricExtentDirection) answers true and cuts ONE direction (measured live:
        # a mid-plane cut removed exactly half the expected material), so a symmetric through-all
        # must hand setTwoSidesExtent a ThroughAllExtentDefinition PER SIDE.
        ef = _install([_sketch("S")])
        out = _payload(ex.handler(sketch_name="S", extent="through_all", symmetric=True, distance=-9))
        side_one, side_two, taper_one, taper_two = ef.last_input.two_sides_extent
        assert side_one == ["through_all"] and side_two == ["through_all"]
        assert side_one is not side_two          # one definition per side, not one object twice
        assert (taper_one, taper_two) == (None, None)
        assert ef.last_input.all_extent is None
        assert ef.last_input.one_side is None    # a symmetric cut is never routed one-sided
        assert out["direction"] == "symmetric"

    def test_a_one_sided_through_all_never_sets_two_sides(self):
        ef = _install([_sketch("S")])
        _payload(ex.handler(sketch_name="S", extent="through_all", distance=-5))
        assert ef.last_input.two_sides_extent is None

    def test_rejects_taper(self):
        _install([_sketch("S")])
        res = ex.handler(sketch_name="S", extent="through_all", taper_deg=5)
        assert res["isError"] is True
        assert "through_all" in res["message"] and "taper" in res["message"]

    def test_setOneSideExtent_false_is_reported(self):
        ef = _install([_sketch("S")])
        ef.next_result = False
        res = ex.handler(sketch_name="S", extent="through_all", distance=-5)
        assert res["isError"] is True
        assert "setOneSideExtent returned false" in res["message"]
        assert "negative" in res["message"]        # names the direction that was refused
        assert ef.added is False

    def test_setTwoSidesExtent_false_is_reported(self):
        ef = _install([_sketch("S")])
        ef.next_result = False
        res = ex.handler(sketch_name="S", extent="through_all", symmetric=True)
        assert res["isError"] is True
        assert "setTwoSidesExtent returned false" in res["message"]
        assert "symmetric" in res["message"]        # names the call that refused, not the other one
        assert ef.added is False


# ── through_all CUT/INTERSECT volume read-back (the rung-4 honesty check) ───
# 'All' extends until it exits the geometry (no partial depth), so ANY volume drop on the body(s) it
# acted on proves the cut went all the way through - a zero delta means the extent missed the body
# entirely (a silent no-op the API would otherwise report as a false ok).

class TestThroughAllVolumeCheck:
    def test_target_bodies_scoped_volume_removed_is_reported(self):
        b = BRepBody("KeepMe", volume=50.0)
        ef = _install_geom(bodies={"KeepMe": b})
        ef.on_add = lambda inp: setattr(b, "volume", 10.0)   # simulate the cut removing material
        out = _payload(ex.handler(sketch_name="S", operation="cut", extent="through_all",
                                  distance=-1, target_bodies="KeepMe"))
        assert out["through_all_volume_removed_cm3"] == {"KeepMe": 40.0}

    def test_twin_body_names_report_two_distinct_qualified_keys(self):
        # Fusion auto-names every component's first body 'Body1', so a cut scoped across two
        # components sees the SAME bare name twice. Keyed by that bare name the dict COLLAPSES to one
        # entry and the second silently overwrites the first - measured live, a cut removing 3.0 cm3
        # from one component and 1.0 cm3 from another published {"Body1": 1.0}, a receipt that cannot
        # tell "cut both" from "cut one". The key is the occurrence-qualified name 'scoped_to_bodies'
        # already echoes, so both bodies stay addressable and both deltas survive.
        h1, h2 = "/v" + "A" * 70, "/v" + "B" * 70
        outer = BRepBody("Body1")
        outer.parentComponent = type("C", (), {"name": "RingOuter"})()
        outer.volume, outer.entityToken = 10.0, "outer"
        inner = BRepBody("Body1")
        inner.parentComponent = type("C", (), {"name": "RingInner"})()
        inner.volume, inner.entityToken = 20.0, "inner"

        def _cut(inp):
            outer.volume, inner.volume = 7.0, 19.0      # -3.0 cm3 and -1.0 cm3

        ef = _install_geom(bodies={h1: outer, h2: inner})
        ef.on_add = _cut
        out = _payload(ex.handler(sketch_name="S", operation="cut", extent="through_all",
                                  distance=-1, target_bodies=[h1, h2]))
        removed = out["through_all_volume_removed_cm3"]
        assert removed == {"RingOuter/Body1": 3.0, "RingInner/Body1": 1.0}
        assert len(removed) == 2                        # neither entry overwrote the other
        # and the keys are the SAME spelling the scoped echo publishes, so a caller can hand one back
        assert sorted(removed) == sorted(out["scoped_to_bodies"])

    def test_solo_body_in_component_is_the_implied_target(self):
        # No target_bodies given, but exactly ONE solid body exists - the unambiguous single-part
        # case model_shell's own default-body resolution mirrors.
        ef = _install([_sketch("S")])
        root = ex.app.activeProduct.rootComponent
        body = BRepBody("Box1", volume=100.0)
        root.bRepBodies = _NamedCollection([body])
        ef.on_add = lambda inp: setattr(body, "volume", 40.0)
        out = _payload(ex.handler(sketch_name="S", operation="cut", extent="through_all", distance=-1))
        assert out["through_all_volume_removed_cm3"] == {"Box1": 60.0}

    def test_no_volume_change_is_reported_as_error(self):
        _install([_sketch("S")])
        root = ex.app.activeProduct.rootComponent
        body = BRepBody("Box1", volume=100.0)
        root.bRepBodies = _NamedCollection([body])
        # on_add left unset -> body.volume never changes -> the through_all cut silently missed it
        res = ex.handler(sketch_name="S", operation="cut", extent="through_all", distance=1)
        assert res["isError"] is True
        assert "removed no material" in res["message"] and "Box1" in res["message"]

    def test_the_no_op_refusal_names_the_leftover_feature_and_how_to_remove_it(self):
        # The refusal rolls nothing back - the check reads only the bodies the cut was aimed at, so
        # an effect elsewhere is not ruled out. The dead feature is therefore DISCLOSED by name,
        # with the tool that removes it, instead of being left in the timeline unmentioned.
        _install([_sketch("S")])
        root = ex.app.activeProduct.rootComponent
        root.bRepBodies = _NamedCollection([BRepBody("Box1", volume=100.0)])
        res = ex.handler(sketch_name="S", operation="cut", extent="through_all", distance=1)
        assert res["isError"] is True
        msg = res["message"]
        assert "'Extrude1' remains in the timeline" in msg
        assert "design_delete_feature" in msg
        assert "rolled back" not in msg          # nothing was removed - it must not claim otherwise

    def test_several_bodies_with_no_target_bodies_skips_the_check(self):
        # Several bodies and no target_bodies -> which one(s) intersect is ambiguous from here, so no
        # guessed target is checked (Fusion's own intersection search still runs the real cut).
        ef = _install([_sketch("S")])
        root = ex.app.activeProduct.rootComponent
        root.bRepBodies = _NamedCollection([BRepBody("A", volume=10.0), BRepBody("B", volume=20.0)])
        out = _payload(ex.handler(sketch_name="S", operation="cut", extent="through_all", distance=-1))
        assert "through_all_volume_removed_cm3" not in out

    def test_new_operation_skips_the_check(self):
        # extent=through_all with operation='new' has no participant body to check a REMOVAL on.
        ef = _install([_sketch("S")])
        root = ex.app.activeProduct.rootComponent
        root.bRepBodies = _NamedCollection([BRepBody("Box1", volume=100.0)])
        out = _payload(ex.handler(sketch_name="S", extent="through_all"))   # operation defaults 'new'
        assert "through_all_volume_removed_cm3" not in out


# ── two_side extent (setTwoSidesDistanceExtent) ──────────────────────────────
# Independent distances per side, no taper, no 'symmetric' (equal distance/distance2 already
# expresses a symmetric two-sided extrude without a second flag).

class TestTwoSide:
    def test_calls_setTwoSidesDistanceExtent_with_scaled_distances(self):
        ef = _install([_sketch("S")])
        out = _payload(ex.handler(sketch_name="S", extent="two_side", distance=10, distance2=5, units="mm"))
        d1, d2 = ef.last_input.two_sides_distance
        assert d1[0] == "real" and abs(d1[1] - 1.0) < 1e-9    # 10mm -> 1.0cm
        assert d2[0] == "real" and abs(d2[1] - 0.5) < 1e-9    # 5mm -> 0.5cm
        assert out["extent"] == "two_side"
        assert out["distance"] == 10.0 and out["distance2"] == 5.0

    def test_rejects_taper(self):
        _install([_sketch("S")])
        res = ex.handler(sketch_name="S", extent="two_side", distance=10, distance2=5, taper_deg=3)
        assert res["isError"] is True
        assert "two_side" in res["message"] and "taper" in res["message"]

    def test_rejects_symmetric(self):
        _install([_sketch("S")])
        res = ex.handler(sketch_name="S", extent="two_side", distance=10, distance2=5, symmetric=True)
        assert res["isError"] is True and "symmetric" in res["message"]

    def test_needs_both_distances_nonzero(self):
        _install([_sketch("S")])
        res = ex.handler(sketch_name="S", extent="two_side", distance=10, distance2=0)
        assert res["isError"] is True and "non-zero" in res["message"]

    def test_setTwoSidesDistanceExtent_false_is_reported(self):
        ef = _install([_sketch("S")])
        ef.next_result = False
        res = ex.handler(sketch_name="S", extent="two_side", distance=10, distance2=5)
        assert res["isError"] is True
        assert "setTwoSidesDistanceExtent returned false" in res["message"]


# ── parameter-expression distance (createByString) + the model-parameter linkage read-back ──────────
# 'distance' accepts a parameter EXPRESSION string ('StockZ/2', '25 mm') routed through
# ValueInput.createByString (ties the feature to a parameter), validated via the units engine so an
# unresolvable one is refused BY NAME. And every extrude NAMES the model parameters (dNN) it created so
# the param_set retarget path is discoverable without fishing through param_get.

def _units_mgr(valid=("25 mm", "StockZ/2", "TestLen * 2")):
    """A units engine resolving only `valid`, each expression carrying its own length unit."""
    return FakeUnitsManager(valid=valid, dimensioned=valid)


class _MP:
    def __init__(self, name):
        self.name = name


class _Ext:
    def __init__(self, dist_name):
        self.distance = _MP(dist_name)


class _ParamFeature(FakeFeature):
    """A feature exposing the extent/taper model PARAMETERS the linkage read-back names."""
    def __init__(self, dist="d1", taper="d2", dist2=None, taper2=None, **kw):
        super().__init__(**kw)
        self.extentOne = _Ext(dist)
        self.taperAngleOne = _MP(taper)
        self.hasTwoExtents = dist2 is not None
        self.extentTwo = _Ext(dist2) if dist2 is not None else None
        self.taperAngleTwo = _MP(taper2) if taper2 is not None else None


def _with_units_mgr(mgr=None):
    """Attach a fake units engine to the installed design so string expressions can evaluate."""
    ex.app.activeProduct.unitsManager = mgr or _units_mgr()


class TestExpressionDistance:
    def test_string_expression_uses_createByString_not_scaled_real(self):
        ef = _install([_sketch("S")])
        _with_units_mgr()
        out = _payload(ex.handler(sketch_name="S", distance="25 mm", units="mm"))
        sym, dist = ef.last_input.distance_extent
        assert dist == ("str", "25 mm")        # createByString - NOT a scaled createByReal
        assert out["distance"] == "25 mm"      # echoed as the expression, not a rounded number

    def test_expression_references_a_parameter(self):
        ef = _install([_sketch("S")])
        _with_units_mgr()
        _payload(ex.handler(sketch_name="S", distance="StockZ/2"))
        _sym, dist = ef.last_input.distance_extent
        assert dist == ("str", "StockZ/2")

    def test_numeric_string_is_a_literal_scaled_via_createByReal(self):
        # a PLAIN numeric string is a literal, not an expression - it still scales through createByReal.
        ef = _install([_sketch("S")])
        out = _payload(ex.handler(sketch_name="S", distance="6", units="mm"))
        _sym, dist = ef.last_input.distance_extent
        assert dist[0] == "real" and abs(dist[1] - 0.6) < 1e-9   # "6" mm -> 0.6 cm, the literal path
        assert out["distance"] == 6.0

    def test_unresolvable_expression_refused_by_name(self):
        _install([_sketch("S")])
        _with_units_mgr()                       # only the known set evaluates; this one does not
        res = ex.handler(sketch_name="S", distance="NoSuchParam * 2")
        assert res["isError"] is True
        assert "NoSuchParam * 2" in res["message"]

    def test_two_side_accepts_expression_per_side(self):
        ef = _install([_sketch("S")])
        _with_units_mgr(_units_mgr(valid=("StockZ/2", "10 mm")))
        _payload(ex.handler(sketch_name="S", extent="two_side", distance="StockZ/2", distance2="10 mm"))
        d1, d2 = ef.last_input.two_sides_distance
        assert d1 == ("str", "StockZ/2") and d2 == ("str", "10 mm")


class TestModelParameterLinkage:
    def test_names_the_distance_and_taper_model_parameters(self):
        ef = _install([_sketch("S")])
        ef.add = lambda inp: _ParamFeature(is_solid=getattr(inp, "isSolid", True))
        out = _payload(ex.handler(sketch_name="S", distance=5))
        assert out["model_parameters"]["distance"] == "d1"
        assert out["model_parameters"]["taper"] == "d2"

    def test_two_side_names_both_side_parameters(self):
        ef = _install([_sketch("S")])
        ef.add = lambda inp: _ParamFeature(dist="d1", taper="d2", dist2="d3", taper2="d4",
                                           is_solid=getattr(inp, "isSolid", True))
        out = _payload(ex.handler(sketch_name="S", extent="two_side", distance=10, distance2=5))
        assert out["model_parameters"]["distance"] == "d1"
        assert out["model_parameters"]["distance2"] == "d3"

    def test_note_advertises_the_model_parameters(self):
        ef = _install([_sketch("S")])
        ef.add = lambda inp: _ParamFeature(is_solid=getattr(inp, "isSolid", True))
        out = _payload(ex.handler(sketch_name="S", distance=5))
        assert "model_parameters" in out["note"] and "param_set" in out["note"]

    def test_absent_when_no_distance_parameter_exists(self):
        # a plain fake feature (no extentOne/taper params, e.g. a through_all/to_face extent) -> the key
        # is simply omitted, never a null or a crash.
        ef = _install([_sketch("S")])
        out = _payload(ex.handler(sketch_name="S", distance=5))   # add() returns a bare FakeFeature
        assert "model_parameters" not in out


class TestDistanceReadBack:
    """The depth the created feature REPORTS, against the number the units engine evaluated the
    request to. A feature that lands at the wrong depth is returned as success by the API, so the
    extent's own distance ModelParameter is the only thing that can contradict it - and because the
    feature HAS landed, a mismatch names both values and says what remains in the timeline."""

    def _feature(self, value_cm, name="Extrude1"):
        """A created extrude whose first side reports `value_cm` on its distance ModelParameter."""
        f = FakeFeature(name=name)
        f.extentOne = types.SimpleNamespace(
            distance=types.SimpleNamespace(name="d1", value=value_cm))
        return f

    def _two_side_feature(self, one_cm, two_cm, name="Extrude1"):
        """A created two-sided extrude: side one on extentOne, side two on extentTwo, each with its
        own distance ModelParameter - the shape a two-sided distance extent lands in (measured)."""
        f = self._feature(one_cm, name)
        f.hasTwoExtents = True
        f.extentTwo = types.SimpleNamespace(
            distance=types.SimpleNamespace(name="d2", value=two_cm))
        return f

    def test_a_matching_read_back_passes_silently(self):
        ef = _install([_sketch("S")])
        ef.add = lambda inp: self._feature(2.5)            # 25 mm -> 2.5 cm
        out = _payload(ex.handler(sketch_name="S", distance=25, units="mm"))
        assert out["extruded"] is True and out["distance"] == 25.0

    def test_a_mismatched_read_back_errors_naming_both_values(self):
        ef = _install([_sketch("S")])
        ef.add = lambda inp: self._feature(1.25)           # asked 25 mm, landed 12.5 mm
        res = ex.handler(sketch_name="S", distance=25, units="mm")
        assert res["isError"] is True
        msg = res["message"]
        assert "12.5 mm" in msg and "25.0 mm" in msg
        assert "Extrude1" in msg and "REMAINS in the timeline" in msg

    def test_the_sign_is_part_of_the_comparison(self):
        # A negative 'distance' reverses the extrude and the parameter keeps that sign, so a feature
        # reading +2.5 cm for a requested -25 mm went the other way - a magnitude-only compare would
        # pass it.
        ef = _install([_sketch("S")])
        ef.add = lambda inp: self._feature(2.5)
        res = ex.handler(sketch_name="S", distance=-25, units="mm")
        assert res["isError"] is True and "-25.0 mm" in res["message"]

    def test_a_matching_negative_read_back_passes(self):
        ef = _install([_sketch("S")])
        ef.add = lambda inp: self._feature(-2.5)
        out = _payload(ex.handler(sketch_name="S", distance=-25, units="mm"))
        assert out["extruded"] is True

    def test_an_expression_is_compared_against_what_it_evaluates_to(self):
        ef = _install([_sketch("S")])
        _with_units_mgr()                                  # a known expression evaluates to 2.5 cm
        ef.add = lambda inp: self._feature(1.25)
        res = ex.handler(sketch_name="S", distance="StockZ/2", units="mm")
        assert res["isError"] is True
        msg = res["message"]
        assert "StockZ/2" in msg and "12.5 mm" in msg and "25.0 mm" in msg

    def test_a_matching_expression_read_back_passes(self):
        ef = _install([_sketch("S")])
        _with_units_mgr()
        ef.add = lambda inp: self._feature(2.5)
        out = _payload(ex.handler(sketch_name="S", distance="StockZ/2", units="mm"))
        assert out["distance"] == "StockZ/2"

    def test_an_evaluation_that_answers_no_number_withholds_the_compare(self):
        # The units engine answered something that is not a number, so nothing here can judge the
        # feature's depth - treating that as zero would refuse an extrude that landed correctly.
        ef = _install([_sketch("S")])
        _with_units_mgr(types.SimpleNamespace(
            evaluateExpression=lambda expr, units=None: "eleven", defaultLengthUnits="mm"))
        ef.add = lambda inp: self._feature(2.5)
        out = _payload(ex.handler(sketch_name="S", distance="StockZ/2", units="mm"))
        assert out["extruded"] is True

    def test_a_feature_reporting_no_distance_number_withholds_the_compare(self):
        _install([_sketch("S")])                        # add() returns a bare FakeFeature
        out = _payload(ex.handler(sketch_name="S", distance=25, units="mm"))
        assert out["extruded"] is True

    def test_a_through_all_extent_is_never_distance_compared(self):
        # through_all carries no depth - 'distance' is only a direction sign there - so comparing it
        # against one would refuse every through-all extrude.
        ef = _install([_sketch("S")])
        ef.add = lambda inp: self._feature(2.5)
        out = _payload(ex.handler(sketch_name="S", distance=-25, units="mm", extent="through_all"))
        assert out["extruded"] is True and out["extent"] == "through_all"

    def test_a_two_side_extent_is_compared_side_by_side(self):
        # A two-sided extrude splits its request across TWO extent definitions - side one on
        # extentOne, side two on extentTwo, each reporting the magnitude ITS side was asked for
        # (measured). So each side is judged against its OWN request: 25/10 mm reads 2.5/1.0 cm.
        ef = _install([_sketch("S")])
        ef.add = lambda inp: self._two_side_feature(2.5, 1.0)
        out = _payload(ex.handler(sketch_name="S", extent="two_side", distance=25, distance2=10,
                                  units="mm"))
        assert out["extruded"] is True and out["extent"] == "two_side"

    def test_side_one_reading_the_other_sides_number_errors(self):
        # the swap the measurement rules out: extentOne reporting side TWO's depth. Comparing only
        # against 'distance' as a whole, or exempting two_side, passes this.
        ef = _install([_sketch("S")])
        ef.add = lambda inp: self._two_side_feature(1.0, 2.5)
        res = ex.handler(sketch_name="S", extent="two_side", distance=25, distance2=10, units="mm")
        assert res["isError"] is True
        msg = res["message"]
        assert "side-one distance" in msg and "10.0 mm" in msg and "25.0 mm" in msg
        assert "Extrude1" in msg and "REMAINS in the timeline" in msg

    def test_side_two_is_compared_against_distance2_not_distance(self):
        # side one lands correctly and side two does not - a compare that read both sides against
        # 'distance' would pass side one and mis-name what side two was asked for
        ef = _install([_sketch("S")])
        ef.add = lambda inp: self._two_side_feature(2.5, 0.4)
        res = ex.handler(sketch_name="S", extent="two_side", distance=25, distance2=10, units="mm")
        assert res["isError"] is True
        msg = res["message"]
        assert "side-two distance" in msg and "4.0 mm" in msg and "10.0 mm" in msg

    def test_a_two_side_feature_reporting_no_second_number_withholds_that_side(self):
        # extentTwo absent (a bare fake feature carries only extentOne): the second side has nothing
        # to judge, and treating that as zero would refuse an extrude that landed correctly
        ef = _install([_sketch("S")])
        ef.add = lambda inp: self._feature(2.5)
        out = _payload(ex.handler(sketch_name="S", extent="two_side", distance=25, distance2=10,
                                  units="mm"))
        assert out["extruded"] is True
        assert "Side-two distance was not depth-verified" in out["note"]
        assert "reported no depth number" in out["note"]

    def test_a_negative_two_side_request_is_left_uncompared(self):
        # the measurement covers POSITIVE two-sided requests; what either parameter stores for a
        # NEGATIVE one is not measured, so the side is not judged against an assumed convention.
        # The feature here reports a POSITIVE magnitude for a negative request - the very thing an
        # unmeasured convention would have to guess about.
        ef = _install([_sketch("S")])
        ef.add = lambda inp: self._two_side_feature(2.5, 1.0)
        out = _payload(ex.handler(sketch_name="S", extent="two_side", distance=25, distance2=-10,
                                  units="mm"))
        assert out["extruded"] is True and out["distance2"] == -10.0

    def test_a_skipped_side_is_disclosed_rather_than_reported_as_verified(self):
        # a success that skipped a depth compare must not be byte-indistinguishable from one that
        # PASSED it - every other success in this payload means the depth read back matched
        ef = _install([_sketch("S")])
        ef.add = lambda inp: self._two_side_feature(2.5, 1.0)
        out = _payload(ex.handler(sketch_name="S", extent="two_side", distance=25, distance2=-10,
                                  units="mm"))
        assert "Side-two distance was not depth-verified" in out["note"]
        assert "NEGATIVE two-sided request" in out["note"]
        # side one WAS judged, so it is not disclosed as unverified
        assert "Side-one distance was not depth-verified" not in out["note"]

    def test_a_verified_two_side_extrude_discloses_nothing(self):
        # the other side of the same boundary: both sides judged and matching means the note carries
        # no unverified clause at all, so the clause reads as a real signal
        ef = _install([_sketch("S")])
        ef.add = lambda inp: self._two_side_feature(2.5, 1.0)
        out = _payload(ex.handler(sketch_name="S", extent="two_side", distance=25, distance2=10,
                                  units="mm"))
        assert "not depth-verified" not in out["note"]

    def test_an_unevaluable_blind_distance_is_disclosed_too(self):
        # the SAME mechanism covers the single-sided compare: an evaluation that answered no number
        # withheld the verdict, and that silence is disclosed rather than passed off as verified
        ef = _install([_sketch("S")])
        _with_units_mgr(types.SimpleNamespace(
            evaluateExpression=lambda expr, units=None: "eleven", defaultLengthUnits="mm"))
        ef.add = lambda inp: self._feature(2.5)
        out = _payload(ex.handler(sketch_name="S", distance="StockZ/2", units="mm"))
        assert "Distance was not depth-verified" in out["note"]
        assert "answered no number" in out["note"]

    def test_a_two_side_expression_side_names_itself_in_the_refusal(self):
        ef = _install([_sketch("S")])
        _with_units_mgr()                                  # a known expression evaluates to 2.5 cm
        ef.add = lambda inp: self._two_side_feature(1.25, 1.0)
        res = ex.handler(sketch_name="S", extent="two_side", distance="StockZ/2", distance2=10,
                         units="mm")
        assert res["isError"] is True
        msg = res["message"]
        assert "StockZ/2" in msg and "side-one distance" in msg and "12.5 mm" in msg

    def test_side_two_quotes_its_OWN_expression_not_side_ones(self):
        # Each side's refusal carries the raw request of THAT side: side one is a plain literal here
        # and side two the expression, so a compare that reused side one's raw would drop the
        # expression clause entirely and leave the caller with no idea which input to fix.
        ef = _install([_sketch("S")])
        _with_units_mgr()                                  # 'StockZ/2' evaluates to 2.5 cm
        ef.add = lambda inp: self._two_side_feature(2.5, 1.0)   # side one ok, side two landed 10 mm
        res = ex.handler(sketch_name="S", extent="two_side", distance=25, distance2="StockZ/2",
                         units="mm")
        assert res["isError"] is True
        msg = res["message"]
        assert "side-two distance" in msg and "StockZ/2" in msg
        assert "10.0 mm" in msg and "25.0 mm" in msg

    def test_a_two_side_difference_at_the_tolerance_passes_and_one_past_it_errors(self):
        # the same 1e-6 cm band as the single-sided compare, on the SECOND side's own comparison
        for got_cm, is_error in ((2e-6, False), (3e-6, True)):
            ef = _install([_sketch("S")])
            ef.add = lambda inp, v=got_cm: self._two_side_feature(1.0, v)
            res = ex.handler(sketch_name="S", extent="two_side", distance=1, distance2=1e-6,
                             units="cm")
            assert res["isError"] is is_error, got_cm

    def test_a_symmetric_extent_is_compared_against_the_per_side_request(self):
        # A symmetric extent reports the per-SIDE number that was requested, so 25 mm each side
        # reads 2.5 cm - the request round-trips and the extrude passes.
        ef = _install([_sketch("S")])
        ef.add = lambda inp: self._feature(2.5)
        out = _payload(ex.handler(sketch_name="S", distance=25, units="mm", symmetric=True))
        assert out["extruded"] is True and out["symmetric"] is True

    def test_a_symmetric_extent_reading_the_full_length_errors_naming_both_values(self):
        # 5.0 cm for a 25 mm per-side request is the whole length, not the side - a depth that
        # disagrees with the request, and exempting symmetric from the compare would pass it.
        ef = _install([_sketch("S")])
        ef.add = lambda inp: self._feature(5.0)
        res = ex.handler(sketch_name="S", distance=25, units="mm", symmetric=True)
        assert res["isError"] is True
        msg = res["message"]
        assert "50.0 mm" in msg and "25.0 mm" in msg
        assert "Extrude1" in msg and "REMAINS in the timeline" in msg

    def test_a_difference_at_the_tolerance_passes_and_one_past_it_errors(self):
        # The exact boundary of the 1e-6 cm band. 2e-6 - 1e-6 is EXACT in binary floating point, so
        # the equal case really sits on the boundary rather than rounding under it.
        for got_cm, is_error in ((2e-6, False), (3e-6, True)):
            ef = _install([_sketch("S")])
            ef.add = lambda inp, v=got_cm: self._feature(v)
            res = ex.handler(sketch_name="S", distance=1e-6, units="cm")
            assert res["isError"] is is_error, got_cm


# ── cross-component cut read-back: the footgun warning + the honest 'component' field ──────────────
# A cut with NO target_bodies bores through EVERY body overlapping the profile sweep - co-located
# bodies in different components share 3D space. feature.bodies returns only the feature's own-component
# result body (confirmed live), so a design-wide pre/post volume snapshot is what reveals which bodies,
# and whose components, actually lost material: it powers both the warning and the 'component' field.

def _install_multi(bodyA_after, bodyB_after):
    """A design with two co-located components: the sketch lives in CompA (bodyA); CompB (bodyB) is a
    separate part sharing space. The fake cut sets each body's post-volume, so the snapshot read-back
    sees exactly which bodies lost material. Returns (bodyA, bodyB)."""
    ef = FakeExtrudeFeatures()
    bodyA = BRepBody("BodyA", volume=48.0, entity_token="BodyA")
    bodyB = BRepBody("BodyB", volume=48.0, entity_token="BodyB")
    sk = _sketch("S")
    compA = _component("CompA", sketches=[sk], bodies=[bodyA], ef=ef)
    compB = _component("CompB", bodies=[bodyB])
    root = _component("Root")
    sk.parentComponent = compA
    design = make_design(comp=root, all_components=[root, compA, compB])
    design.activeComponent = compA

    def _add(inp):
        bodyA.volume, bodyB.volume = bodyA_after, bodyB_after
        f = FakeFeature()
        f.parentComponent = compA
        return f
    ef.add = _add

    install(ex, design)
    _wire_adsk()
    import adsk.fusion
    adsk.fusion.BRepBody = BRepBody                      # so a target_bodies name resolves via BodyRef
    return bodyA, bodyB


class TestAffectedBodies:
    def test_reports_only_bodies_that_lost_volume(self):
        class B:
            def __init__(self, v): self.volume = v
        a, b = B(10.0), B(10.0)
        snap = [(a, "A", "CompA", 10.0), (b, "B", "CompB", 10.0)]
        a.volume = 4.0                       # A lost material; B untouched
        assert ex._affected_bodies(snap) == [("A", "CompA", 6.0)]

    def test_consumed_body_reported_with_none_removed(self):
        class Dead:
            @property
            def volume(self):
                raise RuntimeError("deleted")   # a body an intersect consumed whole
        snap = [(Dead(), "D", "CompB", 5.0)]
        assert ex._affected_bodies(snap) == [("D", "CompB", None)]


class TestCrossComponentCut:
    def test_unscoped_cut_bleeding_into_other_component_warns(self):
        # Case: unscoped cut, both components visible - material leaves BOTH CompA (sketch owner) and CompB.
        _install_multi(bodyA_after=45.6, bodyB_after=45.6)
        out = _payload(ex.handler(sketch_name="S", distance=5, operation="cut"))
        assert out["cut_touched_other_components"] == ["CompB"]
        assert out["affected_components"] == ["CompA", "CompB"]
        note = out["note"]
        assert "WARNING" in note and "target_bodies" in note
        # The warning must teach the VERIFIED rule (confirmed live): an unscoped cut takes every
        # VISIBLE intersecting body, and names BOTH levers - pass target_bodies, or hide the bodies
        # that must survive (a named body wins over visibility, i.e. is cut even if hidden).
        assert "VISIBLE" in note and "hidden bodies are spared" in note
        assert "hide the bodies that must be spared" in note
        assert "a named body is cut even if hidden" in note

    def test_component_field_names_where_material_landed_not_sketch_owner(self):
        # Case: the cut removes material ONLY from CompB, though the sketch lives in CompA. The
        # 'component' field must name CompB (where it landed), not the sketch's owning component.
        _install_multi(bodyA_after=48.0, bodyB_after=45.6)   # CompA untouched
        out = _payload(ex.handler(sketch_name="S", distance=5, operation="cut"))
        assert out["component"] == "CompB"
        assert out["cut_touched_other_components"] == ["CompB"]

    def test_same_component_cut_gets_no_warning(self):
        # A cut confined to the sketch's own component (CompB untouched) warns nobody and names CompA.
        _install_multi(bodyA_after=45.6, bodyB_after=48.0)   # only CompA lost material
        out = _payload(ex.handler(sketch_name="S", distance=5, operation="cut"))
        assert out["component"] == "CompA"
        assert "cut_touched_other_components" not in out
        assert "affected_components" not in out
        assert "WARNING" not in out["note"]

    def test_scoped_cut_never_warns_even_when_other_component_changed(self):
        # With target_bodies given the agent chose the bodies explicitly, so scoped_to suppresses the
        # warning even if the affected-set read-back still shows a co-located component changed.
        _install_multi(bodyA_after=45.6, bodyB_after=45.6)
        out = _payload(ex.handler(sketch_name="S", distance=5, operation="cut", target_bodies="BodyA"))
        assert out["scoped_to_bodies"] == ["BodyA"]
        assert "cut_touched_other_components" not in out
        assert "WARNING" not in out["note"]

    def test_hidden_colocated_body_is_spared_so_no_warning(self):
        # THE VISIBILITY RULE (confirmed live): an unscoped cut takes only the VISIBLE intersecting
        # bodies. A hidden co-located body loses no volume, so the volume-diff detection has nothing
        # to flag - the footgun warning must NOT fire off a spared body. At the mock level "hidden and
        # spared" is exactly "volume unchanged", so bodyB keeps its pre-cut volume here.
        _install_multi(bodyA_after=45.6, bodyB_after=48.0)   # CompB hidden -> spared -> unchanged
        out = _payload(ex.handler(sketch_name="S", distance=5, operation="cut"))
        assert out["component"] == "CompA"
        assert "cut_touched_other_components" not in out
        assert "affected_components" not in out
        assert "WARNING" not in out["note"]

    def test_scoped_cut_to_other_component_body_lands_there_no_warning(self):
        # Probe (c) at the mock level: target_bodies explicitly names the OTHER component's body, so
        # the cut lands on CompB alone (CompA, unnamed, is spared). scoped_to suppresses the warning
        # and 'component' names where the material actually left - CompB, not the sketch's own CompA.
        _install_multi(bodyA_after=48.0, bodyB_after=45.6)   # only CompB (the named body) loses material
        out = _payload(ex.handler(sketch_name="S", distance=5, operation="cut", target_bodies="CompB"))
        assert out["scoped_to_bodies"] == ["BodyB"]
        assert out["component"] == "CompB"
        assert "cut_touched_other_components" not in out
        assert "WARNING" not in out["note"]


# ── no-target-body direction teaching (PLATFORM behavior) ────────────────────────────────────────
# A sketch ON a body's face has its normal pointing AWAY from the material, so through_all's default
# (and symmetric) direction hits pure air and Fusion raises 'body not found to extrude through'. The
# direction MAPPING is correct - a negative 'distance' cuts into the body - so the error TEACHES the
# flip at the failure moment.

class TestBodySplitDisconnection:
    """A cut/intersect that DISCONNECTS the target leaves it in several pieces. The extruded profile
    removes no bodies, so a NET increase in the design-wide solid count is split-off pieces - warned,
    naming the pieces, so a later op does not silently target the wrong one."""

    def test_cut_that_disconnects_target_warns(self):
        ef = _install([_sketch("S")])
        root = ex.app.activeProduct.rootComponent
        bar = BRepBody("Bar", volume=100.0)
        root.bRepBodies = _NamedCollection([bar])
        piece2 = BRepBody("Bar1", volume=40.0)

        def _add(inp):
            root.bRepBodies._items.append(piece2)     # the cut disconnected the bar into a 2nd body
            f = FakeFeature()
            f.bodies = type("BB", (), {"count": 2, "item": staticmethod(lambda i: [bar, piece2][i])})()
            return f
        ef.add = _add
        out = _payload(ex.handler(sketch_name="S", operation="cut", distance=-5))
        assert out["body_split"] == ["Bar", "Bar1"]
        assert "DISCONNECTED" in out["note"]

    def test_blind_cut_that_does_not_disconnect_no_warning(self):
        # a cut that removes material without splitting leaves the solid count unchanged -> no warning.
        ef = _install([_sketch("S")])
        root = ex.app.activeProduct.rootComponent
        root.bRepBodies = _NamedCollection([BRepBody("Bar", volume=100.0)])
        out = _payload(ex.handler(sketch_name="S", operation="cut", distance=-5))
        assert "body_split" not in out

    def test_new_operation_never_flagged_as_split(self):
        # a 'new' extrude makes a body by design - the split warning is scoped to cut/intersect.
        ef = _install([_sketch("S")])
        root = ex.app.activeProduct.rootComponent
        root.bRepBodies = _NamedCollection([BRepBody("Bar", volume=100.0)])
        out = _payload(ex.handler(sketch_name="S", distance=5))   # operation defaults 'new'
        assert "body_split" not in out


# ── the compute-failed feature and the scoped-cut no-op (the false-ok pair) ─────────────────────────
# add() hands back a truthy feature object for a compute Fusion FAILED, and a cut scoped with
# 'target_bodies' whose profile reaches none of them is exactly that case: measured live, the failure
# read the WARNING health state carrying "No target body!Compute Failed" while no volume moved. Both
# the health state and the design-wide effect evidence gate the result, and the inert feature is
# rolled back with the timeline re-read as the proof.


def _fail_feature(state="warning", message="No target body!Compute Failed", name="Extrude3",
                  timeline=None, delete_ok=True):
    """The feature object a FAILED compute hands back: healthState set to the warning/error member,
    the message beside it, and a deleteMe whose success shows up as a SHRINKING timeline count (what
    the rollback sentence is read back from) - or a decline that leaves the count where it was."""
    import adsk.fusion
    states = adsk.fusion.FeatureHealthStates
    f = FakeFeature(name=name)
    f.healthState = (states.WarningFeatureHealthState if state == "warning"
                     else states.ErrorFeatureHealthState)
    f.errorOrWarningMessage = message

    def _delete():
        if delete_ok and timeline is not None:
            timeline.count -= 1
        return delete_ok
    f.deleteMe = _delete
    return f


def _install_scoped_cut(volume_after=48.0, feature=None, timeline_count=None):
    """A two-component design (the sketch's CompA holds BodyA, CompB holds BodyB) whose cut leaves
    BodyA at `volume_after`, with an optional replacement feature and timeline. Returns bodyA."""
    bodyA, _bodyB = _install_multi(bodyA_after=volume_after, bodyB_after=48.0)
    design = ex.app.activeProduct
    if timeline_count is not None:
        design.timeline = types.SimpleNamespace(count=timeline_count)
    if feature is not None:
        ef = design.activeComponent.features.extrudeFeatures
        prior = ef.add

        def _add(inp):
            prior(inp)                      # keep the canned volume effect
            return feature
        ef.add = _add
    return bodyA


class TestComputeFailedFeature:
    def test_a_compute_failed_extrude_is_an_error_not_an_ok_with_a_warning(self):
        tl = types.SimpleNamespace(count=4)
        _install_scoped_cut(feature=_fail_feature(timeline=tl), timeline_count=4)
        ex.app.activeProduct.timeline = tl
        res = ex.handler(sketch_name="S", distance=-20, operation="cut", target_bodies="BodyA")
        assert res["isError"] is True
        msg = res["message"]
        assert "Extrude3" in msg and "FAILED compute" in msg
        # the SHARED condensation: the sentence before Fusion's repeating 'Compute Failed' marker,
        # never the raw blob (a prefix of which lands mid-word)
        assert "No target body!" in msg and "Compute Failed" not in msg
        assert "health state: warning" in msg          # the STATE, read - not the message text
        assert "removed nothing" in msg                # backed by the volume/census evidence
        assert "BodyA" in msg                          # the scoped body that was not reached
        assert "rolled back" in msg and tl.count == 3  # the re-read, not deleteMe's own answer

    def test_the_published_message_is_one_whole_condensed_sentence(self):
        # Fusion's errorOrWarningMessage carries embedded NEWLINES and REPEATS its sentence, joined
        # by the 'Compute Failed' marker plus the feature's own name. The shared reader keeps the
        # first sentence with its whitespace collapsed; a local raw slice republishes the blob.
        tl = types.SimpleNamespace(count=4)
        blob = ("No target body!\n\nCheck the profile.Compute FailedExtrude3"
                "No target body!Compute FailedExtrude3")
        _install_scoped_cut(feature=_fail_feature(message=blob, timeline=tl), timeline_count=4)
        ex.app.activeProduct.timeline = tl
        res = ex.handler(sketch_name="S", distance=-20, operation="cut", target_bodies="BodyA")
        assert res["isError"] is True
        msg = res["message"]
        assert "No target body! Check the profile." in msg
        assert "Compute Failed" not in msg and "\n" not in msg

    def test_the_error_health_state_is_caught_too(self):
        tl = types.SimpleNamespace(count=2)
        _install_scoped_cut(feature=_fail_feature(state="error", message="", timeline=tl))
        ex.app.activeProduct.timeline = tl
        res = ex.handler(sketch_name="S", distance=-20, operation="cut", target_bodies="BodyA")
        assert res["isError"] is True
        assert "health state: error" in res["message"]
        assert "it reports no message" in res["message"]   # never a fabricated cause

    def test_a_rollback_that_did_not_take_names_what_remains(self):
        tl = types.SimpleNamespace(count=5)
        _install_scoped_cut(feature=_fail_feature(timeline=tl, delete_ok=False))
        ex.app.activeProduct.timeline = tl
        res = ex.handler(sketch_name="S", distance=-20, operation="cut", target_bodies="BodyA")
        assert res["isError"] is True
        assert "REMAINS in the timeline" in res["message"]
        assert "design_delete_feature" in res["message"]
        assert tl.count == 5

    def test_an_unreadable_timeline_never_claims_the_rollback_was_confirmed(self):
        # No timeline to re-read (a direct-modelling design): the sentence says the rollback could
        # not be confirmed instead of asserting the design is clean.
        _install_scoped_cut(feature=_fail_feature())
        res = ex.handler(sketch_name="S", distance=-20, operation="cut", target_bodies="BodyA")
        assert res["isError"] is True
        assert "could not be re-read to confirm" in res["message"]
        assert "rolled back -" not in res["message"]

    def test_a_failed_compute_that_DID_change_geometry_is_never_rolled_back(self):
        # The rollback is only for a feature the evidence shows landed nothing. With material
        # measurably gone, deleting the feature would delete a real effect - so the error states the
        # effect and leaves the feature standing.
        tl = types.SimpleNamespace(count=4)
        _install_scoped_cut(volume_after=45.6, feature=_fail_feature(timeline=tl))
        ex.app.activeProduct.timeline = tl
        res = ex.handler(sketch_name="S", distance=-20, operation="cut", target_bodies="BodyA")
        assert res["isError"] is True
        msg = res["message"]
        assert "Material DID change: BodyA in CompA (-2.4 cm3)" in msg
        assert "LEFT in the timeline" in msg and "design_delete_feature" in msg
        assert "rolled back -" not in msg and "removed nothing" not in msg
        assert tl.count == 4                      # the feature is still there

    def test_a_failed_compute_whose_only_evidence_is_the_census_keeps_the_feature(self):
        # A body gone from the design while its held wrapper still reads the pre-cut volume: no
        # affected row, but the census moved - an effect that is not ruled out, so no rollback.
        tl = types.SimpleNamespace(count=4)
        bodyA = _install_scoped_cut(feature=_fail_feature(timeline=tl))
        ex.app.activeProduct.timeline = tl
        compA = ex.app.activeProduct.activeComponent
        ef = compA.features.extrudeFeatures
        prior = ef.add

        def _add(inp):
            f = prior(inp)
            compA.bRepBodies._items.remove(bodyA)
            return f
        ef.add = _add
        res = ex.handler(sketch_name="S", distance=-20, operation="cut", target_bodies="BodyA")
        assert res["isError"] is True
        assert "solid body count changed by -1" in res["message"]
        assert "LEFT in the timeline" in res["message"]
        assert tl.count == 4

    def test_a_failed_new_extrude_claims_no_effect_verdict_and_keeps_the_feature(self):
        # operation='new' takes no volume snapshot, so nothing here can say whether a body landed:
        # the refusal must neither claim a no-op nor delete work it did not measure.
        tl = types.SimpleNamespace(count=2)
        _install_scoped_cut(feature=_fail_feature(timeline=tl))
        ex.app.activeProduct.timeline = tl
        res = ex.handler(sketch_name="S", distance=20, operation="new")
        assert res["isError"] is True
        msg = res["message"]
        assert "Extrude3" in msg and "FAILED compute" in msg
        assert "NOT read for a 'new' extrude" in msg
        assert "LEFT in the timeline" in msg
        assert "removed nothing" not in msg and "rolled back -" not in msg
        assert tl.count == 2

    def test_the_timeline_items_state_is_read_past_a_healthy_feature(self):
        # The failure was MEASURED on the timeline item, so a feature answering HEALTHY (or nothing)
        # for itself must not turn that into a clean success.
        import adsk.fusion
        tl = types.SimpleNamespace(count=4)
        item = _fail_feature(timeline=tl)
        f = FakeFeature(name="Extrude3")
        f.healthState = adsk.fusion.FeatureHealthStates.HealthyFeatureHealthState
        f.timelineObject = item
        f.deleteMe = item.deleteMe
        _install_scoped_cut(feature=f)
        ex.app.activeProduct.timeline = tl
        res = ex.handler(sketch_name="S", distance=-20, operation="cut", target_bodies="BodyA")
        assert res["isError"] is True
        assert "health state: warning" in res["message"]
        assert "No target body!" in res["message"] and "Compute Failed" not in res["message"]

    def test_a_healthy_feature_passes_the_health_gate(self):
        import adsk.fusion
        f = FakeFeature()
        f.healthState = adsk.fusion.FeatureHealthStates.HealthyFeatureHealthState
        _install_scoped_cut(volume_after=45.6, feature=f)
        out = _payload(ex.handler(sketch_name="S", distance=-20, operation="cut",
                                  target_bodies="BodyA"))
        assert out["extruded"] is True and out["component"] == "CompA"

    def test_the_postcondition_never_counts_a_compute_failed_feature(self):
        # features_verified comes from the FeatureHealthy postcondition, which runs only on an ok
        # result - so the handler's own refusal is what keeps a failed feature out of the count.
        tl = types.SimpleNamespace(count=4)
        _install_scoped_cut(feature=_fail_feature(timeline=tl))
        ex.app.activeProduct.timeline = tl
        asserted = ex._assert.wrap(ex.handler, [ex._assert.FeatureHealthy()])
        res = asserted(sketch_name="S", distance=-20, operation="cut", target_bodies="BodyA")
        assert res["isError"] is True
        assert "features_verified" not in json.dumps(res)
        assert "feature_warnings" not in json.dumps(res)


class TestScopedCutNoOp:
    def test_a_scoped_cut_that_changed_nothing_is_an_error_naming_the_bodies(self):
        tl = types.SimpleNamespace(count=3)
        _install_scoped_cut()                       # BodyA keeps its 48.0 - nothing was removed
        ex.app.activeProduct.timeline = tl
        res = ex.handler(sketch_name="S", distance=-20, operation="cut", target_bodies="BodyA")
        assert res["isError"] is True
        assert "changed nothing" in res["message"] and "BodyA" in res["message"]
        assert "target_bodies" in res["message"]

    def test_a_scoped_cut_that_removed_material_is_ok(self):
        _install_scoped_cut(volume_after=45.6)
        out = _payload(ex.handler(sketch_name="S", distance=-20, operation="cut",
                                  target_bodies="BodyA"))
        assert out["scoped_to_bodies"] == ["BodyA"] and out["component"] == "CompA"

    def test_a_scoped_cut_that_consumed_the_body_whole_is_not_flagged(self):
        # A consumed body reports no volume DROP (its volume stops reading at all), so the gate must
        # judge on the consumed row too, not on the volume delta alone.
        bodyA = _install_scoped_cut()
        ef = ex.app.activeProduct.activeComponent.features.extrudeFeatures
        prior = ef.add

        def _add(inp):
            f = prior(inp)
            del bodyA.volume            # the cut consumed it whole
            return f
        ef.add = _add
        out = _payload(ex.handler(sketch_name="S", distance=-20, operation="cut",
                                  target_bodies="BodyA"))
        assert out["extruded"] is True

    def test_a_consumed_body_whose_wrapper_still_answers_is_seen_by_the_census(self):
        # A held body wrapper can keep answering its pre-cut volume after the body itself is gone, so
        # the volume diff alone would read a whole-body consumption as 'nothing happened'. The
        # design-wide solid census still sees the body leave, which is why both back the no-op claim.
        bodyA = _install_scoped_cut()
        compA = ex.app.activeProduct.activeComponent
        ef = compA.features.extrudeFeatures
        prior = ef.add

        def _add(inp):
            f = prior(inp)
            compA.bRepBodies._items.remove(bodyA)    # gone, while the wrapper still reads 48.0
            return f
        ef.add = _add
        out = _payload(ex.handler(sketch_name="S", distance=-20, operation="cut",
                                  target_bodies="BodyA"))
        assert out["extruded"] is True

    def test_an_unscoped_cut_that_changed_nothing_stays_ok(self):
        # Unscoped, Fusion refuses a miss itself ("No target body found to cut or intersect!") and
        # that refusal surfaces through the add() handler - so this gate stays scoped to target_bodies.
        _install_scoped_cut()
        out = _payload(ex.handler(sketch_name="S", distance=-20, operation="cut"))
        assert out["extruded"] is True

    def test_a_scoped_join_is_not_gated_on_removed_material(self):
        # 'join' ADDS material - no body is expected to lose any, so the cut/intersect gate is off.
        _install_scoped_cut()
        out = _payload(ex.handler(sketch_name="S", distance=-20, operation="join",
                                  target_bodies="BodyA"))
        assert out["extruded"] is True


class TestNoTargetBodyDirectionTeaching:
    def test_a_blind_distance_cut_that_reached_nothing_teaches_the_opposite_sign(self):
        # The other platform text for the same trap, on the extent every cut uses by default.
        ef = _install([_sketch("S")])

        def _raise(inp):
            raise RuntimeError("3 : No target body found to cut or intersect!")
        ef.add = _raise
        res = ex.handler(sketch_name="S", distance=-5, operation="cut")
        assert res["isError"] is True
        msg = res["message"]
        assert "POSITIVE" in msg and "'distance' was negative" in msg
        # both remedies survive: the sign flip did not displace "does the profile overlap it at all"
        assert "AWAY from the material" in msg and "overlaps the body at all" in msg

    def test_an_intersect_that_reached_nothing_teaches_the_same_way(self):
        # Fusion words the refusal for both operations at once, so the gate takes both.
        ef = _install([_sketch("S")])

        def _raise(inp):
            raise RuntimeError("3 : No target body found to cut or intersect!")
        ef.add = _raise
        res = ex.handler(sketch_name="S", distance=5, operation="intersect")
        assert res["isError"] is True
        assert "NEGATIVE" in res["message"] and "'distance' was positive" in res["message"]

    def test_a_symmetric_cut_is_not_told_to_flip_a_sign(self):
        # It already went both ways from the sketch plane, so no sign reaches a body it missed.
        ef = _install([_sketch("S")])

        def _raise(inp):
            raise RuntimeError("3 : No target body found to cut or intersect!")
        ef.add = _raise
        res = ex.handler(sketch_name="S", distance=5, operation="cut", symmetric=True)
        assert res["isError"] is True
        msg = res["message"]
        assert "BOTH ways" in msg and "overlaps no participant body" in msg
        assert "NEGATIVE" not in msg and "POSITIVE" not in msg

    def test_a_symmetric_through_all_cut_is_not_told_to_flip_a_sign_either(self):
        # _through_all_direction_key answers 'symmetric' without reading the sign, so a direction
        # remedy here points at a knob this extrude never consulted.
        ef = _install([_sketch("S")])

        def _raise(inp):
            raise RuntimeError("3 : Could not complete Through All Extrude, body not found to "
                               "extrude through.")
        ef.add = _raise
        res = ex.handler(sketch_name="S", operation="cut", extent="through_all", symmetric=True)
        assert res["isError"] is True
        msg = res["message"]
        assert "BOTH ways" in msg
        assert "POSITIVE" not in msg and "NEGATIVE" not in msg

    def test_an_unreadable_distance_sign_is_not_stated(self):
        # The 'distance' was <sign>' clause is dropped rather than guessed at.
        assert "'distance' was" not in ex._no_target_body_hint("distance", None)


def _install_with_bodies(*body_names):
    """The extrude surface over a component that already holds these bodies - the before-image a
    join's result body is told new-vs-existing against."""
    ef = FakeExtrudeFeatures()
    install(ex, make_design(comp=_component("Root", sketches=[_sketch("S")],
                                            bodies=list(body_names), ef=ef)))
    _wire_adsk()
    return ef


class TestJoinLandedANewBody:
    def test_a_result_body_the_component_did_not_hold_before_names_model_combine(self):
        _install_with_bodies("Bar")
        out = _payload(ex.handler(sketch_name="S", distance=5, operation="join"))
        assert "landed a NEW body (Body1)" in out["note"]
        assert "model_combine(join)" in out["note"]

    def test_a_join_that_grew_the_body_already_there_appends_nothing(self):
        # the feature's result body IS the one the component held - the join fused, say nothing
        _install_with_bodies("Body1")
        out = _payload(ex.handler(sketch_name="S", distance=5, operation="join"))
        assert "NEW body" not in out["note"]
        assert "'distance' was positive" in ex._no_target_body_hint("distance", 5)

    def test_the_same_refusal_on_a_new_extrude_keeps_the_plain_hint(self):
        # 'no target body to cut or intersect' is a cut/intersect refusal; a 'new' body has no
        # participants, so that text there is not read as a direction problem.
        ef = _install([_sketch("S")])

        def _raise(inp):
            raise RuntimeError("3 : No target body found to cut or intersect!")
        ef.add = _raise
        res = ex.handler(sketch_name="S", distance=-5, operation="new")
        assert res["isError"] is True and "existing geometry" in res["message"]

    def test_body_not_found_teaches_negative_distance(self):
        ef = _install([_sketch("S")])

        def _raise(inp):
            raise RuntimeError("3 : Could not complete Through All Extrude, body not found to "
                               "extrude through.")
        ef.add = _raise
        res = ex.handler(sketch_name="S", operation="cut", extent="through_all")
        assert res["isError"] is True
        msg = res["message"]
        assert "NEGATIVE" in msg and "sketch-plane normal" in msg

    def test_generic_add_failure_keeps_the_plain_hint(self):
        # a non-through_all add() failure is not a direction problem - it keeps the existing hint.
        ef = _install([_sketch("S")])

        def _raise(inp):
            raise RuntimeError("some other kernel error")
        ef.add = _raise
        res = ex.handler(sketch_name="S", distance=5, operation="cut")
        assert res["isError"] is True
        assert "existing geometry" in res["message"] and "sketch-plane normal" not in res["message"]
