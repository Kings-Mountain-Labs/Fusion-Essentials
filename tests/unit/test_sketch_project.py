"""Unit tests for ``sketch_project.py`` - create sketch curves from existing model geometry, via
Sketch.project2 (into_sketch), Sketch.projectToSurface (to_surface) and
Sketch.intersectWithSketchPlane (intersect).

Pinned here (no live Fusion): the link flag flows to project2 and is refused on the other actions;
projectToSurface receives FACES first, three arguments for closest_point and the direction ENTITY
fourth for along_vector; a source sketch that IS the receiving sketch is refused with Fusion's own
message; intersect's silent outcomes are gated by the tool (zero created is an error naming every
entity and the plane, a partial result is an ok that names the zero contributors); and the created
entities are reported as '<type>:<index>' refs computed from the per-collection count delta. The
actual BRep projection is a live side-effect covered by the post-reload verification pass.
"""

import types

import pytest

from conftest import (BRepBody, MakeComp, Sketch, SketchCurves, _NamedCollection, load_tool,
                      make_design, make_source_document, install, payload as _payload)

sp = load_tool("sketch_project")


def comp_with_axes(name="Comp1", token="comp-1"):
    """A component carrying the three origin construction axes and an entityToken. MakeComp holds
    the standard collections; a tool-specific surface is attached after construction (its own
    contract). The token is what a same-component test must compare on - wrappers are never
    identity-stable."""
    return MakeComp(name=name, entity_token=token,
                    construction_axes=(object(), object(), object()))


# ── fakes: a sketch whose projections grow its curve/point collections ────────

class _Coll(_NamedCollection):
    """The shared collection sized by count alone - grow(k) is a projection landing k entities."""
    def __init__(self, n=0):
        super().__init__([None] * n)

    def grow(self, k):
        self._items += [None] * k


class _Curves(SketchCurves):
    """The shared SketchCurves whose line/arc/circle sub-collections a projection grows."""
    def __init__(self, lines=0, arcs=0, circles=0):
        super().__init__()
        self.sketchLines = _Coll(lines)
        self.sketchArcs = _Coll(arcs)
        self.sketchCircles = _Coll(circles)


class _Created:
    """A created sketch entity stand-in - the four fields a projection's result is read back
    through. is2D is False for a curve lying on a 3D face, True for one on the sketch x-y plane;
    isReference marks a reference curve; isLinked is the projected curve's own link to its source,
    the flag project2's isLinked argument asks for; referencedEntity is the source it is linked to,
    and reads null for a non-parametric reference. `unreadable` drops a field entirely, the case a
    read-back must not fold into either state."""
    def __init__(self, is2d=True, referenced=None, is_reference=True, is_linked=True,
                 unreadable=()):
        if "is2D" not in unreadable:
            self.is2D = is2d
        if "isReference" not in unreadable:
            self.isReference = is_reference
        if "isLinked" not in unreadable:
            self.isLinked = is_linked
        self.referencedEntity = referenced


class FakeSketch(Sketch):
    """The shared Sketch whose projections append entities across its collections and return that many
    stand-ins (mirroring the real returns: a list/vector of created SketchEntity).

    ``creates`` drives project2; ``surface_creates`` drives projectToSurface; ``contributions`` maps a
    source entityToken to how many curves intersectWithSketchPlane makes for it (None = one each), so
    a non-crossing entity is modelled by giving it zero. ``ref_pattern`` sets isReference per created
    curve, cycling (None = the field does not read); ``link_pattern`` does the same for isLinked on
    project2's result, and when it is omitted every created curve reports the linkage that was
    asked for. ``silent_growth`` grows the collections but returns an EMPTY list - the case where
    the census is the only evidence."""

    def __init__(self, name="Sketch1", creates=None, base=None, token=None,
                 surface_creates=None, surface_is2d=False, contributions=None,
                 surface_raises=None, intersect_raises=None, plane="XY", unreadable=(),
                 is_reference=True, unreferenced=False, ref_pattern=None, silent_growth=False,
                 link_pattern=None):
        b = base or {}
        super().__init__(name=name,
                         curves=_Curves(b.get("line", 0), b.get("arc", 0), b.get("circle", 0)))
        self.sketchPoints = _Coll(b.get("point", 0))
        self.creates = creates if creates is not None else {"line": 2, "circle": 1, "point": 1}
        self.surface_creates = surface_creates if surface_creates is not None else {"line": 1}
        self.surface_is2d = surface_is2d
        self.contributions = contributions
        self.surface_raises = surface_raises
        self.intersect_raises = intersect_raises
        self.unreadable = unreadable
        self.is_reference = is_reference
        self.unreferenced = unreferenced
        self.ref_pattern = ref_pattern
        self.link_pattern = link_pattern
        self.silent_growth = silent_growth
        self.entityToken = token
        self.parentComponent = comp_with_axes()
        self.referencePlane = types.SimpleNamespace(name=plane)
        self.projected_with = None
        self.surface_args = None
        self.intersect_args = None

    def _grow(self, c):
        self.sketchCurves.sketchLines.grow(c.get("line", 0))
        self.sketchCurves.sketchArcs.grow(c.get("arc", 0))
        self.sketchCurves.sketchCircles.grow(c.get("circle", 0))
        self.sketchPoints.grow(c.get("point", 0))
        return sum(c.values())

    def _made(self, i, is2d, referenced=None):
        """One created curve. ref_pattern cycles isReference per index; None drops the field."""
        flag, unread = self.is_reference, tuple(self.unreadable)
        if self.ref_pattern is not None:
            flag = self.ref_pattern[i % len(self.ref_pattern)]
            if flag is None:
                unread += ("isReference",)
        return _Created(is2d=is2d, referenced=referenced, is_reference=flag, unreadable=unread)

    def project2(self, entities, is_linked):
        """The created curves carry isLinked: by default what the call asked for, else the cycling
        link_pattern (None = the flag does not read on that curve)."""
        self.projected_with = (list(entities), is_linked)
        n = self._grow(self.creates)
        if self.silent_growth:
            return []
        pattern = self.link_pattern if self.link_pattern is not None else (is_linked,)
        made = []
        for i in range(n):
            flag = pattern[i % len(pattern)]
            unread = tuple(self.unreadable) + (("isLinked",) if flag is None else ())
            made.append(_Created(is_linked=bool(flag), unreadable=unread))
        return made

    def projectToSurface(self, *args):
        # SWIG binds both the 3-argument and the 4-argument prototype; record exactly what arrived so
        # the argument ORDER and arity are assertable.
        self.surface_args = args
        if self.surface_raises:
            raise RuntimeError(self.surface_raises)
        n = self._grow(self.surface_creates)
        if self.silent_growth:
            return []
        return [self._made(i, self.surface_is2d) for i in range(n)]

    def intersectWithSketchPlane(self, entities):
        self.intersect_args = list(entities)
        if self.intersect_raises:
            raise RuntimeError(self.intersect_raises)
        out = []
        for e in self.intersect_args:
            n = 1 if self.contributions is None else self.contributions.get(
                getattr(e, "entityToken", None), 0)
            # a non-parametric reference reads referencedEntity as null, so the source is unknowable
            src = None if self.unreferenced else e
            out += [self._made(len(out) + k, True, referenced=src) for k in range(n)]
        self._grow({"line": len(out)})
        return [] if self.silent_growth else out


