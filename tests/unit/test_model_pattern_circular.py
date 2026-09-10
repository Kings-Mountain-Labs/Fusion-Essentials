"""Unit tests for model_pattern_circular.py - the ring, its axis and its counts."""

import types
from conftest import (BRepBody, BRepEdge, BRepFace, Cylinder, FakePoint, FakeVector3D, Line3D,
                      MakeComp, MakeDesign, Plane, _NamedCollection, _SimpleNamed, install,
                      load_tool, make_occurrence, payload)

pt = load_tool("model_pattern_circular")


class FakeRectInput:
    def __init__(self, coll, d1, q1, dist1, dist_type, refuse_two=False):
        self.coll = coll
        self.d1 = d1
        self.q1 = q1
        self.dist1 = dist1
        self.dist_type = dist_type
        self.dir_two = None
        self._refuse_two = refuse_two

    def setDirectionTwo(self, d2, q2, dist2):
        # The live setter ANSWERS whether it took; a refusal must not be read as success.
        if self._refuse_two:
            return False
        self.dir_two = (d2, q2, dist2)
        return True


def _component_key(comp):
    """What identifies a COMPONENT across two references to it: its entityToken, falling back to
    identity when a fixture supplies none. Never the object itself - see entity_proxy."""
    return getattr(comp, "entityToken", None) or id(comp)


def _add_refusal(direction, comp):
    """The message the kernel raises at add() for a direction it will not build with, or ''.

    MEASURED, and only at add(): the input accepts a face and a foreign NATIVE entity, and reads
    both back unchanged, so nothing before add() can see the refusal coming.

    The kernel discriminates on the COMPONENT, not on the wrapper object: two references to one
    component are different Python objects sharing one entityToken, so this compares tokens. Keyed by
    identity the fake would refuse an entity from its OWN component and model a kernel that does not
    exist."""
    if isinstance(direction, BRepFace):
        return "3 : Unsupported direction entity's type."
    if isinstance(direction, BRepEdge) and getattr(direction, "assemblyContext", None) is None:
        body = getattr(direction, "body", None)
        owner = getattr(body, "parentComponent", comp) if body is not None else comp
        if owner is not comp and _component_key(owner) != _component_key(comp):
            return "InternalValidationError : res"
    return ""


class FakeRectFeatures:
    def __init__(self, refuse_two=False):
        self.last_input = None
        self.add_calls = 0
        self.comp = None                 # the component this collection hangs off (set by its owner)
        self._refuse_two = refuse_two

    def createInput(self, coll, d1, q1, dist1, dist_type):
        self.last_input = FakeRectInput(coll, d1, q1, dist1, dist_type, self._refuse_two)
        return self.last_input

    def add(self, inp):
        self.add_calls += 1
        for d in (inp.d1, inp.dir_two[0] if inp.dir_two else None):
            msg = _add_refusal(d, self.comp)
            if msg:
                raise RuntimeError(msg)
        return type("F", (), {"name": "R-Pattern1"})()


class FakeCircInput:
    """`ignores` names properties whose assignment the platform silently DROPS - the SWIG-proxy
    shape set_verified exists to catch (the value lands on a dead attribute, no exception, and the
    object keeps its API default)."""
    def __init__(self, coll, axis, ignores=()):
        object.__setattr__(self, "_ignores", set(ignores))
        self.coll = coll
        self.axis = axis
        self.quantity = None
        self.totalAngle = None
        self.isSymmetric = False

    def __setattr__(self, name, value):
        if name in self._ignores:
            return
        object.__setattr__(self, name, value)


class FakeCircFeatures:
    """`elements` is how many patternElements the created feature reports; None means as many as
    the request asked for, which is what a pattern that fully built answers."""
    def __init__(self, ignores=(), elements=None):
        self.last_input = None
        self.ignores = ignores
        self.add_calls = 0
        self.elements = elements

    def createInput(self, coll, axis):
        self.last_input = FakeCircInput(coll, axis, self.ignores)
        return self.last_input

    def add(self, inp):
        self.add_calls += 1
        asked = int(inp.quantity[1]) if isinstance(inp.quantity, tuple) else 0
        n = asked if self.elements is None else self.elements
        return types.SimpleNamespace(name="C-Pattern1",
                                     patternElements=_NamedCollection([None] * n))


