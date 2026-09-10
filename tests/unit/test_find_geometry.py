"""Unit tests for ``find_geometry.py`` — query geometry, return stable handles.

This is the QUERY half of geometry-as-values: it must return each match's handle (entityToken),
kind, position, and shape data, and filter by kind / radius / nearest_to. Pinned here (no live
Fusion): the units scaling on positions/radii, the kind filter, the radius filter (5% tol), the
nearest_to sort, that every match carries a handle, the omit-when-default visibility signal
(a hidden body's matches carry hidden:true; visible bodies' records omit the key), and that a body
resolved inside a component is scanned through its occurrence PROXY, so the positions reported are
world and not component-local.
"""

import json

import adsk.core

from conftest import Circle3D, MakeDesign, MeshBody, load_tool, _NamedCollection

fg = load_tool("find_geometry")

_SURFACES = adsk.core.SurfaceTypes
_CURVES = adsk.core.Curve3DTypes


# ── fakes mimicking adsk BRep faces/edges ───────────────────────────────────

class _Pt:
    def __init__(self, x, y, z):
        self.x = x; self.y = y; self.z = z


class _CylGeo:
    def __init__(self, r, axis=(1, 0, 0)):
        self.surfaceType = _SURFACES.CylinderSurfaceType
        self.radius = r
        self.axis = _Pt(*axis)


class _PlaneGeo:
    """A planar face's surface. With `origin`, it also carries the adsk.core.Plane frame
    find_geometry casts to (origin + uDirection/vDirection/normal); without, those reads fail and
    the record's 'frame' must degrade to null."""
    surfaceType = _SURFACES.PlaneSurfaceType

    def __init__(self, origin=None, u=None, v=None, normal=None):
        if origin is not None:
            self.origin = _Pt(*origin)
            self.uDirection = _Pt(*u)
            self.vDirection = _Pt(*v)
            self.normal = _Pt(*normal)


class _Eval:
    """Stands in for a BRepFace SurfaceEvaluator. getNormalAtPoint returns (success, normal) - the
    Python shape of a bool-return + output-normal API. raises=True mimics an off-surface sample point."""
    def __init__(self, normal=None, raises=False):
        self._normal = normal
        self._raises = raises

    def getNormalAtPoint(self, point):
        if self._raises:
            raise RuntimeError("point is off the face surface")
        return (True, _Pt(*self._normal))


class _LineGeo:
    def __init__(self, start, end):
        self.curveType = _CURVES.Line3DCurveType
        self.startPoint = _Pt(*start)
        self.endPoint = _Pt(*end)


class _EllipseGeo:
    """An ellipse edge's curve. Ellipse3D has no live shape dump, so this is a local double."""
    def __init__(self, major, minor, center):
        self.curveType = _CURVES.Ellipse3DCurveType
        self.majorRadius = major
        self.minorRadius = minor
        self.center = _Pt(*center)


class _EllipticalArcGeo:
    """A partial ellipse's curve. EllipticalArc3D has no live shape dump - a local double."""
    def __init__(self, major, minor, center):
        self.curveType = _CURVES.EllipticalArc3DCurveType
        self.majorRadius = major
        self.minorRadius = minor
        self.center = _Pt(*center)


class _NurbsGeo:
    """A spline edge's curve. NurbsCurve3D has no live shape dump - a local double."""
    curveType = _CURVES.NurbsCurve3DCurveType


class _PolylineGeo:
    """A curve type find_geometry gives no label of its own. Polyline3D has no dump - a double."""
    curveType = _CURVES.Polyline3DCurveType


class FakeEdge:
    def __init__(self, token, geo, point_on_edge, length=5.0):
        self.entityToken = token
        self.geometry = geo
        self.pointOnEdge = _Pt(*point_on_edge)
        self.length = length


class FakeFace:
    def __init__(self, token, geo, centroid, area=10.0, evaluator=None):
        self.entityToken = token
        self.geometry = geo
        self.centroid = _Pt(*centroid)
        self.area = area
        # None => _face_normal degrades to no 'normal' field (same as a face lacking an evaluator).
        self.evaluator = evaluator


class FakeBody:
    """A BRep body. `name`/`token` matter to the by-name target path (the shared body resolver walks
    every occurrence's bRepBodies by name and de-duplicates on entityToken); `parentComponent` and
    `assemblyContext` are what a candidate's '<occurrence-or-component>:<body>' label is built from.

    `native` makes this wrapper a PROXY of that body: a body and its occurrence proxy carry DIFFERENT
    entityTokens while nativeObject reads None on a native and the native on a proxy, which is how
    _common.native_token collapses the two wrappers to ONE physical body."""
    def __init__(self, faces=(), edges=(), vertices=(), visible=True, name="Body1", token=None,
                 native=None):
        self.faces = list(faces)
        self.edges = list(edges)
        self.vertices = list(vertices)
        # BRepBody.isVisible is the EFFECTIVE state (own bulb AND ancestor occurrence bulbs rolled up).
        self.isVisible = visible
        self.name = name
        self.entityToken = token or f"BTOK::{name}::{id(self)}"
        self.parentComponent = None
        self.assemblyContext = None
        self.nativeObject = native


class FakeComp:
    """A COMPONENT - the NATIVE home of its bodies, whose geometry is in component-LOCAL coordinates.
    An occurrence of the component hands back PROXY bodies, whose geometry is in WORLD coordinates."""
    def __init__(self, name, bodies=()):
        self.name = name
        self.bRepBodies = _NamedCollection(bodies)
        self.meshBodies = _NamedCollection([])
        for b in bodies:
            b.parentComponent = self


