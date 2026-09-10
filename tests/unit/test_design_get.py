"""Tests for `design_get` - the first RICH READ (one tool, default slice + include= deeper slices).

The rich-read shape tests/CLAUDE.md names as the canonical one to copy.

Pinned: the DEFAULT call returns only the orientation slice (mode summary + health + tree_summary) and
NONE of the heavy slices; an include= returns exactly its slice, the orientation omitted unless
'default' is named beside it; the default note advertises the remaining slices; unknown include
errors; no-active-design guards. What each slice reads out of Fusion
is proven by live validation, not re-mocked here.
"""

import json
from types import SimpleNamespace

import pytest

from conftest import (FakeOccurrence, FakeTimeline, FakeUserParameters, MakeComp, MakeDesign,
                      _NamedCollection, error_message, load_tool, make_design)

dg = load_tool("design_get")

# The occurrence walk that will not enumerate at all - the measured unresolved-external-reference
# state, which reaches the wire as UNKNOWN rather than as zero.
_UNREADABLE_WALK = "2 : InternalValidationError : occ"

# What Design.timeline RAISES in a direct design (measured: direct-design-timeline-raises).
_DIRECT_DESIGN_TIMELINE = "3 : this is not a parametric design"


def _payload(result):
    assert result["isError"] is False, result
    return json.loads(result["content"][0]["text"])


# ── ROUTER composition: stub the slice SEAMS (not the source-tool internals) ────────────────────────
#
# design_get's slices DELEGATE to the 5 source handlers — so the router's own job (compose the default,
# add include= slices, advertise the rest, degrade gracefully) is what the unit tests pin. We stub the
# _slice_* functions to fixed payloads and assert the COMPOSITION. The real slice→source-handler
# delegation is proven by live validation (the honest test of cross-tool wiring), not by mocking 5
# handlers' internals (which would recreate the bespoke-fake problem this convention exists to avoid).

@pytest.fixture
def stub_slices(monkeypatch):
    monkeypatch.setattr(dg._common, "design", lambda: object())   # a non-None design
    monkeypatch.setattr(dg, "_slice_mode", lambda d: (
        {"design_type": "parametric", "has_timeline": True, "timeline_feature_count": 4,
         "in_base_feature_edit": False, "can": {"timeline_ops": True}}, None))
    monkeypatch.setattr(dg, "_slice_health", lambda d: (
        {"healthy": True, "error_count": 0, "warning_count": 0, "errors": [], "warnings": []}, None))
    monkeypatch.setattr(dg, "_fingerprint", lambda d: {"bodies": 2, "sketches": 3})
    monkeypatch.setattr(dg, "_slice_tree", lambda d, max_depth, component, with_bodies=False,
                        with_handles=False, max_children=0, name_filter="": (
        {"root": "Root", "max_depth": max_depth, "children": [], "with_bodies": with_bodies,
         "with_handles": with_handles, "max_children": max_children,
         "name_filter": name_filter}, None))
    monkeypatch.setattr(dg, "_slice_timeline", lambda d, include_suppressed, group,
                        with_params=False, max_rows=0: (
        {"count": 4, "timeline": [], "with_params": with_params, "max_rows": max_rows}, None))
    monkeypatch.setattr(dg, "_slice_configurations", lambda d: ({"table_name": "Configs"}, None))
    monkeypatch.setattr(dg, "_slice_attributes", lambda d, group, key: (
        {"group": group, "key": key, "attributes": []}, None))
    monkeypatch.setattr(dg, "_slice_materials", lambda d, library, name_filter, max_results: (
        {"kind": "materials", "library": library, "name_filter": name_filter,
         "max_results": max_results, "document": {"count": 1}, "libraries": []}, None))
    monkeypatch.setattr(dg, "_slice_appearances", lambda d, library, name_filter, max_results: (
        {"kind": "appearances", "library": library, "document": {"count": 1}, "libraries": []}, None))


# ── default orientation slice is DENSE + bounded (the core rich-read contract) ──────────────────────

class TestDefaultSlice:
    def test_default_is_the_dense_orientation(self, stub_slices):
        out = _payload(dg.handler())                   # no include=
        # the headline: design_type + feature_count + TIMELINE_healthy + a CONTENT fingerprint.
        # The name is scoped: timeline_healthy is timeline-only (NOT stale refs — that's a tree node's
        # is_out_of_date, and the doc-wide verdict is workspace_orient.is_healthy).
        assert out["design_type"] == "parametric" and out["feature_count"] == 4
        assert out["timeline_healthy"] is True and "healthy" not in out
        assert out["contents"] == {"bodies": 2, "sketches": 3}   # "what IS this model"
        # the HEAVY slices must be absent by default (the anti-flood contract)
        assert "tree" not in out and "timeline" not in out and "configurations" not in out
        assert "mode_detail" not in out                # the full can{} map is opt-in only
        assert "materials" not in out and "appearances" not in out   # the catalog is opt-in too

    def test_default_omits_noise_when_healthy(self, stub_slices):
        # a healthy design carries no health DETAIL block + no in_base_feature_edit:false
        out = _payload(dg.handler())
        assert "health" not in out                     # 'timeline_healthy: True' says it all
        assert "in_base_feature_edit" not in out       # only present when True

    def test_default_emits_pointers_for_hidden_content(self, monkeypatch, stub_slices):
        # a design WITH parameters gets a pointers block naming param_get/param_set - the inbound
        # breadcrumb to the otherwise-undiscoverable param family.
        monkeypatch.setattr(dg, "_fingerprint", lambda d: {"parameters": 40, "bodies": 2})
        out = _payload(dg.handler())
        assert "param_get" in out["pointers"]["parameters"]

    def test_an_unreadable_census_is_said_in_words_as_well_as_in_the_marker(self, monkeypatch,
                                                                           stub_slices):
        # The count is absent from contents either way, so an agent reading the default sees the
        # same shape for "no instances" and "the census failed". The note carries the second case.
        monkeypatch.setattr(dg, "_fingerprint",
                            lambda d: {"bodies": 2, "occurrences_walk": "unreadable"})
        out = _payload(dg.handler())
        assert "occurrences_walk='unreadable'" in out["note"]
        assert "UNKNOWN, not because the design holds none" in out["note"]

    def test_a_census_that_was_taken_adds_no_such_sentence(self, monkeypatch, stub_slices):
        # the sentence is a DISCLOSURE of a hole, not a caption on every read
        monkeypatch.setattr(dg, "_fingerprint",
                            lambda d: {"bodies": 2, "occurrences_walk": "recursed"})
        out = _payload(dg.handler())
        assert "unreadable" not in out["note"]

    def test_default_no_pointers_when_only_obvious_content(self, monkeypatch, stub_slices):
        # stub_slices' fingerprint is bodies+sketches only (obvious), and no CAM -> no pointers block.
        monkeypatch.setattr(dg, "_has_cam", lambda d: False)
        out = _payload(dg.handler())
        assert "pointers" not in out

    def test_default_points_at_cam_when_cam_present(self, monkeypatch, stub_slices):
        # a CAM document (design_get is blind to its machining state) gets a cam_get breadcrumb.
        monkeypatch.setattr(dg, "_has_cam", lambda d: True)
        out = _payload(dg.handler())
        assert "cam_get" in out["pointers"]["cam"]

    def test_default_surfaces_health_detail_when_unhealthy(self, monkeypatch, stub_slices):
        monkeypatch.setattr(dg, "_slice_health", lambda d: (
            {"healthy": False, "error_count": 1, "warning_count": 0,
             "errors": ["Extrude3"], "warnings": []}, None))
        out = _payload(dg.handler())
        assert out["timeline_healthy"] is False and out["health"]["errors"] == ["Extrude3"]

    def test_default_note_advertises_remaining_slices(self, stub_slices):
        out = _payload(dg.handler())
        assert "include=" in out["note"]               # un-named flags are invisible — must advertise


# ── each include= adds exactly its slice ────────────────────────────────────────────────────────────

class TestIncludeSlices:
    @pytest.mark.parametrize("slice_name,key", [
        ("tree", "tree"),
        ("timeline", "timeline"),
        ("mode", "mode_detail"),
        ("configurations", "configurations"),
        ("materials", "materials"),
        ("appearances", "appearances"),
        ("attributes", "attributes"),
    ])
    def test_include_adds_the_slice(self, stub_slices, slice_name, key):
        out = _payload(dg.handler(include=[slice_name]))
        assert key in out

    def test_timeline_params_flag_reaches_the_timeline_slice(self, stub_slices):
        # the flag is off unless asked for - the extra allParameters pass is opt-in cost.
        assert _payload(dg.handler(include=["timeline"]))["timeline"]["with_params"] is False
        out = _payload(dg.handler(include=["timeline"], timeline_params=True))
        assert out["timeline"]["with_params"] is True

    def test_tree_bodies_flag_reaches_the_tree_slice(self, stub_slices):
        # same opt-in shape as timeline_params: off by default, passed through when asked for.
        assert _payload(dg.handler(include=["tree"]))["tree"]["with_bodies"] is False
        out = _payload(dg.handler(include=["tree"], tree_bodies=True))
        assert out["tree"]["with_bodies"] is True

    def test_tree_handles_flag_reaches_the_tree_slice(self, stub_slices):
        # the handle is the ~200-char half of a node: opt-in, and off unless asked for.
        assert _payload(dg.handler(include=["tree"]))["tree"]["with_handles"] is False
        assert _payload(dg.handler(include=["tree"],
                                   tree_handles=True))["tree"]["with_handles"] is True

    def test_the_tree_reads_its_page_size_and_filter_off_the_shared_knobs(self, stub_slices):
        # ONE 'rows per page' knob and ONE name filter serve the catalog AND the tree - a second
        # pair of inputs would mean two spellings of the same narrowing on the wire.
        out = _payload(dg.handler(include=["tree"], max_results=5, name_filter="Seat"))
        assert out["tree"]["max_children"] == 5 and out["tree"]["name_filter"] == "Seat"

    def test_attribute_scope_reaches_the_attributes_slice(self, stub_slices):
        out = _payload(dg.handler(include=["attributes"], attribute_group="shop",
                                  attribute_key="op"))
        assert out["attributes"]["group"] == "shop" and out["attributes"]["key"] == "op"

    def test_attributes_slice_error_fails_the_read(self, monkeypatch, stub_slices):
        monkeypatch.setattr(dg, "_slice_attributes",
                            lambda d, g, k: (None, dg.error("needs attribute_group")))
        res = dg.handler(include=["attributes"])
        assert res["isError"] and "attribute_group" in error_message(res)

    def test_include_mode_adds_full_capability_map(self, stub_slices):
        out = _payload(dg.handler(include=["mode"]))
        assert "can" in out["mode_detail"]             # the full map, vs the summary in the default

    def test_multiple_includes(self, stub_slices):
        out = _payload(dg.handler(include=["tree", "timeline"]))
        assert "tree" in out and "timeline" in out

    def test_a_deep_include_omits_the_orientation_slice(self, stub_slices):
        # a deep read returns the slice asked for, not the orientation block again - the
        # design_get(include=['tree']) that re-sends design_type/contents pays for both every call.
        out = _payload(dg.handler(include=["tree"]))
        assert "tree" in out
        for key in ("design_type", "feature_count", "timeline_healthy", "contents", "pointers",
                    "note"):
            assert key not in out

    def test_default_beside_a_deep_slice_keeps_both(self, stub_slices):
        out = _payload(dg.handler(include=["default", "tree"]))
        assert out["design_type"] == "parametric" and out["contents"]["bodies"] == 2
        assert "tree" in out and "include=" in out["note"]

    def test_default_alone_is_the_orientation_read(self, stub_slices):
        out = _payload(dg.handler(include=["default"]))
        assert out["contents"] == {"bodies": 2, "sketches": 3} and "tree" not in out

    def test_include_mode_still_answers_on_a_deep_read(self, stub_slices):
        # the orientation headline and include=['mode'] share ONE _slice_mode call; omitting the
        # orientation must not take the capability map with it.
        out = _payload(dg.handler(include=["mode"]))
        assert "can" in out["mode_detail"] and "design_type" not in out

    def test_a_deep_read_takes_neither_orientation_read(self, monkeypatch, stub_slices):
        # the mode + health reads ARE the orientation's cost; a slice-only read must not pay it.
        calls = []
        monkeypatch.setattr(dg, "_slice_mode", lambda d: (calls.append("mode") or ({}, None)))
        monkeypatch.setattr(dg, "_slice_health", lambda d: (calls.append("health") or ({}, None)))
        _payload(dg.handler(include=["tree"]))
        assert calls == []

    def test_catalog_slices_are_independent(self, stub_slices):
        # materials and appearances are two projections of one walk but two separate slices:
        # asking for one must not pull the other.
        out = _payload(dg.handler(include=["materials"]))
        assert out["materials"]["kind"] == "materials" and "appearances" not in out


