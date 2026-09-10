"""Unit tests for the typed INPUT KINDS framework (_inputs.py).

This is the meta-layer that keeps tools from re-inventing (and mis-shaping) their inputs. The value
is that ONE declaration drives resolution + validation + schema + contract, and that a GeometryHandle
input can ONLY be a real handle constrained to the required kind (the guardrail against a hand-rolled
or wrong-kind reference resolving to the wrong entity).
Pinned: handle resolution + the require-predicate enforcement, the units/Distance scaling chain, the
schema/contract auto-generation, and resolve_inputs end-to-end.
"""

import re
import types

import pytest

from conftest import (load_tool, make_design, make_occurrence, _make_object_collection,
                      _MeshBodies, _NamedCollection, BRepBody, BRepEdge, BRepFace, Circle3D,
                      Cylinder, FakeBaseFeature, FakeOccurrence, FakePoint, FakeTimelineObject,
                      FakeVector3D, Line3D, MakeComp,
                      MakeDesign, MeshBody, Plane, Profile, Sketch, body_proxy, entity_proxy,
                      make_source_document)

inp = load_tool("_inputs")


# ── fakes for entity resolution ─────────────────────────────────────────────
#
# Faces and edges are the shared BRepFace/BRepEdge; each surface/curve fake carries its own measured
# surfaceType/curveType, so a kind predicate reads the real constant rather than a hand-wired one.

_UP = FakeVector3D(0, 0, 1)


def FakePlanarFace(centroid=None, context=None):
    """A planar face. `centroid` is the position a composite handle's locator re-finds it by, and
    `context` the assembly path an ambiguity refusal names it by."""
    return BRepFace(Plane(_UP), centroid=None if centroid is None else FakePoint(*centroid),
                    assembly_context=None if context is None
                    else types.SimpleNamespace(fullPathName=context))


def FakeCylFace():
    return BRepFace(Cylinder(_UP))


def FakeEdge():
    return BRepEdge(Line3D(FakePoint(0, 0, 0), FakePoint(1, 0, 0)))


def _install(token_map):
    """Install a fake design whose findEntityByToken resolves tokens from token_map, and wire the
    adsk isinstance types the kinds check against (SurfaceTypes ints come seeded)."""
    import adsk.fusion
    adsk.fusion.BRepFace = BRepFace
    adsk.fusion.BRepEdge = BRepEdge
    adsk.fusion.BRepVertex = type("V", (), {})

    # _inputs calls _common.design(); patch it. The requirement predicates read surfaceType live on
    # every call, so matching enum values is all they need.
    design = make_design(tokens=token_map)
    inp._common.design = lambda: design


# ── GeometryHandle: the guardrail ───────────────────────────────────────────

class TestGeometryHandle:
    def test_resolves_planar_face(self):
        f = FakePlanarFace()
        _install({"TOK": f})
        k = inp.GeometryHandle("on_face", require="planar_face")
        val, err = k.resolve("TOK")
        assert err is None and val is f

    def test_rejects_wrong_geometry_kind(self):
        # a cylinder face handed to a planar_face input -> clear error, not a crash
        _install({"TOK": FakeCylFace()})
        k = inp.GeometryHandle("on_face", require="planar_face")
        val, err = k.resolve("TOK")
        assert val is None and "must be a PLANAR face" in err

    def test_stale_handle_error(self):
        _install({})   # token not in map -> doesn't resolve
        k = inp.GeometryHandle("h", require="any", required=True)
        val, err = k.resolve("GONE")
        assert val is None and "stale" in err.lower()
        # The error must steer the agent to RE-FIND. With the self-healing composite handle, the
        # entityToken AND its geometry-locator fallback both failed here, so the message reports the
        # locator-recovery failure (not just a dead token) before pointing back at find_geometry.
        assert "find_geometry" in err
        assert "locator" in err.lower()

    def test_contract_note_names_the_required_kind(self):
        k = inp.GeometryHandle("on_face", require="planar_face")
        note = k.contract_note()
        # The note must name the required kind and point at find_geometry (the handle source).
        assert "planar" in note.lower() and "find_geometry" in note

    def test_schema_includes_contract_note(self):
        k = inp.GeometryHandle("on_face", require="cylinder_face", description="The pin face.")
        sch = k.schema()
        assert sch["type"] == "string"
        assert "CYLINDRICAL" in sch["description"] and "The pin face." in sch["description"]


# ── self-healing composite handle: stale token recovers via the kind+position locator ──────────────
# Live failure mode: find_geometry returns N handles; an older one's entityToken goes stale
# (Fusion mints a different token per query) and findEntityByToken returns nothing — even with NO model
# edit. The composite handle '<token>|@<kind>:<x>,<y>,<z>' lets resolution re-find the SAME geometry by
# its kind+position when the token is dead, so the caller never has to re-query.

def _HealFace(centroid):
    """A planar face at a known centroid - what the locator re-finds when the token is dead."""
    return FakePlanarFace(centroid=centroid)


def _install_with_bodies(faces, token_map):
    """A design whose findEntityByToken uses token_map AND whose rootComponent carries bodies/faces so
    _refind_by_locator can scan them (the locator-fallback path)."""
    import adsk.fusion
    adsk.fusion.BRepFace = BRepFace
    adsk.fusion.BRepEdge = BRepEdge
    adsk.fusion.BRepVertex = type("V", (), {})

    body = BRepBody(name="Body1", faces=faces)
    body.edges = _NamedCollection([])
    root = MakeComp(name="Root", bodies=[body])
    design = MakeDesign(comp=root, tokens=token_map)
    inp._common.design = lambda: design


class TestIsHandle:
    """is_handle distinguishes a handle (entityToken) from an int/index/'all' for dual-accept inputs."""
    def test_composite_handle(self):
        assert inp.is_handle(f"tok{inp._HANDLE_SEP}profile:0.4,0.2,0.0") is True

    def test_long_bare_token(self):
        assert inp.is_handle("/v4BAAAARlJLZXk" + "Z" * 40) is True

    def test_int_and_index_selectors_are_not_handles(self):
        assert inp.is_handle(0) is False
        assert inp.is_handle("0,2,3") is False
        assert inp.is_handle("all") is False
        assert inp.is_handle([0, 1]) is False
        assert inp.is_handle("") is False


class TestSelfHealingHandle:
    def test_live_token_resolves_via_fast_path(self):
        f = _HealFace((1.0, 2.0, 3.0))
        _install_with_bodies([f], token_map={"TOK": f})
        handle = f"TOK{inp._HANDLE_SEP}planar_face:1.0,2.0,3.0"
        val, err = inp.GeometryHandle("on_face", require="planar_face").resolve(handle)
        assert err is None and val is f          # token resolved directly

    def test_stale_token_recovers_via_locator(self):
        # token 'DEAD' is NOT in the map (stale), but a face sits exactly at the locator position ->
        # _refind_by_locator finds it and resolution succeeds WITHOUT re-querying.
        f = _HealFace((1.0, 2.0, 3.0))
        _install_with_bodies([f], token_map={})   # no token resolves
        handle = f"DEAD{inp._HANDLE_SEP}planar_face:1.0,2.0,3.0"
        val, err = inp.GeometryHandle("on_face", require="planar_face").resolve(handle)
        assert err is None and val is f          # recovered by geometry locator

    def test_stale_token_no_matching_geometry_errors(self):
        # token dead AND no face near the locator -> honest failure (don't bind the wrong entity).
        f = _HealFace((50.0, 50.0, 50.0))         # far from the locator
        _install_with_bodies([f], token_map={})
        handle = f"DEAD{inp._HANDLE_SEP}planar_face:1.0,2.0,3.0"
        val, err = inp.GeometryHandle("on_face", require="planar_face").resolve(handle)
        assert val is None and "find_geometry" in err

    def test_bare_token_still_works(self):
        # backward-compat: a handle with NO locator suffix resolves exactly as before.
        f = FakePlanarFace()
        _install({"BARE": f})
        val, err = inp.GeometryHandle("on_face", require="planar_face").resolve("BARE")
        assert err is None and val is f

    def test_make_handle_stamps_body_revision(self):
        # BRep entities carry their body's revisionId so locator recovery can verify identity.
        ent = type("E", (), {"entityToken": "TOK",
                             "body": type("B", (), {"revisionId": "REV7"})()})()
        h = inp.make_handle(ent, "cylinder_face", (1.0, 2.0, 3.0))
        assert h.endswith(";rv=REV7") and "|@cylinder_face:1.000000,2.000000,3.000000" in h

    def test_split_handle_parses_revision_and_legacy(self):
        tok, loc = inp._split_handle(f"T{inp._HANDLE_SEP}cylinder_face:1.0,2.0,3.0;rv=REV7")
        assert tok == "T" and loc == ("cylinder_face", 1.0, 2.0, 3.0, "REV7")
        tok, loc = inp._split_handle(f"T{inp._HANDLE_SEP}cylinder_face:1.0,2.0,3.0")
        assert tok == "T" and loc == ("cylinder_face", 1.0, 2.0, 3.0, None)

    def test_locator_recovery_with_matching_revision_succeeds(self):
        # token dead, geometry AND its body revision unchanged -> benign token rotation, recover.
        f = _HealFace((1.0, 2.0, 3.0))
        f.body = type("B", (), {"revisionId": "REV7"})()
        _install_with_bodies([f], token_map={})
        handle = f"DEAD{inp._HANDLE_SEP}planar_face:1.0,2.0,3.0;rv=REV7"
        val, err = inp.GeometryHandle("on_face", require="planar_face").resolve(handle)
        assert err is None and val is f

    def test_locator_recovery_refuses_on_changed_revision(self):
        # A delete-rebuild can put DIFFERENT geometry exactly at the recorded position (live-proven:
        # a rotated cylinder's record point landed on the deleted one's, and a relation read then
        # certified a comparison that never happened). A changed body revision = refuse, never guess.
        f = _HealFace((1.0, 2.0, 3.0))
        f.body = type("B", (), {"revisionId": "REV8-DIFFERENT"})()
        _install_with_bodies([f], token_map={})
        handle = f"DEAD{inp._HANDLE_SEP}planar_face:1.0,2.0,3.0;rv=REV7"
        val, err = inp.GeometryHandle("on_face", require="planar_face").resolve(handle)
        assert val is None
        assert "changed since" in err.lower() or "model changed" in err.lower()
        assert "find_geometry" in err


# ── ONE token, SEVERAL entities: the pick is the locator's, or the handle is refused ─────────────
#
# findEntityByToken answers with a VECTOR. Measured live: splitting a face made the pre-split token
# resolve to BOTH survivors (a straight split yields different centroids; a CONCENTRIC split puts
# both at the SAME centroid), so returning the first silently acts on geometry the caller never
# picked.

def _SplitFace(centroid, context=None):
    """A planar face at a known centroid - a survivor of a split. `context` is the assembly path an
    ambiguity refusal names each candidate by."""
    return FakePlanarFace(centroid=centroid, context=context)


@pytest.fixture
def token_env(monkeypatch):
    """A design whose findEntityByToken answers a token from a map - a LIST value models the several
    entities one token can resolve to."""
    def build(tokens):
        import adsk.fusion
        monkeypatch.setattr(adsk.fusion, "BRepFace", BRepFace, raising=False)
        design = make_design(tokens=tokens)
        monkeypatch.setattr(inp._common, "design", lambda: design)
        monkeypatch.setattr(inp._common, "target_component", lambda _d=None: design.rootComponent)
        return design
    return build


class TestTokenResolvingToSeveralEntities:
    def test_the_locator_picks_the_member_it_names_not_the_first(self, token_env):
        left, right = _SplitFace((2.5, 0.0, 0.0)), _SplitFace((7.5, 0.0, 0.0))
        token_env({"TOK": [left, right]})
        handle = f"TOK{inp._HANDLE_SEP}planar_face:7.5,0.0,0.0"
        val, err = inp.GeometryHandle("on_face", require="planar_face").resolve(handle)
        assert err is None
        assert val is right and val is not left      # the locator's member, never found[0]

    def test_a_locator_matching_none_of_them_is_refused_naming_the_count(self, token_env):
        # The live case: the composite handle holds the PRE-split centroid, which matches neither
        # survivor - so the handle is stale/ambiguous and must be refused, not silently first-matched.
        left, right = _SplitFace((2.5, 0.0, 0.0)), _SplitFace((7.5, 0.0, 0.0))
        token_env({"TOK": [left, right]})
        handle = f"TOK{inp._HANDLE_SEP}planar_face:5.0,0.0,0.0"
        val, err = inp.GeometryHandle("on_face", require="planar_face").resolve(handle)
        assert val is None
        assert "2 entities" in err                   # names HOW MANY it resolved to
        assert "find_geometry" in err                # and points at the way out

    def test_a_bare_token_resolving_to_several_is_refused_saying_it_has_no_locator(self, token_env):
        token_env({"TOK": [_SplitFace((2.5, 0.0, 0.0)), _SplitFace((7.5, 0.0, 0.0))]})
        val, err = inp.GeometryHandle("on_face", require="planar_face").resolve("TOK")
        assert val is None
        assert "2 entities" in err and "no position locator" in err

    def test_the_refusal_names_each_candidate_by_its_assembly_path(self, token_env):
        # What tells the candidates of one token apart is WHERE each is placed, so the refusal
        # prints each assembly context rather than a bare count.
        token_env({"TOK": [_SplitFace((0.0, 0.0, 0.0), context="Arm:1"),
                           _SplitFace((9.0, 0.0, 0.0), context="Arm:2")]})
        val, err = inp.GeometryHandle("on_face", require="planar_face").resolve("TOK")
        assert val is None and "Arm:1" in err and "Arm:2" in err

    def test_a_candidate_exactly_at_the_locator_tolerance_is_still_the_match(self, token_env):
        # EXACT boundary of the 1-micron gate: at the tolerance the candidate IS that geometry.
        far, edge = _SplitFace((9.0, 0.0, 0.0)), _SplitFace((inp._LOCATOR_TOL_CM, 0.0, 0.0))
        token_env({"TOK": [far, edge]})
        handle = f"TOK{inp._HANDLE_SEP}planar_face:0.0,0.0,0.0"
        val, err = inp.GeometryHandle("on_face", require="planar_face").resolve(handle)
        assert err is None and val is edge

    def test_a_candidate_just_past_the_tolerance_is_refused(self, token_env):
        far = _SplitFace((9.0, 0.0, 0.0))
        beyond = _SplitFace((inp._LOCATOR_TOL_CM * 1.01, 0.0, 0.0))
        token_env({"TOK": [far, beyond]})
        handle = f"TOK{inp._HANDLE_SEP}planar_face:0.0,0.0,0.0"
        val, err = inp.GeometryHandle("on_face", require="planar_face").resolve(handle)
        assert val is None and "2 entities" in err

    def test_two_CO_LOCATED_candidates_are_refused_never_first_matched(self, token_env):
        # measured live: a CONCENTRIC face split puts BOTH survivors at the identical centroid,
        # and the hit order is not stable across runs - so a distance tie inside the tolerance
        # must refuse naming the count; "nearest" between equals is a coin flip on someone's
        # geometry.
        a, b = _SplitFace((5.0, 0.0, 0.0)), _SplitFace((5.0, 0.0, 0.0))
        token_env({"TOK": [a, b]})
        handle = f"TOK{inp._HANDLE_SEP}planar_face:5.0,0.0,0.0"
        val, err = inp.GeometryHandle("on_face", require="planar_face").resolve(handle)
        assert val is None
        assert "2 entities" in err and "co-located" in err

    def test_a_second_candidate_EXACTLY_at_the_tolerance_is_still_co_located(self, token_env):
        # The co-located COUNT is judged on the SAME 1-micron gate as the match: a second candidate
        # sitting exactly AT the tolerance is still "at the recorded position", so the pick is
        # refused. Counting it as outside would hand the handle to the nearer one on a tie the
        # tolerance itself calls a tie.
        at, edge = _SplitFace((0.0, 0.0, 0.0)), _SplitFace((inp._LOCATOR_TOL_CM, 0.0, 0.0))
        token_env({"TOK": [at, edge]})
        handle = f"TOK{inp._HANDLE_SEP}planar_face:0.0,0.0,0.0"
        val, err = inp.GeometryHandle("on_face", require="planar_face").resolve(handle)
        assert val is None and val is not at
        assert "2 of them sit at the recorded position" in err

    def test_a_target_miss_carries_the_refusal_the_CALLERS_OWN_handle_earned(self, token_env):
        # TargetRef's mirror of the BodyRef case: its step-4 _resolve_any_body re-attempt can
        # mutilate the composite handle (the ':' rsplit) and overwrite the refusal channel - the
        # early capture keeps the miss describing the caller's handle, never the mutilated prefix.
        a, b = _SplitFace((5.0, 0.0, 0.0)), _SplitFace((5.0, 0.0, 0.0))
        token_env({"TOK": [a, b]})
        handle = f"TOK{inp._HANDLE_SEP}planar_face:5.0,0.0,0.0"
        val, err = inp.TargetRef("target").resolve(handle)
        assert val is None
        assert "co-located" in err and "sit at the recorded position" in err
        assert "no position locator" not in err

    def test_one_entity_still_resolves_with_no_locator_involved(self, token_env):
        # The ordinary case must be untouched: a token naming exactly one entity resolves as before.
        only = _SplitFace((3.0, 0.0, 0.0))
        token_env({"TOK": [only]})
        val, err = inp.GeometryHandle("on_face", require="planar_face").resolve("TOK")
        assert err is None and val is only

    def test_a_name_fallback_kind_reports_the_handle_refusal_instead_of_a_miss(self, token_env):
        # TargetRef falls through to name lookups when the handle does not resolve; its miss would
        # otherwise tell the caller the string named nothing, sending it back to names when the real
        # answer is that the HANDLE names several entities.
        token_env({"TOK": [_SplitFace((0.0, 0.0, 0.0)), _SplitFace((9.0, 0.0, 0.0))]})
        val, err = inp.TargetRef("target").resolve("TOK")
        assert val is None
        assert "2 entities" in err and "find_geometry" in err

    def test_a_body_miss_carries_the_refusal_the_CALLERS_OWN_handle_earned(self, token_env):
        # The body vocabularies below the handle path rsplit on ':' - the same ':' a composite
        # handle's locator carries - and re-resolve that mutilated prefix as a BARE token. The miss
        # must report what the caller's OWN value hit (co-located candidates), never the bare
        # token's "no position locator", which describes a string the caller never passed.
        token_env({"TOK": [_SplitFace((5.0, 0.0, 0.0)), _SplitFace((5.0, 0.0, 0.0))]})
        handle = f"TOK{inp._HANDLE_SEP}planar_face:5.0,0.0,0.0"
        val, err = inp.BodyRef("body").resolve(handle)
        assert val is None
        assert inp.BODY_MISS in err and "also tried as a handle" in err
        assert "2 entities" in err and "co-located" in err
        assert "sit at the recorded position" in err
        assert "no position locator" not in err


# ── GeometryHandleList: the 'these specific edges/bodies' shape ─────────────

class TestGeometryHandleList:
    def test_resolves_list_of_edge_handles(self):
        import adsk.fusion
        e1, e2 = FakeEdge(), FakeEdge()
        _install({"E1": e1, "E2": e2})
        k = inp.GeometryHandleList("edges", require="edge")
        ents, err = k.resolve(["E1", "E2"])
        assert err is None and ents == [e1, e2]

    def test_accepts_comma_string(self):
        e1, e2 = FakeEdge(), FakeEdge()
        _install({"E1": e1, "E2": e2})
        k = inp.GeometryHandleList("edges", require="edge")
        ents, err = k.resolve("E1, E2")
        assert err is None and len(ents) == 2

    def test_one_bad_handle_fails_with_index(self):
        _install({"E1": FakeEdge()})       # E2 missing
        k = inp.GeometryHandleList("edges", require="edge")
        ents, err = k.resolve(["E1", "E2"])
        assert ents is None and "[1]" in err

    def test_wrong_kind_in_list_rejected(self):
        _install({"E1": FakeEdge(), "F1": FakePlanarFace()})
        k = inp.GeometryHandleList("edges", require="edge")
        ents, err = k.resolve(["E1", "F1"])
        assert ents is None and "must be an edge" in err

    def test_empty_optional_returns_empty_list(self):
        _install({})
        k = inp.GeometryHandleList("edges", require="edge")
        ents, err = k.resolve(None)
        assert err is None and ents == []

    def test_schema_is_array(self):
        k = inp.GeometryHandleList("edges", require="edge")
        sch = k.schema()
        assert sch["type"] == "array" and sch["items"]["type"] == "string"


# ── EdgeLoopRef: edge handles as a BOUNDARY, with the closed/open loop contract ─────────────


def _edges_on_one_body(count, token="Surface1"):
    """`count` FakeEdges of ONE body, each holding its OWN proxy - the measured shape of edge.body
    (see conftest entity_proxy). Sharing one object between edges cannot tell an entityToken dedupe
    from an id() one, which is how a per-edge body over-count hides."""
    body = BRepBody(name=token, entity_token=token)
    edges = []
    for _ in range(count):
        e = FakeEdge()
        e.body = entity_proxy(body)
        edges.append(e)
    return edges


def _install_loop(token_map):
    """Edge handles plus a real ObjectCollection.create, so EdgeLoopRef can assemble the boundary
    collection it hands to Patch/Extend."""
    import adsk.core
    _install(token_map)
    adsk.core.ObjectCollection.create = _make_object_collection


class TestEdgeLoopRef:
    def test_required_empty_errors(self):
        _install_loop({})
        k = inp.EdgeLoopRef("boundary", closed=True, required=True)
        val, err = k.resolve(None)
        assert val is None and "edge" in err and "find_geometry" in err

    def test_single_edge_is_a_legal_boundary(self):
        # a lone edge is allowed - Fusion auto-finds the connected loop from it
        e = FakeEdge()
        _install_loop({"E1": e})
        (coll, meta), err = inp.EdgeLoopRef("boundary", closed=True).resolve(["E1"])
        assert err is None
        assert meta["entities"] == [e]
        assert coll.count == 1 and coll.item(0) is e

    def test_open_chain_across_two_bodies_is_refused(self):
        # closed=False (extend / open-extrude): every edge must come from ONE surface body -
        # a multi-body chain is rejected before any mutation runs.
        e1, = _edges_on_one_body(1, token="BodyA")
        e2, = _edges_on_one_body(1, token="BodyB")
        _install_loop({"E1": e1, "E2": e2})
        val, err = inp.EdgeLoopRef("edges", closed=False).resolve(["E1", "E2"])
        assert val is None and "ONE surface body" in err

    def test_open_chain_one_body_resolves_with_body_count(self):
        # Each edge holds its OWN proxy of the one body - the measured shape of edge.body (three
        # edges of one open surface body: three python ids, ONE entityToken). Counting by identity
        # would read two bodies here and REFUSE a legal single-body chain.
        e1, e2 = _edges_on_one_body(2)
        _install_loop({"E1": e1, "E2": e2})
        (coll, meta), err = inp.EdgeLoopRef("edges", closed=False).resolve(["E1", "E2"])
        assert err is None and meta["body_count"] == 1 and coll.count == 2

    def test_open_chain_of_many_edges_on_one_body_reports_one_body(self):
        # body_count is a BODY count, not an edge count: three edges of one body report 1.
        e1, e2, e3 = _edges_on_one_body(3)
        _install_loop({"E1": e1, "E2": e2, "E3": e3})
        (coll, meta), err = inp.EdgeLoopRef("edges", closed=False).resolve(["E1", "E2", "E3"])
        assert err is None and meta["body_count"] == 1 and coll.count == 3

    def test_closed_loop_may_span_bodies_and_reports_body_count(self):
        # the single-body rule gates OPEN chains only; a closed boundary resolves, and meta
        # reports how many bodies the edges touch.
        e1, = _edges_on_one_body(1, token="BodyA")
        e2, = _edges_on_one_body(1, token="BodyB")
        _install_loop({"E1": e1, "E2": e2})
        (coll, meta), err = inp.EdgeLoopRef("boundary", closed=True).resolve(["E1", "E2"])
        assert err is None and meta["body_count"] == 2

    def test_contract_note_states_closed_vs_open(self):
        closed = inp.EdgeLoopRef("boundary", closed=True).contract_note()
        opened = inp.EdgeLoopRef("edges", closed=False).contract_note()
        assert "CLOSED loop" in closed and "one edge suffices" in closed
        assert "OPEN chain" in opened and "one surface body" in opened


# ── BodyRef: name OR handle, dispatched WITHOUT a length heuristic ──────────────────────────────
# Resolution is by what RESOLVES, not by string length: try the token first, then fall back to the
# name. (A length heuristic would mis-route a long body NAME to findEntityByToken as a stale handle.)

def FakeBody(name):
    """A BRep body, reduced to the name a by-name lookup keys on."""
    return BRepBody(name=name)


def _install_bodies(named=None, handle_map=None, components=None):
    """`named` are the ROOT component's bodies, keyed by the name they answer; `components` is
    {component name: its bodies} - or a LIST of (name, bodies) pairs where a test needs two
    components carrying ONE name, which a dict cannot express."""
    import adsk.fusion
    adsk.fusion.BRepBody = BRepBody
    named = named or {}
    handle_map = handle_map or {}
    components = components or {}

    pairs = components.items() if isinstance(components, dict) else components
    comp_objs = [MakeComp(name=n, bodies=list(bs), mesh_bodies=[]) for n, bs in pairs]
    root = MakeComp(name="Root", bodies=list(named.values()))
    # allComponents holds the named sub-components only, so a by-name walk over it never falls back
    # to the root's own bodies.
    design = MakeDesign(comp=root, all_components=comp_objs, tokens=handle_map)
    inp._common.design = lambda: design
    inp._common.target_component = lambda d: root
    return root


class TestBodyRef:
    def test_component_name_with_single_body_resolves_to_it(self):
        b = FakeBody("Body1")
        _install_bodies(components={"Frame": [b]})
        body, err = inp.BodyRef("body_name").resolve("Frame")
        assert err is None and body is b

    def test_exact_occurrence_suffix_resolves_to_its_placed_body(self, monkeypatch):
        import adsk.fusion
        monkeypatch.setattr(adsk.fusion, "BRepBody", BRepBody, raising=False)
        native = FakeBody("Body1")
        frame = MakeComp(name="Frame", bodies=[native], mesh_bodies=[])
        occ = make_occurrence(path="Frame:1", component=frame)
        proxy = body_proxy(native, occ)
        occ.bRepBodies = _NamedCollection([proxy])
        root = MakeComp(name="Root", occurrences=[occ])
        design = MakeDesign(comp=root, all_components=[frame])
        monkeypatch.setattr(inp._common, "design", lambda: design)
        monkeypatch.setattr(inp._common, "target_component", lambda d: root)
        monkeypatch.setattr(inp._common, "occurrence_walk",
                            lambda _d, cap=None: types.SimpleNamespace(occurrences=[occ]))
        body, err = inp.BodyRef("body_name").resolve("Frame:1")
        assert err is None and body is proxy and proxy is not native

    def test_nonexistent_occurrence_suffix_does_not_resolve_a_nearby_instance_or_component(self, monkeypatch):
        import adsk.fusion
        monkeypatch.setattr(adsk.fusion, "BRepBody", BRepBody, raising=False)
        native = FakeBody("Body1")
        frame = MakeComp(name="Frame", bodies=[native], mesh_bodies=[])
        longer = make_occurrence(path="Frame:10", component=frame)
        unrelated = make_occurrence(path="OtherFrame:999", component=frame)
        longer.bRepBodies = _NamedCollection([body_proxy(native, longer)])
        unrelated.bRepBodies = _NamedCollection([body_proxy(native, unrelated)])
        root = MakeComp(name="Root", occurrences=[longer, unrelated])
        design = MakeDesign(comp=root, all_components=[frame])
        monkeypatch.setattr(inp._common, "design", lambda: design)
        monkeypatch.setattr(inp._common, "target_component", lambda d: root)
        monkeypatch.setattr(inp._common, "occurrence_walk",
                            lambda _d, cap=None: types.SimpleNamespace(
                                occurrences=[longer, unrelated], broken_occurrences=[], broken=[]))
        for requested in ("Frame:1", "Frame:999"):
            body, err = inp.BodyRef("body_name").resolve(requested)
            assert body is None and requested in err

    def test_duplicate_nested_occurrence_names_refuse_the_short_name(self, monkeypatch):
        import adsk.fusion
        monkeypatch.setattr(adsk.fusion, "BRepBody", BRepBody, raising=False)
        a, b = FakeBody("Body1"), FakeBody("Body1")
        left = make_occurrence(path="SubA:1+Bolt:1",
                               component=MakeComp(name="Bolt", bodies=[a], mesh_bodies=[]))
        right = make_occurrence(path="SubB:1+Bolt:1",
                                component=MakeComp(name="Bolt", bodies=[b], mesh_bodies=[]))
        left.name = right.name = "Bolt:1"
        left_proxy, right_proxy = body_proxy(a, left), body_proxy(b, right)
        left.bRepBodies, right.bRepBodies = _NamedCollection([left_proxy]), _NamedCollection([right_proxy])
        root = MakeComp(name="Root", occurrences=[left, right])
        design = MakeDesign(comp=root)
        monkeypatch.setattr(inp._common, "design", lambda: design)
        monkeypatch.setattr(inp._common, "target_component", lambda d: root)
        monkeypatch.setattr(inp._common, "occurrence_walk",
                            lambda _d, cap=None: types.SimpleNamespace(
                                occurrences=[left, right], broken_occurrences=[], broken=[]))

        body, err = inp.BodyRef("body_name").resolve("Bolt:1")
        assert body is None and "names 2 occurrences" in err
        assert "SubA:1+Bolt:1" in err and "SubB:1+Bolt:1" in err
        body, err = inp.BodyRef("body_name").resolve("SubB:1+Bolt:1")
        assert err is None and body is right_proxy

    def test_multi_body_component_name_refuses_with_body_names(self):
        _install_bodies(components={"Frame": [FakeBody("Body1"), FakeBody("Body2")]})
        body, err = inp.BodyRef("body_name").resolve("Frame")
        assert body is None
        assert "2 bodies" in err and "Body1" in err and "Body2" in err

    def test_a_name_two_components_carry_refuses_instead_of_picking_a_body(self):
        # "that part's body" names no body when two parts answer to the name - resolving it would
        # colour/measure/cut a body in whichever component the walk reached first
        _install_bodies(components=[("Frame", [FakeBody("Body1")]),
                                    ("Frame", [FakeBody("Body1")])])
        body, err = inp.BodyRef("body_name").resolve("Frame")
        assert body is None
        assert "2 components match 'Frame'" in err
        # the same two remedies the ambiguous-BODY refusal above it offers, both still reachable
        assert "'<occurrence-or-component>:<body>'" in err and "find_geometry" in err
        assert "rename" not in err.lower()

    def test_an_ambiguous_name_is_not_rescued_by_stripping_its_colon_suffix(self):
        # 'Jaw:2' is carried by two components AND splits into a real component 'Jaw'. The ':N'
        # strip exists for the occurrence spelling ('Frame:1' -> the component 'Frame'), so it must
        # not run once the full name has already been refused: re-looking-up the prefix resolves
        # 'Jaw', CLEARS the refusal, and hands back Jaw's body for a name that named neither Jaw:2.
        _install_bodies(components=[("Jaw:2", []), ("Jaw:2", []),
                                    ("Jaw", [FakeBody("Pin")])])
        body, err = inp.BodyRef("body_name").resolve("Jaw:2")
        assert body is None
        assert "2 components match 'Jaw:2'" in err

    def test_a_qualified_prefix_two_components_carry_refuses(self):
        # 'Jaw/Pin' with two 'Jaw' components: the PREFIX names no one scope, so the body cannot be
        # read out of "the" Jaw. The '/' spelling is what pins the qualified path specifically - the
        # ':' spelling would reach the same refusal through the bare-component fallback below it,
        # while '/' has no such fallback and would otherwise deny that any 'Jaw' exists.
        _install_bodies(components=[("Jaw", [FakeBody("Pin")]), ("Jaw", [FakeBody("Pin")])])
        body, err = inp.BodyRef("body_name").resolve("Jaw/Pin")
        assert body is None
        assert "2 components match 'Jaw'" in err
        # the remedy is a prefix that DOES name one scope - _named_scope tries the occurrence
        # vocabulary first, so an instance's fullPathName resolves where the component name cannot
        assert "'<instance>:Pin'" in err

    def test_body_name_wins_over_component_name(self):
        direct = FakeBody("Frame")            # a BODY literally named Frame
        _install_bodies(named={"Frame": direct}, components={"Frame": [FakeBody("Body1")]})
        body, err = inp.BodyRef("body_name").resolve("Frame")
        assert err is None and body is direct

    def test_resolves_a_handle(self):
        b = FakeBody("B")
        _install_bodies(handle_map={"/vTOKEN": b})
        val, err = inp.BodyRef("body").resolve("/vTOKEN")
        assert err is None and val is b

    def test_face_handle_walks_to_its_owning_body(self):
        # find_geometry mints no BODY handle (only face/edge/vertex), and a body in an instanced
        # component is ambiguous by name - so a face handle MUST resolve to its owning body, which
        # is what makes the ambiguity error's "pass a find_geometry handle" advice actually true.
        owner = FakeBody("Body1")
        FakeFace = type("FakeFace", (), {"body": owner})
        _install_bodies(handle_map={"/vFACE": FakeFace()})
        val, err = inp.BodyRef("body").resolve("/vFACE")
        assert err is None and val is owner

    def test_resolves_a_short_name(self):
        b = FakeBody("Body1")
        _install_bodies(named={"Body1": b})
        val, err = inp.BodyRef("body").resolve("Body1")
        assert err is None and val is b

    def test_long_name_is_NOT_mistaken_for_a_handle(self):
        # a 61-char body name must resolve by NAME, not error as a stale handle
        long_name = "Left-Hand-Bracket-Assembly-Revision-C-DO-NOT-MACHINE-final-v2"
        assert len(long_name) > 60
        b = FakeBody(long_name)
        _install_bodies(named={long_name: b})           # NOT in handle_map
        val, err = inp.BodyRef("body").resolve(long_name)
        assert err is None and val is b

    def test_unresolvable_reports_name_guidance(self):
        _install_bodies()
        val, err = inp.BodyRef("body").resolve("Ghost")
        assert val is None and "Ghost" in err


# ── PlaneRef: the MULTI-SOURCE kind (origin alias | construction name | handle) ─────────────────

# The three origin planes an 'xy'/'xz'/'yz' alias resolves to. ConstructionPlane has no measured
# shape, so the entity a test gets back is a tagged tuple rather than a shared fake.
_ORIGIN_PLANES = (("origin", "xy"), ("origin", "xz"), ("origin", "yz"))


class FakeConstructionPlane:
    pass


class _CP:
    """A ConstructionPlane fake: its name, the component that owns it, and a
    createForAssemblyContext that returns a DISTINCT proxy tagged with its occurrence - so a test can
    tell a proxy from the native and confirm WHICH occurrence it was lifted into."""

    def __init__(self, name, component=None):
        self.name = name
        self.component = component

    def createForAssemblyContext(self, occ):
        p = _CP(self.name, self.component)
        p.native = self
        p.context = occ
        return p