class FakeOcc:
    def __init__(self, name, comp, bodies, full_path=None):
        self.name = name
        # fullPathName is the unambiguous key; defaults to name for flat (single-level) assemblies.
        self.fullPathName = full_path or name
        # `comp` is a component NAME for the flat cases, or a FakeComp when the test also needs the
        # component's own (native, component-local) bodies to be reachable.
        self.component = comp if not isinstance(comp, str) else type("C", (), {"name": comp})()
        # Counted+named collection (the live protocol): find_geometry iterates it, the by-name body
        # resolver reads count/item/itemByName off the same object.
        self.bRepBodies = _NamedCollection(bodies)
        for b in bodies:
            b.parentComponent = self.component
            b.assemblyContext = self


class FakeRoot:
    def __init__(self, occs, root_bodies=(), all_occs=None, meshes=()):
        # `occurrences` is the TOP-LEVEL collection; `allOccurrences` is the FLATTENED, recursive list.
        # For a flat assembly they're equal; nested tests pass all_occs ⊋ occs so a top-level-only scan
        # genuinely can't reach the nested occurrence (that's what makes the recursion test bite).
        self.name = "Root"
        self.occurrences = _NamedCollection(occs)
        self.allOccurrences = list(all_occs) if all_occs is not None else list(occs)
        self.bRepBodies = _NamedCollection(root_bodies)
        # Meshes are reached through the COMPONENTS (reading meshBodies off an occurrence raises).
        self.meshBodies = _NamedCollection(meshes)
        for b in root_bodies:
            b.parentComponent = self


class FakeDesign(MakeDesign):
    """The shared design over this file's root. activeComponent is the edit target the body walk
    also scans - None makes it the root, as live."""
    def __init__(self, occs, root_bodies=(), all_occs=None, meshes=(), active=None):
        super().__init__(comp=FakeRoot(occs, root_bodies, all_occs, meshes))
        self.activeComponent = active


import pytest


@pytest.fixture(autouse=True)
def _plane_cast(monkeypatch):
    """adsk.core.Plane.cast(surface) hands back a planar surface and None for anything else. The
    stock mock's cast returns a truthy child Mock, which would present a cylinder as a plane."""
    monkeypatch.setattr(
        adsk.core.Plane, "cast",
        lambda g: g if getattr(g, "surfaceType", None) == _SURFACES.PlaneSurfaceType else None,
        raising=False)


def _install(occs, root_bodies=(), all_occs=None, meshes=(), active=None):
    design = FakeDesign(occs, root_bodies, all_occs, meshes, active)
    fg.app = type("A", (), {"activeProduct": design})()
    fg._common.app = fg.app
    import adsk.fusion
    adsk.fusion.Design.cast = lambda x: x if isinstance(x, FakeDesign) else None
    # The by-name body path resolves through _inputs, which reads the design off the SAME _common
    # module object - and discriminates mesh from BRep by isinstance, so both types are modelled.
    adsk.fusion.BRepBody = FakeBody
    adsk.fusion.MeshBody = MeshBody
    return design


def _payload(result):
    assert result["isError"] is False, result
    return json.loads(result["content"][0]["text"])


_WALK_RAISE = "2 : InternalValidationError : occ"


class _RaisingColl:
    """A collection whose COUNT itself raises - unreadable, not empty."""
    @property
    def count(self):
        raise RuntimeError(_WALK_RAISE)

    def item(self, i):
        raise RuntimeError(_WALK_RAISE)

    def __iter__(self):
        raise RuntimeError(_WALK_RAISE)


class _BlindRoot:
    """The measured shape of a design holding an unresolved external reference: reading
    root.allOccurrences RAISES, and here the component-local fallback will not enumerate either -
    so NEITHER walk answers and the occurrence tree is unknown, not empty."""
    name = "Root"

    def __init__(self, root_bodies=()):
        self.bRepBodies = _NamedCollection(list(root_bodies))
        self.meshBodies = _NamedCollection([])
        self.occurrences = _RaisingColl()
        for b in root_bodies:
            b.parentComponent = self

    @property
    def allOccurrences(self):
        raise RuntimeError(_WALK_RAISE)


class _BrokenOcc:
    """An occurrence whose external reference does not resolve: reading its component RAISES (the
    ONE detector), while its name still reads - the only identity a caller can be given."""
    name = "Ghost:1"

    @property
    def component(self):
        raise RuntimeError("3 : The occurrence's referenced component is unavailable (broken or "
                           "missing external reference).")


def _install_blind(root_bodies=()):
    """An installed design whose occurrence census cannot be taken at all."""
    design = _install([], root_bodies=root_bodies)
    design.rootComponent = _BlindRoot(root_bodies)
    return design


def _cyl(token, r, centroid, axis=(1, 0, 0)):
    return FakeFace(token, _CylGeo(r, axis), centroid)


def _plane(token, centroid):
    return FakeFace(token, _PlaneGeo(), centroid)


def _circle(radius, center=(0, 0, 0)):
    return Circle3D(None, _Pt(*center), radius)


def _blind_radius_circle(center=(0, 0, 0)):
    """A circular edge's curve whose radius read RAISES - the shape measured() answers None for, so
    the record carries radius None."""
    g = _circle(1.0, center)
    del g.radius
    return g


class TestGuards:
    def test_unknown_units(self):
        _install([FakeOcc("P:1", "P", [FakeBody()])])
        res = fg.handler(units="furlong")
        assert res["isError"] is True and "Unknown units" in res["message"]

    def test_unresolved_target(self):
        _install([FakeOcc("P:1", "P", [FakeBody()])])
        res = fg.handler(target="Nope")
        assert res["isError"] is True and "Could not resolve target" in res["message"]


