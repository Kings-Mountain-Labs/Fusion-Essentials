"""Unit tests for ``model_mirror`` - mirror solid bodies OR timeline features across a plane.

Pinned: the one-target-or-the-other guards, the exact-match/refuse-ambiguity feature resolver, the
collection's add() refusal, the join read-back, and the body/volume census - counted over BOTH the
source's component and the build component - that decides whether the mirror did anything.
"""

import re
import types

import pytest

import adsk.core

from conftest import (BRepBody, MakeComp, body_proxy, error_message, install, load_tool,
                      make_design, make_source_document, payload as _payload,
                      _make_object_collection, _NamedCollection)

mr = load_tool("model_mirror")

SOURCE_VOLUME = 10.0          # every source body in these scenes


def mirror_input(entities, plane):
    """MirrorFeatureInput: the entities, the plane, and the isCombine flag a body mirror sets."""
    return types.SimpleNamespace(entities=entities, plane=plane, isCombine=False)


def mirror_feature(name="Mirror1", bodies=(), is_combine=False):
    """MirrorFeature. `bodies` is the result walk the after-volume is read from; `resultFeatures`
    exposes a count that reads None even for a mirror that minted geometry, which is why the census
    is the effect gate."""
    return types.SimpleNamespace(name=name, bodies=_NamedCollection(list(bodies)),
                                 resultFeatures=types.SimpleNamespace(count=None),
                                 isCombine=is_combine, healthState=0)


def feature_entity(name="Extrude1", bodies=()):
    """A timeline feature's `.entity` - the object the mirror collection is handed."""
    return types.SimpleNamespace(name=name, parentComponent=None,
                                 bodies=_NamedCollection(list(bodies)))


def timeline_object(name, index, entity=None, is_group=False):
    return types.SimpleNamespace(name=name, index=index, entity=entity, isGroup=is_group)


def timeline(*objs):
    """The timeline, in index order. Live, ``timeline.item(i).index == i`` - the invariant the
    'name@index' disambiguation form is addressed through - so a fake that reports a different index
    than it sits at is rejected here rather than silently pinning an unreachable candidate."""
    for i, obj in enumerate(objs):
        assert obj.index == i, f"timeline fake: object {obj.name} reports index {obj.index} at {i}"
    return _NamedCollection(objs)


class _MirrorFeatures:
    """comp.features.mirrorFeatures. add() spawns `scene.spawn` bodies into the component's census -
    the two signals a real mirror moves are that census and the volume read off the feature."""

    def __init__(self, scene):
        self.scene = scene
        self.last = None

    def createInput(self, entities, plane):
        self.last = self.scene.input_factory(entities, plane)
        return self.last

    def add(self, inp):
        for i in range(self.scene.spawn):
            self.scene.comp.bRepBodies._items.append(BRepBody(f"Mirror{i + 1}", volume=SOURCE_VOLUME))
        return self.scene.feature


class Scene:
    """The wired-up design plus the knobs a test turns: how many bodies the mirror spawns, what the
    collection refuses, and what add() hands back."""

    def __init__(self, comp, design):
        self.comp = comp
        self.design = design
        self.spawn = 1
        self.refuse = []
        # the mirrored copy: a body of its own, the same volume as the source it reflects
        self.feature = mirror_feature(bodies=[BRepBody("Body2", volume=SOURCE_VOLUME)])
        self.input_factory = mirror_input
        self.mf = _MirrorFeatures(self)

    def body(self, name):
        return self.comp.bRepBodies.itemByName(name)


class _Unreadable(_NamedCollection):
    """A body collection that cannot be counted - the census-blind case."""

    @property
    def count(self):
        raise RuntimeError("3 : collection is gone")

    def item(self, i):
        raise RuntimeError("3 : collection is gone")


