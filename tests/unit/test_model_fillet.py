"""Unit tests for model_fillet.py - the fillet shapes, the edge scope and the rule fillet."""

import math
import pytest
import types
from conftest import (BRepBody, BRepEdge, BRepFace, FakePoint, FakeUnitsManager, FakeVector3D,
                      MakeComp, install, load_tool, make_design, payload as _payload,
                      _NamedCollection)

fl = load_tool("model_fillet")
edge_common_mod = load_tool("_edge_common")
model_chamfer = load_tool("model_chamfer")


# Every rig below is ONE edge running along +Z through the origin. The classifier reads only local
# things there: the edge's own tangent, and per bounding coEdge its face normal plus whether that
# coEdge heads against the edge. A manifold edge's two coEdges head opposite ways round it.
_PLUS_X, _PLUS_Y, _PLUS_Z = (1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)


def _coedge(normal, opposed):
    """A BRepCoEdge: the face it bounds (answering its out-of-material normal at any point) reached
    through its loop, and whether it runs against the edge's own direction."""
    face = BRepFace(surface=None, normal=FakeVector3D(*normal))
    return types.SimpleNamespace(isOpposedToEdge=opposed,
                                 loop=types.SimpleNamespace(face=face))


def _rig(*sides, tangent=_PLUS_Z, param_reversed=False):
    """A BRepEdge bounded by the given (normal, opposed) coEdge sides; tangent None is an edge whose
    curve evaluator does not answer, param_reversed None one whose isParamReversed does not."""
    return BRepEdge(curve=None, point_on_edge=FakePoint(),
                    tangent=FakeVector3D(*tangent) if tangent else None,
                    param_reversed=param_reversed,
                    co_edges=[_coedge(n, o) for n, o in sides])


def _tilted(deg):
    """The +X normal turned `deg` about the edge - the second face of a dihedral that shallow."""
    a = math.radians(deg)
    return (math.cos(a), math.sin(a), 0.0)


def _edge(kind):
    """One rig per named dihedral. True = convex (the cube corner), False = concave (the same two
    normals with both coEdges reversed), 'smooth' = two faces meeting flat, 'antiparallel' = normals
    pointing at each other, 'split' = coEdges that cannot both be right, None = no coEdges at all."""
    if kind is None:
        return BRepEdge(curve=None, point_on_edge=FakePoint(), tangent=FakeVector3D(*_PLUS_Z))
    return {
        True: lambda: _rig((_PLUS_X, False), (_PLUS_Y, True)),
        False: lambda: _rig((_PLUS_X, True), (_PLUS_Y, False)),
        "smooth": lambda: _rig((_PLUS_X, False), (_PLUS_X, True)),
        "antiparallel": lambda: _rig((_PLUS_X, False), ((-1.0, 0.0, 0.0), True)),
        # the two coEdges round one edge head opposite ways, so this pair cannot both be right
        "split": lambda: _rig((_PLUS_X, False), (_PLUS_Y, False)),
    }[kind]()


def make_body(name, edge_kinds, volume=None):
    """A solid body whose edges are the dihedral rigs above. volume None = the read does not answer,
    so the volume gate has nothing to judge."""
    body = BRepBody(name=name, volume=volume)
    body.edges = _NamedCollection([_edge(k) for k in edge_kinds])
    return body


class FakeFilletInput:
    """FilletFeatureInput. Each add*EdgeSet returns True (the API returns a bool), and the arity of
    each matches the real one - addVariableRadiusEdgeSet has NO isTangentChain argument, and its
    positions/radii are plain Python lists (a vector argument): an ObjectCollection raises live, so
    it raises here."""

    def __init__(self):
        self.edge_set = None
        self.variable_set = None
        self.chord_set = None
        self.refuse = False

    def addConstantRadiusEdgeSet(self, edges, val, tangent):
        self.edge_set = (edges, val, tangent)
        return not self.refuse

    def addVariableRadiusEdgeSet(self, tangentEdges, startRadius, endRadius, positions, radii):
        for name, arg in (("positions", positions), ("radii", radii)):
            if not isinstance(arg, list):
                raise TypeError(f"in method 'FilletFeatureInput_addVariableRadiusEdgeSet', "
                                f"argument '{name}' is not a vector")
        self.variable_set = (tangentEdges, startRadius, endRadius, positions, radii)
        return not self.refuse

    def addChordLengthEdgeSet(self, edges, chordLength, tangent):
        self.chord_set = (edges, chordLength, tangent)
        return not self.refuse


class FakeRuleFilletInput:
    """RuleFilletFeatureInput. setByAllEdges/setByBetweenFacesOrFeatures take plain Python lists of
    faces (not an ObjectCollection) and return a bool; radius and topologyType are settable."""

    def __init__(self):
        self.all_edges = None
        self.between = None
        self.radius = None
        self.topologyType = None
        self.refuse = False

    def _check(self, name, arg):
        if not isinstance(arg, list):
            raise TypeError(f"in method 'RuleFilletFeatureInput_{name}', argument is not a vector")

    def setByAllEdges(self, facesOrFeatures):
        self._check("setByAllEdges", facesOrFeatures)
        self.all_edges = facesOrFeatures
        return not self.refuse

    def setByBetweenFacesOrFeatures(self, one, two):
        self._check("setByBetweenFacesOrFeatures", one)
        self._check("setByBetweenFacesOrFeatures", two)
        self.between = (one, two)
        return not self.refuse


# The sentinel a fresh ChamferFeatureInput carries, so "never assigned" is distinguishable from
# "assigned Fusion's own default member".
_UNSET_CORNER = "corner:api-default"


class FakeChamferInput:
    """ChamferFeatureInput. Each setTo* returns its documented bool, and cornerType is a PROPERTY -
    so the SWIG trap applies to it: an assignment the proxy drops leaves the API default in place
    and only a read-back tells. `swallow_corner_set` models that."""

    swallow_corner_set = False

    def __init__(self, edges, tangent):
        self.edges = edges
        self.tangent = tangent
        self.distance = None
        self.two_distances = None
        self.distance_and_angle = None
        self.refuse = None            # the name of the setTo* that answers False
        self._corner = _UNSET_CORNER
    def setToEqualDistance(self, val):
        self.distance = val
        return self.refuse != "equal"
    def setToTwoDistances(self, val1, val2):
        self.two_distances = (val1, val2)
        return self.refuse != "two"
    def setToDistanceAndAngle(self, distance, angle):
        self.distance_and_angle = (distance, angle)
        return self.refuse != "angle"

    @property
    def cornerType(self):
        return self._corner

    @cornerType.setter
    def cornerType(self, value):
        if not self.swallow_corner_set:
            self._corner = value


class FakeCountingFeature:
    """A created feature that ANSWERS .faces.count - the only per-edge effect read-back the real
    Fillet/ChamferFeature offers (neither exposes an .edges collection, measured live).
    healthState 2 with an EMPTY errorOrWarningMessage is a real shape: a variable-radius chain listed
    out of order comes back failed and silent, reading 0 faces like a tangent no-op does."""
    def __init__(self, name, faces, health=0, message=""):
        self.name = name
        self.faces = _NamedCollection([None] * faces)
        self.healthState = health
        self.errorOrWarningMessage = message
        self.deleted = False
    def deleteMe(self):
        self.deleted = True
        return True


