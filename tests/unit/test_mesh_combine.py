"""Unit tests for ``mesh_combine.py`` — boolean join/cut/intersect/merge of MESH bodies.

No live Fusion. We model the slice of the mesh-combine API the tool touches — MeshCombineFeatures
(createInput -> input -> add -> feature with .bodies), the operation/algorithm enums, and MeshBody /
BRepBody fakes NAMED to match the Fusion types so the _inputs.MeshBodyRef / BodyRefList(kind="mesh")
kind discrimination branches correctly.

Pinned (the DoD):
  • each operation (join/cut/intersect/merge) resolves the right MeshCombineOperationTypes enum and
    calls createInput(target, [tools]).
  • the tools LIST rejects a BRep handle (MeshBodyRef redirect) BEFORE any mutation.
  • the same-body guard (target also in tools) is rejected.
  • the combine routes through run_in_base_feature — a parametric design opens a base-feature scope
    (startEdit/finishEdit) around the add(); a direct design opens NONE.
  • a bad operation is rejected by the Choice input.
  • algorithm default is enhanced; the add() mutation is NOT swallowed by safe().
  • the target/tool NAMES are captured before the combine consumes the tool bodies, and the reported
    mode is the DESIGN's own mode - not an inference from a feature object the scope suppresses.
"""

import types

import adsk.fusion
import pytest

from conftest import (BRepBody, FakeBaseFeature, FakeBaseFeatures, FakeFeatures, MakeComp,
                      MakeDesign, MeshBody, _NamedCollection, body_proxy, go_stale, install,
                      load_tool, make_bbox, make_source_document, payload)

mc = load_tool("mesh_combine")

# Measured combine enums (seeded from live_api_facts) - fakes and assertions speak these.
_OT = adsk.fusion.MeshCombineOperationTypes
_AT = adsk.fusion.MeshCombineAlgorithmTypes


# ── mesh-combine feature fakes (createInput -> input ; add -> feature with .bodies) ──────────────

class _CombineInput:
    def __init__(self, target, tools):
        self.target = target
        self.tools = tools
        self.meshCombineOperationType = None
        self.algorithmType = None


class _CoercedAlgorithmInput(_CombineInput):
    """The coercion the API documents: algorithmType "is only effective in non-parametric mode - in
    parametric mode the algorithm type is always LegacyMeshCombineAlgorithmType". The assignment is
    accepted and the property reads back Legacy whatever was asked for."""

    @property
    def algorithmType(self):
        return _AT.LegacyMeshCombineAlgorithmType

    @algorithmType.setter
    def algorithmType(self, value):
        self.asked = value


class _SwallowedAlgorithmInput(_CombineInput):
    """A SWIG proxy accepting an assignment to a name it does not really define: the value lands
    nowhere and the property keeps what the API already held - here Enhanced. The set-and-read-back
    fails for a reason that is NOT the parametric coercion, so nothing ran legacy."""

    @property
    def algorithmType(self):
        return _AT.EnhancedMeshCombineAlgorithmType

    @algorithmType.setter
    def algorithmType(self, value):
        pass


class _UnreadableAlgorithmInput(_CombineInput):
    """An input whose algorithmType cannot be read at all - there is nothing to map back, so which
    algorithm ran is genuinely unknown."""

    @property
    def algorithmType(self):
        raise RuntimeError("3 : algorithmType is unavailable")

    @algorithmType.setter
    def algorithmType(self, value):
        pass


class _NoneAlgorithmInput(_CombineInput):
    """An input whose algorithmType READS successfully and hands back None. A read that returned a
    value is not a read that failed, so the two must not collapse onto the same sentence."""

    @property
    def algorithmType(self):
        return None

    @algorithmType.setter
    def algorithmType(self, value):
        pass


class _StrangeAlgorithmInput(_CombineInput):
    """An input whose algorithmType reads a value belonging to no member of the family - a read
    that succeeded and still decodes to nothing, which is a different fact from a read that failed."""

    @property
    def algorithmType(self):
        return 99

    @algorithmType.setter
    def algorithmType(self, value):
        pass


class _FeatureResult:
    def __init__(self, name, bodies):
        self.name = name
        self.bodies = _NamedCollection(bodies)