@pytest.fixture
def call(monkeypatch):
    """Return a caller that wires a FakeSketch + fake entities into the handler and returns (payload
    or raw result, the sketch). The sketch resolver and the geometry-handle-list resolver are stubbed
    so the test exercises the handler's composition (project2 + delta + refs + honesty), not the live
    token/sketch resolution covered elsewhere."""
    install(sp, make_design())

    def _run(sketch=None, entities="E1", link=True, resolve_err=None, sketch_none=False,
             raw=False, **kw):
        sk = sketch if sketch is not None else FakeSketch()
        # remedy= is the shared resolver's per-caller refusal sentence; the stub takes it so it
        # matches the real signature the scope helper calls through.
        if sketch_none:
            monkeypatch.setattr(sp._common, "find_or_recent_sketch",
                                lambda d, n, remedy=None: (None, n or None, None))
        else:
            monkeypatch.setattr(sp._common, "find_or_recent_sketch",
                                lambda d, n, remedy=None: (sk, n or None, None))
        if resolve_err:
            monkeypatch.setattr(sp._ENTITIES, "resolve", lambda raw: (None, resolve_err))
        else:
            monkeypatch.setattr(sp._ENTITIES, "resolve", lambda raw: ([object(), object()], None))
        res = sp.handler(entities=entities, link=link, **kw)
        return (res if raw else _payload(res)), sk
    return _run


def _stub(monkeypatch, kind, value, err=None):
    monkeypatch.setattr(kind, "resolve", lambda raw, v=value, e=err: ((None, e) if e else (v, None)))


@pytest.fixture
def run(monkeypatch):
    """Return a caller for the to_surface / intersect actions. Every typed input kind is stubbed to a
    fixed resolution so the test exercises the per-action assembly, guards and read-back."""
    install(sp, make_design())
    curve = object()

    def _run(action, sketch=None, source=None, faces_out=None, edges_out=None, bodies_out=None,
             entities_out=None, raw=False, **kw):
        """`*_out` are what each typed input kind RESOLVES to; **kw are the handler's own arguments."""
        sk = sketch if sketch is not None else FakeSketch()
        monkeypatch.setattr(sp._common, "find_or_recent_sketch",
                            lambda d, n, remedy=None: (sk, n or None, None))
        monkeypatch.setattr(sp._common, "find_sketch",
                            lambda d, n, remedy=None: (source, None))
        monkeypatch.setattr(sp._common, "resolve_entity_ref",
                            lambda s, r: curve if r == "line:0" else None)
        _stub(monkeypatch, sp._TARGET_FACES, faces_out if faces_out is not None else ["FACE"])
        _stub(monkeypatch, sp._CURVE_HANDLES, edges_out if edges_out is not None else ["EDGE"])
        _stub(monkeypatch, sp._BODIES, bodies_out if bodies_out is not None else [])
        _stub(monkeypatch, sp._ENTITIES, entities_out if entities_out is not None else [])
        res = sp.handler(action=action, **kw)
        return (res if raw else _payload(res)), sk
    _run.curve = curve
    return _run


# ── into_sketch: link flag + reported refs ────────────────────────────────────

class TestProject:
    def test_projects_and_reports_created_count(self, call):
        out, sk = call(sketch=FakeSketch(creates={"line": 2, "circle": 1, "point": 1}))
        assert out["projected"] is True
        assert out["created_count"] == 4

    def test_default_action_is_into_sketch(self, call):
        out, sk = call()
        assert out["action"] == "into_sketch"

    def test_link_true_flows_to_project2(self, call):
        out, sk = call(link=True)
        assert sk.projected_with[1] is True
        assert out["linked"] is True                 # read off the curves, not echoed
        assert out["link_requested"] is True

    def test_link_false_flows_to_project2(self, call):
        out, sk = call(link=False)
        assert sk.projected_with[1] is False
        assert out["linked"] is False
        assert "Static copy" in out["note"]

    def test_link_defaults_to_linked_when_omitted(self, call):
        out, sk = call(link=None)
        assert sk.projected_with[1] is True
        assert out["linked"] is True
        assert out["link_requested"] is True


