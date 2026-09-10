"""Unit tests for sketch_get's routing between its two depths, and the summary's design-wide walk.

sketch_get is ONE tool switched by specificity: no sketch_name -> summary list; a sketch_name ->
full detail (delegated to the _sketch_detail engine). The return is always about sketches; only the
depth changes. The routing tests pin BOTH halves of each branch - the engine that must run, and the
other one, monkeypatched to a sentinel that fails the test if it is invoked at all (routing to both
depths costs a caller the heavy read they did not ask for, and reads green on a
'the right one ran' assertion alone). The walk tests pin that the summary reaches sketches in
SUB-components and tags each row with its owner.
"""

import json
import sys
import types

from conftest import MakeComp, Profile, load_tool, make_design, make_occurrence, make_sketch

sketches = load_tool("sketch_get")


def _never(what):
    """A stand-in for the depth that must NOT run on this call: invoked at all, it fails the test
    by name instead of quietly returning a result the assertions would never look at."""
    def _refuse(*args, **kwargs):
        raise AssertionError(f"{what} was invoked - sketch_get routed to the wrong depth")
    return _refuse


def _no_detail_engine(monkeypatch):
    """Install a _sketch_detail engine whose HANDLER refuses to be called. Same module-table seam
    the delegation test installs its stub through, so it intercepts the same lookup.

    'scope_components' still answers: the engine owns the component-scope vocabulary for BOTH
    depths, so the summary depth legitimately calls it - only the one-sketch handler is off limits
    here."""
    monkeypatch.setitem(sys.modules, "mcpServer.tools._sketch_detail",
                        types.SimpleNamespace(handler=_never("the _sketch_detail engine"),
                                              scope_components=lambda d, raw: ([], None)))


class TestSketchGetRouting:
    def test_no_name_lists_summary(self, monkeypatch):
        called = {}

        def fake_summary(component="", max_results=0):
            called["summary"] = True
            return {"isError": False}
        monkeypatch.setattr(sketches, "_list_sketches", fake_summary)
        _no_detail_engine(monkeypatch)          # the detail slice must NOT be reached
        res = sketches.handler(sketch_name="")
        assert called.get("summary") is True
        assert res["isError"] is False

    def test_no_name_threads_the_component_scope_into_the_summary(self, monkeypatch):
        # Both depths take the scope; a router that dropped it on this branch would silently list
        # the WHOLE design when one component was asked for.
        seen = {}

        def fake_summary(component="", max_results=0):
            seen["component"], seen["max_results"] = component, max_results
            return {"isError": False}
        monkeypatch.setattr(sketches, "_list_sketches", fake_summary)
        _no_detail_engine(monkeypatch)
        sketches.handler(sketch_name="", component="Frame", max_results=60)
        # the page size rides with the scope: dropped here, the note's remedy would name a knob the
        # router never passed on, and the withheld rows would stay unreachable.
        assert seen.get("component") == "Frame" and seen.get("max_results") == 60

    def test_name_delegates_to_detail_engine(self, monkeypatch):
        seen = {}

        class FakeDetail:
            @staticmethod
            def handler(sketch_name="", include_entities=False, units="mm", component=""):
                seen["name"] = sketch_name
                seen["include_entities"] = include_entities
                seen["units"] = units
                seen["component"] = component
                return {"isError": False, "content": [{"type": "text", "text": "{}"}]}

        # The handler resolves the engine by NAME in the module table on every call, so installing
        # the stub there routes it - whatever else has already imported the real engine. Binding the
        # engine through the package attribute instead makes this test pass or fail on load order.
        monkeypatch.setitem(sys.modules, "mcpServer.tools._sketch_detail", FakeDetail)
        # the reciprocal negative: a named sketch must not ALSO run the design-wide summary walk
        monkeypatch.setattr(sketches, "_list_sketches", _never("the summary walk"))

        res = sketches.handler(sketch_name="Emblem", include_entities=True, units="in",
                                          component="Frame")
        assert seen.get("name") == "Emblem"     # routed to the detail engine with the name
        assert seen.get("include_entities") is True   # the zoom flag is threaded through
        assert seen.get("units") == "in"        # the units param is threaded through too
        # the scope reaches the engine that resolves it - dropped here, a scoped read of a shared
        # name would come back refused as ambiguous with the scope the caller gave ignored
        assert seen.get("component") == "Frame"
        assert res["isError"] is False

    def test_whitespace_name_treated_as_no_name(self, monkeypatch):
        called = {}

        def fake_summary(component="", max_results=0):
            called["summary"] = True
            return {"isError": False}
        monkeypatch.setattr(sketches, "_list_sketches", fake_summary)
        _no_detail_engine(monkeypatch)          # a blank-ish name is not a name to look up
        sketches.handler(sketch_name="   ")
        assert called.get("summary") is True     # blank-ish name -> summary, not detail