@pytest.fixture
def scene(monkeypatch):
    """Build a mirror scene: a component with `bodies`, an optional timeline, and the shared
    ObjectCollection fake carrying this scene's refusals."""
    def build(bodies=("A",), tl=None, body_owner=None, extra_components=()):
        # Every component answers an entityToken and the effect census de-dupes its hosts on it
        # (_common.same_component); a host it cannot identify makes the census unjudgeable, which is
        # its own tested state - see test_an_unidentifiable_host_makes_the_count_unjudgeable.
        comp = MakeComp(name="Comp", entity_token="TOKEN:Comp",
                        bodies=[BRepBody(n, volume=SOURCE_VOLUME,
                                                      parent_component=body_owner) for n in bodies],
                        origin_planes=(("plane", "xy"), ("plane", "xz"), ("plane", "yz")))
        design = make_design(comp=comp, all_components=[comp, *extra_components])
        if tl is not None:
            design.timeline = tl
        sc = Scene(comp, design)
        comp.features = type("F", (), {"mirrorFeatures": sc.mf})()
        install(mr, design)
        monkeypatch.setattr(adsk.core.ObjectCollection, "create",
                            staticmethod(lambda: _make_object_collection(sc.refuse)))
        return sc
    return build


class TestTargetGuards:
    def test_both_bodies_and_features_is_refused_naming_both(self, scene):
        sc = scene(bodies=("A",), tl=timeline(timeline_object("Extrude1", 0, feature_entity())))
        msg = error_message(mr.handler(bodies=["A"], features=["Extrude1"], plane="yz"))
        # precedence would silently mirror one of them - the refusal must name what it saw
        assert "not both" in msg and "A" in msg and "Extrude1" in msg
        assert sc.mf.last is None

    def test_neither_bodies_nor_features_is_refused(self, scene):
        scene()
        msg = error_message(mr.handler(plane="yz"))
        assert "Nothing to mirror" in msg and "features" in msg

    def test_empty_lists_count_as_neither(self, scene):
        scene()
        msg = error_message(mr.handler(bodies=[], features=[], plane="yz"))
        assert "Nothing to mirror" in msg

    def test_bad_plane(self, scene):
        scene()
        msg = error_message(mr.handler(bodies=["A"], plane="qq"))
        # PlaneRef owns the error: 'qq' is not an origin alias / construction name / handle
        assert "not an origin alias" in msg

    def test_body_not_found(self, scene):
        scene(bodies=("A",))
        msg = error_message(mr.handler(bodies=["A", "X"], plane="yz"))
        assert "no body or component named 'X'" in msg


