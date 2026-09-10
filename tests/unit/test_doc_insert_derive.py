"""Unit tests for ``doc_insert_derive.py``: the source-open precondition, the SOURCE-side name
resolvers (component/body - exact match, miss lists names, ambiguity refused), the scoping that turns
names into sourceEntities, the landed read-back (derived_components + body counts, empty-landing is an
error), and the inline verify (healthState / documentReference.isOutOfDate / isDerived / params).

The cloud URN resolution is covered by test_doc_insert_occurrence.py (the shared _resolve_data_file);
here it is monkeypatched so these tests stay offline. Occurrence resolution (into_component) goes
through the real _inputs.OccurrenceRef kind against a fake design (dual seam patched). Every type
carrying a live shape dump uses its shared conftest fake; the DeriveFeature family has none, so it
keeps the bespoke doubles below.
"""

import json

from conftest import (BRepBody, FakeApplication, FakeDataFile, FakeDocumentReference, FakeDocuments,
                      FakeFeatures, FakeFusionDocument, FakeOccurrence, FakeProducts,
                      FakeUserParameter, FakeUserParameters, MakeComp, MakeDesign,
                      _NamedCollection, load_tool, make_occurrence)

io = load_tool("doc_insert_derive")


# ── destination-side fakes ───────────────────────────────────────────────────────────────────────

def FakeBody(name, is_derived=True):
    """One body a derive landed - isDerived is the flag the read-back verifies."""
    return BRepBody(name, is_derived=is_derived)


def FakeBodyCollection(bodies):
    return _NamedCollection(list(bodies))


class FakeDeriveFeature:
    """The landed DeriveFeature: its health pair, the bodies it brought in, whether it is parametric
    and the documentReference a freshness read goes through. DeriveFeature carries no live shape
    dump, so it has no shared fake."""
    def __init__(self, name="Derive1", health=0, message="", bodies=(), is_parametric=True,
                 out_of_date=False):
        self.name = name
        self.healthState = health
        self.errorOrWarningMessage = message
        self.bodies = FakeBodyCollection(bodies)
        self.isParametric = is_parametric
        self.documentReference = FakeDocumentReference(out_of_date=out_of_date)


class FakeDeriveFeatureInput:
    """The DeriveFeatureInput the handler populates - no live shape dump, so no shared fake."""
    def __init__(self):
        self.sourceEntities = None
        self.excludedEntities = None
        self.isIncludeComponentParameters = None
        self.isIncludeFavoriteParameters = None
        self.isPlaceObjectsAtOrigin = None


def _mk_body(spec):
    return FakeBody(*spec) if isinstance(spec, tuple) else FakeBody(spec)


class FakeDeriveFeatures:
    """result: 'default' (build+return a feature) | 'null' (add() returns None) | 'raise'.
    on_add: optional no-arg callback fired the instant add() would create the feature - lets a test
    simulate a side effect that happens DURING the derive (a new occurrence landing, a param count
    change). last_input captures the DeriveFeatureInput the handler populated."""

    def __init__(self, result="default", bodies=("Body1",), health=0, message="",
                 out_of_date=False, is_parametric=True, on_add=None):
        self.result = result
        self.bodies = bodies
        self.health = health
        self.message = message
        self.out_of_date = out_of_date
        self.is_parametric = is_parametric
        self.on_add = on_add
        self.last_input = None
        self.created = None

    def createInput(self, source_design):
        di = FakeDeriveFeatureInput()
        self.last_input = di
        return di

    def add(self, di):
        if self.result == "null":
            return None
        if self.result == "raise":
            raise RuntimeError("derive add failed")
        if self.on_add:
            self.on_add()
        feat = FakeDeriveFeature(health=self.health, message=self.message,
                                 bodies=[_mk_body(b) for b in self.bodies],
                                 is_parametric=self.is_parametric, out_of_date=self.out_of_date)
        self.created = feat
        return feat


def FakeOcc(name, is_derived=True, token=None, body_count=0, children=()):
    """A destination occurrence. body_count + children back _subtree_body_count; isDerived + token
    back the new-derived-occurrence diff."""
    occ = make_occurrence(path=name, derived=is_derived,
                          entity_token=token if token is not None else f"tok-{name}-{id(children)}",
                          bodies=[FakeBody(f"{name}_b{i}") for i in range(body_count)],
                          children=children)
    return occ


def FakeOccurrences(items):
    # tests mutate ._items to simulate an occurrence landing during the derive
    return _NamedCollection(list(items))


