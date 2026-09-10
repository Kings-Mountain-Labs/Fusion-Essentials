"""Unit tests for mesh_to_brep.py - the mesh->BRep conversion and its new-body diff."""

import types

import adsk.fusion
import pytest

from conftest import (BRepBody, FakeBaseFeature, FakeBaseFeatures, FakeFeatures, MakeComp,
                      MeshBody, _NamedCollection, body_proxy, install, load_tool, make_design,
                      payload)

mo = load_tool("mesh_to_brep")

_CONV = adsk.fusion.MeshConvertMethodTypes


def _brep(name, is_solid=True):
    """One BRep body carrying the find_geometry-style handle the payload publishes."""
    return BRepBody(name, is_solid=is_solid, entity_token=f"BTOK::{name}")


class _Features(FakeFeatures):
    """comp.features plus the mesh-convert collection this tool reaches through."""
    def __init__(self, convert=None, base_features=None):
        super().__init__(base_features=base_features)
        self.meshConvertFeatures = convert


class _FeatureResult:
    """The MeshConvertFeature add() returns: its name and the BRep bodies it made."""
    def __init__(self, name, bodies):
        self.name = name
        self.bodies = _NamedCollection(bodies)


class _MeshFeatures:
    """comp.features.meshConvertFeatures: createInput -> input -> add() -> feature or None.

    raise_on_add forces a mutation failure (it must surface, not be swallowed); none_feature is the
    None a non-parametric add returns; on_add_append is the (collection, body) pair the add drops
    onto the component, the observable side effect a None return is judged by."""
    def __init__(self, result_bodies, feat_name="MeshFeat1", raise_on_add=False, none_feature=False,
                 on_add_append=None, on_add=None, input_factory=None):
        self._result_bodies = result_bodies
        self._feat_name = feat_name
        self.raise_on_add = raise_on_add
        self.none_feature = none_feature
        self._on_add_append = on_add_append
        self._on_add = on_add
        self._input_factory = input_factory or (lambda: type("Inp", (), {})())
        self.last_input = None

    def createInput(self, *a):
        self.last_input = self._input_factory()
        return self.last_input

    def add(self, inp):
        if self.raise_on_add:
            raise RuntimeError("conversion failed")
        if self._on_add is not None:
            self._on_add()
        if self._on_add_append is not None:
            coll, body = self._on_add_append
            coll._items.append(body)
        if self.none_feature:
            return None
        return _FeatureResult(self._feat_name, self._result_bodies)


@pytest.fixture(autouse=True)
def _types(monkeypatch):
    """The adsk.fusion type identities the body kind and the base-feature scope check branch on."""
    monkeypatch.setattr(adsk.fusion, "MeshBody", MeshBody, raising=False)
    monkeypatch.setattr(adsk.fusion, "BRepBody", BRepBody, raising=False)
    monkeypatch.setattr(adsk.fusion, "BaseFeature", FakeBaseFeature, raising=False)


def _wire(src, feats, brep_bodies, design_type=0, base_feature=None):
    """One mesh + a meshConvertFeatures collection on its component, wired into the tool."""
    comp = MakeComp("Comp", mesh_bodies=[src] if src is not None else [])
    comp.bRepBodies = brep_bodies
    comp.features = _Features(convert=feats,
                              base_features=FakeBaseFeatures(made=base_feature))
    if src is not None:
        src.parentComponent = comp
    return comp