class TestFeatureResolution:
    def test_mirrors_a_feature_by_exact_name(self, scene):
        ent = feature_entity("Extrude1")
        sc = scene(bodies=("A",), tl=timeline(timeline_object("Extrude1", 0, ent)))
        out = _payload(mr.handler(features=["Extrude1"], plane="yz"))
        assert out["mode"] == "features" and out["source_features"] == ["Extrude1"]
        assert sc.mf.last.entities.item(0) is ent
        assert sc.mf.last.plane == ("plane", "yz")

    def test_the_label_is_the_timeline_name_not_the_entity_name(self, scene):
        # the timeline object's name is what resolved; a name re-read off the feature is a
        # different, unverified value and must never be what the payload publishes
        ent = feature_entity("SomeOtherName")
        scene(tl=timeline(timeline_object("Extrude1", 0, ent)))
        out = _payload(mr.handler(features=["extrude1"], plane="yz"))
        assert out["source_features"] == ["Extrude1"]

    def test_partial_name_does_not_match(self, scene):
        # the substring fallback a delete tool affords is NOT available here: 'Extrude' must not
        # silently resolve to 'Extrude1'
        scene(tl=timeline(timeline_object("Extrude1", 0, feature_entity("Extrude1"))))
        msg = error_message(mr.handler(features=["Extrude"], plane="yz"))
        assert "no timeline feature named 'Extrude'" in msg and "Extrude1" in msg

    def test_duplicate_name_is_refused_with_the_candidates(self, scene):
        scene(tl=timeline(timeline_object("Sketch1", 0, feature_entity("Sketch1")),
                          timeline_object("Extrude1", 1, feature_entity("Extrude1")),
                          timeline_object("Fillet1", 2, feature_entity("Fillet1")),
                          timeline_object("Fillet1", 3, feature_entity("Fillet1"))))
        msg = error_message(mr.handler(features=["Fillet1"], plane="yz"))
        assert "matches 2 timeline objects" in msg and "Fillet1@2" in msg and "Fillet1@3" in msg

    def test_a_printed_candidate_resolves_to_its_own_object(self, scene):
        third, fourth = feature_entity("Fillet1"), feature_entity("Fillet1")
        sc = scene(tl=timeline(timeline_object("Sketch1", 0, feature_entity("Sketch1")),
                               timeline_object("Extrude1", 1, feature_entity("Extrude1")),
                               timeline_object("Fillet1", 2, third),
                               timeline_object("Fillet1", 3, fourth)))
        msg = error_message(mr.handler(features=["Fillet1"], plane="yz"))
        # the candidates the refusal prints must be usable verbatim - the LAST one proves the form
        # addresses a specific object rather than falling back to the first hit
        candidates = re.findall(r"Fillet1@\d+", msg)
        assert candidates == ["Fillet1@2", "Fillet1@3"]
        _payload(mr.handler(features=[candidates[-1]], plane="yz"))
        assert sc.mf.last.entities.item(0) is fourth

    def test_name_at_index_with_a_wrong_name_is_refused(self, scene):
        scene(tl=timeline(timeline_object("Fillet1", 0, feature_entity("Fillet1"))))
        msg = error_message(mr.handler(features=["Extrude1@0"], plane="yz"))
        assert "no timeline feature named 'Extrude1@0'" in msg

    def test_timeline_group_is_refused(self, scene):
        scene(tl=timeline(timeline_object("Group1", 0, None, is_group=True)))
        msg = error_message(mr.handler(features=["Group1"], plane="yz"))
        assert "timeline GROUP" in msg

    def test_the_same_feature_twice_is_refused_as_a_duplicate(self, scene):
        # the duplicate check runs BEFORE the group/entity checks, so naming one object twice is
        # refused as the duplicate it is
        scene(tl=timeline(timeline_object("Extrude1", 0, feature_entity("Extrude1")),
                          timeline_object("Fillet1", 1, feature_entity("Fillet1"))))
        msg = error_message(mr.handler(features=["Extrude1", "Extrude1@0"], plane="yz"))
        assert "already in this call" in msg

    def test_no_timeline_refuses_feature_mode(self, scene):
        sc = scene(bodies=("A",))
        assert not hasattr(sc.design, "timeline")
        msg = error_message(mr.handler(features=["Extrude1"], plane="yz"))
        assert "no timeline" in msg

    def test_comma_separated_feature_names_are_split(self, scene):
        a, b = feature_entity("Extrude1"), feature_entity("Fillet1")
        sc = scene(tl=timeline(timeline_object("Extrude1", 0, a), timeline_object("Fillet1", 1, b)))
        out = _payload(mr.handler(features="Extrude1, Fillet1", plane="yz"))
        assert out["source_features"] == ["Extrude1", "Fillet1"]
        assert sc.mf.last.entities.count == 2


class TestCollectionRefusal:
    def test_a_refused_object_is_reported_naming_it(self, scene):
        ent = feature_entity("Emboss1")
        sc = scene(tl=timeline(timeline_object("Emboss1", 0, ent)))
        sc.refuse.append(ent)
        msg = error_message(mr.handler(features=["Emboss1"], plane="yz"))
        # a swallowed False would leave an EMPTY collection for createInput to run on
        assert "Emboss1" in msg and type(ent).__name__ in msg
        assert sc.mf.last is None