class TestFind:
    def test_returns_handles_and_scaled_positions(self):
        # cylinder at world (2.45,2,0)cm -> reported in mm
        f = _cyl("TOK_PIN", 0.8, (2.45, 2.0, 0.0))
        _install([FakeOcc("Crank:1", "Crank", [FakeBody(faces=[f])])])
        out = _payload(fg.handler(target="Crank:1", units="mm"))
        m = out["matches"][0]
        # The handle is the SELF-HEALING composite '<token>|@<kind>:<x>,<y>,<z>' (locator in cm, the
        # API unit) — the token is the fast path, the '@' locator the stale-token fallback.
        assert m["handle"].startswith("TOK_PIN|@")
        assert m["handle"] == "TOK_PIN|@cylinder_face:2.450000,2.000000,0.000000"
        assert m["kind"] == "cylinder_face"
        assert m["position"] == [24.5, 20.0, 0.0]      # cm -> mm
        assert m["radius"] == 8.0                        # 0.8cm -> 8mm
        # The declared output contract holds: the handler actually mints the 'handle' RETURNS declares.
        assert fg.RETURNS[0].assert_present(out) == ""

    def test_the_result_note_does_not_repeat_the_produces_block(self):
        # the block is a standing contract the DESCRIPTION already carries; repeating it in the
        # note re-sends it on every call, and this read is called many times per session.
        _install([FakeOcc("Crank:1", "Crank", [FakeBody(faces=[_cyl("C", 0.8, (0, 0, 0))])])])
        note = _payload(fg.handler(target="Crank:1"))["note"]
        assert fg._outputs.produces_block(fg.RETURNS) not in note
        assert "hidden:true" in note and "frame.origin" in note   # the per-result facts stay

    def test_kind_filter_cylinder_only(self):
        body = FakeBody(faces=[_cyl("C", 0.8, (0, 0, 0)), _plane("P", (1, 0, 0))])
        _install([FakeOcc("X:1", "X", [body])])
        out = _payload(fg.handler(target="X:1", kind="cylinder_face"))
        kinds = {m["kind"] for m in out["matches"]}
        assert kinds == {"cylinder_face"}

    def test_radius_filter(self):
        body = FakeBody(faces=[_cyl("PIN", 0.8, (0, 0, 0)), _cyl("JRN", 1.0, (1, 0, 0))])
        _install([FakeOcc("X:1", "X", [body])])
        out = _payload(fg.handler(target="X:1", kind="cylinder_face", radius=8, units="mm"))
        # only the r8mm pin; handle is the composite '<token>|@...'
        assert out["returned"] == 1 and out["matches"][0]["handle"].startswith("PIN|@")

    def test_a_record_whose_radius_did_not_read_is_skipped_by_the_radius_filter(self):
        # measured() answers None for a radius the curve would not give, and subtracting from that
        # raises inside the handler. An edge whose radius never read cannot match one either.
        blind = FakeEdge("BLIND", _blind_radius_circle(), (1.0, 0, 0))
        good = FakeEdge("R8", _circle(0.8), (2.0, 0, 0))
        _install([FakeOcc("X:1", "X", [FakeBody(edges=[blind, good])])])
        out = _payload(fg.handler(target="X:1", radius=8, units="mm"))
        assert out["returned"] == 1 and out["matches"][0]["handle"].startswith("R8|@")

    def test_nearest_to_sorts(self):
        # faces at world 10cm (FAR) and 1cm (NEAR); nearest_to is in mm.
        body = FakeBody(faces=[_cyl("FAR", 0.8, (10, 0, 0)), _cyl("NEAR", 0.8, (1, 0, 0))])
        _install([FakeOcc("X:1", "X", [body])])
        # nearest_to=[10,0,0]mm = 1cm -> NEAR (at 1cm) is closest
        out = _payload(fg.handler(target="X:1", nearest_to=[10, 0, 0], units="mm"))
        assert out["matches"][0]["handle"].startswith("NEAR|@")
        # nearest_to=[100,0,0]mm = 10cm -> FAR (at 10cm) is closest
        out2 = _payload(fg.handler(target="X:1", nearest_to=[100, 0, 0], units="mm"))
        assert out2["matches"][0]["handle"].startswith("FAR|@")

    def test_every_match_has_a_handle(self):
        body = FakeBody(faces=[_cyl("A", 0.8, (0, 0, 0)), _plane("B", (1, 0, 0))])
        _install([FakeOcc("X:1", "X", [body])])
        out = _payload(fg.handler(target="X:1"))
        assert all(m.get("handle") for m in out["matches"])

    def test_hidden_body_matches_carry_hidden_true(self):
        # Omit-when-default visibility signal: every match from a body whose isVisible is False (own
        # bulb OR a hidden ancestor - isVisible rolls both up) carries hidden:true, so an agent that
        # hid a body before a trim/cut can CONFIRM it.
        hidden_body = FakeBody(faces=[_cyl("HID", 0.8, (0, 0, 0))],
                               edges=[FakeEdge("HE", _LineGeo((0, 0, 0), (1, 0, 0)), (0.5, 0, 0))],
                               visible=False)
        _install([FakeOcc("X:1", "X", [hidden_body])])
        out = _payload(fg.handler(target="X:1"))
        assert out["matches"], "expected matches from the hidden body"
        assert all(m["hidden"] is True for m in out["matches"])

    def test_visible_body_matches_omit_hidden_field(self):
        # The default (visible) case stays byte-identical: no 'hidden' key at all.
        body = FakeBody(faces=[_cyl("VIS", 0.8, (0, 0, 0))])
        _install([FakeOcc("X:1", "X", [body])])
        out = _payload(fg.handler(target="X:1"))
        assert all("hidden" not in m for m in out["matches"])

    def test_mixed_visibility_flags_only_the_hidden_bodys_matches(self):
        shown = FakeBody(faces=[_cyl("SHOWN", 0.8, (0, 0, 0))])
        hidden = FakeBody(faces=[_cyl("HIDDEN", 0.8, (1, 0, 0))], visible=False)
        _install([FakeOcc("X:1", "X", [shown, hidden])])
        out = _payload(fg.handler(target="X:1"))
        flags = {m["handle"].split("|@")[0]: m.get("hidden") for m in out["matches"]}
        assert flags == {"SHOWN": None, "HIDDEN": True}