class _MeshCombineFeatures:
    """comp.features.meshCombineFeatures — createInput(target, list) then add(input) -> feature whose
    .bodies hold the result. raise_on_add forces a mutation failure (must surface, not be swallowed).
    input_factory swaps in an input whose algorithmType behaves like one of the measured proxy
    shapes (coerced / swallowed / unreadable). tri_after is the triangle count a landed combine
    leaves on the target and on its result bodies; None is a combine that moved nothing."""
    def __init__(self, result_bodies, feat_name="MeshCombine1", raise_on_add=False, none_feature=False,
                 input_factory=_CombineInput, tri_after=1600):
        self._result_bodies = result_bodies
        self._feat_name = feat_name
        self.raise_on_add = raise_on_add
        self.none_feature = none_feature
        self._input_factory = input_factory
        self.tri_after = tri_after
        self.last_input = None
        self.create_args = None

    def createInput(self, target, tools):
        self.create_args = (target, tools)
        self.last_input = self._input_factory(target, tools)
        return self.last_input

    def add(self, inp):
        if self.raise_on_add:
            raise RuntimeError("combine failed")
        if self.tri_after is not None:
            for m in [inp.target] + list(self._result_bodies):
                m.displayMesh.triangleCount = self.tri_after
        # A real combine CONSUMES the tool bodies, so their wrappers stop answering the GEOMETRY
        # reads a reach comparison needs. Anything the payload says about where the inputs sat must
        # therefore be measured BEFORE the add - run afterwards it reads no box at all and the
        # warning silently disappears. (The identity reads are staled by the test that covers them.)
        go_stale(inp.target, *inp.tools, attrs=("parentComponent", "boundingBox"))
        if self.none_feature:
            return None
        return _FeatureResult(self._feat_name, self._result_bodies)


class _Features(FakeFeatures):
    """comp.features plus the mesh-combine collection this tool reaches through."""
    def __init__(self, mesh_combine=None, base_features=None):
        super().__init__(base_features=base_features)
        self.meshCombineFeatures = mesh_combine


@pytest.fixture(autouse=True)
def _fusion_types(monkeypatch):
    """The adsk.fusion type identities the tool and the body-ref kinds branch on."""
    monkeypatch.setattr(adsk.fusion, "MeshBody", MeshBody, raising=False)
    monkeypatch.setattr(adsk.fusion, "BRepBody", BRepBody, raising=False)
    monkeypatch.setattr(adsk.fusion, "BaseFeature", FakeBaseFeature, raising=False)


def _component(name, features, mesh_bodies=None, parent_design=None):
    """A component carrying the mesh-combine feature collection and its mesh bodies."""
    comp = MakeComp(name, mesh_bodies=[], parent_design=parent_design)
    comp.features = features
    if mesh_bodies is not None:
        # comp.meshBodies - the non-parametric side-effect probe reads its .count.
        comp.meshBodies = mesh_bodies
    return comp


def _build(design_type=0, raise_on_add=False, none_feature=False, result_name="Result",
           mesh_bodies=None, input_factory=_CombineInput, lands=True):
    """A target mesh + two tool meshes in a component with a wired mesh-combine feature collection.
    Returns (design, feats, target, tool_a, tool_b); lands=False is a combine that moves nothing."""
    result = MeshBody(result_name)
    feats = _MeshCombineFeatures([result], raise_on_add=raise_on_add, none_feature=none_feature,
                                 input_factory=input_factory, tri_after=1600 if lands else None)
    bf = FakeBaseFeature()
    comp = _component("Comp", _Features(mesh_combine=feats, base_features=FakeBaseFeatures(made=bf)),
                      mesh_bodies=mesh_bodies)
    target = MeshBody("Target", parent=comp)
    tool_a = MeshBody("ToolA", parent=comp)
    tool_b = MeshBody("ToolB", parent=comp)
    des = MakeDesign(comp=comp, design_type=design_type,
                     tokens={"T": target, "A": tool_a, "B": tool_b})
    des._bf = bf
    install(mc, des)
    return des, feats, target, tool_a, tool_b


# ── each operation resolves the right enum + calls createInput(target, [tools]) ─────────────────

class TestOperations:
    def test_join(self):
        des, feats, *_ = _build()
        out = payload(mc.handler(target="T", tools=["A"], operation="join"))
        assert out["combined"] is True and out["operation"] == "join"
        assert feats.last_input.meshCombineOperationType == _OT.JoinMeshCombineType
        # createInput got (target, list[MeshBody])
        tgt, tools = feats.create_args
        assert tgt.name == "Target"
        assert isinstance(tools, list) and [t.name for t in tools] == ["ToolA"]

    def test_cut(self):
        des, feats, *_ = _build()
        payload(mc.handler(target="T", tools=["A"], operation="cut"))
        assert feats.last_input.meshCombineOperationType == _OT.CutMeshCombineType

    def test_intersect(self):
        des, feats, *_ = _build()
        payload(mc.handler(target="T", tools=["A"], operation="intersect"))
        assert feats.last_input.meshCombineOperationType == _OT.IntersectMeshCombineType

    def test_merge(self):
        des, feats, *_ = _build()
        payload(mc.handler(target="T", tools=["A"], operation="merge"))
        assert feats.last_input.meshCombineOperationType == _OT.MergeMeshCombineType

    def test_multiple_tool_bodies(self):
        des, feats, *_ = _build()
        out = payload(mc.handler(target="T", tools=["A", "B"]))
        assert out["tools"] == ["ToolA", "ToolB"]
        _, tools = feats.create_args
        assert [t.name for t in tools] == ["ToolA", "ToolB"]

    def test_comma_string_tools_parsed(self):
        des, feats, *_ = _build()
        out = payload(mc.handler(target="T", tools="A, B"))
        _, tools = feats.create_args
        assert [t.name for t in tools] == ["ToolA", "ToolB"]


