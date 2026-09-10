"""Unit tests for model_pattern_rectangular.py - the grid, its directions and its counts."""

import types
from conftest import (BRepBody, BRepEdge, BRepFace, Circle3D, FakePoint, FakeVector3D, Line3D,
                      MakeComp, MakeDesign, Plane, _NamedCollection, entity_proxy, install,
                      load_tool, make_occurrence, payload)

pt = load_tool("model_pattern_rectangular")


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
    """`elements` is how many patternElements the created feature reports; None means the product of
    the two requested quantities, which is what a grid that fully built answers."""
    def __init__(self, refuse_two=False, elements=None):
        self.last_input = None
        self.add_calls = 0
        self.comp = None                 # the component this collection hangs off (set by its owner)
        self._refuse_two = refuse_two
        self.elements = elements

    def createInput(self, coll, d1, q1, dist1, dist_type):
        self.last_input = FakeRectInput(coll, d1, q1, dist1, dist_type, self._refuse_two)
        return self.last_input

    def add(self, inp):
        self.add_calls += 1
        for d in (inp.d1, inp.dir_two[0] if inp.dir_two else None):
            msg = _add_refusal(d, self.comp)
            if msg:
                raise RuntimeError(msg)
        n = self.elements if self.elements is not None else _asked(inp.q1) * _asked(
            inp.dir_two[1] if inp.dir_two else None)
        return types.SimpleNamespace(name="R-Pattern1",
                                     patternElements=_NamedCollection([None] * n))


def _asked(value_input):
    """The integer a ('real', n) ValueInput stand-in carries; 1 where no direction was set."""
    return int(value_input[1]) if isinstance(value_input, tuple) else 1


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
    def __init__(self, ignores=()):
        self.last_input = None
        self.ignores = ignores
        self.add_calls = 0

    def createInput(self, coll, axis):
        self.last_input = FakeCircInput(coll, axis, self.ignores)
        return self.last_input

    def add(self, inp):
        self.add_calls += 1
        return type("F", (), {"name": "C-Pattern1"})()


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


def _install(occ_names, refuse_two=False, circ_ignores=(), rect_elements=None):
    rf, cf = FakeRectFeatures(refuse_two, rect_elements), FakeCircFeatures(circ_ignores)
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


def _linear_edge(proxy=BRepEdge._UNSET):
    return BRepEdge(curve=Line3D(start=FakePoint(0, 0, 0), end=FakePoint(1, 0, 0)),
                    assembly_proxy=proxy)


def _curved_edge():
    return BRepEdge(curve=Circle3D(normal=FakeVector3D(0, 0, 1)))


def _planar_face():
    return BRepFace(Plane(normal=FakeVector3D(0, 0, 1)))


def _install_with_direction_handles(handle_map):
    """_install plus handle resolution for a DIRECTION (AxisRef routes through findEntityByToken).

    The entity classes must be REAL classes: AxisRef isinstance-checks BRepEdge/SketchLine/BRepFace,
    and a bare Mock attribute is not a type, so isinstance would raise rather than answer False."""
    rf, cf = _install(["Block:1"])
    import adsk.fusion
    adsk.fusion.BRepEdge = BRepEdge
    adsk.fusion.BRepFace = BRepFace
    adsk.fusion.SketchLine = type("SL", (), {})
    design = pt.app.activeProduct
    design.findEntityByToken = lambda t, m=handle_map: ([m[t]] if t in m else [])
    return rf, cf


def _edge_owned_by(component, context=None, proxy=BRepEdge._UNSET):
    """A straight edge whose BODY belongs to `component`. context=None is a NATIVE entity; `proxy`
    is what its createForAssemblyContext hands back."""
    edge = _linear_edge(proxy=proxy)
    edge.assemblyContext = context
    edge.body = BRepBody(parent_component=component)
    return edge


def _foreign_edge(component):
    """(native edge owned by `component`, the proxy its createForAssemblyContext hands back)."""
    proxy = _edge_owned_by(component, context="ASSEMBLY-CONTEXT")
    return _edge_owned_by(component, proxy=proxy), proxy


def _place(root, component, *full_paths):
    """Place `component` in the assembly under the given occurrence fullPathNames."""
    root.occurrences_by_component[_component_key(component)] = [
        make_occurrence(p) for p in full_paths]