class FakeFilletFeatures:
    def __init__(self):
        self.last = None
        self.rule_last = None
        # default created feature answers only .name (no .faces/.edges - the attribute-silent case)
        self.result = type("F", (), {"name": "Fillet1"})()
        self.rule_result = FakeCountingFeature("RuleFillet1", faces=4)
        self.rule_settings_override = None   # set to model a radius/topology that did not land
        # applied when a feature is added, so a test can model the geometry actually moving
        self.on_add = None
        self.refuse_edge_set = False
    def createInput(self):
        self.last = FakeFilletInput()
        self.last.refuse = self.refuse_edge_set
        return self.last
    def add(self, inp):
        if self.on_add:
            self.on_add()
        return self.result
    def createRuleFilletInput(self):
        self.rule_last = FakeRuleFilletInput()
        self.rule_last.refuse = self.refuse_edge_set
        return self.rule_last
    def addRuleFillet(self, inp):
        if self.on_add:
            self.on_add()
        # A live RuleFilletFeature reports what it applied through ruleFilletSettings; the fake
        # echoes the input unless a test overrides it to model a setter the API ignored.
        if self.rule_result is not None and self.rule_settings_override is None:
            applied = getattr(inp.radius, "value", inp.radius)
            self.rule_result.ruleFilletSettings = type(
                "S", (), {"radius": type("V", (), {"value": applied})(),
                          "topologyType": inp.topologyType})()
        elif self.rule_result is not None:
            self.rule_result.ruleFilletSettings = self.rule_settings_override
        return self.rule_result


class FakeChamferFeatures:
    """A live ChamferFeature reports what it BUILT, and that is the only signal a wrong corner type
    or angle leaves - all three corner types build the same face count. So the created feature
    carries cornerType, and (for a distance-and-angle chamfer) chamferType plus a
    chamferTypeDefinition whose .distance/.angle are ModelParameters in CM and RADIANS. The fake
    echoes what the input took; the *_override knobs model the platform building something else."""

    def __init__(self):
        self.last = None
        self.result = type("F", (), {"name": "Chamfer1"})()
        self.refuse = None            # 'equal' | 'two' | 'angle' - which setTo* answers False
        self.added = 0
        self.corner_override = None       # (value,) - what the FEATURE reports instead
        self.definition_override = None   # (distance_cm, angle_rad) - ditto, or False for absent
        self.chamfer_type_override = None
        # applied when a feature is added, so a test can model the geometry actually moving
        self.on_add = None
    def createInput(self, edges, tangent):
        self.last = FakeChamferInput(edges, tangent)
        self.last.refuse = self.refuse
        return self.last
    def add(self, inp):
        import adsk.fusion
        self.added += 1
        if self.on_add:
            self.on_add()
        if self.result is None:
            return self.result
        if self.corner_override is not False:      # False = the feature does not answer cornerType
            self.result.cornerType = (self.corner_override[0] if self.corner_override
                                      else inp.cornerType)
        if inp.distance_and_angle is not None:
            self.result.chamferType = (
                self.chamfer_type_override[0] if self.chamfer_type_override
                else getattr(adsk.fusion.ChamferTypes, "DistanceAndAngleChamferType"))
            if self.definition_override is False:
                self.result.chamferTypeDefinition = None
            else:
                dist_cm, angle_rad = (self.definition_override or
                                      (inp.distance_and_angle[0][1], inp.distance_and_angle[1][1]))
                self.result.chamferTypeDefinition = type("D", (), {
                    "distance": type("P", (), {"value": dist_cm})(),
                    "angle": type("P", (), {"value": angle_rad})()})()
        return self.result


def _component(bodies, ff, cf):
    """The active component: its bodies, plus the fillet/chamfer feature collections the handler
    builds through."""
    comp = MakeComp(name="Comp")
    comp.bRepBodies = _NamedCollection(bodies)
    comp.features = type("F", (), {"filletFeatures": ff, "chamferFeatures": cf})()
    return comp


def _install(bodies):
    ff = FakeFilletFeatures(); cf = FakeChamferFeatures()
    install(fl, make_design(comp=_component(bodies, ff, cf)))
    import adsk.fusion, adsk.core
    # BodyRef(kind='solid') does isinstance(body, adsk.fusion.BRepBody) + checks isSolid — make the
    # fake body pass the BRep type check.
    adsk.fusion.BRepBody = BRepBody
    adsk.core.ValueInput.createByReal = staticmethod(lambda v: ("real", v))
    return ff, cf


def _edge_ent(body_name="Block", body=None, token=None):
    """A BRep edge resolved from a handle, or bounding a face; carries .body.name for the result
    label. Its body's volume does not read, so the material gate has nothing to judge unless a test
    supplies one."""
    edge = BRepEdge(curve=None, entity_token=token)
    edge.body = body if body is not None else BRepBody(name=body_name, volume=None)
    return edge


def _face(body_name="Block"):
    """A BRep face resolved from a handle - what a rule fillet selects through. conftest's shared
    BRepFace, so the face surface stays swept against the live shape record."""
    return BRepFace(surface=None, body_name=body_name)


def _face_with_edges(edges, body_name="Block"):
    """A BRep face carrying the edges that bound it - what a face-scoped fillet expands."""
    return BRepFace(surface=None, body_name=body_name, edges=edges)


def _install_edge_handles(handle_map):
    """Install a design whose findEntityByToken resolves `handle_map` - the seam GeometryHandleList
    reads an edge/face handle through, before its isinstance(BRepEdge/BRepFace) gate."""
    ff = FakeFilletFeatures(); cf = FakeChamferFeatures()
    install(fl, make_design(comp=_component([], ff, cf), tokens=handle_map))
    import adsk.fusion, adsk.core
    adsk.fusion.BRepEdge = BRepEdge
    adsk.fusion.BRepFace = BRepFace
    adsk.core.ValueInput.createByReal = staticmethod(lambda v: ("real", v))
    return ff, cf


def _parametric(monkeypatch, installer, *args, engine=None, **kw):
    """`installer` (either of the two above) plus conftest's shared units engine on the design and a
    ValueInput.createByString seam, so the two ValueInput forms are told apart by shape:
    ('real', cm) vs ('string', expr).

    The default engine resolves 'WallT/2' and nothing else, at 6.5 mm - which the engine's measured
    unit mapping answers as 0.65 internal cm, a value no literal in these tests uses. Passing
    valid=() gives an engine that RAISES on every call, which is how a literal is proved never to
    reach it."""
    import adsk.core
    out = installer(*args, **kw)
    design = fl._inputs._common.design()
    design.unitsManager = engine if engine is not None else FakeUnitsManager(valid=("WallT/2",),
                                                                             value=6.5)
    monkeypatch.setattr(adsk.core.ValueInput, "createByString",
                        staticmethod(lambda s: ("string", s)), raising=False)
    return out + (design,)


def _blind_engine():
    """A units engine that resolves NOTHING - every evaluateExpression raises."""
    return FakeUnitsManager(valid=())