class TestLinkReadBack:
    """'linked' is the flag READ OFF the created curves, the way the file's other two actions read
    isReference back. Echoing the request instead reports link=true on curves that landed unlinked
    and reports a state on curves that answered nothing."""

    def test_unreadable_islinked_publishes_null_not_the_request(self, call):
        out, sk = call(link=True, sketch=FakeSketch(creates={"line": 2}, link_pattern=(None,)))
        assert out["linked"] is None
        assert out["link_requested"] is True
        assert "isLinked did not read on any of the 2 created curves" in out["note"]
        assert "link=true is the value REQUESTED" in out["note"]
        assert "disagree" not in out["note"]

    def test_one_unreadable_curve_among_readable_ones_publishes_null(self, call):
        # the boundary: 2 of 3 read True, so there is no answer every created curve agrees on
        out, sk = call(link=True,
                       sketch=FakeSketch(creates={"line": 3}, link_pattern=(True, True, None)))
        assert out["linked"] is None
        # what was observed is one unread flag, so the note may not report a split result
        assert "isLinked did not read on 1 of the 3 created curves" in out["note"]
        assert "split result" not in out["note"]

    def test_every_curve_readable_keeps_the_verdict(self, call):
        # the one-more boundary on the other side: all three read True -> the claim is made
        out, sk = call(link=True,
                       sketch=FakeSketch(creates={"line": 3}, link_pattern=(True, True, True)))
        assert out["linked"] is True
        assert "read back as LINKED" in out["note"]

    def test_curves_that_all_land_unlinked_against_the_request_are_an_error(self, call):
        # project2 honours isLinked exactly, so a unanimous opposite read is a failed call
        out, sk = call(link=True, sketch=FakeSketch(creates={"line": 2}, link_pattern=(False,)),
                       raw=True)
        assert out["isError"] is True
        assert "link=true was requested" in out["message"]
        assert "read back as NOT linked" in out["message"]

    def test_curves_that_all_land_linked_against_link_false_are_an_error(self, call):
        # the mirror direction: the error keys on the MISMATCH, not on one value of the flag
        out, sk = call(link=False, sketch=FakeSketch(creates={"line": 2}, link_pattern=(True,)),
                       raw=True)
        assert out["isError"] is True
        assert "link=false was requested" in out["message"]
        assert "read back as LINKED" in out["message"]

    def test_the_mismatch_error_names_the_curves_it_left_in_the_sketch(self, call):
        # partial success: the curves exist, so the error says what landed and how to remove it
        out, sk = call(link=True, sketch=FakeSketch(creates={"line": 2}, link_pattern=(False,)),
                       raw=True)
        assert "line:0, line:1" in out["message"]
        assert "sketch_delete_entity" in out["message"]

    def test_curves_matching_the_request_are_not_errored(self, call):
        # the boundary the error must not overshoot: agreement is an ok, not a failure
        out, sk = call(link=False, sketch=FakeSketch(creates={"line": 2}, link_pattern=(False,)),
                       raw=True)
        assert "isError" not in out or out["isError"] is False

    def test_a_split_result_is_disclosed_not_errored(self, call):
        # the other boundary: only a UNANIMOUS opposite read is a measured contradiction, so a
        # partly-honoured result stays an ok whose note reports the split
        out, sk = call(link=True,
                       sketch=FakeSketch(creates={"line": 2}, link_pattern=(True, False)),
                       raw=True)
        assert "isError" not in out or out["isError"] is False

    def test_unreadable_flags_are_disclosed_not_errored(self, call):
        # an unread flag is evidence of neither state, so it can never raise the mismatch error
        out, sk = call(link=True, sketch=FakeSketch(creates={"line": 2}, link_pattern=(None,)),
                       raw=True)
        assert "isError" not in out or out["isError"] is False

    def test_a_split_result_publishes_null_and_is_reported_as_a_split(self, call):
        out, sk = call(link=True,
                       sketch=FakeSketch(creates={"line": 2}, link_pattern=(True, False)))
        assert out["linked"] is None
        assert "Of 2 created curves, 1 read back as LINKED and 1 did not - a split result" \
            in out["note"]

    def test_an_empty_return_claims_no_linkage(self, call):
        # the census grew but the call returned nothing to read - zero reads support no claim
        out, sk = call(link=False, sketch=FakeSketch(creates={"line": 2}, silent_growth=True))
        assert out["created_count"] == 0
        assert out["entity_refs"] == ["line:0", "line:1"]
        assert out["linked"] is None
        assert "Static copy" not in out["note"]

    def test_an_empty_return_is_not_reported_as_curves_disagreeing(self, call):
        # project2 returning nothing while the collections still grew reaches the same null verdict
        # as a split or an unread flag - but there are no created curves to have read anything, so
        # a note about what they read (or failed to read) states a finding nobody made
        out, sk = call(link=True, sketch=FakeSketch(creates={"line": 2}, silent_growth=True))
        assert out["linked"] is None
        assert "project2 returned no entities to read" in out["note"]
        assert "link=true is the value REQUESTED" in out["note"]
        assert "created curves" not in out["note"]
        assert "split result" not in out["note"]

    def test_refs_are_type_index_from_count_delta(self, call):
        # empty sketch -> new line:0, line:1, circle:0, point:0
        out, sk = call(sketch=FakeSketch(creates={"line": 2, "circle": 1, "point": 1}))
        assert out["entity_refs"] == ["line:0", "line:1", "circle:0", "point:0"]

    def test_refs_offset_by_preexisting_entities(self, call):
        # a sketch already holding 3 lines + 1 circle -> new refs continue the index
        out, sk = call(sketch=FakeSketch(base={"line": 3, "circle": 1},
                                         creates={"line": 1, "circle": 2}))
        assert out["entity_refs"] == ["line:3", "circle:1", "circle:2"]

    def test_projected_entities_passed_through(self, call):
        out, sk = call(entities="E1,E2")
        # the resolved entity list (stubbed to 2 objects) reached project2
        assert len(sk.projected_with[0]) == 2

    def test_note_flags_curves_no_ref_addresses(self, call):
        # created_count (4) exceeds the refs the walk could name -> the note says so WITHOUT naming
        # a kind: which kinds carry a ref follows _common.ENTITY_REF_KINDS, and a sentence listing
        # them goes stale the moment that tuple grows.
        out, sk = call(sketch=FakeSketch(creates={"line": 1, "ellipse_like": 3}))
        # only line:0 is addressable; created_count counts all 4
        assert out["entity_refs"] == ["line:0"]
        assert out["created_count"] == 4
        assert ("Some created curves have no '<type>:<index>' ref - list them with "
                "sketch_get(include_entities=true).") in out["note"]


# ── honesty gate: zero entities created is an error ───────────────────────────

class TestHonesty:
    def test_zero_created_is_error_not_false_ok(self, call):
        out, sk = call(sketch=FakeSketch(creates={}), raw=True)
        assert out["isError"] is True
        assert "no sketch entities" in out["message"].lower()


# ── guards ────────────────────────────────────────────────────────────────────

class TestGuards:
    def test_no_sketch_is_error(self, call):
        out, sk = call(sketch_none=True, raw=True)
        assert out["isError"] is True
        assert "No sketch to project into" in out["message"]

    def test_named_missing_sketch_is_error(self, call):
        out, sk = call(sketch_none=True, raw=True, sketch_name="Ghost")
        assert out["isError"] is True and "Ghost" in out["message"]

    def test_entity_resolve_error_is_surfaced(self, call):
        out, sk = call(resolve_err="'entities' needs a handle from find_geometry", raw=True)
        assert out["isError"] is True
        assert "find_geometry" in out["message"]

    def test_no_active_design(self, monkeypatch):
        install(sp, make_design())
        sp._common.design = lambda: None
        res = sp.handler(entities="E1")
        assert res["isError"] is True and "design" in res["message"].lower()

    def test_unknown_action_is_refused(self, call):
        out, sk = call(raw=True, action="wrap")
        assert out["isError"] is True and "into_sketch" in out["message"]


# ── cross-action refusals: an input the chosen method cannot consume ──────────