class TestJoin:
    def test_join_is_refused_in_feature_mode(self, scene):
        sc = scene(tl=timeline(timeline_object("Extrude1", 0, feature_entity("Extrude1"))))
        msg = error_message(mr.handler(features=["Extrude1"], plane="yz", join=True))
        # isCombine is documented as ignored for a non-body input, so reporting joined=true is a lie
        assert "join" in msg and "bodies" in msg.lower()
        assert sc.mf.last is None

    def test_join_sets_iscombine_and_reports_the_feature_read_back(self, scene):
        sc = scene(bodies=("A",))
        sc.spawn = 0                       # a join merges into the source: no new body
        sc.feature = mirror_feature(bodies=[BRepBody("A", volume=SOURCE_VOLUME * 2)],
                                    is_combine=True)
        out = _payload(mr.handler(bodies=["A"], plane="yz", join=True))
        assert sc.mf.last.isCombine is True
        assert out["joined"] is True and out["bodies_added"] == 0
        assert out["volume_change_cm3"] == SOURCE_VOLUME

    def test_joined_comes_from_the_feature_not_the_request(self, scene):
        sc = scene(bodies=("A",))
        sc.feature = mirror_feature(bodies=[BRepBody("Body2", volume=SOURCE_VOLUME)],
                                    is_combine=False)        # the request did not take
        out = _payload(mr.handler(bodies=["A"], plane="yz", join=True))
        assert out["joined"] is False and "did not take" in out["note"]

    def test_join_defaults_false(self, scene):
        sc = scene(bodies=("A",))
        out = _payload(mr.handler(bodies=["A"], plane="yz"))
        assert sc.mf.last.isCombine is False and out["joined"] is False

    def test_feature_mode_publishes_no_joined_key(self, scene):
        scene(tl=timeline(timeline_object("Extrude1", 0, feature_entity("Extrude1"))))
        out = _payload(mr.handler(features=["Extrude1"], plane="yz"))
        assert "joined" not in out and "source_bodies" not in out

    def test_iscombine_that_does_not_take_is_an_error(self, scene):
        class _Stuck:
            """An input that keeps its default: the assignment lands, the property does not move."""

            def __init__(self, entities, plane):
                self.entities, self.plane, self.isCombine = entities, plane, False

            def __setattr__(self, key, value):
                object.__setattr__(self, key, False if key == "isCombine" else value)

        sc = scene(bodies=("A",))
        sc.input_factory = _Stuck
        msg = error_message(mr.handler(bodies=["A"], plane="yz", join=True))
        assert "did not take" in msg

    def test_iscombine_raise_surfaces_as_an_error(self, scene):
        class _ReadOnly:
            """An input whose isCombine setter raises."""

            def __init__(self, entities, plane):
                object.__setattr__(self, "entities", entities)
                object.__setattr__(self, "plane", plane)
                object.__setattr__(self, "isCombine", False)

            def __setattr__(self, key, value):
                if key == "isCombine":
                    raise AttributeError("isCombine is read-only")
                object.__setattr__(self, key, value)

        sc = scene(bodies=("A",))
        sc.input_factory = _ReadOnly
        msg = error_message(mr.handler(bodies=["A"], plane="yz", join=True))
        assert "Could not set join" in msg


def _source_owned_by(comp):
    """A mirror source whose parentComponent is `comp` - the one attribute census_host reads."""
    return types.SimpleNamespace(parentComponent=comp)


