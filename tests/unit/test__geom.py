"""Unit tests for ``_geom.py`` - the direction-vector math find_geometry and sys_get_selection
share: normalizing a Vector3D (``unit_vector``), the unit direction between two points
(``unit_vector_between``), and a face's evaluator-sampled normal (``evaluator_normal_at``); plus the
owning-body walk and the before/after volume and face-count samples every effect check reads through
(``owning_bodies`` / ``volumes`` / ``volume_delta`` / ``face_counts`` / ``face_count_delta``); plus
``parallel_plane_facts``, the bounded gap two parallel planar faces are judged by.
"""

import math
import types

from conftest import (BRepBody, BRepFace, Cylinder, FakeBoundingBox3D, FakePoint, FakeVector3D,
                      MakeComp, Plane, _NamedCollection, body_proxy, entity_proxy, load_tool,
                      make_source_document)

geom = load_tool("_geom")


# ── unit_vector: normalization + zero-vector guard ─────────────────────────

class TestUnitVector:
    def test_normalizes_to_length_one(self):
        assert geom.unit_vector(FakeVector3D(0, 0, 5)) == [0.0, 0.0, 1.0]

    def test_arbitrary_vector_normalized(self):
        u = geom.unit_vector(FakeVector3D(3, 4, 0))  # length 5
        assert u == [0.6, 0.8, 0.0]
        assert math.isclose(math.sqrt(sum(c * c for c in u)), 1.0, abs_tol=1e-9)

    def test_zero_vector_returns_none(self):
        # A zero-length vector has no direction — must be None, not [0,0,0].
        assert geom.unit_vector(FakeVector3D(0, 0, 0)) is None

    def test_none_input_returns_none(self):
        assert geom.unit_vector(None) is None

    def test_decimals_controls_rounding(self):
        v = FakeVector3D(1, 2, 2)  # length 3 -> (1/3, 2/3, 2/3)
        assert geom.unit_vector(v, decimals=2) == [0.33, 0.67, 0.67]
        assert geom.unit_vector(v, decimals=4) == [0.3333, 0.6667, 0.6667]

    def test_already_unit_vector_is_unchanged(self):
        # Renormalizing an already-unit vector (e.g. an evaluator normal) must be a no-op.
        assert geom.unit_vector(FakeVector3D(0, 1, 0)) == [0.0, 1.0, 0.0]


# ── unit_vector_between: point-to-point direction ───────────────────────────

class TestUnitVectorBetween:
    def test_axis_aligned_direction(self):
        d = geom.unit_vector_between(FakePoint(0, 0, 0), FakePoint(3, 0, 0))
        assert d == [1.0, 0.0, 0.0]

    def test_diagonal_direction_is_normalized(self):
        # a 3-4-0 delta -> unit direction [0.6, 0.8, 0]
        d = geom.unit_vector_between(FakePoint(0, 0, 0), FakePoint(3, 4, 0))
        assert d == [0.6, 0.8, 0.0]

    def test_direction_is_independent_of_translation(self):
        # only the DELTA matters, not the absolute positions.
        d = geom.unit_vector_between(FakePoint(10, 10, 10), FakePoint(13, 10, 10))
        assert d == [1.0, 0.0, 0.0]

    def test_same_point_returns_none(self):
        # zero-length delta has no direction.
        assert geom.unit_vector_between(FakePoint(1, 1, 1), FakePoint(1, 1, 1)) is None

    def test_none_endpoints_return_none(self):
        assert geom.unit_vector_between(None, FakePoint(1, 0, 0)) is None
        assert geom.unit_vector_between(FakePoint(0, 0, 0), None) is None

    def test_decimals_controls_rounding(self):
        d = geom.unit_vector_between(FakePoint(0, 0, 0), FakePoint(1, 2, 2), decimals=2)
        assert d == [0.33, 0.67, 0.67]


# ── evaluator_normal_at: the getNormalAtPoint sample ────────────────────────

class _FakeEvaluator:
    """Stands in for a BRepFace SurfaceEvaluator. getNormalAtPoint returns (success, normal) - the
    Python shape of a bool-return + output-normal API."""
    def __init__(self, normal=None, ok=True, raises=False):
        self._normal = normal
        self._ok = ok
        self._raises = raises

    def getNormalAtPoint(self, point):
        if self._raises:
            raise RuntimeError("point is off the face surface")
        return (self._ok, FakeVector3D(*self._normal) if self._normal else None)


class _FakeFace:
    def __init__(self, evaluator=None):
        self.evaluator = evaluator


class TestEvaluatorNormalAt:
    def test_returns_the_sampled_normal(self):
        face = _FakeFace(_FakeEvaluator(normal=(0, 0, 1)))
        assert geom.evaluator_normal_at(face, FakePoint(0, 0, 0)) == [0.0, 0.0, 1.0]

    def test_none_point_returns_none(self):
        face = _FakeFace(_FakeEvaluator(normal=(0, 0, 1)))
        assert geom.evaluator_normal_at(face, None) is None

    def test_missing_evaluator_returns_none(self):
        face = _FakeFace(evaluator=None)
        assert geom.evaluator_normal_at(face, FakePoint(0, 0, 0)) is None

    def test_evaluator_raising_returns_none(self):
        # off-surface sample point -> getNormalAtPoint raises -> degrades to None, never a crash.
        face = _FakeFace(_FakeEvaluator(raises=True))
        assert geom.evaluator_normal_at(face, FakePoint(0, 0, 0)) is None

    def test_failed_okflag_returns_none(self):
        face = _FakeFace(_FakeEvaluator(normal=(1, 0, 0), ok=False))
        assert geom.evaluator_normal_at(face, FakePoint(0, 0, 0)) is None

    def test_decimals_controls_rounding(self):
        face = _FakeFace(_FakeEvaluator(normal=(1, 2, 2)))
        assert geom.evaluator_normal_at(face, FakePoint(0, 0, 0), decimals=2) == [0.33, 0.67, 0.67]


# ── body_aabb: the bodies-only AABB every occurrence/component size read shares ─────────────────
#
# An Occurrence/Component's plain .boundingBox also counts visible sketches + construction datums,
# so an orphaned oversized sketch inflates the box (live-verified: a 68x10 body read 120x120).
# body_aabb must call boundingBox2 with the SOLID|SURFACE|MESH body types instead; a BRepBody has no
# boundingBox2 and its own .boundingBox is already body-only.

class TestBodyAabb:
    def test_occurrence_uses_boundingBox2_with_body_types(self):
        class _Ent:
            boundingBox = "whole_box"          # sketch/datum-inflated - must NOT be used
            def __init__(self):
                self.calls = []
            def boundingBox2(self, types):
                self.calls.append(types)
                return "body_box"
        e = _Ent()
        assert geom.body_aabb(e) == "body_box"
        assert e.calls == [geom._BODY_BBOX_TYPES]

    def test_body_types_exclude_sketch_and_construction(self):
        # the bitmask spans solid|surface|mesh only (1|2|4=7) - sketch(8)/construction bits are out.
        import adsk.fusion
        t = adsk.fusion.BoundingBoxEntityTypes
        assert geom._BODY_BBOX_TYPES == (t.SolidBRepBodyBoundingBoxEntityType
                                         | t.SurfaceBodyBoundingBoxEntityType
                                         | t.MeshBodyBoundingBoxEntityType)
        assert not (geom._BODY_BBOX_TYPES & t.SketchBoundingBoxEntityType)

    def test_body_falls_back_to_plain_boundingBox(self):
        class _Body:
            boundingBox = "solid_box"          # no boundingBox2 attribute -> fallback
        assert geom.body_aabb(_Body()) == "solid_box"

    def test_no_body_geometry_returns_none(self):
        class _Empty:
            def boundingBox2(self, types):
                return None                    # no measurable bodies
        assert geom.body_aabb(_Empty()) is None


# ── owning_bodies: deduped by entityToken, NEVER by identity ────────────────

def _entities_on_one_body(count, token="Body1", face_count=6, kind="face"):
    """`count` faces (or edge-shaped stubs) of ONE body, each holding its OWN proxy - the measured
    shape of face.body / edge.body (see conftest entity_proxy)."""
    body = BRepBody(name=token, entity_token=token, face_count=face_count)
    if kind == "face":
        return body, [BRepFace(None, body=entity_proxy(body)) for _ in range(count)]
    edges = []
    for _ in range(count):
        e = type("E", (), {})()
        e.body = entity_proxy(body)
        edges.append(e)
    return body, edges


class TestOwningBodies:
    def test_many_faces_of_one_body_yield_exactly_one_body(self):
        # THE load-bearing pin for model_split and surface_delete_face: face.body hands back a fresh
        # proxy per read, so three faces of one body are three distinct Python objects sharing one
        # entityToken. Keying on identity yields 3 and inflates every per-body sum built on it.
        _body, faces = _entities_on_one_body(3)
        assert len(geom.owning_bodies(faces)) == 1

    def test_many_edges_of_one_body_yield_exactly_one_body(self):
        # edge.body behaves the same way - this is what EdgeLoopRef's body_count counts through.
        _body, edges = _entities_on_one_body(3, kind="edge")
        assert len(geom.owning_bodies(edges)) == 1

    def test_distinct_bodies_are_kept_apart(self):
        _b1, f1 = _entities_on_one_body(2, token="BodyA")
        _b2, f2 = _entities_on_one_body(2, token="BodyB")
        assert len(geom.owning_bodies(f1 + f2)) == 2

    def test_first_seen_order_is_preserved(self):
        _b1, f1 = _entities_on_one_body(1, token="BodyA")
        _b2, f2 = _entities_on_one_body(1, token="BodyB")
        tokens = [b.entityToken for b in geom.owning_bodies(f2 + f1)]
        assert tokens == ["BodyB", "BodyA"]

    def test_entity_with_no_readable_body_is_skipped(self):
        assert geom.owning_bodies([type("F", (), {})()]) == []

    def test_untokened_bodies_fall_back_to_identity(self):
        # The `or id(b)` last resort: without a token there is nothing else to key on, so two
        # separate proxies of one body read as two. Pinned so the fallback's LIMIT is visible - a
        # live entityToken read is measured non-empty, so this is not the normal path.
        body = BRepBody(name="NoToken", face_count=4)
        body.entityToken = None
        faces = [BRepFace(None, body=entity_proxy(body)) for _ in range(2)]
        assert len(geom.owning_bodies(faces)) == 2