class TestForeignInputs:
    def test_link_refused_on_intersect(self, run):
        out, sk = run("intersect", raw=True, link=False, bodies="Body1")
        assert out["isError"] is True
        assert "'link' applies only to action='into_sketch'" in out["message"]

    def test_bodies_refused_on_into_sketch(self, run):
        out, sk = run("into_sketch", raw=True, bodies="Body1")
        assert out["isError"] is True
        assert "'bodies' applies only to action='intersect'" in out["message"]

    def test_target_faces_refused_on_intersect(self, run):
        out, sk = run("intersect", raw=True, target_faces=["h1"])
        assert out["isError"] is True
        assert "'target_faces' applies only to action='to_surface'" in out["message"]

    def test_direction_refused_on_into_sketch(self, run):
        out, sk = run("into_sketch", raw=True, direction="z")
        assert out["isError"] is True
        assert "'direction' applies only to action='to_surface'" in out["message"]

    def test_entities_refused_on_to_surface(self, run):
        # projectToSurface takes target_faces + curves; a silently dropped 'entities' would sit in
        # the call looking like it steered the projection
        out, sk = run("to_surface", raw=True, entities="h1", curve_handles=["h2"])
        assert out["isError"] is True
        assert "'entities' applies only to action='into_sketch' / 'intersect'" in out["message"]
        assert sk.surface_args is None

    def test_entities_accepted_on_both_of_its_owners(self, run):
        b = BRepBody(name="Body1", entity_token="tok-1")
        out, sk = run("intersect", entities_out=[b], entities="h1")
        assert sk.intersect_args == [b]


# ── _same_sketch: the tri-state identity the self-projection refusal rests on ──

def _sketch_in(urn, name="Sketch1", token="tok"):
    """A sketch whose owning component belongs to the document with lineage id `urn` - the second
    half of the identity, reached through parentComponent.parentDesign (measured: a Sketch carries
    parentComponent). `urn=None` models a never-saved document."""
    sk = FakeSketch(name=name, token=token)
    sk.parentComponent.parentDesign = make_source_document(urn)
    return sk


class TestSameSketch:
    def test_two_reads_of_one_sketch_match(self):
        assert sp._same_sketch(_sketch_in("urn:a", token="tok"),
                               _sketch_in("urn:a", token="tok")) is True

    def test_two_documents_sharing_one_token_are_different_sketches(self):
        # the defect a bare-token compare carries: the token is document-local, so two references of
        # one source design hand back byte-identical tokens for two DIFFERENT sketches
        assert sp._same_sketch(_sketch_in("urn:host", token="tok"),
                               _sketch_in("urn:xref", token="tok")) is False

    def test_two_tokens_in_one_document_are_different_sketches(self):
        assert sp._same_sketch(_sketch_in("urn:a", token="tok-a"),
                               _sketch_in("urn:a", token="tok-b")) is False

    def test_an_unreadable_token_answers_unknown_not_false(self):
        # False is a positive claim ("these are two different sketches") that nothing was read to
        # support; None is the state a caller must branch on separately
        assert sp._same_sketch(FakeSketch(token=None), _sketch_in("urn:a", token="tok")) is None

    def test_two_unreadable_identities_do_not_compare_equal(self):
        # both sides through the None gate BEFORE the compare: two identities that each failed to
        # read are not evidence they are one sketch
        assert sp._same_sketch(FakeSketch(token=None), FakeSketch(token=None)) is None

    def test_a_missing_operand_answers_unknown(self):
        assert sp._same_sketch(None, _sketch_in("urn:a", token="tok")) is None

    def test_two_missing_operands_answer_unknown(self):
        # the input only the OPERAND gate decides: with one operand absent the identity gate below
        # answers anyway, but `None is None` is True, so without this gate two absent operands
        # short-circuit to "the same sketch" - a verdict from nothing at all
        assert sp._same_sketch(None, None) is None

    def test_one_object_handed_in_twice_short_circuits(self):
        # identity is kept as a free short-circuit, so it answers even where no identity reads
        blind = FakeSketch(token=None)
        assert sp._same_sketch(blind, blind) is True


# ── to_surface: projectToSurface(faces, curves, projectType[, directionEntity]) ─