def FakeComp(name="Root", derive_features=None, occurrences=(), token=None):
    """The destination component. entityToken, because _common.same_component compares on it: the
    landing net that catches a derive surfacing at ROOT instead of nested is skipped (and disclosed)
    when the target cannot be told from the root. A test that wants that state deletes it."""
    comp = MakeComp(name, entity_token=token if token is not None else f"TOKEN:{name}")
    features = FakeFeatures()
    features.deriveFeatures = (derive_features if derive_features is not None
                               else FakeDeriveFeatures())
    comp.features = features
    comp.occurrences = FakeOccurrences(occurrences)
    return comp


def _params(count):
    """A parameter collection of `count` entries - only the count is read here."""
    return FakeUserParameters([FakeUserParameter(f"p{i}") for i in range(count)])


def _grow_params(params, count):
    """Resize a parameter collection - what a derive that imported parameters leaves behind."""
    params._parameters = [FakeUserParameter(f"p{i}") for i in range(count)]


def FakeDesign(root_comp, design_type=1, user_param_count=0, all_param_count=None, cls=MakeDesign):
    """The DESTINATION design (what _common.design() returns). designType: 1 = parametric, 0 = direct.
    all_param_count=None leaves the design with NO allParameters collection, so the design-level
    model-parameter count reads as UNKNOWN rather than zero. `cls` swaps in a scenario design."""
    return cls(comp=root_comp, design_type=design_type,
               user_parameters=_params(user_param_count),
               all_parameters=None if all_param_count is None else _params(all_param_count))


class _ReadBackDeclines(MakeDesign):
    """A design whose activeOccurrence READ throws - the state the payload must not report as a
    null. DECLARED, not measured: live the read answers. The setter still records, so activating
    the derive target works."""

    @property
    def activeOccurrence(self):
        raise RuntimeError("activeOccurrence did not read")

    @activeOccurrence.setter
    def activeOccurrence(self, value):
        self._active_occurrence = value


_NO_DEFAULT = object()


def FakeSourceDoc(name="Source", data_file=None, design=_NO_DEFAULT):
    """The open SOURCE document a derive reads its design through."""
    return FakeFusionDocument(
        name=name, data_file=data_file,
        products=FakeProducts(design=_make_source() if design is _NO_DEFAULT else design))


# ── source-side builders ─────────────────────────────────────────────────────────────────────────

def _src_comp(name, bodies=(), token=None):
    """A SOURCE component: name + bRepBodies/meshBodies collections (count/item).

    The entityToken is what _common.same_component compares on - the root derives as ITSELF while
    any other component derives through its occurrences, and the handler refuses rather than pick
    between those two on an identity that did not read. `token=None` is that unreadable state."""
    return MakeComp(name, bodies=list(bodies), mesh_bodies=[],
                    entity_token=token if token is not None else f"TOKEN:{name}")


def _src_occ(name):
    """A SOURCE occurrence - the handler just forwards it into sourceEntities."""
    return make_occurrence(path=name)


def _make_source(components=(), occ_by_comp=None, root_name="SrcRoot"):
    """A SOURCE design: rootComponent (with allOccurrencesByComponent) + allComponents (root + subs)."""
    root = MakeComp(root_name, mesh_bodies=[], entity_token=f"TOKEN:{root_name}",
                    occurrences_by_component=dict(occ_by_comp or {}))
    return MakeDesign(comp=root, all_components=[root] + list(components))


def _install(monkeypatch, *, design_type=1, user_param_count=0, occurrences=(),
             derive_features=None, comp_name="Root", data_file=None,
             source_design=_NO_DEFAULT, source_open=True, all_param_count=None):
    """Wire both design seams + a fake app.documents holding the (open) source doc + a stubbed
    _resolve_data_file. Returns (design, comp, docs, data_file, derive_features, source_doc)."""
    derive_features = derive_features if derive_features is not None else FakeDeriveFeatures()
    comp = FakeComp(comp_name, derive_features=derive_features, occurrences=occurrences)
    design = FakeDesign(comp, design_type=design_type, user_param_count=user_param_count,
                        all_param_count=all_param_count)
    monkeypatch.setattr(io._common, "design", lambda: design)
    monkeypatch.setattr(io._inputs._common, "design", lambda: design)
    df = data_file if data_file is not None else FakeDataFile(
        "SourcePart", file_id="urn:adsk.wipprod:dm.lineage:src", version=3)
    src = _make_source() if source_design is _NO_DEFAULT else source_design
    source_doc = FakeSourceDoc(data_file=df, design=src)
    docs = FakeDocuments([source_doc] if source_open else [])
    monkeypatch.setattr(io, "app", FakeApplication(documents=docs))
    monkeypatch.setattr(io, "_resolve_data_file", lambda raw: (df, df.id, [raw]))
    return design, comp, docs, df, derive_features, source_doc


def _payload(res):
    assert res["isError"] is False, res
    return json.loads(res["content"][0]["text"])


# ── guards ─────────────────────────────────────────────────────────────────────────────────────