# The two x-ref'd documents whose bodies answer ONE document-local entityToken (measured; see
# _common.native_identity), and the lineage ids that separate them.
_URN_A = "urn:adsk.wipprod:dm.lineage:K3I2nkywRlaWPHJexysOdA"
_URN_B = "urn:adsk.wipprod:dm.lineage:N_QoPrrrSJmF__f9BZV86A"
_XREF_TOKEN = "/vB+AAEAAwAAAAAAAAAAAAAA"


def _faces_of_a_body_in(urn, token=_XREF_TOKEN, count=1):
    """(body, faces) for ONE body living in the document whose lineage id is `urn`. Each face holds
    its OWN proxy, the measured shape of face.body."""
    body = BRepBody(name="Frame", entity_token=token, face_count=6,
                    parent_component=MakeComp(name="Frame",
                                              parent_design=make_source_document(urn)))
    return body, [BRepFace(None, body=entity_proxy(body)) for _ in range(count)]


class TestOwningBodiesKeysOnThePhysicalBody:
    def test_two_bodies_in_TWO_documents_sharing_one_token_stay_TWO_bodies(self):
        # An entityToken is DOCUMENT-LOCAL (measured: two x-refs of one design read byte-identical
        # tokens), and find_geometry hands out face handles inside x-ref'd components - so a fillet or
        # a split can legally be given faces of two such bodies. Keyed on the wrapper's token the two
        # merge into ONE entry, and the volume/face-count delta every material-removal feature
        # verifies with is then summed over a sample missing the body that dropped out.
        a, fa = _faces_of_a_body_in(_URN_A)
        b, fb = _faces_of_a_body_in(_URN_B)
        assert fa[0].body.entityToken == fb[0].body.entityToken   # the collision is real
        assert a is not b
        assert len(geom.owning_bodies(fa + fb)) == 2

    def test_one_body_reached_natively_AND_through_a_proxy_is_ONE_body(self):
        # The other direction: a proxy's own entityToken DIFFERS from its native's (measured), so
        # keyed on the wrapper's token one physical body selected through two contexts counts twice -
        # and volumes()/volume_delta() then sum that body's change twice over.
        native = BRepBody(name="Frame", entity_token="TOK-NATIVE", face_count=6)
        proxy = body_proxy(native, types.SimpleNamespace(name="Frame:1", fullPathName="Frame:1"))
        faces = [BRepFace(None, body=entity_proxy(native)),
                 BRepFace(None, body=entity_proxy(proxy))]
        assert faces[0].body.entityToken != faces[1].body.entityToken   # a real proxy, not a copy
        assert len(geom.owning_bodies(faces)) == 1


class TestVolumeAndFaceSamples:
    def test_face_count_delta_sums_each_body_once(self):
        body, faces = _entities_on_one_body(3, face_count=6)
        bodies = geom.owning_bodies(faces)
        before = geom.face_counts(bodies)
        body.faces._items.extend([None] * 3)          # the split adds 3 faces to the ONE body
        delta, readable = geom.face_count_delta(bodies, before)
        assert readable is True and delta == 3        # not 9

    def test_face_count_delta_reports_unreadable_rather_than_zero(self):
        body, faces = _entities_on_one_body(1)
        bodies = geom.owning_bodies(faces)
        before = geom.face_counts(bodies)
        del body.faces
        delta, readable = geom.face_count_delta(bodies, before)
        assert readable is False and delta == 0       # a real zero is distinguishable from this

    def test_volume_delta_sums_each_body_once(self):
        body, faces = _entities_on_one_body(2)
        body.volume = 100.0
        bodies = geom.owning_bodies(faces)
        before = geom.volumes(bodies)
        body.volume = 118.0
        delta, readable = geom.volume_delta(bodies, before)
        assert readable is True and delta == 18.0     # not 36.0


class TestSignedVolume:
    """The single-read counterpart to volumes(): mesh_shell/mesh_repair judge a hollowing on this
    number's SIGN, so anything that is not a real number must read as None (unknown), never 0.0."""

    def _body(self, volume):
        b = BRepBody(name="Mesh1", entity_token="TOK")
        b.volume = volume
        return b

    def test_reads_a_positive_volume_through(self):
        assert geom.signed_volume(self._body(12.5)) == 12.5

    def test_a_negative_volume_keeps_its_sign(self):
        # A mesh whose normals were reversed reports the same magnitude with the opposite sign -
        # dropping the sign (abs) is exactly what would make a reversed mesh read as solid.
        assert geom.signed_volume(self._body(-12.5)) == -12.5

    def test_a_non_numeric_volume_is_unknown_not_zero(self):
        # adsk mocks (and an unmodeled live property) hand back a truthy object for anything
        # unmodeled. Letting one through would make `volume < 0` a TypeError, or worse, compare
        # a Mock as if it were a measurement.
        from unittest.mock import Mock
        assert geom.signed_volume(self._body(Mock())) is None

    def test_a_boolean_volume_is_unknown(self):
        # bool is an int subclass, so a plain isinstance(v, (int, float)) would accept True as 1.0.
        assert geom.signed_volume(self._body(True)) is None

    def test_an_unreadable_volume_is_none_not_zero(self):
        class _Body:
            @property
            def volume(self):
                raise RuntimeError("volume unreadable")
        assert geom.signed_volume(_Body()) is None

    def test_a_genuine_zero_is_still_a_reading(self):
        # 0.0 is an ANSWER (an empty/degenerate body), distinguishable from the None above.
        assert geom.signed_volume(self._body(0.0)) == 0.0


# ── lump_count: the DISCONNECTED-piece read a join is verified with ──────────────────────────────

class _Lumps:
    def __init__(self, count):
        self.count = count


class TestLumpCount:
    def test_reads_the_bodys_lump_count(self):
        body = BRepBody(name="Weldment")
        body.lumps = _Lumps(3)
        assert geom.lump_count(body) == 3

    def test_a_fused_single_piece_body_reads_one(self):
        # 1 is the ANSWER a real fuse gives - it must not collapse to None/0, or the join warning
        # could never tell a fused result from an unreadable one.
        body = BRepBody(name="Bracket")
        body.lumps = _Lumps(1)
        assert geom.lump_count(body) == 1

    def test_a_body_without_lumps_is_unknown_not_zero(self):
        # A MeshBody carries no 'lumps' at all (its API surface has no counterpart to BRepBody.lumps),
        # so the read is UNKNOWN. Answering 0 or 1 here would let a mesh join claim it verified
        # something it cannot see.
        assert geom.lump_count(BRepBody(name="Scan")) is None

    def test_an_unreadable_count_is_none(self):
        class _Body:
            @property
            def lumps(self):
                raise RuntimeError("lumps unreadable")
        assert geom.lump_count(_Body()) is None

    def test_a_non_numeric_count_is_none(self):
        # adsk mocks hand back a truthy child object for anything unmodeled; letting one through
        # would make `result_lumps > 1` a TypeError at the warning site.
        from unittest.mock import Mock
        body = BRepBody(name="Mocked")
        body.lumps = Mock()
        assert geom.lump_count(body) is None

    def test_only_the_brep_body_carries_lumps_in_the_api_surface(self):
        # The split this helper's contract rests on, and the reason mesh_combine reaches for AABBs
        # instead: BRepBody exposes 'lumps', MeshBody exposes no counterpart. If a Fusion build ever
        # gives MeshBody a lump/shell count, this goes red and the mesh gap can close.
        import api_surface
        assert "lumps" in api_surface.PROPERTIES["fusion.BRepBody"]
        mesh_members = api_surface.PROPERTIES["fusion.MeshBody"]
        assert "lumps" not in mesh_members and "shells" not in mesh_members


# ── aabb_gap: the not-touching proof for a body kind carrying no lump count ──────────────────────

def _boxed(name, minp, maxp):
    return BRepBody(name=name, bbox=FakeBoundingBox3D(FakePoint(*minp), FakePoint(*maxp)))


class TestAabbGap:
    def test_boxes_apart_report_the_gap(self):
        # 4.4 cm of clear air on x: the two bodies CANNOT touch, whatever else is true of them.
        a = _boxed("Tensioner", (0, 0, 0), (1, 1, 1))
        b = _boxed("Stub", (5.4, 0, 0), (6.4, 1, 1))
        assert round(geom.aabb_gap(a, b), 6) == 4.4

    def test_the_gap_is_symmetric(self):
        a = _boxed("A", (0, 0, 0), (1, 1, 1))
        b = _boxed("B", (5.4, 0, 0), (6.4, 1, 1))
        assert geom.aabb_gap(a, b) == geom.aabb_gap(b, a)

    def test_a_separating_axis_wins_over_overlapping_ones(self):
        # Fully overlapping in x and z, 2 cm apart in y - one separating axis is enough to prove the
        # bodies are clear of each other, so the MAX (not the min) over the axes is the answer.
        a = _boxed("A", (0, 0, 0), (10, 1, 10))
        b = _boxed("B", (0, 3, 0), (10, 4, 10))
        assert geom.aabb_gap(a, b) == 2

    def test_overlapping_boxes_report_no_gap(self):
        # Overlap proves nothing about contact, so the value must be <= 0 and never trigger a
        # not-touching claim.
        a = _boxed("A", (0, 0, 0), (2, 2, 2))
        b = _boxed("B", (1, 1, 1), (3, 3, 3))
        assert geom.aabb_gap(a, b) == -1

    def test_touching_boxes_report_zero(self):
        # Face-to-face contact: gap 0, which is NOT a positive gap - a real fuse must not be warned on.
        a = _boxed("A", (0, 0, 0), (1, 1, 1))
        b = _boxed("B", (1, 0, 0), (2, 1, 1))
        assert geom.aabb_gap(a, b) == 0

    def test_an_unreadable_box_is_none(self):
        a = _boxed("A", (0, 0, 0), (1, 1, 1))
        assert geom.aabb_gap(a, BRepBody(name="NoBox")) is None
        assert geom.aabb_gap(BRepBody(name="NoBox"), a) is None

    def test_a_non_numeric_coordinate_is_none(self):
        from unittest.mock import Mock
        a = _boxed("A", (0, 0, 0), (1, 1, 1))
        b = BRepBody(name="B", bbox=FakeBoundingBox3D(FakePoint(Mock(), 0, 0), FakePoint(1, 1, 1)))
        assert geom.aabb_gap(a, b) is None