class TestGuards:

    def test_unknown_units(self):
        _install([make_body("B", [True, True])])
        res = fl.handler(body_name="B", radius=1, units="furlong")
        assert res["isError"] is True and "Unknown units" in res["message"]

    def test_nonpositive_radius(self):
        _install([make_body("B", [True])])
        res = fl.handler(body_name="B", radius=0)
        assert res["isError"] is True and "positive" in res["message"]

    def test_body_not_found(self):
        # BodyRef resolves the named body; an unknown name errors, naming the value + the handle path.
        _install([make_body("B", [True])])
        res = fl.handler(body_name="X", radius=1, edge_filter="all")
        assert res["isError"] is True and "no body or component named 'X'" in res["message"]

    def test_omitted_scope_refuses(self):
        # No 'edges' and no 'edge_filter': REFUSED, naming both scoping paths. An omitted scope
        # must never silently mean the whole body - implicit blanket rounding was every
        # executor's default design language while 'all' was the default.
        _install([make_body("B", [True, True])])
        res = fl.handler(body_name="B", radius=1)
        assert res["isError"] is True
        assert "edges" in res["message"] and "edge_filter" in res["message"]
        res2 = model_chamfer.handler(body_name="B", distance=1)
        assert res2["isError"] is True and "edge_filter" in res2["message"]

    def test_blanket_filter_reports_blast_radius(self):
        # An explicit filter sweep names how many of the body's edges it took.
        _install([make_body("B", [True, False, True])])
        out = _payload(fl.handler(body_name="B", radius=1, edge_filter="convex"))
        assert "BLANKET" in out["note"] and "2 of the body's 3 edges" in out["note"]

    def test_bad_edge_filter(self):
        _install([make_body("B", [True])])
        res = fl.handler(body_name="B", radius=1, edge_filter="weird")
        assert res["isError"] is True and "edge_filter" in res["message"]

    def test_nonnumeric_radius_is_read_as_an_expression_and_refused_by_name(self):
        # A non-numeric radius string is a parameter EXPRESSION, so the refusal comes from the units
        # engine that could not evaluate it - naming the input and the value, not "not a number".
        _install([make_body("B", [True])])
        res = fl.handler(body_name="B", radius="big")
        assert res["isError"] is True
        assert "'radius'" in res["message"] and "did not evaluate" in res["message"]
        assert "'big'" in res["message"]

    def test_no_matching_edges_errors(self):
        # body has only convex edges; a concave filter matches nothing
        _install([make_body("B", [True, True])])
        res = fl.handler(body_name="B", radius=1, edge_filter="concave")
        assert res["isError"] is True and "No matching edges" in res["message"]
        assert "body has 2 edges" in res["message"]

    def test_a_body_with_no_readable_edges_refuses_under_all(self):
        # 'all' classifies nothing, so there is no census for this refusal to name - and naming one
        # anyway raises out of the handler instead of refusing.
        _install([make_body("B", [])])
        res = fl.handler(body_name="B", radius=1, edge_filter="all")
        assert res["isError"] is True and "No matching edges" in res["message"]
        assert "body has 0 edges" in res["message"]


class TestFillet:

    def test_fillet_all_edges_scaled(self):
        ff, _ = _install([make_body("Block", [True, True, False])])
        out = _payload(fl.handler(body_name="Block", radius=2, units="mm", edge_filter="all"))
        assert out["filleted"] is True and out["edges_requested"] == 3
        edges, val, tangent = ff.last.edge_set
        assert val == ("real", 0.2)        # 2mm -> 0.2cm

    def test_fillet_convex_filter(self):
        ff, _ = _install([make_body("B", [True, False, True])])
        out = _payload(fl.handler(body_name="B", radius=1, edge_filter="convex"))
        assert out["edges_requested"] == 2  # only the two convex edges

    def test_fillet_concave_filter(self):
        ff, _ = _install([make_body("B", [True, False, True])])
        out = _payload(fl.handler(body_name="B", radius=1, edge_filter="concave"))
        assert out["edges_requested"] == 1

    def test_default_most_recent_body(self):
        _install([make_body("First", [True]), make_body("Last", [True, True])])
        out = _payload(fl.handler(radius=1, edge_filter="all"))
        assert out["body"] == "Last"

    def test_a_smooth_edge_is_in_neither_filter(self):
        # BRepEdge exposes no isConvex, so each edge is classified here; two faces meeting flat are
        # neither convex nor concave and match no filter. The split is published beside the sweep.
        _install([make_body("B", [True, "smooth", False])])
        out = _payload(fl.handler(body_name="B", radius=1, edge_filter="concave"))
        assert (out["edges_convex"], out["edges_concave"], out["edges_smooth"]) == (1, 1, 1)
        assert "1 convex, 1 concave, 1 smooth" in out["note"]

    def test_convex_and_concave_are_told_apart_on_one_body(self):
        # The two differ ONLY in which way each coEdge heads round the edge - the local read.
        _install([make_body("B", [True, False, True])])
        out = _payload(fl.handler(body_name="B", radius=1, edge_filter="convex"))
        assert (out["edges_convex"], out["edges_concave"]) == (2, 1)
        assert out["edges_requested"] == 2 and "edges_swept" not in out

    def test_an_unclassifiable_edge_refuses_a_filtered_sweep(self):
        # An edge the classifier could not answer must NOT be swept in silently. The refusal names
        # the branch and the count.
        ff, _ = _install([make_body("B", [True, None])])
        res = fl.handler(body_name="B", radius=1, edge_filter="convex")
        assert res["isError"] is True
        assert "1 of the 2 edges on 'B' could not be classified" in res["message"]
        assert "1 unreadable" in res["message"]
        assert "'edges' handles" in res["message"] and "edge_filter='all'" in res["message"]
        assert ff.last is None            # refused BEFORE any fillet input was created

    def test_every_unclassified_branch_holds_the_sweep_back(self):
        # A knife edge and a disagreeing pair are as unswept as an unreadable one: all three count
        # toward the refusal, and each is named so the caller knows which it met.
        ff, _ = _install([make_body("B", [True, "antiparallel", "split"])])
        res = fl.handler(body_name="B", radius=1, edge_filter="convex")
        assert res["isError"] is True
        assert "2 of the 3 edges on 'B' could not be classified" in res["message"]
        assert "1 knife" in res["message"] and "1 disagreed" in res["message"]
        assert ff.last is None            # refused BEFORE any fillet input was created

    def test_all_sweeps_every_edge_without_classifying(self, monkeypatch):
        # 'all' gates on nothing, so it neither pays the classifier nor publishes a census it did
        # not take - and an edge no classifier could answer is no reason to refuse.
        _install([make_body("B", [True, None])])
        called = []
        monkeypatch.setattr(edge_common_mod, "_edge_convexity", lambda e: called.append(e))
        out = _payload(fl.handler(body_name="B", radius=1, edge_filter="all"))
        assert out["edges_requested"] == 2 and called == []
        assert "convex" not in out["note"] and "edges_convex" not in out

    def test_two_coedges_heading_the_same_way_are_not_signed(self):
        # The pair runs OPPOSITE ways round one edge, so this pair cannot both be right and the
        # heading the sign would be read off is not known.
        assert edge_common_mod._edge_convexity(_edge("split")) == "disagreed"

    def test_anti_parallel_normals_are_a_knife_not_a_smooth_join(self):
        # Two out-of-material normals pointing at each other bound a knife edge or a crack: no
        # material wedge to sign - and it is NOT a flat join either.
        assert edge_common_mod._edge_convexity(_edge("antiparallel")) == "knife"

    def test_an_edge_running_against_its_curve_flips_the_verdict(self):
        # The evaluator follows the CURVE; isParamReversed says whether the edge runs against it.
        # Uncorrected, both headings negate together and the verdict flips with nothing to catch it.
        assert edge_common_mod._edge_convexity(_rig((_PLUS_X, False), (_PLUS_Y, True))) == "convex"
        assert edge_common_mod._edge_convexity(
            _rig((_PLUS_X, False), (_PLUS_Y, True), param_reversed=True)) == "concave"

    def test_an_edge_whose_param_direction_does_not_read_is_unreadable(self):
        assert edge_common_mod._edge_convexity(
            _rig((_PLUS_X, False), (_PLUS_Y, True), param_reversed=None)) == "unreadable"

    def test_an_edge_whose_tangent_does_not_read_is_unreadable(self):
        # Every side may read and the dihedral still have no heading to sign against.
        assert edge_common_mod._edge_convexity(
            _rig((_PLUS_X, False), (_PLUS_Y, True), tangent=None)) == "unreadable"

    def test_an_edge_without_exactly_two_coedges_is_unreadable(self):
        assert edge_common_mod._edge_convexity(_rig((_PLUS_X, False))) == "unreadable"
        assert edge_common_mod._edge_convexity(
            _rig((_PLUS_X, False), (_PLUS_Y, True), (_PLUS_Y, False))) == "unreadable"

    def test_the_dihedral_tolerance_pins_the_smooth_band(self):
        # 0.05 deg: a 0.04 deg dihedral is a flat join, a 0.06 deg one is a corner to round.
        assert edge_common_mod._edge_convexity(_rig((_PLUS_X, False), (_tilted(0.04), True))) == "smooth"
        assert edge_common_mod._edge_convexity(_rig((_PLUS_X, False), (_tilted(0.06), True))) == "convex"

    def test_the_smooth_band_excludes_its_own_boundary(self, monkeypatch):
        # '>' not '>=': a dihedral sitting exactly ON the tolerance is a corner, not a smooth join.
        corner = _edge(True)                       # 90 deg: the two normals' cosine is exactly 0.0
        monkeypatch.setattr(edge_common_mod, "_SMOOTH_JOIN_COS", 0.0)
        assert edge_common_mod._edge_convexity(corner) == "convex"
        monkeypatch.setattr(edge_common_mod, "_SMOOTH_JOIN_COS", -1e-9)
        assert edge_common_mod._edge_convexity(corner) == "smooth"

    def test_radius_echoed_rounded_in_payload(self):
        _install([make_body("B", [True])])
        out = _payload(fl.handler(body_name="B", radius=3.5, units="mm", edge_filter="all"))
        # the raw (un-scaled) radius is echoed under 'radius'
        assert out["radius"] == 3.5
        assert out["edge_selection"] == "filter"

    def test_partial_edge_application_errors_and_rolls_back(self):
        # The feature consumed FEWER edges than handed in: reporting filleted:true with the input
        # echo as the "result" would be the honesty-contract cardinal sin (a fillet silently rounded
        # fewer edges than requested). The read-back count must gate the call: error naming
        # requested vs applied, and the inert/partial feature must be rolled back (deleteMe called).
        ff, _ = _install([make_body("B", [True, True, True])])
        ff.result = FakeCountingFeature("Fillet1", faces=2)
        res = fl.handler(body_name="B", radius=1, edge_filter="all")
        assert res["isError"] is True
        assert "PARTIALLY" in res["message"]
        assert "3" in res["message"] and "2" in res["message"]   # requested vs applied, both named
        assert ff.result.deleted is True

    def test_measured_fields_omitted_when_feature_does_not_answer(self):
        # safe() returning None means the attribute did not answer - the keys are OMITTED,
        # never reported as null (the default fake feature carries only .name). The no-op guard is
        # gated on faces_created == 0, so a None (unanswered) count does NOT trip it.
        _install([make_body("B", [True])])
        out = _payload(fl.handler(body_name="B", radius=1, edge_filter="all"))
        assert "edges_measured" not in out and "faces_created" not in out
        assert "read from the created feature" not in out["note"]

    def test_fillet_no_op_on_tangent_edge_errors(self):
        # A fillet whose feature reports ZERO created faces rounded nothing (a tangent edge - two
        # faces meeting smoothly, e.g. a hole tangent to a face). filleted:true would be a false ok;
        # the 0-face read-back must convert it to an error naming the tangent cause AND remove the
        # inert feature.
        ff, _ = _install([make_body("B", [True])])
        ff.result = FakeCountingFeature("Fillet1", faces=0)
        res = fl.handler(body_name="B", radius=1, edge_filter="all")
        assert res["isError"] is True
        assert "rounded nothing" in res["message"] and "TANGENT" in res["message"]
        assert ff.result.deleted is True          # the no-op feature was removed