class TestGuards:
    def test_empty_document_id_errors(self):
        res = io.handler(document_id="")
        assert res["isError"] is True and "document_id" in res["message"]

    def test_no_active_design(self, monkeypatch):
        monkeypatch.setattr(io._common, "design", lambda: None)
        res = io.handler(document_id="urn:x")
        assert res["isError"] is True and "No active design" in res["message"]

    def test_direct_mode_refused(self, monkeypatch):
        _install(monkeypatch, design_type=0)
        res = io.handler(document_id="urn:x")
        assert res["isError"] is True
        assert "parametric" in res["message"].lower()
        assert "design_set_mode" in res["message"]

    def test_unresolvable_document_id(self, monkeypatch):
        _install(monkeypatch)
        monkeypatch.setattr(io, "_resolve_data_file", lambda raw: (None, None, ["urn:adsk:1", "urn:adsk:2"]))
        res = io.handler(document_id="urn:nope")
        assert res["isError"] is True
        assert "Could not resolve" in res["message"]
        assert "urn:adsk:1" in res["message"]


# ── source-open precondition (createInput needs the source open - async load) ─────────────────────

class TestSourceOpenPrecondition:
    def test_source_not_open_errors_with_doc_open_guidance(self, monkeypatch):
        _, _, _, _, dfs, _ = _install(monkeypatch, source_open=False)
        res = io.handler(document_id="urn:x")
        assert res["isError"] is True
        assert "not open" in res["message"]
        assert "doc_open" in res["message"]          # points the caller at doc_open
        assert dfs.created is None                    # no partial state - never reached add()

    def test_source_open_is_used(self, monkeypatch):
        _install(monkeypatch)                         # source_open=True by default
        out = _payload(io.handler(document_id="urn:x"))
        assert out["derived"] is True


# ── SOURCE component resolver: exact match, miss lists names, ambiguity refused ───────────────────

class TestSourceComponentResolver:
    def test_exact_match_case_insensitive(self):
        src = _make_source([_src_comp("OuterRing"), _src_comp("InnerRing")])
        comps, err = io._resolve_source_components(src, ["outerring"])
        assert err is None
        assert [c.name for c in comps] == ["OuterRing"]

    def test_miss_lists_available_names(self):
        src = _make_source([_src_comp("OuterRing"), _src_comp("Rotor")])
        comps, err = io._resolve_source_components(src, ["NoSuchPart"])
        assert comps is None
        assert "not found" in err
        assert "OuterRing" in err and "Rotor" in err   # available names surfaced

    def test_ambiguous_name_refused_not_first_match(self):
        # TWO distinct components share the name - the resolver must REFUSE, never grab the first.
        a, b = _src_comp("Ring"), _src_comp("Ring")
        src = _make_source([a, b])
        comps, err = io._resolve_source_components(src, ["Ring"])
        assert comps is None                           # did NOT return [a]
        assert "ambiguous" in err and "2" in err

    def test_size_zero_returns_empty(self):
        comps, err = io._resolve_source_components(_make_source([_src_comp("A")]), [])
        assert err is None and comps == []

    def test_size_two_resolves_both_in_order(self):
        src = _make_source([_src_comp("A"), _src_comp("B"), _src_comp("C")])
        comps, err = io._resolve_source_components(src, ["B", "A"])
        assert err is None
        assert [c.name for c in comps] == ["B", "A"]   # order preserved


# ── SOURCE body resolver: name, Component/Body scoping, ambiguity refused ─────────────────────────

class TestSourceBodyResolver:
    def test_body_by_name(self):
        src = _make_source([_src_comp("OuterRing", bodies=["RingSolid"])])
        bodies, err = io._resolve_source_bodies(src, ["RingSolid"])
        assert err is None
        assert [b.name for b in bodies] == ["RingSolid"]

    def test_ambiguous_body_name_refused(self):
        src = _make_source([_src_comp("A", bodies=["Shared"]), _src_comp("B", bodies=["Shared"])])
        bodies, err = io._resolve_source_bodies(src, ["Shared"])
        assert bodies is None
        assert "ambiguous" in err
        assert "A" in err and "B" in err               # names the holders

    def test_component_slash_body_scopes_to_owner(self):
        # 'Shared' is ambiguous unqualified, but 'B/Shared' pins the owner.
        src = _make_source([_src_comp("A", bodies=["Shared"]), _src_comp("B", bodies=["Shared"])])
        bodies, err = io._resolve_source_bodies(src, ["B/Shared"])
        assert err is None and [b.name for b in bodies] == ["Shared"]

    def test_missing_body_named(self):
        src = _make_source([_src_comp("A", bodies=["X"])])
        bodies, err = io._resolve_source_bodies(src, ["Ghost"])
        assert bodies is None and "Ghost" in err


# ── collecting sourceEntities: component -> occurrences, root -> whole, no-occurrence error ────────