class TestAabbGapSameSpacePrecondition:
    """The boxes only subtract if they are expressed in ONE space. An occurrence PROXY's box is in
    ROOT space while its native's is component-LOCAL, so a cross-wrapper subtraction mints a
    confident number out of two different frames - under the word PROVES, that is a fabrication."""

    def _in_comp(self, name, comp, minp, maxp):
        return BRepBody(name=name, parent_component=comp,
                        bbox=FakeBoundingBox3D(FakePoint(*minp), FakePoint(*maxp)))

    def test_a_native_and_an_occurrence_proxy_do_not_compare(self):
        import types
        from conftest import body_proxy
        comp = types.SimpleNamespace(name="Frame", entityToken="CTOK::Frame")
        native = self._in_comp("Native", comp, (0, 0, 0), (1, 1, 1))
        proxy = body_proxy(native, types.SimpleNamespace(name="Frame:1", fullPathName="Frame:1"))
        other = self._in_comp("Other", comp, (5, 0, 0), (6, 1, 1))
        # the proxy really is the hard case: it answers an assemblyContext, the native does not
        assert proxy.assemblyContext is not None and native.assemblyContext is None
        assert geom.aabb_gap(proxy, other) is None

    def test_two_bodies_of_the_same_component_compare(self):
        import types
        comp = types.SimpleNamespace(name="Frame", entityToken="CTOK::Frame")
        a = self._in_comp("A", comp, (0, 0, 0), (1, 1, 1))
        b = self._in_comp("B", comp, (5, 0, 0), (6, 1, 1))
        assert geom.aabb_gap(a, b) == 4

    def test_two_wrappers_of_one_component_still_compare(self):
        # Component wrappers are never identity-stable: two reads of one component are different
        # objects sharing a token. An identity test here would refuse every legitimate pair.
        import types
        comp_a = types.SimpleNamespace(name="Frame", entityToken="CTOK::Frame")
        comp_b = types.SimpleNamespace(name="Frame", entityToken="CTOK::Frame")
        assert comp_a is not comp_b
        a = self._in_comp("A", comp_a, (0, 0, 0), (1, 1, 1))
        b = self._in_comp("B", comp_b, (5, 0, 0), (6, 1, 1))
        assert geom.aabb_gap(a, b) == 4

    def test_bodies_of_different_components_do_not_compare(self):
        # Two component-LOCAL boxes from different components are in different frames; subtracting
        # them reports a gap that describes neither.
        import types
        a = self._in_comp("A", types.SimpleNamespace(name="Frame", entityToken="CTOK::Frame"),
                          (0, 0, 0), (1, 1, 1))
        b = self._in_comp("B", types.SimpleNamespace(name="Lid", entityToken="CTOK::Lid"),
                          (5, 0, 0), (6, 1, 1))
        assert geom.aabb_gap(a, b) is None

    def test_proxies_under_different_occurrences_do_not_compare(self):
        # Two proxies whose assembly contexts sit on DIFFERENT components are in different frames
        # even though both answer an assemblyContext.
        import types
        from conftest import body_proxy
        comp_a = types.SimpleNamespace(name="Frame", entityToken="CTOK::Frame")
        comp_b = types.SimpleNamespace(name="Lid", entityToken="CTOK::Lid")
        pa = body_proxy(self._in_comp("A", comp_a, (0, 0, 0), (1, 1, 1)),
                        types.SimpleNamespace(name="Frame:1", fullPathName="Frame:1",
                                              component=comp_a))
        pb = body_proxy(self._in_comp("B", comp_b, (5, 0, 0), (6, 1, 1)),
                        types.SimpleNamespace(name="Lid:1", fullPathName="Lid:1",
                                              component=comp_b))
        assert geom.aabb_gap(pa, pb) is None


# ── parallel_plane_facts: the bounded gap between two PARALLEL PLANAR faces ──────────────────────
#
# A plane-to-plane separation is a LOWER BOUND on the gap between two BOUNDED faces sitting on
# those planes, never the gap itself: the two faces can also be offset ACROSS their planes, and any
# such offset only puts them further apart. So a measurement that IS the plane separation says
# nothing about whether the faces meet - which is what makes an unqualified "clearance" reading of
# it tighter than the parts are, and an unqualified "touching" verdict on it unproven.

def _comp(name):
    return types.SimpleNamespace(name=name, entityToken="CTOK::" + name)


_PLATE = _comp("Plate")


def _planar(origin, normal, box=None, comp=_PLATE, occurrence=None):
    """A planar BRepFace: its plane's origin + normal in cm, optionally its own AABB, and the space
    its reads are in - an `occurrence` makes it a PROXY (root space), otherwise it is a NATIVE face
    of `comp` (that component's space)."""
    return BRepFace(Plane(FakeVector3D(*normal), FakePoint(*origin)),
                    body=BRepBody(name="Plate1", parent_component=comp),
                    assembly_context=occurrence,
                    bounding_box=(FakeBoundingBox3D(FakePoint(*box[0]), FakePoint(*box[1]))
                                  if box else None))