class TestEffectCensus:
    def test_body_mirror_reports_the_census_growth(self, scene):
        sc = scene(bodies=("BankL",))
        out = _payload(mr.handler(bodies=["BankL"], plane="yz"))
        assert out["mirrored"] is True and out["mode"] == "bodies" and out["plane"] == "yz"
        assert out["bodies_added"] == 1
        assert out["source_bodies"] == ["BankL"] and out["result_bodies"] == ["Body2"]
        assert sc.mf.last.plane == ("plane", "yz")

    def test_the_census_counts_the_build_component_too(self, scene):
        # the source body is OWNED by another component (an occurrence proxy reports its source
        # component), while the mirror lands in the component the feature is built in - a census
        # scoped to the source's component alone is blind to it
        owner = MakeComp(name="Owner", entity_token="TOKEN:Owner")
        sc = scene(bodies=("A",), body_owner=owner, extra_components=(owner,))
        sc.feature = mirror_feature(bodies=[BRepBody("Mirror1", volume=SOURCE_VOLUME)])
        out = _payload(mr.handler(bodies=["A"], plane="yz"))
        assert out["bodies_added"] == 1 and out["volume_change_cm3"] == 0.0

    def test_an_unidentifiable_host_makes_the_count_unjudgeable_not_doubled(self):
        # The source's owner cannot be told apart from the build component (no token reads), so the
        # census refuses to answer with a number: de-duplicating would drop a component the mirror
        # can land in, and appending would count one component twice and DOUBLE the delta.
        owner = MakeComp(name="Owner", bodies=[BRepBody("A")])      # no entityToken at all
        comp = MakeComp(name="Comp", bodies=[BRepBody("A")])
        assert mr._body_total(mr._census_hosts(_source_owned_by(owner), comp)) is None

    def test_a_host_PROVEN_distinct_is_counted_beside_the_build_component(self):
        owner = MakeComp(name="Owner", bodies=[BRepBody("A")], entity_token="TOKEN:Owner")
        comp = MakeComp(name="Comp", bodies=[BRepBody("B"), BRepBody("C")],
                        entity_token="TOKEN:Comp")
        assert mr._body_total(mr._census_hosts(_source_owned_by(owner), comp)) == 3

    def test_a_host_PROVEN_to_be_the_build_component_is_counted_once(self):
        # the de-dupe still fires on a proven match - counting it twice would double the delta
        comp = MakeComp(name="Comp", bodies=[BRepBody("B"), BRepBody("C")],
                        entity_token="TOKEN:Comp")
        twin = MakeComp(name="Comp", bodies=[BRepBody("B"), BRepBody("C")],
                        entity_token="TOKEN:Comp")
        assert mr._body_total(mr._census_hosts(_source_owned_by(twin), comp)) == 2

    def test_feature_mirror_is_verified_by_the_census(self, scene):
        ent = feature_entity("Emboss1", bodies=[BRepBody("Body1", volume=12.0)])
        sc = scene(bodies=("Body1",), tl=timeline(timeline_object("Emboss1", 0, ent)))
        out = _payload(mr.handler(features=["Emboss1"], plane="yz"))
        assert out["mirrored"] is True and out["bodies_added"] == 1

    def test_a_mirror_that_changes_nothing_is_an_error(self, scene):
        sc = scene(bodies=("A",))
        sc.spawn = 0                        # no new body, and the volume did not move either
        msg = error_message(mr.handler(bodies=["A"], plane="yz"))
        assert "added no body" in msg and "nothing was mirrored" in msg

    def test_a_flat_count_with_no_volume_read_is_unverified_not_a_no_op(self, scene):
        sc = scene(bodies=("A",))
        sc.spawn = 0
        sc.feature = mirror_feature(bodies=[])           # nothing to read a volume off
        msg = error_message(mr.handler(bodies=["A"], plane="yz"))
        assert "added no body" in msg and "UNVERIFIED" in msg and "moved material" in msg

    def test_a_flat_volume_with_no_count_read_is_unverified_not_a_no_op(self, scene):
        ent = feature_entity("Extrude1", bodies=[BRepBody("Body1", volume=12.0)])
        sc = scene(tl=timeline(timeline_object("Extrude1", 0, ent)))
        sc.spawn = 0
        sc.feature = mirror_feature(bodies=[BRepBody("Body1", volume=12.0)])
        sc.comp.bRepBodies = _Unreadable()
        msg = error_message(mr.handler(features=["Extrude1"], plane="yz"))
        assert "moved no volume" in msg and "UNVERIFIED" in msg and "added a body" in msg

    def test_neither_signal_readable_is_unverified(self, scene):
        ent = feature_entity("Extrude1")                 # no bodies -> no volume sample either
        sc = scene(tl=timeline(timeline_object("Extrude1", 0, ent)))
        sc.spawn = 0
        sc.feature = mirror_feature(bodies=[])
        sc.comp.bRepBodies = _Unreadable()
        msg = error_message(mr.handler(features=["Extrude1"], plane="yz"))
        assert "neither the body count" in msg and "UNVERIFIED" in msg

    def test_no_feature_returned_is_an_error(self, scene):
        sc = scene(bodies=("A",))
        sc.feature = None
        msg = error_message(mr.handler(bodies=["A"], plane="yz"))
        assert "Mirror returned no feature" in msg

    def test_add_that_raises_surfaces_the_platform_message(self, scene):
        sc = scene(bodies=("A",))
        sc.mf.add = lambda inp: (_ for _ in ()).throw(RuntimeError("3 : invalid input entities"))
        msg = error_message(mr.handler(bodies=["A"], plane="yz"))
        assert "Mirror failed" in msg and "invalid input entities" in msg

    def test_several_result_bodies_are_all_listed(self, scene):
        sc = scene(bodies=("A",))
        sc.feature = mirror_feature(bodies=[BRepBody(f"R{i}", volume=SOURCE_VOLUME) for i in range(3)])
        out = _payload(mr.handler(bodies=["A"], plane="yz"))
        assert out["result_bodies"] == ["R0", "R1", "R2"]

    def test_two_bodies_mirror_together(self, scene):
        sc = scene(bodies=("a", "b"))
        sc.spawn = 2
        sc.feature = mirror_feature(bodies=[BRepBody("Ma", volume=SOURCE_VOLUME),
                                            BRepBody("Mb", volume=SOURCE_VOLUME)])
        out = _payload(mr.handler(bodies="a, b", plane="xy"))
        assert out["source_bodies"] == ["a", "b"] and out["bodies_added"] == 2
        assert sc.mf.last.entities.count == 2
        assert sc.mf.last.plane == ("plane", "xy")

    def test_mirror_across_xz(self, scene):
        sc = scene(bodies=("A",))
        out = _payload(mr.handler(bodies=["A"], plane="xz"))
        assert out["plane"] == "xz" and sc.mf.last.plane == ("plane", "xz")