class TestToSurface:
    def test_faces_reach_the_call_before_the_curves(self, run):
        out, sk = run("to_surface", faces_out=["F1", "F2"], edges_out=["E1"],
                      curve_handles=["h1"])
        assert sk.surface_args[0] == ["F1", "F2"]
        assert sk.surface_args[1] == ["E1"]

    def test_closest_point_calls_with_three_arguments(self, run):
        out, sk = run("to_surface", curve_handles=["h1"])
        assert len(sk.surface_args) == 3
        assert sk.surface_args[2] is sp.adsk.fusion.SurfaceProjectTypes.ClosestPointSurfaceProjectType

    def test_along_vector_passes_the_direction_entity_fourth(self, run):
        sk = FakeSketch()
        out, _ = run("to_surface", sketch=sk, curve_handles=["h1"],
                     project_type="along_vector", direction="z")
        assert len(sk.surface_args) == 4
        assert sk.surface_args[2] is sp.adsk.fusion.SurfaceProjectTypes.AlongVectorSurfaceProjectType
        # a world axis key becomes the component's origin ConstructionAxis, not a direction vector
        assert sk.surface_args[3] is sk.parentComponent.zConstructionAxis

    def test_along_vector_without_direction_is_refused(self, run):
        out, sk = run("to_surface", raw=True, curve_handles=["h1"], project_type="along_vector")
        assert out["isError"] is True
        assert "directionEntity" in out["message"]
        assert sk.surface_args is None

    def test_direction_with_closest_point_is_refused(self, run):
        out, sk = run("to_surface", raw=True, curve_handles=["h1"], direction="z")
        assert out["isError"] is True
        assert "along_vector" in out["message"]
        assert sk.surface_args is None

    def test_curve_refs_resolve_against_the_source_sketch(self, run):
        src = FakeSketch(name="Source", token="tok-src")
        sk = FakeSketch(name="Target", token="tok-tgt")
        out, _ = run("to_surface", sketch=sk, source=src,
                     source_sketch="Source", curve_refs=["line:0"])
        assert sk.surface_args[1] == [run.curve]

    def test_source_sketch_that_is_the_target_is_refused(self, run):
        sk = FakeSketch(name="Sketch1")
        out, _ = run("to_surface", raw=True, sketch=sk, source=sk,
                     source_sketch="Sketch1", curve_refs=["line:0"])
        assert out["isError"] is True
        assert "same sketch" in out["message"]

    def test_two_proxies_of_one_sketch_are_refused_as_a_self_projection(self, run):
        # a sketch read twice is a fresh proxy, so the refusal cannot rest on identity - both reads
        # answer one native identity (one token, one source document) and that is what matches
        sk = _sketch_in("urn:host", name="Sketch1", token="tok-same")
        proxy = _sketch_in("urn:host", name="Sketch1", token="tok-same")
        out, _ = run("to_surface", raw=True, sketch=sk, source=proxy,
                     source_sketch="Sketch1", curve_refs=["line:0"])
        assert out["isError"] is True
        assert "same sketch" in out["message"]

    def test_a_source_sketch_from_another_document_is_not_a_self_projection(self, run):
        # An entityToken is DOCUMENT-LOCAL: two components brought in by two references of ONE
        # source design read byte-identical tokens, and so do the sketches they hold. Refused on the
        # bare token, this legitimate cross-document projection never reaches Fusion at all.
        sk = _sketch_in("urn:host", name="Shared", token="tok-shared")
        src = _sketch_in("urn:xref", name="Shared", token="tok-shared")
        out, _ = run("to_surface", sketch=sk, source=src,
                     source_sketch="Shared", curve_refs=["line:0"])
        assert out["projected"] is True
        assert sk.surface_args[1] == [run.curve]

    def test_an_unreadable_identity_does_not_refuse_up_front(self, run):
        # The refusal STATES the two references are one sketch, so only a PROVEN match may raise it.
        # Neither sketch's identity reads here, which supports no such claim - the call goes through
        # and Fusion answers for itself.
        sk = FakeSketch(name="Target", token=None)
        src = FakeSketch(name="Source", token=None)
        out, _ = run("to_surface", sketch=sk, source=src,
                     source_sketch="Source", curve_refs=["line:0"])
        assert out["projected"] is True

    def test_curve_refs_without_source_sketch_is_refused(self, run):
        out, sk = run("to_surface", raw=True, curve_refs=["line:0"])
        assert out["isError"] is True and "'source_sketch'" in out["message"]

    def test_missing_source_sketch_lists_the_available_names(self, run):
        out, sk = run("to_surface", raw=True, source=None,
                      source_sketch="Ghost", curve_refs=["line:0"])
        assert out["isError"] is True and "No sketch named 'Ghost'" in out["message"]

    def test_unresolvable_curve_ref_is_refused(self, run):
        src = FakeSketch(name="Source", token="tok-src")
        out, sk = run("to_surface", raw=True, source=src, source_sketch="Source",
                      curve_refs=["arc:9"])
        assert out["isError"] is True and "arc:9" in out["message"]

    def test_no_curves_names_both_inputs(self, run):
        out, sk = run("to_surface", raw=True)
        assert out["isError"] is True
        assert "curve_refs" in out["message"] and "curve_handles" in out["message"]

    def test_zero_created_is_error_not_false_ok(self, run):
        out, sk = run("to_surface", raw=True, sketch=FakeSketch(surface_creates={}),
                      curve_handles=["h1"])
        assert out["isError"] is True
        assert "created no curves" in out["message"]

    def test_refs_offset_by_preexisting_entities(self, run):
        sk = FakeSketch(base={"line": 2}, surface_creates={"line": 3})
        out, _ = run("to_surface", sketch=sk, curve_handles=["h1"])
        assert out["entity_refs"] == ["line:2", "line:3", "line:4"]

    def test_off_face_curves_are_counted(self, run):
        sk = FakeSketch(surface_creates={"line": 2}, surface_is2d=False)
        out, _ = run("to_surface", sketch=sk, curve_handles=["h1"])
        assert out["off_plane_count"] == 2
        assert "All of them read as lying on the face" in out["note"]

    def test_all_on_plane_curves_are_flagged_in_the_note(self, run):
        sk = FakeSketch(surface_creates={"line": 2}, surface_is2d=True)
        out, _ = run("to_surface", sketch=sk, curve_handles=["h1"])
        assert out["off_plane_count"] == 0
        assert "All of them read as lying on the sketch x-y plane" in out["note"]

    def test_unreadable_is2d_is_not_counted_as_on_the_plane(self, run):
        # an is2D that will not read is evidence of neither state - it must not inflate either count
        sk = FakeSketch(surface_creates={"line": 2}, unreadable=("is2D",))
        out, _ = run("to_surface", sketch=sk, curve_handles=["h1"])
        assert out["off_plane_count"] == 0
        assert "is2D did not read on 2" in out["note"]
        assert "All of them read as" not in out["note"]

    def test_linkage_is_stated_from_the_isreference_read_back(self, run):
        sk = FakeSketch(surface_creates={"line": 2})
        out, _ = run("to_surface", sketch=sk, curve_handles=["h1"])
        assert "All 2 read back as REFERENCE curves" in out["note"]

    def test_curves_that_do_not_read_as_references_are_reported_as_such(self, run):
        sk = FakeSketch(surface_creates={"line": 2}, is_reference=False)
        out, _ = run("to_surface", sketch=sk, curve_handles=["h1"])
        assert "0 read back as REFERENCE curves" in out["note"] and "2 did not" in out["note"]

    def test_a_mixed_split_keeps_unreadable_apart_from_read_false(self, run):
        # True / False / unreadable across three curves - the failed read must not be folded into
        # the "did not" count, which would report a definite state nothing measured
        sk = FakeSketch(surface_creates={"line": 3}, ref_pattern=(True, False, None))
        out, _ = run("to_surface", sketch=sk, curve_handles=["h1"])
        assert ("Of 3 created curves, 1 read back as REFERENCE curves" in out["note"]
                and "1 did not" in out["note"]
                and "isReference did not read on 1" in out["note"])

    def test_an_empty_return_claims_nothing_about_where_the_curves_landed(self, run):
        # the collections grew but the call returned nothing to read: a universal from ZERO reads
        # would be a fabricated claim
        sk = FakeSketch(surface_creates={"line": 2}, silent_growth=True)
        out, _ = run("to_surface", sketch=sk, curve_handles=["h1"])
        assert out["created_count"] == 0
        assert out["entity_refs"] == ["line:0", "line:1"]
        assert "All of them read as" not in out["note"]
        assert "REFERENCE curves" not in out["note"]
        assert "isReference did not read" not in out["note"]
        assert "returned no entities" in out["note"]

    def test_a_projection_that_misses_surfaces_fusions_message(self, run):
        msg = "3 : Failed to project the selected geometries."
        out, sk = run("to_surface", raw=True, sketch=FakeSketch(surface_raises=msg),
                      curve_handles=["h1"])
        assert out["isError"] is True and msg in out["message"]

    def test_payload_reports_the_projection_shape(self, run):
        out, sk = run("to_surface", faces_out=["F1", "F2"], edges_out=["E1"],
                      curve_handles=["h1"])
        assert out["action"] == "to_surface"
        assert out["target_face_count"] == 2 and out["source_curve_count"] == 1
        assert out["project_type"] == "closest_point"


# ── intersect: intersectWithSketchPlane(entities) and its silent outcomes ─────