class TestParallelPlaneFacts:
    def test_faces_offset_across_their_planes_report_the_boxed_lower_bound(self):
        # THE case: planes 2 cm apart, faces 3 cm apart along x. Every point of one face is 2 cm
        # from the other's PLANE, so 2.0 is a true lower bound - but the nearest points of the two
        # BOUNDED faces are sqrt(3^2 + 2^2) = 3.6056 cm apart, and a clearance judged on 2.0 reads
        # the parts as tighter than they are.
        a = _planar((0, 0, 0), (0, 0, 1), box=((0, 0, 0), (1, 1, 0)))
        b = _planar((0, 0, 2), (0, 0, 1), box=((4, 0, 2), (5, 1, 2)))
        facts = geom.parallel_plane_facts(a, b, 2.0, 1.0, "cm")
        assert facts["bounded"] is True
        assert facts["plane_separation_only"] is False
        assert facts["separation_cm"] == 2.0
        assert round(facts["distance_cm"], 6) == round(math.sqrt(13.0), 6)

    def test_coplanar_faces_side_by_side_are_not_reported_as_touching(self):
        # Separation 0 and a measured 0 read as CONTACT everywhere downstream, yet these two faces
        # are 4.4 cm apart in their shared plane.
        a = _planar((0, 0, 0), (0, 0, 1), box=((0, 0, 0), (1, 1, 0)))
        b = _planar((0, 0, 0), (0, 0, 1), box=((5.4, 0, 0), (6.4, 1, 0)))
        facts = geom.parallel_plane_facts(a, b, 0.0, 1.0, "cm")
        assert facts["bounded"] is True
        assert round(facts["distance_cm"], 6) == 4.4

    def test_faces_that_overlap_across_their_planes_disclose_the_separation_only(self):
        # Boxes overlapping in x and y: the only separating axis IS the plane normal, so the boxes
        # prove exactly the separation and nothing more. The number stands, flagged for what it is.
        a = _planar((0, 0, 0), (0, 0, 1), box=((0, 0, 0), (1, 1, 0)))
        b = _planar((0, 0, 2), (0, 0, 1), box=((0, 0, 2), (1, 1, 2)))
        facts = geom.parallel_plane_facts(a, b, 2.0, 1.0, "cm")
        assert facts["bounded"] is False
        assert facts["plane_separation_only"] is True
        assert facts["distance_cm"] == 2.0

    def test_anti_parallel_normals_are_the_same_pair(self):
        # Two faces looking AT each other carry opposing normals; a signed test would miss them.
        a = _planar((0, 0, 0), (0, 0, 1), box=((0, 0, 0), (1, 1, 0)))
        b = _planar((0, 0, 2), (0, 0, -1), box=((4, 0, 2), (5, 1, 2)))
        assert geom.parallel_plane_facts(a, b, 2.0, 1.0, "cm")["bounded"] is True

    def test_b_behind_a_s_normal_gives_the_same_separation(self):
        # Half of all real operand orders put b's plane on the NEGATIVE side of a's normal, and the
        # normal a face carries is not reliably outward - measured, the two faces parallel to the
        # originating sketch plane both read that plane's normal - so the sign of the projection
        # says nothing about which side anything is on. A separation is a distance; a signed one
        # would silently stop the gate
        # firing for every reversed pair. The origins are offset LATERALLY as well, so the
        # separation is taken against a's own normal rather than b's.
        a = _planar((0, 0, 2), (0, 0, 1), box=((0, 0, 2), (1, 1, 2)))
        b = _planar((0, 0, 0), (0, 0, 1), box=((4, 0, 0), (5, 1, 0)))
        facts = geom.parallel_plane_facts(a, b, 2.0, 1.0, "cm")
        assert facts["bounded"] is True
        assert facts["separation_cm"] == 2.0
        assert round(facts["distance_cm"], 6) == round(math.sqrt(13.0), 6)

    def test_the_separation_is_taken_against_the_first_face_s_own_normal(self):
        # With a lateral offset AND normals that are near-parallel rather than identical, projecting
        # the origin delta onto b's normal instead of a's lands on a different number: 4 cm of
        # lateral delta at 0.009 deg of tilt moves it by 6e-4 cm, far outside the band, so the gate
        # would stop firing on a pair it must describe.
        tilt = math.radians(geom.PARALLEL_PLANE_TOL_DEG * 0.9)
        a = _planar((0, 0, 0), (0, 0, 1), box=((0, 0, 0), (1, 1, 0)))
        b = _planar((4, 0, 2), (math.sin(tilt), 0, math.cos(tilt)), box=((4, 0, 2), (5, 1, 2)))
        facts = geom.parallel_plane_facts(a, b, 2.0, 1.0, "cm")
        assert facts is not None and facts["separation_cm"] == 2.0

    def test_a_boolean_coordinate_is_not_read_as_a_number(self):
        # bool is an int subclass: True would pass as the coordinate 1 cm and place a plane
        # somewhere nobody measured - here it would fabricate a separation of exactly 2.0 and open
        # the gate on it.
        a = _planar((0, 0, 0), (0, 0, 1), box=((0, 0, 0), (1, 1, 0)))
        b = BRepFace(Plane(FakeVector3D(0, 0, 1), FakePoint(True, 0, 2)),
                     body=BRepBody(name="Plate1", parent_component=_PLATE),
                     bounding_box=FakeBoundingBox3D(FakePoint(4, 0, 2), FakePoint(5, 1, 2)))
        assert geom.parallel_plane_facts(a, b, 2.0, 1.0, "cm") is None

    def test_a_measurement_that_is_not_the_plane_separation_is_left_alone(self):
        # The read-back gate: this module's own plane arithmetic has to land on the number the
        # measurement API returned before any of it describes that number.
        a = _planar((0, 0, 0), (0, 0, 1), box=((0, 0, 0), (1, 1, 0)))
        b = _planar((0, 0, 2), (0, 0, 1), box=((4, 0, 2), (5, 1, 2)))
        assert geom.parallel_plane_facts(a, b, 3.605551, 1.0, "cm") is None

    def test_the_read_back_gate_admits_a_difference_of_exactly_the_band(self):
        # Coplanar faces, so the separation is exactly 0 and the band is the whole difference:
        # a measurement one band away is still THIS separation, two bands away is not.
        a = _planar((0, 0, 0), (0, 0, 1), box=((0, 0, 0), (1, 1, 0)))
        b = _planar((0, 0, 0), (0, 0, 1), box=((5.4, 0, 0), (6.4, 1, 0)))
        assert geom.parallel_plane_facts(a, b, geom._SAME_LENGTH_CM, 1.0, "cm") is not None
        assert geom.parallel_plane_facts(a, b, 2 * geom._SAME_LENGTH_CM, 1.0, "cm") is None

    def test_a_bound_equal_to_the_measurement_is_not_a_bound(self):
        # The > vs >= boundary. Equal proves nothing new, and calling it bounded would relabel
        # every correctly-measured overlapping pair as a lower bound.
        a = _planar((0, 0, 0), (0, 0, 1), box=((0, 0, 0), (1, 1, 0)))
        b = _planar((0, 0, 0), (0, 0, 1), box=((0.5, 0.5, 0), (1.5, 1.5, 0)))
        assert geom.parallel_plane_facts(a, b, 0.0, 1.0, "cm")["bounded"] is False

    def test_a_bound_inside_the_band_does_not_count(self):
        # Half a band above the measurement is arithmetic noise, not a proof.
        a = _planar((0, 0, 0), (0, 0, 1), box=((0, 0, 0), (1, 1, 0)))
        b = _planar((0, 0, 0), (0, 0, 1), box=((1 + geom._SAME_LENGTH_CM / 2, 0, 0), (2, 1, 0)))
        assert geom.parallel_plane_facts(a, b, 0.0, 1.0, "cm")["bounded"] is False

    def test_a_bound_past_the_band_counts(self):
        a = _planar((0, 0, 0), (0, 0, 1), box=((0, 0, 0), (1, 1, 0)))
        b = _planar((0, 0, 0), (0, 0, 1), box=((1 + 2 * geom._SAME_LENGTH_CM, 0, 0), (2, 1, 0)))
        assert geom.parallel_plane_facts(a, b, 0.0, 1.0, "cm")["bounded"] is True

    def test_boxes_meeting_on_an_axis_add_nothing_to_the_bound(self):
        # A zero (or negative) axis gap is not a separation on that axis, so it must not enter the
        # distance: these boxes touch along x and the bound stays the plane separation.
        a = _planar((0, 0, 0), (0, 0, 1), box=((0, 0, 0), (1, 1, 0)))
        b = _planar((0, 0, 2), (0, 0, 1), box=((1, 0, 2), (2, 1, 2)))
        facts = geom.parallel_plane_facts(a, b, 2.0, 1.0, "cm")
        assert facts["bounded"] is False and facts["distance_cm"] == 2.0

    def test_faces_tilted_past_the_tolerance_are_left_alone(self):
        # Beyond the tolerance the pair no longer has ONE separation, so nothing here describes it.
        tilt = math.radians(2 * geom.PARALLEL_PLANE_TOL_DEG)
        a = _planar((0, 0, 0), (0, 0, 1), box=((0, 0, 0), (1, 1, 0)))
        b = _planar((0, 0, 2), (math.sin(tilt), 0, math.cos(tilt)), box=((4, 0, 2), (5, 1, 2)))
        assert geom.parallel_plane_facts(a, b, 2.0, 1.0, "cm") is None

    def test_faces_just_inside_the_tolerance_still_count(self):
        # The tolerance is pinned to a thousandth of its own size from both sides (an angle exactly
        # at it is not representable through the acos round trip), so widening or narrowing the
        # constant breaks one of the pair.
        tilt = math.radians(geom.PARALLEL_PLANE_TOL_DEG * 0.999)
        a = _planar((0, 0, 0), (0, 0, 1), box=((0, 0, 0), (1, 1, 0)))
        b = _planar((0, 0, 2), (math.sin(tilt), 0, math.cos(tilt)), box=((4, 0, 2), (5, 1, 2)))
        assert geom.parallel_plane_facts(a, b, 2.0, 1.0, "cm") is not None

    def test_faces_just_outside_the_tolerance_are_left_alone(self):
        tilt = math.radians(geom.PARALLEL_PLANE_TOL_DEG * 1.001)
        a = _planar((0, 0, 0), (0, 0, 1), box=((0, 0, 0), (1, 1, 0)))
        b = _planar((0, 0, 2), (math.sin(tilt), 0, math.cos(tilt)), box=((4, 0, 2), (5, 1, 2)))
        assert geom.parallel_plane_facts(a, b, 2.0, 1.0, "cm") is None

    def test_a_micron_of_disagreement_is_not_the_same_number(self):
        # The band pinned ABSOLUTELY, not through its own constant: a measurement 1e-4 cm (one
        # micron) away from the computed separation is a DIFFERENT number and the gate must refuse
        # it. A band wide enough to swallow a micron would let this module describe, and relabel, a
        # measurement its own arithmetic never reproduced.
        a = _planar((0, 0, 0), (0, 0, 1), box=((0, 0, 0), (1, 1, 0)))
        b = _planar((0, 0, 2), (0, 0, 1), box=((4, 0, 2), (5, 1, 2)))
        assert geom.parallel_plane_facts(a, b, 2.0001, 1.0, "cm") is None

    def test_a_micron_of_proven_gap_still_counts_as_a_bound(self):
        # The same band from the other side: 1e-4 cm of proven clear air is a real gap, so a band
        # wide enough to swallow it would discard bounds this module actually proved.
        a = _planar((0, 0, 0), (0, 0, 1), box=((0, 0, 0), (1, 1, 0)))
        b = _planar((0, 0, 0), (0, 0, 1), box=((1.0001, 0, 0), (2, 1, 0)))
        assert geom.parallel_plane_facts(a, b, 0.0, 1.0, "cm")["bounded"] is True

    def test_the_band_stays_below_any_length_a_design_expresses(self):
        # 1e-6 cm is 10 nanometres. The band has to sit under the smallest length a model can carry
        # (0.1 micron) and above zero, or it stops being a tolerance in one direction or the other.
        assert 0.0 < geom._SAME_LENGTH_CM <= 1e-5

    def test_the_tolerance_stays_tight_enough_that_one_separation_describes_the_pair(self):
        # Everything here treats the pair as ONE separation apart, so the tolerance has to be tight
        # enough that the two planes do not drift measurably across a face: over a 100 mm span the
        # admitted tilt must stay under 20 microns, or "the planes are X apart" stops being true of
        # the whole face. Widening the constant breaks this before it reaches a payload.
        assert 100.0 * math.tan(math.radians(geom.PARALLEL_PLANE_TOL_DEG)) < 0.02

    def test_identical_normals_are_parallel_at_zero(self):
        a = _planar((0, 0, 0), (0, 0, 1), box=((0, 0, 0), (1, 1, 0)))
        b = _planar((0, 0, 2), (0, 0, 1), box=((4, 0, 2), (5, 1, 2)))
        assert geom.parallel_plane_facts(a, b, 2.0, 1.0, "cm") is not None

    def test_an_unreadable_box_proves_nothing_and_says_so(self):
        # No box, no bound - the separation still stands, flagged as all that is known.
        a = _planar((0, 0, 0), (0, 0, 1))
        b = _planar((0, 0, 2), (0, 0, 1))
        facts = geom.parallel_plane_facts(a, b, 2.0, 1.0, "cm")
        assert facts["bounded"] is False and facts["plane_separation_only"] is True

    def test_an_unreadable_box_leaves_the_measurement_as_the_distance(self):
        # The box was never compared, so there is no bound to report - the distance must stay the
        # measurement. A 0.0 from an untested box would report the two faces as touching.
        a = _planar((0, 0, 0), (0, 0, 1))
        b = _planar((0, 0, 2), (0, 0, 1))
        assert geom.parallel_plane_facts(a, b, 2.0, 1.0, "cm")["distance_cm"] == 2.0

    def test_the_reported_distance_never_falls_below_the_measurement(self):
        # The docstring's invariant across all three outcomes: this only ever RAISES a lower bound,
        # so a distance under the measurement would report a gap the platform already ruled out.
        a = _planar((0, 0, 0), (0, 0, 1), box=((0, 0, 0), (1, 1, 0)))
        others = (_planar((0, 0, 2), (0, 0, 1), box=((4, 0, 2), (5, 1, 2))),   # bounded
                  _planar((0, 0, 2), (0, 0, 1), box=((0, 0, 2), (1, 1, 2))),   # boxes prove nothing
                  _planar((0, 0, 2), (0, 0, 1)))                               # boxes untested
        for other in others:
            assert geom.parallel_plane_facts(a, other, 2.0, 1.0, "cm")["distance_cm"] >= 2.0

    def test_an_untested_offset_is_a_different_state_from_a_tested_one(self):
        # "The boxes overlap, so contact is plausible" and "no box could be read, nothing was
        # tested" are different things to know. Reporting them with one flag and one sentence is
        # the safe(read, 0.0) fabrication in another shape.
        a = _planar((0, 0, 0), (0, 0, 1), box=((0, 0, 0), (1, 1, 0)))
        tested = geom.parallel_plane_facts(
            a, _planar((0, 0, 2), (0, 0, 1), box=((0, 0, 2), (1, 1, 2))), 2.0, 1.0, "cm")
        untested = geom.parallel_plane_facts(a, _planar((0, 0, 2), (0, 0, 1)), 2.0, 1.0, "cm")
        assert tested["lateral_offset_untested"] is False
        assert untested["lateral_offset_untested"] is True
        assert "were compared and prove no larger gap" in tested["note"]
        assert "were NOT compared" in untested["note"]