class TestCatalogSlices:
    """The materials/appearances slices - the catalog an agent picks a name FROM before assigning it.
    Both delegate to the one bounded walk in _materials; the router's job is passing the scope flags
    through, keeping the two kinds apart, and advertising the flags."""

    def test_scope_flags_reach_the_slice(self, stub_slices):
        out = _payload(dg.handler(include=["materials"], library="Fusion Material Library",
                                  name_filter="steel", max_results=10))
        assert out["materials"]["library"] == "Fusion Material Library"
        assert out["materials"]["name_filter"] == "steel" and out["materials"]["max_results"] == 10

    def test_default_note_advertises_the_catalog_slices_and_flags(self, stub_slices):
        note = _payload(dg.handler())["note"]
        assert "materials" in note and "appearances" in note
        assert "library" in note and "name_filter" in note   # un-named flags are invisible

    def test_default_note_advertises_the_attributes_slice_and_timeline_params(self, stub_slices):
        # both are invisible to a caller unless the default names them AND their scope flags.
        note = _payload(dg.handler())["note"]
        assert "attributes" in note and "attribute_group" in note
        assert "timeline_params" in note

    def test_slice_error_fails_the_read(self, monkeypatch, stub_slices):
        monkeypatch.setattr(dg, "_slice_materials",
                            lambda d, library, name_filter, max_results: (None, dg.error("no such library")))
        res = dg.handler(include=["materials"])
        assert res["isError"] and "no such library" in error_message(res)

    def test_each_slice_browses_its_own_kind(self, monkeypatch):
        # the delegation itself: two slices, one walk, DIFFERENT kind - passing 'materials' for both
        # would return the wrong catalog under the right label.
        seen = []
        monkeypatch.setattr(dg._materials, "browse",
                            lambda design, kind, library, name_filter, max_results: (
                                seen.append(kind) or ({"kind": kind}, None)))
        dg._slice_materials(object(), "", "", 0)
        dg._slice_appearances(object(), "", "", 0)
        assert seen == ["materials", "appearances"]


def _occurrences(n):
    """N healthy occurrences, each answering `component` (a real Occurrence always does; one that
    raises is an unresolved reference)."""
    return [FakeOccurrence(path=f"C{i}:1", component=MakeComp(f"C{i}")) for i in range(n)]


def _blind_root(joints=0, asbuilt=0):
    """A root whose occurrence census cannot be taken AT ALL: allOccurrences will not enumerate (the
    measured unresolved-external-reference state) and the component-local `occurrences` fallback
    will not either. The state that must reach the wire as UNKNOWN, never as zero."""
    root = MakeComp("Root")
    root.allOccurrences = _NamedCollection(raises=_UNREADABLE_WALK)
    root.occurrences = _NamedCollection(raises=_UNREADABLE_WALK)
    root.joints = _NamedCollection([None] * joints)
    root.asBuiltJoints = _NamedCollection([None] * asbuilt)
    return root


def _recursed_root(n=2, **kw):
    """allOccurrences will not enumerate, but the component-local `occurrences` still does - so the
    census IS taken, by the fallback walk, and the count it reports is real."""
    root = _blind_root(**kw)
    root.occurrences = _NamedCollection(_occurrences(n))
    return root


def _design_around(root):
    """A design whose only content is `root` - the shape `_fingerprint` reads its counts off."""
    return make_design(comp=root, user_parameters=FakeUserParameters())


class TestFingerprint:
    """The content fingerprint (`_fingerprint`) - the 'what IS this model' counts in the default slice."""

    def _design(self, bodies=0, sketches=0, occs=0, defs=0, joints=0, asbuilt=0, params=0):
        from types import SimpleNamespace
        c = lambda n: SimpleNamespace(count=n)
        # allOccurrences is ENUMERATED, not just counted: the fingerprint reads its occurrence total
        # through the shared census, which classifies each row (an occurrence whose component raises
        # is an unresolved reference, not a countable instance).
        root = SimpleNamespace(bRepBodies=c(bodies), sketches=c(sketches),
                               allOccurrences=_NamedCollection(_occurrences(occs)),
                               joints=c(joints), asBuiltJoints=c(asbuilt))
        return SimpleNamespace(rootComponent=root, userParameters=c(params),
                               allComponents=c(defs))

    def test_counts_both_joint_collections(self):
        # joints and asBuiltJoints are separate collections; the count must include both.
        fp = dg._fingerprint(self._design(joints=2, asbuilt=3))
        assert fp["joints"] == 5

    def test_as_built_only_still_counts(self):
        # a design whose ONLY joints are as-built must not report 0 joints.
        fp = dg._fingerprint(self._design(asbuilt=1))
        assert fp["joints"] == 1

    def test_counts_user_parameters(self):
        # parameters must appear in the fingerprint - an invisible parameter count is the whole reason
        # the param_* family would have no inbound breadcrumb.
        fp = dg._fingerprint(self._design(params=40))
        assert fp["parameters"] == 40

    def test_zero_counts_omitted(self):
        fp = dg._fingerprint(self._design(bodies=1, defs=1))     # root is the only definition
        # no joints/sketches/components/parameters when zero; the census marker rides beside them,
        # since it is what says whether an ABSENT count was counted or never read
        assert fp == {"bodies": 1, "occurrences_walk": "allOccurrences"}

    def test_an_unreadable_census_is_distinguishable_from_a_design_with_no_instances(self):
        # The truthy filter drops 'occurrences' for BOTH reads - a design holding no placed instance
        # (total 0) and one whose census could not be taken (total None) - so the missing key alone
        # reports a hole in the read as a single-component design. occurrences_walk is what tells
        # them apart on the wire.
        empty = dg._fingerprint(self._design(bodies=1))
        blind = dg._fingerprint(_design_around(_blind_root()))
        assert "occurrences" not in empty and "occurrences" not in blind
        assert empty["occurrences_walk"] == "allOccurrences"
        assert blind["occurrences_walk"] == "unreadable"

    def test_the_fallback_walk_reports_a_real_count_and_names_itself(self):
        # allOccurrences raising is not the census failing: the component-local recursion answers,
        # so the count is real - and the marker still says which walk produced it, because a
        # recursed census is not the same evidence as the fast one.
        fp = dg._fingerprint(_design_around(_recursed_root(n=2)))
        assert fp["occurrences"] == 2 and fp["occurrences_walk"] == "recursed"

    def test_components_counts_definitions_not_instances(self):
        # a component instanced 5 times is ONE definition: 'components' counts definitions, and
        # 'occurrences' carries the instance count honestly beside it.
        fp = dg._fingerprint(self._design(defs=3, occs=5))       # root + 2 definitions, 5 instances
        assert fp["components"] == 2 and fp["occurrences"] == 5

    def test_bodies_and_sketches_are_design_wide_not_root_only(self):
        # bodies/sketches must be summed across every component (via the shared
        # _common.design_wide_counts, the same one workspace_orient uses), not read off the root
        # alone - a design whose geometry lives in sub-components would otherwise under-report (a
        # sketch-only-in-sub-components doc reading sketches:0 despite having 2).
        c = lambda n: SimpleNamespace(count=n)
        root = SimpleNamespace(bRepBodies=c(0), sketches=c(0),
                               allOccurrences=c(2), joints=c(0), asBuiltJoints=c(0))
        subs = [SimpleNamespace(bRepBodies=c(1), sketches=c(1)),
                SimpleNamespace(bRepBodies=c(2), sketches=c(1))]
        items = [root] + subs
        design = SimpleNamespace(rootComponent=root, userParameters=c(0),
                                 allComponents=SimpleNamespace(count=len(items), item=lambda i: items[i]))
        fp = dg._fingerprint(design)
        assert fp["bodies"] == 3      # 0 (root) + 1 + 2, NOT the root-only 0
        assert fp["sketches"] == 2    # 0 (root) + 1 + 1, NOT the root-only 0


class TestContentPointers:
    """`_content_pointers` - the inbound breadcrumb: a present content class names the tool acting on it."""

    def test_parameters_present_points_at_param_tools(self):
        p = dg._content_pointers({"parameters": 40, "bodies": 2})
        assert "parameters" in p and "param_get" in p["parameters"] and "param_set" in p["parameters"]

    def test_obvious_classes_get_no_pointer(self):
        # bodies/sketches are omitted - every agent already knows model_*/sketch_*; only the hidden
        # families (parameters/joints/components) get a breadcrumb.
        p = dg._content_pointers({"bodies": 5, "sketches": 3})
        assert p == {}

    def test_only_present_classes_pointed(self):
        p = dg._content_pointers({"joints": 3})
        assert set(p) == {"joints"} and "assembly_get" in p["joints"]

    def test_empty_contents_no_pointers(self):
        assert dg._content_pointers({}) == {} and dg._content_pointers(None) == {}


class TestHasCam:
    """`_has_cam` - a CAM document's machining state is invisible to design_get, so it needs a cam_get
    breadcrumb. It delegates to the shared _cam_common.get_cam() (the active document's CAM product)."""

    def test_cam_present(self, monkeypatch):
        monkeypatch.setattr(dg._cam_common, "get_cam", lambda: (object(), None))
        assert dg._has_cam(None) is True

    def test_no_cam(self, monkeypatch):
        monkeypatch.setattr(dg._cam_common, "get_cam", lambda: (None, "no CAM data"))
        assert dg._has_cam(None) is False


