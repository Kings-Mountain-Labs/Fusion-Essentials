"""Unit tests for ``design_delete_feature.py`` - delete one timeline feature by name.

The logic pinned here, no live Fusion: name matching through the shared timeline resolver (EXACT,
case- and surrounding-whitespace-insensitive, never a substring; a repeated name REFUSED with the
'name@index' candidates), the GROUP guard (a timeline group has no deletable entity), the no-entity guard,
the actual ``entity.deleteMe()`` call (captured so a wrong method name regresses here), the
deleteMe-returns-false path, the before/after timeline-health guard (a delete that breaks a
downstream feature is reported, the deletion still standing), and the occurrence-remove reroute (a
Remove feature's timeline entity is the removed OCCURRENCE, so the delete goes to the RemoveFeature
resolved by the same name).
"""

import adsk.fusion
import pytest

import live_api_facts as _api_facts
from conftest import (FakeFeature, FakeFeatures, FakeOccurrence, FakeTimeline, FakeTimelineObject,
                      MakeComp, _NamedCollection, error_message, install, load_tool, make_design,
                      payload)

df = load_tool("design_delete_feature")

_HEALTHY = _api_facts.ENUMS["fusion.FeatureHealthStates"]["HealthyFeatureHealthState"]
_WARNING = _api_facts.ENUMS["fusion.FeatureHealthStates"]["WarningFeatureHealthState"]
_ERROR = _api_facts.ENUMS["fusion.FeatureHealthStates"]["ErrorFeatureHealthState"]


# ── fakes ────────────────────────────────────────────────────────────────────

def _entity(type_name="ExtrudeFeature", delete_returns=True, breaks=None):
    """A feature entity whose CLASS NAME is `type_name`, so the handler's type(entity).__name__
    reports the right entity_type. `breaks` is a timeline the delete injects an error into."""
    def deleteMe(self):
        self._deletes += 1
        if self._breaks is not None:
            self._breaks._items.append(_tl("BrokenChild", 99, health=_ERROR))
        return self._delete_ok

    inst = type(type_name, (FakeFeature,), {"deleteMe": deleteMe})(
        name=type_name, delete_ok=delete_returns)
    inst._breaks = breaks
    return inst


def _tl(name, index, is_group=False, entity="auto", health=_HEALTHY,
        entity_type="ExtrudeFeature", delete_returns=True):
    """One timeline object; `entity` defaults to a fresh feature of class `entity_type`."""
    if is_group:
        ent = None
    elif entity == "auto":
        ent = _entity(entity_type, delete_returns)
    else:
        ent = entity
    return FakeTimelineObject(name=name, index=index, is_group=is_group, entity=ent, health=health)


def _detaches(tl, obj):
    """Wrap a timeline object's entity so a successful deleteMe takes that OBJECT out of the
    timeline - what the live API does, and the absence the handler re-reads the name census for."""
    ent = getattr(obj, "entity", None)
    inner = getattr(ent, "deleteMe", None)
    if inner is None:
        return

    def wrapped():
        did = inner()
        if did and obj in tl._items:
            tl._items.remove(obj)
        return did

    ent.deleteMe = wrapped


def _timeline(items):
    """A timeline whose entities detach their own object on a successful delete."""
    tl = FakeTimeline(list(items))
    for obj in tl._items:
        _detaches(tl, obj)
    return tl


def _install(items, has_timeline=True):
    tl = _timeline(items) if has_timeline else None
    install(df, make_design(timeline=tl))
    return tl


# ── helpers ──────────────────────────────────────────────────────────────────

class TestHealthHelper:
    def test_rolls_up_errors_and_warnings(self):
        tl = _timeline([_tl("A", 0, health=_HEALTHY), _tl("B", 1, health=_ERROR),
                        _tl("C", 2, health=_WARNING)])
        errors, warnings, total = df._timeline_health(make_design(timeline=tl))
        assert total == 3 and errors == ["B"] and warnings == ["C"]

    def test_none_timeline_empty(self):
        assert df._timeline_health(make_design()) == ([], [], 0)