class TestEdgeHandles:

    def test_fillet_specific_edges_via_handles(self):
        e1, e2 = _edge_ent("Bracket"), _edge_ent("Bracket")
        ff, _ = _install_edge_handles({"E1": e1, "E2": e2})
        out = _payload(fl.handler(edges=["E1", "E2"], radius=2, units="mm"))
        assert out["filleted"] is True
        assert out["edges_requested"] == 2           # only the 2 named edges, not a whole body
        assert "handle" in out["edge_selection"]
        assert out["body"] == "Bracket"             # labelled from the edge's owning body

    def test_edges_take_precedence_over_body(self):
        e1 = _edge_ent("X")
        ff, _ = _install_edge_handles({"E1": e1})
        out = _payload(fl.handler(edges=["E1"], body_name="ignored", radius=1))
        assert out["edges_requested"] == 1           # used the handle, not body_name

    def test_bad_edge_handle_errors(self):
        _install_edge_handles({"E1": _edge_ent()})   # E2 missing
        res = fl.handler(edges=["E1", "E2"], radius=1)
        assert res["isError"] is True and "edges" in res["message"]

    def test_stale_handle_among_n_names_requested_count(self):
        # A stale/unresolvable handle among N passed handles must refuse the WHOLE call before any
        # feature is created (never silently fillet just the live ones) - and name how many handles
        # were requested, plus point back at find_geometry for a fresh one.
        _install_edge_handles({"E1": _edge_ent()})   # E2 is stale/unresolvable
        res = fl.handler(edges=["E1", "E2"], radius=1)
        assert res["isError"] is True
        assert "2 edge handle(s) were requested" in res["message"]
        assert "find_geometry" in res["message"]


class TestQualifiedBodyName:

    def test_body_name_qualified_with_occurrence_path(self):
        # a body reachable through an occurrence proxy reports '<occ fullPathName>/<name>', so the same
        # local name in two components is distinguishable.
        occ = type("Occ", (), {"fullPathName": "Gearbox:2"})()
        body = type("Bdy", (), {"name": "Housing", "assemblyContext": occ,
                                "parentComponent": type("C", (), {"name": "GearboxComp"})()})()
        assert edge_common_mod._qualified_body_name(body) == "Gearbox:2/Housing"

    def test_body_name_unqualified_when_no_context(self):
        # a root-owned body with no readable context degrades to the plain local name (no '?/name').
        body = type("Bdy", (), {"name": "Plate"})()
        assert edge_common_mod._qualified_body_name(body) == "Plate"

    def test_none_body_is_none(self):
        assert edge_common_mod._qualified_body_name(None) is None


class TestFilletType:

    def test_unknown_fillet_type_refused(self):
        _install([make_body("B", [True])])
        res = fl.handler(body_name="B", radius=1, edge_filter="all", fillet_type="rolling")
        assert res["isError"] is True and "fillet_type" in res["message"]

    def test_constant_is_the_default_and_is_named_in_the_payload(self):
        ff, _ = _install([make_body("B", [True])])
        out = _payload(fl.handler(body_name="B", radius=1, edge_filter="all"))
        assert out["fillet_type"] == "constant"
        assert ff.last.variable_set is None and ff.last.chord_set is None

    def test_refused_edge_set_errors_instead_of_adding_a_feature(self):
        # addConstantRadiusEdgeSet returns a bool; a False means nothing was selected, so adding the
        # feature anyway would report a fillet that rounds nothing.
        ff, _ = _install_edge_handles({"E1": _edge_ent()})
        ff.refuse_edge_set = True
        res = fl.handler(edges=["E1"], radius=1)
        assert res["isError"] is True and "constant-radius" in res["message"]