_comp_serial = iter(range(1, 10_000))


class TestSketchSummaryWalk:
    @staticmethod
    def _comp(name, sketch_names=()):
        # Like the live Component: no allComponents attribute (that collection is a Design
        # property), so reading it here raises AttributeError exactly as adsk does - and a DISTINCT
        # entityToken, which every live component has and which is the only thing separating two
        # components that wear one name.
        return MakeComp(name=name, sketches=[make_sketch(name=n) for n in sketch_names],
                        entity_token=f"comp-{name}-{next(_comp_serial)}")

    _occ = staticmethod(make_occurrence)         # the shared conftest occurrence fake

    def test_lists_sub_component_sketches_tagged_with_their_owner(self, monkeypatch):
        # A multi-part doc keeps each part's sketch in its own component; a walk that only reaches
        # the root reports sketch_count 0 for the whole design (observed live on a 3-component doc).
        root = self._comp("Root")
        frame = self._comp("Frame", ["FrameSketch"])
        ring = self._comp("OuterRing", ["OuterRingSketch"])
        d = make_design(comp=root, all_components=[root, frame, ring])
        monkeypatch.setattr(sketches._common, "design", lambda: d)
        res = sketches._list_sketches()
        payload = json.loads(res["content"][0]["text"])
        assert payload["sketch_count"] == 2
        assert {(r["name"], r["component"]) for r in payload["sketches"]} == {
            ("FrameSketch", "Frame"), ("OuterRingSketch", "OuterRing")}

    def _two_component_design(self, monkeypatch):
        root = self._comp("Root")
        frame = self._comp("Frame", ["Sketch1"])
        ring = self._comp("OuterRing", ["Sketch1"])
        d = make_design(comp=root, all_components=[root, frame, ring])
        monkeypatch.setattr(sketches._common, "design", lambda: d)

    def test_the_component_scope_narrows_the_list_to_that_component(self, monkeypatch):
        # Both components hold a "Sketch1" (Fusion numbers per component from 1), so the OWNER is
        # the only thing separating the rows - and the scope is how a caller asks for one.
        self._two_component_design(monkeypatch)
        res = sketches._list_sketches("OuterRing")
        payload = json.loads(res["content"][0]["text"])
        assert payload["sketch_count"] == 1
        assert [(r["name"], r["component"]) for r in payload["sketches"]] == [("Sketch1", "OuterRing")]

    def test_an_unknown_component_is_refused_rather_than_listed_as_empty(self, monkeypatch):
        # An empty list would read as "that component has no sketches" - the opposite of what was
        # read, and it hides the typo.
        self._two_component_design(monkeypatch)
        res = sketches._list_sketches("Ghost")
        assert res["isError"] is True
        assert "No component named 'Ghost'" in res["message"]
        assert "Frame" in res["message"] and "OuterRing" in res["message"]

    def test_no_scope_still_lists_every_component(self, monkeypatch):
        # The other side of the branch: the scope is opt-in, so a blank one must not narrow.
        self._two_component_design(monkeypatch)
        payload = json.loads(sketches._list_sketches("")["content"][0]["text"])
        assert payload["sketch_count"] == 2

    def test_rows_of_uniquely_named_components_carry_no_paths(self, monkeypatch):
        # The common design has nothing to tell apart, so no row pays for a path field - and these
        # components ARE placed by occurrences, so a path exists to publish and the NAME being
        # unique is the only reason it is withheld.
        frame = self._comp("Frame", ["Sketch1"])
        ring = self._comp("OuterRing", ["Sketch1"])
        root = MakeComp(name="Root", entity_token="comp-root",
                        occurrences=[self._occ("Frame:1", frame),
                                     self._occ("OuterRing:1", ring)])
        d = make_design(comp=root, all_components=[root, frame, ring])
        monkeypatch.setattr(sketches._common, "design", lambda: d)
        payload = json.loads(sketches._list_sketches("")["content"][0]["text"])
        assert payload["sketch_count"] == 2
        assert all("component_paths" not in r for r in payload["sketches"])
        assert "note" not in payload

    def _dense(self, monkeypatch, held, scope="", max_results=0):
        """One component holding `held` sketches beside a component holding one - the shape a
        sketch-dense document has (245 sketches over 82 components on one measured part, 71 KB of
        list)."""
        big = self._comp("Bench", [f"Sketch{i}" for i in range(held)])
        small = self._comp("Plate", ["PlateSketch"])
        root = self._comp("Root")
        d = make_design(comp=root, all_components=[root, big, small])
        monkeypatch.setattr(sketches._common, "design", lambda: d)
        res = sketches._list_sketches(scope, max_results)
        return json.loads(res["content"][0]["text"])

    def test_a_dense_design_is_paged_and_the_true_count_still_published(self, monkeypatch):
        # The page is DESIGN-WIDE: a per-component page never bites on the measured documents (245
        # sketches over 82 components, max 6 each), so the small component is crowded out here.
        cap = sketches._LIST_CAP
        payload = self._dense(monkeypatch, held=cap + 40)
        # The rows are cut; the COUNT is not - a caller reading sketch_count off a paged list
        # would otherwise be told the document holds only what fitted.
        assert payload["sketch_count"] == cap + 41 and payload["returned"] == cap
        assert payload["truncated"] is True and len(payload["sketches"]) == cap
        assert [r["name"] for r in payload["sketches"] if r["component"] == "Plate"] == []
        assert "max_results" in payload["note"]

    def test_max_results_is_what_reaches_the_rows_past_the_page(self, monkeypatch):
        # The remedy has to actually REACH the rows the page withheld. Scoping does not: the page
        # applies whether or not a scope named a component, so component='Bench' hands back the
        # same first page. Raising the page size is the read that gets the rest.
        cap = sketches._LIST_CAP
        scoped = self._dense(monkeypatch, held=cap + 40, scope="Bench")
        assert len(scoped["sketches"]) == cap and scoped["truncated"] is True
        whole = self._dense(monkeypatch, held=cap + 40, max_results=cap + 41)
        assert [r["name"] for r in whole["sketches"] if r["component"] == "Bench"] == [
            f"Sketch{i}" for i in range(cap + 40)]
        assert "truncated" not in whole and "note" not in whole

    def test_a_design_holding_exactly_the_page_is_not_truncated(self, monkeypatch):
        # The exact boundary: at the page size nothing was withheld, so no truncation is claimed
        # and no note sends the caller after rows that are all already here.
        cap = sketches._LIST_CAP
        payload = self._dense(monkeypatch, held=cap - 1)
        assert payload["sketch_count"] == cap and len(payload["sketches"]) == cap
        assert "truncated" not in payload and "returned" not in payload
        assert "note" not in payload

    def test_one_row_past_the_page_is_truncated(self, monkeypatch):
        # The other side of that boundary, one row over.
        cap = sketches._LIST_CAP
        payload = self._dense(monkeypatch, held=cap)
        assert payload["sketch_count"] == cap + 1 and len(payload["sketches"]) == cap
        assert payload["truncated"] is True and payload["returned"] == cap

    # ONE token for two distinct components - measured on a host holding two inserted references.
    _XREF_TOKEN = "/v4BAAEAegEAAAAAAAAAAAAA"

    def _two_frames(self, monkeypatch):
        """Two components both named 'Frame', each with its own 'Frame_Ring', SHARING one
        entityToken - the measured shape after inserting two referenced documents. The shared token
        is what makes any identity-keyed grouping report each component's paths as both."""
        a = self._comp("Frame", ["Frame_Ring"])
        b = self._comp("Frame", ["Frame_Ring"])
        a.entityToken = self._XREF_TOKEN
        b.entityToken = self._XREF_TOKEN
        root = MakeComp(name="Root", entity_token="comp-root",
                        occurrences=[self._occ("P2a-Gimbal:1+Frame:1", a),
                                     self._occ("P3-Gimbal:1+Frame:1", b)])
        d = make_design(comp=root, all_components=[root, a, b])
        monkeypatch.setattr(sketches._common, "design", lambda: d)
        assert a.entityToken == b.entityToken        # the fixture models the collision, or it lies

    def test_an_ambiguous_scope_publishes_each_placement_exactly_once(self, monkeypatch):
        # Measured defect: BOTH rows carried BOTH paths, so a row's paths did not identify that
        # row's component - the one thing they existed to do. The placement block states only what
        # the walk read: these paths exist, and each places a component of this name.
        self._two_frames(monkeypatch)
        payload = json.loads(sketches._list_sketches("Frame")["content"][0]["text"])
        assert payload["sketch_count"] == 2
        assert payload["placements"] == [
            {"path": "P2a-Gimbal:1+Frame:1", "component": "Frame"},
            {"path": "P3-Gimbal:1+Frame:1", "component": "Frame"}]

    def test_no_row_claims_a_path_it_cannot_back(self, monkeypatch):
        # The rows are indistinguishable and the payload must not pretend otherwise.
        self._two_frames(monkeypatch)
        payload = json.loads(sketches._list_sketches("Frame")["content"][0]["text"])
        assert all("component_paths" not in r for r in payload["sketches"])

    def test_the_note_names_the_ambiguous_name_and_what_to_pass_next(self, monkeypatch):
        self._two_frames(monkeypatch)
        payload = json.loads(sketches._list_sketches("Frame")["content"][0]["text"])
        assert "Frame" in payload["note"] and "'placements'" in payload["note"]
        assert "'component'" in payload["note"]

    def _two_ambiguous_names(self, monkeypatch):
        """Two shared names - Frame x2 and Carrier x2 - each pair sharing a token, the shape of a
        host holding two inserted references. A call scoped to one of them must not be answered with
        the other's placements."""
        frame_a = self._comp("Frame", ["Frame_Ring"])
        frame_b = self._comp("Frame", ["Frame_Ring"])
        frame_a.entityToken = self._XREF_TOKEN
        frame_b.entityToken = self._XREF_TOKEN
        car_a = self._comp("Carrier", ["Carrier_Ring"])
        car_b = self._comp("Carrier", ["Carrier_Ring"])
        car_a.entityToken = self._XREF_TOKEN
        car_b.entityToken = self._XREF_TOKEN
        root = MakeComp(name="Root", entity_token="comp-root",
                        occurrences=[self._occ("P2a-Gimbal:1+Frame:1", frame_a),
                                     self._occ("P2a-Gimbal:1+Carrier:1", car_a),
                                     self._occ("P3-Gimbal:1+Frame:1", frame_b),
                                     self._occ("P3-Gimbal:1+Carrier:1", car_b)])
        d = make_design(comp=root, all_components=[root, frame_a, frame_b, car_a, car_b])
        monkeypatch.setattr(sketches._common, "design", lambda: d)

    def test_a_scoped_call_gets_only_ITS_names_placements(self, monkeypatch):
        # MEASURED defect: asking about 'Frame' returned 16 placements - every placement of every
        # shared name in the design (Carrier, Pedestal, OuterRing, Shaft, Rotor, Crank ...). The
        # block has to answer the QUERY, not enumerate the design.
        self._two_ambiguous_names(monkeypatch)
        payload = json.loads(sketches._list_sketches("Frame")["content"][0]["text"])
        assert payload["sketch_count"] == 2
        assert [p["path"] for p in payload["placements"]] == [
            "P2a-Gimbal:1+Frame:1", "P3-Gimbal:1+Frame:1"]
        assert all(p["component"] == "Frame" for p in payload["placements"])

    def test_the_note_states_only_what_this_walk_READ(self, monkeypatch):
        # The note asserted "because those components report the same internal id" - a cause this
        # handler never checked: _shared_component_names counts NAMES and nothing here reads an
        # entityToken. Same class as a refusal claiming the query is a name some component carries.
        self._two_ambiguous_names(monkeypatch)
        note = json.loads(sketches._list_sketches("Frame")["content"][0]["text"])["note"]
        assert "internal id" not in note and "entityToken" not in note and "token" not in note
        assert "does not tell those rows apart" in note      # what it DID do

    def test_the_note_names_only_the_names_the_block_covers(self, monkeypatch):
        # the note said "one of those names", which a reader takes as the name in THIS result - it
        # has to stay true of what actually ships
        self._two_ambiguous_names(monkeypatch)
        payload = json.loads(sketches._list_sketches("Frame")["content"][0]["text"])
        assert "(Frame)" in payload["note"] and "Carrier" not in payload["note"]

    def test_an_unscoped_call_still_covers_every_ambiguous_name(self, monkeypatch):
        # the other side: with no scope every row is in play, so design-wide IS the answer
        self._two_ambiguous_names(monkeypatch)
        payload = json.loads(sketches._list_sketches("")["content"][0]["text"])
        assert payload["sketch_count"] == 4
        assert [p["path"] for p in payload["placements"]] == [
            "P2a-Gimbal:1+Frame:1", "P2a-Gimbal:1+Carrier:1",
            "P3-Gimbal:1+Frame:1", "P3-Gimbal:1+Carrier:1"]
        assert "Carrier, Frame" in payload["note"]

    def test_placements_covers_only_the_names_that_need_it(self, monkeypatch):
        # A uniquely-named component is placed too, but its name already identifies it - listing its
        # path would be noise in a block whose only job is separating names that collide.
        a = self._comp("Frame", ["Frame_Ring"])
        b = self._comp("Frame", ["Frame_Ring"])
        a.entityToken = self._XREF_TOKEN
        b.entityToken = self._XREF_TOKEN
        bolt = self._comp("Bolt", ["BoltProfile"])
        root = MakeComp(name="Root", entity_token="comp-root",
                        occurrences=[self._occ("P2a-Gimbal:1+Frame:1", a),
                                     self._occ("P3-Gimbal:1+Frame:1", b),
                                     self._occ("Bolt:1", bolt)])
        d = make_design(comp=root, all_components=[root, a, b, bolt])
        monkeypatch.setattr(sketches._common, "design", lambda: d)
        payload = json.loads(sketches._list_sketches("")["content"][0]["text"])
        assert payload["sketch_count"] == 3
        assert [p["path"] for p in payload["placements"]] == [
            "P2a-Gimbal:1+Frame:1", "P3-Gimbal:1+Frame:1"]
        assert "Bolt" not in payload["note"]

    def _deferred_design(self, monkeypatch, deferred):
        """One component holding one sketch whose compute flag reads `deferred` and whose
        profile_count is the pre-deferral 3."""
        sk = make_sketch(name="Sketch1", is_compute_deferred=deferred,
                         profiles=[Profile(), Profile(), Profile()])
        root = MakeComp(name="Root", sketches=[sk], entity_token="comp-root")
        d = make_design(comp=root, all_components=[root])
        monkeypatch.setattr(sketches._common, "design", lambda: d)

    def test_a_deferred_row_is_flagged_and_the_note_names_the_remedy(self, monkeypatch):
        # The list is where profile_count is met first, and a stale one reads exactly like a fresh
        # one - so the row carries the flag and the payload note carries the way out.
        self._deferred_design(monkeypatch, True)
        payload = json.loads(sketches._list_sketches("")["content"][0]["text"])
        row = payload["sketches"][0]
        assert row["compute_deferred"] is True and row["profiles_stale"] is True
        assert row["profile_count"] == 3
        assert "isComputeDeferred" in payload["note"]
        assert "sketch_add_geometry" in payload["note"]

    def test_a_sketch_computing_normally_carries_neither_flag_nor_note(self, monkeypatch):
        self._deferred_design(monkeypatch, False)
        payload = json.loads(sketches._list_sketches("")["content"][0]["text"])
        assert "compute_deferred" not in payload["sketches"][0]
        assert "profiles_stale" not in payload["sketches"][0]
        assert "note" not in payload

    def test_an_occurrence_path_narrows_the_list_to_one_of_them(self, monkeypatch):
        # the loop closes: the path the list published is a scope the same input accepts, and it
        # resolves to ONE component even though both report the same internal id
        self._two_frames(monkeypatch)
        payload = json.loads(
            sketches._list_sketches("P3-Gimbal:1+Frame:1")["content"][0]["text"])
        assert payload["sketch_count"] == 1
        assert payload["sketches"][0]["component"] == "Frame"