class TestBoxComparabilityPrecondition:
    """Two boxes only subtract when they are expressed in ONE coordinate space. MEASURED on
    2705.1.4: an assembly-context PROXY face reads its plane origin AND its bounding box in ROOT
    space (a cube in a component placed at (10,4,2) cm read plane origin (11,5,4) inside box
    (10,4,4)-(12,6,4)), so two proxies compare across DIFFERENT occurrences. A NATIVE face reads in
    its own component's space. The MIXED pair is unmeasured and must not be scored."""

    def _occ(self, name):
        return types.SimpleNamespace(name=name, fullPathName=name)

    def test_proxies_under_different_occurrences_compare(self):
        # THE part-to-part clearance case: two parts, each read through its own occurrence.
        a = _planar((0, 0, 0), (0, 0, 1), box=((0, 0, 0), (1, 1, 0)),
                    comp=_comp("PartA"), occurrence=self._occ("PartA:1"))
        b = _planar((0, 0, 2), (0, 0, 1), box=((4, 0, 2), (5, 1, 2)),
                    comp=_comp("PartB"), occurrence=self._occ("PartB:1"))
        facts = geom.parallel_plane_facts(a, b, 2.0, 1.0, "cm")
        assert facts["bounded"] is True
        assert round(facts["distance_cm"], 6) == round(math.sqrt(13.0), 6)

    def test_a_native_and_a_proxy_are_not_scored_against_each_other(self):
        # Unmeasured: a native reads in its component's space and a proxy in root space. This falls
        # through to the untested state rather than subtracting across two frames.
        a = _planar((0, 0, 0), (0, 0, 1), box=((0, 0, 0), (1, 1, 0)))
        b = _planar((0, 0, 2), (0, 0, 1), box=((4, 0, 2), (5, 1, 2)),
                    occurrence=self._occ("PartB:1"))
        facts = geom.parallel_plane_facts(a, b, 2.0, 1.0, "cm")
        assert facts["bounded"] is False
        assert facts["lateral_offset_untested"] is True
        assert facts["distance_cm"] == 2.0

    def test_natives_of_different_components_are_not_scored_against_each_other(self):
        a = _planar((0, 0, 0), (0, 0, 1), box=((0, 0, 0), (1, 1, 0)), comp=_comp("PartA"))
        b = _planar((0, 0, 2), (0, 0, 1), box=((4, 0, 2), (5, 1, 2)), comp=_comp("PartB"))
        assert geom.parallel_plane_facts(a, b, 2.0, 1.0, "cm")["lateral_offset_untested"] is True

    def test_two_wrappers_of_one_component_still_compare(self):
        # Component wrappers are measured never identity-stable - two reads of one component are
        # different objects sharing a token - so an identity test here would refuse a legal pair.
        one, other = _comp("PartA"), _comp("PartA")
        assert one is not other
        a = _planar((0, 0, 0), (0, 0, 1), box=((0, 0, 0), (1, 1, 0)), comp=one)
        b = _planar((0, 0, 2), (0, 0, 1), box=((4, 0, 2), (5, 1, 2)), comp=other)
        assert geom.parallel_plane_facts(a, b, 2.0, 1.0, "cm")["bounded"] is True

    def test_a_face_whose_owning_component_will_not_read_is_not_scored(self):
        a = _planar((0, 0, 0), (0, 0, 1), box=((0, 0, 0), (1, 1, 0)), comp=None)
        b = _planar((0, 0, 2), (0, 0, 1), box=((4, 0, 2), (5, 1, 2)))
        assert geom.parallel_plane_facts(a, b, 2.0, 1.0, "cm")["lateral_offset_untested"] is True

    def test_a_non_planar_face_on_either_side_is_left_alone(self):
        planar = _planar((0, 0, 0), (0, 0, 1), box=((0, 0, 0), (1, 1, 0)))
        curved = BRepFace(Cylinder(FakeVector3D(0, 0, 1), FakePoint(0, 0, 2)))
        assert geom.parallel_plane_facts(planar, curved, 2.0, 1.0, "cm") is None
        assert geom.parallel_plane_facts(curved, planar, 2.0, 1.0, "cm") is None

    def test_the_gate_is_the_surface_type_not_the_presence_of_a_normal(self):
        # A surface that is not a PLANE has no single separation from a parallel plane, whatever
        # fields it happens to expose - so the type decides, not whether an origin and a normal
        # read. (The measured CylinderSurfaceType value comes off the shared Cylinder fake.)
        not_a_plane = type("NotAPlane", (), {
            "surfaceType": Cylinder(FakeVector3D(0, 0, 1)).surfaceType,
            "origin": FakePoint(0, 0, 2), "normal": FakeVector3D(0, 0, 1)})()
        a = _planar((0, 0, 0), (0, 0, 1), box=((0, 0, 0), (1, 1, 0)))
        assert geom.parallel_plane_facts(a, BRepFace(not_a_plane), 2.0, 1.0, "cm") is None

    def test_an_entity_with_no_surface_geometry_is_left_alone(self):
        a = _planar((0, 0, 0), (0, 0, 1))
        assert geom.parallel_plane_facts(a, BRepBody(name="Plate"), 2.0, 1.0, "cm") is None

    def test_a_plane_with_no_origin_point_at_all_is_left_alone(self):
        # An absent origin is not the origin (0,0,0): defaulting it would place one plane at the
        # world origin and report a separation nobody measured.
        a = _planar((0, 0, 0), (0, 0, 1))
        b = BRepFace(Plane(FakeVector3D(0, 0, 1), None))
        assert geom.parallel_plane_facts(a, b, 2.0, 1.0, "cm") is None

    def test_a_plane_whose_origin_will_not_read_is_left_alone(self):
        # A half-read plane would place the separation somewhere nobody measured.
        a = _planar((0, 0, 0), (0, 0, 1))
        b = BRepFace(Plane(FakeVector3D(0, 0, 1), _RaisingCoord()))
        assert geom.parallel_plane_facts(a, b, 2.0, 1.0, "cm") is None

    def test_a_degenerate_normal_is_left_alone(self):
        a = _planar((0, 0, 0), (0, 0, 1))
        b = _planar((0, 0, 2), (0, 0, 0))
        assert geom.parallel_plane_facts(a, b, 2.0, 1.0, "cm") is None

    def test_a_boolean_measurement_is_not_read_as_a_number(self):
        # bool is an int subclass: False would sail through as a 0 cm reading and be compared
        # against a separation as though it were measured.
        a = _planar((0, 0, 0), (0, 0, 1), box=((0, 0, 0), (1, 1, 0)))
        b = _planar((0, 0, 0), (0, 0, 1), box=((5.4, 0, 0), (6.4, 1, 0)))
        assert geom.parallel_plane_facts(a, b, False, 1.0, "cm") is None

    def test_a_non_numeric_measurement_is_left_alone(self):
        a = _planar((0, 0, 0), (0, 0, 1))
        b = _planar((0, 0, 2), (0, 0, 1))
        assert geom.parallel_plane_facts(a, b, None, 1.0, "cm") is None


class TestParallelPlaneNote:
    """One sentence, one home: both measure tools publish this wording, and it states only what was
    read - the separation the two planes carry, and the bound the two boxes allow."""

    def test_the_bounded_note_reports_both_numbers_in_the_callers_units(self):
        a = _planar((0, 0, 0), (0, 0, 1), box=((0, 0, 0), (1, 1, 0)))
        b = _planar((0, 0, 2), (0, 0, 1), box=((4, 0, 2), (5, 1, 2)))
        note = geom.parallel_plane_facts(a, b, 2.0, 10.0, "mm")["note"]
        assert "20.0 mm apart" in note                        # 2 cm separation, scaled
        assert f"{round(math.sqrt(13.0) * 10.0, 6)} mm" in note
        assert "LOWER BOUND" in note

    def test_the_unproven_note_says_the_distance_is_the_plane_separation(self):
        a = _planar((0, 0, 0), (0, 0, 1), box=((0, 0, 0), (1, 1, 0)))
        b = _planar((0, 0, 2), (0, 0, 1), box=((0, 0, 2), (1, 1, 2)))
        note = geom.parallel_plane_facts(a, b, 2.0, 10.0, "mm")["note"]
        assert "20.0 mm apart" in note and "IS that plane separation" in note

    def test_both_notes_are_pure_ascii(self):
        # These sentences cross the wire JSON-serialized with ensure_ascii; a non-ASCII dash or
        # degree sign would reach the agent as an escape sequence.
        a = _planar((0, 0, 0), (0, 0, 1), box=((0, 0, 0), (1, 1, 0)))
        apart = _planar((0, 0, 2), (0, 0, 1), box=((4, 0, 2), (5, 1, 2)))
        over = _planar((0, 0, 2), (0, 0, 1), box=((0, 0, 2), (1, 1, 2)))
        for other in (apart, over):
            assert geom.parallel_plane_facts(a, other, 2.0, 1.0, "cm")["note"].isascii()


# ── subtree_facts: the child occurrences a measurement does NOT cover ──────────────────────────

def _occ(name, children=(), path=None, unresolved=()):
    """An occurrence as the disclosure reads one: a name, a fullPathName (the address a measure
    tool's target input resolves), its DIRECT children, and - through `component.occurrences` - the
    component-local collection that still holds the children childOccurrences drops."""
    kids = list(children)
    comp = types.SimpleNamespace(occurrences=_NamedCollection(kids + list(unresolved)))
    return types.SimpleNamespace(name=name, fullPathName=name if path is None else path,
                                 childOccurrences=_NamedCollection(kids), component=comp)