class TestCollectSourceEntities:
    def test_component_contributes_its_occurrences(self):
        occ = _src_occ("OuterRing:1")
        src = _make_source([_src_comp("OuterRing")], occ_by_comp={"OuterRing": [occ]})
        ents, labels, err = io._collect_source_entities(src, ["OuterRing"], [])
        assert err is None
        assert ents == [occ]                           # the occurrence, not the component
        assert labels == ["OuterRing"]

    def test_two_components_both_occurrences(self):
        o1, o2 = _src_occ("OuterRing:1"), _src_occ("InnerRing:1")
        src = _make_source([_src_comp("OuterRing"), _src_comp("InnerRing")],
                           occ_by_comp={"OuterRing": [o1], "InnerRing": [o2]})
        ents, labels, err = io._collect_source_entities(src, ["OuterRing", "InnerRing"], [])
        assert err is None and ents == [o1, o2]

    def test_naming_the_root_derives_whole_design(self):
        src = _make_source([_src_comp("Sub")], root_name="Assembly")
        ents, labels, err = io._collect_source_entities(src, ["Assembly"], [])
        assert err is None
        assert ents == [src.rootComponent]             # the root Component itself (whole design)
        assert "whole design" in labels[0]

    def test_component_without_occurrence_errors(self):
        src = _make_source([_src_comp("Lonely")], occ_by_comp={})   # defined, not instanced
        ents, labels, err = io._collect_source_entities(src, ["Lonely"], [])
        assert ents is None and "no occurrence" in err

    def test_the_root_is_recognised_by_identity_not_by_its_name(self):
        # The resolved component and source_design.rootComponent are separate WRAPPERS of one
        # component (component identity is never stable), and a per-field read on a source design is
        # guarded because it can fail. Here the rootComponent wrapper's name will not read: a NAME
        # compare then misses the root and sends the whole design down the occurrence branch, where
        # the root has none. The shared entityToken is what settles it.
        named_root = _src_comp("Assembly", token="tok-root")

        class _UnreadableName(MakeComp):
            """The root wrapper whose display name will not read - the same component, addressed
            through a read that declines."""
            @property
            def name(self):
                raise RuntimeError("3 : cloud read failed")

            @name.setter
            def name(self, value):
                pass

        unreadable = _UnreadableName("Assembly", mesh_bodies=[], entity_token="tok-root")
        src = MakeDesign(comp=unreadable, all_components=[named_root, _src_comp("Sub")])
        ents, labels, err = io._collect_source_entities(src, ["Assembly"], [])
        assert err is None
        assert ents == [named_root]                    # the whole design, not a no-occurrence error
        assert "whole design" in labels[0]


# ── read-back: subtree body count + new-derived-occurrence diff ───────────────────────────────────

class TestReadBackWalk:
    def test_subtree_body_count_sums_descendants(self):
        # a top occurrence with 0 direct bodies but children holding 1 + 2 -> 3 (the whole-design shape)
        child_a = FakeOcc("A:1", body_count=1)
        child_b = FakeOcc("B:1", body_count=2)
        top = FakeOcc("Root:1", body_count=0, children=[child_a, child_b])
        assert io._subtree_body_count(top) == 3

    def test_new_derived_occurrence_diff_excludes_preexisting(self):
        pre = FakeOcc("Old:1", is_derived=True, token="stable", body_count=1)
        comp = FakeComp("Root", occurrences=[pre])
        before = io._occurrence_tokens(comp)
        comp.occurrences._items.append(FakeOcc("New:1", is_derived=True, body_count=2))
        found = io._new_derived_occurrences(comp, before)
        assert found == [{"name": "New:1", "body_count": 2}]   # only the NEW one


# ── scoping through the handler: no selector -> whole; names -> occurrences on sourceEntities ──────

class TestScopingHandler:
    def test_no_selector_derives_whole_design(self, monkeypatch):
        src = _make_source([_src_comp("Sub")], root_name="WholeSrc")
        _, _, _, _, dfs, _ = _install(monkeypatch, source_design=src)
        out = _payload(io.handler(document_id="urn:x"))
        assert out["scope"] == "whole design"
        assert dfs.last_input.sourceEntities == [src.rootComponent]

    def test_scoped_components_forward_occurrences(self, monkeypatch):
        occ = _src_occ("OuterRing:1")
        src = _make_source([_src_comp("OuterRing")], occ_by_comp={"OuterRing": [occ]})
        _, _, _, _, dfs, _ = _install(monkeypatch, source_design=src)
        out = _payload(io.handler(document_id="urn:x", source_components=["OuterRing"]))
        assert dfs.last_input.sourceEntities == [occ]
        assert out["scope"] == "OuterRing"

    def test_bad_scope_name_errors_before_add(self, monkeypatch):
        src = _make_source([_src_comp("OuterRing")])
        _, _, _, _, dfs, _ = _install(monkeypatch, source_design=src)
        res = io.handler(document_id="urn:x", source_components=["Ghost"])
        assert res["isError"] is True and "Ghost" in res["message"]
        assert dfs.created is None                      # aborted before deriving - no partial state

    def test_exclude_components_set_excluded_entities(self, monkeypatch):
        drop = _src_occ("Rotor:1")
        src = _make_source([_src_comp("Rotor")], occ_by_comp={"Rotor": [drop]}, root_name="WholeSrc")
        _, _, _, _, dfs, _ = _install(monkeypatch, source_design=src)
        out = _payload(io.handler(document_id="urn:x", exclude_components=["Rotor"]))
        assert dfs.last_input.excludedEntities == [drop]
        assert out["excluded"] == "Rotor"


