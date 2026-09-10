"""Tests for `design_remove_feature` - the either/or target pair, the component whose
removeFeatures collection the feature is added to, and the census that judges whether the item
really went away.

The census (a fresh walk of the collection the item lived in) is the only honest survivor signal:
a wrapper held across a successful remove keeps answering isValid True, so nothing here lets the
handler trust the object it was handed.
"""

import types

import adsk.fusion
import pytest

from conftest import (BRepBody, FakeOccurrence, FakeTimelineObject, MakeComp, _NamedCollection,
                      error_message, install, load_tool, make_design, payload)

drf = load_tool("design_remove_feature")

# The walk that will not enumerate at all - neither allOccurrences nor the component.occurrences
# fallback answers, which is a different state from an empty assembly.
_UNREADABLE = "gone"


# ── fake surfaces built from the shared conftest fakes ──────────────────────────────────────────

def _remove_features(*, on_add=None, feature_name="Remove1", returns_feature=True, registers=True,
                     raises=False):
    """A features.removeFeatures fake: add(item) records the item, runs the realistic side effect
    (`on_add` - the item leaves its collection) and returns a RemoveFeature that itemByName finds.

    returns_feature=False models an add() that hands back nothing while the mutation lands;
    registers=False models a feature that names itself but does not resolve out of its own
    collection."""
    made, calls = [], []

    def add(item):
        calls.append(item)
        if raises:
            raise RuntimeError("remove failed")
        if on_add is not None:
            on_add()
        if not returns_feature:
            return None
        feat = types.SimpleNamespace(name=feature_name)
        if registers:
            made.append(feat)
        return feat

    return types.SimpleNamespace(add=add, calls=calls,
                                 itemByName=lambda n: next((f for f in made if f.name == n), None))


def _timeline(*items):
    """A timeline of (name, healthState) pairs - what _common.timeline_health walks."""
    return _NamedCollection([FakeTimelineObject(name=n, index=i, health=h)
                             for i, (n, h) in enumerate(items)])


def _parametric(design, design_type=1):
    """RemoveFeatures is parametric-only, so every rig hands the ModeGuard a design whose
    designType reads parametric (1 parametric, 0 direct - current_design_type's int convention)."""
    design.designType = design_type
    return design


def _body_design(body_names=("Body1",), target_name="Body1", removes=True, **kw):
    """A one-component design holding `body_names`; the removeFeatures fake drops `target_name`
    from bRepBodies on add() unless removes=False (the platform reporting success while changing
    nothing)."""
    comp = MakeComp("Root", bodies=list(body_names))
    target = comp.bRepBodies.itemByName(target_name)
    for i in range(comp.bRepBodies.count):
        comp.bRepBodies.item(i).parentComponent = comp
    on_add = (lambda: comp.bRepBodies._items.remove(target)) if removes else None
    comp.features = types.SimpleNamespace(removeFeatures=_remove_features(on_add=on_add, **kw))
    return _parametric(make_design(comp=comp)), comp, target


def _occurrence_design(paths=("Part:1",), removes=True, **kw):
    """A root component holding one occurrence per fullPathName in `paths`; the first is the
    target the removeFeatures fake drops from allOccurrences."""
    # Each occurrence answers `component`, as a real one always does: the shared census classifies an
    # occurrence whose component read RAISES as an unresolved reference and keeps it out of the walk.
    occs = [FakeOccurrence(path=p, component=MakeComp(name=p.split(":")[0])) for p in paths]
    comp = MakeComp("Root", occurrences=occs)
    target = occs[0]
    on_add = (lambda: comp.allOccurrences.remove(target)) if removes else None
    comp.features = types.SimpleNamespace(removeFeatures=_remove_features(on_add=on_add, **kw))
    return _parametric(make_design(comp=comp)), comp, target


@pytest.fixture(autouse=True)
def brep_type(monkeypatch):
    """BodyRef(kind='brep') discriminates by isinstance against adsk.fusion.BRepBody, which is a
    bare Mock in this harness - point it at the shared fake so the kind check is exercised for
    real rather than refusing every body."""
    monkeypatch.setattr(adsk.fusion, "BRepBody", BRepBody)


# ── the body path ───────────────────────────────────────────────────────────────────────────────