def _root(occurrences, rf, cf):
    """The design's root: the occurrence census, the three origin axes and both pattern
    feature collections."""
    # Components carry an entityToken and are compared on it: a wrapper is never identity-stable
    # (two reads of design.rootComponent are DIFFERENT objects sharing one token), so a fake that
    # keys on id() models a stability the platform does not have.
    root = MakeComp(name="Root", occurrences=occurrences, entity_token="TOKEN:Root",
                    construction_axes=("AXIS_X", "AXIS_Y", "AXIS_Z"))
    root.features = types.SimpleNamespace(rectangularPatternFeatures=rf,
                                          circularPatternFeatures=cf)
    rf.comp = root               # so add() can tell an OWN entity from a foreign native one
    # Components placed nowhere by default; a test that patterns across components installs its
    # own mapping (Fusion's root-level component -> its occurrences lookup).
    root.occurrences_by_component = {}
    root.allOccurrencesByComponent = lambda comp: _occurrences_of(root, comp)
    return root


def _occurrences_of(root, comp):
    """The occurrence collection a root exposes for one component.

    Keyed by entityToken, not id(): two references to one component are different Python objects, so
    an id()-keyed fake would answer for one wrapper and not for another wrapper of the SAME
    component."""
    return _NamedCollection(root.occurrences_by_component.get(_component_key(comp), []))


def _install(occ_names, refuse_two=False, circ_ignores=(), circ_elements=None):
    rf, cf = FakeRectFeatures(refuse_two), FakeCircFeatures(circ_ignores, circ_elements)
    occs = [make_occurrence(n, component=MakeComp(name=n.split(":")[0])) for n in occ_names]
    install(pt, MakeDesign(comp=_root(occs, rf, cf)))
    import adsk.fusion, adsk.core
    adsk.core.ValueInput.createByReal = staticmethod(lambda v: ("real", v))
    adsk.core.ValueInput.createByString = staticmethod(lambda s: ("str", s))
    pdt = adsk.fusion.PatternDistanceType
    pdt.SpacingPatternDistanceType = "Spacing"
    pdt.ExtentPatternDistanceType = "Extent"
    return rf, cf


def _install_with_bodies(body_map):
    """Install a design that also resolves body handles/names (the app-reference seam).

    The handler resolves its design via _common.design() (the SAME seam _inputs uses), so there is
    ONE design: _install's root already carries the construction axes the handler needs, and gains
    the body collection plus the token map here."""
    rf, cf = _install([])
    import adsk.fusion
    adsk.fusion.BRepBody = BRepBody
    design = pt.app.activeProduct
    design.rootComponent.bRepBodies = _NamedCollection(list(body_map.values()))
    design.findEntityByToken = lambda t, bm=body_map: ([bm[t]] if t in bm else [])
    return rf, cf


def _install_body_in_subcomponent():
    """A body whose parentComponent is a distinct sub-component (its OWN axes + pattern features).
    Taking the axis from ROOT and building the feature there mismatches the body's object path -
    Fusion raises 'InternalValidationError getObjectPath'. _owning_component must resolve to the
    body's parent."""
    _install([])                    # installs the adsk fakes (ValueInput, ObjectCollection, axes)
    import adsk.fusion
    adsk.fusion.BRepBody = BRepBody
    # the SUB-component that owns the body - distinct axes + its OWN pattern-feature collections
    sub_rf, sub_cf = FakeRectFeatures(), FakeCircFeatures()
    sub = MakeComp(name="Sub", construction_axes=("SUB_X", "SUB_Y", "SUB_Z"))
    sub.features = types.SimpleNamespace(rectangularPatternFeatures=sub_rf,
                                         circularPatternFeatures=sub_cf)
    body = BRepBody("SubBoss", parent_component=sub)
    # root carries DIFFERENT axes so a mistaken root build would be detectable
    root_rf, root_cf = FakeRectFeatures(), FakeCircFeatures()
    root = MakeComp(name="Root", construction_axes=("ROOT_X", "ROOT_Y", "ROOT_Z"))
    root.features = types.SimpleNamespace(rectangularPatternFeatures=root_rf,
                                          circularPatternFeatures=root_cf)
    design = MakeDesign(comp=root)
    design.activeComponent = MakeComp(name="Holder", bodies=[body])
    install(pt, design)
    return sub_rf, sub_cf, root_rf, root_cf


def _linear_edge():
    return BRepEdge(curve=Line3D(start=FakePoint(0, 0, 0), end=FakePoint(1, 0, 0)))


def _planar_face():
    return BRepFace(Plane(normal=FakeVector3D(0, 0, 1)))


def _place(root, component, *full_paths):
    """Place `component` in the assembly under the given occurrence fullPathNames."""
    root.occurrences_by_component[_component_key(component)] = [
        make_occurrence(p) for p in full_paths]