# ── the search space is disclosed, so a HOLE in it is never published as a complete result ──────
#
# root.allOccurrences RAISES on a design holding an unresolved external reference (measured), and
# swallowing that raise into an empty list turns "the assembly could not be read" into "the design
# has no occurrences" - a root-bodies-only scan the payload would otherwise present as the whole
# design. The census the scan ran over is published beside every result.

class TestSearchSpaceDisclosure:
    def test_a_clean_walk_publishes_the_fast_method_and_adds_no_caveat(self):
        _install([FakeOcc("X:1", "X", [FakeBody(faces=[_plane("F", (0, 0, 0))])])])
        out = _payload(fg.handler())
        assert out["occurrences_walk"] == "allOccurrences"
        assert "not a complete set" not in out["note"]

    def test_an_unreadable_census_is_disclosed_not_published_as_the_whole_design(self):
        # THE row: the scan falls back to root-level bodies, and without the marker that reads as a
        # single-body design rather than an assembly nothing could be enumerated from.
        _install_blind(root_bodies=[FakeBody(faces=[_plane("ROOT_FACE", (0, 0, 0))])])
        out = _payload(fg.handler())
        assert out["returned"] == 1                       # the root body was still scanned
        assert out["occurrences_walk"] == "unreadable"
        assert "not a complete set" in out["note"]
        assert "not an empty design" in out["note"]

    def test_a_MISS_carries_the_same_disclosure(self):
        # "no such target" is a claim about the design; a census that did not enumerate cannot
        # support it, so the refusal says which read failed rather than blaming the name.
        _install_blind()
        res = fg.handler(target="Bracket:1")
        assert res["isError"] is True
        assert "occurrences_walk='unreadable'" in res["message"]

    def test_the_recursed_fallback_is_named(self):
        # allOccurrences raised but the component.occurrences recursion answered - the rows are real
        # and the payload says which walk produced them.
        occ = FakeOcc("X:1", "X", [FakeBody(faces=[_plane("F", (0, 0, 0))])])
        occ.component.occurrences = _NamedCollection([])
        occ.childOccurrences = _NamedCollection([])
        design = _install([occ])
        root = design.rootComponent
        design.rootComponent = type("R", (), {
            "name": "Root",
            "occurrences": _NamedCollection([occ]),
            "bRepBodies": root.bRepBodies,
            "meshBodies": root.meshBodies,
            "allOccurrences": property(lambda self: (_ for _ in ()).throw(
                RuntimeError(_WALK_RAISE))),
        })()
        out = _payload(fg.handler())
        assert out["occurrences_walk"] == "recursed"
        assert "recursed" in out["note"] and out["returned"] == 1

    def test_an_unresolved_reference_is_NAMED_as_geometry_that_was_not_scanned(self):
        # Its component raises, so it carries no geometry to scan - and saying nothing would let a
        # short match list read as "that part has no such face".
        good = FakeOcc("X:1", "X", [FakeBody(faces=[_plane("F", (0, 0, 0))])])
        _install([good], all_occs=[good, _BrokenOcc()])
        out = _payload(fg.handler())
        assert out["returned"] == 1
        assert "'Ghost:1'" in out["note"] and "unresolved external reference" in out["note"]

    def test_a_walk_that_stopped_short_says_part_of_the_design_was_not_scanned(self):
        # complete=False: the rows are real but they are not all of them, so the caveat is about
        # what was NOT reached rather than about which collection answered.
        occ = FakeOcc("X:1", "X", [FakeBody(faces=[_plane("F", (0, 0, 0))])])
        occ.component.occurrences = _NamedCollection([])
        occ.childOccurrences = _RaisingColl()          # the subtree under it will not enumerate
        design = _install([occ])
        root = design.rootComponent
        design.rootComponent = type("R", (), {
            "name": "Root",
            "occurrences": _NamedCollection([occ]),
            "bRepBodies": root.bRepBodies,
            "meshBodies": root.meshBodies,
            "allOccurrences": property(lambda self: (_ for _ in ()).throw(
                RuntimeError(_WALK_RAISE))),
        })()
        out = _payload(fg.handler())
        assert out["occurrences_walk"] == "recursed"
        assert "did not run to the end" in out["note"]


# ── NESTED sub-assembly reach (scan allOccurrences, target by fullPathName) ─────────────────────
# find_geometry must scan allOccurrences (not just root.occurrences), so a nested occurrence that
# design_get(tree)/assembly_get report by fullPathName resolves here too, agreeing with the
# self-heal path (_refind_by_locator also scans allOccurrences).