class TestTimelineSlice:
    """Timeline slice logic moved into design_get (_entity_type / _object_summary / _slice_timeline)."""

    def _tlobj(self, index=0, name="Extrude1", is_group=False, suppressed=False, rolled_back=False,
               parent_group=None, health=0, message=None, entity_name="ExtrudeFeature",
               members=None, collapsed=False):
        from types import SimpleNamespace
        _Ent = type(entity_name, (), {})
        pg = SimpleNamespace(name=parent_group) if parent_group is not None else None
        obj = SimpleNamespace(index=index, name=name, isGroup=is_group, isSuppressed=suppressed,
                              isRolledBack=rolled_back, parentGroup=pg, healthState=health,
                              errorOrWarningMessage=message, entity=(None if is_group else _Ent()))
        if members is not None:
            # A TimelineGroup carries its members on its OWN count/item - the only route to them
            # while the group is collapsed and the timeline walk shows one row.
            obj.count = len(members)
            obj.item = lambda i, _m=list(members): _m[i]
            obj.isCollapsed = collapsed
        return obj

    def test_entity_type_group(self):
        from types import SimpleNamespace
        assert dg._entity_type(SimpleNamespace(isGroup=True)) == "TimelineGroup"

    def test_entity_type_class_name(self):
        assert dg._entity_type(self._tlobj()) == "ExtrudeFeature"

    def test_object_summary_maps_health_label(self):
        assert dg._object_summary(self._tlobj(health=2))["health"] == "error"

    def test_object_summary_message_only_when_present(self):
        assert dg._object_summary(self._tlobj(message="x"))["message"] == "x"
        assert "message" not in dg._object_summary(self._tlobj(message=None))

    def _design_with(self, items, marker=0, groups=(), root_name="Root"):
        from types import SimpleNamespace
        tl = SimpleNamespace(_items=list(items), markerPosition=marker, timelineGroups=list(groups),
                             count=len(items), item=lambda i, _it=list(items): _it[i])
        return SimpleNamespace(timeline=tl, rootComponent=SimpleNamespace(name=root_name))

    def _owned_row(self, index, name, component, health=0):
        """A timeline row whose entity names the COMPONENT that owns it - what tells the Slider's
        Sketch1 from the Ball's."""
        row = self._tlobj(index, name, health=health)
        row.entity.parentComponent = SimpleNamespace(name=component)
        return row

    def test_each_timeline_row_names_the_component_that_owns_it(self):
        # MEASURED on Joints Demo Block (118 rows): feature names are per-COMPONENT, so 'Sketch1'
        # appears six times over with nothing to tell the instances apart.
        d = self._design_with([self._owned_row(0, "Sketch1", "Slider"),
                               self._owned_row(1, "Sketch1", "Ball")])
        out, _ = dg._slice_timeline(d, include_suppressed=True, group="")
        assert [(r["name"], r["component"]) for r in out["timeline"]] == [
            ("Sketch1", "Slider"), ("Sketch1", "Ball")]

    def test_the_root_owner_is_dropped_as_noise(self):
        # in a single-component design EVERY row would name the root: a column that never varies
        # is bytes on the wire and nothing else.
        d = self._design_with([self._owned_row(0, "Extrude1", "Root")], root_name="Root")
        row = dg._slice_timeline(d, include_suppressed=True, group="")[0]["timeline"][0]
        assert "component" not in row

    def test_a_row_whose_owner_does_not_read_states_none(self):
        # the fallback is silence, not the root: naming a component nothing was read from would
        # put the wrong Sketch1 in front of the caller.
        d = self._design_with([self._tlobj(0, "Sketch1")])
        row = dg._slice_timeline(d, include_suppressed=True, group="")[0]["timeline"][0]
        assert "component" not in row

    def test_the_timeline_page_is_capped_and_names_its_narrowings(self, monkeypatch):
        # MEASURED: 150 rows on Wireless Speaker "Outer Shell". An unbounded slice is the flood a
        # capped read exists to avoid - and a cap is only usable if it says how to page past it.
        monkeypatch.setattr(dg, "_TIMELINE_MAX_ITEMS", 2)
        d = self._design_with([self._tlobj(i, f"F{i}") for i in range(4)])
        out, _ = dg._slice_timeline(d, include_suppressed=True, group="")
        assert out["returned"] == 2 and out["count"] == 4 and out["truncated"] is True
        assert "max_results" in out["truncated_note"] and "group=" in out["truncated_note"]
        # the note offers only NARROWINGS and the page knob - timeline_params widens the payload,
        # so pointing at it from a cap message sends the caller the wrong way.
        assert "timeline_params" not in out["truncated_note"]

    def test_max_results_pages_the_timeline_past_the_default_cap(self, monkeypatch):
        # the route to rows 251+ on a design with no groups to narrow by: without it the cap is a
        # wall, and the note would be naming a narrowing that cannot reach them.
        monkeypatch.setattr(dg, "_TIMELINE_MAX_ITEMS", 2)
        d = self._design_with([self._tlobj(i, f"F{i}") for i in range(4)])
        out, _ = dg._slice_timeline(d, include_suppressed=True, group="", max_rows=4)
        assert out["returned"] == 4 and "truncated" not in out

    def test_an_unset_page_size_falls_back_to_the_timeline_default(self, monkeypatch):
        # max_results is 0 on the wire (the catalog's 'unset'), and max(1, 0) would page one row.
        monkeypatch.setattr(dg, "_TIMELINE_MAX_ITEMS", 3)
        d = self._design_with([self._tlobj(i, f"F{i}") for i in range(3)])
        out, _ = dg._slice_timeline(d, include_suppressed=True, group="", max_rows=0)
        assert out["returned"] == 3 and "truncated" not in out

    def test_a_timeline_exactly_at_the_page_size_is_not_flagged(self, monkeypatch):
        monkeypatch.setattr(dg, "_TIMELINE_MAX_ITEMS", 2)
        d = self._design_with([self._tlobj(i, f"F{i}") for i in range(2)])
        out, _ = dg._slice_timeline(d, include_suppressed=True, group="")
        assert out["returned"] == 2 and "truncated" not in out

    def test_slice_returns_all_with_marker_count(self):
        d = self._design_with([self._tlobj(0, "A"), self._tlobj(1, "B")], marker=2)
        out, err = dg._slice_timeline(d, include_suppressed=True, group="")
        assert err is None and out["count"] == 2 and out["marker_position"] == 2
        assert [o["name"] for o in out["timeline"]] == ["A", "B"]

    def test_uncountable_timeline_refuses_instead_of_reading_empty(self):
        # timeline.count raising means the timeline could not be read AT ALL. Answering with an
        # empty list + count 0 would read as "this design has no history" - a false answer a caller
        # gates on. The refusal carries the platform's reason.
        class _Uncountable(FakeTimeline):
            """A timeline whose count raises; item() may never be reached."""
            def item(self, i):
                raise AssertionError("must not be reached")

        timeline = _Uncountable(raises="timeline is mid-recompute")
        timeline.timelineGroups = []
        out, err = dg._slice_timeline(SimpleNamespace(timeline=timeline),
                                      include_suppressed=True, group="")
        assert out is None and err["isError"] is True
        assert "mid-recompute" in err["message"]

    def test_slice_include_suppressed_false(self):
        d = self._design_with([self._tlobj(0, "Live"), self._tlobj(1, "Hid", suppressed=True)])
        out, _ = dg._slice_timeline(d, include_suppressed=False, group="")
        assert out["returned"] == 1 and out["count"] == 2

    def test_slice_group_filter(self):
        d = self._design_with([self._tlobj(0, "A", parent_group="W"),
                               self._tlobj(1, "B", parent_group="F")])
        out, _ = dg._slice_timeline(d, include_suppressed=True, group="W")
        assert [o["name"] for o in out["timeline"]] == ["A"]

    def test_a_collapsed_group_is_ONE_row_carrying_its_member_count(self):
        # MEASURED on the CAM sample "2D - Overview of toolpaths": a collapsed group reads as ONE
        # timeline row (count 1) while the group itself holds 26 members - so the row has to say
        # what it stands for, and where the members are read.
        members = [self._tlobj(1, "Fixture"), self._tlobj(2, "Clamp")]
        d = self._design_with([self._tlobj(0, "Group1", is_group=True, health=5, members=members)])
        out, _ = dg._slice_timeline(d, include_suppressed=True, group="")
        row = out["timeline"][0]
        assert row["type"] == "TimelineGroup" and row["member_count"] == 2
        assert "group='<name>'" in out["groups_note"]

    def test_a_group_rows_health_is_not_scored_as_a_feature(self):
        # A group is a container: its own healthState reads 'unknown', and tallied it makes a design
        # that WAS read report an unread row. Its MEMBER is a feature and is counted. The group is
        # COLLAPSED, which is why the member is absent from the timeline list beside it.
        member = self._tlobj(1, "Fixture")
        del member.index
        d = self._design_with([self._tlobj(0, "Group1", is_group=True, health=5, collapsed=True,
                                           members=[member]),
                               self._tlobj(2, "Extrude1", health=0)])
        out, _ = dg._slice_timeline(d, include_suppressed=True, group="")
        assert out["summary"]["states"] == {"healthy": 2}     # Fixture + Extrude1, not the group
        assert out["summary"]["exceptions"] == []

    def test_a_COLLAPSED_groups_suppressed_member_reaches_the_states_tally(self):
        # The measured hole: a collapsed group is ONE row, so its members never reach the walk -
        # summary.states read 'healthy 1' over a design holding a suppressed Extrude, and a caller
        # gating on the tally saw a clean design. A collapsed member answers no index.
        hidden = self._tlobj(name="Extrude9", suppressed=True, health=3)
        fixture = self._tlobj(2, "Fixture")
        del hidden.index
        del fixture.index
        d = self._design_with([self._tlobj(0, "Group1", is_group=True, health=5, collapsed=True,
                                           members=[hidden, fixture]),
                               self._tlobj(3, "Extrude1", health=0)])
        out, _ = dg._slice_timeline(d, include_suppressed=True, group="")
        assert out["summary"]["states"] == {"suppressed": 1, "healthy": 2}
        assert [r["name"] for r in out["timeline"]] == ["Group1", "Extrude1"]   # rows unchanged
        assert out["timeline"][0]["member_count"] == 2
        assert "summary.states counts them" in out["groups_note"]

    def test_a_COLLAPSED_groups_ERRORED_member_becomes_an_exception_addressed_by_name(self):
        # index RAISES on a collapsed member (measured), so the exception it raises carries a null
        # index - the name is the address, and the note is what says so.
        broken = self._tlobj(name="Loft2", health=2)
        del broken.index
        d = self._design_with([self._tlobj(0, "Group1", is_group=True, health=5, collapsed=True,
                                           members=[broken])])
        out, _ = dg._slice_timeline(d, include_suppressed=True, group="")
        assert out["summary"]["exceptions"] == [
            {"name": "Loft2", "index": None, "health": "error"}]
        assert "address it by name" in out["groups_note"].lower()

    def test_a_member_both_listed_and_reachable_through_its_group_is_tallied_once(self):
        # OBSERVED live after deleting and re-adding an overlapping group: a group read collapsed
        # while its members stayed enumerated beside its row. Tallying both doors without deduping
        # doubles every state count in that state.
        member = self._tlobj(1, "Fixture", parent_group="Group1", health=0)
        d = self._design_with([self._tlobj(0, "Group1", is_group=True, collapsed=True,
                                           members=[member]), member])
        out, _ = dg._slice_timeline(d, include_suppressed=True, group="")
        assert out["summary"]["states"] == {"healthy": 1}

    def test_a_group_row_says_whether_its_members_are_in_the_walk(self):
        # the note claims the tally covers members the rows omit; is_collapsed is the flag that
        # tells a reader WHICH group those omitted rows belong to. read_flag honesty: a build whose
        # flag will not read publishes null, never a confident false.
        for collapsed in (True, None):
            g = self._tlobj(0, "Group1", is_group=True, health=5, collapsed=True,
                            members=[self._tlobj(1, "Fixture")])
            if collapsed is None:
                del g.isCollapsed
            d = self._design_with([g])
            row = dg._slice_timeline(d, include_suppressed=True, group="")[0]["timeline"][0]
            assert row["is_collapsed"] is collapsed

    def test_an_EXPANDED_group_contributes_rows_and_no_group_row(self):
        # MEASURED (measure_api row timeline-group-collapse-shape): collapse decides the
        # enumeration - expanded, the members are the rows (carrying parent_group) and the group's
        # own row is not enumerated at all, so each member is tallied exactly once off its own row.
        rows = [self._tlobj(0, "Sk1"),
                self._tlobj(1, "Sk2", parent_group="G1"),
                self._tlobj(2, "Sk3", parent_group="G1", suppressed=True, health=3)]
        out, _ = dg._slice_timeline(self._design_with(rows), include_suppressed=True, group="")
        assert out["summary"]["states"] == {"healthy": 2, "suppressed": 1}
        assert "groups_note" not in out            # no group row means nothing to point at

    def _nested(self):
        """A COLLAPSED outer group holding a COLLAPSED inner group holding one healthy and one
        errored feature. Only the outer's row is enumerated, so the inner is reached by recursion
        and its members answer no index."""
        good, bad = self._tlobj(3, "Fixture", health=0), self._tlobj(4, "Loft2", health=2)
        inner = self._tlobj(1, "Inner", is_group=True, health=5, collapsed=True,
                            members=[good, bad])
        for obj in (good, bad, inner):
            del obj.index                    # nothing under a collapsed group answers an index
        outer = self._tlobj(0, "Outer", is_group=True, health=5, collapsed=True, members=[inner])
        return self._design_with([outer])

    def test_a_NESTED_group_under_a_COLLAPSED_parent_is_walked_through_and_never_scored(self):
        # a nested container's own healthState reads 'unknown' too - scoring it would report an
        # unread row, and skipping it without recursing would lose its members entirely.
        out, _ = dg._slice_timeline(self._nested(), include_suppressed=True, group="")
        assert out["summary"]["states"] == {"healthy": 1, "error": 1}
        assert [e["name"] for e in out["summary"]["exceptions"]] == ["Loft2"]

    def test_a_group_reachable_TWICE_is_tallied_once(self):
        # OBSERVED live after deleting and re-adding an overlapping group: the group read
        # isCollapsed true while its members stayed enumerated beside its row. The index guard is
        # what stops that state from doubling every count it touches.
        member = self._tlobj(7, "Fixture", health=0)
        group = self._tlobj(6, "G1", is_group=True, health=5, collapsed=True, members=[member])
        states, exceptions, tallied = {}, [], set()
        summ = dg._object_summary(group)
        dg._tally_group(states, exceptions, tallied, group, summ)
        dg._tally_group(states, exceptions, tallied, group, summ)
        assert states == {"healthy": 1}

    def test_a_group_row_published_by_the_group_scope_carries_the_container_keys(self):
        # the group= expansion is the second door onto a group row; publishing it without
        # member_count/is_collapsed leaves a container reading health 'unknown' like a broken
        # feature, with nothing saying what it stands for.
        inner = self._tlobj(2, "Inner", is_group=True, health=5, collapsed=True,
                            members=[self._tlobj(3, "Fixture")])
        d = self._design_with([self._tlobj(0, "Outer", is_group=True, collapsed=True,
                                           members=[inner]), inner])
        out, _ = dg._slice_timeline(d, include_suppressed=True, group="Outer")
        row = next(r for r in out["timeline"] if r["name"] == "Inner")
        assert row["member_count"] == 1 and row["is_collapsed"] is True
        assert "group='Inner'" in out["groups_note"]

    def test_the_group_scope_expands_a_collapsed_group_the_walk_never_lists(self):
        members = [self._tlobj(1, "Fixture"), self._tlobj(2, "Clamp")]
        d = self._design_with([self._tlobj(0, "Group1", is_group=True, members=members)])
        out, _ = dg._slice_timeline(d, include_suppressed=True, group="Group1")
        assert [o["name"] for o in out["timeline"]] == ["Fixture", "Clamp"]

    def test_a_member_listed_beside_its_group_row_is_listed_once_not_twice(self):
        # OBSERVED live after an overlapping group was deleted and re-added: a group read collapsed
        # while its members stayed enumerated beside its row, so the expansion dedupes on the
        # timeline index rather than listing each one twice.
        member = self._tlobj(1, "Fixture", parent_group="Group1")
        d = self._design_with([self._tlobj(0, "Group1", is_group=True, collapsed=True,
                                           members=[member]), member])
        out, _ = dg._slice_timeline(d, include_suppressed=True, group="Group1")
        assert [o["name"] for o in out["timeline"]] == ["Fixture"]

    def test_a_member_listed_BEFORE_its_group_row_is_still_listed_once(self):
        # The other order: the walk lists the member first, so the expansion is the side that has
        # to skip it. Deduping only after the expansion lists it twice.
        member = self._tlobj(0, "Fixture", parent_group="Group1")
        d = self._design_with([member, self._tlobj(1, "Group1", is_group=True, collapsed=True,
                                                   members=[member])])
        out, _ = dg._slice_timeline(d, include_suppressed=True, group="Group1")
        assert [o["name"] for o in out["timeline"]] == ["Fixture"]

    def test_a_collapsed_groups_members_publish_a_null_index_and_say_so(self):
        # A member whose index read nothing lands addressable by NAME only, and the note has to say
        # which half of the address is missing rather than leaving a null to be guessed at.
        member = self._tlobj(1, "Fixture", parent_group="Group1")
        del member.index
        d = self._design_with([self._tlobj(0, "Group1", is_group=True, collapsed=True,
                                           members=[member])])
        out, _ = dg._slice_timeline(d, include_suppressed=True, group="Group1")
        row = out["timeline"][0]
        assert row["name"] == "Fixture" and row["index"] is None
        assert "address them by name" in out["groups_note"]

    def test_a_collapsed_groups_SUPPRESSED_member_obeys_include_suppressed(self):
        # The expansion is a second door into the same list, so the filter the walk applies has to
        # hold there too - or a group scope becomes the way to see rows the caller filtered out.
        live = self._tlobj(1, "Live", parent_group="Group1")
        hidden = self._tlobj(2, "Hid", parent_group="Group1", suppressed=True)
        d = self._design_with([self._tlobj(0, "Group1", is_group=True, members=[live, hidden])])
        shown = dg._slice_timeline(d, include_suppressed=False, group="Group1")[0]
        assert [o["name"] for o in shown["timeline"]] == ["Live"]
        both = dg._slice_timeline(d, include_suppressed=True, group="Group1")[0]
        assert [o["name"] for o in both["timeline"]] == ["Live", "Hid"]

    def test_a_collapsed_groups_member_carries_its_model_parameters(self):
        # timeline_params is the other half of the row pipeline the expansion has to run: without
        # it a group's members come back as bare name/type while every other row carries its knobs.
        ent = type("FilletFeature", (), {})()
        ent.name = "Fillet1"
        ent.entityToken = "tok-fillet"
        member = self._tlobj(1, "Fillet1", parent_group="Group1")
        member.entity = ent
        rows = [self._tlobj(0, "Group1", is_group=True, members=[member])]
        tl = SimpleNamespace(_items=rows, markerPosition=0, timelineGroups=[], count=len(rows),
                             item=lambda i, _r=list(rows): _r[i])
        param = SimpleNamespace(name="d7", role="radius", expression="3 mm", value=0.3,
                                createdBy=ent)
        d = SimpleNamespace(timeline=tl, allParameters=_NamedCollection([param]))
        out, err = dg._slice_timeline(d, include_suppressed=True, group="Group1",
                                      with_params=True)
        assert err is None
        assert out["timeline"][0]["params"] == [
            {"name": "d7", "role": "radius", "expression": "3 mm", "value": 0.3}]
        assert "params_note" in out               # the units caveat rides with any params row

    def test_the_groups_note_is_absent_when_a_group_scope_was_asked_for(self):
        # The note points at group='<name>'; shipped to a caller who already passed one it is
        # prose telling them to do what they did.
        d = self._design_with([self._tlobj(0, "Group1", is_group=True,
                                           members=[self._tlobj(1, "Fixture")])])
        out, _ = dg._slice_timeline(d, include_suppressed=True, group="Group1")
        assert "groups_note" not in out

    def test_a_timeline_with_no_group_row_carries_no_groups_note(self):
        d = self._design_with([self._tlobj(0, "Extrude1")])
        out, _ = dg._slice_timeline(d, include_suppressed=True, group="")
        assert "groups_note" not in out

    def test_slice_summary_states_and_exceptions(self):
        # the timeline slice leads with a states tally + exceptions = the FAILED features
        # (error/warning health). A suppressed (intentional) feature is NOT an exception.
        d = self._design_with([self._tlobj(0, "Good", health=0),
                               self._tlobj(1, "Bad", health=2),                    # error
                               self._tlobj(2, "Warned", health=1),                 # warning
                               self._tlobj(3, "Off", suppressed=True, health=3)])  # suppressed
        out, _ = dg._slice_timeline(d, include_suppressed=True, group="")
        s = out["summary"]
        assert s["states"]["error"] == 1 and s["states"]["warning"] == 1 and s["states"]["healthy"] == 1
        names = {e["name"] for e in s["exceptions"]}
        assert names == {"Bad", "Warned"}                # the failures; suppressed excluded

    def test_unreadable_row_flags_are_null_and_survive_the_terse_razor(self):
        # bool(safe(...)) turns an unreadable flag into a False, which EQUALS the noise default, so
        # terse drops it and the row reads as a normal healthy feature. Null keeps it visible.
        row_obj = self._tlobj(0, "Odd")
        del row_obj.isSuppressed
        del row_obj.isRolledBack
        out, err = dg._slice_timeline(self._design_with([row_obj]), True, "")
        assert err is None
        row = out["timeline"][0]
        assert row["is_suppressed"] is None and row["is_rolled_back"] is None

    def test_a_row_whose_suppressed_flag_did_not_read_is_not_filtered_out(self):
        # the gate's None-behavior, decided explicitly: only a flag that READ True hides a row, so
        # include_suppressed=false never drops a row on a flag nobody read.
        hidden = self._tlobj(1, "Unknown")
        del hidden.isSuppressed
        d = self._design_with([self._tlobj(0, "Live"), hidden])
        out, _ = dg._slice_timeline(d, include_suppressed=False, group="")
        assert [o["name"] for o in out["timeline"]] == ["Live", "Unknown"]

    def test_a_row_that_reads_suppressed_is_still_filtered_out(self):
        # the boundary on the other side: a True flag is an answer and still hides the row.
        d = self._design_with([self._tlobj(0, "Live"), self._tlobj(1, "Hid", suppressed=True)])
        out, _ = dg._slice_timeline(d, include_suppressed=False, group="")
        assert [o["name"] for o in out["timeline"]] == ["Live"]

    def test_slice_no_timeline_errors(self):
        class _DirectDesign(MakeDesign):
            """MEASURED (direct-design-timeline-raises): in a DIRECT design the timeline read
            RAISES this, so the guard has to catch the platform's error, not a missing attribute."""
            @property
            def timeline(self):
                raise RuntimeError(_DIRECT_DESIGN_TIMELINE)

        out, err = dg._slice_timeline(_DirectDesign(), include_suppressed=True, group="")
        assert out is None and err["isError"] is True
        assert "not a parametric design" in error_message(err)


