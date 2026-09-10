"""Unit tests for model_chamfer.py - the three chamfer definitions and their read-backs."""

import math
import pytest
import types
from conftest import (BRepBody, BRepEdge, BRepFace, FakePoint, FakeUnitsManager, FakeVector3D,
                      MakeComp, install, load_tool, make_design, payload as _payload,
                      _NamedCollection)

fl = load_tool("model_chamfer")
edge_common_mod = load_tool("_edge_common")


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
        # The cm an EQUAL-DISTANCE chamfer's own definition reports; None leaves the feature
        # answering no definition at all, which is how the read degrades when nothing answers.
        self.equal_distance_cm = None
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
        elif self.equal_distance_cm is not None:
            self.result.chamferTypeDefinition = type("D", (), {
                "distance": type("P", (), {"value": self.equal_distance_cm})()})()
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


class _NoNumberEngine(FakeUnitsManager):
    """A units engine that RESOLVES an expression and answers something that is not a number, which
    is the deliberate value_cm=None path of length_value_input."""

    def evaluateExpression(self, expression, units=""):
        return None


def _edge_ent(token=None):
    """A BRep edge resolved from a handle, or bounding a face. Its body's volume does not read, so
    the material gate has nothing to judge."""
    edge = BRepEdge(curve=None, entity_token=token)
    edge.body = BRepBody(name="Block", volume=None)
    return edge


def _face_with_edges(edges):
    """A BRep face carrying the edges that bound it - what a face-scoped chamfer expands."""
    return BRepFace(surface=None, body_name="Block", edges=edges)


def _install_handles(handle_map):
    """Install a design whose findEntityByToken resolves `handle_map` - the seam a face/edge handle
    list reads through before its isinstance(BRepEdge/BRepFace) gate."""
    ff = FakeFilletFeatures(); cf = FakeChamferFeatures()
    install(fl, make_design(comp=_component([], ff, cf), tokens=handle_map))
    import adsk.fusion, adsk.core
    adsk.fusion.BRepEdge = BRepEdge
    adsk.fusion.BRepFace = BRepFace
    adsk.core.ValueInput.createByReal = staticmethod(lambda v: ("real", v))
    return ff, cf