class TestNestedAssembly:
    def test_nested_occurrence_resolved_by_full_path(self):
        # A bracket nested under a sub-assembly: it is NOT in the TOP-LEVEL occurrences, only in the
        # flattened allOccurrences. Targetable by fullPathName. (A top-level-only scan can't see it —
        # that's what this pins.)
        f = _cyl("NESTED_PIN", 0.8, (1.0, 0.0, 0.0))
        parent = FakeOcc("Frame:1", "Frame", [], full_path="Frame:1")
        nested = FakeOcc("Bracket:1", "Bracket", [FakeBody(faces=[f])],
                         full_path="Frame:1/Bracket:1")
        # top-level = [parent] only; allOccurrences = [parent, nested]
        _install([parent], all_occs=[parent, nested])
        out = _payload(fg.handler(target="Frame:1/Bracket:1", units="mm"))
        assert out["returned"] == 1
        assert out["matches"][0]["handle"].startswith("NESTED_PIN|@")

    def test_nested_also_reachable_by_local_name(self):
        f = _cyl("PIN", 0.8, (0, 0, 0))
        parent = FakeOcc("Frame:1", "Frame", [], full_path="Frame:1")
        nested = FakeOcc("Bracket:1", "Bracket", [FakeBody(faces=[f])],
                         full_path="Frame:1/Bracket:1")
        _install([parent], all_occs=[parent, nested])
        out = _payload(fg.handler(target="Bracket:1"))
        assert out["returned"] == 1

    def test_targeting_a_wrapper_includes_its_nested_subtree(self):
        # An inserted xref/derive wraps its geometry one level down: the wrapper occurrence has
        # NO direct bodies; its nested child holds them (real fullPathNames separate with '+').
        # Targeting the wrapper must scan the whole subtree - a direct-bodies-only scan reads 0
        # faces on a wrapper whose child holds them all (live-verified).
        f = _cyl("DEEP", 0.8, (0, 0, 0))
        top = FakeOcc("Model:1", "Model", [], full_path="Model:1")
        wrapper = FakeOcc("P5:1", "P5", [], full_path="Model:1+P5:1")
        child = FakeOcc("Ring:1", "Ring", [FakeBody(faces=[f])],
                        full_path="Model:1+P5:1+Ring:1")
        _install([top], all_occs=[top, wrapper, child])
        out = _payload(fg.handler(target="Model:1+P5:1", units="mm"))
        assert out["returned"] == 1
        assert out["matches"][0]["handle"].startswith("DEEP|@")

    def test_whole_design_includes_nested_and_root_bodies(self):
        # whole-design scan reaches BOTH a root-level body (occurrence None) AND a NESTED occurrence's
        # body — the self-heal path scans both, so the initial query must too.
        root_face = _plane("ROOT_FACE", (0, 0, 0))
        nested_face = _cyl("NESTED", 0.8, (5, 0, 0))
        parent = FakeOcc("Frame:1", "Frame", [], full_path="Frame:1")
        nested = FakeOcc("Bracket:1", "Bracket", [FakeBody(faces=[nested_face])],
                         full_path="Frame:1/Bracket:1")
        _install([parent], all_occs=[parent, nested],
                 root_bodies=[FakeBody(faces=[root_face])])
        out = _payload(fg.handler())
        handles = {m["handle"].split("|@")[0] for m in out["matches"]}
        assert "ROOT_FACE" in handles and "NESTED" in handles


# ── BODY-NAME targets: a body inside ANY component, ambiguity refused, qualified form accepted ──
# 'target' takes a BODY name, and a body almost never lives at the root: the by-name path walks every
# occurrence's bodies, so 'Pin' inside Frame:1 resolves. A body name is only LOCALLY unique, so a name
# two components answer to is REFUSED with each candidate written as '<occurrence-or-component>:<body>'
# - the form that then picks exactly one.

def _named_body(name, token, face_token):
    return FakeBody(faces=[_cyl(face_token, 0.8, (0, 0, 0))], name=name, token=token)


class TestBodyNameTargets:
    def test_body_inside_a_component_resolves_by_name(self):
        pin = _named_body("Pin", "TOK_A", "PIN_FACE")
        _install([FakeOcc("Frame:1", "Frame", [pin])])
        out = _payload(fg.handler(target="Pin"))
        assert out["returned"] == 1
        assert out["matches"][0]["handle"].startswith("PIN_FACE|@")
        # the label names the body that RESOLVED, in the form that resolves back - not the raw input
        assert out["target"] == "body 'Frame:1:Pin'"

    def test_the_label_echoes_the_resolved_body_not_the_raw_input(self):
        # a mis-cased bare name and the qualified form both report the ONE body they reached
        pin = _named_body("Pin", "TOK_A", "PIN_FACE")
        _install([FakeOcc("Frame:1", "Frame", [pin])])
        assert _payload(fg.handler(target="pin"))["target"] == "body 'Frame:1:Pin'"
        assert _payload(fg.handler(target="Frame:Pin"))["target"] == "body 'Frame:1:Pin'"

    def test_root_level_body_still_resolves_by_name(self):
        base = _named_body("Base", "TOK_ROOT", "BASE_FACE")
        _install([], root_bodies=[base])
        out = _payload(fg.handler(target="Base"))
        assert out["returned"] == 1 and out["matches"][0]["handle"].startswith("BASE_FACE|@")

    def test_body_name_is_matched_case_insensitively(self):
        pin = _named_body("Pin", "TOK_A", "PIN_FACE")
        _install([FakeOcc("Frame:1", "Frame", [pin])])
        out = _payload(fg.handler(target="pin"))
        assert out["returned"] == 1 and out["matches"][0]["handle"].startswith("PIN_FACE|@")

    def test_name_in_several_components_is_refused_naming_each(self):
        a = _named_body("Pin", "TOK_A", "A_FACE")
        b = _named_body("Pin", "TOK_B", "B_FACE")
        _install([FakeOcc("A:1", "A", [a]), FakeOcc("B:1", "B", [b])])
        res = fg.handler(target="Pin")
        assert res["isError"] is True
        assert "ambiguous" in res["message"].lower()
        assert "A:1:Pin" in res["message"] and "B:1:Pin" in res["message"]

    def test_qualified_occurrence_body_form_picks_one(self):
        a = _named_body("Pin", "TOK_A", "A_FACE")
        b = _named_body("Pin", "TOK_B", "B_FACE")
        _install([FakeOcc("A:1", "A", [a]), FakeOcc("B:1", "B", [b])])
        out = _payload(fg.handler(target="B:1:Pin"))
        assert out["returned"] == 1 and out["matches"][0]["handle"].startswith("B_FACE|@")

    def test_qualified_component_body_form_picks_one(self):
        # the component-name prefix (no instance suffix) is accepted too, when it picks exactly one
        a = _named_body("Pin", "TOK_A", "A_FACE")
        b = _named_body("Pin", "TOK_B", "B_FACE")
        _install([FakeOcc("A:1", "A", [a]), FakeOcc("B:1", "B", [b])])
        out = _payload(fg.handler(target="A:Pin"))
        assert out["returned"] == 1 and out["matches"][0]["handle"].startswith("A_FACE|@")

    def test_qualified_form_still_matching_two_instances_is_refused(self):
        # one component instanced twice: '<component>:<body>' names BOTH instances' bodies - refuse,
        # listing the per-instance forms that separate them.
        a = _named_body("Pin", "TOK_A", "A_FACE")
        b = _named_body("Pin", "TOK_B", "B_FACE")
        _install([FakeOcc("Jaw:1", "Jaw", [a]), FakeOcc("Jaw:2", "Jaw", [b])])
        res = fg.handler(target="Jaw:Pin")
        assert res["isError"] is True and "ambiguous" in res["message"].lower()
        assert "Jaw:1:Pin" in res["message"] and "Jaw:2:Pin" in res["message"]

    def test_a_mistyped_body_in_a_real_scope_is_refused_naming_what_it_holds(self):
        # A typo in the qualified form must not fall through to "that component's single body" - the
        # caller would silently get a body they never named. The scope resolved, so say what it holds.
        pin = _named_body("Pin", "TOK_A", "PIN_FACE")
        frame = FakeComp("Frame", [pin])
        design = _install([FakeOcc("Frame:1", frame, [pin])])
        design._all_components.append(frame)
        res = fg.handler(target="Frame:Pinn")
        assert res["isError"] is True
        assert "holds no body named 'Pinn'" in res["message"] and "'Pin'" in res["message"]

    def test_a_mesh_body_name_is_refused_with_a_pointer(self):
        # a MeshBody has no BRep faces/edges/vertices to hand back - say so instead of reporting an
        # empty scan. Meshes are reached through the components (an occurrence's meshBodies raises).
        _install([], meshes=[MeshBody(name="Scan1")])
        res = fg.handler(target="Scan1")
        assert res["isError"] is True
        assert "MESH" in res["message"] and "mesh_get" in res["message"]