# ── algorithm: default enhanced; explicit legacy ────────────────────────────────────────────────

class TestAlgorithm:
    def test_default_enhanced(self):
        des, feats, *_ = _build()
        out = payload(mc.handler(target="T", tools=["A"]))
        assert out["algorithm"] == "enhanced"
        assert feats.last_input.algorithmType == _AT.EnhancedMeshCombineAlgorithmType

    def test_legacy(self):
        des, feats, *_ = _build()
        out = payload(mc.handler(target="T", tools=["A"], algorithm="legacy"))
        assert out["algorithm"] == "legacy"
        assert feats.last_input.algorithmType == _AT.LegacyMeshCombineAlgorithmType

    def test_the_documented_coercion_is_reported_from_the_read_back(self):
        # parametric mode forces Legacy whatever was asked. That is not a failure to report - but
        # 'legacy' has to come from reading algorithmType, never from assuming the doc applies.
        des, feats, *_ = _build(input_factory=_CoercedAlgorithmInput)
        out = payload(mc.handler(target="T", tools=["A"], algorithm="enhanced"))
        assert out["algorithm"] == "legacy"
        assert "algorithm_unverified" not in out

    def test_a_swallowed_write_publishes_what_the_input_holds_not_legacy(self):
        # the set-and-read-back can fail for reasons that are NOT the coercion - a proxy that
        # accepts an assignment to a name it does not define keeps the API's own Enhanced. Naming
        # 'legacy' here would publish a fact about the toolpath that nothing measured.
        des, feats, *_ = _build(input_factory=_SwallowedAlgorithmInput)
        out = payload(mc.handler(target="T", tools=["A"], algorithm="legacy"))
        assert out["algorithm"] == "enhanced"
        assert out["algorithm"] != "legacy"
        assert "algorithm_unverified" not in out

    # The three ways the algorithm comes back unknown are DIFFERENT facts, and one shared sentence
    # would state the wrong one twice: the family being absent means nothing was ever assigned, a
    # read that raised means the property is unreachable, and a value matching no member means the
    # read SUCCEEDED and still decoded to nothing.
    def test_an_unreadable_algorithm_is_null_and_says_the_read_failed(self):
        des, feats, *_ = _build(input_factory=_UnreadableAlgorithmInput)
        out = payload(mc.handler(target="T", tools=["A"], algorithm="legacy"))
        assert out["algorithm"] is None
        assert "algorithmType could not be read back" in out["algorithm_unverified"]
        assert "not available on this Fusion version, so the algorithm was never set" not in \
            out["algorithm_unverified"]

    def test_a_build_without_the_algorithm_family_says_nothing_was_ever_set(self, monkeypatch):
        # no MeshCombineAlgorithmTypes at all: there is no member to set and none to map back, so
        # the algorithm is unknown - claiming legacy would invent a fact about a missing enum, and
        # blaming a failed read-back would blame a read that was never attempted.
        des, feats, *_ = _build()
        monkeypatch.delattr(mc.adsk.fusion, "MeshCombineAlgorithmTypes", raising=False)
        out = payload(mc.handler(target="T", tools=["A"], algorithm="legacy"))
        assert out["algorithm"] is None
        assert "the algorithm was never set and cannot be decoded" in out["algorithm_unverified"]
        assert "could not be read back" not in out["algorithm_unverified"]

    def test_a_value_matching_no_member_says_the_read_decoded_to_nothing(self):
        des, feats, *_ = _build(input_factory=_StrangeAlgorithmInput)
        out = payload(mc.handler(target="T", tools=["A"], algorithm="legacy"))
        assert out["algorithm"] is None
        assert "matching no member of MeshCombineAlgorithmTypes" in out["algorithm_unverified"]
        assert "could not be read back" not in out["algorithm_unverified"]

    def test_a_property_that_reads_none_is_a_read_that_worked_not_one_that_failed(self):
        # None is a VALUE the property handed back, so the reason names the decode, not the read -
        # testing the read's result against None instead of a sentinel merges the two.
        des, feats, *_ = _build(input_factory=_NoneAlgorithmInput)
        out = payload(mc.handler(target="T", tools=["A"], algorithm="legacy"))
        assert out["algorithm"] is None
        assert "matching no member of MeshCombineAlgorithmTypes" in out["algorithm_unverified"]
        assert "could not be read back" not in out["algorithm_unverified"]

    def test_a_clean_set_reports_no_unverified_marker(self):
        des, feats, *_ = _build()
        out = payload(mc.handler(target="T", tools=["A"], algorithm="enhanced"))
        assert out["algorithm"] == "enhanced" and "algorithm_unverified" not in out