def _install_planes(named=None, handle_map=None, subs=(), active=None):
    """Install a fake design exposing origin planes, construction planes on the ROOT and on each
    sub-component, and a findEntityByToken for handle resolution. PlaneRef resolves via
    _common.design()/target_component().

    `named`: {name: the object the test expects back} for the ROOT component's planes.
    `subs`: [(component name, [plane names], [occurrence fullPathNames placing it])].
    `active`: the component name PlaneRef should treat as active (default: the root).
    Returns the design; its components are reachable by name through allComponents.itemByName."""
    import adsk.fusion
    adsk.fusion.BRepFace = BRepFace
    adsk.fusion.ConstructionPlane = FakeConstructionPlane
    named = named or {}
    handle_map = handle_map or {}

    def _comp(name, planes=(), token=None):
        # Every live component answers an entityToken, and PlaneRef's assembly-context step REFUSES
        # an owner it cannot tell from the root rather than hand back a native plane Fusion would
        # reject. A test that wants that state passes token=None.
        c = MakeComp(name=name, entity_token=token, origin_planes=_ORIGIN_PLANES)
        c.constructionPlanes = _NamedCollection(list(planes))
        return c

    root = _comp("Root", token="TOKEN:Root")
    for nm, cp in named.items():
        cp.name, cp.component = nm, root       # the walk matches on the plane's OWN name
    root.constructionPlanes = _NamedCollection(list(named.values()))
    comps, occs = [root], []
    for comp_name, plane_names, paths in subs:
        sub = _comp(comp_name, token=f"TOKEN:{comp_name}")
        sub.constructionPlanes = _NamedCollection([_CP(n, sub) for n in plane_names])
        comps.append(sub)
        occs += [make_occurrence(path=p, component=sub) for p in paths]
    design = MakeDesign(comp=root, all_components=comps, tokens=handle_map)
    design.activeComponent = design.allComponents.itemByName(active) or root
    root.allOccurrences = list(occs)
    root.allOccurrencesByComponent = lambda c: _NamedCollection(
        [o for o in occs if o.component is c])
    inp._common.design = lambda: design
    inp._common.target_component = lambda d: d.activeComponent
    return design


def _sub_plane(design, comp_name, plane_name):
    """The NATIVE construction plane a sub-component of the installed design owns."""
    return design.allComponents.itemByName(comp_name).constructionPlanes.itemByName(plane_name)