class TestIntersect:
    def test_bodies_and_entities_both_reach_the_call(self, run):
        b, f = BRepBody(name="Body1", entity_token="tok-b"), BRepBody(name="Face1", entity_token="tok-f")
        out, sk = run("intersect", bodies_out=[b], entities_out=[f],
                      bodies="Body1", entities="h1")
        assert sk.intersect_args == [f, b]

    def test_no_sources_is_refused(self, run):
        out, sk = run("intersect", raw=True)
        assert out["isError"] is True
        assert "'bodies'" in out["message"] and "'entities'" in out["message"]

    def test_a_same_component_source_reaches_the_call(self, run):
        sk = FakeSketch(contributions={"tok-1": 2})
        b1 = BRepBody(name="Body1", entity_token="tok-1", parent_component=sk.parentComponent)
        out, _ = run("intersect", sketch=sk, bodies_out=[b1], bodies="Body1")
        assert sk.intersect_args == [b1] and out["created_count"] == 2

    def test_the_same_component_read_twice_is_not_refused(self, run):
        # component wrappers are never identity-stable: a body's parentComponent and the sketch's
        # are DIFFERENT objects for one component, sharing one entityToken. An identity test here
        # would take the "different component" branch every time and refuse every legal call.
        sk = FakeSketch(contributions={"tok-1": 2})
        other_wrapper = MakeComp(name="Comp1-other-wrapper")
        other_wrapper.entityToken = sk.parentComponent.entityToken
        b1 = BRepBody(name="Body1", entity_token="tok-1", parent_component=other_wrapper)
        out, _ = run("intersect", sketch=sk, bodies_out=[b1], bodies="Body1")
        assert sk.intersect_args == [b1] and out["created_count"] == 2

    def test_an_occurrence_proxy_from_another_component_is_refused(self, run):
        # measured: a root-context sketch handed an occurrence proxy creates nothing and does NOT
        # raise, so the refusal has to happen here or the caller gets a wrong-cause plane error
        sk = FakeSketch(name="Sketch1")
        proxy = BRepBody(name="Body1", entity_token="tok-1",
                         parent_component=MakeComp(name="CompX", entity_token="TOKEN:CompX"))
        proxy.assemblyContext = types.SimpleNamespace(name="CompX:1")
        out, _ = run("intersect", raw=True, sketch=sk, bodies_out=[proxy], bodies="Body1")
        assert out["isError"] is True
        assert "lives in component 'CompX'" in out["message"]
        assert "owned by the sketch's own component" in out["message"]
        assert "sketch_create" in out["message"]
        assert sk.intersect_args is None

    def test_a_foreign_components_native_body_is_refused(self, run):
        # measured: this one RAISES InternalValidationError, which the refusal pre-empts
        sk = FakeSketch(name="Sketch1")
        native = BRepBody(name="Body2", entity_token="tok-2",
                          parent_component=MakeComp(name="CompY", entity_token="TOKEN:CompY"))
        out, _ = run("intersect", raw=True, sketch=sk, bodies_out=[native], bodies="Body2")
        assert out["isError"] is True
        assert "lives in component 'CompY'" in out["message"]
        assert "InternalValidationError" in out["message"]
        assert sk.intersect_args is None

    def test_a_face_is_judged_by_its_owning_bodys_component(self, run):
        sk = FakeSketch(name="Sketch1")
        face = types.SimpleNamespace(
            body=BRepBody(name="Body1", parent_component=MakeComp(name="CompX", entity_token="TOKEN:CompX")))
        out, _ = run("intersect", raw=True, sketch=sk, entities_out=[face], entities="h1")
        assert out["isError"] is True and "lives in component 'CompX'" in out["message"]

    def test_an_unreadable_owner_does_not_refuse(self, run):
        # an owning component that will not read is evidence of nothing - it must not block the call
        sk = FakeSketch(contributions={"tok-1": 1})
        b1 = BRepBody(name="Body1", entity_token="tok-1")   # parent_component defaults to None
        out, _ = run("intersect", sketch=sk, bodies_out=[b1], bodies="Body1")
        assert sk.intersect_args == [b1] and out["created_count"] == 1

    def test_an_owner_that_reads_but_cannot_be_TOLD_APART_does_not_refuse(self, run):
        # The owner reads, but same_component answers None (no token on it), so nothing established
        # that the two components DIFFER - and the refusal's whole sentence is that they do. It must
        # only fire on a proven difference, exactly as an unreadable owner does not fire it.
        sk = FakeSketch(contributions={"tok-1": 1})
        b1 = BRepBody(name="Body1", entity_token="tok-1",
                      parent_component=MakeComp(name="CompZ"))     # no entityToken
        out, _ = run("intersect", sketch=sk, bodies_out=[b1], bodies="Body1")
        assert sk.intersect_args == [b1] and out["created_count"] == 1

    def test_zero_created_names_every_entity_and_the_plane(self, run):
        b1, b2 = BRepBody(name="Body1", entity_token="tok-1"), BRepBody(name="Body2", entity_token="tok-2")
        sk = FakeSketch(name="Section", plane="XY", contributions={})
        out, _ = run("intersect", raw=True, sketch=sk, bodies_out=[b1, b2],
                     bodies="Body1,Body2")
        assert out["isError"] is True
        assert "Body1" in out["message"] and "Body2" in out["message"]
        assert "Section" in out["message"] and "XY" in out["message"]

    def test_partial_result_is_ok_and_names_the_zero_contributors(self, run):
        b1, b2 = BRepBody(name="Body1", entity_token="tok-1"), BRepBody(name="Body2", entity_token="tok-2")
        sk = FakeSketch(contributions={"tok-1": 4, "tok-2": 0})
        out, _ = run("intersect", sketch=sk, bodies_out=[b1, b2], bodies="Body1,Body2")
        assert out["created_count"] == 4
        assert out["per_source"] == [{"source": "Body1", "created": 4},
                                     {"source": "Body2", "created": 0}]
        assert "Body2" in out["note"] and "contributed nothing" in out["note"]

    def test_full_contribution_leaves_the_note_free_of_a_zero_claim(self, run):
        b1, b2 = BRepBody(name="Body1", entity_token="tok-1"), BRepBody(name="Body2", entity_token="tok-2")
        sk = FakeSketch(contributions={"tok-1": 2, "tok-2": 3})
        out, _ = run("intersect", sketch=sk, bodies_out=[b1, b2], bodies="Body1,Body2")
        assert out["created_count"] == 5
        assert "contributed nothing" not in out["note"]

    def test_attribution_is_suppressed_when_a_curve_cannot_be_matched_back(self, run):
        # a non-parametric reference reads referencedEntity as null; unmatched, that would otherwise
        # read as "this source made nothing"
        b1 = BRepBody(name="Body1", entity_token="tok-1")
        sk = FakeSketch(contributions=None, unreferenced=True)
        out, _ = run("intersect", sketch=sk, bodies_out=[b1], bodies="Body1")
        assert out["created_count"] == 1
        assert "per_source" not in out
        assert "contributed nothing" not in out["note"]

    def test_refs_are_tail_indexed(self, run):
        b1 = BRepBody(name="Body1", entity_token="tok-1")
        sk = FakeSketch(base={"line": 5}, contributions={"tok-1": 4})
        out, _ = run("intersect", sketch=sk, bodies_out=[b1], bodies="Body1")
        assert out["entity_refs"] == ["line:5", "line:6", "line:7", "line:8"]

    def test_intersect_raise_is_surfaced(self, run):
        b1 = BRepBody(name="Body1", entity_token="tok-1")
        out, sk = run("intersect", raw=True, sketch=FakeSketch(intersect_raises="boom"),
                      bodies_out=[b1], bodies="Body1")
        assert out["isError"] is True and "boom" in out["message"]

    def test_linkage_is_stated_from_the_isreference_read_back(self, run):
        b1 = BRepBody(name="Body1", entity_token="tok-1")
        sk = FakeSketch(contributions={"tok-1": 3})
        out, _ = run("intersect", sketch=sk, bodies_out=[b1], bodies="Body1")
        assert "All 3 read back as REFERENCE curves" in out["note"]

    def test_unreadable_isreference_falls_back_to_the_method_fact(self, run):
        b1 = BRepBody(name="Body1", entity_token="tok-1")
        sk = FakeSketch(contributions={"tok-1": 2}, unreadable=("isReference",))
        out, _ = run("intersect", sketch=sk, bodies_out=[b1], bodies="Body1")
        assert "isReference did not read" in out["note"]
        assert "read back as REFERENCE curves" not in out["note"]

    def test_an_empty_return_claims_no_linkage(self, run):
        # the census grew while the call returned nothing to read - with zero reads there is no
        # linkage to claim either way
        b1 = BRepBody(name="Body1", entity_token="tok-1")
        sk = FakeSketch(contributions={"tok-1": 3}, silent_growth=True)
        out, _ = run("intersect", sketch=sk, bodies_out=[b1], bodies="Body1")
        assert out["created_count"] == 0
        assert out["entity_refs"] == ["line:0", "line:1", "line:2"]
        assert "REFERENCE curves" not in out["note"]
        assert "isReference did not read" not in out["note"]
        assert "They lie on the sketch plane" not in out["note"]
        assert "returned no entities" in out["note"]