def _other_component(name="Rail"):
    return MakeComp(name=name, entity_token="TOKEN:" + name)


class TestResolution:

    def test_exact_name(self):
        rf, _ = _install(["Block:1", "Other:1"])
        out = payload(pt.handler(occurrences="Block:1", quantity_one=2, spacing_one=10))
        assert out["entities"] == ["Block:1"]

    def test_substring_fallback(self):
        rf, _ = _install(["Block:1"])
        out = payload(pt.handler(occurrences="block", quantity_one=2, spacing_one=10))
        assert out["entities"] == ["Block:1"]

    def test_missing_reported(self):
        _install(["Block:1"])
        res = pt.handler(occurrences="Ghost", quantity_one=2, spacing_one=10)
        assert res["isError"] is True and "no occurrence matching" in res["message"].lower()

    def test_comma_separated_multiple(self):
        _install(["A:1", "B:1"])
        out = payload(pt.handler(occurrences="A:1, B:1", quantity_one=2, spacing_one=10))
        assert set(out["entities"]) == {"A:1", "B:1"}


class TestRectangular:

    def test_single_direction_scales_spacing(self):
        rf, _ = _install(["Block:1"])
        out = payload(pt.handler(occurrences="Block:1", quantity_one=3,
                                              spacing_one=30, direction_one="x", units="mm"))
        assert out["total_instances"] == 3
        inp = rf.last_input
        assert inp.d1 == "AXIS_X"
        assert inp.q1 == ("real", 3)
        assert inp.dist1[0] == "real" and abs(inp.dist1[1] - 3.0) < 1e-9   # 30 mm -> 3 cm
        # Direction two is ALWAYS set explicitly to quantity 1 for a single row: a fresh createInput
        # carries a UI-style quantityTwo=3 default, so leaving it unset silently TRIPLES the pattern
        # (verified live: quantity_one=2 with dir-two unset produced 6 coincident instances).
        assert inp.dir_two is not None
        assert inp.dir_two[1] == ("real", 1)     # quantityTwo pinned to 1, never the API default
        assert inp.dist_type == "Spacing"

    def test_two_directions(self):
        rf, _ = _install(["Block:1"])
        out = payload(pt.handler(occurrences="Block:1", quantity_one=3, spacing_one=30,
                                              direction_one="x", quantity_two=2, spacing_two=20,
                                              direction_two="y", units="mm"))
        assert out["total_instances"] == 6
        d2, q2, dist2 = rf.last_input.dir_two
        assert d2 == "AXIS_Y"
        assert q2 == ("real", 2)
        assert dist2[0] == "real" and abs(dist2[1] - 2.0) < 1e-9   # 20 mm -> 2 cm

    def test_quantity_one_must_be_positive(self):
        _install(["Block:1"])
        res = pt.handler(occurrences="Block:1", quantity_one=0, spacing_one=10)
        assert res["isError"] is True and "quantity_one must be >= 1" in res["message"]

    def test_unknown_direction(self):
        _install(["Block:1"])
        res = pt.handler(occurrences="Block:1", quantity_one=2, spacing_one=10,
                                     direction_one="w")
        # Not a world axis and not a resolvable handle: the AxisRef kind names both accepted forms.
        assert res["isError"] is True
        assert "'direction_one': 'w' is not a world axis" in res["message"]
        # entity_only refuses a face, so the miss text must not advertise a face handle either.
        assert "face" not in res["message"]

    def test_unknown_units(self):
        _install(["Block:1"])
        res = pt.handler(occurrences="Block:1", quantity_one=2, spacing_one=10,
                                     units="furlong")
        assert res["isError"] is True and "Unknown units" in res["message"]

    def test_unknown_direction_two_errors(self):
        # quantity_two>1 forces direction_two resolution; a bad axis must error
        _install(["Block:1"])
        res = pt.handler(occurrences="Block:1", quantity_one=2, spacing_one=10,
                                     quantity_two=2, spacing_two=5, direction_two="w")
        assert res["isError"] is True
        assert "'direction_two': 'w' is not a world axis" in res["message"]

    def test_single_row_direction_two_none_in_payload(self):
        _install(["Block:1"])
        out = payload(pt.handler(occurrences="Block:1", quantity_one=4, spacing_one=10,
                                              quantity_two=1))
        # quantity_two==1 -> direction_two reported as None, total = quantity_one
        assert out["direction_two"] is None
        assert out["total_instances"] == 4

    def test_spacing_scaled_inches(self):
        rf, _ = _install(["Block:1"])
        payload(pt.handler(occurrences="Block:1", quantity_one=2, spacing_one=1,
                                        units="in"))
        # 1in -> 2.54cm
        assert abs(rf.last_input.dist1[1] - 2.54) < 1e-9