class TestFindByName:
    def test_a_longer_name_sharing_the_prefix_is_not_a_match(self):
        tl = _timeline([_tl("Fillet1", 0), _tl("Fillet10", 1)])
        obj, err = df._find_object(tl, "Fillet1")
        assert err is None and obj.name == "Fillet1"      # exact only, not Fillet10

    def test_a_substring_resolves_nothing_on_a_destructive_tool(self):
        # 'Fillet' names no timeline object. Deleting Fillet12 because it CONTAINS the typed text
        # is a silent wrong-target delete - the shared resolver refuses and lists what is there.
        tl = _timeline([_tl("Fillet12", 0)])
        obj, err = df._find_object(tl, "Fillet")
        assert obj is None
        assert "no timeline feature named 'Fillet'" in err and "Fillet12" in err

    def test_surrounding_whitespace_is_not_a_distinguishing_feature(self):
        # Fusion names an occurrence-create timeline object with a LEADING SPACE (measured); no
        # caller retypes that, and no listing shows it.
        tl = _timeline([_tl(" InsProbe:1", 0)])
        obj, err = df._find_object(tl, "InsProbe:1")
        assert err is None and obj.index == 0


class TestAtIndexForm:
    """'name@index' - the disambiguation target the ambiguity error advertises (e.g. 'Extrude1@9') -
    resolves to the object whose OWN .index is that number, confirmed by name."""

    def test_at_index_targets_that_timeline_index(self):
        tl = _timeline([_tl("Extrude1", 0), _tl("Sketch1", 1), _tl("Extrude1", 2)])
        obj, err = df._find_object(tl, "Extrude1@2")
        assert err is None and obj.index == 2             # the SECOND Extrude1, not the first

    def test_at_index_out_of_range_refused(self):
        tl = _timeline([_tl("Extrude1", 0)])
        obj, err = df._find_object(tl, "Extrude1@5")
        assert obj is None and "Extrude1@5" in err

    def test_at_index_name_mismatch_refused(self):
        # index 0 is Sketch1, not Extrude1 - a stale pairing is refused, never widened to a name match
        tl = _timeline([_tl("Sketch1", 0), _tl("Extrude1", 1)])
        obj, err = df._find_object(tl, "Extrude1@0")
        assert obj is None and "Extrude1@0" in err

    def test_at_index_reads_the_objects_own_index_not_its_position(self):
        # A timeline whose .index does NOT equal list position - what design_get publishes and what
        # the ambiguity error prints is o.index, so '@4' must mean the object carrying index 4.
        tl = _timeline([_tl("Joint1", 4), _tl("Joint1", 7)])
        obj, err = df._find_object(tl, "Joint1@7")
        assert err is None and obj.index == 7
        # position 1, but no object holds .index 1
        assert df._find_object(tl, "Joint1@1")[0] is None

    def test_the_candidates_the_refusal_prints_resolve_back(self):
        # the ambiguity error advertises 'name@index' pairs; every one it prints must be a string
        # this same tool can resolve, or the refusal names a target the user cannot act on
        _install([_tl("Joint1", 4), _tl("Joint1", 7)])
        msg = error_message(df.handler(feature="Joint1"))
        for cand in ("Joint1@4", "Joint1@7"):
            assert cand in msg
            tl = _install([_tl("Joint1", 4), _tl("Joint1", 7)])
            obj, err = df._find_object(tl, cand)
            assert err is None and f"Joint1@{obj.index}" == cand

    def test_resolves_through_the_shared_matcher_not_a_local_copy(self):
        # design_delete_feature, design_edit_timeline and _inputs.FeatureRef answer the SAME wire
        # forms; a local re-roll is how one tool targets a different feature than the others.
        inputs = load_tool("_inputs")
        objs = [_tl("Extrude1", 4), _tl("Extrude1", 9)]
        tl = _timeline(objs)
        for want in ("Extrude1@4", "Extrude1@9", "Extrude1@0", "Extrude1@1", "Extrude1", "Extru"):
            theirs = inputs._match_timeline_objects(objs, want)
            obj, err = df._find_object(tl, want)
            if len(theirs) == 1:
                assert err is None and obj is theirs[0], want
            else:
                assert obj is None, want                  # 0 or >1 hits is always a refusal

    def test_handler_deletes_the_indexed_duplicate(self):
        # end-to-end: two features share a name; the @index form deletes exactly the RIGHT one
        a = _tl("Extrude1", 0, entity_type="ExtrudeFeature")
        s = _tl("Sketch1", 1)
        b = _tl("Extrude1", 2, entity_type="ExtrudeFeature")
        _install([a, s, b])
        out = payload(df.handler(feature="Extrude1@2"))
        assert out["deleted"] is True and out["index"] == 2
        assert b.entity._deletes == 1 and a.entity._deletes == 0