class TestTimelineParams:
    """`timeline_params` - a timeline row carries type+name only, so a fillet's radius is unreadable
    from it. The opt-in adds each row's own MODEL parameters, grouped from ONE pass over
    design.allParameters by each parameter's .createdBy owner."""

    def _entity(self, name, type_name="FilletFeature", token=None):
        ent = type(type_name, (), {})()
        ent.name = name
        if token is not None:
            ent.entityToken = token
        return ent

    def _model_param(self, name, role, expression, value, created_by):
        return SimpleNamespace(name=name, role=role, expression=expression, value=value,
                               createdBy=created_by)

    def _user_param(self, name="Width", expression="20 mm", value=2.0):
        """A UserParameter: it carries NO createdBy, so the read raises AttributeError. That missing
        attribute IS the guard the grouping pass relies on - no type test."""
        return SimpleNamespace(name=name, expression=expression, value=value)

    def _row(self, entity, index=0, name=None):
        return SimpleNamespace(index=index, name=name or getattr(entity, "name", "Row"),
                               isGroup=False, isSuppressed=False, isRolledBack=False,
                               parentGroup=None, healthState=0, errorOrWarningMessage=None,
                               entity=entity)

    def _design(self, rows, params):
        tl = SimpleNamespace(markerPosition=0, timelineGroups=[], count=len(rows),
                             item=lambda i, _r=list(rows): _r[i])
        return SimpleNamespace(timeline=tl, allParameters=_NamedCollection(list(params)))

    def test_fillet_row_carries_its_radius_parameter(self):
        fillet = self._entity("Fillet1", token="tok-fillet")
        design = self._design([self._row(fillet)],
                              [self._model_param("d7", "radius", "3 mm", 0.3, fillet)])
        out, err = dg._slice_timeline(design, True, "", True)
        assert err is None
        assert out["timeline"][0]["params"] == [
            {"name": "d7", "role": "radius", "expression": "3 mm", "value": 0.3}]

    def test_a_user_parameter_does_not_sink_the_pass(self):
        # the UserParameter comes FIRST: an unguarded .createdBy read would raise before the
        # fillet's own parameter is ever seen, and the row would lose its radius.
        fillet = self._entity("Fillet1", token="tok-fillet")
        design = self._design([self._row(fillet)],
                              [self._user_param(),
                               self._model_param("d7", "radius", "3 mm", 0.3, fillet)])
        out, err = dg._slice_timeline(design, True, "", True)
        assert err is None
        params = out["timeline"][0]["params"]
        assert [p["name"] for p in params] == ["d7"]      # the user parameter belongs to no feature

    def test_off_by_default_and_does_not_walk_the_parameters(self):
        class _Counting(_NamedCollection):
            """A parameter collection recording how often its count was read."""
            _reads = 0

            @property
            def count(self):
                self._reads += 1
                return super().count

        fillet = self._entity("Fillet1", token="tok-fillet")
        params = _Counting([self._model_param("d7", "radius", "3 mm", 0.3, fillet)])
        design = SimpleNamespace(
            timeline=SimpleNamespace(markerPosition=0, timelineGroups=[], count=1,
                                     item=lambda i, _r=[self._row(fillet)]: _r[i]),
            allParameters=params)
        out, err = dg._slice_timeline(design, True, "")
        assert err is None and "params" not in out["timeline"][0]
        assert "params_note" not in out
        assert params._reads == 0           # the opt-in cost is not paid by the default call

    def test_name_and_type_match_when_no_token_reads(self):
        # neither side reads a token; the owner is still matched by name+type, which is the only
        # identity left.
        owner = self._entity("Extrude1", type_name="ExtrudeFeature")
        row_entity = self._entity("Extrude1", type_name="ExtrudeFeature")
        design = self._design([self._row(row_entity)],
                              [self._model_param("d1", "Distance", "10 mm", 1.0, owner)])
        out, _ = dg._slice_timeline(design, True, "", True)
        assert [p["name"] for p in out["timeline"][0]["params"]] == ["d1"]

    def test_two_owners_sharing_a_name_are_refused_not_mixed(self):
        # two DIFFERENT features (distinct tokens) wear one name+type. The row's own token does not
        # read, so only the name key is left - and answering it would publish the union of two
        # features' parameters on one row.
        a = self._entity("Fillet1", token="tok-a")
        b = self._entity("Fillet1", token="tok-b")
        row_entity = self._entity("Fillet1")           # no token
        design = self._design([self._row(row_entity)],
                              [self._model_param("d7", "radius", "3 mm", 0.3, a),
                               self._model_param("d9", "radius", "5 mm", 0.5, b)])
        out, _ = dg._slice_timeline(design, True, "", True)
        assert "params" not in out["timeline"][0]

    def _fresh_proxy_param(self, name, role, expression, value, owner_name,
                           type_name="FilletFeature", token=None, tl_index=None):
        """A ModelParameter whose createdBy mints a NEW proxy object per read - the measured
        live behavior (proxy identity is never stable) - around a shared owner identity."""
        class _P:
            def __init__(self):
                self.name, self.role = name, role
                self.expression, self.value = expression, value
            @property
            def createdBy(self):
                ent = type(type_name, (), {})()
                ent.name = owner_name
                if token is not None:
                    ent.entityToken = token
                if tl_index is not None:
                    ent.timelineObject = SimpleNamespace(index=tl_index)
                return ent
        return _P()

    def test_two_TOKENLESS_owners_sharing_a_name_are_also_refused(self):
        # neither owner carries a token and .createdBy mints a FRESH proxy per read (measured):
        # owner identity comes from the timeline index, and two different indices must refuse
        # the shared name rather than union two features' parameters.
        design = self._design(
            [self._row(self._entity("Fillet1"))],
            [self._fresh_proxy_param("d7", "radius", "3 mm", 0.3, "Fillet1", tl_index=4),
             self._fresh_proxy_param("d9", "radius", "5 mm", 0.5, "Fillet1", tl_index=9)])
        out, _ = dg._slice_timeline(design, True, "", True)
        assert "params" not in out["timeline"][0]

    def test_one_tokenless_owner_with_two_params_still_answers(self):
        # the same feature read twice mints two DISTINCT proxy objects - the shared timeline
        # index is what keeps its two parameters on one row instead of tripping the guard.
        design = self._design(
            [self._row(self._entity("Fillet1"))],
            [self._fresh_proxy_param("d7", "radius", "3 mm", 0.3, "Fillet1", tl_index=4),
             self._fresh_proxy_param("d8", "depth", "2 mm", 0.2, "Fillet1", tl_index=4)])
        out, _ = dg._slice_timeline(design, True, "", True)
        assert [p["name"] for p in out["timeline"][0]["params"]] == ["d7", "d8"]

    def test_tokenless_owners_with_no_timeline_index_refuse_a_multi_param_name(self):
        # no token AND no timeline index leaves no readable owner identity: the per-parameter
        # sentinel refuses the shared name - an absent answer is the safe direction, a union of
        # possibly-two features' parameters is not.
        design = self._design(
            [self._row(self._entity("Fillet1"))],
            [self._fresh_proxy_param("d7", "radius", "3 mm", 0.3, "Fillet1"),
             self._fresh_proxy_param("d9", "radius", "5 mm", 0.5, "Fillet1")])
        out, _ = dg._slice_timeline(design, True, "", True)
        assert "params" not in out["timeline"][0]

    def test_params_capped_with_a_flag(self, monkeypatch):
        monkeypatch.setattr(dg, "_PARAMS_PER_ROW", 1)
        hole = self._entity("Hole1", type_name="HoleFeature", token="tok-hole")
        design = self._design([self._row(hole)],
                              [self._model_param("d1", "HoleDiameter", "6 mm", 0.6, hole),
                               self._model_param("d2", "HoleDepth", "12 mm", 1.2, hole)])
        out, _ = dg._slice_timeline(design, True, "", True)
        row = out["timeline"][0]
        assert len(row["params"]) == 1 and row["params_truncated"] is True

    def test_unreadable_value_is_none_not_zero(self):
        fillet = self._entity("Fillet1", token="tok-fillet")
        design = self._design([self._row(fillet)],
                              [self._model_param("d7", "radius", "3 mm", "not-a-number", fillet)])
        out, _ = dg._slice_timeline(design, True, "", True)
        assert out["timeline"][0]["params"][0]["value"] is None

    def test_payload_names_the_unit_the_values_are_in(self):
        # value is the DATABASE-unit number (cm/radians) while expression carries the authored unit;
        # publishing the raw number with no unit said anywhere is a 10x error waiting to happen.
        fillet = self._entity("Fillet1", token="tok-fillet")
        design = self._design([self._row(fillet)],
                              [self._model_param("d7", "radius", "3 mm", 0.3, fillet)])
        out, _ = dg._slice_timeline(design, True, "", True)
        assert "internal units" in out["params_note"]

    def test_a_row_owning_no_parameters_stays_terse(self):
        fillet = self._entity("Fillet1", token="tok-fillet")
        sketch = self._entity("Sketch1", type_name="Sketch", token="tok-sketch")
        design = self._design([self._row(fillet), self._row(sketch, index=1)],
                              [self._model_param("d7", "radius", "3 mm", 0.3, fillet)])
        out, _ = dg._slice_timeline(design, True, "", True)
        assert "params" in out["timeline"][0] and "params" not in out["timeline"][1]