# ── the named-sketch miss ─────────────────────────────────────────────────────

@pytest.fixture
def real_walk():
    """A design holding one sketch named 'Plate', with the sketch walk left UNSTUBBED - so the
    miss path runs through _common.find_or_recent_sketch, which strips the name before searching."""
    return install(sp, make_design(comp=MakeComp(name="Root", sketches=[FakeSketch("Plate")])))


class TestNamedSketchMiss:
    def test_reports_the_name_the_walk_searched_for_not_the_raw_input(self, real_walk):
        # the resolver strips before searching, so echoing the raw input quotes a name nothing
        # ever looked for - and the caller retries against a sketch that was never missing.
        res = sp.handler(sketch_name="  Ghost  ", entities="F1")
        assert res["isError"] is True
        assert "No sketch named 'Ghost'" in res["message"]
        assert "'  Ghost  '" not in res["message"]

    def test_the_miss_still_lists_what_is_there(self, real_walk):
        res = sp.handler(sketch_name="Ghost", entities="F1")
        assert "Available: Plate" in res["message"]


# ── RETURNS contract ──────────────────────────────────────────────────────────

class TestReturnsContract:
    def test_declared_entity_refs_present_in_payload(self, call):
        out, sk = call()
        assert sp.RETURNS[0].assert_present(out) == ""


# ── the 'component' / 'source_component' SCOPES ──────────────────────────────
# Fusion numbers sketches per component from 1, so two components each holding a "Plate" is the
# norm, and the design-wide walk REFUSES that name, pointing at the scope: a rename is impossible
# for a component that arrived inside a referenced document. Two sketch names here
# ('sketch_name' projected INTO, 'source_sketch' read FROM) so two scopes, each naming itself in
# its own refusal. These drive the REAL _common walk, no stubbed resolver.

@pytest.fixture
def scoped(monkeypatch):
    """Two components each holding a 'Plate', with DIFFERENT starting curve counts (Alpha 4 lines,
    Beta none), so which sketch a projection landed in is readable from the counts."""
    def _build():
        alpha_sk = FakeSketch(name="Plate", base={"line": 4}, token="tok-alpha")
        beta_sk = FakeSketch(name="Plate", token="tok-beta")
        alpha = MakeComp(name="Alpha", sketches=[alpha_sk])
        beta = MakeComp(name="Beta", sketches=[beta_sk])
        install(sp, make_design(comp=alpha, all_components=[alpha, beta]))
        _stub(monkeypatch, sp._ENTITIES, [object(), object()])
        return alpha_sk, beta_sk
    return _build


class TestProjectComponentScope:
    def test_the_unscoped_shared_name_refuses_and_names_the_scope_input(self, scoped):
        alpha_sk, beta_sk = scoped()
        res = sp.handler(sketch_name="Plate", entities="F1")
        assert res["isError"] is True
        assert "2 sketches are named 'Plate'" in res["message"]
        assert "'component'" in res["message"] and "Rename one" not in res["message"]
        assert alpha_sk.projected_with is None and beta_sk.projected_with is None

    def test_the_scope_projects_into_THAT_components_sketch(self, scoped):
        alpha_sk, beta_sk = scoped()
        _payload(sp.handler(sketch_name="Plate", component="Beta", entities="F1"))
        assert beta_sk.projected_with is not None and alpha_sk.projected_with is None

    def test_the_sibling_component_is_reachable_by_the_same_call(self, scoped):
        alpha_sk, beta_sk = scoped()
        _payload(sp.handler(sketch_name="Plate", component="Alpha", entities="F1"))
        assert alpha_sk.projected_with is not None and beta_sk.projected_with is None

    def test_an_unknown_component_is_refused_before_the_projection(self, scoped):
        alpha_sk, beta_sk = scoped()
        res = sp.handler(sketch_name="Plate", component="Gamma", entities="F1")
        assert res["isError"] is True and "No component named 'Gamma'" in res["message"]
        assert alpha_sk.projected_with is None and beta_sk.projected_with is None

    def test_a_wrong_component_is_refused_even_when_the_name_is_UNIQUE(self, monkeypatch):
        # The scope is VALIDATED: a dropped one projects into Alpha on a call that named Beta.
        alpha_sk = FakeSketch(name="OnlyOne", token="tok-alpha")
        alpha = MakeComp(name="Alpha", sketches=[alpha_sk])
        beta = MakeComp(name="Beta", sketches=[])
        install(sp, make_design(comp=alpha, all_components=[alpha, beta]))
        _stub(monkeypatch, sp._ENTITIES, [object()])
        res = sp.handler(sketch_name="OnlyOne", component="Beta", entities="F1")
        assert res["isError"] is True and "'Beta'" in res["message"]
        assert alpha_sk.projected_with is None