def _parametric(monkeypatch, installer, *args, engine=None, **kw):
    """`installer` plus conftest's shared units engine on the design and a
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


class TestGuards:

    def test_an_unresolvable_distance_expression_is_refused_naming_it(self, monkeypatch):
        # The distance takes a parameter expression, so a string naming no parameter is refused BY
        # NAME by the units engine rather than reaching the feature.
        _parametric(monkeypatch, _install, [make_body("B", [True])])
        res = fl.handler(body_name="B", distance="big", edge_filter="all")
        assert res["isError"] is True
        assert "'distance' expression 'big' did not evaluate" in res["message"]


class TestFillet:

    def test_chamfer_zero_faces_errors_as_partial(self):
        # A chamfer creates one bevel face per edge it actually cut, and the feature offers no
        # per-edge read-back besides its faces (no .edges collection, measured live) - so a chamfer
        # whose feature holds ZERO faces cut nothing, and the partial-application guard converts it
        # to an error and rolls the inert feature back.
        _, cf = _install([make_body("B", [True])])
        cf.result = FakeCountingFeature("Chamfer1", faces=0)
        res = fl.handler(body_name="B", distance=1, edge_filter="all")
        assert res["isError"] is True
        assert "PARTIALLY" in res["message"]
        assert cf.result.deleted is True

    def test_chamfer_partial_edge_application_errors_and_rolls_back(self):
        # The SAME partial-application guard applies to chamfer as to fillet: 2 edges requested, the
        # feature's own edge count reads only 1 applied - error naming both counts, and the partial
        # feature is rolled back.
        _, cf = _install([make_body("B", [True, True])])
        cf.result = FakeCountingFeature("Chamfer1", faces=1)
        res = fl.handler(body_name="B", distance=1, edge_filter="all")
        assert res["isError"] is True
        assert "PARTIALLY" in res["message"]
        assert "2" in res["message"] and "1" in res["message"]
        assert cf.result.deleted is True


class TestChamfer:

    def test_chamfer_scales_distance(self):
        _, cf = _install([make_body("B", [True, True])])
        out = _payload(fl.handler(body_name="B", distance=1, units="in", edge_filter="all"))
        assert out["chamfered"] is True
        assert cf.last.distance == ("real", 2.54)

    def test_two_distance_chamfer(self):
        _, cf = _install([make_body("B", [True, True])])
        out = _payload(fl.handler(body_name="B", distance=2, distance_two=4, units="mm", edge_filter="all"))
        # setToTwoDistances used (not equal-distance), both scaled to cm
        assert cf.last.two_distances == (("real", 0.2), ("real", 0.4))
        assert cf.last.distance is None
        assert out["distance_two"] == 4

    def test_equal_distance_when_no_second(self):
        _, cf = _install([make_body("B", [True, True])])
        out = _payload(fl.handler(body_name="B", distance=2, units="mm", edge_filter="all"))
        assert cf.last.two_distances is None
        assert cf.last.distance == ("real", 0.2)
        assert "distance_two" not in out

    def test_a_refused_equal_distance_is_an_error(self):
        # The refusal message must name the requested size, not fall through the handler's
        # catch-all as an exception about an undefined name.
        _, cf = _install([make_body("B", [True])])
        cf.refuse = "equal"
        res = fl.handler(body_name="B", distance=2, units="mm", edge_filter="all")
        assert res["isError"] is True and "equal-distance chamfer of 2.0 mm" in res["message"]
        assert cf.added == 0

    def test_a_refused_two_distance_is_an_error(self):
        _, cf = _install([make_body("B", [True])])
        cf.refuse = "two"
        res = fl.handler(body_name="B", distance=2, distance_two=3, units="mm",
                                  edge_filter="all")
        assert res["isError"] is True and "two-distance chamfer (2.0 mm / 3.0 mm)" in res["message"]
        assert cf.added == 0


class TestDistanceAndAngle:

    """The third chamfer definition: setToDistanceAndAngle(distance, angle)."""

    def test_angle_routes_to_set_to_distance_and_angle(self):
        _, cf = _install([make_body("B", [True, True])])
        out = _payload(fl.handler(body_name="B", distance=2, angle_deg=30, units="mm",
                                           edge_filter="all"))
        distance, angle = cf.last.distance_and_angle
        assert distance == ("real", pytest.approx(0.2))          # 2 mm -> cm
        # Measured live: a REAL ValueInput angle is read as RADIANS - setToDistanceAndAngle(0.3,
        # radians(30)) cut legs of 0.3 cm and 0.1732 cm (= 0.3*tan(30)).
        assert angle == ("real", pytest.approx(0.5235987755982988))
        assert cf.last.distance is None and cf.last.two_distances is None
        assert out["angle_deg"] == 30 and "distance_two" not in out

    def test_the_angle_is_not_scaled_by_the_length_units(self):
        # 'units' scales the distance only - an inch job must not multiply the angle by 2.54.
        _, cf = _install([make_body("B", [True])])
        _payload(fl.handler(body_name="B", distance=1, angle_deg=45, units="in",
                                     edge_filter="all"))
        distance, angle = cf.last.distance_and_angle
        assert distance == ("real", pytest.approx(2.54))
        assert angle == ("real", pytest.approx(math.radians(45)))

    def test_angle_with_distance_two_is_refused_naming_both(self):
        # Two DIFFERENT definitions of the same chamfer; silently dropping one would bevel
        # something other than what was asked for.
        _, cf = _install([make_body("B", [True])])
        res = fl.handler(body_name="B", distance=2, distance_two=3, angle_deg=45,
                                  edge_filter="all")
        assert res["isError"] is True
        assert "'angle_deg' (45)" in res["message"] and "'distance_two' (3)" in res["message"]
        assert cf.last is None and cf.added == 0      # refused before any input was even created

    def test_a_refused_distance_and_angle_errors_naming_the_values(self):
        _, cf = _install([make_body("B", [True])])
        cf.refuse = "angle"
        res = fl.handler(body_name="B", distance=2, angle_deg=45, units="mm",
                                  edge_filter="all")
        assert res["isError"] is True
        assert "2.0 mm at 45.0 deg" in res["message"]
        assert cf.added == 0                          # nothing was added on a refused definition

    def test_a_nonpositive_angle_is_refused(self):
        _install([make_body("B", [True])])
        res = fl.handler(body_name="B", distance=2, angle_deg=0, edge_filter="all")
        assert res["isError"] is True and "'angle_deg' must be positive, got 0.0" in res["message"]

    def test_a_nonnumeric_angle_is_refused(self):
        _install([make_body("B", [True])])
        res = fl.handler(body_name="B", distance=2, angle_deg="steep", edge_filter="all")
        assert res["isError"] is True and "'angle_deg' must be a number" in res["message"]

    def test_no_angle_leaves_the_equal_distance_path_alone(self):
        _, cf = _install([make_body("B", [True])])
        out = _payload(fl.handler(body_name="B", distance=2, units="mm", edge_filter="all"))
        assert cf.last.distance_and_angle is None
        assert "angle_deg" not in out


class TestCornerType:

    """ChamferFeatureInput.cornerType - a PROPERTY on the classic input, so it goes through the
    set-then-read-back a SWIG proxy demands."""

    def test_the_map_carries_the_apis_own_member_spelling(self):
        # The API spells it BlendCornertype, lowercase 't'; "correcting" it to BlendCornerType
        # names a member that does not exist, and getattr would raise on the live enum.
        assert edge_common_mod._CORNER_TYPES == {"chamfer": "ChamferCornerType", "miter": "MiterCornerType",
                                    "blend": "BlendCornertype"}

    @pytest.mark.parametrize("key,member", [("chamfer", "ChamferCornerType"),
                                            ("miter", "MiterCornerType"),
                                            ("blend", "BlendCornertype")])
    def test_each_option_lands_the_matching_enum_member(self, key, member):
        import adsk.fusion
        _, cf = _install([make_body("B", [True])])
        out = _payload(fl.handler(body_name="B", distance=1, edge_filter="all",
                                           corner_type=key))
        assert cf.last.cornerType == getattr(adsk.fusion.ChamferCornerTypes, member)
        assert out["corner_type"] == key

    def test_omitting_corner_type_leaves_the_api_default_untouched(self):
        _, cf = _install([make_body("B", [True])])
        out = _payload(fl.handler(body_name="B", distance=1, edge_filter="all"))
        assert cf.last.cornerType == _UNSET_CORNER      # never assigned
        assert "corner_type" not in out

    def test_an_unknown_corner_type_is_refused(self):
        _, cf = _install([make_body("B", [True])])
        res = fl.handler(body_name="B", distance=1, edge_filter="all",
                                  corner_type="rounded")
        assert res["isError"] is True
        assert "'corner_type' must be one of" in res["message"] and "rounded" in res["message"]
        assert cf.added == 0

    def test_a_corner_type_the_input_never_took_is_an_error(self, monkeypatch):
        # A SWIG proxy accepts an assignment to a name it does not define; the value lands nowhere
        # and the chamfer would run on the DEFAULT corner while the payload claimed 'miter'.
        _, cf = _install([make_body("B", [True])])
        monkeypatch.setattr(FakeChamferInput, "swallow_corner_set", True)
        res = fl.handler(body_name="B", distance=1, edge_filter="all", corner_type="miter")
        assert res["isError"] is True
        assert "did not take" in res["message"] and "corner_type='miter'" in res["message"]
        assert cf.added == 0

    def test_an_unavailable_enum_family_is_reported_not_guessed(self):
        import adsk.fusion
        _, cf = _install([make_body("B", [True])])
        adsk.fusion.ChamferCornerTypes = None
        res = fl.handler(body_name="B", distance=1, edge_filter="all", corner_type="blend")
        assert res["isError"] is True and "not available on this Fusion version" in res["message"]
        assert cf.added == 0

    def test_corner_type_rides_along_with_a_distance_and_angle_chamfer(self):
        import adsk.fusion
        _, cf = _install([make_body("B", [True])])
        out = _payload(fl.handler(body_name="B", distance=1, angle_deg=45,
                                           edge_filter="all", corner_type="miter"))
        assert cf.last.distance_and_angle is not None
        assert cf.last.cornerType == adsk.fusion.ChamferCornerTypes.MiterCornerType
        assert out["angle_deg"] == 45 and out["corner_type"] == "miter"


class TestChamferReadsBackWhatItBuilt:

    """The INPUT taking a value is not the feature building it, and a chamfer leaves no indirect
    trace - measured live, all three corner types build the same face count. So the corner type,
    the definition kind and the distance/angle pair are read off the created feature."""

    def test_a_feature_reporting_a_different_corner_errors_and_rolls_back(self):
        import adsk.fusion
        _, cf = _install([make_body("B", [True])])
        cf.result = FakeCountingFeature("Chamfer1", faces=1)
        cf.corner_override = (adsk.fusion.ChamferCornerTypes.ChamferCornerType,)
        res = fl.handler(body_name="B", distance=1, edge_filter="all", corner_type="blend")
        assert res["isError"] is True
        want = adsk.fusion.ChamferCornerTypes.BlendCornertype
        got = adsk.fusion.ChamferCornerTypes.ChamferCornerType
        assert f"reads back {got}" in res["message"]          # what the feature says
        assert f"'blend' ({want})" in res["message"]          # what was asked for
        assert cf.result.deleted is True and "rolled back" in res["message"]

    def test_a_feature_reporting_a_different_angle_errors_and_rolls_back(self):
        _, cf = _install([make_body("B", [True])])
        cf.result = FakeCountingFeature("Chamfer1", faces=1)
        cf.definition_override = (0.2, math.radians(30))     # asked 45 deg, built 30
        res = fl.handler(body_name="B", distance=2, angle_deg=45, units="mm",
                                  edge_filter="all")
        assert res["isError"] is True
        assert "angle reads back 30.0 deg" in res["message"] and "requested 45.0" in res["message"]
        assert cf.result.deleted is True and "rolled back" in res["message"]

    def test_a_feature_reporting_a_different_distance_errors_and_rolls_back(self):
        _, cf = _install([make_body("B", [True])])
        cf.result = FakeCountingFeature("Chamfer1", faces=1)
        cf.definition_override = (0.5, math.radians(45))     # asked 2 mm, built 5 mm
        res = fl.handler(body_name="B", distance=2, angle_deg=45, units="mm",
                                  edge_filter="all")
        assert res["isError"] is True
        assert "distance reads back 5.0" in res["message"] and "requested 2.0" in res["message"]
        assert cf.result.deleted is True

    def test_a_feature_built_as_another_definition_errors(self):
        # A distance-and-angle request that fell back to equal-distance would still read a
        # plausible distance; chamferType is what names the definition that got built.
        _, cf = _install([make_body("B", [True])])
        cf.result = FakeCountingFeature("Chamfer1", faces=1)
        cf.chamfer_type_override = ("equal-distance-type",)
        res = fl.handler(body_name="B", distance=2, angle_deg=45, edge_filter="all")
        assert res["isError"] is True
        assert "chamfer type equal-distance-type" in res["message"]
        assert cf.result.deleted is True

    def test_the_reported_values_come_from_the_feature_not_the_request(self):
        # The feature answers a distance/angle that round-trip within tolerance of the request, so
        # the call succeeds - and the payload carries the FEATURE's numbers.
        _, cf = _install([make_body("B", [True])])
        cf.result = FakeCountingFeature("Chamfer1", faces=1)
        cf.definition_override = (0.2, math.radians(45))
        out = _payload(fl.handler(body_name="B", distance=2, angle_deg=45, units="mm",
                                           edge_filter="all"))
        assert out["angle_deg"] == 45.0 and out["distance"] == 2.0
        # both fields are named as READ BACK - a value that merely survived as the request echo
        # would be missing from this list even though the number happens to match.
        assert "angle_deg/distance are read back off the created feature" in out["note"]
        assert "angle_deg_unverified" not in out
        assert "chamfer_type_unverified" not in out

    def test_an_unreadable_definition_is_flagged_never_echoed(self):
        _, cf = _install([make_body("B", [True])])
        cf.result = FakeCountingFeature("Chamfer1", faces=1)
        cf.definition_override = False        # the feature answers no chamferTypeDefinition
        out = _payload(fl.handler(body_name="B", distance=2, angle_deg=45, units="mm",
                                           edge_filter="all"))
        assert "angle_deg" not in out                      # never echoed as if confirmed
        assert out["angle_deg_unverified"] is True and out["distance_unverified"] is True
        assert "could NOT be read back" in out["note"]

    def test_an_unreadable_corner_type_is_flagged_never_echoed(self):
        _, cf = _install([make_body("B", [True])])
        cf.result = FakeCountingFeature("Chamfer1", faces=1)
        cf.corner_override = False            # the feature does not answer cornerType
        out = _payload(fl.handler(body_name="B", distance=1, edge_filter="all",
                                           corner_type="miter"))
        assert "corner_type" not in out
        assert out["corner_type_unverified"] is True

    def test_an_equal_distance_chamfer_reads_back_nothing_extra(self):
        # The definition read-back is scoped to the distance-and-angle path; the other two
        # definitions are unchanged and carry no verified/unverified fields.
        _, cf = _install([make_body("B", [True])])
        cf.result = FakeCountingFeature("Chamfer1", faces=1)
        out = _payload(fl.handler(body_name="B", distance=2, units="mm",
                                           edge_filter="all"))
        assert not [key for key in out if key.endswith("_unverified")]
        assert "angle_deg" not in out and "corner_type" not in out


class TestVolumeReadBack:

    def test_chamfer_with_unchanged_volume_errors_and_rolls_back(self):
        # A chamfer that moved no material must not read chamfered:true - the same volume gate as
        # its fillet sibling (a bevel always removes or adds material).
        body = make_body("B", [True], volume=10.0)
        _, cf = _install([body])
        cf.result = FakeCountingFeature("Chamfer1", faces=1)
        res = fl.handler(body_name="B", distance=1, edge_filter="all")
        assert res["isError"] is True and "moved no material" in res["message"]
        assert cf.result.deleted is True

    def test_chamfer_reports_volume_delta_like_its_sibling(self):
        body = make_body("B", [True], volume=10.0)
        _, cf = _install([body])
        cf.result = FakeCountingFeature("Chamfer1", faces=1)
        cf.on_add = lambda: setattr(body, "volume", 9.75)
        out = _payload(fl.handler(body_name="B", distance=1, edge_filter="all"))
        assert out["chamfered"] is True and out["volume_delta_cm3"] == -0.25


class TestDistanceTakesAParameterExpression:

    """The chamfer DISTANCE may be a parameter EXPRESSION, so the bevel follows a user parameter
    rather than freezing at a number - the form model_fillet's radius takes. 'chord_length' is not
    opened here and stays numeric, and the schemas say so."""

    def test_an_expression_crosses_as_a_string_value_input_and_is_echoed(self, monkeypatch):
        # createByString ties the feature to the live parameter; createByReal would freeze a number.
        _, cf, _design = _parametric(monkeypatch, _install, [make_body("B", [True])])
        cf.result = FakeCountingFeature("Chamfer1", faces=1)
        out = _payload(fl.handler(body_name="B", distance="WallT/2", units="mm", edge_filter="all"))
        assert cf.last.distance == ("string", "WallT/2")
        assert out["distance"] == "WallT/2"
        # this feature answers no definition, so the value is flagged unconfirmed, never echoed as read
        assert out["distance_unverified"] is True

    def test_a_landed_expression_is_read_back_off_the_feature(self, monkeypatch):
        _, cf, _design = _parametric(monkeypatch, _install, [make_body("B", [True])])
        cf.result = FakeCountingFeature("Chamfer1", faces=1)
        cf.equal_distance_cm = 0.65                  # what 'WallT/2' evaluates to, in internal cm
        out = _payload(fl.handler(body_name="B", distance="WallT/2", units="mm", edge_filter="all"))
        assert out["distance"] == 6.5                # the FEATURE's number, in mm
        assert out["distance_expression"] == "WallT/2"

    def test_a_distance_the_feature_did_not_take_rolls_the_chamfer_back(self, monkeypatch):
        _, cf, _design = _parametric(monkeypatch, _install, [make_body("B", [True])])
        cf.result = FakeCountingFeature("Chamfer1", faces=1)
        cf.equal_distance_cm = 0.30                  # not the 0.65 'WallT/2' evaluates to
        res = fl.handler(body_name="B", distance="WallT/2", units="mm", edge_filter="all")
        assert res["isError"] is True
        assert "reads back 3.0, not the requested 6.5" in res["message"]
        assert cf.result.deleted is True

    def test_an_expression_answering_no_number_publishes_the_distance_unconfirmed(self, monkeypatch):
        # value_cm is None here, so there is nothing to compare the feature's own distance against.
        # Subtracting from it would RAISE after chamferFeatures.add() had already landed a feature -
        # a mutation left in the model with no isError.
        _, cf, _design = _parametric(monkeypatch, _install, [make_body("B", [True])],
                                     engine=_NoNumberEngine())
        cf.result = FakeCountingFeature("Chamfer1", faces=1)
        cf.equal_distance_cm = 0.65
        out = _payload(fl.handler(body_name="B", distance="WallT/2", units="mm", edge_filter="all"))
        assert out["distance"] == "WallT/2"
        assert out["distance_unverified"] is True
        assert "distance_expression" not in out       # nothing was verified, so nothing is claimed

    def test_a_single_read_back_field_reads_as_a_sentence(self):
        # MEASURED LIVE: an expression chamfer verifies exactly ONE field, and the note read
        # "distance are read back" - the plural is wrong for the common case.
        body = make_body("B", [True], volume=10.0)
        _, cf = _install([body])
        cf.result = FakeCountingFeature("Chamfer1", faces=1)
        cf.on_add = lambda: setattr(body, "volume", 9.75)
        out = _payload(fl.handler(body_name="B", distance=2, units="mm", edge_filter="all",
                                  corner_type="miter"))
        assert "corner_type is read back off the created feature" in out["note"]

    def test_exactly_the_read_back_band_lands_and_one_step_past_it_does_not(self):
        # The band is 1e-6 cm. Measured against 0 so the subtraction is exact: a difference OF the
        # band is the same distance, more than it is a value that did not take.
        band = 1e-6
        assert edge_common_mod._distance_mismatch(band, 0.0, 0.1) == ""
        assert "reads back" in edge_common_mod._distance_mismatch(2 * band, 0.0, 0.1)


class TestFaceScopedChamfer:

    """'faces' bevels every edge of the named faces - the selection model_fillet's rule fillet
    offers, expanded to edges here because a chamfer has no rule API."""

    def test_every_edge_of_the_named_faces_is_taken_once(self):
        # Two faces meeting at one edge: the shared edge belongs to both, and the same edge twice in
        # the feature's edge set is a duplicate.
        shared, only_a, only_b = _edge_ent("e0"), _edge_ent("e1"), _edge_ent("e2")
        _, cf = _install_handles({"FA": _face_with_edges([shared, only_a]),
                                  "FB": _face_with_edges([shared, only_b])})
        out = _payload(fl.handler(faces=["FA", "FB"], distance=1))
        assert out["edges_requested"] == 3
        assert out["faces_selected"] == 2
        assert out["edge_selection"] == "3 edge(s) of 2 face(s)"

    def test_faces_override_an_edge_filter(self):
        # the input says "overrides 'edge_filter'" - the faces decide the set and no body sweep runs
        _install_handles({"FA": _face_with_edges([_edge_ent("e1"), _edge_ent("e2")])})
        out = _payload(fl.handler(faces=["FA"], edge_filter="convex", distance=1))
        assert out["edge_selection"] == "2 edge(s) of 1 face(s)"
        assert "edges_convex" not in out

    def test_faces_and_edges_together_are_refused(self):
        _install_handles({"FA": _face_with_edges([_edge_ent("e1")]), "E1": _edge_ent("e1")})
        res = fl.handler(faces=["FA"], edges=["E1"], distance=1)
        assert res["isError"] is True and "'faces' or 'edges', not both" in res["message"]

    def test_faces_that_answer_no_edges_refuse_before_any_feature_is_created(self):
        _, cf = _install_handles({"FA": _face_with_edges([])})
        res = fl.handler(faces=["FA"], distance=1)
        assert res["isError"] is True and "answered any edges" in res["message"]
        assert cf.added == 0