# ── read-back honesty: something must actually land ───────────────────────────────────────────────

class TestReadBackHonesty:
    def test_nothing_landed_is_an_error(self, monkeypatch):
        # a feature with no bodies, and no new derived occurrence, and no design-wide body delta.
        derive_features = FakeDeriveFeatures(bodies=())
        _install(monkeypatch, derive_features=derive_features, occurrences=[])
        res = io.handler(document_id="urn:x")
        assert res["isError"] is True and "nothing landed" in res["message"]

    def test_geometry_without_derived_marker_is_an_error(self, monkeypatch):
        # bodies appear but none report isDerived=true -> the one-way link did not form.
        derive_features = FakeDeriveFeatures(bodies=[("Body1", False)])
        _install(monkeypatch, derive_features=derive_features)
        res = io.handler(document_id="urn:x")
        assert res["isError"] is True and "isDerived" in res["message"]

    def test_direct_derived_bodies_reported(self, monkeypatch):
        derive_features = FakeDeriveFeatures(bodies=[("Body1", True), ("Body2", False)])
        _install(monkeypatch, derive_features=derive_features)
        out = _payload(io.handler(document_id="urn:x"))
        assert out["derived_occurrence"] == "Body1"
        assert out["derived_bodies"] == [
            {"name": "Body1", "is_derived": True}, {"name": "Body2", "is_derived": False}]

    def test_derived_occurrence_reported_with_body_count(self, monkeypatch):
        derive_features = FakeDeriveFeatures(bodies=())
        design, comp, docs, df, dfs, _ = _install(monkeypatch, derive_features=derive_features,
                                                  occurrences=[])
        dfs.on_add = lambda: comp.occurrences._items.append(
            FakeOcc("SourcePart:1", is_derived=True, body_count=1))
        out = _payload(io.handler(document_id="urn:x"))
        assert out["derived_bodies"] == []
        assert out["derived_components"] == [{"name": "SourcePart:1", "body_count": 1}]
        assert out["derived_occurrence"] == "SourcePart:1"

    def test_preexisting_derived_occurrence_not_counted(self, monkeypatch):
        pre_existing = FakeOcc("OldDerived:1", is_derived=True, token="stable-token")
        derive_features = FakeDeriveFeatures(bodies=())
        _install(monkeypatch, derive_features=derive_features, occurrences=[pre_existing])
        res = io.handler(document_id="urn:x")
        assert res["isError"] is True and "nothing landed" in res["message"]


# ── null add() / exceptions / missing collection (bite-proofed) ──────────────────────────────────

class TestAddFailures:
    def test_null_add_errors_not_false_ok(self, monkeypatch):
        derive_features = FakeDeriveFeatures(result="null")
        _install(monkeypatch, derive_features=derive_features)
        res = io.handler(document_id="urn:x")
        assert res["isError"] is True and "add returned nothing" in res["message"]

    def test_add_raising_errors(self, monkeypatch):
        derive_features = FakeDeriveFeatures(result="raise")
        _install(monkeypatch, derive_features=derive_features)
        res = io.handler(document_id="urn:x")
        assert res["isError"] is True and "Derive failed" in res["message"]

    def test_missing_derive_features_collection_errors(self, monkeypatch):
        comp = FakeComp()
        comp.features.deriveFeatures = None      # this build's Features carries no derive collection
        design = FakeDesign(comp)
        monkeypatch.setattr(io._common, "design", lambda: design)
        monkeypatch.setattr(io._inputs._common, "design", lambda: design)
        df = FakeDataFile("SourcePart", file_id="urn:adsk.wipprod:dm.lineage:src")
        source_doc = FakeSourceDoc(data_file=df, design=_make_source())
        monkeypatch.setattr(io, "app",
                            FakeApplication(documents=FakeDocuments([source_doc])))
        monkeypatch.setattr(io, "_resolve_data_file", lambda raw: (df, raw, [raw]))
        res = io.handler(document_id="urn:x")
        assert res["isError"] is True and "deriveFeatures" in res["message"]