class TestInstanceCountReadBack:
    """The count is read off the CREATED feature, never echoed from the request."""

    def test_a_feature_reporting_fewer_instances_is_an_error_naming_both_counts(self):
        _install(["Spoke:1"], rect_elements=4)
        res = pt.handler(occurrences="Spoke:1", quantity_one=3, spacing_one=10,
                         quantity_two=2, spacing_two=10)
        assert res["isError"] is True
        assert "created 4 instances but 6 were requested (3 x 2)" in res["message"]
        assert "design_delete_feature" in res["message"]

    def test_a_matching_count_is_reported_from_the_feature(self):
        _install(["Spoke:1"], rect_elements=6)
        out = payload(pt.handler(occurrences="Spoke:1", quantity_one=3, spacing_one=10,
                                 quantity_two=2, spacing_two=10))
        assert out["quantity_one"] == 3 and out["quantity_two"] == 2


class TestBodyTargets:

    def test_rectangular_patterns_bodies_by_name(self):
        rf, _ = _install_with_bodies({"Boss": BRepBody("Boss")})
        out = payload(pt.handler(bodies="Boss", quantity_one=3, spacing_one=10))
        assert out["entity_kind"] == "bodies"
        assert out["entities"] == ["Boss"]
        assert out["total_instances"] == 3

    def test_bodies_take_precedence_over_occurrences(self):
        rf, _ = _install_with_bodies({"Boss": BRepBody("Boss")})
        out = payload(pt.handler(occurrences="ignored", bodies="Boss",
                                              quantity_one=2, spacing_one=5))
        assert out["entity_kind"] == "bodies" and out["entities"] == ["Boss"]

    def test_bad_body_name_errors(self):
        _install_with_bodies({"Boss": BRepBody("Boss")})
        res = pt.handler(bodies="Nope", quantity_one=2, spacing_one=5)
        assert res["isError"] is True and "Nope" in res["message"]


class TestBodyOwningComponent:

    def test_rectangular_builds_on_bodys_parent_component(self):
        sub_rf, sub_cf, root_rf, root_cf = _install_body_in_subcomponent()
        payload(pt.handler(bodies="SubBoss", quantity_one=3, spacing_one=10,
                                        direction_one="x"))
        assert sub_rf.last_input is not None and sub_rf.last_input.d1 == "SUB_X"
        assert root_rf.last_input is None