class TestTimelineRazor:
    """Keeping rows terse: a healthy timeline row drops its boring-default fields; an abnormal row keeps
    them and stands out. Tests _terse directly (pure, no design needed)."""

    def test_healthy_row_drops_noise(self):
        row = {"index": 1, "name": "Extrude1", "type": "ExtrudeFeature", "is_group": False,
               "is_suppressed": False, "is_rolled_back": False, "parent_group": None, "health": "healthy"}
        out = dg.terse(row, dg._TIMELINE_NOISE)
        assert out == {"index": 1, "name": "Extrude1", "type": "ExtrudeFeature"}

    def test_abnormal_row_keeps_its_flags(self):
        row = {"index": 2, "name": "Extrude2", "type": "ExtrudeFeature", "is_suppressed": True,
               "is_rolled_back": False, "health": "error"}
        out = dg.terse(row, dg._TIMELINE_NOISE)
        assert out["is_suppressed"] is True and out["health"] == "error"   # the interesting bits pop
        assert "is_rolled_back" not in out                                  # the boring one still dropped

    def test_tree_scope_params_pass_through(self, stub_slices):
        out = _payload(dg.handler(include=["tree"], max_depth=5))
        assert out["tree"]["max_depth"] == 5           # filter args reach the slice

    def test_configurations_degrades_for_non_configured_design(self, monkeypatch, stub_slices):
        # a non-configured design errors in the source -> design_get degrades to a marker, not a failure
        monkeypatch.setattr(dg, "_slice_configurations",
                            lambda d: (None, {"isError": True, "message": "not a configured design"}))
        out = _payload(dg.handler(include=["configurations"]))
        assert out["configurations"]["configured"] is False


# ── guards ──────────────────────────────────────────────────────────────────────────────────────

class TestGuards:
    def test_unknown_include_errors(self, monkeypatch):
        monkeypatch.setattr(dg._common, "design", lambda: object())   # a design, so we reach the check
        res = dg.handler(include=["bogus"])
        msg = error_message(res)
        assert "bogus" in msg.lower() or "unknown" in msg.lower()

    def test_the_refusal_lists_default_beside_the_slices(self, monkeypatch):
        # the refusal IS the vocabulary a caller that mistyped reads next; naming only the deep
        # slices hides the token that keeps the orientation block beside them.
        monkeypatch.setattr(dg._common, "design", lambda: object())
        msg = error_message(dg.handler(include=["bogus"]))
        assert "tree" in msg and "default" in msg

    def test_the_include_enum_matches_the_slice_tuple(self, stub_slices):
        # Catches a HAND-EDITED schema drifting from the tuple. It cannot catch a name added to the
        # tuple itself - both sides read it - which is what the dispatch test below covers.
        enum = dg.tool.input_schema["properties"]["include"]["items"]["enum"]
        assert sorted(enum) == sorted(dg._SLICES + dg._DEFAULT_NAMES)

    def test_every_advertised_slice_actually_dispatches(self, stub_slices):
        # A name in _SLICES that no `if ... in inc` branch reads returns a silent empty ok: the
        # schema offers a slice the router never builds. 'mode' is the one slice whose payload key
        # differs from its include name.
        keys = {"mode": "mode_detail"}
        for name in dg._SLICES:
            out = _payload(dg.handler(include=[name], attribute_group="g"))
            assert keys.get(name, name) in out, name

    def test_no_active_design_guard(self, monkeypatch):
        monkeypatch.setattr(dg._common, "design", lambda: None)
        res = dg.handler()
        assert error_message(res)


# ── include normalization (pure, no design needed) ─────────────────────────────────────────────────

class TestNormalizeInclude:
    def test_none_empty(self):
        assert dg._normalize_include(None) == [] and dg._normalize_include("") == []

    def test_comma_string(self):
        assert dg._normalize_include("tree, timeline") == ["tree", "timeline"]

    def test_list_lowercased(self):
        assert dg._normalize_include(["Tree", "TIMELINE"]) == ["tree", "timeline"]


# ── tree scoping (_find_occurrence_by_name): component-name scope + ambiguity refusal ───────────────
#
# Tree scoping resolves a name to the occurrence to root at: an occurrence name/fullPathName goes
# through the shared ambiguity-refusing resolver (a name shared by several instances is refused, not
# matched to the first), while a COMPONENT name roots at its first instance (all instances share one
# structure).

def _occ(name, comp_name, full=None):
    """One tree-scoping occurrence: its own name, its fullPathName, and the component it places."""
    return FakeOccurrence(path=full or name, component=MakeComp(comp_name))


def _wire_tree(monkeypatch, occs):
    root = MakeComp("RootComp", occurrences=occs)
    design = SimpleNamespace(rootComponent=root)
    monkeypatch.setattr(dg._common, "design", lambda: design)
    return root