class TestRemoveBody:
    @pytest.fixture
    def rig(self):
        design, comp, body = _body_design()
        install(drf, design)
        return types.SimpleNamespace(design=design, comp=comp, body=body,
                                     feats=comp.features.removeFeatures)

    def test_body_is_handed_to_add_and_leaves_the_collection(self, rig):
        out = payload(drf.handler(body="Body1"))
        assert out["removed"] == "Body1"
        assert out["target_kind"] == "body"
        assert out["component"] == "Root"
        assert out["feature"] == "Remove1"
        assert out["feature_read_back"] is True
        # the RESOLVED body object went to add(), not its name
        assert rig.feats.calls == [rig.body]
        assert rig.comp.bRepBodies.itemByName("Body1") is None

    def test_the_feature_is_added_to_the_bodys_own_component(self):
        # a body living in a sub-component must not have its Remove feature added to the root:
        # only the sub-component's collection carries one here, so a root-targeting handler
        # would find no removeFeatures at all.
        root = MakeComp("Root", bodies=[])
        sub = MakeComp("Sub", bodies=["Body1"])
        body = sub.bRepBodies.itemByName("Body1")
        body.parentComponent = sub
        sub.features = types.SimpleNamespace(
            removeFeatures=_remove_features(on_add=lambda: sub.bRepBodies._items.remove(body)))
        design = _parametric(make_design(comp=root, all_components=[root, sub],
                                         tokens={"TOK::sub": body}))
        install(drf, design)
        out = payload(drf.handler(body="TOK::sub"))
        assert out["component"] == "Sub"
        assert sub.features.removeFeatures.calls == [body]

    def test_a_survivor_after_reported_success_is_an_error(self):
        # add() returns a feature but the body never left bRepBodies - the census must catch the lie.
        design, comp, body = _body_design(removes=False)
        install(drf, design)
        msg = error_message(drf.handler(body="Body1"))
        assert "still present" in msg
        assert comp.bRepBodies.itemByName("Body1") is body

    def test_a_raising_add_surfaces_and_removes_nothing(self):
        design, comp, body = _body_design(raises=True)
        install(drf, design)
        assert "removeFeatures.add raised" in error_message(drf.handler(body="Body1"))
        assert comp.bRepBodies.itemByName("Body1") is body

    def test_none_feature_with_the_body_gone_is_success_with_a_warning(self):
        # the platform can hand back no feature object while the mutation lands - the census is the
        # verdict, and the missing name is surfaced rather than silently dropped.
        design, comp, body = _body_design(returns_feature=False)
        install(drf, design)
        out = payload(drf.handler(body="Body1"))
        assert out["feature"] is None
        assert out["feature_read_back"] is False
        assert "returned no feature object" in out["feature_warning"]
        assert comp.bRepBodies.itemByName("Body1") is None

    def test_a_feature_that_does_not_resolve_back_is_flagged_not_claimed(self):
        design, comp, body = _body_design(registers=False)
        install(drf, design)
        out = payload(drf.handler(body="Body1"))
        assert out["feature"] == "Remove1"
        assert out["feature_read_back"] is False
        assert "does not resolve back" in out["feature_warning"]

    def test_an_unreadable_census_after_the_add_is_never_reported_as_success(self):
        design, comp, body = _body_design()
        install(drf, design)

        def poison():
            comp.bRepBodies._items.remove(body)
            type(comp).bRepBodies = property(lambda self: (_ for _ in ()).throw(RuntimeError("gone")))

        comp.features.removeFeatures = _remove_features(on_add=poison)
        try:
            msg = error_message(drf.handler(body="Body1"))
        finally:
            del type(comp).bRepBodies
        assert "may or may not have taken" in msg

    def test_a_target_the_census_cannot_see_is_refused_before_any_mutation(self):
        # the pre-check: if the collection walk cannot find the body it is about to remove, its
        # absence afterwards would prove nothing - so nothing is removed at all.
        design, comp, body = _body_design()
        install(drf, design)
        design._tokens["TOK::held"] = body       # the handle still resolves the held wrapper ...
        comp.bRepBodies._items.remove(body)      # ... while the collection no longer shows it
        msg = error_message(drf.handler(body="TOK::held"))
        assert "could not be verified" in msg
        assert comp.features.removeFeatures.calls == []

    def test_a_same_named_sibling_body_does_not_hide_the_removal(self):
        # two 'Body1' bodies in one component: removing one is a genuine success (one fewer), which
        # a bare "does this name still appear" check would call a failure.
        comp = MakeComp("Root", bodies=[])
        first, second = BRepBody("Body1"), BRepBody("Body1")
        for b in (first, second):
            b.parentComponent = comp
            comp.bRepBodies._items.append(b)
        comp.features = types.SimpleNamespace(
            removeFeatures=_remove_features(on_add=lambda: comp.bRepBodies._items.remove(first)))
        design = _parametric(make_design(comp=comp))
        install(drf, design)
        # the name is ambiguous for the resolver, so target the exact body by its token
        design._tokens["TOK::first"] = first
        out = payload(drf.handler(body="TOK::first"))
        assert out["removed"] == "Body1"
        assert comp.bRepBodies.count == 1 and comp.bRepBodies.item(0) is second

    def test_a_mesh_body_is_refused_by_the_typed_kind(self, monkeypatch):
        # RemoveFeatures.add takes a solid or surface body; the mesh redirect must fire before add().
        mesh_type = type("MeshBody", (), {})
        monkeypatch.setattr(adsk.fusion, "MeshBody", mesh_type)
        mesh = mesh_type()
        mesh.name = "Scan1"
        design, comp, body = _body_design()
        comp.meshBodies = _NamedCollection([mesh])
        install(drf, design)
        msg = error_message(drf.handler(body="Scan1"))
        assert "mesh" in msg.lower()
        assert comp.features.removeFeatures.calls == []