# ── the no-op gate: unchanged body count + unchanged target triangles = error ────────────────────

class TestNoOpGate:
    def test_unchanged_target_bites(self):
        # add() returned (non-parametric None) but the target's triangle count AND the component's
        # mesh body count are identical - nothing was combined, so ok would be a false success.
        des, feats, target, tool_a, _ = _build(none_feature=True, lands=False)
        target.displayMesh.triangleCount = 1000
        res = mc.handler(target="T", tools=["A"], operation="cut")
        assert res["isError"] is True
        assert "unchanged" in res["message"]

    def test_target_triangle_change_passes(self):
        des, feats, target, tool_a, _ = _build(none_feature=True, lands=False)
        target.displayMesh.triangleCount = 1000
        feats_add = feats.add

        def add_and_mutate(inp):
            target.displayMesh.triangleCount = 1600     # the cut actually landed in the target
            return feats_add(inp)

        feats.add = add_and_mutate
        out = payload(mc.handler(target="T", tools=["A"], operation="cut"))
        assert out["combined"] is True


# ── the tools LIST rejects a BRep handle (redirect) BEFORE any mutation ──────────────────────────

class TestMeshKindEnforcement:
    def test_brep_in_tools_list_redirected_no_mutation(self):
        des, feats, target, *_ = _build()
        brep = BRepBody("SolidBody", is_solid=True)
        des._tokens["BR"] = brep
        res = mc.handler(target="T", tools=["A", "BR"])
        assert res["isError"] is True
        assert "must be a MESH body" in res["message"]
        assert "SOLID body" in res["message"]
        # nothing was created/added — the kind gate fired before any mutation
        assert feats.create_args is None and feats.last_input is None

    def test_brep_target_redirected(self):
        des, feats, *_ = _build()
        brep = BRepBody("SolidTarget", is_solid=True)
        des._tokens["BR"] = brep
        res = mc.handler(target="BR", tools=["A"])
        assert res["isError"] is True and "must be a MESH body" in res["message"]
        assert feats.create_args is None


# ── same-body guard ─────────────────────────────────────────────────────────────────────────────

class TestSameBodyGuard:
    def test_target_in_tools_rejected(self):
        des, feats, *_ = _build()
        res = mc.handler(target="T", tools=["T"])
        assert res["isError"] is True and "same as the target" in res["message"]
        assert feats.create_args is None      # rejected before any mutation

    def test_rejected_when_the_two_references_are_distinct_wrappers(self):
        # target and tools resolve through separate reads, so they are different Python objects
        # sharing one entityToken (measured). Identity alone reads False and lets a mesh be combined
        # into itself; the token clause is what actually fires here.
        des, feats, target, *_ = _build()
        # two handles onto the SAME mesh: DISTINCT objects carrying one entityToken, which is what
        # two reads of one body give live
        twin = MeshBody("Target", token=target.entityToken, parent=target.parentComponent)
        assert twin is not target and twin.entityToken == target.entityToken
        des._tokens = {"T": target, "T2": twin}
        res = mc.handler(target="T", tools=["T2"])
        assert res["isError"] is True and "same as the target" in res["message"]
        assert feats.create_args is None

    def test_rejected_when_the_tool_is_an_occurrence_PROXY_of_the_target(self):
        # The pair a wrapper's OWN token cannot see: a body and its occurrence proxy carry DIFFERENT
        # entityTokens (measured), so a bare-token guard reads one body as two and combines it into
        # itself. The guard reads the NATIVE token, which both wrappers answer with.
        des, feats, target, *_ = _build()
        proxy = body_proxy(target, types.SimpleNamespace(name="Jaw:1", fullPathName="Jaw:1"))
        assert proxy.entityToken != target.entityToken and proxy.nativeObject is target
        des._tokens = {"T": target, "P": proxy}
        res = mc.handler(target="T", tools=["P"])
        assert res["isError"] is True and "same as the target" in res["message"]
        assert feats.create_args is None

    def test_rejected_when_the_TARGET_is_the_proxy_and_the_tool_the_native(self):
        # the REVERSED arrangement: the proxy on the target side, the native on the tool side. The
        # guard must read the native token on BOTH sides - a bare-token read on the target side
        # alone misses this pair.
        des, feats, target, *_ = _build()
        proxy = body_proxy(target, types.SimpleNamespace(name="Jaw:1", fullPathName="Jaw:1"))
        des._tokens = {"P": proxy, "T": target}
        res = mc.handler(target="P", tools=["T"])
        assert res["isError"] is True and "same as the target" in res["message"]
        assert feats.create_args is None