class TestPlaneRef:
    def test_origin_alias(self):
        _install_planes()
        k = inp.PlaneRef("plane", default="yz")
        val, err = k.resolve("xy")
        assert err is None and val == ("origin", "xy")

    def test_alias_front_maps_to_xz(self):
        _install_planes()
        k = inp.PlaneRef("plane")
        val, err = k.resolve("front")
        assert err is None and val == ("origin", "xz")

    def test_construction_plane_by_name(self):
        cp = FakeConstructionPlane()
        _install_planes(named={"MidPlane": cp})
        k = inp.PlaneRef("plane")
        val, err = k.resolve("MidPlane")
        assert err is None and val is cp

    def test_planar_face_handle(self):
        f = FakePlanarFace()
        _install_planes(handle_map={"/v_longtoken_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa": f})
        k = inp.PlaneRef("plane")
        val, err = k.resolve("/v_longtoken_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
        assert err is None and val is f

    def test_curved_face_handle_rejected(self):
        c = FakeCylFace()
        _install_planes(handle_map={"/v_longtoken_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb": c})
        k = inp.PlaneRef("plane")
        val, err = k.resolve("/v_longtoken_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")
        assert val is None and "not PLANAR" in err

    def test_unknown_string(self):
        _install_planes()
        k = inp.PlaneRef("plane")
        val, err = k.resolve("qq")
        assert val is None and "not an origin alias" in err

    def test_the_miss_refusal_carries_the_whole_vocabulary_the_note_no_longer_spells(self):
        # The note names the shapes; the refusal is where an agent that got one wrong learns the
        # alias set, the handle's source, and that an angled plane must come as a handle. Each
        # assertion is a fact that lives ONLY here now, so it is pinned against a silent reword.
        _install_planes()
        _val, err = inp.PlaneRef("plane").resolve("qq")
        assert "top/front/right" in err
        assert "planar-face/construction-plane handle from find_geometry" in err
        assert "arbitrary or angled" in err
        # ...and none of the three rode along in the note, which is what made the refusal the home
        note = inp.PlaneRef("plane").contract_note()
        assert "find_geometry" not in note and "angled" not in note

    def test_long_construction_plane_name_not_mistaken_for_handle(self):
        # same heuristic bug for planes: a >60-char construction-plane name must resolve by NAME
        long_name = "Mid-Span-Reference-Plane-For-The-Left-Outrigger-Pivot-Datum-A"
        assert len(long_name) > 60
        cp = FakeConstructionPlane()
        _install_planes(named={long_name: cp})          # NOT in handle_map
        val, err = inp.PlaneRef("plane").resolve(long_name)
        assert err is None and val is cp

    def test_contract_note_mentions_all_three_sources(self):
        # All three vocabularies, in one wire clause: the alias set, the construction-plane name and
        # the face handle. Where a handle comes from is the miss refusal's job, tested below.
        note = inp.PlaneRef("plane").contract_note()
        assert "xy/xz/yz" in note and "top/front/right" in note
        assert "construction-plane name" in note and "handle" in note
        assert len(note) <= 90, note

    def test_non_string_raw_does_not_crash(self):
        # PlaneRef.resolve must isinstance-guard before `.strip()`: a non-string plane arg returns a
        # clean (None, error), not an AttributeError. Every sibling kind guards this; pin it here.
        _install_planes()
        val, err = inp.PlaneRef("plane", required=True).resolve(["xy"])
        assert val is None and err is not None      # clean rejection, not an AttributeError

    def test_subcomponent_plane_resolves_from_the_root_as_a_proxy(self):
        # the bug this walk exists for: a datum created inside a sub-component is invisible to a
        # root-only lookup, and its NATIVE form is component-local - Fusion refuses it in root
        # context. A design-unique bare name resolves, PROXIED into the occurrence that places it.
        design = _install_planes(subs=[("Tower", ["Datum_A"], ["Tower:1"])])
        val, err = inp.PlaneRef("plane").resolve("Datum_A")
        assert err is None
        assert getattr(val, "native", None) is _sub_plane(design, "Tower", "Datum_A")
        assert val.context.fullPathName == "Tower:1"

    def test_a_plane_whose_owner_cannot_be_told_from_the_root_is_refused(self):
        # same_component answers None with no readable token. Handing the NATIVE plane back would be
        # the root-owner branch's answer, and Fusion refuses a component-local plane in another
        # context ('object is not in the assembly context of this component') at add() - so the kind
        # refuses first, saying the comparison failed rather than that the plane is foreign.
        design = _install_planes(subs=[("Tower", ["Datum_A"], ["Tower:1"])])
        del design.allComponents.itemByName("Tower").entityToken
        val, err = inp.PlaneRef("plane").resolve("Datum_A")
        assert val is None
        assert "Datum_A" in err and "could not be read" in err and "find_geometry" in err

    def test_a_plane_whose_owner_does_not_read_AT_ALL_is_refused_too(self):
        # A missing owner is an owner that did not read, so it takes the same refusal as one whose
        # token will not read - handing the native plane back for it would be the identical guess.
        # Driven at _in_context directly: the design-wide walk pairs each plane with the component
        # it was FOUND in, so a None owner never reaches it through resolve().
        design = _install_planes(subs=[("Tower", ["Datum_A"], ["Tower:1"])])
        cp = _sub_plane(design, "Tower", "Datum_A")
        val, err = inp.PlaneRef("plane")._in_context(design, None, cp, None)
        assert val is None and "could not be read" in err

    def test_bare_name_shared_by_two_components_is_refused_with_qualified_candidates(self):
        _install_planes(subs=[("A", ["Mid"], ["A:1"]), ("B", ["Mid"], ["B:1"])])
        val, err = inp.PlaneRef("plane").resolve("Mid")
        assert val is None and "ambiguous" in err
        assert "A:1:Mid" in err and "B:1:Mid" in err        # every candidate resolves

    def test_qualified_name_picks_the_named_occurrence(self):
        design = _install_planes(subs=[("A", ["Mid"], ["A:1"]), ("B", ["Mid"], ["B:1"])])
        val, err = inp.PlaneRef("plane").resolve("B:1:Mid")
        assert err is None
        assert getattr(val, "native", None) is _sub_plane(design, "B", "Mid")
        assert val.context.fullPathName == "B:1"

    def test_qualified_name_on_an_occurrence_without_that_plane_is_named(self):
        _install_planes(subs=[("Tower", ["Datum_A"], ["Tower:1"])])
        val, err = inp.PlaneRef("plane").resolve("Tower:1:Nope")
        assert val is None and "no construction plane named 'Nope'" in err

    def test_root_plane_is_handed_back_native(self):
        # a root-owned plane is already in assembly context: it must NOT be lifted into an
        # occurrence, and the presence of sub-components must not change what it resolves to.
        cp = FakeConstructionPlane()
        _install_planes(named={"MidPlane": cp}, subs=[("A", ["Other"], ["A:1"])])
        val, err = inp.PlaneRef("plane").resolve("MidPlane")
        assert err is None and val is cp

    def test_active_component_name_shadows_another_components_plane(self):
        # Fusion default-names the first datum of EVERY component 'Plane1'; the active component's
        # own plane wins (native - it is already the context being built in) instead of a refusal.
        design = _install_planes(subs=[("A", ["Plane1"], ["A:1"]), ("B", ["Plane1"], ["B:1"])],
                                 active="A")
        val, err = inp.PlaneRef("plane").resolve("Plane1")
        assert err is None and val is _sub_plane(design, "A", "Plane1")

    def test_plane_on_a_component_placed_twice_is_refused(self):
        # one NAME, but the owning component is instanced twice - each instance holds the plane
        # somewhere different, so the instance is refused rather than guessed.
        _install_planes(subs=[("Jaw", ["Grip"], ["Jaw:1", "Jaw:2"])])
        val, err = inp.PlaneRef("plane").resolve("Grip")
        assert val is None and "placed 2 times" in err
        assert "Jaw:1:Grip" in err and "Jaw:2:Grip" in err

    @pytest.mark.parametrize("given, origin", [
        ("xyplane", ("origin", "xy")), ("xzplane", ("origin", "xz")),
        ("yzplane", ("origin", "yz")), ("XYPlane", ("origin", "xy")),
        ("  XY Plane ", ("origin", "xy")), ("yz plane", ("origin", "yz"))])
    def test_the_alias_plane_spelling_names_the_same_origin_plane(self, given, origin):
        # '<alias> plane' / '<alias>plane' is the spelling an agent reaches for; it resolves to the
        # very plane the bare alias does, so no consumer needs its own fold.
        _install_planes()
        val, err = inp.PlaneRef("plane").resolve(given)
        assert err is None and val == origin

    def test_a_datum_named_mid_plane_still_reaches_the_name_lookup(self):
        # only the three AXIS aliases take the 'plane' suffix - a construction plane genuinely
        # named 'Mid plane' must not be folded into an origin alias.
        cp = FakeConstructionPlane()
        _install_planes(named={"Mid plane": cp})
        val, err = inp.PlaneRef("plane").resolve("Mid plane")
        assert err is None and val is cp

    def test_an_empty_value_resolves_the_declared_default_to_an_entity(self):
        # the raw default string is not a plane: an empty value must come back as the SAME resolved
        # entity the default's own spelling resolves to, so no consumer hands 'xy' to the API.
        _install_planes()
        k = inp.PlaneRef("plane", default="xy")
        val, err = k.resolve("")
        assert err is None
        assert val == ("origin", "xy") == k.resolve("xy")[0]
        assert val != "xy"

    def test_the_default_resolves_down_the_same_path_a_given_value_takes(self):
        # not an alias-only shortcut: a default naming a construction plane resolves by NAME.
        cp = FakeConstructionPlane()
        _install_planes(named={"Datum1": cp})
        val, err = inp.PlaneRef("plane", default="Datum1").resolve("")
        assert err is None and val is cp

    def test_an_unresolvable_default_returns_the_resolve_error(self):
        _install_planes()
        val, err = inp.PlaneRef("plane", default="qq").resolve("")
        assert val is None and "not an origin alias" in err

    def test_an_empty_value_with_no_default_stays_none(self):
        # a kind with no default has nothing to resolve: empty is still empty, and a required kind
        # still refuses.
        _install_planes()
        assert inp.PlaneRef("plane").resolve("") == (None, None)
        val, err = inp.PlaneRef("plane", required=True).resolve("")
        assert val is None and "is required" in err

    def test_a_plane_whose_proxy_FAILS_is_refused_naming_the_plane_and_the_occurrence(self):
        # the native is the component-LOCAL object Fusion refuses in this context - the very thing
        # the lift exists to avoid - so a failed createForAssemblyContext must not fall back to it.
        design = _install_planes(subs=[("Tower", ["Datum_A"], ["Tower:1"])])
        native = _sub_plane(design, "Tower", "Datum_A")
        native.createForAssemblyContext = lambda occ: None
        val, err = inp.PlaneRef("plane").resolve("Datum_A")
        assert val is None and val is not native
        assert "Datum_A" in err and "Tower:1" in err

    def test_a_QUALIFIED_plane_whose_proxy_FAILS_is_refused_the_same_way(self):
        design = _install_planes(subs=[("Tower", ["Datum_A"], ["Tower:1"])])
        native = _sub_plane(design, "Tower", "Datum_A")
        native.createForAssemblyContext = lambda occ: None
        val, err = inp.PlaneRef("plane").resolve("Tower:1:Datum_A")
        assert val is None and val is not native
        assert "Datum_A" in err and "Tower:1" in err

    def test_composite_face_handle_resolves(self):
        # PlaneRef already routes through _resolve_token_entity, so a COMPOSITE planar-face handle
        # ('<token>|@planar_face:x,y,z') must resolve by its bare token. Guards against a regression
        # back to a raw findEntityByToken(s) that the locator suffix would corrupt.
        f = FakePlanarFace()
        _install_planes(handle_map={"FACETOK": f})
        handle = f"FACETOK{inp._HANDLE_SEP}planar_face:0.0,0.0,0.0"
        val, err = inp.PlaneRef("plane").resolve(handle)
        assert err is None and val is f


# ── AxisRef: world axis OR edge handle ──────────────────────────────────────

def _curved_edge():
    """An edge whose curve is not a line - the shape a straight-axis input refuses."""
    return BRepEdge(Circle3D(_UP))


def _install_axis(handle_map=None):
    import adsk.fusion
    adsk.fusion.BRepEdge = BRepEdge
    design = make_design(tokens=handle_map or {})
    inp._common.design = lambda: design


class TestAxisRef:
    def test_world_axis(self):
        _install_axis()
        k = inp.AxisRef("axis", default="z")
        val, err = k.resolve("x")
        assert err is None and val == ("world", (1, 0, 0))

    def test_edge_handle_axis(self):
        e = FakeEdge()
        _install_axis(handle_map={"E": e})
        k = inp.AxisRef("axis")
        val, err = k.resolve("E")
        assert err is None and val == ("edge", e)

    def test_sketch_line_handle_axis(self):
        # a SketchLine is straight by construction - no curveType check needed, unlike a BRepEdge.
        import adsk.fusion

        class _FakeSketchLine:
            pass
        adsk.fusion.SketchLine = _FakeSketchLine
        ln = _FakeSketchLine()
        _install_axis(handle_map={"L": ln})
        k = inp.AxisRef("axis")
        val, err = k.resolve("L")
        assert err is None and val == ("edge", ln)

    def test_curved_edge_rejected(self):
        a = _curved_edge()
        _install_axis(handle_map={"A": a})
        k = inp.AxisRef("axis")
        val, err = k.resolve("A")
        assert val is None and "not straight" in err

    def test_unknown_axis_string(self):
        _install_axis()
        k = inp.AxisRef("axis")
        val, err = k.resolve("q")
        assert val is None and "not a world axis" in err

    def test_composite_handle_resolves_via_token(self):
        # AxisRef must accept a COMPOSITE find_geometry handle ('<token>|@<kind>:x,y,z'), like
        # every other handle kind. The handle_map is keyed on the BARE token; passing the composite
        # must still resolve (the '|@locator' suffix is split off before findEntityByToken).
        e = FakeEdge()
        _install_axis(handle_map={"TOKEN": e})
        handle = f"TOKEN{inp._HANDLE_SEP}edge:1.0,2.0,3.0"
        val, err = inp.AxisRef("axis").resolve(handle)
        assert err is None and val == ("edge", e)

    def test_non_string_raw_does_not_crash(self):
        # AxisRef already guards a non-string raw; pin it (a list/None must yield a clean error/default,
        # never an AttributeError from .strip()).
        _install_axis()
        val, err = inp.AxisRef("axis", required=True).resolve(["x"])
        assert val is None and err is not None      # no crash; a clean rejection


# ── AxisRef face-as-direction: a planar face -> its normal, a cylinder/cone face -> its axis ──────
# A face handle can source a DIRECTION (joint orient_axis / revolve axis). It comes back tagged
# ('world', unit_vec) - the SAME shape a world axis uses - so every AxisRef consumer that handles a
# world direction handles a face-derived one with NO code change (this is why joint_create_origin needs
# none). The existing world-axis / straight-edge / sketch-line paths must keep working (tested above).

def _FakePlanarAxisFace(normal):
    """A planar face whose surface carries the NORMAL a direction read takes."""
    return BRepFace(Plane(FakeVector3D(*normal)))


def _FakeCylAxisFace(axis):
    """A cylinder face whose surface carries the AXIS a direction read takes."""
    return BRepFace(Cylinder(FakeVector3D(*axis)))


def _install_axis_face(handle_map):
    """AxisRef reaches the face branch only after the edge/sketch-line isinstance checks, so wire real
    (non-Mock) BRepEdge + SketchLine classes so those checks return False cleanly, plus BRepFace (the
    SurfaceTypes ints come seeded)."""
    import adsk.fusion
    adsk.fusion.BRepFace = BRepFace
    adsk.fusion.BRepEdge = BRepEdge
    adsk.fusion.SketchLine = type("SL", (), {})
    design = make_design(tokens=handle_map)
    inp._common.design = lambda: design


class TestAxisRefFace:
    def test_the_note_states_what_a_face_resolves_to_since_no_refusal_can(self):
        # Both face forms SUCCEED (the two tests below), so no error ever teaches them - the note
        # is their only home, and it must keep naming both halves.
        note = inp.AxisRef("axis").contract_note()
        assert "planar = normal" in note and "round = axis" in note

    def test_planar_face_gives_the_normal_as_direction(self):
        f = _FakePlanarAxisFace((0, 0, 1))
        _install_axis_face({"F": f})
        val, err = inp.AxisRef("axis").resolve("F")
        assert err is None and val == ("world", (0.0, 0.0, 1.0))

    def test_cylinder_face_gives_its_axis_normalized(self):
        f = _FakeCylAxisFace((0, 0, 2))          # non-unit input -> returned as a UNIT vector
        _install_axis_face({"C": f})
        val, err = inp.AxisRef("axis").resolve("C")
        assert err is None and val == ("world", (0.0, 0.0, 1.0))

    def test_composite_face_handle_resolves_via_token(self):
        f = _FakePlanarAxisFace((1, 0, 0))
        _install_axis_face({"FT": f})
        handle = f"FT{inp._HANDLE_SEP}planar_face:0.0,0.0,0.0"
        val, err = inp.AxisRef("axis").resolve(handle)
        assert err is None and val == ("world", (1.0, 0.0, 0.0))

    def test_world_axis_still_resolves_with_face_types_wired(self):
        # backward-compat: adding the face branch must not disturb the world-axis path
        _install_axis_face({})
        val, err = inp.AxisRef("axis").resolve("y")
        assert err is None and val == ("world", (0, 1, 0))


# ── AxisRef: a CONSTRUCTION AXIS, by handle or by name ───────────────────────────────────────────
#
# A construction axis is a linear ENTITY (its .geometry is an InfiniteLine3D), so it comes back
# tagged ('edge', axis) - the same shape a straight edge uses, which is what every consumer that
# feeds a linear entity to a feature input already handles. The NAME path resolves within the ACTIVE
# component only, case-insensitive EXACT, and refuses a name two axes share.

class _FakeConstructionAxis:
    """A construction axis: a NAME plus .geometry, an InfiniteLine3D (origin + direction) - the
    shape that tells a datum axis from a bounded edge's Line3D. Its geometry reads component-LOCAL
    while native and WORLD through the createForAssemblyContext proxy, which is the split a world
    lift exists for. Also stands in for the ConstructionAxis type the TargetRef extension tests bind
    (one fake per live type per file)."""
    def __init__(self, name="Axis1", origin=None, direction=None, component=None, proxy=None,
                 assembly_context=None):
        self.name = name
        self.geometry = types.SimpleNamespace(origin=origin, direction=direction)
        self.component = component
        self.assemblyContext = assembly_context
        self.proxied_into = []
        if proxy is not None:
            def _for_context(occ, p=proxy):
                self.proxied_into.append(occ)
                return p
            self.createForAssemblyContext = _for_context


@pytest.fixture
def axis_env(monkeypatch):
    """A design whose ACTIVE component is NOT the root - so a lookup scoped to the active component
    is told apart from one that walks the root - plus token resolution and occurrence placement.

    Returns a callable: env(axes=[...], tokens={...}, root_axes=[...]) -> a namespace with .active,
    .root, .design and .place(component, *fullPathNames). The adsk types AxisRef isinstance-checks
    must be REAL classes (a bare Mock attribute is not a type)."""
    import adsk.fusion

    def build(axes=(), tokens=None, root_axes=()):
        monkeypatch.setattr(adsk.fusion, "ConstructionAxis", _FakeConstructionAxis, raising=False)
        monkeypatch.setattr(adsk.fusion, "BRepEdge", BRepEdge, raising=False)
        monkeypatch.setattr(adsk.fusion, "SketchLine", type("SL", (), {}), raising=False)
        monkeypatch.setattr(adsk.fusion, "BRepFace", BRepFace, raising=False)
        # Tokens, because _common.same_component compares on entityToken and answers None without
        # one - and the assembly-context lift REFUSES a pair it cannot identify rather than guess
        # whether an entity needs proxying. A test that wants that state deletes the attribute.
        active = MakeComp(name="Active", entity_token="TOKEN:Active")
        active.constructionAxes = _NamedCollection(list(axes))
        root = MakeComp(name="Root", entity_token="TOKEN:Root")
        root.constructionAxes = _NamedCollection(list(root_axes))
        design = make_design(comp=root, tokens=dict(tokens or {}))
        placed = {}
        root.allOccurrencesByComponent = lambda c: _NamedCollection(placed.get(id(c), []))
        monkeypatch.setattr(inp._common, "design", lambda: design)
        monkeypatch.setattr(inp._common, "target_component", lambda _d=None: active)

        def place(comp, *full_paths):
            placed[id(comp)] = [make_occurrence(path=p) for p in full_paths]

        return types.SimpleNamespace(active=active, root=root, design=design, place=place)

    return build


class TestAxisRefConstructionAxis:
    def test_handle_resolves_to_the_axis_entity(self, axis_env):
        ax = _FakeConstructionAxis("WheelAxis")
        axis_env(axes=[ax], tokens={"CA": ax})
        val, err = inp.AxisRef("axis").resolve("CA")
        assert err is None and val == ("edge", ax)

    def test_name_resolves_in_the_active_component(self, axis_env):
        ax = _FakeConstructionAxis("WheelAxis")
        axis_env(axes=[ax])
        val, err = inp.AxisRef("axis").resolve("WheelAxis")
        assert err is None and val == ("edge", ax)

    def test_name_match_is_case_insensitive_but_exact(self, axis_env):
        ax = _FakeConstructionAxis("WheelAxis")
        axis_env(axes=[ax])
        assert inp.AxisRef("axis").resolve("wheelaxis")[0] == ("edge", ax)
        # EXACT: a prefix is not a match (a substring hit would silently target the wrong axis)
        val, err = inp.AxisRef("axis").resolve("Wheel")
        assert val is None and "WheelAxis" in err        # the miss lists what IS available

    def test_ambiguous_name_is_refused_naming_the_count(self, axis_env):
        axis_env(axes=[_FakeConstructionAxis("Hinge"), _FakeConstructionAxis("Hinge")])
        val, err = inp.AxisRef("axis").resolve("Hinge")
        assert val is None
        assert "2" in err and "Hinge" in err              # refuses, never grabs the first
        assert "handle" in err                            # and names the unambiguous way in

    def test_entity_only_input_still_takes_a_construction_axis(self, axis_env):
        # entity_only refuses a FACE (a direction vector); a construction axis IS a linear entity.
        ax = _FakeConstructionAxis("Spin")
        axis_env(axes=[ax], tokens={"CA": ax})
        assert inp.AxisRef("d", entity_only=True).resolve("CA")[0] == ("edge", ax)
        assert inp.AxisRef("d", entity_only=True).resolve("Spin")[0] == ("edge", ax)

    def test_a_world_key_still_wins_over_the_name_lookup(self, axis_env):
        # regression: the world keys resolve BEFORE any component walk, so an axis named 'x'
        # cannot shadow the world x direction.
        axis_env(axes=[_FakeConstructionAxis("x")])
        assert inp.AxisRef("axis").resolve("x")[0] == ("world", (1, 0, 0))

    def test_component_without_construction_axes_still_reports_the_miss(self, axis_env):
        env = axis_env(axes=[])
        val, err = inp.AxisRef("axis").resolve("Nope")
        assert val is None and "not a world axis" in err
        assert env.active.name in err          # the miss says WHERE it looked

    def test_the_name_lookup_is_scoped_to_the_ACTIVE_component(self, axis_env):
        # An axis owned by the ROOT while another component is active is NOT name-reachable: the
        # lookup walks the active component, so a root-scoped walk would resolve it and be wrong.
        env = axis_env(axes=[], root_axes=[_FakeConstructionAxis("RootSpin")])
        val, err = inp.AxisRef("axis").resolve("RootSpin")
        assert val is None
        assert "active component 'Active'" in err       # the refusal names the component searched
        # the root's axis was never a candidate - the active component is reported as empty
        assert f"'{env.active.name}' has no construction axes" in err


# ── AxisRef(face_entity=True): the face ENTITY, for an input whose API takes the axis-defining face ──

class TestAxisRefFaceEntity:
    def test_cylindrical_face_resolves_to_the_face_itself(self, axis_env):
        f = _FakeCylAxisFace((0, 0, 1))
        axis_env(tokens={"F": f})
        # NOT ('world', vector): the vector throws the axis POSITION away, which is exactly what an
        # off-origin rotation axis needs.
        assert inp.AxisRef("axis", face_entity=True).resolve("F")[0] == ("edge", f)

    def test_planar_face_is_refused(self, axis_env):
        f = _FakePlanarAxisFace((0, 0, 1))
        axis_env(tokens={"F": f})
        val, err = inp.AxisRef("axis", face_entity=True).resolve("F")
        assert val is None and "cylindrical" in err

    def test_world_key_and_construction_axis_unchanged(self, axis_env):
        ax = _FakeConstructionAxis("Spin")
        axis_env(axes=[ax])
        k = inp.AxisRef("axis", face_entity=True)
        assert k.resolve("z")[0] == ("world", (0, 0, 1))
        assert k.resolve("Spin")[0] == ("edge", ax)


# ── axis_line_of: the NUMERIC axis a rotation pivots about must be in WORLD space ────────────────
#
# A ConstructionAxis has no worldGeometry and its .geometry reads component-LOCAL, so a datum in a
# placed component describes a line that is off by the placement - a rotation built from it turns
# about the wrong pivot and reports success. A BRepEdge/SketchLine carries worldGeometry and keeps
# its existing path.

def _datum_in(component, local_origin, world_origin=None, name="Spin"):
    """(axis, its assembly-context proxy) - a datum whose LOCAL geometry differs from what the
    proxy reads, i.e. a component placed away from the origin."""
    proxy = (_FakeConstructionAxis(name, origin=FakePoint(*world_origin),
                                   direction=FakeVector3D(0, 0, 1), assembly_context="OCC")
             if world_origin is not None else None)
    axis = _FakeConstructionAxis(name, origin=FakePoint(*local_origin),
                                 direction=FakeVector3D(0, 0, 1), component=component, proxy=proxy)
    return axis, proxy


class TestAxisLineOfWorldSpace:
    def test_a_datum_in_a_placed_component_is_lifted_through_its_occurrence(self, axis_env):
        env = axis_env()
        wheel = MakeComp(name="Wheel", entity_token="TOKEN:Wheel")
        axis, proxy = _datum_in(wheel, (0, 0, 0), world_origin=(5, 0, 0))
        env.place(wheel, "Wheel:1")
        pair, err = inp.axis_line_of("rotate_axis", axis)
        assert err is None
        point, _direction = pair
        # the PROXY's world origin - the component-local (0,0,0) would pivot about the world origin
        assert (point.x, point.y, point.z) == (5, 0, 0)
        assert axis.proxied_into == ["Wheel:1"] or len(axis.proxied_into) == 1

    def test_a_root_owned_datum_is_used_as_is(self, axis_env):
        # The datum's owner and the design's root are DISTINCT wrappers sharing one entityToken -
        # the measured shape, since component references are never identity-stable. An identity test
        # reads False here and sends a perfectly legal ROOT datum down the placed-component lookup,
        # which finds no occurrences and refuses it.
        env = axis_env()
        env.root.entityToken = "TOKEN:Root"
        axis, _ = _datum_in(entity_proxy(env.root), (2, 2, 0))
        pair, err = inp.axis_line_of("rotate_axis", axis)
        assert err is None and (pair[0].x, pair[0].y) == (2, 2)
        assert axis.proxied_into == []            # local IS world on the root - no lift attempted

    def test_a_proxied_datum_is_read_directly_even_where_its_component_is_placed_twice(self, axis_env):
        # A proxy already reads WORLD (and createForAssemblyContext on one RAISES), so the early
        # return is the ONLY route for a datum handed in from a multiply-placed component: without
        # it the ambiguity refusal fires on a reference that names its instance already.
        env = axis_env()
        wheel = MakeComp(name="Wheel", entity_token="TOKEN:Wheel")
        axis = _FakeConstructionAxis("Spin", origin=FakePoint(9, 0, 0),
                                     direction=FakeVector3D(0, 0, 1), component=wheel,
                                     assembly_context="Assy:1+Wheel:2")
        env.place(wheel, "Assy:1+Wheel:1", "Assy:1+Wheel:2")
        pair, err = inp.axis_line_of("rotate_axis", axis)
        assert err is None and pair[0].x == 9

    def test_a_datum_already_in_context_is_not_re_proxied(self, axis_env):
        axis_env()
        axis = _FakeConstructionAxis("Spin", origin=FakePoint(7, 0, 0),
                                     direction=FakeVector3D(0, 0, 1), assembly_context="OCC")
        pair, err = inp.axis_line_of("rotate_axis", axis)
        assert err is None and pair[0].x == 7
        assert axis.proxied_into == []

    def test_a_component_placed_twice_is_refused_naming_each_path(self, axis_env):
        env = axis_env()
        wheel = MakeComp(name="Wheel", entity_token="TOKEN:Wheel")
        axis, _ = _datum_in(wheel, (0, 0, 0), world_origin=(5, 0, 0))
        env.place(wheel, "Assy:1+Wheel:1", "Assy:1+Wheel:2")
        pair, err = inp.axis_line_of("rotate_axis", axis)
        assert pair is None
        assert "Assy:1+Wheel:1" in err and "Assy:1+Wheel:2" in err

    def test_an_unplaced_component_datum_is_refused(self, axis_env):
        axis_env()
        axis, _ = _datum_in(MakeComp(name="Wheel", entity_token="TOKEN:Wheel"), (0, 0, 0),
                            world_origin=(5, 0, 0))
        pair, err = inp.axis_line_of("rotate_axis", axis)
        assert pair is None and "not placed in the assembly" in err

    def test_an_owner_that_cannot_be_told_from_the_build_component_is_refused(self, axis_env):
        # single_placement's tri-state branch: with no readable token the lift cannot tell whether
        # this datum already sits in the calling component's space. BOTH guesses are wrong (a proxy
        # of an entity already in context, or a native the feature input rejects at add()), so it
        # refuses - and says the comparison failed, naming the owner and the ways out.
        env = axis_env()
        wheel = MakeComp(name="Wheel")                    # no entityToken at all
        axis, _ = _datum_in(wheel, (0, 0, 0), world_origin=(5, 0, 0))
        env.place(wheel, "Wheel:1")
        pair, err = inp.axis_line_of("rotate_axis", axis)
        assert pair is None
        assert "Wheel" in err and "could not be read" in err and "world axis" in err
        assert axis.proxied_into == []                    # nothing was lifted on a guess

    def test_an_unreadable_root_component_is_refused_naming_the_owner(self, axis_env):
        # With no root there is no placement to look the datum up in, so where it SITS is unknown.
        # Falling back to its own .geometry here would hand back a component-LOCAL line as though it
        # were world - the exact silent wrong-pivot this lift exists to prevent.
        env = axis_env()
        wheel = MakeComp(name="Wheel", entity_token="TOKEN:Wheel")
        axis, _ = _datum_in(wheel, (0, 0, 0), world_origin=(5, 0, 0))
        env.place(wheel, "Wheel:1")
        env.design.rootComponent = None
        pair, err = inp.axis_line_of("rotate_axis", axis)
        assert pair is None
        assert "Wheel" in err and "root component could not be read" in err

    def test_an_edge_keeps_its_worldgeometry_path(self, axis_env):
        # regression: only a ConstructionAxis takes the lift; an edge's world line is read directly,
        # and its direction is DERIVED from the two endpoints.
        axis_env()
        edge = FakeEdge()
        edge.worldGeometry = types.SimpleNamespace(startPoint=FakePoint(1, 0, 0),
                                                   endPoint=FakePoint(4, 0, 0))
        pair, err = inp.axis_line_of("rotate_axis", edge)
        assert err is None
        point, direction = pair
        assert (point.x, point.y, point.z) == (1, 0, 0)
        assert (direction.x, direction.y, direction.z) == (1.0, 0.0, 0.0)   # normalized

    def test_a_line_whose_endpoints_coincide_is_refused_as_degenerate(self, axis_env):
        # The derived direction is the zero vector, and normalize() reports success while leaving it
        # zero (the measured behaviour the shared vector fake carries), so nothing downstream tells
        # this from a real axis: the consumer would pivot about a direction pointing nowhere and
        # report success. Only this guard refuses it, and the refusal says WHY.
        axis_env()
        edge = FakeEdge()
        edge.worldGeometry = types.SimpleNamespace(startPoint=FakePoint(2, 3, 4),
                                                   endPoint=FakePoint(2, 3, 4))
        pair, err = inp.axis_line_of("rotate_axis", edge)
        assert pair is None
        assert "degenerate (zero length)" in err and "rotate_axis" in err

    def test_the_degeneracy_band_is_a_tolerance_and_both_of_its_sides_hold(self, axis_env):
        # The guard is a tolerance, not an equality on 0.0: a length AT 1e-12 cm is refused and one
        # just past it resolves and normalizes. Pinning only an exact zero leaves the comparison
        # free to be an '==' or a '<' and stay green.
        axis_env()
        at = FakeEdge()
        at.worldGeometry = types.SimpleNamespace(startPoint=FakePoint(0, 0, 0),
                                                 endPoint=FakePoint(1e-12, 0, 0))
        pair, err = inp.axis_line_of("rotate_axis", at)
        assert pair is None and "degenerate (zero length)" in err
        above = FakeEdge()
        above.worldGeometry = types.SimpleNamespace(startPoint=FakePoint(0, 0, 0),
                                                    endPoint=FakePoint(2e-12, 0, 0))
        pair, err = inp.axis_line_of("rotate_axis", above)
        assert err is None
        assert (pair[1].x, pair[1].y, pair[1].z) == (1.0, 0.0, 0.0)


# ── Distance + UnitField scaling chain ──────────────────────────────────────

class TestDistanceUnits:
    def test_distance_scaled_by_units(self):
        d = inp.Distance("dist")
        val, err = d.resolve_scaled(6, 0.1)        # 6 mm at scale 0.1 -> 0.6 cm
        assert err is None and abs(val - 0.6) < 1e-9

    def test_distance_nonzero_guard(self):
        d = inp.Distance("dist", allow_zero=False)
        _, err = d.resolve_scaled(0, 0.1)
        assert "non-zero" in err

    @pytest.mark.parametrize("raw", ["nan", "NaN", float("nan")])
    def test_a_non_finite_NaN_length_is_refused_naming_the_value(self, raw):
        # NaN passes BOTH guards under it - every comparison against NaN is False - so without the
        # finite check it reaches ValueInput.createByReal as a dimension.
        d = inp.Distance("dist", allow_zero=False, allow_negative=False)
        val, err = d.resolve_scaled(raw, 0.1)
        assert val is None
        assert "finite" in err and "nan" in err.lower()

    @pytest.mark.parametrize("raw, named", [("inf", "inf"), ("Infinity", "inf"),
                                            ("-inf", "-inf"), (float("inf"), "inf"),
                                            (float("-inf"), "-inf")])
    def test_a_non_finite_INFINITE_length_is_refused_naming_the_value(self, raw, named):
        d = inp.Distance("dist")
        val, err = d.resolve_scaled(raw, 0.1)
        assert val is None
        assert "finite" in err and named in err

    def test_the_largest_FINITE_float_still_resolves(self):
        # the boundary the check draws is finite-vs-not, not large-vs-small: a huge but finite
        # length is the caller's problem to make sense of, not this guard's to refuse.
        import sys
        val, err = inp.Distance("dist").resolve_scaled(sys.float_info.max, 1.0)
        assert err is None and val == sys.float_info.max

    def test_unit_field_returns_scale(self):
        u = inp.UnitField()
        sf, err = u.resolve("in")
        assert err is None and abs(sf - 2.54) < 1e-9

    def test_unknown_unit(self):
        u = inp.UnitField()
        _, err = u.resolve("furlong")
        assert "Unknown units" in err

    def test_unit_field_schema_emits_enum(self):
        # units choices live in the schema enum, not re-spelled as "mm | cm | in" prose in 20 tools
        sch = inp.UnitField().schema()
        assert sch["type"] == "string" and sch["enum"] == ["mm", "cm", "in"]
        assert sch["default"] == "mm"                  # the default is structure, not prose
        assert "Default" not in sch.get("description", "")


# ── shared singletons + as_property (the dedup mechanism for the enum migration) ─────────────────

class TestSharedInputs:
    def test_as_property_splats_name_and_schema(self):
        name, sch = inp.UNITS.as_property()
        assert name == "units"
        assert sch["enum"] == ["mm", "cm", "in"]

    def test_brief_drops_the_contract_note_and_keeps_the_tools_own_description(self):
        # The short form for a tool carrying one kind several times (model_construction's
        # plane/plane2/plane3): the repeat still says which slot it is, the shared clause rides once.
        k = inp.PlaneRef("plane2", description="2nd plane (see 'mode').")
        full = k.as_property()[1]["description"]
        brief = k.as_property(brief=True)[1]["description"]
        assert brief == "2nd plane (see 'mode')."
        assert k.contract_note() in full and k.contract_note() not in brief

    def test_brief_reaches_the_kinds_that_override_schema(self):
        # every override has to thread the flag - one that forgot would ship its note on the repeat
        for k in (inp.OccurrenceRefList("occs", description="Move these."),
                  inp.ProfileRefList("profiles", description="Loft through these."),
                  inp.Choice("mode", ["a", "b"], default="a", description="How."),
                  inp.UnitField(description="Display units.")):
            assert k.as_property(brief=True)[1]["description"] == k.description, k.name

    def test_units_property_factory(self):
        name, sch = inp.units_property(description="Display units.")
        assert name == "units" and sch["enum"] == ["mm", "cm", "in"]
        assert "Display units" in sch["description"]

    def test_boolean_op_subset(self):
        # combine supports only join/cut/intersect — the factory carries exactly that subset as enum
        name, sch = inp.boolean_op(options=("join", "cut", "intersect")).as_property()
        assert name == "operation" and sch["enum"] == ["join", "cut", "intersect"]

    def test_frame_axis(self):
        name, sch = inp.frame_axis(default="x").as_property()
        assert name == "axis" and sch["enum"] == ["x", "y", "z"]
        assert sch["default"] == "x"

    def test_joint_motion_full_set(self):
        # joint_create/edit get all six; the shared set means it can't drift from joint_at_geometry's
        name, sch = inp.joint_motion().as_property()
        assert name == "joint_type"
        assert sch["enum"] == ["rigid", "revolute", "slider", "cylindrical", "planar", "ball"]

    def test_joint_motion_subset_preserves_capability_difference(self):
        # joint_at_geometry omits planar — pass the subset explicitly; still structured, still an enum
        name, sch = inp.joint_motion("motion",
                                     options=("rigid", "revolute", "slider", "cylindrical", "ball"),
                                     default="revolute").as_property()
        assert name == "motion" and "planar" not in sch["enum"]
        assert sch["enum"] == ["rigid", "revolute", "slider", "cylindrical", "ball"]


# ── Choice ──────────────────────────────────────────────────────────────────

class TestChoice:
    def test_valid_option(self):
        c = inp.Choice("op", ["new", "join", "cut"], default="new")
        val, err = c.resolve("cut")
        assert err is None and val == "cut"

    def test_invalid_option(self):
        c = inp.Choice("op", ["new", "join"], default="new")
        _, err = c.resolve("weld")
        assert "must be one of" in err

    def test_default_when_empty(self):
        c = inp.Choice("op", ["new", "join"], default="new")
        val, _ = c.resolve("")
        assert val == "new"

    def test_schema_emits_enum(self):
        # the whole point of #2: the legal values live in the JSON-schema `enum`, validated by the
        # client/server, not only described in prose. The description must NOT re-list them (the Choice
        # contract_note would just duplicate the enum).
        c = inp.Choice("op", ["new", "join", "cut"], description="The boolean operation.")
        sch = c.schema()
        assert sch["type"] == "string"
        assert sch["enum"] == ["new", "join", "cut"]
        # the bare option list should not be re-spelled in the description (enum carries it)
        assert "new, join, cut" not in sch["description"]

    def test_schema_enum_with_default_carries_it_as_structure(self):
        c = inp.Choice("op", ["new", "cut"], default="new")
        sch = c.schema()
        assert sch["enum"] == ["new", "cut"]
        assert sch["default"] == "new"
        assert "description" not in sch                # nothing left to say in prose
        assert "default" not in inp.Choice("op", ["new", "cut"]).schema()


# ── resolve_inputs: end-to-end (units resolved first, distances scaled) ─────

class TestResolveInputs:
    def test_resolves_all_with_unit_dependency(self):
        spec = [inp.UnitField(), inp.Distance("depth", allow_zero=False),
                inp.Choice("op", ["new", "cut"], default="new")]
        vals, err = inp.resolve_inputs(spec, {"units": "in", "depth": 1, "op": "cut"})
        assert err is None
        assert abs(vals["depth"] - 2.54) < 1e-9       # 1 in -> 2.54 cm
        assert vals["op"] == "cut"

    def test_first_failure_short_circuits(self):
        spec = [inp.UnitField(), inp.Distance("depth", allow_zero=False)]
        vals, err = inp.resolve_inputs(spec, {"units": "mm", "depth": 0})
        assert vals is None and err["isError"] is True and "non-zero" in err["message"]


# ── contract_block + apply_to_tool generation ───────────────────────────────

class TestGeneration:
    def test_contract_block_lists_each_input(self):
        spec = [inp.GeometryHandle("on_face", require="planar_face"),
                inp.Choice("op", ["new", "cut"], default="new")]
        block = inp.contract_block(spec)
        assert "INPUTS:" in block
        assert "on_face" in block and "op" in block
        assert "planar" in block.lower()

    def test_apply_to_tool_adds_properties_and_required(self):
        class _Registrar:
            """The mcp_primitives Tool as apply_to_tool drives it - not adsk.cam's cutting Tool."""
            def __init__(self):
                self.props = {}
                self.required = []
            def add_input_property(self, n, s):
                self.props[n] = s; return self
            def add_required_input(self, n):
                self.required.append(n); return self
        t = _Registrar()
        spec = [inp.GeometryHandle("on_face", require="planar_face", required=True),
                inp.Choice("op", ["new"], default="new")]
        inp.apply_to_tool(t, spec)
        assert "on_face" in t.props and "op" in t.props
        assert t.required == ["on_face"]


# ── BodyRef KIND axis: solid | surface | mesh | any (+ redirecting wrong-kind error) ────────────
# A BRepBody can be a SOLID (isSolid True) or an OPEN SURFACE (isSolid False); a MeshBody is a
# SEPARATE type living in meshBodies. The kind axis validates at resolve time and, on the WRONG kind,
# returns a REDIRECTING error (the high-value part) instead of a silent miss / misleading downstream.

def FakeBRep(name="Body1", is_solid=True, entity_token=None):
    """A BRep body on the shared fake: isSolid distinguishes solid vs open-surface, and entityToken
    is the stable identity two wrappers of the SAME physical body share."""
    return BRepBody(name=name, is_solid=is_solid, entity_token=entity_token)


def _install_kind_bodies(brep_named=None, mesh_named=None, handle_map=None):
    """Install fakes for the kind axis: BRepBody/MeshBody types wired for isinstance, a component with
    BOTH bRepBodies and meshBodies collections, and a handle resolver."""
    import adsk.fusion
    adsk.fusion.BRepBody = BRepBody
    adsk.fusion.MeshBody = MeshBody
    brep_named = brep_named or {}
    mesh_named = mesh_named or {}
    handle_map = handle_map or {}

    # meshBodies is the _MeshBodies collection - no itemByName, unlike bRepBodies - so a mesh is
    # found by iterate-and-match.
    comp = MakeComp(bodies=list(brep_named.values()), mesh_bodies=list(mesh_named.values()))
    design = MakeDesign(comp=comp, tokens=handle_map)
    inp._common.design = lambda: design
    inp._common.target_component = lambda d: comp
    return comp


def _install_occurrence_scope(comp_name="MeshComp", brep_name="Body1", mesh_name="CompMesh",
                              placements=1, occ_mesh_read="empty", lifts=True):
    """A component holding ONE BRep and ONE mesh, placed `placements` times.

    `occ_mesh_read` is the shape of the OCCURRENCE-level meshBodies read (meshbodyvector-shape says
    the counted walk gets nothing off it, so both spellings of "nothing" are modelled): "empty" -
    the read answers an EMPTY collection; "raises" - the read throws. `lifts` is MeshBody's
    createForAssemblyContext knob. Returns (occurrences, brep, mesh)."""
    import adsk.fusion
    adsk.fusion.BRepBody = BRepBody
    adsk.fusion.MeshBody = MeshBody
    brep, mesh = FakeBRep(brep_name, is_solid=True), MeshBody(mesh_name, lifts=lifts)

    comp = MakeComp(name=comp_name, bodies=[brep], mesh_bodies=[mesh])
    brep.parentComponent = comp
    mesh.parentComponent = comp

    class _Occ(FakeOccurrence):
        """The occurrence-level meshBodies read, which the shared occurrence fake carries no knob
        for: it answers an empty collection, or throws when `occ_mesh_read` says so."""

        @property
        def meshBodies(self):
            if occ_mesh_read == "raises":
                raise AttributeError("MeshBodies is not readable on an Occurrence")
            return _MeshBodies([])          # answers, and answers NOTHING

    occs = [_Occ(path=f"{comp_name}:{i + 1}", component=comp, bodies=[brep])
            for i in range(placements)]

    root = MakeComp(name="Root", mesh_bodies=[], all_occurrences=occs)
    root.allOccurrencesByComponent = lambda c: _NamedCollection(
        list(occs) if c is comp else [])
    # findEntityByToken answers nothing, so every lookup takes the NAME path.
    design = MakeDesign(comp=root, all_components=[root, comp])

    inp._common.design = lambda: design
    inp._common.target_component = lambda d=None: root
    return occs, brep, mesh


class TestBodyKind:
    def test_default_kind_is_any_for_backcompat(self):
        # A pre-kind BodyRef accepted ANY BRepBody (no isSolid check). Default 'any' preserves that:
        # a surface body (isSolid False) must STILL resolve under the default.
        surf = FakeBRep("Surf", is_solid=False)
        _install_kind_bodies(handle_map={"H": surf})
        val, err = inp.BodyRef("body").resolve("H")
        assert err is None and val is surf

    def test_solid_kind_resolves_a_solid(self):
        s = FakeBRep("S", is_solid=True)
        _install_kind_bodies(handle_map={"H": s})
        val, err = inp.BodyRef("body", kind="solid").resolve("H")
        assert err is None and val is s

    def test_solid_kind_rejects_a_surface_with_redirect(self):
        surf = FakeBRep("Surf", is_solid=False)
        _install_kind_bodies(handle_map={"H": surf})
        val, err = inp.BodyRef("target", kind="solid").resolve("H")
        assert val is None
        assert "must be a SOLID body" in err and "OPEN SURFACE body" in err

    def test_solid_kind_rejects_a_mesh_with_redirect(self):
        # the headline redirect: solid asked, MESH given -> name the mesh + point at mesh_* / convert
        m = MeshBody("M")
        _install_kind_bodies(handle_map={"H": m})
        val, err = inp.BodyRef("target", kind="solid").resolve("H")
        assert val is None
        assert "must be a SOLID body" in err and "MESH body" in err and "mesh_to_brep" in err

    def test_surface_kind_resolves_a_surface(self):
        surf = FakeBRep("Surf", is_solid=False)
        _install_kind_bodies(handle_map={"H": surf})
        val, err = inp.SurfaceBodyRef("body").resolve("H")
        assert err is None and val is surf

    def test_surface_kind_rejects_a_solid(self):
        s = FakeBRep("S", is_solid=True)
        _install_kind_bodies(handle_map={"H": s})
        val, err = inp.SurfaceBodyRef("body").resolve("H")
        assert val is None and "must be an OPEN SURFACE body" in err and "SOLID body" in err

    def test_mesh_kind_resolves_a_mesh(self):
        m = MeshBody("M")
        _install_kind_bodies(handle_map={"H": m})
        val, err = inp.MeshBodyRef("body").resolve("H")
        assert err is None and val is m

    def test_mesh_kind_rejects_a_brep_solid(self):
        # mesh-vs-brep discrimination: a BRep solid handed to a mesh input is redirected, not accepted
        s = FakeBRep("S", is_solid=True)
        _install_kind_bodies(handle_map={"H": s})
        val, err = inp.MeshBodyRef("body").resolve("H")
        assert val is None and "must be a MESH body" in err and "SOLID body" in err

    def test_mesh_resolves_by_name_from_meshBodies(self):
        # name lookup searches meshBodies too - a mesh name must never be an invisible miss
        m = MeshBody("ScanData")
        _install_kind_bodies(mesh_named={"ScanData": m})
        val, err = inp.MeshBodyRef("body").resolve("ScanData")
        assert err is None and val is m

    def test_a_mesh_in_a_subcomponent_resolves_through_the_component_walk(self):
        # A mesh in a sub-component must resolve by name even though the OCCURRENCE cannot answer for
        # it: reading meshBodies off an occurrence RAISES, so the occurrence pass can never see a
        # mesh. The design-wide component walk is the one path that reaches it - this fake raises on
        # occ.meshBodies exactly as live does, so a resolver that leaned on the occurrence fails here.
        import adsk.fusion
        adsk.fusion.BRepBody = BRepBody
        adsk.fusion.MeshBody = MeshBody
        m = MeshBody("Occ_Scan")

        # the sub-COMPONENT that owns the mesh, reachable only via design.allComponents
        sub = MakeComp(name="Scanned", mesh_bodies=[m])

        class _Occ(FakeOccurrence):
            """The occurrence of that component: bRepBodies reads fine, meshBodies RAISES."""
            @property
            def meshBodies(self):
                raise AttributeError("MeshBodies is not readable on an Occurrence")

        root = MakeComp(name="Root", mesh_bodies=[],
                        all_occurrences=[_Occ(path="Scanned:1", component=sub)])
        # findEntityByToken answers nothing, so the lookup takes the NAME path.
        design = MakeDesign(comp=root, all_components=[root, sub])

        inp._common.design = lambda: design
        # target_component is the root (which has NO matching mesh) -> resolution must reach the
        # sub-component through the component walk, not through the occurrence.
        inp._common.target_component = lambda d: root
        val, err = inp.MeshBodyRef("body").resolve("Occ_Scan")
        assert err is None and val is m

    @pytest.mark.parametrize("occ_mesh_read", ["empty", "raises"])
    def test_a_singly_placed_occurrence_address_reaches_the_component_mesh(self, occ_mesh_read):
        # '<occurrence>:<body>' is the spelling that picks ONE instance's body out of a shared name.
        # The counted walk gets nothing off an occurrence's MeshBodyVector - the read may answer
        # empty or raise - so the mesh comes off the placed component and is LIFTED into the
        # placement, which is the body carrying the assembly context the address named.
        _occs, _brep, mesh = _install_occurrence_scope(occ_mesh_read=occ_mesh_read)
        for spec in ("MeshComp:1/CompMesh", "MeshComp:1:CompMesh"):
            val, err = inp.BodyRef("body", kind="mesh").resolve(spec)
            assert err is None, (spec, occ_mesh_read, err)
            assert val.nativeObject is mesh
            assert val.assemblyContext.fullPathName == "MeshComp:1"

    def test_a_lift_that_hands_nothing_back_falls_to_the_native_under_one_placement(self):
        # One placement, so the component's own mesh IS that instance's - no ambiguity to refuse
        _occs, _brep, mesh = _install_occurrence_scope(lifts=False)
        val, err = inp.BodyRef("body", kind="mesh").resolve("MeshComp:1/CompMesh")
        assert err is None and val is mesh

    def test_the_same_occurrence_scope_still_resolves_its_brep_proxy(self):
        # the BRep half must keep coming from the OCCURRENCE, which carries the assembly context the
        # component's native body does not
        _occs, brep, _mesh = _install_occurrence_scope()
        val, err = inp.BodyRef("body", kind="solid").resolve("MeshComp:1/Body1")
        assert err is None and val is brep

    def test_a_solid_input_redirects_an_occurrence_qualified_mesh(self):
        # the consumer that does NOT take a mesh gets the kind redirect naming what was found -
        # never "that scope holds no such body", which would deny a body the scope really holds
        _install_occurrence_scope()
        val, err = inp.BodyRef("target", kind="solid").resolve("MeshComp:1/CompMesh")
        assert val is None
        assert "must be a SOLID body" in err and "MESH body" in err

    @pytest.mark.parametrize("occ_mesh_read", ["empty", "raises"])
    def test_a_miss_under_an_occurrence_scope_lists_its_mesh_bodies_too(self, occ_mesh_read):
        # the "it holds" list is what tells a caller which names DO resolve there; leaving the
        # meshes out of it points them away from a body the scope holds
        _install_occurrence_scope(occ_mesh_read=occ_mesh_read)
        val, err = inp.BodyRef("body").resolve("MeshComp:1/Ghost")
        assert val is None and "holds no body named 'Ghost'" in err
        assert "'Body1'" in err and "'CompMesh'" in err

    def test_each_placement_of_a_twice_placed_component_gets_its_own_mesh(self):
        # The mesh half of '<instance>:<body>' now answers per instance the way the BRep half does:
        # each address lifts the component's mesh into ITS occurrence, so the two calls hand back
        # two proxies over one native rather than one shared body.
        _occs, _brep, mesh = _install_occurrence_scope(placements=2)
        got = []
        for spec in ("MeshComp:1/CompMesh", "MeshComp:2/CompMesh"):
            val, err = inp.BodyRef("body", kind="mesh").resolve(spec)
            assert err is None, (spec, err)
            assert val.nativeObject is mesh
            got.append(val.assemblyContext.fullPathName)
        assert got == ["MeshComp:1", "MeshComp:2"]

    def test_a_multiply_placed_mesh_that_will_not_lift_is_still_refused(self):
        # Without the lift the component's own body is all there is, so both addresses would answer
        # ONE body while the BRep half of the same address answers per instance. Refused, naming
        # both placements and the component-scoped spelling that honestly names that one body.
        _install_occurrence_scope(placements=2, lifts=False)
        for spec in ("MeshComp:1/CompMesh", "MeshComp:2/CompMesh"):
            val, err = inp.BodyRef("body", kind="mesh").resolve(spec)
            assert val is None, spec
            assert "did not lift" in err and "no assembly context" in err
            assert "placed 2 times" in err and "'MeshComp:1'" in err and "'MeshComp:2'" in err
            assert "'MeshComp:CompMesh'" in err          # the remedy that does resolve

    def test_two_placements_of_one_mesh_carry_distinct_tokens_over_one_identity(self):
        # MEASURED (meshbody-proxy-token-differs): each placement's proxy token differs from the
        # native's and from the other's, while nativeObject ties both back - so a de-dup keying on
        # (nativeObject or self).entityToken still sees ONE physical body, and a handle minted in
        # one instance cannot resolve to the other.
        _occs, _brep, mesh = _install_occurrence_scope(placements=2)
        proxies = []
        for spec in ("MeshComp:1/CompMesh", "MeshComp:2/CompMesh"):
            val, err = inp.BodyRef("body", kind="mesh").resolve(spec)
            assert err is None, (spec, err)
            proxies.append(val)
        a, b = proxies
        assert a.entityToken != mesh.entityToken and b.entityToken != mesh.entityToken
        assert a.entityToken != b.entityToken
        assert a.nativeObject.entityToken == mesh.entityToken
        assert inp._common.native_identity(a) == inp._common.native_identity(mesh)
        assert inp._common.native_identity(b) == inp._common.native_identity(mesh)

    def test_a_lifted_mesh_is_a_view_onto_the_native_not_a_copy_of_it(self):
        # One body seen from an occurrence: a change made to the native after the lift reads back
        # through the proxy. A copy would answer the state captured at lift time forever.
        _occs, _brep, mesh = _install_occurrence_scope()
        val, err = inp.BodyRef("body", kind="mesh").resolve("MeshComp:1/CompMesh")
        assert err is None and val.nativeObject is mesh
        mesh.isClosed = False
        mesh.name = "Rescanned"
        assert val.isClosed is False and val.name == "Rescanned"

    def test_the_brep_half_of_a_two_placement_address_still_resolves_per_instance(self):
        # the refusal above is about the MESH only - the BRep proxy vocabulary is untouched
        _install_occurrence_scope(placements=2)
        val, err = inp.BodyRef("body", kind="solid").resolve("MeshComp:2/Body1")
        assert err is None and val is not None

    def test_an_unreadable_placement_census_refuses_rather_than_guessing_one_instance(self):
        # the lift handed nothing back AND nothing established that this instance is the only one,
        # so the address is not answered with a component-wide body in silence
        _install_occurrence_scope(lifts=False)
        root = inp._common.design().rootComponent
        root.allOccurrencesByComponent = lambda c: None
        val, err = inp.BodyRef("body", kind="mesh").resolve("MeshComp:1/CompMesh")
        assert val is None and "did not read" in err and "'MeshComp:CompMesh'" in err

    def test_a_component_scope_has_no_placed_component_to_fall_back_to(self):
        # `Component.component` does not read, so the fallback is empty for a component scope. That
        # is what stops a component's own mesh - already found by the collection walk - entering the
        # candidate list a SECOND time and turning one match into a spurious ambiguity refusal.
        _occs, _brep, _mesh = _install_occurrence_scope()
        comp = inp._common.design().allComponents.item(1)
        assert inp._placed_meshes_named(comp, "CompMesh") == []
        assert inp._placed_component(comp) is None
        # the guarantee rests on WHICH kind carries `component`, so the basis is pinned rather than
        # left as prose: an Occurrence does, a Component does not
        import live_api_facts
        assert "component" in live_api_facts.SHAPES["Occurrence"]
        assert "component" not in live_api_facts.SHAPES["Component"]

    def test_a_component_qualified_mesh_resolves_to_exactly_one_body(self):
        # the end-to-end counterpart: '<component>:<mesh>' names ONE body, never an ambiguity
        _occs, _brep, mesh = _install_occurrence_scope()
        val, err = inp.BodyRef("body", kind="mesh").resolve("MeshComp:CompMesh")
        assert err is None and val is mesh

    def test_any_kind_accepts_solid_surface_and_mesh(self):
        s, surf, m = FakeBRep("S", True), FakeBRep("Surf", False), MeshBody("M")
        _install_kind_bodies(handle_map={"S": s, "U": surf, "M": m})
        for h, want in (("S", s), ("U", surf), ("M", m)):
            val, err = inp.BodyRef("body", kind="any").resolve(h)
            assert err is None and val is want

    def test_list_kind_checks_every_element_before_returning(self):
        # one wrong-kind element fails the WHOLE list (so no partial mutation downstream), with its index
        s1, m = FakeBRep("S1", True), MeshBody("M")
        _install_kind_bodies(handle_map={"S1": s1, "M": m})
        val, err = inp.BodyRefList("bodies", kind="solid").resolve(["S1", "M"])
        assert val is None and "[1]" in err and "must be a SOLID body" in err

    def test_list_all_correct_kind_resolves_in_order(self):
        s1, s2 = FakeBRep("S1", True), FakeBRep("S2", True)
        _install_kind_bodies(handle_map={"S1": s1, "S2": s2})
        val, err = inp.BodyRefList("bodies", kind="solid").resolve(["S1", "S2"])
        assert err is None and val == [s1, s2]

    def test_surface_list_alias(self):
        u1, u2 = FakeBRep("U1", False), FakeBRep("U2", False)
        _install_kind_bodies(handle_map={"U1": u1, "U2": u2})
        val, err = inp.SurfaceBodyRefList("bodies").resolve(["U1", "U2"])
        assert err is None and val == [u1, u2]


# ── BodyRef kind='brep': a SOLID or SURFACE BRep body, but NOT a mesh (model_split's target/cutter) ──

class TestBodyBrepKind:
    def test_brep_resolves_a_solid(self):
        s = FakeBRep("S", is_solid=True)
        _install_kind_bodies(handle_map={"H": s})
        val, err = inp.BodyRef("body", kind="brep").resolve("H")
        assert err is None and val is s

    def test_brep_resolves_a_surface(self):
        surf = FakeBRep("Surf", is_solid=False)
        _install_kind_bodies(handle_map={"H": surf})
        val, err = inp.BodyRef("body", kind="brep").resolve("H")
        assert err is None and val is surf

    def test_brep_rejects_a_mesh_with_redirect(self):
        # 'brep' = solid OR surface but EXCLUDES a mesh -> a mesh is redirected, not accepted
        m = MeshBody("M")
        _install_kind_bodies(handle_map={"H": m})
        val, err = inp.BodyRef("target", kind="brep").resolve("H")
        assert val is None
        assert "must be a BRep (non-mesh) body" in err and "MESH body" in err and "mesh_to_brep" in err


# ── the wrong-kind redirect names the vocabulary the caller ACTUALLY used ────────────────────────
# BodyRef takes a handle OR a name. A redirect that says "that handle" for a bare name sends the
# caller hunting for a handle it never passed - and find_geometry mints no body handle, so the hunt
# has no end. The word follows _resolve_any_body's own answer about which vocabulary resolved.

class TestRedirectNamesWhatWasPassed:
    def test_a_name_sourced_wrong_kind_says_NAME_and_quotes_it(self):
        m = MeshBody("ScanData")
        _install_kind_bodies(mesh_named={"ScanData": m})
        val, err = inp.BodyRef("target", kind="solid").resolve("ScanData")
        assert val is None
        assert "the name 'ScanData' resolves to a MESH body" in err
        assert "handle points at" not in err

    def test_a_handle_sourced_wrong_kind_still_says_HANDLE(self):
        m = MeshBody("M")
        _install_kind_bodies(handle_map={"tok-mesh": m})
        val, err = inp.BodyRef("target", kind="solid").resolve("tok-mesh")
        assert val is None
        assert "that handle points at a MESH body" in err
        assert "the name" not in err

    def test_a_face_handle_walked_to_its_body_is_still_HANDLE_sourced(self):
        # a find_geometry FACE handle resolves through the owning-body walk - the caller still gave
        # a handle, so naming a "name" there would describe a string that was never typed
        m = MeshBody("M")
        face = FakePlanarFace()
        face.body = m
        _install_kind_bodies(handle_map={"tok-face": face})
        val, err = inp.BodyRef("target", kind="solid").resolve("tok-face")
        assert val is None
        assert "that handle points at a MESH body" in err

    def test_an_occurrence_qualified_name_is_NAME_sourced(self):
        # '<occurrence>:<body>' is a NAME vocabulary, however handle-like the colon makes it look
        _install_occurrence_scope()
        val, err = inp.BodyRef("target", kind="solid").resolve("MeshComp:1/CompMesh")
        assert val is None
        assert "the name 'MeshComp:1/CompMesh' resolves to a MESH body" in err

    def test_the_list_form_carries_the_same_wording_per_element(self):
        m = MeshBody("ScanData")
        _install_kind_bodies(mesh_named={"ScanData": m})
        val, err = inp.BodyRefList("bodies", kind="solid").resolve(["ScanData"])
        assert val is None
        assert "the name 'ScanData' resolves to a MESH body" in err


# ── BodyRef by-NAME ambiguity refusal: a name matching 2+ bodies is refused, not first-matched ───
# A body's name is only LOCALLY unique (like an occurrence's). Two same-named bodies (e.g. a part
# instanced twice) must ERROR with the candidate list, not silently grab the first. A precise handle is
# never ambiguous. This mirrors OccurrenceRef's _resolve_occurrence house pattern.

def _install_ambiguous_bodies(*, handle_map=None, occ_bodies=()):
    """Wire a design whose ROOT has empty bRepBodies but whose OCCURRENCES each carry a body, so a name
    can match several distinct proxies. `occ_bodies` = list of (name -> body) dicts, one per occurrence.
    handle_map feeds the precise-handle path."""
    import adsk.fusion
    adsk.fusion.BRepBody = BRepBody
    adsk.fusion.MeshBody = MeshBody
    handle_map = handle_map or {}

    # bRepBodies matches the name AS SPELLED, so a case-variant match can only come from the
    # iteration pass, never from the named lookup.
    occs = [make_occurrence(path=f"Sub-{i}:1", component=MakeComp(name=f"Sub-{i}"),
                            bodies=list(m.values()))
            for i, m in enumerate(occ_bodies)]
    root = MakeComp(name="Root", all_occurrences=occs)
    design = MakeDesign(comp=root, tokens=handle_map)
    inp._common.design = lambda: design
    inp._common.target_component = lambda d=None: root
    return root


def _install_native_and_proxy(comp_bodies=(), occ_bodies=(), comp_name="Probe"):
    """Wire the design shape a body's TWO reachable wrappers come from: the ACTIVE component owns
    `comp_bodies` natively, and the root's allOccurrences pass hands back `occ_bodies` - a list of
    (occurrence fullPathName, [bodies]) pairs, the proxies. The root itself owns no bodies, so the
    same physical body arrives once per pass and the de-dup key is what decides how many candidates
    a name has."""
    import adsk.fusion
    adsk.fusion.BRepBody = BRepBody
    adsk.fusion.MeshBody = MeshBody
    comp = MakeComp(name=comp_name, bodies=list(comp_bodies))
    for b in comp_bodies:
        b.parentComponent = comp
    # component: a real Occurrence always answers it; a read that RAISES is the unresolved-reference
    # signal the shared census filters on.
    occs = [make_occurrence(path=path, component=MakeComp(name=path.split(":")[0]),
                            bodies=list(bodies))
            for path, bodies in occ_bodies]
    root = MakeComp(name="Root", all_occurrences=occs)
    # findEntityByToken answers nothing, so every lookup takes the NAME path.
    design = MakeDesign(comp=root, all_components=[root, comp])
    inp._common.design = lambda: design
    inp._common.target_component = lambda d=None: comp
    return comp


# The x-ref shape, measured on a host holding two x-refs of one design: the two documents' 'Frame'
# bodies answer ONE document-local entityToken while their source documents' lineage ids differ.
_URN_A = "urn:adsk.wipprod:dm.lineage:K3I2nkywRlaWPHJexysOdA"
_URN_B = "urn:adsk.wipprod:dm.lineage:N_QoPrrrSJmF__f9BZV86A"
_XREF_TOKEN = "/vB+AAEAAwAAAAAAAAAAAAAA"


def _body_from_document(name, token, urn, comp_name=None):
    """One body owned by a component in the document whose lineage id is `urn` - the chain
    ``_common.native_identity`` reads a body's source document through."""
    comp = MakeComp(name=comp_name or name, parent_design=make_source_document(urn))
    return BRepBody(name, entity_token=token, parent_component=comp)


class TestTheXrefBodyFixture:
    def test_the_two_document_fixture_really_models_the_collision(self):
        # Without the token collision the merge this key prevents never happens; without a
        # native/proxy pair a key that pulls one body apart from its own proxy would look correct.
        a = _body_from_document("Frame", _XREF_TOKEN, _URN_A, comp_name="P2a-Gimbal")
        b = _body_from_document("Frame", _XREF_TOKEN, _URN_B, comp_name="P3-Gimbal")
        assert a is not b and a.entityToken == b.entityToken
        assert (a.parentComponent.parentDesign.parentDocument.dataFile.id
                != b.parentComponent.parentDesign.parentDocument.dataFile.id)
        pa = body_proxy(a, make_occurrence(path="P2a-Gimbal:1"))
        assert pa.entityToken != a.entityToken and pa.nativeObject is a


class TestXrefBodiesAreNotOneBody:
    def _host_and_xref(self):
        """The HOST's own body and an x-ref'd document's body, both named 'Frame' and both answering
        ONE document-local entityToken - the host body reached natively, the x-ref'd one through the
        proxy its occurrence hands back.

        Grouped together these two are not equal partners: a group holding a native and a placement
        offers only the PLACEMENT as a candidate, so the host's own body drops out of the walk
        entirely and the bare name resolves - to the other document's body, with no ambiguity
        refusal and nothing in the payload saying so."""
        host_body = BRepBody("Frame", entity_token=_XREF_TOKEN)
        xref = _body_from_document("Frame", _XREF_TOKEN, _URN_A, comp_name="P2a-Gimbal")
        proxy = body_proxy(xref, make_occurrence(path="P2a-Gimbal:1"))
        _install_native_and_proxy(comp_bodies=[], comp_name="Host",
                                  occ_bodies=[("P2a-Gimbal:1", [proxy])])
        # The host's own body sits at the ROOT, where a body modelled before anything was inserted
        # lives; the x-ref'd one is reachable only through its occurrence.
        root = inp._common.design().rootComponent
        root.bRepBodies = _NamedCollection([host_body])
        host_body.parentComponent = root
        return host_body, proxy

    def test_the_bare_name_is_REFUSED_with_both_documents_bodies_listed(self):
        self._host_and_xref()
        val, err = inp.BodyRef("body").resolve("Frame")
        assert val is None, "the host's own body must not vanish into the x-ref'd body's group"
        assert "ambiguous" in err.lower()
        assert "'Root:Frame'" in err and "'P2a-Gimbal:1:Frame'" in err

    def test_the_LIST_kind_refuses_the_bare_name_too(self):
        # BodyRefList resolves each element through the same walk, so the merge reaches a LIST as a
        # silently SHORTER list: one entry standing for two different documents' bodies.
        self._host_and_xref()
        val, err = inp.BodyRefList("bodies").resolve(["Frame"])
        assert val is None, "a list element naming two distinct bodies must not resolve to one"
        assert "ambiguous" in err.lower()
        assert "'Root:Frame'" in err and "'P2a-Gimbal:1:Frame'" in err

    def test_each_qualified_spelling_the_refusal_offers_resolves_to_its_OWN_body(self):
        # The way out the refusal names has to work, and has to land on two DIFFERENT bodies.
        host_body, proxy = self._host_and_xref()
        val, err = inp.BodyRefList("bodies").resolve(["Root:Frame", "P2a-Gimbal:1:Frame"])
        assert err is None, err
        assert val == [host_body, proxy]

    def test_one_bodys_native_and_proxy_are_still_ONE_candidate(self):
        # The pair's other direction, with a readable document at both ends: the native and its own
        # proxy must NOT become two candidates, or every placed body is ambiguous with itself.
        native = _body_from_document("Frame", _XREF_TOKEN, _URN_A, comp_name="P2a-Gimbal")
        proxy = body_proxy(native, make_occurrence(path="P2a-Gimbal:1"))
        _install_native_and_proxy(comp_bodies=[native], comp_name="P2a-Gimbal",
                                  occ_bodies=[("P2a-Gimbal:1", [proxy])])
        val, err = inp.BodyRef("body").resolve("Frame")
        assert err is None, err
        assert val is proxy                      # the PLACEMENT, which carries the context


def _pin_in(path, token):
    """A body named 'Pin' placed at assembly path `path`. Two of these are DISTINCT physical bodies
    that happen to share a name, so each carries its own entityToken - grouped on one token they
    would be a single candidate and the name would not be ambiguous at all."""
    body = FakeBRep("Pin", is_solid=True, entity_token=token)
    body.assemblyContext = types.SimpleNamespace(fullPathName=path)
    return body


def _two_pins():
    return _pin_in("Sub-A:1", "TOK-PIN-A"), _pin_in("Sub-B:1", "TOK-PIN-B")


class TestBodyNameAmbiguity:
    def test_ambiguous_name_is_refused_with_candidates(self):
        pin_a, pin_b = _two_pins()
        _install_ambiguous_bodies(occ_bodies=[{"Pin": pin_a}, {"Pin": pin_b}])
        val, err = inp.BodyRef("body").resolve("Pin")
        assert val is None
        assert "ambiguous" in err.lower()
        assert "Sub-A:1" in err and "Sub-B:1" in err        # both candidate contexts listed

    def test_a_single_named_body_still_resolves(self):
        only = FakeBRep("Pin", is_solid=True)
        _install_ambiguous_bodies(occ_bodies=[{"Pin": only}])
        val, err = inp.BodyRef("body").resolve("Pin")
        assert err is None and val is only

    def test_one_body_reached_by_two_paths_is_not_falsely_ambiguous(self):
        # One physical body is reachable through several collection paths (active component, root, an
        # occurrence proxy) and the API returns a FRESH wrapper object each time. De-dup MUST key on
        # the stable entityToken, not id() - or the same body counts once per path and reports a
        # spurious ambiguity. Two distinct wrappers, one shared token -> resolves as ONE.
        wrap_a = FakeBRep("Pin", is_solid=True, entity_token="TOK-PIN")
        wrap_b = FakeBRep("Pin", is_solid=True, entity_token="TOK-PIN")
        assert wrap_a is not wrap_b                          # genuinely different objects...
        _install_ambiguous_bodies(occ_bodies=[{"Pin": wrap_a}, {"Pin": wrap_b}])
        val, err = inp.BodyRef("body").resolve("Pin")
        assert err is None and val is not None              # ...but the same body -> not ambiguous

    def test_ambiguity_lists_each_candidate_in_the_qualified_form(self):
        # the refusal must hand back the string that RESOLVES - '<occurrence-or-component>:<body>' -
        # not just a prose "in Sub-A:1", so the caller can re-issue without a second lookup.
        pin_a, pin_b = _two_pins()
        _install_ambiguous_bodies(occ_bodies=[{"Pin": pin_a}, {"Pin": pin_b}])
        val, err = inp.BodyRef("body").resolve("Pin")
        assert val is None and "'Sub-A:1:Pin'" in err and "'Sub-B:1:Pin'" in err

    def test_qualified_scope_body_name_picks_one_of_the_candidates(self):
        pin_a, pin_b = _two_pins()
        _install_ambiguous_bodies(occ_bodies=[{"Pin": pin_a}, {"Pin": pin_b}])
        val, err = inp.BodyRef("body").resolve("Sub-B:1:Pin")
        assert err is None and val is pin_b

    def test_one_body_reached_twice_with_an_unreadable_token_is_not_ambiguous(self):
        # itemByName and item(i) each hand back a FRESH wrapper of the same physical body, and both
        # lookups run (one answers the spelling, the other the case variants + meshes). De-dup keys on
        # the entityToken and, when THAT is unreadable, on (name, scope) - never on object identity,
        # which would refuse ONE body as several candidates all printing the same name.
        owner = types.SimpleNamespace(name="Frame")

        def _unreadable():
            body = BRepBody("Pin", parent_component=owner)
            del body.entityToken            # the token read that does not answer
            return body

        class _FreshColl:
            """Every read mints a NEW wrapper of the one body, as the live collection does - which
            is the whole point here and what a shared collection, handing back one object, cannot
            model."""
            @property
            def count(self):
                return 1
            def item(self, i):
                return _unreadable()
            def itemByName(self, n):
                return _unreadable() if n == "Pin" else None

        root = MakeComp(name="Frame")
        root.bRepBodies = _FreshColl()
        design = MakeDesign(comp=root)
        inp._common.design = lambda: design
        inp._common.target_component = lambda d=None: root
        val, err = inp.BodyRef("body").resolve("Pin")
        assert err is None and val is not None and val.name == "Pin"

    def test_a_slash_qualified_label_resolves_like_the_colon_form(self):
        # model_extrude / model_fillet publish their body labels as '<scope>/<body>'; the
        # resolver accepts that spelling too, so a label a tool printed can be handed straight back.
        pin_a, pin_b = _two_pins()
        _install_ambiguous_bodies(occ_bodies=[{"Pin": pin_a}, {"Pin": pin_b}])
        val, err = inp.BodyRef("body").resolve("Sub-B:1/Pin")
        assert err is None and val is pin_b

    def test_a_case_variant_name_resolves(self):
        # the by-name path matches case-insensitively, so an agent that typed 'pin' is not told the
        # body does not exist (the named lookup alone answers only the spelling it was given).
        only = FakeBRep("Pin", is_solid=True)
        _install_ambiguous_bodies(occ_bodies=[{"Pin": only}])
        val, err = inp.BodyRef("body").resolve("pin")
        assert err is None and val is only

    def test_the_exact_spelling_wins_over_a_case_variant(self):
        # widening to case-insensitive must not manufacture an ambiguity: two bodies differing only
        # in case, asked for by exact spelling, resolve to the one spelled that way.
        pin, lower = FakeBRep("Pin", is_solid=True), FakeBRep("pin", is_solid=True)
        pin.assemblyContext = type("O", (), {"fullPathName": "Sub-A:1"})()
        lower.assemblyContext = type("O", (), {"fullPathName": "Sub-B:1"})()
        _install_ambiguous_bodies(occ_bodies=[{"Pin": pin}, {"pin": lower}])
        val, err = inp.BodyRef("body").resolve("Pin")
        assert err is None and val is pin
        val2, err2 = inp.BodyRef("body").resolve("pin")
        assert err2 is None and val2 is lower

    def test_one_body_reached_natively_and_as_its_ONE_proxy_is_ONE_candidate(self):
        # The two-token shape: a body and its occurrence PROXY carry DIFFERENT entityTokens, and
        # _collect_bodies_by_name reaches the SAME physical body twice - natively through the active
        # component, then as a proxy through the allOccurrences pass. Keyed on each wrapper's own
        # token that is two candidates and the name is refused as ambiguous, listing the one body
        # under both contexts. Grouped by the PHYSICAL body it is one candidate: the placement.
        native = BRepBody("Probe", entity_token="TOK-NATIVE")
        proxy = body_proxy(native, make_occurrence(path="Probe:1"))
        assert proxy.entityToken != native.entityToken     # the measured pair, not a shared token
        assert proxy.nativeObject is native and native.nativeObject is None
        _install_native_and_proxy(comp_bodies=[native], occ_bodies=[("Probe:1", [proxy])])
        val, err = inp.BodyRef("body").resolve("Probe")
        assert err is None, err
        assert val is proxy                                # the PLACEMENT, which carries the context

    def test_a_body_reached_ONLY_as_a_proxy_still_resolves_to_that_proxy(self):
        native = BRepBody("Probe", entity_token="TOK-NATIVE")
        proxy = body_proxy(native, make_occurrence(path="Probe:1"))
        _install_native_and_proxy(comp_bodies=[], occ_bodies=[("Probe:1", [proxy])])
        val, err = inp.BodyRef("body").resolve("Probe")
        assert err is None and val is proxy

    def test_an_UNPLACED_bodys_native_is_the_candidate(self):
        # The native is dropped only when a placement exists to replace it. A body no occurrence
        # references (a root-level body, or a component nothing instances) has only its native, and
        # dropping that would refuse a body that is not ambiguous at all.
        native = BRepBody("Probe", entity_token="TOK-NATIVE")
        _install_native_and_proxy(comp_bodies=[native], occ_bodies=[])
        val, err = inp.BodyRef("body").resolve("Probe")
        assert err is None and val is native

    def test_a_component_placed_TWICE_still_refuses_the_bare_name(self):
        # Two placements of ONE body are two world positions. The physical body groups to one key, but
        # each placement is its own candidate, so the bare name is refused with both instance-qualified
        # spellings - picking either would target a placement the caller never chose.
        native = BRepBody("Pin", entity_token="TOK-NATIVE")
        one = body_proxy(native, make_occurrence(path="Jaw:1"))
        two = body_proxy(native, make_occurrence(path="Jaw:2"))
        _install_native_and_proxy(comp_bodies=[native], comp_name="Jaw",
                                  occ_bodies=[("Jaw:1", [one]), ("Jaw:2", [two])])
        val, err = inp.BodyRef("body").resolve("Pin")
        assert val is None and "ambiguous" in err.lower()
        assert "'Jaw:1:Pin'" in err and "'Jaw:2:Pin'" in err
        assert "'Jaw:Pin'" not in err          # the dropped native is not offered as a candidate

    def test_two_placements_with_unreadable_paths_still_refuse(self):
        # Group members are keyed by each wrapper's OWN token, with the printable context only as a
        # fallback - keyed on the context, two placements whose fullPathName raises read the same
        # "Jaw" string, silently merge, and the ambiguity degrades to a first-placement pick.
        def _raising_path(path):
            return make_occurrence(path=path, raises_on={
                "fullPathName": "4 : An API Object refers to a deleted Object"})
        native = BRepBody("Pin", entity_token="TOK-NATIVE")
        one = body_proxy(native, _raising_path("Jaw:1"))
        two = body_proxy(native, _raising_path("Jaw:2"))
        _install_native_and_proxy(comp_bodies=[native], comp_name="Jaw",
                                  occ_bodies=[("Jaw:1", [one]), ("Jaw:2", [two])])
        val, err = inp.BodyRef("body").resolve("Pin")
        assert val is None and "ambiguous" in err.lower()

    def test_the_COMPONENT_qualified_form_also_refuses_when_placed_twice(self):
        # 'Jaw:Pin' names the component, which both instances answer to - so it is still ambiguous and
        # is refused with the instance-qualified spellings that are not.
        native = BRepBody("Pin", entity_token="TOK-NATIVE")
        one = body_proxy(native, make_occurrence(path="Jaw:1"))
        two = body_proxy(native, make_occurrence(path="Jaw:2"))
        _install_native_and_proxy(comp_bodies=[native], comp_name="Jaw",
                                  occ_bodies=[("Jaw:1", [one]), ("Jaw:2", [two])])
        val, err = inp.BodyRef("body").resolve("Jaw:Pin")
        assert val is None and "ambiguous" in err.lower()
        assert "'Jaw:1:Pin'" in err and "'Jaw:2:Pin'" in err

    def test_one_instance_of_a_twice_placed_component_resolves_by_its_own_name(self):
        # The way OUT of that refusal: the instance-qualified spelling the refusal listed resolves.
        native = BRepBody("Pin", entity_token="TOK-NATIVE")
        one = body_proxy(native, make_occurrence(path="Jaw:1"))
        two = body_proxy(native, make_occurrence(path="Jaw:2"))
        _install_native_and_proxy(comp_bodies=[native], comp_name="Jaw",
                                  occ_bodies=[("Jaw:1", [one]), ("Jaw:2", [two])])
        val, err = inp.BodyRef("body").resolve("Jaw:2:Pin")
        assert err is None and val is two

    def test_two_DIFFERENT_bodies_sharing_a_name_are_still_refused(self):
        # The grouping is per PHYSICAL body: two bodies with their own native tokens group apart and
        # the ambiguity refusal stands, with both contexts listed.
        a, b = BRepBody("Pin", entity_token="TOK-A"), BRepBody("Pin", entity_token="TOK-B")
        pa = body_proxy(a, make_occurrence(path="Jaw:1"))
        pb = body_proxy(b, make_occurrence(path="Clamp:1"))
        _install_native_and_proxy(comp_bodies=[], occ_bodies=[("Jaw:1", [pa]), ("Clamp:1", [pb])])
        val, err = inp.BodyRef("body").resolve("Pin")
        assert val is None and "ambiguous" in err.lower()
        assert "'Jaw:1:Pin'" in err and "'Clamp:1:Pin'" in err

    def test_the_key_of_a_proxy_IS_its_natives_identity(self):
        native = BRepBody("Probe", entity_token="TOK-NATIVE")
        proxy = body_proxy(native, make_occurrence(path="Probe:1"))
        assert inp._body_key(proxy) == inp._body_key(native) == ("TOK-NATIVE", None)

    def test_a_wrapper_that_does_not_answer_nativeObject_keys_on_its_OWN_token(self):
        # nativeObject is read through safe(): a wrapper kind that does not answer it at all still
        # has its own token to key on, and must not be dropped to the (name, scope) fallback.
        b = BRepBody("Probe", entity_token="TOK-ONLY")
        del b.nativeObject
        assert inp._body_key(b) == ("TOK-ONLY", None)

    def test_two_bodies_in_TWO_documents_sharing_one_token_key_APART(self):
        # An entityToken is document-local: two x-ref'd documents' bodies answer the same one
        # (measured). Keyed on the token alone these two DISTINCT bodies group together, and the walk
        # that groups them reports ONE body where there are two - a merge, which leaves no trace.
        a = _body_from_document("Frame", _XREF_TOKEN, _URN_A)
        b = _body_from_document("Frame", _XREF_TOKEN, _URN_B)
        assert inp._body_key(a) != inp._body_key(b)

    def test_one_bodys_native_and_proxy_key_TOGETHER_inside_a_saved_document(self):
        # The constraint the document half must not break: a proxy resolves to the same native, so
        # both halves of the key are read off one entity even when the document IS readable.
        native = _body_from_document("Frame", _XREF_TOKEN, _URN_A)
        proxy = body_proxy(native, make_occurrence(path="Frame:1"))
        assert inp._body_key(proxy) == inp._body_key(native) == (_XREF_TOKEN, _URN_A)

    def test_an_unreadable_token_falls_back_to_name_and_scope(self):
        # With no token at either end the key is (name, scope): two fresh wrappers of one body in one
        # scope collapse, and a same-named body in ANOTHER scope stays a separate candidate.
        frame = types.SimpleNamespace(name="Frame")
        one, again = BRepBody("Pin", parent_component=frame), BRepBody("Pin", parent_component=frame)
        other = BRepBody("Pin", parent_component=types.SimpleNamespace(name="Lid"))
        for b in (one, again, other):
            del b.entityToken
        assert inp._body_key(one) == inp._body_key(again) == ("Pin", "Frame")
        assert inp._body_key(other) != inp._body_key(one)

    def test_a_handle_is_never_ambiguous_even_when_name_is_duplicated(self):
        # the precise path: two 'Pin' bodies exist by name, but a HANDLE resolves ONE directly
        pin_a, pin_b = _two_pins()
        target = FakeBRep("Pin", is_solid=True, entity_token="TOK-PIN-TARGET")
        _install_ambiguous_bodies(handle_map={"HANDLE": target},
                                  occ_bodies=[{"Pin": pin_a}, {"Pin": pin_b}])
        val, err = inp.BodyRef("body").resolve("HANDLE")
        assert err is None and val is target


# ── ModeGuard: declarative precondition, error DERIVED from the requirement (non-invertible) ─────

def _FakeModeDesign(design_type=None, edit_object=None):
    """A design whose designType maps to parametric/direct via the numeric convention (1/0);
    design_type None is the design that answers no mode read at all."""
    return MakeDesign(design_type=design_type, active_edit_object=edit_object)


def _install_mode():
    """Wire BaseFeature so current_design_type / _in_base_feature_scope work (the DesignTypes ints
    come seeded from live_api_facts)."""
    import adsk.fusion
    adsk.fusion.BaseFeature = FakeBaseFeature


class TestModeGuard:
    def test_current_design_type_reads_parametric(self):
        _install_mode()
        assert inp.current_design_type(_FakeModeDesign(design_type=1)) == inp.MODE_PARAMETRIC

    def test_current_design_type_reads_direct(self):
        _install_mode()
        assert inp.current_design_type(_FakeModeDesign(design_type=0)) == inp.MODE_DIRECT

    def test_current_design_type_unknown_when_unreadable(self):
        _install_mode()
        # a design with no designType attribute -> 'unknown', not a crash
        assert inp.current_design_type(_FakeModeDesign(design_type=None)) == "unknown"

    def test_parametric_guard_passes_in_parametric(self):
        _install_mode()
        g = inp.ModeGuard(inp.MODE_PARAMETRIC)
        ok, err = g.check(_FakeModeDesign(design_type=1))
        assert ok is True and err is None

    def test_direct_guard_fails_in_parametric(self):
        _install_mode()
        g = inp.ModeGuard(inp.MODE_DIRECT, why="setByPoint is direct-only.", fix_hint="Switch modes.")
        ok, err = g.check(_FakeModeDesign(design_type=1))
        assert ok is False and err["isError"] is True

    def test_error_names_the_REQUIRED_mode_not_inverted(self):
        # the anti-inversion proof: requiring DIRECT, sitting in PARAMETRIC, the message must say it
        # needs DIRECT (and report the actual PARAMETRIC) — it cannot tell you to switch the wrong way.
        _install_mode()
        g = inp.ModeGuard(inp.MODE_DIRECT)
        ok, err = g.check(_FakeModeDesign(design_type=1))
        msg = err["message"]
        assert f"needs {inp.MODE_DIRECT} mode" in msg
        assert f"in {inp.MODE_PARAMETRIC} mode" in msg

    def test_parametric_guard_error_names_parametric(self):
        # symmetric direction check: requiring PARAMETRIC while DIRECT names PARAMETRIC as the need
        _install_mode()
        g = inp.ModeGuard(inp.MODE_PARAMETRIC)
        ok, err = g.check(_FakeModeDesign(design_type=0))
        assert ok is False and f"needs {inp.MODE_PARAMETRIC} mode" in err["message"]

    def test_base_feature_guard_passes_inside_a_base_feature_scope(self):
        _install_mode()
        des = _FakeModeDesign(design_type=1, edit_object=FakeBaseFeature())
        g = inp.ModeGuard(inp.MODE_BASE_FEATURE)
        ok, err = g.check(des)
        assert ok is True and err is None

    def test_base_feature_guard_fails_without_scope(self):
        _install_mode()
        des = _FakeModeDesign(design_type=1, edit_object=None)
        g = inp.ModeGuard(inp.MODE_BASE_FEATURE)
        ok, err = g.check(des)
        assert ok is False and "BASE-FEATURE edit scope" in err["message"]

    def test_contract_note(self):
        _install_mode()
        assert inp.ModeGuard(inp.MODE_DIRECT).contract_note() == "Requires direct mode."
        assert "base-feature" in inp.ModeGuard(inp.MODE_BASE_FEATURE).contract_note()


# ── ProfileRef / ProfileRefList: stable handle first, legacy {sketch, index} fallback, ORDER-keeping ─

def _profile_sketch_fake(name, profs, ntexts=0, compute_deferred=None):
    """One sketch as the profile resolvers read it: `name`, the counted `profiles` collection an
    index selector addresses, and the counted `sketchTexts` behind the 'text:<i>' address space -
    a member the shared sketch fake does not carry. Each text is tagged '<sketch>#<i>' so a test can
    tell WHICH one resolved. `compute_deferred` sets isComputeDeferred; left None the member is
    DROPPED, so the read raises."""
    sk = Sketch(name=name, profiles=list(profs), is_compute_deferred=bool(compute_deferred))
    sk.sketchTexts = _NamedCollection([types.SimpleNamespace(tag=f"{name}#{i}")
                                       for i in range(ntexts)])
    if compute_deferred is None:
        del sk.isComputeDeferred
    return sk


def _install_profiles(handle_map=None, sketches=None, monkeypatch=None):
    """Wire adsk.fusion.Profile for isinstance and install a design whose one component holds
    `sketches`, each owning a `profiles` counted collection. `handle_map` is its
    findEntityByToken table.

    `sketches`: ordered list of (name, [Profile, ...]) or (name, [...], text_count). The LAST is
    the 'most recent'.

    Built on conftest's `make_design`, so the design answers `rootComponent` and `allComponents` the
    way a live one does. That is what a profile selector actually reads: the by-name sketch resolve
    walks `all_components`, and the locator scan for a handle carrying NO sketch name walks
    `all_components` and nothing else - a design missing those two reads answers that scan with an
    empty component list, so the locator can never find anything through it."""
    import adsk.fusion
    adsk.fusion.Profile = Profile
    comp = MakeComp(name="Root",
                    sketches=[_profile_sketch_fake(*row) for row in (sketches or [])])
    des = make_design(comp=comp, tokens=dict(handle_map or {}))
    if monkeypatch is not None:
        monkeypatch.setattr(inp._common, "design", lambda: des)
        monkeypatch.setattr(inp._common, "target_component", lambda d: comp)
    else:
        inp._common.design = lambda: des
        inp._common.target_component = lambda d: comp
    return comp


class TestProfileRef:
    def test_resolves_a_handle_first(self):
        p = Profile("P")
        _install_profiles(handle_map={"PROF": p})
        val, err = inp.ProfileRef("profile").resolve("PROF")
        assert err is None and val is p

    def test_handle_to_non_profile_rejected(self):
        notp = object()
        _install_profiles(handle_map={"X": notp})
        val, err = inp.ProfileRef("profile").resolve("X")
        assert val is None and "not a profile" in err

    def test_legacy_selector_by_sketch_and_index(self):
        p0, p1 = Profile("p0"), Profile("p1")
        _install_profiles(sketches=[("Sketch1", [p0, p1])])
        val, err = inp.ProfileRef("profile").resolve({"sketch": "Sketch1", "profile_index": 1})
        assert err is None and val is p1

    def test_legacy_selector_blank_sketch_uses_most_recent(self):
        a, b = Profile("a"), Profile("b")
        _install_profiles(sketches=[("Old", [a]), ("New", [b])])
        val, err = inp.ProfileRef("profile").resolve({"profile_index": 0})
        assert err is None and val is b          # most-recent sketch

    def test_legacy_index_out_of_range(self):
        _install_profiles(sketches=[("S", [Profile("p0")])])
        val, err = inp.ProfileRef("profile").resolve({"sketch": "S", "profile_index": 5})
        assert val is None and "out of range" in err

    def test_legacy_unknown_sketch(self):
        _install_profiles(sketches=[("S", [Profile("p0")])])
        val, err = inp.ProfileRef("profile").resolve({"sketch": "Nope", "profile_index": 0})
        assert val is None and "no sketch named" in err

    def test_a_shared_sketch_name_is_refused_under_the_input_name(self, monkeypatch):
        # The selector's sketch lookup states what the design-wide walk READ: a name SEVERAL
        # sketches carry is the refusal naming each owner, never "no sketch named 'S'".
        _install_profiles(sketches=[("S", [Profile("p0")])])
        refusal = "2 sketches are named 'S' ('S' in Root, 'S' in Frame)"
        monkeypatch.setattr(inp._common, "find_or_recent_sketch", lambda d, n: (None, n, refusal))
        val, err = inp.ProfileRef("profile").resolve({"sketch": "S", "profile_index": 0})
        assert val is None
        assert err == f"'profile': {refusal}"
        assert "no sketch named" not in err


class TestProfileRefComputeDeferred:
    """A sketch whose compute is DEFERRED answers `profiles` with the set from before the deferral,
    so a handle minted off that read - or an index into it - can name a region that is not the one
    the caller saw. Both forms are refused naming the flag and the two ways to resume compute."""

    def test_a_handle_off_a_deferred_sketch_is_refused(self, monkeypatch):
        p0 = Profile("p0")
        comp = _install_profiles(handle_map={"PROF": p0},
                                 sketches=[("Stale", [p0], 0, True)], monkeypatch=monkeypatch)
        p0.parentSketch = comp.sketches.itemByName("Stale")
        val, err = inp.ProfileRef("profile").resolve("PROF")
        assert val is None
        assert "isComputeDeferred=true" in err and "'Stale'" in err
        assert "sketch_add_geometry" in err and "sys_execute_script" in err

    def test_an_index_into_a_deferred_sketch_is_refused(self, monkeypatch):
        # the silent path: profile_index=0 hands back whichever profile is first in a set the
        # caller never saw, and the cut lands on it without a word
        _install_profiles(sketches=[("Stale", [Profile("p0")], 0, True)],
                          monkeypatch=monkeypatch)
        val, err = inp.ProfileRef("profile").resolve({"sketch": "Stale", "profile_index": 0})
        assert val is None and "isComputeDeferred=true" in err

    def test_a_sketch_computing_normally_still_resolves(self, monkeypatch):
        p0 = Profile("p0")
        comp = _install_profiles(handle_map={"PROF": p0},
                                 sketches=[("Fine", [p0], 0, False)], monkeypatch=monkeypatch)
        p0.parentSketch = comp.sketches.itemByName("Fine")
        assert inp.ProfileRef("profile").resolve("PROF") == (p0, None)

    def test_an_unreadable_flag_is_not_a_refusal(self, monkeypatch):
        # read_flag answers None for a read that raised; coerced True it would block every handle
        # whose sketch simply does not carry the member
        p0 = Profile("p0")
        comp = _install_profiles(handle_map={"PROF": p0}, sketches=[("Fine", [p0])],
                                 monkeypatch=monkeypatch)
        p0.parentSketch = comp.sketches.itemByName("Fine")
        assert inp.ProfileRef("profile").resolve("PROF") == (p0, None)

    def test_a_handle_whose_owning_sketch_will_not_read_still_resolves(self, monkeypatch):
        # parentSketch absent is not evidence of a deferral either
        p0 = Profile("p0")
        _install_profiles(handle_map={"PROF": p0}, monkeypatch=monkeypatch)
        assert inp.ProfileRef("profile").resolve("PROF") == (p0, None)


class TestProfileRefSchema:
    """The PUBLISHED schema must carry both forms _resolve_one_profile accepts - a handle/text
    STRING and the legacy {sketch, profile_index} OBJECT. A bare "string" type bars a
    schema-validating client from the very selector the input's own description offers."""

    def test_the_single_profile_schema_declares_both_forms(self):
        sch = inp.ProfileRef("profile").schema()
        assert sch["type"] == ["string", "object"]

    def test_the_list_schema_declares_both_forms_per_ELEMENT(self):
        sch = inp.ProfileRefList("profiles").schema()
        assert sch["type"] == "array"
        assert sch["items"] == {"type": ["string", "object"]}

    def test_resolve_inputs_takes_the_object_AND_the_string_through_one_declaration(
            self, monkeypatch):
        p0, p1 = Profile("p0"), Profile("p1")
        _install_profiles(handle_map={"PROFTOK": p1}, sketches=[("Sketch1", [p0, p1])],
                          monkeypatch=monkeypatch)
        spec = [inp.ProfileRef("profile")]
        by_object, err = inp.resolve_inputs(spec, {"profile": {"sketch": "Sketch1",
                                                               "profile_index": 0}})
        assert err is None and by_object["profile"] is p0
        by_string, err = inp.resolve_inputs(spec, {"profile": "PROFTOK"})
        assert err is None and by_string["profile"] is p1

    def test_a_list_takes_an_object_element_beside_a_string_one_in_ORDER(self, monkeypatch):
        p0, p1 = Profile("p0"), Profile("p1")
        _install_profiles(handle_map={"PROFTOK": p1}, sketches=[("Sketch1", [p0, p1])],
                          monkeypatch=monkeypatch)
        vals, err = inp.resolve_inputs(
            [inp.ProfileRefList("profiles")],
            {"profiles": [{"sketch": "Sketch1", "profile_index": 0}, "PROFTOK"]})
        assert err is None and vals["profiles"] == [p0, p1]


class FakeAreaProfile(Profile):
    """A profile carrying areaProperties() at the origin unless a centroid is given - what the
    locator re-find reads."""
    def __init__(self, tag, centroid=(0.0, 0.0, 0.0), area=1.0):
        super().__init__(tag, centroid=centroid, area=area)


class TestProfileHandleLocator:
    """findEntityByToken returns NOTHING for a sub-component sketch profile's token (live API fact),
    so a profile handle's '|@profile[<sketch>~<area>]:<centroid>' locator is its real resolution
    path - these pin that path."""

    def test_dead_token_resolves_via_sketch_area_locator(self):
        band = FakeAreaProfile("band", centroid=(0.0, 0.0, 0.0), area=27.269)
        _install_profiles(sketches=[("OuterRingSketch", [band])])
        h = "DEADTOKEN|@profile[OuterRingSketch~27.2690]:0.000000,0.000000,0.000000"
        val, err = inp.ProfileRef("profile").resolve(h)
        assert err is None and val is band

    def test_area_disambiguates_same_centroid_profiles(self):
        # An annulus band and its full disk share centroid (0,0,0); only the area tells them apart.
        # A centroid-only match would grab whichever scans first - the wrong-region extrude.
        disk = FakeAreaProfile("disk", centroid=(0.0, 0.0, 0.0), area=78.5398)
        band = FakeAreaProfile("band", centroid=(0.0, 0.0, 0.0), area=27.269)
        _install_profiles(sketches=[("OuterRingSketch", [disk, band])])
        h = "DEADTOKEN|@profile[OuterRingSketch~27.2690]:0.000000,0.000000,0.000000"
        val, err = inp.ProfileRef("profile").resolve(h)
        assert err is None and val is band

    def test_wrong_area_is_a_miss_not_a_nearest_grab(self):
        disk = FakeAreaProfile("disk", centroid=(0.0, 0.0, 0.0), area=78.5398)
        _install_profiles(sketches=[("S", [disk])])
        h = "DEADTOKEN|@profile[S~999.0000]:0.000000,0.000000,0.000000"
        val, err = inp.ProfileRef("profile").resolve(h)
        assert val is None and "did not resolve" in err

    def test_far_centroid_is_a_miss(self):
        p = FakeAreaProfile("p", centroid=(5.0, 0.0, 0.0), area=10.0)
        _install_profiles(sketches=[("S", [p])])
        h = "DEADTOKEN|@profile[S~10.0000]:0.000000,0.000000,0.000000"
        val, err = inp.ProfileRef("profile").resolve(h)
        assert val is None and "did not resolve" in err

    def test_a_locator_missing_inside_a_deferred_sketch_names_the_deferral(self):
        # The measured shape: a handle minted before a deferral, whose region the pre-deferral
        # profile set no longer carries. Left unset, the miss reads as a stale handle and offers
        # "re-run find_geometry for a fresh handle" - a remedy no read can supply while deferred.
        old = FakeAreaProfile("old", centroid=(0.0, 0.0, 0.0), area=78.5398)
        _install_profiles(sketches=[("Stale", [old], 0, True)])
        h = "DEADTOKEN|@profile[Stale~27.2690]:0.000000,0.000000,0.000000"
        val, err = inp.ProfileRef("profile").resolve(h)
        assert val is None
        assert "isComputeDeferred=true" in err and "'Stale'" in err
        assert "fresh handle" in err and "handle did not resolve" in err

    def test_a_miss_in_a_sketch_computing_normally_still_reads_as_a_stale_handle(self):
        # the other side of the branch: the deferral sentence must not attach to an ordinary miss
        old = FakeAreaProfile("old", centroid=(0.0, 0.0, 0.0), area=78.5398)
        _install_profiles(sketches=[("Fine", [old], 0, False)])
        err = inp.ProfileRef("profile").resolve(
            "DEADTOKEN|@profile[Fine~27.2690]:0.000000,0.000000,0.000000")[1]
        assert "isComputeDeferred" not in err and "did not resolve" in err

    def test_an_unnamed_locator_scans_the_design_wide_component_walk(self):
        # A locator whose bracket carries NO sketch name has no name to resolve, so _refind_profile
        # gathers its candidates from `all_components` and NOTHING else - there is no active-component
        # or root fallback on that branch. A design that cannot answer allComponents/rootComponent
        # hands that scan an empty list, and this handle misses however many profiles are installed.
        p = FakeAreaProfile("p", centroid=(1.0, 2.0, 3.0), area=10.0)
        _install_profiles(sketches=[("S", [p])])
        val, err = inp.ProfileRef("profile").resolve(
            "DEADTOKEN|@profile:1.000000,2.000000,3.000000")
        assert err is None and val is p

    def test_an_unnamed_locator_does_not_blame_an_unrelated_deferred_sketch(self):
        # A nameless locator scans EVERY sketch, so a deferred sketch holding no profile anywhere
        # near the recorded point would otherwise be named as the cause of this miss.
        far = FakeAreaProfile("far", centroid=(9.0, 9.0, 9.0), area=10.0)
        other = FakeAreaProfile("other", centroid=(5.0, 5.0, 5.0), area=10.0)
        _install_profiles(sketches=[("Unrelated", [far], 0, True), ("Working", [other], 0, False)])
        val, err = inp.ProfileRef("profile").resolve(
            "DEADTOKEN|@profile:1.000000,2.000000,3.000000")
        assert val is None and "did not resolve" in err
        assert "isComputeDeferred" not in err and "Unrelated" not in err

    def test_the_deferred_sketch_at_the_recorded_point_is_the_one_named(self):
        # The signal the scoping keeps: this deferred sketch DOES hold a profile at the locator's
        # point and only its area disagrees, while the other deferred one is somewhere else.
        elsewhere = FakeAreaProfile("elsewhere", centroid=(9.0, 9.0, 9.0), area=10.0)
        at_point = FakeAreaProfile("at_point", centroid=(1.0, 2.0, 3.0), area=78.5398)
        _install_profiles(sketches=[("Elsewhere", [elsewhere], 0, True),
                                    ("AtThePoint", [at_point], 0, True)])
        val, err = inp.ProfileRef("profile").resolve(
            "DEADTOKEN|@profile[~27.2690]:1.000000,2.000000,3.000000")
        assert val is None and "isComputeDeferred=true" in err
        assert "'AtThePoint'" in err and "Elsewhere" not in err


class TestProfileRefComponentScope:
    """SKETCH-6: a {sketch, profile_index} selector addresses a sketch BY NAME, and Fusion numbers
    sketches per component from 1 - so 'Sketch1' in two components identifies nothing. Declaring
    scope_input turns the refusal's way forward into the consuming tool's own 'component' input, and
    the scope must SELECT, not merely soften the message."""

    def _two_components(self, monkeypatch):
        alpha_p, beta_p = Profile("alpha-region"), Profile("beta-region")
        alpha = MakeComp(name="Alpha", sketches=[types.SimpleNamespace(
            name="Sketch1", profiles=_NamedCollection([alpha_p]))])
        beta = MakeComp(name="Beta", sketches=[types.SimpleNamespace(
            name="Sketch1", profiles=_NamedCollection([beta_p]))])
        des = make_design(comp=alpha, all_components=[alpha, beta])
        monkeypatch.setattr(inp._common, "design", lambda: des)
        monkeypatch.setattr(inp._common, "target_component", lambda d: alpha)
        import adsk.fusion
        monkeypatch.setattr(adsk.fusion, "Profile", Profile)
        return alpha_p, beta_p

    def test_the_scope_selects_the_named_components_own_profile(self, monkeypatch):
        # asserted on OBJECT IDENTITY: two profiles under one sketch name, and the scope decides
        # WHICH object comes back. A test asserting only "no error" would pass on either.
        alpha_p, beta_p = self._two_components(monkeypatch)
        kind = inp.ProfileRef("profile", scope_input="component")
        sel = {"sketch": "Sketch1", "profile_index": 0}
        assert kind.resolve(sel, "Beta") == (beta_p, None)
        assert kind.resolve(sel, "Alpha") == (alpha_p, None)

    def test_without_a_scope_the_shared_name_is_refused_naming_the_input(self, monkeypatch):
        self._two_components(monkeypatch)
        val, err = inp.ProfileRef("profile", scope_input="component").resolve(
            {"sketch": "Sketch1", "profile_index": 0}, "")
        assert val is None
        assert "2 sketches are named 'Sketch1'" in err
        assert "as 'component'" in err and "Rename" not in err

    def test_an_undeclared_scope_keeps_the_generic_remedy(self, monkeypatch):
        # a consumer that declares NO component input must not be handed a remedy naming one: its
        # strict schema would reject the very call the refusal told the caller to make.
        self._two_components(monkeypatch)
        val, err = inp.ProfileRef("profile").resolve({"sketch": "Sketch1", "profile_index": 0})
        assert val is None and "as 'component'" not in err
        assert "Rename one" in err


class TestProfileRefListComponentScope:
    """The LIST kind carries its OWN scope gate, a separate line from the single kind's - and it is
    the one model_emboss and model_loft resolve through. Dropping it does not error: a selector
    with no 'sketch' key then falls through to the most recent sketch in the ACTIVE component, so
    a caller that named 'Beta' silently gets Alpha's region."""

    def _two_components(self, monkeypatch):
        alpha_p, beta_p = Profile("alpha-region"), Profile("beta-region")
        alpha = MakeComp(name="Alpha", sketches=[types.SimpleNamespace(
            name="Sketch1", profiles=_NamedCollection([alpha_p]))])
        beta = MakeComp(name="Beta", sketches=[types.SimpleNamespace(
            name="Sketch1", profiles=_NamedCollection([beta_p]))])
        des = make_design(comp=alpha, all_components=[alpha, beta])
        monkeypatch.setattr(inp._common, "design", lambda: des)
        monkeypatch.setattr(inp._common, "target_component", lambda d: alpha)
        import adsk.fusion
        monkeypatch.setattr(adsk.fusion, "Profile", Profile)
        return alpha_p, beta_p

    def test_the_scope_selects_the_named_components_own_profile(self, monkeypatch):
        # OBJECT IDENTITY, both spellings: one sketch name, two components, and the scope decides
        # which profile object comes back.
        alpha_p, beta_p = self._two_components(monkeypatch)
        kind = inp.ProfileRefList("profiles", scope_input="component")
        sel = {"sketch": "Sketch1", "profile_index": 0}
        assert kind.resolve([sel], "Beta") == ([beta_p], None)
        assert kind.resolve([sel], "Alpha") == ([alpha_p], None)

    def test_a_blank_sketch_key_takes_the_SCOPED_components_most_recent(self, monkeypatch):
        # the selector names no sketch, so "most recent" decides - and the scope decides WHOSE.
        # Alpha is active, so an unscoped read answers alpha_p: this is the case a dropped scope
        # resolves silently and wrongly instead of refusing.
        alpha_p, beta_p = self._two_components(monkeypatch)
        kind = inp.ProfileRefList("profiles", scope_input="component")
        assert kind.resolve([{"profile_index": 0}], "Beta") == ([beta_p], None)
        assert kind.resolve([{"profile_index": 0}], "Alpha") == ([alpha_p], None)

    def test_without_a_scope_the_shared_name_is_refused_under_the_element_index(self, monkeypatch):
        self._two_components(monkeypatch)
        val, err = inp.ProfileRefList("profiles", scope_input="component").resolve(
            [{"sketch": "Sketch1", "profile_index": 0}], "")
        assert val is None
        assert err.startswith("'profiles'[0]: ")
        assert "2 sketches are named 'Sketch1'" in err
        assert "as 'component'" in err and "Rename" not in err


class TestProfileLocatorSharedSketchName:
    """A profile handle's locator carries the SKETCH NAME the handle was minted in, and a sketch name
    is only unique within a component - so two components each holding a 'Sketch1' make that name
    identify nothing. The collision and a name NO sketch carries are different reads, and the refusal
    has to tell them apart: reporting a collision as "did not resolve to a profile handle" states a
    fact that was never read. No scope input can fix it - the name came from the handle, not the
    caller."""

    def _design(self, monkeypatch, rows):
        """A design whose components each own named sketches: rows = [(comp, sketch, [profiles])]."""
        comps = []
        for comp_name, sk_name, profs in rows:
            sk = types.SimpleNamespace(name=sk_name, profiles=_NamedCollection(profs))
            comps.append(MakeComp(name=comp_name, sketches=[sk]))
        des = make_design(comp=comps[0], all_components=comps)
        monkeypatch.setattr(inp._common, "design", lambda: des)
        monkeypatch.setattr(inp._common, "target_component", lambda d: comps[0])
        import adsk.fusion
        monkeypatch.setattr(adsk.fusion, "Profile", Profile)
        return des

    def test_one_owner_still_resolves_to_that_exact_profile(self, monkeypatch):
        # the control: a name only ONE sketch carries keeps healing, and it binds THAT object.
        wanted = FakeAreaProfile("wanted", centroid=(0.0, 0.0, 0.0), area=10.0)
        self._design(monkeypatch, [("Alpha", "Plate", [wanted]), ("Beta", "Other", [])])
        h = "DEADTOKEN|@profile[Plate~10.0000]:0.000000,0.000000,0.000000"
        val, err = inp.ProfileRef("profile").resolve(h)
        assert err is None and val is wanted

    def test_a_shared_locator_sketch_name_is_refused_naming_both_owners(self, monkeypatch):
        # the discriminating case: TWO components carry 'Plate', and each holds a profile the
        # locator's centroid+area would match. Nothing may be picked, and the refusal must name the
        # collision - the caller cannot act on "not found" here.
        in_alpha = FakeAreaProfile("alpha-region", centroid=(0.0, 0.0, 0.0), area=10.0)
        in_beta = FakeAreaProfile("beta-region", centroid=(0.0, 0.0, 0.0), area=10.0)
        self._design(monkeypatch, [("Alpha", "Plate", [in_alpha]), ("Beta", "Plate", [in_beta])])
        h = "DEADTOKEN|@profile[Plate~10.0000]:0.000000,0.000000,0.000000"
        val, err = inp.ProfileRef("profile").resolve(h)
        assert val is None
        assert "2 sketches are named 'Plate'" in err
        assert "Alpha" in err and "Beta" in err

    def test_the_collision_reads_differently_from_a_name_nothing_carries(self, monkeypatch):
        # the two misses are distinguishable on the wire: a name NO sketch carries keeps the generic
        # handle sentence; a name SEVERAL carry states the collision instead.
        self._design(monkeypatch, [("Alpha", "Plate", [FakeAreaProfile("a", area=10.0)]),
                                   ("Beta", "Plate", [FakeAreaProfile("b", area=10.0)])])
        shared = inp.ProfileRef("profile").resolve(
            "DEADTOKEN|@profile[Plate~10.0000]:0.000000,0.000000,0.000000")[1]
        absent = inp.ProfileRef("profile").resolve(
            "DEADTOKEN|@profile[Ghost~10.0000]:0.000000,0.000000,0.000000")[1]
        assert "sketches are named" in shared
        assert "sketches are named" not in absent
        assert "did not resolve to a profile handle" in absent
        assert shared != absent

    def test_the_refusal_offers_a_call_the_caller_can_make(self, monkeypatch):
        # the locator's name is not a caller input, so the way through is re-minting the handle from
        # the component meant - sketch_get takes that scope. A rename sentence would be the dead end.
        self._design(monkeypatch, [("Alpha", "Plate", [FakeAreaProfile("a", area=10.0)]),
                                   ("Beta", "Plate", [FakeAreaProfile("b", area=10.0)])])
        err = inp.ProfileRef("profile").resolve(
            "DEADTOKEN|@profile[Plate~10.0000]:0.000000,0.000000,0.000000")[1]
        assert "sketch_get(sketch_name='Plate', component=" in err
        assert "Rename" not in err


class TestProfileLocatorTie:
    """A profile locator carries a centroid and (when the mint could read one) an area - nothing
    else. Two profiles agreeing on both are indistinguishable to it, and the scan order is then the
    only thing left to pick between them, so the resolve is REFUSED naming the sketches. The tie is
    reachable on either branch: design-wide when the handle carries no sketch name, and inside one
    sketch when it does."""

    def _design(self, monkeypatch, rows):
        """rows = [(component, sketch, [profiles])]; a sketch name of None carries NO name
        attribute at all - what a name that does not read looks like to safe()."""
        comps = []
        for comp_name, sk_name, profs in rows:
            sk = types.SimpleNamespace(profiles=_NamedCollection(profs))
            if sk_name is not None:
                sk.name = sk_name
            comps.append(MakeComp(name=comp_name, sketches=[sk]))
        des = make_design(comp=comps[0], all_components=comps)
        monkeypatch.setattr(inp._common, "design", lambda: des)
        monkeypatch.setattr(inp._common, "target_component", lambda d: comps[0])
        import adsk.fusion
        monkeypatch.setattr(adsk.fusion, "Profile", Profile)
        return des

    # No sketch name in the locator - the design-wide scan. _sketch_detail._profiles mints this
    # form when the sketch name holds a ':' or ',', and drops the bracket entirely when the area
    # does not read.
    _UNNAMED = "DEADTOKEN|@profile[~10.0000]:0.000000,0.000000,0.000000"

    def test_two_equally_matching_profiles_are_refused_naming_both_sketches(self, monkeypatch):
        # THE discriminating case: both candidates score identically, so "picked one" and "refused
        # the tie" are only told apart by what comes back - assert neither object did.
        in_alpha = FakeAreaProfile("alpha-region", centroid=(0.0, 0.0, 0.0), area=10.0)
        in_beta = FakeAreaProfile("beta-region", centroid=(0.0, 0.0, 0.0), area=10.0)
        self._design(monkeypatch, [("Alpha", "Plate", [in_alpha]), ("Beta", "Cover", [in_beta])])
        val, err = inp.ProfileRef("profile").resolve(self._UNNAMED)
        assert val is not in_alpha and val is not in_beta and val is None
        assert "2 profiles" in err
        assert "'Plate'" in err and "'Cover'" in err

    def test_a_tie_inside_ONE_named_sketch_is_refused_too(self, monkeypatch):
        # the named branch scopes the scan to one sketch, which does not stop two of its profiles
        # sharing a centroid and an area.
        first = FakeAreaProfile("first", centroid=(0.0, 0.0, 0.0), area=10.0)
        second = FakeAreaProfile("second", centroid=(0.0, 0.0, 0.0), area=10.0)
        self._design(monkeypatch, [("Alpha", "Plate", [first, second])])
        val, err = inp.ProfileRef("profile").resolve(
            "DEADTOKEN|@profile[Plate~10.0000]:0.000000,0.000000,0.000000")
        assert val is not first and val is not second and val is None
        assert "2 profiles" in err and "'Plate'" in err

    def test_a_lone_match_still_binds_that_exact_profile(self, monkeypatch):
        # the control: the tie refusal must not fire on a scan with one winner. Object identity -
        # "no error" would pass on the wrong region.
        wanted = FakeAreaProfile("wanted", centroid=(0.0, 0.0, 0.0), area=10.0)
        other = FakeAreaProfile("other", centroid=(0.0, 0.0, 0.0), area=78.5)
        self._design(monkeypatch, [("Alpha", "Plate", [wanted]), ("Beta", "Cover", [other])])
        val, err = inp.ProfileRef("profile").resolve(self._UNNAMED)
        assert err is None and val is wanted

    @pytest.mark.parametrize("near_first", [True, False])
    def test_a_nearer_candidate_beats_a_farther_one_rather_than_tying_with_it(self, monkeypatch,
                                                                             near_first):
        # both sit inside the 0.1 cm gate, so both are candidates - but their distances differ, so
        # the locator DOES separate them and the nearer one is the answer, not a refusal. Run in
        # both scan orders: only the LATER-scanned loser exercises the tie arm, so a single order
        # leaves half the branch unpinned and a refusal on any second candidate passes.
        near = FakeAreaProfile("near", centroid=(0.0, 0.0, 0.0), area=10.0)
        far = FakeAreaProfile("far", centroid=(0.05, 0.0, 0.0), area=10.0)
        order = [near, far] if near_first else [far, near]
        self._design(monkeypatch, [("Alpha", "Plate", [order[0]]),
                                   ("Beta", "Cover", [order[1]])])
        val, err = inp.ProfileRef("profile").resolve(self._UNNAMED)
        assert err is None and val is near

    def test_the_refusal_offers_the_selector_not_a_re_mint(self, monkeypatch):
        # sketch_get(sketch_name=, component=) is the way out of a SHARED locator sketch name; it is
        # a dead end for a tie, because a re-minted handle carries the same locator and ties again.
        # {sketch, profile_index} is the form that separates two profiles by position.
        self._design(monkeypatch, [("Alpha", "Plate", [FakeAreaProfile("a", area=10.0)]),
                                   ("Beta", "Cover", [FakeAreaProfile("b", area=10.0)])])
        err = inp.ProfileRef("profile").resolve(self._UNNAMED)[1]
        assert "{sketch, profile_index}" in err
        assert "sketch_get(sketch_name=" not in err

    def test_a_bare_profile_locator_carrying_no_area_ties_on_centroid_alone(self, monkeypatch):
        # _profiles drops the whole '[<sketch>~<area>]' bracket when the area does not read, leaving
        # the centroid as the only gate - so two profiles sharing one centroid tie even though their
        # AREAS differ, and the refusal must not claim the areas agreed.
        disk = FakeAreaProfile("disk", centroid=(0.0, 0.0, 0.0), area=78.5)
        band = FakeAreaProfile("band", centroid=(0.0, 0.0, 0.0), area=27.3)
        self._design(monkeypatch, [("Alpha", "Plate", [disk]), ("Beta", "Cover", [band])])
        val, err = inp.ProfileRef("profile").resolve(
            "DEADTOKEN|@profile:0.000000,0.000000,0.000000")
        assert val is not disk and val is not band and val is None
        assert "2 profiles" in err and "nothing in the locator separates them" in err

    def test_the_area_half_of_the_score_separates_two_candidates_inside_the_gate(self, monkeypatch):
        # The rel half of the score decides BINDING vs REFUSING, not just ordering: both areas are
        # inside the 1% gate and both centroids are identical, so distance says nothing and only
        # rel picks the exact-area profile. Object identity - "no error" would pass on either.
        exact = FakeAreaProfile("exact", centroid=(0.0, 0.0, 0.0), area=10.0)
        near = FakeAreaProfile("near", centroid=(0.0, 0.0, 0.0), area=10.05)
        self._design(monkeypatch, [("Alpha", "Plate", [exact]), ("Beta", "Cover", [near])])
        val, err = inp.ProfileRef("profile").resolve(self._UNNAMED)
        assert err is None and val is exact

    def test_more_tied_profiles_than_the_wire_cap_names_some_and_counts_the_rest(self, monkeypatch):
        # The refusal states a total, so the names it shows plus the remainder it counts have to
        # add back up to that total - a list capped at one number and a remainder computed off
        # another would name two sketches, omit three, and claim five.
        cap = inp._common._MAX_NAMED_CANDIDATES
        n = cap + 1
        self._design(monkeypatch, [
            (f"C{i}", f"S{i}", [FakeAreaProfile(f"p{i}", centroid=(0.0, 0.0, 0.0), area=10.0)])
            for i in range(n)])
        val, err = inp.ProfileRef("profile").resolve(self._UNNAMED)
        assert val is None and f"{n} profiles" in err
        listed = sum(1 for i in range(n) if f"'S{i}'" in err)
        # an ABSENT remainder clause counts as nothing omitted, so a list that just drops names
        # fails the sum below instead of crashing this parse.
        m = re.search(r"\(\+(\d+) more not listed\)", err)
        omitted = int(m.group(1)) if m else 0
        assert listed == cap
        assert listed + omitted == n

    def test_a_sketch_whose_name_does_not_read_is_still_counted(self, monkeypatch):
        # the count is the fact the caller acts on, so an unreadable sketch name must not drop a
        # tied candidate out of it.
        a = FakeAreaProfile("a", centroid=(0.0, 0.0, 0.0), area=10.0)
        b = FakeAreaProfile("b", centroid=(0.0, 0.0, 0.0), area=10.0)
        self._design(monkeypatch, [("Alpha", "Plate", [a]), ("Beta", None, [b])])
        val, err = inp.ProfileRef("profile").resolve(self._UNNAMED)
        assert val is None
        assert "2 profiles" in err and "did not read" in err


class TestProfileSelectorSketchScope:
    """The {sketch, profile_index} selector's own sketch lookup - design-wide, not active-component."""

    def test_legacy_named_sketch_resolves_design_wide(self):
        # The sketch lives in a SUB-component while root is active: the {sketch, index} selector
        # must reach it (an active-component-scoped lookup reports 'no sketch named' instead).
        import adsk.fusion
        adsk.fusion.Profile = Profile
        p = Profile("sub-profile")

        root = MakeComp(name="Root")
        sub = MakeComp(name="Frame", sketches=[Sketch(name="FrameSketch", profiles=[p])])
        # findEntityByToken answers nothing, so the selector takes the NAME path.
        design = MakeDesign(comp=root, all_components=[root, sub])
        inp._common.design = lambda: design
        inp._common.target_component = lambda d: root
        val, err = inp.ProfileRef("profile").resolve({"sketch": "FrameSketch", "profile_index": 0})
        assert err is None and val is p


class TestProfileRefList:
    def test_resolves_handles_in_order(self):
        p0, p1, p2 = Profile("0"), Profile("1"), Profile("2")
        _install_profiles(handle_map={"A": p0, "B": p1, "C": p2})
        val, err = inp.ProfileRefList("profiles").resolve(["A", "B", "C"])
        assert err is None and val == [p0, p1, p2]

    def test_order_is_PRESERVED_not_sorted(self):
        # loft order is load-bearing: a reversed input must come back reversed, no sort/dedupe
        p0, p1, p2 = Profile("0"), Profile("1"), Profile("2")
        _install_profiles(handle_map={"A": p0, "B": p1, "C": p2})
        val, err = inp.ProfileRefList("profiles").resolve(["C", "A", "B"])
        assert err is None and val == [p2, p0, p1]

    def test_duplicates_are_NOT_deduped(self):
        p = Profile("0")
        _install_profiles(handle_map={"A": p})
        val, err = inp.ProfileRefList("profiles").resolve(["A", "A"])
        assert err is None and val == [p, p]      # both kept — loft may revisit a section

    def test_mixed_handles_and_legacy_selectors(self):
        ph = Profile("h")
        pl = Profile("l")
        _install_profiles(handle_map={"H": ph}, sketches=[("S", [pl])])
        val, err = inp.ProfileRefList("profiles").resolve(["H", {"sketch": "S", "profile_index": 0}])
        assert err is None and val == [ph, pl]

    def test_one_bad_element_fails_with_index(self):
        p = Profile("0")
        _install_profiles(handle_map={"A": p})
        val, err = inp.ProfileRefList("profiles").resolve(["A", "MISSING"])
        assert val is None and "[1]" in err


# ── the component SCOPE on the by-name sketch kinds ──────────────────────────────────────────────
#
# The discriminating fixture is ONE sketch name in TWO components: two DIFFERENT names both resolve
# whether or not a scope is honoured, so only the shared name shows a scope-blind resolver up - it
# returns one of them with no error, and no caller learns which.

def _scope_sketch(name, profs):
    """A sketch carrying `profs` plus the empty sketchTexts collection the text address space reads,
    which the shared sketch fake does not carry."""
    sk = Sketch(name=name, profiles=list(profs))
    sk.sketchTexts = _NamedCollection([])
    return sk


def _two_components_one_sketch_name(monkeypatch, name="Plate", shared=True):
    """Alpha and Beta each hold a sketch called `name`, each with one distinct profile. Returns
    (alpha_profile, beta_profile) - the two answers a scope has to choose between. shared=False
    leaves the name in Alpha ALONE, so the name identifies a sketch without any scope."""
    pa, pb = Profile("alpha"), Profile("beta")
    alpha = MakeComp(name="Alpha", entity_token="comp-Alpha",
                     sketches=[_scope_sketch(name, [pa])])
    beta = MakeComp(name="Beta", entity_token="comp-Beta",
                    sketches=[_scope_sketch(name if shared else "Other", [pb])])
    # findEntityByToken answers nothing, so every lookup takes the NAME path.
    design = MakeDesign(comp=alpha, all_components=[alpha, beta])
    monkeypatch.setattr(inp._common, "design", lambda: design)
    monkeypatch.setattr(inp._common, "target_component", lambda d: alpha)
    return pa, pb


class TestSketchScopeOnTheKinds:
    def test_an_unscoped_profile_selector_refuses_the_shared_name(self, monkeypatch):
        _two_components_one_sketch_name(monkeypatch)
        val, err = inp.ProfileRef("profile", scope_input="component").resolve(
            {"sketch": "Plate", "profile_index": 0})
        assert val is None and "Alpha" in err and "Beta" in err

    def test_the_scope_picks_that_components_profile(self, monkeypatch):
        pa, pb = _two_components_one_sketch_name(monkeypatch)
        kind = inp.ProfileRef("profile", scope_input="component")
        got_a, err_a = kind.resolve({"sketch": "Plate", "profile_index": 0}, "Alpha")
        got_b, err_b = kind.resolve({"sketch": "Plate", "profile_index": 0}, "Beta")
        assert err_a is None and got_a is pa
        assert err_b is None and got_b is pb

    def test_the_refusal_names_the_consumers_own_input(self, monkeypatch):
        # a tool spelling its scope 'dxf_component' must not be told to pass 'component' - that is
        # a call its own strict schema rejects.
        _two_components_one_sketch_name(monkeypatch)
        val, err = inp.ProfileRef("profile", scope_input="dxf_component").resolve(
            {"sketch": "Plate", "profile_index": 0})
        assert val is None and "'dxf_component'" in err and "ename" not in err

    def test_without_a_scope_input_the_kind_stays_design_wide(self, monkeypatch):
        # The `if scope_input:` gate is what protects a consumer that declares NO scope input: it
        # routes to the unscoped walk, which is handed no `remedy` at all, so the shared refusal
        # keeps its own closing sentence. A gate that let this through would word the remedy off
        # whatever scope_input holds - naming an input the tool's strict schema rejects.
        _two_components_one_sketch_name(monkeypatch)
        val, err = inp.ProfileRef("profile").resolve({"sketch": "Plate", "profile_index": 0},
                                                     "Alpha")
        assert val is None
        assert err.endswith(inp._common._RENAME_REMEDY)
        assert "'None'" not in err                 # no input name is fabricated from an unset scope

    def test_sketch_ref_list_without_a_scope_input_stays_design_wide(self, monkeypatch):
        # SketchRefList carries its own copy of that gate, so it needs its own proof.
        _two_components_one_sketch_name(monkeypatch)
        val, err = inp.SketchRefList("sketches").resolve(["Plate"], "Alpha")
        assert val is None
        assert err.endswith(inp._common._RENAME_REMEDY)
        assert "'None'" not in err

    def test_sketch_ref_list_refuses_the_shared_name_and_the_scope_resolves_it(self, monkeypatch):
        pa, _pb = _two_components_one_sketch_name(monkeypatch)
        kind = inp.SketchRefList("sketches", scope_input="component")
        val, err = kind.resolve(["Plate"])
        assert val is None and "'component'" in err and "ename" not in err
        scoped, serr = kind.resolve(["Plate"], "Alpha")
        assert serr is None and scoped[0].profiles.item(0) is pa

    def test_a_scope_the_design_does_not_hold_is_refused_not_dropped(self, monkeypatch):
        # an input a caller can get wrong without being told is a trap, so an unknown component
        # refuses even where the sketch name would have resolved on its own.
        pa, _pb = _two_components_one_sketch_name(monkeypatch, name="Unique", shared=False)
        kind = inp.SketchRefList("sketches", scope_input="component")
        assert kind.resolve(["Unique"])[0][0].profiles.item(0) is pa      # resolves unscoped
        val, err = kind.resolve(["Unique"], "Gamma")
        assert val is None and "Gamma" in err


# ── a sketch TEXT as a profile input (allow_text) ────────────────────────────────────────────────
#
# A SketchText carries no Profile of its own, and the emboss/extrude createInput contracts take the
# text itself in the profile slot while sweep/revolve/loft do not - so the address resolves to the
# SketchText object, and only where the kind was declared allow_text.

class TestProfileRefSketchText:
    def test_the_published_text_id_resolves_to_the_sketch_text(self, monkeypatch):
        _install_profiles(sketches=[("Nameplate", [], 1)], monkeypatch=monkeypatch)
        val, err = inp.ProfileRef("profiles", allow_text=True).resolve("text:0")
        assert err is None and val.tag == "Nameplate#0"

    def test_the_index_picks_that_text_not_the_first(self, monkeypatch):
        _install_profiles(sketches=[("Nameplate", [], 3)], monkeypatch=monkeypatch)
        val, err = inp.ProfileRef("p", allow_text=True).resolve("text:2")
        assert err is None and val.tag == "Nameplate#2"

    def test_a_sketch_qualified_id_targets_that_sketch_not_the_most_recent(self, monkeypatch):
        # blank-sketch resolution takes the LAST sketch, so an ignored '<sketch>/' prefix stamps the
        # wrong sketch's text - silently, since both resolve to a SketchText.
        _install_profiles(sketches=[("Nameplate", [], 1), ("Later", [], 1)], monkeypatch=monkeypatch)
        val, err = inp.ProfileRef("p", allow_text=True).resolve("Nameplate/text:0")
        assert err is None and val.tag == "Nameplate#0"
        bare, berr = inp.ProfileRef("p", allow_text=True).resolve("text:0")
        assert berr is None and bare.tag == "Later#0"

    def test_a_sketch_name_carrying_a_slash_still_addresses_its_text(self, monkeypatch):
        _install_profiles(sketches=[("Plate/Front", [], 1), ("Later", [], 1)], monkeypatch=monkeypatch)
        val, err = inp.ProfileRef("p", allow_text=True).resolve("Plate/Front/text:0")
        assert err is None and val.tag == "Plate/Front#0"

    def test_an_out_of_range_text_names_the_count_and_the_legal_span(self, monkeypatch):
        _install_profiles(sketches=[("Nameplate", [], 2)], monkeypatch=monkeypatch)
        val, err = inp.ProfileRef("p", allow_text=True).resolve("text:2")
        assert val is None
        assert "'text:2'" in err and "2 sketch text(s)" in err and "text:0..text:1" in err

    def test_a_negative_text_index_is_refused_by_the_range_guard(self, monkeypatch):
        # int('-1') parses, so only the lower bound of the range guard stops it; without that bound
        # 'text:-1' would index sketchTexts from the END and stamp the LAST text.
        _install_profiles(sketches=[("Nameplate", [], 2)], monkeypatch=monkeypatch)
        val, err = inp.ProfileRef("p", allow_text=True).resolve("text:-1")
        assert val is None
        assert "'text:-1' is out of range" in err
        assert "2 sketch text(s)" in err and "(text:0..text:1)" in err

    def test_a_malformed_text_index_is_refused_by_the_grammar_not_the_handle_path(self, monkeypatch):
        # 'text:abc' opens with the text grammar, so the refusal must talk about texts - falling
        # through to "did not resolve to a profile handle" is the dead end in miniature.
        _install_profiles(sketches=[("Nameplate", [], 2)], monkeypatch=monkeypatch)
        kind = inp.ProfileRef("p", allow_text=True)
        for raw in ("text:abc", "text:", "text:1.5"):
            val, err = kind.resolve(raw)
            assert val is None, raw
            assert f"'{raw}' carries no whole-number text index" in err
            assert "2 sketch text(s)" in err and "(text:0..text:1)" in err
            assert "profile handle" not in err

    def test_a_malformed_text_index_is_still_a_text_where_text_is_not_allowed(self, monkeypatch):
        # the same string reaches the allow_text refusal, not the handle path.
        _install_profiles(sketches=[("Nameplate", [], 2)], monkeypatch=monkeypatch)
        val, err = inp.ProfileRef("profile").resolve("text:abc")
        assert val is None and "SKETCH TEXT" in err and "model_emboss" in err

    def test_a_sketch_qualified_malformed_index_names_that_sketch(self, monkeypatch):
        _install_profiles(sketches=[("Nameplate", [], 1), ("Later", [], 3)], monkeypatch=monkeypatch)
        val, err = inp.ProfileRef("p", allow_text=True).resolve("Nameplate/text:x")
        assert val is None
        assert "'Nameplate'" in err and "1 sketch text(s)" in err and "(text:0..text:0)" in err

    def test_a_sketch_holding_no_text_is_refused_by_name(self, monkeypatch):
        _install_profiles(sketches=[("Plain", [Profile("p0")], 0)], monkeypatch=monkeypatch)
        val, err = inp.ProfileRef("p", allow_text=True).resolve("text:0")
        assert val is None and "'Plain' holds none" in err

    def test_a_text_is_REFUSED_where_the_feature_takes_only_profiles(self, monkeypatch):
        # none of sweep/revolve/loft names SketchText in its accepted list, so the kind refuses
        # first rather than handing the API an argument it rejects.
        _install_profiles(sketches=[("Nameplate", [], 1)], monkeypatch=monkeypatch)
        val, err = inp.ProfileRef("profile").resolve("text:0")
        assert val is None
        assert "'text:0'" in err and "SKETCH TEXT" in err and "model_emboss" in err

    def test_a_non_text_entity_id_is_not_taken_for_a_text(self, monkeypatch):
        # only 'text:<i>' is a text address; 'line:0' must fall through to the handle path, not
        # resolve to whatever sits at sketchTexts[0].
        _install_profiles(sketches=[("Nameplate", [], 1)], monkeypatch=monkeypatch)
        val, err = inp.ProfileRef("p", allow_text=True).resolve("line:0")
        assert val is None and "did not resolve to a profile handle" in err

    def test_a_text_only_sketch_teaches_the_text_route_instead_of_draw_a_region(self, monkeypatch):
        # a nameplate sketch has no closed profile and never will, so "draw one" is a dead end.
        _install_profiles(sketches=[("Nameplate", [], 2)], monkeypatch=monkeypatch)
        val, err = inp.ProfileRefList("profiles", allow_text=True).resolve([{"sketch": "Nameplate"}])
        assert val is None
        assert "2 sketch text(s)" in err and "'text:0'..'text:1'" in err
        assert "Draw a closed region" not in err

    def test_the_same_dead_end_stays_plain_where_text_is_not_allowed(self, monkeypatch):
        _install_profiles(sketches=[("Nameplate", [], 2)], monkeypatch=monkeypatch)
        val, err = inp.ProfileRefList("profiles").resolve([{"sketch": "Nameplate"}])
        assert val is None and "Draw a closed region first." in err and "sketch text" not in err

    def test_a_list_keeps_order_across_a_profile_and_a_text(self, monkeypatch):
        prof = Profile("region")
        _install_profiles(handle_map={"H": prof}, sketches=[("Nameplate", [], 1)],
                          monkeypatch=monkeypatch)
        val, err = inp.ProfileRefList("profiles", allow_text=True).resolve(["text:0", "H"])
        assert err is None
        assert val[0].tag == "Nameplate#0" and val[1] is prof

    def test_the_text_address_is_advertised_only_where_it_is_accepted(self):
        assert "text:<i>" in inp.ProfileRefList("profiles", allow_text=True).contract_note()
        assert "text:<i>" not in inp.ProfileRefList("profiles").contract_note()
        assert "text:<i>" in inp.ProfileRef("profile", allow_text=True).schema()["description"]


class TestIsTextRef:
    """is_text_ref is the ROUTING predicate over the same grammar ProfileRef.allow_text resolves:
    a tool whose one input carries a text address, a profile handle AND an index selector asks it
    before is_handle, which answers False for a short non-numeric string like 'text:0'."""

    def test_both_published_forms_are_text_addresses(self):
        assert inp.is_text_ref("text:0") is True
        assert inp.is_text_ref("Nameplate/text:2") is True

    def test_a_sketch_name_carrying_a_slash_still_reads_as_one(self):
        assert inp.is_text_ref("Plate/Front/text:0") is True

    def test_an_address_with_no_whole_number_index_is_not_one(self):
        # an incomplete address must not route: it carries no text to resolve
        for raw in ("text:", "text:abc", "text:1.5", "Nameplate/text:"):
            assert inp.is_text_ref(raw) is False, raw

    def test_index_selectors_are_not_text_addresses(self):
        for raw in (0, "0", "0,2,3", "all", "*", [0, 1], None, "", "line:0"):
            assert inp.is_text_ref(raw) is False, raw

    def test_a_geometry_handle_is_not_a_text_address(self):
        assert inp.is_text_ref("/v4BAAAARlJLZXkAH4sIAAAA" + "x" * 40) is False
        assert inp.is_text_ref("sometoken|@profile:0.4,0.2,0.0") is False

    def test_the_two_routing_predicates_do_not_overlap(self):
        # the measured defect: is_handle reads 'text:0' as an index selector, so a tool asking only
        # is_handle never reaches ProfileRef with it. The two must classify each address exactly once.
        assert inp.is_handle("text:0") is False
        assert inp.is_text_ref("/v4BAAAARlJLZXkAH4sIAAAA" + "x" * 40) is False


# ── OccurrenceRef: fullPathName-preferring, ambiguity-refusing instance resolution ────────────────

# The sentinel _FakeOcc.isReferencedComponent RAISES for - a distinct object, so it can never be
# confused with the True/False the property otherwise answers.
_REF_UNREADABLE = object()


def _FakeOcc(name, full_path, token="", component="", is_reference=False):
    """An occurrence as the resolver reads it: both name forms plus the identity facts a collision
    refusal names - its component, whether it is an external reference, and its entityToken.

    ``component`` is that component's NAME, or the component OBJECT itself where two instances have
    to place the SAME one. ``is_reference=_REF_UNREADABLE`` makes that read RAISE, the third state
    ``_common.read_flag`` answers None for. A caller that renders a definite "local" there has
    published a reference state nothing read."""
    unreadable = is_reference is _REF_UNREADABLE
    return make_occurrence(
        path=full_path, entity_token=token,
        component=(MakeComp(name=component or name.split(":")[0])
                   if isinstance(component, str) else component),
        referenced=False if unreadable else is_reference,
        raises_on=({"isReferencedComponent":
                    "isReferencedComponent is not readable on this occurrence"}
                   if unreadable else None))


def _install_occurrences(*occs, tokens=None):
    """Point _common.design() at a root whose allOccurrences are the given occurrence list, with
    findEntityByToken answering from each occurrence's own entityToken (plus any extra `tokens`
    entries - the non-occurrence entity a handle can point at). adsk.fusion.Occurrence is wired to
    the shared fake so the resolver's type check is real; the autouse fixture puts it back."""
    import adsk.fusion
    adsk.fusion.Occurrence = FakeOccurrence
    by_token = dict(tokens or {})
    for o in occs:
        if getattr(o, "entityToken", ""):
            by_token.setdefault(o.entityToken, o)
    design = MakeDesign(comp=MakeComp(name="Root", all_occurrences=occs), tokens=by_token)
    inp._common.design = lambda: design
    return list(occs)


class TestOccurrenceRef:
    def test_a_handle_resolves_to_that_exact_instance(self):
        # the token is the identity: it picks one of two instances a shared NAME cannot tell apart.
        a = _FakeOcc("Bolt:1", "Sub-A:1+Bolt:1", token="tok-A")
        b = _FakeOcc("Bolt:1", "Sub-B:1+Bolt:1", token="tok-B")
        _install_occurrences(a, b)
        val, err = inp.OccurrenceRef("occ").resolve("tok-B")
        assert err is None and val is b

    def test_a_handle_on_a_non_occurrence_is_refused_naming_the_type(self):
        # silently falling through to the name paths would report "no occurrence matching <token>"
        # and hide what the caller actually passed.
        a = _FakeOcc("Bolt:1", "Bolt:1", token="tok-A")
        _install_occurrences(a, tokens={"tok-body": FakeBRep("Body1")})
        val, err = inp.OccurrenceRef("occ").resolve("tok-body")
        assert val is None
        assert "BRepBody" in err and "not an occurrence" in err

    def test_an_unknown_handle_is_refused_not_guessed(self):
        # a stale/unknown token resolves to nothing and matches no name either - a refusal, never a
        # fall-back to some instance.
        a = _FakeOcc("Bolt:1", "Bolt:1", token="tok-A")
        _install_occurrences(a)
        val, err = inp.OccurrenceRef("occ").resolve("tok-GONE")
        assert val is None and "no occurrence matching" in err

    def test_a_path_worn_by_two_siblings_is_REFUSED_with_both_handles(self):
        # measured: an xref insert plus an import of the same-named source leave two siblings with
        # ONE fullPathName, one referenced and one not. No name form tells them apart, so the refusal
        # carries what does - the discriminator and the handle that addresses each.
        xref = _FakeOcc("CMG-050:1", "CMG-050:1", token="tok-xref", component="CMG-050",
                        is_reference=True)
        imported = _FakeOcc("CMG-050:1", "CMG-050:1", token="tok-import", component="CMG-050")
        _install_occurrences(xref, imported)
        val, err = inp.OccurrenceRef("occ").resolve("CMG-050:1")
        assert val is None, "a path two occurrences wear must not resolve to one of them"
        assert "tok-xref" in err and "tok-import" in err
        assert "referenced" in err and "local" in err
        assert "CMG-050" in err

    def test_each_collided_sibling_resolves_by_its_own_handle(self):
        # the refusal above must hand back keys that WORK - otherwise the collision is a dead end.
        xref = _FakeOcc("CMG-050:1", "CMG-050:1", token="tok-xref", is_reference=True)
        imported = _FakeOcc("CMG-050:1", "CMG-050:1", token="tok-import")
        _install_occurrences(xref, imported)
        assert inp.OccurrenceRef("occ").resolve("tok-import") == (imported, None)
        assert inp.OccurrenceRef("occ").resolve("tok-xref") == (xref, None)

    def test_exact_fullpathname_wins(self):
        a = _FakeOcc("Bolt:1", "Sub-A:1+Bolt:1")
        b = _FakeOcc("Bolt:1", "Sub-B:1+Bolt:1")     # same NAME, different path
        _install_occurrences(a, b)
        val, err = inp.OccurrenceRef("occ").resolve("Sub-B:1+Bolt:1")
        assert err is None and val is b               # picked the RIGHT instance by path

    def test_exact_name_resolves_when_unique(self):
        a = _FakeOcc("Base:1", "Base:1")
        _install_occurrences(a, _FakeOcc("Lid:1", "Lid:1"))
        val, err = inp.OccurrenceRef("occ").resolve("Base:1")
        assert err is None and val is a

    def test_ambiguous_name_is_REFUSED_not_guessed(self):
        # The wrong-instance bug: two instances share the local name. A bare name must ERROR (listing
        # the candidate fullPathNames), NOT silently grab the first.
        a = _FakeOcc("Bolt:1", "Sub-A:1+Bolt:1")
        b = _FakeOcc("Bolt:1", "Sub-B:1+Bolt:1")
        _install_occurrences(a, b)
        val, err = inp.OccurrenceRef("occ").resolve("Bolt")   # substring matches both
        assert val is None
        assert "ambiguous" in err.lower()
        assert "Sub-A:1+Bolt:1" in err and "Sub-B:1+Bolt:1" in err

    def test_duplicated_EXACT_name_is_REFUSED_not_guessed(self):
        # A duplicated EXACT name must refuse with the candidate paths, not resolve to one of them -
        # returning a hit here targets an instance the caller did not choose, which for
        # design_delete_occurrence means deleting the wrong one. The substring branch below refuses
        # the looser input already; the precise-looking one needs the same guarantee.
        a = _FakeOcc("Bolt:1", "Sub-A:1+Bolt:1")
        b = _FakeOcc("Bolt:1", "Sub-B:1+Bolt:1")
        _install_occurrences(a, b)
        val, err = inp.OccurrenceRef("occ").resolve("Bolt:1")   # EXACT name, matches both
        assert val is None, "a duplicated exact name must not resolve to one of the instances"
        assert "Sub-A:1+Bolt:1" in err and "Sub-B:1+Bolt:1" in err

    def test_duplicated_exact_name_still_resolves_by_its_fullPathName(self):
        # The refusal above must not block the unambiguous key the error tells the caller to use.
        a = _FakeOcc("Bolt:1", "Sub-A:1+Bolt:1")
        b = _FakeOcc("Bolt:1", "Sub-B:1+Bolt:1")
        _install_occurrences(a, b)
        val, err = inp.OccurrenceRef("occ").resolve("Sub-A:1+Bolt:1")
        assert err is None and val is a

    def test_unique_substring_resolves(self):
        a = _FakeOcc("LeftBracket:1", "LeftBracket:1")
        _install_occurrences(a, _FakeOcc("Plate:1", "Plate:1"))
        val, err = inp.OccurrenceRef("occ").resolve("bracket")  # case-insensitive, unique
        assert err is None and val is a

    def test_miss_lists_available_paths(self):
        _install_occurrences(_FakeOcc("A:1", "A:1"), _FakeOcc("B:1", "B:1"))
        val, err = inp.OccurrenceRef("occ").resolve("Nope")
        assert val is None and "A:1" in err and "B:1" in err

    def test_required_blank_errors(self):
        val, err = inp.OccurrenceRef("occ", required=True).resolve("")
        assert val is None and "required" in err


class TestOccurrenceRefList:
    def test_a_handle_element_resolves_beside_a_path_element(self):
        a = _FakeOcc("X:1", "A:1+X:1", token="tok-a")
        b = _FakeOcc("X:1", "B:1+X:1", token="tok-b")
        _install_occurrences(a, b)
        val, err = inp.OccurrenceRefList("occs").resolve(["tok-b", "A:1+X:1"])
        assert err is None and val == [b, a]

    def test_resolves_each_by_path_in_order(self):
        a = _FakeOcc("X:1", "A:1+X:1")
        b = _FakeOcc("X:1", "B:1+X:1")
        _install_occurrences(a, b)
        val, err = inp.OccurrenceRefList("occs").resolve(["B:1+X:1", "A:1+X:1"])
        assert err is None and val == [b, a]

    def test_comma_string_accepted(self):
        a = _FakeOcc("P:1", "P:1")
        b = _FakeOcc("Q:1", "Q:1")
        _install_occurrences(a, b)
        val, err = inp.OccurrenceRefList("occs").resolve("P:1, Q:1")
        assert err is None and val == [a, b]

    def test_one_ambiguous_element_fails_whole_list(self):
        a = _FakeOcc("Bolt:1", "Sub-A:1+Bolt:1")
        b = _FakeOcc("Bolt:1", "Sub-B:1+Bolt:1")
        _install_occurrences(a, b)
        val, err = inp.OccurrenceRefList("occs").resolve(["Sub-A:1+Bolt:1", "Bolt"])
        assert val is None and "ambiguous" in err.lower() and "occs[1]" in err


class TestSharedResolverBehaviour:
    """Behavioural anchors for _resolve_occurrence ITSELF - the canonical resolver
    test_no_first_match_resolvers.py points every routed tool at. OccurrenceRef wraps it, but a tool may
    also call it directly, so the helper's own contract (a handle is the exact identity; fullPathName
    beats a same-named instance; an ambiguous bare name or a collided path errors) is pinned here,
    not only through the kind."""

    def test_a_handle_resolves_directly(self):
        a = _FakeOcc("Bolt:1", "Sub-A:1+Bolt:1", token="tok-A")
        b = _FakeOcc("Bolt:1", "Sub-B:1+Bolt:1", token="tok-B")
        _install_occurrences(a, b)
        occ, err = inp._resolve_occurrence("t", "tok-A")
        assert err is None and occ is a

    def test_a_collided_path_errors_with_the_handles(self):
        one = _FakeOcc("CMG-050:1", "CMG-050:1", token="tok-1", is_reference=True)
        two = _FakeOcc("CMG-050:1", "CMG-050:1", token="tok-2")
        _install_occurrences(one, two)
        occ, err = inp._resolve_occurrence("t", "CMG-050:1")
        assert occ is None and "tok-1" in err and "tok-2" in err

    def test_fullpath_beats_a_same_named_instance(self):
        a = _FakeOcc("Bolt:1", "Sub-A:1+Bolt:1")
        b = _FakeOcc("Bolt:1", "Sub-B:1+Bolt:1")
        _install_occurrences(a, b)
        occ, err = inp._resolve_occurrence("t", "Sub-B:1+Bolt:1")
        assert err is None and occ is b

    def test_ambiguous_bare_name_errors(self):
        a = _FakeOcc("Bolt:1", "Sub-A:1+Bolt:1")
        b = _FakeOcc("Bolt:1", "Sub-B:1+Bolt:1")
        _install_occurrences(a, b)
        occ, err = inp._resolve_occurrence("t", "Bolt")
        assert occ is None and "ambiguous" in err.lower()

    def test_a_leading_space_name_is_reached_by_the_name_the_caller_can_type(self):
        # MEASURED: Fusion mints occurrence names with a leading space, and no listing shows it. Both
        # occurrences are NESTED, so no fullPathName can equal the typed string and the NAME branch
        # is the only one that can answer; the sibling contains the stem, so substring gives two.
        spaced = _FakeOcc(" Handle:1", "Asm:1+ Handle:1", token="tok-spaced")
        longer = _FakeOcc("Handle:10", "Asm:1+Handle:10", token="tok-longer")
        _install_occurrences(spaced, longer)
        occ, err = inp._resolve_occurrence("t", "Handle:1")
        assert err is None and occ is spaced

    def test_two_names_differing_only_by_that_space_are_one_ambiguity(self):
        # the other half of the same decision: stripping cannot silently pick the unpadded one.
        # Nested again, so the unpadded sibling cannot be reached by its PATH instead.
        spaced = _FakeOcc(" Handle:1", "Asm:1+ Handle:1", token="tok-spaced")
        plain = _FakeOcc("Handle:1", "Asm:2+Handle:1", token="tok-plain")
        _install_occurrences(spaced, plain)
        occ, err = inp._resolve_occurrence("t", "Handle:1")
        assert occ is None
        assert "names 2 occurrences" in err
        assert "Asm:1+ Handle:1" in err and "Asm:2+Handle:1" in err


# ── the by-NAME candidate list has to DISCRIMINATE ───────────────────────────────────────────────
# Two occurrences sharing a NAME can share a PATH as well (a local component named like an xref
# mints its own ':N' sequence), so a candidate list of raw fullPathNames prints one string twice,
# restates its own count and names nothing the caller can pass. The paths go through
# _common.told_apart, and where they collide the refusal SAYS so and offers the handle - the
# address that does tell them apart within this one document.

def _collided_pair():
    """Two occurrences under one parent wearing one NAME and one byte-identical fullPathName - the
    xref-plus-import sibling shape. Their bare name is what a caller types, so the resolver reaches
    them through the exact-NAME branch, not the path branch.

    The imported sibling's COMPONENT is named 'SourcePart', a string its path does not contain: an
    occurrence carries its own name, so the component behind it need not be spelled anywhere in the
    path, and a discriminator that dropped the component field would still read right against a
    fixture whose component name the path happens to repeat."""
    xref = _FakeOcc("CMG-050:1", "Assy:1+CMG-050:1", token="tok-xref", component="CMG-050",
                    is_reference=True)
    imported = _FakeOcc("CMG-050:1", "Assy:1+CMG-050:1", token="tok-import", component="SourcePart")
    _install_occurrences(xref, imported)
    return xref, imported


class TestNameAmbiguityCandidatesDiscriminate:
    def test_a_duplicated_name_whose_paths_collide_names_the_handles(self):
        _collided_pair()
        occ, err = inp._resolve_occurrence("t", "CMG-050:1")
        assert occ is None
        assert "do not tell them all apart" in err
        assert "tok-xref" in err and "tok-import" in err

    def test_every_discriminator_field_is_pinned_to_ITS_OWN_occurrence(self):
        # asserting the fields separately cannot see a swap: with one referenced and one local hit,
        # both words appear whichever occurrence carries which. Each row is pinned whole, so the
        # component, the reference state and the handle have to agree about the SAME instance.
        _collided_pair()
        occ, err = inp._resolve_occurrence("t", "CMG-050:1")
        assert occ is None
        assert "component 'CMG-050', referenced, handle 'tok-xref'" in err
        assert "component 'SourcePart', local, handle 'tok-import'" in err

    def test_an_unreadable_reference_state_is_never_rendered_as_a_definite_one(self):
        # read_flag answers None when the property raises, and None is not "local" - publishing a
        # definite state there claims a fact the read never produced. Both hits are unreadable, so
        # a fixture where the OTHER row supplies the word cannot mask it.
        xref = _FakeOcc("CMG-050:1", "Assy:1+CMG-050:1", token="tok-a", component="CMG-050",
                        is_reference=_REF_UNREADABLE)
        other = _FakeOcc("CMG-050:1", "Assy:1+CMG-050:1", token="tok-b", component="SourcePart",
                         is_reference=_REF_UNREADABLE)
        _install_occurrences(xref, other)
        occ, err = inp._resolve_occurrence("t", "CMG-050:1")
        assert occ is None
        assert "reference state unreadable" in err
        assert "local" not in err and "referenced," not in err
        assert "component 'CMG-050', reference state unreadable, handle 'tok-a'" in err

    def test_a_collided_name_refusal_withholds_the_path_remedy(self):
        # "pass the exact fullPathName" would send the caller back with the string that just failed
        _collided_pair()
        occ, err = inp._resolve_occurrence("t", "CMG-050:1")
        assert occ is None
        assert "Pass the exact fullPathName" not in err
        assert "Pass the 'handle' of the one you mean" in err

    def test_each_collided_sibling_resolves_by_the_handle_the_refusal_named(self):
        # the refusal has to hand back keys that WORK, or the collision is a dead end
        xref, imported = _collided_pair()
        assert inp._resolve_occurrence("t", "tok-xref") == (xref, None)
        assert inp._resolve_occurrence("t", "tok-import") == (imported, None)

    def test_distinct_paths_are_still_listed_plainly(self):
        # the substitution fires ONLY on a repeat: where the paths already separate the hits, adding
        # a component/handle parenthetical to each row is noise the caller has to read past
        a = _FakeOcc("Bolt:1", "Sub-A:1+Bolt:1", token="tok-a")
        b = _FakeOcc("Bolt:1", "Sub-B:1+Bolt:1", token="tok-b")
        _install_occurrences(a, b)
        occ, err = inp._resolve_occurrence("t", "Bolt:1")
        assert occ is None
        assert "Sub-A:1+Bolt:1" in err and "Sub-B:1+Bolt:1" in err
        assert "do not tell them all apart" not in err
        assert "component '" not in err and "tok-a" not in err
        assert "Pass the exact fullPathName" in err

    def test_one_collided_pair_among_distinct_paths_still_names_the_handles(self):
        # a MIXED list: the two that collide get their discriminators, the third keeps its plain
        # path, and the refusal claims only that they are not ALL told apart
        a = _FakeOcc("Bolt:1", "Sub-A:1+Bolt:1", token="tok-a")
        b = _FakeOcc("Bolt:1", "Sub-B:1+Bolt:1", token="tok-b")
        c = _FakeOcc("Bolt:1", "Sub-B:1+Bolt:1", token="tok-c")
        _install_occurrences(a, b, c)
        occ, err = inp._resolve_occurrence("t", "Bolt:1")
        assert occ is None
        assert "do not tell them all apart" in err
        assert "tok-b" in err and "tok-c" in err
        assert "tok-a" not in err                      # its own path already addresses it
        assert "Sub-A:1+Bolt:1" in err

    def test_a_substring_hit_whose_paths_collide_points_at_the_handle(self):
        # the same list feeds the substring branch, which a bare stem reaches when no name matches
        # exactly - so it carries the same collision, and needs the same discriminator
        _collided_pair()
        occ, err = inp._resolve_occurrence("t", "CMG")
        assert occ is None
        assert "ambiguous" in err.lower()
        assert "do not tell them all apart" in err
        assert "tok-xref" in err and "tok-import" in err

    def test_the_candidate_list_counts_what_the_cap_left_out(self):
        # a silently truncated list reads as the COMPLETE set, so a caller picking its next call out
        # of it never learns the row it wanted was cut
        occs = [_FakeOcc("Bolt:1", f"Sub-{i}:1+Bolt:1", token=f"tok-{i}") for i in range(11)]
        _install_occurrences(*occs)
        occ, err = inp._resolve_occurrence("t", "Bolt:1")
        assert occ is None
        assert "+3 more not listed" in err
        assert "Sub-7:1+Bolt:1" in err and "Sub-8:1+Bolt:1" not in err


# ── the by-PATH candidate list is its own rendering, and needs its own pins ──────────────────────
# The exact-fullPathName branch lists ONLY discriminators - the path is already quoted in the
# sentence, so repeating it per row separates nothing. That makes it a second renderer with a second
# set of promises: every row addresses its own instance, and the cap discloses what it cut. The
# by-NAME pins above cannot see either, because they never reach this branch.

class TestPathCollisionCandidates:
    def test_each_row_is_pinned_to_ITS_OWN_occurrence(self):
        # asserting the fields separately cannot see a swap: with one referenced and one local hit,
        # both words appear whichever occurrence carries which. Each row is pinned whole, so the
        # component, the reference state and the handle have to agree about the SAME instance.
        _collided_pair()
        occ, err = inp._resolve_occurrence("t", "Assy:1+CMG-050:1")
        assert occ is None
        assert "is worn by 2 occurrences" in err
        assert "component 'CMG-050', referenced, handle 'tok-xref'" in err
        assert "component 'SourcePart', local, handle 'tok-import'" in err

    def test_an_unreadable_reference_state_is_never_rendered_as_a_definite_one(self):
        # read_flag answers None when the property raises, and None is not "local". Both hits are
        # unreadable, so a fixture where the OTHER row supplies the word cannot mask it.
        one = _FakeOcc("CMG-050:1", "Assy:1+CMG-050:1", token="tok-a", component="CMG-050",
                       is_reference=_REF_UNREADABLE)
        two = _FakeOcc("CMG-050:1", "Assy:1+CMG-050:1", token="tok-b", component="SourcePart",
                       is_reference=_REF_UNREADABLE)
        _install_occurrences(one, two)
        occ, err = inp._resolve_occurrence("t", "Assy:1+CMG-050:1")
        assert occ is None
        assert "component 'CMG-050', reference state unreadable, handle 'tok-a'" in err
        assert "local" not in err and "referenced," not in err

    def test_the_candidate_list_counts_what_the_cap_left_out(self):
        # a silently truncated list reads as EVERY instance wearing that path, and the handle the
        # caller retries with is picked out of it - so a hit that was cut is one it never learns of.
        occs = [_FakeOcc("Bolt:1", "Sub:1+Bolt:1", token=f"tok-{i}") for i in range(11)]
        _install_occurrences(*occs)
        occ, err = inp._resolve_occurrence("t", "Sub:1+Bolt:1")
        assert occ is None
        assert "is worn by 11 occurrences" in err
        assert "+3 more not listed" in err
        assert "tok-7" in err and "tok-8" not in err

    def test_the_rows_stay_separable_where_a_discriminator_carries_commas(self):
        # the shared renderer joins on ', ' and a discriminator holds two commas of its own, so an
        # unbracketed join prints six comma-separated fragments for two hits and no row boundary.
        _collided_pair()
        occ, err = inp._resolve_occurrence("t", "Assy:1+CMG-050:1")
        assert occ is None
        assert err.count("(component ") == 2
        assert "handle 'tok-xref'), (component 'SourcePart'" in err


# ── TargetRef (the polymorphic measure/colour target) ───────────────────────

def _install_target(*, handle_map=None, occurrences=(), components=(), brep_named=None, mesh_named=None):
    """A design wired for every TargetRef path: findEntityByToken (handle), allOccurrences (occurrence),
    allComponents (component), bRepBodies/meshBodies (body-by-name)."""
    import adsk.fusion
    adsk.fusion.BRepBody = BRepBody
    adsk.fusion.MeshBody = MeshBody
    adsk.fusion.BRepFace = BRepFace
    handle_map = handle_map or {}
    brep_named = brep_named or {}
    mesh_named = mesh_named or {}

    root = MakeComp(name="Root", bodies=list(brep_named.values()),
                    mesh_bodies=list(mesh_named.values()), all_occurrences=occurrences)
    # component -> its occurrences (for TargetRefList's component->occurrence mapping). A component
    # named "X" maps to the occurrence(s) whose component.name is "X".
    root.allOccurrencesByComponent = lambda c: [
        o for o in occurrences
        if getattr(getattr(o, "component", None), "name", None) == c.name]
    # allComponents lives on the DESIGN in the live API (Component has no such attribute)
    design = MakeDesign(comp=root, all_components=[root] + [MakeComp(name=n) for n in components],
                        tokens=handle_map)
    inp._common.design = lambda: design
    inp._common.target_component = lambda d=None: root
    return design


class TestTargetRef:
    def test_empty_is_whole_design(self):
        d = _install_target()
        (ent, kind), err = inp.TargetRef("target").resolve("")
        assert err is None and kind == "design" and ent is d.rootComponent

    def test_handle_to_body(self):
        b = FakeBRep("Body1", is_solid=True)
        _install_target(handle_map={"H": b})
        (ent, kind), err = inp.TargetRef("target").resolve("H")
        assert err is None and kind == "body" and ent is b

    def test_handle_to_face(self):
        f = FakePlanarFace()
        _install_target(handle_map={"H": f})
        (ent, kind), err = inp.TargetRef("target").resolve("H")
        assert err is None and kind == "face"

    def test_handle_to_mesh(self):
        m = MeshBody("M1")
        _install_target(handle_map={"H": m})
        (ent, kind), err = inp.TargetRef("target").resolve("H")
        assert err is None and kind == "mesh" and ent is m

    def test_occurrence_by_fullpath(self):
        occ = _FakeOcc("Wheel:1", "Chassis:1+Wheel:1")
        _install_target(occurrences=[occ])
        (ent, kind), err = inp.TargetRef("target").resolve("Chassis:1+Wheel:1")
        assert err is None and kind == "occurrence" and ent is occ

    def test_a_component_name_one_component_carries_resolves(self):
        _install_target(components=["Bracket"])
        (ent, kind), err = inp.TargetRef("target").resolve("Bracket")
        assert err is None and kind == "component" and ent.name == "Bracket"

    def test_a_name_two_components_carry_is_refused(self):
        # the component path refuses like the occurrence path above it: measuring/colouring one of
        # two same-named components without saying which is the wrong-entity bug this kind exists
        # to prevent
        _install_target(components=["Bracket", "Bracket"])
        res, err = inp.TargetRef("target").resolve("Bracket")
        assert res is None
        assert "2 components match 'Bracket'" in err
        # handle and occurrence were both tried BEFORE this step, so both still resolve on a retry
        assert "occurrence name/fullPathName" in err and "find_geometry" in err
        assert "rename" not in err.lower()

    def test_a_placed_components_name_answers_with_its_OCCURRENCE(self):
        # Fusion names an instance '<component>:<n>', so the occurrence step's name match reaches a
        # placed component's own name before the component step below it ever runs. The occurrence
        # is the right answer - it carries the placement - and this is the boundary the next test
        # marks the other side of: the component step is what answers for a component NOTHING places.
        _install_target(occurrences=[_FakeOcc("Frame:1", "Frame:1")], components=["Frame"])
        (ent, kind), err = inp.TargetRef("target").resolve("Frame")
        assert err is None and kind == "occurrence" and ent.fullPathName == "Frame:1"

    def test_an_UNPLACED_components_name_resolves_through_the_component_step(self):
        # A component no occurrence places has no occurrence spelling at all, so the component step
        # is its ONLY address: the root component (never placed - it IS the design) and a component
        # whose instances were removed both land here. Every occurrence in the design is unrelated,
        # so the step above answers a plain miss and this one answers the name.
        _install_target(occurrences=[_FakeOcc("Frame:1", "Frame:1")], components=["Fixture"])
        (ent, kind), err = inp.TargetRef("target").resolve("Fixture")
        assert err is None and kind == "component" and ent.name == "Fixture"
        # the root is the always-present case of the same shape - no occurrence places it either
        (root_ent, root_kind), root_err = inp.TargetRef("target").resolve("Root")
        assert root_err is None and root_kind == "component" and root_ent.name == "Root"

    def test_body_by_name(self):
        b = FakeBRep("Plate", is_solid=True)
        _install_target(brep_named={"Plate": b})
        (ent, kind), err = inp.TargetRef("target").resolve("Plate")
        assert err is None and kind == "body" and ent is b

    def test_allow_restricts_kind(self):
        # a mesh-only TargetRef refuses a brep-body handle
        b = FakeBRep("Body1", is_solid=True)
        _install_target(handle_map={"H": b})
        res, err = inp.TargetRef("target", allow=("mesh",)).resolve("H")
        assert res is None and "mesh" in err.lower()

    def test_unresolvable_errors(self):
        _install_target()
        res, err = inp.TargetRef("target").resolve("Ghost")
        assert res is None and "Ghost" in err

    def test_ambiguous_occurrence_name_errors_with_candidates(self):
        # An ambiguous occurrence match across DIFFERENT components must propagate
        # _resolve_occurrence's ambiguity error (with the candidate fullPathNames), not fall through
        # to a generic "did not resolve" miss - there is no single answer to give.
        a = _FakeOcc("Bolt:1", "Sub-A:1+Bolt:1",
                     component=MakeComp(name="BoltA", entity_token="tA"))
        b = _FakeOcc("Bolt:1", "Sub-B:1+Bolt:1",
                     component=MakeComp(name="BoltB", entity_token="tB"))
        _install_target(occurrences=[a, b])
        res, err = inp.TargetRef("target").resolve("Bolt")
        assert res is None
        assert "ambiguous" in err.lower()
        assert "Sub-A:1+Bolt:1" in err and "Sub-B:1+Bolt:1" in err

    def test_a_path_TWO_occurrences_wear_is_refused_naming_both_candidates(self):
        # A local component named like an x-ref'd one numbers in its OWN :N sequence, so both
        # occurrences answer to one fullPathName and no string tells them apart. The resolver's
        # refusal names each candidate's reference state and handle; it carries no word this step
        # could match on, so only the OCCURRENCE_MISS stem keeps it from falling through to the
        # component/body vocabularies and ending on the miss text - which would deny that anything
        # of that name exists while the design holds two.
        local = _FakeOcc("ProbeFrame:1", "ProbeFrame:1", token="tok-local",
                         component="ProbeFrame", is_reference=False)
        xref = _FakeOcc("ProbeFrame:1", "ProbeFrame:1", token="tok-xref",
                        component="ProbeFrame", is_reference=True)
        _install_target(occurrences=[local, xref])
        res, err = inp.TargetRef("target").resolve("ProbeFrame:1")
        assert res is None
        assert "worn by 2 occurrences" in err
        assert "tok-local" in err and "tok-xref" in err     # the only unique addresses there are
        assert "local" in err and "referenced" in err
        assert "did not resolve" not in err                 # the miss text must not mask it

    def test_a_name_TWO_occurrences_answer_to_is_refused_naming_their_paths(self):
        # The resolver's other wordless refusal - an exact NAME several instances answer to - which
        # the same stem test propagates.
        a = _FakeOcc("Bolt:2", "Sub-A:1+Bolt:2")
        b = _FakeOcc("Bolt:2", "Sub-B:1+Bolt:2")
        _install_target(occurrences=[a, b])
        res, err = inp.TargetRef("target").resolve("Bolt:2")
        assert res is None
        assert "names 2 occurrences" in err
        assert "Sub-A:1+Bolt:2" in err and "Sub-B:1+Bolt:2" in err
        assert "did not resolve" not in err

    def test_a_genuine_miss_still_falls_through_to_the_body_vocabulary(self):
        # The boundary the stem test exists to keep: a name NO occurrence carries is a plain miss,
        # and the body step below it is where it resolves. Propagating that miss would break every
        # body/component name this kind accepts.
        plate = FakeBRep("Plate", is_solid=True)
        _install_target(occurrences=[_FakeOcc("Wheel:1", "Wheel:1")], brep_named={"Plate": plate})
        (ent, kind), err = inp.TargetRef("target").resolve("Plate")
        assert err is None and kind == "body" and ent is plate

    def _instances_of_one_component(self):
        shared = MakeComp(name="Bolt", entity_token="tBolt")
        a = _FakeOcc("Bolt:1", "Sub-A:1+Bolt:1", component=shared)
        b = _FakeOcc("Bolt:1", "Sub-B:1+Bolt:1", component=shared)
        _install_target(occurrences=[a, b])
        return shared

    def test_a_default_target_still_REFUSES_instances_of_one_component(self):
        # The blast radius of a default TargetRef must not widen: for appearance_set / model_inspect /
        # model_set_material, an ambiguous 'Bolt' is still a refusal, not "act on the component"
        # (which would colour or re-material every instance).
        self._instances_of_one_component()
        res, err = inp.TargetRef("target").resolve("Bolt")
        assert res is None and "ambiguous" in err.lower()

    def test_an_OPTED_IN_target_resolves_instances_of_one_component_to_it(self):
        # The instance-only ambiguity, for a caller whose target IS the component: every hit is an
        # instance of the SAME one, so the component is the unambiguous answer. Opt-in only.
        shared = self._instances_of_one_component()
        (ent, kind), err = inp.TargetRef(
            "target", collapse_ambiguous_occurrences=True).resolve("Bolt")
        assert err is None and kind == "component" and ent is shared

    def test_the_opt_in_still_refuses_when_the_caller_takes_no_component(self):
        self._instances_of_one_component()
        res, err = inp.TargetRef("target", allow=("occurrence",),
                                 collapse_ambiguous_occurrences=True).resolve("Bolt")
        assert res is None and "ambiguous" in err.lower()

    def test_the_opt_in_still_refuses_instances_of_DIFFERENT_components(self):
        a = _FakeOcc("Bolt:1", "Sub-A:1+Bolt:1",
                     component=MakeComp(name="BoltA", entity_token="tA"))
        b = _FakeOcc("Bolt:1", "Sub-B:1+Bolt:1",
                     component=MakeComp(name="BoltB", entity_token="tB"))
        _install_target(occurrences=[a, b])
        res, err = inp.TargetRef(
            "target", collapse_ambiguous_occurrences=True).resolve("Bolt")
        assert res is None and "ambiguous" in err.lower()

    def test_ambiguous_body_name_errors_with_candidates(self):
        # The same propagation rule for the BODY step: two same-named bodies must surface
        # _resolve_any_body's ambiguity error (with each candidate's context), not fall through
        # to the generic "did not resolve" miss.
        pin_a, pin_b = _two_pins()
        _install_ambiguous_bodies(occ_bodies=[{"Pin": pin_a}, {"Pin": pin_b}])
        res, err = inp.TargetRef("target").resolve("Pin")
        assert res is None
        assert "ambiguous" in err.lower()
        assert "Sub-A:1" in err and "Sub-B:1" in err       # both candidate contexts listed
        assert "did not resolve" not in err                # the generic miss must not mask it


class TestTargetRefList:
    """The CAM-setup selector: a list of bodies AND/OR component occurrences/components. A component
    maps to its single occurrence (the CAM API wants the Occurrence, not the Component)."""

    def test_the_note_states_what_naming_an_occurrence_selects(self):
        # Both spellings SUCCEED, so no error carries the difference: naming an occurrence selects
        # the component, naming a body selects that body. The note is the only home.
        note = inp.TargetRefList("models").contract_note()
        assert "an occurrence selects the whole component" in note
        assert inp.TargetRefList("models", contract="Just these.").contract_note() == "Just these."

    def test_body_handles_pass_through(self):
        b1 = FakeBRep("Body1", is_solid=True)
        b2 = FakeBRep("Body2", is_solid=True)
        _install_target(handle_map={"H1": b1, "H2": b2})
        val, err = inp.TargetRefList("models").resolve(["H1", "H2"])
        assert err is None and val == [b1, b2]

    def test_component_occurrence_selected_as_occurrence(self):
        # the RFA pattern: selecting the COMPONENT occurrence, not a body inside it.
        occ = _FakeOcc("Model Component:1", "Model Component:1")
        _install_target(occurrences=[occ])
        val, err = inp.TargetRefList("models").resolve(["Model Component:1"])
        assert err is None and val == [occ]        # the Occurrence itself, ready for Setup.models

    def test_component_name_maps_to_its_occurrence(self):
        # a bare component name resolves to its single occurrence (CAM wants the Occurrence). The
        # component name deliberately does NOT substring-match the occurrence's own name, so
        # resolution FALLS THROUGH TargetRef's occurrence step to the component step + the mapping.
        occ = _FakeOcc("stockInst:1", "stockInst:1", component="StockDef")
        _install_target(occurrences=[occ], components=["StockDef"])
        val, err = inp.TargetRefList("stock").resolve(["StockDef"])
        assert err is None and val == [occ]        # mapped Component -> its Occurrence

    def test_component_with_no_occurrence_errors(self):
        _install_target(components=["Orphan"])     # a component that is not instanced
        val, err = inp.TargetRefList("models").resolve(["Orphan"])
        assert val is None and "no occurrence" in err.lower()

    def test_component_with_multiple_occurrences_refused(self):
        # two instances of the same component -> ambiguous which to machine; refuse (never guess).
        # The component name ("JawDef") does not substring-match either occurrence name, so
        # resolution reaches the component branch and its multi-occurrence refusal.
        comp = MakeComp(name="JawDef")
        a = _FakeOcc("jawInstA:1", "Vise:1+jawInstA:1", component=comp)
        b = _FakeOcc("jawInstB:1", "Vise:1+jawInstB:1", component=comp)
        _install_target(occurrences=[a, b], components=["JawDef"])
        val, err = inp.TargetRefList("models").resolve(["JawDef"])
        assert val is None and "ambiguous" in err.lower() and "fullPathName" in err

    def test_mixed_body_and_component_occurrence(self):
        b = FakeBRep("StockBody", is_solid=True)
        occ = _FakeOcc("Model Component:1", "Model Component:1")
        _install_target(handle_map={"H": b}, occurrences=[occ])
        val, err = inp.TargetRefList("models").resolve(["H", "Model Component:1"])
        assert err is None and val == [b, occ]

    def test_empty_optional_is_empty_list(self):
        _install_target()
        val, err = inp.TargetRefList("models", required=False).resolve([])
        assert err is None and val == []

    def test_one_bad_element_fails_whole_list(self):
        occ = _FakeOcc("Good:1", "Good:1")
        _install_target(occurrences=[occ])
        val, err = inp.TargetRefList("models").resolve(["Good:1", "Ghost"])
        assert val is None and "[1]" in err and "Ghost" in err


# ── TargetRef edge + construction geometry (gated by allow=) ─────────────────────────────────────
# TargetRef ALSO resolves a BRepEdge and a ConstructionAxis/ConstructionPlane handle - but ONLY when
# the caller opts in via allow=. A default-allow caller (its allow is the six original kinds) refuses
# them exactly as it refuses any out-of-allow kind, so model_inspect / appearance_set / model_set_material
# are UNAFFECTED. model_measure_relation's concentric relation opts in with allow=(...,'edge').

class _FakeConsPlane:
    pass


def _install_target_ext(handle_map):
    """A design wired for the EXTENDED TargetRef paths: findEntityByToken plus the BRepEdge /
    ConstructionAxis / ConstructionPlane types the new isinstance branches check."""
    import adsk.fusion
    adsk.fusion.BRepBody = BRepBody
    adsk.fusion.MeshBody = MeshBody
    adsk.fusion.BRepFace = BRepFace
    adsk.fusion.BRepEdge = BRepEdge
    adsk.fusion.ConstructionAxis = _FakeConstructionAxis
    adsk.fusion.ConstructionPlane = _FakeConsPlane

    root = MakeComp(name="Root")
    design = MakeDesign(comp=root, all_components=[], tokens=handle_map)
    inp._common.design = lambda: design
    inp._common.target_component = lambda d=None: root


class TestTargetRefEdgeAndConstruction:
    def test_edge_handle_resolves_when_allowed(self):
        e = FakeEdge()
        _install_target_ext({"H": e})
        (ent, kind), err = inp.TargetRef("t", allow=("edge",)).resolve("H")
        assert err is None and kind == "edge" and ent is e

    def test_edge_handle_refused_under_default_allow(self):
        # the backward-compat guarantee: a default-allow TargetRef does NOT accept an edge
        e = FakeEdge()
        _install_target_ext({"H": e})
        res, err = inp.TargetRef("t").resolve("H")
        assert res is None and "edge" in err.lower()

    def test_construction_axis_resolves_when_allowed(self):
        ax = _FakeConstructionAxis()
        _install_target_ext({"H": ax})
        (ent, kind), err = inp.TargetRef("t", allow=("construction_axis",)).resolve("H")
        assert err is None and kind == "construction_axis" and ent is ax

    def test_construction_plane_resolves_when_allowed(self):
        pl = _FakeConsPlane()
        _install_target_ext({"H": pl})
        (ent, kind), err = inp.TargetRef("t", allow=("construction_plane",)).resolve("H")
        assert err is None and kind == "construction_plane" and ent is pl

    def test_original_body_kind_unaffected_by_the_extension(self):
        # body/face/mesh/occurrence/component/design must resolve EXACTLY as before
        b = FakeBRep("B", is_solid=True)
        _install_target_ext({"H": b})
        (ent, kind), err = inp.TargetRef("t").resolve("H")
        assert err is None and kind == "body" and ent is b


# ── JointOriginRef: a Joint Origin by handle OR name (bare/qualified), ambiguity refused ─────────────
#
# The resolve-one leaf of the JOINT-ORIGIN SEAM. It composes the shared _joints JO walk: a bare name is
# unique-or-refused, a qualified '<occ>:<JO name>' proxies into that occurrence, and a handle round-trips
# a JointOrigin entityToken. Non-unique JO names (two components sharing one) MUST refuse with the
# qualified candidates - the house rule OccurrenceRef enforces for the occurrence name space.

class _JO:
    """A JointOrigin fake: name + a createForAssemblyContext that returns a DISTINCT proxy tagged with
    its occurrence, so a test can tell a proxy apart from the native and confirm the right occurrence.

    A proxy carries `nativeObject` (the object it was made from) and a native reads None there -
    measured on a live JO, and the read the native selector steps back through."""
    def __init__(self, name, token=None):
        self.name = name
        self.entityToken = token
        self.nativeObject = None

    def createForAssemblyContext(self, occ):
        p = _JO(self.name)
        p.native = self
        p.nativeObject = self
        p.context = occ
        return p


def _Comp(name, jos=()):
    """A component carrying the Joint Origins the walk reads off it."""
    return MakeComp(name=name, joint_origins=list(jos))


def _OccJO(full, comp):
    """One occurrence placing `comp` at assembly path `full`."""
    return make_occurrence(path=full, component=comp)


def _RootJO(name="Root", jos=(), occ_by_comp=None):
    """The root component, plus the {component name: [occurrences]} map both occurrence walks
    answer from."""
    by_comp = occ_by_comp or {}
    root = _Comp(name, jos)
    root.allOccurrencesByComponent = lambda c: by_comp.get(getattr(c, "name", None), [])
    root.allOccurrences = [o for lst in by_comp.values() for o in lst]
    return root


def _DesignJO(root, subs=(), token_map=None):
    """The design under the JO walk. allComponents is a COUNTED collection on the DESIGN that
    already CARRIES the root (a bare list models neither), which is what _common.all_components -
    the walk under the shared JO walk - reads with count/item."""
    return MakeDesign(comp=root, all_components=[root] + list(subs), tokens=token_map)


def _install_jo(design):
    """Point the _inputs design seam at `design` and make adsk.fusion.JointOrigin a real type so
    is_joint_origin() works. JointOriginRef fetches design via _inputs._common.design() then passes it
    explicitly to the _joints walk / _resolve_occurrence, so ONE seam covers it. Returns the ref."""
    import adsk.fusion
    adsk.fusion.JointOrigin = _JO
    inp._common.design = lambda: design
    return inp.JointOriginRef("jo")


class TestJointOriginRef:
    def test_bare_unique_root_name_resolves(self):
        target = _JO("Stock_Center")
        design = _DesignJO(_RootJO(jos=[target]))
        ref = _install_jo(design)
        jo, err = ref.resolve("Stock_Center")
        assert err is None and jo is target

    def test_miss_lists_available_names(self):
        design = _DesignJO(_RootJO(jos=[_JO("Stock_Center"), _JO("Vise_Center")]))
        ref = _install_jo(design)
        jo, err = ref.resolve("Nope")
        assert jo is None
        assert "no Joint Origin named 'Nope'" in err
        assert "Stock_Center" in err and "Vise_Center" in err        # self-correction data

    def test_ambiguous_bare_name_is_refused_with_qualified_candidates(self):
        # two sub-components each carry 'Center' -> a bare 'Center' must REFUSE, listing the qualified
        # '<occ>:Center' forms - never silently grab the first (the non-unique-name-space house rule).
        a, b = _Comp("A", [_JO("Center")]), _Comp("B", [_JO("Center")])
        occ_a, occ_b = _OccJO("A:1", a), _OccJO("B:1", b)
        root = _RootJO(jos=[], occ_by_comp={"A": [occ_a], "B": [occ_b]})
        design = _DesignJO(root, subs=[a, b])
        ref = _install_jo(design)
        jo, err = ref.resolve("Center")
        assert jo is None
        assert "ambiguous" in err.lower()
        assert "A:1:Center" in err and "B:1:Center" in err

    def test_qualified_name_proxies_into_the_named_occurrence(self):
        native = _JO("Center")
        sub = _Comp("Tower", [native])
        occ = _OccJO("Tower:1", sub)
        root = _RootJO(jos=[], occ_by_comp={"Tower": [occ]})
        design = _DesignJO(root, subs=[sub])
        ref = _install_jo(design)
        jo, err = ref.resolve("Tower:1:Center")
        assert err is None
        assert getattr(jo, "native", None) is native      # a PROXY, not the native
        assert jo.context is occ                            # proxied into the RIGHT occurrence

    def test_a_bare_name_whose_proxy_FAILS_is_refused_naming_the_frame_and_the_occurrence(self):
        # a native sub-component JO is what yields 'Provided input paths for joint are not valid',
        # so a failed lift must refuse rather than hand that object back as the resolved frame.
        native = _JO("Stock_Center")
        native.createForAssemblyContext = lambda occ: None
        sub = _Comp("Stock", [native])
        occ = _OccJO("Stock:1", sub)
        design = _DesignJO(_RootJO(jos=[], occ_by_comp={"Stock": [occ]}), subs=[sub])
        jo, err = _install_jo(design).resolve("Stock_Center")
        assert jo is None and jo is not native
        assert "Stock_Center" in err and "Stock:1" in err

    def test_a_QUALIFIED_name_whose_proxy_FAILS_is_refused_the_same_way(self):
        native = _JO("Center")
        native.createForAssemblyContext = lambda occ: None
        sub = _Comp("Tower", [native])
        occ = _OccJO("Tower:1", sub)
        design = _DesignJO(_RootJO(jos=[], occ_by_comp={"Tower": [occ]}), subs=[sub])
        jo, err = _install_jo(design).resolve("Tower:1:Center")
        assert jo is None and jo is not native
        assert "Center" in err and "Tower:1" in err

    def test_bare_name_on_single_instance_subcomponent_is_proxied(self):
        # a UNIQUE bare name whose owning component has ONE occurrence resolves - proxied into it.
        native = _JO("Stock_Center")
        sub = _Comp("Stock", [native])
        occ = _OccJO("Stock:1", sub)
        root = _RootJO(jos=[], occ_by_comp={"Stock": [occ]})
        design = _DesignJO(root, subs=[sub])
        ref = _install_jo(design)
        jo, err = ref.resolve("Stock_Center")
        assert err is None and getattr(jo, "native", None) is native and jo.context is occ

    def test_multi_instance_subcomponent_bare_name_is_refused(self):
        # a unique NAME but the owning component is instanced twice -> which instance's frame? Refuse.
        native = _JO("Grip")
        sub = _Comp("Jaw", [native])
        occ1, occ2 = _OccJO("Jaw:1", sub), _OccJO("Jaw:2", sub)
        root = _RootJO(jos=[], occ_by_comp={"Jaw": [occ1, occ2]})
        design = _DesignJO(root, subs=[sub])
        ref = _install_jo(design)
        jo, err = ref.resolve("Grip")
        assert jo is None and "instanced 2 times" in err

    def test_handle_resolves_to_the_joint_origin(self):
        target = _JO("Stock_Center", token="JO_TOKEN")
        design = _DesignJO(_RootJO(jos=[target]), token_map={"JO_TOKEN": target})
        ref = _install_jo(design)
        jo, err = ref.resolve("JO_TOKEN")
        assert err is None and jo is target

    def test_a_handle_naming_a_PROXY_is_returned_as_minted(self):
        # the default selector hands the JO to a joint, which needs the instance's own object - so
        # the proxy an assembly_get handle round-trips to is exactly what it must return.
        native = _JO("Grip")
        sub = _Comp("Jaw", [native])
        occ = _OccJO("Jaw:1", sub)
        proxy = native.createForAssemblyContext(occ)
        proxy.entityToken = "JO_PROXY"
        design = _DesignJO(_RootJO(jos=[], occ_by_comp={"Jaw": [occ]}), subs=[sub],
                           token_map={"JO_PROXY": proxy})
        jo, err = _install_jo(design).resolve("JO_PROXY")
        assert err is None and jo is proxy and jo is not native

    def test_handle_pointing_at_non_jo_is_rejected(self):
        face = FakePlanarFace()
        design = _DesignJO(_RootJO(jos=[]), token_map={"FACE_TOKEN": face})
        ref = _install_jo(design)
        jo, err = ref.resolve("FACE_TOKEN")
        assert jo is None and "not a Joint Origin" in err

    def test_an_ambiguous_name_is_never_offered_back_as_its_own_remedy(self):
        # jo_reference_names answers the BARE name for a ROOT-owned JO (it is unique on root, so
        # there is nothing to qualify it with). When a root-owned 'Center' is one of the ambiguous
        # hits, the unfiltered candidate list contains 'Center' itself - so the refusal hands back
        # the exact string it just rejected, and passing it lands in this same branch again.
        root_jo, sub_jo = _JO("Center"), _JO("Center")
        a = _Comp("A", [sub_jo])
        design = _DesignJO(_RootJO(jos=[root_jo], occ_by_comp={"A": [_OccJO("A:1", a)]}), subs=[a])
        jo, err = _install_jo(design).resolve("Center")
        assert jo is None and "ambiguous" in err.lower()
        offered = err.split("(", 1)[1].split(")", 1)[0]   # exactly the parenthesised remedy list
        assert offered == "A:1:Center"                    # unfixed: 'Center, A:1:Center'

    def test_the_match_with_no_qualified_form_is_counted_not_hidden(self):
        # Dropping the self-named candidate must not make its JO vanish from the refusal: the count
        # of matches still says 2, and the tail names the handle as that one's only reference form.
        root_jo, sub_jo = _JO("Center"), _JO("Center")
        a = _Comp("A", [sub_jo])
        design = _DesignJO(_RootJO(jos=[root_jo], occ_by_comp={"A": [_OccJO("A:1", a)]}), subs=[a])
        err = _install_jo(design).resolve("Center")[1]
        assert "2 Joint Origins share that name" in err
        assert "1 further match(es) carry no reference form other than 'Center' itself" in err
        assert "reach those by handle" in err

    def test_when_every_match_is_self_named_the_refusal_offers_the_handle_only(self):
        # A root-owned JO and one on a component with NO occurrence both answer the bare name, so
        # after the filter there is no qualified vocabulary left at all. The refusal must say that
        # rather than print an empty candidate list.
        root_jo, orphan_jo = _JO("Center"), _JO("Center")
        b = _Comp("B", [orphan_jo])                       # never placed: no '<occ>:' form exists
        design = _DesignJO(_RootJO(jos=[root_jo], occ_by_comp={}), subs=[b])
        jo, err = _install_jo(design).resolve("Center")
        assert jo is None
        assert "2 match(es) carry no reference form other than 'Center' itself" in err
        assert "Pass a handle from assembly_get(include=['joint_origins'])." in err
        assert "()" not in err                            # no empty candidate list left behind

    def test_walk_finds_a_subcomponent_jo_the_root_walk_would_miss(self):
        # a JO living ONLY in a sub-component must be found (a root-only walk under-reports).
        # find_joint_origins_by_name is the resolve-one leaf over all_joint_origins.
        native = _JO("Deep_Frame")
        sub = _Comp("Inner", [native])
        root = _RootJO(jos=[], occ_by_comp={"Inner": [_OccJO("Inner:1", sub)]})
        design = _DesignJO(root, subs=[sub])
        _install_jo(design)
        matches = inp._joints.find_joint_origins_by_name(design, "Deep_Frame")
        assert len(matches) == 1 and matches[0][0] is native and matches[0][1] is sub


class TestJointOriginRefNativeSelector:
    """native=True: the JO as its OWNING COMPONENT carries it, for a consumer that READS the frame.
    One frame per component is the whole contract - so an instance-qualified reference to a
    multi-placed owner is refused (it would be honored in name only), while the bare name and the
    handle, which name no instance, keep resolving where the proxy selector has to refuse."""

    def _native(self, design):
        _install_jo(design)
        return inp.JointOriginRef("jo", native=True)

    def _jaw(self):
        """A 'Grip' frame on a component placed TWICE."""
        native = _JO("Grip")
        sub = _Comp("Jaw", [native])
        root = _RootJO(jos=[], occ_by_comp={"Jaw": [_OccJO("Jaw:1", sub), _OccJO("Jaw:2", sub)]})
        return native, _DesignJO(root, subs=[sub])

    def test_a_single_instance_bare_name_resolves_to_the_NATIVE_not_a_proxy(self):
        native = _JO("Stock_Center")
        sub = _Comp("Stock", [native])
        design = _DesignJO(_RootJO(jos=[], occ_by_comp={"Stock": [_OccJO("Stock:1", sub)]}), subs=[sub])
        jo, err = self._native(design).resolve("Stock_Center")
        assert err is None and jo is native            # the proxy selector returns a proxy here

    def test_a_multi_placed_owners_bare_name_resolves_where_the_proxy_selector_refuses(self):
        native, design = self._jaw()
        jo, err = self._native(design).resolve("Grip")
        assert err is None and jo is native

    def test_neither_instance_qualified_form_resolves_for_a_multi_placed_owner(self):
        # both forms name the SAME native, so accepting either would honor the '<occurrence>:' half
        # in name only - the defect this selector exists to refuse.
        native, design = self._jaw()
        ref = self._native(design)
        for spec in ("Jaw:1:Grip", "Jaw:2:Grip"):
            jo, err = ref.resolve(spec)
            assert jo is None and jo is not native, spec
            assert "placed 2 times" in err and "Jaw:1" in err and "Jaw:2" in err
            assert "Pass the bare name 'Grip'" in err          # a remedy this input accepts

    def test_an_instance_qualified_form_resolves_natively_when_the_owner_is_placed_once(self):
        native = _JO("Center")
        sub = _Comp("Tower", [native])
        design = _DesignJO(_RootJO(jos=[], occ_by_comp={"Tower": [_OccJO("Tower:1", sub)]}), subs=[sub])
        jo, err = self._native(design).resolve("Tower:1:Center")
        assert err is None and jo is native            # the native, where the proxy selector proxies

    def test_an_ambiguous_name_offers_only_forms_that_name_ONE_frame(self):
        # 'Center' on a singly-placed A and a twice-placed B: B's two forms both name B's one frame,
        # so they are not offered as a way to pick between the two Centers.
        a, b = _Comp("A", [_JO("Center")]), _Comp("B", [_JO("Center")])
        root = _RootJO(jos=[], occ_by_comp={"A": [_OccJO("A:1", a)],
                                            "B": [_OccJO("B:1", b), _OccJO("B:2", b)]})
        design = _DesignJO(root, subs=[a, b])
        jo, err = self._native(design).resolve("Center")
        assert jo is None and "ambiguous" in err.lower()
        assert "A:1:Center" in err
        assert "B:1:Center" not in err and "B:2:Center" not in err

    def test_when_no_qualified_form_names_one_frame_the_refusal_says_so_and_points_at_the_handle(self):
        a, b = _Comp("A", [_JO("Center")]), _Comp("B", [_JO("Center")])
        root = _RootJO(jos=[], occ_by_comp={"A": [_OccJO("A:1", a), _OccJO("A:2", a)],
                                            "B": [_OccJO("B:1", b), _OccJO("B:2", b)]})
        design = _DesignJO(root, subs=[a, b])
        jo, err = self._native(design).resolve("Center")
        assert jo is None
        # both hits drop for the multi-placed reason here; the mixed set below is what proves each
        # count is read off the loop rather than echoing len(matches).
        assert "2 match(es) sit on a component placed several times" in err
        assert "handle" in err and "()" not in err     # no empty candidate list left behind

    def test_a_MIXED_ambiguous_set_states_BOTH_causes_with_their_own_counts(self):
        # The two reasons a hit leaves the candidate list are independent and can apply to different
        # hits in one refusal: root's 'Center' carries no reference form but the refused name itself,
        # while B's sits on a component placed twice, so both of ITS forms name the one frame this
        # selector returns. A refusal that states one cause and drops the other leaves the caller
        # unable to act on the half it hid. Each count is read off the loop, and here neither equals
        # the match total - so a count that echoed len(matches) instead reads 2 and shows up.
        root_jo, b_jo = _JO("Center"), _JO("Center")
        b = _Comp("B", [b_jo])
        root = _RootJO(jos=[root_jo], occ_by_comp={"B": [_OccJO("B:1", b), _OccJO("B:2", b)]})
        design = _DesignJO(root, subs=[b])
        jo, err = self._native(design).resolve("Center")
        assert jo is None
        assert "2 Joint Origins share that name" in err                    # neither hit is hidden
        assert "1 match(es) sit on a component placed several times" in err
        assert "1 match(es) carry no reference form other than 'Center' itself" in err

    def test_the_root_owned_self_named_candidate_is_filtered_here_too(self):
        # The self-named-candidate filter lives in the KIND, so the native selector gets it without a
        # line of its own - and a consumer like model_inspect adds no local filter of its own.
        root_jo, sub_jo = _JO("Center"), _JO("Center")
        a = _Comp("A", [sub_jo])
        design = _DesignJO(_RootJO(jos=[root_jo], occ_by_comp={"A": [_OccJO("A:1", a)]}), subs=[a])
        jo, err = self._native(design).resolve("Center")
        assert jo is None
        assert err.split("(", 1)[1].split(")", 1)[0] == "A:1:Center"

    def test_a_handle_naming_a_PROXY_steps_back_to_the_native(self):
        # MEASURED live: assembly_get mints one handle per INSTANCE of a JO and each round-trips to
        # that instance's proxy, with its own assemblyContext and its own world origin. Handing one
        # back under this selector would read axes off a per-instance object while the contract note,
        # the refusal above and the consumer's payload all say the frame is the component's.
        native = _JO("Grip")
        sub = _Comp("Jaw", [native])
        occ1, occ2 = _OccJO("Jaw:1", sub), _OccJO("Jaw:2", sub)
        p1, p2 = native.createForAssemblyContext(occ1), native.createForAssemblyContext(occ2)
        p1.entityToken, p2.entityToken = "JO_P1", "JO_P2"
        design = _DesignJO(_RootJO(jos=[], occ_by_comp={"Jaw": [occ1, occ2]}), subs=[sub],
                           token_map={"JO_P1": p1, "JO_P2": p2})
        ref = self._native(design)
        for token in ("JO_P1", "JO_P2"):
            jo, err = ref.resolve(token)
            assert err is None, token
            assert jo is native and jo is not p1 and jo is not p2, token

    def test_a_handle_naming_the_NATIVE_resolves_to_it(self):
        # a native's own nativeObject reads None (measured), so the step-back must fall through to
        # the entity the handle named rather than answering nothing.
        target = _JO("Stock_Center", token="JO_NATIVE")
        design = _DesignJO(_RootJO(jos=[target]), token_map={"JO_NATIVE": target})
        jo, err = self._native(design).resolve("JO_NATIVE")
        assert err is None and jo is target

    def test_the_multi_placed_refusal_offers_only_remedies_this_input_honors(self):
        # the remedy has to survive its own rule: the bare name reads the component frame, and every
        # per-instance handle now steps back to the same native, so both forms answer alike.
        native, design = self._jaw()
        err = self._native(design).resolve("Jaw:1:Grip")[1]
        assert "Pass the bare name 'Grip'" in err
        assert "one row per instance and this input reads the same component frame from each" in err

    def test_the_contract_note_states_the_component_frame_only_for_the_native_selector(self):
        assert "owning component's frame" in inp.JointOriginRef("jo", native=True).contract_note()
        assert "owning component" not in inp.JointOriginRef("jo").contract_note()


# ── the shared collection walks: an unreadable member costs its own row, not the census ──

class _WalkColl(_NamedCollection):
    """The shared collection whose members break INDIVIDUALLY: `broken` indices raise on item(i),
    which the shared raises/item_raises pair (all-or-nothing) cannot express. A None member is a
    hole; `count_raises` is the count that will not read at all."""

    def __init__(self, items, broken=(), count_raises=False):
        super().__init__(items)
        self._broken = set(broken)
        self._count_raises = count_raises

    @property
    def count(self):
        if self._count_raises:
            raise RuntimeError("count unreadable")
        return len(self._items)

    def item(self, i):
        if i in self._broken:
            raise RuntimeError("item unreadable")
        return self._items[i]


def _tl_obj(name, index):
    return FakeTimelineObject(name=name, index=index, entity=object())


class TestTimelineObjectsWalk:
    """_timeline_objects is what every FeatureRef resolution reads the timeline through."""

    def test_returns_every_object_in_index_order(self):
        objs = [_tl_obj("Extrude1", 0), _tl_obj("Fillet1", 1)]
        assert inp._timeline_objects(_WalkColl(objs)) == objs

    def test_an_unreadable_object_is_skipped_not_raised(self):
        # Without the safe() guard this raises straight out of resolve(), so ONE bad timeline row
        # makes every feature name in the document unresolvable instead of just its own.
        objs = [_tl_obj("Extrude1", 0), _tl_obj("Broken", 1), _tl_obj("Fillet1", 2)]
        names = [o.name for o in inp._timeline_objects(_WalkColl(objs, broken=(1,)))]
        assert names == ["Extrude1", "Fillet1"]

    def test_an_unreadable_count_is_an_empty_timeline_not_a_raise(self):
        assert inp._timeline_objects(_WalkColl([_tl_obj("X", 0)], count_raises=True)) == []

    def test_the_index_form_reads_the_objects_own_index_not_its_position(self):
        # A skipped row leaves a HOLE, so position 1 holds the object whose own .index is 2. The
        # '@index' number an agent passes came from a candidate list / design_get, which publish
        # o.index - addressing by position would resolve a different object than was named.
        objs = [_tl_obj("Extrude1", 0), _tl_obj("Broken", 1), _tl_obj("Fillet1", 2)]
        walked = inp._timeline_objects(_WalkColl(objs, broken=(1,)))
        assert [o.name for o in inp._match_timeline_objects(walked, "Fillet1@2")] == ["Fillet1"]
        assert inp._match_timeline_objects(walked, "Fillet1@1") == []

    def test_two_same_named_features_across_a_hole_resolve_by_their_own_index(self):
        # THE silent-wrong-target case: two features share the name 'Fillet1' (indices 3 and 4) and
        # an unreadable row sits before them. The ambiguity refusal offers 'Fillet1@3'/'Fillet1@4';
        # under a position-based pick '@4' missed and '@3' quietly resolved the OTHER Fillet1.
        first, second = _tl_obj("Fillet1", 3), _tl_obj("Fillet1", 4)
        objs = [_tl_obj("Extrude1", 0), _tl_obj("Broken", 1), _tl_obj("Chamfer1", 2), first, second]
        walked = inp._timeline_objects(_WalkColl(objs, broken=(1,)))
        assert inp._match_timeline_objects(walked, "Fillet1@4") == [second]
        assert inp._match_timeline_objects(walked, "Fillet1@3") == [first]

    def test_an_index_no_object_carries_is_a_miss(self):
        objs = [_tl_obj("Extrude1", 0), _tl_obj("Fillet1", 1)]
        assert inp._match_timeline_objects(objs, "Fillet1@7") == []

    def test_the_index_form_still_confirms_the_name(self):
        # '@index' is a disambiguator, not an override: naming the wrong feature at a real index
        # must miss rather than resolve whatever sits there.
        objs = [_tl_obj("Extrude1", 0), _tl_obj("Fillet1", 1)]
        assert inp._match_timeline_objects(objs, "Extrude1@1") == []


class TestStaleTimelineIndex:
    """A 'name@index' whose two halves name different objects - what a re-used index becomes once
    the timeline has moved. The matcher confirms the name, so the pair MISSES; the refusal is what
    tells the caller which half went stale."""

    def test_a_mismatch_names_the_object_at_that_index_and_where_the_name_is(self):
        objs = [_tl_obj("Extrude3", 4), _tl_obj("Fillet2", 7)]
        obj, err = inp.resolve_timeline_object(objs, "Extrude3@7", "'feature'")
        assert obj is None
        assert "'Extrude3@7'" in err and "index 7 is 'Fillet2'" in err
        assert "'Extrude3' is at index 4" in err
        assert "design_get(include=['timeline'])" in err

    def test_an_index_no_object_carries_still_places_the_name(self):
        objs = [_tl_obj("Extrude3", 4)]
        obj, err = inp.resolve_timeline_object(objs, "Extrude3@7", "'feature'")
        assert obj is None and "no timeline item reads index 7" in err
        assert "'Extrude3' is at index 4" in err

    def test_a_pair_that_agrees_still_resolves(self):
        objs = [_tl_obj("Extrude3", 4), _tl_obj("Fillet2", 7)]
        obj, err = inp.resolve_timeline_object(objs, "Extrude3@4", "'feature'")
        assert err is None and obj is objs[0]

    def test_a_name_no_object_carries_keeps_the_plain_miss_listing(self):
        # the index half explains nothing when the NAME is absent anywhere: the sample of what IS
        # there is the actionable answer, so that path stays untouched.
        objs = [_tl_obj("Extrude3", 4)]
        obj, err = inp.resolve_timeline_object(objs, "Ghost@7", "'feature'")
        assert obj is None and "no timeline feature named 'Ghost@7'" in err
        assert "Extrude3" in err


class TestTimelineNameWhitespace:
    """Surrounding whitespace is not a distinguishing feature on EITHER side of the comparison.

    Fusion names an occurrence-create timeline object with a leading space (probe_fix_campaign.log
    [F74]), which no listing shows and no caller retypes. Both sides are stripped here, at the
    matcher - every tool's own input handling sits above it, so a caller that does not strip its
    own input still resolves the same object as one that does.
    """

    def test_the_OBJECT_side_is_stripped(self):
        objs = [_tl_obj(" InsProbe:1", 0)]
        assert inp._match_timeline_objects(objs, "InsProbe:1") == objs

    def test_the_WANT_side_is_stripped(self):
        # the half no tool exercises today: every caller strips its own input first, so only a
        # direct call reaches this - and the docstring's contract is what a NEW caller relies on.
        objs = [_tl_obj("Extrude1", 0)]
        assert inp._match_timeline_objects(objs, "  Extrude1  ") == objs
        obj, err = inp.resolve_timeline_object(objs, "  Extrude1  ", "'feature'")
        assert err is None and obj is objs[0]

    def test_both_sides_at_once(self):
        objs = [_tl_obj(" InsProbe:1 ", 0)]
        assert inp._match_timeline_objects(objs, "  InsProbe:1  ") == objs

    def test_the_index_form_strips_the_name_half_too(self):
        objs = [_tl_obj(" Extrude1", 4)]
        assert inp._match_timeline_objects(objs, " Extrude1 @4") == objs

    def test_whitespace_is_never_a_disambiguator(self):
        # two objects differing ONLY by padding are one ambiguity, refused - never silently split
        # into two addressable names an agent cannot tell apart in any listing.
        objs = [_tl_obj("Extrude1", 0), _tl_obj(" Extrude1", 1)]
        assert inp._match_timeline_objects(objs, "Extrude1") == objs
        obj, err = inp.resolve_timeline_object(objs, "Extrude1", "'feature'")
        assert obj is None and "matches 2 timeline objects" in err


class TestSketchRefListRunsTheOneSharedWalk:
    """SketchRefList resolves through _common.find_sketch - the ONE design-wide sketch walk - so
    its census and its resolution cannot disagree about which sketches a name names."""

    def _comp(self, name, sketch_names, **kw):
        comp = MakeComp(name=name)
        # the sketches collection carries per-member breakage, which no MakeComp knob expresses
        comp.sketches = _WalkColl([Sketch(name=n) for n in sketch_names], **kw)
        return comp

    def _resolve(self, monkeypatch, comps, raw):
        d = MakeDesign(comp=comps[0], all_components=comps)
        monkeypatch.setattr(inp._common, "design", lambda: d)
        return inp.SketchRefList("sketches").resolve(raw)

    def test_a_name_exactly_one_sketch_carries_resolves(self, monkeypatch):
        comps = [self._comp("Root", ["Profile"]), self._comp("Frame", ["Other"])]
        got, err = self._resolve(monkeypatch, comps, "Profile")
        assert err is None
        assert got == [comps[0].sketches.item(0)]

    def test_a_shared_name_is_refused_naming_every_owning_component(self, monkeypatch):
        comps = [self._comp("Root", ["Profile"]), self._comp("Frame", ["Profile"])]
        got, err = self._resolve(monkeypatch, comps, "Profile")
        assert got is None
        assert "2 sketches are named 'Profile'" in err
        assert "Root" in err and "Frame" in err
        assert "'sketches'[0]" in err        # the refusal still names the slot the name came from

    def test_the_match_is_exact_case_included(self, monkeypatch):
        # The walk asks each component's sketches.itemByName, which is case-SENSITIVE. A census
        # matching case-INSENSITIVELY beside it counted a hit the resolve could not hand back, so
        # 'profile' dead-ended on "did not resolve to a live sketch" instead of the miss + names.
        comps = [self._comp("Root", ["Profile"])]
        got, err = self._resolve(monkeypatch, comps, "profile")
        assert got is None
        assert "no sketch named 'profile'" in err
        assert "Available: Profile" in err

    def test_each_list_entry_is_refused_under_its_own_index(self, monkeypatch):
        comps = [self._comp("Root", ["Outline", "Profile"]), self._comp("Frame", ["Profile"])]
        got, err = self._resolve(monkeypatch, comps, ["Outline", "Profile"])
        assert got is None and "'sketches'[1]" in err

    def test_a_component_whose_sketches_will_not_read_costs_only_its_own_row(self, monkeypatch):
        # A component whose collection raises contributes nothing; the rest of the design still
        # resolves, rather than the whole walk failing on one bad component.
        comps = [self._comp("Root", ["Profile"]),
                 types.SimpleNamespace(name="Empty", sketches=None)]
        got, err = self._resolve(monkeypatch, comps, "Profile")
        assert err is None and got == [comps[0].sketches.item(0)]

    def test_every_available_name_is_listed_whole(self, monkeypatch):
        # The miss lists names so the caller can pass one back. Budgeting the JOINED string by
        # characters lands the cut inside a name, and a fragment is not a name anything resolves.
        names = [f"Bracket_Outline_Revision_{i:02d}_Master" for i in range(12)]
        got, err = self._resolve(monkeypatch, [self._comp("Root", names)], "Ghost")
        assert got is None
        for n in names:
            assert n in err

    def test_the_cap_lists_every_name_at_the_limit(self, monkeypatch):
        names = [f"S{i:02d}" for i in range(inp._SKETCH_NAMES_LISTED)]
        _got, err = self._resolve(monkeypatch, [self._comp("Root", names)], "Ghost")
        assert "not listed" not in err         # nothing was held back, so nothing is counted
        for n in names:
            assert n in err

    def test_one_name_over_the_limit_is_counted_not_printed(self, monkeypatch):
        # The remainder is worded by the shared renderer, so this listing discloses what it held
        # back in the same words every other refusal a caller reads uses.
        names = [f"S{i:02d}" for i in range(inp._SKETCH_NAMES_LISTED + 1)]
        _got, err = self._resolve(monkeypatch, [self._comp("Root", names)], "Ghost")
        assert "... (+1 more not listed)" in err
        assert names[-1] not in err

    def test_a_design_holding_no_sketch_says_none(self, monkeypatch):
        got, err = self._resolve(monkeypatch, [self._comp("Root", [])], "Ghost")
        assert got is None and "Available: (none)." in err


class TestTargetRefMissHint:
    def test_miss_error_advertises_empty_form_only_when_design_is_allowed(self):
        # design_set_name excludes 'design' from allow= yet the miss error advertised the '' form
        # (measured) - an unreachable suggestion. The hint follows the kind's own allow set.
        _install_target()
        _, err_with = inp.TargetRef("target").resolve("Nope")
        assert "'' (whole design)" in err_with
        _, err_without = inp.TargetRef(
            "target", allow=("body", "mesh", "occurrence", "component")).resolve("Nope")
        assert "whole design" not in err_without


class TestEntityComponent:
    """The ONE owning-component chain every reference entity is resolved through. A FACE answers on
    the same first link an edge does - api_surface gives fusion.BRepFace a `body` and none of
    `parentSketch` / `component` / `parent` - which is what lets model_hole host a hole in the
    component that owns the drilled face without re-rolling the read."""

    def test_a_face_answers_its_bodys_component(self):
        owner = MakeComp(name="Chassis")
        face = types.SimpleNamespace(body=types.SimpleNamespace(parentComponent=owner))
        assert inp.entity_component(face) is owner

    def test_a_face_whose_body_owner_will_not_read_answers_None(self):
        # the fallback branch its consumers stand in for the active component on: nothing was read,
        # so nothing is returned - never the body or the entity itself
        class _Body:
            @property
            def parentComponent(self):
                raise RuntimeError("owner unavailable")

        assert inp.entity_component(types.SimpleNamespace(body=_Body())) is None

    def test_an_entity_with_none_of_the_chain_answers_None(self):
        assert inp.entity_component(types.SimpleNamespace()) is None

    def test_a_sketch_line_answers_its_sketchs_component(self):
        owner = MakeComp(name="Plate")
        line = types.SimpleNamespace(parentSketch=types.SimpleNamespace(parentComponent=owner))
        assert inp.entity_component(line) is owner


# ── every list a refusal prints DISCLOSES what it left out ───────────────────────────────────────
#
# A refusal that lists names is the caller's whole vocabulary for the retry: the names printed are
# the ones it can pass back. Each of these lists is rendered by _common.named_with_remainder, so a
# list longer than the cap COUNTS what it held back instead of stopping at the last named one - a
# silently cut list reads as the complete set, and the entry the caller wanted may be the one gone.

# The shared cap itself, read from its owner: these tests pin the DISCLOSURE, not the number.
_CAP = inp._common._MAX_NAMED_CANDIDATES
# The one over-cap design every test here builds: cap + 1 candidates, so exactly one is held back.
_OVER = _CAP + 1
_HELD_BACK = "(+1 more not listed)"


def _pin_placed(count, comp_name="Jaw", body_name="Pin"):
    """One body named `body_name` placed `count` times - the shape a bare body name is ambiguous
    in, since each placement is its own candidate."""
    native = BRepBody(body_name, entity_token="TOK-NATIVE")
    occ_bodies = []
    for i in range(1, count + 1):
        path = f"{comp_name}:{i}"
        occ = make_occurrence(path=path)
        occ_bodies.append((path, [body_proxy(native, occ)]))
    _install_native_and_proxy(comp_bodies=[native], comp_name=comp_name, occ_bodies=occ_bodies)


def _case_variants(word, n):
    """`n` distinct case spellings of `word`, the first being `word` itself - the shape ONE scope
    holds several bodies of one name in, since the body-name match is case-insensitive."""
    out = [word]
    for bits in range(1, 2 ** len(word)):
        v = "".join(c.upper() if bits >> i & 1 else c.lower() for i, c in enumerate(word))
        if v not in out:
            out.append(v)
        if len(out) == n:
            break
    return out


def _TokenBody(name, token, owner="Frame"):
    """A body carrying its own entityToken - what tells two same-named bodies in ONE scope apart
    (_body_key groups on the token first, and falls back to (name, scope), which they share)."""
    return BRepBody(name=name, entity_token=token,
                    parent_component=types.SimpleNamespace(name=owner))


class TestRefusalListsDiscloseTheirRemainder:
    def test_a_bare_body_name_over_the_cap_counts_the_placements_it_did_not_name(self):
        _pin_placed(_OVER)
        val, err = inp.BodyRef("body").resolve("Pin")
        assert val is None and f"names {_OVER} bodies" in err
        assert err.count(":Pin'") == _CAP                  # the cap still bounds the list
        assert f"'Jaw:{_OVER}:Pin'" not in err and _HELD_BACK in err

    def test_a_bare_body_name_AT_the_cap_names_every_placement_and_counts_nothing(self):
        # The boundary the disclosure must not fire at: the cap'th candidate is printed, and a
        # refusal that named everything it had says nothing about a remainder.
        _pin_placed(_CAP)
        val, err = inp.BodyRef("body").resolve("Pin")
        assert val is None and f"names {_CAP} bodies" in err
        assert err.count(":Pin'") == _CAP and f"'Jaw:{_CAP}:Pin'" in err
        assert "not listed" not in err

    def test_a_qualified_prefix_over_the_cap_counts_the_placements_it_did_not_name(self):
        # 'Jaw:Pin' names the component, which every instance answers to - so the prefix narrows
        # nothing and the same list is refused, one entry per placement.
        _pin_placed(_OVER)
        val, err = inp.BodyRef("body").resolve("Jaw:Pin")
        assert val is None and f"still names {_OVER} bodies" in err
        assert f"'Jaw:{_CAP}:Pin'" in err and f"'Jaw:{_OVER}:Pin'" not in err
        assert _HELD_BACK in err

    def test_a_scope_holding_over_the_cap_of_ONE_body_name_counts_the_rest(self):
        # _bodies_named_in answers EVERY body of that name in one scope, so this list is as long as
        # the scope makes it. Asked in the '/' spelling, which resolves like the ':' one and keeps
        # the echoed spec out of the candidate count.
        _install_bodies(components={"Frame": [_TokenBody("Pin", f"TOK-{i}") for i in range(_OVER)]})
        val, err = inp.BodyRef("body").resolve("Frame/Pin")
        assert val is None and f"names {_OVER} bodies" in err
        assert err.count("'Frame:Pin'") == _CAP and _HELD_BACK in err

    def test_a_scope_that_holds_no_such_name_counts_the_bodies_it_did_not_list(self):
        # The miss lists what the scope DOES hold, which is what the caller picks its retry from.
        _install_bodies(components={"Frame": [FakeBody(f"Body{i}") for i in range(_OVER)]})
        val, err = inp.BodyRef("body").resolve("Frame:Ghost")
        assert val is None and "holds no body named 'Ghost'" in err
        assert err.count("'Body") == _CAP and _HELD_BACK in err

    def test_a_multi_body_component_over_the_cap_counts_the_bodies_it_did_not_name(self):
        _install_bodies(components={"Frame": [FakeBody(f"Body{i}") for i in range(_OVER)]})
        val, err = inp.BodyRef("body").resolve("Frame")
        assert val is None and f"holding {_OVER} bodies" in err
        assert err.count("'Body") == _CAP and _HELD_BACK in err

    # The SCOPED resolve prints three lists of its own - the placements a component scope cannot
    # separate, the bodies one scope holds under a single name, and what a scope holds when the name
    # misses - and each is as long as the design makes it.

    def _scoped_bodies(self, raw, component):
        return inp.BodyRefList("bodies", scope_input="component").resolve(raw, component)

    def test_a_scope_over_the_cap_of_placements_counts_the_ones_it_did_not_name(self, monkeypatch):
        # A component name answers EVERY placement of it, so a component placed this often leaves
        # that many candidates inside the scope the caller did narrow to.
        _install_shared_body_name(
            monkeypatch, placements=tuple(f"Bracket:{i}" for i in range(1, _OVER + 1)))
        got, err = self._scoped_bodies(["Body1"], "Bracket")
        assert got is None and f"still names {_OVER} bodies" in err
        assert err.count(":Body1'") == _CAP and f"'Bracket:{_CAP}:Body1'" in err
        assert f"'Bracket:{_OVER}:Body1'" not in err and _HELD_BACK in err

    def test_a_scope_AT_the_cap_of_placements_names_every_one_and_counts_nothing(self, monkeypatch):
        _install_shared_body_name(
            monkeypatch, placements=tuple(f"Bracket:{i}" for i in range(1, _CAP + 1)))
        got, err = self._scoped_bodies(["Body1"], "Bracket")
        assert got is None and f"still names {_CAP} bodies" in err
        assert err.count(":Body1'") == _CAP and f"'Bracket:{_CAP}:Body1'" in err
        assert "not listed" not in err

    def test_one_scope_holding_over_the_cap_of_ONE_name_counts_the_rest(self, monkeypatch):
        # The scope is asked directly - the one path a component no occurrence places is reached by -
        # and it answers every body of that name at once.
        names = _case_variants("pins", _OVER)
        _install_unplaced_component(monkeypatch, names)
        got, err = self._scoped_bodies([names[0]], "Bracket")
        assert got is None and f"holds {_OVER} bodies named '{names[0]}'" in err
        assert err.count("'Bracket:") == _CAP and f"'Bracket:{names[_CAP - 1]}'" in err
        assert f"'Bracket:{names[_OVER - 1]}'" not in err and _HELD_BACK in err

    def test_one_scope_holding_the_cap_of_ONE_name_names_every_body(self, monkeypatch):
        names = _case_variants("pins", _CAP)
        _install_unplaced_component(monkeypatch, names)
        got, err = self._scoped_bodies([names[0]], "Bracket")
        assert got is None and f"holds {_CAP} bodies named '{names[0]}'" in err
        assert err.count("'Bracket:") == _CAP and f"'Bracket:{names[_CAP - 1]}'" in err
        assert "not listed" not in err

    def test_a_scoped_miss_over_the_cap_counts_the_bodies_it_did_not_list(self, monkeypatch):
        # What the scope DOES hold is the caller's whole vocabulary for the retry.
        _install_unplaced_component(monkeypatch, [f"Body{i}" for i in range(_OVER)])
        got, err = self._scoped_bodies(["Ghost"], "Bracket")
        assert got is None and "holds no body named 'Ghost'" in err
        assert err.count("'Body") == _CAP and f"'Body{_CAP - 1}'" in err
        assert f"'Body{_OVER - 1}'" not in err and _HELD_BACK in err

    def test_a_scoped_miss_AT_the_cap_names_every_body_it_holds(self, monkeypatch):
        _install_unplaced_component(monkeypatch, [f"Body{i}" for i in range(_CAP)])
        got, err = self._scoped_bodies(["Ghost"], "Bracket")
        assert got is None and "holds no body named 'Ghost'" in err
        assert err.count("'Body") == _CAP and f"'Body{_CAP - 1}'" in err
        assert "not listed" not in err

    def test_an_ambiguous_feature_name_over_the_cap_counts_the_objects_it_did_not_name(self):
        objs = [_tl_obj("Fillet1", i) for i in range(_OVER)]
        obj, err = inp.resolve_timeline_object(objs, "Fillet1", "'feature'")
        assert obj is None and f"matches {_OVER} timeline objects" in err
        assert err.count("Fillet1@") == _CAP and _HELD_BACK in err

    def test_an_ambiguous_feature_name_AT_the_cap_names_every_object(self):
        objs = [_tl_obj("Fillet1", i) for i in range(_CAP)]
        obj, err = inp.resolve_timeline_object(objs, "Fillet1", "'feature'")
        assert obj is None and f"matches {_CAP} timeline objects" in err
        assert err.count("Fillet1@") == _CAP and f"Fillet1@{_CAP - 1}" in err
        assert "not listed" not in err

    def test_a_shared_plane_name_over_the_cap_counts_the_candidates_it_did_not_name(self):
        _install_planes(subs=[(f"C{i}", ["Mid"], [f"C{i}:1"]) for i in range(_OVER)])
        val, err = inp.PlaneRef("plane").resolve("Mid")
        assert val is None and f"{_OVER} construction planes share that name" in err
        assert err.count(":1:Mid") == _CAP and _HELD_BACK in err

    def test_a_plane_whose_owner_is_placed_over_the_cap_times_counts_the_instances(self):
        _install_planes(subs=[("Jaw", ["Mid"], [f"Jaw:{i}" for i in range(1, _OVER + 1)])])
        val, err = inp.PlaneRef("plane").resolve("Mid")
        assert val is None and f"placed {_OVER} times" in err
        assert err.count("Jaw:") == _CAP and _HELD_BACK in err

    def test_the_construction_axis_miss_counts_the_axes_it_did_not_list(self, axis_env):
        listed = 10                 # this listing carries a cap of its own, wider than the shared one
        axis_env(axes=[_FakeConstructionAxis(f"Ax{i:02d}") for i in range(listed + 1)])
        val, err = inp.AxisRef("axis").resolve("Nope")
        assert val is None and "Construction axes in 'Active':" in err
        assert err.count("Ax") == listed and _HELD_BACK in err

    def test_an_ambiguous_joint_origin_name_over_the_cap_counts_the_candidates(self):
        comps = [_Comp(f"C{i}", [_JO("Center")]) for i in range(_OVER)]
        root = _RootJO(jos=[], occ_by_comp={c.name: [_OccJO(f"{c.name}:1", c)] for c in comps})
        jo, err = _install_jo(_DesignJO(root, subs=comps)).resolve("Center")
        assert jo is None and f"{_OVER} Joint Origins share that name" in err
        assert err.count(":1:Center") == _CAP and _HELD_BACK in err

    def test_the_joint_origin_miss_counts_the_names_it_did_not_list(self):
        design = _DesignJO(_RootJO(jos=[_JO(f"JO{i}") for i in range(_OVER)]))
        jo, err = _install_jo(design).resolve("Nope")
        assert jo is None and "no Joint Origin named 'Nope'" in err
        assert err.count("'JO") == _CAP and _HELD_BACK in err

    def test_a_token_naming_more_entities_than_its_cap_counts_the_rest(self, token_env):
        # The candidate list a handle's ambiguity refusal prints is what the caller judges "which of
        # these did I mean" from, so a hit past the cap has to be COUNTED, not dropped.
        n = inp._TOKEN_CANDIDATES_LISTED + 1
        token_env({"TOK": [_SplitFace((float(i), 0.0, 0.0), context=f"Arm:{i}") for i in range(n)]})
        val, err = inp.GeometryHandle("on_face", require="planar_face").resolve("TOK")
        assert val is None and f"{n} entities" in err
        assert err.count("Arm:") == inp._TOKEN_CANDIDATES_LISTED
        assert f"Arm:{n - 1}" not in err and _HELD_BACK in err

    def test_a_token_naming_exactly_its_cap_of_entities_names_every_one(self, token_env):
        n = inp._TOKEN_CANDIDATES_LISTED
        token_env({"TOK": [_SplitFace((float(i), 0.0, 0.0), context=f"Arm:{i}") for i in range(n)]})
        val, err = inp.GeometryHandle("on_face", require="planar_face").resolve("TOK")
        assert val is None and f"{n} entities" in err
        assert err.count("Arm:") == n and f"Arm:{n - 1}" in err
        assert "not listed" not in err

    def test_a_datum_whose_owner_is_placed_over_the_cap_times_counts_the_paths(self, axis_env):
        # single_placement's refusal IS the caller's list of instances to re-address from, and it
        # reaches the wire through every consumer of a possibly-foreign entity (an axis, a moved
        # body, a pattern direction) - one line per placement is what an unbounded join costs there.
        env = axis_env()
        wheel = MakeComp(name="Wheel", entity_token="TOKEN:Wheel")
        axis, _ = _datum_in(wheel, (0, 0, 0), world_origin=(5, 0, 0))
        env.place(wheel, *[f"Assy:1+Wheel:{i}" for i in range(1, _OVER + 1)])
        pair, err = inp.axis_line_of("rotate_axis", axis)
        assert pair is None and f"placed {_OVER} times" in err
        assert err.count("Assy:1+Wheel:") == _CAP and _HELD_BACK in err

    def test_a_datum_whose_owner_is_placed_AT_the_cap_names_every_path(self, axis_env):
        env = axis_env()
        wheel = MakeComp(name="Wheel", entity_token="TOKEN:Wheel")
        axis, _ = _datum_in(wheel, (0, 0, 0), world_origin=(5, 0, 0))
        env.place(wheel, *[f"Assy:1+Wheel:{i}" for i in range(1, _CAP + 1)])
        pair, err = inp.axis_line_of("rotate_axis", axis)
        assert pair is None and f"placed {_CAP} times" in err
        assert err.count("Assy:1+Wheel:") == _CAP and f"Assy:1+Wheel:{_CAP}" in err
        assert "not listed" not in err

    def test_the_profile_selectors_sketch_miss_counts_the_names_it_did_not_list(self, monkeypatch):
        # Both answers to "which sketches does this design hold" render through
        # _available_sketch_names, so the profile selector's miss and SketchRefList's miss cannot
        # differ in length on one design.
        n = inp._SKETCH_NAMES_LISTED + 1
        _install_profiles(sketches=[(f"S{i:02d}", [Profile(f"p{i}")]) for i in range(n)],
                          monkeypatch=monkeypatch)
        val, err = inp.ProfileRef("profile").resolve({"sketch": "Nope", "profile_index": 0})
        assert val is None and "no sketch named 'Nope'" in err
        assert f"S{n - 2:02d}" in err and f"S{n - 1:02d}" not in err
        assert _HELD_BACK in err

    def test_the_profile_selectors_sketch_miss_names_every_sketch_AT_the_cap(self, monkeypatch):
        n = inp._SKETCH_NAMES_LISTED
        _install_profiles(sketches=[(f"S{i:02d}", [Profile(f"p{i}")]) for i in range(n)],
                          monkeypatch=monkeypatch)
        val, err = inp.ProfileRef("profile").resolve({"sketch": "Nope", "profile_index": 0})
        assert val is None and f"S{n - 1:02d}" in err
        assert "not listed" not in err


class TestJointOriginMissTellsNamesakesApart:
    """The MISS listing is the caller's whole vocabulary for the retry. A name it prints twice has
    restated the count and named nothing, so a repeated name gives way to its qualified form."""

    def _design(self, *comp_names_and_jos):
        comps = [_Comp(name, [_JO(jo) for jo in jos]) for name, jos in comp_names_and_jos]
        root = _RootJO(jos=[], occ_by_comp={c.name: [_OccJO(f"{c.name}:1", c)] for c in comps})
        return _DesignJO(root, subs=comps)

    def test_a_name_two_joint_origins_share_prints_the_forms_that_tell_them_apart(self):
        jo, err = _install_jo(self._design(("A", ["Center"]), ("B", ["Center"]))).resolve("Nope")
        assert jo is None and "no Joint Origin named 'Nope'" in err
        assert "'A:1:Center'" in err and "'B:1:Center'" in err
        assert "'Center'" not in err          # the bare name addresses neither of them

    def test_a_name_only_one_joint_origin_carries_stays_the_bare_name(self):
        # A discriminator where the name already identifies one separates nothing, and the bare name
        # is the string this input resolves.
        jo, err = _install_jo(self._design(("A", ["Grip"]), ("B", ["Center"]))).resolve("Nope")
        assert jo is None
        assert "'Grip'" in err and "'Center'" in err
        assert "A:1:Grip" not in err and "B:1:Center" not in err


# ── BodyRefList's component SCOPE: the answer to the auto-named 'Body1' ──────────────────────────
#
# Fusion names every component's first body 'Body1', so a design-wide body NAME is shared by
# construction. The scope narrows the name to ONE component (or ONE placement) exactly as the sketch
# scope does, and it filters the SAME per-placement candidate set the unscoped walk resolves from -
# so a scope decides WHICH candidate answers and never changes what a body reference resolves to.

def _install_shared_body_name(monkeypatch, name="Body1", placements=("Bracket:1",)):
    """A root component 'Carrier' owning a body called `name` natively, plus a sub-component
    'Bracket' owning one of the SAME name reached through each path in `placements`. Returns
    (the root's native body, [each placement's proxy])."""
    import adsk.fusion
    monkeypatch.setattr(adsk.fusion, "BRepBody", BRepBody, raising=False)
    sub_body = BRepBody(name, entity_token="TOK-SUB")
    sub = MakeComp(name="Bracket", bodies=[sub_body], entity_token="TOKEN:Bracket")
    sub_body.parentComponent = sub
    occs, proxies = [], []
    for path in placements:
        occ = make_occurrence(path=path, component=sub)
        proxy = body_proxy(sub_body, occ)
        occ.bRepBodies = _NamedCollection([proxy])
        occs.append(occ)
        proxies.append(proxy)
    root_body = BRepBody(name, entity_token="TOK-ROOT")
    root = MakeComp(name="Carrier", bodies=[root_body], occurrences=occs,
                    entity_token="TOKEN:Carrier")
    root_body.parentComponent = root
    design = make_design(comp=root, all_components=[root, sub])
    monkeypatch.setattr(inp._common, "design", lambda: design)
    monkeypatch.setattr(inp._common, "target_component", lambda _d=None: root)
    return root_body, proxies


def _install_cased_bodies(monkeypatch):
    """One PLACED component holding 'Pin' and 'pin' - the pair a case-insensitive match widens to.
    Returns (the 'Pin' proxy, the 'pin' proxy)."""
    import adsk.fusion
    monkeypatch.setattr(adsk.fusion, "BRepBody", BRepBody, raising=False)
    upper, lower = BRepBody("Pin", entity_token="TOK-UPPER"), BRepBody("pin", entity_token="TOK-LOWER")
    sub = MakeComp(name="Bracket", bodies=[upper, lower], entity_token="TOKEN:Bracket")
    upper.parentComponent = lower.parentComponent = sub
    occ = make_occurrence(path="Bracket:1", component=sub)
    proxies = (body_proxy(upper, occ), body_proxy(lower, occ))
    occ.bRepBodies = _NamedCollection(list(proxies))
    root = MakeComp(name="Carrier", occurrences=[occ], entity_token="TOKEN:Carrier")
    design = make_design(comp=root, all_components=[root, sub])
    monkeypatch.setattr(inp._common, "design", lambda: design)
    monkeypatch.setattr(inp._common, "target_component", lambda _d=None: root)
    return proxies


def _install_unplaced_component(monkeypatch, body_names):
    """A 'Bracket' component holding `body_names` that NO occurrence places - the one scope the
    design-wide walk cannot reach. Returns its native bodies."""
    import adsk.fusion
    monkeypatch.setattr(adsk.fusion, "BRepBody", BRepBody, raising=False)
    bodies = [BRepBody(n, entity_token=f"TOK-{n}") for n in body_names]
    sub = MakeComp(name="Bracket", bodies=bodies, entity_token="TOKEN:Bracket")
    for b in bodies:
        b.parentComponent = sub
    root = MakeComp(name="Carrier", entity_token="TOKEN:Carrier")
    design = make_design(comp=root, all_components=[root, sub])
    monkeypatch.setattr(inp._common, "design", lambda: design)
    monkeypatch.setattr(inp._common, "target_component", lambda _d=None: root)
    return bodies


class _UnnamedOcc(FakeOccurrence):
    """An occurrence whose COMPONENT reads while its own fullPathName and name do not - what a scope
    resolved by HANDLE can still be left holding. ``name`` is a plain attribute on the shared fake,
    so a read that DECLINES there is only expressible here."""

    def __init__(self, comp):
        super().__init__(component=comp, raises_on={"fullPathName": "path unavailable"})

    @property
    def name(self):
        raise RuntimeError("name unavailable")

    @name.setter
    def name(self, _value):
        pass                    # the shared constructor assigns one; this instance never reads it


def _install_unnamed_placement(monkeypatch):
    """A design whose handle 'OCCTOK' resolves to an occurrence that will not name itself."""
    import adsk.fusion
    monkeypatch.setattr(adsk.fusion, "BRepBody", BRepBody, raising=False)
    monkeypatch.setattr(adsk.fusion, "Occurrence", FakeOccurrence, raising=False)
    pin = BRepBody("Pin", entity_token="TOK-PIN")
    sub = MakeComp(name="Bracket", bodies=[pin], entity_token="TOKEN:Bracket")
    pin.parentComponent = sub
    root = MakeComp(name="Carrier", entity_token="TOKEN:Carrier")
    design = make_design(comp=root, all_components=[root, sub],
                         tokens={"OCCTOK": _UnnamedOcc(sub)})
    monkeypatch.setattr(inp._common, "design", lambda: design)
    monkeypatch.setattr(inp._common, "target_component", lambda _d=None: root)
    return design


class TestBodyRefListComponentScope:
    def _scoped(self, **kw):
        return inp.BodyRefList("bodies", scope_input="component", **kw)

    def test_a_shared_body_name_with_no_scope_is_refused_naming_the_scope_input(self, monkeypatch):
        # The qualified spellings stay the primary remedy; the scope is the second way out, and a
        # tool that declares one must say so or the caller never learns its own input narrows this.
        _install_shared_body_name(monkeypatch)
        got, err = self._scoped().resolve(["Body1"])
        assert got is None and "names 2 bodies" in err
        assert "'Carrier:Body1'" in err and "'Bracket:1:Body1'" in err
        assert "'component'" in err

    def test_a_kind_declaring_no_scope_input_never_names_one(self, monkeypatch):
        # A refusal may only name an input the consuming tool's schema actually takes.
        _install_shared_body_name(monkeypatch)
        got, err = inp.BodyRefList("bodies").resolve(["Body1"])
        assert got is None and "names 2 bodies" in err
        assert "'component'" not in err

    def test_the_scope_picks_the_named_components_body(self, monkeypatch):
        _native, proxies = _install_shared_body_name(monkeypatch)
        got, err = self._scoped().resolve(["Body1"], "Bracket")
        assert err is None and got == [proxies[0]]

    def test_the_scope_picks_the_ROOT_owned_body_when_it_names_the_root(self, monkeypatch):
        native, _proxies = _install_shared_body_name(monkeypatch)
        got, err = self._scoped().resolve(["Body1"], "Carrier")
        assert err is None and got == [native]

    def test_the_scope_narrows_EVERY_name_in_the_list(self, monkeypatch):
        # A list resolves one name at a time, so a scope applied to the first element only would
        # machine one component's body beside another's and report the same count either way.
        _native, proxies = _install_shared_body_name(monkeypatch)
        got, err = self._scoped().resolve(["Body1", "Body1"], "Bracket")
        assert err is None and got == [proxies[0], proxies[0]]

    def test_a_scope_the_design_does_not_hold_is_refused(self, monkeypatch):
        # An input a caller can get wrong without being told is a trap: dropping the unusable scope
        # would resolve against a design the caller never scoped.
        _install_shared_body_name(monkeypatch)
        got, err = self._scoped().resolve(["Body1"], "Ghost")
        assert got is None and "No component named 'Ghost'" in err

    def test_a_name_the_scope_does_not_hold_lists_what_it_holds(self, monkeypatch):
        _install_shared_body_name(monkeypatch)
        got, err = self._scoped().resolve(["Ghost"], "Bracket")
        assert got is None
        assert "'Bracket'" in err and "no body named 'Ghost'" in err and "'Body1'" in err

    def test_a_handle_still_resolves_under_a_scope(self, monkeypatch):
        # The scope narrows the NAME vocabulary; a handle addresses one body already, and refusing
        # it for sitting outside the scope would reject a reference that names its instance.
        native, _proxies = _install_shared_body_name(monkeypatch)
        design = inp._common.design()
        monkeypatch.setattr(design, "_tokens", {"TOK-ROOT": native}, raising=False)
        got, err = self._scoped().resolve(["TOK-ROOT"], "Bracket")
        assert err is None and got == [native]

    def test_a_component_scope_cannot_separate_two_placements_and_says_so(self, monkeypatch):
        # A component NAME answers every instance of it, so a body in a twice-placed component stays
        # ambiguous under that scope - resolving there would pick a placement the caller did not.
        _install_shared_body_name(monkeypatch, placements=("Bracket:1", "Bracket:2"))
        got, err = self._scoped().resolve(["Body1"], "Bracket")
        assert got is None and "names 2 bodies" in err
        assert "'Bracket:1:Body1'" in err and "'Bracket:2:Body1'" in err

    def test_an_occurrence_path_scope_picks_ONE_placement(self, monkeypatch):
        # The remedy the refusal above leaves has to work: the occurrence half of the scope's own
        # vocabulary names one instance, and that instance's proxy is what resolves.
        _native, proxies = _install_shared_body_name(monkeypatch,
                                                     placements=("Bracket:1", "Bracket:2"))
        got, err = self._scoped().resolve(["Body1"], "Bracket:2")
        assert err is None and got == [proxies[1]]

    def test_a_scope_with_no_active_design_is_refused_in_its_own_words(self, monkeypatch):
        # The scope is resolved against the design, so with none there is nothing to narrow to. The
        # sentence has to describe what THIS call was resolving: the vocabularies underneath refuse
        # in their own nouns ("to resolve the occurrence against"), which sends the caller to fix a
        # scope spelling when the design is what is missing.
        monkeypatch.setattr(inp._common, "design", lambda: None)
        got, err = self._scoped().resolve(["Body1"], "Bracket")
        assert got is None
        assert err == "No active design to resolve the body names against."

    def test_a_case_variant_inside_the_scope_does_not_manufacture_an_ambiguity(self, monkeypatch):
        # The match is case-insensitive, so a scope holding 'Pin' and 'pin' WIDENS to two hits. The
        # one spelled exactly as asked is the answer, exactly as on the unscoped path.
        upper, _lower = _install_cased_bodies(monkeypatch)
        got, err = self._scoped().resolve(["Pin"], "Bracket")
        assert err is None and got == [upper]

    def test_a_scope_no_occurrence_places_is_asked_directly(self, monkeypatch):
        # A component nothing places is the one scope the design-wide walk cannot reach - it walks
        # the active component, the root and each occurrence - so the scope answers for itself.
        native = _install_unplaced_component(monkeypatch, ["Pin"])[0]
        got, err = self._scoped().resolve(["Pin"], "Bracket")
        assert err is None and got == [native]

    def test_an_unplaced_scope_holding_the_name_twice_is_refused(self, monkeypatch):
        # Two bodies answering one name inside the ONE scope that was asked directly: picking either
        # would be the first-match this resolver exists to refuse.
        _install_unplaced_component(monkeypatch, ["Pin", "pin"])
        got, err = self._scoped().resolve(["Pin"], "Bracket")
        assert got is None and "holds 2 bodies named 'Pin'" in err
        assert "'Bracket:Pin'" in err and "'Bracket:pin'" in err

    def test_a_scope_whose_placement_will_not_name_itself_is_refused(self, monkeypatch):
        # The scope RESOLVED - its component read - but neither its path nor its name did, so there
        # is no key to narrow a body reference by. Narrowing on the empty key set would match every
        # candidate and silently resolve design-wide under a scope the caller did pass.
        _install_unnamed_placement(monkeypatch)
        got, err = self._scoped().resolve(["Pin"], "OCCTOK")
        assert got is None and "nothing that names what it resolved to could be read" in err

    def test_a_scope_passed_with_an_EMPTY_list_is_refused(self, monkeypatch):
        # There is no name for the scope to narrow, so it applies to nothing while the consuming
        # tool goes on acting on the empty form - the form the caller asked to narrow. Dropping it
        # leaves that caller believing a scope took.
        _install_shared_body_name(monkeypatch)
        got, err = self._scoped().resolve([], "Bracket")
        assert got is None
        assert "'component' was passed with an empty 'bodies'" in err
        assert "would apply to nothing" in err

    def test_an_unresolvable_scope_with_an_empty_list_is_refused_too(self, monkeypatch):
        # The empty-list refusal fires ahead of any scope resolution, so it refuses whatever the
        # scope spells - a component the design does not hold included.
        _install_shared_body_name(monkeypatch)
        got, err = self._scoped().resolve([], "Ghost")
        assert got is None and "'component'" in err and "'bodies'" in err

    def test_an_empty_list_with_no_scope_still_resolves_to_no_bodies(self, monkeypatch):
        # The empty form is what a consumer switches its whole-scope behaviour on (cam_select_geometry
        # machines the setup's own models there) - only a scope beside it is refused.
        _install_shared_body_name(monkeypatch)
        got, err = self._scoped().resolve([], "")
        assert err is None and got == []

    def test_a_kind_declaring_no_scope_input_never_refuses_on_a_component(self, monkeypatch):
        # A refusal may only name an input the consuming tool's schema takes, and a kind carrying no
        # scope input takes no component value at all.
        _install_shared_body_name(monkeypatch)
        got, err = inp.BodyRefList("bodies").resolve([], "Bracket")
        assert err is None and got == []

    def test_a_REQUIRED_empty_list_still_asks_for_a_body_first(self, monkeypatch):
        # The scope is the secondary problem there: the call cannot proceed without a body either
        # way, so the refusal names the missing list rather than the input beside it.
        _install_shared_body_name(monkeypatch)
        got, err = self._scoped(required=True).resolve([], "Bracket")
        assert got is None and err == "'bodies' needs at least one body (handle or name)."