class TestDirectionFromGeometry:

    def test_direction_one_takes_an_edge_handle(self):
        e = _linear_edge()
        rf, _ = _install_with_direction_handles({"E1": e})
        out = payload(pt.handler(occurrences="Block:1", quantity_one=3,
                                              spacing_one=10, direction_one="E1"))
        # the EDGE itself reaches createInput - not the component's construction axis
        assert rf.last_input.d1 is e
        assert out["direction_one"] == "BRepEdge"

    def test_direction_two_takes_an_edge_handle(self):
        e = _linear_edge()
        rf, _ = _install_with_direction_handles({"E2": e})
        out = payload(pt.handler(occurrences="Block:1", quantity_one=2, spacing_one=10,
                                              quantity_two=2, spacing_two=5, direction_two="E2"))
        d2, _q2, _dist2 = rf.last_input.dir_two
        assert d2 is e
        assert out["direction_two"] == "BRepEdge"

    def test_world_axis_still_resolves_to_the_construction_axis(self):
        rf, _ = _install_with_direction_handles({})
        out = payload(pt.handler(occurrences="Block:1", quantity_one=2,
                                              spacing_one=10, direction_one="y"))
        assert rf.last_input.d1 == "AXIS_Y"
        assert out["direction_one"] == "y"

    def test_blank_direction_uses_the_kinds_default_axis(self):
        rf, _ = _install_with_direction_handles({})
        out = payload(pt.handler(occurrences="Block:1", quantity_one=2,
                                              spacing_one=10, direction_one=""))
        assert rf.last_input.d1 == "AXIS_X"          # AxisRef default 'x', not a blank lookup
        assert out["direction_one"] == "x"

    def test_curved_edge_handle_refused(self):
        rf, _ = _install_with_direction_handles({"ARC": _curved_edge()})
        res = pt.handler(occurrences="Block:1", quantity_one=2, spacing_one=10,
                                     direction_one="ARC")
        assert res["isError"] is True and "not straight" in res["message"]
        assert rf.last_input is None                 # refused before any feature transaction opened

    def test_face_handle_refused_naming_what_it_is(self):
        rf, _ = _install_with_direction_handles({"F": _planar_face()})
        res = pt.handler(occurrences="Block:1", quantity_one=2, spacing_one=10,
                                     direction_one="F")
        assert res["isError"] is True
        assert "direction VECTOR" in res["message"] and "linear ENTITY" in res["message"]
        assert rf.last_input is None

    def test_bad_direction_two_is_refused_before_createinput(self):
        # direction_two is ALWAYS set, so it must be resolved before the transaction starts -
        # otherwise a bad value aborts a half-built pattern input.
        rf, _ = _install_with_direction_handles({})
        res = pt.handler(occurrences="Block:1", quantity_one=2, spacing_one=10,
                                     quantity_two=2, spacing_two=5, direction_two="nope")
        assert res["isError"] is True
        assert rf.last_input is None

    def test_refused_second_direction_stops_before_add(self):
        # setDirectionTwo ANSWERS whether it took; a false must abort, not fall through to add()
        # and report a pattern built on the API's own default second direction.
        rf, _ = _install(["Block:1"], refuse_two=True)
        res = pt.handler(occurrences="Block:1", quantity_one=2, spacing_one=10,
                                     quantity_two=2, spacing_two=5)
        assert res["isError"] is True and "setDirectionTwo returned" in res["message"]
        assert rf.add_calls == 0


class TestDirectionInThePatternsOwnComponent:

    """A direction owned by the pattern's OWN component passes straight through. The owner and the
    pattern's component are DISTINCT wrappers sharing one entityToken - the measured shape (two reads
    of design.rootComponent are different objects). An identity test reads False here, falls through
    to allOccurrencesByComponent, gets 0 for the root, and REFUSES a legal direction while telling
    the caller to pass exactly what they just passed."""

    def test_own_root_edge_passes_through_untouched(self):
        handles = {}
        rf, _ = _install_with_direction_handles(handles)
        root = pt.app.activeProduct.rootComponent
        # the edge's body reports a DIFFERENT wrapper of the same root component
        edge = _edge_owned_by(entity_proxy(root))
        handles["E"] = edge
        out = payload(pt.handler(occurrences="Block:1", quantity_one=2,
                                              spacing_one=10, direction_one="E"))
        assert rf.last_input.d1 is edge           # the NATIVE entity reaches createInput, unproxied
        assert out["direction_one"] == "BRepEdge"

    def test_own_component_edge_is_not_refused_for_being_unplaced(self):
        # An unplaced own-component direction is legal: the "belongs to component '...', which is not
        # placed in the assembly" refusal is for a FOREIGN component's geometry, never for the
        # pattern's own.
        handles = {}
        rf, _ = _install_with_direction_handles(handles)
        root = pt.app.activeProduct.rootComponent
        handles["E"] = _edge_owned_by(entity_proxy(root))
        res = pt.handler(occurrences="Block:1", quantity_one=2, spacing_one=10,
                                     direction_one="E")
        assert res["isError"] is False, res
        assert rf.add_calls == 1