class TestXrefMeshesAreNotTheSameBody:
    """Two DISTINCT meshes, one in each of two x-ref'd documents, answering ONE entityToken.

    An entityToken is DOCUMENT-LOCAL (measured on a host holding two x-refs of one design: the two
    'Frame' bodies read byte-identical tokens), so a same-body guard keyed on the token alone refuses
    a legitimate combine of two different meshes - and from the host document the caller can do
    nothing about either token."""

    _URN_A = "urn:adsk.wipprod:dm.lineage:K3I2nkywRlaWPHJexysOdA"
    _URN_B = "urn:adsk.wipprod:dm.lineage:N_QoPrrrSJmF__f9BZV86A"
    _SHARED_TOKEN = "/vB+AAEAAwAAAAAAAAAAAAAA"

    def _component_in_document(self, name, urn, feats):
        # parent_design is the chain a body's source document is read through
        return _component(name, _Features(mesh_combine=feats,
                                          base_features=FakeBaseFeatures(made=FakeBaseFeature())),
                          parent_design=make_source_document(urn))

    def _two_xrefs(self):
        feats = _MeshCombineFeatures([MeshBody("Result")])
        comp_a = self._component_in_document("P2a-Gimbal", self._URN_A, feats)
        comp_b = self._component_in_document("P3-Gimbal", self._URN_B, feats)
        target = MeshBody("Frame", token=self._SHARED_TOKEN, parent=comp_a)
        tool = MeshBody("Frame", token=self._SHARED_TOKEN, parent=comp_b)
        install(mc, MakeDesign(comp=comp_a, design_type=0, tokens={"T": target, "X": tool}))
        return feats, target, tool

    def test_the_x_ref_fixture_really_models_the_collision(self):
        # Without the token collision the guard was never going to fire; without two documents there
        # is nothing to tell the two meshes apart by.
        _feats, target, tool = self._two_xrefs()
        assert target is not tool and target.entityToken == tool.entityToken
        assert (target.parentComponent.parentDesign.parentDocument.dataFile.id
                != tool.parentComponent.parentDesign.parentDocument.dataFile.id)

    def test_a_combine_of_the_two_is_NOT_refused_as_a_self_combine(self):
        feats, _target, _tool = self._two_xrefs()
        res = mc.handler(target="T", tools=["X"], operation="join")
        assert "same as the target" not in (res.get("message") or "")
        assert feats.create_args is not None          # the combine actually ran

    def test_a_genuine_self_combine_INSIDE_one_document_is_still_refused(self):
        # The other direction: the document half must not weaken the guard where it is right - two
        # references reading one token AND one source document are one mesh.
        feats, target, _tool = self._two_xrefs()
        twin = MeshBody("Frame", token=target.entityToken, parent=target.parentComponent)
        install(mc, MakeDesign(comp=target.parentComponent, design_type=0,
                               tokens={"T": target, "T2": twin}))
        res = mc.handler(target="T", tools=["T2"])
        assert res["isError"] is True and "same as the target" in res["message"]
        assert feats.create_args is None

    def test_two_meshes_with_NO_readable_identity_are_not_taken_for_one_body(self):
        # An unreadable identity is None, and two Nones are not a match. Without the truthiness gate
        # on both keys these two DISTINCT meshes compare equal and get the same false self-combine
        # refusal - reached through the unreadable path instead of the x-ref one.
        des, feats, target, tool_a, _tool_b = _build()
        del target.entityToken
        del tool_a.entityToken
        res = mc.handler(target="T", tools=["A"], operation="join")
        assert "same as the target" not in (res.get("message") or "")
        assert feats.create_args is not None

    def test_a_native_and_its_PROXY_inside_a_saved_document_are_still_refused(self):
        feats, target, _tool = self._two_xrefs()
        proxy = body_proxy(target, types.SimpleNamespace(name="P2a-Gimbal:1",
                                                         fullPathName="P2a-Gimbal:1"))
        assert proxy.entityToken != target.entityToken
        install(mc, MakeDesign(comp=target.parentComponent, design_type=0,
                               tokens={"T": target, "P": proxy}))
        res = mc.handler(target="T", tools=["P"])
        assert res["isError"] is True and "same as the target" in res["message"]
        assert feats.create_args is None


# ── base-feature routing: parametric opens a scope, direct opens none ───────────────────────────

class TestBaseFeatureRouting:
    def test_direct_no_scope(self):
        des, feats, *_ = _build(design_type=0)         # direct
        out = payload(mc.handler(target="T", tools=["A"]))
        assert out["combined"] is True
        # no base-feature scope opened in direct mode
        assert des._bf._starts == 0 and des._bf._finishes == 0

    def test_parametric_opens_and_finishes_scope(self):
        des, feats, *_ = _build(design_type=1)         # parametric
        out = payload(mc.handler(target="T", tools=["A"]))
        assert out["combined"] is True
        # the add() ran INSIDE an atomic base-feature scope (opened then finished)
        assert des._bf._starts == 1 and des._bf._finishes == 1


# ── Choice rejects a bad operation ──────────────────────────────────────────────────────────────