# ── WORLD SPACE: a body inside a component is scanned through its occurrence PROXY ──────────────
# find_geometry publishes WORLD positions. A native body's geometry is component-LOCAL, so a body
# resolved inside an occurrence must be scanned through the PROXY the occurrence hands back - the
# native and the proxy are one physical body (same nativeObject token) reachable two ways, and the
# placed wrapper is the one carrying the world placement.

class TestWorldSpaceProxy:
    def _placed(self, local, world, active=False):
        """One physical body 'Pin' reachable two ways: NATIVE in component Frame (geometry at `local`)
        and as occurrence Frame:1's PROXY (geometry at `world`). `active` activates the component, so
        the walk reaches the native too and the placed-wrapper choice genuinely has to be made."""
        native = FakeBody(faces=[_cyl("LOCAL_FACE", 0.8, local)], name="Pin", token="NATIVE_TOK")
        proxy = FakeBody(faces=[_cyl("WORLD_FACE", 0.8, world)], name="Pin", token="PROXY_TOK",
                         native=native)
        comp = FakeComp("Frame", [native])
        occ = FakeOcc("Frame:1", comp, [proxy], full_path="Frame:1")
        _install([occ], active=comp if active else None)

    def test_bare_name_reports_world_position_not_component_local(self):
        # the component is ACTIVE, so both wrappers are in the walk: scanning the native would
        # publish 10mm (component-local) as if it were a world position.
        self._placed(local=(1, 0, 0), world=(11, 0, 0), active=True)
        out = _payload(fg.handler(target="Pin", units="mm"))
        assert out["returned"] == 1
        m = out["matches"][0]
        assert m["handle"].startswith("WORLD_FACE|@")
        assert m["position"] == [110.0, 0.0, 0.0]
        # one physical body reached twice is ONE candidate, never an ambiguity with itself
        assert out["target"] == "body 'Frame:1:Pin'"

    def test_world_position_holds_when_the_component_is_not_active(self):
        # the ordinary case: only the occurrence pass reaches the body, and it hands back the proxy.
        self._placed(local=(1, 0, 0), world=(11, 0, 0))
        out = _payload(fg.handler(target="Pin", units="mm"))
        assert out["matches"][0]["position"] == [110.0, 0.0, 0.0]

    def test_qualified_form_also_lands_on_the_proxy(self):
        self._placed(local=(1, 0, 0), world=(11, 0, 0), active=True)
        out = _payload(fg.handler(target="Frame:1:Pin", units="mm"))
        assert out["returned"] == 1
        assert out["matches"][0]["handle"].startswith("WORLD_FACE|@")
        assert out["matches"][0]["position"] == [110.0, 0.0, 0.0]


# ── PERCEPTION FIELDS: face outward normal + linear-edge direction ──────────────────────────────
# A face record carries the outward unit normal at its reported position; a straight edge carries its
# unit direction. Both degrade to an omitted field (never a fabricated value) when unevaluable.