# ── the volume sample keys on the PHYSICAL body, not on a wrapper token ─────

# The x-ref shape, measured on a host holding two x-refs of one design: two DISTINCT bodies read one
# byte-identical entityToken while their source documents' lineage ids differ. A body and its own
# occurrence proxy are the other half - one physical body reading two different wrapper tokens.
_URN_XREF = "urn:adsk.wipprod:dm.lineage:K3I2nkywRlaWPHJexysOdA"
_URN_HOST = "urn:adsk.wipprod:dm.lineage:N_QoPrrrSJmF__f9BZV86A"
_SHARED_TOKEN = "/vB+AAEAAwAAAAAAAAAAAAAA"


def _body_in_document(name, volume, urn):
    """A source body owned by a component in the document with lineage id `urn` - the chain a body's
    source document is read through (parentComponent -> parentDesign -> parentDocument ->
    dataFile.id). Every body built here carries the SAME document-local token."""
    return BRepBody(name, volume=volume, entity_token=_SHARED_TOKEN,
                    parent_component=MakeComp(name=name,
                                              parent_design=make_source_document(urn)))


class TestFeatureVolumeSampleKeysOnPhysicalIdentity:
    """A feature mirror's BEFORE volume is summed over the bodies its source features act on. That
    sample is de-duplicated by _common.native_identity: an entityToken is DOCUMENT-LOCAL, so a
    wrapper-token key MERGES two distinct source bodies and understates the starting volume, and a
    body's own occurrence proxy carries a DIFFERENT token, so the same key SPLITS one body and
    counts its volume twice."""

    def test_two_source_bodies_sharing_a_document_local_token_are_both_measured(self, scene):
        a = _body_in_document("Frame", 10.0, _URN_XREF)
        b = _body_in_document("Lid", 4.0, _URN_HOST)
        assert a.entityToken == b.entityToken             # the tokens really collide
        ent = feature_entity("Emboss1", bodies=[a, b])
        sc = scene(bodies=("Body1",), tl=timeline(timeline_object("Emboss1", 0, ent)))
        sc.spawn = 0                                     # the volume is the only signal left
        sc.feature = mirror_feature(bodies=[BRepBody("Mirror1", volume=20.0)])
        out = _payload(mr.handler(features=["Emboss1"], plane="yz"))
        assert out["volume_change_cm3"] == 6.0           # 20 after - (10 + 4) before

    def test_a_source_body_reached_through_its_proxy_is_measured_once(self, scene):
        native = _body_in_document("Frame", 10.0, _URN_HOST)
        proxy = body_proxy(native, types.SimpleNamespace(name="Frame:1"))
        assert proxy.entityToken != native.entityToken   # the wrappers really differ
        ent = feature_entity("Emboss1", bodies=[native, proxy])
        sc = scene(bodies=("Body1",), tl=timeline(timeline_object("Emboss1", 0, ent)))
        sc.spawn = 0
        sc.feature = mirror_feature(bodies=[BRepBody("Mirror1", volume=20.0)])
        out = _payload(mr.handler(features=["Emboss1"], plane="yz"))
        assert out["volume_change_cm3"] == 10.0          # 20 after - 10 before, not 20 - 20

    def test_two_source_bodies_with_no_readable_identity_are_both_measured(self, scene):
        # The `or id(b)` last resort: two bodies nothing can be identified from over-count rather
        # than merge, so neither drops silently out of the starting total.
        a, b = BRepBody("Frame", volume=10.0), BRepBody("Lid", volume=4.0)
        for body in (a, b):
            del body.entityToken
        ent = feature_entity("Emboss1", bodies=[a, b])
        sc = scene(bodies=("Body1",), tl=timeline(timeline_object("Emboss1", 0, ent)))
        sc.spawn = 0
        sc.feature = mirror_feature(bodies=[BRepBody("Mirror1", volume=20.0)])
        out = _payload(mr.handler(features=["Emboss1"], plane="yz"))
        assert out["volume_change_cm3"] == 6.0