class TestChoiceGuards:
    def test_bad_operation_rejected(self):
        des, feats, *_ = _build()
        res = mc.handler(target="T", tools=["A"], operation="weld")
        assert res["isError"] is True
        assert "operation" in res["message"] and "weld" in res["message"]
        assert feats.create_args is None      # rejected before any mutation

    def test_bad_algorithm_rejected(self):
        des, feats, *_ = _build()
        res = mc.handler(target="T", tools=["A"], algorithm="turbo")
        assert res["isError"] is True and "algorithm" in res["message"]


# ── the add() mutation is NOT swallowed by safe() ───────────────────────────────────────────────

class TestMutationSurfaces:
    def test_add_failure_surfaces(self):
        des, feats, *_ = _build(raise_on_add=True)
        res = mc.handler(target="T", tools=["A"])
        assert res["isError"] is True and "failed" in res["message"].lower()

    def test_create_input_raise_surfaces(self):
        des, feats, *_ = _build()
        def _boom(target, tools):
            raise RuntimeError("create blew up")
        feats.createInput = _boom
        res = mc.handler(target="T", tools=["A"])
        assert res["isError"] is True
        assert "Could not create the mesh-combine input" in res["message"]

    def test_create_input_none_surfaces(self):
        des, feats, *_ = _build()
        feats.createInput = lambda target, tools: None
        res = mc.handler(target="T", tools=["A"])
        assert res["isError"] is True
        assert "returned nothing" in res["message"]


# ── the meshCombineFeatures collection may be absent on a given design ───────────────────────────

class TestNoCollection:
    def test_missing_mesh_combine_features_errors(self):
        # the component has no meshCombineFeatures collection -> honest error before any work
        comp = _component("Comp", _Features(mesh_combine=None))
        target = MeshBody("Target", parent=comp)
        tool_a = MeshBody("ToolA", parent=comp)
        install(mc, MakeDesign(comp=comp, design_type=0, tokens={"T": target, "A": tool_a}))
        res = mc.handler(target="T", tools=["A"])
        assert res["isError"] is True
        assert "no meshCombineFeatures collection" in res["message"]


# ── REGRESSION: a None add() return is SUCCESS, not a failure ───────────────────────────────────
# add() "Return nothing in the case where the feature is non-parametric" (a DIRECT design OR an add
# inside the BaseFeature scope). A None return must report SUCCESS via the target mesh, not error -
# and it says nothing about the DESIGN's mode: a parametric design gets the scope, so its combine
# also comes back featureless.

class TestNoneFeatureSuccess:
    def test_none_feature_is_success_via_target_body(self):
        # add() returns None -> SUCCESS, reported against the target mesh body.
        des, feats, target, *_ = _build(none_feature=True)
        out = payload(mc.handler(target="T", tools=["A"]))
        assert out["combined"] is True
        assert out["feature"] is None
        # the result is observed on the TARGET (combine lands in place), not via the None feature
        assert out["result_bodies"][0]["name"] == "Target"
        assert out["result_bodies"][0]["handle"] == "MTOK::Target"

    def test_none_feature_in_parametric_scope_is_success(self):
        # PARAMETRIC: the scoped add still returns None -> SUCCESS, and the base-feature scope was
        # opened/closed around it.
        des, feats, *_ = _build(design_type=1, none_feature=True)
        out = payload(mc.handler(target="T", tools=["A"]))
        assert out["combined"] is True and out["feature"] is None
        assert des._bf._starts == 1 and des._bf._finishes == 1


# ── the reported mode is the DESIGN's, read before the scope opens ──────────────────────────────
#
# The BaseFeature scope a PARAMETRIC design requires suppresses the feature object, so a featureless
# add() supports no claim about the design's mode - only a read of the design itself does.

class TestNoteStatesTheReach:
    def test_the_note_names_the_component_reach(self):
        # The reach of the write, on the wire where the caller meets it: the feature is built on
        # the component that owns the target, so every occurrence placing that component shows the
        # change - which the payload's names and counts do not say.
        _build(design_type=1)
        out = payload(mc.handler(target="T", tools=["A"]))
        assert "EVERY instance" in out["note"] and "COMPONENT" in out["note"]
        assert "where that occurrence places it" in out["note"]