class TestPerception:
    def test_planar_face_reports_outward_normal(self):
        # a planar top face at z=5mm with a +Z evaluator normal -> normal [0,0,1]
        face = FakeFace("TOP", _PlaneGeo(), (0, 0, 0.5), evaluator=_Eval(normal=(0, 0, 1)))
        _install([FakeOcc("X:1", "X", [FakeBody(faces=[face])])])
        out = _payload(fg.handler(target="X:1"))
        assert out["matches"][0]["normal"] == [0.0, 0.0, 1.0]

    def test_normal_omitted_when_evaluator_raises(self):
        # off-surface sample point -> getNormalAtPoint raises -> safe() degrades to NO 'normal' field.
        face = FakeFace("CURVED", _CylGeo(0.8), (0, 0, 0), evaluator=_Eval(raises=True))
        _install([FakeOcc("X:1", "X", [FakeBody(faces=[face])])])
        out = _payload(fg.handler(target="X:1", kind="cylinder_face"))
        assert "normal" not in out["matches"][0]

    def test_normal_omitted_when_face_has_no_evaluator(self):
        # a face lacking an evaluator (evaluator=None) yields no normal, not a crash.
        face = FakeFace("PL", _PlaneGeo(), (0, 0, 0))
        _install([FakeOcc("X:1", "X", [FakeBody(faces=[face])])])
        out = _payload(fg.handler(target="X:1"))
        assert "normal" not in out["matches"][0]

    def test_linear_edge_reports_unit_direction(self):
        # a line edge from (0,0,0) to (3,0,0)cm -> normalized direction [1,0,0]
        edge = FakeEdge("LN", _LineGeo((0, 0, 0), (3, 0, 0)), (1.5, 0, 0), length=3.0)
        _install([FakeOcc("X:1", "X", [FakeBody(edges=[edge])])])
        out = _payload(fg.handler(target="X:1", kind="line_edge"))
        m = out["matches"][0]
        assert m["kind"] == "line_edge"
        assert m["direction"] == [1.0, 0.0, 0.0]

    def test_diagonal_edge_direction_is_normalized(self):
        # a 3-4-0 edge -> unit direction [0.6, 0.8, 0]
        edge = FakeEdge("DIAG", _LineGeo((0, 0, 0), (3, 4, 0)), (1.5, 2, 0), length=5.0)
        _install([FakeOcc("X:1", "X", [FakeBody(edges=[edge])])])
        out = _payload(fg.handler(target="X:1", kind="line_edge"))
        assert out["matches"][0]["direction"] == [0.6, 0.8, 0.0]


# ── PLANAR-FACE FRAME: the face plane's own world coordinate system ─────────────────────────────
# A world centroid alone cannot express a position ON a face: a caller computing where to put
# something needs the face's own axes. Every planar match carries 'frame' (origin + x_world/y_world/
# normal, from adsk.core.Plane), and an unreadable frame is null rather than a partial one.

class TestPlanarFaceFrame:
    def _face(self, token, centroid, origin, u, v, n):
        return FakeFace(token, _PlaneGeo(origin=origin, u=u, v=v, normal=n), centroid)

    def _frame_of(self, face, units="mm"):
        _install([FakeOcc("X:1", "X", [FakeBody(faces=[face])])])
        out = _payload(fg.handler(target="X:1", kind="planar_face", units=units))
        return out["matches"][0]

    def test_frame_reports_the_plane_origin_and_axes(self):
        # a frame rotated 90 deg about Z: local +X runs along world +Y, local +Y along world -X.
        # Scrambling any of the three vectors would place a computed point somewhere else entirely.
        m = self._frame_of(self._face("TOP", (5, 5, 3), (1, 2, 3),
                                      (0, 1, 0), (-1, 0, 0), (0, 0, 1)))
        assert m["frame"] == {"origin": [10.0, 20.0, 30.0], "x_world": [0.0, 1.0, 0.0],
                              "y_world": [-1.0, 0.0, 0.0], "normal": [0.0, 0.0, 1.0]}

    def test_frame_origin_is_scaled_into_the_requested_units(self):
        # cm -> in: the origin rides the same conversion as 'position', not raw API centimetres
        m = self._frame_of(self._face("TOP", (0, 0, 0), (2.54, 5.08, 0),
                                      (1, 0, 0), (0, 1, 0), (0, 0, 1)), units="in")
        assert m["frame"]["origin"] == [1.0, 2.0, 0.0]

    def test_frame_origin_is_not_the_centroid(self):
        # 'position' is the face CENTROID, frame.origin the plane's PARAMETRIC origin - a caller
        # measuring in the frame subtracts the origin, so collapsing the two silently offsets
        # every local coordinate it computes.
        m = self._frame_of(self._face("TOP", (5, 5, 3), (1, 2, 3),
                                      (1, 0, 0), (0, 1, 0), (0, 0, 1)))
        assert m["position"] == [50.0, 50.0, 30.0]
        assert m["frame"]["origin"] == [10.0, 20.0, 30.0]

    def test_frame_axes_are_unit_vectors(self):
        # a non-unit axis would scale every local coordinate measured along it
        m = self._frame_of(self._face("TOP", (0, 0, 0), (0, 0, 0),
                                      (0, 3, 0), (-4, 0, 0), (0, 0, 2)))
        assert m["frame"]["x_world"] == [0.0, 1.0, 0.0]
        assert m["frame"]["y_world"] == [-1.0, 0.0, 0.0]
        assert m["frame"]["normal"] == [0.0, 0.0, 1.0]

    def test_frame_is_null_when_the_plane_cast_refuses(self, monkeypatch):
        import adsk.core
        monkeypatch.setattr(adsk.core.Plane, "cast", lambda g: None, raising=False)
        m = self._frame_of(self._face("TOP", (0, 0, 0), (1, 2, 3),
                                      (1, 0, 0), (0, 1, 0), (0, 0, 1)))
        assert m["frame"] is None                    # the key is present, the value honest

    def test_frame_is_null_when_the_axes_cannot_be_read(self):
        # a plane surface that answers no uDirection/vDirection - null, never a fabricated frame
        m = self._frame_of(FakeFace("PL", _PlaneGeo(), (1, 0, 0)))
        assert m["frame"] is None

    def test_a_degenerate_axis_voids_the_whole_frame(self):
        # a zero-length axis makes the frame unusable; three quarters of a coordinate system would
        # look complete enough to compute a wrong point from
        m = self._frame_of(self._face("TOP", (0, 0, 0), (1, 2, 3),
                                      (0, 0, 0), (0, 1, 0), (0, 0, 1)))
        assert m["frame"] is None

    def test_a_cylinder_face_carries_no_frame_key(self):
        # the frame is a PLANAR-face field; a curved face has no single in-plane coordinate system
        _install([FakeOcc("X:1", "X", [FakeBody(faces=[_cyl("C", 0.8, (0, 0, 0))])])])
        out = _payload(fg.handler(target="X:1", kind="cylinder_face"))
        assert "frame" not in out["matches"][0]


