"""Unit tests for surface_patch.py - the loop fill, the rails and the multi-loop shape."""

import types

import adsk.core
import adsk.fusion
import pytest

from conftest import (BRepBody, BRepEdge, FakeFeature as _SharedFeature,
                      FakeFeatures as _SharedFeatures, MakeComp, MakeDesign,
                      _FakeObjectCollection, install, load_tool, payload)

sc = load_tool("surface_patch")


def _body(name="Surf1", is_solid=False, solid_readable=True):
    """One result body - an open sheet unless a test asks for a solid or an unreadable flag."""
    return BRepBody(name, is_solid=is_solid, solid_readable=solid_readable)


def _edge():
    """One B-Rep edge whose owning body does not read, so the host falls back to the component."""
    edge = BRepEdge(curve=None)
    edge.body = None
    return edge


def _comp(features, name="Root"):
    """A component carrying the patch feature collection."""
    comp = MakeComp(name=name)
    comp.features = features
    return comp


class _RailsColl(_FakeObjectCollection):
    """What interiorRailsAndPoints reads back: a FRESH ObjectCollection on every read (never the
    object assigned), whose count reads None when it is EMPTY. Both measured live."""

    def __init__(self, items=()):
        super().__init__()
        self._items = list(items)

    @property
    def count(self):
        return len(self._items) or None


class FakeFeature(_SharedFeature):
    """The shared feature plus the extent a depth read-back reads."""
    def __init__(self, name="Surface1", bodies=None, extent_cm=None):
        super().__init__(name=name, bodies=bodies if bodies is not None else [_body()])
        if extent_cm is not None:
            # ExtrudeFeature.extentOne is a DistanceExtentDefinition (a SymmetricExtentDefinition
            # for a symmetric extrude) whose .distance is a ModelParameter reading CM, signed as
            # requested. extent_cm=None gives a feature whose extent cannot be read at all.
            self.extentOne = types.SimpleNamespace(
                distance=types.SimpleNamespace(value=extent_cm))


class FakePatchInput:
    def __init__(self, boundary, op, rails_dropped=0):
        self.boundary = boundary
        self.operation = op
        self.continuity = None
        self._rails = []
        self._rails_dropped = rails_dropped

    @property
    def interiorRailsAndPoints(self):
        kept = self._rails[:len(self._rails) - self._rails_dropped]
        return _RailsColl(kept)

    @interiorRailsAndPoints.setter
    def interiorRailsAndPoints(self, coll):
        self._rails = list(coll)


class _ContinuityRejectingPatchInput:
    """A patch input whose .continuity setter raises - models the API rejecting the value. The set
    must NOT be wrapped in safe(): a failed patch surfaces as an error, never a silent no-op that
    reports success."""
    def __init__(self, boundary, op):
        self.boundary = boundary
        self.operation = op
    def __setattr__(self, name, value):
        if name == "continuity":
            raise RuntimeError("continuity rejected by the API")
        object.__setattr__(self, name, value)


class FakePatchFeatures:
    def __init__(self, result_bodies=None, feature=True, raises=None, rails_dropped=0):
        # raises: add() raises this message - models the kernel refusing the patch
        # (e.g. a tangent saddle opening the single-seed auto-complete cannot chain).
        # rails_dropped: how many assigned rails the input fails to keep, so the count read-back
        # disagrees with what was assigned.
        self.last_input = None
        self._result = result_bodies
        self._feature = feature
        self._raises = raises
        self._rails_dropped = rails_dropped
    def createInput(self, boundary, op):
        self.last_input = FakePatchInput(boundary, op, rails_dropped=self._rails_dropped)
        return self.last_input
    def add(self, inp):
        if self._raises:
            raise RuntimeError(self._raises)
        if not self._feature:
            return None
        return FakeFeature(name="Patch1", bodies=self._result)