# ── happy path ───────────────────────────────────────────────────────────────

class TestDelete:
    def test_deletes_named_feature(self):
        obj = _tl("Rectangular Pattern1", 5, entity_type="RectangularPatternFeature")
        tl = _install([obj])
        out = payload(df.handler(feature="Rectangular Pattern1"))
        assert out["deleted"] is True
        assert out["feature"] == "Rectangular Pattern1"
        assert out["index"] == 5
        assert out["entity_type"] == "RectangularPatternFeature"
        assert obj.entity._deletes == 1                    # deleteMe actually called
        assert tl._items == []                             # and the object left the timeline

    def test_a_name_matches_case_insensitively(self):
        obj = _tl("Mirror1", 3, entity_type="MirrorFeature")
        _install([obj])
        out = payload(df.handler(feature="mirror1"))
        assert out["feature"] == "Mirror1"
        assert obj.entity._deletes == 1

    def test_a_substring_deletes_nothing(self):
        # the corrected contract: 'mirror' is not 'Mirror1'. A destructive tool never widens a name
        # it was given - the refusal lists what IS there and 'name@index' targets one of them.
        tl = _install([_tl("Mirror1", 3, entity_type="MirrorFeature")])
        assert "Mirror1" in error_message(df.handler(feature="mirror"))
        assert tl._items[0].entity._deletes == 0


# ── absence is proved, never assumed ─────────────────────────────────────────

class TestAbsenceReRead:
    """deleteMe()'s bool is the platform's claim; the name census re-read is the proof. A survivor
    is an error, and a census that could not read is disclosed, never counted as absence."""

    def test_a_survivor_is_an_error_not_a_false_ok(self):
        # deleteMe reports success and the object is still in the timeline: the payload may not
        # publish deleted:true off the bool alone.
        obj = _tl("Extrude1", 0)
        _install([obj])
        obj.entity.deleteMe = lambda: True          # reports success, removes nothing
        msg = error_message(df.handler(feature="Extrude1"))
        assert "NOT removed" in msg
        assert "Extrude1" in msg

    def test_one_of_two_same_named_leaving_is_proof_enough(self):
        # the census counts the NAME, so the boundary is after < before, not after == 0: deleting
        # 'Extrude1@2' leaves the other Extrude1 standing and that is still a proven removal.
        tl = _install([_tl("Extrude1", 0), _tl("Extrude1", 2)])
        out = payload(df.handler(feature="Extrude1@2"))
        assert out["deleted"] is True
        assert [o.index for o in tl._items] == [0]

    def test_a_census_that_stops_reading_is_unverified_not_absence(self):
        # the timeline will not report a size after the delete. The object IS gone, but the check
        # cannot show it - so the flag is null and the note says why, never deleted:true.
        obj = _tl("Extrude1", 0)
        tl = _install([obj])
        inner = obj.entity.deleteMe

        def blinding():
            did = inner()
            tl._raises = "timeline unavailable"      # .count now raises: an unreadable collection
            return did

        obj.entity.deleteMe = blinding
        out = payload(df.handler(feature="Extrude1"))
        assert out["deleted"] is None
        assert "UNVERIFIED" in out["note"] and "could not be read back" in out["note"]
        # the note may not carry the claim the null flag denies
        assert "Timeline feature deleted" not in out["note"]


# ── guards ───────────────────────────────────────────────────────────────────