def _pair(a, b, kind_a="occurrence", kind_b="occurrence"):
    """The two (label, entity, kind) targets of one measurement, labelled as the tool labels them."""
    return (("a", a, kind_a), ("b", b, kind_b))


class _UncountedColl:
    """A collection whose .count raises - UNREADABLE, which is a different state from empty."""

    @property
    def count(self):
        raise RuntimeError("unreadable")


class _NoAddress:
    """An occurrence answering neither address: an unresolved external reference raises on
    fullPathName, and this one will not give a name either."""

    @property
    def fullPathName(self):
        raise RuntimeError("unresolved reference")

    @property
    def name(self):
        raise RuntimeError("unreadable")


class _Unresolved:
    """An occurrence whose external reference is UNRESOLVED: reading .component raises, which is the
    one detector _common.broken_reference gates on."""

    name = "Ghost:1"

    @property
    def component(self):
        raise RuntimeError("reference is not resolved")


def _address_of(entity):
    try:
        return entity.fullPathName
    except Exception:
        return None


def _gaps(monkeypatch, table):
    """Stub the shared measurement seam with {child fullPathName: cm} and return the (measured,
    against) call log. A child missing from the table does not measure - what a bodyless occurrence
    does live (3 : measurement failed)."""
    calls = []

    def fake(first, second):
        key = _address_of(first)
        calls.append((key, _address_of(second)))
        if key not in table:
            return None, {"isError": True}
        return types.SimpleNamespace(value=table[key]), None
    monkeypatch.setattr(geom._common, "min_distance", fake)
    return calls


class _NameOnly:
    """An occurrence whose external reference is UNRESOLVED: fullPathName RAISES and name still
    answers (measured, see _common.broken_reference)."""

    name = "Ghost:1"

    @property
    def fullPathName(self):
        raise RuntimeError("reference is not resolved")


class TestAddress:
    """The ONE way an occurrence is named on the wire - the shared helper model_measure_between's
    echo and the nested-child rows both address an entity through."""

    def test_the_full_path_wins_over_the_leaf_name(self):
        # THE reason this prefers fullPathName: `name` is the LEAF only, and two sub-assemblies can
        # each hold a 'Pedestal:1' - naming the leaf addresses a different entity than the one read.
        occ = types.SimpleNamespace(name="Pedestal:1", fullPathName="Frame:1+Pedestal:1")
        assert geom.address(occ) == "Frame:1+Pedestal:1"

    def test_an_unresolved_reference_is_named_by_the_name_it_still_answers(self):
        # The row survives the raise rather than vanishing: the caller is being told what a number
        # does not cover, which is true whether or not the path reads.
        assert geom.address(_NameOnly()) == "Ghost:1"

    def test_an_empty_full_path_falls_through_to_the_name(self):
        # THE boundary: an entity answering '' is not one that answered an address. Accepting the
        # empty string publishes a row naming nothing while a real name sits unread beside it.
        occ = types.SimpleNamespace(name="Pedestal:1", fullPathName="")
        assert geom.address(occ) == "Pedestal:1"

    def test_an_entity_answering_neither_says_it_is_unreadable(self):
        assert geom.address(_NoAddress()) == "(unreadable name)"

    def test_a_caller_holding_a_real_address_names_it_with_that_instead(self):
        # A face or edge carries neither property, and the find_geometry handle the caller passed
        # IS a real address for it - '(unreadable name)' would throw away the one it holds.
        assert geom.address(_NoAddress(), fallback="face:h7") == "face:h7"

    def test_the_fallback_is_reached_only_when_both_reads_fail(self):
        # the other side of that: a caller-supplied fallback must not outrank an entity that names.
        occ = types.SimpleNamespace(name="Pedestal:1", fullPathName="Frame:1+Pedestal:1")
        assert geom.address(occ, fallback="face:h7") == "Frame:1+Pedestal:1"
        assert geom.address(_NameOnly(), fallback="face:h7") == "Ghost:1"