class TestTreeUnresolvedRows:
    """The measured defect: the container read child_count 4 and listed four healthy children while a
    fifth occurrence, whose referenced component would not load, had no row at all - so an agent
    concluded the container held four healthy children."""

    def test_zero_unresolved_children_add_no_marker(self):
        design = _tree_design([_tocc("Bracket:1", kids=[_tocc("Pin:1")])])
        out, _ = dg._slice_tree(design, 3, "")
        node = out["children"][0]
        assert "children_unresolved" not in node
        assert [k["name"] for k in node["children"]] == ["Pin:1"]

    def test_an_unresolved_child_gets_a_ROW_and_the_parent_counts_it(self):
        design = _tree_design([_tocc("Op1 Workholding Container:1",
                                     kids=[_tocc("48205-125 (1):1")],
                                     broken_kids=[_broken_occ("45740")])])
        out, _ = dg._slice_tree(design, 3, "")
        node = out["children"][0]
        # child_count comes off childOccurrences, which DROPS it - so the count alone still reads 1
        assert node["child_count"] == 1
        assert node["children_unresolved"] == 1
        rows = {k["name"]: k for k in node["children"]}
        assert set(rows) == {"48205-125 (1):1", "45740"}
        assert rows["45740"]["unresolved"] is True
        assert rows["45740"]["detail"] == UNAVAILABLE
        # nothing is claimed about it: no child_count/body_count built from swallowed reads
        assert "child_count" not in rows["45740"] and "body_count" not in rows["45740"]

    def test_a_broken_TOP_LEVEL_occurrence_is_a_row_not_a_blank_node(self):
        design = _tree_design([_broken_occ("45740"), _tocc("Stock:1")])
        out, _ = dg._slice_tree(design, 3, "")
        rows = {c["name"]: c for c in out["children"]}
        assert rows["45740"] == {"name": "45740", "unresolved": True, "detail": UNAVAILABLE}
        assert rows["Stock:1"]["child_count"] == 0

    def test_a_node_whose_ONLY_children_are_unresolved_is_not_reported_childless(self):
        design = _tree_design([_tocc("Op1:1", broken_kids=[_broken_occ("45740")])])
        node = dg._slice_tree(design, 3, "")[0]["children"][0]
        assert node["child_count"] == 0                 # childOccurrences really is empty
        assert node["children_unresolved"] == 1
        assert [k["name"] for k in node["children"]] == ["45740"]

    def test_the_unresolved_rows_obey_the_LEVEL_cap_too(self):
        # appended past the cap, a level paged at 1 hands back 1 healthy child plus every
        # unresolved one - more rows than the caller asked for. children_unresolved keeps the count.
        design = _tree_design([_tocc("Op1:1", kids=[_tocc("Pin:1"), _tocc("Pin:2")],
                                     broken_kids=[_broken_occ("45740"), _broken_occ("45741")])])
        node = dg._slice_tree(design, 3, "", max_children=1)[0]["children"][0]
        assert len(node["children"]) == 1 and node["children_truncated"] is True
        assert node["children_unresolved"] == 2

    def test_the_depth_cap_flags_an_unresolved_child_it_did_not_emit(self):
        design = _tree_design([_tocc("Op1:1", broken_kids=[_broken_occ("45740")])])
        node = dg._slice_tree(design, 1, "")[0]["children"][0]
        assert node["children_truncated"] is True       # not walked - never "no children"
        assert node["children_unresolved"] == 1

    def test_the_scoped_tree_shows_it_too(self):
        design = _tree_design([_tocc("Op1 Workholding Container:1",
                                     broken_kids=[_broken_occ("45740")])])
        out, err = dg._slice_tree(design, 3, "Op1 Workholding Container")
        assert err is None
        assert out["tree"]["children_unresolved"] == 1
        assert out["tree"]["children"][0]["name"] == "45740"


class TestFindOccurrenceByName:
    def test_component_name_roots_at_first_instance(self, monkeypatch):
        # a bare component name is unambiguous for a READ: every instance shows the same structure.
        occs = [_occ("Bracket:1", "Bracket"), _occ("Bracket:2", "Bracket")]
        root = _wire_tree(monkeypatch, occs)
        found, err = dg._find_occurrence_by_name(root, "Bracket")
        assert err is None
        assert found is not None and found.component.name == "Bracket"

    def test_exact_occurrence_name_resolves(self, monkeypatch):
        occs = [_occ("Gear:1", "Gear"), _occ("Gear:2", "Gear")]
        root = _wire_tree(monkeypatch, occs)
        found, err = dg._find_occurrence_by_name(root, "Gear:2")
        assert err is None and found is not None and found.name == "Gear:2"

    def test_ambiguous_occurrence_name_is_refused_not_first_matched(self, monkeypatch):
        # 'Bolt' substring-matches two DIFFERENT-component instances - must refuse (list candidates),
        # never silently root at the first.
        occs = [_occ("M6-Bolt:1", "M6-Bolt"), _occ("M8-Bolt:1", "M8-Bolt")]
        root = _wire_tree(monkeypatch, occs)
        found, err = dg._find_occurrence_by_name(root, "Bolt")
        assert found is None
        assert err and "ambiguous" in err.lower()

    def test_miss_returns_no_error(self, monkeypatch):
        occs = [_occ("Gear:1", "Gear")]
        root = _wire_tree(monkeypatch, occs)
        found, err = dg._find_occurrence_by_name(root, "Nonexistent")
        assert found is None and err is None


class TestRootBodies:
    """The tree walks occurrences, so bodies directly in the ROOT component would be invisible
    without _root_body_names (a root body isn't a jointable occurrence — the agent must be told it
    exists). design_get(tree) must include root-level bodies."""

    def test_root_body_names_lists_direct_bodies(self):
        from types import SimpleNamespace

        def _coll(items):
            return SimpleNamespace(count=len(items), item=lambda i: items[i])

        body = SimpleNamespace(name="RootBlock")
        root = SimpleNamespace(bRepBodies=_coll([body]))
        assert dg._root_body_names(root) == (["RootBlock"], False)

    def test_the_root_body_name_list_is_capped_and_says_so(self, monkeypatch):
        # the names branch shared the tree's 2000-node bound and reported no cut, so a root holding
        # more bodies than the cap read as a COMPLETE list of the ones it happened to reach.
        monkeypatch.setattr(dg, "_TREE_BODY_CAP", 2)
        design = _tree_design([_tocc("Gear:1")], root_bodies=3)
        out, _ = dg._slice_tree(design, 3, "")
        assert out["root_bodies"] == ["RootBody1", "RootBody2"]
        assert out["root_bodies_truncated"] is True

    def test_a_root_body_list_exactly_at_the_cap_is_not_flagged(self, monkeypatch):
        monkeypatch.setattr(dg, "_TREE_BODY_CAP", 2)
        design = _tree_design([_tocc("Gear:1")], root_bodies=2)
        out, _ = dg._slice_tree(design, 3, "")
        assert len(out["root_bodies"]) == 2 and "root_bodies_truncated" not in out

    def test_no_root_bodies_returns_empty(self):
        from types import SimpleNamespace
        root = SimpleNamespace(bRepBodies=SimpleNamespace(count=0, item=lambda i: None))
        assert dg._root_body_names(root) == ([], False)


# ── the tree slice itself (_slice_tree / _walk_occurrence) — depth/cap bounds + node shape ──────────
#
# The router tests above stub _slice_tree; these pin the slice's OWN behavior: the depth clamp, the
# node cap with its truncated flag, per-node counts, reference metadata, and the component scope.

def _tbody(name, token=None, solid=True, visible=True):
    """A body a tree node can publish a record for: name + entityToken handle + solid/visible flags."""
    return SimpleNamespace(name=name, entityToken=token or f"btok-{name}",
                           isSolid=solid, isVisible=visible)


UNAVAILABLE = ("3 : The occurrence's referenced component is unavailable (broken or missing "
               "external reference).")


def _broken_occ(name="45740"):
    """An occurrence whose referenced component will not load. Only `name` reads; component,
    fullPathName and childOccurrences raise, and isReferencedComponent reads FALSE - so the tree's
    childOccurrences walk never yields it and no is_reference gate would catch it."""
    invalid = "2 : InternalValidationError : path.valid()"
    occ = FakeOccurrence(path=name, raises_on={"component": UNAVAILABLE, "fullPathName": invalid,
                                               "childOccurrences": invalid})
    occ.isReferencedComponent = False
    return occ


def _tocc(name, comp=None, kids=(), bodies=0, is_ref=False, docref=None, token=None,
          broken_kids=()):
    """A tree-walkable occurrence: named collections (count + iterable) for bodies/children, plus the
    entityToken the row publishes as its handle. `broken_kids` land ONLY in the component-local
    collection, mirroring the measured structure - childOccurrences drops an unresolved child."""
    return SimpleNamespace(
        name=name, fullPathName=name, entityToken=token or f"tok-{name}",
        component=SimpleNamespace(name=comp or name.split(":")[0],
                                  occurrences=_NamedCollection(list(broken_kids) + list(kids))),
        isReferencedComponent=is_ref,
        bRepBodies=_NamedCollection([_tbody(f"B{i+1}") for i in range(bodies)]),
        childOccurrences=_NamedCollection(kids),
        documentReference=docref,
    )


def _tree_design(occs=(), root_bodies=0, root_name="RootComp"):
    bodies = [_tbody(f"RootBody{i+1}") for i in range(root_bodies)]
    root = SimpleNamespace(name=root_name, occurrences=_NamedCollection(occs),
                           allOccurrences=list(occs), bRepBodies=_NamedCollection(bodies))
    return SimpleNamespace(rootComponent=root)