class TestGuards:
    def test_empty_feature_errors(self):
        _install([_tl("X", 0)])
        assert "provide 'feature'" in error_message(df.handler(feature="")).lower()

    def test_no_active_design_errors(self):
        install(df, None)
        assert "no active design" in error_message(df.handler(feature="X")).lower()

    def test_direct_design_no_timeline_errors(self):
        _install([], has_timeline=False)
        assert "no timeline" in error_message(df.handler(feature="X")).lower()

    def test_missing_feature_errors(self):
        _install([_tl("Extrude1", 0)])
        msg = error_message(df.handler(feature="Ghost"))
        assert "no timeline feature named" in msg.lower()
        assert "Extrude1" in msg                            # what IS there

    def test_a_repeated_name_is_refused_with_the_indexed_candidates(self):
        # two timeline objects carry the name - refuse, listing the 'name@index' form that picks one
        _install([_tl("Joint1", 4), _tl("Joint1", 7)])
        msg = error_message(df.handler(feature="Joint1"))
        assert "matches 2 timeline objects" in msg
        assert "Joint1@4" in msg and "Joint1@7" in msg

    def test_group_refused(self):
        _install([_tl("Group1", 2, is_group=True)])
        assert "group" in error_message(df.handler(feature="Group1")).lower()

    def test_delete_me_false_reported(self):
        _install([_tl("Stubborn1", 1, delete_returns=False)])
        assert "declined" in error_message(df.handler(feature="Stubborn1")).lower()

    def test_no_entity_guard(self):
        # a non-group object with no associated entity is refused (nothing to delete)
        _install([_tl("Weird1", 3, is_group=False, entity=None)])
        assert "no associated entity" in error_message(df.handler(feature="Weird1")).lower()

    def test_preexisting_warnings_surface_without_new_error(self):
        # deleting succeeds; the timeline already carries a WARNING -> reported under
        # timeline_warnings (the elif branch), distinct from a NEW error.
        _install([_tl("Extrude1", 0, health=_HEALTHY), _tl("WarnFeature", 1, health=_WARNING)])
        out = payload(df.handler(feature="Extrude1"))
        assert out["deleted"] is True
        assert "timeline_warning" not in out          # no NEW error
        assert out["timeline_warnings"] == ["WarnFeature"]

    def test_a_body_remove_deletes_its_timeline_entity_directly(self, monkeypatch):
        # a body-remove timeline object's entity IS the RemoveFeature: no re-resolution, and the
        # entity the timeline handed over is what gets deleted.
        monkeypatch.setattr(adsk.fusion, "Occurrence", FakeOccurrence)
        ent = _entity("RemoveFeature")
        install(df, make_design(timeline=_timeline([_tl("RemoveBody-Body1", 2, entity=ent)])))
        out = payload(df.handler(feature="RemoveBody-Body1"))
        assert out["deleted"] is True and out["entity_type"] == "RemoveFeature"
        assert ent._deletes == 1

    def test_downstream_error_after_delete_reported(self):
        # a new timeline error after the delete is surfaced, naming the feature that carries it.
        ent = _entity("ExtrudeFeature", delete_returns=True)
        tl = _install([_tl("Extrude1", 0, entity=ent, health=_HEALTHY)])
        ent._breaks = tl                    # deleting injects a downstream error into THIS timeline
        out = payload(df.handler(feature="Extrude1"))
        assert out["deleted"] is True
        assert "timeline_warning" in out
        assert "BrokenChild" in out["timeline_warning"]

    def test_the_downstream_warning_states_only_what_was_read(self):
        # Two claims this sentence may not make: WHY the error appeared (nothing here reads what the
        # broken feature consumed) and that "the deletion stands" - 'deleted' is the absence verdict
        # and it can be null on the very same call.
        ent = _entity("ExtrudeFeature", delete_returns=True)
        ent._breaks = _install([_tl("Extrude1", 0, entity=ent, health=_HEALTHY)])
        warning = payload(df.handler(feature="Extrude1"))["timeline_warning"]
        assert "the deletion stands" not in warning
        assert "consumed the removed geometry" not in warning
        assert "Nothing was rolled back" in warning
        assert "'deleted'" in warning                 # where the caller reads the verified fact


# ── the occurrence-remove reroute ────────────────────────────────────────────
# An Occurrence .entity does not say which kind of timeline object reported it: an occurrence-remove
# object reports the REMOVED occurrence (deleteMe raises InternalValidationError), an occurrence
# CREATE reports the LIVE instance (deleteMe succeeds). The name lookup in removeFeatures is the
# discriminator. The RemoveFeature class name is load-bearing - the payload's entity_type reports it.

class _RemovedOccurrence(FakeOccurrence):
    """The entity an occurrence-REMOVE timeline object reports. deleteMe() raises the way Fusion's
    does on a removed occurrence, so a handler that deletes the entity directly cannot pass."""

    def __init__(self, path="Scrap:1"):
        super().__init__(path=path)

    def deleteMe(self):
        raise RuntimeError("2 : InternalValidationError : Xl::Utils::findObjectPath(this, objPath)")


class _LiveOccurrence(FakeOccurrence):
    """The entity an occurrence-CREATE timeline object reports: the LIVE instance, whose deleteMe()
    succeeds and removes it. Same type as the removed one, so only the name lookup tells them
    apart."""

    def __init__(self, path="InsProbe:1"):
        super().__init__(path=path)
        self.deleted = False

    def deleteMe(self):
        self.deleted = True
        return True