class TestSubtreeFacts:
    """MEASURED: measureMinimumDistance against an occurrence measures that occurrence's OWN bodies
    and NOT what is nested inside it - a parent whose own body sits 90 mm from the other target,
    holding a child 40 mm from it, answers 90. So a gap read off a parent is silently OPTIMISTIC
    about the assembly under it, and each child is measured here on its own to say by how much."""

    def test_a_target_with_no_children_discloses_nothing(self, monkeypatch):
        # The quiet case, and the common one: a caveat on every measurement is one nobody reads.
        _gaps(monkeypatch, {})
        assert geom.subtree_facts(_pair(_occ("Carrier:1"), _occ("Frame:1")), 10.0, "mm") is None

    def test_a_kind_that_holds_no_occurrences_is_never_walked(self, monkeypatch):
        # A body/face/edge target has no subtree; the kind decides that, not what the object answers.
        _gaps(monkeypatch, {})
        held = _occ("Frame:1", [_occ("Pedestal:1")])
        assert geom.subtree_facts(_pair(held, _occ("Third:1"), kind_a="body"), 10.0, "mm") is None

    def test_a_component_target_is_never_walked(self, monkeypatch):
        # A Component never reaches this: measureMinimumDistance refuses one outright
        # (3 : invalid argument geometryOne), so the handler errors before the disclosure runs.
        # The fake ANSWERS childOccurrences, so only the kind check can refuse it - a fake without
        # one would pass whether the guard is there or not.
        _gaps(monkeypatch, {})
        comp = types.SimpleNamespace(name="Frame",
                                     occurrences=_NamedCollection([_occ("Pedestal:1")]),
                                     childOccurrences=_NamedCollection([_occ("Pedestal:1")]))
        assert geom.subtree_facts(_pair(comp, _occ("Third:1"), kind_a="component"),
                                  10.0, "mm") is None

    def test_a_parent_names_its_children_and_measures_each_one(self, monkeypatch):
        # THE case: the parent's own body is 9 cm from the other target and its child is 4 cm - the
        # 90 mm the caller got says nothing about the 40 mm part inside it.
        near = _occ("ParentWithBody:1+ChildNear:1")
        parent = _occ("ParentWithBody:1", [near])
        _gaps(monkeypatch, {"ParentWithBody:1+ChildNear:1": 4.0})
        facts = geom.subtree_facts(_pair(parent, _occ("Third:1")), 10.0, "mm")
        assert [r["target"] for r in facts["targets"]] == ["a"]
        rec = facts["targets"][0]
        assert rec["name"] == "ParentWithBody:1" and rec["child_count"] == 1
        assert rec["children"] == [{"name": "ParentWithBody:1+ChildNear:1", "distance": 40.0}]
        assert rec["measured_against"] == "b"
        assert "children_truncated" not in rec and "nested_deeper" not in rec
        assert facts["note"].startswith("NESTED TARGET: ")
        assert ("measured individually against target b as named; the nearest is "
                "ParentWithBody:1+ChildNear:1 at 40.0 mm") in facts["note"]

    def test_the_note_states_own_bodies_and_a_smaller_true_gap(self, monkeypatch):
        # The one sentence that has to be right: the number EXCLUDES the subtree, so the real gap
        # can be smaller. The opposite reading - that the number may belong to a nested child -
        # tells the caller to doubt a correct number. It says an OCCURRENCE contributes its own
        # bodies, not that the targets are occurrences: half of these pairs are a face handle.
        near = _occ("Frame:1+Pedestal:1")
        _gaps(monkeypatch, {"Frame:1+Pedestal:1": 0.0})
        note = geom.subtree_facts(_pair(_occ("Frame:1", [near]), _occ("Carrier:1")),
                                  10.0, "mm")["note"]
        assert "an occurrence contributes its OWN bodies" in note
        assert "NOT in that number" in note and "SMALLER" in note
        assert "may describe a nested child" not in note
        assert "each target's OWN bodies" not in note

    def test_the_note_names_the_nearest_child_and_points_at_the_payload(self, monkeypatch):
        # Point, don't inline: the full per-child list is already in targets_with_children, in the
        # same payload. The note carries the ONE actionable number - the nearest gap, which is what
        # undercuts the distance above - and says where the rest are.
        kids = [_occ("Frame:1+Far:1"), _occ("Frame:1+Near:1"), _occ("Frame:1+Mid:1")]
        _gaps(monkeypatch, {"Frame:1+Far:1": 9.0, "Frame:1+Near:1": 0.5, "Frame:1+Mid:1": 4.0})
        note = geom.subtree_facts(_pair(_occ("Frame:1", kids), _occ("Carrier:1")),
                                  10.0, "mm")["note"]
        assert "the nearest is Frame:1+Near:1 at 5.0 mm" in note
        assert "every child gap is in targets_with_children" in note
        assert "Frame:1+Far:1" not in note and "Frame:1+Mid:1" not in note

    def test_equal_nearest_gaps_name_the_first_of_them(self, monkeypatch):
        # A tie has no nearer answer to pick, so the first is named - deterministic either way.
        kids = [_occ("Frame:1+One:1"), _occ("Frame:1+Two:1")]
        _gaps(monkeypatch, {"Frame:1+One:1": 2.0, "Frame:1+Two:1": 2.0})
        note = geom.subtree_facts(_pair(_occ("Frame:1", kids), _occ("Carrier:1")),
                                  10.0, "mm")["note"]
        assert "the nearest is Frame:1+One:1 at 20.0 mm" in note

    def test_a_single_child_needs_no_pointer_to_the_rest(self, monkeypatch):
        kid = _occ("Frame:1+Pedestal:1")
        _gaps(monkeypatch, {"Frame:1+Pedestal:1": 0.0})
        note = geom.subtree_facts(_pair(_occ("Frame:1", [kid]), _occ("Carrier:1")),
                                  10.0, "mm")["note"]
        assert "the nearest is Frame:1+Pedestal:1 at 0.0 mm" in note
        assert "every child gap is in" not in note

    def test_each_record_is_measured_against_the_other_target(self, monkeypatch):
        # a's children are measured to b and b's to a - the only pairing that answers "how near is
        # anything inside this target to the thing I asked about". The cm gaps scale to the
        # caller's units like every other number here.
        a_kid, b_kid = _occ("Carrier:1+Pad:1"), _occ("Frame:1+Pedestal:1")
        a, b = _occ("Carrier:1", [a_kid]), _occ("Frame:1", [b_kid])
        calls = _gaps(monkeypatch, {"Carrier:1+Pad:1": 1.0, "Frame:1+Pedestal:1": 0.0})
        facts = geom.subtree_facts(_pair(a, b), 10.0, "mm")
        assert [r["target"] for r in facts["targets"]] == ["a", "b"]
        assert [r["measured_against"] for r in facts["targets"]] == ["b", "a"]
        assert calls == [("Carrier:1+Pad:1", "Frame:1"), ("Frame:1+Pedestal:1", "Carrier:1")]
        assert facts["note"].startswith("NESTED TARGETS: ")
        assert "the nearest is Carrier:1+Pad:1 at 10.0 mm" in facts["note"]   # 1 cm in caller mm
        assert "the nearest is Frame:1+Pedestal:1 at 0.0 mm" in facts["note"]

    def test_a_child_that_does_not_measure_is_null_never_zero(self, monkeypatch):
        # 0 is TOUCHING everywhere this lands. A bodyless child raises live, and publishing that
        # as 0.0 would invent contact inside an assembly nobody measured.
        kid = _occ("Frame:1+Air:1")
        _gaps(monkeypatch, {})
        rec = geom.subtree_facts(_pair(_occ("Frame:1", [kid]), _occ("Third:1")),
                                 10.0, "mm")["targets"][0]
        assert rec["children"] == [{"name": "Frame:1+Air:1", "distance": None}]

    def test_a_bool_child_measurement_is_not_read_as_a_gap(self, monkeypatch):
        # bool is an int subclass: False would publish as a 0.0 mm gap - contact.
        kid = _occ("Frame:1+Air:1")
        _gaps(monkeypatch, {"Frame:1+Air:1": False})
        rec = geom.subtree_facts(_pair(_occ("Frame:1", [kid]), _occ("Third:1")),
                                 10.0, "mm")["targets"][0]
        assert rec["children"][0]["distance"] is None

    def test_an_unreadable_child_count_is_not_evidence_of_children(self, monkeypatch):
        _gaps(monkeypatch, {})
        occ = types.SimpleNamespace(name="Frame:1", fullPathName="Frame:1",
                                    childOccurrences=_UncountedColl())
        assert geom.subtree_facts(_pair(occ, _occ("Third:1")), 10.0, "mm") is None

    def test_an_absent_child_collection_discloses_nothing(self, monkeypatch):
        _gaps(monkeypatch, {})
        occ = types.SimpleNamespace(name="Frame:1", fullPathName="Frame:1")
        assert geom.subtree_facts(_pair(occ, _occ("Third:1")), 10.0, "mm") is None

    def test_exactly_the_cap_is_measured_in_full(self, monkeypatch):
        kids = [_occ(f"Frame:1+Bolt{i:02d}:1") for i in range(geom.SUBTREE_NAMES_MAX)]
        _gaps(monkeypatch, {k.fullPathName: 1.0 for k in kids})
        facts = geom.subtree_facts(_pair(_occ("Frame:1", kids), _occ("Third:1")), 10.0, "mm")
        rec = facts["targets"][0]
        assert rec["child_count"] == geom.SUBTREE_NAMES_MAX
        assert len(rec["children"]) == geom.SUBTREE_NAMES_MAX
        assert "children_truncated" not in rec and "of which the first" not in facts["note"]
        assert "measured individually against target b as named" in facts["note"]

    def test_one_past_the_cap_measures_the_cap_and_says_it_was_cut(self, monkeypatch):
        # The count comes off the collection, so a capped list still publishes how many there are.
        kids = [_occ(f"Frame:1+Bolt{i:02d}:1") for i in range(geom.SUBTREE_NAMES_MAX + 1)]
        _gaps(monkeypatch, {k.fullPathName: 1.0 for k in kids})
        facts = geom.subtree_facts(_pair(_occ("Frame:1", kids), _occ("Third:1")), 10.0, "mm")
        rec = facts["targets"][0]
        assert rec["child_count"] == geom.SUBTREE_NAMES_MAX + 1
        assert len(rec["children"]) == geom.SUBTREE_NAMES_MAX
        assert rec["children_truncated"] is True
        # The scope is stated UP FRONT, not corrected twelve entries later.
        assert (f"holds {geom.SUBTREE_NAMES_MAX + 1} child occurrences, of which the first "
                f"{geom.SUBTREE_NAMES_MAX} were measured") in facts["note"]
        assert "(1 was not measured, so a nearer child is possible)" in facts["note"]

    def test_a_deep_child_is_disclosed_whichever_position_it_sits_in(self, monkeypatch):
        # The flag accumulates ACROSS children: a last-child-wins or first-child-wins read would
        # pass every single-child test and lose the deeper level in a mixed list.
        for kids in ([_occ("Flat:1"), _occ("Deep:1", [_occ("Pin:1")])],
                     [_occ("Deep:1", [_occ("Pin:1")]), _occ("Flat:1")]):
            _gaps(monkeypatch, {k.fullPathName: 1.0 for k in kids})
            facts = geom.subtree_facts(_pair(_occ("Frame:1", kids), _occ("Third:1")), 10.0, "mm")
            assert facts["targets"][0]["nested_deeper"] is True
            assert "holds children of its own, which were not measured" in facts["note"]

    def test_children_of_children_are_neither_measured_nor_flagged_when_absent(self, monkeypatch):
        kids = [_occ("Flat:1"), _occ("AlsoFlat:1")]
        _gaps(monkeypatch, {k.fullPathName: 1.0 for k in kids})
        facts = geom.subtree_facts(_pair(_occ("Frame:1", kids), _occ("Third:1")), 10.0, "mm")
        assert "nested_deeper" not in facts["targets"][0]
        assert "children of its own" not in facts["note"]

    def test_children_the_named_list_cannot_carry_are_counted(self, monkeypatch):
        # childOccurrences DROPS an unresolved child, so the list is short by exactly this many -
        # a disclosure whose job is honesty must not present a list it knows is incomplete.
        kid = _occ("Frame:1+Pedestal:1")
        _gaps(monkeypatch, {"Frame:1+Pedestal:1": 0.0})
        facts = geom.subtree_facts(
            _pair(_occ("Frame:1", [kid], unresolved=[_Unresolved()]), _occ("Third:1")), 10.0, "mm")
        rec = facts["targets"][0]
        assert rec["child_count"] == 1 and rec["unresolved_children"] == 1
        assert "1 more hold an unresolved reference" in facts["note"]

    def test_a_tree_with_nothing_unresolved_keeps_the_key_out(self, monkeypatch):
        kid = _occ("Frame:1+Pedestal:1")
        _gaps(monkeypatch, {"Frame:1+Pedestal:1": 0.0})
        facts = geom.subtree_facts(_pair(_occ("Frame:1", [kid]), _occ("Third:1")), 10.0, "mm")
        assert "unresolved_children" not in facts["targets"][0]
        assert "unresolved reference" not in facts["note"]

    def test_a_child_that_answers_no_address_is_still_counted_and_named_unreadable(self, monkeypatch):
        # Dropping the row would under-state what the number does NOT cover, which is the whole
        # point of the disclosure.
        _gaps(monkeypatch, {})
        facts = geom.subtree_facts(_pair(_occ("Frame:1", [_NoAddress()]), _occ("Third:1")),
                                   10.0, "mm")
        rec = facts["targets"][0]
        assert rec["child_count"] == 1 and rec["children"][0]["name"] == "(unreadable name)"
        assert rec["children"][0]["distance"] is None

    def test_a_target_whose_children_all_fail_to_measure_says_so(self, monkeypatch):
        # There is no nearest gap to name, and the note must not go silent about the children it
        # counted - the caller still has to know the number excludes them.
        kids = [_occ("Frame:1+Air:1"), _occ("Frame:1+Ghost:1")]
        _gaps(monkeypatch, {})
        facts = geom.subtree_facts(_pair(_occ("Frame:1", kids), _occ("Third:1")), 10.0, "mm")
        assert "holds 2 child occurrences, none of which measured" in facts["note"]
        assert "the nearest" not in facts["note"]
        # no shortfall parenthetical: there is no nearest claim to hedge, and "none measured" is
        # already the stronger statement.
        assert "so a nearer child is possible" not in facts["note"]

    def test_a_partly_measured_list_does_not_claim_a_proven_nearest(self, monkeypatch):
        # THE partial case: one listed child's gap is UNKNOWN, so the smallest number in hand is
        # the nearest of what ANSWERED - a nearer child is possible, and an agent reads this
        # disclosure precisely to learn the smallest gap inside the assembly.
        kids = [_occ("Frame:1+Far:1"), _occ("Frame:1+Air:1"), _occ("Frame:1+Near:1")]
        _gaps(monkeypatch, {"Frame:1+Far:1": 7.0, "Frame:1+Near:1": 4.0})
        facts = geom.subtree_facts(_pair(_occ("Frame:1", kids), _occ("Third:1")), 10.0, "mm")
        note = facts["note"]
        assert "holds 3 child occurrences, 2 of the 3 listed measured individually" in note
        assert "(1 was not measured, so a nearer child is possible)" in note
        assert "the nearest of those is Frame:1+Near:1 at 40.0 mm" in note
        # never the bare claim - that would assert a minimum over a set holding an unknown
        assert "the nearest is" not in note

    def test_a_fully_measured_list_keeps_the_plain_nearest_claim(self, monkeypatch):
        # The boundary the other way: with every child the TARGET holds measured, the minimum IS
        # proven, so the hedge must not fire and cost the caller a usable number.
        kids = [_occ("Frame:1+Far:1"), _occ("Frame:1+Near:1")]
        _gaps(monkeypatch, {"Frame:1+Far:1": 7.0, "Frame:1+Near:1": 4.0})
        note = geom.subtree_facts(_pair(_occ("Frame:1", kids), _occ("Third:1")), 10.0, "mm")["note"]
        assert "holds 2 child occurrences, measured individually" in note
        assert "the nearest is Frame:1+Near:1 at 40.0 mm" in note
        assert "not measured" not in note and "of those" not in note

    def test_a_capped_list_hedges_even_when_every_listed_child_measured(self, monkeypatch):
        # The trap this closes: 'listed' is the CAPPED list, not what the target holds, so a
        # fully-measured capped list still has unknowns past the cap. Measured live with the 12
        # inside the cap FAR and the 4 beyond it NEAR, the bare claim called 290 mm the nearest
        # while a 5 mm child sat unlisted - EVC-8's own failure mode one level down.
        far = [_occ(f"Frame:1+Far{i:02d}:1") for i in range(geom.SUBTREE_NAMES_MAX)]
        near = [_occ(f"Frame:1+Near{i:02d}:1") for i in range(4)]
        _gaps(monkeypatch, {k.fullPathName: 29.0 for k in far})   # only the listed ones measure
        note = geom.subtree_facts(_pair(_occ("Frame:1", far + near), _occ("Third:1")),
                                  10.0, "mm")["note"]
        assert (f"holds {geom.SUBTREE_NAMES_MAX + 4} child occurrences, of which the first "
                f"{geom.SUBTREE_NAMES_MAX} were measured individually") in note
        assert "(4 were not measured, so a nearer child is possible)" in note
        assert "the nearest of those is Frame:1+Far00:1 at 290.0 mm" in note
        assert "the nearest is" not in note

    def test_a_capped_list_that_is_also_partly_unmeasured_counts_both_shortfalls(self, monkeypatch):
        # Two limits at once: the list stops at the cap AND one listed child did not answer. The
        # unknown count is over what the TARGET holds, so the caller is not left adding 4 unlisted
        # to 1 unmeasured themselves.
        kids = [_occ(f"Frame:1+Bolt{i:02d}:1") for i in range(geom.SUBTREE_NAMES_MAX + 4)]
        _gaps(monkeypatch, {k.fullPathName: 5.0 for k in kids[1:]})   # the first does not measure
        note = geom.subtree_facts(_pair(_occ("Frame:1", kids), _occ("Third:1")),
                                  10.0, "mm")["note"]
        assert (f"holds {geom.SUBTREE_NAMES_MAX + 4} child occurrences, of which the first "
                f"{geom.SUBTREE_NAMES_MAX} are listed and {geom.SUBTREE_NAMES_MAX - 1} of those "
                "measured individually") in note
        assert "(5 were not measured, so a nearer child is possible)" in note   # 4 unlisted + 1
        assert "the nearest of those is Frame:1+Bolt01:1 at 50.0 mm" in note

    def test_a_capped_list_where_nothing_measured_says_both(self, monkeypatch):
        kids = [_occ(f"Frame:1+Bolt{i:02d}:1") for i in range(geom.SUBTREE_NAMES_MAX + 1)]
        _gaps(monkeypatch, {})
        note = geom.subtree_facts(_pair(_occ("Frame:1", kids), _occ("Third:1")),
                                  10.0, "mm")["note"]
        assert (f"of which the first {geom.SUBTREE_NAMES_MAX} are listed, none of which measured"
                in note)
        assert "the nearest" not in note

    def test_the_note_is_pure_ascii(self, monkeypatch):
        # It crosses the wire JSON-serialized with ensure_ascii.
        deep = _occ("Frame:1", [_occ("Pedestal:1", [_occ("Pin:1")])], unresolved=[_Unresolved()])
        capped = _occ("Frame:1", [_occ(f"Frame:1+Bolt{i:02d}:1") for i in range(geom.SUBTREE_NAMES_MAX + 1)])
        _gaps(monkeypatch, {})
        for target in (deep, capped):
            assert geom.subtree_facts(_pair(target, _occ("Third:1")), 10.0, "mm")["note"].isascii()