class TestSourceComponentScope:
    """'source_component' narrows the sketch 'curve_refs' are read AGAINST. It is separate from
    'component' because that one narrows the receiving sketch and would not change this answer."""

    def _two_sources(self, monkeypatch):
        target = FakeSketch(name="Target", token="tok-tgt")
        a_src = FakeSketch(name="Plate", token="tok-a")
        b_src = FakeSketch(name="Plate", token="tok-b")
        alpha = MakeComp(name="Alpha", sketches=[target, a_src])
        beta = MakeComp(name="Beta", sketches=[b_src])
        install(sp, make_design(comp=alpha, all_components=[alpha, beta]))
        _stub(monkeypatch, sp._TARGET_FACES, ["FACE"])
        _stub(monkeypatch, sp._CURVE_HANDLES, [])
        return target, a_src, b_src

    def test_a_shared_source_name_refuses_naming_source_component(self, monkeypatch):
        target, a_src, b_src = self._two_sources(monkeypatch)
        res = sp.handler(action="to_surface", sketch_name="Target", component="Alpha",
                         source_sketch="Plate", curve_refs=["line:0"], target_faces="F")
        assert res["isError"] is True
        assert "2 sketches are named 'Plate'" in res["message"]
        assert "'source_component'" in res["message"] and "Rename one" not in res["message"]
        assert target.surface_args is None

    def test_source_component_selects_which_sketch_the_refs_are_read_against(self, monkeypatch):
        target, a_src, b_src = self._two_sources(monkeypatch)
        seen = []
        monkeypatch.setattr(sp._common, "resolve_entity_ref",
                            lambda s, r: (seen.append(s), object())[1])
        _payload(sp.handler(action="to_surface", sketch_name="Target", component="Alpha",
                            source_sketch="Plate", source_component="Beta",
                            curve_refs=["line:0"], target_faces="F"))
        assert seen == [b_src]          # Beta's 'Plate', not Alpha's

    def test_the_sibling_source_is_reachable_by_the_same_call(self, monkeypatch):
        target, a_src, b_src = self._two_sources(monkeypatch)
        seen = []
        monkeypatch.setattr(sp._common, "resolve_entity_ref",
                            lambda s, r: (seen.append(s), object())[1])
        _payload(sp.handler(action="to_surface", sketch_name="Target", component="Alpha",
                            source_sketch="Plate", source_component="Alpha",
                            curve_refs=["line:0"], target_faces="F"))
        assert seen == [a_src]

    def test_a_scoped_MISS_names_source_component_and_never_the_bare_component(self, monkeypatch):
        # The scoped-miss refusal has to name the input that narrows THIS reference. 'component'
        # narrows the sketch being projected INTO, so quoting it points at an input that cannot
        # change which source was found.
        target = FakeSketch(name="Target", token="tok-tgt")
        a_src = FakeSketch(name="Plate", token="tok-a")
        alpha = MakeComp(name="Alpha", sketches=[target, a_src])
        beta = MakeComp(name="Beta", sketches=[FakeSketch(name="Other", token="tok-o")])
        install(sp, make_design(comp=alpha, all_components=[alpha, beta]))
        _stub(monkeypatch, sp._TARGET_FACES, ["FACE"])
        _stub(monkeypatch, sp._CURVE_HANDLES, [])
        res = sp.handler(action="to_surface", sketch_name="Target", component="Alpha",
                         source_sketch="Plate", source_component="Beta",
                         curve_refs=["line:0"], target_faces="F")
        assert res["isError"] is True
        assert "holds no sketch named 'Plate'" in res["message"]
        assert "'source_component'" in res["message"]
        assert "'component'" not in res["message"]
        assert target.surface_args is None

    def test_an_AMBIGUOUS_source_component_names_source_component(self, monkeypatch):
        from conftest import make_occurrence
        target = FakeSketch(name="Target", token="tok-tgt")
        root = MakeComp(name="Root", sketches=[target])
        a = MakeComp(name="Frame", sketches=[FakeSketch(name="Plate", token="tok-a")])
        b = MakeComp(name="Frame", sketches=[FakeSketch(name="Plate", token="tok-b")])
        root.allOccurrences = [make_occurrence("P2-Gimbal:1+Frame:1", a),
                               make_occurrence("P3-Gimbal:1+Frame:1", b)]
        install(sp, make_design(comp=root, all_components=[root, a, b]))
        _stub(monkeypatch, sp._TARGET_FACES, ["FACE"])
        _stub(monkeypatch, sp._CURVE_HANDLES, [])
        res = sp.handler(action="to_surface", sketch_name="Target", source_sketch="Plate",
                         source_component="Frame", curve_refs=["line:0"], target_faces="F")
        assert res["isError"] is True
        assert "2 components match 'Frame'" in res["message"]
        assert "'source_component' also takes an occurrence fullPathName" in res["message"]
        assert "'component'" not in res["message"]
        assert target.surface_args is None

    def test_a_wrong_source_component_is_refused_even_when_the_name_is_UNIQUE(self, monkeypatch):
        # The validation decision at the ALTERNATE scope: dropped, it reads the refs against
        # Alpha's 'OnlyOne' on a call that named Beta.
        target = FakeSketch(name="Target", token="tok-tgt")
        a_src = FakeSketch(name="OnlyOne", token="tok-a")
        alpha = MakeComp(name="Alpha", sketches=[target, a_src])
        beta = MakeComp(name="Beta", sketches=[])
        install(sp, make_design(comp=alpha, all_components=[alpha, beta]))
        _stub(monkeypatch, sp._TARGET_FACES, ["FACE"])
        _stub(monkeypatch, sp._CURVE_HANDLES, [])
        res = sp.handler(action="to_surface", sketch_name="Target", source_sketch="OnlyOne",
                         source_component="Beta", curve_refs=["line:0"], target_faces="F")
        assert res["isError"] is True and "'Beta'" in res["message"]
        assert target.surface_args is None

    def test_source_component_is_refused_on_the_actions_that_have_no_source(self, monkeypatch):
        # It rides the same action gate as 'source_sketch' - an input silently dropped by the
        # action router is the same trap as a scope silently ignored.
        self._two_sources(monkeypatch)
        res = sp.handler(action="into_sketch", sketch_name="Target", source_component="Beta",
                         entities="F1")
        assert res["isError"] is True and "'source_component' applies only to" in res["message"]