def _other_component(name="Rail"):
    return MakeComp(name=name, entity_token="TOKEN:" + name)


def _install_with_axis_handles(handle_map=None, axes=()):
    """_install plus token resolution and a component that owns construction axes (the two paths
    AxisRef adds beyond a world key).

    A construction axis reaches this tool as a NAMED entity it passes straight to createInput, so
    conftest's name-only stand-in carries it - bound as the ConstructionAxis type AxisRef
    isinstance-checks (a bare Mock attribute is not a type)."""
    rf, cf = _install(["Spoke:1"])
    import adsk.fusion
    adsk.fusion.BRepEdge = BRepEdge
    adsk.fusion.BRepFace = BRepFace
    adsk.fusion.SketchLine = type("SL", (), {})
    adsk.fusion.ConstructionAxis = _SimpleNamed
    design = pt.app.activeProduct
    design.findEntityByToken = lambda t, m=dict(handle_map or {}): ([m[t]] if t in m else [])
    design.rootComponent.constructionAxes = _NamedCollection(list(axes))
    return rf, cf


def _cylindrical_face():
    return BRepFace(Cylinder(FakeVector3D(0, 0, 1)))


class TestCircular:

    def test_basic_full_ring(self):
        _, cf = _install(["Spoke:1"])
        out = payload(pt.handler(occurrences="Spoke:1", quantity=6,
                                           total_angle_deg=360, axis="z"))
        assert out["quantity"] == 6
        inp = cf.last_input
        assert inp.axis == "AXIS_Z"
        assert inp.quantity == ("real", 6)
        assert inp.totalAngle == ("str", "360.0 deg")

    def test_axis_selection(self):
        _, cf = _install(["Spoke:1"])
        payload(pt.handler(occurrences="Spoke:1", quantity=4, axis="y"))
        assert cf.last_input.axis == "AXIS_Y"

    def test_symmetric_flag(self):
        _, cf = _install(["Spoke:1"])
        payload(pt.handler(occurrences="Spoke:1", quantity=4, symmetric=True))
        assert cf.last_input.isSymmetric is True
        assert cf.add_calls == 1

    def test_a_symmetric_the_platform_drops_refuses_before_the_pattern_runs(self):
        # A SWIG proxy ACCEPTS an assignment it then ignores, with no exception - and the only
        # read-back a pattern has (patternElements.count) is IDENTICAL for a symmetric and an
        # asymmetric spread, so nothing downstream would ever catch it.
        _, cf = _install(["Spoke:1"], circ_ignores=("isSymmetric",))
        res = pt.handler(occurrences="Spoke:1", quantity=4, symmetric=True)
        assert res["isError"] is True
        assert "symmetric" in res["message"] and "No pattern was created" in res["message"]
        assert cf.add_calls == 0

    def test_quantity_must_be_at_least_two(self):
        _install(["Spoke:1"])
        res = pt.handler(occurrences="Spoke:1", quantity=1)
        assert res["isError"] is True and "quantity must be >= 2" in res["message"]

    def test_unknown_axis(self):
        # AxisRef owns the refusal now - it names every accepted form, not just x/y/z.
        _install(["Spoke:1"])
        res = pt.handler(occurrences="Spoke:1", quantity=4, axis="w")
        assert res["isError"] is True
        assert "'axis': 'w' is not a world axis" in res["message"]

    def test_partial_arc_angle_string(self):
        _, cf = _install(["Spoke:1"])
        out = payload(pt.handler(occurrences="Spoke:1", quantity=3, total_angle_deg=90))
        # the angle is formatted as a "<float> deg" ValueInput string and echoed in payload
        assert cf.last_input.totalAngle == ("str", "90.0 deg")
        assert out["total_angle_deg"] == 90.0

    def test_symmetric_defaults_false(self):
        _, cf = _install(["Spoke:1"])
        out = payload(pt.handler(occurrences="Spoke:1", quantity=4))
        assert cf.last_input.isSymmetric is False
        assert out["symmetric"] is False


class TestInstanceCountReadBack:
    """The count is read off the CREATED feature, never echoed from the request."""

    def test_a_feature_reporting_fewer_instances_is_an_error_naming_both_counts(self):
        _, cf = _install(["Spoke:1"], circ_elements=3)
        res = pt.handler(occurrences="Spoke:1", quantity=6)
        assert res["isError"] is True
        assert "created 3 instances but 6 were requested" in res["message"]
        assert "design_delete_feature" in res["message"]

    def test_a_matching_count_is_reported_from_the_feature(self):
        _install(["Spoke:1"], circ_elements=6)
        assert payload(pt.handler(occurrences="Spoke:1", quantity=6))["quantity"] == 6