class TestVariableRadius:

    def test_positions_and_radii_cross_as_plain_lists(self):
        # The API takes positions/radii as vectors - plain Python lists. An ObjectCollection there
        # raises a vector-type argument error live, and the fake raises the same way.
        ff, _ = _install_edge_handles({"E1": _edge_ent()})
        out = _payload(fl.handler(edges=["E1"], radius=2, end_radius=5, units="mm",
                                          fillet_type="variable",
                                          positions=[0.25, 0.75], radii=[3, 4]))
        tangent_edges, start, end, positions, radii = ff.last.variable_set
        assert isinstance(positions, list) and isinstance(radii, list)
        assert positions == [("real", 0.25), ("real", 0.75)]     # a position is unitless, unscaled
        assert radii == [("real", pytest.approx(0.3)), ("real", pytest.approx(0.4))]  # mm -> cm
        assert start == ("real", 0.2) and end == ("real", 0.5)
        assert tangent_edges.count == 1
        assert ff.last.edge_set is None                          # not the constant-radius path
        assert out["fillet_type"] == "variable" and out["end_radius"] == 5
        assert out["positions"] == [0.25, 0.75] and out["radii"] == [3.0, 4.0]

    def test_without_intermediate_radii_the_arrays_are_empty(self):
        ff, _ = _install_edge_handles({"E1": _edge_ent()})
        out = _payload(fl.handler(edges=["E1"], radius=1, end_radius=2,
                                          fillet_type="variable"))
        _, _, _, positions, radii = ff.last.variable_set
        assert positions == [] and radii == []
        assert "positions" not in out and "radii" not in out

    def test_needs_end_radius(self):
        _install_edge_handles({"E1": _edge_ent()})
        res = fl.handler(edges=["E1"], radius=1, fillet_type="variable")
        assert res["isError"] is True and "end_radius" in res["message"]

    def test_refuses_mismatched_positions_and_radii(self):
        _install_edge_handles({"E1": _edge_ent()})
        res = fl.handler(edges=["E1"], radius=1, end_radius=2, fillet_type="variable",
                                 positions=[0.3, 0.6], radii=[4])
        assert res["isError"] is True
        assert "2 position(s)" in res["message"] and "1 radius(es)" in res["message"]

    def test_refuses_a_position_outside_the_unit_interval(self):
        _install_edge_handles({"E1": _edge_ent()})
        res = fl.handler(edges=["E1"], radius=1, end_radius=2, fillet_type="variable",
                                 positions=[1.5], radii=[4])
        assert res["isError"] is True
        assert "'positions'[0] is 1.5" in res["message"]

    def test_refuses_a_nonpositive_intermediate_radius(self):
        _install_edge_handles({"E1": _edge_ent()})
        res = fl.handler(edges=["E1"], radius=1, end_radius=2, fillet_type="variable",
                                 positions=[0.5], radii=[0])
        assert res["isError"] is True and "'radii'[0] must be positive" in res["message"]

    def test_requires_edge_handles_not_a_filter_sweep(self):
        # The chain must be tangentially connected and in order; a filter sweep has no such order,
        # so there is no start end for the start radius.
        _install([make_body("B", [True, True])])
        res = fl.handler(body_name="B", radius=1, end_radius=2, edge_filter="all",
                                 fillet_type="variable")
        assert res["isError"] is True and "edges" in res["message"]

    def test_refused_variable_edge_set_errors(self):
        ff, _ = _install_edge_handles({"E1": _edge_ent()})
        ff.refuse_edge_set = True
        res = fl.handler(edges=["E1"], radius=1, end_radius=2, fillet_type="variable")
        assert res["isError"] is True and "variable-radius" in res["message"]


class TestChordLength:

    def test_chord_length_scaled_and_tangent_chained(self):
        ff, _ = _install_edge_handles({"E1": _edge_ent()})
        out = _payload(fl.handler(edges=["E1"], fillet_type="chord_length",
                                          chord_length=2, units="mm"))
        edges, chord, tangent = ff.last.chord_set
        assert chord == ("real", 0.2) and tangent is True
        assert ff.last.edge_set is None
        assert out["chord_length"] == 2 and "radius" not in out
        assert out["fillet_type"] == "chord_length"

    def test_needs_a_chord_length(self):
        _install_edge_handles({"E1": _edge_ent()})
        res = fl.handler(edges=["E1"], radius=3, fillet_type="chord_length")
        assert res["isError"] is True and "chord_length" in res["message"]

    def test_nonpositive_chord_length_names_the_input(self):
        _install_edge_handles({"E1": _edge_ent()})
        res = fl.handler(edges=["E1"], fillet_type="chord_length", chord_length=-1)
        assert res["isError"] is True and "positive chord_length" in res["message"]

    def test_sweeps_a_body_by_filter_too(self):
        ff, _ = _install([make_body("B", [True, False])])
        out = _payload(fl.handler(body_name="B", fillet_type="chord_length",
                                          chord_length=1, edge_filter="convex"))
        assert out["edges_requested"] == 1
        assert ff.last.chord_set[1] == ("real", 0.1)


class TestFaceScopedFillet:

    """'faces' rounds every edge of the named faces - the expansion model_chamfer runs, reached
    here for the constant and chord-length shapes. The other two say why they cannot take it."""

    def test_a_constant_fillet_takes_every_edge_of_the_named_faces_once(self):
        # Two faces meeting at one edge: the shared edge belongs to both, and the same edge twice in
        # the feature's edge set is a duplicate.
        shared, only_a, only_b = (_edge_ent(token="e0"), _edge_ent(token="e1"),
                                  _edge_ent(token="e2"))
        ff, _ = _install_edge_handles({"FA": _face_with_edges([shared, only_a]),
                                       "FB": _face_with_edges([shared, only_b])})
        out = _payload(fl.handler(faces=["FA", "FB"], radius=1, fillet_type="constant"))
        edges, _val, _tangent = ff.last.edge_set
        assert list(edges) == [shared, only_a, only_b]   # those faces' edges, and nothing else
        assert out["edges_requested"] == 3 and out["faces_selected"] == 2
        assert out["edge_selection"] == "3 edge(s) of 2 face(s)"

    def test_faces_reach_the_chord_length_shape_too(self):
        ff, _ = _install_edge_handles({"FA": _face_with_edges([_edge_ent(token="e1")])})
        out = _payload(fl.handler(faces=["FA"], fillet_type="chord_length", chord_length=1))
        assert ff.last.chord_set is not None and ff.last.edge_set is None
        assert out["edges_requested"] == 1 and out["faces_selected"] == 1

    def test_a_variable_fillet_refuses_faces_naming_both_ways_out(self):
        # The radius runs from the chain's start to its far end and a face set carries no such
        # order, so 'faces' is REFUSED here - never accepted and dropped.
        ff, _ = _install_edge_handles({"FA": _face_with_edges([_edge_ent(token="e1")])})
        res = fl.handler(faces=["FA"], fillet_type="variable", radius=1, end_radius=2)
        assert res["isError"] is True and "'faces'" in res["message"]
        assert "'edges'" in res["message"] and "fillet_type='rule'" in res["message"]
        assert ff.last is None

    def test_a_rule_fillet_refuses_edges_rather_than_dropping_them(self):
        # The rule fillet's selection is the FACES, so an 'edges' list has nowhere to land.
        ff, _ = _install_edge_handles({"FA": _face_with_edges([]), "E1": _edge_ent()})
        res = fl.handler(fillet_type="rule", faces=["FA"], edges=["E1"], radius=1)
        assert res["isError"] is True and "'edges'" in res["message"]
        assert "fillet_type='constant'" in res["message"]
        assert ff.rule_last is None