class TestSliceTree:
    def test_unscoped_walk_lists_children_with_counts(self):
        kid = _tocc("Pin:1")
        design = _tree_design([_tocc("Bracket:1", kids=[kid], bodies=2), _tocc("Gear:1")])
        out, err = dg._slice_tree(design, 3, "")
        assert err is None
        assert out["root"] == "RootComp" and out["node_count"] == 3   # 2 top-level + 1 child
        assert [c["name"] for c in out["children"]] == ["Bracket:1", "Gear:1"]
        bracket = out["children"][0]
        assert bracket["body_count"] == 2 and bracket["child_count"] == 1
        assert [k["name"] for k in bracket["children"]] == ["Pin:1"]
        assert out["truncated"] is False and "root_bodies" not in out

    def test_an_unreadable_count_is_null_not_zero(self):
        # these two counts ARE the caller's evidence of what a node holds, so a coerced 0 says "no
        # bodies / no children" about a collection nothing was read from - and this walk is exactly
        # where an agent decides a sub-assembly is empty and stops drilling.
        occ = _tocc("Gear:1")
        del occ.bRepBodies
        del occ.childOccurrences
        node = dg._walk_occurrence(occ, 0, 3, {"n": 0, "truncated": False})
        assert node["body_count"] is None and node["child_count"] is None
        assert "children" not in node and "children_truncated" not in node

    def test_a_genuinely_empty_node_still_reads_zero(self):
        # the boundary the null must not swallow: 0 is an answer when the collections DID read.
        node = dg._walk_occurrence(_tocc("Gear:1"), 0, 3, {"n": 0, "truncated": False})
        assert node["body_count"] == 0 and node["child_count"] == 0

    def test_a_null_body_count_pays_for_no_body_records(self):
        # the count gates the opt-in body walk; a null must take the same branch a 0 does, without
        # claiming the node is empty.
        occ = _tocc("Gear:1")
        del occ.bRepBodies
        node = dg._walk_occurrence(occ, 0, 3, {"n": 0, "truncated": False}, with_bodies=True)
        assert node["body_count"] is None and "bodies" not in node

    def test_tree_handles_carry_the_exact_identity_at_every_depth(self):
        # a name repeats under every sub-assembly and even a fullPathName can be worn by two
        # siblings, so the opt-in address has to reach the nested rows too, not just the top level.
        design = _tree_design([_tocc("Bracket:1", kids=[_tocc("Pin:1")])])
        out, _ = dg._slice_tree(design, 3, "", with_handles=True)
        node = out["children"][0]
        assert node["handle"] == "tok-Bracket:1" and node["full_path"] == "Bracket:1"
        assert node["children"][0]["handle"] == "tok-Pin:1"

    def test_the_light_node_withholds_the_handle_and_names_the_flag_that_restores_it(self):
        # the measured flood: ~200 chars of handle on every one of 64 nodes. The default node drops
        # it - and a dropped address is only safe because the note says which flag brings it back.
        design = _tree_design([_tocc("Bracket:1", kids=[_tocc("Pin:1")])])
        out, _ = dg._slice_tree(design, 3, "")
        node = out["children"][0]
        assert "handle" not in node and "full_path" not in node
        assert set(node) >= {"name", "component", "body_count", "child_count"}
        assert "tree_handles=true" in out["note"]

    def test_an_unreadable_token_reads_as_no_handle_not_a_wrong_one(self):
        occ = _tocc("Gear:1")
        del occ.entityToken
        node = dg._walk_occurrence(occ, 0, 3, {"n": 0, "truncated": False}, with_handles=True)
        assert node["handle"] is None and node["full_path"] == "Gear:1"

    def test_depth_clamped_to_max(self):
        out, _ = dg._slice_tree(_tree_design([_tocc("Gear:1")]), 99, "")
        assert out["max_depth"] == dg._TREE_MAX_DEPTH

    def test_unparseable_depth_falls_back_to_default(self):
        out, _ = dg._slice_tree(_tree_design([_tocc("Gear:1")]), "junk", "")
        assert out["max_depth"] == dg._TREE_DEFAULT_DEPTH

    def test_max_depth_cuts_off_children_and_flags_it(self):
        # at the depth limit a node with children carries children_truncated instead of the children -
        # an agent must be able to tell "no children" from "not walked".
        design = _tree_design([_tocc("Bracket:1", kids=[_tocc("Pin:1")])])
        out, _ = dg._slice_tree(design, 1, "")
        node = out["children"][0]
        assert node["children_truncated"] is True and "children" not in node

    def test_node_cap_truncates_the_walk(self, monkeypatch):
        monkeypatch.setattr(dg, "_TREE_MAX_NODES", 2)
        design = _tree_design([_tocc(f"P{i}:1") for i in range(3)])
        out, _ = dg._slice_tree(design, 3, "")
        assert out["truncated"] is True and len(out["children"]) == 2

    def test_the_top_level_is_capped_per_page_and_still_counts_the_rest(self):
        # the measured flood: 64 top-level occurrences returned in full. The page is bounded and
        # child_count keeps the true total, so the caller learns what it did NOT get.
        design = _tree_design([_tocc(f"P{i}:1") for i in range(5)])
        out, _ = dg._slice_tree(design, 3, "", max_children=2)
        assert [c["name"] for c in out["children"]] == ["P0:1", "P1:1"]
        assert out["child_count"] == 5 and out["children_truncated"] is True

    def test_a_top_level_exactly_at_the_page_size_is_not_flagged_truncated(self):
        # the boundary: exactly cap-many children is a COMPLETE level. A >= guard here would claim
        # rows were dropped when none were, and send the caller paging for nothing.
        design = _tree_design([_tocc(f"P{i}:1") for i in range(2)])
        out, _ = dg._slice_tree(design, 3, "", max_children=2)
        assert len(out["children"]) == 2 and out["children_truncated"] is False

    def test_the_page_size_bounds_every_LEVEL_not_just_the_top(self):
        # a 30-child level three deep is the same flood as a 30-child root; the cap travels down.
        kids = [_tocc(f"Pin:{i}") for i in range(4)]
        design = _tree_design([_tocc("Bracket:1", kids=kids)])
        out, _ = dg._slice_tree(design, 3, "", max_children=2)
        node = out["children"][0]
        assert len(node["children"]) == 2 and node["children_truncated"] is True
        assert node["child_count"] == 4          # the level's true count, not the page

    def test_a_nested_level_exactly_at_the_page_size_is_not_flagged(self):
        design = _tree_design([_tocc("Bracket:1", kids=[_tocc(f"Pin:{i}") for i in range(2)])])
        node = dg._slice_tree(design, 3, "", max_children=2)[0]["children"][0]
        assert len(node["children"]) == 2 and "children_truncated" not in node

    def test_an_unset_page_size_falls_back_to_the_default(self):
        # max_results defaults to 0 on the wire (it is the catalog's 'unset'), and max(1, 0) would
        # silently page the tree one node at a time.
        design = _tree_design([_tocc(f"P{i}:1") for i in range(3)])
        out, _ = dg._slice_tree(design, 3, "", max_children=0)
        assert len(out["children"]) == 3 and out["children_truncated"] is False


class TestTreeNameFilter:
    """The top-level name filter - the narrowing that answers 'where is the Seat' without the other
    63 occurrences coming too."""

    def test_filter_keeps_only_the_matching_top_level_nodes(self):
        design = _tree_design([_tocc("SeatFrame:1", comp="SeatFrame"), _tocc("Leg:1", comp="Leg")])
        out, _ = dg._slice_tree(design, 3, "", name_filter="seat")
        assert [c["name"] for c in out["children"]] == ["SeatFrame:1"]
        assert out["matched"] == 1 and out["child_count"] == 2 and out["name_filter"] == "seat"

    def test_filter_matches_the_COMPONENT_name_too(self):
        # an occurrence auto-named 'Component1:1' places a component the modeller DID name; keying
        # on the occurrence name alone would answer nothing for the name the caller knows.
        design = _tree_design([_tocc("Component1:1", comp="Armrest")])
        out, _ = dg._slice_tree(design, 3, "", name_filter="armrest")
        assert [c["name"] for c in out["children"]] == ["Component1:1"]

    def test_a_filter_that_matches_nothing_is_an_empty_level_not_an_error(self):
        design = _tree_design([_tocc("Leg:1", comp="Leg")])
        out, err = dg._slice_tree(design, 3, "", name_filter="nosuchpart")
        assert err is None and out["children"] == [] and out["matched"] == 0
        assert out["child_count"] == 1 and out["children_truncated"] is False

    def test_no_filter_leaves_the_filter_keys_off_the_payload(self):
        out, _ = dg._slice_tree(_tree_design([_tocc("Leg:1")]), 3, "")
        assert "name_filter" not in out and "matched" not in out

    def test_the_filter_pages_on_the_MATCHES_not_on_the_whole_level(self):
        # child_count is the level's total, so truncation has to be judged against what matched -
        # comparing the page against child_count would flag a complete filtered answer.
        design = _tree_design([_tocc("Seat:1"), _tocc("Seat:2"), _tocc("Leg:1")])
        out, _ = dg._slice_tree(design, 3, "", max_children=2, name_filter="seat")
        assert len(out["children"]) == 2 and out["matched"] == 2
        assert out["child_count"] == 3 and out["children_truncated"] is False

    def test_root_bodies_surface_with_promote_note(self):
        # bodies directly in root are invisible to the occurrence walk - the slice must list them and
        # say how to make one jointable.
        design = _tree_design([_tocc("Gear:1")], root_bodies=1)
        out, _ = dg._slice_tree(design, 3, "")
        assert out["root_bodies"] == ["RootBody1"]
        assert "model_create_component" in out["root_bodies_note"]

    def test_default_walk_carries_no_body_records(self):
        # body records are opt-in cost: without tree_bodies a node carries body_count only.
        design = _tree_design([_tocc("Bracket:1", bodies=2)])
        out, _ = dg._slice_tree(design, 3, "")
        assert "bodies" not in out["children"][0]

    def test_tree_bodies_adds_name_handle_and_honest_flags(self):
        # the record that makes a body TARGETABLE without replaying old feature receipts:
        # name + entityToken handle + solid/visible.
        design = _tree_design([_tocc("Bracket:1", bodies=2)])
        out, _ = dg._slice_tree(design, 3, "", with_bodies=True)
        rows = out["children"][0]["bodies"]
        assert rows[0] == {"name": "B1", "handle": "btok-B1", "is_solid": True, "visible": True}
        assert [r["name"] for r in rows] == ["B1", "B2"]

    def test_tree_bodies_reaches_nested_children(self):
        design = _tree_design([_tocc("Bracket:1", kids=[_tocc("Pin:1", bodies=1)])])
        out, _ = dg._slice_tree(design, 3, "", with_bodies=True)
        kid = out["children"][0]["children"][0]
        assert kid["bodies"][0]["name"] == "B1"

    def test_tree_bodies_reaches_a_component_scoped_walk(self, monkeypatch):
        design = _tree_design([_tocc("Bracket:1", comp="Bracket", bodies=1)])
        monkeypatch.setattr(dg._common, "design", lambda: design)
        out, err = dg._slice_tree(design, 3, "Bracket", with_bodies=True)
        assert err is None and out["tree"]["bodies"][0]["handle"] == "btok-B1"

    def test_body_rows_capped_with_flag(self, monkeypatch):
        monkeypatch.setattr(dg, "_TREE_BODY_CAP", 1)
        design = _tree_design([_tocc("Bracket:1", bodies=3)])
        out, _ = dg._slice_tree(design, 3, "", with_bodies=True)
        node = out["children"][0]
        assert len(node["bodies"]) == 1 and node["bodies_truncated"] is True

    def test_body_rows_exactly_at_the_cap_are_not_flagged(self, monkeypatch):
        # the boundary: exactly cap-many bodies is a COMPLETE list - a >= guard would flag a
        # complete list as truncated, claiming bodies were dropped when none were.
        monkeypatch.setattr(dg, "_TREE_BODY_CAP", 2)
        design = _tree_design([_tocc("Bracket:1", bodies=2)])
        out, _ = dg._slice_tree(design, 3, "", with_bodies=True)
        node = out["children"][0]
        assert len(node["bodies"]) == 2 and "bodies_truncated" not in node

    def test_unreadable_body_flags_read_None_not_False(self):
        # read_flag honesty: a body whose isSolid/isVisible cannot be read publishes None - a
        # coerced False would claim "surface body, hidden" about a body nothing was read from.
        bare = SimpleNamespace(name="Mystery", entityToken="btok-M")   # no isSolid / isVisible
        rows, truncated = dg._body_rows(_NamedCollection([bare]))
        assert truncated is False
        assert rows[0]["is_solid"] is None and rows[0]["visible"] is None

    def test_tree_bodies_upgrades_root_bodies_to_records(self):
        design = _tree_design([_tocc("Gear:1")], root_bodies=1)
        out, _ = dg._slice_tree(design, 3, "", with_bodies=True)
        row = out["root_bodies"][0]
        assert row["name"] == "RootBody1" and row["handle"]
        assert "model_create_component" in out["root_bodies_note"]

    def test_component_scope_roots_the_tree_there(self):
        kid = _tocc("Pin:1")
        design = _tree_design([_tocc("Bracket:1", comp="Bracket", kids=[kid]),
                               _tocc("Gear:1", comp="Gear")])
        out, err = dg._slice_tree(design, 3, "Bracket")
        assert err is None
        assert out["root"] == "Bracket" and out["tree"]["name"] == "Bracket:1"
        assert [k["name"] for k in out["tree"]["children"]] == ["Pin:1"]
        assert "children" not in out                       # scoped: one rooted tree, not the root list

    def test_component_scoped_tree_reports_truncation_from_the_walk(self, monkeypatch):
        # the truncated flag must be read AFTER the scoped walk runs - a dict literal that reads
        # the counter before walking pins the pre-walk False and a capped tree reads complete.
        monkeypatch.setattr(dg, "_TREE_MAX_NODES", 2)
        kids = [_tocc(f"Pin:{i}") for i in range(1, 5)]
        design = _tree_design([_tocc("Bracket:1", comp="Bracket", kids=kids)])
        monkeypatch.setattr(dg._common, "design", lambda: design)
        out, err = dg._slice_tree(design, 3, "Bracket")
        assert err is None and out["truncated"] is True

    def test_component_scope_miss_errors_naming_it(self, monkeypatch):
        root = _wire_tree(monkeypatch, [_occ("Gear:1", "Gear")])
        out, err = dg._slice_tree(SimpleNamespace(rootComponent=root), 3, "Ghost")
        assert out is None and "Ghost" in error_message(err)

    def test_no_root_component_errors(self):
        out, err = dg._slice_tree(SimpleNamespace(rootComponent=None), 3, "")
        assert out is None and "root" in error_message(err).lower()