class TestDesignModeReported:
    def test_parametric_design_reports_parametric_with_a_null_feature(self):
        des, feats, *_ = _build(design_type=1, none_feature=True)
        out = payload(mc.handler(target="T", tools=["A"]))
        assert out["design_mode"] == "parametric"      # the design's own mode, not the add() result
        assert out["feature"] is None
        assert out["base_feature"] == "BaseFeature1"   # where the combine actually landed
        # the note explains the null feature by naming the scope THIS call opened - it claims
        # nothing about what the same add returns in a direct design (unmeasured for this class)
        assert "BaseFeature1" in out["note"]
        assert "direct" not in out["note"].lower()

    def test_direct_design_reports_direct_and_the_shared_direct_note(self):
        des, feats, *_ = _build(design_type=0, none_feature=True)
        out = payload(mc.handler(target="T", tools=["A"]))
        assert out["design_mode"] == "direct"
        assert out["base_feature"] is None             # direct opens no scope
        # the fleet's ONE direct-mode sentence, not a locally worded claim, and it does not point
        # the caller at the null base_feature key
        assert mc._common.DIRECT_FEATURE_NOTE in out["note"]
        assert "base_feature" not in out["note"]

    def test_the_null_feature_sentence_is_the_shared_one(self):
        # the exemplar must not re-roll the sentence its siblings import: an inline copy goes stale
        # the next time the shared one is corrected.
        des, feats, *_ = _build(design_type=1, none_feature=True)
        out = payload(mc.handler(target="T", tools=["A"]))
        assert out["note"].endswith(
            mc._common.null_feature_note(des, None, "BaseFeature1", "combine"))

    def test_a_returned_feature_is_still_named(self):
        des, feats, *_ = _build(design_type=1)
        out = payload(mc.handler(target="T", tools=["A"]))
        assert out["feature"] == "MeshCombine1" and out["design_mode"] == "parametric"

    def test_mode_is_read_before_the_scope_opens(self):
        # designType reads DIRECT while a base-feature edit scope is open; a mode read taken after
        # the combine would report 'direct' for a parametric design.
        des, feats, *_ = _build(design_type=1, none_feature=True)
        bf = des._bf
        real_start = bf.startEdit

        def start_and_flip():
            des.designType = 0        # what the platform reports while the scope is open
            return real_start()

        bf.startEdit = start_and_flip
        out = payload(mc.handler(target="T", tools=["A"]))
        assert out["design_mode"] == "parametric"


# ── the names are captured BEFORE the combine consumes the tool bodies ──────────────────────────

class TestNamesCapturedBeforeMutation:
    def test_consumed_tool_names_survive_in_the_payload(self):
        # the combine consumes the tool bodies; a consumed body's wrapper is not guaranteed to still
        # answer .name, so a payload projected AFTER the add published nulls for the very bodies it
        # combined. go_stale drops exactly those identity reads.
        des, feats, target, *_ = _build(none_feature=True, lands=False)
        target.displayMesh.triangleCount = 1000
        real_add = feats.add

        def consume(inp):
            target.displayMesh.triangleCount = 1600      # the combine landed
            go_stale(inp.target, *inp.tools)
            return real_add(inp)

        feats.add = consume
        out = payload(mc.handler(target="T", tools=["A", "B"]))
        assert out["tools"] == ["ToolA", "ToolB"]
        assert out["target"] == "Target"
        # the in-place result body is reported from the same pre-mutation capture
        assert out["result_bodies"][0]["name"] == "Target"
        assert out["result_bodies"][0]["handle"] == "MTOK::Target"


# ── result bodies are reported ──────────────────────────────────────────────────────────────────

class TestResult:
    def test_reports_result_bodies(self):
        des, feats, *_ = _build(result_name="Combined")
        out = payload(mc.handler(target="T", tools=["A"]))
        assert out["result_bodies"][0]["name"] == "Combined"
        assert out["result_bodies"][0]["handle"] == "MTOK::Combined"
        assert out["target"] == "Target"

    def test_no_design_errors(self, monkeypatch):
        monkeypatch.setattr(mc._common, "design", lambda: None)
        monkeypatch.setattr(mc._inputs._common, "design", lambda: None)
        res = mc.handler(target="T", tools=["A"])
        assert res["isError"] is True and "No active design" in res["message"]


# ── a mesh join of bodies that do not touch ─────────────────────────────────────────────────────
#
# The result is ONE body still holding both shells, which the triangle/body census reads as a clean
# combine. MeshBody carries no lump or shell count to check that with (BRepBody.lumps has no mesh
# counterpart), so the tool uses the one signal a mesh body does expose: AABBs that do not overlap
# PROVE the two cannot touch.

def _boxed_mesh(name, minp, maxp, parent=None):
    """A mesh body whose world AABB spans (minp, maxp) in cm."""
    return MeshBody(name, parent=parent, bbox=make_bbox(minp, maxp))


def _build_boxed(target_box, tool_boxes, design_type=0):
    """The _build rig with AABBs on the target and each tool. Returns (design, feats)."""
    feats = _MeshCombineFeatures([MeshBody("Result")])
    bf = FakeBaseFeature()
    comp = _component("Comp", _Features(mesh_combine=feats, base_features=FakeBaseFeatures(made=bf)))
    target = _boxed_mesh("Target", *target_box, parent=comp)
    handles = {"T": target}
    for key, name, box in tool_boxes:
        handles[key] = (_boxed_mesh(name, *box, parent=comp) if box is not None
                        else MeshBody(name, parent=comp))
    des = MakeDesign(comp=comp, design_type=design_type, tokens=handles)
    install(mc, des)
    return des, feats