class TestRuleFillet:

    def test_all_edges_takes_a_plain_face_list(self):
        f1, f2 = _face(), _face()
        ff, _ = _install_edge_handles({"F1": f1, "F2": f2})
        out = _payload(fl.handler(fillet_type="rule", faces=["F1", "F2"], radius=3,
                                          units="mm"))
        assert ff.rule_last.all_edges == [f1, f2]
        assert ff.rule_last.between is None
        assert ff.rule_last.radius == ("real", pytest.approx(0.3))
        assert out["rule"] == "all_edges" and out["faces_selected"] == 2
        assert out["fillet_type"] == "rule" and out["faces_created"] == 4

    def test_between_two_face_sets(self):
        f1, f2 = _face(), _face()
        ff, _ = _install_edge_handles({"F1": f1, "F2": f2})
        out = _payload(fl.handler(fillet_type="rule", faces=["F1"], second_faces=["F2"],
                                          radius=1))
        assert ff.rule_last.between == ([f1], [f2])
        assert ff.rule_last.all_edges is None
        assert out["rule"] == "between_faces" and out["faces_selected"] == 2

    def test_topology_maps_to_the_api_member(self):
        import adsk.fusion
        ff, _ = _install_edge_handles({"F1": _face()})
        _payload(fl.handler(fillet_type="rule", faces=["F1"], radius=1,
                                    topology="rounds_only"))
        assert (ff.rule_last.topologyType
                is adsk.fusion.RuleFilletTopologyTypes.RoundsOnlyRuleFilletTopologyType)

    def test_default_topology_takes_rounds_and_fillets(self):
        import adsk.fusion
        ff, _ = _install_edge_handles({"F1": _face()})
        _payload(fl.handler(fillet_type="rule", faces=["F1"], radius=1))
        assert (ff.rule_last.topologyType
                is adsk.fusion.RuleFilletTopologyTypes.RoundsAndFilletsRuleFilletTopologyType)

    def test_unknown_topology_refused(self):
        _install_edge_handles({"F1": _face()})
        res = fl.handler(fillet_type="rule", faces=["F1"], radius=1, topology="corners")
        assert res["isError"] is True and "topology" in res["message"]

    def test_needs_faces(self):
        _install_edge_handles({})
        res = fl.handler(fillet_type="rule", radius=1)
        assert res["isError"] is True and "faces" in res["message"]

    def test_an_edge_handle_in_faces_is_refused(self):
        _install_edge_handles({"E1": _edge_ent()})
        res = fl.handler(fillet_type="rule", faces=["E1"], radius=1)
        assert res["isError"] is True and "must be a face" in res["message"]

    def test_zero_created_faces_errors_and_rolls_back(self):
        ff, _ = _install_edge_handles({"F1": _face()})
        ff.rule_result = FakeCountingFeature("RuleFillet1", faces=0)
        res = fl.handler(fillet_type="rule", faces=["F1"], radius=1)
        assert res["isError"] is True and "rounded nothing" in res["message"]
        assert ff.rule_result.deleted is True

    def test_unchanged_volume_beats_a_positive_face_count(self):
        # The measured volume is the authority when it can be read: a feature reporting 4 created
        # faces over geometry that did not move is still a no-op, not a rounded body.
        body = make_body("B", [], volume=10.0)
        ff, _ = _install_edge_handles({"F1": BRepFace(surface=None, body=body)})
        res = fl.handler(fillet_type="rule", faces=["F1"], radius=1)
        assert res["isError"] is True and "rounded nothing" in res["message"]
        assert ff.rule_result.deleted is True

    def test_measured_volume_change_is_reported(self):
        body = make_body("B", [], volume=10.0)
        ff, _ = _install_edge_handles({"F1": BRepFace(surface=None, body=body)})
        ff.on_add = lambda: setattr(body, "volume", 9.75)
        out = _payload(fl.handler(fillet_type="rule", faces=["F1"], radius=1))
        assert out["volume_delta_cm3"] == -0.25

    def test_refused_face_set_errors(self):
        ff, _ = _install_edge_handles({"F1": _face()})
        ff.refuse_edge_set = True
        res = fl.handler(fillet_type="rule", faces=["F1"], radius=1)
        assert res["isError"] is True and "refused" in res["message"]

    def test_nonpositive_radius_refused(self):
        _install_edge_handles({"F1": _face()})
        res = fl.handler(fillet_type="rule", faces=["F1"], radius=0)
        assert res["isError"] is True and "positive radius" in res["message"]


class TestVolumeReadBack:

    def test_unchanged_volume_errors_and_rolls_back(self):
        # A fillet cuts a convex corner away or fills a concave one; an unchanged volume means the
        # feature exists but moved nothing, which must not read as filleted:true.
        body = make_body("B", [True], volume=10.0)
        ff, _ = _install([body])
        ff.result = FakeCountingFeature("Fillet1", faces=1)
        res = fl.handler(body_name="B", radius=1, edge_filter="all")
        assert res["isError"] is True and "moved no material" in res["message"]
        assert ff.result.deleted is True

    def test_measured_volume_change_is_reported(self):
        body = make_body("B", [True], volume=10.0)
        ff, _ = _install([body])
        ff.result = FakeCountingFeature("Fillet1", faces=1)
        ff.on_add = lambda: setattr(body, "volume", 9.5)
        out = _payload(fl.handler(body_name="B", radius=1, edge_filter="all"))
        assert out["volume_delta_cm3"] == -0.5

    def test_unreadable_volume_is_not_treated_as_a_no_op(self):
        ff, _ = _install([make_body("B", [True])])          # volume defaults to None
        ff.result = FakeCountingFeature("Fillet1", faces=1)
        out = _payload(fl.handler(body_name="B", radius=1, edge_filter="all"))
        assert out["filleted"] is True and "volume_delta_cm3" not in out


class TestRuleFilletReadsBackWhatItApplied:

    """ruleFilletSettings carries the applied radius and topology (live-verified: radius 0.3 cm and
    topologyType 0 read back off a real rule fillet), so a setter the API ignored is caught."""

    def test_a_radius_that_did_not_land_is_an_error(self):
        ff, _ = _install_edge_handles({"F1": _face()})
        ff.rule_settings_override = type(
            "S", (), {"radius": type("V", (), {"value": 9.9})(), "topologyType": 0})()
        res = fl.handler(fillet_type="rule", faces=["F1"], radius=3, units="mm")
        assert res["isError"] is True
        assert "radius reads back" in res["message"] and "RuleFillet1" in res["message"]

    def test_a_topology_that_did_not_land_is_an_error(self):
        ff, _ = _install_edge_handles({"F1": _face()})
        ff.rule_settings_override = type(
            "S", (), {"radius": type("V", (), {"value": 0.3})(), "topologyType": 99})()
        res = fl.handler(fillet_type="rule", faces=["F1"], radius=3, units="mm")
        assert res["isError"] is True and "topology is not the requested" in res["message"]

    def test_the_reported_radius_comes_from_the_feature(self):
        _install_edge_handles({"F1": _face()})
        out = _payload(fl.handler(fillet_type="rule", faces=["F1"], radius=3, units="mm"))
        assert out["radius"] == 3.0