class TestDirectionFromAnotherComponent:

    def test_native_foreign_edge_is_proxied_into_its_single_occurrence(self):
        handles = {}
        rf, _ = _install_with_direction_handles(handles)
        root = pt.app.activeProduct.rootComponent
        rail = _other_component()
        edge, proxy = _foreign_edge(rail)
        _place(root, rail, "Assy:1+Rail:1")
        handles["E"] = edge
        out = payload(pt.handler(occurrences="Block:1", quantity_one=2,
                                              spacing_one=10, direction_one="E"))
        assert rf.last_input.d1 is proxy          # the PROXY reaches createInput, not the native
        assert rf.add_calls == 1                  # add() accepted it; a native one raises
        assert out["direction_one"] == "BRepEdge"

    def test_two_occurrences_refused_naming_each_path(self):
        handles = {}
        rf, _ = _install_with_direction_handles(handles)
        root = pt.app.activeProduct.rootComponent
        rail = _other_component()
        edge, _proxy = _foreign_edge(rail)
        _place(root, rail, "Assy:1+Rail:1", "Assy:1+Rail:2")
        handles["E"] = edge
        res = pt.handler(occurrences="Block:1", quantity_one=2, spacing_one=10,
                                     direction_one="E")
        assert res["isError"] is True
        assert "Assy:1+Rail:1" in res["message"] and "Assy:1+Rail:2" in res["message"]
        assert rf.last_input is None and rf.add_calls == 0

    def test_unplaced_component_direction_refused(self):
        handles = {}
        rf, _ = _install_with_direction_handles(handles)
        rail = _other_component()
        edge, _proxy = _foreign_edge(rail)         # never placed - no occurrence to proxy into
        handles["E"] = edge
        res = pt.handler(occurrences="Block:1", quantity_one=2, spacing_one=10,
                                     direction_one="E")
        assert res["isError"] is True and "not placed in the assembly" in res["message"]
        assert rf.last_input is None

    def test_an_edge_that_will_not_proxy_is_refused_not_handed_over_native(self):
        # The lift can fail in two shapes - a createForAssemblyContext that ANSWERS NOTHING, and an
        # edge carrying no such factory at all - and both are one refusal naming the occurrence.
        # Falling back to the native edge hands add() the very entity it refuses.
        for proxy_kw in ({"proxy": None}, {}):
            handles = {}
            rf, _ = _install_with_direction_handles(handles)
            root = pt.app.activeProduct.rootComponent
            rail = _other_component()
            _place(root, rail, "Assy:1+Rail:1")
            handles["E"] = _edge_owned_by(rail, **proxy_kw)
            res = pt.handler(occurrences="Block:1", quantity_one=2, spacing_one=10,
                             direction_one="E")
            assert res["isError"] is True, (proxy_kw, res)
            assert "could not be brought into the pattern's assembly context (Assy:1+Rail:1)" \
                in res["message"]
            assert "world axis (x/y/z)" in res["message"]
            assert rf.last_input is None and rf.add_calls == 0

    def test_edge_already_in_context_passes_untouched(self):
        handles = {}
        rf, _ = _install_with_direction_handles(handles)
        rail = _other_component()
        handles["E"] = _edge_owned_by(rail, context="ASSEMBLY-CONTEXT")
        payload(pt.handler(occurrences="Block:1", quantity_one=2, spacing_one=10,
                                        direction_one="E"))
        assert rf.last_input.d1 is handles["E"]    # already a proxy - not re-proxied
        assert rf.add_calls == 1

    def test_same_component_edge_passes_untouched(self):
        handles = {}
        rf, _ = _install_with_direction_handles(handles)
        root = pt.app.activeProduct.rootComponent
        handles["E"] = _edge_owned_by(root)        # native, but the pattern's OWN component
        payload(pt.handler(occurrences="Block:1", quantity_one=2, spacing_one=10,
                                        direction_one="E"))
        assert rf.last_input.d1 is handles["E"]
        assert rf.add_calls == 1


class TestZeroSpacingGuards:

    def test_rect_zero_spacing_one_refused(self):
        # spacing_one=0 with quantity>1 stacks every instance on the seed (coincident duplicates
        # reported as a clean pattern) - refused before any feature transaction opens.
        res = pt.handler(bodies="B", quantity_one=3, spacing_one=0)
        assert res["isError"] is True and "spacing_one=0" in res["message"]

    def test_rect_zero_spacing_two_refused(self):
        res = pt.handler(bodies="B", quantity_one=2, spacing_one=5,
                                     quantity_two=2, spacing_two=0)
        assert res["isError"] is True and "spacing_two=0" in res["message"]

    def test_single_row_zero_spacing_two_is_fine_to_pass_the_guard(self, monkeypatch):
        # quantity_two=1 never uses spacing_two - the guard must not refuse it. The absent-design
        # error AFTER the guard is the proof the guard let the call through.
        monkeypatch.setattr(pt._common, "design", lambda: None)
        res = pt.handler(bodies="B", quantity_one=2, spacing_one=5,
                                     quantity_two=1, spacing_two=0)
        assert "spacing_two" not in res.get("message", "")
        assert "No active design" in res["message"]