class TestMeshJoinThatCannotFuse:
    def test_a_tool_clear_of_the_target_is_named_with_its_gap(self):
        _build_boxed(((0, 0, 0), (1, 1, 1)), [("A", "Tensioner", ((5.4, 0, 0), (6.4, 1, 1)))])
        out = payload(mc.handler(target="T", tools=["A"], operation="join"))
        assert out["disjoint_tools"] == [{"tool": "Tensioner", "gap_cm": 4.4}]
        # a box separation is a LOWER BOUND on the clearance, not the distance to move - the wire
        # must not read as a measured clearance right before a move-the-piece remedy
        assert "'Tensioner' is at least 4.4 cm clear of the target" in out["note"]
        assert "LOWER BOUND" in out["note"]

    def test_the_warning_claims_nothing_about_the_result_body(self):
        # What was measured is that the tool cannot touch the TARGET. What the result body ends up
        # holding is NOT measured (see the chain case below), so no shell/lump claim may appear.
        _build_boxed(((0, 0, 0), (1, 1, 1)), [("A", "Far", ((9, 0, 0), (10, 1, 1)))])
        out = payload(mc.handler(target="T", tools=["A"], operation="join"))
        assert "shell" not in out["note"].lower()

    def test_a_tool_that_fused_through_another_tool_is_still_only_reported_as_clear_of_the_target(self):
        # The chain case: A touches the target, B touches A but is clear of the target. B DID fuse
        # into the result through A, so a "B is a separate shell" claim would be false; the honest
        # statement is exactly the one that was measured - B cannot touch the TARGET.
        _build_boxed(((0, 0, 0), (1, 1, 1)),
                     [("A", "Middle", ((1, 0, 0), (2, 1, 1))),
                      ("B", "Outer", ((2, 0, 0), (3, 1, 1)))])
        out = payload(mc.handler(target="T", tools=["A", "B"], operation="join"))
        assert [d["tool"] for d in out["disjoint_tools"]] == ["Outer"]
        assert "'Outer' is at least 1 cm clear of the target" in out["note"]
        assert "shell" not in out["note"].lower()
        assert "nothing of the target fused with those directly" in out["note"]

    def test_the_gap_is_measured_before_the_combine_consumes_the_tools(self):
        # The add() consumes its inputs (identity AND geometry reads stop answering). Running the
        # comparison after the mutation reads no box at all and the warning silently disappears.
        _build_boxed(((0, 0, 0), (1, 1, 1)), [("A", "Far", ((9, 0, 0), (10, 1, 1)))])
        out = payload(mc.handler(target="T", tools=["A"], operation="join"))
        assert out["disjoint_tools"] == [{"tool": "Far", "gap_cm": 8.0}]

    def test_touching_bodies_are_not_warned_about(self):
        # Boxes sharing a face: gap 0, which proves nothing against contact - a real fuse must come
        # back clean or the warning is noise on every good join.
        _build_boxed(((0, 0, 0), (1, 1, 1)), [("A", "ToolA", ((1, 0, 0), (2, 1, 1)))])
        out = payload(mc.handler(target="T", tools=["A"], operation="join"))
        assert "disjoint_tools" not in out and "WARNING" not in out["note"]

    def test_only_the_tools_that_cannot_reach_are_named(self):
        _build_boxed(((0, 0, 0), (1, 1, 1)),
                     [("A", "Near", ((0.5, 0, 0), (1.5, 1, 1))),
                      ("B", "Far", ((9, 0, 0), (10, 1, 1)))])
        out = payload(mc.handler(target="T", tools=["A", "B"], operation="join"))
        assert [d["tool"] for d in out["disjoint_tools"]] == ["Far"]
        assert "'Near'" not in out["note"]

    def test_an_unreadable_box_claims_nothing(self):
        # No AABB on the tool -> the test cannot run; silence, never a "they touch" verdict.
        _build_boxed(((0, 0, 0), (1, 1, 1)), [("A", "NoBox", None)])
        out = payload(mc.handler(target="T", tools=["A"], operation="join"))
        assert "disjoint_tools" not in out

    def test_a_cut_with_an_apart_tool_is_refused_before_the_add(self):
        # A cut/intersect consumes its tool unconditionally, so a tool whose AABB proves it cannot
        # touch the target is REFUSED up front - nothing combined, no body consumed - instead of
        # the measured silent no-op (success reported, cutter destroyed, target untouched).
        _build_boxed(((0, 0, 0), (1, 1, 1)), [("A", "Far", ((9, 0, 0), (10, 1, 1)))])
        res = mc.handler(target="T", tools=["A"], operation="cut")
        assert res["isError"] is True
        assert "REFUSED before combining" in res["message"] and "Far" in res["message"]
        assert "no body was consumed" in res["message"]