class TestFilletGuardsBite:

    """Input guards on the three new fillet shapes - none had an asserting test."""

    def test_variable_without_edges_is_refused(self):
        _install_edge_handles({"F1": _face()})
        res = fl.handler(fillet_type="variable", radius=2, end_radius=5, units="mm")
        assert res["isError"] is True and "edges" in res["message"]

    def test_chord_length_without_its_length_is_refused(self):
        _install_edge_handles({"E1": _edge_ent("Bracket")})
        res = fl.handler(fillet_type="chord_length", edges=["E1"], units="mm")
        assert res["isError"] is True and "chord_length" in res["message"]

    def test_an_unknown_unit_is_refused(self):
        _install_edge_handles({"E1": _edge_ent("Bracket")})
        res = fl.handler(fillet_type="constant", edges=["E1"], radius=2, units="furlong")
        assert res["isError"] is True and "furlong" in res["message"]

    def test_rule_without_faces_is_refused(self):
        _install_edge_handles({"E1": _edge_ent("Bracket")})
        res = fl.handler(fillet_type="rule", radius=2, units="mm")
        assert res["isError"] is True and "faces" in res["message"]

    def test_a_position_at_either_endpoint_is_refused(self):
        # add() raises "position value must be greater than 0 and less than 1" - the interval is
        # open, and the two ends already carry 'radius' and 'end_radius'.
        for endpoint in (0.0, 1.0):
            _install_edge_handles({"E1": _edge_ent("Bracket")})
            res = fl.handler(fillet_type="variable", edges=["E1"], radius=2, end_radius=5,
                                     positions=[endpoint], radii=[3], units="mm")
            assert res["isError"] is True
            assert str(endpoint) in res["message"] and "exclusive" in res["message"]

    def test_an_interior_position_is_accepted(self):
        ff, _ = _install_edge_handles({"E1": _edge_ent("Bracket")})
        ff.result = FakeCountingFeature("Fillet1", faces=3)
        ff.on_add = lambda: setattr(ff, "_bumped", True)
        res = fl.handler(fillet_type="variable", edges=["E1"], radius=2, end_radius=5,
                                 positions=[0.5], radii=[3], units="mm")
        assert res.get("isError") is not True or "exclusive" not in res.get("message", "")

    def test_a_feature_reported_failed_is_an_error_not_a_tangent_diagnosis(self):
        # A mis-ordered variable chain yields healthState 2 with NO message and 0 faces. Reading
        # only the face count would blame a tangent edge - a cause that observation cannot support.
        ff, _ = _install_edge_handles({"E1": _edge_ent("Bracket")})
        ff.result = FakeCountingFeature("Fillet1", faces=0, health=2, message="")
        res = fl.handler(fillet_type="variable", edges=["E1"], radius=2, end_radius=5,
                                 units="mm")
        assert res["isError"] is True
        assert "FAILED" in res["message"] and "reports no message" in res["message"]
        assert "TANGENT edge" not in res["message"]
        assert ff.result.deleted is True              # the failed feature is rolled back

    def test_a_failed_feature_passes_fusions_own_message_through(self):
        ff, _ = _install_edge_handles({"E1": _edge_ent("Bracket")})
        ff.result = FakeCountingFeature("Fillet1", faces=0, health=2,
                                        message="radius too large for the edge")
        res = fl.handler(edges=["E1"], radius=2, units="mm")
        assert res["isError"] is True and "radius too large for the edge" in res["message"]

    def test_size_hint_only_when_the_platform_text_names_a_size_cause(self):
        # The retry hint is gated on the raise's own text: a size-flavored raise gets "try a
        # smaller value", any other raise (a tangent-chain conflict reads the same at every
        # radius) must NOT - that hint sent agents into shrink-and-retry loops no radius fixes.
        def raising(msg):
            def _raise():
                raise RuntimeError(msg)
            return _raise
        ff, _ = _install([make_body("B", [True])])
        ff.on_add = raising("5 : radius too large for adjacent geometry")
        res = fl.handler(body_name="B", radius=50, edge_filter="all")
        assert res["isError"] is True and "try a smaller value" in res["message"]

        ff, _ = _install([make_body("B", [True])])
        ff.on_add = raising("2 : FILLET_NO_EDGE_FOUND")
        res = fl.handler(body_name="B", radius=50, edge_filter="all")
        assert res["isError"] is True
        assert "try a smaller value" not in res["message"]
        assert "not necessarily the problem" in res["message"]


class TestRadiusTakesAParameterExpression:

    """A fillet radius may be a parameter EXPRESSION, so the fillet is driven by a user parameter
    rather than frozen at a number. Only the RADIUS: the chamfer's distance is compared as a number
    against the distance the created feature reports, and 'chord_length' is not opened here - both
    stay numeric, and their schemas say so."""

    def test_an_expression_radius_crosses_as_the_string_not_an_evaluated_number(self, monkeypatch):
        ff, _cf, _d = _parametric(monkeypatch, _install, [make_body("B", [True])])
        out = _payload(fl.handler(body_name="B", radius="WallT/2", units="mm",
                                          edge_filter="all"))
        _edges, val, _tangent = ff.last.edge_set
        assert val == ("string", "WallT/2")
        # not the evaluated 0.65 cm, and not any scaling of it: substituting the number would
        # freeze the fillet at today's value of the parameter
        assert val != ("real", 0.65) and val != ("real", 0.065)
        assert out["radius"] == "WallT/2"

    def test_a_literal_radius_still_crosses_as_a_scaled_number(self, monkeypatch):
        # the engine resolves nothing, so a literal routed through it would come back refused
        ff, _cf, _d = _parametric(monkeypatch, _install, [make_body("B", [True])],
                                  engine=_blind_engine())
        out = _payload(fl.handler(body_name="B", radius=2, units="mm", edge_filter="all"))
        assert ff.last.edge_set[1] == ("real", pytest.approx(0.2))
        assert out["radius"] == 2.0

    def test_a_numeric_STRING_is_a_literal_not_an_expression(self, monkeypatch):
        # '2' is a number written as a string - resolving it as an expression would tie the fillet
        # to nothing, and against this engine it would be refused outright
        ff, _cf, _d = _parametric(monkeypatch, _install, [make_body("B", [True])],
                                  engine=_blind_engine())
        out = _payload(fl.handler(body_name="B", radius="2", units="mm", edge_filter="all"))
        assert ff.last.edge_set[1] == ("real", pytest.approx(0.2))
        assert out["radius"] == 2.0

    def test_an_unresolvable_expression_is_refused_before_any_feature_is_built(self, monkeypatch):
        ff, _cf, _d = _parametric(monkeypatch, _install, [make_body("B", [True])])
        res = fl.handler(body_name="B", radius="Missing/2", units="mm", edge_filter="all")
        assert res["isError"] is True
        assert "'radius'" in res["message"] and "Missing/2" in res["message"]
        assert "param_get" in res["message"]
        assert ff.last is None            # createInput was never reached

    def test_the_expression_is_refused_ahead_of_the_edge_scope_guard(self, monkeypatch):
        # the same order a literal is judged in: the size before the edge scope
        _parametric(monkeypatch, _install, [make_body("B", [True])])
        res = fl.handler(body_name="B", radius="Missing/2", units="mm")
        assert res["isError"] is True and "did not evaluate" in res["message"]

    def test_a_variable_fillets_START_radius_takes_the_expression(self, monkeypatch):
        ff, _cf, _d = _parametric(monkeypatch, _install_edge_handles,
                                  {"E1": _edge_ent("Bracket")})
        ff.result = FakeCountingFeature("Fillet1", faces=1)
        _payload(fl.handler(fillet_type="variable", edges=["E1"], radius="WallT/2",
                                    end_radius=5, units="mm"))
        _edges, start, end, _pos, _rad = ff.last.variable_set
        assert start == ("string", "WallT/2")
        assert end == ("real", pytest.approx(0.5))     # end_radius stays a number

    def test_a_chord_LENGTH_is_still_a_number_only(self, monkeypatch):
        # the expression form is opened on 'radius' alone; chord_length's schema types it as a
        # number, and the refusal is what keeps the two surfaces saying the same thing
        _parametric(monkeypatch, _install_edge_handles, {"E1": _edge_ent("Bracket")})
        res = fl.handler(fillet_type="chord_length", edges=["E1"], chord_length="WallT/2",
                                 units="mm")
        assert res["isError"] is True and "'chord_length' must be a number." in res["message"]

    def test_the_positive_and_zero_guards_still_bite_on_a_literal(self, monkeypatch):
        _parametric(monkeypatch, _install, [make_body("B", [True])])
        for bad in (0, -1):
            res = fl.handler(body_name="B", radius=bad, units="mm", edge_filter="all")
            assert res["isError"] is True and "positive radius" in res["message"]

    def test_an_expression_evaluating_NON_POSITIVE_is_refused_naming_the_value(self, monkeypatch):
        # '-1 mm' and '0 mm' are legal expressions the engine resolves happily; only the evaluated
        # value catches them, and without it they reach filletFeatures.add
        for expr, value, shown in (("Neg", -1.0, "-1.0 mm"), ("Zero", 0.0, "0.0 mm")):
            ff, _cf, _d = _parametric(monkeypatch, _install, [make_body("B", [True])],
                                      engine=FakeUnitsManager(valid=(expr,), value=value))
            res = fl.handler(body_name="B", radius=expr, units="mm", edge_filter="all")
            assert res["isError"] is True, expr
            assert "positive radius" in res["message"] and f"'{expr}'" in res["message"]
            assert shown in res["message"]
            assert ff.last is None            # refused before the input was built

    def test_the_boundary_a_hair_ABOVE_zero_is_accepted(self, monkeypatch):
        # the guard is <= 0, not < 0: the smallest positive value must still build
        ff, _cf, _d = _parametric(monkeypatch, _install, [make_body("B", [True])],
                                  engine=FakeUnitsManager(valid=("Tiny",), value=0.001))
        _payload(fl.handler(body_name="B", radius="Tiny", units="mm", edge_filter="all"))
        assert ff.last.edge_set[1] == ("string", "Tiny")

    def test_a_units_engine_answering_a_NON_NUMBER_does_not_refuse_the_call(self, monkeypatch):
        # an unreadable evaluation is not evidence of a bad radius - it withholds the guard rather
        # than inventing a verdict
        engine = FakeUnitsManager(valid=("Odd",))
        engine.evaluateExpression = lambda expr, units=None: object()
        ff, _cf, _d = _parametric(monkeypatch, _install, [make_body("B", [True])], engine=engine)
        _payload(fl.handler(body_name="B", radius="Odd", units="mm", edge_filter="all"))
        assert ff.last.edge_set[1] == ("string", "Odd")