class TestTreePayloadSize:
    """The rich read's SIZE on a large design - the thing the projection exists to bound.

    MEASURED with tree_handles=true on the Airport Seating Primary Assembly sample (65 nodes, 64
    top level): 41.1 KB compact, and 21.3 KB at max_depth=1; on the Bench sample (117 nodes),
    77.3 KB. The fake rig below stands in for that shape offline."""

    # The ceiling sits between the fake rig's two projections, so restoring either half - the
    # addresses by default, or an uncapped level - trips it.
    LIGHT_CEILING_BYTES = 20_000

    def _big(self, n=64, per=3):
        handle = "a" * 200            # the measured ~200-char entityToken
        kids = lambda i: [_tocc(f"Sub{i}_{j}:1", token=handle) for j in range(per)]
        return _tree_design([_tocc(f"Part{i}:1", kids=kids(i), token=handle) for i in range(n)])

    def _size(self, payload):
        return len(json.dumps(payload, separators=(",", ":")))

    def test_the_light_tree_read_stays_an_order_of_magnitude_under_the_full_one(self):
        design = self._big()
        light = self._size(dg._slice_tree(design, 3, "")[0])
        full = self._size(dg._slice_tree(design, 3, "", with_handles=True,
                                         max_children=10_000)[0])
        assert light < self.LIGHT_CEILING_BYTES < full

    def test_the_name_filter_answers_one_branch_not_the_assembly(self):
        # the narrowing the note points at has to actually pay: 'Part7' is one of 64.
        design = self._big()
        assert self._size(dg._slice_tree(design, 3, "", name_filter="Part7")[0]) < 2_000


class TestWalkOccurrenceReference:
    """An xref node carries its source identity (version/staleness/file) so an agent can see WHERE a
    referenced component comes from and whether it is out of date."""

    _DF = SimpleNamespace(id="urn:adsk:123", name="LibPart", fusionWebURL="https://autodesk/x")

    def _ref_node(self, **kw):
        dr = SimpleNamespace(version=7, isOutOfDate=True, dataFile=self._DF)
        return dg._walk_occurrence(_tocc("Lib:1", is_ref=True, docref=dr), 0, 3,
                                   {"n": 0, "truncated": False}, **kw)

    def test_reference_node_carries_source_metadata(self):
        node = self._ref_node()
        assert node["is_reference"] is True
        assert node["source_version"] == 7 and node["is_out_of_date"] is True
        # the light node names the source FILE; its unique address rides under tree_handles
        assert node["source_name"] == "LibPart"

    def test_the_light_node_withholds_the_source_addresses(self):
        # the default tree is the read that must stay light, so the addresses ride with the other
        # addresses instead of on every node.
        node = self._ref_node()
        assert "source_id" not in node and "source_url" not in node

    def test_tree_handles_adds_the_source_addresses(self):
        node = self._ref_node(with_handles=True)
        assert node["source_id"] == "urn:adsk:123"
        assert node["source_url"] == "https://autodesk/x"
        assert node["source_name"] == "LibPart"

    def test_local_node_states_neither_is_reference_nor_source_fields(self):
        # a flag that READ false is the boring case: publishing is_reference:false on every node of
        # a 64-node local assembly is noise, and its absence is not a claim about anything.
        node = dg._walk_occurrence(_tocc("Gear:1"), 0, 3, {"n": 0, "truncated": False})
        assert "is_reference" not in node and "source_version" not in node

    def test_unreadable_is_reference_is_null_not_false(self):
        # read_flag honesty: a coerced False claims "this is a local component" about an occurrence
        # nothing was read from.
        occ = _tocc("Lib:1")
        del occ.isReferencedComponent
        node = dg._walk_occurrence(occ, 0, 3, {"n": 0, "truncated": False})
        assert node["is_reference"] is None

    def test_a_null_is_reference_still_reads_the_freshness_block(self):
        # the gate's None-behavior, decided explicitly: None takes the TRUE branch, because a local
        # occurrence simply carries no documentReference while an xref whose own flag will not read
        # still has real staleness to publish - the False branch would drop a STALE reference.
        df = SimpleNamespace(id="urn:adsk:9", name="LibPart", fusionWebURL="https://autodesk/x")
        occ = _tocc("Lib:1", docref=SimpleNamespace(version=2, isOutOfDate=True, dataFile=df))
        del occ.isReferencedComponent
        node = dg._walk_occurrence(occ, 0, 3, {"n": 0, "truncated": False})
        assert node["is_reference"] is None
        assert node["is_out_of_date"] is True and node["source_name"] == "LibPart"

    def test_a_false_is_reference_still_skips_the_freshness_block(self):
        # the other side of that gate: a flag that READ false is an answer, so the cloud reads stay
        # unpaid for a genuinely local occurrence - and the gate keys on the READ, not on the key
        # the row no longer publishes.
        occ = _tocc("Gear:1", docref=SimpleNamespace(version=2, isOutOfDate=True, dataFile=None))
        node = dg._walk_occurrence(occ, 0, 3, {"n": 0, "truncated": False})
        assert "is_reference" not in node and "is_out_of_date" not in node


# ── the configurations slice (_slice_configurations) ───────────────────────────────────────────────

class TestSliceConfigurations:
    def _row(self, name, rid, idx):
        return SimpleNamespace(name=name, id=rid, index=idx)

    def _design(self, rows, active=None, cols=()):
        table = SimpleNamespace(name="Table1", id="t1", activeRow=active,
                                rows=list(rows), columns=list(cols))
        return SimpleNamespace(configurationTopTable=table)

    def test_not_configured_design_errors(self):
        out, err = dg._slice_configurations(SimpleNamespace(configurationTopTable=None))
        assert out is None and "configured design" in error_message(err).lower()

    def test_rows_columns_and_active_flag(self):
        r1, r2 = self._row("Variant A", "r1", 0), self._row("Variant B", "r2", 1)
        col = SimpleNamespace(title="Length", id="c1", index=0)
        out, err = dg._slice_configurations(self._design([r1, r2], active=r2, cols=[col]))
        assert err is None
        assert out["active_configuration"] == "Variant B"
        assert [r["is_active"] for r in out["configurations"]] == [False, True]
        assert out["configuration_count"] == 2 and "truncated" not in out
        assert out["columns"][0]["title"] == "Length"   # ConfigurationColumn exposes .title, not .name

    def test_row_cap_truncates(self, monkeypatch):
        monkeypatch.setattr(dg, "_CONFIG_MAX_ROWS", 1)
        out, _ = dg._slice_configurations(self._design([self._row("A", "r1", 0),
                                                        self._row("B", "r2", 1)]))
        assert out["truncated"] is True and out["configuration_count"] == 1


# ── the attributes slice (_slice_attributes) ───────────────────────────────────────────────────────
#
# design_edit_timeline can SET and DELETE an entity attribute; this is the read side. The group is
# required - the slice must refuse rather than dump every attribute in the design.

def _attr_design(attrs):
    """A design whose findAttributes filters like the platform's: rows in the named group, narrowed
    to one key only when a key was asked for (an empty key matches every key). It hands back a plain
    list - an AttributeVector answers len()/[i], never .count/.item(i). `calls` records what the
    slice actually searched for."""
    calls = []

    def find(group, key):
        calls.append((group, key))
        return [a for a in attrs if a._group == group and (not key or a.name == key)]

    return SimpleNamespace(findAttributes=find, calls=calls)


def _attr(group, key, value, parent=None):
    a = SimpleNamespace(name=key, value=value, parent=parent)
    a._group = group
    return a


def _attr_parent(type_name, name=None):
    ent = type(type_name, (), {})()
    if name is not None:
        ent.name = name
    return ent


class TestSliceAttributes:
    def test_group_filter_returns_only_that_group(self):
        design = _attr_design([_attr("shop", "op", "mill"), _attr("other", "op", "turn")])
        out, err = dg._slice_attributes(design, "shop", "")
        assert err is None
        assert out["count"] == 1 and [r["value"] for r in out["attributes"]] == ["mill"]
        assert out["attributes"][0]["group"] == "shop"

    def test_empty_key_matches_every_key_in_the_group(self):
        design = _attr_design([_attr("shop", "op", "mill"), _attr("shop", "rev", "B")])
        out, err = dg._slice_attributes(design, "shop", "")
        assert err is None
        assert {r["key"] for r in out["attributes"]} == {"op", "rev"}
        assert design.calls == [("shop", "")]      # the empty key crosses to findAttributes as-is
        assert out["key"] is None

    def test_key_narrows_to_one(self):
        design = _attr_design([_attr("shop", "op", "mill"), _attr("shop", "rev", "B")])
        out, _ = dg._slice_attributes(design, "shop", "rev")
        assert [r["key"] for r in out["attributes"]] == ["rev"]
        assert out["key"] == "rev"

    def test_missing_group_is_refused_naming_what_it_needs(self):
        design = _attr_design([_attr("shop", "op", "mill")])
        out, err = dg._slice_attributes(design, "  ", "op")
        assert out is None
        assert "attribute_group" in error_message(err)
        assert design.calls == []                  # refused BEFORE any design-wide search ran

    def test_row_names_the_entity_the_attribute_is_attached_to(self):
        design = _attr_design([
            _attr("shop", "op", "mill", parent=_attr_parent("ExtrudeFeature", "Extrude3"))])
        out, _ = dg._slice_attributes(design, "shop", "")
        assert out["attributes"][0]["entity"] == {"type": "ExtrudeFeature", "name": "Extrude3"}

    def test_nameless_entity_still_reports_its_type(self):
        design = _attr_design([_attr("shop", "op", "mill", parent=_attr_parent("BRepFace"))])
        out, _ = dg._slice_attributes(design, "shop", "")
        assert out["attributes"][0]["entity"] == {"type": "BRepFace"}      # no invented name

    def test_no_matches_is_an_empty_read_not_an_error(self):
        design = _attr_design([_attr("other", "op", "mill")])
        out, err = dg._slice_attributes(design, "shop", "")
        assert err is None and out["count"] == 0 and out["attributes"] == []

    def test_cap_truncates_and_reports_the_true_total(self, monkeypatch):
        monkeypatch.setattr(dg, "_ATTR_MAX_ROWS", 1)
        design = _attr_design([_attr("shop", "a", "1"), _attr("shop", "b", "2")])
        out, _ = dg._slice_attributes(design, "shop", "")
        assert out["truncated"] is True and out["returned"] == 1 and out["count"] == 2

    def test_unreadable_search_is_an_error_not_an_empty_read(self):
        class _Raises(MakeDesign):
            """A design whose attribute search will not run."""
            def findAttributes(self, group, key):
                raise RuntimeError("attribute search unavailable")

        out, err = dg._slice_attributes(_Raises(), "shop", "")
        assert out is None and "shop" in error_message(err)


# ── router error propagation + _unwrap ─────────────────────────────────────────────────────────────

class TestRouterErrorPropagation:
    def test_tree_slice_error_fails_the_read(self, monkeypatch, stub_slices):
        monkeypatch.setattr(dg, "_slice_tree",
                            lambda d, md, c, wb=False, wh=False, mc=0, nf="": (
                                None, dg.error("no tree here")))
        res = dg.handler(include=["tree"])
        assert res["isError"] and "no tree here" in error_message(res)

    def test_timeline_slice_error_fails_the_read(self, monkeypatch, stub_slices):
        monkeypatch.setattr(dg, "_slice_timeline",
                            lambda d, s, g, p=False, mr=0: (
                                None, dg.error("direct-modeling design")))
        res = dg.handler(include=["timeline"])
        assert res["isError"] and "direct-modeling" in error_message(res)

    def test_mode_error_fails_the_default(self, monkeypatch, stub_slices):
        monkeypatch.setattr(dg, "_slice_mode", lambda d: (None, dg.error("mode read failed")))
        res = dg.handler()
        assert res["isError"] and "mode read failed" in error_message(res)

    def test_in_base_feature_edit_surfaces_only_when_true(self, monkeypatch, stub_slices):
        monkeypatch.setattr(dg, "_slice_mode", lambda d: (
            {"design_type": "parametric", "timeline_feature_count": 1,
             "in_base_feature_edit": True}, None))
        out = _payload(dg.handler())
        assert out["in_base_feature_edit"] is True


class TestUnwrap:
    def test_ok_payload_decodes(self):
        assert dg._unwrap(dg.ok({"a": 1})) == ({"a": 1}, None)

    def test_error_passes_through(self):
        res = dg.error("boom")
        assert dg._unwrap(res) == (None, res)

    def test_undecodable_ok_returns_the_result_as_error(self):
        res = {"isError": False, "content": [{"text": "not json{"}]}
        assert dg._unwrap(res) == (None, res)