class TestBodyTargets:

    def test_circular_patterns_bodies_by_handle(self):
        h = "/v" + "B" * 70
        rf, _ = _install_with_bodies({h: BRepBody("FromHandle")})
        out = payload(pt.handler(bodies=h, quantity=4))
        assert out["entity_kind"] == "bodies"
        assert out["entities"] == ["FromHandle"]


class TestBodyOwningComponent:

    def test_circular_builds_on_bodys_parent_component(self):
        sub_rf, sub_cf, root_rf, root_cf = _install_body_in_subcomponent()
        out = payload(pt.handler(bodies="SubBoss", quantity=6, axis="y"))
        assert out["entity_kind"] == "bodies"
        # the feature was created on the SUB-component (axis from the sub, not root)
        assert sub_cf.last_input is not None and sub_cf.last_input.axis == "SUB_Y"
        assert root_cf.last_input is None          # root must NOT be used


class TestCircularAxisFromGeometry:

    def test_cylindrical_face_handle_reaches_createinput_as_the_face(self):
        f = _cylindrical_face()
        _, cf = _install_with_axis_handles({"CYL": f})
        out = payload(pt.handler(occurrences="Spoke:1", quantity=5, axis="CYL"))
        assert cf.last_input.axis is f            # the FACE, not the component's origin axis
        assert out["axis"] == "BRepFace"

    def test_straight_edge_handle_reaches_createinput(self):
        e = _linear_edge()
        _, cf = _install_with_axis_handles({"E": e})
        out = payload(pt.handler(occurrences="Spoke:1", quantity=3, axis="E"))
        assert cf.last_input.axis is e
        assert out["axis"] == "BRepEdge"

    def test_construction_axis_by_name(self):
        ax = _SimpleNamed("WheelAxis")
        _, cf = _install_with_axis_handles(axes=[ax])
        out = payload(pt.handler(occurrences="Spoke:1", quantity=5, axis="WheelAxis"))
        assert cf.last_input.axis is ax
        assert out["axis"] == "WheelAxis"         # the datum's NAME, never the raw input token

    def test_ambiguous_construction_axis_name_is_refused(self):
        _, cf = _install_with_axis_handles(
            axes=[_SimpleNamed("Hinge"), _SimpleNamed("Hinge")])
        res = pt.handler(occurrences="Spoke:1", quantity=5, axis="Hinge")
        assert res["isError"] is True
        assert "names 2 construction axes" in res["message"]   # refused, not resolved to the first
        assert cf.last_input is None              # refused before any feature transaction opened

    def test_planar_face_handle_is_refused(self):
        _, cf = _install_with_axis_handles({"F": _planar_face()})
        res = pt.handler(occurrences="Spoke:1", quantity=5, axis="F")
        assert res["isError"] is True and "cylindrical" in res["message"]
        assert cf.last_input is None

    def test_world_keys_still_resolve_to_the_origin_construction_axis(self):
        # regression: the world x/y/z keys must keep working exactly as they did.
        _, cf = _install_with_axis_handles()
        out = payload(pt.handler(occurrences="Spoke:1", quantity=4, axis="y"))
        assert cf.last_input.axis == "AXIS_Y" and out["axis"] == "y"

    def test_blank_axis_uses_the_kinds_default(self):
        _, cf = _install_with_axis_handles()
        out = payload(pt.handler(occurrences="Spoke:1", quantity=4, axis=""))
        assert cf.last_input.axis == "AXIS_Z" and out["axis"] == "z"

    def test_a_foreign_construction_axis_is_proxied_via_its_component_not_its_parent(self):
        # A datum's .parent is a BASE FEATURE when the datum is non-parametric in a parametric
        # design; .component always answers the owning component, and the proxy decision hangs on
        # getting that owner right - read from .parent here, the axis would be judged against a
        # base feature and refused as "not placed in the assembly".
        rail = _other_component()
        ax = _SimpleNamed("Hinge")
        ax.component = rail
        ax.parent = types.SimpleNamespace(name="BaseFeature1")
        proxy = _SimpleNamed("Hinge")
        ax.createForAssemblyContext = lambda occ, p=proxy: p
        _, cf = _install_with_axis_handles({"CA": ax})
        _place(pt.app.activeProduct.rootComponent, rail, "Assy:1+Rail:1")
        payload(pt.handler(occurrences="Spoke:1", quantity=4, axis="CA"))
        assert cf.last_input.axis is proxy      # the PROXY reaches createInput, not the native