class TestRuleFilletRadiusTakesAnExpression:

    """The rule fillet's radius is the same input, so it takes the same two forms - and the applied
    radius is read back and compared on BOTH: a literal against its own scaled number, an
    expression against the value the units engine evaluated it to."""

    def _settings(self, cm, topology=0):
        return type("S", (), {"radius": type("V", (), {"value": cm})(),
                              "topologyType": topology})()

    def test_the_expression_reaches_the_rule_input_as_a_string(self, monkeypatch):
        ff, _cf, _d = _parametric(monkeypatch, _install_edge_handles, {"F1": _face()})
        out = _payload(fl.handler(fillet_type="rule", faces=["F1"], radius="WallT/2",
                                          units="mm"))
        assert ff.rule_last.radius == ("string", "WallT/2")
        assert out["radius_expression"] == "WallT/2"

    def test_an_unresolvable_rule_radius_is_refused_before_the_input_is_built(self, monkeypatch):
        ff, _cf, _d = _parametric(monkeypatch, _install_edge_handles, {"F1": _face()})
        res = fl.handler(fillet_type="rule", faces=["F1"], radius="Missing", units="mm")
        assert res["isError"] is True and "did not evaluate" in res["message"]
        assert ff.rule_last is None

    def test_a_settings_radius_that_disagrees_IS_an_error_for_an_expression(self, monkeypatch):
        # the case the skipped compare let through: asked for 6.5 mm, landed at 99, reported ok
        ff, _cf, _d = _parametric(monkeypatch, _install_edge_handles, {"F1": _face()})
        ff.rule_settings_override = self._settings(9.9)
        res = fl.handler(fillet_type="rule", faces=["F1"], radius="WallT/2", units="mm")
        assert res["isError"] is True
        assert "radius reads back 99.0 mm" in res["message"]
        assert "6.5 mm" in res["message"] and "'WallT/2'" in res["message"]

    def test_the_same_disagreement_IS_an_error_for_a_literal(self, monkeypatch):
        ff, _cf, _d = _parametric(monkeypatch, _install_edge_handles, {"F1": _face()})
        ff.rule_settings_override = self._settings(9.9)
        res = fl.handler(fillet_type="rule", faces=["F1"], radius=3, units="mm")
        assert res["isError"] is True and "radius reads back" in res["message"]

    def test_a_radius_MATCHING_the_evaluated_expression_is_accepted(self, monkeypatch):
        # the other half of the compare: the applied radius IS what the expression evaluates to
        ff, _cf, _d = _parametric(monkeypatch, _install_edge_handles, {"F1": _face()})
        ff.rule_settings_override = self._settings(0.65)
        out = _payload(fl.handler(fillet_type="rule", faces=["F1"], radius="WallT/2",
                                          units="mm"))
        assert out["radius"] == 6.5 and out["radius_expression"] == "WallT/2"

    def test_a_literal_radius_publishes_no_expression_key(self, monkeypatch):
        _parametric(monkeypatch, _install_edge_handles, {"F1": _face()})
        out = _payload(fl.handler(fillet_type="rule", faces=["F1"], radius=3, units="mm"))
        assert out["radius"] == 3.0 and "radius_expression" not in out

    def test_the_topology_check_still_bites_under_an_expression(self, monkeypatch):
        # the radius here MATCHES what the expression evaluates to, so the topology mismatch is the
        # one thing left to report
        ff, _cf, _d = _parametric(monkeypatch, _install_edge_handles, {"F1": _face()})
        ff.rule_settings_override = self._settings(0.65, topology=99)
        res = fl.handler(fillet_type="rule", faces=["F1"], radius="WallT/2", units="mm")
        assert res["isError"] is True and "topology is not the requested" in res["message"]

    def test_a_rule_expression_evaluating_NON_POSITIVE_is_refused(self, monkeypatch):
        # both sides of the <= 0 boundary a legal expression can land on
        for expr, value, shown in (("Neg", -2.0, "-2.0 mm"), ("Zero", 0.0, "0.0 mm")):
            ff, _cf, _d = _parametric(monkeypatch, _install_edge_handles, {"F1": _face()},
                                      engine=FakeUnitsManager(valid=(expr,), value=value))
            res = fl.handler(fillet_type="rule", faces=["F1"], radius=expr, units="mm")
            assert res["isError"] is True, expr
            assert "positive radius" in res["message"] and shown in res["message"]
            assert ff.rule_last is None

    def test_an_UNREADABLE_evaluation_withholds_the_compare(self, monkeypatch):
        # want_cm None means nothing was measured to compare the applied radius against. Comparing
        # against 0 instead would roll a healthy fillet out reporting "reads back 6.5 mm, not the
        # requested 0.0" - a verdict from a read that never answered.
        engine = FakeUnitsManager(valid=("Odd",))
        engine.evaluateExpression = lambda expr, units=None: object()
        ff, _cf, _d = _parametric(monkeypatch, _install_edge_handles, {"F1": _face()},
                                  engine=engine)
        ff.rule_settings_override = self._settings(0.65)
        out = _payload(fl.handler(fillet_type="rule", faces=["F1"], radius="Odd",
                                          units="mm"))
        assert out["radius"] == 6.5 and out["radius_expression"] == "Odd"