# ── the occurrence path ─────────────────────────────────────────────────────────────────────────

class TestRemoveOccurrence:
    def test_occurrence_is_removed_and_judged_by_full_path(self):
        design, comp, occ = _occurrence_design(paths=("Part:1", "Part:2"))
        install(drf, design)
        out = payload(drf.handler(occurrence="Part:1"))
        assert out["removed"] == "Part:1"
        assert out["target_kind"] == "occurrence"
        assert out["component"] == "Root"
        assert comp.features.removeFeatures.calls == [occ]
        assert [o.fullPathName for o in comp.allOccurrences] == ["Part:2"]

    def test_a_nested_occurrence_is_removed_in_its_parents_component(self):
        # the Remove feature belongs to the component CONTAINING the instance, which for a nested
        # occurrence is its assemblyContext's component - not the root.
        parent_comp = MakeComp("Sub", bodies=[])
        parent_occ = FakeOccurrence(path="Sub:1", component=parent_comp)
        child = FakeOccurrence(path="Sub:1+Bolt:1", component=MakeComp("Bolt"),
                               assembly_context=parent_occ)
        root = MakeComp("Root", occurrences=[parent_occ, child])
        parent_comp.features = types.SimpleNamespace(
            removeFeatures=_remove_features(on_add=lambda: root.allOccurrences.remove(child)))
        design = _parametric(make_design(comp=root, all_components=[root, parent_comp]))
        install(drf, design)
        out = payload(drf.handler(occurrence="Sub:1+Bolt:1"))
        assert out["component"] == "Sub"
        assert parent_comp.features.removeFeatures.calls == [child]

    def test_an_ambiguous_occurrence_name_is_refused_not_guessed(self):
        design, comp, occ = _occurrence_design(paths=("SubA:1+Bolt:1", "SubB:1+Bolt:1"))
        install(drf, design)
        msg = error_message(drf.handler(occurrence="Bolt:1"))
        assert "ambiguous" in msg or "names 2 occurrences" in msg
        assert comp.features.removeFeatures.calls == []

    def test_an_unreadable_occurrence_walk_after_the_add_is_never_reported_as_success(self):
        # BOTH walks raise - allOccurrences and the component.occurrences fallback - so the census
        # cannot answer and the call reports an honest "may or may not have taken" instead of
        # letting the exception escape or claiming a verified removal.
        design, comp, occ = _occurrence_design()

        def poison():
            comp.allOccurrences.remove(occ)
            comp.allOccurrences = _NamedCollection(raises=_UNREADABLE)
            comp.occurrences = _NamedCollection(raises=_UNREADABLE)

        comp.features.removeFeatures = _remove_features(on_add=poison)
        install(drf, design)
        msg = error_message(drf.handler(occurrence="Part:1"))
        assert "may or may not have taken" in msg

    def test_a_raising_allOccurrences_still_verifies_through_the_fallback_walk(self):
        # allOccurrences alone raising must NOT make a real removal unverifiable: the shared census
        # rebuilds from component.occurrences, so the removal is confirmed rather than refused.
        design, comp, occ = _occurrence_design()

        def poison():
            comp.allOccurrences.remove(occ)
            comp.occurrences = _NamedCollection([])   # the fallback collection agrees: it really went
            comp.allOccurrences = _NamedCollection(raises=_UNREADABLE)

        comp.features.removeFeatures = _remove_features(on_add=poison)
        install(drf, design)
        out = payload(drf.handler(occurrence="Part:1"))
        assert out["removed"] == "Part:1"

    def test_a_surviving_occurrence_after_reported_success_is_an_error(self):
        design, comp, occ = _occurrence_design(removes=False)
        install(drf, design)
        assert "still present" in error_message(drf.handler(occurrence="Part:1"))