# ── BOUNDED READS: 'matches' is capped (CLAUDE.md "Bound it") ────────────────────────────────────

class TestCaps:
    def _many_faces(self, n):
        faces = [FakeFace(f"F{i:04d}", _PlaneGeo(), (i, 0, 0)) for i in range(n)]
        _install([FakeOcc("X:1", "X", [FakeBody(faces=faces)])])

    def test_default_cap_truncates_and_reports_the_true_count(self):
        self._many_faces(25)
        out = _payload(fg.handler(target="X:1", kind="planar_face"))
        assert out["returned"] == 20                    # the default cap
        assert out["match_count"] == 25                 # the full count is still honest

    def test_a_caller_cannot_lift_the_cap_past_the_ceiling(self):
        # every match row crosses the wire: max_results is clamped into 1.._MAX_RESULTS_CEILING,
        # so an oversized request is held at the ceiling, not honoured.
        self._many_faces(fg._MAX_RESULTS_CEILING + 10)
        out = _payload(fg.handler(target="X:1", kind="planar_face", max_results=999999))
        assert out["returned"] == fg._MAX_RESULTS_CEILING
        assert out["match_count"] == fg._MAX_RESULTS_CEILING + 10

    def test_a_non_numeric_max_results_falls_back_to_the_default(self):
        # the wire types it integer, but the clamp must not raise on a junk value either
        self._many_faces(25)
        out = _payload(fg.handler(target="X:1", kind="planar_face", max_results="lots"))
        assert out["returned"] == 20


# ── ELLIPSE / SPLINE edges: labelled, filterable, and carrying their own shape data ─────────────
# An extruded ellipse's two elliptical edges came back as the generic "edge" with no 'kind' value to
# filter by, so a caller narrowing by kind never saw them. Each curve type the classifier knows gets
# its OWN label; anything it does not know still reads "edge".

class TestEllipseAndSplineEdges:
    def _edges(self, *edges):
        _install([FakeOcc("X:1", "X", [FakeBody(edges=list(edges))])])

    def _ellipse(self, token="ELL", center=(3, 0, 0)):
        return FakeEdge(token, _EllipseGeo(2.0, 1.0, center), (5.0, 0, 0))

    def test_elliptical_edge_reports_its_kind_and_both_radii(self):
        # major/minor ride the same cm -> requested-units conversion 'radius' does; swapping the two
        # or leaving them in cm would size a bore wrong by exactly the factor under test.
        self._edges(self._ellipse())
        m = _payload(fg.handler(target="X:1", units="mm"))["matches"][0]
        assert m["kind"] == "ellipse_edge"
        assert m["major_radius"] == 20.0 and m["minor_radius"] == 10.0

    def test_an_elliptical_edge_reports_its_CENTRE_as_the_position(self):
        # the circular-edge convention: 'position' is the centre, not the point on the edge at 5cm
        self._edges(self._ellipse(center=(3, 0, 0)))
        m = _payload(fg.handler(target="X:1", units="mm"))["matches"][0]
        assert m["position"] == [30.0, 0.0, 0.0]

    def test_a_partial_ellipse_is_its_own_kind(self):
        # a full ellipse and an elliptical ARC are different curve types; one label for both would
        # hand a caller asking for closed rims the open arcs too
        self._edges(FakeEdge("ARC", _EllipticalArcGeo(2.0, 1.0, (0, 0, 0)), (1.0, 0, 0)))
        m = _payload(fg.handler(target="X:1", units="mm"))["matches"][0]
        assert m["kind"] == "elliptical_arc_edge"
        assert m["major_radius"] == 20.0 and m["minor_radius"] == 10.0

    def test_a_nurbs_edge_reads_spline_edge(self):
        self._edges(FakeEdge("SPL", _NurbsGeo(), (1.0, 0, 0)))
        m = _payload(fg.handler(target="X:1"))["matches"][0]
        assert m["kind"] == "spline_edge"
        # a spline has no centre and no radii - none may be fabricated for it
        assert "major_radius" not in m and "minor_radius" not in m
        assert m["position"] == [10.0, 0.0, 0.0]        # the point ON the edge, in mm

    def test_kind_filter_selects_only_the_full_ellipse(self):
        self._edges(self._ellipse(),
                    FakeEdge("ARC", _EllipticalArcGeo(2.0, 1.0, (0, 0, 0)), (1.0, 0, 0)),
                    FakeEdge("LN", _LineGeo((0, 0, 0), (3, 0, 0)), (1.5, 0, 0)))
        out = _payload(fg.handler(target="X:1", kind="ellipse_edge"))
        assert out["returned"] == 1
        assert out["matches"][0]["handle"].startswith("ELL|@ellipse_edge:")

    def test_each_new_kind_is_offered_on_the_wire(self):
        # a label the 'kind' enum does not carry cannot be asked for at all - the schema is the only
        # place a caller learns the filter value from.
        enum = fg.find_tool.to_dict()["inputSchema"]["properties"]["kind"]["enum"]
        assert {"ellipse_edge", "elliptical_arc_edge", "spline_edge"} <= set(enum)

    def test_an_unclassified_curve_type_still_reads_edge(self):
        # the fallback stays: a curve type with no label of its own is "edge", never mislabelled as
        # one of the six the classifier knows
        self._edges(FakeEdge("POLY", _PolylineGeo(), (1.0, 0, 0)))
        assert _payload(fg.handler(target="X:1"))["matches"][0]["kind"] == "edge"