# ── healthState / documentReference verify ───────────────────────────────────────────────────────

class TestHealthAndReferenceVerify:
    def test_error_health_state_bites(self, monkeypatch):
        derive_features = FakeDeriveFeatures(health=2, message="boundary edges do not match")
        _install(monkeypatch, derive_features=derive_features)
        res = io.handler(document_id="urn:x")
        assert res["isError"] is True
        assert "FAILED to compute" in res["message"]
        assert "boundary edges do not match" in res["message"]

    def test_warning_health_state_does_not_error(self, monkeypatch):
        derive_features = FakeDeriveFeatures(health=1, message="a minor warning")
        _install(monkeypatch, derive_features=derive_features)
        out = _payload(io.handler(document_id="urn:x"))
        assert out["derived"] is True and out["feature_name"] == "Derive1"

    def test_out_of_date_at_creation_errors(self, monkeypatch):
        derive_features = FakeDeriveFeatures(out_of_date=True)
        _install(monkeypatch, derive_features=derive_features)
        res = io.handler(document_id="urn:x")
        assert res["isError"] is True and "isOutOfDate=true" in res["message"]


# ── parameter-flag verify ─────────────────────────────────────────────────────────────────────────

class TestParameterVerify:
    def test_warns_when_flags_set_and_zero_landed(self, monkeypatch):
        _install(monkeypatch, user_param_count=4)   # count never changes -> 0 imported
        out = _payload(io.handler(document_id="urn:x"))
        assert out["parameters_imported"] == 0
        assert "parameter_warning" in out and "flaky" in out["parameter_warning"]

    def test_warning_points_at_the_model_parameter_route(self, monkeypatch):
        # 0 user parameters is not "nothing arrived": source values can land as read-only MODEL
        # parameters, and the warning must name the read that sees them.
        _install(monkeypatch, user_param_count=4)
        out = _payload(io.handler(document_id="urn:x"))
        assert "param_get(include_model_parameters=true)" in out["parameter_warning"]

    def test_model_parameters_added_is_the_design_level_delta(self, monkeypatch):
        # the imported values land as MODEL parameters at the DESIGN level, so the delta is taken
        # over design.allParameters - not over the derived component, which holds only its own.
        design, comp, docs, df, dfs, _ = _install(monkeypatch, user_param_count=4,
                                                  all_param_count=9)
        dfs.on_add = lambda: _grow_params(design.allParameters, 29)
        out = _payload(io.handler(document_id="urn:x"))
        assert out["parameters_imported"] == 0        # the userParameters delta saw none...
        assert out["model_parameters_added"] == 20    # ...while 20 model parameters landed

    def test_zero_delta_is_published_honestly(self, monkeypatch):
        # both counts read, and they matched: 0 is an ANSWER here, unlike an unreadable count.
        _install(monkeypatch, all_param_count=9)
        out = _payload(io.handler(document_id="urn:x"))
        assert out["model_parameters_added"] == 0

    def test_model_parameter_delta_omitted_when_unreadable(self, monkeypatch):
        # allParameters cannot be read, so the delta is UNKNOWN - the key is omitted, never a false 0.
        _install(monkeypatch)
        out = _payload(io.handler(document_id="urn:x"))
        assert "model_parameters_added" not in out

    def test_no_warning_when_both_flags_false(self, monkeypatch):
        _install(monkeypatch, user_param_count=4)
        out = _payload(io.handler(document_id="urn:x", include_parameters=False,
                                  include_favorite_parameters=False))
        assert out["parameters_imported"] == 0
        assert "parameter_warning" not in out

    def test_reports_imported_count_honestly(self, monkeypatch):
        design, comp, docs, df, dfs, _ = _install(monkeypatch, user_param_count=2)
        dfs.on_add = lambda: _grow_params(design.userParameters, 5)
        out = _payload(io.handler(document_id="urn:x"))
        assert out["parameters_imported"] == 3
        assert "parameter_warning" not in out

    def test_flags_forwarded_to_derive_input(self, monkeypatch):
        design, comp, docs, df, dfs, _ = _install(monkeypatch)
        _payload(io.handler(document_id="urn:x", include_parameters=False,
                            include_favorite_parameters=True, place_at_origin=False))
        di = dfs.last_input
        assert di.isIncludeComponentParameters is False
        assert di.isIncludeFavoriteParameters is True
        assert di.isPlaceObjectsAtOrigin is False


# ── into_component resolution (shared OccurrenceRef kind) ────────────────────────────────────────