class _RaisingCoord:
    """A vector/point whose .y read raises - the unreadable-coordinate case the None-guards catch."""

    x = 1.0
    z = 0.0

    @property
    def y(self):
        raise RuntimeError("unreadable")


class TestUnreadableCoordinateGuards:
    def test_unit_vector_with_an_unreadable_coordinate_is_none(self):
        assert geom.unit_vector(_RaisingCoord()) is None

    def test_unit_vector_between_with_an_unreadable_endpoint_is_none(self):
        assert geom.unit_vector_between(_RaisingCoord(), FakePoint(1, 0, 0)) is None
        assert geom.unit_vector_between(FakePoint(0, 0, 0), _RaisingCoord()) is None

    def test_axis_vec_with_a_non_numeric_component_is_none(self):
        from unittest.mock import Mock
        assert geom.axis_vec(FakeVector3D(Mock(), 0, 0)) is None
        assert geom.axis_vec(_RaisingCoord()) is None


class TestOccWorldFrameGuards:
    """occ_world_frame omits keys rather than faking them: every unreadable piece drops ONLY its
    own keys, so a partial read stays honest instead of publishing a zeroed placement."""

    def test_an_unreadable_transform_omits_origin_and_axes_but_keeps_the_bbox(self):
        import types
        # body_aabb reads Occurrence.boundingBox2(entityTypes) - the bodies-only box.
        box = FakeBoundingBox3D(FakePoint(0, 0, 0), FakePoint(2, 2, 2))
        occ = types.SimpleNamespace(transform2=None, boundingBox2=lambda types_: box)
        out = geom.occ_world_frame(occ, 10.0)
        assert "origin" not in out and "x_axis" not in out
        assert out["bbox_center"] == [10.0, 10.0, 10.0]
        assert out["bbox_size"] == [20.0, 20.0, 20.0]

    def test_a_malformed_coordinate_system_omits_the_axes(self):
        import types
        m = types.SimpleNamespace(translation=FakeVector3D(1, 2, 3),
                                  getAsCoordinateSystem=lambda: "not-a-4-tuple")
        occ = types.SimpleNamespace(transform2=m, bRepBodies=[])
        out = geom.occ_world_frame(occ, 10.0)
        assert out["origin"] == [10.0, 20.0, 30.0]
        assert "x_axis" not in out and "y_axis" not in out and "z_axis" not in out

    def test_a_degenerate_axis_is_omitted_while_readable_ones_land(self):
        import types
        from unittest.mock import Mock
        cs = (FakePoint(0, 0, 0), FakeVector3D(1, 0, 0), FakeVector3D(Mock(), 0, 0),
              FakeVector3D(0, 0, 1))
        m = types.SimpleNamespace(translation=FakeVector3D(0, 0, 0),
                                  getAsCoordinateSystem=lambda: cs)
        occ = types.SimpleNamespace(transform2=m, bRepBodies=[])
        out = geom.occ_world_frame(occ, 1.0)
        assert out["x_axis"] == [1.0, 0.0, 0.0] and out["z_axis"] == [0.0, 0.0, 1.0]
        assert "y_axis" not in out

    def test_a_translation_component_that_will_not_read_omits_the_origin(self):
        # 0.0 for the component that failed places the part AT the world origin as a measured
        # position - the same fabrication axis_vec refuses for a direction, on the number a caller
        # positions and measures against.
        import types
        m = types.SimpleNamespace(translation=_RaisingCoord(), getAsCoordinateSystem=lambda: None)
        occ = types.SimpleNamespace(transform2=m, bRepBodies=[])
        assert "origin" not in geom.occ_world_frame(occ, 1.0)

    def test_a_non_numeric_translation_component_omits_the_origin(self):
        import types
        from unittest.mock import Mock
        m = types.SimpleNamespace(translation=FakeVector3D(Mock(), 0, 0),
                                  getAsCoordinateSystem=lambda: None)
        occ = types.SimpleNamespace(transform2=m, bRepBodies=[])
        assert "origin" not in geom.occ_world_frame(occ, 1.0)

    def test_an_occurrence_really_at_the_world_origin_publishes_zeros(self):
        # 0,0,0 is an ANSWER (an unmoved occurrence), distinguishable from the omissions above
        import types
        m = types.SimpleNamespace(translation=FakeVector3D(0, 0, 0),
                                  getAsCoordinateSystem=lambda: None)
        occ = types.SimpleNamespace(transform2=m, bRepBodies=[])
        assert geom.occ_world_frame(occ, 10.0)["origin"] == [0.0, 0.0, 0.0]

    def test_an_unreadable_bbox_endpoint_omits_the_bbox_keys(self):
        import types

        class _NoMin:
            minPoint = property(lambda s: (_ for _ in ()).throw(RuntimeError("unreadable")))
            maxPoint = FakePoint(1, 1, 1)

        occ = types.SimpleNamespace(transform2=None, boundingBox2=lambda types_: _NoMin())
        out = geom.occ_world_frame(occ, 1.0)
        assert "bbox_center" not in out and "bbox_size" not in out

    def test_a_corner_COORDINATE_that_will_not_read_omits_the_bbox_keys(self):
        # The box and both corner POINTS read; one corner's .y does not. The centre/size arithmetic
        # is a guarded read like every other coordinate here, so the two keys drop - unguarded, the
        # raise leaves the occurrence row's caller with no row at all.
        import types
        box = FakeBoundingBox3D(_RaisingCoord(), FakePoint(2, 2, 2))
        occ = types.SimpleNamespace(transform2=None, boundingBox2=lambda types_: box)
        out = geom.occ_world_frame(occ, 1.0)
        assert "bbox_center" not in out and "bbox_size" not in out

    def test_a_non_numeric_corner_coordinate_omits_the_bbox_keys(self):
        # adsk mocks answer a truthy Mock for anything unmodeled; multiplying one into bbox_size
        # would publish a Mock as a measurement (or raise at json time).
        import types
        from unittest.mock import Mock
        box = FakeBoundingBox3D(FakePoint(0, Mock(), 0), FakePoint(2, 2, 2))
        occ = types.SimpleNamespace(transform2=None, boundingBox2=lambda types_: box)
        assert geom.occ_world_frame(occ, 1.0) == {}

    def test_a_fully_readable_box_still_reports_centre_and_size(self):
        # The guard must not cost the normal reading: an occurrence spanning (0,0,0)-(2,4,6) cm in
        # mm reads centre (10,20,30) and size (20,40,60).
        import types
        box = FakeBoundingBox3D(FakePoint(0, 0, 0), FakePoint(2, 4, 6))
        occ = types.SimpleNamespace(transform2=None, boundingBox2=lambda types_: box)
        out = geom.occ_world_frame(occ, 10.0)
        assert out["bbox_center"] == [10.0, 20.0, 30.0]
        assert out["bbox_size"] == [20.0, 40.0, 60.0]