# ── guards + the timeline diff ──────────────────────────────────────────────────────────────────

class TestGuards:
    def test_both_targets_is_refused_without_mutating(self):
        design, comp, body = _body_design()
        install(drf, design)
        msg = error_message(drf.handler(body="Body1", occurrence="Part:1"))
        assert "EITHER" in msg
        assert comp.features.removeFeatures.calls == []

    def test_no_target_names_both_inputs(self):
        design, comp, body = _body_design()
        install(drf, design)
        msg = error_message(drf.handler())
        assert "'body'" in msg and "'occurrence'" in msg

    def test_no_active_design_is_a_clean_error(self, monkeypatch):
        monkeypatch.setattr(drf._common, "design", lambda: None)
        assert "No active design" in error_message(drf.handler(body="Body1"))

    def test_an_unreadable_target_name_is_refused_before_any_mutation(self):
        # the census keys on the name/fullPathName; without one, "it is gone" would be unprovable -
        # and a census of unnamed bodies would answer for the wrong ones.
        design, comp, body = _body_design()
        design._tokens["TOK::nameless"] = body
        install(drf, design)
        del body.name
        msg = error_message(drf.handler(body="TOK::nameless"))
        assert "nothing was changed" in msg
        assert comp.features.removeFeatures.calls == []

    def test_direct_mode_is_refused_before_any_mutation(self):
        # RemoveFeatures is parametric-only (add() raises "not supported in Direct Modeling"), so
        # the ModeGuard refuses first and names the mode the design is actually in.
        design, comp, body = _body_design()
        design.designType = 0            # direct
        install(drf, design)
        msg = error_message(drf.handler(body="Body1"))
        assert "parametric" in msg and "direct" in msg
        assert "design_set_mode" in msg
        assert comp.features.removeFeatures.calls == []

    def test_an_unreadable_pre_census_names_the_collection_not_a_missing_target(self):
        # a collection that cannot be walked is a different failure from a target that is not in it:
        # calling it "not visible" would name the wrong cause. The handle still resolves the body,
        # so the census - not the resolver - is what declines to answer.
        design, comp, body = _body_design()
        design._tokens["TOK::held"] = body
        install(drf, design)
        type(comp).bRepBodies = property(lambda self: (_ for _ in ()).throw(RuntimeError("gone")))
        try:
            msg = error_message(drf.handler(body="TOK::held"))
        finally:
            del type(comp).bRepBodies
        assert "could not be read" in msg
        assert "not visible" not in msg
        assert comp.features.removeFeatures.calls == []

    def test_a_component_without_remove_features_is_reported(self):
        design, comp, body = _body_design()
        comp.features = types.SimpleNamespace(removeFeatures=None)
        install(drf, design)
        assert "no removeFeatures collection" in error_message(drf.handler(body="Body1"))

    def test_only_the_newly_errored_feature_is_named_in_the_timeline_warning(self):
        # 'Sweep1' was already broken before the call - blaming the remove for it would misattribute
        # a pre-existing error, so only the feature that errored BETWEEN the two reads is named.
        design, comp, body = _body_design()
        design.timeline = _timeline(("Sweep1", 2), ("Extrude1", 0))

        def break_downstream():
            comp.bRepBodies._items.remove(body)
            design.timeline = _timeline(("Sweep1", 2), ("Extrude1", 0), ("Fillet1", 2))

        comp.features.removeFeatures = _remove_features(on_add=break_downstream)
        install(drf, design)
        out = payload(drf.handler(body="Body1"))
        assert out["removed"] == "Body1"
        assert "Fillet1" in out["timeline_warning"]
        assert "Sweep1" not in out["timeline_warning"]

    def test_a_pre_existing_timeline_warning_is_reported_not_escalated(self):
        design, comp, body = _body_design()
        design.timeline = _timeline(("Extrude1", 1))
        install(drf, design)
        out = payload(drf.handler(body="Body1"))
        assert out["timeline_warnings"] == ["Extrude1"]
        assert "timeline_warning" not in out