class FakePatchFeaturesRejectContinuity(FakePatchFeatures):
    """createInput returns a patch input that raises when 'continuity' is set."""
    def createInput(self, boundary, op):
        self.last_input = _ContinuityRejectingPatchInput(boundary, op)
        return self.last_input


class FakeFeatures(_SharedFeatures):
    """comp.features plus the three surface-build collections this tool reaches through."""
    def __init__(self, ef=None, rf=None, pf=None):
        super().__init__()
        self.extrudeFeatures = ef
        self.revolveFeatures = rf
        self.patchFeatures = pf


@pytest.fixture
def wire(monkeypatch):
    """Factory: install a design holding `comp` into the tool module, with the adsk members a
    patch build reads."""
    def _wire(comp, handle_map=None):
        design = MakeDesign(comp=comp, tokens=handle_map or {})
        install(sc, design)
        monkeypatch.setattr(adsk.core.ValueInput, "createByReal",
                            staticmethod(lambda v: ("real", v)))
        monkeypatch.setattr(adsk.fusion, "BRepEdge", BRepEdge)
        return design
    return _wire


class TestSurfacePatch:

    def test_patch_over_closed_edge_loop(self, wire):
        e1, e2, e3 = _edge(), _edge(), _edge()
        pf = FakePatchFeatures(result_bodies=[_body("Patch1")])
        wire(_comp(FakeFeatures(pf=pf)), handle_map={"E1": e1, "E2": e2, "E3": e3})
        out = payload(sc.handler(boundary=["E1", "E2", "E3"]))
        assert out["patched"] is True
        assert out["is_solid"] is False
        assert out["boundary_edge_count"] == 3
        # multiple edges -> an ObjectCollection was passed as the boundary
        assert isinstance(pf.last_input.boundary, _FakeObjectCollection)

    def test_single_edge_passes_edge_for_autocomplete(self, wire):
        e1 = _edge()
        pf = FakePatchFeatures(result_bodies=[_body("Patch1")])
        wire(_comp(FakeFeatures(pf=pf)), handle_map={"E1": e1})
        out = payload(sc.handler(boundary="E1"))
        assert out["patched"] is True
        # a single edge is passed directly (Fusion auto-finds the loop), NOT wrapped in a collection
        assert pf.last_input.boundary is e1

    def test_null_feature_errors(self, wire):
        e1 = _edge()
        pf = FakePatchFeatures(feature=False)
        wire(_comp(FakeFeatures(pf=pf)), handle_map={"E1": e1})
        res = sc.handler(boundary="E1")
        assert res["isError"] is True and "no feature" in res["message"]

    def test_chain_failure_names_both_known_causes_not_just_tangent_saddle(self, wire):
        # a single-seed patch failure ('invalid argument chainOptions', live-verified) has TWO known
        # causes that read identically from the exception string alone: a degenerate TANGENT saddle
        # opening, OR an edge loop SPLIT into more segments by a later feature (e.g. a rim fillet).
        # The message must name BOTH causes and both recipes, never assert tangent-saddle alone.
        e1 = _edge()
        pf = FakePatchFeatures(raises="3 : invalid argument chainOptions")
        wire(_comp(FakeFeatures(pf=pf)), handle_map={"E1": e1})
        res = sc.handler(boundary="E1")
        assert res["isError"] is True
        msg = res["message"]
        # cause 1: tangent saddle + its two-half-edge recipe
        assert "TANGENT" in msg and "two half-edges" in msg
        # cause 2: a fillet-split loop + its all-edges recipe
        assert "SPLIT" in msg and "fillet" in msg and "ALL of the loop's edges" in msg
        # the discriminating probe: find_geometry's edge count
        assert "find_geometry" in msg and "exactly 2 means case (1), more means case (2)" in msg

    def test_chain_failure_message_does_not_assert_tangent_as_sole_cause(self, wire):
        # regression guard for the exact misdiagnosis: the message must not present the tangent-saddle
        # story as the ONLY explanation ("this looks like a TANGENT saddle opening") - it must frame
        # it as one of two possibilities.
        e1 = _edge()
        pf = FakePatchFeatures(raises="3 : invalid argument chainOptions")
        wire(_comp(FakeFeatures(pf=pf)), handle_map={"E1": e1})
        res = sc.handler(boundary="E1")
        msg = res["message"]
        assert "this looks like a tangent saddle opening" not in msg.lower()
        assert "two known causes" in msg

    def test_generic_patch_failure_keeps_the_plain_closed_loop_hint(self, wire):
        # a non-tangency failure keeps the existing closed-loop hint, not the tangency teaching.
        e1 = _edge()
        pf = FakePatchFeatures(raises="some other kernel error")
        wire(_comp(FakeFeatures(pf=pf)), handle_map={"E1": e1})
        res = sc.handler(boundary="E1")
        assert res["isError"] is True
        assert "CLOSED loop" in res["message"] and "TANGENT" not in res["message"]

    def test_unknown_operation_rejected(self, wire):
        e1 = _edge()
        wire(_comp(FakeFeatures(pf=FakePatchFeatures())), handle_map={"E1": e1})
        res = sc.handler(boundary="E1", operation="cut")
        assert res["isError"] is True and "new, new_component" in res["message"]

    def test_boundaries_patches_every_loop_in_one_call(self, wire):
        # the "patch all 8 holes at once" case: 4 separate rim edges -> 4 patches in one call
        edges = {f"R{i}": _edge() for i in range(4)}
        pf = FakePatchFeatures(result_bodies=[_body("P")])
        wire(_comp(FakeFeatures(pf=pf)), handle_map=edges)
        out = payload(sc.handler(boundaries=["R0", "R1", "R2", "R3"]))
        assert out["patched"] == 4 and out["requested"] == 4 and out["failed"] == 0
        assert len(out["patches"]) == 4
        assert len(out["result_bodies"]) == 4   # one patch body per loop

    def test_boundaries_reports_per_loop_failure_without_aborting(self, wire):
        # one good rim + one stale handle -> 1 patched, 1 failed, the rest still done
        edges = {"R0": _edge(), "R2": _edge()}   # "R1" intentionally unresolvable (stale)
        pf = FakePatchFeatures(result_bodies=[_body("P")])
        wire(_comp(FakeFeatures(pf=pf)), handle_map=edges)
        out = payload(sc.handler(boundaries=["R0", "R1", "R2"]))
        assert out["patched"] == 2 and out["failed"] == 1
        assert out["errors"][0]["index"] == 1   # the failing loop is identified by index

    def test_neither_boundary_nor_boundaries_errors(self, wire):
        wire(_comp(FakeFeatures(pf=FakePatchFeatures())))
        res = sc.handler()
        assert res["isError"] is True and "boundaries" in res["message"]

    def test_unknown_continuity_rejected(self, wire):
        e1 = _edge()
        wire(_comp(FakeFeatures(pf=FakePatchFeatures())), handle_map={"E1": e1})
        res = sc.handler(boundary="E1", continuity="silky")
        assert res["isError"] is True
        assert "connected, tangent, curvature" in res["message"]

    def test_continuity_set_failure_surfaces_as_error(self, wire):
        # A continuity-set rejection must abort the patch and report the real failure, not be
        # dropped under safe() while the patch still reports success.
        e1 = _edge()
        pf = FakePatchFeaturesRejectContinuity(result_bodies=[_body("Patch1")])
        wire(_comp(FakeFeatures(pf=pf)), handle_map={"E1": e1})
        res = sc.handler(boundary="E1", continuity="tangent")
        assert res["isError"] is True
        assert "continuity rejected" in res["message"]

    def test_continuity_tangent_set_on_input(self, wire):
        # continuity resolves through SurfaceContinuityTypes - the PLURAL class is the one that
        # exists; a singular SurfaceContinuityType resolves to nothing, so the value the input
        # carries must come from the plural class or the patch runs on its default.
        e1 = _edge()
        pf = FakePatchFeatures(result_bodies=[_body("Patch1")])
        wire(_comp(FakeFeatures(pf=pf)), handle_map={"E1": e1})
        out = payload(sc.handler(boundary="E1", continuity="tangent"))
        assert out["continuity"] == "tangent"
        assert (pf.last_input.continuity
                is adsk.fusion.SurfaceContinuityTypes.TangentSurfaceContinuityType)

    def test_continuity_unavailable_member_is_refused_not_run_on_the_default(self, wire,
                                                                             monkeypatch):
        # an unresolvable continuity member must REFUSE. Reporting continuity=curvature while the
        # patch ran connected is the defect this path closes.
        e1 = _edge()
        monkeypatch.setattr(adsk.fusion, "SurfaceContinuityTypes", object())
        pf = FakePatchFeatures(result_bodies=[_body("Patch1")])
        wire(_comp(FakeFeatures(pf=pf)), handle_map={"E1": e1})
        res = sc.handler(boundary="E1", continuity="curvature")
        assert res["isError"] is True
        assert "continuity=curvature" in res["message"]
        assert "not available on this Fusion version" in res["message"]

    def test_continuity_that_does_not_take_is_refused(self, wire):
        # the input accepts the write and keeps its default: nothing raises, so only the read-back
        # catches it

        class _SwallowingPatchInput(FakePatchInput):
            def __setattr__(self, name, value):
                if name == "continuity":
                    object.__setattr__(self, "continuity", "connected-default")
                    return
                object.__setattr__(self, name, value)

        class _Feats(FakePatchFeatures):
            def createInput(self, boundary, op):
                self.last_input = _SwallowingPatchInput(boundary, op)
                return self.last_input

        e1 = _edge()
        pf = _Feats(result_bodies=[_body("Patch1")])
        wire(_comp(FakeFeatures(pf=pf)), handle_map={"E1": e1})
        res = sc.handler(boundary="E1", continuity="tangent")
        assert res["isError"] is True
        assert "continuity=tangent" in res["message"] and "reads back unchanged" in res["message"]

    def test_interior_rails_assigned_and_verified_by_count(self, wire):
        e1, r1, r2 = _edge(), _edge(), _edge()
        pf = FakePatchFeatures(result_bodies=[_body("Patch1")])
        wire(_comp(FakeFeatures(pf=pf)), handle_map={"E1": e1, "R1": r1, "R2": r2})
        out = payload(sc.handler(boundary="E1", interior_rails=["R1", "R2"]))
        # the published count is the one the INPUT reads back, not the number of handles asked for
        assert out["interior_rail_count"] == pf.last_input.interiorRailsAndPoints.count == 2
        # the read-back is a FRESH collection each time - identity can never be the check, so the
        # verification is the COUNT
        first, second = pf.last_input.interiorRailsAndPoints, pf.last_input.interiorRailsAndPoints
        assert first is not second
        assert list(first) == [r1, r2]

    def test_interior_rails_count_mismatch_is_refused(self, wire):
        # the input keeps only some of the assigned rails -> the patch would run without them
        e1, r1, r2 = _edge(), _edge(), _edge()
        pf = FakePatchFeatures(result_bodies=[_body("Patch1")], rails_dropped=1)
        wire(_comp(FakeFeatures(pf=pf)), handle_map={"E1": e1, "R1": r1, "R2": r2})
        res = sc.handler(boundary="E1", interior_rails=["R1", "R2"])
        assert res["isError"] is True
        assert "reads back 1 entity(ies) after assigning 2" in res["message"]

    def test_interior_rails_empty_readback_counts_as_zero_not_as_success(self, wire):
        # an EMPTY ObjectCollection reads count None (measured) - a None treated as "unreadable, so
        # assume it took" would pass a patch that dropped every rail
        e1, r1 = _edge(), _edge()
        pf = FakePatchFeatures(result_bodies=[_body("Patch1")], rails_dropped=1)
        wire(_comp(FakeFeatures(pf=pf)), handle_map={"E1": e1, "R1": r1})
        res = sc.handler(boundary="E1", interior_rails=["R1"])
        assert res["isError"] is True
        assert "reads back 0 entity(ies) after assigning 1" in res["message"]

    def test_interior_rails_rejected_with_the_multi_loop_form(self, wire):
        r1 = _edge()
        pf = FakePatchFeatures(result_bodies=[_body("P")])
        wire(_comp(FakeFeatures(pf=pf)), handle_map={"R0": _edge(), "R1": r1})
        res = sc.handler(boundaries=["R0"], interior_rails=["R1"])
        assert res["isError"] is True
        assert "interior_rails" in res["message"] and "boundary" in res["message"]
        assert pf.last_input is None      # refused BEFORE any patch was attempted

    def test_interior_rails_omitted_writes_nothing(self, wire):
        e1 = _edge()
        pf = FakePatchFeatures(result_bodies=[_body("Patch1")])
        wire(_comp(FakeFeatures(pf=pf)), handle_map={"E1": e1})
        out = payload(sc.handler(boundary="E1"))
        assert pf.last_input._rails == []
        assert "interior_rail_count" not in out

    def test_interior_rails_failure_names_both_causes_without_asserting_one(self, wire):
        e1, r1 = _edge(), _edge()
        pf = FakePatchFeatures(raises="ASM_BL_BAD_INPUT")
        wire(_comp(FakeFeatures(pf=pf)), handle_map={"E1": e1, "R1": r1})
        res = sc.handler(boundary="E1", interior_rails=["R1"])
        assert res["isError"] is True
        msg = res["message"]
        assert "two candidate causes" in msg and "asserts neither" in msg
        assert "Retry WITHOUT interior_rails" in msg

    def test_boundaries_all_fail_reports_zero_patched(self, wire):
        # every loop is a stale handle -> 0 patched, all failed, still a non-error multi report
        pf = FakePatchFeatures(result_bodies=[_body("P")])
        wire(_comp(FakeFeatures(pf=pf)), handle_map={})   # nothing resolves
        out = payload(sc.handler(boundaries=["R0", "R1"]))
        assert out["patched"] == 0 and out["requested"] == 2 and out["failed"] == 2
        assert out["result_bodies"] == []
        assert "Some loops failed" in out["note"]

    def test_boundaries_accepts_composite_handles_without_comma_shredding(self, wire):
        # A find_geometry handle is COMPOSITE ('<token>|@<kind>:x,y,z') - its locator carries commas.
        # The plural 'boundaries' path passes each element as a bare STRING, so a naive comma-split
        # shreds ONE handle into broken fragments ('2.000000','3.000000') that resolve as stale -
        # 'boundaries' must accept the same fresh handles the singular 'boundary' accepts.
        # Each composite handle must resolve as ONE edge.
        e0, e1 = _edge(), _edge()
        pf = FakePatchFeatures(result_bodies=[_body("P")])
        wire(_comp(FakeFeatures(pf=pf)), handle_map={"TOK0": e0, "TOK1": e1})
        h0 = "TOK0|@circular_edge:1.000000,2.000000,3.000000"
        h1 = "TOK1|@circular_edge:4.000000,5.000000,6.000000"
        out = payload(sc.handler(boundaries=[h0, h1]))
        assert out["patched"] == 2 and out["failed"] == 0

    def test_patch_is_solid_is_read_back_not_hardcoded_false(self, wire):
        # is_solid is read off the patch body, never the module's expectation that a patch is a
        # surface: a literal False here would report an open sheet for a body reading solid
        e1 = _edge()
        pf = FakePatchFeatures(result_bodies=[_body("Patch1", is_solid=True)])
        wire(_comp(FakeFeatures(pf=pf)), handle_map={"E1": e1})
        out = payload(sc.handler(boundary="E1"))
        assert out["is_solid"] is True
        assert "SOLID" in out["note"]
        assert "isSolid=false" not in out["note"]

    def test_patch_unreadable_is_solid_is_null_and_unverified(self, wire):
        e1 = _edge()
        pf = FakePatchFeatures(result_bodies=[_body("Patch1", solid_readable=False)])
        wire(_comp(FakeFeatures(pf=pf)), handle_map={"E1": e1})
        out = payload(sc.handler(boundary="E1"))
        assert out["is_solid"] is None
        assert out["unverified"] == ["is_solid"]
        assert "UNVERIFIED" in out["note"]

    def test_patch_with_no_result_body_is_an_error(self, wire):
        # 'patched: true' beside result_bodies [] claims a fill the payload cannot show
        e1 = _edge()
        pf = FakePatchFeatures(result_bodies=[])
        wire(_comp(FakeFeatures(pf=pf)), handle_map={"E1": e1})
        res = sc.handler(boundary="E1")
        assert res["isError"] is True
        assert "owns no result body" in res["message"]

    def test_one_patch_body_is_the_boundary_that_passes(self, wire):
        e1 = _edge()
        pf = FakePatchFeatures(result_bodies=[_body("Patch1")])
        wire(_comp(FakeFeatures(pf=pf)), handle_map={"E1": e1})
        out = payload(sc.handler(boundary="E1"))
        assert out["result_bodies"] == ["Patch1"]

    def test_multi_loop_bodyless_patch_is_a_per_loop_failure(self, wire):
        pf = FakePatchFeatures(result_bodies=[])
        wire(_comp(FakeFeatures(pf=pf)), handle_map={"R0": _edge(), "R1": _edge()})
        out = payload(sc.handler(boundaries=["R0", "R1"]))
        assert out["patched"] == 0 and out["failed"] == 2
        assert "owns no result body" in out["errors"][0]["error"]

    def test_multi_loop_is_solid_is_the_aggregate_read_back(self, wire):
        pf = FakePatchFeatures(result_bodies=[_body("P")])
        wire(_comp(FakeFeatures(pf=pf)), handle_map={"R0": _edge(), "R1": _edge()})
        out = payload(sc.handler(boundaries=["R0", "R1"]))
        assert out["is_solid"] is False
        assert [p["is_solid"] for p in out["patches"]] == [False, False]

    def test_multi_loop_solid_result_is_not_reported_as_a_surface(self, wire):
        # the aggregate is read off the patch bodies - a literal False here would call a body
        # reading isSolid=true an open surface
        pf = FakePatchFeatures(result_bodies=[_body("P", is_solid=True)])
        wire(_comp(FakeFeatures(pf=pf)), handle_map={"R0": _edge(), "R1": _edge()})
        out = payload(sc.handler(boundaries=["R0", "R1"]))
        assert out["is_solid"] is True
        assert "isSolid=false" not in out["note"]

    def test_multi_loop_unreadable_is_solid_is_null_and_unverified(self, wire):
        pf = FakePatchFeatures(result_bodies=[_body("P", solid_readable=False)])
        wire(_comp(FakeFeatures(pf=pf)), handle_map={"R0": _edge(), "R1": _edge()})
        out = payload(sc.handler(boundaries=["R0", "R1"]))
        assert out["is_solid"] is None
        assert out["unverified"] == ["is_solid"]

    def test_singular_boundary_composite_handle_string_not_shredded(self, wire):
        # A lone composite handle passed as a STRING resolves to its ONE edge
        # (the singular param's schema is an array, but a raw string must not be comma-shredded either).
        e0 = _edge()
        pf = FakePatchFeatures(result_bodies=[_body("P")])
        wire(_comp(FakeFeatures(pf=pf)), handle_map={"TOK0": e0})
        h0 = "TOK0|@circular_edge:1.000000,2.000000,3.000000"
        out = payload(sc.handler(boundary=h0))
        assert out["patched"] is True
        assert pf.last_input.boundary is e0     # the ONE edge, not a shredded fragment