_TL_INDEX = 3      # the timeline index _design() places its object at


class RemoveFeature(FakeFeature):
    """timelineObject.index is the feature's own place in the timeline - the reroute accepts the
    feature only when that index is the one the caller resolved, so a same-named feature elsewhere
    in the timeline cannot stand in for the object named."""

    def __init__(self, name, delete_returns=True, restores_to=None, restored_path="Scrap:1",
                 timeline_index=_TL_INDEX):
        super().__init__(name=name, delete_ok=delete_returns,
                         timeline_object=FakeTimelineObject(name=name, index=timeline_index))
        self._restores_to = restores_to      # the allOccurrences list the occurrence comes back into
        self._restored_path = restored_path

    def deleteMe(self):
        did = super().deleteMe()
        if did and self._restores_to is not None:
            # component: a real Occurrence always answers it; the shared census reads it to tell an
            # ordinary occurrence from one whose external reference will not resolve.
            self._restores_to.append(FakeOccurrence(
                path=self._restored_path,
                component=MakeComp(name=self._restored_path.split(":")[0])))
        return did


def _remove_feature_detaches(tl, feat):
    """Deleting a RemoveFeature takes its OWN timeline object - the one carrying its index - out of
    the timeline. The reroute deletes the FEATURE, not the timeline object's .entity, so this is the
    absence path the entity wrapper never sees."""
    inner = feat.deleteMe

    def wrapped():
        did = inner()
        if did:
            for obj in list(tl._items):
                if obj.index == feat.timelineObject.index:
                    tl._items.remove(obj)
        return did

    feat.deleteMe = wrapped


def _removes(*feats):
    """A component's features, whose removeFeatures itemByName is the lookup the reroute resolves
    on."""
    features = FakeFeatures()
    features.removeFeatures = _NamedCollection(list(feats))
    return features