class TestMeshToBrep:

    def _setup(self, is_closed=True, raise_on_add=False, none_feature=False, organic=True,
               none_appends_body=False, parametric=False, base_feature=None):
        if not organic:
            # organic method ABSENT (older Fusion / no Product Design Extension) -> _organic_available()
            # False -> honest refusal. Genuinely REMOVE the seeded member so the safe() read raises,
            # matching the live "attribute does not exist" case (conftest restores it after the test).
            if hasattr(_CONV, "OrganicMeshConvertMethodType"):
                delattr(_CONV, "OrganicMeshConvertMethodType")
        brep_coll = _NamedCollection()                      # comp.bRepBodies - starts empty
        # In non-parametric mode add() returns None; none_appends_body models the side effect: the
        # convert still drops a new BRep body onto the component (that body is the success signal).
        append = (brep_coll, _brep("ConvertedBody")) if none_appends_body else None
        feats = _MeshFeatures([_brep("ConvertedBody")], raise_on_add=raise_on_add,
                              none_feature=none_feature, on_add_append=append)
        src = MeshBody("Scan", is_closed=is_closed)
        bf = base_feature
        comp = _wire(src, feats, brep_coll, design_type=1 if parametric else 0,
                     base_feature=bf if parametric else None)
        install(mo, make_design(comp=comp, tokens={"H": src},
                                design_type=1 if parametric else 0,
                                active_edit_object=bf if parametric else None))
        return src, feats

    _OCC = types.SimpleNamespace(name="Comp:1", fullPathName="Comp:1")

    def _proxy_convert(self, made, census_ghost=False):
        """A direct design whose convert leaves the component's BRep collection holding the
        pre-existing body's occurrence PROXY plus `made`. Every collection read mints a fresh wrapper
        and a proxy's entityToken differs from its native's (measured), so this is the shape that
        separates a key read off the WRAPPER from one read off the physical body. Returns the
        pre-existing body.

        census_ghost adds a second PRE-EXISTING body whose token reads empty, so the BEFORE set is
        asked to hold a None key of its own - the only shape in which the census filter and the
        publish clause stop covering for each other."""
        existing = _brep("Existing")
        census = [existing]
        if census_ghost:
            ghost = _brep("Ghost")
            ghost.entityToken = ""
            census.append(ghost)
        brep_coll = _NamedCollection(census)

        def _convert():
            brep_coll._items[:] = [body_proxy(existing, self._OCC)] + census[1:] + [made]

        assert body_proxy(existing, self._OCC).entityToken != existing.entityToken   # a real proxy
        feats = _MeshFeatures([], none_feature=True, on_add=_convert)
        src = MeshBody("Scan", is_closed=True)
        comp = _wire(src, feats, brep_coll)
        install(mo, make_design(comp=comp, tokens={"H": src}, design_type=0))
        return existing

    def test_prismatic_converts_and_reports_method(self):
        self._setup(is_closed=True)
        out = payload(mo.handler(mesh="H", method="prismatic"))
        assert out["converted"] is True
        assert out["method"] == "prismatic"
        assert out["brep_bodies"][0]["name"] == "ConvertedBody"
        assert out["brep_bodies"][0]["handle"] == "BTOK::ConvertedBody"

    def test_non_watertight_refused_up_front(self):
        # the watertight pre-check: an open mesh is refused BEFORE any add(), with the likely cause
        src, feats = self._setup(is_closed=False)
        res = mo.handler(mesh="H", method="prismatic")
        assert res["isError"] is True
        assert "not watertight" in res["message"].lower()
        # and the mutation was never attempted (no input was created)
        assert feats.last_input is None

    def test_organic_without_extension_is_honest_error(self):
        # API-not-available op surfaces an HONEST error, NOT a fake success or a silent fallback
        src, feats = self._setup(is_closed=True, organic=False)
        res = mo.handler(mesh="H", method="organic")
        assert res["isError"] is True
        assert "Product Design Extension" in res["message"]
        assert "silently" in res["message"].lower() or "not silently" in res["message"].lower()
        assert feats.last_input is None      # refused before any mutation

    def test_conversion_add_failure_surfaces(self):
        self._setup(is_closed=True, raise_on_add=True)
        res = mo.handler(mesh="H", method="prismatic")
        assert res["isError"] is True and "failed" in res["message"].lower()

    def test_none_feature_with_new_brep_body_is_success(self):
        # add() returns None in a direct design but the conversion applied - a new BRep body appeared on
        # the component. Success is judged by that body, not by the (None) feature return.
        self._setup(is_closed=True, none_feature=True, none_appends_body=True)
        out = payload(mo.handler(mesh="H", method="prismatic"))
        assert out["converted"] is True
        assert out["design_mode"] == "direct"
        assert out["base_feature"] is None            # direct opens no scope
        assert out["feature"] is None
        assert out["brep_bodies"][0]["name"] == "ConvertedBody"
        assert mo._common.DIRECT_FEATURE_NOTE in out["note"]

    def test_a_pre_existing_body_rewrapped_as_a_proxy_is_not_reported_as_converted(self):
        # Keyed on the wrapper's token a body that was already there reads as newly converted, and
        # the payload publishes a BRep body this conversion never made. The diff has to key on the
        # physical body, whatever wrapper each read hands back.
        native = _brep("ConvertedBody")
        existing = self._proxy_convert(body_proxy(native, self._OCC))
        out = payload(mo.handler(mesh="H", method="prismatic"))
        names = [r["name"] for r in out["brep_bodies"]]
        assert names == ["ConvertedBody"]
        assert existing.name not in names

    def test_the_published_handle_is_the_WRAPPER_token_not_the_identity_token(self):
        # The handle is what resolves back to THIS reference, so it is the wrapper's own token - the
        # identity beside it resolves to the NATIVE, which addresses a different reference. The two
        # coincide on a native converted body, so the body has to arrive as a proxy to tell them
        # apart at all.
        native = _brep("ConvertedBody")
        made = body_proxy(native, self._OCC)
        self._proxy_convert(made)
        out = payload(mo.handler(mesh="H", method="prismatic"))
        assert out["brep_bodies"][0]["handle"] == made.entityToken
        assert out["brep_bodies"][0]["handle"] != native.entityToken

    def test_a_converted_body_with_no_readable_identity_is_still_published(self):
        # An empty token yields NO identity, so nothing can show this body was in the census before.
        # The guard publishes it - an over-report the caller can see and check with model_inspect -
        # rather than dropping it, which would report a conversion that produced nothing at all.
        made = _brep("ConvertedBody")
        made.entityToken = ""
        self._proxy_convert(made)
        out = payload(mo.handler(mesh="H", method="prismatic"))
        assert [r["name"] for r in out["brep_bodies"]] == ["ConvertedBody"]

    def test_an_identity_less_census_body_does_not_swallow_the_converted_one(self):
        # The census can hold a body whose token reads empty too. Two things then keep the converted
        # body publishable: the before-set drops its own None key, and the publish clause admits a
        # None key outright. Either one alone is enough, so each hides the loss of the other - and
        # with BOTH gone the before-set's None matches the converted body's None, the conversion is
        # dropped, and a call that SUCCEEDED reports "did not produce a BRep body".
        made = _brep("ConvertedBody")
        made.entityToken = ""
        self._proxy_convert(made, census_ghost=True)
        out = payload(mo.handler(mesh="H", method="prismatic"))
        assert "ConvertedBody" in [r["name"] for r in out["brep_bodies"]]

    def test_null_feature_in_a_parametric_scope_reports_parametric(self):
        # the scope suppresses the feature; the payload reports the DESIGN's own mode and names the
        # base feature the conversion landed in, instead of labelling the design non-parametric.
        bf = FakeBaseFeature()
        self._setup(is_closed=True, parametric=True, base_feature=bf, none_feature=True,
                    none_appends_body=True)
        out = payload(mo.handler(mesh="H", method="prismatic"))
        assert out["feature"] is None
        assert out["design_mode"] == "parametric"
        assert out["base_feature"] == "BaseFeature1"
        assert "BaseFeature1" in out["note"]
        assert "direct" not in out["note"].lower()

    def test_none_feature_with_no_new_body_is_real_failure_with_hint(self):
        # add() returned None AND no new BRep body appeared -> a REAL failure. Keep the prismatic
        # face-groups hint so the agent knows the likely fix.
        self._setup(is_closed=True, none_feature=True, none_appends_body=False)
        res = mo.handler(mesh="H", method="prismatic")
        assert res["isError"] is True
        assert "did not produce a BRep body" in res["message"]
        assert "mesh_generate_face_groups" in res["message"]

    def test_parametric_routes_through_base_feature_scope(self):
        # REGRESSION: in PARAMETRIC the convert runs INSIDE the helper's base-feature scope (opened AND
        # finished). The parametric add returns a feature carrying .bodies; success is reported without
        # any undetectable-scope guard defeating it.
        bf = FakeBaseFeature()
        self._setup(is_closed=True, parametric=True, base_feature=bf)
        out = payload(mo.handler(mesh="H", method="prismatic"))
        assert out["converted"] is True
        assert out["brep_bodies"][0]["name"] == "ConvertedBody"
        assert bf._starts == 1 and bf._finishes == 1

    def test_brep_handle_to_convert_is_redirected(self):
        # passing a BRep body to mesh_to_brep (it wants a MESH) -> MeshBodyRef redirect
        brep = _brep("AlreadySolid")
        comp = _wire(None, None, _NamedCollection())
        install(mo, make_design(comp=comp, tokens={"H": brep}, design_type=0))
        res = mo.handler(mesh="H")
        assert res["isError"] is True and "must be a MESH body" in res["message"]

    def test_missing_convert_features_collection_errors(self):
        src = MeshBody("Scan", is_closed=True)
        comp = _wire(src, None, _NamedCollection())
        install(mo, make_design(comp=comp, tokens={"H": src}, design_type=0))
        res = mo.handler(mesh="H", method="prismatic")
        assert res["isError"] is True
        assert "meshConvertFeatures collection" in res["message"]

    def test_create_input_none_errors(self):
        src, feats = self._setup(is_closed=True)
        feats.createInput = lambda *a: None
        res = mo.handler(mesh="H", method="prismatic")
        assert res["isError"] is True and "returned nothing" in res["message"]

    def test_faceted_method_resolves_enum(self):
        # the faceted branch maps to FacetedMeshConvertMethodType on the input
        src, feats = self._setup(is_closed=True)
        out = payload(mo.handler(mesh="H", method="faceted"))
        assert out["method"] == "faceted"
        assert getattr(feats.last_input, "meshConvertMethodType", None) == _CONV.FacetedMeshConvertMethodType