class TestIntoComponent:
    def test_empty_uses_root(self, monkeypatch):
        _install(monkeypatch)
        out = _payload(io.handler(document_id="urn:x"))
        assert out["into_component"] == "root component"

    def _nested_setup(self, monkeypatch, *, occ_activates=True, chassis_derive=None,
                      root_restore="lands", read_back="reads", prev_active=None):
        """A design with a Chassis:1 occurrence targetable by into_component. The occ counts its
        activate() calls and takes the design's edit target with it (the platform routes a derive
        into the ACTIVE component, so nesting activates the target first). activateRootComponent
        answers True in every measured state, so root_restore says what it DOES: 'lands' clears
        activeOccurrence, 'lies' leaves the occurrence holding it, 'raise' throws. read_back
        'declines' makes the activeOccurrence READ throw; prev_active names the component holding
        the edit target BEFORE the call (default: the root)."""
        chassis_derive = chassis_derive or FakeDeriveFeatures()
        chassis_comp = FakeComp("Chassis", derive_features=chassis_derive)
        calls = {"activated": 0, "root_restored": 0}
        root_comp = FakeComp("Root")
        design = FakeDesign(root_comp,
                            cls=_ReadBackDeclines if read_back == "declines" else MakeDesign)
        if prev_active is not None:
            design.activeComponent = MakeComp(prev_active)

        class _ActivatableOcc(FakeOccurrence):
            """The into_component target: activating it makes it the design's activeOccurrence."""
            def activate(self):
                calls["activated"] += 1
                if occ_activates:
                    design.activeOccurrence = self
                return occ_activates

        occ = _ActivatableOcc("Chassis:1", chassis_comp)
        root_comp.allOccurrences = [occ]

        def _restore_root():
            calls["root_restored"] += 1
            if root_restore == "raise":
                raise RuntimeError("activateRootComponent blew up")
            if root_restore == "lands":
                design.activeOccurrence = None
            return True
        design.activateRootComponent = _restore_root
        monkeypatch.setattr(io._common, "design", lambda: design)
        monkeypatch.setattr(io._inputs._common, "design", lambda: design)
        df = FakeDataFile("SourcePart", file_id="urn:adsk.wipprod:dm.lineage:src")
        source_doc = FakeSourceDoc(data_file=df, design=_make_source())
        monkeypatch.setattr(io, "app",
                            FakeApplication(documents=FakeDocuments([source_doc])))
        monkeypatch.setattr(io, "_resolve_data_file", lambda raw: (df, raw, [raw]))
        return design, chassis_comp, chassis_derive, calls

    def test_named_occurrence_activates_derives_and_restores_root(self, monkeypatch):
        design, chassis, chassis_derive, calls = self._nested_setup(monkeypatch)
        out = _payload(io.handler(document_id="urn:x", into_component="Chassis:1"))
        assert "Chassis" in out["into_component"]
        assert chassis_derive.created is not None
        assert calls["activated"] == 1       # nesting = activate the target before add()
        assert calls["root_restored"] == 1   # ...and restore the root edit target after
        assert out["active_occurrence_after"] is None    # the read-back that confirms the restore
        assert "root_restore_note" not in out

    def test_a_non_root_edit_target_before_the_call_is_named_after_it(self, monkeypatch):
        # The restore landed, so the payload says where the edit target WAS - the caller may want
        # it back. This is the CONFIRMED-root arm; a refuted one publishes root_restore_note.
        self._nested_setup(monkeypatch, prev_active="Fixture")
        out = _payload(io.handler(document_id="urn:x", into_component="Chassis:1"))
        assert out["active_occurrence_after"] is None
        assert "'Fixture'" in out["edit_target_note"]

    def test_a_root_restore_the_read_back_refutes_is_reported(self, monkeypatch):
        # activateRootComponent() answers True whether or not the edit target moved, so the
        # activeOccurrence read-back is the only signal: still holding the occurrence means the
        # restore did not land, and the derive that DID land is still reported beside it.
        _design, _chassis, derive, calls = self._nested_setup(monkeypatch, root_restore="lies",
                                                              prev_active="Fixture")
        out = _payload(io.handler(document_id="urn:x", into_component="Chassis:1"))
        assert derive.created is not None and calls["root_restored"] == 1
        assert out["active_occurrence_after"] == "Chassis:1"
        assert out["root_restore_note"].startswith("nesting activated component 'Chassis'")
        assert "not confirmed" in out["root_restore_note"]
        # the OTHER arm's note would claim the target reads ROOT now, which nothing read
        assert "edit_target_note" not in out

    def test_a_raising_read_back_is_not_reported_as_root(self, monkeypatch):
        # A read that DECLINES is not the null that confirms the restore: the sentinel keeps it out
        # of the payload entirely and the note says the read did not answer.
        _design, _chassis, derive, calls = self._nested_setup(monkeypatch, read_back="declines")
        out = _payload(io.handler(document_id="urn:x", into_component="Chassis:1"))
        assert derive.created is not None and calls["root_restored"] == 1
        assert "active_occurrence_after" not in out
        assert "did not read" in out["root_restore_note"]

    def test_a_RAISING_root_restore_does_not_sink_the_call(self, monkeypatch):
        # The restore runs in a finally, so a raise there would escape the handler and turn a
        # landed derive into an exception - it is swallowed, and the read-back still reports where
        # the edit target is.
        _design, _chassis, derive, calls = self._nested_setup(monkeypatch, root_restore="raise")
        out = _payload(io.handler(document_id="urn:x", into_component="Chassis:1"))
        assert "Chassis" in out["into_component"] and derive.created is not None
        assert calls["root_restored"] == 1
        assert out["active_occurrence_after"] == "Chassis:1"
        assert "not confirmed" in out["root_restore_note"]

    def test_activation_failure_refuses_before_deriving(self, monkeypatch):
        design, chassis, chassis_derive, calls = self._nested_setup(monkeypatch,
                                                                    occ_activates=False)
        res = io.handler(document_id="urn:x", into_component="Chassis:1")
        assert res["isError"] is True and "activate" in res["message"].lower()
        assert chassis_derive.created is None       # nothing was derived

    def test_root_stray_landing_is_an_honest_error(self, monkeypatch):
        # The platform ignored the activation and landed the derive at ROOT: the read-back must
        # say so, never report a nested success over a root sibling.
        chassis_derive = FakeDeriveFeatures()
        design, chassis, _, calls = self._nested_setup(monkeypatch,
                                                       chassis_derive=chassis_derive)
        stray = FakeOcc("Stray:1", is_derived=True, body_count=1)
        chassis_derive.on_add = (
            lambda: design.rootComponent.occurrences._items.append(stray))
        design.rootComponent.occurrences = FakeOccurrences([])
        res = io.handler(document_id="urn:x", into_component="Chassis:1")
        assert res["isError"] is True
        assert "ROOT" in res["message"] and "Stray:1" in res["message"]

    def test_an_unidentifiable_target_discloses_the_unrun_landing_check(self, monkeypatch):
        # same_component answers None with no readable token, so the stray-at-ROOT net cannot run:
        # a root-targeted derive answers that net POSITIVELY, and firing it here would report a
        # successful derive as a failure and tell the caller to delete it. The derive stands and the
        # payload says the landing was not verified.
        chassis_derive = FakeDeriveFeatures()
        design, chassis, _, _calls = self._nested_setup(monkeypatch,
                                                        chassis_derive=chassis_derive)
        del chassis.entityToken
        stray = FakeOcc("Stray:1", is_derived=True, body_count=1)
        chassis_derive.on_add = (
            lambda: design.rootComponent.occurrences._items.append(stray))
        design.rootComponent.occurrences = FakeOccurrences([])
        out = _payload(io.handler(document_id="urn:x", into_component="Chassis:1"))
        assert out["derived"] is True                     # not rolled back on an unread token
        assert "unverified" in out["landing_unverified"].lower()

    def test_a_PROVEN_root_target_runs_no_stray_net(self, monkeypatch):
        # the other side of the boundary: `is False` gates the net, so a target proven to BE the
        # root neither reports its own successful landing as a stray nor discloses an unrun check
        _install(monkeypatch)
        out = _payload(io.handler(document_id="urn:x"))
        assert out["derived"] is True and "landing_unverified" not in out

    def test_unknown_occurrence_errors(self, monkeypatch):
        _install(monkeypatch)
        res = io.handler(document_id="urn:x", into_component="Ghost")
        assert res["isError"] is True and "Ghost" in res["message"]


# ── payload contract ───────────────────────────────────────────────────────────────────────────

class TestPayloadContract:
    def test_payload_keys_match_returns(self, monkeypatch):
        _install(monkeypatch)
        out = _payload(io.handler(document_id="urn:x"))
        for kind in io.RETURNS:
            problem = kind.assert_present(out)
            assert problem == "", problem

    def test_saved_version_wire_sentence_present(self, monkeypatch):
        # derive reads the source's last SAVED cloud version, not live in-session edits.
        _install(monkeypatch)
        out = _payload(io.handler(document_id="urn:x"))
        assert "last SAVED cloud version" in out["note"]
        assert "last SAVED cloud version" in io.TOOL_DESCRIPTION

    def test_document_metadata_reported(self, monkeypatch):
        df = FakeDataFile("Gimbal", file_id="urn:adsk.wipprod:dm.lineage:abc", version=7)
        _install(monkeypatch, data_file=df)
        out = _payload(io.handler(document_id="whatever-resolves"))
        assert out["document_id"] == df.id
        assert out["document_name"] == "Gimbal"
        assert out["source_version"] == 7
        assert out["is_parametric"] is True