class TestOccurrenceRemoveReroute:
    @pytest.fixture(autouse=True)
    def occurrence_type(self, monkeypatch):
        """The handler discriminates the removed occurrence by isinstance against
        adsk.fusion.Occurrence, a bare Mock in this harness (isinstance against which raises, so the
        branch would silently never fire). Point it at the shared occurrence fake."""
        monkeypatch.setattr(adsk.fusion, "Occurrence", FakeOccurrence)

    def _design(self, name, comps, entity=None):
        design = make_design(comp=comps[0], all_components=comps)
        design.timeline = _timeline([_tl(name, _TL_INDEX,
                                         entity=_RemovedOccurrence() if entity is None else entity)])
        for c in comps:
            removes = getattr(getattr(c, "features", None), "removeFeatures", None)
            feat = removes.itemByName(name) if removes is not None else None
            if feat is not None:
                _remove_feature_detaches(design.timeline, feat)
        return install(df, design)

    def test_the_delete_is_routed_to_the_remove_feature(self):
        feat = RemoveFeature("RemoveInstance-Scrap:1")
        comp = MakeComp("Root")
        comp.features = _removes(feat)
        self._design("RemoveInstance-Scrap:1", [comp])
        out = payload(df.handler(feature="RemoveInstance-Scrap:1"))
        assert out["deleted"] is True
        assert out["entity_type"] == "RemoveFeature"   # what was deleted, not what .entity handed over
        assert feat._deletes == 1

    def test_the_note_says_the_occurrence_came_back(self):
        # deleting a Remove feature RESTORES what it removed - the generic note says the opposite
        # ("instances it created go with it") and would contradict design_remove_feature's promise.
        feat = RemoveFeature("RemoveInstance-Scrap:1")
        comp = MakeComp("Root")
        comp.features = _removes(feat)
        self._design("RemoveInstance-Scrap:1", [comp])
        out = payload(df.handler(feature="RemoveInstance-Scrap:1"))
        assert "back in the assembly" in out["note"]
        assert "go with it" not in out["note"]

    def test_the_restored_occurrence_is_read_back(self):
        comp = MakeComp("Root")
        feat = RemoveFeature("RemoveInstance-Scrap:1", restores_to=comp.allOccurrences,
                             restored_path="Scrap:1")
        comp.features = _removes(feat)
        self._design("RemoveInstance-Scrap:1", [comp])
        out = payload(df.handler(feature="RemoveInstance-Scrap:1"))
        assert out["occurrence_restored"] == "Scrap:1"

    def test_an_unconfirmed_restore_is_omitted_not_denied(self):
        # the walk does not show the occurrence back: the delete still stands, and the key is simply
        # absent - "not confirmed" is never published as "it did not come back".
        feat = RemoveFeature("RemoveInstance-Scrap:1")      # restores_to=None: nothing comes back
        comp = MakeComp("Root")
        comp.features = _removes(feat)
        self._design("RemoveInstance-Scrap:1", [comp])
        out = payload(df.handler(feature="RemoveInstance-Scrap:1"))
        assert out["deleted"] is True
        assert "occurrence_restored" not in out

    def test_the_feature_is_found_in_a_sub_component(self):
        # the RemoveFeature lives in the component that owns the instance, not necessarily the root.
        root, sub = MakeComp("Root"), MakeComp("Sub")
        feat = RemoveFeature("RemoveInstance-Bolt:1")
        root.features = _removes()
        sub.features = _removes(feat)
        self._design("RemoveInstance-Bolt:1", [root, sub])
        out = payload(df.handler(feature="RemoveInstance-Bolt:1"))
        assert out["deleted"] is True and feat._deletes == 1

    def test_a_live_occurrence_with_no_remove_feature_is_deleted_not_refused(self):
        # an occurrence-CREATE timeline object also reports an Occurrence entity, and deleting it
        # through the timeline works - so no RemoveFeature by that name means delete the entity as
        # handed over, never refuse. The create object's name carries a leading space on this build.
        occ = _LiveOccurrence(" InsProbe:1")
        comp = MakeComp("Root")
        comp.features = _removes()
        self._design(" InsProbe:1", [comp], entity=occ)
        out = payload(df.handler(feature=" InsProbe:1"))
        assert out["deleted"] is True
        assert occ.deleted is True
        assert "back in the assembly" not in out["note"]   # not a Remove feature - the generic note

    def test_a_removed_occurrence_with_no_feature_reports_the_platform_error(self):
        # nothing resolves by the name, so the entity is deleted as handed over and Fusion's own
        # refusal is what the agent sees - never a diagnosis this tool never checked.
        comp = MakeComp("Root")
        comp.features = _removes()
        self._design("RemoveInstance-Ghost:1", [comp])
        msg = error_message(df.handler(feature="RemoveInstance-Ghost:1"))
        assert "InternalValidationError" in msg
        assert "is a Remove feature" not in msg

    def test_the_name_at_index_form_deletes_the_object_it_names(self):
        # two timeline objects share the name "Bolt:1": an occurrence CREATE at index 1 and a
        # renamed RemoveFeature at index 0. 'Bolt:1@1' names the CREATE, and the name lookup finds
        # the RemoveFeature - so only the index identity keeps the delete on the object named.
        occ = _LiveOccurrence("Bolt:1")
        feat = RemoveFeature("Bolt:1", timeline_index=0)
        comp = MakeComp("Root")
        comp.features = _removes(feat)
        design = make_design(comp=comp, all_components=[comp])
        design.timeline = _timeline([_tl("Bolt:1", 0, entity=_RemovedOccurrence("Bolt:1")),
                                     _tl("Bolt:1", 1, entity=occ)])
        install(df, design)
        out = payload(df.handler(feature="Bolt:1@1"))
        assert out["deleted"] is True
        assert occ.deleted is True                      # the object the caller named
        assert feat._deletes == 0                       # the same-named RemoveFeature is untouched
        assert "back in the assembly" not in out["note"]

    def test_the_same_name_in_two_components_is_refused_not_guessed(self):
        a, b = MakeComp("CompA"), MakeComp("CompB")
        fa, fb = RemoveFeature("RemoveInstance-Bolt:1"), RemoveFeature("RemoveInstance-Bolt:1")
        a.features, b.features = _removes(fa), _removes(fb)
        self._design("RemoveInstance-Bolt:1", [a, b])
        msg = error_message(df.handler(feature="RemoveInstance-Bolt:1"))
        assert "CompA" in msg and "CompB" in msg
        assert fa._deletes == 0 and fb._deletes == 0

    def test_a_declining_remove_feature_is_reported_not_claimed(self):
        feat = RemoveFeature("RemoveInstance-Scrap:1", delete_returns=False)
        comp = MakeComp("Root")
        comp.features = _removes(feat)
        self._design("RemoveInstance-Scrap:1", [comp])
        assert "declined" in error_message(df.handler(feature="RemoveInstance-Scrap:1")).lower()
